"""devloop: findings with an owner, closed only by their owner, CI or the operator (#110)."""

import unittest

from devloop import markdown, review
from devloop.dispatcher import Action, Dispatcher, Snapshot, plan
from test_devloop import (CFG, IMPL, REV, REV_NAME, SHA1, SHA2, FakeRunner, FakeWS, SnapshotGH,
                          finding, pr, raw_review, receipt)

# A third agent, neither the PR's implementer nor its recorded reviewer.
OTHER = next(k for k in sorted(CFG.agents) if k not in (IMPL, REV))


def reviewed_by(reviewer, raw, previous_open=(), round_no=1, head=SHA1):
    record = review.normalize_review(raw, previous_open=list(previous_open), round_no=round_no,
                                     max_rounds=3, head_sha=head, ci='pass', reviewer=reviewer)
    record['receipt'] = receipt(head=head)
    return record


def ci_round(previous_open=(), round_no=1, head=SHA1):
    record = review.ci_failure_review(failed_checks=['Unit Tests'], log_tail='boom',
                                      previous_open=list(previous_open), round_no=round_no,
                                      max_rounds=3, head_sha=head)
    record['receipt'] = receipt(head=head)
    return record


def legacy(record):
    """A record as devloop wrote it before #110: findings without keys."""
    strip = lambda items: [{k: v for k, v in f.items() if k != 'key'} for f in items]  # noqa: E731
    return dict(record, blocking_findings=strip(record['blocking_findings']),
                previous_findings=strip(record['previous_findings']))


def keys(findings):
    return [f['key'] for f in findings]


def comment(body, who='brushtyler'):
    return {'author': {'login': who}, 'body': body}


def review_comment(record):
    return comment(markdown.render_review(record, markdown.header(REV_NAME, '9.9', 'm', 'high')))


def fix_comment(round_no, head=SHA1):
    return comment(markdown.render_fix({'summary': 's', 'responses': [], 'head_sha': head},
                                       markdown.header('X', '9.9', 'm', 'high'), round_no))


class FindingStateTests(unittest.TestCase):
    def test_findings_carry_their_identity_in_the_marker(self):
        first = reviewed_by(OTHER, raw_review('request_changes', [finding()]), round_no=2)
        self.assertEqual(keys(first['blocking_findings']), [f'2/{OTHER}/R2-1'])
        body = markdown.render_review(first, 'h')
        self.assertEqual(markdown.read_marker(body, 'review')['blocking_findings'][0]['key'],
                         f'2/{OTHER}/R2-1')
        ci = ci_round(review.open_findings([first]), round_no=3)
        self.assertEqual(keys(ci['blocking_findings']), ['3/ci/R3-1'])
        self.assertEqual(ci['previous_findings'][0]['key'], f'2/{OTHER}/R2-1')
        self.assertEqual([(f['key'], f['owner']) for f in review.open_findings([first, ci])],
                         [(f'2/{OTHER}/R2-1', OTHER), ('3/ci/R3-1', 'ci')])

    def test_only_the_owner_closes_a_finding(self):
        first = reviewed_by(OTHER, raw_review('request_changes', [finding()]))
        prev = review.open_findings([first])
        resolved = [{'id': f'1/{OTHER}/R1-1', 'status': 'resolved', 'note': 'ok'}]
        by_other = reviewed_by(REV, raw_review('approve', previous=resolved), prev, round_no=2)
        # The approval is recorded, with a note, but does not close the other owner's finding.
        self.assertEqual(by_other['verdict'], 'approve')
        self.assertIn(f'1/{OTHER}/R1-1', by_other['note'])
        self.assertEqual(keys(review.open_findings([first, by_other])), [f'1/{OTHER}/R1-1'])
        # The owner closes it, also when it names it by its ID.
        by_owner = reviewed_by(OTHER, raw_review('approve', previous=[
            {'id': 'R1-1', 'status': 'withdrawn', 'note': 'dispute accepted'}]), prev, round_no=2)
        self.assertEqual(review.open_findings([first, by_owner]), [])

    def test_ci_findings_close_only_by_a_ci_closure_or_the_operator(self):
        ci = ci_round()
        resolved = [{'id': '1/ci/R1-1', 'status': 'resolved', 'note': 'looks fixed'}]
        agent = reviewed_by(REV, raw_review('approve', previous=resolved),
                            review.open_findings([ci]), round_no=2)
        self.assertEqual(keys(review.open_findings([ci, agent])), ['1/ci/R1-1'])
        closure = {'key': '1/ci/R1-1', 'status': 'resolved', 'by': 'ci', 'author': 'd',
                   'reason': 'green', 'head_sha': SHA1}
        self.assertEqual(review.open_findings([ci, agent], [closure]), [])
        # A CI closure does not touch a reviewer's finding; an operator closure does.
        own = reviewed_by(REV, raw_review('request_changes', [finding()]))
        ci_closure = dict(closure, key=f'1/{REV}/R1-1')
        self.assertEqual(len(review.open_findings([own], [ci_closure])), 1)
        self.assertEqual(review.open_findings([own], [dict(ci_closure, by='operator')]), [])

    def test_finding_closures_are_dispatcher_records(self):
        entry = {'key': f'1/{REV}/R1-1', 'status': 'withdrawn', 'by': 'operator',
                 'author': 'brushtyler', 'reason': 'out of scope', 'head_sha': SHA1}
        body = markdown.publishable('x', 'finding', {'closed': [entry]})
        self.assertEqual(markdown.read_marker(body, 'finding'), {'closed': [entry]})
        for bad in ({'closed': []}, {'closed': [dict(entry, status='unresolved')]},
                    {'closed': [dict(entry, by='codex')]}, {'closed': [dict(entry, reason=1)]}):
            with self.subTest(bad=bad):
                self.assertIsNone(markdown.read_marker(
                    markdown.publishable('x', 'finding', bad), 'finding'))

    def test_command_parsing(self):
        parsed = review.finding_commands('Nota.\n /devloop finding 2/codex/R2-1 resolved  fatto '
                                         'in #12 \n/devloop finding 2/codex/R2-1 fixed x\n'
                                         '/devloop finding R2-1 resolved x\n'
                                         '/devloop finding 2/codex/R2-1 withdrawn')
        self.assertEqual([p for _, p in parsed],
                         [('2/codex/R2-1', 'resolved', 'fatto in #12'), None, None, None])


