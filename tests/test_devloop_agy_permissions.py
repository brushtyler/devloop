"""devloop: minimal per-turn permissions of headless agy, and its stops without output (#122)."""

import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devloop import agents, config, markdown, policy, schema
from devloop.dispatcher import Dispatcher
from devloop.planning import Action, Snapshot, plan
from helpers.devloop_fakes import FakeGH
from test_devloop import CFG, FakeRunner, FakeWS, norm, pr, raw_review
from test_devloop_fallback import ARGON, DATA, HYDROGEN, QUOTA, FakeCLI, with_agent
from test_devloop_preflight import IssueGH, checked

JETSKI = ('jetski: no output produced — a tool required the "command" permission that headless '
          'mode cannot prompt for, so it was auto-denied.')
DEFAULT_RULES = ['command(python3 scripts/run-tests.py)', 'command(python3 -m unittest)',
                 'command(git status)', 'command(git diff)', 'command(git log)',
                 'command(git show)']


# agy without fallback models: a second denial escalates at once (#180, #319).
NO_FALLBACK = config.parse_config(with_agent('agy', fallback_models=None))


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
        # The other CLIs keep their own permission arguments: claude's --settings only turns
        # its auto memory off (#438).
        for key in ('claude', 'codex'):
            argv, _ = agents.build_command(CFG.agent(key), 'review', 'p',
                                           schema_file=schema.schema_path('review'),
                                           out_file='o')
            given = [json.loads(argv[i + 1]) for i, a in enumerate(argv) if a == '--settings']
            self.assertEqual(given, [agents.CLAUDE_TURN_SETTINGS] if key == 'claude' else [])
            self.assertNotIn('permissions', agents.CLAUDE_TURN_SETTINGS)


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
        # An interpreter, shell or git given by path is still bare.
        for value in ('/usr/bin/python3', '/bin/sh', '/bin/bash', '/usr/bin/env', '/usr/bin/git',
                      './python3', 'venv/bin/python', '.venv/bin/python3.12'):
            for key in ('review_commands', 'write_commands'):
                with self.subTest(key=key, value=value), self.assertRaises(config.ConfigError):
                    config.parse_config(with_agent('agy', **{key: [value]}))
        for test_cmd in ('python3', 'bash', 'make test && make lint', 'pytest -k "x"',
                         '/usr/bin/python3', '/bin/sh', 'venv/bin/python'):
            with self.subTest(test_cmd=test_cmd), self.assertRaises(config.ConfigError):
                config.parse_config(dict(DATA, test_cmd=test_cmd))
        # A path with arguments is a prefix like any other.
        cfg = config.parse_config(dict(DATA, test_cmd='/usr/bin/python3 scripts/run-tests.py'))
        self.assertEqual(cfg.agent('agy').review_commands[0],
                         '/usr/bin/python3 scripts/run-tests.py')
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

    def test_claude_arguments_cannot_replace_the_settings_of_its_turns(self):
        # #438, 2/codex/R2-1: the last --settings counts, and devloop's turns the auto memory
        # off over the settings of the project, which a turn writes.
        for key in ('write_args', 'review_args'):
            for arg in ('--settings', '--settings={"autoMemoryEnabled": true}'):
                with self.subTest(key=key, arg=arg), \
                        self.assertRaisesRegex(config.ConfigError, 'auto memory'):
                    config.parse_config(with_agent('claude', **{key: [arg]}))
        argv, _ = agents.build_command(
            config.parse_config(DATA).agent('claude'), 'write', 'p',
            schema_file=schema.schema_path('implement'), out_file='out.json')
        self.assertEqual(argv.count('--settings'), 1)
        self.assertEqual(json.loads(argv[argv.index('--settings') + 1]),
                         {'autoMemoryEnabled': False,
                          'env': {'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1'}})


def with_pool(*keys, **agy):
    """The default configuration with the reviewer pool ``keys`` and ``[agents.agy]`` updated."""
    data = with_agent('agy', **agy)
    return config.parse_config(dict(data, roles=dict(data['roles'], reviewers=list(keys))))


