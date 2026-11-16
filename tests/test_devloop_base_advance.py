"""devloop: the base branch advancing during a round (#160)."""

import dataclasses
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from devloop.dispatcher import Action, counted_rounds, touched_by_base, waived_rounds
from devloop.workspace import Workspace, WorkspaceError
from test_devloop import BASE, CFG, finding, raw_review, receipt
from test_devloop_quorum import FIRST, SECOND, Quorum

NEW = 'e' * 40  # the base after main advanced
PR_FILE = 'devloop/dispatcher.py'  # the PR's diff in Quorum: level 2


class BaseAdvanceTests(Quorum):
    def first_review_then_base_moves(self, touched, *outputs, cfg=CFG):
        """The first reviewer reviews on BASE; then main advances to NEW, changing
        ``touched`` (a list of paths, or an exception: not listable)."""
        self.make(raw_review(), *outputs, cfg=cfg)
        self.assertEqual(self.d.tick(), [Action('review', 100)])
        self.assertEqual(self.reviewed(), [FIRST])
        self.ws.base, self.ws.base_touched = NEW, touched

    def test_a_base_that_leaves_the_pr_alone_continues_the_round(self):
        self.first_review_then_base_moves(['README.md', 'docs/other.md'], raw_review())
        self.assertEqual(self.planned(), [Action('review', 100, f'the round waits for {SECOND}')])
        self.d.tick()
        # Only the missing reviewer runs, in the same round.
        self.assertEqual(self.reviewed(), [FIRST, SECOND])
        first, second = self.records('review')
        self.assertEqual((first['round'], second['round']), (1, 1))
        self.assertEqual((first['receipt']['base'], second['receipt']['base']), (BASE, NEW))
        # The joining review records both bases and the check.
        self.assertEqual(second['bases'], [{'from': BASE, 'to': NEW, 'outcome': 'disjoint'}])
        self.assertNotIn('restarts', second)
        self.assertIn('La base è avanzata durante il round', self.gh.comments[1][1])
        # Promotion recomputes the receipt on NEW and admits the approval given on BASE only
        # after checking the commits in between again.
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        (promoted,) = self.records('policy')
        self.assertEqual(promoted['event'], 'promoted')
        self.assertEqual(promoted['receipt']['base'], NEW)
        self.assertEqual(promoted['bases'], [{'from': BASE, 'to': NEW, 'outcome': 'disjoint'}])
        self.assertIn('a parte la base', self.gh.comments[-1][1])
        self.assertEqual(self.ws.base_spans, [(BASE, NEW), (BASE, NEW)])

    def test_a_base_that_touches_a_pr_file_restarts_the_round(self):
        self.first_review_then_base_moves([PR_FILE], raw_review(), raw_review())
        self.d.tick()
        # The round restarts: the first reviewer reviews again, on the new base.
        self.assertEqual(self.reviewed(), [FIRST, FIRST])
        restarted = self.records('review')[-1]
        self.assertEqual((restarted['round'], restarted['receipt']['base']), (2, NEW))
        self.assertEqual(restarted['restarts'], {'round': 1, 'bases': [
            {'from': BASE, 'to': NEW, 'outcome': 'touched', 'paths': [PR_FILE]}]})
        self.assertIn('Il round 1 è stato abbandonato', self.gh.comments[-1][1])
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.d.tick()
        self.assertEqual(self.reviewed(), [FIRST, FIRST, SECOND])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        (promoted,) = self.records('policy')
        self.assertNotIn('bases', promoted)  # both approvals are on the current base

    def test_a_directory_or_an_unlistable_range_restarts_the_round(self):
        # A file of the PR under a path main turned into a file, or the other way round.
        for touched in (['devloop'], ['devloop/dispatcher.py/inner'],
                        WorkspaceError('main was rewritten')):
            with self.subTest(touched=touched):
                self.first_review_then_base_moves(touched, raw_review())
                self.d.tick()
                self.assertEqual(self.reviewed(), [FIRST, FIRST])
                self.assertEqual(self.records('review')[-1]['round'], 2)
                outcome = self.records('review')[-1]['restarts']['bases'][0]['outcome']
                self.assertEqual(outcome, 'unverifiable' if isinstance(touched, Exception)
                                 else 'touched')

    def test_a_round_restarted_for_the_base_does_not_count(self):
        # With one round allowed, the restart is not a second round.
        cfg = dataclasses.replace(CFG, max_review_rounds=1)
        self.first_review_then_base_moves([PR_FILE], raw_review(), raw_review(), cfg=cfg)
        self.d.tick()
        self.assertEqual(self.reviewed(), [FIRST, FIRST])
        reviews = self.d.snapshot().prs[0].reviews
        self.assertEqual(waived_rounds(reviews), {1})
        self.assertEqual(counted_rounds(reviews), 1)
        # The prompt shows the last round the limit allows, round 2 replacing round 1.
        self.assertIn('round 2 of at most 2', self.runner.calls[-1][3])
        self.assertEqual(self.records('review')[-1]['max_rounds'], 2)
        self.assertEqual(self.planned(), [Action('review', 100, f'the round waits for {SECOND}')])
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})

    def test_the_limit_still_applies_to_the_round_that_replaces(self):
        # The replacing round counts: a request for changes in it reaches the limit.
        cfg = dataclasses.replace(CFG, max_review_rounds=1)
        self.first_review_then_base_moves(
            [PR_FILE], raw_review(), raw_review('request_changes', [finding()]), cfg=cfg)
        self.d.tick(), self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:needs-human'})
        self.assertIn('Raggiunto il limite di 1 round', self.gh.comments[-1][1])

    def test_a_base_that_advances_during_the_last_review_is_checked_at_promotion(self):
        for touched, label in ((['README.md'], 'agent:ready-to-merge'),
                               ([PR_FILE], 'agent:review'),
                               (WorkspaceError('missing commit'), 'agent:review')):
            with self.subTest(touched=touched):
                self.make(raw_review(), raw_review(), raw_review())
                self.d.tick()
                # The second review starts on BASE; main advances before the promotion.
                bases = iter([BASE, NEW, NEW])
                diff = self.ws.review_diff
                self.ws.review_diff = lambda branch: (next(bases), *diff(branch)[1:])
                self.ws.base_touched = touched
                self.d.tick()
                self.assertEqual(self.reviewed(), [FIRST, SECOND])
                self.assertEqual([r['receipt']['base'] for r in self.records('review')],
                                 [BASE, BASE])
                self.assertEqual(self.gh.labels[100], {label})
                (record,) = self.records('policy')
                self.assertEqual(record['receipt']['base'], NEW)
                self.assertEqual([c['from'] for c in record['bases']], [BASE])
                if label == 'agent:review':
                    self.assertEqual(record['event'], 'stale')
                    self.assertIn('commit di base', self.gh.comments[-1][1])
                    # A new round follows, and it counts: the round was complete.
                    self.assertEqual(self.planned(), [Action(
                        'review', 100, 'the approval has no matching policy receipt')])
                    self.d.tick()
                    reviews = self.d.snapshot().prs[0].reviews
                    self.assertEqual((reviews[-1]['round'], counted_rounds(reviews)), (2, 2))

    def test_a_replayed_approval_on_an_earlier_base_is_revalidated(self):
        self.make(raw_review(), raw_review())
        self.d.tick(), self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        # CI runs again: back to review; meanwhile main advances elsewhere.
        self.gh.ci = 'pending'
        self.d.tick()
        self.gh.ci, self.ws.base, self.ws.base_touched = 'pass', NEW, ['README.md']
        self.assertEqual(self.planned(), [Action('apply_review', 100,
                                                 'approval already recorded')])
        self.d.tick()
        self.assertEqual(self.reviewed(), [FIRST, SECOND])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        promoted = self.records('policy')[-1]
        self.assertEqual((promoted['event'], promoted['receipt']['base']), ('promoted', NEW))
        self.assertEqual(promoted['bases'], [{'from': BASE, 'to': NEW, 'outcome': 'disjoint'}])

    def test_other_changes_with_the_base_still_restart_and_count(self):
        # A rereview voids the round even if the base also moved: it counts as before.
        self.first_review_then_base_moves(['README.md'], raw_review())
        self.gh.comments.append((100, '/devloop rereview\n\nDi nuovo.'))
        self.gh.written.append('brushtyler')
        self.d.tick()
        last = self.records('review')[-1]
        self.assertEqual((last['round'], last['reviewer']), (2, FIRST))
        self.assertNotIn('restarts', last)
        self.assertEqual(counted_rounds(self.d.snapshot().prs[0].reviews), 2)