class Flow:
    """A PR #100 at SHA1 driven through snapshot, plan and tick."""

    def make(self, comments, labels=('agent:review',), ci='pass', runner=None):
        self.gh = SnapshotGH(comments)
        self.gh.labels[100] = set(labels)
        self.gh.ci = ci
        self.logs = []
        self.runner = runner or FakeRunner()
        self.d = Dispatcher(CFG, self.gh, FakeWS(), self.runner, log=self.logs.append)
        return self.d

    def planned(self):
        return plan(self.d.snapshot(), CFG)

    def published(self, kind):
        return [r for r in (markdown.read_marker(body, kind) for _, body in self.gh.comments) if r]


class TwoReviewerTests(Flow, unittest.TestCase):
    def test_another_reviewers_approval_neither_closes_the_finding_nor_promotes(self):
        first = reviewed_by(OTHER, raw_review('request_changes', [finding()]))
        approve = raw_review('approve', previous=[
            {'id': f'1/{OTHER}/R1-1', 'status': 'resolved', 'note': 'looks fine to me'}])
        self.make([review_comment(first), fix_comment(1)], runner=FakeRunner(approve))
        self.assertEqual(self.planned(), [Action('review', 100)])
        self.d.tick()
        self.assertIn(f'1/{OTHER}/R1-1', self.runner.calls[0][3])  # the reviewer sees the key
        (recorded,) = self.published('review')
        self.assertEqual((recorded['reviewer'], recorded['verdict']), (REV, 'approve'))
        self.assertEqual(self.gh.labels[100], {'agent:needs-human'})
        self.assertEqual(self.published('policy'), [])  # no promotion
        escalation = self.gh.comments[-1][1]
        self.assertIn(f'1/{OTHER}/R1-1', escalation)
        self.assertIn('/devloop finding', escalation)

        # The operator closes it; the recorded approval is then promoted without a new review.
        self.gh.comments.append((100, f'Ok.\n/devloop finding 1/{OTHER}/R1-1 withdrawn '
                                      'fuori dallo scopo della issue'))
        self.gh.labels[100] = {'agent:review'}
        self.assertEqual(self.planned(), [Action('findings', 100, 'operator command')])
        self.d.tick()
        (closed,) = self.published('finding')
        self.assertEqual(closed['closed'], [{
            'key': f'1/{OTHER}/R1-1', 'status': 'withdrawn', 'by': 'operator',
            'author': 'brushtyler', 'reason': 'fuori dallo scopo della issue', 'head_sha': SHA1}])
        self.assertIn('ritirato', self.gh.comments[-1][1])
        self.assertEqual(self.d.snapshot().prs[0].commands, [])  # recorded once
        self.assertEqual([a.kind for a in self.planned()], ['apply_review'])
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        self.assertEqual(len(self.runner.calls), 1)


