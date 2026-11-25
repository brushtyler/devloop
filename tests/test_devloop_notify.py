"""devloop: the notices to the operator (#593). ``devloop notify`` with ``[notify]`` of the
overlay and the Telegram backend behind a port, once per key and interval; the notices of
devloop (a stopped loop, a refused start, the ping of the alerts on ``[run].alert_issue``); the
mention of ``operator_mentions`` in the requests for a decision; the ``OnFailure=`` unit of the
services, the roles' sudoers and the documents. No test reaches the network: the channel is a
fake, or the opener of the Telegram backend is."""

import contextlib
import dataclasses
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import devloop.__main__ as cli  # noqa: E402
from devloop import config, notify, runloop  # noqa: E402
from devloop.config import ConfigError, NotifyPolicy  # noqa: E402
from devloop.dispatcher import Dispatcher  # noqa: E402
from devloop.markdown import read_marker, with_mentions, publishable  # noqa: E402

from helpers import ssdlc_project  # noqa: E402
from helpers.devloop_fakes import FakeGH  # noqa: E402
from test_devloop import CFG, FakeRunner, FakeWS, implemented  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TOKEN = '123456789:' + 'A' * 35
TELEGRAM = NotifyPolicy(backend='telegram', chat_id='42', token_file='/keys/bot.token',
                        interval=900)


class FakeChannel:
    """A channel that records what it would send, or fails with ``fail``."""

    sent = []
    fail = None

    def __init__(self, policy):
        self.policy = policy

    def send(self, text):
        if FakeChannel.fail is not None:
            raise FakeChannel.fail
        FakeChannel.sent.append(text)


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def fresh_channel():
    FakeChannel.sent, FakeChannel.fail = [], None


