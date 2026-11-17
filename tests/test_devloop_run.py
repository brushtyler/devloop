"""devloop: the continuous loop ``devloop run`` (#177), with simulated ticks and watcher."""

import dataclasses
import os
import shutil
import signal
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import devloop.__main__ as cli
from devloop import config, pin, runloop
from devloop.config import ConfigError, RunPolicy
from devloop.github import GitHubError
from devloop.watch import NOTHING_NEW

REPO = Path(__file__).resolve().parent.parent


class Clock:
    """A monotonic clock that advances only when the loop sleeps."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class LoopCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.stop = self.tmp / 'STOP'
        self.clock = Clock()
        self.logs = []
        self.events = []  # ('tick', n) and ('watch', timeout), in order
        self.watch_results = []
        self.alerts = []

    def loop(self, ticks, alert=True, idle_timeout=900, alert_issue=42):
        """A loop whose ticks follow ``ticks``: a list of actions to return or an exception to
        raise, then a STOP file once they run out."""
        ticks = list(ticks)

        def tick():
            self.events.append(('tick', len([e for e in self.events if e[0] == 'tick']) + 1))
            outcome = ticks.pop(0)
            if not ticks:
                self.stop.touch()  # no tick after the scripted ones
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome() if callable(outcome) else outcome

        def watch(timeout, sleep, check):
            self.events.append(('watch', timeout))
            result = self.watch_results.pop(0) if self.watch_results else NOTHING_NEW
            if callable(result):
                return result(sleep)
            return result

        return runloop.Loop(tick, watch, self.stop, log=self.logs.append,
                            alert=self.alerts.append if alert else None, alert_issue=alert_issue,
                            idle_timeout=idle_timeout, clock=self.clock, sleep=self.clock.sleep)


class TickAndWaitTests(LoopCase):
    def test_a_tick_with_actions_is_followed_at_once_by_another(self):
        code = self.loop([['implement'], ['review'], []]).run()
        self.assertEqual(code, 0)
        self.assertEqual(self.events, [('tick', 1), ('tick', 2), ('tick', 3)])
        self.assertEqual(self.clock.slept, [])

    def test_a_tick_without_actions_waits_for_news_or_the_timeout(self):
        self.watch_results = [0, NOTHING_NEW]
        code = self.loop([[], [], ['implement'], []], idle_timeout=600).run()
        self.assertEqual(code, 0)
        self.assertEqual(self.events, [('tick', 1), ('watch', 600), ('tick', 2), ('watch', 600),
                                       ('tick', 3), ('tick', 4)])
        self.assertIn('news on GitHub: next tick', self.logs)
        self.assertIn('no news: next tick', self.logs)

    def test_the_watcher_sleeps_through_the_loop(self):
        def wait(sleep):
            sleep(60)
            sleep(60)
            return NOTHING_NEW
        self.watch_results = [wait]
        self.loop([[], []]).run()
        self.assertEqual(sum(self.clock.slept), 120)

    def test_a_failed_watcher_waits_the_rest_of_the_timeout(self):
        def broken(sleep):
            sleep(100)
            raise OSError('state file')
        self.watch_results = [broken]
        code = self.loop([[], []], idle_timeout=300).run()
        self.assertEqual(code, 0)
        self.assertEqual(sum(self.clock.slept), 300)
        self.assertEqual([e[0] for e in self.events], ['tick', 'watch', 'tick'])
        self.assertIn('waiting for news failed (OSError): next tick at the timeout', self.logs)

    def test_a_new_pin_propagates_to_restart_the_process(self):
        with self.assertRaises(pin.Repinned):
            self.loop([pin.Repinned(self.tmp / 'new'), []]).run()


class StopTests(LoopCase):
    def test_sigterm_stops_the_loop_after_the_tick_in_progress(self):
        def tick_with_sigterm():
            os.kill(os.getpid(), signal.SIGTERM)
            self.events.append(('tick ended', None))
            return ['implement']  # with actions: without the stop, another tick would follow
        before = signal.getsignal(signal.SIGTERM)
        code = self.loop([tick_with_sigterm, ['review']]).run()
        self.assertEqual(code, 0)
        self.assertEqual(self.events, [('tick', 1), ('tick ended', None)])
        self.assertIn('stop requested (SIGTERM): no further tick', self.logs)
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_sigint_stops_the_loop_too(self):
        def tick_with_sigint():
            os.kill(os.getpid(), signal.SIGINT)
            return []
        code = self.loop([tick_with_sigint, []]).run()
        self.assertEqual(code, 0)
        self.assertEqual(self.events, [('tick', 1)])
        self.assertIn('stop requested (SIGINT): no further tick', self.logs)

    def test_the_stop_file_stops_the_loop_after_the_tick_in_progress(self):
        def tick_then_stop():
            self.stop.touch()
            return ['implement']
        code = self.loop([tick_then_stop, ['review']]).run()
        self.assertEqual(code, 0)
        self.assertEqual(self.events, [('tick', 1)])
        self.assertIn('stop requested (the STOP file): no further tick', self.logs)
        self.assertTrue(self.stop.exists())  # never consumed

    def test_with_the_stop_file_at_start_no_tick_runs(self):
        self.stop.touch()
        code = self.loop([['implement']]).run()
        self.assertEqual(code, 0)
        self.assertEqual(self.events, [])
        self.assertTrue(self.stop.exists())
        self.assertIn('no tick runs until the operator removes it', self.logs[0])

    def test_a_stop_cuts_the_wait_for_news_short(self):
        def wait(sleep):
            sleep(10)
            self.stop.touch()
            sleep(60)  # cut short: Stopped
            self.fail('the wait went on after the stop')
        self.watch_results = [wait]
        code = self.loop([[], ['never']]).run()
        self.assertEqual(code, 0)
        self.assertEqual([e[0] for e in self.events], ['tick', 'watch'])
        self.assertEqual(sum(self.clock.slept), 10)

    def test_a_stop_during_a_tick_that_repins_ends_the_loop_without_a_restart(self):
        def tick_with_sigterm_then_pin():
            os.kill(os.getpid(), signal.SIGTERM)
            raise pin.Repinned(self.tmp / 'new')
        code = self.loop([tick_with_sigterm_then_pin, ['review']]).run()
        self.assertEqual(code, 0)
        self.assertEqual(self.events, [('tick', 1)])
        self.assertIn('stop requested (SIGTERM): no restart from the new pin, no further tick',
                      self.logs)

    def test_the_stop_file_during_a_tick_that_repins_ends_the_loop_too(self):
        def tick_with_stop_then_pin():
            self.stop.touch()
            raise pin.Repinned(self.tmp / 'new')
        code = self.loop([tick_with_stop_then_pin, ['review']]).run()
        self.assertEqual((code, self.events), (0, [('tick', 1)]))

    def test_a_stop_cuts_the_backoff_short(self):
        def stop_during_backoff():
            self.stop.touch()
            raise GitHubError('HTTP 502')
        code = self.loop([stop_during_backoff, []]).run()
        self.assertEqual(code, 0)
        self.assertEqual(self.events, [('tick', 1)])
        self.assertEqual(self.clock.slept, [])


class BackoffTests(LoopCase):
    def test_failed_ticks_wait_1_then_5_minutes_and_the_third_stops_with_an_alert(self):
        failures = [GitHubError('HTTP 502 bad gateway'), GitHubError('error connecting to x'),
                    RuntimeError('/home/devloop/secret path')]
        code = self.loop(failures + [[]]).run()
        self.assertEqual(code, runloop.FAILED)
        self.assertEqual([e for e in self.events if e[0] == 'tick'],
                         [('tick', 1), ('tick', 2), ('tick', 3)])
        # 60 s after the first failure, 300 s after the second, none after the third.
        self.assertEqual(sum(self.clock.slept), 360)
        waits = [line for line in self.logs if line.startswith('next tick in')]
        self.assertEqual(waits, ['next tick in 60 seconds', 'next tick in 300 seconds'])
        (body,) = self.alerts
        self.assertTrue(body.startswith('> [!NOTE]\n> **Bot:** devloop dispatcher\n'))
        self.assertIn('`server error`, `network`, `RuntimeError`', body)
        self.assertNotIn('/home/devloop', body)
        self.assertNotIn('bad gateway', body)
        self.assertIn('alert published on issue #42', self.logs[-1])

    def test_a_successful_tick_resets_the_count(self):
        error = GitHubError('HTTP 502')
        code = self.loop([error, error, [], error, error, []]).run()
        self.assertEqual(code, 0)
        self.assertEqual(self.alerts, [])
        # The simulated watcher returns at once: only the backoffs sleep.
        self.assertEqual(sum(self.clock.slept), 60 + 300 + 60 + 300)
        self.assertEqual(len([e for e in self.events if e[0] == 'tick']), 6)

    def test_an_action_that_fails_is_not_a_failed_tick(self):
        # The tick escalates its failed actions itself and returns normally.
        code = self.loop([['implement'], ['implement'], ['implement'], []]).run()
        self.assertEqual((code, self.alerts), (0, []))

    def test_without_alert_issue_the_alert_goes_to_the_log_and_the_exit_code_is_not_zero(self):
        error = GitHubError('HTTP 401 bad credentials')
        code = self.loop([error, error, error, []], alert=False).run()
        self.assertEqual(code, runloop.FAILED)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.logs[-1], 'ALERT: 3 consecutive ticks failed (authentication, '
                                        'authentication, authentication): the loop stops; no '
                                        '[run].alert_issue is configured')

    def test_an_alert_that_cannot_be_published_is_tried_once_and_logged(self):
        attempts = []

        def failing(body):
            attempts.append(body)
            raise GitHubError('HTTP 403 Resource not accessible by integration, token ghs_x')
        error = GitHubError('HTTP 502')
        loop = self.loop([error, error, error, []])
        loop.alert = failing
        self.assertEqual(loop.run(), runloop.FAILED)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(len([e for e in self.events if e[0] == 'tick']), 3)
        self.assertIn('could not be published (permission)', self.logs[-1])
        self.assertNotIn('ghs_x', self.logs[-1])


class LockTests(unittest.TestCase):
    """The dispatcher lock: a manual tick and the loop never overlap."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dispatcher = SimpleNamespace(tick=mock.Mock(return_value=[]))
        patches = [mock.patch.object(cli, 'build', return_value=(SimpleNamespace(), self.tmp,
                                                                 self.dispatcher)),
                   mock.patch.object(cli, 'log')]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_manual_tick_is_refused_while_the_loop_runs(self):
        refused = []

        def loop(cfg, state, dispatcher, timeout):
            with self.assertRaisesRegex(SystemExit, 'another dispatcher is running'):
                cli.main(['tick'])
            refused.append(timeout)
            return 0
        with mock.patch.object(cli, 'run_loop', loop):
            cli.main(['run'])
        self.assertEqual(refused, [900])
        self.dispatcher.tick.assert_not_called()

    def test_the_loop_does_not_start_while_a_manual_tick_runs(self):
        loop = mock.Mock(return_value=0)

        def tick(dry_run=False):
            with self.assertRaisesRegex(SystemExit, 'another dispatcher is running'):
                cli.main(['run'])
            return []
        self.dispatcher.tick = tick
        with mock.patch.object(cli, 'run_loop', loop):
            cli.main(['tick'])
        loop.assert_not_called()

    def test_a_loop_stopped_by_failures_exits_with_its_code(self):
        with mock.patch.object(cli, 'run_loop', return_value=runloop.FAILED), \
                self.assertRaises(SystemExit) as raised:
            cli.main(['run'])
        self.assertEqual(raised.exception.code, runloop.FAILED)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def timeout(self, *argv):
        seen = []
        with mock.patch.object(cli, 'build', return_value=(None, self.tmp, None)), \
                mock.patch.object(cli, 'log'), \
                mock.patch.object(cli, 'run_loop',
                                  lambda cfg, state, d, timeout: seen.append(timeout) or 0):
            cli.main(['run', *argv])
        return seen[0]

    def test_idle_timeout_defaults_to_900_seconds(self):
        self.assertEqual(self.timeout(), 900)
        self.assertEqual(self.timeout('--idle-timeout', '120'), 120)

    def test_interval_is_a_deprecated_alias(self):
        with mock.patch.object(cli, 'build', return_value=(None, self.tmp, None)), \
                mock.patch.object(cli, 'log') as log, \
                mock.patch.object(cli, 'run_loop', return_value=0) as loop:
            cli.main(['run', '--interval', '600'])
        self.assertEqual(loop.call_args.args[3], 600)
        log.assert_any_call('run: --interval is deprecated, use --idle-timeout')

    def test_interval_and_idle_timeout_together_are_refused(self):
        with mock.patch('sys.stderr'), self.assertRaises(SystemExit) as raised:
            cli.main(['run', '--interval', '600', '--idle-timeout', '900'])
        self.assertEqual(raised.exception.code, 2)

    def test_a_timeout_below_one_second_is_refused(self):
        for argv in (['--idle-timeout', '0'], ['--interval', '-1']):
            with self.subTest(argv=argv), mock.patch('sys.stderr'), \
                    self.assertRaises(SystemExit) as raised:
                cli.main(['run', *argv])
            self.assertEqual(raised.exception.code, 2)

    def test_the_loop_posts_the_alert_as_the_dispatcher_on_alert_issue(self):
        gh = mock.Mock(time_left=None)
        failing = mock.Mock(side_effect=GitHubError('HTTP 502'))
        dispatcher = SimpleNamespace(tick=failing, gh=gh, identities=None)
        cfg = dataclasses.replace(config.load_config(), run=RunPolicy(alert_issue=7))
        with mock.patch.object(runloop, 'BACKOFF_SECONDS', (0, 0, 0)), \
                mock.patch.object(cli, 'log'):
            code = cli.run_loop(cfg, self.tmp, dispatcher, 900)
        self.assertEqual((code, failing.call_count), (runloop.FAILED, 3))
        number, body = gh.comment.call_args.args
        self.assertEqual(number, 7)
        self.assertIn('`devloop run` si è fermato', body)

    def test_the_wait_uses_a_watcher_with_its_own_state_and_restores_the_client(self):
        gh = mock.Mock(time_left=None)
        dispatcher = SimpleNamespace(tick=mock.Mock(return_value=[]), gh=gh, identities=None)
        seen = []

        class Watcher:
            def __init__(self, client, trust, state, *, interval, timeout, sleep, out, err):
                seen.append((client, state, interval, timeout))
                self.time_left = lambda: 1

            def run(self):
                seen.append(gh.time_left)
                (seen[0][1].parent / 'STOP').touch()  # the loop stops after this wait
                return NOTHING_NEW
        with mock.patch.object(cli.watch, 'Watcher', Watcher), mock.patch.object(cli, 'log'):
            code = cli.run_loop(config.load_config(), self.tmp, dispatcher, 30)
        self.assertEqual(code, 0)
        self.assertEqual(seen[0], (gh, self.tmp / 'run-watch-state.json', 30, 30))
        self.assertTrue(callable(seen[1]))
        self.assertIsNone(gh.time_left)

    def test_a_sigterm_during_an_auto_pin_ends_the_loop_without_a_restart(self):
        def tick():
            os.kill(os.getpid(), signal.SIGTERM)
            raise pin.Repinned(self.tmp / 'pinned-new')
        dispatcher = SimpleNamespace(tick=mock.Mock(side_effect=tick), identities=None,
                                     gh=mock.Mock(time_left=None))
        with mock.patch.object(cli, 'build', return_value=(config.load_config(), self.tmp,
                                                           dispatcher)), \
                mock.patch.object(cli, 'log'), mock.patch.object(pin, 'reexec') as reexec:
            cli.main(['run'])
        reexec.assert_not_called()
        self.assertEqual(dispatcher.tick.call_count, 1)

    def wait_with_stop_during_first_read(self, stop):
        """A real watcher on a client whose first read requests a stop with ``stop``: the
        GitHub reads that the wait starts, by name."""
        reads = []

        class Client:
            time_left = None

            def __getattr__(client, name):
                def read(*args):
                    if client.time_left is not None:
                        client.time_left()  # as github.GitHub does before each gh call
                    reads.append(name)
                    if len(reads) == 1:
                        stop()
                    return []
                return read
        gh = Client()
        dispatcher = SimpleNamespace(tick=mock.Mock(return_value=[]), gh=gh, identities=None)
        with mock.patch.object(cli, 'log'):
            code = cli.run_loop(config.load_config(), self.tmp, dispatcher, 900)
        self.assertEqual(code, 0)
        self.assertEqual(dispatcher.tick.call_count, 1)
        self.assertIsNone(gh.time_left)  # restored after the stop too
        self.assertFalse((self.tmp / runloop.WATCH_STATE_FILE).exists())  # no round completed
        return reads

    def test_a_sigterm_during_a_read_of_the_wait_starts_no_other_read(self):
        reads = self.wait_with_stop_during_first_read(
            lambda: os.kill(os.getpid(), signal.SIGTERM))
        self.assertEqual(reads, ['open_prs'])

    def test_the_stop_file_during_a_read_of_the_wait_starts_no_other_read(self):
        reads = self.wait_with_stop_during_first_read((self.tmp / runloop.STOP_FILE).touch)
        self.assertEqual(reads, ['open_prs'])


