"""devloop: ``devloop run`` reads the configuration and its local overlay again before each tick
(#278). Only ``max_open_prs``, ``max_parked_prs`` and ``max_review_rounds`` change while the loop
runs; any other key waits for a restart, and a configuration that does not load leaves the
previous one in use."""

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import devloop.__main__ as cli
from devloop import config
from devloop.config import DEFAULT_CONFIG, LIVE_KEYS, Reloader
from devloop.dispatcher import Action, Dispatcher, Snapshot
from devloop.watch import NOTHING_NEW
from helpers.devloop_fakes import FakeGH
from test_devloop import FakeRunner, FakeWS, ready

REPO = Path(__file__).resolve().parent.parent


class FilesCase(unittest.TestCase):
    """A config.toml (the installation's) and an overlay in a temporary directory."""

    OVERLAY = 'max_open_prs = 1\n'

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.base = self.tmp / 'config.toml'
        self.base.write_text(DEFAULT_CONFIG.read_text(encoding='utf-8'), encoding='utf-8')
        self.local = self.tmp / 'config.local.toml'
        self.local.write_text(self.OVERLAY, encoding='utf-8')
        self.cfg = config.load_config(self.base, self.local)
        self.logs = []
        self.reloader = Reloader(self.cfg, self.base, self.local, log=self.logs.append)

    def overlay(self, text):
        self.local.write_text(text, encoding='utf-8')


class ReloaderTests(FilesCase):
    def test_unchanged_files_keep_the_configuration(self):
        self.assertIs(self.reloader.refresh(), self.cfg)
        self.assertIs(self.reloader.refresh(), self.cfg)
        self.assertEqual(self.logs, [])

    def test_the_limits_of_the_overlay_apply_from_the_next_tick(self):
        self.overlay('max_open_prs = 4\nmax_parked_prs = 5\nmax_review_rounds = 6\n')
        new = self.reloader.refresh()
        self.assertEqual((new.max_open_prs, new.max_parked_prs, new.max_review_rounds),
                         (4, 5, 6))
        for field in config.dataclasses.fields(config.Config):
            if field.name not in LIVE_KEYS:
                self.assertEqual(getattr(new, field.name), getattr(self.cfg, field.name))
        self.assertIn('config: max_open_prs 1 -> 4 from this tick', self.logs)
        self.assertIn('config: max_parked_prs 3 -> 5 from this tick', self.logs)
        self.assertIn('config: max_review_rounds 3 -> 6 from this tick', self.logs)
        self.logs.clear()
        self.assertIs(self.reloader.refresh(), new)  # read again only after the next change
        self.assertEqual(self.logs, [])

    def test_a_change_of_the_installations_config_is_read_too(self):
        text = self.base.read_text(encoding='utf-8')
        self.base.write_text(text.replace('\nmax_review_rounds = 3', '\nmax_review_rounds = 5', 1),
                             encoding='utf-8')
        self.assertEqual(self.reloader.refresh().max_review_rounds, 5)

    def test_an_invalid_overlay_leaves_the_previous_configuration_in_use(self):
        for text in ('max_open_prs = \n', 'bogus = 1\n', 'max_open_prs = 0\n',
                     'max_open_prs = "3"\n', '[merge]\nmode = "always"\n',
                     'roles = 1\n', b'\xff\xfe'):
            with self.subTest(text=text):
                self.logs.clear()
                if isinstance(text, bytes):
                    self.local.write_bytes(text)
                else:
                    self.overlay(text)
                self.assertIs(self.reloader.refresh(), self.cfg)
                self.assertEqual(len(self.logs), 1)
                self.assertTrue(self.logs[0].startswith(
                    'config: changed but not valid, the previous configuration stays in use: '),
                    self.logs[0])
                # The error is logged once, not at every tick.
                self.assertIs(self.reloader.refresh(), self.cfg)
                self.assertEqual(len(self.logs), 1)
        self.overlay('max_open_prs = 3\n')
        self.assertEqual(self.reloader.refresh().max_open_prs, 3)

    def test_a_removed_overlay_leaves_the_previous_configuration_in_use(self):
        self.local.unlink()
        self.assertIs(self.reloader.refresh(), self.cfg)
        self.assertIn('cannot be read', self.logs[0])

    def test_any_other_key_waits_for_a_restart(self):
        self.overlay('max_open_prs = 3\nstate_dir = "elsewhere"\n'
                     'trusted_users = ["someone"]\nmax_parallel_actions = 1\n'
                     '[merge]\nmode = "strict"\n[pin]\nauto = true\n'
                     '[roles]\nsupervisor = "claude"\n')
        new = self.reloader.refresh()
        self.assertEqual(new.max_open_prs, 3)
        self.assertEqual(
            (new.state_dir, new.trusted_users, new.max_parallel_actions, new.merge, new.pin,
             new.supervisor),
            (self.cfg.state_dir, self.cfg.trusted_users, self.cfg.max_parallel_actions,
             self.cfg.merge, self.cfg.pin, self.cfg.supervisor))
        restart = [line for line in self.logs if 'only after a restart' in line]
        self.assertEqual(len(restart), 1)
        for key in ('state_dir', 'trusted_users', 'max_parallel_actions', 'merge', 'pin',
                    'supervisor'):
            self.assertIn(key, restart[0])
        self.assertNotIn('max_open_prs', restart[0])