class ApprovalBindingTests(unittest.TestCase):
    """The review commands of agy bind its approvals like its model and arguments (#122)."""

    def test_commands_enter_the_profile_and_the_policy_version(self):
        before, after = with_pool('agy'), with_pool('agy', review_commands=['git diff'])
        self.assertNotEqual(before.review_policy.version(), after.review_policy.version())
        profile = policy.agent_profile(before.agent('agy'), None, 'review')
        self.assertEqual(profile['commands'], list(before.agent('agy').review_commands))
        self.assertTrue(policy.valid_profile(profile))
        self.assertNotEqual(policy.agent_profile(after.agent('agy'), None, 'review'), profile)
        self.assertEqual(policy.agent_profile(before.agent('agy'), None, 'write')['commands'],
                         list(before.agent('agy').write_commands))
        # The other agents, and a pool without agy commands, keep their profile and version.
        self.assertNotIn('commands', policy.agent_profile(CFG.agent('codex'), None, 'review'))
        row = ('agy', None, 'agy', ARGON, 'high', ())
        self.assertEqual(policy.ReviewPolicy(reviewers=(row + ((), ()),)).version(),
                         policy.ReviewPolicy(reviewers=(row,)).version())
        self.assertNotEqual(policy.ReviewPolicy(reviewers=(row + ((), ('git diff',)),)).version(),
                            policy.ReviewPolicy(reviewers=(row + ((('m', 'high'),),),)).version())

    def test_changed_commands_void_an_approval(self):
        # agy in the pool, and agy outside it as the PR's recorded reviewer: in both cases only
        # its review commands change.
        for pool in (('agy',), ('codex',)):
            before, after = with_pool(*pool), with_pool(*pool, review_commands=['git diff'])
            for cfg, stale in ((before, False), (after, True)):
                with self.subTest(pool=pool, stale=stale):
                    p = pr()
                    p.meta = dict(p.meta, reviewer='agy')
                    approval = dict(norm(raw_review()), reviewer='agy')
                    approval['receipt'] = Dispatcher(before, FakeGH(), FakeWS(), FakeRunner(),
                                                     log=lambda _: None)._receipt(p)
                    self.assertEqual(approval['receipt']['reviewers'], ['agy'])
                    p.reviews = [approval]
                    gh = FakeGH()
                    gh.labels[100] = set(p.labels)
                    self.assertEqual(plan(Snapshot([p], [], []), cfg), [
                        Action('apply_review', 100, 'approval already recorded')])
                    Dispatcher(cfg, gh, FakeWS(), FakeRunner(),
                               log=lambda _: None)._do_apply_review(p)
                    events = [r['event'] for r in (markdown.read_marker(body, 'policy')
                                                   for _, body in gh.comments) if r]
                    if stale:
                        self.assertEqual(gh.labels[100], {'agent:review'})
                        self.assertEqual(events, ['stale'])
                    else:
                        self.assertEqual(gh.labels[100], {'agent:ready-to-merge'})
                        self.assertEqual(events, ['promoted'])
            if pool == ('codex',):
                # Outside the pool only the recorded profile binds the commands.
                self.assertEqual(before.review_policy.version(), after.review_policy.version())


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

    def test_a_valid_answer_quoting_the_message_is_kept(self):
        answer = dict(raw_review(), summary=f'Gestito correttamente {agents.NO_OUTPUT}.',
                      checks_performed=[JETSKI])
        for out in (json.dumps({'status': 'SUCCESS', 'structured_output': answer}),
                    json.dumps({'status': 'SUCCESS', 'structured_output': answer}, indent=2)):
            with self.subTest(indent='\n' in out):
                self.assertIsNone(agents.no_output_reason('agy', out, ''))
                cli = FakeCLI(gemini_4_argon_high=(0, out, ''))
                with tempfile.TemporaryDirectory() as tmp:
                    result = agents.AgentRunner(tmp, 10, run=cli).run(
                        CFG.agent('agy'), 'review', 'p', 'review', tmp, 'pr-1')
                self.assertEqual(result['summary'], answer['summary'])
        # The CLI's own diagnostic still counts, on stderr or on a line of its own.
        self.assertEqual(agents.no_output_reason('agy', '', f'warn\n{JETSKI}\n'), JETSKI)
        self.assertEqual(agents.no_output_reason('agy', f'  {JETSKI}\n', ''), JETSKI)

    def test_the_escalation_comment_gives_the_reason(self):
        cli = FakeCLI(gemini_4_argon_high=(0, '', JETSKI))
        gh = FakeGH()
        with tempfile.TemporaryDirectory() as tmp:
            d = Dispatcher(NO_FALLBACK, gh, FakeWS(), agents.AgentRunner(tmp, 10, run=cli),
                           log=lambda _: None)
            p = pr()
            p.meta['reviewer'] = 'agy'
            gh.labels[100] = set(p.labels)
            d.snapshot = lambda: Snapshot([p], [], [])
            self.assertEqual(d.tick(), [Action('review', 100)])
        # Not an unavailable model: no fallback, one retry on the same model (#180).
        self.assertEqual(cli.models(), [ARGON, ARGON])
        self.assertNotIn(HYDROGEN, gh.comments[-1][1])
        self.assertEqual(gh.labels[100], {'agent:needs-operator'})
        self.assertIn(f'agy stopped without output: {JETSKI}', gh.comments[-1][1])