class NotifierTests(unittest.TestCase):
    def setUp(self):
        fresh_channel()
        self.dir = tempfile.TemporaryDirectory()
        self.state = Path(self.dir.name)
        self.lines = []
        self.clock = Clock()

    def tearDown(self):
        self.dir.cleanup()

    def notifier(self, policy=TELEGRAM, **kw):
        return notify.Notifier(policy, self.state, repo='o/r', log=self.lines.append,
                               clock=self.clock, channels={'telegram': FakeChannel}, **kw)

    def test_without_a_channel_only_a_line_of_the_log(self):
        n = self.notifier(NotifyPolicy())
        self.assertEqual(n.notify('loop down', key='k'), notify.OFF)
        self.assertEqual(FakeChannel.sent, [])
        self.assertIn('no channel is configured', self.lines[-1])
        self.assertFalse((self.state / notify.STATE_FILE).exists())

    def test_sent_with_the_project_first(self):
        self.assertEqual(self.notifier().notify('loop down', key='k'), notify.SENT)
        self.assertEqual(FakeChannel.sent, ['devloop o/r: loop down'])

    def test_the_same_key_is_not_sent_again_within_the_interval(self):
        n = self.notifier()
        self.assertEqual(n.notify('one', key='k'), notify.SENT)
        self.clock.now += 899
        self.assertEqual(n.notify('two', key='k'), notify.SKIPPED)
        self.assertEqual(n.notify('other', key='j'), notify.SENT)
        self.clock.now += 1
        self.assertEqual(n.notify('three', key='k'), notify.SENT)
        self.assertEqual(FakeChannel.sent, ['devloop o/r: one', 'devloop o/r: other',
                                            'devloop o/r: three'])

    def test_the_interval_is_configurable(self):
        n = self.notifier(dataclasses.replace(TELEGRAM, interval=60))
        n.notify('one', key='k')
        self.clock.now += 60
        self.assertEqual(n.notify('two', key='k'), notify.SENT)

    def test_without_a_key_the_hash_of_the_message(self):
        n = self.notifier()
        self.assertEqual(n.notify('same'), notify.SENT)
        self.assertEqual(n.notify('same'), notify.SKIPPED)
        self.assertEqual(n.notify('different'), notify.SENT)
        keys = json.loads((self.state / notify.STATE_FILE).read_text())['keys']
        self.assertIn(notify.key_of('same'), keys)

    def test_a_failed_attempt_counts_and_is_never_retried(self):
        FakeChannel.fail = notify.NotifyError('HTTP 502')
        n = self.notifier()
        self.assertEqual(n.notify('down', key='k'), notify.FAILED)
        self.assertIn('not sent on telegram (HTTP 502): no retry', self.lines[-1])
        FakeChannel.fail = None
        self.assertEqual(n.notify('down', key='k'), notify.SKIPPED)
        self.assertEqual(FakeChannel.sent, [])

    def test_an_unexpected_error_is_logged_by_its_class_and_never_raised(self):
        FakeChannel.fail = RuntimeError(f'https://api.telegram.org/bot{TOKEN}/sendMessage')
        self.assertEqual(self.notifier().notify('down', key='k'), notify.FAILED)
        self.assertNotIn(TOKEN, '\n'.join(self.lines))
        self.assertIn('RuntimeError', self.lines[-1])

    def test_the_state_is_shared_by_the_callers_of_the_project(self):
        self.notifier().notify('down', key='k')
        self.assertEqual(self.notifier().notify('down', key='k'), notify.SKIPPED)
        mode = (self.state / notify.STATE_FILE).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_force_sends_whatever_the_interval(self):
        n = self.notifier()
        n.notify('t', key='test')
        self.assertEqual(n.notify('t', key='test', force=True), notify.SENT)

    def test_a_state_that_cannot_be_read_lets_the_notice_go(self):
        (self.state / notify.STATE_FILE).write_text('not json')
        self.assertEqual(self.notifier().notify('down', key='k'), notify.SENT)
        n = notify.Notifier(TELEGRAM, self.state / 'missing', log=self.lines.append,
                            clock=self.clock, channels={'telegram': FakeChannel})
        self.assertEqual(n.notify('down', key='k'), notify.SENT)
        self.assertIn('sent without the deduplication', '\n'.join(self.lines))

    def test_old_keys_are_forgotten(self):
        n = self.notifier()
        n.notify('a', key='a')
        self.clock.now += 1000
        n.notify('b', key='b')
        keys = json.loads((self.state / notify.STATE_FILE).read_text())['keys']
        self.assertEqual(sorted(keys), ['b'])

    def test_a_key_not_valid_is_refused(self):
        self.assertEqual(self.notifier().notify('x', key='a b\n'), notify.FAILED)
        self.assertEqual(FakeChannel.sent, [])

    def test_a_long_text_is_cut(self):
        self.notifier().notify('x' * 10000, key='k')
        self.assertEqual(len(FakeChannel.sent[0]), notify.MAX_TEXT)


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / 'bot.token'

    def tearDown(self):
        self.dir.cleanup()

    def write(self, text, mode=0o600):
        self.path.write_text(text)
        os.chmod(self.path, mode)

    def test_a_file_600_of_this_user(self):
        self.write(TOKEN + '\n')
        self.assertEqual(notify.read_token(self.path), TOKEN)

    def test_a_file_others_can_read_is_refused(self):
        self.write(TOKEN, 0o640)
        with self.assertRaisesRegex(notify.NotifyError, 'must be 600') as caught:
            notify.read_token(self.path)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_a_file_of_another_user_is_refused(self):
        self.write(TOKEN)
        with self.assertRaisesRegex(notify.NotifyError, "not of devloop's user"):
            notify.read_token(self.path, uid=os.geteuid() + 1)

    def test_a_link_is_refused(self):
        self.write(TOKEN)
        link = Path(self.dir.name) / 'link'
        link.symlink_to(self.path)
        with self.assertRaisesRegex(notify.NotifyError, 'cannot be opened'):
            notify.read_token(link)

    def test_a_file_without_a_token_never_shows_its_content(self):
        self.write('secret-but-not-a-token')
        with self.assertRaisesRegex(notify.NotifyError, 'does not hold') as caught:
            notify.read_token(self.path)
        self.assertNotIn('secret', str(caught.exception))


