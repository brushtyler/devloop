"""devloop: a resolution by deletion survives the files the local tests regenerate (#662).

On a real repository whose ``origin`` is itself: the base branch takes a tracked file out of
the index and ignores it in ``.gitignore``, the PR changes it, the sync resolves the conflict by
deleting it, and the local tests create it again. The resolution enters the index before the
tests, so the file is an untracked, ignored file that the commit leaves out."""

import dataclasses
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devloop import markdown
from devloop.dispatcher import Dispatcher
from devloop.workspace import Workspace
from test_devloop import CFG, FakeGH, FakeRunner, finding, fixed, norm, pr, raw_review

BRANCH = 'agent/issue-7'
REGENERATED = 'build/out.txt'
SYNCED = {'status': 'completed', 'summary': 'Resolved.', 'blocked_reason': ''}
IDENTITY = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@x',
            'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@x'}
# The local tests create the file again, as a test suite creates its caches or artefacts.
CFG_TESTS = dataclasses.replace(CFG, local_tests='full', implement_test_retries=0,
                                test_cmd=f'mkdir -p build && echo regenerated > {REGENERATED}')


class ActingRunner(FakeRunner):
    """A runner whose turns act on their worktree: each output comes with a function of it."""

    def __init__(self, *turns):
        super().__init__(*(output for _, output in turns))
        self.actions = [action for action, _ in turns]

    def run(self, agent, mode, prompt, schema_name, workdir, label):
        self.actions.pop(0)(Path(workdir))
        return super().run(agent, mode, prompt, schema_name, workdir, label)


def delete(path):
    (path / REGENERATED).unlink()  # the resolution: the file goes, as on the base


def keep(path):
    pass  # the resolution: the branch's version stays


def change(path):
    (path / 'pkg' / 'm.py').write_text('X = 2\n')  # the change of a fix


class SyncResolutionTests(unittest.TestCase):
    def git(self, *args, cwd=None):
        return subprocess.run(['git', *args], cwd=cwd or self.repo, check=True,
                              capture_output=True, text=True,
                              env=dict(os.environ, **IDENTITY)).stdout.strip()

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = repo = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, IDENTITY)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.git('init', '-q', '-b', 'main')
        (repo / '.gitignore').write_text('.state/\n')
        (repo / 'pkg').mkdir()
        (repo / 'pkg' / 'm.py').write_text('X = 1\n')
        (repo / 'build').mkdir()
        (repo / REGENERATED).write_text('v1\n')
        self.commit('root')
        self.git('remote', 'add', 'origin', str(repo))
        self.git('checkout', '-q', '-b', BRANCH)
        (repo / REGENERATED).write_text('changed by the PR\n')
        self.branch_head = self.commit('the PR changes the file')
        self.git('checkout', '-q', 'main')
        # The base takes the file out of the index and ignores it.
        self.git('rm', '-q', '--cached', REGENERATED)
        (repo / '.gitignore').write_text('.state/\nbuild/\n')
        self.commit('untrack build/')
        (repo / REGENERATED).unlink()
        self.ws = Workspace(repo, '.state', 'main')
        self.gh = FakeGH()

    def dispatcher(self, *turns):
        self.runner = ActingRunner(*turns)
        return Dispatcher(CFG_TESTS, self.gh, self.ws, self.runner, log=lambda m: None)

    def files(self, commit):
        return self.git('ls-tree', '-r', '--name-only', commit).splitlines()

    def pushed(self):
        return self.git('rev-parse', f'refs/heads/{BRANCH}')

    def sync_marker(self):
        (body,) = [b for _, b in self.gh.comments if markdown.read_marker(b, 'sync')]
        return body, markdown.read_marker(body, 'sync')

    def test_a_sync_resolved_by_deletion_leaves_the_regenerated_file_out(self):
        d = self.dispatcher((delete, SYNCED))
        p = pr(merge='conflict', head=self.branch_head)
        self.gh.labels[100] = set(p.labels)
        d._do_sync(p)
        head = self.pushed()
        self.assertNotEqual(head, self.branch_head)
        parents = self.git('rev-list', '--parents', '-n1', head).split()[1:]
        self.assertEqual(parents, [self.branch_head, self.git('rev-parse', 'main')])
        self.assertNotIn(REGENERATED, self.files(head))
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        body, marker = self.sync_marker()
        self.assertNotIn('generated_files', marker)
        self.assertNotIn('File generati nel commit', body)

    def test_a_kept_file_the_base_removed_is_pointed_out(self):
        d = self.dispatcher((keep, SYNCED))
        p = pr(merge='conflict', head=self.branch_head)
        self.gh.labels[100] = set(p.labels)
        d._do_sync(p)
        self.assertIn(REGENERATED, self.files(self.pushed()))
        body, marker = self.sync_marker()
        self.assertEqual(marker['generated_files'],
                         [{'path': REGENERATED, 'reasons': ['restored']}])
        self.assertIn('File generati nel commit', body)
        self.assertIn(f'- `{REGENERATED}` (tolto dal branch principale, mantenuto dal branch)',
                      body)

    def test_a_fix_after_the_sync_leaves_the_regenerated_file_out(self):
        reviews = [norm(raw_review('request_changes', [finding()]), head=self.branch_head)]
        d = self.dispatcher((delete, SYNCED), (change, fixed()))
        before = pr(labels=['agent:changes'], reviews=reviews, head=self.branch_head)
        self.gh.labels[100] = set(before.labels)
        # The branch conflicts with the base: the fix runs the sync first (#250).
        d._do_fix(before)
        synced = self.pushed()
        self.assertNotIn(REGENERATED, self.files(synced))
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        after = pr(labels=['agent:changes'], reviews=reviews, head=synced)
        after.syncs = [{'head_sha': synced, 'from': self.branch_head}]
        d._do_fix(after)
        head = self.pushed()
        self.assertEqual(self.git('rev-parse', f'{head}^'), synced)
        self.assertEqual(self.git('show', f'{head}:pkg/m.py'), 'X = 2')
        self.assertNotIn(REGENERATED, self.files(head))
        self.assertEqual(self.runner.outputs, [])


