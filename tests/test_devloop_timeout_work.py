"""devloop: a writing turn that times out keeps its uncommitted work for the retry (#374)."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from devloop import agents, config
from devloop.agents import AgentError
from devloop.dispatcher import Dispatcher, kept_work_note
from devloop.planning import Action, Snapshot
from devloop.workspace import Workspace
from helpers.devloop_fakes import FakeGH
from test_devloop import CFG, FakeRunner, fixed, implemented
from test_devloop_fallback import DATA, unavailable

IDENTITY = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@x', 'GIT_COMMITTER_NAME': 't',
            'GIT_COMMITTER_EMAIL': 't@x'}
EVIDENCE = 'timeout-issue-7-implement.diff'
NOTE = '## The previous turn timed out'
BINARY = bytes(range(256))


def timed_out():
    """The error of a turn the runner stopped after its timeout."""
    error = AgentError('claude timed out after 3600s')
    error.timed_out = True
    return error


class TurnRunner(FakeRunner):
    """A runner whose outputs may be callables of the turn's worktree, run as the turn runs:
    what the agent does there before it ends, or before the runner stops it."""

    def run(self, agent, mode, prompt, schema_name, workdir, label):
        self.calls.append((agent.key, mode, schema_name, prompt))
        self.workdirs.append(workdir)
        out = self.outputs.pop(0)
        if callable(out):
            out = out(Path(workdir))
        if isinstance(out, Exception):
            raise out
        return out


def partial_work(path):
    """What the implementer leaves before the timeout: a changed file, a deleted one, a new
    text file in a new directory and a new binary file, none committed."""
    (path / 'a.py').write_text('a\nchanged\n')
    (path / 'b.py').unlink()
    (path / 'tests' / 'helpers').mkdir(parents=True)
    (path / 'tests' / 'helpers' / 'host_commands.py').write_text('one\ntwo\nthree\n')
    (path / 'blob.bin').write_bytes(BINARY)
    return timed_out()


class TimeoutWorkTests(unittest.TestCase):
    """A real repository whose ``origin`` is itself, the dispatcher's real Workspace."""

    def git(self, *args, cwd=None):
        return self.raw_git(*args, cwd=cwd).strip()

    def raw_git(self, *args, cwd=None):
        return subprocess.run(['git', *args], cwd=cwd or self.repo, capture_output=True,
                              text=True, check=True).stdout

    def setUp(self):
        env = mock.patch.dict(os.environ, IDENTITY)
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.git('init', '-q', '-b', 'main')
        (self.repo / '.gitignore').write_text(f'{CFG.state_dir}/\n')
        (self.repo / 'a.py').write_text('a\n')
        (self.repo / 'b.py').write_text('b\n')
        self.commit('root')
        self.git('remote', 'add', 'origin', str(self.repo))
        self.logs = self.repo / CFG.state_dir / 'logs'

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def make(self, *outputs, cfg=CFG):
        self.gh, self.lines = FakeGH(), []
        self.ws = Workspace(self.repo, cfg.state_dir, 'main')
        self.runner = TurnRunner(*outputs)
        self.d = Dispatcher(cfg, self.gh, self.ws, self.runner, log=self.lines.append)
        self.d.snapshot = lambda: Snapshot([], [{'number': 7, 'title': 't',
                                                 'labels': {'agent:ready'},
                                                 'labeler': 'brushtyler', 'admitted': True}], [])

    def implement(self):
        self.assertEqual(self.d.tick(), [Action('implement', 7)])

    def worktree(self):
        return self.ws.path('issue-7')

    def test_a_timeout_with_uncommitted_work_saves_it_and_reports_it(self):
        self.make(partial_work)
        root = self.git('rev-parse', 'HEAD')
        self.implement()
        self.assertEqual(self.gh.labels[7], {'agent:needs-operator'})
        body = self.gh.comments[-1][1]
        self.assertIn('Il passo `implement` è fallito: claude timed out after 3600s.', body)
        self.assertIn('Il turno in timeout ha lasciato modifiche non committate: 4 file, '
                      '+4 −1 righe.', body)
        self.assertIn(f'evidenza in `{EVIDENCE}` in `{CFG.state_dir}/logs`', body)
        for line in ('- `a.py`: +1 −0', '- `b.py`: +0 −1', '- `blob.bin`: binario',
                     '- `tests/helpers/host_commands.py`: +3 −0'):
            self.assertIn(line, body)
        self.assertNotIn(str(self.repo), body)
        # Saved as evidence, new and binary files included, and never committed.
        patch = (self.logs / EVIDENCE).read_text(encoding='utf-8')
        self.assertIn('+++ b/tests/helpers/host_commands.py', patch)
        self.assertIn('GIT binary patch', patch)
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.worktree()), root)
        self.assertEqual(self.git('rev-parse', 'HEAD'), root)
        self.assertEqual(self.git('branch', '--list', 'agent/issue-7', '--format=%(objectname)'),
                         root)
        self.assertTrue(any(f'saved as {EVIDENCE}, not committed' in line
                            for line in self.lines))
        self.assertEqual(self.gh.created, [])

    def test_the_retry_finds_the_work_reapplied_in_its_new_worktree(self):
        seen = {}

        def retry(path):
            seen['a'] = (path / 'a.py').read_text()
            seen['b'] = (path / 'b.py').exists()
            seen['new'] = (path / 'tests/helpers/host_commands.py').read_text()
            seen['bin'] = (path / 'blob.bin').read_bytes()
            seen['status'] = self.raw_git('status', '--porcelain', cwd=path)
            seen['head'] = self.git('rev-parse', 'HEAD', cwd=path)
            return implemented('blocked')
        self.make(partial_work, retry)
        root = self.git('rev-parse', 'HEAD')
        self.implement()
        (self.worktree() / 'leftover').write_text('only the old worktree has it\n')
        self.implement()  # the retry: a new worktree from the base
        self.assertFalse((self.worktree() / 'leftover').exists())
        self.assertEqual(seen['a'], 'a\nchanged\n')
        self.assertFalse(seen['b'])
        self.assertEqual(seen['new'], 'one\ntwo\nthree\n')
        self.assertEqual(seen['bin'], BINARY)
        self.assertEqual(seen['head'], root)  # uncommitted, nothing staged
        self.assertEqual(sorted(seen['status'].splitlines()),
                         [' D b.py', ' M a.py', '?? blob.bin', '?? tests/'])
        prompt = self.runner.calls[1][3]
        self.assertIn(NOTE, prompt)
        self.assertIn('stopped after 3600 seconds (`agent_timeout_seconds`)', prompt)
        self.assertIn('They have been reapplied to it, uncommitted', prompt)
        self.assertIn('- `tests/helpers/host_commands.py`', prompt)
        self.assertIn('run the targeted tests of what you change first', prompt)
        self.assertNotIn('```diff', prompt)
        self.assertNotIn(NOTE, self.runner.calls[0][3])
        # Taken once: kept as evidence under another name, not applied by a later run.
        self.assertFalse((self.logs / EVIDENCE).exists())
        (used,) = self.logs.glob(f'{EVIDENCE}.used-*')
        self.assertIn('+changed', used.read_text(encoding='utf-8'))

    def test_a_retry_whose_base_conflicts_gets_the_diff_in_the_prompt(self):
        seen = {}

        def retry(path):
            seen['a'] = (path / 'a.py').read_text()
            seen['new'] = (path / 'tests/helpers/host_commands.py').exists()
            seen['status'] = self.git('status', '--porcelain', cwd=path)
            return implemented('blocked')
        self.make(partial_work, retry)
        self.implement()
        (self.repo / 'a.py').write_text('main rewrote it\n')
        self.commit('main moves')
        self.implement()
        self.assertEqual(seen['a'], 'main rewrote it\n')  # nothing applied, all or nothing
        self.assertFalse(seen['new'])
        self.assertEqual(seen['status'], '')
        prompt = self.runner.calls[1][3]
        self.assertIn(NOTE, prompt)
        self.assertIn('They could not be reapplied to it (`git apply`: ', prompt)
        for name in ('a.py', 'b.py', 'blob.bin', 'tests/helpers/host_commands.py'):
            self.assertIn(f'- `{name}`', prompt)
        self.assertIn('```diff\ndiff --git a/a.py b/a.py', prompt)
        self.assertIn('+changed', prompt)
        self.assertIn('+++ b/tests/helpers/host_commands.py', prompt)
        self.assertIn('run the targeted tests of what you change first', prompt)
        self.assertTrue(any('not applied' in line for line in self.lines))

    def test_the_retry_gets_back_the_exact_bytes_of_crlf_and_non_utf8_text(self):
        # git treats both files as text; the patch must keep their bytes, never decode them.
        self.git('config', 'core.autocrlf', 'false')
        crlf, latin = b'first\r\nsecond\r\n', b'caf\xe9\n'

        def work(path):
            (path / 'crlf.txt').write_bytes(crlf)
            (path / 'a.py').write_bytes(b'a\n' + latin)
            (path / 'latin.txt').write_bytes(latin)
            return timed_out()
        seen = {}

        def retry(path):
            seen.update({name: (path / name).read_bytes()
                         for name in ('crlf.txt', 'a.py', 'latin.txt')})
            return implemented('blocked')
        self.make(work, retry)
        self.implement()
        body = self.gh.comments[-1][1]
        self.assertIn('3 file, +4 −0 righe', body)
        saved = (self.logs / EVIDENCE).read_bytes()
        self.assertIn(b'+first\r\n+second\r\n', saved)
        self.assertIn(b'+' + latin, saved)
        self.implement()
        self.assertEqual(seen, {'crlf.txt': crlf, 'a.py': b'a\n' + latin, 'latin.txt': latin})
        self.assertIn('They have been reapplied to it', self.runner.calls[1][3])

    def test_a_non_utf8_patch_that_does_not_apply_is_shown_in_the_prompt(self):
        def work(path):
            (path / 'a.py').write_bytes(b'a\ncaf\xe9\n')
            return timed_out()
        self.make(work, implemented('blocked'))
        self.implement()
        (self.repo / 'a.py').write_text('main rewrote it\n')
        self.commit('main moves')
        self.implement()
        prompt = self.runner.calls[1][3]
        self.assertIn('They could not be reapplied to it', prompt)
        self.assertIn('+caf�\n', prompt)

    def test_a_timeout_without_uncommitted_work_changes_nothing(self):
        self.make(timed_out(), implemented('blocked'))
        self.implement()
        self.assertEqual(self.gh.labels[7], {'agent:needs-operator'})
        body = self.gh.comments[-1][1]
        self.assertIn('Il passo `implement` è fallito: claude timed out after 3600s.', body)
        self.assertNotIn('non committate', body)
        self.assertNotIn('timeout-', body)
        self.assertFalse(list(self.logs.glob('timeout-*')) if self.logs.exists() else [])
        self.implement()
        self.assertNotIn(NOTE, self.runner.calls[1][3])
        # The same escalation as a turn that fails otherwise, without uncommitted work.
        self.make(AgentError('claude timed out after 3600s'))
        self.implement()
        self.assertEqual(self.gh.comments[-1][1], body)

    def test_a_timeout_on_a_fallback_model_keeps_the_work_too(self):
        cfg = config.parse_config(dict(DATA, roles={'implementer': 'agy', 'reviewers': ['codex']}))
        self.make(unavailable(), partial_work, cfg=cfg)
        self.implement()
        body = self.gh.comments[-1][1]
        self.assertIn('the last model tried for `agy` failed', body)
        self.assertIn(f'evidenza in `{EVIDENCE}`', body)
        self.assertTrue((self.logs / EVIDENCE).is_file())

    def test_a_failure_other_than_a_timeout_keeps_nothing(self):
        def crash(path):
            partial_work(path)
            return AgentError('claude exited with 1')
        self.make(crash)
        self.implement()
        body = self.gh.comments[-1][1]
        self.assertNotIn('non committate', body)
        self.assertFalse(list(self.logs.glob('timeout-*')) if self.logs.exists() else [])

    def test_a_fix_keeps_its_work_for_the_retry_of_the_same_round(self):
        self.git('checkout', '-q', '-b', 'agent/issue-7')
        (self.repo / 'a.py').write_text('a\npr\n')
        head = self.commit('pr')
        self.git('checkout', '-q', 'main')
        seen = {}

        def retry(path):
            seen['a'] = (path / 'a.py').read_text()
            return fixed()
        self.make(lambda path: ((path / 'a.py').write_text('a\npr\nfix\n'), timed_out())[1],
                  retry)
        agent = CFG.agent('claude')
        path = self.ws.existing_branch('pr-9', 'agent/issue-7')
        with self.assertRaises(AgentError) as ctx:
            self.d._write_turn(agent, 'fix it', 'fix', path, 'pr-9-fix-r1')
        self.assertEqual(ctx.exception.kept_work['file'], 'timeout-pr-9-fix-r1.diff')
        self.assertEqual(ctx.exception.kept_work['stats'], [('a.py', 1, 0)])
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=path), head)
        path = self.ws.existing_branch('pr-9', 'agent/issue-7')
        with mock.patch.object(self.ws, 'run_tests', return_value=(True, 'ok')):
            self.d._write_turn(agent, 'fix it', 'fix', path, 'pr-9-fix-r1')
        self.assertEqual(seen['a'], 'a\npr\nfix\n')
        self.assertIn('They have been reapplied to it', self.runner.calls[1][3])

    def test_a_sync_keeps_nothing(self):
        self.make(partial_work)
        path = self.ws.new_branch('pr-9', 'agent/issue-7')
        with self.assertRaises(AgentError) as ctx:
            self.d._write_turn(CFG.agent('claude'), 'sync', 'sync', path, 'pr-9-sync')
        self.assertIsNone(getattr(ctx.exception, 'kept_work', None))
        self.assertFalse(list(self.logs.glob('timeout-*')) if self.logs.exists() else [])


class NoteTests(unittest.TestCase):
    def test_a_patch_too_long_for_the_prompt_is_cut_and_fenced_apart(self):
        patch = 'diff --git a/x b/x\n+```\n' + 'x' * 200_000
        note = kept_work_note(60, [('x', 1, 0)], False, 'patch does not apply', patch)
        self.assertIn('[… cut: the patch has', note)
        self.assertLess(len(note), 110_000)
        self.assertIn('````diff\n', note)
        self.assertTrue(note.rstrip().endswith('them.'))


class RunnerTests(unittest.TestCase):
    def test_only_a_timeout_is_marked_timed_out(self):
        def timeout(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 1)

        def exits(argv, **kw):
            return SimpleNamespace(returncode=1, stdout='', stderr='boom')
        with tempfile.TemporaryDirectory() as tmp:
            for run, expected in ((timeout, True), (exits, False)):
                with self.subTest(expected=expected), self.assertRaises(AgentError) as ctx:
                    agents.AgentRunner(tmp, 1, run=run).run(
                        CFG.agent('codex'), 'write', 'p', 'implement', tmp, 'x')
                self.assertIs(ctx.exception.timed_out, expected)


if __name__ == '__main__':
    unittest.main()
