"""devloop: the rounds after a `/devloop fix` of a trusted user on an approved head (#536).

A review round that answers a `/devloop fix` a trusted user asked on a head the quorum approved,
with no finding open, does not count against `max_review_rounds`, at most
`max_requested_fix_rounds` of them per PR (2, a cap of the code the configuration may only
lower); beyond them, and after a fix of the reviewers' findings or one asked on a head with a
request for changes, the round counts as before.
"""

import dataclasses
import unittest

from devloop import markdown
from devloop.config import (MAX_REQUESTED_FIX_ROUNDS, OVERLAY_KEYS, TRUST_KEYS, ConfigError,
                            parse_config)
from devloop.dispatcher import (Dispatcher, pr_status_line, round_limit_reason,
                                with_native_reviews)
from devloop.planning import (Snapshot, counted_rounds, next_requested, plan, requested_rounds,
                              round_reasons, waived_rounds, waiver)
from devloop.review import CI, ci_failure_review, normalize_review, open_findings, owner_of
from helpers import ssdlc_project
from test_devloop import (CFG, REV, REV_NAME, SHA1, SHA2, SHA3, FakeRunner, finding, fixed,
                          norm, pr, raw_review)
from test_devloop_fix_command import REQUEST, Case, header, records
from test_devloop_merge import SUPERVISOR_LOGIN

SHA4, SHA5 = 'e' * 40, 'f' * 40
TRUSTED = CFG.trusted_users[0]
ONE = dataclasses.replace(CFG, max_review_rounds=1)
TWO = dataclasses.replace(CFG, max_review_rounds=2)


def approved(number, head):
    return norm(raw_review(), round_no=number, head=head)


def changes(number, head):
    return norm(raw_review('request_changes', [finding()]), round_no=number, head=head)


def requested(number, start, head, by=TRUSTED, after=None):
    """The record of a fix asked with `/devloop fix` by ``by`` on ``start``, answering round
    ``number``, as the dispatcher reads it: its request with the reviews published before its
    comment (``after``, by default the review of round ``number`` by ``REV``, the reviewer of
    ``approved``)."""
    after = [(number, REV)] if after is None else after
    return {'round': number, 'head_sha': head, 'responses': [], 'from': start,
            'requests': [{'comment': 1000 + number, 'by': by, 'request': REQUEST,
                          'after': after}]}


def reviewers_fix(number, head):
    return {'round': number, 'head_sha': head,
            'responses': [{'finding_id': 'R1-1', 'action': 'fixed', 'note': 'done'}]}


def rounds_of(reviews, fixes, cfg=CFG, closures=()):
    waive = waiver(cfg, fixes, closures)
    return waived_rounds(reviews, **waive), counted_rounds(reviews, **waive)


def ci_closure(reviews, head, comment):
    """The closures the dispatcher records for the CI findings of ``reviews`` once the CI
    passes on ``head``, in its comment ``comment`` (the id ``_closures`` gives them)."""
    return [{'key': f['key'], 'status': 'resolved', 'by': CI, 'author': 'devloop dispatcher',
             'reason': f'CI verde sul commit {head[:12]}', 'head_sha': head, 'comment': comment}
            for f in open_findings(reviews) if f['owner'] == CI]


def ci_failure(number, head):
    return ci_failure_review(failed_checks=['Unit Tests'], log_tail='', previous_open=[],
                             round_no=number, max_rounds=3, head_sha=head)


