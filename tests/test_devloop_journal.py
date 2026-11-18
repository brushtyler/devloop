"""devloop: the durable journal of the turns' intents and outcomes (#362, ADR 0004, P2-2)."""

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devloop import journal as journal_mod
from devloop import markdown
from devloop.config import ConfigError, load_config, parse_config, load_data
from devloop.dispatcher import Dispatcher
from devloop.github import GitHubError
from devloop.journal import (JOURNAL, LEGACY, SNAPSHOT, JournalError, TurnJournal,
                             reconcile_command)
from devloop.ledger import Ledger
from devloop.planning import Action, Snapshot
from test_devloop import CFG, META, REV, implemented, pr, raw_review
from test_devloop_ledger import LedgerGH, ReportingRunner, TreeWS

EMPTY = Snapshot(prs=[], ready_issues=[], working_issues=[])
BLOCKING = dataclasses.replace(CFG, block_uncertain_turns=True)


def intent(j, number=7, issue=7, kind='implement'):
    return j.intent(number, issue=issue, repo=CFG.repo, kind=kind, agent='claude', model='m',
                    label=f'issue-{number}-{kind}-1')


def lines(path):
    return path.read_bytes().splitlines()


class Crash(Exception):
    """The dispatcher stops while a CLI runs."""


class JournalChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / JOURNAL

    def test_each_record_carries_the_hash_of_the_previous_one(self):
        j = TurnJournal(self.dir)
        turn = intent(j)
        j.outcome(turn, 'returned')
        first, second = [json.loads(line) for line in lines(self.path)]
        self.assertEqual((first['sequence'], first['previous']), (1, ''))
        self.assertEqual((second['sequence'], second['previous']), (2, first['sha256']))
        self.assertEqual(second['sha256'], journal_mod.digest(
            {k: v for k, v in second.items() if k != 'sha256'}))
        # Read again by a new process: the same turn, with its outcome.
        self.assertEqual(TurnJournal(self.dir).turns()[turn]['outcome'], 'returned')
        snapshot = json.loads((self.dir / SNAPSHOT).read_text())
        self.assertEqual(snapshot, {'sequence': 2, 'sha256': second['sha256']})

    def test_a_hash_that_does_not_match_is_an_explicit_error(self):
        j = TurnJournal(self.dir)
        intent(j)
        intent(j, number=8, issue=8)
        records = [json.loads(line) for line in lines(self.path)]
        for name, index in (('a record before the last', 0), ('the last record', 1)):
            with self.subTest(name):
                altered = [dict(r) for r in records]
                altered[index]['data'] = dict(altered[index]['data'], number=99)
                self.path.write_text(''.join(json.dumps(r) + '\n' for r in altered))
                with self.assertRaisesRegex(JournalError, 'hash chain breaks at line'):
                    TurnJournal(self.dir).turns()
                # Nothing is set aside nor cut: the error is not ignored.
                self.assertEqual(len(lines(self.path)), 2)
                self.assertEqual(list(self.dir.glob('turn-journal-tail-*')), [])

    def test_an_unreadable_record_before_the_last_is_an_error(self):
        j = TurnJournal(self.dir)
        intent(j)
        intent(j, number=8, issue=8)
        good = lines(self.path)
        self.path.write_bytes(b'{not json\n' + good[1] + b'\n')
        with self.assertRaisesRegex(JournalError, 'line 1 is not a record'):
            TurnJournal(self.dir).turns()

    def test_a_truncated_or_corrupt_last_record_is_set_aside_and_the_valid_ones_stay(self):
        for name, tail in (('truncated, no newline', b'{"sequence": 3, "prev'),
                           ('corrupt, with its newline', b'\x00garbage\n')):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                j = TurnJournal(folder)
                first = intent(j)
                j.outcome(first, 'failed')
                path = folder / JOURNAL
                good = path.read_bytes()
                path.write_bytes(good + tail)
                again = TurnJournal(folder)
                self.assertEqual(again.turns()[first]['outcome'], 'failed')
                self.assertEqual(path.read_bytes(), good)  # cut off, records kept
                (aside,) = folder.glob('turn-journal-tail-*.bin')
                self.assertEqual(aside.read_bytes(), tail)
                # The chain goes on from the last valid record.
                second = intent(again, number=8, issue=8)
                self.assertIn(second, TurnJournal(folder).turns())

    def test_a_read_only_journal_changes_no_file(self):
        j = TurnJournal(self.dir)
        first = intent(j)
        (self.dir / LEGACY).write_text(json.dumps({'100': 1}))
        tail = b'{"sequence": 2, "prev'
        self.path.write_bytes(self.path.read_bytes() + tail)  # a write in flight
        files = {p.name: p.read_bytes() for p in self.dir.iterdir()}
        reader = TurnJournal(self.dir, read_only=True)
        # It reads what the dispatcher would, the old trace imported in memory...
        self.assertIsNone(reader.turns()[first]['outcome'])
        self.assertEqual(reader.unlinked(), [100])
        # ... and what its planning records stays in memory.
        reader.link({100: 7}, CFG.repo)
        reader.uncertain(7)
        self.assertEqual(reader.unlinked(), [])
        self.assertEqual(reader.turns()[first]['outcome'], 'uncertain')
        with self.assertRaisesRegex(JournalError, 'read-only'):
            intent(reader, number=8, issue=8)
        self.assertEqual({p.name: p.read_bytes() for p in self.dir.iterdir()}, files)

    def test_a_snapshot_without_its_journal_is_refused(self):
        j = TurnJournal(self.dir)
        intent(j)
        self.path.unlink()
        with self.assertRaisesRegex(JournalError, 'refused without its journal'):
            TurnJournal(self.dir).turns()
        # An empty journal is no better.
        self.path.write_bytes(b'')
        with self.assertRaisesRegex(JournalError, 'refused without its journal'):
            TurnJournal(self.dir).turns()

    def test_a_journal_behind_its_snapshot_is_refused(self):
        j = TurnJournal(self.dir)
        intent(j)
        kept = self.path.read_bytes()
        intent(j, number=8, issue=8)
        self.path.write_bytes(kept)  # rolled back to an older copy
        with self.assertRaisesRegex(JournalError, 'does not reach its snapshot'):
            TurnJournal(self.dir).turns()

    def test_a_record_that_cannot_be_made_durable_is_an_error(self):
        j = TurnJournal(self.dir)
        first = intent(j)
        with mock.patch.object(journal_mod.os, 'fsync', side_effect=OSError('EIO')):
            with self.assertRaisesRegex(JournalError, 'intent not written'):
                intent(j, number=8, issue=8)
        self.assertEqual(list(j.turns()), [first])
        # The line written in part is cut back: the chain goes on, valid.
        second = intent(j, number=9, issue=9)
        self.assertEqual(list(TurnJournal(self.dir).turns()), [first, second])

    def test_a_turn_that_ended_without_a_durable_outcome_blocks_its_work(self):
        j = TurnJournal(self.dir)
        running = intent(j)
        # Running in this process, in a parallel action: it blocks nothing.
        self.assertEqual(j.blocking(CFG.repo, 7, 7), [])
        with mock.patch.object(journal_mod.os, 'fsync', side_effect=OSError('EIO')):
            with self.assertRaisesRegex(JournalError, 'outcome not written'):
                j.outcome(running, 'failed')
        # Ended, without a durable outcome: it blocks the work, here and in a new process.
        self.assertEqual([t['turn'] for t in j.blocking(CFG.repo, 7, 100)], [running])
        self.assertEqual([t['turn'] for t in TurnJournal(self.dir).blocking(CFG.repo, 7, 7)],
                         [running])
        self.assertEqual(j.blocking(CFG.repo, 8, 8), [])  # not another work
        self.assertIn('terminato senza che il suo esito sia stato scritto',
                      journal_mod.describe_turn(j.turns()[running]))

    def test_a_turn_the_recovery_cannot_make_uncertain_blocks_its_work(self):
        left = intent(TurnJournal(self.dir))  # the process that wrote it stopped
        restarted = TurnJournal(self.dir)
        with mock.patch.object(journal_mod.os, 'fsync', side_effect=OSError('EIO')):
            with self.assertRaisesRegex(JournalError, 'outcome not written'):
                restarted.uncertain(7)
        self.assertIsNone(restarted.turns()[left]['outcome'])
        self.assertEqual([t['turn'] for t in restarted.blocking(CFG.repo, 7, 7)], [left])

    def test_an_uncertain_number_stays_traced_until_it_is_settled(self):
        j = TurnJournal(self.dir)
        intent(j)
        restarted = TurnJournal(self.dir)
        self.assertEqual(restarted.pending(), [7])
        restarted.uncertain(7)
        self.assertEqual(TurnJournal(self.dir).pending(), [7])
        restarted.settle(7)
        self.assertEqual(TurnJournal(self.dir).pending(), [])

    def test_an_outcome_is_recorded_once(self):
        j = TurnJournal()
        turn = intent(j)
        j.outcome(turn, 'returned')
        with self.assertRaisesRegex(JournalError, 'already has outcome'):
            j.outcome(turn, 'failed')
        with self.assertRaises(ValueError):
            j.outcome(intent(j), 'success')  # not an outcome of the journal