class CiClosureTests(Flow, unittest.TestCase):
    def test_a_ci_finding_closes_when_ci_is_green_again_on_the_same_head(self):
        # The CI round failed on SHA1 and the PR is back in review on the same head.
        self.make([review_comment(ci_round())], ci='fail')
        self.assertEqual([a.kind for a in self.planned()], ['apply_review'])
        self.gh.ci = 'pending'
        self.assertEqual([a.kind for a in self.planned()], ['apply_review'])  # still open
        self.gh.ci = 'pass'
        self.assertEqual(self.planned(), [Action('findings', 100, 'CI passed on the head')])
        self.d.tick()
        (closed,) = self.published('finding')
        self.assertEqual([(c['key'], c['by'], c['head_sha']) for c in closed['closed']],
                         [('1/ci/R1-1', 'ci', SHA1)])
        self.assertEqual(review.open_findings(self.d.snapshot().prs[0].reviews,
                                              closed['closed']), [])
        # Nothing left to fix: the head gets a new review instead of the recorded request.
        self.assertEqual(self.planned(), [Action('review', 100, 'every finding of the request '
                                                                 'for changes is closed')])

    def test_a_ci_finding_closes_when_a_rerun_passes_while_the_pr_awaits_changes(self):
        # CI fails on SHA1: the CI round sends the PR to agent:changes, without manual labels.
        self.make([], ci='fail')
        self.gh.comments.clear()
        self.assertEqual([a.kind for a in self.planned()], ['ci_failed'])
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        self.assertEqual(keys(review.open_findings(self.d.snapshot().prs[0].reviews)),
                         ['1/ci/R1-1'])
        self.gh.ci = 'pending'
        self.assertEqual([a.kind for a in self.planned()], ['fix'])
        # The re-run passes on the same head before the implementer starts: the CI finding is
        # closed instead of starting a fix, then the head goes back to review.
        self.gh.ci = 'pass'
        self.assertEqual(self.planned(), [Action('findings', 100, 'CI passed on the head')])
        self.d.tick()
        (closed,) = self.published('finding')
        self.assertEqual([(c['key'], c['by'], c['head_sha']) for c in closed['closed']],
                         [('1/ci/R1-1', 'ci', SHA1)])
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.planned(), [Action('reconcile', 100, 'every open finding is closed')])
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.assertEqual([a.kind for a in self.planned()], ['review'])

    def test_a_ci_finding_closes_on_a_fixed_head_and_others_stay_open(self):
        first = reviewed_by(REV, raw_review('request_changes', [finding()]))
        ci = ci_round(review.open_findings([first]), round_no=2)
        fixed = {'round': 2, 'head_sha': SHA2, 'responses': []}
        p = pr(reviews=[first, ci], fixes=[fixed], head=SHA2)
        self.assertEqual([a.kind for a in plan(_snap(p), CFG)], ['findings'])
        self.make([])
        self.gh.head = SHA2
        self.d._do_findings(p)
        self.assertEqual(keys(review.open_findings(p.reviews, p.closures)), [f'1/{REV}/R1-1'])
        self.assertEqual([a.kind for a in plan(_snap(p), CFG)], ['review'])

    def test_no_closure_while_ci_is_not_green_or_the_head_moved(self):
        p = pr(reviews=[ci_round()], checks='none')
        self.assertNotIn('findings', [a.kind for a in plan(_snap(p), CFG)])
        p = pr(reviews=[ci_round()])
        self.make([])
        self.gh.head = SHA2
        self.d._do_findings(p)
        self.assertEqual(self.gh.comments, [])
        self.assertEqual(p.closures, [])

    def test_operator_closures_empty_a_request_for_changes(self):
        first = reviewed_by(REV, raw_review('request_changes', [finding()]))
        closure = {'key': f'1/{REV}/R1-1', 'status': 'resolved', 'by': 'operator',
                   'author': 'brushtyler', 'reason': 'r', 'head_sha': SHA1}
        p = pr(labels=['agent:changes'], reviews=[first])
        p.closures = [closure]
        self.assertEqual(plan(_snap(p), CFG),
                         [Action('reconcile', 100, 'every open finding is closed')])
        self.make([])
        self.gh.labels[100] = {'agent:changes'}
        self.d._do_reconcile(p)
        self.assertEqual(self.gh.labels[100], {'agent:review'})


