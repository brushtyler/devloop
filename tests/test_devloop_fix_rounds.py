"""devloop: the rounds after a `/devloop fix` of a trusted user on an approved head (#536).

A review round that answers a `/devloop fix` a trusted user asked on a head the quorum approved,
with no finding open, does not count against `max_review_rounds`, at most
`max_requested_fix_rounds` of them per PR (2, a cap of the code the configuration may only
lower); beyond them, and after a fix of the reviewers' findings or one asked on a head with a
request for changes, the round counts as before.
"""

import dataclasses
import unittest

from devloop.config import (MAX_REQUESTED_FIX_ROUNDS, OVERLAY_KEYS, TRUST_KEYS, ConfigError,
                            parse_config)
from devloop.dispatcher import pr_status_line, round_limit_reason
from devloop.planning import (Snapshot, counted_rounds, next_requested, plan, requested_rounds,
                              round_reasons, waived_rounds, waiver)
from devloop.review import ci_failure_review
from helpers import ssdlc_project
from test_devloop import (CFG, SHA1, SHA2, SHA3, FakeRunner, finding, fixed, norm, pr,
                          raw_review)
from test_devloop_fix_command import REQUEST, Case, records
from test_devloop_merge import SUPERVISOR_LOGIN

SHA4, SHA5 = 'e' * 40, 'f' * 40
TRUSTED = CFG.trusted_users[0]
ONE = dataclasses.replace(CFG, max_review_rounds=1)


def approved(number, head):
    return norm(raw_review(), round_no=number, head=head)


def changes(number, head):
    return norm(raw_review('request_changes', [finding()]), round_no=number, head=head)


def requested(number, start, head, by=TRUSTED):
    """The record of a fix asked with `/devloop fix` by ``by`` on ``start``, answering round
    ``number``."""
    return {'round': number, 'head_sha': head, 'responses': [], 'from': start,
            'requests': [{'comment': 1000 + number, 'by': by, 'request': REQUEST}]}


def reviewers_fix(number, head):
    return {'round': number, 'head_sha': head,
            'responses': [{'finding_id': 'R1-1', 'action': 'fixed', 'note': 'done'}]}


def rounds_of(reviews, fixes, cfg=CFG):
    return waived_rounds(reviews, **waiver(cfg, fixes)), counted_rounds(reviews, **waiver(cfg, fixes))


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


class CountedTests(unittest.TestCase):
    def test_after_a_request_for_changes_the_round_counts(self):
        # The command came on a head with a request for changes and an open finding.
        reviews = [changes(1, SHA1), approved(2, SHA2)]
        fixes = [requested(1, SHA1, SHA2)]
        self.assertEqual(rounds_of(reviews, fixes), (set(), 2))
        reasons = round_reasons(reviews, fixes, (), **_cap(CFG))
        self.assertEqual(reasons[1], (2, 'review dopo una correzione', True))

    def test_the_head_must_be_the_approved_one(self):
        # A sync moved the approved head before the command: the fix ran on SHA3.
        reviews = [approved(1, SHA1), approved(2, SHA2)]
        self.assertEqual(rounds_of(reviews, [requested(1, SHA3, SHA2)]), (set(), 2))

    def test_a_fix_of_the_reviewers_or_of_an_untrusted_author_counts(self):
        reviews = [approved(1, SHA1), approved(2, SHA2)]
        for fixes in ([reviewers_fix(1, SHA2)],
                      [requested(1, SHA1, SHA2, by=SUPERVISOR_LOGIN)],
                      [requested(1, SHA1, SHA2, by='stranger')],
                      # One request of the fix from outside trusted_users is enough.
                      [dict(requested(1, SHA1, SHA2),
                            requests=[{'comment': 1, 'by': TRUSTED, 'request': 'a'},
                                      {'comment': 2, 'by': SUPERVISOR_LOGIN, 'request': 'b'}])]):
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
