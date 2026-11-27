"""devloop: no new work while a role is at rest (#684), ``devloop.rest``."""

import dataclasses
import os
import re
import shutil
import signal
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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


MIGRATE = Path(__file__).resolve().parents[1] / 'ops' / 'host' / 'migrate.md'


def migrate_section(title):
    """The section of ``ops/host/migrate.md`` with heading ``title``, up to the next heading of
    the same level or above."""
    text = MIGRATE.read_text(encoding='utf-8')
    start = text.index(f'\n{title}\n')
    level = title.index(' ')
    following = re.compile(rf'^#{{1,{level}}} ', re.M).search(text, start + len(title) + 2)
    return text[start:following.start()] if following else text[start:]


class HostStepOrderTests(unittest.TestCase):
    """#702: the step on the host of #684 pins by hand before ``rest_dir`` is in the overlay,
    since the pinned copy refuses a key of ``[run]`` it does not know, and starts the service
    after."""

    def test_a_copy_that_does_not_know_a_key_of_run_refuses_it(self):
        # What the pinned copy of before does with a key it does not know, here a new one.
        with self.assertRaisesRegex(ConfigError, r"unknown key 'run\.rest_later'"):
            config.apply_overlay({}, {'run': {'rest_later': 1}})

    def test_step_3_of_684_pins_then_writes_rest_dir_then_starts(self):
        section = migrate_section('## Il riposo dei ruoli (#684)')
        commands = section[section.index('\n### Comandi\n'):]
        step = commands[commands.index('\n3. '):commands.index('\n4. ')]
        pin = step.index('`devloop pin origin/main`')
        key = step.index('`rest_dir = "<COORDINATION_DIR>/rest"`')
        start = step.index('`systemctl start <SERVICE_NAME>`')
        self.assertLess(pin, key)
        self.assertLess(key, start)
        self.assertIn('qui è il passo 3,\nche fa il pin prima di scrivere `rest_dir`', section)
        # The rollback: the key goes before the pin of the copy of before, which refuses it.
        rollback = section[section.index('**Rollback**'):]
        self.assertIn('si toglie **prima** del pin', rollback)

    def test_through_the_procedure_between_tags_rest_dir_goes_after_the_pin(self):
        # A project with SOURCE_REF runs #684 inside step 3 of #666, before the pin of step 5:
        # the new key of the overlay waits for step 6, after the pin and before the start.
        section = ' '.join(migrate_section(
            '## Un progetto con la sorgente a un tag nuovo (#666)').split())
        step3 = section[section.index('3. **Le sezioni introdotte'):section.index('4. **La verifica')]
        self.assertIn('**Una chiave nuova dell\'overlay** che una sezione fa scrivere (come '
                      '`rest_dir`', step3)
        self.assertIn('non si scrive lì', step3)
        self.assertIn('si scrive al passo 6, dopo il pin e prima dell\'avvio', step3)
        pin = section.index('5. **Il pin del tag nuovo**')
        step6 = section.index('6. **L\'avvio del servizio**')
        key = section.index('le chiavi nuove dell\'overlay annotate al passo 3', step6)
        start = section.index('`systemctl start <SERVICE_NAME>`', step6)
        self.assertLess(pin, step6)
        self.assertLess(key, start)
        rollback = section[section.index('**Rollback.**'):]
        self.assertIn('il passo 6, senza chiavi nuove', rollback)
        # #684 itself points to that order for a project with SOURCE_REF.
        rest = ' '.join(migrate_section('## Il riposo dei ruoli (#684)').split())
        self.assertIn('il pin è il suo passo 5, `rest_dir` si scrive al suo passo 6, prima '
                      'dell\'avvio, mai al suo passo 3', rest)

    def test_the_general_rule_on_a_new_key_of_the_overlay(self):
        rule = migrate_section('### Ogni cambio con un passo sull\'host')
        self.assertIn('Una chiave nuova dell\'overlay si scrive **dopo** il pin del codice che la '
                      'conosce', ' '.join(rule.split()))


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


