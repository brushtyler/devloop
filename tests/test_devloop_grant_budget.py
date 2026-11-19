"""devloop: the supervisor extends once the budget envelope of a story (#411, ADR 0005, P5-7).

``[delegations] budget`` (off by default) sends a turn the envelope of its story refused at a
limit to the supervisor as ``budget_grantable`` when the story was never extended and the
extension would cover the turn; ``/devloop grant-budget <reason>`` extends that envelope once,
in turns and time only, by 25% of the frozen values rounded down (D3), never in dollars nor
tokens, with a ``supervision`` record, a ledger ``event`` and a ``grant`` in the turn journal,
which is the authority on the extension.
"""

import dataclasses
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from devloop import budget, config, markdown, supervision
from devloop.budget import BudgetRefused
from devloop.config import BudgetPolicy, ConfigError, DelegationPolicy, parse_delegations
from devloop.journal import TurnJournal
from devloop.planning import Action, plan, runs_issue_step
from test_devloop import CFG, SHA1, SHA2, implemented, pr, raw_review
from test_devloop_dependencies import block
from test_devloop_grant_round import GrantCase
from test_devloop_merged_issue import merged_pr
from test_devloop_supervision import (AUTO, HUMAN, READY, REVIEW, SUPER, SUPERVISOR_LOGIN,
                                      crash)

REPO = CFG.repo
ROLES = ('implement', 'fix', 'review', 'sync')
QUOTA = (('model', 'unmetered_quota'), ('reason', 'the turn caps bound it'))
REASON = ('La review ha chiesto due fix sullo stesso file: l\'issue era troppo larga, divisa '
          'con #999.\nCausa: issue troppo larga.')
START = 1_800_000_000  # 2027-01-15T08:00:00Z


def envelope(turns=4, role=4, wall=86400, usage=None):
    """An envelope with ``turns`` in total and in the phase, ``role`` turns per role."""
    return BudgetPolicy(enabled=True, turns=turns, role_turns=tuple((r, role) for r in ROLES),
                        phase_turns=(('implementation', turns),), wall_seconds=wall,
                        metered_usd=5.0, cli_estimate_usd=5.0,
                        usage=usage or tuple((agent, QUOTA) for agent in AUTO.agents))


ON = dataclasses.replace(AUTO, story_identity=True, budget=envelope(),
                         delegations=DelegationPolicy(budget=True))
OFF = dataclasses.replace(ON, delegations=DelegationPolicy())
# 4 turns: 25% is 1 more for each turn cap; 21600 seconds more for the day of wall time.
EXTENSION = {'percent': 25, 'turns': 1, 'role_turns': dict.fromkeys(ROLES, 1),
             'phase_turns': {'implementation': 1}, 'wall_seconds': 21600}


class BudgetCase(GrantCase):
    def make(self, cfg=ON, *outputs, has_pr=True):
        d = super().make(cfg, *outputs)
        if not has_pr:
            self.gh.has_pr = False
            self.gh.labels[7] = {READY}
        self.story = d.ledger.journal.story(REPO, 7, 'h1')['story']
        return d

    def spend(self, d, count, kind='review', agent=None):
        """Book and close ``count`` turns of ``kind`` of ``agent`` in the story of issue 7."""
        journal = d.ledger.journal
        candidate = d.cfg.agent(agent or d.cfg.reviewer)
        for _ in range(count):
            turn = journal.intent(
                100, issue=7, repo=REPO, kind=kind, agent=candidate.key, model=candidate.model,
                label='l', reserve=lambda j: budget.reserve(d.cfg.budget, j, repo=REPO, issue=7,
                                                            kind=kind, agent=candidate))
            journal.outcome(turn, 'returned')

    def refuse_review(self, d):
        """The review of PR #100 refused by the envelope of its story: its escalation."""
        self.assertEqual(self.tick(d), [('review', 100)])
        self.assertEqual(self.runner.calls, [])  # no CLI started
        return self.escalations(100)[-1]