class NotCountedTests(unittest.TestCase):
    def test_the_review_after_a_trusted_fix_on_an_approved_head_does_not_count(self):
        reviews = [approved(1, SHA1), changes(2, SHA2)]
        fixes = [requested(1, SHA1, SHA2)]
        self.assertEqual(requested_rounds(reviews, **waiver(CFG, fixes)), {2: (TRUSTED,)})
        self.assertEqual(rounds_of(reviews, fixes), ({2}, 1))
        # Without the PR's fixes, as before #536, it counted.
        self.assertEqual(counted_rounds(reviews), 2)

    def test_at_the_limit_the_review_of_the_requested_fix_still_starts(self):
        # Round 1 approved SHA1, the only round max_review_rounds = 1 allows; the trusted
        # user's fix pushed SHA2: its review starts, since it does not count.
        fixes = [requested(1, SHA1, SHA2)]
        snap_pr = pr(reviews=[approved(1, SHA1)], fixes=fixes, head=SHA2)
        self.assertEqual(next_requested(snap_pr.reviews, **waiver(ONE, fixes)), (TRUSTED,))
        (action,) = plan(_snapshot(snap_pr), ONE)
        self.assertEqual(action.kind, 'review')
        self.assertIn('does not count', action.reason)

    def test_a_round_replacing_the_requested_review_does_not_count_either(self):
        # Round 2, the review of the requested fix, was interrupted by the base (#160) and
        # round 3 replaced it: neither counts, and the cap is spent once.
        reviews = [approved(1, SHA1), _pending(2, SHA2),
                   approved(3, SHA2) | {'restarts': {'round': 2}}]
        fixes = [requested(1, SHA1, SHA2)]
        waived, counted = rounds_of(reviews, fixes)
        self.assertEqual((waived, counted), ({2, 3}, 1))
        self.assertEqual(requested_rounds(reviews, **waiver(CFG, fixes)), {3: (TRUSTED,)})
        # Before round 3 existed: the round replacing round 2 answers the same fix.
        before = reviews[:2]
        self.assertEqual(next_requested(before, **waiver(CFG, fixes), replacing=2), (TRUSTED,))
        self.assertEqual(next_requested(before, **waiver(CFG, fixes)), ())

    def test_a_ci_finding_closed_before_the_request_does_not_block_it(self):
        # Round 1: the CI failed on SHA1. Round 2 approved SHA2, and the CI passed on it: the
        # dispatcher closed the CI finding (comment 1001). Then the trusted user's fix, asked
        # in comment 1002, pushed SHA3.
        reviews = [ci_failure(1, SHA1), approved(2, SHA2)]
        closures = ci_closure(reviews, SHA2, 1001)
        fixes = [requested(2, SHA2, SHA3)]
        self.assertEqual(next_requested(reviews, **waiver(TWO, fixes, closures)), (TRUSTED,))
        snap_pr = dataclasses.replace(pr(reviews=reviews, fixes=fixes, head=SHA3),
                                      closures=closures)
        self.assertEqual([a.kind for a in plan(_snapshot(snap_pr), TWO)], ['review'])
        done = reviews + [approved(3, SHA3)]
        self.assertEqual(rounds_of(done, fixes, closures=closures), ({3}, 2))
        # Without the closure the finding was open at the request: the round counts.
        self.assertEqual(rounds_of(done, fixes), (set(), 3))
        # A closure recorded only after the request does not reach back to it.
        self.assertEqual(rounds_of(done, fixes, closures=ci_closure(reviews, SHA3, 1003)),
                         (set(), 3))

    def test_a_closure_on_a_head_no_review_saw_counts_when_it_came_first(self):
        # The CI failed on SHA1 and passed on SHA2, which no review saw: the dispatcher closed
        # the finding (comment 1001). Round 2 approved SHA3, then the fix was asked (1002).
        reviews = [ci_failure(1, SHA1), approved(2, SHA3)]
        closures = ci_closure(reviews, SHA2, 1001)
        fixes = [requested(2, SHA3, SHA4)]
        self.assertEqual(open_findings(reviews, closures), [])
        self.assertEqual(next_requested(reviews, **waiver(TWO, fixes, closures)), (TRUSTED,))
        self.assertEqual(rounds_of(reviews + [approved(3, SHA4)], fixes, closures=closures),
                         ({3}, 2))


