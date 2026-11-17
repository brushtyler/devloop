"""devloop: the log and the escalation of an agent turn that does not end on its own (#215)."""

import json
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
from devloop.ledger import Ledger
from devloop.markdown import read_marker
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

    def test_short_known_secrets_are_redacted_as_words(self):
        env = {'DATABASE_PASSWORD': 'hunter2', 'KEYTIMEOUT': '1', 'HOME': '/home/op'}
        tail = agents.output_tail('connect with hunter2 now\nhunter22 and ran 1 test', '',
                                  env=env)
        self.assertEqual(tail, 'connect with [REDACTED] now\nhunter22 and ran 1 test')

    def test_values_assigned_to_secret_names_are_redacted(self):
        out = '\n'.join([
            'DATABASE_PASSWORD=pw1 export GH_TOKEN="t0k" api-key: k3y',
            '{"password": "abc", "input_tokens": 3}',
            '{"aggregated_output":"DB_SECRET=\\"s3c\\"\\n"}',
            "KeyError: 'name' in monkey: banana",
        ])
        tail = agents.output_tail(out, '', env={})
        for secret in ('pw1', 't0k', 'k3y', 'abc', 's3c'):
            self.assertNotIn(secret, tail)
        lines = tail.splitlines()
        self.assertEqual(lines[0], 'DATABASE_PASSWORD=[REDACTED] export GH_TOKEN="[REDACTED]" '
                                   'api-key: [REDACTED]')
        self.assertEqual(lines[1], '{"password": "[REDACTED]", "input_tokens": 3}')
        self.assertEqual(lines[3], out.splitlines()[3])

    def test_terminal_colours_do_not_hide_absolute_paths(self):
        raw = '\x1b[31m/home/operator/private/file.py\x1b[0m: \x1b[1;32mok\x1b[0m'
        self.assertEqual(agents.output_tail(raw, '', env={}), '…/file.py: ok')
        title = '\x1b]0;/home/operator/title\x07done'
        self.assertEqual(agents.output_tail(title, '', env={}), 'done')
        # As the JSON events of a CLI write them: escaped, with \n between the lines.
        event = ('{"aggregated_output":"\\u001b[31m/home/operator/private/file.py\\u001b[0m'
                 '\\n/etc/passwd\\n"}')
        tail = agents.output_tail(event, '', env={})
        self.assertEqual(tail, '{"aggregated_output":"…/file.py\\n…/passwd\\n"}')
        self.assertNotIn('/home/', tail)
        self.assertNotIn('/etc/', tail)


def codex_events(command, progress):
    """The ``--json`` stdout of a codex turn that started ``command`` and then wrote
    ``progress`` lines."""
    events = [{'type': 'thread.started', 'thread_id': 't'},
              {'type': 'item.started', 'item': {'id': 'i1', 'type': 'command_execution',
                                                'command': 'bash -lc ls', 'status': 'in_progress'}},
              {'type': 'item.completed', 'item': {'id': 'i1', 'type': 'command_execution',
                                                  'command': 'bash -lc ls', 'exit_code': 0}},
              {'type': 'item.started', 'item': {'id': 'i2', 'type': 'command_execution',
                                                'command': command, 'aggregated_output': '',
                                                'status': 'in_progress'}}]
    return '\n'.join([json.dumps(e) for e in events] + [f'progress {i}' for i in range(progress)])


