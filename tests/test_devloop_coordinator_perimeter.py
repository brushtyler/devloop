"""devloop: the coordinator's perimeter aligned with its role (#595, ADR 0002, precision of
2026-11-23).

With ``[roles.coordinator]`` the coordinator's identity:

- writes ``/devloop capacity`` on ``[report] issue`` as the supervisor does, within the caps of
  the code only, beyond the lower ones of ``[delegations]`` that bind the supervisor (#611), each
  grant counted in ``max_grants_per_day``, in merge mode ``auto``; the delegation is on with the
  table, whatever ``[delegations] capacity`` says;
- grants ``/devloop grant-budget``, the one extension of a story, on a ``budget_grantable``
  escalation and on a ``budget`` escalation to the operator, in merge mode ``auto`` only (#611);
  a second one stays the operator's;
- sets priority labels that count where its ``agent:ready`` counts; its dependency and
  implementer labels still do not;
- opens issues that count as devloop's: the supervisor and the coordinator queue them, but one
  the readiness check declares touching the trust policy goes to ``agent:needs-operator`` until
  a trusted user adds ``agent:ready``.
"""

import dataclasses
import unittest
from pathlib import Path
from unittest import mock

import test_devloop_preflight_recheck as recheck_tests
from devloop import budget, supervision
from devloop.budget import BudgetRefused
from devloop.config import DelegationPolicy, MergePolicy
from devloop.dispatcher import Dispatcher
from devloop.planning import Action, plan, runs_issue_step
from test_devloop import (CFG, SHA2, FakeIdentities, FakeRunner, FakeWS, implemented, pr,
                          raw_review)
from test_devloop_capacity import ISSUE
from test_devloop_capacity import OFF as CAPACITY_OFF
from test_devloop_capacity import ON as CAPACITY_ON
from test_devloop_capacity import REASON as CAPACITY_REASON
from test_devloop_capacity import CapacityCase
from test_devloop_coordinator import COORDINATOR, coordinated, declared
from test_devloop_dependencies import block
from test_devloop_grant_budget import EXTENSION, REASON, BudgetCase
from test_devloop_grant_budget import OFF as BUDGET_OFF
from test_devloop_grant_budget import ON as BUDGET_ON
from test_devloop_preflight import checked
from test_devloop_supervision import HUMAN, REVIEW, SUPER, SUPERVISOR_LOGIN
from test_devloop_supervisor_labels import HIGH, READY, SCFG, QueueGH, added

COMMAND = '/devloop capacity max_open_prs 4 6h ' + CAPACITY_REASON
ISSUE_CFG = coordinated(recheck_tests.CFG)


class PerimeterTests(unittest.TestCase):
    def test_capacity_and_grant_budget_are_delegated(self):
        self.assertIn('capacity', supervision.COORDINATOR_DELEGATED)
        self.assertIn('grant-budget', supervision.COORDINATOR_DELEGATED)
        self.assertEqual(set(supervision.COORDINATOR_RESERVED),
                         {'approve', 'merge-budget', 'reconcile'})
        for command in ('capacity', 'grant-budget'):
            self.assertIsNone(supervision.coordinator_refusal(command))