class ClosureOrderTests(Case):
    def test_a_closure_after_the_request_on_the_same_head_does_not_reach_back(self):
        # Round 2 approved SHA2 with the CI finding of round 1 still open; the trusted user
        # asked a fix (1002), which made no commit: the head stays SHA2. The CI then passed on
        # SHA2 and the dispatcher closed the finding (1003), after the request.
        reviews = [ci_failure(1, SHA1), approved(2, SHA2)]
        fixes = [requested(2, SHA2, SHA2)]
        snap_pr = pr(reviews=reviews, fixes=fixes, head=SHA2)
        # The CI passed: the dispatcher records the closure first.
        self.assertEqual([a.kind for a in plan(_snapshot(snap_pr), TWO)], ['findings'])
        closures = ci_closure(reviews, SHA2, 1003)
        self.assertEqual(open_findings(reviews, closures), [])
        self.assertEqual(next_requested(reviews, **waiver(TWO, fixes, closures)), ())
        closed = dataclasses.replace(snap_pr, closures=closures)
        self.assertEqual([a.kind for a in plan(_snapshot(closed), TWO)], ['round_limit'])
        self.assertEqual(rounds_of(reviews + [approved(3, SHA2)], fixes, closures=closures),
                         (set(), 3))

    def test_without_the_ids_no_closure_counts(self):
        reviews = [ci_failure(1, SHA1), approved(2, SHA2)]
        (fix,) = [requested(2, SHA2, SHA3)]
        anonymous = dict(fix, requests=[dict(fix['requests'][0], comment=None)])
        for closures, fixes in ((ci_closure(reviews, SHA2, None), [fix]),
                                (ci_closure(reviews, SHA2, 1001), [anonymous])):
            with self.subTest(closures=closures, fixes=fixes):
                self.assertEqual(next_requested(reviews, **waiver(TWO, fixes, closures)), ())

    def test_the_dispatcher_orders_closures_and_requests_by_their_comments(self):
        d = self.make(cfg=TWO)
        self.gh.head = SHA2
        reviews = [ci_failure(1, SHA1), approved(2, SHA2)]
        self.gh.say('brushtyler', markdown.render_review(reviews[0], header()))
        self.gh.say('brushtyler', markdown.render_review(reviews[1], header(REV_NAME)))
        (closure,) = ci_closure(reviews, SHA2, None)
        del closure['comment']
        body = markdown.publishable('Finding chiusi.', 'finding', {'closed': [closure]})
        command = '/devloop fix\n' + REQUEST
        for closed_first in (True, False):
            with self.subTest(closed_first=closed_first):
                thread = list(self.gh.thread)
                if closed_first:
                    closed = self.gh.say('brushtyler', body)
                    asked = self.gh.say(TRUSTED, command)
                else:
                    asked = self.gh.say(TRUSTED, command)
                    closed = self.gh.say('brushtyler', body)
                snap_pr = d.snapshot().prs[0]
                self.assertEqual([c['comment'] for c in snap_pr.closures], [closed])
                fixes = [dict(requested(2, SHA2, SHA3),
                              requests=[{'comment': asked, 'by': TRUSTED, 'request': REQUEST,
                                         'after': [(2, REV)]}])]
                self.assertEqual(next_requested(snap_pr.reviews,
                                                **waiver(TWO, fixes, snap_pr.closures)),
                                 (TRUSTED,) if closed_first else ())
                self.gh.thread = thread