class RoutingTests(BudgetCase):
    def test_off_by_default_the_refused_turn_stays_with_the_operator(self):
        self.assertFalse(config.load_config().delegations.budget)
        self.assertFalse(AUTO.delegations.budget)
        d = self.make(OFF)
        self.spend(d, 4)
        record = self.refuse_review(d)
        self.assertEqual((record['class'], record['to']), ('budget', 'operator'))
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertNotIn('grant-budget <motivo>', self.body())
        self.assertNotIn('andrebbe al supervisore', self.body())

    def test_a_grantable_refusal_goes_to_the_supervisor(self):
        d = self.make()
        self.spend(d, 4)
        record = self.refuse_review(d)
        self.assertEqual({k: record[k] for k in ('class', 'critical', 'to', 'story', 'kind',
                                                 'agent', 'step', 'resume', 'category')},
                         {'class': 'budget_grantable', 'critical': False, 'to': 'supervisor',
                          'story': self.story, 'kind': 'review', 'agent': CFG.reviewer,
                          'step': 'review', 'resume': REVIEW, 'category': 'LIMIT_REACHED'})
        self.assertEqual(record['extension'], EXTENSION)
        self.assertEqual(record['key'], {'step': 'review', 'head': SHA1})
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertIn('`/devloop grant-budget <motivo>`', self.body())
        self.assertIn('analisi delle cause', self.body())
        self.assertIn('turni +1', self.body())
        self.assertIn('tempo +21600 s', self.body())
        # Nothing runs on the PR until the supervisor decides, and nothing is spent yet.
        self.assertEqual(self.tick(d), [])
        self.assertIsNone(d.ledger.journal.extension(self.story))
        self.assertEqual(d.ledger.journal.grants(), [])

    def test_a_null_increment_is_not_offered_and_not_spent(self):
        # 3 turns × 25% = 0.75, rounded down to 0: the extension would add nothing to the
        # exhausted caps, so it is refused, and the story keeps it.
        cfg = dataclasses.replace(ON, budget=envelope(turns=3, role=3))
        d = self.make(cfg)
        self.spend(d, 3)
        record = self.refuse_review(d)
        self.assertEqual((record['class'], record['to']), ('budget', 'operator'))
        self.assertEqual(self.gh.labels[100], {HUMAN})
        body = ' '.join(self.body().split())
        self.assertIn('la delega del budget non si applica', body)
        self.assertIn('arrotondato per difetto', body)
        self.assertIn('turni +0', body)
        self.assertIn('andrebbe al supervisore', body)
        self.assertIsNone(d.ledger.journal.extension(self.story))
        self.assertEqual(d.ledger.journal.grants(), [])

    def test_the_daily_cap_counts_every_delegation(self):
        d = self.make()
        for n in range(supervision.MAX_DAILY_GRANTS):
            d.ledger.journal.grant('rounds', 200 + n, 9000 + n, SUPERVISOR_LOGIN,
                                   supervision.MAX_DAILY_GRANTS)
        self.spend(d, 4)
        record = self.refuse_review(d)
        self.assertEqual(record['class'], 'budget')
        self.assertIn('il massimo giornaliero', self.body())

    def test_what_makes_the_extension_the_operators(self):
        cases = {
            'the supervisor implemented the PR': (
                dataclasses.replace(ON, supervisor=CFG.implementer), lambda gh: None,
                'il supervisore ha implementato'),
            'phase:design': (ON, lambda gh: gh.labels[7].add('phase:design'), 'phase:design'),
        }
        for name, (cfg, change, why) in cases.items():
            with self.subTest(case=name):
                d = self.make(cfg)
                change(self.gh)
                self.spend(d, 4)
                record = self.refuse_review(d)
                self.assertEqual((record['class'], record['to']), ('budget', 'operator'))
                self.assertIn(why, self.body())

    def test_an_issue_step_refused_goes_to_the_supervisor_and_resumes(self):
        d = self.make(ON, implemented(), has_pr=False)
        # The ledger binds the tested tree to the commit of the implementation.
        self.ws.commit_tree = lambda path, commit: 'e' * 40
        self.spend(d, 4, kind='implement', agent=CFG.implementer)
        self.assertEqual(self.tick(d), [('implement', 7)])
        self.assertEqual(self.runner.calls, [])
        record = self.escalations(7)[-1]
        self.assertEqual((record['class'], record['step'], record['kind']),
                         ('budget_grantable', 'implement', 'implement'))
        self.assertEqual(record['key']['step'], 'implement')
        self.assertIn(SUPER, self.gh.labels[7])
        self.gh.command(7, '/devloop grant-budget ' + REASON)
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.decisions(7)[0]['outcome'], 'granted')
        self.assertTrue(any('riparte ora' in c['body'] for c in self.gh.threads[7]))
        # The implementation runs at once, as after a retry, within the extended envelope.
        self.assertEqual(len(self.runner.calls), 1)
        self.assertNotIn(SUPER, self.gh.labels[7])
        self.assertEqual(len(self.decisions(7)), 1)


