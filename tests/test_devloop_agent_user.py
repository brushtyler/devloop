"""The system user of the agents and of the tests (issue #415).

The agent CLIs and the test command run as ``[agent_user] name`` through sudo, and a startup
check refuses a user that can read ``key_dir`` or ``state_dir``. sudo is never run here: the
launch mechanism is a fake that records the commands, or one that runs them as the current user.
"""
import dataclasses
import getpass
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import devloop.__main__ as cli
from devloop import agents, agentuser, config
from devloop.agentuser import AgentUser, AgentUserError
from devloop.workspace import Workspace, run_in_group

CFG = dataclasses.replace(config.load_config(), max_parallel_actions=1)
ME = getpass.getuser()
SUDO = ['sudo', '-n', '-E', '-u', 'devloop-agent', '--']


def review():
    return {'verdict': 'approve', 'summary': 'ok', 'blocking_findings': [],
            'non_blocking_findings': [], 'previous_findings': [], 'human_reason': '',
            'checks_performed': ['read diff']}


class LocalUser(AgentUser):
    """An agent user whose commands run as the current user, without sudo: the real probe and
    the real process groups, on any machine."""

    def __init__(self, home, kills=None):
        super().__init__('devloop-agent', home=home)
        self.kills = kills if kills is not None else []

    def command(self, argv):
        return list(argv)

    def kill_group(self, pgid):
        self.kills.append(pgid)
        os.killpg(pgid, signal.SIGKILL)


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='agent-user-'))
        self.addCleanup(lambda: subprocess.run(['rm', '-rf', str(self.tmp)]))
        self.calls = []
        self.user = AgentUser('devloop-agent', home='/home/devloop-agent', run=self.record)

    def record(self, argv, **kw):
        self.calls.append((argv, kw))
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def test_commands_run_as_the_agent_user_through_sudo(self):
        self.assertEqual(self.user.command(['claude', '-p']), SUDO + ['claude', '-p'])
        self.assertEqual(self.user.shell('python3 -m unittest'),
                         SUDO + ['/usr/bin/bash', '-c', 'python3 -m unittest'])
        env = self.user.environment({'HOME': '/home/devloop', 'USER': 'devloop', 'X': '1'})
        self.assertEqual(env, {'HOME': '/home/devloop-agent', 'USER': 'devloop-agent',
                               'LOGNAME': 'devloop-agent', 'X': '1'})

    def test_the_process_group_is_killed_as_the_agent_user(self):
        self.user.kill_group(4242)
        self.assertEqual(self.calls[0][0], SUDO + ['/usr/bin/kill', '-KILL', '--', '-4242'])

    def test_an_unknown_user_is_a_config_error(self):
        cfg = dataclasses.replace(CFG, agent_user=config.AgentUserPolicy(name='nosuchuser415x'))
        with self.assertRaisesRegex(config.ConfigError, 'no system user'):
            cli.agent_user_of(cfg)
        self.assertIsNone(cli.agent_user_of(CFG))

    def test_the_environment_has_the_agent_users_home_and_trusts_the_worktrees(self):
        env = cli.agent_environment(CFG, self.tmp / 'gh', self.user)
        self.assertEqual((env['HOME'], env['USER']), ('/home/devloop-agent', 'devloop-agent'))
        pairs = {(env[f'GIT_CONFIG_KEY_{i}'], env[f'GIT_CONFIG_VALUE_{i}'])
                 for i in range(int(env['GIT_CONFIG_COUNT']))}
        self.assertEqual(pairs, {('credential.helper', ''), ('http.extraHeader', ''),
                                 ('safe.directory', '*')})
        self.assertNotIn('safe.directory', json.dumps(cli.agent_environment(CFG, self.tmp)))

    def test_follow_reads_the_transcripts_in_the_agent_users_home(self):
        cfg = dataclasses.replace(CFG, agent_user=config.AgentUserPolicy(name='devloop-agent'))
        with mock.patch.object(cli, 'load_config', return_value=cfg), \
                mock.patch.object(cli, 'main_checkout', return_value=Path('/repo')), \
                mock.patch.object(cli, 'agent_user_of',
                                  side_effect=lambda c: self.user if c is cfg else None), \
                mock.patch.object(cli.follow, 'follow', return_value=0) as run, \
                self.assertRaises(SystemExit):
            cli.main(['follow', 'issue-7'])
        root = Path('/repo') / CFG.state_dir / 'worktrees'
        run.assert_called_once_with([root / 'issue-7'], home=Path('/home/devloop-agent'))

    def test_a_codex_turn_runs_as_the_agent_user_with_its_files_in_the_shared_directory(self):
        shared = self.tmp / 'state/agent-shared'
        logs = self.tmp / 'state/logs'
        seen = {}

        def fake_run(argv, **kw):
            if argv[-1] == '--version':
                seen['version'] = (argv, kw.get('env'))
                return SimpleNamespace(returncode=0, stdout='codex-cli 0.1.0', stderr='')
            seen['argv'], seen['env'] = argv, kw.get('env')
            out = Path(argv[argv.index('-o') + 1])
            schema = Path(argv[argv.index('--output-schema') + 1])
            seen['schema'] = json.loads(schema.read_text())
            out.write_text(json.dumps(review()))  # what codex writes, as the agent user
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        runner = agents.AgentRunner(logs, 10, run=fake_run, env={'HOME': '/home/devloop-agent'},
                                    agent_user=self.user, shared_dir=shared)
        result = runner.run(CFG.agent('codex'), 'review', 'p', 'review', self.tmp, 'pr-1')
        self.assertEqual(result['verdict'], 'approve')
        argv = seen['argv']
        self.assertEqual(argv[:7], SUDO + ['codex'])
        for flag in ('-o', '--output-schema'):
            path = Path(argv[argv.index(flag) + 1])
            self.assertEqual(path.parent, shared / 'turns', flag)
            self.assertFalse(path.exists(), flag)  # removed after the turn
        self.assertEqual(seen['schema']['type'], 'object')
        self.assertEqual(seen['env'], {'HOME': '/home/devloop-agent'})
        # The final message stays in the log of the turn, the CLI's name in its first line.
        self.assertEqual([json.loads(p.read_text())['verdict']
                          for p in logs.glob('*.last.json')], ['approve'])
        self.assertTrue(next(logs.glob('*.log')).read_text().startswith('$ codex (review)'))
        runner.version(CFG.agent('codex'))
        self.assertEqual(seen['version'], (SUDO + ['codex', '--version'],
                                           {'HOME': '/home/devloop-agent'}))

    def test_without_an_agent_user_nothing_changes(self):
        seen = []

        def fake_run(argv, **kw):
            seen.append(argv)
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                {'is_error': False, 'structured_output': review()}), stderr='')
        runner = agents.AgentRunner(self.tmp / 'logs', 10, run=fake_run)
        runner.run(CFG.agent('claude'), 'review', 'p', 'review', self.tmp, 'pr-1')
        self.assertEqual(seen[0][:2], ['claude', '-p'])
        self.assertFalse((self.tmp / 'agent-shared').exists())

    def test_the_tests_run_as_the_agent_user_with_a_temporary_directory_it_can_write(self):
        seen = {}

        def fake_run(argv, **kw):
            seen['argv'], seen['env'] = argv, kw['env']
            tmp = Path(kw['env']['TMPDIR'])
            seen['mode'] = tmp.stat().st_mode & 0o777
            (tmp / 'left-by-the-tests').write_text('x')
            return SimpleNamespace(returncode=0, stdout='ok', stderr='')
        ws = Workspace(self.tmp, 'state', 'main', run=fake_run, test_env={'X': '1'},
                       private_tmp=True, agent_user=self.user)
        self.assertEqual(ws.run_tests(self.tmp, 'make test', 5), (True, 'ok'))
        self.assertEqual(seen['argv'], SUDO + ['/usr/bin/bash', '-c', 'make test'])
        tmp = Path(seen['env']['TMPDIR'])
        self.assertEqual(tmp.parent, self.tmp / 'state/agent-shared/tmp')
        self.assertEqual(seen['mode'], 0o770)
        self.assertFalse(tmp.exists())


