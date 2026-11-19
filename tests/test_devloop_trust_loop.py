"""devloop: a ``needs_operator`` for the trust policy does not stop the loop (#434).

A reviewer says why it asks for the operator (``operator_reason``): ``trust_policy`` or
``decision``. A ``trust_policy`` with blocking findings goes to the fix as a request for changes,
within the usual round limit; without findings, and with green CI, the PR goes to the operator
once, in ``agent:ready-to-merge`` + ``agent:needs-operator``, for the review and the merge. A
``decision``, or a ``needs_operator`` without the field, is still an immediate escalation."""

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from devloop import agents, markdown, review
from devloop.config import MergePolicy
from devloop.dispatcher import Dispatcher
from devloop.planning import Action, Snapshot, ci_joins_round, plan, waiting_reviewers
from devloop.schema import SchemaError, with_defaults
from test_devloop import (CFG, REV, REV_NAME, SHA1, FakeGH, FakeRunner, FakeWS, finding, norm,
                          pr, raw_review)
from test_devloop_merge import MERGE, READY, STRICT, Case, MergeGH, header, records

OPERATOR = 'agent:needs-operator'
PARTS = 'Tocca la policy di fiducia: punto 4, `devloop/dispatcher.py` (condizioni di merge).'


def trust(blocking=(), previous=(), human=PARTS):
    return raw_review('needs_operator', blocking, previous, human=human, reason='trust_policy')


def without_reason(raw):
    """A review output as written before #434, without ``operator_reason``."""
    return {k: v for k, v in raw.items() if k != 'operator_reason'}


def escalations(gh):
    found = (markdown.read_marker(b, 'notice') for _, b in gh.comments)
    return [r for r in found if r and r.get('event') == 'escalation']


def cli(answer):
    """The ``run`` of a real ``AgentRunner``: ``answer`` as the final message of the turn, in the
    envelope of each kind of CLI."""
    def run(argv, **kw):
        if argv[-1] == '--version':
            return SimpleNamespace(returncode=0, stdout='2.1.322 (Claude Code)', stderr='')
        for arg in argv:  # codex writes its final message in a file
            if str(arg).endswith('.last.json'):
                Path(arg).write_text(json.dumps(answer), encoding='utf-8')
        return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps(
            {'is_error': False, 'status': 'SUCCESS', 'structured_output': answer}))
    return run


class NormalizeTests(unittest.TestCase):
    def test_a_trust_policy_is_recorded_and_acts_on_its_findings(self):
        fixing = norm(trust([finding()]))
        self.assertEqual((fixing['verdict'], fixing['operator_reason']),
                         ('needs_operator', 'trust_policy'))
        self.assertTrue(review.trust_policy(fixing))
        self.assertEqual(review.flow_verdict(fixing), 'request_changes')
        self.assertEqual(review.round_verdict([fixing]), 'request_changes')
        clean = norm(trust())
        self.assertEqual(review.flow_verdict(clean), 'approve')
        self.assertEqual(review.round_verdict([clean]), 'approve')
        # An unresolved finding of its own from an earlier round keeps the fix going.
        first = dict(norm(trust([finding()])), round=1)
        later = review.normalize_review(
            trust(previous=[{'id': '1/' + REV + '/R1-1', 'status': 'unresolved', 'note': 'no'}]),
            previous_open=review.open_findings([first]), round_no=2, max_rounds=3,
            head_sha=SHA1, ci='pass', reviewer=REV)
        self.assertEqual(review.flow_verdict(later), 'request_changes')

    def test_a_decision_or_no_reason_stays_a_decision(self):
        # ``none`` (#441) is the value of the other verdicts: with ``needs_operator``, a decision.
        for raw in (raw_review('needs_operator', human='scelta di prodotto', reason='decision'),
                    raw_review('needs_operator', human='scelta di prodotto', reason='none'),
                    without_reason(raw_review('needs_operator', human='scelta di prodotto'))):
            with self.subTest(raw=raw.get('operator_reason')):
                record = norm(raw)
                self.assertEqual(record['operator_reason'], 'decision')
                self.assertFalse(review.trust_policy(record))
                self.assertEqual(review.flow_verdict(record), 'needs_operator')
                self.assertEqual(review.round_verdict([norm(trust()), record]), 'needs_operator')
        # A record published before #434 carries no field: a decision.
        old = {k: v for k, v in norm(trust()).items() if k != 'operator_reason'}
        self.assertEqual(review.flow_verdict(old), 'needs_operator')
        # A legacy verdict is read under its current name, as before.
        self.assertEqual(review.flow_verdict(dict(old, verdict=review.LEGACY_VERDICT)),
                         'needs_operator')

    def test_a_trust_policy_with_another_verdict_becomes_needs_operator(self):
        for verdict in ('approve', 'request_changes'):
            with self.subTest(verdict=verdict):
                record = norm(raw_review(verdict, [finding()], human=PARTS,
                                         reason='trust_policy'))
                self.assertEqual((record['verdict'], record['operator_reason']),
                                 ('needs_operator', 'trust_policy'))
                self.assertIn('policy di fiducia', record['note'])
        # The reason is recorded only with needs_operator.
        self.assertNotIn('operator_reason', norm(raw_review(reason='decision')))

    def test_the_trust_policy_parts_are_one_per_reviewer(self):
        first = dict(norm(trust([finding()], human='punto 4')), round=1)
        second = dict(norm(trust(human='punto 4 e punto 5'),
                           previous_open=review.open_findings([first]), round_no=2), round=2)
        self.assertEqual(review.trust_policy_parts([first, second]),
                         [(REV, 2, 'punto 4 e punto 5')])
        self.assertEqual(review.trust_policy_parts([dict(norm(raw_review()), round=3)]), [])