class CountingTests(unittest.TestCase):
    def record(self, round_no, reviewer, restarts=None, source='agent'):
        r = {'round': round_no, 'source': source, 'reviewer': reviewer, 'verdict': 'approve',
             'receipt': dict(receipt(), level=2, reviewers=[FIRST, SECOND])}
        if restarts is not None:
            r['restarts'] = {'round': restarts}
        return r

    def test_the_example_of_the_issue(self):
        # Round 1 complete, round 2 abandoned for the base, round 3 replaces it: two rounds
        # count, so with three allowed one more (round 4) is left.
        reviews = [self.record(1, FIRST), self.record(1, SECOND), self.record(2, FIRST),
                   self.record(3, FIRST, restarts=2), self.record(3, SECOND)]
        self.assertEqual((waived_rounds(reviews), counted_rounds(reviews)), ({2}, 2))

    def test_only_an_earlier_incomplete_round_is_waived(self):
        complete = [self.record(1, FIRST), self.record(1, SECOND),
                    self.record(2, FIRST, restarts=1)]
        self.assertEqual(counted_rounds(complete), 2)
        ci = [dict(self.record(1, 'ci'), source='ci'), self.record(2, FIRST, restarts=1)]
        self.assertEqual(counted_rounds(ci), 2)
        later = [self.record(1, FIRST, restarts=1), self.record(2, FIRST, restarts=3)]
        self.assertEqual(counted_rounds(later), 2)
        for bad in (True, '1', None):
            self.assertEqual(waived_rounds([self.record(1, FIRST),
                                            dict(self.record(2, FIRST), restarts=bad)]), set())

    def test_touched_paths(self):
        paths = ['a/b.py', 'c.py', 'd/e/f.py']
        self.assertEqual(touched_by_base(paths, []), [])
        self.assertEqual(touched_by_base(paths, ['x.py', 'a/c.py']), [])
        self.assertEqual(touched_by_base(paths, ['c.py']), ['c.py'])
        self.assertEqual(touched_by_base(paths, ['d/e']), ['d/e/f.py'])  # now a file
        self.assertEqual(touched_by_base(paths, ['c.py/g']), ['c.py'])  # now a directory


