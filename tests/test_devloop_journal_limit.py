"""devloop: the limit of the journal of the turns and the warning before it (#397)."""

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from devloop import journal as journal_mod
from devloop.config import ConfigError, RunPolicy, load_data, parse_config
from devloop.dispatcher import Dispatcher
from devloop.journal import JOURNAL, MIB, SNAPSHOT, SIZE_WARNING, JournalError, TurnJournal
from devloop.ledger import Ledger
from helpers.devloop_fakes import FakeGH
from test_devloop import CFG


def intent(j, number=7):
    return j.intent(number, issue=number, repo=CFG.repo, kind='implement', agent='claude',
                    model='m', label=f'issue-{number}-implement-1')


class JournalLimitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / JOURNAL

    def files(self):
        return {p.name: p.read_bytes() for p in self.dir.iterdir()}

    def test_the_default_limit_is_64_mib(self):
        self.assertEqual(journal_mod.MAX_BYTES, 64 * MIB)
        self.assertEqual(TurnJournal(self.dir).max_bytes, 64 * MIB)
        self.assertEqual(Ledger(None, state_dir=self.dir).journal.max_bytes, 64 * MIB)
        self.assertEqual(TurnJournal(self.dir).warn_bytes(), int(64 * MIB * 0.8))
        self.assertIsNone(TurnJournal(self.dir, max_bytes=None).warn_bytes())

    def test_an_event_beyond_the_limit_is_refused_whole(self):
        j = TurnJournal(self.dir)
        turn = intent(j)
        size = self.path.stat().st_size
        # Room for less than one more event.
        j.max_bytes = size + 10
        before = self.files()
        with self.assertRaisesRegex(JournalError, 'outcome not written: the journal would be '
                                                  r'\d+ bytes, beyond the limit of '
                                                  f'{size + 10} bytes'):
            j.outcome(turn, 'returned')
        # Nothing written, nothing removed: the files are as they were.
        self.assertEqual(self.files(), before)
        with self.assertRaises(JournalError):
            intent(j, 8)  # no turn starts
        self.assertEqual(self.files(), before)
        # A raised limit takes it again, on the same chain.
        j.max_bytes = 64 * MIB
        turn2 = intent(j, 8)
        self.assertEqual(set(TurnJournal(self.dir).turns()), {turn, turn2})

    def test_an_event_up_to_the_limit_is_written(self):
        # The size of the same two events in another journal: ids and times of fixed length.
        with tempfile.TemporaryDirectory() as other:
            probe = TurnJournal(other, clock=lambda: 1_800_000_000)
            probe.outcome(intent(probe), 'returned')
            exact = (Path(other) / JOURNAL).stat().st_size
        j = TurnJournal(self.dir, clock=lambda: 1_800_000_000, max_bytes=exact)
        turn = intent(j)
        j.outcome(turn, 'returned')  # the journal reaches the limit exactly
        self.assertEqual(self.path.stat().st_size, exact)
        self.assertEqual(TurnJournal(self.dir, max_bytes=exact).turns()[turn]['outcome'],
                         'returned')
        with self.assertRaises(JournalError):
            j.unsettled(7)  # one byte more is too many

    def test_a_journal_beyond_the_limit_is_not_loaded_and_kept_as_it_is(self):
        j = TurnJournal(self.dir)
        for number in range(1, 6):
            intent(j, number)
        before = self.files()
        size = self.path.stat().st_size
        for read_only in (False, True):
            with self.subTest(read_only=read_only):
                over = TurnJournal(self.dir, max_bytes=size - 1, read_only=read_only)
                with self.assertRaisesRegex(JournalError, f'not loaded: {size} bytes, beyond '
                                                          f'the limit of {size - 1} bytes'):
                    over.turns()
                with self.assertRaises(JournalError):
                    over.pending()
                # Nothing cut, removed or rewritten: journal and snapshot stay.
                self.assertEqual(self.files(), before)
        # The size is checked before the journal is read, not after.
        with mock.patch.object(TurnJournal, '_read') as read:
            with self.assertRaises(JournalError):
                TurnJournal(self.dir, max_bytes=size - 1).turns()
            read.assert_not_called()
        # The integrity checks stay: a raised limit loads it, and verifies it as before.
        self.assertEqual(len(TurnJournal(self.dir, max_bytes=size).turns()), 5)
        (self.dir / SNAPSHOT).write_text(json.dumps({'sequence': 9, 'sha256': 'x'}))
        with self.assertRaisesRegex(JournalError, 'does not reach its snapshot'):
            TurnJournal(self.dir, max_bytes=size).turns()

    def test_without_state_dir_there_is_no_file_to_limit(self):
        j = TurnJournal(max_bytes=1)
        intent(j)
        self.assertEqual(j.size(), 0)

    def test_the_size_warning_is_recorded_once_per_threshold(self):
        j = TurnJournal(self.dir)
        self.assertFalse(j.size_warned(100))
        j.size_warning(120, 100)
        self.assertTrue(j.size_warned(100))
        self.assertFalse(j.size_warned(200))
        again = TurnJournal(self.dir)
        self.assertTrue(again.size_warned(100))
        [event] = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual(event['event'], SIZE_WARNING)
        self.assertEqual({k: event['data'][k] for k in ('size', 'threshold', 'limit')},
                         {'size': 120, 'threshold': 100, 'limit': 64 * MIB})
        # The turns ignore it.
        self.assertEqual((again.turns(), again.pending()), ({}, []))
        with self.assertRaises(JournalError):
            TurnJournal(self.dir, read_only=True).size_warning(120, 100)