class RunLoopTests(FilesCase):
    """``cli.run_loop`` with the reloader ``main`` gives it."""

    def test_a_new_max_open_prs_starts_an_action_while_another_runs(self):
        # Continuous mode, one PR slot: #9 starts. The operator raises max_open_prs while it
        # runs, and the next tick starts #10 alongside it; #9 ends as it would have.
        self.overlay('max_open_prs = 1\nmax_parallel_actions = 3\n')
        self.cfg = config.load_config(self.base, self.local)
        self.reloader = Reloader(self.cfg, self.base, self.local, log=self.logs.append)
        gh = FakeGH()
        gh.time_left = None  # the wait for news bounds the client's calls with it
        d = Dispatcher(self.cfg, gh, FakeWS(), FakeRunner(), log=lambda line: None)
        snapshots = []

        def snapshot():
            snapshots.append(d.cfg.max_open_prs)
            if len(snapshots) > 2:
                return Snapshot([], [], [])
            return Snapshot([], [ready(9), ready(10)], [])
        d.snapshot = snapshot
        release = threading.Event()
        events = []

        def implement(number):
            events.append(('start', number, [a for a, _ in d.workers.running()]))
            if number == 10:
                (self.tmp / 'STOP').touch()
                release.set()
            else:
                release.wait(timeout=10)
            events.append(('end', number))
        d._do_implement = implement
        waits = []

        class Watcher:
            def __init__(watcher, client, trust, state, *, interval, timeout, sleep, out, err):
                watcher.time_left = lambda: 1

            def run(watcher):
                waits.append(1)
                if len(waits) == 1:  # the operator edits the overlay during the wait
                    self.overlay('max_open_prs = 2\nmax_parallel_actions = 3\n')
                    return 0
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    time.sleep(0.01)
                    if (self.tmp / 'STOP').exists() or d.workers.ended():
                        return 0
                (self.tmp / 'STOP').touch()  # a test that fails ends anyway
                return NOTHING_NEW
        with mock.patch.object(cli.watch, 'Watcher', Watcher), mock.patch.object(cli, 'log'):
            code = cli.run_loop(self.cfg, self.tmp, d, 900, self.reloader)
        self.assertEqual(code, 0)
        self.assertEqual(snapshots[:2], [1, 2])
        self.assertEqual(d.cfg.max_open_prs, 2)
        self.assertIn('config: max_open_prs 1 -> 2 from this tick', self.logs)
        self.assertEqual(events[0], ('start', 9, [Action('implement', 9)]))
        start10 = events.index(('start', 10, [Action('implement', 9), Action('implement', 10)]))
        self.assertLess(start10, events.index(('end', 9)))  # #10 started while #9 ran
        self.assertIn(('end', 9), events)  # the running action was not touched

    def test_an_invalid_overlay_does_not_stop_the_loop(self):
        ticks = []
        d = SimpleNamespace(cfg=self.cfg, gh=mock.Mock(time_left=None), identities=None)

        def tick():
            ticks.append(d.cfg)
            if len(ticks) == 1:
                self.overlay('max_open_prs = [\n')
            elif len(ticks) == 3:
                (self.tmp / 'STOP').touch()
            return [Action('implement', len(ticks))]
        d.tick = tick
        with mock.patch.object(cli, 'log') as log:
            code = cli.run_loop(self.cfg, self.tmp, d, 900, self.reloader)
        self.assertEqual(code, 0)
        self.assertEqual(len(ticks), 3)
        self.assertTrue(all(cfg is self.cfg for cfg in ticks))
        self.assertFalse(any('tick failed' in str(call) for call in log.call_args_list))
        self.assertEqual(len(self.logs), 1)
        self.assertIn('previous configuration stays in use', self.logs[0])

    def test_main_gives_run_the_files_it_started_with(self):
        seen = []
        with mock.patch.object(cli, 'build', return_value=(self.cfg, self.tmp, None)), \
                mock.patch.object(cli, 'log'), \
                mock.patch.object(cli, 'run_loop',
                                  lambda cfg, state, d, timeout, reloader: seen.append(reloader)
                                  or 0):
            cli.main(['--config', str(self.base), '--local-config', str(self.local), 'run'])
        self.assertEqual(seen[0].paths, (self.base, self.local))
        self.assertIs(seen[0].cfg, self.cfg)


class DocumentationTests(unittest.TestCase):
    def test_the_documents_say_which_keys_are_read_again(self):
        guide = (REPO / 'docs' / 'devloop' / 'guide.md').read_text(encoding='utf-8')
        start = guide.index('#### Changes while the loop runs')
        section = guide[start:guide.index('\n#### ', start + 1)]
        for needle in ('`max_open_prs`, `max_parked_prs` and `max_review_rounds`',
                       '**Every other key needs a restart**', '`[merge]`', '`[pin]`',
                       '`trusted_users`', '`[apps]`', '`state_dir`',
                       'the previous configuration stays in use', 'not interrupted'):
            self.assertIn(needle, section)
        self.assertIn('(#changes-while-the-loop-runs)', guide)
        reference = (REPO / 'docs' / 'devloop' / 'reference.md').read_text(encoding='utf-8')
        self.assertIn('guide.md#changes-while-the-loop-runs', reference)


if __name__ == '__main__':
    unittest.main()
