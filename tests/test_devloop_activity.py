"""devloop: the running action in `status` and `devloop follow` (#86)."""

import fcntl
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from devloop import activity, agents, follow
from devloop.dispatcher import Action, Dispatcher, Snapshot
from test_devloop import CFG, IMPL, FakeGH, FakeRunner, FakeWS, implemented, pr, raw_review, ready


class ActivityRecordTests(unittest.TestCase):
    def test_start_turn_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = activity.Activity(tmp, clock=lambda: 0, pid=4242)
            current.turn('claude', '/ignored')  # no action running: nothing recorded
            self.assertIsNone(activity.read(tmp))
            current.start('implement', 52)
            self.assertEqual(activity.read(tmp), {
                'kind': 'implement', 'number': 52, 'agent': None, 'prompt': None,
                'started_at': '1970-01-01T00:00:00Z', 'pid': 4242})
            current.turn('claude', Path(tmp) / 'logs' / 'p.prompt.md')
            record = activity.read(tmp)
            self.assertEqual((record['agent'], record['prompt'], record['started_at']),
                             ('claude', str(Path(tmp) / 'logs' / 'p.prompt.md'),
                              '1970-01-01T00:00:00Z'))
            current.clear()
            self.assertIsNone(activity.read(tmp))
            self.assertEqual(os.listdir(tmp), [])  # no temporary file left behind

    def test_action_is_removed_on_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = activity.Activity(tmp)
            with self.assertRaises(KeyError), current.action('review', 74):
                self.assertEqual(activity.read(tmp)['kind'], 'review')
                raise KeyError('boom')
            self.assertIsNone(activity.read(tmp))

    def test_runner_reports_each_turn_with_its_prompt(self):
        turns = []

        def fake_run(argv, **kw):
            out = {'is_error': False, 'structured_output': raw_review()}
            return SimpleNamespace(returncode=0, stdout=json.dumps(out), stderr='')
        with tempfile.TemporaryDirectory() as tmp:
            runner = agents.AgentRunner(tmp, 10, run=fake_run,
                                        on_turn=lambda agent, path: turns.append(
                                            (agent.key, path, path.read_text())))
            runner.run(CFG.agent('claude'), 'review', 'the prompt', 'review', tmp, 'pr-1')
        ((key, path, text),) = turns
        self.assertEqual((key, text), ('claude', 'the prompt'))
        self.assertTrue(path.name.endswith('-pr-1-claude.prompt.md'))


class RecordingRunner(FakeRunner):
    """Captures the recorded action while the agent turn runs."""

    def __init__(self, current, state, *outputs):
        super().__init__(*outputs)
        self.current, self.state, self.seen = current, state, []

    def run(self, agent, mode, prompt, schema_name, workdir, label):
        self.current.turn(agent.key, f'/logs/{label}.prompt.md')
        self.seen.append(activity.read(self.state))
        return super().run(agent, mode, prompt, schema_name, workdir, label)


class TickActivityTests(unittest.TestCase):
    def make(self, tmp, *outputs):
        current = activity.Activity(tmp)
        runner = RecordingRunner(current, tmp, *outputs)
        self.gh = FakeGH()
        d = Dispatcher(CFG, self.gh, FakeWS(), runner, log=lambda m: None, activity=current)
        return d, runner

    def test_tick_records_the_running_action_and_removes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            d, runner = self.make(tmp, implemented())
            self.gh.labels[7] = {'agent:ready'}
            d.snapshot = lambda: Snapshot([], [ready(7)], [])
            self.assertEqual(d.tick(), [Action('implement', 7)])
            (seen,) = runner.seen
            self.assertEqual((seen['kind'], seen['number'], seen['agent'], seen['prompt'],
                              seen['pid']),
                             ('implement', 7, IMPL, '/logs/issue-7-implement-1.prompt.md',
                              os.getpid()))
            self.assertIsNone(activity.read(tmp))

    def test_record_is_removed_when_the_action_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            d, runner = self.make(tmp, agents.AgentError('claude exited with 1'))
            p = pr()
            d.snapshot = lambda: Snapshot([p], [], [])
            d.tick()  # escalated
            self.assertEqual(runner.seen[0]['kind'], 'review')
            self.assertIsNone(activity.read(tmp))
            d, runner = self.make(tmp, RuntimeError('unexpected'))
            d.snapshot = lambda: Snapshot([p], [], [])
            with self.assertRaises(RuntimeError):
                d.tick()  # not escalated, but still cleaned up
            self.assertIsNone(activity.read(tmp))

    def test_dry_run_records_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            d, _ = self.make(tmp)
            d.activity.start = mock.Mock(side_effect=AssertionError('recorded'))
            d.snapshot = lambda: Snapshot([pr()], [], [])
            d.tick(dry_run=True)
            self.assertIsNone(activity.read(tmp))