class BasePathsTests(unittest.TestCase):
    def git(self, *args, cwd):
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@x',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@x')
        return subprocess.run(['git', *args], cwd=cwd, env=env, capture_output=True, text=True,
                              check=True).stdout.strip()

    def commit(self, repo, message, write=(), remove=()):
        for name, text in write:
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        for name in remove:
            (repo / name).unlink()
        self.git('add', '-A', cwd=repo)
        self.git('commit', '-q', '-m', message, cwd=repo)
        return self.git('rev-parse', 'HEAD', cwd=repo)

    def test_every_commit_counts_with_renames_and_merges(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'
            self.git('init', '-q', '-b', 'main', str(repo), cwd=tmp)
            self.commit(repo, 'root', write=[('keep.py', 'k\n'), ('old.py', 'o\n'),
                                             ('early.py', 'e\n')])
            # A branch forked before the old base, merged after it.
            self.git('checkout', '-q', '-b', 'side', cwd=repo)
            self.commit(repo, 'side', write=[('side/s.py', 's\n')])
            self.git('checkout', '-q', 'main', cwd=repo)
            self.commit(repo, 'before old', write=[('early.py', 'changed\n')])
            old = self.git('rev-parse', 'HEAD', cwd=repo)
            # A change later reverted, a rename and the merge of the side branch.
            self.commit(repo, 'add', write=[('tmp.py', 't\n')])
            self.commit(repo, 'revert', remove=['tmp.py'])
            self.git('mv', 'old.py', 'new.py', cwd=repo)
            self.commit(repo, 'rename')
            self.git('merge', '-q', '--no-ff', '-m', 'merge side', 'side', cwd=repo)
            new = self.git('rev-parse', 'HEAD', cwd=repo)
            ws = Workspace(repo, '.state', 'main')
            # early.py changed before the old base: the merge against its second parent would
            # show it, against its first parent it does not.
            self.assertEqual(ws.base_paths(old, new), ['new.py', 'old.py', 'side/s.py',
                                                       'tmp.py'])
            self.assertEqual(ws.base_paths(new, new), [])
            # Not an ancestor (main rewritten), or an unknown commit: not listable.
            with self.assertRaises(WorkspaceError):
                ws.base_paths(new, old)
            with self.assertRaises(WorkspaceError):
                ws.base_paths('f' * 40, new)


if __name__ == '__main__':
    unittest.main()
