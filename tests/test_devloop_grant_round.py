"""devloop: the supervisor grants a review round within the caps of the code (#405, ADR 0005, P5-1).

``[delegations] rounds`` (off by default) sends a PR at its round limit to the supervisor as
``round_grantable`` when every condition of the delegation holds; ``/devloop grant-round
<reason>`` raises the limit of that PR only, by one round, with a ``supervision`` record, a ledger
``event`` and a ``grant`` in the turn journal. The caps are constants of the code that the
configuration may only lower; a finding of the review says whether it concerns security, and one
that does not say counts as one.

On a ``round_limit`` escalation to the operator (#553) a trusted user's ``/devloop grant-round
<reason>`` grants one more round to that PR only, on the head the escalation recorded, recorded
as the rounds of the delegation but outside its caps.
"""

import dataclasses
import threading
import unittest

from devloop import acceptance, config, markdown, review, schema, supervision
from devloop.config import ConfigError, DelegationPolicy, parse_delegations
from devloop.journal import TurnJournal
from devloop.ledger import Ledger
from devloop.planning import Snapshot, findings_decreasing, last_counted_ci, plan
from helpers import ssdlc_project
from test_devloop import CFG, SHA1, SHA2, finding, norm, pr, raw_review
from test_devloop_supervision import (AUTO, HUMAN, REVIEW, SUPER, SUPERVISOR_LOGIN, Case, crash)

ON = dataclasses.replace(AUTO, max_review_rounds=1, delegations=DelegationPolicy(rounds=True))
OFF = dataclasses.replace(ON, delegations=DelegationPolicy())
REASON = ('La CI è fallita per un test instabile, non per la modifica: corretto con #999.\n'
          'Causa: CI.')
TRUSTED = 'brushtyler'


def ci_round(round_no=1, head=SHA1, previous_open=()):
    return review.ci_failure_review(failed_checks=['Unit Tests'], log_tail='boom',
                                    previous_open=list(previous_open), round_no=round_no,
                                    max_rounds=round_no, head_sha=head)


def granted_record(comment):
    return markdown.render_supervision({'command': 'grant-round', 'by': SUPERVISOR_LOGIN,
                                        'reason': 'x', 'comment': comment,
                                        'outcome': 'granted'}, 't')


class GrantCase(Case):
    def make(self, cfg=ON, *outputs, ledger=True):
        d = super().make(cfg, *outputs)
        if ledger:
            d.ledger = Ledger(self.gh)
            # The ledger's verification of the local tests reads the tree of the checkout.
            self.ws.tree = lambda path: 'e' * 40
        return d

    def record(self, record):
        self.gh.say(100, 'brushtyler', markdown.publishable('review', 'review', record))

    def snapshot_pr(self, d):
        (found,) = [p for p in d.snapshot().prs if p.number == 100]
        return found

    def at_limit(self, d, *records):
        """Record ``records`` in the thread of PR #100 and escalate it at its round limit."""
        for record in records:
            self.record(record)
        d._do_round_limit(self.snapshot_pr(d))
        return self.escalations(100)[-1]

    def body(self):
        return self.gh.threads[100][-1]['body']


