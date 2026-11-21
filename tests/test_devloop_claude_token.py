"""devloop: the long-lived OAuth token of the claude turns (#507).

Every claude turn, and only those, gets the token of ``key_dir`` as ``CLAUDE_CODE_OAUTH_TOKEN``;
it never reaches a log, a record, a prompt or the output. From ``rotate_days`` one alert on
``[run].alert_issue``; from ``max_days``, or without a valid date, the claude turns are refused
with an escalation ``configuration``. ``max_days`` beyond the cap of the code stops devloop at
startup."""

import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from devloop import __main__ as cli
from devloop import agents, claudetoken, config, markdown
from devloop.config import ClaudeTokenPolicy, ConfigError, RunPolicy, parse_claude_token
from devloop.dispatcher import Dispatcher
from devloop.ledger import Ledger
from devloop.planning import Action
from helpers import ssdlc_project
from helpers.devloop_fakes import FakeGH
from test_devloop import CFG, FakeWS, raw_review

# Not a pattern the generic cleaning knows (``sk-…``, ``Bearer …``): only devloop's own
# redaction of the token hides it.
TOKEN = 'tok3n' + 'Zq9' * 12
NOW = datetime(2026, 11, 21, 12, tzinfo=timezone.utc).timestamp()
ALERT_ISSUE = 76


def clock():
    return NOW


def token_file(key_dir, issued='2026-11-11', text=TOKEN + '\n', mode=0o600):
    path = Path(key_dir) / f'claude.{issued}.oauth-token'
    path.write_text(text, encoding='utf-8')
    path.chmod(mode)
    return path


class ClaudeCLI:
    """``subprocess.run`` for claude and codex: records the environment of each turn, and
    echoes the token it received everywhere a CLI could, its output included. ``timeout``:
    the turn is stopped after writing the token."""

    def __init__(self, timeout=False):
        self.envs = []
        self.timeout = timeout

    def __call__(self, argv, **kw):
        if argv[-1] == '--version':
            return SimpleNamespace(returncode=0, stdout='2.1.0 (Claude Code)', stderr='')
        env = kw.get('env') or {}
        self.envs.append((argv[0], env))
        token = env.get(claudetoken.ENV, '')
        if self.timeout:
            raise subprocess.TimeoutExpired(argv, 10, output=f'printenv: {token}',
                                            stderr=f'oauth {token}')
        review = dict(raw_review(), summary=f'the environment has {token}')
        if argv[0] == 'codex':
            out_file = Path(argv[argv.index('-o') + 1])
            out_file.write_text(json.dumps(review), encoding='utf-8')
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        out = json.dumps({'type': 'result', 'is_error': False, 'result': f'env {token}',
                          'structured_output': review})
        return SimpleNamespace(returncode=0, stdout=out, stderr=f'debug: token {token}')


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.keys = self.dir / 'keys'
        self.keys.mkdir(mode=0o700)
        self.logs = self.dir / 'logs'
        self.work = self.dir / 'work'
        self.work.mkdir()

    def token(self, **limits):
        return claudetoken.ClaudeToken(self.keys, clock=clock, **limits)

    def runner(self, cli_run, token=None):
        return agents.AgentRunner(self.logs, 10, run=cli_run, env={'PATH': '/usr/bin'},
                                  claude_token=self.token() if token is None else token)

    def logged(self):
        return '\n'.join(p.read_text(encoding='utf-8') for p in self.logs.iterdir())


