"""devloop: no new work while a role is at rest (#684), ``devloop.rest``."""

import dataclasses
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from devloop import config, rest, runloop
from devloop.config import ConfigError, RunPolicy, parse_run
from devloop.dispatcher import Dispatcher
from devloop.planning import Action, Snapshot
from devloop.watch import NOTHING_NEW
from test_devloop import FakeRunner, FakeWS
from test_devloop_preflight import PCFG, IssueGH, checked


class Clock:
    def __init__(self, now=1_000_000):
        self.now = now

    def __call__(self):
        return self.now


class Notifier:
    def __init__(self):
        self.sent = []

    def notify(self, text, key=None):
        self.sent.append((key, text))
        return 'sent'


class RestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rest-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.rest_dir = self.tmp / 'coordination/rest'
        self.rest_dir.mkdir(parents=True)
        self.state = self.tmp / 'state'
        self.logs = []
        self.clock = Clock()
        self.notifier = Notifier()

    def hold(self, timeout=300):
        return rest.RestHold(self.rest_dir, self.state, timeout, log=self.logs.append,
                             notifier=self.notifier, clock=self.clock)

    def at_rest(self, role):
        (self.rest_dir / role).write_text('1764115200 riposo\n')

    @property
    def hold_file(self):
        return self.state / 'events' / rest.HOLD_FILE