class CapacityTests(CapacityCase):
    """``/devloop capacity`` of the coordinator on ``[report] issue``."""

    def test_a_grant_within_the_caps_with_the_supervisors_delegation_off(self):
        d = self.make(coordinated(CAPACITY_OFF))
        self.command(COMMAND, login=COORDINATOR)
        cfg = self.planned(d)
        self.assertEqual((cfg.max_open_prs, cfg.max_parked_prs), (4, 3))
        (decision,) = self.decisions()
        self.assertEqual({k: decision[k] for k in ('command', 'by', 'outcome', 'role', 'value',
                                                   'cap', 'daily', 'daily_cap')},
                         {'command': 'capacity', 'by': COORDINATOR, 'outcome': 'granted',
                          'role': 'coordinator', 'value': 4, 'cap': 6, 'daily': 1,
                          'daily_cap': 3})
        (grant,) = d.ledger.journal.grants()  # it counts in max_grants_per_day
        self.assertEqual((grant['delegation'], grant['by']), ('capacity', COORDINATOR))
        # Still in force at the next tick, and spent.
        self.assertEqual(self.planned(d).max_open_prs, 4)
        self.assertEqual(len(self.decisions()), 1)

    def refused(self, cfg, text, why):
        d = self.make(cfg)
        self.command(text, login=COORDINATOR)
        planned = self.planned(d)
        self.assertEqual((planned.max_open_prs, planned.max_parked_prs),
                         (cfg.max_open_prs, cfg.max_parked_prs))
        decision = self.decisions()[-1]
        self.assertEqual((decision['by'], decision['outcome']), (COORDINATOR, 'refused'))
        self.assertIn(why, ' '.join(self.body().split()))
        return d

    def test_beyond_the_caps_it_is_refused(self):
        cfg = coordinated(CAPACITY_OFF)
        for text, why in (
                ('/devloop capacity max_open_prs 7 2h ' + CAPACITY_REASON,
                 '`max_open_prs` va da 1 a 6'),
                ('/devloop capacity max_open_prs 4 25h ' + CAPACITY_REASON,
                 'supera il massimo di 24 ore'),
                ('/devloop capacity max_parallel_actions 3 2h ' + CAPACITY_REASON,
                 '`max_parallel_actions` non è delegata'),
                ('/devloop capacity max_open_prs 4 2h', 'non è ben formato')):
            with self.subTest(text=text[:40]):
                self.refused(cfg, text, why)

    def test_beyond_the_caps_of_the_delegation_within_the_codes(self):
        # #611: the caps of [delegations] bind the supervisor only; the coordinator decides
        # beyond the delegation, within the caps of the code.
        lowered = DelegationPolicy(capacity_max_parked_prs=4, capacity_max_hours=2)
        for delegations in (lowered, dataclasses.replace(lowered, capacity=True)):
            with self.subTest(capacity=delegations.capacity):
                d = self.make(coordinated(dataclasses.replace(CAPACITY_OFF,
                                                              delegations=delegations)))
                self.command('/devloop capacity max_parked_prs 6 24h ' + CAPACITY_REASON,
                             login=COORDINATOR)
                self.assertEqual(self.planned(d).max_parked_prs, 6)
                decision = self.decisions()[-1]
                self.assertEqual({k: decision[k] for k in ('outcome', 'value', 'cap',
                                                           'minutes')},
                                 {'outcome': 'granted', 'value': 6,
                                  'cap': supervision.MAX_CAPACITY['max_parked_prs'],
                                  'minutes': 24 * 60})
                trace = [c['body'] for c in self.gh.threads[ISSUE]
                         if 'tetto del codice: 6' in ' '.join(c['body'].split())]
                self.assertEqual(len(trace), 1)
                # Still applied at the next tick, above the cap of the delegation.
                self.assertEqual(self.planned(d).max_parked_prs, 6)

    def test_beyond_the_caps_of_the_code_with_lower_delegation_caps(self):
        cfg = coordinated(dataclasses.replace(CAPACITY_OFF, delegations=DelegationPolicy(
            capacity_max_parked_prs=4, capacity_max_hours=2)))
        self.refused(cfg, '/devloop capacity max_parked_prs 7 1h ' + CAPACITY_REASON,
                     '`max_parked_prs` va da 1 a 6 (tetto del codice)')
        self.refused(cfg, '/devloop capacity max_parked_prs 5 25h ' + CAPACITY_REASON,
                     'supera il massimo di 24 ore (tetto del codice)')

    def test_the_supervisor_stays_within_the_caps_of_the_delegation(self):
        cfg = coordinated(dataclasses.replace(CAPACITY_ON, delegations=DelegationPolicy(
            capacity=True, capacity_max_parked_prs=4, capacity_max_hours=2)))
        for text, why in (
                ('/devloop capacity max_parked_prs 5 1h ' + CAPACITY_REASON,
                 '`max_parked_prs` va da 1 a 4 (`[delegations] capacity_max_parked_prs`)'),
                ('/devloop capacity max_parked_prs 3 3h ' + CAPACITY_REASON,
                 'supera il massimo di 2 ore (`[delegations] capacity_max_hours`)')):
            with self.subTest(text=text[:40]):
                d = self.make(cfg)
                self.command(text)
                self.assertEqual(self.planned(d).max_parked_prs, cfg.max_parked_prs)
                decision = self.decisions()[-1]
                self.assertEqual((decision['by'], decision['outcome']),
                                 (SUPERVISOR_LOGIN, 'refused'))
                self.assertNotIn('role', decision)
                self.assertIn(why, ' '.join(self.body().split()))

    def test_the_daily_cap_of_the_grants_holds(self):
        cap = supervision.MAX_DAILY_GRANTS
        d = self.make(coordinated(CAPACITY_OFF))
        for n in range(cap):
            d.ledger.journal.grant('rounds', 200 + n, 9000 + n, SUPERVISOR_LOGIN, cap)
        self.command(COMMAND, login=COORDINATOR)
        self.assertEqual(self.planned(d).max_open_prs, 2)
        self.assertEqual(self.decisions()[-1]['outcome'], 'refused')
        self.assertIn('il massimo giornaliero', self.body())
        self.assertEqual(len(d.ledger.journal.grants()), cap)

    def test_merge_mode_auto_is_required(self):
        cfg = coordinated(dataclasses.replace(CAPACITY_OFF, merge=MergePolicy(mode='strict')))
        self.refused(cfg, COMMAND, 'non `auto`')

    def test_its_grant_lasts_while_the_table_is_on(self):
        d = self.make(coordinated(CAPACITY_OFF))
        self.command(COMMAND, login=COORDINATOR)
        self.assertEqual(self.planned(d).max_open_prs, 4)
        # The supervisor's delegation on, the coordinator's table off: its grant stops.
        d.cfg = CAPACITY_ON
        self.assertEqual(self.planned(d).max_open_prs, 2)
        d.cfg = coordinated(CAPACITY_OFF)
        self.assertEqual(self.planned(d).max_open_prs, 4)

    def test_a_supervisors_grant_needs_its_own_delegation(self):
        # The table turns on the coordinator's delegation only.
        d = self.make(coordinated(CAPACITY_OFF))
        self.command(COMMAND)
        self.assertEqual(self.planned(d).max_open_prs, 2)
        self.assertIn('la delega dei limiti di capacità è spenta',
                      ' '.join(self.body().split()))

    def test_a_grant_replaced_is_not_restored_by_a_revocation(self):
        # The latest grant of a key is chosen before its delegation is checked: revoking it
        # gives the configured value back, never an earlier grant it replaced.
        both = coordinated(CAPACITY_ON)
        for first, second, revoked in ((None, COORDINATOR, CAPACITY_ON),
                                       (COORDINATOR, None, coordinated(CAPACITY_OFF))):
            with self.subTest(first=first, second=second):
                d = self.make(both)
                self.command('/devloop capacity max_open_prs 6 6h ' + CAPACITY_REASON,
                             **({'login': first} if first else {}))
                self.assertEqual(self.planned(d).max_open_prs, 6)
                self.command('/devloop capacity max_open_prs 1 6h ' + CAPACITY_REASON,
                             **({'login': second} if second else {}))
                self.assertEqual(self.planned(d).max_open_prs, 1)
                d.cfg = revoked
                self.assertEqual(self.planned(d).max_open_prs, 2)

    def test_off_it_is_ignored(self):
        d = self.make(declared(CAPACITY_ON))
        self.command(COMMAND, login=COORDINATOR)
        self.assertEqual(self.planned(d).max_open_prs, 2)
        self.assertEqual(self.decisions(), [])
        self.assertTrue(any(f'ignored `/devloop capacity` by {COORDINATOR}' in line
                            for line in self.logs))

    def test_merge_budget_stays_the_operators(self):
        d = self.make(coordinated(CAPACITY_ON))
        self.command('/devloop merge-budget 3 motivo', login=COORDINATOR)
        self.planned(d)
        self.assertEqual(self.decisions(), [])
        self.assertTrue(any(f'ignored `/devloop merge-budget` by {COORDINATOR}' in line
                            for line in self.logs))