class CountedTests(unittest.TestCase):
    def test_after_a_request_for_changes_the_round_counts(self):
        # The command came on a head with a request for changes and an open finding.
        reviews = [changes(1, SHA1), approved(2, SHA2)]
        fixes = [requested(1, SHA1, SHA2)]
        self.assertEqual(rounds_of(reviews, fixes), (set(), 2))
        reasons = round_reasons(reviews, fixes, (), **_cap(CFG))
        self.assertEqual(reasons[1], (2, 'review dopo una correzione', True))

    def test_an_approval_published_after_the_request_counts(self):
        # The command came while round 1 was still under review: its approval, published
        # after the request, does not make the request one on an approved head.
        fixes = [requested(1, SHA1, SHA2, after=[])]
        self.assertEqual(next_requested([approved(1, SHA1)], **waiver(ONE, fixes)), ())
        snap_pr = pr(reviews=[approved(1, SHA1)], fixes=fixes, head=SHA2)
        self.assertEqual([a.kind for a in plan(_snapshot(snap_pr), ONE)], ['round_limit'])
        self.assertEqual(rounds_of([approved(1, SHA1), approved(2, SHA2)], fixes), (set(), 2))
        # Without the list of the reviews before it, the request proves no order either.
        (fix,) = fixes
        bare = dict(fix, requests=[{k: v for k, v in fix['requests'][0].items()
                                    if k != 'after'}])
        self.assertEqual(next_requested([approved(1, SHA1)], **waiver(ONE, [bare])), ())

    def test_a_quorum_completed_after_the_request_counts(self):
        # Round 1 needs two reviewers: the first approved before the command, the second
        # after it. The quorum approved the head only once the request was already there.
        first = _pending(1, SHA1)
        second = normalize_review(raw_review(), previous_open=[], round_no=1, max_rounds=3,
                                  head_sha=SHA1, ci='pass', reviewer='other-reviewer')
        second['receipt'] = first['receipt']
        reviews = [first, second]
        both = [(1, REV), (1, owner_of(second))]
        self.assertEqual(next_requested(reviews, **waiver(ONE, [requested(1, SHA1, SHA2,
                                                                          after=both)])),
                         (TRUSTED,))
        fixes = [requested(1, SHA1, SHA2, after=[(1, REV)])]
        self.assertEqual(next_requested(reviews, **waiver(ONE, fixes)), ())
        self.assertEqual(rounds_of(reviews + [approved(2, SHA2)], fixes), (set(), 2))

    def test_the_head_must_be_the_approved_one(self):
        # A sync moved the approved head before the command: the fix ran on SHA3.
        reviews = [approved(1, SHA1), approved(2, SHA2)]
        self.assertEqual(rounds_of(reviews, [requested(1, SHA3, SHA2)]), (set(), 2))

    def test_an_approval_with_a_finding_still_open_counts(self):
        # Round 1: a reviewer outside the later quorum asked for changes; the reviewers' fix
        # pushed SHA2, and round 2 approved it without the owner of the finding, which stays
        # open. The trusted user's fix on SHA2 answers an approval, not a head without findings.
        other = normalize_review(raw_review('request_changes', [finding()]), previous_open=[],
                                 round_no=1, max_rounds=3, head_sha=SHA1, ci='pass',
                                 reviewer='other-reviewer')
        reviews = [other, approved(2, SHA2)]
        self.assertEqual(len(open_findings(reviews)), 1)
        fixes = [reviewers_fix(1, SHA2), requested(2, SHA2, SHA3)]
        self.assertEqual(next_requested(reviews, **waiver(CFG, fixes)), ())
        self.assertEqual(rounds_of(reviews + [approved(3, SHA3)], fixes), (set(), 3))

    def test_two_fixes_before_a_review_count(self):
        # Two /devloop fix in a row on round 1: the second ran on SHA2, which no review
        # approved.
        fixes = [requested(1, SHA1, SHA2), requested(1, SHA2, SHA3)]
        self.assertEqual(next_requested([approved(1, SHA1)], **waiver(ONE, fixes)), ())
        snap_pr = pr(reviews=[approved(1, SHA1)], fixes=fixes, head=SHA3)
        self.assertEqual([a.kind for a in plan(_snapshot(snap_pr), ONE)], ['round_limit'])
        self.assertEqual(rounds_of([approved(1, SHA1), approved(2, SHA3)], fixes), (set(), 2))

    def test_an_approval_without_a_receipt_proves_no_quorum(self):
        unbound = norm(raw_review(), round_no=1, head=SHA1, bound=False)
        fixes = [requested(1, SHA1, SHA2)]
        self.assertEqual(next_requested([unbound], **waiver(ONE, fixes)), ())
        snap_pr = pr(reviews=[unbound], fixes=fixes, head=SHA2)
        self.assertEqual([a.kind for a in plan(_snapshot(snap_pr), ONE)], ['round_limit'])
        self.assertEqual(rounds_of([unbound, approved(2, SHA2)], fixes), (set(), 2))

    def test_a_fix_of_the_reviewers_or_of_an_untrusted_author_counts(self):
        reviews = [approved(1, SHA1), approved(2, SHA2)]
        for fixes in ([reviewers_fix(1, SHA2)],
                      [requested(1, SHA1, SHA2, by=SUPERVISOR_LOGIN)],
                      [requested(1, SHA1, SHA2, by='stranger')],
                      # One request of the fix from outside trusted_users is enough.
                      [dict(requested(1, SHA1, SHA2),
                            requests=[{'comment': 1, 'by': TRUSTED, 'request': 'a',
                                       'after': [(1, REV)]},
                                      {'comment': 2, 'by': SUPERVISOR_LOGIN, 'request': 'b',
                                       'after': [(1, REV)]}])]):
            with self.subTest(fixes=fixes):
                self.assertEqual(rounds_of(reviews, fixes), (set(), 2))

    def test_an_approval_voided_by_the_ci_or_still_partial_is_not_approved(self):
        fixes = [requested(1, SHA1, SHA2)]
        ci = ci_failure_review(failed_checks=['Unit Tests'], log_tail='', previous_open=[],
                               round_no=1, max_rounds=3, head_sha=SHA1)
        self.assertEqual(rounds_of([approved(1, SHA1), ci, approved(2, SHA2)], fixes),
                         (set(), 2))
        self.assertEqual(rounds_of([_pending(1, SHA1), approved(2, SHA2)], fixes), (set(), 2))

    def test_a_ci_failure_of_the_corrected_head_counts(self):
        fixes = [requested(1, SHA1, SHA2)]
        ci = ci_failure_review(failed_checks=['Unit Tests'], log_tail='', previous_open=[],
                               round_no=2, max_rounds=3, head_sha=SHA2)
        self.assertEqual(rounds_of([approved(1, SHA1), ci], fixes), (set(), 2))
        snap_pr = pr(reviews=[approved(1, SHA1)], fixes=fixes, head=SHA2, checks='fail')
        self.assertEqual([a.kind for a in plan(_snapshot(snap_pr), ONE)], ['round_limit'])

    def test_the_round_after_the_fix_of_its_findings_counts(self):
        # The requested review asked for changes: the fix answers the reviewers, and the
        # round after it counts as before.
        reviews = [approved(1, SHA1), changes(2, SHA2), approved(3, SHA3)]
        fixes = [requested(1, SHA1, SHA2), reviewers_fix(2, SHA3)]
        self.assertEqual(rounds_of(reviews, fixes), ({2}, 2))