class RestHoldTests(RestCase):
    ACTIONS = [Action('merge', 3), Action('preflight', 7, 'no check'), Action('review', 4),
               Action('implement', 8)]

    def test_without_a_role_at_rest_nothing_is_held(self):
        hold = self.hold()
        self.assertEqual(hold.apply(self.ACTIONS), self.ACTIONS)
        self.assertFalse(hold.holding())
        self.assertFalse(self.hold_file.exists())

    def test_a_role_at_rest_holds_the_readiness_checks_and_the_implementations(self):
        hold = self.hold()
        self.at_rest('coordinator')
        kept = hold.apply(self.ACTIONS)
        self.assertEqual([(a.kind, a.number) for a in kept], [('merge', 3), ('review', 4)])
        self.assertTrue(hold.holding())
        # The file the sentinel wakes the roles on, and the watchdog reads: since when, which
        # roles and what.
        self.assertEqual(self.hold_file.read_text(),
                         '1000000 coordinator held: preflight #7, implement #8\n')
        self.assertEqual(self.hold_file.stat().st_mode & 0o777, 0o640)
        self.assertIn('rest: coordinator at rest: preflight #7, implement #8 held', self.logs[-1])
        # Still held later: the first time stays.
        self.clock.now += 60
        self.at_rest('supervisor')
        hold.apply(self.ACTIONS)
        self.assertEqual(self.hold_file.read_text(),
                         '1000000 supervisor,coordinator held: preflight #7, implement #8\n')
        # The roles leave their rest: the next tick starts the work, and the file goes.
        for role in ('supervisor', 'coordinator'):
            (self.rest_dir / role).unlink()
        self.assertEqual(hold.apply(self.ACTIONS), self.ACTIONS)
        self.assertFalse(hold.holding())
        self.assertFalse(self.hold_file.exists())

    def test_nothing_to_hold_removes_the_file_and_reads_no_rest(self):
        hold = self.hold()
        self.at_rest('supervisor')
        hold.apply(self.ACTIONS)
        others = [Action('merge', 3)]
        self.assertEqual(hold.apply(others), others)
        self.assertFalse(self.hold_file.exists())

    def test_the_wait_ends_when_the_roles_leave_their_rest(self):
        hold = self.hold()
        self.assertFalse(hold.poll())  # nothing held: nothing to wait for
        self.at_rest('supervisor')
        hold.apply(self.ACTIONS)
        self.assertFalse(hold.poll())
        (self.rest_dir / 'supervisor').unlink()
        self.assertTrue(hold.poll())
        self.assertIn('no role at rest any more: preflight #7, implement #8 may start',
                      self.logs[-1])

    def test_roles_that_do_not_wake_up_are_told_to_the_operator_once(self):
        hold = self.hold(timeout=300)
        self.at_rest('coordinator')
        hold.apply(self.ACTIONS)
        self.clock.now += 299
        self.assertFalse(hold.poll())
        self.assertEqual(self.notifier.sent, [])
        self.clock.now += 1
        self.assertFalse(hold.poll())
        self.assertEqual(len(self.notifier.sent), 1)
        key, text = self.notifier.sent[0]
        self.assertEqual(key, 'rest-hold')
        self.assertIn('preflight #7, implement #8', text)
        self.assertIn('(coordinator)', text)
        self.assertIn('300', text)
        # Once: the work stays held, the notice is not repeated.
        self.clock.now += 3600
        hold.poll()
        hold.apply(self.ACTIONS)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual([a.kind for a in hold.apply(self.ACTIONS)], ['merge', 'review'])
        # A new hold, after the roles woke up, is a new episode.
        (self.rest_dir / 'coordinator').unlink()
        hold.apply(self.ACTIONS)
        self.at_rest('coordinator')
        hold.apply(self.ACTIONS)
        self.clock.now += 300
        hold.poll()
        self.assertEqual(len(self.notifier.sent), 2)

    @unittest.skipIf(os.geteuid() == 0, 'root reads any directory')
    def test_a_rest_directory_it_cannot_read_holds_the_work_and_tells_the_operator(self):
        # The rest cannot be verified: not the same as no role at rest.
        hold = self.hold()
        self.at_rest('supervisor')
        self.rest_dir.chmod(0)
        self.addCleanup(self.rest_dir.chmod, 0o755)
        kept = hold.apply(self.ACTIONS)
        self.assertEqual([a.kind for a in kept], ['merge', 'review'])
        self.assertTrue(hold.holding())
        self.assertEqual(self.hold_file.read_text(),
                         '1000000 unknown held: preflight #7, implement #8\n')
        self.assertEqual(len(self.notifier.sent), 1)
        key, text = self.notifier.sent[0]
        self.assertEqual(key, 'rest-unreadable')
        self.assertIn('PermissionError', text)
        # Once: the next ticks and waits hold on, without a second notice nor the one of the
        # roles that do not wake up.
        self.clock.now += 600
        self.assertFalse(hold.poll())
        self.assertEqual([a.kind for a in hold.apply(self.ACTIONS)], ['merge', 'review'])
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(len([line for line in self.logs if 'cannot be read' in line]), 1)
        # Readable again, with nobody at rest: the work starts.
        self.rest_dir.chmod(0o755)
        (self.rest_dir / 'supervisor').unlink()
        self.assertTrue(hold.poll())
        self.assertEqual(hold.apply(self.ACTIONS), self.ACTIONS)
        self.assertFalse(self.hold_file.exists())
        self.assertIn('can be read again', ' '.join(self.logs))

    def test_a_missing_rest_directory_holds_the_work_too(self):
        hold = self.hold()
        shutil.rmtree(self.rest_dir)
        self.assertEqual([a.kind for a in hold.apply(self.ACTIONS)], ['merge', 'review'])
        self.assertEqual(self.notifier.sent[0][0], 'rest-unreadable')
        self.assertIn('FileNotFoundError', self.notifier.sent[0][1])

    def test_a_queue_without_actions_is_work_for_the_roles_at_rest(self):
        # An issue in the queue blocked by a prerequisite: the plan has no action for it, but a
        # role does not rest with it, and the watchdog reads it from the file.
        snap = Snapshot(prs=[SimpleNamespace(number=9)],
                        ready_issues=[{'number': 7, 'blocked_by': ['#8']}], working_issues=[5])
        self.assertEqual(rest.queue(snap), ['issue #5', 'issue #7', 'PR #9'])
        self.assertEqual(rest.queue(None), [])
        hold = self.hold()
        # Nobody at rest: nothing written.
        self.assertEqual(hold.apply([], snap), [])
        self.assertFalse(self.hold_file.exists())
        self.at_rest('supervisor')
        self.assertEqual(hold.apply([], snap), [])
        self.assertEqual(self.hold_file.read_text(),
                         '1000000 supervisor queue: issue #5, issue #7, PR #9\n')
        # Nothing held: the waits of the loop do not poll, and no notice of the hold.
        self.assertFalse(hold.holding())
        self.clock.now += 600
        hold.apply([], snap)
        self.assertEqual(self.notifier.sent, [])
        # With an action held too, both.
        hold.apply([Action('implement', 7)], snap)
        self.assertEqual(self.hold_file.read_text(),
                         '1000000 supervisor held: implement #7; queue: issue #5, issue #7, '
                         'PR #9\n')
        # The queue emptied: the file goes.
        empty = Snapshot(prs=[], ready_issues=[], working_issues=[])
        hold.apply([], empty)
        self.assertFalse(self.hold_file.exists())

    def test_a_long_queue_is_cut(self):
        snap = Snapshot(prs=[], ready_issues=[{'number': n} for n in range(1, 14)],
                        working_issues=[])
        hold = self.hold()
        self.at_rest('coordinator')
        hold.apply([], snap)
        self.assertTrue(self.hold_file.read_text().endswith('issue #10 and 3 more\n'))

    def test_from_config_only_with_rest_dir(self):
        cfg = dataclasses.replace(PCFG, run=RunPolicy())
        self.assertIsNone(rest.RestHold.from_config(cfg, self.state))
        cfg = dataclasses.replace(PCFG, run=RunPolicy(rest_dir=str(self.rest_dir),
                                                      rest_wake_timeout=60))
        hold = rest.RestHold.from_config(cfg, self.state)
        self.assertEqual((hold.rest_dir, hold.timeout), (self.rest_dir, 60))