# What agy 1.5 printed on 2026-11-16 for a review stopped on a denied command (#155).
DENIED = '[{"action":"command","display_name":"Bash"}]'
DENIED_ENVELOPE = ('{"status":"SUCCESS","response":"","denied_actions":'
                   '[{"action":"command","display_name":"Bash"}]}')


class DeniedActionsTests(unittest.TestCase):
    """A turn stopped on a denied command says which action was denied (#155)."""

    def run_cli(self, code, out, err):
        cli = FakeCLI(gemini_4_argon_high=(code, out, err))
        with tempfile.TemporaryDirectory() as tmp, \
                self.assertRaises(agents.AgentError) as ctx:
            agents.AgentRunner(tmp, 10, run=cli).run(CFG.agent('agy'), 'review', 'p', 'review',
                                                     tmp, 'pr-1')
        self.assertEqual(cli.models(), [ARGON])
        return ctx.exception

    def test_the_real_envelope_gives_the_denied_actions(self):
        exc = self.run_cli(0, DENIED_ENVELOPE, JETSKI)
        self.assertNotIsInstance(exc, agents.ModelUnavailable)
        self.assertEqual(str(exc),
                         f'agy stopped without output: {JETSKI}; denied_actions: {DENIED}')
        # Without the diagnostic on stderr the envelope still has no result.
        exc = self.run_cli(0, DENIED_ENVELOPE, '')
        self.assertNotIsInstance(exc, agents.ModelUnavailable)
        self.assertEqual(str(exc), f'agy returned no structured_output; denied_actions: {DENIED}')
        exc = self.run_cli(3, DENIED_ENVELOPE, JETSKI)
        self.assertEqual(str(exc), f'agy exited with 3: {JETSKI}; denied_actions: {DENIED}')

    def test_without_denied_actions_the_messages_are_unchanged(self):
        self.assertEqual(str(self.run_cli(0, '{"status":"SUCCESS","response":""}', JETSKI)),
                         f'agy stopped without output: {JETSKI}')
        for out in ('{"status":"SUCCESS","denied_actions":[]}', 'not json', '[1]'):
            self.assertIsNone(agents.denied_actions('agy', out))
        self.assertIsNone(agents.denied_actions('claude', DENIED_ENVELOPE))
        self.assertEqual(agents.denied_actions('agy', DENIED_ENVELOPE), DENIED)

    def test_the_escalation_cites_them_without_fallback(self):
        cli = FakeCLI(gemini_4_argon_high=(0, DENIED_ENVELOPE, JETSKI))
        gh = FakeGH()
        with tempfile.TemporaryDirectory() as tmp:
            d = Dispatcher(NO_FALLBACK, gh, FakeWS(), agents.AgentRunner(tmp, 10, run=cli),
                           log=lambda _: None)
            p = pr()
            p.meta['reviewer'] = 'agy'
            gh.labels[100] = set(p.labels)
            d.snapshot = lambda: Snapshot([p], [], [])
            self.assertEqual(d.tick(), [Action('review', 100)])
        self.assertEqual(cli.models(), [ARGON, ARGON])  # one retry (#180), no fallback
        self.assertEqual(gh.labels[100], {'agent:needs-operator'})
        self.assertIn(f'denied_actions: {DENIED}', gh.comments[-1][1])