class GrantTests(BudgetCase):
    def escalate(self, d):
        self.spend(d, 4)
        self.assertEqual(self.refuse_review(d)['class'], 'budget_grantable')

    def test_a_grant_extends_the_envelope_of_the_story_once(self):
        d = self.make(ON, raw_review())
        self.escalate(d)
        self.gh.command(100, '/devloop grant-budget ' + REASON)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        (decision,) = self.decisions(100)
        self.assertEqual({k: decision[k] for k in ('command', 'by', 'outcome', 'class', 'story',
                                                   'extension', 'daily', 'daily_cap',
                                                   'daily_left')},
                         {'command': 'grant-budget', 'by': SUPERVISOR_LOGIN,
                          'outcome': 'granted', 'class': 'budget_grantable',
                          'story': self.story, 'extension': EXTENSION, 'daily': 1,
                          'daily_cap': 3, 'daily_left': 2})
        self.assertEqual(decision['reason'], REASON)  # the analysis of the causes, whole
        self.assertIn('unica estensione', self.gh.threads[100][-1]['body'])
        events =[r for r in self.gh.records(100, 'ledger')
                  if r.get('event') == 'delegation_grant']
        self.assertEqual(len(events), 1)
        self.assertEqual({k: events[0][k] for k in ('delegation', 'by', 'story', 'extension',
                                                    'daily')},
                         {'delegation': 'budget', 'by': SUPERVISOR_LOGIN, 'story': self.story,
                          'extension': EXTENSION, 'daily': 1})
        journal = d.ledger.journal
        (grant,) = journal.grants()
        self.assertEqual((grant['delegation'], grant['number'], grant['story'],
                          grant['extension'], grant['reason']),
                         ('budget', 100, self.story, EXTENSION, REASON))
        self.assertEqual(journal.extension(self.story)['extension'], EXTENSION)
        # The envelope of the story is the frozen one with the extension; the configuration and
        # the frozen limits of the bookings do not change.
        state = budget.of_issue(d.cfg.budget, journal, REPO, 7)
        self.assertEqual((state['turns']['cap'], state['roles']['review']['cap'],
                          state['time']['cap']), (5, 5, 86400 + 21600))
        self.assertEqual(state['usd']['cli_estimate_usd']['cap'], 5.0)  # never the dollars
        self.assertIn('tetti già estesi una volta', budget.describe(state))
        self.assertEqual(d.cfg.budget.turns, 4)
        # The review runs, booked in the extended envelope with the frozen limits.
        self.assertEqual(self.tick(d), [('review', 100)])
        self.assertEqual(len(self.runner.calls), 1)
        booked = [t['budget'] for t in journal.turns().values()]
        self.assertEqual(len(booked), 5)
        self.assertEqual({b['envelope']['turns'] for b in booked}, {4})
        # The command is spent; another refusal of the story goes to the operator.
        self.assertNotIn(('supervise', 100), self.tick(d))
        self.assertEqual(len(self.decisions(100)), 1)
        reviewer = d.cfg.agent(d.cfg.reviewer)
        with self.assertRaises(BudgetRefused) as caught:
            budget.reserve(d.cfg.budget, journal, repo=REPO, issue=7, kind='review',
                           agent=reviewer)
        self.assertIn('tetti già estesi una volta', str(caught.exception))
        d._escalate_failure(Action('review', 100), pr(), caught.exception)
        record = self.escalations(100)[-1]
        self.assertEqual((record['class'], record['to']), ('budget', 'operator'))
        self.assertIn('ha già avuto la sua unica estensione', self.body())
        self.assertEqual(len(journal.grants()), 1)

    def test_the_extension_survives_a_restart(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        journal = TurnJournal(tmp)
        story = journal.story(REPO, 7, 'h1')['story']
        self.assertEqual(journal.extend(story, EXTENSION, 100, 1, 'x', 3), (1, None))
        again = TurnJournal(tmp)
        self.assertEqual(again.extension(story)['extension'], EXTENSION)
        self.assertEqual(again.extend(story, EXTENSION, 100, 2, 'x', 3), (None, 'spent'))
        self.assertTrue((Path(tmp) / 'turn-journal.jsonl').exists())

    def test_a_grant_without_a_reason_is_answered_and_not_applied(self):
        d = self.make()
        self.escalate(d)
        self.gh.command(100, '/devloop grant-budget')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('manca il testo', self.body())
        self.assertIsNone(d.ledger.journal.extension(self.story))

    def test_a_grant_after_the_revocation_goes_to_the_operator(self):
        d = self.make()
        self.escalate(d)
        d.cfg = OFF  # the operator turned the delegation off while it waited
        self.gh.command(100, '/devloop grant-budget ' + REASON)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        record = self.escalations(100)[-1]
        self.assertEqual((record['class'], record['critical']), ('budget', True))
        self.assertEqual(record['step'], 'review')  # the operator may still retry it
        self.assertIn('`[delegations] budget`', self.body())
        self.assertIsNone(d.ledger.journal.extension(self.story))

    def test_a_story_extended_in_the_meantime_goes_to_the_operator(self):
        d = self.make()
        self.escalate(d)
        # Another command, on another item of the same story, took the extension first.
        self.assertEqual(d.ledger.journal.extend(self.story, EXTENSION, 7, 1, 'x', 3),
                         (1, None))
        self.gh.command(100, '/devloop grant-budget ' + REASON)
        self.tick(d)
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('unica estensione', self.gh.threads[100][-2]['body'])
        self.assertEqual(len(d.ledger.journal.grants()), 1)

    def test_a_grant_on_another_class_is_refused(self):
        d = self.make(ON, crash())
        self.assertEqual(self.tick(d), [('review', 100)])
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.gh.command(100, '/devloop grant-budget ' + REASON)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('vale solo per un\'escalation `budget_grantable`', self.body())

    def test_a_retry_on_a_grantable_refusal_is_refused(self):
        d = self.make()
        self.escalate(d)
        self.gh.command(100, '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('`/devloop grant-budget <motivo>`', self.body())

    def test_a_grant_on_a_moved_head_is_refused_and_not_spent(self):
        d = self.make()
        self.escalate(d)
        self.gh.head = SHA2  # the input of the refused review is superseded
        self.gh.command(100, '/devloop grant-budget ' + REASON)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('`head`', self.body())
        self.assertIn('input superato', self.body())
        self.assertIsNone(d.ledger.journal.extension(self.story))
        self.assertEqual(d.ledger.journal.grants(), [])
        self.assertEqual(self.runner.calls, [])

    def test_an_escalation_handed_to_the_operator_keeps_its_step(self):
        d = self.make()
        self.escalate(d)
        self.gh.command(100, '/devloop escalate serve l\'operatore')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {HUMAN})
        record = self.escalations(100)[-1]
        self.assertEqual((record['class'], record['step'], record['resume'], record['key']),
                         ('supervisor_escalated', 'review', REVIEW,
                          {'step': 'review', 'head': SHA1}))
        self.assertIn('/devloop retry', self.body())
        # The operator's retry runs the step again, as on a refusal sent to it directly.
        self.gh.command(100, '/devloop retry', login='brushtyler')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        decision = self.decisions(100)[-1]
        self.assertEqual((decision['command'], decision['outcome']), ('retry', 'applied'))
        self.assertEqual(self.gh.labels[100], {REVIEW})
        self.assertIsNone(d.ledger.journal.extension(self.story))

    def test_the_operator_does_not_extend_with_the_command(self):
        d = self.make(OFF)
        self.spend(d, 4)
        self.refuse_review(d)
        self.gh.command(100, '/devloop grant-budget ' + REASON, login='brushtyler')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('delega del supervisore', self.body())
        self.assertIsNone(d.ledger.journal.extension(self.story))


class RecoveryTests(BudgetCase):
    """A run stopped after the journal recorded the extension (#411): the next one completes
    the records of the same command without extending the story again."""

    def command(self, d):
        self.spend(d, 4)
        self.assertEqual(self.refuse_review(d)['class'], 'budget_grantable')
        self.gh.command(100, '/devloop grant-budget ' + REASON)
        return self.gh.threads[100][-1]['id']

    def events(self):
        return [r for r in self.gh.records(100, 'ledger')
                if r.get('event') == 'delegation_grant']

    def assert_completed(self, d):
        self.assertEqual(self.tick(d), [('supervise', 100)])
        (decision,) = self.decisions(100)
        self.assertEqual((decision['outcome'], decision['story'], decision['extension'],
                          decision['daily']), ('granted', self.story, EXTENSION, 1))
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]['comment'], decision['comment'])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        journal = d.ledger.journal
        self.assertEqual(len(journal.grants()), 1)
        state = budget.of_issue(d.cfg.budget, journal, REPO, 7)
        self.assertEqual(state['turns']['cap'], 5)  # extended once, not twice
        # The command is spent: the next tick runs the review.
        self.assertEqual(self.tick(d), [('review', 100)])
        self.assertEqual(len(self.decisions(100)), 1)

    def test_a_stop_before_the_ledger_event(self):
        d = self.make(ON, raw_review())
        self.command(d)
        with mock.patch.object(d.ledger, 'event', side_effect=RuntimeError('stopped')):
            with self.assertRaises(RuntimeError):
                self.tick(d)
        self.assertEqual(d.ledger.journal.extension(self.story)['extension'], EXTENSION)
        self.assertEqual((self.decisions(100), self.events()), ([], []))
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assert_completed(d)

    def test_a_stop_before_the_supervision_record(self):
        d = self.make(ON, raw_review())
        self.command(d)
        with mock.patch('devloop.dispatch_supervision.render_supervision',
                        side_effect=RuntimeError('stopped')):
            with self.assertRaises(RuntimeError):
                self.tick(d)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.decisions(100), [])
        self.assert_completed(d)

    def test_a_recovery_ignores_what_changed_since(self):
        # The extension is part of the envelope already: it is neither checked nor applied
        # again, even with the delegation turned off meanwhile.
        d = self.make(ON, raw_review())
        comment = self.command(d)
        self.assertEqual(d.ledger.journal.extend(self.story, EXTENSION, 100, comment,
                                                 SUPERVISOR_LOGIN, 3), (1, None))
        d.cfg = OFF
        self.assert_completed(d)

    def test_a_later_command_does_not_hide_the_stopped_grant(self):
        # The run stopped after the journal; another command came before the next run. The
        # stopped grant is completed first, as its own command, whatever the latest one is.
        later = {'grant-budget': '/devloop grant-budget Un altro motivo.',
                 'escalate': '/devloop escalate serve l\'operatore',
                 'retry': '/devloop retry'}
        for name, body in later.items():
            with self.subTest(command=name):
                d = self.make(ON, raw_review())
                first = self.command(d)
                with mock.patch.object(d.ledger, 'event', side_effect=RuntimeError('stopped')):
                    with self.assertRaises(RuntimeError):
                        self.tick(d)
                self.gh.command(100, body)
                self.assert_completed(d)
                (decision,) = self.decisions(100)
                self.assertEqual((decision['command'], decision['comment'], decision['by'],
                                  decision['reason']),
                                 ('grant-budget', first, SUPERVISOR_LOGIN, REASON))
                self.assertTrue(any('senza applicarla di nuovo' in c['body']
                                    for c in self.gh.threads[100]))
                self.assertEqual(self.escalations(100)[-1]['class'], 'budget_grantable')

    def test_the_journal_keeps_the_text_of_the_command(self):
        # The text is read from the journal, even when the comment is gone from the thread.
        d = self.make(ON, raw_review())
        first = self.command(d)
        with mock.patch.object(d.ledger, 'event', side_effect=RuntimeError('stopped')):
            with self.assertRaises(RuntimeError):
                self.tick(d)
        self.assertEqual(d.ledger.journal.extension(self.story)['reason'], REASON)
        self.gh.threads[100] = [c for c in self.gh.threads[100] if c['id'] != first]
        self.assert_completed(d)
        self.assertEqual(self.decisions(100)[0]['reason'], REASON)

    def test_another_command_on_an_extended_story_is_refused(self):
        d = self.make()
        comment = self.command(d)
        d.ledger.journal.extend(self.story, EXTENSION, 7, comment - 500, 'x', 3)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertEqual(self.events(), [])
        self.assertEqual(self.gh.labels[100], {HUMAN})


