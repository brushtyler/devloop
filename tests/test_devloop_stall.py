"""devloop: stalled agent turns in the tick log and in `status` (#100)."""

import dataclasses
import json
import os
import shlex
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from devloop import activity, agents, config, follow, stall
from devloop.dispatcher import Action, Dispatcher, Snapshot
from test_devloop import CFG, IMPL, FakeGH, FakeWS, implemented, ready

HAS_PROC = Path('/proc/self/stat').exists()


class StallConfigTests(unittest.TestCase):
    def test_keys_are_validated_with_warn_by_default(self):
        data = tomllib.loads(config.DEFAULT_CONFIG.read_text())
        self.assertEqual((CFG.agent_stall_seconds, CFG.agent_stall_action), (900, 'warn'))
        omitted = {k: v for k, v in data.items()
                   if k not in ('agent_stall_seconds', 'agent_stall_action')}
        cfg = config.parse_config(omitted)
        self.assertEqual((cfg.agent_stall_seconds, cfg.agent_stall_action), (900, 'warn'))
        cfg = config.parse_config(dict(data, agent_stall_seconds=60, agent_stall_action='abort'))
        self.assertEqual((cfg.agent_stall_seconds, cfg.agent_stall_action), (60, 'abort'))
        for key, value in (('agent_stall_seconds', 0), ('agent_stall_seconds', '900'),
                           ('agent_stall_seconds', True), ('agent_stall_action', 'kill'),
                           ('agent_stall_action', 1)):
            with self.subTest(key=key, value=value), self.assertRaises(config.ConfigError):
                config.parse_config(dict(data, **{key: value}))


class FakeProc:
    """A /proc directory: ``{pid: (ppid, session, utime, stime, cutime, cstime)}``."""

    def __init__(self, root, table):
        self.root = Path(root)
        for pid, (ppid, session, *times) in table.items():
            (self.root / str(pid)).mkdir()
            fields = ['S', ppid, pid, session] + [0] * 7 + times + [0] * 5
            (self.root / str(pid) / 'stat').write_text(
                f'{pid} (a (weird) name) ' + ' '.join(map(str, fields)) + '\n')
        (self.root / 'self').mkdir()


