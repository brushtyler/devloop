"""devloop status: the test commands the agents may not run (#697).

``test_cmd`` and the command of one test module that the prompts give (``[prompts]
module_test_cmd``) are checked against the commands each configured agent admits in the turns of
its roles: claude's ``--allowedTools`` of ``write_args`` and ``review_args``, agy's
``write_commands`` and ``review_commands``. codex has no list. Only warnings: the permissions
stay the operator's, and the loop goes on.
"""

import dataclasses
import sys
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import devloop.__main__ as cli  # noqa: E402
from devloop import config  # noqa: E402
from devloop.agents import admits_command, command_key  # noqa: E402

from helpers import ssdlc_project  # noqa: E402
from test_devloop_quickstart import example  # noqa: E402

RUN_TESTS = 'python3 scripts/run-tests.py'
MODULE = "python3 -m unittest discover -s tests -p 'test_name.py'"
NPM_PROJECT = 'test_cmd = "npm test"\n[prompts]\nmodule_test_cmd = "npx vitest run src/a.test.ts"\n'


def agent(kind, write_args=(), review_args=(), write_commands=(), review_commands=()):
    return config.AgentConfig(key=f'{kind}-x', kind=kind, display_name=kind, model='m',
                              model_label='m', effort='high', version_cmd=(kind, '--version'),
                              write_args=tuple(write_args), review_args=tuple(review_args),
                              write_commands=tuple(write_commands),
                              review_commands=tuple(review_commands))


def composed(project, overlay=''):
    """The configuration devloop composes on the host (``config.parse_composed``) from the
    installation, the project file ``project`` and the overlay of the quick start without its
    agy lists, with ``overlay`` over it; both TOML text."""
    base = tomllib.loads(example('config.local.toml'))
    del base['agents']
    tables = {'config': config.read_toml(config.DEFAULT_CONFIG), 'project': tomllib.loads(project),
              'local': config._merge(base, tomllib.loads(overlay))}
    data = config.compose(tables['config'], tables['project'], tables['local'])
    return config.parse_config(data, agent_test_cmd=config.agent_test_cmd(tables))