class PauseCase(RestCase):
    """The pause of the operator (#685): ``<rest_dir>/pause``, root's. The tests own their files:
    ``OPERATOR_UID`` is the user of the tests unless a test says otherwise."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(rest, 'OPERATOR_UID', os.getuid())
        patcher.start()
        self.addCleanup(patcher.stop)

    def pause(self, text='1764115200 pausa\n'):
        (self.rest_dir / rest.PAUSE_FILE).write_text(text)


class OperatorPauseTests(PauseCase):
    def test_the_pause_of_the_operator_is_a_file_of_root_with_its_start(self):
        self.assertIsNone(rest.operator_pause(self.rest_dir))
        self.pause()
        self.assertEqual(rest.operator_pause(self.rest_dir), 1764115200)
        self.assertEqual(self.hold().paused(), 1764115200)

    def test_nobody_but_the_operator_opens_the_pause(self):
        # A role writes in rest_dir: its file is no pause, whatever it says.
        self.pause()
        with mock.patch.object(rest, 'OPERATOR_UID', os.getuid() + 1):
            self.assertIsNone(rest.operator_pause(self.rest_dir))
        # Nor a link to a file of root, nor another text, nor a directory.
        (self.rest_dir / rest.PAUSE_FILE).unlink()
        target = self.tmp / 'root-file'
        target.write_text('1764115200 pausa\n')
        (self.rest_dir / rest.PAUSE_FILE).symlink_to(target)
        self.assertIsNone(rest.operator_pause(self.rest_dir))
        (self.rest_dir / rest.PAUSE_FILE).unlink()
        for text in ('1764115200 riposo\n', 'pausa\n', '', '1764115200 pausax\n'):
            with self.subTest(text=text):
                self.pause(text)
                self.assertIsNone(rest.operator_pause(self.rest_dir))
        (self.rest_dir / rest.PAUSE_FILE).unlink()
        (self.rest_dir / rest.PAUSE_FILE).mkdir()
        self.assertIsNone(rest.operator_pause(self.rest_dir))

    def test_each_tick_writes_the_queue_for_the_watchdog_of_the_pause(self):
        hold = self.hold()
        queue_file = self.state / 'events' / rest.QUEUE_FILE
        snap = Snapshot(prs=[SimpleNamespace(number=9)], ready_issues=[{'number': 7}],
                        working_issues=[])
        # Nobody at rest: the queue is written all the same, and nothing is held.
        self.assertEqual(hold.apply(self.ACTIONS, snap), self.ACTIONS)
        self.assertEqual(queue_file.read_text(), '1000000 issue #7, PR #9\n')
        self.assertEqual(queue_file.stat().st_mode & 0o777, 0o640)
        # The same queue later: the time of its first sight stays.
        self.clock.now += 60
        hold.apply([], snap)
        self.assertEqual(queue_file.read_text(), '1000000 issue #7, PR #9\n')
        # An empty queue: the time alone.
        hold.apply([], Snapshot(prs=[], ready_issues=[], working_issues=[]))
        self.assertEqual(queue_file.read_text(), '1000060\n')
        # Without a snapshot (a plan of nothing to read) the file stays.
        hold.apply([])
        self.assertEqual(queue_file.read_text(), '1000060\n')

    ACTIONS = RestHoldTests.ACTIONS


class LoopPauseTests(PauseCase):
    """runloop.Loop with the pause of the operator: it stops, and it does not start."""

    def loop(self, tick, lines):
        def watch(timeout, sleep, check):
            for _ in range(3):
                check()
            return NOTHING_NEW
        return runloop.Loop(tick, watch, self.tmp / 'STOP', log=lines.append, idle_timeout=900,
                            sleep=lambda s: None, rest=self.hold())

    def test_while_paused_the_loop_does_not_start(self):
        # A service started by anyone but the operator's resume (the supervisor may start it).
        self.pause()
        ticks, lines = [], []
        self.assertEqual(self.loop(lambda: ticks.append(1) or [], lines).run(), 0)
        self.assertEqual(ticks, [])
        self.assertIn('the operator has paused devloop (#685): no tick runs until the operator '
                      'resumes it', lines[0])

    def test_a_pause_that_begins_stops_the_loop_after_the_tick_in_progress(self):
        ticks, lines = [], []

        def tick():
            ticks.append(1)
            self.pause()
            return [Action('review', 4)]  # with actions, another tick would follow at once
        self.assertEqual(self.loop(tick, lines).run(), 0)
        self.assertEqual(ticks, [1])
        self.assertIn('stop requested (the pause of the operator): no further tick', lines)

    def test_the_sigterm_of_pause_stops_the_loop_cleanly_without_other_ticks(self):
        # `devloop pause` stops the service: SIGTERM, during a tick or a wait.
        ticks, lines = [], []

        def tick():
            ticks.append(1)
            os.kill(os.getpid(), signal.SIGTERM)
            return [Action('implement', 8)]
        before = signal.getsignal(signal.SIGTERM)
        self.assertEqual(self.loop(tick, lines).run(), 0)
        self.assertEqual(ticks, [1])
        self.assertIn('stop requested (SIGTERM): no further tick', lines)
        self.assertIs(signal.getsignal(signal.SIGTERM), before)
        # And during the wait for news.
        ticks.clear()

        def idle():
            ticks.append(1)
            return []

        def watch(timeout, sleep, check):
            os.kill(os.getpid(), signal.SIGTERM)
            check()
            raise AssertionError('the check did not stop the wait')
        lines.clear()
        loop = runloop.Loop(idle, watch, self.tmp / 'STOP', log=lines.append, idle_timeout=900,
                            sleep=lambda s: None, rest=self.hold())
        self.assertEqual(loop.run(), 0)
        self.assertEqual(ticks, [1])
        self.assertIn('stop requested (SIGTERM): no further tick', lines)


if __name__ == '__main__':
    unittest.main()