class ConfigTests(unittest.TestCase):
    def test_journal_max_mib(self):
        self.assertEqual(load_data()['journal_max_mib'], 64)
        self.assertEqual(parse_config(load_data()).journal_max_mib, 64)
        data = dict(load_data())
        data.pop('journal_max_mib')
        self.assertEqual(parse_config(data).journal_max_mib, 64)
        self.assertEqual(parse_config(dict(load_data(), journal_max_mib=128)).journal_max_mib,
                         128)
        for bad in (0, -1, '64', 1.5, True):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                parse_config(dict(load_data(), journal_max_mib=bad))

    def test_build_passes_the_limit_to_the_journal(self):
        import devloop.__main__ as devloop_main
        from devloop.apps import Identities
        for command, dry_run in (('run', False), ('status', False), ('tick', True)):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                cfg = dataclasses.replace(CFG, journal_max_mib=3)
                args = SimpleNamespace(config=None, local_config=None, command=command,
                                       dry_run=dry_run)
                with mock.patch.object(devloop_main, 'load_config', return_value=cfg), \
                        mock.patch.object(devloop_main, 'main_checkout', return_value=Path(tmp)), \
                        mock.patch.object(devloop_main.Identities, 'from_config',
                                          return_value=Identities(cfg.repo, {})):
                    _, _, d = devloop_main.build(args)
                self.assertEqual(d.ledger.journal.max_bytes, 3 * MIB)