class GrantBudgetTests(BudgetCase):
    """``/devloop grant-budget`` of the coordinator: the one extension of a story."""

    def grant(self, d, number=100):
        self.gh.command(number, '/devloop grant-budget ' + REASON, login=COORDINATOR)
        self.assertEqual(self.tick(d), [('supervise', number)])
        return self.decisions(number)[-1]

    def assert_granted(self, decision, cls):
        self.assertEqual({k: decision[k] for k in ('by', 'outcome', 'class', 'role', 'story',
                                                   'extension', 'daily', 'daily_cap')},
                         {'by': COORDINATOR, 'outcome': 'granted', 'class': cls,
                          'role': 'coordinator', 'story': self.story, 'extension': EXTENSION,
                          'daily': 1, 'daily_cap': 3})

    def test_on_a_budget_grantable_escalation(self):
        d = self.make(coordinated(BUDGET_ON), raw_review())
        self.spend(d, 4)
        self.assertEqual(self.refuse_review(d)['class'], 'budget_grantable')
        self.assert_granted(self.grant(d), 'budget_grantable')
        self.assertEqual(self.gh.labels[100], {REVIEW})
        self.assertEqual(self.tick(d), [('review', 100)])

    def test_on_a_budget_escalation_to_the_operator(self):
        # [delegations] budget off: the refusal goes to the operator as budget, with what the
        # extension needs; the coordinator grants it within the same caps.
        d = self.make(coordinated(BUDGET_OFF), raw_review())
        self.spend(d, 4)
        record = self.refuse_review(d)
        self.assertEqual({k: record[k] for k in ('class', 'to', 'story', 'kind', 'agent',
                                                 'step')},
                         {'class': 'budget', 'to': 'operator', 'story': self.story,
                          'kind': 'review', 'agent': CFG.reviewer, 'step': 'review'})
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assert_granted(self.grant(d), 'budget')
        self.assertEqual(self.gh.labels[100], {REVIEW})
        journal = d.ledger.journal
        self.assertEqual(journal.extension(self.story)['by'], COORDINATOR)
        (grant,) = journal.grants()
        self.assertEqual((grant['delegation'], grant['number']), ('budget', 100))
        events = [r for r in self.gh.records(100, 'ledger')
                  if r.get('event') == 'delegation_grant']
        self.assertEqual([(e['delegation'], e['by']) for e in events], [('budget', COORDINATOR)])
        # The review runs in the extended envelope, and the command is spent.
        self.assertEqual(self.tick(d), [('review', 100)])
        self.assertEqual(len(self.decisions(100)), 1)

    def test_a_second_extension_stays_the_operators(self):
        d = self.make(coordinated(BUDGET_OFF), raw_review())
        self.spend(d, 4)
        self.refuse_review(d)
        self.grant(d)
        self.assertEqual(self.tick(d), [('review', 100)])
        reviewer = d.cfg.agent(d.cfg.reviewer)
        with self.assertRaises(BudgetRefused) as caught:
            budget.reserve(d.cfg.budget, d.ledger.journal, repo=CFG.repo, issue=7,
                           kind='review', agent=reviewer)
        d._escalate_failure(Action('review', 100), pr(), caught.exception)
        self.assertEqual(self.escalations(100)[-1]['class'], 'budget')
        decision = self.grant(d)
        self.assertEqual((decision['by'], decision['outcome']), (COORDINATOR, 'refused'))
        self.assertIn('unica estensione', self.body())
        self.assertIn(HUMAN, self.gh.labels[100])
        self.assertEqual(len(d.ledger.journal.grants()), 1)

    def test_the_daily_cap_of_the_grants_holds(self):
        d = self.make(coordinated(BUDGET_OFF))
        cap = supervision.MAX_DAILY_GRANTS
        for n in range(cap):
            d.ledger.journal.grant('rounds', 200 + n, 9000 + n, SUPERVISOR_LOGIN, cap)
        self.spend(d, 4)
        self.refuse_review(d)
        decision = self.grant(d)
        self.assertEqual(decision['outcome'], 'refused')
        self.assertIn('il massimo giornaliero', self.body())
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertIsNone(d.ledger.journal.extension(self.story))

    def test_a_moved_head_is_refused(self):
        d = self.make(coordinated(BUDGET_OFF))
        self.spend(d, 4)
        self.refuse_review(d)
        self.gh.head = SHA2
        decision = self.grant(d)
        self.assertEqual(decision['outcome'], 'refused')
        self.assertIn('input superato', self.body())
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertIsNone(d.ledger.journal.extension(self.story))

    def test_an_escalation_without_the_story_is_refused(self):
        # A budget escalation written before #595 records no story nor agent.
        d = self.make(coordinated(BUDGET_OFF))
        self.spend(d, 4)
        self.refuse_review(d)
        with mock.patch('devloop.dispatch_supervision.budget_fields', return_value={}):
            decision = self.grant(d)
        self.assertEqual(decision['outcome'], 'refused')
        self.assertIn('precedente a #595', self.body())
        self.assertIsNone(d.ledger.journal.extension(self.story))

    def test_merge_mode_auto_is_required(self):
        # #611: as the coordinator's capacity, its grant-budget applies only in auto.
        d = self.make(coordinated(BUDGET_OFF), raw_review())
        self.spend(d, 4)
        self.assertEqual(self.refuse_review(d)['class'], 'budget')
        for mode in ('strict', 'off'):
            with self.subTest(mode=mode):
                d.cfg = dataclasses.replace(d.cfg, merge=MergePolicy(mode=mode))
                decision = self.grant(d)
                self.assertEqual((decision['by'], decision['outcome']),
                                 (COORDINATOR, 'refused'))
                self.assertIn(f'la modalità di merge è `{mode}`, non `auto`',
                              ' '.join(self.body().split()))
                self.assertEqual(self.gh.labels[100], {HUMAN})
                self.assertIsNone(d.ledger.journal.extension(self.story))
                self.assertEqual(d.ledger.journal.grants(), [])

    def test_the_operators_command_still_does_not_extend(self):
        d = self.make(coordinated(BUDGET_OFF))
        self.spend(d, 4)
        self.refuse_review(d)
        self.gh.command(100, '/devloop grant-budget ' + REASON, login='brushtyler')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('delega del supervisore', self.body())
        self.assertIsNone(d.ledger.journal.extension(self.story))

    def test_off_it_is_ignored(self):
        d = self.make(declared(BUDGET_OFF))
        self.spend(d, 4)
        self.refuse_review(d)
        self.gh.command(100, '/devloop grant-budget ' + REASON, login=COORDINATOR)
        self.assertEqual(self.tick(d), [])
        self.assertEqual(self.decisions(100), [])


