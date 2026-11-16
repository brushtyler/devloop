"""devloop: minimal per-turn permissions of headless agy, and its stops without output (#122)."""

import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devloop import agents, config, schema
from devloop.dispatcher import Action, Dispatcher, Snapshot
from test_devloop import CFG, FakeGH, FakeWS, pr
from test_devloop_fallback import ARGON, DATA, HYDROGEN, FakeCLI, with_agent

JETSKI = ('jetski: no output produced — a tool required the "command" permission that headless '
          'mode cannot prompt for, so it was auto-denied.')
DEFAULT_RULES = ['command(python3 scripts/run-tests.py)', 'command(python3 -m unittest)',
                 'command(git status)', 'command(git diff)', 'command(git log)',
                 'command(git show)']


def settings(argv):
    return json.loads(argv[argv.index('--settings') + 1])


class AgyArgumentsTests(unittest.TestCase):
    def build(self, mode, agent=None):
        argv, _ = agents.build_command(agent or CFG.agent('agy'), mode, 'p',
                                       schema_file=schema.schema_path('review'), out_file='o')
        return argv

    def test_every_turn_gets_only_the_rules_of_its_mode(self):
        self.assertEqual(CFG.test_cmd, 'python3 scripts/run-tests.py')
        for mode in agents.MODES:
            with self.subTest(mode):
                argv = self.build(mode)
                self.assertEqual(argv.count('--settings'), 1)
                self.assertEqual(settings(argv), {'permissions': {'allow': DEFAULT_RULES}})
        agy = dataclasses.replace(CFG.agent('agy'), write_commands=('make check', 'git diff'),
                                  review_commands=('git log',))
        self.assertEqual(settings(self.build('write', agy))['permissions']['allow'],
                         ['command(make check)', 'command(git diff)'])
        self.assertEqual(settings(self.build('review', agy))['permissions']['allow'],
                         ['command(git log)'])
        # A fallback model runs with the same permissions.
        fallback = CFG.agent('agy').chain()[1]
        self.assertEqual(settings(self.build('review', fallback)),
                         {'permissions': {'allow': DEFAULT_RULES}})

    def test_no_global_permission_nor_bypass(self):
        for mode in agents.MODES:
            argv = self.build(mode)
            self.assertNotIn('--dangerously-skip-permissions', argv)
            rules = settings(argv)['permissions']['allow']
            self.assertEqual(list(settings(argv)), ['permissions'])
            self.assertEqual(list(settings(argv)['permissions']), ['allow'])
            for rule in rules:
                self.assertRegex(rule, r'^command\([^*():]+ [^*():]+\)$')
            for bare in ('*', 'python3', 'python', 'bash', 'sh', 'git'):
                self.assertNotIn(f'command({bare})', rules)
            self.assertNotIn('command(git add)', rules)
            self.assertNotIn('command(git commit)', rules)
        # The other CLIs keep their own permission arguments.
        for key in ('claude', 'codex'):
            argv, _ = agents.build_command(CFG.agent(key), 'review', 'p',
                                           schema_file=schema.schema_path('review'),
                                           out_file='o')
            self.assertNotIn('--settings', argv)


class AgyConfigTests(unittest.TestCase):
    def test_defaults_come_from_test_cmd(self):
        cfg = config.parse_config(dict(DATA, test_cmd='  make   check '))
        expected = ('make check', *config.AGY_DEFAULT_COMMANDS)
        self.assertEqual(cfg.agent('agy').review_commands, expected)
        self.assertEqual(cfg.agent('agy').write_commands, expected)
        self.assertEqual((cfg.agent('claude').review_commands, cfg.agent('codex').write_commands),
                         ((), ()))
        # A test_cmd already in the list is not repeated.
        cfg = config.parse_config(dict(DATA, test_cmd='python3 -m unittest'))
        self.assertEqual(cfg.agent('agy').review_commands, config.AGY_DEFAULT_COMMANDS)

    def test_lists_are_configurable_per_mode(self):
        cfg = config.parse_config(with_agent('agy', review_commands=['git diff'],
                                             write_commands=['git diff', 'make check']))
        self.assertEqual(cfg.agent('agy').review_commands, ('git diff',))
        self.assertEqual(cfg.agent('agy').write_commands, ('git diff', 'make check'))
        cfg = config.parse_config(with_agent('agy', review_commands=[]))
        self.assertEqual(cfg.agent('agy').review_commands, ())

    def test_global_or_unsafe_commands_are_refused(self):
        for value in ('*', 'python3', 'python3.12', 'bash', 'sh', 'env', 'git', '', '   ',
                      'python3:*', 'python3 *', 'command(*)', 'make test && rm -rf x',
                      'git diff; rm x', 'echo $HOME', 'python3 -c "x"', 'ls | sh',
                      'cat ~/.ssh/id_rsa'):
            for key in ('review_commands', 'write_commands'):
                with self.subTest(key=key, value=value), self.assertRaises(config.ConfigError):
                    config.parse_config(with_agent('agy', **{key: [value]}))
        for test_cmd in ('python3', 'bash', 'make test && make lint', 'pytest -k "x"'):
            with self.subTest(test_cmd=test_cmd), self.assertRaises(config.ConfigError):
                config.parse_config(dict(DATA, test_cmd=test_cmd))
        # An explicit list does not need a usable test_cmd.
        config.parse_config(dict(with_agent('agy', review_commands=['git diff'],
                                            write_commands=['git diff']),
                                 test_cmd='make test && make lint'))

    def test_bypass_arguments_and_other_agents_are_refused(self):
        for key in ('write_args', 'review_args'):
            for arg in ('--dangerously-skip-permissions', '--settings',
                        '--settings={"permissions": {"allow": ["command(*)"]}}'):
                with self.subTest(key=key, arg=arg), self.assertRaises(config.ConfigError):
                    config.parse_config(with_agent('agy', **{key: [arg]}))
        for key in ('claude', 'codex'):
            with self.subTest(key), self.assertRaises(config.ConfigError):
                config.parse_config(with_agent(key, review_commands=['git diff']))