class CapTests(unittest.TestCase):
    def cycles(self):
        """Four approvals, each followed by a trusted user's /devloop fix."""
        heads = [SHA1, SHA2, SHA3, SHA4, SHA5]
        reviews = [approved(n, heads[n - 1]) for n in range(1, 5)]
        fixes = [requested(n, heads[n - 1], heads[n]) for n in range(1, 4)]
        return reviews, fixes

    def test_beyond_the_cap_the_rounds_count(self):
        self.assertEqual(MAX_REQUESTED_FIX_ROUNDS, 2)
        self.assertEqual(CFG.max_requested_fix_rounds, 2)
        reviews, fixes = self.cycles()
        self.assertEqual(rounds_of(reviews, fixes), ({2, 3}, 2))
        reasons = round_reasons(reviews, fixes, (), **_cap(CFG))
        self.assertEqual([counts for _, _, counts in reasons], [True, False, False, True])
        self.assertIn('oltre i 2 round di questo tipo che non contano', reasons[3][1])
        self.assertIn(f'`{TRUSTED}`', reasons[3][1])
        # The next requested review would be beyond the cap too: it waits for the limit.
        self.assertEqual(next_requested(reviews, **waiver(CFG, fixes + [
            requested(4, SHA4, SHA5)])), ())

    def test_the_configuration_may_only_lower_the_cap(self):
        reviews, fixes = self.cycles()
        lower = dataclasses.replace(CFG, max_requested_fix_rounds=1)
        self.assertEqual(rounds_of(reviews, fixes, lower), ({2}, 3))
        off = dataclasses.replace(CFG, max_requested_fix_rounds=0)
        self.assertEqual(rounds_of(reviews, fixes, off), (set(), 4))
        # The overlay may set it, and a change of it is a trust change in the journal.
        self.assertIn('max_requested_fix_rounds', OVERLAY_KEYS)
        self.assertIn('max_requested_fix_rounds', TRUST_KEYS)
        for value in (0, 1, 2):
            with self.subTest(value=value):
                cfg = parse_config(dict(ssdlc_project.data(), max_requested_fix_rounds=value))
                self.assertEqual(cfg.max_requested_fix_rounds, value)
        for value in (3, -1, True, '1'):
            with self.subTest(value=value), self.assertRaises(ConfigError) as caught:
                parse_config(dict(ssdlc_project.data(), max_requested_fix_rounds=value))
            self.assertIn('may only lower', str(caught.exception))


class RoundLimitTests(unittest.TestCase):
    def test_the_escalation_lists_the_round_and_who_asked_the_fix(self):
        reviews = [approved(1, SHA1), changes(2, SHA2)]
        fixes = [requested(1, SHA1, SHA2)]
        text = round_limit_reason('Limite.', reviews, fixes, (), 1, **_cap(ONE))
        counted, free = text.split('Non contano:')
        self.assertIn('- round 1: prima review', counted)
        self.assertNotIn('round 2', counted)
        self.assertIn(f'- round 2: review dopo una correzione chiesta da `{TRUSTED}` con '
                      '`/devloop fix` su un head approvato', free)