class MigrationTests(unittest.TestCase):
    def test_the_old_trace_is_imported_once_as_uncertain_legacy_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / LEGACY).write_text(json.dumps({'7': 2, '100': 1, 'x': 3, '8': 0}))
            j = TurnJournal(folder)
            turns = j.turns()
            legacy = sorted((t['number'], t['legacy'], t['outcome']) for t in turns.values())
            # Number and count, explicitly uncertain; no outcome made up.
            self.assertEqual(legacy, [(7, 2, 'uncertain'), (100, 1, 'uncertain')])
            self.assertTrue(all(t['turn'].startswith(f'legacy-{t["number"]}-')
                                for t in turns.values()))
            self.assertFalse((folder / LEGACY).exists())
            self.assertEqual(sorted(j.pending()), [7, 100])
            # Once: a file that comes back is not imported again.
            (folder / LEGACY).write_text(json.dumps({'9': 1}))
            again = TurnJournal(folder)
            self.assertEqual(sorted(t['number'] for t in again.turns().values()), [7, 100])
            self.assertFalse((folder / LEGACY).exists())
            events = [json.loads(line)['event'] for line in lines(folder / JOURNAL)]
            self.assertEqual(events, ['migrated'])

    def test_a_legacy_turn_on_a_pr_is_linked_to_its_issue_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / LEGACY).write_text(json.dumps({'100': 1, '8': 1}))
            j = TurnJournal(tmp)
            (turn,) = [t for t, v in j.turns().items() if v['number'] == 100]
            (issue8,) = [t for t, v in j.turns().items() if v['number'] == 8]
            # Before the link, #100 may be a PR of any work: it blocks every one; with the
            # dispatcher's map it blocks the work of issue #7 alone.
            self.assertEqual(sorted(j.unlinked()), [8, 100])
            self.assertIn(turn, [t['turn'] for t in j.blocking(CFG.repo, 9, 9)])
            self.assertEqual(j.blocking(CFG.repo, 9, 9, {100: 7, 8: 8}), [])
            self.assertEqual([t['turn'] for t in j.blocking(CFG.repo, 7, 7, {100: 7, 8: 8})],
                             [turn])
            # An issue is linked to itself.
            self.assertEqual(sorted(j.link({100: 7, 8: 8}, CFG.repo)), sorted([turn, issue8]))
            self.assertEqual(j.link({100: 7}, CFG.repo), [])  # once
            self.assertEqual(j.unlinked(), [])
            again = TurnJournal(tmp)
            self.assertEqual((again.turns()[turn]['issue'], again.turns()[turn]['repo']),
                             (7, CFG.repo))
            # The issue and its other PRs, without the map: a restart, the PR closed.
            for number in (7, 101):
                self.assertEqual([t['turn'] for t in again.blocking(CFG.repo, 7, number)], [turn])
            self.assertEqual([t['number'] for t in again.blocking(CFG.repo, 9, 9)], [])

    def test_an_unreadable_old_trace_stops_the_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / LEGACY).write_text('[1, 2]')
            with self.assertRaisesRegex(JournalError, 'not a JSON object'):
                TurnJournal(tmp).turns()
            self.assertTrue((Path(tmp) / LEGACY).exists())


