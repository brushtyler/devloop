"""devloop: a ``needs_operator`` for the trust policy does not stop the loop (#434).

A reviewer says why it asks for the operator (``operator_reason``): ``trust_policy`` or
``decision``. A ``trust_policy`` with blocking findings goes to the fix as a request for changes,
within the usual round limit; without findings, and with green CI, the PR goes to the operator
once, in ``agent:ready-to-merge`` + ``agent:needs-operator``, for the review and the merge. A
``decision``, or a ``needs_operator`` without the field, is still an immediate escalation."""

import dataclasses
import unittest

from devloop import markdown, review
from devloop.config import MergePolicy
from devloop.dispatcher import Dispatcher
from devloop.planning import Action, Snapshot, ci_joins_round, plan, waiting_reviewers
from test_devloop import (CFG, REV, REV_NAME, SHA1, FakeGH, FakeRunner, FakeWS, finding, norm,
                          pr, raw_review)
from test_devloop_merge import READY, STRICT, Case, MergeGH, header, records

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
        for raw in (raw_review('needs_operator', human='scelta di prodotto', reason='decision'),
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

    def test_the_trust_policy_parts_are_sticky_and_one_per_reviewer(self):
        first = dict(norm(trust([finding()], human='punto 4')), round=1)
        second = dict(norm(trust(human='punto 4 e punto 5')), round=2)
        approval = dict(norm(raw_review()), round=3)
        self.assertEqual(review.trust_policy_parts([first, second, approval]),
                         [(REV, 2, 'punto 4 e punto 5')])
        self.assertEqual(review.trust_policy_parts([approval]), [])

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

    def test_a_later_approval_does_not_hand_the_merge_back_to_devloop(self):
        gh = TrustGH(trust([finding()]))
        approval = dict(norm(raw_review(previous=[
            {'id': f'1/{REV}/R1-1', 'status': 'resolved', 'note': 'ok'}]),
            previous_open=review.open_findings([norm(trust([finding()]))]), round_no=2),
            round=2)
        gh.say('brushtyler', markdown.render_review(approval, header(REV_NAME)))
        d = self.make(STRICT, gh=gh)
        self.assertEqual(d.snapshot().prs[0].merge_mode, 'off')
        self.assertEqual([a.kind for a in d.tick()], ['operator_merge'])
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