class IssueTests(BudgetCase):
    """``grant-budget`` on an issue runs its step, as a retry does."""

    def escalate(self):
        d = self.make(ON, implemented(), has_pr=False)
        self.ws.commit_tree = lambda path, commit: 'e' * 40
        self.spend(d, 4, kind='implement', agent=CFG.implementer)
        self.assertEqual(self.tick(d), [('implement', 7)])
        self.assertEqual(self.escalations(7)[-1]['class'], 'budget_grantable')
        return d

    def assert_nothing_spent(self, d):
        self.assertIsNone(d.ledger.journal.extension(self.story))
        self.assertEqual(d.ledger.journal.grants(), [])
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.gh.created, [])

    def test_a_grant_on_a_superseded_input_is_refused(self):
        changes = {'base': lambda: setattr(self.ws, 'base', 'e' * 40),
                   'request': lambda: self.gh.say(7, 'brushtyler', 'Anche Y.')}
        for field, change in changes.items():
            with self.subTest(field=field):
                d = self.escalate()
                change()
                self.gh.command(7, '/devloop grant-budget ' + REASON)
                self.assertEqual(self.tick(d), [('supervise', 7)])
                self.assertEqual(self.decisions(7)[-1]['outcome'], 'refused')
                self.assertIn(f'`{field}`', self.gh.threads[7][-1]['body'])
                self.assertIn(SUPER, self.gh.labels[7])
                self.assert_nothing_spent(d)

    def test_a_resolved_issue_is_not_implemented_again(self):
        d = self.escalate()
        self.gh.merged_raw = (merged_pr(),)
        self.gh.command(7, '/devloop grant-budget ' + REASON)
        self.assertEqual(self.tick(d), [('supervise', 7)])
        decision = self.decisions(7)[-1]
        self.assertEqual(decision['outcome'], 'refused')
        self.assertIn('#99', [c['body'] for c in self.gh.threads[7]
                              if markdown.read_marker(c['body'], 'supervision')][-1])
        escalation = self.escalations(7)[-1]
        self.assertEqual(escalation['class'], 'merged_issue')
        self.assert_nothing_spent(d)

    def test_a_stopped_grant_on_an_issue_resolved_since_is_completed(self):
        # The run stopped after the journal; a devloop PR resolving the issue was merged before
        # the next run. The grant gets its records, and the issue is not implemented again.
        d = self.escalate()
        self.gh.command(7, '/devloop grant-budget ' + REASON)
        first = self.gh.threads[7][-1]['id']
        with mock.patch.object(d.ledger, 'event', side_effect=RuntimeError('stopped')):
            with self.assertRaises(RuntimeError):
                self.tick(d)
        self.assertEqual(d.ledger.journal.extension(self.story)['comment'], first)
        self.assertEqual(self.decisions(7), [])
        self.gh.merged_raw = (merged_pr(),)
        self.assertEqual(self.tick(d), [('supervise', 7)])
        (decision,) = self.decisions(7)
        self.assertEqual((decision['outcome'], decision['comment'], decision['story'],
                          decision['extension'], decision['reason']),
                         ('granted', first, self.story, EXTENSION, REASON))
        self.assertIn('#99', self.gh.threads[7][-2]['body'] + self.gh.threads[7][-1]['body'])
        events = [r for r in self.gh.records(7, 'ledger')
                  if r.get('event') == 'delegation_grant']
        self.assertEqual([(e['delegation'], e['comment']) for e in events], [('budget', first)])
        self.assertEqual(self.escalations(7)[-1]['class'], 'merged_issue')
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.gh.created, [])
        self.assertEqual(len(d.ledger.journal.grants()), 1)

    def test_a_stopped_grant_on_a_blocked_issue_waits(self):
        d = self.escalate()
        self.gh.command(7, '/devloop grant-budget ' + REASON)
        with mock.patch.object(d.ledger, 'event', side_effect=RuntimeError('stopped')):
            with self.assertRaises(RuntimeError):
                self.tick(d)
        pair = block(1)
        self.gh.issue_dependencies = lambda n: (
            {'author': 'brushtyler', 'blocked_by': [dict(pair[0])], 'added': [dict(pair[1])]}
            if n == 7 else {'author': 'brushtyler', 'blocked_by': [], 'added': []})
        self.gh.command(7, '/devloop escalate serve l\'operatore')
        d._do_supervise(7)
        self.assertEqual(self.decisions(7), [])
        self.assertIn(SUPER, self.gh.labels[7])
        self.assertIn('  the extension waits, blocked by #1', self.logs)
        self.assertEqual(self.runner.calls, [])

    def test_a_blocked_issue_waits_and_takes_no_slot(self):
        d = self.escalate()
        pair = block(1)
        self.gh.issue_dependencies = lambda n: (
            {'author': 'brushtyler', 'blocked_by': [dict(pair[0])], 'added': [dict(pair[1])]}
            if n == 7 else {'author': 'brushtyler', 'blocked_by': [], 'added': []})
        self.gh.command(7, '/devloop grant-budget ' + REASON)
        snap = d.snapshot()
        self.assertEqual(snap.supervised[0]['blocked_by'], ['#1'])
        self.assertEqual(plan(snap, ON), [])
        self.assertEqual(self.tick(d), [])
        self.assertIn('  issue #7: the extension waits, blocked by #1', self.logs)
        self.assertEqual(self.decisions(7), [])
        self.assert_nothing_spent(d)

    def test_the_grant_takes_a_free_pr_slot(self):
        d = self.escalate()
        self.gh.command(7, '/devloop grant-budget ' + REASON)
        snap = d.snapshot()
        snap.ready_issues.append({'number': 8, 'title': 'Issue 8', 'labels': {READY},
                                  'labeler': 'brushtyler', 'admitted': True, 'merged': [],
                                  'blocked_by': []})
        one = dataclasses.replace(ON, max_open_prs=1)
        self.assertEqual([(a.kind, a.number) for a in plan(snap, one)], [('supervise', 7)])
        full = dataclasses.replace(ON, max_open_prs=0)
        self.assertEqual(plan(snap, full), [])
        # A command only to answer, or one on a PR, takes no slot.
        self.assertFalse(runs_issue_step(dict(snap.supervised[0], refusal='x')))
        self.assertFalse(runs_issue_step(dict(snap.supervised[0], pr=True)))
        self.assertTrue(runs_issue_step(snap.supervised[0]))