class ReconcileCommandTests(unittest.TestCase):
    def test_the_command_and_its_note(self):
        header = '> [!NOTE]\n> **Agent:** x\n\n'
        self.assertEqual(
            reconcile_command(header + '/devloop reconcile turn-3-0a1b2c3d failed\n'
                                       'Il processo è stato ucciso prima di pushare.\n'),
            ('turn-3-0a1b2c3d', 'failed', 'Il processo è stato ucciso prima di pushare.'))
        self.assertEqual(reconcile_command('/devloop reconcile legacy-7-0a1b2c3d returned'),
                         ('legacy-7-0a1b2c3d', 'returned', None))  # no note: malformed
        self.assertEqual(reconcile_command('/devloop reconcile turn-3-0a1b2c3d uncertain\nn'),
                         ('turn-3-0a1b2c3d', None, 'n'))
        self.assertEqual(reconcile_command('/devloop reconcile turn-3 returned\nn'),
                         (None, 'returned', 'n'))
        self.assertEqual(reconcile_command('/devloop reconcile turn-3-0a1b2c3d returned why\nn'),
                         (None, None, 'n'))
        self.assertIsNone(reconcile_command('/devloop retry'))
        self.assertIsNone(reconcile_command('text\n/devloop reconcile turn-3-0a1b2c3d failed'))

    def test_one_reconciliation_per_turn_and_never_of_a_turn_not_uncertain(self):
        j = TurnJournal()
        turn = intent(j)
        with self.assertRaisesRegex(ValueError, 'non è incerto'):
            j.reconcile(turn, 'failed', by='brushtyler', comment=1, note='n')
        j.uncertain(7)
        with self.assertRaisesRegex(ValueError, 'nota'):
            j.reconcile(turn, 'failed', by='brushtyler', comment=1, note='  ')
        with self.assertRaisesRegex(ValueError, 'non ammesso'):
            j.reconcile(turn, 'uncertain', by='brushtyler', comment=1, note='n')
        self.assertTrue(j.reconcile(turn, 'failed', by='brushtyler', comment=1, note='n'))
        self.assertFalse(j.reconcile(turn, 'failed', by='brushtyler', comment=2, note='n'))
        with self.assertRaisesRegex(ValueError, 'esito diverso è rifiutato'):
            j.reconcile(turn, 'returned', by='brushtyler', comment=3, note='n')
        done = j.turns()[turn]['reconciled']
        self.assertEqual((done['outcome'], done['by'], done['comment'], done['note']),
                         ('failed', 'brushtyler', 1, 'n'))
        self.assertRegex(done['at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        # Still counted: the turn keeps its intent and its uncertain outcome.
        self.assertEqual(j.turns()[turn]['outcome'], 'uncertain')
        with self.assertRaisesRegex(ValueError, 'non è nel journal'):
            j.reconcile('turn-99-0a1b2c3d', 'failed', by='brushtyler', comment=4, note='n')


class DispatcherJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def make(self, *outputs, cfg=CFG, number=7):
        self.gh = LedgerGH()
        self.ws = TreeWS()
        self.runner = ReportingRunner(*outputs, gh=self.gh, number=number)
        self.logs = []
        return Dispatcher(cfg, self.gh, self.ws, self.runner, log=self.logs.append,
                          ledger=Ledger(self.gh, state_dir=self.state))

    def journal(self):
        return TurnJournal(self.state)

    def escalations(self, number):
        found = [markdown.read_marker(c['body'], 'notice') for c in self.gh.thread.get(number, [])]
        return [n for n in found if n and n.get('event') == 'escalation']

    def crashed_implementation(self, cfg=CFG):
        """An implementation on issue 7 whose dispatcher stopped while the CLI ran, and the
        dispatcher started again; returns it after its first tick checked the turn."""
        d = self.make(Crash('dispatcher killed'))
        with self.assertRaises(Crash):
            d._do_implement(7)
        # The same repository, a new process.
        self.runner = ReportingRunner(raw_review(), gh=self.gh, number=100)
        restarted = Dispatcher(cfg, self.gh, self.ws, self.runner, log=self.logs.append,
                               ledger=Ledger(self.gh, state_dir=self.state))
        restarted._flag_incomplete(EMPTY, dry_run=False)
        return restarted

    def test_the_intent_is_durable_before_the_cli_starts(self):
        seen = []
        d = self.make(implemented())
        run = self.runner.run

        def check(*args):
            # What a new process would read now, while the CLI runs.
            seen.append([(t['kind'], t['number'], t['outcome'])
                         for t in TurnJournal(self.state).turns().values()])
            return run(*args)
        self.runner.run = check
        d._do_implement(7)
        self.assertEqual(seen, [[('implement', 7, None)]])
        (turn,) = self.journal().turns().values()
        self.assertEqual((turn['outcome'], turn['issue'], turn['repo'], turn['agent']),
                         ('returned', 7, CFG.repo, CFG.implementer))

    def test_a_turn_does_not_start_if_its_intent_cannot_be_written(self):
        d = self.make(raw_review(), number=100)
        with mock.patch.object(journal_mod.os, 'fsync', side_effect=OSError('EIO')):
            with self.assertRaises(GitHubError):
                d._do_review(pr())
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.gh.records(100, 'turn'), [])

    def test_a_turn_whose_record_cannot_be_written_is_not_dispatched(self):
        d = self.make(raw_review(), number=100)
        self.gh.fail = lambda body: markdown.read_marker(body, 'ledger') is not None
        with self.assertRaises(GitHubError):
            d._do_review(pr())
        self.assertEqual(self.runner.calls, [])
        (turn,) = self.journal().turns().values()
        self.assertEqual(turn['outcome'], 'not_dispatched')

    def test_failed_turns_are_failed_in_the_journal(self):
        from devloop.agents import AgentError
        d = self.make(AgentError('codex exited with 1'), number=100)
        with self.assertRaises(AgentError):
            d._do_review(pr())
        (turn,) = self.journal().turns().values()
        self.assertEqual((turn['kind'], turn['outcome']), ('review', 'failed'))

    def test_the_ledger_on_github_keeps_its_format(self):
        d = self.make(implemented())
        d._do_implement(7)
        (record,) = self.gh.records(7, 'turn')
        self.assertEqual(set(record), {
            'type', 'state', 'kind', 'agent', 'identity', 'mode', 'model', 'effort',
            'fallback_for', 'model_real', 'label', 'started_at', 'ended_at', 'duration_ms',
            'outcome', 'error', 'telemetry'})
        self.assertNotIn('turn-', self.gh.thread[7][-1]['body'])

    def test_an_interrupted_turn_becomes_uncertain_and_is_never_retried(self):
        d = self.crashed_implementation()
        (turn,) = self.journal().turns().values()
        self.assertEqual(turn['outcome'], 'uncertain')
        self.assertTrue(any(f'uncertain turn {turn["turn"]} on #7' in m for m in self.logs))
        self.assertEqual(self.gh.records(7, 'turn')[0]['state'], 'incomplete')
        self.assertEqual(self.journal().pending(), [])
        # Without the block the work goes on as before: a new turn of issue 7's PR runs.
        p = pr()
        self.gh.labels[100] = set(p.labels)
        d._issue_of[100] = 7
        d._do_review(p)
        self.assertEqual(len(self.runner.calls), 1)

    def test_with_the_block_a_new_turn_of_the_same_work_is_refused_with_an_escalation(self):
        d = self.crashed_implementation(cfg=BLOCKING)
        (uncertain,) = self.journal().turns().values()
        p = pr()  # the PR of issue 7, as the snapshot recognises it by its branch and marker
        self.gh.labels[100] = set(p.labels)
        d._issue_of[100] = 7
        error = d._run_action(Action('review', 100), {100: p})
        self.assertIsInstance(error, journal_mod.TurnBlocked)
        self.assertEqual(self.runner.calls, [])  # no CLI, no record, no intent
        self.assertEqual(self.gh.records(100, 'turn'), [])
        self.assertEqual(list(self.journal().turns()), [uncertain['turn']])
        (escalation,) = self.escalations(100)
        self.assertEqual(escalation['class'], 'uncertain_turn')
        body = self.gh.thread[100][-1]['body']
        self.assertIn(uncertain['turn'], body)
        self.assertIn('/devloop reconcile', body)
        self.assertIn('agent:needs-operator', self.gh.labels[100])
        # Another work is not blocked.
        other = pr(number=101, issue=8)
        self.gh.labels[101] = set(other.labels)
        d._issue_of[101] = 8
        self.runner.number = 101
        d._do_review(other)
        self.assertEqual(len(self.runner.calls), 1)

    def test_with_the_block_legacy_turns_block_their_number(self):
        (self.state / LEGACY).write_text(json.dumps({'7': 1}))
        d = self.make(implemented(), cfg=BLOCKING)
        with self.assertRaises(journal_mod.TurnBlocked) as caught:
            d._do_implement(7)
        self.assertRegex(caught.exception.turns[0]['turn'], r'^legacy-7-[0-9a-f]{8}$')
        self.assertEqual(self.runner.calls, [])
        # #7 is an issue (GitHub says so): linked to itself, it blocks no other work.
        self.assertEqual(self.journal().unlinked(), [])
        d._check_turns(8)

    @staticmethod
    def closed_pr(number=100, issue=7, author='brushtyler'):
        """A PR closed while the dispatcher was stopped, as ``find_pr`` reads it."""
        return {'number': number, 'title': 't', 'state': 'CLOSED',
                'body': markdown.publishable('b', 'pr', dict(META, issue=issue)),
                'labels': [], 'headRefName': f'agent/issue-{issue}', 'headRefOid': 'a' * 40,
                'author': {'login': author}, 'isCrossRepository': False}

    def test_with_the_block_a_legacy_turn_on_a_closed_pr_blocks_its_whole_work(self):
        # PR #100 of issue #7 closed before the first start: no open PR, no map to read.
        (self.state / LEGACY).write_text(json.dumps({'100': 1}))
        d = self.make(implemented(), cfg=BLOCKING)
        self.gh.any_prs = {100: self.closed_pr()}
        with mock.patch.object(self.gh, 'find_pr', side_effect=GitHubError('HTTP 502')):
            d._flag_incomplete(EMPTY, dry_run=False)
            # Its work not read yet: it blocks every new turn, of any work.
            with self.assertRaises(journal_mod.TurnBlocked) as caught:
                d._check_turns(8)
        self.assertIn('blocca ogni nuovo turno', str(caught.exception))
        self.assertEqual(self.journal().unlinked(), [100])
        # The next read links it to issue #7, durably, before any turn is admitted.
        with self.assertRaises(journal_mod.TurnBlocked) as caught:
            d._do_implement(7)
        self.assertRegex(caught.exception.turns[0]['turn'], r'^legacy-100-[0-9a-f]{8}$')
        (legacy,) = self.journal().turns().values()
        self.assertEqual((legacy['issue'], legacy['repo']), (7, CFG.repo))
        # A new process, GitHub unreachable: the journal ties #100 to the work of #7.
        restarted = Dispatcher(BLOCKING, self.gh, self.ws, self.runner, log=self.logs.append,
                               ledger=Ledger(self.gh, state_dir=self.state))
        with mock.patch.object(self.gh, 'find_pr', side_effect=GitHubError('HTTP 502')):
            with self.assertRaises(journal_mod.TurnBlocked):
                restarted._do_implement(7)
            restarted._issue_of[101] = 7  # another PR of the same work
            with self.assertRaises(journal_mod.TurnBlocked):
                restarted._check_turns(101)
            restarted._check_turns(8)  # another work is not blocked
        self.assertEqual(self.runner.calls, [])

    def test_with_the_block_a_legacy_turn_on_a_pr_of_no_work_blocks_its_number(self):
        # A PR devloop does not recognise (another author): its work is its number alone.
        (self.state / LEGACY).write_text(json.dumps({'100': 1}))
        d = self.make(implemented(), cfg=BLOCKING)
        self.gh.any_prs = {100: self.closed_pr(author='stranger')}
        d._do_implement(7)
        self.assertEqual(len(self.runner.calls), 1)
        (legacy,) = [t for t in self.journal().turns().values() if t.get('legacy')]
        self.assertEqual(legacy['issue'], 100)
        with self.assertRaises(journal_mod.TurnBlocked):
            d._check_turns(100)

    def test_with_the_block_a_legacy_turn_on_a_pr_blocks_its_whole_work(self):
        (self.state / LEGACY).write_text(json.dumps({'100': 1}))
        d = self.make(implemented(), cfg=BLOCKING)
        d._issue_of[100] = 7  # PR #100 of issue #7, as the snapshot recognises it
        with self.assertRaises(journal_mod.TurnBlocked):
            d._do_implement(7)
        d._flag_incomplete(EMPTY, dry_run=False)
        # A new process, the old PR closed: no map ties #100 to #7 any more, the journal does.
        restarted = Dispatcher(BLOCKING, self.gh, self.ws, self.runner, log=self.logs.append,
                               ledger=Ledger(self.gh, state_dir=self.state))
        with self.assertRaises(journal_mod.TurnBlocked) as caught:
            restarted._do_implement(7)
        self.assertRegex(caught.exception.turns[0]['turn'], r'^legacy-100-[0-9a-f]{8}$')
        restarted._issue_of[101] = 7  # another PR of the same work
        with self.assertRaises(journal_mod.TurnBlocked):
            restarted._check_turns(101)
        restarted._check_turns(8)  # another work is not blocked
        self.assertEqual(self.runner.calls, [])

    def test_with_the_block_a_turn_whose_outcome_is_not_written_stops_the_step(self):
        from devloop.agents import NoOutput
        for cfg, calls in ((BLOCKING, 1), (CFG, 2)):
            with self.subTest(block=cfg.block_uncertain_turns), \
                    tempfile.TemporaryDirectory() as tmp:
                self.state = Path(tmp)
                d = self.make(NoOutput('agy: run ended with no output'), raw_review(), cfg=cfg,
                              number=100)
                journal = d.ledger.journal
                append = journal._append

                def no_outcome(kind, data):
                    if kind == 'outcome':
                        raise JournalError('turn-journal.jsonl: outcome not written: EIO')
                    return append(kind, data)
                p = pr()
                self.gh.labels[100] = set(p.labels)
                d._issue_of[100] = 7
                with mock.patch.object(journal, '_append', side_effect=no_outcome):
                    error = d._run_action(Action('review', 100), {100: p})
                # With the block the retry of the same model never starts, and the step
                # escalates; without it the retry runs as before.
                self.assertEqual(len(self.runner.calls), calls)
                if cfg.block_uncertain_turns:
                    self.assertIsInstance(error, journal_mod.TurnBlocked)
                    (escalation,) = self.escalations(100)
                    self.assertEqual(escalation['class'], 'uncertain_turn')
                    (first,) = self.journal().turns().values()
                    self.assertIsNone(first['outcome'])
                    self.assertIn(first['turn'], self.gh.thread[100][-1]['body'])

    def test_a_failed_check_on_github_is_tried_again_at_the_next_tick(self):
        d = self.make(Crash('dispatcher killed'))
        with self.assertRaises(Crash):
            d._do_implement(7)
        restarted = Dispatcher(CFG, self.gh, self.ws, self.runner, log=self.logs.append,
                               ledger=Ledger(self.gh, state_dir=self.state))
        with mock.patch.object(self.gh, 'issue_comments', side_effect=GitHubError('HTTP 502')):
            restarted._flag_incomplete(EMPTY, dry_run=False)
        (turn,) = self.journal().turns().values()
        self.assertEqual(turn['outcome'], 'uncertain')
        self.assertEqual(self.gh.records(7, 'turn')[0]['state'], 'open')
        self.assertEqual(self.journal().pending(), [7])  # kept for the next tick
        again = Dispatcher(CFG, self.gh, self.ws, self.runner, log=self.logs.append,
                           ledger=Ledger(self.gh, state_dir=self.state))
        again._flag_incomplete(EMPTY, dry_run=False)
        self.assertEqual(self.gh.records(7, 'turn')[0]['state'], 'incomplete')
        self.assertEqual(self.journal().pending(), [])

    def test_with_the_block_a_conclave_call_is_refused_before_it_starts(self):
        from test_devloop_conclave import CCFG, ConclaveWS, HookRunner, ThreadGH
        from test_devloop_conclave import Base as ConclaveBase
        from test_devloop_conclave import proposal
        from helpers.devloop_fakes import FakeIdentities
        j = TurnJournal(self.state)
        left = intent(j)
        j.uncertain(7)  # a turn of issue 7's work, not reconciled
        self.gh = ThreadGH()
        self.runner = HookRunner(proposal())
        self.logs = []
        cfg =dataclasses.replace(CCFG, block_uncertain_turns=True)
        d = Dispatcher(cfg, self.gh, ConclaveWS(), self.runner, log=self.logs.append,
                       identities=FakeIdentities(self.gh),
                       ledger=Ledger(self.gh, state_dir=self.state))
        d._issue_of[100] = 7
        ConclaveBase.block_pr(self)  # PR #100 in agent:needs-operator, its fixer stopped
        target = pr(labels=('agent:needs-operator',))
        self.assertIsNotNone(d._conclave_view(100, self.gh.pr_comments(100), target))
        error = d._run_action(Action('conclave', 100), {100: target})
        self.assertIsInstance(error, journal_mod.TurnBlocked)
        self.assertEqual(self.runner.calls, [])
        # Nothing recorded as started, so nothing a later tick would count as missing.
        self.assertEqual(self.gh.conclaves(100), [])
        (escalation,) = self.escalations(100)
        self.assertEqual(escalation['class'], 'uncertain_turn')
        self.assertIn(left, self.gh.thread[100][-1]['body'])
        # The conclave of that block ends there: the item stays with the operator.
        self.assertIsNone(d._conclave_view(100, self.gh.pr_comments(100), target))
        self.assertIn('agent:needs-operator', self.gh.labels[100])

    def test_with_the_block_an_advisory_review_escalates_once_and_waits(self):
        from test_devloop_advisory import ON, AdvisoryWS, OperatorGH, answer
        from helpers.devloop_fakes import FakeIdentities
        from devloop.planning import plan
        from test_devloop import FakeRunner
        j = TurnJournal(self.state)
        left = intent(j)
        j.uncertain(7)  # a turn of issue 7's work, not reconciled
        cfg = dataclasses.replace(ON, block_uncertain_turns=True)
        # A ready PR whose merge is the operator's: the escalation takes the readiness away.
        self.gh = OperatorGH()
        self.gh.labels[100] = {'agent:needs-operator', 'agent:ready-to-merge'}
        ws, runner = AdvisoryWS(), FakeRunner(answer())
        d = Dispatcher(cfg, self.gh, ws, runner, log=lambda m: None,
                       identities=FakeIdentities(self.gh),
                       ledger=Ledger(self.gh, state_dir=self.state))

        def tick():
            snap = d.snapshot()
            prs = {p.number: p for p in snap.prs}
            errors = [d._run_action(a, prs) for a in plan(snap, cfg) if a.kind == 'advisory']
            return snap, errors

        snap, (error,) = tick()
        self.assertIsInstance(error, journal_mod.TurnBlocked)
        self.assertEqual((runner.calls, ws.copies), ([], []))  # refused before the checkout
        (escalation,) = self.escalations(100)
        self.assertEqual((escalation['class'], escalation['turns']), ('uncertain_turn', [left]))
        body = [c['body'] for c in self.gh.thread[100]
                if markdown.read_marker(c['body'], 'notice')][-1]
        self.assertIn(left, body)
        self.assertIn('/devloop reconcile', body)
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        # The turns already escalated: no other action nor escalation until they are reconciled.
        snap, errors = tick()
        self.assertEqual((snap.advisories, errors), ([], []))
        self.assertEqual(len(self.escalations(100)), 1)
        self.assertEqual(d._do_advisory(snap.prs[0]), None)
        self.assertEqual((runner.calls, ws.copies, len(self.escalations(100))), ([], [], 1))
        # A new uncertain turn of the same work is escalated in its turn.
        journal = d.ledger.journal  # the dispatcher's own: it keeps the journal it read
        other = intent(journal, number=100, kind='review')
        journal.uncertain(100)
        snap, (error,) = tick()
        self.assertIsInstance(error, journal_mod.TurnBlocked)
        self.assertEqual([e['turns'] for e in self.escalations(100)], [[left], [left, other]])
        # Reconciled, the advisory review runs.
        for turn in (left, other):
            journal.reconcile(turn, 'failed', by='brushtyler', comment=1,
                              note='la CLI non è partita')
        snap, errors = tick()
        self.assertEqual(errors, [None])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(self.escalations(100)), 2)

    def test_status_and_a_dry_run_beside_the_dispatcher_write_nothing_in_the_journal(self):
        from test_devloop_advisory import ON, AdvisoryWS, OperatorGH, answer
        from helpers.devloop_fakes import FakeIdentities
        from test_devloop import FakeRunner
        # A legacy turn on PR #100 still to link, the advisory review planned on it: the
        # planning checks the turns of its work, and links them.
        (self.state / LEGACY).write_text(json.dumps({'100': 1}))
        cfg = dataclasses.replace(ON, block_uncertain_turns=True)
        self.gh = OperatorGH()
        self.gh.labels[100] = {'agent:needs-operator'}

        def dispatcher(read_only):
            return Dispatcher(cfg, self.gh, AdvisoryWS(), FakeRunner(answer()), log=lambda m: None,
                              identities=FakeIdentities(self.gh),
                              ledger=Ledger(self.gh, state_dir=self.state, read_only=read_only))
        # The running dispatcher, with the lock, has read its journal and keeps it in memory.
        active = dispatcher(read_only=False)
        self.assertEqual(active.ledger.journal.unlinked(), [100])
        files = {p.name: p.read_bytes() for p in self.state.iterdir()}
        # Beside it, without the lock: `devloop status` and `devloop tick --dry-run`.
        for run in (lambda d: d.snapshot(), lambda d: d.tick(dry_run=True)):
            beside = dispatcher(read_only=True)
            run(beside)
            self.assertEqual(beside.ledger.journal.unlinked(), [])  # linked, in memory alone
            self.assertEqual({p.name: p.read_bytes() for p in self.state.iterdir()}, files)
        # The running dispatcher writes next, at its own tick: the chain stays valid, read again
        # from the file.
        active.snapshot()
        (legacy,) = TurnJournal(self.state).turns().values()
        self.assertEqual((legacy['issue'], legacy['repo']), (7, CFG.repo))
        self.assertEqual([json.loads(line)['event'] for line in lines(self.state / JOURNAL)],
                         ['migrated', 'linked'])

    def test_only_the_commands_with_the_lock_write_the_journal(self):
        import devloop.__main__ as devloop_main
        from devloop import apps
        from types import SimpleNamespace
        for command, dry_run, read_only in (('status', False, True), ('tick', True, True),
                                            ('tick', False, False), ('run', False, False)):
            with self.subTest(command=command, dry_run=dry_run), \
                    tempfile.TemporaryDirectory() as tmp:
                args = SimpleNamespace(config=None, local_config=None, command=command,
                                       dry_run=dry_run)
                with mock.patch.object(devloop_main, 'load_config', return_value=CFG), \
                        mock.patch.object(devloop_main, 'main_checkout', return_value=Path(tmp)), \
                        mock.patch.object(devloop_main.Identities, 'from_config',
                                          return_value=apps.Identities(CFG.repo, {})):
                    _, _, d = devloop_main.build(args)
                self.assertIs(d.ledger.journal.read_only, read_only)

    def test_without_the_block_legacy_turns_are_checked_as_before(self):
        (self.state / LEGACY).write_text(json.dumps({'100': 1}))
        d = self.make(number=100)
        self.gh.comment(100, markdown.publishable('x', 'ledger', {
            'type': 'turn', 'state': 'open', 'kind': 'review', 'agent': REV,
            'started_at': '2026-11-18T10:00:00Z'}))
        self.assertEqual(d.ledger.pending(), [100])
        d._flag_incomplete(EMPTY, dry_run=False)
        self.assertEqual(self.gh.records(100, 'turn')[0]['state'], 'incomplete')
        self.assertEqual(d.ledger.pending(), [])

    def reconcile(self, number, body, login='brushtyler'):
        self.gh.comment(number, body)
        self.gh.thread[number][-1]['author'] = {'login': login}
        return self.gh.thread[number][-1]['id']

    def test_reconcile_only_by_a_trusted_user_once_with_its_note(self):
        d = self.crashed_implementation(cfg=BLOCKING)
        (turn,) = self.journal().turns()
        command = f'/devloop reconcile {turn} failed'
        # Neither an agent's app nor a stranger.
        for login in ('brushtyler-devloop-claude-agent[bot]', 'stranger'):
            self.reconcile(7, command + '\nNota.', login=login)
        # A trusted user without the note: refused, answered once.
        self.reconcile(7, command)
        snap = Snapshot(prs=[pr()], ready_issues=[], working_issues=[])
        d._reconcile_turns(snap, dry_run=False)
        d._reconcile_turns(snap, dry_run=False)
        self.assertIsNone(self.journal().turns()[turn]['reconciled'])
        answers = [c for c in self.gh.thread[7] if 'Riconciliazione non applicata' in c['body']]
        self.assertEqual(len(answers), 1)
        self.assertIn('nota', answers[0]['body'])
        self.assertTrue(any('ignored `/devloop reconcile` by stranger' in m for m in self.logs))
        # On the PR of the same work: applied, then a different outcome is refused.
        applied = self.reconcile(100, command + '\nIl processo è stato ucciso dopo il push.')
        self.reconcile(100, f'/devloop reconcile {turn} returned\nAnzi no.')
        d._reconcile_turns(snap, dry_run=False)
        done = self.journal().turns()[turn]['reconciled']
        self.assertEqual((done['outcome'], done['by'], done['comment'], done['note']),
                         ('failed', 'brushtyler', applied,
                          'Il processo è stato ucciso dopo il push.'))
        refused = [c for c in self.gh.thread[100] if 'Riconciliazione non applicata' in c['body']]
        self.assertEqual(len(refused), 1)
        self.assertIn('esito diverso è rifiutato', refused[0]['body'])
        # Reconciled: the work runs again, and the turn stays counted.
        p = pr()
        self.gh.labels[100] = set(p.labels)
        d._issue_of[100] = 7
        d._do_review(p)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.journal().turns()[turn]['outcome'], 'uncertain')

    def test_reconcile_records_the_outcome_with_the_block_off(self):
        d = self.crashed_implementation()
        (turn,) = self.journal().turns()
        self.reconcile(7, f'/devloop reconcile {turn} not_dispatched\nMai partito.')
        d._reconcile_turns(EMPTY, dry_run=True)
        self.assertIsNone(self.journal().turns()[turn]['reconciled'])
        d._reconcile_turns(EMPTY, dry_run=False)
        self.assertEqual(self.journal().turns()[turn]['reconciled']['outcome'], 'not_dispatched')


class ConfigTests(unittest.TestCase):
    def test_the_block_is_off_by_default(self):
        self.assertFalse(load_config().block_uncertain_turns)
        data = load_data()
        data.pop('block_uncertain_turns', None)
        self.assertFalse(parse_config(data).block_uncertain_turns)
        self.assertTrue(parse_config(dict(data, block_uncertain_turns=True)).block_uncertain_turns)
        with self.assertRaises(ConfigError):
            parse_config(dict(data, block_uncertain_turns='yes'))


if __name__ == '__main__':
    unittest.main()
