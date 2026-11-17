"""devloop: the review of the same head after the base revoked its approval (#252)."""

import dataclasses
import unittest

from devloop import markdown
from devloop.dispatcher import (BASE_CHANGED, RESYNC_AFTER_APPROVAL, Action, base_revocation,
                                counted_rounds, plan, waived_rounds)
from helpers.devloop_fakes import FakeIdentities
from test_devloop import BASE, CFG, IMPL, SHA1, SHA2, finding, raw_review, receipt
from test_devloop_quorum import FIRST, Quorum

NEW = 'e' * 40  # the base after main advanced
LEVEL1 = [{'path': 'x.py', 'links': []}]  # a diff reviewed at level 1
ONE_ROUND = dataclasses.replace(CFG, max_review_rounds=1)
TOUCHED = {'from': BASE, 'to': NEW, 'outcome': 'touched', 'paths': ['x.py']}


class BaseRevocationTests(Quorum):
    def approved_then_revoked(self, *outputs, cfg=ONE_ROUND, touched=('x.py',)):
        """Round 1 approves SHA1 on BASE; before the promotion main advances to NEW changing
        ``touched``, and the dispatcher revokes the approval (``stale``)."""
        self.make(raw_review(), *outputs, cfg=cfg, diff=LEVEL1)
        diff, calls = self.ws.review_diff, []

        def moving(branch):
            # The review reads BASE; anything after it, the promotion included, reads NEW.
            calls.append(branch)
            return (BASE if len(calls) == 1 else NEW, *diff(branch)[1:])

        self.ws.review_diff = moving
        self.ws.base_touched = list(touched)
        self.assertEqual(self.d.tick(), [Action('review', 100)])
        self.ws.review_diff, self.ws.base = diff, NEW
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        (stale,) = self.records('policy')
        self.assertEqual(stale['event'], 'stale')
        return stale

    def stale_index(self):
        return next(i for i, (_, body) in enumerate(self.gh.comments)
                    if markdown.read_marker(body, 'policy'))

    def test_the_same_head_after_a_base_revocation_is_reviewed_without_spending_a_round(self):
        # The case of #249: approved in the last round allowed, revoked because main touched
        # a file of the PR, reviewed again on the same commit.
        stale = self.approved_then_revoked(raw_review())
        self.assertEqual(stale['bases'], [TOUCHED])
        self.assertIn('La nuova review dello stesso commit apre un nuovo round, che non conta '
                      'per il limite dei round', self.gh.comments[-1][1])
        self.assertNotIn('Round e finding aperti restano invariati', self.gh.comments[-1][1])
        before = counted_rounds(self.d.snapshot().prs[0].reviews)
        self.assertEqual(self.planned(), [
            Action('review', 100, 'the approval was revoked because the base touched the PR')])
        self.d.tick()
        first, second = self.records('review')
        self.assertEqual((second['round'], second['head_sha']), (2, SHA1))
        self.assertEqual(second['receipt']['base'], NEW)
        self.assertEqual(second['resync'], {'reason': BASE_CHANGED, 'round': 1, 'from': SHA1})
        self.assertNotIn('resync', first)
        self.assertEqual(second['max_rounds'], 2)
        self.assertIn('round 2 of at most 2', self.runner.calls[-1][3])
        self.assertIn('questo round rivede lo stesso commit', self.gh.comments[-2][1])
        reviews = self.d.snapshot().prs[0].reviews
        self.assertEqual((waived_rounds(reviews), counted_rounds(reviews)), ({2}, before))
        self.assertEqual(before, 1)
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})

    def test_a_new_commit_after_the_revocation_counts(self):
        # The implementer's new commit (not a merge of main): the review counts as before.
        self.approved_then_revoked()
        self.gh.head = self.ws._head = SHA2
        self.ws.resynced = False
        self.assertIsNone(self.d.snapshot().prs[0].resync)
        self.assertEqual(self.planned(), [Action('round_limit', 100)])
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        self.assertEqual(len(self.records('review')), 1)

    def test_a_new_commit_after_the_revocation_spends_a_round(self):
        self.approved_then_revoked(raw_review(), cfg=CFG)
        self.gh.head = self.ws._head = SHA2
        self.assertEqual(self.planned(), [Action('review', 100)])
        self.d.tick()
        second = self.records('review')[-1]
        self.assertEqual((second['round'], second['head_sha']), (2, SHA2))
        self.assertNotIn('resync', second)
        self.assertEqual(counted_rounds(self.d.snapshot().prs[0].reviews), 2)

    def test_a_revocation_not_written_by_the_dispatcher_gives_no_waiver(self):
        self.approved_then_revoked()
        # The dispatcher's own record: a review of the same head is free.
        snap = self.d.snapshot()
        self.assertEqual(snap.prs[0].resync, {'reason': BASE_CHANGED, 'round': 1, 'from': SHA1})
        index = self.stale_index()
        for login in ('brushtyler', FakeIdentities.LOGINS[IMPL], FakeIdentities.LOGINS[FIRST]):
            with self.subTest(login=login):
                # The same marker, written by anyone else: no revocation, no waiver.
                self.gh.written[index] = login
                snap = self.d.snapshot()
                pr = snap.prs[0]
                self.assertFalse(pr.stale_receipt)
                self.assertIsNone(pr.revoked)
                self.assertIsNone(pr.resync)
                # A review of the same head needed for another reason is a round that counts.
                pr.acceptance = ['criterio senza test']
                self.assertEqual(plan(snap, self.d.cfg), [Action('round_limit', 100)])

    def test_a_rereview_or_a_ci_failure_after_the_revocation_counts(self):
        self.approved_then_revoked()
        self.gh.ci = 'fail'
        self.assertEqual(self.planned(), [Action('round_limit', 100)])
        self.gh.ci = 'pass'
        self.gh.comments.append((100, '/devloop rereview\n\nDi nuovo.'))
        self.gh.written.append('brushtyler')
        self.assertEqual(self.planned(), [Action('round_limit', 100)])

    def test_a_request_for_changes_in_the_free_round_keeps_the_limit(self):
        self.approved_then_revoked(raw_review('request_changes', [finding()]))
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        self.assertIn('Raggiunto il limite di 1 round', self.gh.comments[-1][1])


