"""devloop: a fix starts from a branch that contains the base, as CI's merge commit (#250)."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from devloop.dispatch_actions import synced_from
from devloop.planning import Action, counted_rounds
from devloop.workspace import Workspace
import test_devloop_supervision as supervision_tests
from test_devloop import CFG, SHA1, SHA2, SHA3, finding, fixed, pr, raw_review
from test_devloop_quorum import Quorum

LEVEL1 = [{'path': 'x.py', 'links': []}]  # a diff reviewed at level 1
CHANGES = raw_review('request_changes', [finding()])
SYNCED = {'status': 'completed', 'summary': 'Kept both sides.', 'blocked_reason': ''}
MERGE = 'Merge origin/main into agent/issue-7'


class FixBaseTests(Quorum):
    def changes_requested(self, *outputs):
        """Round 1 asks for changes on SHA1; then what each agent turn and push saw of the
        worktree is recorded: ``(schema, commits so far, merge in progress)``."""
        self.make(CHANGES, *outputs, diff=LEVEL1)
        self.assertEqual(self.d.tick(), [Action('review', 100)])
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        self.seen, self.pushed, self.events = [], [], []
        run, push = self.runner.run, self.ws.push

        def spy_run(agent, mode, prompt, schema, workdir, label):
            self.seen.append((schema, list(self.ws.commits), self.ws.merging))
            return run(agent, mode, prompt, schema, workdir, label)

        def spy_push(path, branch, replace=False, auth=None):
            self.pushed.append(list(self.ws.commits))
            return push(path, branch, replace=replace, auth=auth)
        self.runner.run, self.ws.push = spy_run, spy_push
        self.d._ledger_event = lambda number, event, register=None, **fields: (
            self.events.append((number, event, fields)))

    def test_a_fix_behind_the_base_starts_on_its_merge_and_pushes_both(self):
        self.changes_requested(fixed())
        self.ws.behind, self.ws.conflicts = True, ()
        self.assertEqual(self.planned(), [Action('fix', 100)])
        self.d.tick()
        # The agent starts on the merge commit the dispatcher concluded, no merge in progress.
        (seen,) = self.seen
        self.assertEqual(seen[0], 'fix')
        self.assertEqual(len(seen[1]), 1)
        self.assertTrue(seen[1][0].startswith(MERGE))
        self.assertIn('Refs #7', seen[1][0])
        self.assertIsNone(seen[2])
        # One push, of the merge and of the fix on top of it.
        merge, fix = self.ws.commits
        self.assertIn('fix: review', fix)
        self.assertEqual(self.pushed, [[merge, fix]])
        self.assertEqual(self.ws.pushes, [('agent/issue-7', False)])
        # The merge is the implementer's, as in a sync.
        self.assertEqual(self.ws.merge_authors, [self.ws.authors[0]])
        self.assertEqual(self.ws.authors[0], self.ws.authors[1])
        # Protected paths: the merge against the base, the fix against the merge; then the
        # files of the fix's own commit, for its generated files (#627).
        self.assertEqual(self.ws.spans, ['origin/main', SHA2, SHA2])
        prompt = self.runner.calls[-1][3]
        self.assertIn('The branch already contains the latest `origin/main`', prompt)
        self.assertIn('Do not fetch,\nmerge or rebase', prompt)
        # The ledger records the sync, with base and heads, as a sync without an agent.
        self.assertEqual(self.events, [(100, 'sync', {'base': SHA3, 'head': SHA2,
                                                      'previous_head': SHA1})])
        body = self.gh.comments[-1][1]
        self.assertIn('Prima della correzione il dispatcher ha fatto il merge di `origin/main`',
                      body)
        (record,) = self.records('fix')
        self.assertEqual((record['round'], record['head_sha']), (1, SHA2))
        self.assertEqual(self.gh.labels[100], {'agent:review'})

    def test_the_merge_is_pushed_even_when_the_agent_changes_nothing(self):
        self.changes_requested(fixed())
        self.ws.behind, self.ws.conflicts, self.ws.changes = True, (), False
        self.d.tick()
        (merge,) = self.ws.commits
        self.assertTrue(merge.startswith(MERGE))
        self.assertEqual(self.pushed, [[merge]])
        self.assertEqual(self.records('fix')[0]['head_sha'], SHA2)

    def test_the_merge_of_the_base_is_not_a_round(self):
        self.changes_requested(fixed(), raw_review())
        self.ws.behind, self.ws.conflicts = True, ()
        self.d.tick()
        self.assertEqual(len(self.records('review')), 1)  # the fix tick wrote no review
        self.gh.head = SHA2
        reviews = self.d.snapshot().prs[0].reviews
        self.assertEqual(counted_rounds(reviews), 1)
        self.assertEqual(self.planned(), [Action('review', 100)])
        self.ws.behind = False
        self.d.tick()
        # The review of the fix is round 2: the merge opened none.
        second = self.records('review')[-1]
        self.assertEqual((second['round'], second['head_sha']), (2, SHA2))
        self.assertEqual(counted_rounds(self.d.snapshot().prs[0].reviews), 2)

    def test_with_conflicts_the_fix_waits_for_the_usual_sync(self):
        self.changes_requested(SYNCED, fixed())
        self.ws.behind, self.ws.conflicts = True, ('x.py',)
        self.d.tick()
        # The sync's agent resolved the conflicts; the fix did not start.
        self.assertEqual([s[0] for s in self.seen], ['sync'])
        self.assertEqual(self.records('fix'), [])
        self.assertEqual(len(self.ws.commits), 1)
        self.assertIn('x.py', self.ws.commits[0])
        self.assertEqual(self.ws.pushes, [('agent/issue-7', False)])
        (sync,) = self.records('sync')
        self.assertEqual((sync['head_sha'], sync['from']), (SHA2, SHA1))
        self.assertIn('La correzione in attesa parte dal nuovo head', self.gh.comments[-1][1])
        # #289: the review after the fix counts, and the comment says which round it is.
        self.assertIn('La review dopo la correzione **conterà** come round, come ogni review '
                      'dopo una correzione: round 2 di 3', self.gh.comments[-1][1])
        # The PR keeps its label: the fix runs on the synced head at the next tick.
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        self.gh.head = SHA2
        self.ws.behind = False
        snap = self.d.snapshot()
        self.assertEqual(counted_rounds(snap.prs[0].reviews), 1)
        self.assertEqual(self.planned(), [Action('fix', 100)])
        self.d.tick()
        self.assertEqual([s[0] for s in self.seen], ['sync', 'fix'])
        (record,) = self.records('fix')
        self.assertEqual(record['round'], 1)
        self.assertNotIn('base_merge', record)
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.assertEqual(counted_rounds(self.d.snapshot().prs[0].reviews), 1)

    def test_a_requested_fix_waits_for_the_sync_too(self):
        self.make(SYNCED, dict(fixed(), responses=[]), diff=LEVEL1)
        self.gh.comments.append((100, '/devloop fix\nRinomina x.'))
        self.gh.written.append('brushtyler')
        self.ws.behind, self.ws.conflicts = True, ('x.py',)
        self.assertEqual([a.kind for a in self.planned()], ['fix'])
        self.d.tick()
        self.assertEqual([c[2] for c in self.runner.calls], ['sync'])
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.gh.head = SHA2
        self.ws.behind = False
        self.assertEqual([a.kind for a in self.planned()], ['fix'])
        self.d.tick()
        self.assertEqual([c[2] for c in self.runner.calls], ['sync', 'fix'])
        (record,) = self.records('fix')
        self.assertEqual(record['from'], SHA2)

    def test_a_branch_that_contains_the_base_is_fixed_as_before(self):
        self.changes_requested(fixed())
        self.d.tick()
        self.assertEqual(self.ws.merge_authors, [])  # no merge started
        # Protected paths, then the generated files of the fix's commit (#627).
        self.assertEqual(self.ws.spans, [SHA1, SHA1])
        (commit,) = self.ws.commits
        self.assertIn('fix: review', commit)
        self.assertEqual(self.events, [])
        self.assertNotIn('Prima della correzione', self.gh.comments[-1][1])

    def test_a_head_moved_by_someone_else_still_goes_back_to_review(self):
        self.changes_requested(fixed())
        self.gh.head = self.ws._head = SHA2
        self.d.tick()
        self.assertEqual(self.seen, [])
        self.assertEqual(self.gh.labels[100], {'agent:review'})


class SupervisedSyncTests(supervision_tests.Case):
    def test_a_sync_started_by_a_fix_in_changes_is_retried_from_changes(self):
        # The sync a fix started in agent:changes fails: the supervisor's retry resumes the PR
        # in agent:changes, where the fix of the round is still to run, not in agent:review.
        d = self.make(supervision_tests.AUTO, CHANGES, supervision_tests.crash(), SYNCED,
                      fixed())
        self.assertEqual(self.tick(d), [('review', 100)])
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        self.ws.behind, self.ws.conflicts = True, ('x.py',)
        self.assertEqual(self.tick(d), [('fix', 100)])
        self.assertEqual(self.gh.labels[100], {supervision_tests.SUPER})
        (record,) = self.escalations(100)
        self.assertEqual((record['step'], record['resume']), ('sync', 'agent:changes'))
        self.gh.command(100, '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        # The fix starts the sync again, which now succeeds, and runs on the synced head.
        self.assertEqual(self.tick(d), [('fix', 100)])
        self.assertEqual(self.gh.records(100, 'fix'), [])
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        self.gh.head = SHA2
        self.ws.behind = False
        self.assertEqual(self.tick(d), [('fix', 100)])
        (fix,) = self.gh.records(100, 'fix')
        self.assertEqual(fix['round'], 1)
        self.assertEqual(self.gh.labels[100], {'agent:review'})

    def test_a_sync_resumes_from_the_label_of_the_fix_that_started_it(self):
        d = self.make(supervision_tests.AUTO)
        for labels, resume in ((('agent:changes',), 'agent:changes'),
                               (('agent:review',), 'agent:review'),
                               (('agent:ready-to-merge',), 'agent:review')):
            with self.subTest(labels=labels):
                route, _ = d._supervised(100, 'sync_failed', 'sync', pr(labels=labels))
                self.assertEqual(route['resume'], resume)


class SyncedFromTests(unittest.TestCase):
    def test_only_a_chain_of_sync_markers_leads_back_to_the_reviewed_head(self):
        one = {'head_sha': SHA2, 'from': SHA1}
        two = {'head_sha': SHA3, 'from': SHA2}
        self.assertTrue(synced_from([one], SHA2, SHA1))
        self.assertTrue(synced_from([one, two], SHA3, SHA1))
        self.assertFalse(synced_from([two], SHA3, SHA1))  # a gap
        self.assertFalse(synced_from([{'head_sha': SHA2}], SHA2, SHA1))  # before #250
        self.assertFalse(synced_from([], SHA2, SHA1))
        loop = [{'head_sha': SHA2, 'from': SHA3}, {'head_sha': SHA3, 'from': SHA2}]
        self.assertFalse(synced_from(loop, SHA2, SHA1))


class WorkspaceTests(unittest.TestCase):
    """The git steps of a fix behind the base, on a real repository whose ``origin`` is
    itself."""

    def git(self, *args, cwd=None):
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@x',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@x')
        return subprocess.run(['git', *args], cwd=cwd or self.repo, env=env,
                              capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, message, **files):
        for name, text in files.items():
            (self.repo / f'{name}.py').write_text(text)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.git('init', '-q', '-b', 'main')
        (self.repo / '.gitignore').write_text('.state/\n')
        self.commit('root', a='a\n', b='b\n')
        self.git('remote', 'add', 'origin', str(self.repo))
        self.git('checkout', '-q', '-b', 'pr')
        self.commit('pr', a='pr\n')
        self.git('checkout', '-q', 'main')
        self.ws = Workspace(self.repo, '.state', 'main')

    def test_a_branch_behind_the_base_gets_a_merge_commit_that_contains_it(self):
        main = self.commit('main moves', b='main\n')
        path = self.ws.existing_branch('pr-1', 'pr')
        self.assertFalse(self.ws.contains_base(path))
        author = {'name': 'impl', 'email': 'impl@x'}
        self.assertEqual(self.ws.start_merge(path, author=author), [])
        head = self.ws.commit_all(path, 'Merge origin/main into pr\n', author=author)
        self.assertTrue(self.ws.contains_base(path))
        self.git('merge-base', '--is-ancestor', main, head, cwd=path)  # raises if not
        self.assertEqual(len(self.git('rev-list', '--parents', '-n1', head, cwd=path).split()), 3)

    def test_a_branch_that_contains_the_base_needs_no_merge(self):
        path = self.ws.existing_branch('pr-1', 'pr')
        self.assertTrue(self.ws.contains_base(path))
        self.assertIsNone(self.ws.start_merge(path))


if __name__ == '__main__':
    unittest.main()