class StatusDescriptionTests(unittest.TestCase):
    def record(self, tmp, **extra):
        current = activity.Activity(tmp, clock=lambda: 1_000_000, pid=os.getpid())
        current.start('implement', 52)
        current.turn('claude', '/state/logs/x.prompt.md')
        for key, value in extra.items():
            current._record[key] = value
        current._write()

    def test_nothing_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(activity.describe(tmp))

    def test_running_while_the_dispatcher_holds_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.record(tmp)
            with open(Path(tmp) / activity.LOCK, 'w') as fh:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                line = activity.describe(tmp, now=1_000_000 + 3723)
                # The probe never takes the lock away from the dispatcher.
                with open(Path(tmp) / activity.LOCK, 'rb') as other, \
                        self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(line, 'running: implement #52 with claude for 1h02m03s '
                                   '(since 1970-01-12T13:46:40Z, prompt /state/logs/x.prompt.md)')

    def test_interrupted_without_lock_or_live_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.record(tmp)
            # No lock file at all, then a free lock.
            for _ in range(2):
                line = activity.describe(tmp, now=1_000_000 + 59)
                self.assertTrue(line.startswith('interrupted: implement #52 with claude, started '
                                                '1970-01-12T13:46:40Z (59s ago'), line)
                (Path(tmp) / activity.LOCK).touch()
            # The lock is held, by another dispatcher, but the recorded process is gone.
            line = activity.describe(tmp, now=1_000_000, held=lambda path: True,
                                     alive=lambda pid: False)
            self.assertTrue(line.startswith('interrupted:'), line)

    def test_unreadable_record_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / activity.FILE).write_text('{not json')
            self.assertIn('unreadable', activity.describe(tmp))

    def test_pid_and_duration_helpers(self):
        self.assertTrue(activity.pid_alive(os.getpid()))
        for pid in (None, 0, -1, True, '12'):
            self.assertFalse(activity.pid_alive(pid))
        self.assertEqual([activity.duration(s) for s in (-5, 42, 125, 7200)],
                         ['0s', '42s', '2m05s', '2h00m00s'])


def claude_line(ts, *content):
    return json.dumps({'type': 'assistant', 'timestamp': ts,
                       'message': {'content': list(content)}})


class FollowFormatTests(unittest.TestCase):
    def test_claude_records(self):
        record = json.loads(claude_line(
            '2026-11-15T10:11:12.345Z',
            {'type': 'text', 'text': 'Reading\nthe code'},
            {'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'ls -la'}},
            {'type': 'tool_use', 'name': 'Read', 'input': {'file_path': '/x.py'}},
            {'type': 'tool_use', 'name': 'Agent', 'input': {'prompt': 'p'}},
            {'type': 'thinking', 'thinking': 'hidden'}))
        self.assertEqual(follow.format_record(record), [
            '10:11:12 · Reading the code', '10:11:12 → Bash ls -la', '10:11:12 → Read /x.py',
            '10:11:12 → Agent '])
        self.assertEqual(follow.format_record({'type': 'user', 'message': {}}), [])

    def test_codex_records(self):
        call = {'type': 'response_item', 'timestamp': '2026-11-15T10:11:12Z',
                'payload': {'type': 'function_call', 'name': 'shell',
                            'arguments': '{"command":["ls"]}'}}
        custom = {'type': 'response_item', 'payload': {'type': 'custom_tool_call',
                                                        'name': 'apply_patch', 'input': {'a': 1}}}
        message = {'type': 'response_item', 'timestamp': '2026-11-15T10:11:13Z',
                   'payload': {'type': 'message', 'role': 'assistant',
                               'content': [{'type': 'output_text', 'text': 'Done'},
                                           {'type': 'output_text', 'text': 'here'}]}}
        user = {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                                                     'content': [{'text': 'x'}]}}
        self.assertEqual([follow.format_record(r) for r in (call, custom, message, user)], [
            ['10:11:12 → shell {"command":["ls"]}'], [' → apply_patch {"a":1}'],
            ['10:11:13 · Done here'], []])

    def test_output_is_cut_and_has_no_control_characters(self):
        record = json.loads(claude_line('2026-11-15T10:11:12Z',
                                        {'type': 'text', 'text': '\x1b[2Jx' + 'y' * 500},
                                        {'type': 'tool_use', 'name': 'Bash',
                                         'input': {'command': 'z' * 500}}))
        text, tool = follow.format_record(record)
        self.assertNotIn('\x1b', text)
        self.assertEqual(len(text), len('10:11:12 · ') + 200)
        self.assertEqual(len(tool), len('10:11:12 → Bash ') + 110)
        for junk in (None, [], 'x', {'type': 'assistant', 'message': 'x'},
                     {'type': 'response_item', 'payload': None}):
            self.assertEqual(follow.format_record(junk), [])


class FollowTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'home'
        self.wt = Path(self.tmp.name) / 'repo' / '.ssdlc' / 'devloop' / 'worktrees' / 'issue-52'
        self.wt.mkdir(parents=True)
        self.wt = Path(os.path.realpath(self.wt))

    def claude(self, name, lines, mtime):
        folder = self.home / '.claude' / 'projects' / str(self.wt).replace('/', '-').replace('.', '-')
        folder.mkdir(parents=True, exist_ok=True)
        return self.write(folder / name, lines, mtime)

    def codex(self, name, cwd, lines, mtime):
        folder = self.home / '.codex' / 'sessions' / '2026' / '11' / '15'
        folder.mkdir(parents=True, exist_ok=True)
        meta = json.dumps({'type': 'session_meta', 'payload': {'cwd': str(cwd)}})
        return self.write(folder / name, [meta, *lines], mtime)

    def write(self, path, lines, mtime):
        path.write_text(''.join(line + '\n' for line in lines))
        os.utime(path, (mtime, mtime))
        return path

    def test_finds_the_newest_transcript_of_the_worktree(self):
        self.assertIsNone(follow.find_transcript(self.wt, self.home))
        old = self.claude('old.jsonl', [], 100)
        self.assertEqual(follow.find_transcript(self.wt, self.home), old)
        new = self.claude('new.jsonl', [], 200)
        self.codex('other.jsonl', self.wt.parent / 'pr-74', [], 900)  # another worktree
        self.assertEqual(follow.find_transcript(self.wt, self.home), new)
        tie = self.codex('tie.jsonl', self.wt, [], 200)
        self.assertEqual(follow.find_transcript(self.wt, self.home), new)  # Claude wins a tie
        os.utime(tie, (300, 300))
        self.assertEqual(follow.find_transcript(self.wt, self.home), tie)

    def test_claude_project_directory_names(self):
        dirs = {d.name for d in follow.claude_dirs('/h', '/root/repos/my_repo/.ssdlc/wt/issue-1')}
        self.assertIn('-root-repos-my-repo--ssdlc-wt-issue-1', dirs)  # every non-alphanumeric
        self.assertIn('-root-repos-my_repo--ssdlc-wt-issue-1', dirs)  # what the shell script did

    def test_follows_appended_lines_and_partial_writes(self):
        path = self.claude('s.jsonl', [claude_line('2026-11-15T10:00:00Z',
                                                   {'type': 'text', 'text': 'first'}),
                                       'not json'], 100)
        out, err = io.StringIO(), io.StringIO()
        second = claude_line('2026-11-15T10:00:01Z', {'type': 'text', 'text': 'second'})
        steps = [lambda: self.append(path, second[:10]),
                 lambda: self.append(path, second[10:] + '\n'), lambda: None]

        def sleep(_):
            steps.pop(0)()
            if len(steps) == 1:
                self.assertEqual(out.getvalue(), '10:00:00 · first\n')  # partial line held back
        self.assertEqual(follow.follow(self.wt, self.home, out, err, sleep=sleep, polls=3), 0)
        self.assertEqual(out.getvalue(), '10:00:00 · first\n10:00:01 · second\n')
        self.assertEqual(err.getvalue(), f'following {path}\n')

    def append(self, path, text):
        with open(path, 'a') as fh:
            fh.write(text)

    def test_switches_to_a_newer_transcript_of_the_same_worktree(self):
        first = self.claude('a.jsonl', [claude_line('2026-11-15T10:00:00Z',
                                                    {'type': 'text', 'text': 'attempt 1'})], 100)
        out, err = io.StringIO(), io.StringIO()

        def sleep(_):
            if not err.getvalue().count('following') > 1:
                self.codex('b.jsonl', self.wt, [json.dumps(
                    {'type': 'response_item', 'timestamp': '2026-11-15T10:05:00Z',
                     'payload': {'type': 'message', 'role': 'assistant',
                                 'content': [{'text': 'attempt 2'}]}})], 200)
        follow.follow(self.wt, self.home, out, err, sleep=sleep, rescan_every=1, polls=3)
        self.assertEqual(out.getvalue(), '10:00:00 · attempt 1\n10:05:00 · attempt 2\n')
        self.assertIn(f'following {first}\n', err.getvalue())
        self.assertIn('b.jsonl\n', err.getvalue())

    def test_reopens_a_replaced_transcript(self):
        path = self.claude('s.jsonl', [claude_line('2026-11-15T10:00:00Z',
                                                   {'type': 'text', 'text': 'one'})], 100)
        out = io.StringIO()

        def sleep(_):
            path.unlink()
            self.write(path, [claude_line('2026-11-15T10:00:01Z',
                                          {'type': 'text', 'text': 'two'})], 100)
        follow.follow(self.wt, self.home, out, io.StringIO(), sleep=sleep, rescan_every=99,
                      polls=2)
        self.assertEqual(out.getvalue(), '10:00:00 · one\n10:00:01 · two\n')

    def test_no_session_found(self):
        err = io.StringIO()
        self.assertEqual(follow.follow(self.wt, self.home, io.StringIO(), err, polls=1), 1)
        self.assertIn('no Claude or Codex session found', err.getvalue())

    def test_follow_finds_the_session_of_a_codex_turn_run_by_devloop(self):
        # Codex review of #95: with --ephemeral, Codex saves no session and follow finds nothing.
        # The stand-in CLI saves its session like Codex does, unless told to be ephemeral.
        bin_dir = Path(self.tmp.name) / 'bin'
        bin_dir.mkdir()
        fake = bin_dir / 'codex'
        fake.write_text(FAKE_CODEX)
        fake.chmod(0o755)
        env = {**os.environ, 'HOME': str(self.home),
               'PATH': f'{bin_dir}{os.pathsep}{os.environ.get("PATH", "")}'}
        env.pop('CODEX_HOME', None)
        runner = agents.AgentRunner(Path(self.tmp.name) / 'logs', 60, env=env)
        output = runner.run(CFG.agent('codex'), 'write', 'p', 'implement', self.wt, 'issue-52')
        self.assertEqual(output['status'], 'completed')
        path = follow.find_transcript(self.wt, self.home)
        self.assertIsNotNone(path, 'the Codex turn left no session for devloop follow')
        out = io.StringIO()
        follow.follow(self.wt, self.home, out, io.StringIO(), polls=1)
        self.assertEqual(out.getvalue(), '10:00:00 · working on it\n')

    def test_follow_pr_shows_the_reviewer_working_in_the_review_copy(self):
        # Review of #95: the reviewer runs in pr-<N>-review, and `follow pr-<N>` found only the
        # fixer's older session, or nothing at all.
        import devloop.__main__ as cli
        from devloop.workspace import Workspace
        repo = Path(os.path.realpath(self.tmp.name)) / 'repo'

        def git(argv, **kw):
            return SimpleNamespace(returncode=0, stdout='0' * 40 + '\n', stderr='')
        ws = Workspace(repo, CFG.state_dir, 'main', run=git)
        pr_wt = ws.path('pr-95')
        pr_wt.mkdir(parents=True)
        self.codex('fixer.jsonl', pr_wt, [json.dumps(
            {'type': 'response_item', 'timestamp': '2026-11-15T09:00:00Z',
             'payload': {'type': 'message', 'role': 'assistant',
                         'content': [{'text': 'fixing'}]}})], 100)
        name, copy = ws.review_copy(pr_wt)  # where Dispatcher._do_review runs the reviewer
        copy.mkdir()
        bin_dir = Path(self.tmp.name) / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'codex').write_text(FAKE_CODEX)
        (bin_dir / 'codex').chmod(0o755)
        env = {**os.environ, 'HOME': str(self.home),
               'PATH': f'{bin_dir}{os.pathsep}{os.environ.get("PATH", "")}'}
        env.pop('CODEX_HOME', None)
        runner = agents.AgentRunner(Path(self.tmp.name) / 'logs', 60, env=env)
        runner.run(CFG.agent('codex'), 'write', 'p', 'implement', copy, name)

        def run(target, out):
            real = follow.follow
            with mock.patch.object(cli, 'main_checkout', return_value=repo), \
                    mock.patch.dict(os.environ, {'HOME': str(self.home)}), \
                    mock.patch.object(cli.follow, 'follow', side_effect=lambda wts: real(
                        wts, out=out, err=io.StringIO(), polls=1)), \
                    self.assertRaises(SystemExit) as ctx:
                cli.main(['follow', target])
            return ctx.exception.code
        out = io.StringIO()
        self.assertEqual(run('pr-95', out), 0)
        self.assertEqual(out.getvalue(), '10:00:00 · working on it\n')
        # The fixer's next session, newer than the review, is followed again.
        self.codex('fixer2.jsonl', pr_wt, [json.dumps(
            {'type': 'response_item', 'timestamp': '2026-11-15T11:00:00Z',
             'payload': {'type': 'message', 'role': 'assistant',
                         'content': [{'text': 'fixing again'}]}})], time.time() + 60)
        out = io.StringIO()
        self.assertEqual(run('pr-95', out), 0)
        self.assertEqual(out.getvalue(), '11:00:00 · fixing again\n')
        # The review copy belongs to the PR only.
        self.assertEqual(run('issue-95', io.StringIO()), 1)

    def test_worktrees_of_a_target(self):
        root = Path('/r/worktrees')
        self.assertEqual(follow.worktrees(root, 'issue-7'), [root / 'issue-7'])
        self.assertEqual(follow.worktrees(root, 'pr-7'), [root / 'pr-7', root / 'pr-7-review'])