class EnvelopeTests(unittest.TestCase):
    """The extension on the journal, with ``core.budget``."""

    def setUp(self):
        self.now = START
        self.journal = TurnJournal(clock=lambda: self.now)
        self.story = self.journal.story(REPO, 7, 'h1')['story']

    def book(self, policy, agent='codex', kind='review'):
        candidate = CFG.agent(agent)
        turn = self.journal.intent(
            7, issue=7, repo=REPO, kind=kind, agent=agent, model=candidate.model, label='l',
            reserve=lambda j: budget.reserve(policy, j, repo=REPO, issue=7, kind=kind,
                                             agent=candidate))
        self.journal.outcome(turn, 'returned')

    def grantable(self, policy, agent='codex'):
        return budget.grantable(policy, self.journal, repo=REPO, issue=7, kind='review',
                                agent=CFG.agent(agent))

    def test_the_increments_are_25_percent_rounded_down(self):
        self.assertEqual(supervision.BUDGET_TOLERANCE_PERCENT, 25)
        found = budget.increments(envelope(turns=10, role=3, wall=3601))
        self.assertEqual(found, {'percent': 25, 'turns': 2, 'role_turns': dict.fromkeys(ROLES, 0),
                                 'phase_turns': {'implementation': 2}, 'wall_seconds': 900})
        big = budget.extended(envelope(turns=10, role=3, wall=3601), found)
        self.assertEqual((big.turns, dict(big.role_turns)['review'], big.wall_seconds,
                          big.cli_estimate_usd), (12, 3, 4501, 5.0))
        self.assertEqual(budget.extended(envelope(), None), envelope())

    def test_the_time_is_extended_too(self):
        policy = envelope(wall=3600)
        self.book(policy)
        self.now = START + 3600
        with self.assertRaisesRegex(BudgetRefused, 'wall-time exhausted'):
            self.book(policy)
        story, extension, why = self.grantable(policy)
        self.assertEqual((story, why), (self.story, ''))
        self.assertEqual(extension['wall_seconds'], 900)
        self.assertEqual(self.journal.extend(self.story, extension, 7, 1, 'x', 3), (1, None))
        self.book(policy)  # covered now, within the 900 seconds more
        self.now = START + 4500
        with self.assertRaisesRegex(BudgetRefused, 'wall-time exhausted'):
            self.book(policy)

    def test_a_time_past_the_extension_is_not_offered(self):
        policy = envelope(wall=3600)
        self.book(policy)
        self.now = START + 4500  # past the 900 seconds the extension would add
        story, extension, why = self.grantable(policy)
        self.assertIsNone(extension)
        self.assertIn('non coprirebbe comunque', why)
        self.assertIsNone(self.journal.extension(self.story))

    def test_the_dollars_are_never_extended(self):
        reserve = (('model', 'reserve_per_invocation'), ('reason', 'r'), ('metered_usd', 1),
                   ('cli_estimate_usd', 2.5))
        policy = dataclasses.replace(
            envelope(turns=10, role=10),
            usage=tuple((a, reserve if a == 'claude' else QUOTA) for a in AUTO.agents))
        self.book(policy, agent='claude')
        self.book(policy, agent='claude')
        with self.assertRaisesRegex(BudgetRefused, 'LIMIT_REACHED'):
            self.book(policy, agent='claude')
        _, extension, why = self.grantable(policy, agent='claude')
        self.assertIsNone(extension)
        self.assertIn('mai dollari né token', why)

    def test_a_refusal_that_is_not_a_limit_is_not_offered(self):
        policy = envelope()
        # Covered without an extension: nothing to grant.
        _, extension, why = self.grantable(policy)
        self.assertIsNone(extension)
        self.assertIn('copre già il turno', why)
        # A turn left without outcome by a process that stopped: OUTCOME_UNCERTAIN, which only
        # a reconciliation settles.
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        journal = TurnJournal(tmp, clock=lambda: self.now)
        journal.story(REPO, 7, 'h1')
        candidate = CFG.agent('codex')
        journal.intent(7, issue=7, repo=REPO, kind='review', agent='codex',
                       model=candidate.model, label='l',
                       reserve=lambda j: budget.reserve(policy, j, repo=REPO, issue=7,
                                                        kind='review', agent=candidate))
        restarted = TurnJournal(tmp, clock=lambda: self.now)
        _, extension, why = budget.grantable(policy, restarted, repo=REPO, issue=7,
                                             kind='review', agent=candidate)
        self.assertIsNone(extension)
        self.assertIn('OUTCOME_UNCERTAIN', why)
        self.assertIsNone(restarted.extension(journal.stories().popitem()[0]))

    def test_the_journal_extends_a_story_once_with_two_commands_together(self):
        start, results = threading.Barrier(2), {}

        def extend(comment):
            start.wait()
            results[comment] = self.journal.extend(self.story, EXTENSION, 100, comment, 'x', 3)

        threads = [threading.Thread(target=extend, args=(n,)) for n in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results.values(), key=str), [(1, None), (None, 'spent')])
        self.assertEqual(len(self.journal.grants()), 1)
        # The same command counts once.
        (winner,) = [c for c, r in results.items() if r == (1, None)]
        self.assertEqual(self.journal.extend(self.story, EXTENSION, 100, winner, 'x', 3),
                         (1, None))
        self.assertEqual(len(self.journal.grants()), 1)

    def test_the_journal_checks_the_daily_cap_of_every_delegation(self):
        for n in range(3):
            self.journal.grant('rounds', 200 + n, 9000 + n, 'x', 3)
        self.assertEqual(self.journal.extend(self.story, EXTENSION, 100, 1, 'x', 3),
                         (None, 'daily'))
        self.assertIsNone(self.journal.extension(self.story))