def resolving(earlier, round_no=2, raw=None, reviewer=REV):
    """The record of ``reviewer`` in round ``round_no`` that closes every finding ``earlier``
    left open, with the verdict of ``raw`` (an approval by default)."""
    left = review.open_findings(earlier)
    raw = dict(raw or raw_review(), previous_findings=[
        {'id': f['key'], 'status': 'resolved', 'note': 'ok'} for f in left])
    record = review.normalize_review(raw, previous_open=left, round_no=round_no, max_rounds=3,
                                     head_sha=SHA1, ci='pass', reviewer=reviewer)
    return dict(record, receipt=norm(raw_review())['receipt'])


class LatestRoundTests(unittest.TestCase):
    """The operator's decision of 2026-11-19 (#461): a ``trust_policy`` of an earlier round no
    longer holds the merge when the reviewer who gave it approves the head of the latest round,
    it closed its own findings itself, and no reviewer of that round gives ``trust_policy``."""

    def setUp(self):
        self.first = norm(trust([finding()]))

    def test_the_same_reviewer_approving_the_latest_round_clears_it(self):
        self.assertEqual(review.trust_policy_parts([self.first, resolving([self.first])]), [])
        # Without findings of its own there is nothing to close.
        clean = norm(trust())
        self.assertEqual(review.trust_policy_parts([clean, resolving([clean])]), [])

    def test_a_trust_policy_in_the_latest_round_keeps_every_one(self):
        again = resolving([self.first], raw=trust(human='punto 5'))
        self.assertEqual(review.trust_policy_parts([self.first, again]), [(REV, 2, 'punto 5')])
        # Another reviewer of the latest round says so: the first one stays too.
        other = dict(norm(trust(human='punto 5'), round_no=2), reviewer='other')
        self.assertEqual(review.trust_policy_parts([self.first, resolving([self.first]), other]),
                         [(REV, 1, PARTS), ('other', 2, 'punto 5')])
        # Even in a record that duplicates the reviewer's approval of the round.
        duplicate = norm(trust(human='punto 5'), round_no=2)
        self.assertEqual(
            review.trust_policy_parts([self.first, resolving([self.first]), duplicate]),
            [(REV, 2, 'punto 5')])

    def test_it_stays_without_the_approval_of_the_same_reviewer(self):
        # The latest round has only another reviewer's approval.
        other = dict(resolving([self.first]), reviewer='other')
        self.assertEqual(review.trust_policy_parts([self.first, other]), [(REV, 1, PARTS)])
        # The latest round is a CI round.
        ci = review.ci_failure_review(failed_checks=['tests'], log_tail='', round_no=2,
                                      previous_open=review.open_findings([self.first]),
                                      max_rounds=3, head_sha=SHA1)
        self.assertEqual(review.trust_policy_parts([self.first, ci]), [(REV, 1, PARTS)])

    def test_it_stays_when_another_closed_the_findings(self):
        # The operator closed R1-1 (a ``finding`` record): the reviewer's approval of round 2
        # did not close it.
        approval = norm(raw_review(), round_no=2)
        self.assertEqual(review.open_findings([self.first, approval],
                                              [{'key': f'1/{REV}/R1-1', 'by': 'operator'}]), [])
        self.assertEqual(review.trust_policy_parts([self.first, approval]), [(REV, 1, PARTS)])
        # Another reviewer's assessment closes nothing either.
        foreign = resolving([self.first], reviewer='other')
        self.assertEqual(review.trust_policy_parts([self.first, foreign, approval]),
                         [(REV, 1, PARTS)])

    def test_a_finding_carried_by_the_trust_policy_must_be_closed_too(self):
        # Round 1 asks for changes; round 2 gives trust_policy and leaves R1-1 unresolved; the
        # operator closes it, and round 3 approves without closing it.
        changes = norm(raw_review('request_changes', [finding()]))
        carried = norm(trust(previous=[{'id': f'1/{REV}/R1-1', 'status': 'unresolved',
                                        'note': 'no'}]),
                       previous_open=review.open_findings([changes]), round_no=2)
        approval = norm(raw_review(), round_no=3)
        self.assertEqual(review.trust_policy_parts([changes, carried, approval]),
                         [(REV, 2, PARTS)])
        closed = resolving([changes, carried], round_no=3)
        self.assertEqual(review.trust_policy_parts([changes, carried, closed]), [])

    def test_the_review_says_the_loop_goes_on(self):
        body = markdown.render_review(norm(trust([finding()])), header(REV_NAME),
                                      native='REQUEST_CHANGES')
        self.assertIn('Policy di fiducia: review e merge dell\'operatore', body)
        self.assertIn('### Policy di fiducia toccata', body)
        self.assertIn('devloop non si ferma', body)
        self.assertNotIn('Decisione richiesta', body)
        self.assertEqual(markdown.native_event(norm(trust([finding()]))), 'REQUEST_CHANGES')
        self.assertEqual(markdown.native_event(norm(trust())), 'APPROVE')
        decision = markdown.render_review(norm(raw_review('needs_operator', human='scelta')),
                                          header(REV_NAME))
        self.assertIn('### Decisione richiesta', decision)