FAKE_CODEX = '''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
sys.stdin.read()
if '--ephemeral' not in argv:
    folder = os.path.join(os.environ['HOME'], '.codex', 'sessions', '2026', '11', '15')
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, 'rollout-2026-11-15T10-00-00-x.jsonl'), 'w') as fh:
        fh.write(json.dumps({'type': 'session_meta', 'payload': {'cwd': os.getcwd()}}) + '\\n')
        fh.write(json.dumps({'type': 'response_item', 'timestamp': '2026-11-15T10:00:00Z',
                             'payload': {'type': 'message', 'role': 'assistant',
                                         'content': [{'text': 'working on it'}]}}) + '\\n')
with open(argv[argv.index('-o') + 1], 'w') as fh:
    json.dump({'status': 'completed', 'summary': 's', 'pr_title': 't', 'commit_message': 'c',
               'pr_body': 'b', 'blocked_reason': ''}, fh)
'''


class FollowCommandTests(unittest.TestCase):
    def test_follow_reads_the_worktree_without_github(self):
        import devloop.__main__ as cli
        with mock.patch.object(cli, 'main_checkout', return_value=Path('/repo')), \
                mock.patch.object(cli.follow, 'follow', return_value=0) as run, \
                mock.patch.object(cli, 'build', side_effect=AssertionError('needs GitHub')), \
                self.assertRaises(SystemExit) as ctx:
            cli.main(['follow', 'pr-74'])
        self.assertEqual(ctx.exception.code, 0)
        root = Path('/repo') / CFG.state_dir / 'worktrees'
        run.assert_called_once_with([root / 'pr-74', root / 'pr-74-review'])

    def test_follow_refuses_other_names(self):
        import devloop.__main__ as cli
        for target in ('../../etc', 'issue-', 'issue-5/..', 'cleanup', '52'):
            with self.subTest(target=target), \
                    mock.patch.object(cli.follow, 'follow') as run, \
                    self.assertRaises(SystemExit) as ctx:
                cli.main(['follow', target])
            self.assertIn('issue-<N> or pr-<N>', str(ctx.exception.code))
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