class CpuTicksTests(unittest.TestCase):
    def test_descendants_and_session_members_without_the_cli_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            FakeProc(tmp, {
                10: (1, 10, 500, 500, 7, 3),   # the CLI: only its reaped children count
                11: (10, 10, 1, 2, 3, 4),      # its Bash tool
                12: (11, 12, 5, 0, 0, 0),      # a descendant in its own session
                13: (1, 10, 6, 0, 0, 0),       # an orphan of the session, reparented to init
                20: (1, 20, 99, 0, 0, 0),      # unrelated
            })
            self.assertEqual(stall.cpu_ticks(10, Path(tmp)), {10: 10, 11: 10, 12: 5, 13: 6})
            self.assertIsNone(stall.cpu_ticks(10, Path(tmp) / 'missing'))

    @unittest.skipUnless(HAS_PROC, 'needs /proc')
    def test_reads_the_real_proc(self):
        ticks = stall.cpu_ticks(os.getpid())
        self.assertIn(os.getpid(), ticks)


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class StallWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'home'
        self.wt = Path(os.path.realpath(self.tmp.name)) / 'worktrees' / 'issue-52'
        self.wt.mkdir(parents=True)
        self.state = Path(self.tmp.name) / 'state'
        self.clock, self.cpu, self.logs = Clock(), {1: {1: 0}}, []
        self.current = activity.Activity(self.state, clock=self.clock, pid=os.getpid())
        self.current.start('implement', 52)
        self.current.turn('claude', '/state/logs/x.prompt.md')

    def watch(self, action='warn'):
        return stall.StallWatch(100, action, log=self.logs.append, activity=self.current,
                                home=self.home, clock=self.clock,
                                cpu=lambda pid: dict(self.cpu[pid]), interval=10)

    def transcript(self, lines, mtime):
        folder = follow.claude_dirs(self.home, self.wt)[0]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 's.jsonl'
        path.write_text(''.join(json.dumps(line) + '\n' for line in lines))
        os.utime(path, (mtime, mtime))
        return path

    def step(self, check, seconds, ticks=0):
        self.clock.now += seconds
        self.cpu[1][1] = self.cpu[1].get(1, 0) + ticks
        check(1)

    def status(self):
        return activity.describe(self.state, now=self.clock.now, held=lambda path: True)

    def test_no_events_nor_cpu_is_reported_once_with_the_command(self):
        call = {'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': 'sleep 3600'}}]}}
        self.transcript([call], self.clock.now + 5)
        check = self.watch().turn('claude', self.wt)
        for _ in range(10):
            self.step(check, 10)  # 100s since the last event: 105s after the start
        self.assertEqual(self.logs, [])
        self.step(check, 10)
        self.assertEqual(self.logs, [
            '  claude in issue-52 stalled: no transcript event nor child process using CPU for '
            '1m45s, command: Bash sleep 3600; not stopped (agent_stall_action = "warn")'])
        self.assertEqual(activity.read(self.state)['stalled'],
                         {'since': '1970-01-12T13:46:45Z', 'command': 'Bash sleep 3600'})
        self.step(check, 10)
        self.assertEqual(len(self.logs), 1)  # reported once
        self.assertEqual(self.status(),
                         'stalled: implement #52 with claude, no agent activity for 1m55s (since '
                         '1970-01-12T13:46:45Z, command: Bash sleep 3600); running for 2m00s '
                         '(since 1970-01-12T13:46:40Z, prompt /state/logs/x.prompt.md)')

    def test_transcript_events_keep_the_turn_active(self):
        check = self.watch('abort').turn('claude', self.wt)
        for _ in range(30):
            self.transcript([{'type': 'assistant'}], self.clock.now + 5)
            self.step(check, 10)
        self.assertEqual(self.logs, [])
        self.assertNotIn('stalled', activity.read(self.state))
        self.assertTrue(self.status().startswith('running:'), self.status())

    def test_a_child_using_cpu_keeps_the_turn_active(self):
        # A long test suite writes nothing to the transcript, but its processes use CPU.
        check = self.watch('abort').turn('claude', self.wt)
        for _ in range(30):
            self.step(check, 10, ticks=500)
        self.assertEqual(self.logs, [])
        # A wait loop wakes up now and then, for much less than 1% of the time.
        for _ in range(8):
            self.step(check, 10, ticks=0)
        self.step(check, 10, ticks=1)  # 90s without activity
        self.assertEqual(self.logs, [])
        with self.assertRaisesRegex(stall.AgentStalled, 'stopped by agent_stall_action = "abort"'):
            self.step(check, 10, ticks=0)
        self.assertIn('stopping it', self.logs[0])

    def test_warn_goes_on_and_reports_the_recovery(self):
        check = self.watch().turn('claude', self.wt)
        for _ in range(11):
            self.step(check, 10)
        self.assertIn('stalled', activity.read(self.state))
        self.step(check, 10, ticks=500)
        self.assertEqual(self.logs[-1], '  claude in issue-52 is active again')
        self.assertNotIn('stalled', activity.read(self.state))
        # A new turn starts without the mark.
        self.current.stalled(self.clock.now)
        self.current.turn('claude', '/state/logs/y.prompt.md')
        self.assertNotIn('stalled', activity.read(self.state))

    def test_check_is_off_without_cpu_information(self):
        watch = stall.StallWatch(1, 'abort', log=self.logs.append, home=self.home,
                                 clock=self.clock, cpu=lambda pid: None, interval=1)
        check = watch.turn('claude', self.wt)
        for _ in range(5):
            self.step(check, 10)
        self.assertEqual(len(self.logs), 1)
        self.assertIn('stall check off', self.logs[0])

    def test_pending_call_of_each_cli(self):
        claude = [
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'id': 'a', 'name': 'Bash', 'input': {'command': 'ls'}}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'a'}]}},
        ]
        path = self.transcript(claude, self.clock.now)
        self.assertIsNone(follow.pending_call(path))
        path = self.transcript(claude[:1], self.clock.now)
        self.assertEqual(follow.pending_call(path), 'Bash ls')
        codex = [{'type': 'response_item', 'payload': {
                     'type': 'function_call', 'call_id': 'c', 'name': 'shell',
                     'arguments': '{"command":["pytest"]}'}},
                 {'type': 'response_item', 'payload': {
                     'type': 'function_call', 'call_id': 'd', 'name': 'shell',
                     'arguments': '{"command":["sleep","9"]}'}},
                 {'type': 'response_item', 'payload': {
                     'type': 'function_call_output', 'call_id': 'd'}}]
        path = self.transcript(codex, self.clock.now)
        self.assertEqual(follow.pending_call(path), 'shell {"command":["pytest"]}')
        self.assertIsNone(follow.pending_call(Path(self.tmp.name) / 'missing.jsonl'))


class SlowWS(FakeWS):
    def __init__(self, root):
        super().__init__()
        self.root = Path(root)

    def new_branch(self, name, branch):
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path