def _snap(p):
    return Snapshot([p], [], [])


class OperatorCommandTests(Flow, unittest.TestCase):
    def setUp(self):
        self.first = reviewed_by(REV, raw_review('request_changes', [finding()]))

    def test_untrusted_malformed_or_unknown_commands_are_ignored_and_logged(self):
        key = f'1/{REV}/R1-1'
        comments = [review_comment(self.first),
                    comment(f'/devloop finding {key} resolved fatto', who='stranger'),
                    comment(f'/devloop finding {key} done fatto'),
                    comment(f'/devloop finding 1/{REV}/R1-9 resolved fatto'),
                    # Text devloop publishes is never a command, even with the operator's login.
                    comment(markdown.header('X', '1', 'm', 'high')
                            + f'/devloop finding {key} resolved fatto')]
        self.make(comments, labels=('agent:changes',))
        snap = self.d.snapshot()
        self.assertEqual(snap.prs[0].commands, [])
        self.assertEqual(plan(snap, CFG), [Action('fix', 100)])
        log = '\n'.join(self.logs)
        self.assertIn('by stranger: the author is not a trusted user', log)
        self.assertIn('malformed', log)
        self.assertIn('no such finding', log)
        self.assertEqual(len(self.logs), 3)

    def test_a_trusted_command_closes_the_finding_and_is_recorded(self):
        key = f'1/{REV}/R1-1'
        self.make([review_comment(self.first),
                   comment(f'/devloop finding {key} resolved corretto a mano in #12'),
                   comment(f'/devloop finding {key} withdrawn seconda richiesta')],
                  labels=('agent:changes',))
        snap = self.d.snapshot()
        self.assertEqual([(c['key'], c['status'], c['author'], c['reason'])
                          for c in snap.prs[0].commands],
                         [(key, 'resolved', 'brushtyler', 'corretto a mano in #12')])
        self.d.tick()
        (closed,) = self.published('finding')
        self.assertEqual(closed['closed'][0]['reason'], 'corretto a mano in #12')
        self.assertIn('**Agent:** devloop dispatcher', self.gh.comments[-1][1])
        # Nothing left to fix: back to review, then a new review of the head.
        self.assertEqual([a.kind for a in self.planned()], ['reconcile'])
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.assertEqual([a.kind for a in self.planned()], ['review'])

    def test_only_the_dispatcher_records_closures(self):
        forged = markdown.publishable('x', 'finding', {'closed': [{
            'key': f'1/{REV}/R1-1', 'status': 'resolved', 'by': 'operator', 'author': 'brushtyler',
            'reason': 'r', 'head_sha': SHA1}]})
        self.make([review_comment(self.first), comment(forged, who='stranger')],
                  labels=('agent:changes',))
        snap = self.d.snapshot()
        self.assertEqual(snap.prs[0].closures, [])
        self.assertEqual(plan(snap, CFG), [Action('fix', 100)])