class TurnTests(Case):
    def test_a_claude_turn_gets_the_token_and_nothing_shows_it(self):
        token_file(self.keys)
        run = ClaudeCLI()
        runner = self.runner(run)
        output = runner.run(CFG.agent('claude'), 'review', 'review the diff', 'review',
                            self.work, 'pr-1')
        [(cli_name, env)] = run.envs
        self.assertEqual((cli_name, env[claudetoken.ENV]), ('claude', TOKEN))
        # The output of the turn, its log, its prompt and the command it ran: never the token.
        self.assertEqual(output['summary'], 'the environment has [REDACTED]')
        self.assertNotIn(TOKEN, json.dumps(output))
        text = self.logged()
        self.assertIn('debug: token [REDACTED]', text)
        self.assertNotIn(TOKEN, text)
        self.assertEqual(len(list(self.logs.glob('*.prompt.md'))), 1)
        self.assertNotIn(TOKEN, repr(runner.last_command))
        # The dispatcher's own environment does not get it either.
        self.assertNotIn(claudetoken.ENV, runner._env)

    def test_only_claude_gets_the_token(self):
        token_file(self.keys)
        run = ClaudeCLI()
        self.runner(run).run(CFG.agent('codex'), 'review', 'p', 'review', self.work, 'pr-1')
        [(cli_name, env)] = run.envs
        self.assertEqual(cli_name, 'codex')
        self.assertNotIn(claudetoken.ENV, env)

    def test_without_the_table_claude_uses_its_own_login(self):
        run = ClaudeCLI()
        runner = agents.AgentRunner(self.logs, 10, run=run, env={'PATH': '/usr/bin'})
        runner.run(CFG.agent('claude'), 'review', 'p', 'review', self.work, 'pr-1')
        self.assertNotIn(claudetoken.ENV, run.envs[0][1])
        self.assertIsNone(cli.claude_token_of(CFG))
        cfg = dataclasses.replace(CFG, claude_token=ClaudeTokenPolicy(enabled=True,
                                                                      rotate_days=10,
                                                                      max_days=20))
        with mock.patch.dict(os.environ, {'DEVLOOP_APP_KEY_DIR': str(self.keys)}):
            built = cli.claude_token_of(cfg)
        self.assertEqual((built.key_dir, built.rotate_days, built.max_days),
                         (self.keys, 10, 20))

    def test_a_stopped_turn_shows_no_token(self):
        token_file(self.keys)
        with self.assertRaises(agents.AgentError) as ctx:
            self.runner(ClaudeCLI(timeout=True)).run(CFG.agent('claude'), 'review', 'p',
                                                     'review', self.work, 'pr-1')
        self.assertTrue(ctx.exception.timed_out)
        self.assertIn('[REDACTED]', ctx.exception.tail)
        self.assertNotIn(TOKEN, ctx.exception.tail + str(ctx.exception) + self.logged())

    def test_a_failure_message_shows_no_token(self):
        token_file(self.keys)

        def fails(argv, **kw):
            token = kw['env'][claudetoken.ENV]
            out = json.dumps({'type': 'result', 'is_error': True, 'subtype': 'error',
                              'result': f'401 for {token}'})
            return SimpleNamespace(returncode=1, stdout=out, stderr='')
        with self.assertRaises(agents.AgentError) as ctx:
            self.runner(fails).run(CFG.agent('claude'), 'review', 'p', 'review', self.work,
                                   'pr-1')
        self.assertNotIn(TOKEN, str(ctx.exception) + self.logged())


class AgeTests(Case):
    def refused(self, run=None):
        run = run or ClaudeCLI()
        with self.assertRaises(agents.CredentialRefused) as ctx:
            self.runner(run).run(CFG.agent('claude'), 'write', 'p', 'implement', self.work,
                                 'issue-7')
        self.assertEqual(run.envs, [])  # refused before the CLI starts
        self.assertNotIn(TOKEN, str(ctx.exception))
        return str(ctx.exception)

    def test_the_age_counts_from_the_date_in_the_name(self):
        token_file(self.keys, '2026-11-11')
        status = self.token().status()
        self.assertEqual((status.age, status.problem, status.rotate), (10, '', False))
        self.assertEqual(self.token(rotate_days=10).status().rotate, True)

    def test_from_max_days_the_turns_are_refused(self):
        token_file(self.keys, '2026-08-23')  # 90 days before NOW
        self.assertIn('beyond max_days = 90: expired', self.refused())
        # The day before it is still in use, to rotate.
        path = next(self.keys.iterdir())
        path.rename(self.keys / 'claude.2026-08-24.oauth-token')
        status = self.token().status()
        self.assertEqual((status.age, status.problem, status.rotate), (89, '', True))
        self.assertEqual(self.token().value(), TOKEN)

    def test_a_date_that_is_not_valid_counts_as_expired(self):
        for issued in ('2026-02-30', 'today', '20261101', '2026-12-01'):  # the last: future
            with self.subTest(issued=issued):
                for path in self.keys.iterdir():
                    path.unlink()
                token_file(self.keys, issued)
                message = self.refused()
                self.assertTrue('treated as expired' in message, message)

    def test_a_missing_ambiguous_or_open_token_is_refused(self):
        self.assertIn('no claude token', self.refused())
        token_file(self.keys, '2026-11-10')
        token_file(self.keys, '2026-11-11')
        self.assertIn('2 claude tokens', self.refused())
        (self.keys / 'claude.2026-11-10.oauth-token').unlink()
        token_file(self.keys, '2026-11-11', mode=0o640)
        self.assertIn('readable by group or others', self.refused())
        token_file(self.keys, '2026-11-11', text='\n')
        self.assertIn('the token alone', self.refused())
        token_file(self.keys, '2026-11-11', text='two words\n')
        self.assertIn('the token alone', self.refused())
        (self.keys / 'claude.2026-11-11.oauth-token').unlink()
        target = token_file(self.dir, '2026-11-11')
        (self.keys / target.name).symlink_to(target)
        self.assertIn('a link is refused', self.refused())

    def test_the_cap_holds_whatever_the_caller_passes(self):
        self.assertEqual(claudetoken.MAX_DAYS, 90)
        self.assertEqual(self.token(max_days=400).max_days, 90)