class OperatorProfileTests(unittest.TestCase):
    def test_turns_leave_the_operator_profile_and_home_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'home'
            profile = home / '.gemini' / 'antigravity-cli' / 'settings.json'
            profile.parent.mkdir(parents=True)
            profile.write_text('{"model": "Gemini 4 Argon (High)"}', encoding='utf-8')
            before = profile.read_bytes()
            seen = []
            cli = FakeCLI()

            def run(argv, **kw):
                if argv[-1] != '--version':
                    seen.append(kw['env']['HOME'])
                return cli(argv, **kw)
            env = dict(os.environ, HOME=str(home))
            runner = agents.AgentRunner(Path(tmp) / 'logs', 10, run=run, env=env)
            for mode in agents.MODES:
                runner.run(CFG.agent('agy'), mode, 'p', 'review', tmp, f'pr-1-{mode}')
            self.assertEqual(seen, [str(home)] * 2)
            self.assertEqual(profile.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in profile.parent.iterdir()), ['settings.json'])


class NoOutputTests(unittest.TestCase):
    def run_script(self, script):
        """A fake agy CLI: a real process running ``script``."""
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(agents, 'build_command',
                                  return_value=(['bash', '-c', script], None)):
            with self.assertRaises(agents.AgentError) as ctx:
                agents.AgentRunner(Path(tmp) / 'logs', 30).run(
                    CFG.agent('agy'), 'review', 'p', 'review', tmp, 'pr-1')
            self.log = ctx.exception.log.read_text(encoding='utf-8')
        return ctx.exception

    def test_jetski_with_exit_zero_is_an_agent_error_with_its_reason(self):
        exc = self.run_script(f"printf '%s\\n' '{JETSKI}' >&2; exit 0")
        self.assertNotIsInstance(exc, agents.ModelUnavailable)
        self.assertEqual(str(exc), f'agy stopped without output: {JETSKI}')
        self.assertIn(JETSKI, self.log)
        self.assertIn('exit 0', self.log)

    def test_silent_exit_zero_is_an_agent_error(self):
        exc = self.run_script('exit 0')
        self.assertEqual(str(exc), 'agy stopped without output: no output')

    def test_jetski_with_a_failure_keeps_the_reason(self):
        exc = self.run_script(f"printf '%s\\n' '{JETSKI}' >&2; exit 3")
        self.assertEqual(str(exc), f'agy exited with 3: {JETSKI}')

    def test_other_clis_are_unchanged(self):
        self.assertIsNone(agents.no_output_reason('claude', '', JETSKI))
        self.assertIsNone(agents.no_output_reason('agy', '{"status": "SUCCESS"}', ''))

    def test_the_escalation_comment_gives_the_reason(self):
        cli = FakeCLI(gemini_4_argon_high=(0, '', JETSKI))
        gh = FakeGH()
        with tempfile.TemporaryDirectory() as tmp:
            d = Dispatcher(CFG, gh, FakeWS(), agents.AgentRunner(tmp, 10, run=cli),
                           log=lambda _: None)
            p = pr()
            p.meta['reviewer'] = 'agy'
            gh.labels[100] = set(p.labels)
            d.snapshot = lambda: Snapshot([p], [], [])
            self.assertEqual(d.tick(), [Action('review', 100)])
        self.assertEqual(cli.models(), [ARGON])  # not an unavailable model: no fallback
        self.assertNotIn(HYDROGEN, gh.comments[-1][1])
        self.assertEqual(gh.labels[100], {'agent:needs-human'})
        self.assertIn(f'agy stopped without output: {JETSKI}', gh.comments[-1][1])


if __name__ == '__main__':
    unittest.main()