# The stop of the level-2 review of PR #159 (#180): agy read the log of its own background task.
READ_JETSKI = ('jetski: no output produced — a tool required the "read_file" permission that '
               'headless mode cannot prompt for, so it was auto-denied.')
READ_TARGET = '{"action":"read_file","display_name":"Read","path":"/home/op/.gemini/tasks/t1.log"}'
READ_ENVELOPE = ('{"status":"SUCCESS","response":"","denied_actions":[{"action":"command",'
                 f'"display_name":"Bash"}},{READ_TARGET}]}}')


class SequenceCLI(FakeCLI):
    """``FakeCLI`` where a model given a list answers, call after call, with the next reply of
    the list, and with a valid review once the list is exhausted."""

    def __init__(self, **replies):
        super().__init__(**{k: v for k, v in replies.items() if not isinstance(v, list)})
        self.queues = {k: list(v) for k, v in replies.items() if isinstance(v, list)}

    def __call__(self, argv, **kw):
        if argv[-1] != '--version':
            model = argv[argv.index('--model') + 1].replace('-', '_')
            queue = self.queues.get(model)
            if queue:
                self.replies[model] = queue.pop(0)
            elif queue is not None:
                self.replies.pop(model, None)
        return super().__call__(argv, **kw)


class PermissionRetryTests(unittest.TestCase):
    """A turn agy ends on a denied permission, with no output, is a failure of the tool: the same
    model runs it once more before the escalation (#180)."""

    def tick(self, cli, cfg=CFG):
        self.gh, self.logs = FakeGH(), []
        with tempfile.TemporaryDirectory() as tmp:
            d = Dispatcher(cfg, self.gh, FakeWS(), agents.AgentRunner(tmp, 10, run=cli),
                           log=self.logs.append)
            p = pr()
            p.meta['reviewer'] = 'agy'
            self.gh.labels[100] = set(p.labels)
            d.snapshot = lambda: Snapshot([p], [], [])
            self.assertEqual(d.tick(), [Action('review', 100)])
            self.log_files = sorted(f.name for f in Path(tmp).glob('*.log'))
        return '\n'.join(self.logs)

    def test_the_runner_classifies_the_denial(self):
        cli = FakeCLI(gemini_4_argon_high=(0, '', READ_JETSKI))
        with tempfile.TemporaryDirectory() as tmp, \
                self.assertRaises(agents.PermissionDenied) as ctx:
            agents.AgentRunner(tmp, 10, run=cli).run(CFG.agent('agy'), 'review', 'p', 'review',
                                                     tmp, 'pr-1')
        self.assertEqual((ctx.exception.permission, ctx.exception.target), ('read_file', None))
        self.assertEqual(str(ctx.exception), f'agy stopped without output: {READ_JETSKI}')
        cli = FakeCLI(gemini_4_argon_high=(0, READ_ENVELOPE, READ_JETSKI))
        with tempfile.TemporaryDirectory() as tmp, \
                self.assertRaises(agents.PermissionDenied) as ctx:
            agents.AgentRunner(tmp, 10, run=cli).run(CFG.agent('agy'), 'review', 'p', 'review',
                                                     tmp, 'pr-1')
        self.assertEqual(ctx.exception.target, READ_TARGET)  # the last action denied
        self.assertEqual(agents.denied_permission(JETSKI), 'command')
        self.assertIsNone(agents.denied_permission('no output'))
        self.assertIsNone(agents.denied_target('claude', READ_ENVELOPE))

    def test_a_denied_permission_is_retried_once_on_the_same_model(self):
        cli = SequenceCLI(gemini_4_argon_high=[(0, '', READ_JETSKI)])
        log = self.tick(cli)
        self.assertEqual(cli.models(), [ARGON, ARGON])
        # The same prompt, with what was denied before the command block (#319).
        first, again = cli.calls[0][1], cli.calls[1][1]
        base = first[:first.index('\n\n## Shell commands\n')]
        self.assertTrue(again.startswith(base + '\n\n## Denied in the previous attempt\n'))
        self.assertTrue(again.endswith(first[len(base):]))
        self.assertIn('denied the `read_file` permission (target non riportato da agy)', again)
        self.assertNotIn('Denied in the previous attempt', first)
        self.assertNotIn('Previous attempt on another model', cli.calls[1][1])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        body = self.gh.comments[0][1]
        self.assertEqual(markdown.read_marker(body, 'review')['verdict'], 'approve')
        self.assertIn('> **Model:** Gemini 4 Argon\n', body)
        self.assertIn(f'agy: model {ARGON} denied the "read_file" permission '
                      '(target non riportato da agy); retrying once', log)
        # Each attempt keeps its own log.
        self.assertEqual(len(self.log_files), 2)
        self.assertEqual(sum('-retry-' in name for name in self.log_files), 1)

    def test_a_second_denial_escalates_citing_the_permission(self):
        cli = FakeCLI(gemini_4_argon_high=(0, READ_ENVELOPE, READ_JETSKI))
        log = self.tick(cli, NO_FALLBACK)
        self.assertEqual(cli.models(), [ARGON, ARGON])  # once more, then no fallback
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        body = self.gh.comments[-1][1]
        self.assertIn(f'Il passo `review` è fallito: agy stopped without output: {READ_JETSKI}',
                      body)
        self.assertIn('permission `read_file` denied again after one retry; model '
                      f'`{ARGON}`; target {READ_TARGET}', body)
        self.assertNotIn(HYDROGEN, body)
        self.assertIn(f'denied the "read_file" permission ({READ_TARGET}); retrying once', log)
        self.assertIn(f'denied the "read_file" permission again ({READ_TARGET})', log)

    def test_a_denial_on_the_fallback_model_is_retried_too(self):
        cli = SequenceCLI(gemini_4_argon_high=(1, '', QUOTA),
                          gemini_4_hydrogen_high=[(0, '', READ_JETSKI), (0, '', READ_JETSKI)])
        self.tick(cli)
        self.assertEqual(cli.models(), [ARGON, HYDROGEN, HYDROGEN])
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        body = self.gh.comments[-1][1]
        self.assertIn('the last model tried for `agy` failed', body)
        self.assertIn(f'- `{HYDROGEN}`: agy stopped without output: {READ_JETSKI}; permission '
                      '`read_file` denied again after one retry', body)

    def test_other_failures_keep_todays_path(self):
        # Quota: the fallback, no retry on the model.
        cli = FakeCLI(gemini_4_argon_high=(1, '', QUOTA))
        log = self.tick(cli)
        self.assertEqual(cli.models(), [ARGON, HYDROGEN])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        self.assertNotIn('retrying once', log)
        # A non-zero exit, even with the denial on stderr, or a stop without a named permission:
        # escalation at the first failure.
        cases = {'exit 3': ((3, '', READ_JETSKI), f'agy exited with 3: {READ_JETSKI}'),
                 'exit 1': ((1, '', 'segmentation fault'), 'agy exited with 1.'),
                 'silent': ((0, '', ''), 'agy stopped without output: no output.')}
        for name, (reply, message) in cases.items():
            with self.subTest(name):
                cli = FakeCLI(gemini_4_argon_high=reply)
                log = self.tick(cli)
                self.assertEqual(cli.models(), [ARGON])
                self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
                body = self.gh.comments[-1][1]
                self.assertIn(f'Il passo `review` è fallito: {message}', body)
                self.assertNotIn('after one retry', body)
                self.assertNotIn('retrying once', log)