class DispatcherTests(Case):
    def test_a_requested_fix_at_the_limit_is_reviewed_and_listed_as_not_counted(self):
        # max_review_rounds = 1: round 1 approved, then the trusted user asks for a fix.
        runner = FakeRunner(dict(fixed(), responses=[]),
                            raw_review('request_changes', [finding()]))
        d = self.make(cfg=ONE, runner=runner)
        self.approve()
        self.gh.say(TRUSTED, f'/devloop fix\n{REQUEST}')
        d.tick()
        (fix,) = records(self.gh, 'fix')
        self.assertEqual((fix['round'], fix['from']), (1, SHA1))
        self.gh.head = SHA2
        snap = d.snapshot()
        self.assertEqual([a.kind for a in plan(snap, ONE)], ['review'])
        d.tick()
        # Round 2 asked for changes: it does not count, but the one after its fix would be
        # the second of a limit of 1, so the PR goes to the operator, who sees why.
        reviews = records(self.gh, 'review')
        self.assertEqual([r['round'] for r in reviews], [2])
        self.assertIn('agent:needs-operator', self.gh.labels[100])
        # `status` reads the thread of the escalated PR, its fixes included.
        parked = d.snapshot().prs[0]
        self.assertIn('rounds=1/1 (+1 not counted)',
                      pr_status_line(parked, ONE, d.status_reviews(parked)))
        (notice,) = [r for r in records(self.gh, 'notice') if r['event'] == 'escalation']
        self.assertEqual(notice['class'], 'round_limit')
        (body,) = [b for _, b in self.gh.comments if 'round_limit' in b]
        counted, free = body.split('Non contano:')
        self.assertIn('- round 1: prima review', counted)
        self.assertIn(f'- round 2: review dopo una correzione chiesta da `{TRUSTED}` con '
                      '`/devloop fix` su un head approvato', free)


class RequestOrderTests(Case):
    def test_a_fix_asked_while_the_round_was_under_review_counts(self):
        # max_review_rounds = 1: the trusted user asked the fix while round 1 was still
        # under review, and its approval came after the command. The fix runs, but the review
        # of its head counts: at the limit, it goes to the operator.
        d = self.make(cfg=ONE, runner=FakeRunner(dict(fixed(), responses=[])))
        asked = self.gh.say(TRUSTED, f'/devloop fix\n{REQUEST}')
        self.approve()
        d.tick()
        (fix,) = records(self.gh, 'fix')
        self.assertEqual((fix['round'], fix['from']), (1, SHA1))
        self.gh.head = SHA2
        snap_pr = d.snapshot().prs[0]
        (request,) = snap_pr.fixes[0]['requests']
        self.assertEqual((request['comment'], request['after']), (asked, []))
        self.assertEqual(next_requested(snap_pr.reviews, **waiver(ONE, snap_pr.fixes,
                                                                   snap_pr.closures)), ())
        self.assertEqual([a.kind for a in plan(d.snapshot(), ONE)], ['round_limit'])

    def test_the_approval_before_the_request_is_listed(self):
        d = self.make(cfg=ONE, runner=FakeRunner(dict(fixed(), responses=[])))
        self.approve()
        self.gh.say(TRUSTED, f'/devloop fix\n{REQUEST}')
        d.tick()
        self.gh.head = SHA2
        (request,) = d.snapshot().prs[0].fixes[0]['requests']
        self.assertEqual(request['after'], [(1, REV)])

    def test_a_native_review_comes_before_only_at_an_earlier_time(self):
        # A native review (#325) is placed among the comments by its time: one at the same
        # second as the command may have come after it.
        body = markdown.render_review(approved(1, SHA1), header(REV_NAME))
        native = {'id': 7, 'login': 'reviewer-app[bot]', 'body': body, 'state': 'APPROVED',
                  'submitted_at': '2026-11-21T12:00:00Z'}
        for asked_at, after in (('2026-11-21T12:00:00Z', []),
                                ('2026-11-21T12:00:01Z', [(1, REV)])):
            with self.subTest(asked_at=asked_at):
                command = {'id': 1001, 'author': {'login': TRUSTED}, 'created_at': asked_at,
                           'body': f'/devloop fix\n{REQUEST}'}
                thread = with_native_reviews([command], [native])
                self.assertTrue(thread[0]['native'])
                entries = [(0, approved(1, SHA1))]
                self.assertEqual(Dispatcher._reviewed_before(thread, entries, 1), after)
        # A request whose comment is not in the conversation has no review before it.
        self.assertEqual(Dispatcher._reviewed_before(thread, entries, None), [])


def _pending(number, head):
    """A round of two reviewers where only the first reviewed: still waiting."""
    record = approved(number, head)
    record['receipt'] = dict(record['receipt'], reviewers=[*record['receipt']['reviewers'],
                                                           'other-reviewer'])
    return record


def _cap(cfg):
    return {'trusted': cfg.trusted_users, 'cap': cfg.max_requested_fix_rounds}


def _snapshot(snap_pr):
    return Snapshot(prs=[snap_pr], ready_issues=[], working_issues=[])


if __name__ == '__main__':
    unittest.main()