class RoutingTests(GrantCase):
    def test_off_by_default_the_round_limit_stays_with_the_operator(self):
        self.assertFalse(ssdlc_project.load().delegations.rounds)
        self.assertFalse(AUTO.delegations.rounds)
        d = self.make(OFF)
        record = self.at_limit(d, ci_round())
        self.assertEqual((record['class'], record['to']), ('round_limit', 'operator'))
        # The head and the label to resume, for a trusted user's grant-round (#553).
        self.assertEqual((record['head'], record['resume']), (SHA1, REVIEW))
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertIn('alzando `max_review_rounds`', self.body())
        self.assertIn('Un utente fidato può concedere un round in più a questa sola PR con '
                      '`/devloop grant-round <motivo>`', self.body())
        self.assertNotIn('il supervisore può concedere', self.body())
        self.assertNotIn('andrebbe al supervisore', self.body())

    def test_a_grantable_limit_goes_to_the_supervisor(self):
        d = self.make()
        record = self.at_limit(d, ci_round())
        self.assertEqual({k: record[k] for k in ('class', 'critical', 'to', 'head', 'resume')},
                         {'class': 'round_grantable', 'critical': False, 'to': 'supervisor',
                          'head': SHA1, 'resume': REVIEW})
        self.assertNotIn('step', record)  # nothing a retry would run
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertIn('`/devloop grant-round <motivo>`', self.body())
        self.assertIn('analisi delle cause', self.body())
        # Nothing runs on the PR until the supervisor decides.
        self.assertEqual(self.tick(d), [])

    def test_an_open_security_finding_keeps_the_operator(self):
        d = self.make()
        record = self.at_limit(d, norm(raw_review('request_changes',
                                                  [finding(security=True)])))
        self.assertEqual((record['class'], record['to']), ('round_limit', 'operator'))
        self.assertIn('finding di sicurezza', self.body())
        self.assertIn('`1/', self.body())  # the key of the finding

    def test_a_finding_that_does_not_say_counts_as_security(self):
        old = ci_round()
        del old['blocking_findings'][0]['security']  # a record written before #405
        d = self.make()
        record = self.at_limit(d, old)
        self.assertEqual(record['class'], 'round_limit')
        self.assertIn('finding che non dicono se lo sono', self.body())

    def test_neither_a_ci_round_nor_fewer_findings(self):
        d = self.make()
        record = self.at_limit(d, norm(raw_review('request_changes', [finding()])))
        self.assertEqual((record['class'], record['to']), ('round_limit', 'operator'))
        self.assertIn('non sono meno di quelli del round precedente', self.body())
        self.assertIn('andrebbe al supervisore', self.body())

    def test_fewer_open_findings_than_the_round_before(self):
        cfg = dataclasses.replace(ON, max_review_rounds=2)
        first = norm(raw_review('request_changes', [finding('a'), finding('b')]))
        second = norm(raw_review('request_changes', [finding('c')], previous=[
            {'id': 'R1-1', 'status': 'resolved', 'note': 'ok'},
            {'id': 'R1-2', 'status': 'resolved', 'note': 'ok'}]),
            previous_open=review.open_findings([first]), round_no=2)
        d = self.make(cfg)
        self.assertEqual(self.at_limit(d, first, second)['class'], 'round_grantable')

    def test_a_pr_of_the_supervisors_agent_is_grantable_too(self):
        # A grant within the caps is an escalation of process (#527, principle 4 of ADR 0002
        # as clarified on #524): the supervisor decides it on its agent's PRs too.
        d = self.make(dataclasses.replace(ON, supervisor=CFG.implementer))
        record = self.at_limit(d, ci_round())
        self.assertEqual((record['class'], record['to']), ('round_grantable', 'supervisor'))
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertNotIn('il supervisore ha implementato', self.body())

    def test_what_makes_the_grant_the_operators(self):
        cases = {
            'phase:design': (ON, lambda gh: gh.labels[7].add('phase:design'), 'phase:design'),
            'a merge mode other than auto': (
                dataclasses.replace(ON, merge=config.MergePolicy(mode='auto',
                                                                 levels=((1, 'strict'),))),
                lambda gh: None, '`strict`'),
        }
        for name, (cfg, change, why) in cases.items():
            with self.subTest(case=name):
                d = self.make(cfg)
                change(self.gh)
                record = self.at_limit(d, ci_round())
                self.assertEqual((record['class'], record['to']), ('round_limit', 'operator'))
                self.assertIn(why, self.body())