class EscalationTests(Case):
    def test_a_refused_turn_is_a_configuration_escalation_to_the_operator(self):
        token_file(self.keys, '2026-01-01')
        run = ClaudeCLI()
        gh = FakeGH()
        d = Dispatcher(CFG, gh, FakeWS(), self.runner(run), log=lambda m: None)
        with self.assertRaises(agents.CredentialRefused) as ctx:
            d._do_implement(7)
        self.assertEqual(run.envs, [])
        d._escalate_failure(Action('implement', 7), None, ctx.exception)
        body = gh.comments[-1][1]
        notice = markdown.read_marker(body, 'notice')
        self.assertEqual((notice['class'], notice['critical']), ('configuration', True))
        self.assertEqual(gh.labels[7], {'agent:needs-operator'})
        self.assertIn('claude.2026-01-01.oauth-token', body)
        self.assertIn('claude setup-token', body)
        self.assertIn('Token di claude per l\'utente degli agent (#507)', body)
        self.assertNotIn(TOKEN, body)

    def test_a_wrapped_refusal_is_a_configuration_too(self):
        refused = agents.CredentialRefused('claude: turn refused')
        wrapped = agents.ModelsUnavailable('claude', [('m', 'x')])
        wrapped.__cause__ = refused
        gh = FakeGH()
        d = Dispatcher(CFG, gh, FakeWS(), None, log=lambda m: None)
        d._escalate_failure(Action('implement', 7), None, wrapped)
        self.assertEqual(markdown.read_marker(gh.comments[-1][1], 'notice')['class'],
                         'configuration')