@unittest.skipUnless(HAS_PROC, 'needs /proc')
class StalledTickTests(unittest.TestCase):
    """A tick with a real agent process, a stand-in for the CLI."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.state = self.root / 'state'
        self.home = self.root / 'home'
        self.out = self.root / 'out.json'
        self.out.write_text(json.dumps({'is_error': False, 'structured_output': implemented()}))
        self.seen = self.root / 'seen.json'
        self.logs = []

    def tick(self, script, action='warn'):
        """One tick that implements issue #5 with ``script`` as the agent CLI."""
        cfg = dataclasses.replace(CFG, agent_stall_action=action)
        current = activity.Activity(self.state)
        watch = stall.StallWatch(0.5, cfg.agent_stall_action, log=self.logs.append,
                                 activity=current, home=self.home, interval=0.1)
        runner = agents.AgentRunner(self.state / 'logs', 60, stall=watch,
                                    on_turn=lambda agent, path: current.turn(agent.key, path))
        self.gh = FakeGH()
        d = Dispatcher(cfg, self.gh, SlowWS(self.root / 'worktrees'), runner,
                       log=self.logs.append, activity=current)
        d.snapshot = lambda: Snapshot([], [ready(5)], [])
        with mock.patch.object(agents, 'build_command', return_value=(['bash', '-c', script], None)), \
                mock.patch.object(runner, 'version', return_value='9.9'):
            started = time.monotonic()
            self.assertEqual(d.tick(), [Action('implement', 5)])
        return time.monotonic() - started

    def finish(self):
        """Shell code that saves the activity record as `status` reads it, then answers."""
        return (f'cp {shlex.quote(str(self.state / activity.FILE))} {shlex.quote(str(self.seen))};'
                f' cat {shlex.quote(str(self.out))}')

    def test_silent_agent_is_reported_in_the_log_and_in_status_without_interruption(self):
        self.tick(f'sleep 1.5; {self.finish()}')
        stalled = [line for line in self.logs if 'stalled' in line]
        self.assertEqual(len(stalled), 1, self.logs)
        self.assertIn(f'{IMPL} in issue-5 stalled: no transcript event nor child process using '
                      'CPU', stalled[0])
        self.assertIn('not stopped (agent_stall_action = "warn")', stalled[0])
        self.assertEqual(self.gh.created[0]['head'], 'agent/issue-5')  # the turn went on
        status = self.state / 'status'
        status.mkdir()
        (status / activity.FILE).write_bytes(self.seen.read_bytes())
        line = activity.describe(status, held=lambda path: True)
        self.assertTrue(line.startswith(f'stalled: implement #5 with {IMPL}, no agent activity '
                                        'for '), line)

    def test_agent_writing_events_is_not_reported(self):
        folder = follow.claude_dirs(self.home, self.root / 'worktrees' / 'issue-5')[0]
        folder.mkdir(parents=True)
        transcript = shlex.quote(str(folder / 's.jsonl'))
        self.tick(f'for i in $(seq 15); do echo "{{}}" >> {transcript}; sleep 0.1; done; '
                  f'{self.finish()}')
        self.assertEqual([line for line in self.logs if 'stalled' in line], [], self.logs)
        self.assertNotIn('stalled', json.loads(self.seen.read_text()))

    def test_agent_with_a_busy_child_is_not_reported(self):
        busy = 'import time\nt = time.time()\nwhile time.time() - t < 1.5: pass'
        self.tick(f'python3 -c {shlex.quote(busy)}; {self.finish()}')
        self.assertEqual([line for line in self.logs if 'stalled' in line], [], self.logs)
        self.assertEqual(self.gh.created[0]['head'], 'agent/issue-5')

    def test_abort_stops_the_agent_and_escalates_with_a_comment(self):
        pid_file = self.root / 'pid'
        elapsed = self.tick(f'sleep 60 & echo $! > {shlex.quote(str(pid_file))}; sleep 60; '
                            f'{self.finish()}', action='abort')
        self.assertLess(elapsed, 30)
        self.assertEqual(self.gh.created, [])
        self.assertEqual(self.gh.labels[5], {'agent:needs-human'})
        number, body = self.gh.comments[-1]
        self.assertEqual(number, 5)
        self.assertIn(f'{IMPL} in issue-5 stalled: no transcript event nor child process', body)
        self.assertIn('agent_stall_action = "abort"', body)
        pid = int(pid_file.read_text())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, 9)
            self.fail('the agent background child outlived the abort')
        self.assertIsNone(activity.read(self.state))


if __name__ == '__main__':
    unittest.main()