class Response:
    def __init__(self, body):
        self.body = body

    def read(self, size=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TelegramTests(unittest.TestCase):
    def channel(self, answer):
        self.requests = []

        def opener(request, timeout):
            self.requests.append((request, timeout))
            if isinstance(answer, Exception):
                raise answer
            return Response(answer)
        return notify.TelegramChannel(TELEGRAM, opener=opener, token=lambda path: TOKEN)

    def test_one_send_message_to_the_chat(self):
        self.channel(b'{"ok": true}').send('ciao')
        request, timeout = self.requests[0]
        self.assertEqual(request.full_url,
                         f'https://api.telegram.org/bot{TOKEN}/sendMessage')
        self.assertEqual(request.get_method(), 'POST')
        self.assertIn(b'chat_id=42', request.data)
        self.assertIn(b'text=ciao', request.data)
        self.assertEqual(timeout, notify.TelegramChannel.TIMEOUT)

    def test_an_answer_not_ok_is_a_failure(self):
        with self.assertRaisesRegex(notify.NotifyError, 'did not confirm'):
            self.channel(b'{"ok": false}').send('ciao')

    def test_an_error_never_carries_the_token(self):
        url = f'https://api.telegram.org/bot{TOKEN}/sendMessage'
        for error, text in ((urllib.error.HTTPError(url, 401, 'Unauthorized', {}, None),
                             'HTTP 401'),
                            (urllib.error.URLError(url), 'URLError'),
                            (TimeoutError(url), 'TimeoutError')):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(notify.NotifyError) as caught:
                    self.channel(error).send('ciao')
                self.assertEqual(str(caught.exception), text)
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)

    def test_the_backend_is_the_only_one_of_the_port(self):
        self.assertEqual(set(notify.CHANNELS), set(config.NOTIFY_BACKENDS))
        self.assertTrue(issubclass(notify.TelegramChannel, notify.Channel))

    def test_the_bot_never_reads_the_messages(self):
        source = Path(notify.__file__).read_text()
        for method in ('getUpdates', 'setWebhook', 'getWebhookInfo'):
            self.assertNotIn(method, source)


class ConfigTests(unittest.TestCase):
    def test_default_no_channel_and_no_mentions(self):
        self.assertEqual(CFG.notify, NotifyPolicy())
        self.assertFalse(CFG.notify.enabled())
        self.assertEqual(CFG.operator_mentions, ())
        self.assertEqual(config.parse_notify({}), NotifyPolicy())

    def test_values(self):
        policy = config.parse_notify({'backend': 'telegram', 'chat_id': -100123,
                                      'token_file': '/keys/bot.token'})
        self.assertEqual(policy, NotifyPolicy('telegram', '-100123', '/keys/bot.token', 900))
        self.assertEqual(config.parse_notify({'backend': 'telegram', 'chat_id': '@my_channel',
                                              'token_file': '/k', 'interval': 60}).interval, 60)

    def test_values_not_valid(self):
        good = {'backend': 'telegram', 'chat_id': 42, 'token_file': '/keys/bot.token'}
        for table in ({**good, 'backend': 'ntfy'}, {**good, 'chat_id': 'x y'},
                      {**good, 'chat_id': True}, {**good, 'token_file': 'bot.token'},
                      {**good, 'token': TOKEN}, {**good, 'interval': 0},
                      {**good, 'interval': True}, {'chat_id': 42}, []):
            with self.subTest(table=table), self.assertRaises(ConfigError):
                config.parse_notify(table)

    def test_the_overlay_sets_them(self):
        data = config.apply_overlay(ssdlc_project.data(), {
            'notify': {'backend': 'telegram', 'chat_id': 42, 'token_file': '/k/bot.token'},
            'operator_mentions': ['brushtyler']})
        cfg = config.parse_config(data)
        self.assertEqual(cfg.notify.backend, 'telegram')
        self.assertEqual(cfg.operator_mentions, ('brushtyler',))
        with self.assertRaises(ConfigError):
            config.apply_overlay(ssdlc_project.data(), {'notify': {'other': 1}})

    def test_not_keys_of_the_project_file(self):
        self.assertNotIn('notify', config.PROJECT_KEYS)
        self.assertNotIn('operator_mentions', config.PROJECT_KEYS)

    def test_the_mentions_are_logins(self):
        for value in (['@brushtyler'], ['a b'], ['bot[bot]'], 'brushtyler', [1]):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                config.parse_operator_mentions({'operator_mentions': value})
        self.assertEqual(config.parse_operator_mentions({'operator_mentions': ['a', 'b', 'a']}),
                         ('a', 'b'))


def overlay(directory, text):
    path = Path(directory) / 'config.local.toml'
    path.write_text(text)
    return path


NOTIFY_TABLE = ('repo = "o/r"\nlanguage = "Italian"\n'
                '[notify]\nbackend = "telegram"\nchat_id = 42\ntoken_file = "/k/bot.token"\n')