class SizeWarningTests(unittest.TestCase):
    """From 80% of the limit the operator is warned: once per threshold on
    ``[run].alert_issue``; without it, in the log at each start. The turns go on."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.gh = FakeGH()

    def dispatcher(self, alert_issue, max_bytes):
        lines = []
        cfg = dataclasses.replace(CFG, run=RunPolicy(alert_issue=alert_issue))
        ledger = Ledger(None, state_dir=self.dir, journal_max_bytes=max_bytes)
        d = Dispatcher(cfg, self.gh, None, None, log=lines.append, ledger=ledger)
        return d, lines

    def fill(self, events=40):
        """A journal of ``events`` turns: 15% of it leaves room for a few more."""
        j = TurnJournal(self.dir)
        for number in range(1, events + 1):
            intent(j, number)
        return (self.dir / JOURNAL).stat().st_size

    def alerts(self):
        return [b for n, b in self.gh.comments if n == 76]

    def test_below_the_threshold_nothing(self):
        size = self.fill()
        d, lines = self.dispatcher(76, int(size / 0.8) + 100)
        d._watch_journal()
        self.assertEqual((lines, self.alerts()), ([], []))

    def test_at_the_threshold_one_alert_on_the_alert_issue(self):
        size = self.fill()
        limit = size * 100 // 85  # the journal is at about 85% of it
        d, lines = self.dispatcher(76, limit)
        d._watch_journal()
        [alert] = self.alerts()
        self.assertIn('## Journal dei turni vicino al limite', alert)
        self.assertIn(f'è di {size} byte', alert)
        self.assertIn(f'limite di {limit} byte', alert)
        self.assertIn('`journal_max_mib`', alert)
        self.assertTrue(lines[0].startswith(f'WARNING: the journal of the turns is {size} bytes'))
        self.assertIn('  alert of the size of the journal published on #76', lines)
        threshold = d.ledger.journal.warn_bytes()
        self.assertTrue(TurnJournal(self.dir).size_warned(threshold))
        # Once per threshold: not at the next tick, nor after a restart.
        d._watch_journal()
        d2, _ = self.dispatcher(76, limit)
        d2._watch_journal()
        self.assertEqual(len(self.alerts()), 1)
        # A new limit is a new threshold: warned again once it is reached.
        d3, _ = self.dispatcher(76, limit + 1000)
        d3._watch_journal()
        self.assertEqual(len(self.alerts()), 2)
        # The turns go on.
        intent(d3.ledger.journal, 99)

    def test_a_failed_alert_is_tried_again_at_the_next_tick(self):
        self.fill()
        size = (self.dir / JOURNAL).stat().st_size
        d, lines = self.dispatcher(76, size * 100 // 85)
        with mock.patch.object(self.gh, 'comment', side_effect=RuntimeError('down')):
            d._watch_journal()
        self.assertTrue(any('could not be published (RuntimeError)' in line for line in lines))
        self.assertEqual(self.alerts(), [])
        d._watch_journal()
        self.assertEqual(len(self.alerts()), 1)

    def test_an_alert_with_an_uncertain_outcome_is_never_sent_twice(self):
        """R1-1: with the outbox, GitHub applies the alert, its answer is uncertain and the
        first reconciliation cannot read: no second alert for the threshold, neither at the
        next tick nor after a restart; once the outbox finds it, the warning is recorded."""
        from test_devloop_outbox import OutboxGH, make
        limit = self.fill(200) * 100 // 85
        gh = OutboxGH()
        gh.uncertain_writes, gh.read_fails = ['applied'], 1

        def start():
            ledger = Ledger(None, state_dir=self.dir, journal_max_bytes=limit)
            box, client = make(gh, journal=ledger.journal)
            lines = []
            cfg = dataclasses.replace(CFG, run=RunPolicy(alert_issue=76))
            return Dispatcher(cfg, client, None, None, log=lines.append, ledger=ledger,
                              identities=SimpleNamespace(outbox=box)), box, lines

        d, box, lines = start()
        threshold = d.ledger.journal.warn_bytes()
        d._watch_journal()
        self.assertEqual(len(gh.bodies(76)), 1)
        self.assertTrue(any('has an uncertain outcome (GitHubUncertain)' in line
                            for line in lines))
        self.assertEqual(box.alerts(), {f'journal_size:{threshold}': 'uncertain'})
        self.assertFalse(d.ledger.journal.size_warned(threshold))
        d._watch_journal()  # the next tick, before the outbox settles it
        self.assertEqual(len(gh.bodies(76)), 1)
        # A restart: nothing published before the outbox settles the alert, then recorded.
        d2, box2, lines2 = start()
        d2._watch_journal()
        self.assertEqual(len(gh.bodies(76)), 1)
        self.assertEqual(box2.resume(), [])
        d2._watch_journal()
        self.assertEqual(len(gh.bodies(76)), 1)
        self.assertIn('  alert of the size of the journal found published on #76: recorded in '
                      'the journal', lines2)
        self.assertTrue(TurnJournal(self.dir, max_bytes=limit).size_warned(threshold))
        d3, _, _ = start()
        d3._watch_journal()
        self.assertEqual(len(gh.sent), 1)  # one POST in all

    def test_without_an_alert_issue_a_warning_in_the_log_at_each_start(self):
        size = self.fill()
        limit = size * 100 // 85
        for start in range(3):
            with self.subTest(start=start):
                d, lines = self.dispatcher(0, limit)
                d._watch_journal()
                d._watch_journal()  # once per process
                [line] = lines
                now = (self.dir / JOURNAL).stat().st_size  # each start adds a turn
                self.assertIn(f'WARNING: the journal of the turns is {now} bytes', line)
                self.assertIn('no [run].alert_issue is configured: this warning is only in the '
                              'log, at each start', line)
                self.assertEqual(self.gh.comments, [])
                # Nothing recorded, and the turns go on as usual.
                self.assertFalse(d.ledger.journal.size_warned(d.ledger.journal.warn_bytes()))
                intent(d.ledger.journal, 50 + start)

    def test_beyond_the_limit_the_turns_are_refused(self):
        size = self.fill()
        d, lines = self.dispatcher(0, size - 1)
        d._watch_journal()
        self.assertIn('beyond it no turn starts', lines[0])
        with self.assertRaises(JournalError):
            intent(d.ledger.journal, 99)
        self.assertEqual((self.dir / JOURNAL).stat().st_size, size)

    def test_dry_run_and_status_never_warn(self):
        size = self.fill()
        lines = []
        ledger = Ledger(None, state_dir=self.dir, read_only=True,
                        journal_max_bytes=size * 100 // 85)
        d = Dispatcher(dataclasses.replace(CFG, run=RunPolicy(alert_issue=76)), self.gh, None,
                       None, log=lines.append, ledger=ledger)
        d._watch_journal()
        self.assertEqual((lines, self.gh.comments), ([], []))


if __name__ == '__main__':
    unittest.main()