class TerminationTests(unittest.TestCase):
    """A timeout kills the process group as the agent user: the dispatcher's user cannot
    signal its processes (``os.killpg`` raises PermissionError)."""

    def test_a_timeout_kills_the_group_as_the_agent_user(self):
        real_killpg = os.killpg
        pgids = []

        def kill(pgid):
            pgids.append(pgid)
            real_killpg(pgid, signal.SIGKILL)
        with mock.patch('devloop.workspace.os.killpg', side_effect=PermissionError):
            start = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                run_in_group(['sh', '-c', 'sleep 30 & sleep 30'], timeout=0.5, kill=kill)
        self.assertEqual(len(pgids), 1)
        self.assertLess(time.monotonic() - start, 15)
        # Nothing of the group is left, once the background sleep is reaped.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                real_killpg(pgids[0], 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail('the process group is still alive')

    def test_a_turn_that_times_out_is_killed_as_the_agent_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            user = LocalUser(tmp)
            # The launch mechanism runs a CLI that never ends instead of codex.
            user.command = lambda argv: ['sh', '-c', 'sleep 30 & sleep 30']
            runner = agents.AgentRunner(Path(tmp) / 'logs', 1, agent_user=user,
                                        shared_dir=Path(tmp) / 'shared')
            with self.assertRaises(agents.AgentError) as ctx:
                runner.run(CFG.agent('codex'), 'review', 'p', 'review', tmp, 'pr-1')
            self.assertTrue(ctx.exception.timed_out)
            self.assertEqual(len(user.kills), 1)

    def test_a_test_command_that_times_out_is_killed_as_the_agent_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            user = LocalUser(tmp)
            ws = Workspace(tmp, 'state', 'main', agent_user=user)
            passed, tail = ws.run_tests(tmp, 'sleep 30', 1)
            self.assertFalse(passed)
            self.assertIn('timed out', tail)
            self.assertEqual(len(user.kills), 1)


class StartupCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='agent-user-check-'))
        self.addCleanup(lambda: subprocess.run(['rm', '-rf', str(self.tmp)]))
        self.keys = self.tmp / 'ghapps'
        self.keys.mkdir()
        (self.keys / 'x-dispatcher.2026-11-19.private-key.pem').write_text('not a key\n')
        self.state = self.tmp / 'state'
        for name in ('worktrees', 'agent-shared', 'logs', 'pinned-abc'):
            (self.state / name).mkdir(parents=True)
        (self.state / 'turn-journal.jsonl').write_text('')
        self.calls = []

    def fake(self, readable=(), unwritable=(), returncode=0, stderr=''):
        def run(argv, **kw):
            self.calls.append(argv)
            out = ''.join(f'r:{p}\0' for p in readable) + ''.join(f'w:{p}\0' for p in unwritable)
            return SimpleNamespace(returncode=returncode, stdout=out, stderr=stderr)
        return AgentUser('devloop-agent', home='/home/devloop-agent', run=run)

    def test_a_user_that_can_read_the_key_dir_is_refused(self):
        user = self.fake(readable=[str(self.keys)])
        with self.assertRaisesRegex(AgentUserError, f'devloop-agent can read {self.keys}'):
            agentuser.check(user, self.keys, self.state, me='devloop')

    def test_the_probe_covers_keys_state_and_overlay_but_not_the_worktrees(self):
        user = self.fake()
        overlay = self.state / 'config.local.toml'
        agentuser.check(user, self.keys, self.state, overlay, me='devloop')
        argv = self.calls[0]
        self.assertEqual(argv[:8], SUDO + ['/usr/bin/bash', '-c'])
        args = argv[argv.index('probe') + 1:]
        split = args.index('--')
        readable, writable = args[:split], args[split + 1:]
        key = str(next(self.keys.iterdir()))
        for path in (self.keys, key, self.state, self.state / 'logs',
                     self.state / 'pinned-abc', self.state / 'turn-journal.jsonl', overlay):
            self.assertIn(str(path), readable)
        self.assertEqual(writable, [str(self.state / 'agent-shared'),
                                    str(self.state / 'worktrees')])

    def test_a_user_that_cannot_write_the_worktrees_is_refused(self):
        user = self.fake(unwritable=[str(self.state / 'worktrees')])
        with self.assertRaisesRegex(AgentUserError, 'cannot write .*worktrees'):
            agentuser.check(user, self.keys, self.state, me='devloop')

    def test_the_dispatchers_own_user_is_refused(self):
        with self.assertRaisesRegex(AgentUserError, 'own user'):
            agentuser.check(self.fake(), self.keys, self.state, me='devloop-agent')
        self.assertEqual(self.calls, [])

    def test_a_user_sudo_does_not_start_is_refused(self):
        user = self.fake(returncode=1, stderr='sudo: a password is required')
        with self.assertRaisesRegex(AgentUserError, 'a password is required'):
            agentuser.check(user, self.keys, self.state, me='devloop')

    def test_the_real_probe_finds_what_the_user_can_read(self):
        # Run as the current user, who owns everything here: the probe must report the keys.
        user = LocalUser(self.tmp)
        with self.assertRaises(AgentUserError) as ctx:
            agentuser.check(user, self.keys, self.state, me='devloop')
        message = str(ctx.exception)
        self.assertIn(f'can read {self.keys}', message)
        self.assertIn('private-key.pem', message)
        self.assertIn(f'can read {self.state}', message)
        self.assertNotIn(f'can read {self.state / "worktrees"}', message)
        self.assertNotIn('cannot write', message)
        (self.state / 'worktrees').rmdir()
        with self.assertRaisesRegex(AgentUserError, 'cannot write .*worktrees'):
            agentuser.check(user, self.tmp / 'missing', self.tmp / 'missing', me='devloop')

    def test_devloop_refuses_to_start_and_the_check_can_be_turned_off(self):
        user = self.fake(readable=[str(self.keys)])
        cfg = dataclasses.replace(CFG, app_key_dir=str(self.keys))
        with mock.patch.dict(os.environ):
            os.environ.pop('DEVLOOP_APP_KEY_DIR', None)
            with self.assertRaisesRegex(AgentUserError, 'can read'):
                cli.check_agent_user(cfg, self.state, None, user, log=lambda m: None, me='x')
            logged = []
            off = dataclasses.replace(cfg, agent_user=config.AgentUserPolicy(
                name='devloop-agent', check=False))
            cli.check_agent_user(off, self.state, None, user, log=logged.append, me='x')
        self.assertEqual(len(self.calls), 1)
        self.assertIn('for development only', logged[0])
        cli.check_agent_user(cfg, self.state, None, None, me='x')  # no agent user: no check

    def test_main_exits_before_any_tick(self):
        dispatcher = SimpleNamespace(ws=SimpleNamespace(agent_user=self.fake()), tick=mock.Mock())
        with mock.patch.object(cli, 'build', return_value=(CFG, self.state, dispatcher)), \
                mock.patch.object(cli, 'check_agent_user',
                                  side_effect=AgentUserError('devloop-agent can read /k')):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(['tick'])
        self.assertIn('devloop-agent can read /k', str(ctx.exception.code))
        dispatcher.tick.assert_not_called()


class ConfigTests(unittest.TestCase):
    def test_the_agent_user_is_off_by_default_and_checked_when_set(self):
        self.assertEqual(CFG.agent_user, config.AgentUserPolicy(name='', check=True))
        data = config.load_data()
        cfg = config.parse_config(dict(data, agent_user={'name': 'devloop-agent',
                                                         'check': False}))
        self.assertEqual(cfg.agent_user, config.AgentUserPolicy('devloop-agent', False))
        for table in ({'name': 'Bad User'}, {'name': 3}, {'check': 'yes'}, {'user': 'x'}, []):
            with self.subTest(table=table), self.assertRaises(config.ConfigError):
                config.parse_config(dict(data, agent_user=table))

    def test_the_local_overlay_may_set_it(self):
        data = config.apply_overlay(config.load_data(),
                                    {'agent_user': {'name': 'devloop-agent'}})
        self.assertEqual(config.parse_config(data).agent_user.name, 'devloop-agent')


if __name__ == '__main__':
    unittest.main()