class StageMergeTests(unittest.TestCase):
    """``Workspace.stage_merge`` and ``Workspace.restored_files`` on a real repository."""

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, check=True, capture_output=True,
                              text=True, env=dict(os.environ, **IDENTITY)).stdout.strip()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = repo = Path(tmp.name)
        self.git('init', '-q', '-b', 'main')
        (repo / 'a.py').write_text('a\n')
        (repo / 'gone.txt').write_text('g\n')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'root')
        self.git('checkout', '-q', '-b', 'pr')
        (repo / 'a.py').write_text('pr\n')
        (repo / 'gone.txt').write_text('pr\n')
        self.git('commit', '-q', '-am', 'pr')
        self.head = self.git('rev-parse', 'HEAD')
        self.git('checkout', '-q', 'main')
        (repo / 'a.py').write_text('main\n')
        self.git('rm', '-q', 'gone.txt')
        self.git('commit', '-q', '-am', 'main')
        self.git('update-ref', 'refs/remotes/origin/main', 'main')
        self.git('checkout', '-q', 'pr')
        self.ws = Workspace(repo, '.state', 'main')

    def test_without_a_merge_nothing_is_staged(self):
        (self.repo / 'a.py').write_text('changed\n')
        self.assertIsNone(self.ws.stage_merge(self.repo))
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')

    def test_the_resolution_enters_the_index(self):
        self.assertEqual(self.ws.start_merge(self.repo, author={'name': 't', 'email': 't@x'}),
                         ['a.py', 'gone.txt'])
        (self.repo / 'a.py').write_text('pr\nmain\n')
        (self.repo / 'gone.txt').unlink()
        self.assertEqual(self.ws.stage_merge(self.repo), [])
        self.assertEqual(self.git('ls-files', 'gone.txt'), '')
        # A file created again afterwards is untracked: not in the index.
        (self.repo / 'gone.txt').write_text('again\n')
        self.assertEqual(self.git('ls-files', 'gone.txt'), '')
        self.assertIsNotNone(self.ws.merge_head(self.repo))  # the merge stays in progress

    def test_a_kept_file_the_base_removed_is_restored(self):
        self.ws.start_merge(self.repo, author={'name': 't', 'email': 't@x'})
        (self.repo / 'a.py').write_text('pr\nmain\n')
        self.ws.stage_merge(self.repo)
        self.ws.commit_all(self.repo, 'merge', author={'name': 't', 'email': 't@x'})
        self.assertEqual(self.ws.restored_files(self.repo, self.head), ['gone.txt'])
        # Not since a commit that already holds the deletion.
        self.assertEqual(self.ws.restored_files(self.repo, 'origin/main'), [])


if __name__ == '__main__':
    unittest.main()