class RotationAlertTests(Case):
    def dispatcher(self, alert_issue=ALERT_ISSUE, rotate_days=30, max_days=90, state=None):
        self.lines = []
        self.gh = getattr(self, 'gh', None) or FakeGH()
        cfg = dataclasses.replace(CFG, run=RunPolicy(alert_issue=alert_issue))
        runner = SimpleNamespace(claude_token=self.token(rotate_days=rotate_days,
                                                         max_days=max_days))
        return Dispatcher(cfg, self.gh, None, runner, log=self.lines.append,
                          token_alerts=claudetoken.RotationAlert(state or self.dir / 'state'))

    def alerts(self):
        return [b for n, b in self.gh.comments if n == ALERT_ISSUE]

    def test_before_rotate_days_nothing(self):
        token_file(self.keys, '2026-11-01')
        self.dispatcher()._watch_claude_token()
        self.assertEqual((self.alerts(), self.lines), ([], []))

    def test_from_rotate_days_one_alert_only(self):
        token_file(self.keys, '2026-10-20')  # 32 days
        d = self.dispatcher()
        d._watch_claude_token()
        d._watch_claude_token()
        # A restart reads the record in state_dir: no second alert.
        self.dispatcher()._watch_claude_token()
        [alert] = self.alerts()
        self.assertIn('## Token di claude da ruotare', alert)
        self.assertIn('`claude.2026-10-20.oauth-token`', alert)
        self.assertIn('32 giorni fa', alert)
        self.assertIn('`rotate_days` = 30', alert)
        self.assertIn('`max_days` = 90', alert)
        self.assertIn('claude setup-token', alert)
        self.assertIn('revocato su claude.ai', alert)
        self.assertNotIn(TOKEN, alert)
        # A new token is alerted in its turn, once its age reaches rotate_days.
        next(self.keys.iterdir()).unlink()
        token_file(self.keys, '2026-11-20')
        self.dispatcher()._watch_claude_token()
        self.assertEqual(len(self.alerts()), 1)
        next(self.keys.iterdir()).unlink()
        token_file(self.keys, '2026-10-01')
        self.dispatcher()._watch_claude_token()
        self.assertEqual(len(self.alerts()), 2)

    def test_an_expired_token_is_alerted_and_logged(self):
        token_file(self.keys, '2026-06-01')
        d = self.dispatcher()
        d._watch_claude_token()
        d._watch_claude_token()
        [alert] = self.alerts()
        self.assertIn('i turni di claude sono rifiutati', alert)
        warnings = [line for line in self.lines if line.startswith('WARNING')]
        self.assertEqual(len(warnings), 1)
        self.assertIn('expired', warnings[0])

    def test_without_alert_issue_a_warning_in_the_log_once(self):
        token_file(self.keys, '2026-10-01')
        d = self.dispatcher(alert_issue=0)
        d._watch_claude_token()
        d._watch_claude_token()
        self.assertEqual(self.gh.comments, [])
        [line] = self.lines
        self.assertIn('no [run].alert_issue is configured', line)

    def test_a_failed_alert_is_tried_again(self):
        token_file(self.keys, '2026-10-01')
        d = self.dispatcher()
        with mock.patch.object(self.gh, 'comment', side_effect=RuntimeError('down')):
            d._watch_claude_token()
        self.assertEqual(self.alerts(), [])
        d._watch_claude_token()
        self.assertEqual(len(self.alerts()), 1)

    def test_without_the_table_nothing_is_read(self):
        d = Dispatcher(CFG, FakeGH(), None, SimpleNamespace(), log=self.fail)
        d._watch_claude_token()


class ConfigTests(unittest.TestCase):
    def test_defaults_and_values(self):
        self.assertEqual(parse_claude_token({}), ClaudeTokenPolicy(False, 30, 90))
        self.assertEqual(CFG.claude_token, ClaudeTokenPolicy(False, 30, 90))
        self.assertEqual(parse_claude_token({'enabled': True, 'rotate_days': 7, 'max_days': 60}),
                         ClaudeTokenPolicy(True, 7, 60))

    def test_max_days_beyond_the_cap_and_other_bad_values_are_refused(self):
        for table in ({'max_days': 91}, {'max_days': 0}, {'rotate_days': 0},
                      {'rotate_days': 40, 'max_days': 30}, {'enabled': 'yes'},
                      {'max_days': True}, {'days': 3}):
            with self.subTest(table=table), self.assertRaises(ConfigError):
                parse_claude_token(table)
        with self.assertRaisesRegex(ConfigError, 'beyond 90, the cap of the code'):
            parse_claude_token({'max_days': 365})

    def test_the_overlay_may_only_lower_max_days(self):
        data = ssdlc_project.data()
        lower = config.apply_overlay(data, {'claude_token': {'enabled': True, 'max_days': 60}})
        self.assertEqual(config.parse_config(lower).claude_token.max_days, 60)
        with self.assertRaises(ConfigError):
            config.parse_config(config.apply_overlay(data, {'claude_token': {'max_days': 120}}))

    def test_max_days_beyond_the_cap_stops_devloop_at_startup(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        base = tmp / 'config.toml'
        base.write_text(ssdlc_project.text(), encoding='utf-8')
        local = tmp / 'config.local.toml'
        local.write_text('[claude_token]\nenabled = true\nmax_days = 91\n', encoding='utf-8')
        build = mock.Mock(side_effect=AssertionError('built'))
        for command in ('tick', 'run'):
            with self.subTest(command=command), mock.patch.object(cli, 'log'), \
                    mock.patch.object(cli, 'Dispatcher', build), \
                    self.assertRaises(SystemExit) as raised:
                cli.main(['--config', str(base), '--local-config', str(local), command])
            self.assertIn('invalid config: claude_token.max_days: 91 is beyond 90',
                          str(raised.exception))
        build.assert_not_called()


if __name__ == '__main__':
    unittest.main()