class FromOverlayTests(unittest.TestCase):
    def test_the_overlay_alone(self):
        with tempfile.TemporaryDirectory() as d:
            n, language = notify.from_overlay(overlay(d, NOTIFY_TABLE), log=lambda m: None)
            self.assertTrue(n.enabled())
            self.assertEqual((n.repo, language, n.state_dir), ('o/r', 'Italian', Path(d)))

    def test_without_the_overlay_or_with_a_table_not_valid_no_channel(self):
        lines = []
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(notify.from_overlay(None)[0].enabled())
            self.assertFalse(notify.from_overlay(Path(d) / 'missing.toml')[0].enabled())
            bad = overlay(d, '[notify]\nbackend = "ntfy"\n')
            self.assertFalse(notify.from_overlay(bad, log=lines.append)[0].enabled())
            self.assertIn('[notify] is not valid', lines[-1])

    def test_the_language_of_the_project_file(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'project.toml').write_text('language = "Italian"\n')
            path = overlay(d, '[notify]\nbackend = "telegram"\nchat_id = 1\n'
                              'token_file = "/k"\n')
            self.assertEqual(notify.from_overlay(path)[1], 'Italian')


class CommandTests(unittest.TestCase):
    def setUp(self):
        fresh_channel()
        self.dir = tempfile.TemporaryDirectory()
        self.lines = []
        patches = (mock.patch.dict(notify.CHANNELS, {'telegram': FakeChannel}),
                   mock.patch.object(cli, 'log', self.lines.append))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.dir.cleanup()

    def run_cli(self, *argv, text=NOTIFY_TABLE):
        path = overlay(self.dir.name, text)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.main(['--local-config', str(path), 'notify', *argv])
        return caught.exception.code

    def test_a_message_is_sent_once_per_key(self):
        self.assertEqual(self.run_cli('il servizio è giù', '--key', 'host'), 0)
        self.assertEqual(self.run_cli('il servizio è ancora giù', '--key', 'host'), 0)
        self.assertEqual(FakeChannel.sent, ['devloop o/r: il servizio è giù'])
        self.assertTrue((Path(self.dir.name) / notify.STATE_FILE).exists())

    def test_without_notify_a_line_and_exit_code_0(self):
        self.assertEqual(self.run_cli('x', text='repo = "o/r"\n'), 0)
        self.assertIn('no channel is configured', self.lines[-1])
        self.assertEqual(FakeChannel.sent, [])

    def test_a_failed_send_exits_with_1(self):
        FakeChannel.fail = notify.NotifyError('HTTP 502')
        self.assertEqual(self.run_cli('x'), 1)

    def test_the_test_ignores_the_interval_and_needs_a_channel(self):
        self.assertEqual(self.run_cli('--test'), 0)
        self.assertEqual(self.run_cli('--test'), 0)
        self.assertEqual(FakeChannel.sent, ['devloop o/r: Prova degli avvisi di devloop: il '
                                            'canale funziona.'] * 2)
        self.assertEqual(self.run_cli('--test', text='repo = "o/r"\n'), 1)
        FakeChannel.fail = notify.NotifyError('HTTP 401')
        self.assertEqual(self.run_cli('--test'), 1)

    def test_the_service_failed(self):
        self.assertEqual(self.run_cli('--service-failed', 'widget'), 0)
        self.assertIn('progetto widget', FakeChannel.sent[0])
        state = json.loads((Path(self.dir.name) / notify.STATE_FILE).read_text())
        self.assertIn('service-failed:widget', state['keys'])

    def test_arguments_not_valid(self):
        for argv in ((), ('x', '--test'), ('x', '--key', 'a b'), ('   ',),
                     ('--service-failed', '../x')):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cli(*argv), 2)
        self.assertEqual(FakeChannel.sent, [])


