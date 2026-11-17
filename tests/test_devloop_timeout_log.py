"""devloop: the log and the escalation of an agent turn that does not end on its own (#215)."""

import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from devloop import agents
from devloop.dispatcher import Action, Dispatcher, Snapshot
from devloop.workspace import run_in_group
from helpers.devloop_fakes import FakeGH
from test_devloop import CFG, FakeWS

AGENT = SimpleNamespace(key='x', kind='claude', model='m', effort='e', write_args=(),
                        review_args=())
TOKEN = 'ghp_' + 'A1b2C3d4E5f6G7h8I9j0K1l2'
# An absolute path, anywhere in a comment line: a slash at the start of the line or after a
# character that cannot end a word, a URL or a relative path.
ABSOLUTE = re.compile(r'(?:^|[\s\'"`(=\[{<,;:])/[^\s/]', re.M)


def _wait_gone(test, pid):
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    os.kill(pid, 9)
    test.fail(f'process {pid} outlived the timeout')


def code_block(body):
    """The text of the last ```text block of a comment."""
    return body.rsplit('```text\n', 1)[1].split('\n```', 1)[0]


class RunnerLogTests(unittest.TestCase):
    def run_script(self, tmp, script, timeout=1, env=None):
        """Run ``script`` with bash as the turn of a claude-like agent; return the error."""
        runner = agents.AgentRunner(Path(tmp) / 'logs', timeout, env=env)
        with mock.patch.object(agents, 'build_command',
                               return_value=(['bash', '-c', script], None)), \
                self.assertRaisesRegex(agents.AgentError, 'timed out after') as ctx:
            runner.run(AGENT, 'write', 'p', 'implement', tmp, 'issue-7-implement-1')
        return ctx.exception

    def test_a_timeout_keeps_the_output_written_until_then(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = ('for i in $(seq 1 15); do echo "step $i"; done; '
                      f'echo "editing {tmp}/devloop/agents.py"; echo "warn: slow" >&2; sleep 60')
            exc = self.run_script(tmp, script)
            log = Path(exc.log)
            self.assertTrue(log.is_file(), 'the log the escalation cites must exist')
            self.assertEqual(log.name.rsplit('-', 1)[-1], 'x.log')
            text = log.read_text(encoding='utf-8')
            self.assertIn('x timed out after 1s', text)
            self.assertIn('step 1\n', text)
            self.assertIn('step 15\n', text)
            self.assertIn(f'{tmp}/devloop/agents.py', text)  # the local log is complete
            self.assertIn('warn: slow', text)
            tail = exc.tail.splitlines()
            self.assertEqual(len(tail), agents.TAIL_LINES)
            self.assertEqual(tail[0], 'step 8')
            self.assertEqual(tail[-2:], ['editing ./devloop/agents.py', 'warn: slow'])
            self.assertNotIn(tmp, exc.tail)

    def test_a_timeout_with_an_escaped_descendant_keeps_the_output(self):
        # A child in its own session keeps the pipes open after the group is killed: the
        # output read until then is not given up with them.
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / 'pid'
            exc = self.run_script(tmp, f'echo "running the tests"; setsid sleep 60 & '
                                       f'echo $! > {pid_file}; sleep 60')
            try:
                os.kill(int(pid_file.read_text()), 9)
            except (ProcessLookupError, ValueError):
                pass
            self.assertIn('running the tests', Path(exc.log).read_text(encoding='utf-8'))
            self.assertEqual(exc.tail, 'running the tests')

    def test_a_timeout_without_output_still_writes_the_log(self):
        def fake_run(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 1)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(agents.AgentError) as ctx:
                agents.AgentRunner(tmp, 1, run=fake_run).run(
                    CFG.agent('codex'), 'write', 'p', 'implement', tmp, 'x')
            self.assertTrue(Path(ctx.exception.log).is_file())
            self.assertIn('codex timed out after 1s', Path(ctx.exception.log).read_text())
            self.assertEqual(ctx.exception.tail, '')

    def test_a_timeout_reported_in_bytes_is_decoded(self):
        def fake_run(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 1, output=b'partial \xc3\xa8\r\n', stderr=None)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(agents.AgentError) as ctx:
                agents.AgentRunner(tmp, 1, run=fake_run).run(
                    CFG.agent('codex'), 'write', 'p', 'implement', tmp, 'x')
            self.assertIn('partial è\n', Path(ctx.exception.log).read_text(encoding='utf-8'))
            self.assertEqual(ctx.exception.tail, 'partial è')

    def test_a_stalled_turn_keeps_its_output(self):
        class Stall:
            interval = 0.2

            def turn(self, agent, workdir):
                def watch(pid):
                    raise agents.AgentError('x stalled')
                return watch
        with tempfile.TemporaryDirectory() as tmp:
            runner = agents.AgentRunner(Path(tmp) / 'logs', 30, stall=Stall())
            with mock.patch.object(agents, 'build_command',
                                   return_value=(['bash', '-c', 'echo before; sleep 60'], None)), \
                    self.assertRaisesRegex(agents.AgentError, 'stalled') as ctx:
                runner.run(AGENT, 'write', 'p', 'implement', tmp, 'label')
            text = Path(ctx.exception.log).read_text(encoding='utf-8')
            self.assertIn('stopped: x stalled', text)
            self.assertIn('before', text)

    def test_a_turn_that_ends_writes_the_log_as_before(self):
        def fake_run(argv, **kw):
            if argv[-1] == '--version':
                return SimpleNamespace(returncode=0, stdout='2.1.0', stderr='')
            return SimpleNamespace(returncode=3, stdout='out', stderr='err')
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(agents.AgentError, 'exited with 3') as ctx:
                agents.AgentRunner(tmp, 10, run=fake_run).run(
                    CFG.agent('claude'), 'write', 'p', 'implement', tmp, 'label')
            self.assertEqual(Path(ctx.exception.log).read_text(encoding='utf-8'),
                             '$ claude (write)\nexit 3\n--- stdout\nout\n--- stderr\nerr')
            self.assertEqual(ctx.exception.tail, '')  # only a stopped turn has one

    def test_run_in_group_keeps_the_partial_output_of_a_watch_failure(self):
        def watch(pid):
            raise RuntimeError('stop')
        with self.assertRaises(RuntimeError) as ctx:
            run_in_group(['bash', '-c', 'echo out; echo err >&2; sleep 60'], timeout=30,
                         watch=watch, watch_interval=0.5)
        self.assertEqual(ctx.exception.partial_output, ('out\n', 'err\n'))


class OutputTailTests(unittest.TestCase):
    def test_at_most_ten_lines_cut_to_width(self):
        out = '\n'.join(f'line {i}' for i in range(30)) + '\n\n   \n'
        self.assertEqual(agents.output_tail(out, '', env={}).splitlines(),
                         [f'line {i}' for i in range(20, 30)])
        long = agents.output_tail('x' * 1000, '', env={})
        self.assertEqual(len(long), agents.TAIL_WIDTH)
        self.assertTrue(long.endswith('…'))
        self.assertEqual(agents.output_tail('', None, env={}), '')

    def test_no_absolute_paths(self):
        out = '\n'.join([
            'cd /home/devloop/ssdlc/.ssdlc/devloop/worktrees/issue-215 && python3 -m unittest',
            'File "/usr/lib/python3.12/subprocess.py", line 1209, in communicate',
            'PATH=/usr/bin:/home/op/.local/bin --log=/var/log/x.log (/tmp/abc/)',
            'see https://github.com/o/r/blob/main/devloop/agents.py and tests/test_x.py',
            'relative ./devloop/agents.py, home ~/.codex/config.toml, ratio 3/4',
        ])
        tail = agents.output_tail(out, '', '/home/devloop/ssdlc/.ssdlc/devloop/worktrees/issue-215',
                                  env={})
        self.assertIsNone(ABSOLUTE.search(tail), tail)
        lines = tail.splitlines()
        self.assertEqual(lines[0], 'cd . && python3 -m unittest')
        self.assertEqual(lines[1], 'File "…/subprocess.py", line 1209, in communicate')
        self.assertEqual(lines[2], 'PATH=…/bin:…/bin --log=…/x.log (…/abc)')
        self.assertEqual(lines[3], out.splitlines()[3])  # URLs and relative paths stay
        self.assertEqual(lines[4], out.splitlines()[4])

    def test_the_worktree_becomes_relative(self):
        tail = agents.output_tail('edit /wt/agent/issue-7/devloop/x.py\n/wt/agent/issue-7-other/y',
                                  '', '/wt/agent/issue-7', env={})
        self.assertEqual(tail, 'edit ./devloop/x.py\n…/y')

    def test_no_secrets(self):
        env = {'MY_API_KEY': 'supersecretvalue123', 'HOME': '/home/op', 'SHORT_TOKEN': 'abc'}
        out = (f'token {TOKEN}\nkey supersecretvalue123\nAuthorization: Bearer '
               + 'z' * 30 + '\n-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n'
               '-----END OPENSSH PRIVATE KEY-----\nsk-' + 'q' * 24 + ' abc')
        tail = agents.output_tail(out, '', env=env)
        for secret in (TOKEN, 'supersecretvalue123', 'z' * 30, 'AAAA', 'q' * 24):
            self.assertNotIn(secret, tail)
        self.assertIn('[REDACTED]', tail)
        self.assertIn('abc', tail)  # values too short to be a secret stay


class EscalationTests(unittest.TestCase):
    def test_the_escalation_of_a_timeout_cites_the_log_and_quotes_the_tail(self):
        lines = [f'step {i}: /wt/issue-7/tests/test_{i}.py' for i in range(1, 16)]  # the worktree
        lines += ['File "/usr/lib/python3/x.py", line 3', f'push with {TOKEN}']

        def fake_run(argv, **kw):
            if argv[-1] == '--version':
                return SimpleNamespace(returncode=0, stdout='2.1.0 (Claude Code)', stderr='')
            raise subprocess.TimeoutExpired(argv, 5, output='\n'.join(lines[:-1]) + '\n',
                                            stderr=lines[-1] + '\n')
        with tempfile.TemporaryDirectory() as tmp:
            runner = agents.AgentRunner(tmp, 5, run=fake_run)
            gh = FakeGH()
            d = Dispatcher(CFG, gh, FakeWS(), runner, log=lambda m: None)
            d.snapshot = lambda: Snapshot([], [{'number': 7, 'title': 't',
                                                'labels': {'agent:ready'},
                                                'labeler': 'brushtyler', 'admitted': True}], [])
            self.assertEqual(d.tick(), [Action('implement', 7)])
            (log,) = Path(tmp).glob('*-issue-7-implement*.log')
            self.assertIn('step 1: /wt/issue-7/tests/test_1.py', log.read_text())
        self.assertEqual(gh.labels[7], {'agent:needs-human'})
        body = gh.comments[-1][1]
        self.assertIn('Il passo `implement` è fallito: claude timed out after 5s.', body)
        self.assertIn(f'Log locale: `{log.name}` in `{CFG.state_dir}/logs`.', body)
        self.assertIn('Le ultime righe dell\'output', body)
        quoted = code_block(body).splitlines()
        self.assertEqual(len(quoted), agents.TAIL_LINES)
        self.assertEqual(quoted[0], 'step 8: ./tests/test_8.py')
        self.assertEqual(quoted[-2:], ['File "…/x.py", line 3', 'push with [REDACTED]'])
        self.assertNotIn('step 7:', body)
        self.assertNotIn(TOKEN, body)
        self.assertNotIn('/wt/', body)
        self.assertNotIn('/usr/lib', body)

    def test_an_escalation_without_output_has_no_block(self):
        def fake_run(argv, **kw):
            if argv[-1] == '--version':
                return SimpleNamespace(returncode=0, stdout='2.1.0 (Claude Code)', stderr='')
            raise subprocess.TimeoutExpired(argv, 5)
        with tempfile.TemporaryDirectory() as tmp:
            gh = FakeGH()
            d = Dispatcher(CFG, gh, FakeWS(), agents.AgentRunner(tmp, 5, run=fake_run),
                           log=lambda m: None)
            d.snapshot = lambda: Snapshot([], [{'number': 7, 'title': 't',
                                                'labels': {'agent:ready'},
                                                'labeler': 'brushtyler', 'admitted': True}], [])
            d.tick()
            (log,) = Path(tmp).glob('*-issue-7-implement*.log')
        body = gh.comments[-1][1]
        self.assertIn(f'Log locale: `{log.name}`', body)
        self.assertNotIn('```text', body)
        self.assertNotIn('ultime righe', body)


if __name__ == '__main__':
    unittest.main()