class IssueGrantBudgetTests(BudgetCase):
    """The coordinator's ``/devloop grant-budget`` on an issue's ``budget`` escalation to the
    operator runs the refused step, as the step itself would: it waits while the issue is
    blocked and takes a free PR slot."""

    def escalate(self):
        d = self.make(coordinated(BUDGET_OFF), implemented(), has_pr=False)
        self.ws.commit_tree = lambda path, commit: 'e' * 40
        self.spend(d, 4, kind='implement', agent=CFG.implementer)
        self.assertEqual(self.tick(d), [('implement', 7)])
        record = self.escalations(7)[-1]
        self.assertEqual((record['class'], record['to'], record['step']),
                         ('budget', 'operator', 'implement'))
        self.assertIn(HUMAN, self.gh.labels[7])
        return d

    def assert_nothing_spent(self, d):
        self.assertIsNone(d.ledger.journal.extension(self.story))
        self.assertEqual(d.ledger.journal.grants(), [])
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.gh.created, [])

    def test_the_step_runs(self):
        d = self.escalate()
        self.gh.command(7, '/devloop grant-budget ' + REASON, login=COORDINATOR)
        self.assertTrue(runs_issue_step(d.snapshot().supervised[0]))
        self.assertEqual(self.tick(d), [('supervise', 7)])
        decision = self.decisions(7)[-1]
        self.assertEqual((decision['by'], decision['outcome'], decision['class']),
                         (COORDINATOR, 'granted', 'budget'))
        # The implementation runs at once, within the extended envelope.
        self.assertEqual(len(self.runner.calls), 1)
        self.assertNotIn(HUMAN, self.gh.labels[7])

    def test_a_blocked_issue_waits_and_takes_no_slot(self):
        d = self.escalate()
        pair = block(1)
        self.gh.issue_dependencies = lambda n: (
            {'author': 'brushtyler', 'blocked_by': [dict(pair[0])], 'added': [dict(pair[1])]}
            if n == 7 else {'author': 'brushtyler', 'blocked_by': [], 'added': []})
        self.gh.command(7, '/devloop grant-budget ' + REASON, login=COORDINATOR)
        snap = d.snapshot()
        self.assertEqual(snap.supervised[0]['blocked_by'], ['#1'])
        self.assertEqual(plan(snap, d.cfg), [])
        self.assertEqual(self.tick(d), [])
        self.assertIn('  issue #7: the extension waits, blocked by #1', self.logs)
        self.assertEqual(self.decisions(7), [])
        self.assertIn(HUMAN, self.gh.labels[7])
        self.assert_nothing_spent(d)

    def test_the_grant_takes_a_free_pr_slot(self):
        d = self.escalate()
        self.gh.command(7, '/devloop grant-budget ' + REASON, login=COORDINATOR)
        snap = d.snapshot()
        snap.ready_issues.append({'number': 8, 'title': 'Issue 8', 'labels': {READY},
                                  'labeler': 'brushtyler', 'admitted': True, 'merged': [],
                                  'blocked_by': []})
        one = dataclasses.replace(d.cfg, max_open_prs=1)
        self.assertEqual([(a.kind, a.number) for a in plan(snap, one)], [('supervise', 7)])
        self.assertEqual(plan(snap, dataclasses.replace(d.cfg, max_open_prs=0)), [])
        self.assertEqual(plan(snap, dataclasses.replace(d.cfg, max_parked_prs=0)), [])

    def test_the_operators_command_is_only_answered(self):
        # A trusted user's grant-budget on a budget escalation runs nothing: it takes no slot
        # and does not wait on the blocks.
        d = self.escalate()
        self.gh.command(7, '/devloop grant-budget ' + REASON, login='brushtyler')
        (item,) = d.snapshot().supervised
        self.assertFalse(item['coordinator'])
        self.assertFalse(runs_issue_step(item))
        self.assertNotIn('blocked_by', item)
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.decisions(7)[-1]['outcome'], 'refused')
        self.assertIn(HUMAN, self.gh.labels[7])
        self.assert_nothing_spent(d)