class AdmitsCommandTests(unittest.TestCase):
    def test_claude_reads_its_allowed_and_disallowed_tools(self):
        claude = agent('claude', write_args=['--permission-mode', 'acceptEdits', '--allowedTools',
                                             'Bash(python3 -m unittest *)', 'Bash(npm test:*)',
                                             'Bash(git diff *)', '--disallowedTools', 'WebFetch'])
        self.assertTrue(admits_command(claude, 'write', MODULE))
        self.assertTrue(admits_command(claude, 'write', 'npm test'))
        self.assertTrue(admits_command(claude, 'write', 'npm test -- --run'))
        self.assertFalse(admits_command(claude, 'write', 'npm testx'))
        self.assertFalse(admits_command(claude, 'write', RUN_TESTS))
        # The review turns read review_args, empty here: nothing is admitted.
        self.assertFalse(admits_command(claude, 'review', MODULE))
        # A pattern matches the whole command, `*` any text.
        claude = agent('claude', review_args=['--allowedTools',
                                              'Bash(python3 -m unittest tests.*)'])
        self.assertTrue(admits_command(claude, 'review', 'python3 -m unittest tests.test_a'))
        self.assertFalse(admits_command(claude, 'review', MODULE))

    def test_claude_checks_each_command_of_a_chain_and_its_denials(self):
        claude = agent('claude', write_args=['--allowedTools=Bash(npm ci *)', '--allowedTools',
                                             'Bash(npm test *)'])
        self.assertTrue(admits_command(claude, 'write', 'npm ci && npm test'))
        self.assertFalse(admits_command(claude, 'write', 'npm ci && npm run lint'))
        claude = agent('claude', write_args=['--allowedTools', 'Bash', '--disallowedTools',
                                             'Bash(npm publish *)'])
        self.assertTrue(admits_command(claude, 'write', 'npm test'))
        self.assertFalse(admits_command(claude, 'write', 'npm publish --tag x'))

    def test_claude_splits_the_chain_as_the_shell_does(self):
        # Review of PR #698: an operator between quotes splits nothing, a newline and `&` do.
        claude = agent('claude', write_args=['--allowedTools', 'Bash(npx vitest *)',
                                             'Bash(npm test *)'])
        for command in ("npx vitest run --testNamePattern='foo|bar'",
                        'npx vitest run -t "a && b; c"', r'npx vitest run -t a\;b',
                        'npm test 2>&1', 'npm test &> out.log', 'npm test >| out.log',
                        'npm test -- --run \\\n  --silent', 'npm test |& npx vitest run'):
            with self.subTest(command=command):
                self.assertTrue(admits_command(claude, 'write', command))
        for command in ('npm test -- --run\nnpx tsc --noEmit', 'npm test & npx tsc --noEmit',
                        'npm test |& npx tsc', "npm test 'x' ; npx tsc", 'npm test "a\\"" | tsc'):
            with self.subTest(command=command):
                self.assertFalse(admits_command(claude, 'write', command))

    def test_a_quoted_vitest_filter_in_the_npm_recipe_warns_nothing(self):
        project = example('npm project.toml').replace(
            '"npx vitest run src/name.test.ts"', '''"npx vitest run --testNamePattern='foo|bar'"''')
        cfg = composed(project, example('npm config.local.toml'))
        self.assertEqual(cfg.prompts.module_test_cmd, "npx vitest run --testNamePattern='foo|bar'")
        self.assertEqual(cli.command_lines(cfg), [])

    def test_claude_without_permission_prompts_admits_everything(self):
        for args in (['--permission-mode', 'bypassPermissions'],
                     ['--permission-mode=bypassPermissions'], ['--dangerously-skip-permissions']):
            with self.subTest(args=args):
                self.assertTrue(admits_command(agent('claude', write_args=args), 'write',
                                               'npm test'))
        self.assertFalse(admits_command(agent('claude', write_args=['--permission-mode',
                                                                     'acceptEdits']),
                                        'write', 'npm test'))

    def test_agy_matches_its_prefixes_word_by_word(self):
        agy = agent('agy', write_commands=['python3 -m unittest', 'npm run'],
                    review_commands=['git diff'])
        self.assertTrue(admits_command(agy, 'write', MODULE))
        self.assertTrue(admits_command(agy, 'write', 'npm run test'))
        self.assertFalse(admits_command(agy, 'write', 'npm runner'))
        self.assertFalse(admits_command(agy, 'review', MODULE))

    def test_codex_has_no_list(self):
        self.assertIsNone(admits_command(agent('codex'), 'write', 'npm test'))
        self.assertEqual(command_key(agent('codex'), 'review'), '')

    def test_the_key_to_look_at(self):
        self.assertEqual(command_key(agent('claude'), 'write'),
                         'agents.claude-x.write_args (--allowedTools)')
        self.assertEqual(command_key(agent('claude'), 'review'),
                         'agents.claude-x.review_args (--allowedTools)')
        self.assertEqual(command_key(agent('agy'), 'review'), 'agents.agy-x.review_commands')