class ReviewRoundTests(unittest.TestCase):
    def make(self, runner):
        self.gh, self.ws = FakeGH(), FakeWS()
        return Dispatcher(CFG, self.gh, self.ws, runner, log=lambda m: None)

    def review(self, raw, **kw):
        d = self.make(FakeRunner(raw))
        p = pr(**kw)
        self.gh.labels[100] = set(p.labels)
        d._do_review(p)
        return d

    def test_a_trust_policy_with_findings_starts_the_fix_without_escalation(self):
        self.review(trust([finding()]))
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        self.assertEqual(escalations(self.gh), [])
        self.assertEqual(len(self.gh.comments), 1)  # the review alone
        record = markdown.read_marker(self.gh.comments[0][1], 'review')
        self.assertEqual(record['operator_reason'], 'trust_policy')
        # The next plan fixes it, as a request for changes.
        p = pr(labels=['agent:changes'], reviews=[record])
        self.assertEqual(plan(Snapshot([p], [], []), CFG), [Action('fix', 100)])

    def test_a_decision_or_no_reason_escalates_at_once(self):
        for raw in (raw_review('needs_operator', [finding()], human='scelta', reason='decision'),
                    without_reason(raw_review('needs_operator', [finding()], human='scelta'))):
            with self.subTest(reason=raw.get('operator_reason')):
                self.review(raw)
                self.assertEqual(self.gh.labels[100], {OPERATOR})
                (escalation,) = escalations(self.gh)
                self.assertEqual(escalation['class'], 'reviewer_decision')

    def test_a_decision_beside_a_trust_policy_escalates_at_once(self):
        d = self.make(FakeRunner())
        records = [norm(trust()), norm(raw_review('needs_operator', human='scelta',
                                                  reason='decision'))]
        p = pr()
        self.gh.labels[100] = set(p.labels)
        d._apply_round(p, records, announce=True, reviews=records)
        self.assertEqual(self.gh.labels[100], {OPERATOR})
        self.assertEqual(escalations(self.gh)[0]['class'], 'reviewer_decision')

    def test_a_cli_answer_without_the_field_escalates_through_the_real_runner(self):
        # The real ``AgentRunner`` checks the output against the strict schema before
        # ``normalize_review``: a review without ``operator_reason`` is still a decision, an
        # escalation ``reviewer_decision``, not a failure of the tool.
        raw = without_reason(raw_review('needs_operator', [finding()], human='scelta'))
        with tempfile.TemporaryDirectory() as tmp:
            d = self.make(agents.AgentRunner(Path(tmp) / 'logs', 10, run=cli(raw)))
            p = pr()
            self.gh.labels[100] = set(p.labels)
            d._do_review(p)
        self.assertEqual(self.gh.labels[100], {OPERATOR})
        (escalation,) = escalations(self.gh)
        self.assertEqual(escalation['class'], 'reviewer_decision')
        record = markdown.read_marker(self.gh.comments[0][1], 'review')
        self.assertEqual((record['verdict'], record['operator_reason']),
                         ('needs_operator', 'decision'))

    def test_the_runner_fills_in_the_field_and_keeps_the_schema_strict(self):
        def turn(raw, kind):
            with tempfile.TemporaryDirectory() as tmp:
                return agents.AgentRunner(Path(tmp) / 'logs', 10, run=cli(raw)).run(
                    CFG.agent(kind), 'review', 'p', 'review', tmp, 'pr-100')
        for kind in ('claude', 'codex', 'agy'):
            with self.subTest(kind=kind):
                self.assertEqual(turn(without_reason(raw_review()), kind)['operator_reason'],
                                 'none')
                self.assertEqual(turn(trust(), kind)['operator_reason'], 'trust_policy')
                with self.assertRaises(agents.AgentError) as ctx:
                    turn(raw_review(reason=''), kind)  # no empty value in the enum (#441)
                self.assertIsInstance(ctx.exception.__cause__, SchemaError)
                with self.assertRaises(agents.AgentError) as ctx:
                    turn(raw_review(reason='other'), kind)
                self.assertIsInstance(ctx.exception.__cause__, SchemaError)
        # The default belongs to the review: the other schemas are unchanged.
        self.assertEqual(with_defaults('fix', {}), {})
        self.assertIn('operator_reason', agents.load_schema('review')['required'])

    def test_the_round_limit_holds_as_before(self):
        rounds = [dict(norm(raw_review('request_changes', [finding()])), round=i) for i in (1, 2)]
        self.review(trust([finding()]), reviews=rounds,
                    fixes=[{'round': 2, 'head_sha': SHA1, 'responses': []}])
        self.assertEqual(self.gh.labels[100], {OPERATOR})
        (escalation,) = escalations(self.gh)
        self.assertEqual(escalation['class'], 'round_limit')
        self.assertIn('limite di 3 round', self.gh.comments[-1][1])
        # Below the limit the same review goes to the fix; at the limit no new round starts.
        self.review(trust([finding()]), reviews=rounds[:1],
                    fixes=[{'round': 1, 'head_sha': SHA1, 'responses': []}])
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        third = [*rounds, dict(norm(trust([finding()])), round=3)]
        p = pr(reviews=third, fixes=[{'round': 3, 'head_sha': SHA1, 'responses': []}])
        self.assertEqual(plan(Snapshot([p], [], []), CFG), [Action('round_limit', 100)])

    def test_without_findings_and_with_green_ci_the_pr_is_promoted(self):
        self.review(trust())
        self.assertEqual(self.gh.labels[100], {READY})
        self.assertEqual(escalations(self.gh), [])

    def test_without_green_ci_the_pr_waits(self):
        for ci in ('pending', 'fail'):
            with self.subTest(ci=ci):
                d = self.make(FakeRunner(trust()))
                self.gh.ci = ci
                p = pr()
                self.gh.labels[100] = set(p.labels)
                d._do_review(p)
                self.assertEqual(self.gh.labels[100], {'agent:review'})
                self.assertEqual(escalations(self.gh), [])

    def test_the_round_waits_for_the_quorum_also_with_the_conclave(self):
        def two(record):
            return dict(record, receipt=dict(record['receipt'], reviewers=[REV, 'other']))
        conclave = ('claude', 'codex', 'agy')
        self.assertEqual(waiting_reviewers([two(norm(trust()))], conclave), ['other'])
        self.assertEqual(waiting_reviewers(
            [two(norm(raw_review('needs_operator', human='scelta')))], conclave), [])

    def test_a_ci_failure_joins_the_round_of_a_trust_policy(self):
        for raw in (trust([finding()]), trust()):
            with self.subTest(findings=bool(raw['blocking_findings'])):
                latest = [norm(raw)]
                self.assertEqual(ci_joins_round(pr(reviews=latest, checks='fail')), latest)