class CountingTests(unittest.TestCase):
    def record(self, round_no, verdict='approve', head=SHA1, resync=None, source='agent'):
        r = {'round': round_no, 'source': source, 'reviewer': FIRST, 'verdict': verdict,
             'head_sha': head}
        if resync is not None:
            r['resync'] = resync
        return r

    def revoked(self, round_no=1, head=SHA1, reason=BASE_CHANGED):
        return {'reason': reason, 'round': round_no, 'from': head}

    def test_only_the_same_head_after_its_approval_is_waived(self):
        good = [self.record(1), self.record(2, resync=self.revoked())]
        self.assertEqual((waived_rounds(good), counted_rounds(good)), ({2}, 1))
        for name, reviews in {
            'other head': [self.record(1), self.record(2, head=SHA2, resync=self.revoked())],
            'not approved': [self.record(1, 'request_changes'),
                             self.record(2, resync=self.revoked())],
            'from another head': [self.record(1, head=SHA2),
                                  self.record(2, head=SHA2, resync=self.revoked())],
            'not the previous round': [self.record(1), self.record(2),
                                       self.record(3, resync=self.revoked())],
            'ci round': [self.record(1, source='ci'), self.record(2, resync=self.revoked())],
            'bool round': [self.record(1), self.record(2, resync=self.revoked(round_no=True))],
            'sync reason on the same head': [
                self.record(1), self.record(2, resync=self.revoked(reason=RESYNC_AFTER_APPROVAL))],
        }.items():
            with self.subTest(name):
                self.assertEqual(waived_rounds(reviews), set())

    def test_only_a_revocation_for_the_base_counts(self):
        stale = {'event': 'stale', 'receipt': receipt(base=NEW), 'bases': [TOUCHED]}
        self.assertTrue(base_revocation(stale, SHA1))
        for outcome in ('unverifiable', 'conflict'):
            self.assertTrue(base_revocation(
                dict(stale, bases=[{'from': BASE, 'to': NEW, 'outcome': outcome}]), SHA1))
        for name, record in {
            'other head': stale, 'promoted': dict(stale, event='promoted'),
            'no bases': {'event': 'stale', 'receipt': receipt(base=NEW)},
            'disjoint only': dict(stale, bases=[{'from': BASE, 'to': NEW,
                                                 'outcome': 'disjoint'}]),
            'malformed bases': dict(stale, bases=['touched']), 'none': None,
        }.items():
            with self.subTest(name):
                self.assertFalse(base_revocation(record, SHA2 if name == 'other head' else SHA1))

    def test_the_review_shows_why_it_does_not_count(self):
        record = dict(self.record(2, resync=self.revoked()), max_rounds=2, ci='pass',
                      summary='ok', human_reason='', blocking_findings=[],
                      non_blocking_findings=[], previous_findings=[], checks_performed=[])
        body = markdown.render_review(record, '')
        self.assertIn(f'Il round 1 aveva approvato `{SHA1[:12]}`', body)
        self.assertIn('questo round rivede lo stesso commit', body)


if __name__ == '__main__':
    unittest.main()
