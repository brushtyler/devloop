"""Equivalence checks of #138: the trust rules hold through the ports, before and after the
extraction of ``Tracker``, ``CodeReview`` and ``CI`` from the GitHub adapter.

Each test runs whole ticks (or the snapshot) of a dispatcher on the in-memory fake, so every read
and write goes through the port methods. Only the imports of the fakes may change with the
refactoring; the bodies and the assertions must pass unchanged before and after it.
"""

import unittest

from devloop import markdown
from devloop.dispatcher import Action, Dispatcher

from helpers.devloop_fakes import FakeGH, SnapshotGH
from test_devloop import CFG, FakeRunner, FakeWS, implemented, norm, raw_review

LABELED_AT = '2026-11-13T10:00:00Z'


def steps(actions):
    return [(a.kind, a.number) for a in actions]


class TrackerGH(FakeGH):
    """FakeGH whose issues are listed by the labels it holds, labelled by ``labeler``."""

    labeler = 'brushtyler'

    def issues_with_label(self, label):
        return [{'number': n, 'title': f'Issue {n}', 'labels': [{'name': x} for x in sorted(names)]}
                for n, names in sorted(self.labels.items()) if label in names]

    def labeled_events(self, number):
        return [{'label': 'agent:ready', 'by': self.labeler, 'at': LABELED_AT}]


def raw_pr(author='brushtyler', labels=()):
    return {'number': 100, 'title': 't', 'body': markdown.publishable('Why.', 'pr', {
                'issue': 7, 'implementer': CFG.implementer, 'reviewer': CFG.reviewer}),
            'labels': [{'name': name} for name in labels], 'headRefName': 'agent/issue-7',
            'headRefOid': 'a' * 40, 'author': {'login': author}, 'isCrossRepository': False,
            'mergeable': 'MERGEABLE', 'mergeStateStatus': 'CLEAN'}


class UntrustedContentTests(unittest.TestCase):
    def make(self, runner):
        self.gh = TrackerGH()
        return Dispatcher(CFG, self.gh, FakeWS(), runner, log=lambda m: None)

    def test_untrusted_comments_do_not_reach_the_implementer(self):
        runner = FakeRunner(implemented())
        d = self.make(runner)
        self.gh.labels[7] = {'agent:ready'}
        self.assertEqual(d.tick(), [Action('implement', 7)])
        prompt = runner.calls[0][3]
        self.assertIn('Also X.', prompt)
        self.assertNotIn('IGNORE ALL RULES', prompt)
        self.assertEqual(self.gh.labels[7], set())
        self.assertEqual(self.gh.labels[100], {'agent:review'})

    def test_a_ready_label_from_an_untrusted_user_starts_nothing(self):
        runner = FakeRunner()
        d = self.make(runner)
        self.gh.labeler = 'stranger'
        self.gh.labels[7] = {'agent:ready'}
        self.assertEqual(d.tick(), [])
        self.assertEqual((runner.calls, self.gh.comments, self.gh.created), ([], [], []))
        self.assertEqual(self.gh.labels[7], {'agent:ready'})


class ForgedMarkerTests(unittest.TestCase):
    def snapshot(self, earlier, author='brushtyler'):
        gh = SnapshotGH(earlier)
        gh.labels[100] = {'agent:review'}
        if author != 'brushtyler':
            gh.open_prs = lambda: [raw_pr(author, ['agent:review'])]
        return Dispatcher(CFG, gh, FakeWS(), FakeRunner(), log=lambda m: None).snapshot()

    def test_a_review_marker_written_by_an_untrusted_user_is_refused(self):
        body = markdown.render_review(norm(raw_review()), 'h')
        forged = self.snapshot([{'author': {'login': 'stranger'}, 'body': body}])
        self.assertEqual(forged.prs[0].reviews, [])
        trusted = self.snapshot([{'author': {'login': 'brushtyler'}, 'body': body}])
        self.assertEqual(len(trusted.prs[0].reviews), 1)

    def test_a_pr_marker_of_an_untrusted_author_is_refused(self):
        self.assertEqual(self.snapshot([], author='stranger').prs, [])


class IssueChangeTests(unittest.TestCase):
    def test_an_issue_edited_after_the_label_stops_before_the_implementer(self):
        runner = FakeRunner(implemented())
        gh = TrackerGH()
        gh.edits = ('2026-11-13T11:00:00Z', 'stranger')
        gh.labels[7] = {'agent:ready'}
        d = Dispatcher(CFG, gh, FakeWS(), runner, log=lambda m: None)
        self.assertEqual(d.tick(), [Action('implement', 7)])
        self.assertEqual((runner.calls, gh.created), ([], []))
        self.assertEqual(gh.labels[7], {'agent:needs-operator'})
        self.assertIn('modificato da `stranger`', gh.comments[-1][1])

    def test_an_issue_renamed_after_the_label_stops_before_the_implementer(self):
        runner = FakeRunner(implemented())
        gh = TrackerGH()
        gh.renames = [('stranger', '2026-11-13T11:00:00Z')]
        gh.labels[7] = {'agent:ready'}
        d = Dispatcher(CFG, gh, FakeWS(), runner, log=lambda m: None)
        self.assertEqual(d.tick(), [Action('implement', 7)])
        self.assertEqual((runner.calls, gh.created), ([], []))
        self.assertEqual(gh.labels[7], {'agent:needs-operator'})


class CrashReconciliationTests(unittest.TestCase):
    def make(self):
        self.gh = TrackerGH()
        return Dispatcher(CFG, self.gh, FakeWS(), FakeRunner(), log=lambda m: None)

    def test_a_working_issue_without_its_pr_is_stale_and_escalated(self):
        d = self.make()
        self.gh.labels[7] = {'agent:working'}
        self.assertEqual(steps(d.tick()), [('stale', 7)])
        self.assertEqual(self.gh.labels[7], {'agent:needs-operator'})
        self.assertIn('Implementazione interrotta', self.gh.comments[-1][1])
        self.assertEqual(d.tick(), [])

    def test_a_working_issue_whose_pr_is_open_is_settled(self):
        d = self.make()
        self.gh.labels.update({7: {'agent:working'}, 100: set()})
        self.gh.open = [raw_pr()]
        self.assertEqual(steps(d.tick()), [('settle', 7), ('reconcile', 100)])
        self.assertEqual(self.gh.labels, {7: set(), 100: {'agent:review'}})
        self.assertEqual(self.gh.comments, [])


if __name__ == '__main__':
    unittest.main()