class RunConfigTests(unittest.TestCase):
    def test_rest_dir_and_its_timeout(self):
        self.assertEqual(parse_run({}), RunPolicy(alert_issue=0, rest_dir='',
                                                  rest_wake_timeout=300))
        self.assertEqual(parse_run({'rest_dir': '/var/lib/x/rest', 'rest_wake_timeout': 60}),
                         RunPolicy(rest_dir='/var/lib/x/rest', rest_wake_timeout=60))
        for table in ({'rest_dir': 'relative/rest'}, {'rest_dir': 3},
                      {'rest_wake_timeout': 0}, {'rest_wake_timeout': True},
                      {'rest_wake_timeout': '300'}):
            with self.subTest(table=table), self.assertRaises(ConfigError):
                parse_run(table)

    def test_they_are_keys_of_the_overlay(self):
        self.assertIn('rest_dir', config.OVERLAY_KEYS['run'])
        self.assertIn('rest_wake_timeout', config.OVERLAY_KEYS['run'])


class DispatcherRestTests(RestCase):
    """The tick holds the readiness check of issue #7 while a role is at rest."""

    def test_the_tick_starts_no_new_work_while_a_role_is_at_rest(self):
        gh, runner = IssueGH(), FakeRunner(checked(True))
        d = Dispatcher(PCFG, gh, FakeWS(), runner, log=self.logs.append)
        d.rest = self.hold()
        self.at_rest('coordinator')
        self.assertEqual(d.tick(), [])
        self.assertEqual(runner.calls, [])
        # The queue of the snapshot too: issue #7 is ready.
        self.assertEqual(self.hold_file.read_text(),
                         '1000000 coordinator held: preflight #7; queue: issue #7\n')
        # A dry run shows the plan as it is, and holds nothing.
        self.assertEqual([a.kind for a in d.tick(dry_run=True)], ['preflight'])
        # Out of the rest, the check starts.
        (self.rest_dir / 'coordinator').unlink()
        self.assertEqual([a.kind for a in d.tick()], ['preflight'])
        self.assertEqual(len(runner.calls), 1)
        self.assertFalse(self.hold_file.exists())


class LoopRestTests(RestCase):
    """runloop.Loop: a wait with work held for the roles at rest ends when they leave it."""

    def test_the_idle_wait_ends_as_soon_as_the_roles_leave_their_rest(self):
        hold = self.hold()
        self.at_rest('supervisor')
        hold.apply([Action('implement', 8)])
        ticks = []
        stop = self.tmp / 'STOP'

        def tick():
            ticks.append(1)
            if len(ticks) == 2:
                stop.write_text('')
            return []
        checks = []

        def watch(timeout, sleep, check):
            # Reads GitHub until the timeout, calling check before each pause.
            for _ in range(5):
                checks.append(1)
                if len(checks) == 3:
                    (self.rest_dir / 'supervisor').unlink()
                check()
            return NOTHING_NEW
        lines = []
        loop = runloop.Loop(tick, watch, stop, log=lines.append, idle_timeout=900,
                            sleep=lambda s: None, rest=hold)
        self.assertEqual(loop.run(), 0)
        self.assertEqual(len(ticks), 2)
        self.assertEqual(len(checks), 3)
        self.assertIn('the roles left their rest: next tick', lines)


if __name__ == '__main__':
    unittest.main()