class LastCommandTests(unittest.TestCase):
    def test_the_last_codex_command_is_cleaned(self):
        out = codex_events('bash -lc "cd /wt/issue-7 && DB_PASSWORD=pw python3 /opt/x/run.py"', 0)
        self.assertEqual(agents.public_command('codex', out, '/wt/issue-7', env={}),
                         'bash -lc "cd . && DB_PASSWORD=[REDACTED] python3 …/run.py"')
        self.assertEqual(len(agents.public_command('codex', codex_events('x' * 500, 0), env={})),
                         agents.COMMAND_WIDTH)

    def test_no_command_when_the_cli_does_not_expose_it(self):
        self.assertIsNone(agents.public_command('codex', 'plain text\n{"type": "x"}', env={}))
        self.assertIsNone(agents.public_command('claude', codex_events('ls', 0), env={}))
        self.assertIsNone(agents.public_command('agy', '', env={}))

    def test_a_timed_out_turn_records_its_last_command(self):
        command = 'bash -lc "python3 -m unittest discover -s tests"'

        def fake_run(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 1, output=codex_events(command, 12))
        with tempfile.TemporaryDirectory() as tmp:
            runner = agents.AgentRunner(tmp, 1, run=fake_run, env={})
            with self.assertRaises(agents.AgentError) as ctx:
                runner.run(CFG.agent('codex'), 'write', 'p', 'implement', tmp, 'x')
            self.assertNotIn('unittest', ctx.exception.tail)  # out of the last 10 lines
            self.assertEqual(runner.last_command, command)
            runner._run = lambda argv, **kw: SimpleNamespace(returncode=1, stdout='',
                                                             stderr='boom')
            with self.assertRaises(agents.AgentError):
                runner.run(CFG.agent('codex'), 'write', 'p', 'implement', tmp, 'x')
            self.assertIsNone(runner.last_command)  # a new turn starts without one

    def test_the_turn_marker_records_the_duration_and_the_last_command(self):
        command = 'bash -lc "cd /wt/issue-7 && python3 -m `echo unittest`"'

        class GH(FakeGH):
            bodies = {}

            def comment(self, number, body):
                self.bodies[len(self.bodies) + 1] = body
                return len(self.bodies)

            def edit_comment(self, comment_id, body):
                self.bodies[comment_id] = body

        def fake_run(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 5, output=codex_events(command, 12))
        clock = iter([100.0, 103.5])
        gh = GH()
        with tempfile.TemporaryDirectory() as tmp:
            runner = agents.AgentRunner(tmp, 5, run=fake_run, env={})
            d = Dispatcher(CFG, gh, FakeWS(), runner, log=lambda m: None,
                           ledger=Ledger(gh, monotonic=lambda: next(clock)))
            entry = d._open_turn((7, 'implement'), CFG.agent('codex'), 'write', 'issue-7')
            with self.assertRaises(agents.AgentError) as ctx:
                runner.run(CFG.agent('codex'), 'write', 'p', 'implement', '/wt/issue-7', 'x')
            d._close_turn(entry, 'failure', ctx.exception)
        record = read_marker(gh.bodies[1], 'ledger')
        self.assertEqual(record['duration_ms'], 3500)
        self.assertEqual(record['last_command'],
                         'bash -lc "cd . && python3 -m `echo unittest`"')
        self.assertIn('- **Ultimo comando (esposto dalla CLI):** `` bash -lc "cd . && '
                      'python3 -m `echo unittest`" ``', gh.bodies[1])
        self.assertNotIn('/wt/', gh.bodies[1])

    def test_no_last_command_in_the_marker_when_the_cli_does_not_expose_it(self):
        gh = FakeGH()
        recorded = []
        gh.comment = lambda number, body: 1
        gh.edit_comment = lambda cid, body: recorded.append(read_marker(body, 'ledger'))
        runner = SimpleNamespace(last_report=None, last_command=None)
        d = Dispatcher(CFG, gh, FakeWS(), runner, log=lambda m: None, ledger=Ledger(gh))
        d._close_turn(d._open_turn((7, 'implement'), CFG.agent('claude'), 'write', 'l'),
                      'failure', agents.AgentError('claude timed out after 5s'))
        self.assertNotIn('last_command', recorded[-1])
        self.assertIsInstance(recorded[-1]['duration_ms'], int)


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

    def test_the_escalation_hides_short_passwords_and_coloured_paths(self):
        out = ('DATABASE_PASSWORD=hunter2\nlogin with hunter2\n'
               '\x1b[31m/home/operator/private/file.py\x1b[0m\n')

        def fake_run(argv, **kw):
            if argv[-1] == '--version':
                return SimpleNamespace(returncode=0, stdout='2.1.0 (Claude Code)', stderr='')
            raise subprocess.TimeoutExpired(argv, 5, output=out)
        with tempfile.TemporaryDirectory() as tmp:
            gh = FakeGH()
            runner = agents.AgentRunner(tmp, 5, run=fake_run, env={'DATABASE_PASSWORD': 'hunter2'})
            d = Dispatcher(CFG, gh, FakeWS(), runner, log=lambda m: None)
            d.snapshot = lambda: Snapshot([], [{'number': 7, 'title': 't',
                                                'labels': {'agent:ready'},
                                                'labeler': 'brushtyler', 'admitted': True}], [])
            d.tick()
        body = gh.comments[-1][1]
        self.assertEqual(code_block(body).splitlines(),
                         ['DATABASE_PASSWORD=[REDACTED]', 'login with [REDACTED]', '…/file.py'])
        self.assertNotIn('hunter2', body)
        self.assertNotIn('/home/operator', body)
        self.assertNotIn('\x1b', body)

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