class PriorityTests(unittest.TestCase):
    """The coordinator's priority labels count where its ``agent:ready`` counts."""

    def make(self, issues, cfg):
        self.gh = QueueGH(issues)
        return Dispatcher(cfg, self.gh, FakeWS(), FakeRunner(), log=lambda m: None,
                          identities=FakeIdentities(self.gh))

    def priorities(self, cfg):
        d = self.make({
            1: ('brushtyler', {READY}, [added(READY, 1, by='brushtyler')]),
            2: ('brushtyler', {READY, HIGH},
                [added(READY, 2, by='brushtyler'), added(HIGH, 3, by=COORDINATOR)]),
            # Never on a stranger's issue, as for its agent:ready.
            3: ('stranger', {READY, HIGH},
                [added(READY, 1, by='brushtyler'), added(HIGH, 3, by=COORDINATOR)]),
        }, cfg)
        snap = d.snapshot()
        return ({i['number']: i['priority'] for i in snap.ready_issues},
                [a.number for a in plan(snap, cfg) if a.kind == 'implement'])

    def test_its_priority_counts(self):
        cfg = coordinated(SCFG)
        self.assertEqual(self.priorities(cfg), ({1: 'normal', 2: 'high', 3: 'normal'},
                                                [2, 1, 3]))

    def test_off_it_does_not(self):
        cfg = declared(SCFG)
        self.assertEqual(self.priorities(cfg)[0], {1: 'normal', 2: 'normal', 3: 'normal'})

    def test_its_dependency_and_implementer_labels_never_count(self):
        d = self.make({}, coordinated(SCFG))
        self.assertIn(COORDINATOR, d._admitters('brushtyler', coordinator=True))
        self.assertNotIn(COORDINATOR, d._admitters('brushtyler'))