class PreflightPermissionRetryTests(unittest.TestCase):
    """The readiness check run by agy gets the same single retry on a denied permission (#180)."""

    def tick(self, *outputs):
        cfg = dataclasses.replace(CFG, preflight='agy')
        self.assertNotEqual(cfg.implementer, 'agy')
        self.gh, self.ws, self.logs, self.labels = IssueGH(), FakeWS(), [], []
        self.runner = FakeRunner(*outputs)
        run = self.runner.run

        def recording(agent, mode, prompt, schema_name, workdir, label):
            self.labels.append(label)  # the label names the turn's log
            return run(agent, mode, prompt, schema_name, workdir, label)
        self.runner.run = recording
        d = Dispatcher(cfg, self.gh, self.ws, self.runner, log=self.logs.append)
        self.assertEqual([a.kind for a in d.tick()], ['preflight'])
        return '\n'.join(self.logs)

    @staticmethod
    def denied():
        return agents.PermissionDenied(f'agy stopped without output: {READ_JETSKI}', None,
                                       'read_file', READ_TARGET)

    def test_a_denied_check_is_retried_once_in_the_same_checkout(self):
        log = self.tick(self.denied(), checked(True))
        self.assertEqual([c[:3] for c in self.runner.calls], [('agy', 'review', 'preflight')] * 2)
        # The same prompt, told what was denied and which commands are allowed (#319).
        first, again = self.runner.calls[0][3], self.runner.calls[1][3]
        self.assertEqual(again, first + agents.denied_retry_note(CFG.agent('agy'), 'review',
                                                                 self.denied()))
        self.assertIn(f'denied the `read_file` permission (target {READ_TARGET})', again)
        self.assertEqual(self.runner.workdirs, [self.runner.workdirs[0]] * 2)
        self.assertEqual(self.labels, ['issue-7-preflight', 'issue-7-preflight-retry'])
        self.assertIn(f'agy: model {CFG.agent("agy").model} denied the "read_file" permission '
                      f'({READ_TARGET}); retrying once', log)
        self.assertIn('issue-7-preflight', self.ws.removed)
        self.assertNotIn('agent:needs-operator', self.gh.labels[7])
        record = markdown.read_marker(self.gh.thread[-1]['body'], 'preflight')
        self.assertTrue(record['ready'])

    def test_a_second_denial_escalates_citing_the_permission(self):
        # The check runs on the chain of models (#148): after two denials on the primary the
        # fallback runs, with its own retry; denied there too, the check escalates.
        log = self.tick(*[self.denied() for _ in range(4)])
        self.assertEqual(len(self.runner.calls), 4)
        fallback = CFG.agent('agy').chain()[1].model
        self.assertEqual(self.labels, ['issue-7-preflight', 'issue-7-preflight-retry',
                                       f'issue-7-preflight-{fallback}',
                                       f'issue-7-preflight-{fallback}-retry'])
        self.assertIn('agent:needs-operator', self.gh.labels[7])
        body = self.gh.thread[-1]['body']
        self.assertIn(f'agy stopped without output: {READ_JETSKI}', body)
        self.assertIn('permission `read_file` denied again after one retry', body)
        self.assertIn(f'denied the "read_file" permission again ({READ_TARGET})', log)
        self.assertIn('issue-7-preflight', self.ws.removed)

    def test_other_failures_of_the_check_are_not_retried(self):
        # An unavailable model is not retried either: the fallback runs instead (#148,
        # test_devloop_preflight.PreflightFallbackTests).
        for error in (agents.AgentError(f'agy exited with 3: {READ_JETSKI}'),
                      agents.AgentError('agy stopped without output: no output')):
            with self.subTest(error=str(error)):
                log = self.tick(error)
                self.assertEqual(len(self.runner.calls), 1)
                self.assertIn('agent:needs-operator', self.gh.labels[7])
                self.assertNotIn('after one retry', self.gh.thread[-1]['body'])
                self.assertNotIn('retrying once', log)