class RefusedStartTests(unittest.TestCase):
    def setUp(self):
        fresh_channel()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = overlay(self.dir.name, NOTIFY_TABLE)
        patches = (mock.patch.dict(notify.CHANNELS, {'telegram': FakeChannel}),
                   mock.patch.object(cli, 'log', lambda m: None))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_cli(self, command, error):
        with mock.patch.object(cli, 'build', side_effect=error), \
                self.assertRaises(SystemExit) as caught:
            cli.main(['--local-config', str(self.path), command])
        return caught.exception.code

    def test_a_refused_start_of_run_is_notified_once_per_interval(self):
        code = self.run_cli('run', ConfigError('trusted_users: at least one'))
        self.assertEqual(code, 'devloop: invalid config: trusted_users: at least one')
        self.assertEqual(len(FakeChannel.sent), 1)
        self.assertIn('`devloop run` non è partito, o si è fermato: devloop: invalid config: '
                      'trusted_users', FakeChannel.sent[0])
        self.run_cli('run', ConfigError('trusted_users: at least one'))  # a restart of systemd
        self.assertEqual(len(FakeChannel.sent), 1)

    def test_a_crash_names_only_the_class_of_its_error(self):
        with mock.patch.object(cli, 'build', side_effect=KeyError(f'/home/x {TOKEN}')), \
                self.assertRaises(KeyError):
            cli.main(['--local-config', str(self.path), 'run'])
        self.assertIn('unexpected error KeyError', FakeChannel.sent[0])
        self.assertNotIn(TOKEN, FakeChannel.sent[0])

    def test_only_run(self):
        self.run_cli('tick', ConfigError('x'))
        self.assertEqual(FakeChannel.sent, [])

    def test_an_exit_code_is_not_a_refusal(self):
        with mock.patch.object(cli, '_command', side_effect=SystemExit(runloop.FAILED)), \
                self.assertRaises(SystemExit):
            cli.main(['--local-config', str(self.path), 'run'])
        self.assertEqual(FakeChannel.sent, [])


class Recorder:
    """A notifier that records its notices."""

    def __init__(self):
        self.notices = []

    def notify(self, message, key=None, force=False):
        self.notices.append((key, message))
        return notify.SENT


class StoppedLoopTests(unittest.TestCase):
    def loop(self, alert, notifier, issue=55):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        clock = Clock(0.0)

        def sleep(seconds):
            clock.now += seconds

        def tick():
            raise RuntimeError('boom')
        return runloop.Loop(tick, lambda *a: 0, Path(self.dir.name) / 'STOP', log=lambda m: None,
                            alert=alert, alert_issue=issue, clock=clock, sleep=sleep,
                            language='Italian', notify=notifier)

    def test_the_notice_names_the_issue_of_the_alert(self):
        recorder, published = Recorder(), []
        self.assertEqual(self.loop(published.append, recorder).run(), runloop.FAILED)
        self.assertEqual(len(published), 1)
        [(key, text)] = recorder.notices
        self.assertEqual(key, 'run-stopped')
        self.assertIn('3 tick di fila sono falliti', text)
        self.assertIn('issue #55', text)

    def test_without_the_alert_the_notice_says_the_log(self):
        def fails(body):
            raise RuntimeError('HTTP 502')
        for alert in (None, fails):
            with self.subTest(alert=alert):
                recorder = Recorder()
                self.loop(alert, recorder).run()
                [(key, text)] = recorder.notices
                self.assertNotIn('#55', text)
                self.assertIn('log locale', text)


class DispatcherTests(unittest.TestCase):
    def dispatcher(self, cfg=CFG, notifier=None):
        self.lines = []
        self.gh = FakeGH()
        return Dispatcher(cfg, self.gh, FakeWS(), FakeRunner(implemented()),
                          log=self.lines.append, notifier=notifier)

    def test_a_request_to_the_operator_mentions_operator_mentions(self):
        d = self.dispatcher(dataclasses.replace(CFG, operator_mentions=('op-one', 'optwo')))
        d._escalate(7, 'Serve una decisione.', cls='failure')
        body = self.gh.comments[-1][1]
        self.assertIn("Per l'operatore: @op-one @optwo", body)
        self.assertEqual(read_marker(body, 'notice')['to'], 'operator')
        self.assertTrue(body.rstrip().endswith('-->'))

    def test_without_logins_nobody_and_a_line_of_the_log(self):
        d = self.dispatcher()
        d._escalate(7, 'Serve una decisione.', cls='failure')
        self.assertNotIn('@', self.gh.comments[-1][1].split('-->')[-1])
        self.assertNotIn("Per l'operatore", self.gh.comments[-1][1])
        self.assertTrue(any('no operator_mentions configured' in line for line in self.lines))

    def test_the_supervisor_is_not_the_operator(self):
        d = self.dispatcher(dataclasses.replace(CFG, operator_mentions=('op',)))
        d._escalate(7, 'Decide il supervisore.', cls='failure',
                    supervised={'step': 'implement', 'key': 'k'})
        self.assertNotIn('@op', self.gh.comments[-1][1])

    def test_an_alert_on_alert_issue_is_pinged_with_its_number(self):
        recorder = Recorder()
        d = self.dispatcher(notifier=recorder)
        d._alert(55, publishable('alert'), ['journal_size:1'])
        self.assertEqual(self.gh.comments[-1][0], 55)
        self.assertEqual(recorder.notices,
                         [('alert-issue:55', "Nuovo allarme di devloop sull'issue #55.")])

    def test_without_a_notifier_nothing(self):
        d = self.dispatcher()
        d._alert(55, publishable('alert'), ['journal_size:1'])
        self.assertEqual(len(self.gh.comments), 1)

    def test_the_warnings_of_the_start_ping_too(self):
        recorder = Recorder()
        with tempfile.TemporaryDirectory() as d:
            cli.agent_user_warning(True, 55, d, FakeGH(), out=lambda m: None,
                                   notifier=recorder)
        self.assertEqual(recorder.notices[0][0], 'alert-issue:55')