class CoordinatorIssueTests(recheck_tests.Case):
    """The coordinator's issues: queued by the supervisor and by the coordinator, except a
    change of the trust policy, which waits for a trusted user's ``agent:ready``."""

    def issue(self, ready_by=COORDINATOR, cfg=ISSUE_CFG):
        d = self.make(cfg=cfg)
        self.gh.author = COORDINATOR
        self.gh.ready_events = [(ready_by, self.gh.now())]
        return d

    def check(self, d, trust, ready=True):
        self.runner.outputs.append(checked(ready, trust=trust,
                                           reason='' if trust == 'none' else 'punto 1'))
        self.assertEqual([a.kind for a in d.tick()], ['preflight'])
        return self.records()[-1]

    def test_its_app_is_one_of_devloops(self):
        d = self.issue()
        self.assertIn(COORDINATOR, d._devloop_logins())
        self.assertEqual(d._admitters(COORDINATOR, coordinator=True),
                         {*d.cfg.trusted_users, SUPERVISOR_LOGIN, COORDINATOR})
        self.assertNotIn(COORDINATOR, d.cfg.trusted_users)

    def test_outside_the_trust_policy_its_label_queues_it(self):
        for ready_by in (COORDINATOR, SUPERVISOR_LOGIN):
            with self.subTest(ready_by=ready_by):
                d = self.issue(ready_by)
                self.assertEqual(self.planned(d, ISSUE_CFG), [('preflight', 7)])
                self.assertTrue(self.check(d, 'none')['ready'])
                self.assertEqual(self.gh.labels[7], {READY})
                self.assertEqual(self.planned(d, ISSUE_CFG), [('implement', 7)])

    def test_on_the_trust_policy_it_waits_for_a_trusted_user(self):
        for trust in ('covered', 'uncovered'):
            with self.subTest(trust=trust):
                d = self.issue()
                record = self.check(d, trust)
                self.assertFalse(record['ready'])
                self.assertEqual(record['gaps'][0]['kind'], 'operator_decision')
                self.assertIn('aperta dal coordinatore', record['gaps'][0]['description'])
                self.assertEqual(self.gh.labels[7], {READY, HUMAN})
                self.assertNotIn(SUPER, self.gh.labels[7])
                self.assertEqual(self.planned(d, ISSUE_CFG), [])
                # The coordinator's agent:ready again: the check runs, and stops it again.
                self.gh.relabel(COORDINATOR)
                self.assertFalse(self.check(d, trust)['ready'])
                self.assertEqual(self.gh.labels[7], {READY, HUMAN})
                # The operator's agent:ready admits it.
                self.gh.relabel('brushtyler')
                self.assertEqual(self.planned(d, ISSUE_CFG), [('preflight', 7)])
                self.assertTrue(self.check(d, 'covered')['ready'])
                self.assertEqual(self.gh.labels[7], {READY})
                self.assertEqual(self.planned(d, ISSUE_CFG), [('implement', 7)])

    def test_a_trusted_users_label_admits_it_at_once(self):
        d = self.issue('brushtyler')
        self.assertTrue(self.check(d, 'covered')['ready'])
        self.assertEqual(self.planned(d, ISSUE_CFG), [('implement', 7)])

    def test_a_reused_check_holds_only_while_a_trusted_user_queued_it(self):
        # The operator's agent:ready replaced by the coordinator's or the supervisor's after a
        # positive check: the reused outcome passes the gate again, and blocks the issue.
        for ready_by in (COORDINATOR, SUPERVISOR_LOGIN):
            with self.subTest(ready_by=ready_by):
                d = self.issue('brushtyler')
                self.assertTrue(self.check(d, 'covered')['ready'])
                self.gh.relabel(ready_by)
                self.assertEqual(self.planned(d, ISSUE_CFG), [('preflight_block', 7)])
                self.assertEqual([a.kind for a in d.tick()], ['preflight_block'])
                record = self.records()[-1]
                self.assertFalse(record['ready'])
                self.assertIn('aperta dal coordinatore', record['gaps'][0]['description'])
                self.assertEqual(self.gh.labels[7], {READY, HUMAN})
                self.assertEqual(self.planned(d, ISSUE_CFG), [])
                # The operator's agent:ready admits it again, with a new check.
                self.gh.relabel('brushtyler')
                self.assertEqual(self.planned(d, ISSUE_CFG), [('preflight', 7)])
                self.assertTrue(self.check(d, 'covered')['ready'])
                self.assertEqual(self.planned(d, ISSUE_CFG), [('implement', 7)])

    def test_a_reused_check_outside_the_trust_policy_still_holds(self):
        d = self.issue('brushtyler')
        self.assertTrue(self.check(d, 'none')['ready'])
        self.gh.relabel(COORDINATOR)
        self.assertEqual(self.planned(d, ISSUE_CFG), [('implement', 7)])

    def test_an_issue_of_a_trusted_user_is_not_held(self):
        d = self.make(cfg=ISSUE_CFG)
        self.gh.ready_events = [(COORDINATOR, self.gh.now())]
        self.assertTrue(self.check(d, 'covered')['ready'])

    def test_without_a_readiness_check_only_trusted_users_queue_it(self):
        cfg = coordinated(dataclasses.replace(recheck_tests.CFG, preflight=''))
        d = self.issue(cfg=cfg)
        self.assertEqual(d._admitters(COORDINATOR, coordinator=True), set(cfg.trusted_users))
        self.assertEqual(self.planned(d, cfg), [])


class DocsTests(unittest.TestCase):
    def test_the_guide_and_the_runbook_describe_the_perimeter(self):
        root = Path(__file__).resolve().parents[1]
        guide = ' '.join((root / 'docs/devloop/guide.md').read_text(encoding='utf-8').split())
        runbook = ' '.join((root / 'docs/devloop/roles/coordinator.md')
                           .read_text(encoding='utf-8').split())
        adr = (root / 'docs/adr/0002-ruoli-e-responsabilita.md').read_text(encoding='utf-8')
        for text in (guide, runbook):
            self.assertIn('#595', text)
        self.assertIn('precisazione-del-2026-11-23-compiti-del-coordinatore-594', guide)
        self.assertIn('#595', adr)


if __name__ == '__main__':
    unittest.main()