class CommandLinesTests(unittest.TestCase):
    def test_the_configuration_of_this_repository(self):
        # ssdlc's installation, project file and overlay, as devloop composes them on the host.
        # The warnings on claude are expected (#697): its permissions are the operator's, and
        # this issue does not change them; agy's overlay admits both, codex has no list.
        cfg = config.load_config(None, ssdlc_project.OVERLAY, ssdlc_project.PROJECT)
        self.assertEqual((cfg.test_cmd, cfg.prompts.module_test_cmd), (RUN_TESTS, MODULE))
        self.assertEqual(cli.agent_modes(cfg),
                         {'claude-implementer': ('write',), 'codex-reviewer': ('review',),
                          'agy-reviewer': ('review',), 'claude-reviewer': ('review',)})
        self.assertEqual(cli.command_lines(cfg), [
            f'agent commands: claude-implementer: test_cmd `{RUN_TESTS}` not admitted in its '
            'write turns (agents.claude-implementer.write_args (--allowedTools))',
            f'agent commands: claude-reviewer: test_cmd `{RUN_TESTS}` and '
            f'prompts.module_test_cmd `{MODULE}` not admitted in its review turns '
            '(agents.claude-reviewer.review_args (--allowedTools))'])

    def test_agy_without_the_lists_of_the_overlay(self):
        # The test_cmd of the project file never becomes a command of agy (ADR 0006, D2).
        overlay = config.read_toml(ssdlc_project.OVERLAY, 'local config')
        del overlay['agents']
        cfg = config.parse_config(config.compose(config.read_toml(config.DEFAULT_CONFIG),
                                                 ssdlc_project.project(), overlay),
                                  agent_test_cmd='')
        self.assertIn(f'agent commands: agy-reviewer: test_cmd `{RUN_TESTS}` not admitted in '
                      'its review turns (agents.agy-reviewer.review_commands)',
                      cli.command_lines(cfg))

    def test_an_npm_project_with_the_defaults(self):
        cfg = composed(NPM_PROJECT)
        module = 'npx vitest run src/a.test.ts'
        both = f'test_cmd `npm test` and prompts.module_test_cmd `{module}`'
        self.assertEqual(cli.command_lines(cfg), [
            f'agent commands: claude-implementer: {both} not admitted in its write turns '
            '(agents.claude-implementer.write_args (--allowedTools))',
            f'agent commands: agy-reviewer: {both} not admitted in its review turns '
            '(agents.agy-reviewer.review_commands)',
            f'agent commands: claude-reviewer: {both} not admitted in its review turns '
            '(agents.claude-reviewer.review_args (--allowedTools))'])

    def test_an_npm_project_with_the_recipe_of_the_quick_start(self):
        # The recipe sets the keys devloop reads: with it no agent is left out.
        cfg = composed(example('npm project.toml'), example('npm config.local.toml'))
        self.assertEqual(cfg.test_cmd, 'npm test')
        self.assertEqual(cli.command_lines(cfg), [])
        for key in ('claude-implementer', 'agy-reviewer'):
            self.assertTrue(admits_command(cfg.agents[key], 'write', 'npx tsc --noEmit'))
        # The reviewer stays read only.
        self.assertIn('Edit', cfg.agents['claude-reviewer'].review_args)

    def test_a_key_in_both_roles_gets_one_line(self):
        # A key without a role serves the roles it is given: both modes, in one line.
        base = composed(NPM_PROJECT)
        agents = dict(base.agents)
        agents['agy'] = dataclasses.replace(agents['agy-reviewer'], key='agy')
        cfg = dataclasses.replace(base, agents=agents, implementer='agy',
                                  reviewers=('codex-reviewer', 'agy'))
        self.assertEqual(cli.agent_modes(cfg)['agy'], ('write', 'review'))
        line = [x for x in cli.command_lines(cfg) if x.startswith('agent commands: agy: ')]
        self.assertEqual(len(line), 1)
        self.assertIn('in its write turns (agents.agy.write_commands); ', line[0])
        self.assertTrue(line[0].endswith('in its review turns (agents.agy.review_commands)'))

    def test_no_module_command_checks_test_cmd_alone(self):
        cfg = composed('test_cmd = "npm test"\n')
        self.assertEqual(cfg.prompts.module_test_cmd, '')
        for line in cli.command_lines(cfg):
            self.assertNotIn('module_test_cmd', line)


class StatusTests(unittest.TestCase):
    def test_status_prints_the_warnings_and_goes_on(self):
        cfg = composed(NPM_PROJECT)
        lines = []

        def stop():
            raise RuntimeError('stop')
        d = SimpleNamespace(gh=None, pinner=None, snapshot=stop)
        with mock.patch.object(cli, 'build', return_value=(cfg, Path('/state'), d)), \
                mock.patch.object(cli, 'log', lines.append), \
                mock.patch.object(cli, 'direction_line', return_value='direction: none'), \
                mock.patch.object(cli, 'label_transition', return_value=None), \
                mock.patch.object(cli, 'tracking_lines', return_value=[]), \
                self.assertRaises(RuntimeError):
            cli.main(['status'])
        warnings = [line for line in lines if line.startswith('agent commands: ')]
        self.assertEqual(warnings, cli.command_lines(cfg))
        self.assertEqual(len(warnings), 3)
        # The permissions are untouched.
        self.assertEqual(cfg.agents['agy-reviewer'].review_commands,
                         config.AGY_DEFAULT_COMMANDS)


if __name__ == '__main__':
    unittest.main()