class CapTests(GrantCase):
    def test_the_cap_of_the_pr(self):
        cfg = dataclasses.replace(ON, max_review_rounds=1)
        d = self.make(cfg)
        self.gh.say(100, 'brushtyler', granted_record(1))
        self.gh.say(100, 'brushtyler', granted_record(2))
        record = self.at_limit(d, ci_round(1), ci_round(2), ci_round(3))
        self.assertEqual(record['class'], 'round_limit')
        self.assertIn('già stati concessi 2 round', self.body())

    def test_the_configuration_lowers_the_cap_of_the_pr(self):
        cfg = dataclasses.replace(ON, delegations=DelegationPolicy(rounds=True,
                                                                   max_rounds_per_pr=1))
        d = self.make(cfg)
        self.gh.say(100, 'brushtyler', granted_record(1))
        record = self.at_limit(d, ci_round(1), ci_round(2))
        self.assertEqual(record['class'], 'round_limit')
        self.assertIn('`[delegations] max_rounds_per_pr` = 1', self.body())

    def test_the_daily_cap_counts_every_pr(self):
        d = self.make()
        for n in range(supervision.MAX_DAILY_GRANTS):
            d.ledger.journal.grant('rounds', 200 + n, 9000 + n, SUPERVISOR_LOGIN,
                                   supervision.MAX_DAILY_GRANTS)
        record = self.at_limit(d, ci_round())
        self.assertEqual(record['class'], 'round_limit')
        self.assertIn('il massimo giornaliero', self.body())

    def test_the_configuration_lowers_the_daily_cap(self):
        cfg = dataclasses.replace(ON, delegations=DelegationPolicy(rounds=True,
                                                                   max_grants_per_day=0))
        record = self.at_limit(self.make(cfg), ci_round())
        self.assertEqual(record['class'], 'round_limit')
        self.assertIn('`[delegations] max_grants_per_day` = 0', self.body())

    def test_without_a_journal_the_daily_cap_is_not_verifiable(self):
        record = self.at_limit(self.make(ledger=False), ci_round())
        self.assertEqual(record['class'], 'round_limit')
        self.assertIn('non sono verificabili', self.body())

    def test_two_grants_together_never_pass_the_daily_cap(self):
        # Two grant-round commands on different PRs, applied together (parallel actions), on
        # the last grant of the day: both pass the check of the conditions before either one
        # records its grant. The cap is checked again when the grant is recorded, in one step,
        # so the second one is refused and its PR goes to the operator.
        d = self.make()
        self.assertEqual(self.at_limit(d, ci_round())['class'], 'round_grantable')
        journal, cap = d.ledger.journal, supervision.MAX_DAILY_GRANTS
        for n in range(cap - 1):
            journal.grant('rounds', 200 + n, 9000 + n, SUPERVISOR_LOGIN, cap)
        check = d._round_grant
        other = []

        def together(p, reviews):
            route, why = check(p, reviews)
            self.assertIsNotNone(route)  # the last grant of the day is still free
            # The command on the other PR records its grant now, before this one.
            other.append(journal.grant('rounds', 300, 9999, SUPERVISOR_LOGIN, cap))
            return route, why

        d._round_grant = together
        self.gh.command(100, '/devloop grant-round ' + REASON)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(other, [cap])
        self.assertEqual(len(journal.grants()), cap)
        self.assertEqual([g['number'] for g in journal.grants()][-1], 300)
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertNotIn('granted', [r['outcome'] for r in self.decisions(100)])
        record = self.escalations(100)[-1]
        self.assertEqual((record['class'], record['to']), ('round_limit', 'operator'))
        self.assertIn('nel frattempo le concessioni di oggi hanno raggiunto il massimo '
                      'giornaliero', self.body())
        self.assertEqual([r for r in self.gh.records(100, 'ledger')
                          if r.get('event') == 'delegation_grant'], [])

    def test_the_journal_checks_the_cap_and_records_the_grant_in_one_step(self):
        journal, cap = TurnJournal(), supervision.MAX_DAILY_GRANTS
        for n in range(cap - 1):
            self.assertEqual(journal.grant('rounds', 200 + n, 9000 + n, 'x', cap), n + 1)
        start, results = threading.Barrier(2), {}

        def grant(number):
            start.wait()
            results[number] = journal.grant('rounds', number, number, 'x', cap)

        threads = [threading.Thread(target=grant, args=(n,)) for n in (100, 101)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results.values(), key=str), [cap, None])
        self.assertEqual(len(journal.grants()), cap)
        # Nothing is recorded past the cap; the same command counts once.
        (winner,) = [n for n, daily in results.items() if daily == cap]
        self.assertIsNone(journal.grant('rounds', 102, 102, 'x', cap))
        self.assertEqual(journal.grant('rounds', winner, winner, 'x', cap), cap)
        self.assertEqual(len(journal.grants()), cap)

    def test_the_caps_are_constants_the_configuration_may_only_lower(self):
        self.assertEqual((supervision.MAX_GRANTED_ROUNDS, supervision.MAX_DAILY_GRANTS), (2, 3))
        self.assertEqual(parse_delegations({}),
                         DelegationPolicy(retry_own_prs=False, rounds=False, max_rounds_per_pr=2,
                                          max_grants_per_day=3))
        self.assertEqual(parse_delegations({'rounds': True, 'max_rounds_per_pr': 1,
                                            'max_grants_per_day': 0, 'retry_own_prs': True}),
                         DelegationPolicy(retry_own_prs=True, rounds=True, max_rounds_per_pr=1,
                                          max_grants_per_day=0))
        for table in ({'max_rounds_per_pr': 3}, {'max_grants_per_day': 4},
                      {'max_rounds_per_pr': -1}, {'max_grants_per_day': True},
                      {'rounds': 'yes'}, {'max_rounds': 1}):
            with self.subTest(table=table):
                with self.assertRaises(ConfigError):
                    parse_delegations(table)
        # The local overlay may set the keys, and is held to the same caps.
        data = ssdlc_project.data()
        on = config.apply_overlay(data, {'delegations': {'rounds': True}})
        self.assertTrue(config.parse_config(on).delegations.rounds)
        with self.assertRaises(ConfigError):
            config.parse_config(config.apply_overlay(
                data, {'delegations': {'max_rounds_per_pr': 5}}))
        # Revoked from the next tick, without a restart.
        self.assertIn('delegations', config.LIVE_KEYS)