class TrustGH(MergeGH):
    """A PR #100 at SHA1 that the level-1 reviewer approved as a trust policy change."""

    def __init__(self, raw=None):
        super().__init__()
        self.thread = []
        self.say('brushtyler', markdown.render_review(norm(raw or trust()), header(REV_NAME)))


class OperatorMergeTests(Case):
    def test_the_pr_goes_to_the_operator_once_for_the_review_and_the_merge(self):
        for cfg in (CFG, STRICT, dataclasses.replace(STRICT, merge=MergePolicy(mode='auto'),
                                                     supervisor='agy')):
            with self.subTest(mode=cfg.merge.mode):
                d = self.make(cfg, gh=TrustGH())
                if cfg.merge.enabled():
                    self.assertEqual(d.snapshot().prs[0].merge_mode, 'off')
                self.assertEqual([a.kind for a in d.tick()], ['operator_merge'])
                self.assert_not_merged()
                self.assertEqual(self.gh.labels[100], {READY, OPERATOR})
                body = self.gh.comments[-1][1]
                self.assertIn('Il merge spetta all\'operatore', body)
                self.assertIn('la PR tocca la policy di fiducia: servono la review e il merge '
                              'dell\'operatore', body)
                self.assertIn(f'`{REV}`, round 1: {PARTS}', body)
                self.assertEqual(len(records(self.gh, 'notice')), 1)
                # Once: the next tick neither writes again nor asks for anything else.
                self.assertEqual(d.tick(), [])
                self.assertEqual(len(records(self.gh, 'notice')), 1)
                self.assertEqual(escalations(self.gh), [])

    def test_an_approval_of_the_same_reviewer_hands_the_merge_back_to_devloop(self):
        # #461: trust_policy with a finding in round 1, a faithful implementation approved in
        # round 2 by the same reviewer, who closes its finding: devloop merges.
        gh = TrustGH(trust([finding()]))
        gh.say('brushtyler', markdown.render_review(resolving([norm(trust([finding()]))]),
                                                    header(REV_NAME)))
        d = self.make(STRICT, gh=gh)
        self.assertEqual(d.snapshot().prs[0].merge_mode, 'strict')
        self.assertEqual(d.tick(), [MERGE])
        self.assertEqual(self.gh.merged, [(100, SHA1)])
        self.assertNotIn(OPERATOR, self.gh.labels[100])
        self.assertEqual(records(self.gh, 'notice'), [])

    def test_a_trust_policy_on_the_approved_head_keeps_the_merge_with_the_operator(self):
        # #461: the reviewer of round 2 still says the PR touches the trust policy: the PR goes
        # to the operator as before, and the notice cites the parts declared on that head.
        gh = TrustGH(trust([finding()]))
        again = resolving([norm(trust([finding()]))], raw=trust(human='punto 5, `config.toml`'))
        gh.say('brushtyler', markdown.render_review(again, header(REV_NAME)))
        d = self.make(STRICT, gh=gh)
        self.assertEqual(d.snapshot().prs[0].merge_mode, 'off')
        self.assertEqual([a.kind for a in d.tick()], ['operator_merge'])
        self.assert_not_merged()
        self.assertEqual(self.gh.labels[100], {READY, OPERATOR})
        body = self.gh.comments[-1][1]
        self.assertIn(f'`{REV}`, round 2: punto 5, `config.toml`', body)
        self.assertNotIn('round 1', body)

    def test_an_approval_after_the_operator_closed_the_findings_does_not_clear_it(self):
        gh = TrustGH(trust([finding()]))
        gh.say('brushtyler', f'/devloop finding 1/{REV}/R1-1 resolved fatto a mano')
        gh.say('brushtyler', markdown.render_review(norm(raw_review(), round_no=2),
                                                    header(REV_NAME)))
        d = self.make(STRICT, gh=gh)
        self.assertEqual(d.snapshot().prs[0].merge_mode, 'off')
        self.assert_not_merged()

    def test_the_merge_itself_refuses_a_trust_policy_pr(self):
        d = self.make(STRICT, gh=TrustGH())
        p = d.snapshot().prs[0]
        p.merge_mode = 'strict'  # as if the snapshot had missed it
        d._do_merge(p)
        self.assert_not_merged()
        self.assertTrue(any('trust policy' in line for line in self.logs))


if __name__ == '__main__':
    unittest.main()