class ConfigTests(unittest.TestCase):
    def data(self, **run):
        data = config.read_toml(config.DEFAULT_CONFIG)
        data['run'] = run
        return data

    def test_alert_issue_is_optional_and_not_set_in_the_shipped_config(self):
        self.assertEqual(config.load_config().run.alert_issue, 0)
        self.assertEqual(config.parse_config(self.data(alert_issue=12)).run.alert_issue, 12)

    def test_invalid_values_and_keys_are_refused(self):
        for run in ({'alert_issue': 0}, {'alert_issue': '12'}, {'alert_issue': True},
                    {'issue': 12}):
            with self.subTest(run=run), self.assertRaises(ConfigError):
                config.parse_config(self.data(**run))

    def test_the_overlay_may_set_alert_issue(self):
        data = config.apply_overlay(config.read_toml(config.DEFAULT_CONFIG),
                                    {'run': {'alert_issue': 5}})
        self.assertEqual(config.parse_config(data).run.alert_issue, 5)


class DocumentationTests(unittest.TestCase):
    def test_the_devloop_document_describes_the_loop_and_the_unit(self):
        text = (REPO / 'docs' / 'devloop.md').read_text(encoding='utf-8')
        start = text.index('### Continuous loop')
        section = text[start:text.index('\n### ', start + 1)]
        for needle in ('--idle-timeout', '`STOP`', 'SIGTERM', 'alert_issue', 'devloop.service',
                       'KillMode=mixed', 'RestartPreventExitStatus=4', 'systemctl stop',
                       'devloop-pinned run'):
            self.assertIn(needle, section)


if __name__ == '__main__':
    unittest.main()