class GrantTests(GrantCase):
    def escalate(self, d):
        self.assertEqual(self.at_limit(d, ci_round())['class'], 'round_grantable')

    def test_a_grant_raises_the_limit_of_the_pr_only(self):
        d = self.make()
        self.escalate(d)
        self.gh.command(100, '/devloop grant-round ' + REASON)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        (decision,) = self.decisions(100)
        self.assertEqual({k: decision[k] for k in ('command', 'by', 'outcome', 'class', 'head',
                                                   'granted', 'cap', 'left', 'daily',
                                                   'daily_cap', 'daily_left')},
                         {'command': 'grant-round', 'by': SUPERVISOR_LOGIN,
                          'outcome': 'granted', 'class': 'round_grantable', 'head': SHA1,
                          'granted': 1, 'cap': 2, 'left': 1, 'daily': 1, 'daily_cap': 3,
                          'daily_left': 2})
        self.assertEqual(decision['reason'], REASON)  # the analysis of the causes, whole
        events = [r for r in self.gh.records(100, 'ledger')
                  if r.get('event') == 'delegation_grant']
        self.assertEqual(len(events), 1)
        self.assertEqual({k: events[0][k] for k in ('delegation', 'by', 'granted', 'cap', 'left',
                                                    'daily', 'daily_cap', 'daily_left')},
                         {'delegation': 'rounds', 'by': SUPERVISOR_LOGIN, 'granted': 1,
                          'cap': 2, 'left': 1, 'daily': 1, 'daily_cap': 3, 'daily_left': 2})
        (grant,) = d.ledger.journal.grants()
        self.assertEqual((grant['delegation'], grant['number'], grant['by']),
                         ('rounds', 100, SUPERVISOR_LOGIN))
        # The limit of the PR is max_review_rounds plus the round granted; the setting is
        # unchanged.
        self.assertEqual(d.cfg.max_review_rounds, 1)
        p = self.snapshot_pr(d)
        self.assertEqual(p.granted_rounds, 1)
        kinds = [(a.kind, a.number) for a in plan(d.snapshot(), d.cfg)]
        self.assertNotIn(('round_limit', 100), kinds)
        self.assertIn(('apply_review', 100), kinds)
        # The command is spent: it is never applied again.
        self.assertNotIn(('supervise', 100), self.tick(d))
        self.assertEqual(len(self.decisions(100)), 1)

    def test_a_grant_without_a_reason_is_answered_and_not_applied(self):
        d = self.make()
        self.escalate(d)
        self.gh.command(100, '/devloop grant-round')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('manca il testo', self.body())
        self.assertEqual(d.ledger.journal.grants(), [])

    def test_a_grant_after_the_revocation_goes_to_the_operator(self):
        d = self.make()
        self.escalate(d)
        d.cfg = OFF  # the operator turned the delegation off while it waited
        self.gh.command(100, '/devloop grant-round ' + REASON)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertEqual(self.escalations(100)[-1]['class'], 'round_limit')
        self.assertTrue(self.escalations(100)[-1]['critical'])
        self.assertEqual(d.ledger.journal.grants(), [])

    def test_a_grant_rechecks_the_conditions_on_the_thread_read_now(self):
        d = self.make()
        self.escalate(d)
        # A security finding, recorded after the escalation in the same round.
        record = norm(raw_review('request_changes', [finding(security=True)]))
        self.record(record)
        self.gh.command(100, '/devloop grant-round ' + REASON)
        self.tick(d)
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('sicurezza', self.gh.threads[100][-2]['body'])

    def test_a_grant_of_a_superseded_head_is_refused(self):
        d = self.make()
        self.escalate(d)
        self.gh.head = SHA2
        self.gh.command(100, '/devloop grant-round ' + REASON)
        self.tick(d)
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('input superato', self.body())

    def test_a_grant_on_another_class_is_refused(self):
        d = self.make(ON, crash())
        self.assertEqual(self.tick(d), [('review', 100)])
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.gh.command(100, '/devloop grant-round ' + REASON)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('vale solo per un\'escalation `round_grantable`', self.body())
        self.assertIn('`/devloop retry`', self.body())

    def test_a_retry_on_a_grantable_limit_is_refused(self):
        d = self.make()
        self.escalate(d)
        self.gh.command(100, '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('`/devloop grant-round <motivo>`', self.body())

    def test_a_round_that_reaches_the_limit_is_routed_too(self):
        # The limit reached by applying a round that asked for changes: the round decreased the
        # open findings, so the next one is the supervisor's to grant.
        cfg = dataclasses.replace(ON, max_review_rounds=2)
        first = norm(raw_review('request_changes', [finding('a'), finding('b')]))
        second = raw_review('request_changes', [finding('c')], previous=[
            {'id': 'R1-1', 'status': 'resolved', 'note': 'ok'},
            {'id': 'R1-2', 'status': 'resolved', 'note': 'ok'}])
        d = self.make(cfg, second)
        self.record(first)
        self.gh.say(100, 'brushtyler', markdown.publishable('fix', 'fix', {
            'round': 1, 'head_sha': SHA1, 'responses': []}))
        self.assertEqual(self.tick(d), [('review', 100)])
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual(self.escalations(100)[-1]['class'], 'round_grantable')

    def test_a_round_that_reaches_the_limit_without_the_conditions(self):
        d = self.make(ON, raw_review('request_changes', [finding()]))
        self.assertEqual(self.tick(d), [('review', 100)])
        self.assertEqual(self.gh.labels[100], {HUMAN})
        record = self.escalations(100)[-1]
        self.assertEqual(record['class'], 'round_limit')
        self.assertIn('la delega dei round non si applica', self.body())


class OperatorGrantTests(GrantCase):
    """A trusted user's ``/devloop grant-round <reason>`` on a ``round_limit`` escalation to the
    operator (#553): one more round for that PR only, outside the caps of the delegation."""

    def escalate(self, d, *records):
        record = self.at_limit(d, *(records or (ci_round(),)))
        self.assertEqual((record['class'], record['to'], record['head']),
                         ('round_limit', 'operator', SHA1))
        self.assertEqual(self.gh.labels[100], {HUMAN})
        return record

    def grant(self, d, text='/devloop grant-round ' + REASON, login=TRUSTED):
        self.gh.command(100, text, login=login)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        return self.decisions(100)[-1]

    def trace(self):
        """The text of the latest ``supervision`` record of PR #100."""
        return [c['body'] for c in self.gh.threads[100]
                if markdown.read_marker(c['body'], 'supervision')][-1]

    def grant_events(self):
        return [r for r in self.gh.records(100, 'ledger') if r.get('event') == 'delegation_grant']

    def test_a_trusted_user_grants_one_round_to_this_pr_only(self):
        d = self.make(OFF)
        self.escalate(d)
        decision = self.grant(d)
        self.assertEqual(self.gh.labels[100], {REVIEW})
        self.assertEqual({k: decision.get(k) for k in ('command', 'by', 'outcome', 'class',
                                                       'head', 'granted', 'operator')},
                         {'command': 'grant-round', 'by': TRUSTED, 'outcome': 'granted',
                          'class': 'round_limit', 'head': SHA1, 'granted': 1, 'operator': True})
        self.assertIn('il suo limite passa a 2 round', self.trace())
        # The setting does not change: the limit of this PR only is raised.
        self.assertEqual(d.cfg.max_review_rounds, 1)
        p = self.snapshot_pr(d)
        self.assertEqual((p.granted_rounds, p.operator_rounds), (1, 1))
        # Another PR at the limit keeps it: nothing was granted to it.
        other = pr(number=101, issue=8, reviews=[norm(raw_review('request_changes', [finding()]))],
                   fixes=[{'round': 1, 'head_sha': SHA1, 'responses': []}], head=SHA2)
        kinds = [(a.kind, a.number) for a in plan(Snapshot([p, other], [], []), d.cfg)]
        self.assertNotIn(('round_limit', 100), kinds)
        self.assertIn(('round_limit', 101), kinds)
        # The command is spent: it is never applied again.
        self.assertNotIn(('supervise', 100), self.tick(d))
        self.assertEqual(len(self.decisions(100)), 1)

    def test_the_round_is_recorded_as_those_of_the_delegation_with_its_author(self):
        d = self.make(OFF)
        self.escalate(d)
        decision = self.grant(d)
        self.assertEqual(decision['reason'], REASON)  # the reason, whole
        (comment,) = [c['id'] for c in self.gh.threads[100] if c['body'].endswith(REASON)]
        self.assertEqual(decision['comment'], comment)
        (event,) = self.grant_events()
        self.assertEqual({k: event.get(k) for k in ('delegation', 'by', 'comment', 'head',
                                                    'granted', 'operator')},
                         {'delegation': 'operator_rounds', 'by': TRUSTED, 'comment': comment,
                          'head': SHA1, 'granted': 1, 'operator': True})
        (grant,) = d.ledger.journal.operator_grants()
        self.assertEqual((grant['delegation'], grant['number'], grant['comment'], grant['by']),
                         ('operator_rounds', 100, comment, TRUSTED))
        # Outside the daily cap of the delegations.
        self.assertEqual(d.ledger.journal.grants(), [])

    def test_the_caps_of_the_delegation_stay_apart(self):
        # A round granted by a trusted user takes neither the cap of the PR nor the daily cap
        # of the delegation: at the next limit the supervisor may still grant its own.
        d = self.make(ON)
        self.gh.say(100, 'brushtyler', granted_record(1))
        self.gh.say(100, 'brushtyler', granted_record(2))
        self.escalate(d, ci_round(1), ci_round(2), ci_round(3))
        self.assertIn('già stati concessi 2 round', self.body())
        self.assertEqual(self.grant(d)['outcome'], 'granted')
        self.assertEqual(self.snapshot_pr(d).granted_rounds, 3)
        self.assertEqual(d.ledger.journal.grants(), [])
        # The supervisor's cap of the PR counts its own rounds only: after a trusted user's
        # round, the delegation (turned on later) still has both of its own.
        d2 = self.make(OFF)
        self.escalate(d2)
        self.assertEqual(self.grant(d2)['outcome'], 'granted')
        d2.cfg = ON
        record = self.at_limit(d2, ci_round(2))
        self.assertEqual((record['class'], record['granted']), ('round_grantable', 0))
        self.gh.command(100, '/devloop grant-round ' + REASON)
        self.assertEqual(self.tick(d2), [('supervise', 100)])
        decision = self.decisions(100)[-1]
        self.assertEqual({k: decision[k] for k in ('outcome', 'granted', 'cap', 'left', 'daily')},
                         {'outcome': 'granted', 'granted': 1, 'cap': 2, 'left': 1, 'daily': 1})
        self.assertIn('il suo limite passa a 3 round', self.trace())

    def test_without_a_reason_it_is_refused(self):
        d = self.make(OFF)
        self.escalate(d)
        decision = self.grant(d, '/devloop grant-round')
        self.assertEqual(decision['outcome'], 'refused')
        self.assertIn('manca il testo', self.body())
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(d.ledger.journal.operator_grants(), [])
        self.assertEqual(self.grant_events(), [])

    def test_on_a_changed_head_it_is_refused(self):
        d = self.make(OFF)
        self.escalate(d)
        self.gh.head = SHA2
        decision = self.grant(d)
        self.assertEqual(decision['outcome'], 'refused')
        self.assertIn('input superato', self.body())
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(d.ledger.journal.operator_grants(), [])
        self.assertEqual(self.grant_events(), [])

    def test_on_an_escalation_of_another_class_it_is_refused(self):
        for cls in ('open_findings', 'supervisor_escalated', 'round_grantable'):
            with self.subTest(cls=cls):
                d = self.make(OFF)
                self.gh.labels[100] = {HUMAN}
                self.gh.say(100, 'brushtyler', markdown.render_escalation(
                    'x', cls=cls, extra={'head': SHA1, 'resume': REVIEW}))
                decision = self.grant(d)
                self.assertEqual(decision['outcome'], 'refused')
                self.assertIn('su un\'escalation di altra classe non vale', self.body())
                self.assertEqual(self.gh.labels[100], {HUMAN})
                self.assertEqual(d.ledger.journal.operator_grants(), [])

    def test_on_a_round_limit_that_records_no_head_it_is_refused(self):
        # An escalation written before #553.
        d = self.make(OFF)
        self.gh.labels[100] = {HUMAN}
        self.gh.say(100, 'brushtyler', markdown.render_escalation('x', cls='round_limit'))
        decision = self.grant(d)
        self.assertEqual(decision['outcome'], 'refused')
        self.assertIn('non registra il head', self.body())
        self.assertEqual(self.gh.labels[100], {HUMAN})

    def test_the_supervisors_command_does_not_count(self):
        d = self.make(ON)
        self.gh.say(100, 'brushtyler', granted_record(1))
        self.gh.say(100, 'brushtyler', granted_record(2))
        self.escalate(d, ci_round(1), ci_round(2), ci_round(3))
        decision = self.grant(d, login=SUPERVISOR_LOGIN)
        self.assertEqual(decision['outcome'], 'refused')
        self.assertIn('affidata all\'operatore', self.body())
        self.assertEqual(self.gh.labels[100], {HUMAN})

    def test_without_a_journal_nothing_is_granted(self):
        d = self.make(OFF, ledger=False)
        self.escalate(d)
        decision = self.grant(d)
        self.assertEqual(decision['outcome'], 'refused')
        self.assertIn('journal dei turni non è disponibile', self.body())
        self.assertEqual(self.gh.labels[100], {HUMAN})


class RoundLimitTests(unittest.TestCase):
    def test_the_limit_of_a_pr_counts_its_granted_rounds(self):
        cfg = dataclasses.replace(CFG, max_review_rounds=1)
        rounds = [norm(raw_review('request_changes', [finding()]))]
        fixes = [{'round': 1, 'head_sha': SHA1, 'responses': []}]
        waiting = pr(reviews=rounds, fixes=fixes, head=SHA2)
        self.assertEqual([a.kind for a in plan(Snapshot([waiting], [], []), cfg)],
                         ['round_limit'])
        waiting.granted_rounds = 1
        self.assertEqual([a.kind for a in plan(Snapshot([waiting], [], []), cfg)], ['review'])

    def test_the_latest_counted_round_and_the_trend_of_findings(self):
        agent = norm(raw_review('request_changes', [finding('a'), finding('b')]))
        self.assertFalse(last_counted_ci([agent]))
        self.assertTrue(last_counted_ci([agent, ci_round(2)]))
        self.assertFalse(last_counted_ci([]))
        self.assertFalse(findings_decreasing([agent]))
        fewer = norm(raw_review('request_changes', [], previous=[
            {'id': 'R1-1', 'status': 'resolved', 'note': 'ok'},
            {'id': 'R1-2', 'status': 'unresolved', 'note': 'no'}]),
            previous_open=review.open_findings([agent]), round_no=2)
        self.assertTrue(findings_decreasing([agent, fewer]))
        same = norm(raw_review('request_changes', [finding('c')], previous=[
            {'id': 'R1-1', 'status': 'resolved', 'note': 'ok'},
            {'id': 'R1-2', 'status': 'unresolved', 'note': 'no'}]),
            previous_open=review.open_findings([agent]), round_no=2)
        self.assertFalse(findings_decreasing([agent, same]))


class SecurityFieldTests(unittest.TestCase):
    def test_every_finding_of_the_review_says_whether_it_is_of_security(self):
        review_schema = schema.load_schema('review')
        for kind in ('blocking_findings', 'non_blocking_findings'):
            item = review_schema['properties'][kind]['items']
            self.assertIn('security', item['required'])
            self.assertEqual(item['properties']['security'], {'type': 'boolean'})
            raw = raw_review('request_changes', [finding()])
            raw[kind] = [{k: v for k, v in finding().items() if k != 'security'}]
            with self.subTest(kind=kind), self.assertRaises(schema.SchemaError):
                review.normalize_review(raw, previous_open=[], round_no=1, max_rounds=3,
                                        head_sha=SHA1, ci='pass', reviewer='codex')
        record = norm(raw_review('request_changes', [finding(security=True)]))
        self.assertIs(record['blocking_findings'][0]['security'], True)

    def test_the_findings_devloop_writes_are_not_of_security(self):
        self.assertIs(ci_round()['blocking_findings'][0]['security'], False)
        derived = acceptance.acceptance_findings(['criterio'], None)
        self.assertTrue(derived)
        self.assertTrue(all(f['security'] is False for f in derived))

    def test_the_published_review_marks_a_security_finding(self):
        record = norm(raw_review('request_changes', [finding('falla', security=True),
                                                     finding('stile', line=4)]))
        text = markdown.render_review(record, SHA1, language='Italian')
        self.assertIn('falla *(sicurezza)*', text)
        self.assertNotIn('stile *(sicurezza)*', text)
        # In English by default (#632).
        text = markdown.render_review(record, SHA1)
        self.assertIn('falla *(security)*', text)
        self.assertNotIn('stile *(security)*', text)

    def test_the_review_prompt_asks_for_the_field(self):
        from devloop.context import PROMPT_DIR
        prompt = ' '.join((PROMPT_DIR / 'review.md').read_text(encoding='utf-8').split())
        self.assertIn('set `security` to `true` on every finding', prompt)
        self.assertIn('When in doubt, set it to `true`', prompt)


class CommandTests(unittest.TestCase):
    def test_parsing(self):
        cmd = supervision.supervisor_command
        self.assertEqual(cmd('/devloop grant-round CI instabile\n\nCausa: CI.'),
                         ('grant-round', 'CI instabile\n\nCausa: CI.'))
        self.assertEqual(cmd('/devloop grant-round\nCausa nel diario #76.'),
                         ('grant-round', 'Causa nel diario #76.'))
        self.assertEqual(cmd('/devloop grant-round   \n\n'), ('grant-round', None))

    def test_a_grant_record_is_valid(self):
        body = markdown.render_supervision({'command': 'grant-round', 'by': 'x',
                                            'reason': REASON, 'outcome': 'granted',
                                            'granted': 1}, 't')
        self.assertEqual(markdown.read_marker(body, 'supervision')['outcome'], 'granted')
        self.assertFalse(supervision.critical(supervision.ROUND_GRANTABLE))
        self.assertTrue(supervision.critical('round_limit'))


class DocsTests(unittest.TestCase):
    def test_the_delegation_is_documented(self):
        from devloop.context import PROMPT_DIR
        root = PROMPT_DIR.parents[1]
        guide = ' '.join((root / 'docs' / 'devloop' / 'guide.md').read_text(
            encoding='utf-8').split())
        self.assertIn('#### Granting a review round', (root / 'docs' / 'devloop' / 'guide.md')
                      .read_text(encoding='utf-8'))
        self.assertIn('**off by default**: `[delegations] rounds = false`', guide)
        self.assertIn('- `/devloop grant-round <reason>` grants one more review round', guide)
        reference = ' '.join((root / 'docs' / 'devloop' / 'reference.md').read_text(
            encoding='utf-8').split())
        self.assertIn('`[delegations]` (#405, ADR 0005)', reference)
        toml = (root / 'devloop' / 'config.toml').read_text(encoding='utf-8')
        self.assertIn('[delegations]\nrounds = false\n', toml)

    def test_the_round_granted_on_an_escalation_to_the_operator_is_documented(self):
        from devloop.context import PROMPT_DIR
        root = PROMPT_DIR.parents[1]
        text = (root / 'docs' / 'devloop' / 'guide.md').read_text(encoding='utf-8')
        self.assertIn('#### Granting a review round on an escalation to the operator', text)
        guide = ' '.join(text.split())
        self.assertIn('- `/devloop grant-round <reason>`, when that record is a `round_limit` '
                      'escalation of a PR', guide)
        self.assertIn('**outside the caps of the delegation**', guide)
        self.assertNotIn('the operator grants more rounds by raising `max_review_rounds`, as '
                         'before', guide)
        reference = ' '.join((root / 'docs' / 'devloop' / 'reference.md').read_text(
            encoding='utf-8').split())
        self.assertIn('`round_limit` escalation (#553', reference)
        self.assertIn('`MAX_COORDINATOR_OPERATOR_ROUNDS` (1) per PR', reference)
        self.assertEqual(supervision.MAX_COORDINATOR_OPERATOR_ROUNDS, 1)


if __name__ == '__main__':
    unittest.main()