def block_commands(prompt):
    """The command prefixes listed by the ``## Shell commands`` block at the end of ``prompt``."""
    block = prompt[prompt.index('\n\n## Shell commands\n'):]
    return [line[3:-1] for line in block.splitlines() if line.startswith('- `')]


class PromptBlockTests(unittest.TestCase):
    """agy is told, in its prompt, which commands it may run and how to read files (#155)."""

    def run_agy(self, agent, mode, prompt='the prompt'):
        cli = FakeCLI()
        with tempfile.TemporaryDirectory() as tmp:
            agents.AgentRunner(Path(tmp) / 'logs', 10, run=cli).run(
                agent, mode, prompt, 'review', tmp, f'pr-1-{mode}')
            logged = next((Path(tmp) / 'logs').glob('*.prompt.md')).read_text(encoding='utf-8')
        sent = cli.calls[0][1]
        self.assertEqual(logged, sent)
        return sent

    def test_both_modes_list_exactly_the_rules_of_settings(self):
        custom = dataclasses.replace(CFG.agent('agy'), write_commands=('make check', 'git diff'),
                                     review_commands=('git log',))
        fallback = CFG.agent('agy').chain()[1]
        for agent in (CFG.agent('agy'), custom, fallback):
            for mode in agents.MODES:
                with self.subTest(model=agent.model, mode=mode):
                    sent = self.run_agy(agent, mode)
                    self.assertTrue(sent.startswith('the prompt\n\n## Shell commands\n'))
                    argv, _ = agents.build_command(agent, mode, sent,
                                                   schema_file=schema.schema_path('review'),
                                                   out_file='o')
                    rules = settings(argv)['permissions']['allow']
                    self.assertEqual([f'command({c})' for c in block_commands(sent)], rules)
                    for text in ('own file tools', 'relative to the working directory',
                                 '`grep`, `ls`, `cat`, `find`, `sed`', 'ends the turn with no '
                                 'result'):
                        self.assertIn(text, sent)
        self.assertEqual(block_commands(self.run_agy(custom, 'write')), ['make check', 'git diff'])
        self.assertEqual(block_commands(self.run_agy(custom, 'review')), ['git log'])
        self.assertEqual(block_commands(self.run_agy(CFG.agent('agy'), 'review')),
                         [rule[len('command('):-1] for rule in DEFAULT_RULES])

    def test_the_block_keeps_output_short_and_reads_inside_the_workdir(self):
        """The instructions of #180 come in the same block as the commands, in every mode."""
        custom = dataclasses.replace(CFG.agent('agy'), review_commands=())
        for agent in (CFG.agent('agy'), custom):
            for mode in agents.MODES:
                with self.subTest(commands=bool(agents.agy_commands(agent, mode)), mode=mode):
                    sent = self.run_agy(agent, mode)
                    block = sent[sent.index('\n\n## Shell commands\n'):]
                    self.assertEqual(block, agents.agy_prompt_block(agent, mode))
                    for text in ('output stays short and complete',
                                 'a single test module at a time, with `-q`',
                                 'If an output is truncated, run the command again on a narrower '
                                 'scope',
                                 'Never read logs or files outside the working directory',
                                 'including the logs of your own background tasks'):
                        self.assertIn(text, block)
                    self.assertGreater(block.index('Never read logs'),
                                       block.index('Shell commands'))

    def test_no_commands_says_so(self):
        agy = dataclasses.replace(CFG.agent('agy'), review_commands=())
        sent = self.run_agy(agy, 'review')
        self.assertEqual(block_commands(sent), [])
        self.assertIn('You may run no shell command in this turn.', sent)

    def test_other_agents_prompts_are_unchanged(self):
        for key in ('claude', 'codex'):
            for mode in agents.MODES:
                with self.subTest(key=key, mode=mode), tempfile.TemporaryDirectory() as tmp:
                    seen = []

                    def run(argv, **kw):
                        seen.append(kw.get('input'))
                        raise OSError('not started')
                    with self.assertRaises(agents.AgentError):
                        agents.AgentRunner(Path(tmp) / 'logs', 10, run=run).run(
                            CFG.agent(key), mode, 'the prompt', 'review', tmp, 'pr-1')
                    self.assertEqual(seen, ['the prompt'])
                    self.assertEqual(agents.agy_prompt_block(CFG.agent(key), mode), '')
                    logged = next((Path(tmp) / 'logs').glob('*.prompt.md'))
                    self.assertEqual(logged.read_text(encoding='utf-8'), 'the prompt')


if __name__ == '__main__':
    unittest.main()