class MentionTests(unittest.TestCase):
    def test_the_markers_stay_at_the_end(self):
        body = publishable('## Title\n\ntext', 'notice', {'event': 'escalation'})
        out = with_mentions(body, ('a', 'b'), 'it')
        self.assertIn("text\n\nPer l'operatore: @a @b\n\n<!-- devloop:notice:v1", out)
        self.assertEqual(read_marker(out, 'notice'), {'event': 'escalation'})
        self.assertEqual(with_mentions(body, ()), body)
        self.assertTrue(with_mentions('plain', ('a',)).endswith('For the operator: @a\n'))


class StatusTests(unittest.TestCase):
    def test_the_lines_of_status(self):
        lines = cli.attention_lines(CFG)
        self.assertIn('notify: no channel', lines[0])
        self.assertIn('operator mentions: none', lines[1])
        cfg = dataclasses.replace(CFG, notify=TELEGRAM, operator_mentions=('op',))
        self.assertEqual(cli.attention_lines(cfg),
                         ['notify: telegram, the same key at most once every 900 seconds',
                          'operator mentions: @op'])


class HostTests(unittest.TestCase):
    """The ``OnFailure=`` unit of the services and the roles' sudoers (#593, N2 (a))."""

    def read(self, name):
        return (ROOT / 'ops' / 'host' / name).read_text()

    def test_the_units_of_the_service_have_on_failure(self):
        self.assertIn('OnFailure=devloop-failed@@OPS_INSTANCE@.service',
                      self.read('devloop.service'))
        self.assertIn('OnFailure=devloop-failed@%i.service', self.read('devloop@.service'))

    def test_the_failed_unit_runs_devloop_notify(self):
        unit = self.read('devloop-failed@.service')
        self.assertIn('Type=oneshot', unit)
        self.assertIn('ExecStart=@DEVLOOP_COMMAND@ --project %i notify --service-failed %i',
                      unit)
        # No OnFailure= of its own: a failed notice never starts another one.
        directives = [line for line in unit.splitlines() if not line.startswith('#')]
        self.assertFalse([line for line in directives if line.startswith('OnFailure')])

    def test_the_roles_send_only_through_devloop_notify(self):
        for name in ('sudoers-supervisor', 'sudoers-coordinator'):
            with self.subTest(name=name):
                lines = [line.strip() for line in self.read(name).splitlines()]
                self.assertIn('@LAUNCHER_CMD@ notify *', ' '.join(lines))
        self.assertNotIn('notify', self.read('sudoers-agent'))


class DocumentTests(unittest.TestCase):
    def test_security_asks_for_the_private_report(self):
        text = (ROOT / 'SECURITY.md').read_text()
        self.assertIn('private vulnerability reporting', text)
        self.assertIn('never in an issue', text)

    def test_the_guide_describes_the_channels_and_the_bot(self):
        guide = (ROOT / 'docs' / 'devloop' / 'guide.md').read_text()
        for phrase in ('## Notices to the operator', '@BotFather', 'devloop notify --test',
                       'private advisory'):
            self.assertIn(phrase, guide)

    def test_the_runbooks_of_the_roles(self):
        for name in ('supervisor.md', 'coordinator.md', 'host.md'):
            with self.subTest(name=name):
                text = (ROOT / 'docs' / 'devloop' / 'roles' / name).read_text()
                self.assertIn('#593', text)
        coordinator = (ROOT / 'docs' / 'devloop' / 'roles' / 'coordinator.md').read_text()
        self.assertIn('advisory', coordinator)

    def test_the_migration_has_its_section(self):
        self.assertIn('## Avvisi all\'operatore (#593)',
                      (ROOT / 'ops' / 'host' / 'migrate.md').read_text())


if __name__ == '__main__':
    unittest.main()