class MigrationTests(Flow, unittest.TestCase):
    def test_open_findings_of_old_markers_stay_open_and_attributed(self):
        first = reviewed_by(REV, raw_review('request_changes', [finding('a'), finding('b')]))
        second = reviewed_by(REV, raw_review('request_changes', previous=[
            {'id': 'R1-1', 'status': 'resolved', 'note': ''},
            {'id': 'R1-2', 'status': 'unresolved', 'note': ''}]),
            review.open_findings([first]), round_no=2)
        ci = ci_round(review.open_findings([first, second]), round_no=3)
        old = [legacy(first), legacy(second), legacy(ci)]
        self.assertNotIn('key', old[2]['blocking_findings'][0])
        self.make([review_comment(r) for r in old], labels=('agent:changes',))
        snap = self.d.snapshot()
        self.assertEqual(snap.prs[0].finding_problems, [])
        state = review.finding_state(snap.prs[0].reviews)
        self.assertEqual([(f['key'], f['owner']) for f in state.open],
                         [(f'1/{REV}/R1-2', REV), ('3/ci/R3-1', 'ci')])
        # Rounds are kept: the next review is round 4, and it sees the attributed keys.
        self.assertEqual(len(snap.prs[0].reviews), 3)
        later = reviewed_by(REV, raw_review('approve', previous=[
            {'id': f'1/{REV}/R1-2', 'status': 'resolved', 'note': ''},
            {'id': '3/ci/R3-1', 'status': 'resolved', 'note': ''}]), state.open, round_no=4)
        self.assertEqual(keys(review.open_findings(old + [later])), ['3/ci/R3-1'])

    def test_a_record_without_keys_after_keyed_ones_closes_nothing(self):
        # A keyed request for changes by OTHER, then an approval by REV without keys that
        # declares OTHER's finding resolved, through snapshot, plan and promotion.
        first = reviewed_by(OTHER, raw_review('request_changes', [finding()]))
        approve = legacy(reviewed_by(REV, raw_review('approve', previous=[
            {'id': f'1/{OTHER}/R1-1', 'status': 'resolved', 'note': 'ok'}]),
            review.open_findings([first]), round_no=2))
        self.assertEqual(approve['previous_findings'][0]['status'], 'resolved')
        self.assertEqual(keys(review.open_findings([first, approve])), [f'1/{OTHER}/R1-1'])
        self.make([review_comment(first), fix_comment(1), review_comment(approve)])
        snap = self.d.snapshot()
        self.assertEqual(snap.prs[0].finding_problems, [])
        self.assertEqual(len(snap.prs[0].reviews), 2)
        self.assertEqual([a.kind for a in plan(snap, CFG)], ['apply_review'])
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:needs-human'})
        self.assertEqual(self.published('policy'), [])  # no promotion
        self.assertIn(f'1/{OTHER}/R1-1', self.gh.comments[-1][1])
        # Its own findings still open, attributed to its reviewer; so do partly keyed records.
        later = legacy(reviewed_by(REV, raw_review('request_changes', [finding()]),
                                   review.open_findings([first]), round_no=2))
        self.assertEqual(keys(review.open_findings([first, later])),
                         [f'1/{OTHER}/R1-1', f'2/{REV}/R2-1'])
        mixed = dict(approve, blocking_findings=[dict(finding(), id='R2-1', key=f'2/{REV}/R2-1')])
        self.assertEqual(keys(review.open_findings([legacy(first), mixed])),
                         [f'1/{OTHER}/R1-1', f'2/{REV}/R2-1'])

    def test_an_unattributable_marker_sends_the_pr_to_a_human(self):
        first = legacy(reviewed_by(REV, raw_review('request_changes', [finding()])))
        orphan = legacy(reviewed_by(REV, raw_review('request_changes', [finding()]), round_no=2))
        orphan['previous_findings'] = [{'id': 'R1-7', 'status': 'unresolved', 'note': ''}]
        no_reviewer = {k: v for k, v in first.items() if k != 'reviewer'}
        for comments, detail in (([review_comment(first), review_comment(orphan)], 'R1-7'),
                                 ([review_comment(no_reviewer)], 'non registra il reviewer')):
            with self.subTest(detail=detail):
                self.make(comments)
                self.assertEqual([a.kind for a in self.planned()], ['unattributable'])
                self.d.tick()
                self.assertEqual(self.gh.labels[100], {'agent:needs-human'})
                self.assertIn(detail, self.gh.comments[-1][1])
                self.assertEqual(self.runner.calls, [])

    def test_a_review_cannot_attribute_findings_to_another_owner(self):
        record = reviewed_by(REV, raw_review('request_changes', [finding()]))
        record['blocking_findings'][0]['key'] = '1/ci/R1-1'
        self.make([review_comment(record)], labels=('agent:changes',))
        snap = self.d.snapshot()
        self.assertEqual(snap.prs[0].reviews, [])
        self.assertIn('1/ci/R1-1', snap.prs[0].finding_problems[0])
        self.assertEqual([a.kind for a in plan(snap, CFG)], ['unattributable'])


if __name__ == '__main__':
    unittest.main()