class ConfigTests(unittest.TestCase):
    def test_the_revocation_key_is_off_by_default(self):
        self.assertEqual(parse_delegations({}).budget, False)
        self.assertTrue(parse_delegations({'budget': True}).budget)
        with self.assertRaises(ConfigError):
            parse_delegations({'budget': 'yes'})
        data = config.load_data()
        on = config.apply_overlay(data, {'delegations': {'budget': True}})
        self.assertTrue(config.parse_config(on).delegations.budget)
        self.assertIn('delegations', config.LIVE_KEYS)
        toml = (Path(config.__file__).parent / 'config.toml').read_text(encoding='utf-8')
        self.assertIn('\nbudget = false\n', toml)

    def test_the_gate_of_the_activation_stays_closed(self):
        # Lifting it is the activation of the budget, the operator's decision.
        source = Path(config.__file__).read_text()
        self.assertIn('\nBUDGET_EXTENSION = False\n', source)


class CommandTests(unittest.TestCase):
    def test_parsing(self):
        cmd = supervision.supervisor_command
        self.assertEqual(cmd('/devloop grant-budget issue troppo larga\n\nCausa: perimetro.'),
                         ('grant-budget', 'issue troppo larga\n\nCausa: perimetro.'))
        self.assertEqual(cmd('/devloop grant-budget\nCausa nel diario #76.'),
                         ('grant-budget', 'Causa nel diario #76.'))
        self.assertEqual(cmd('/devloop grant-budget   \n\n'), ('grant-budget', None))

    def test_the_class_is_not_critical_and_the_record_is_valid(self):
        self.assertFalse(supervision.critical(supervision.BUDGET_GRANTABLE))
        self.assertTrue(supervision.critical('budget'))
        body = markdown.render_supervision({'command': 'grant-budget', 'by': 'x',
                                            'reason': REASON, 'outcome': 'granted'}, 't')
        self.assertEqual(markdown.read_marker(body, 'supervision')['command'], 'grant-budget')
        self.assertIn('grant-budget <motivo>', markdown.SUPERVISOR_RESUME)


class DocsTests(unittest.TestCase):
    def test_the_delegation_is_documented(self):
        root = Path(config.__file__).parents[1]
        text = (root / 'docs' / 'devloop' / 'guide.md').read_text(encoding='utf-8')
        guide = ' '.join(text.split())
        self.assertIn('#### Extending the budget envelope', text)
        self.assertIn('**off by default**: `[delegations] budget = false`', guide)
        self.assertIn('- `/devloop grant-budget <reason>` extends once', guide)
        self.assertIn('**rounded down** (`floor`)', guide)
        reference = ' '.join((root / 'docs' / 'devloop' / 'reference.md').read_text(
            encoding='utf-8').split())
        self.assertIn('**The one extension** (#411, ADR 0005, P5-7)', reference)
        protocol = ' '.join((root / 'docs' / 'devloop' / 'protocol.md').read_text(
            encoding='utf-8').split())
        self.assertIn('`/devloop grant-budget` (#411)', protocol)


if __name__ == '__main__':
    unittest.main()
