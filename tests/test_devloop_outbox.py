"""devloop: the outbox of the publications (#385, ADR 0004, P2-4)."""

import inspect
import json
import tempfile
import unittest
from pathlib import Path

from devloop import outbox as outbox_mod
from devloop import ports
from devloop.apps import Identities
from devloop.config import ConfigError, parse_config, load_data
from devloop.dispatcher import Dispatcher
from devloop.github import GitHubError, GitHubUncertain
from devloop.journal import JOURNAL, JournalError, TurnJournal
from devloop.ledger import Ledger
from devloop.outbox import (DROPPED, FAILED, SENT, UNCERTAIN, Outbox, OutboxClient,
                            for_identities)
from helpers.devloop_fakes import FIRST_COMMENT_ID, FakeGH

REPO = 'brushtyler/ssdlc'


class Crash(BaseException):
    """The dispatcher stops right there (a kill, a power cut): nothing after it runs."""


class OutboxGH(FakeGH):
    """FakeGH with comment ids, whose writes may crash the process (``crash``: ``'applied'``
    or ``'lost'``) and whose reads may fail (``read_fails``). It records each body sent."""

    def __init__(self):
        super().__init__()
        self.with_ids = True
        self.crash = None
        self.read_fails = 0
        self.sent = []

    def comment(self, number, body):
        self.sent.append(body)
        if self.crash is not None:
            how, self.crash = self.crash, None
            if how == 'applied':
                self.comments.append((number, body))
            raise Crash()
        return super().comment(number, body)

    def edit_comment(self, comment_id, body):
        self.sent.append(body)
        return super().edit_comment(comment_id, body)

    def issue_comments(self, number):
        if self.read_fails:
            self.read_fails -= 1
            raise GitHubError('read failed')
        return super().issue_comments(number)

    def bodies(self, number):
        return [b for n, b in self.comments if n == number]


def make(gh, state=None, journal=None):
    """``(outbox, client of the dispatcher)`` over ``gh``, its journal in ``state``."""
    journal = journal if journal is not None else TurnJournal(state)
    box = Outbox(journal, lambda actor, permissions: gh, lambda actor: ['brushtyler'])
    return box, box.client(gh, 'dispatcher', {'issues': 'write'})


def statuses(box):
    return [e['status'] for e in box.entries().values()]


def journal_events(state):
    return [json.loads(line) for line in (Path(state) / JOURNAL).read_text().splitlines()]


class JournalBeforePublicationTests(unittest.TestCase):
    """Criterion 1: each record, output and receipt is in the journal before it is sent."""

    def test_every_write_is_durable_before_it_is_sent(self):
        with tempfile.TemporaryDirectory() as state:
            seen = []

            class Checking(OutboxGH):
                def comment(self, number, body):
                    seen.append(self._journaled(body))
                    return super().comment(number, body)

                def edit_comment(self, comment_id, body):
                    seen.append(self._journaled(body))
                    return super().edit_comment(comment_id, body)

                def _journaled(self, body):
                    # Read from disk: the event is durable, not only in memory.
                    return any(e['event'] == 'publication' and e['data']['body'] == body
                               for e in journal_events(state))

            gh = Checking()
            box, client = make(gh, state)
            ledger = Ledger(client, state_dir=state)
            ledger.journal = box.journal
            # Ledger records: the request, a turn opened and closed (an edit), an event.
            ledger.request(7, title='t', body='b', labeled=None, comments=[])
            entry = ledger.open_turn(7, kind='implement', agent='claude', identity=None,
                                     mode='write', model='m', effort=None, fallback_for=None,
                                     label='l', repo=REPO)
            ledger.close_turn(entry, 'success')
            ledger.event(100, 'ci_round', round_no=1, head='a' * 40, state='pass')
            # An agent's output, written with its own client, and a receipt.
            agent = box.client(gh, 'claude', {'pull_requests': 'write'})
            agent.comment(100, 'review of claude')
            client.comment(100, 'receipt of the promotion')
            self.assertEqual(len(seen), 6)
            self.assertTrue(all(seen), seen)
            self.assertEqual(statuses(box), [SENT] * 6)
            published = [e['data'] for e in journal_events(state)
                         if e['event'] == 'publication']
            self.assertEqual([(p['actor'], p['op']) for p in published],
                             [('dispatcher', 'comment'), ('dispatcher', 'comment'),
                              ('dispatcher', 'edit'), ('dispatcher', 'comment'),
                              ('claude', 'comment'), ('dispatcher', 'comment')])
            # The turn's record is edited on the comment the outbox published.
            self.assertEqual(published[2]['comment_id'], entry.comment_id)
            self.assertEqual(published[2]['number'], 7)

    def test_nothing_is_sent_when_the_journal_cannot_be_written(self):
        gh = OutboxGH()
        with tempfile.TemporaryDirectory() as state:
            _, client = make(gh, journal=TurnJournal(state, read_only=True))
            with self.assertRaises(JournalError):
                client.comment(7, 'record')
            self.assertEqual(gh.sent, [])
        # A JournalError is a GitHubError: the caller handles it as a failed write.
        self.assertTrue(issubclass(JournalError, GitHubError))

    def test_reads_and_other_methods_reach_the_client(self):
        gh = OutboxGH()
        _, client = make(gh)
        gh.labels[7] = {'agent:ready'}
        self.assertEqual(client.issue(7)['number'], 7)
        client.edit_labels(7, add=['x'], remove=[])
        self.assertIn('x', gh.labels[7])
        client.time_left = 5  # set on the client, as devloop watch does
        self.assertEqual(gh.time_left, 5)


class UncertainPostTests(unittest.TestCase):
    """Criterion 2: an uncertain POST is reconciled, never repeated blindly."""

    def test_an_uncertain_post_that_took_effect_is_found_and_not_sent_again(self):
        gh = OutboxGH()
        gh.uncertain_writes = ['applied']
        box, client = make(gh)
        self.assertEqual(client.comment(7, 'record'), FIRST_COMMENT_ID)
        self.assertEqual(gh.sent, ['record'])
        self.assertEqual(gh.bodies(7), ['record'])
        [entry] = box.entries().values()
        self.assertEqual((entry['status'], entry['sends']), (SENT, 1))

    def test_an_uncertain_post_that_was_lost_is_published_once(self):
        gh = OutboxGH()
        gh.uncertain_writes = ['lost']
        box, client = make(gh)
        self.assertEqual(client.comment(7, 'record'), FIRST_COMMENT_ID)
        self.assertEqual(gh.sent, ['record', 'record'])
        self.assertEqual(gh.bodies(7), ['record'])
        [entry] = box.entries().values()
        self.assertEqual((entry['status'], entry['sends']), (SENT, 2))

    def test_after_two_sends_the_outcome_stays_uncertain(self):
        gh = OutboxGH()
        gh.uncertain_writes = ['lost', 'lost']
        box, client = make(gh)
        with self.assertRaises(GitHubUncertain):
            client.comment(7, 'record')
        self.assertEqual(len(gh.sent), 2)
        self.assertEqual(statuses(box), [UNCERTAIN])
        # The next write on #7 reconciles it first: not there, and its caller was told it
        # failed, so it is dropped, not sent a third time.
        client.comment(7, 'next')
        self.assertEqual(gh.bodies(7), ['next'])
        self.assertEqual(gh.sent, ['record', 'record', 'next'])
        self.assertEqual(statuses(box), [DROPPED, SENT])

    def test_an_unreadable_reconciliation_blocks_the_next_post(self):
        gh = OutboxGH()
        gh.uncertain_writes = ['applied']
        gh.read_fails = 2
        box, client = make(gh)
        with self.assertRaises(GitHubUncertain):
            client.comment(7, 'record')
        self.assertEqual(statuses(box), [UNCERTAIN])
        # Still not readable: the new POST on #7 is not sent.
        with self.assertRaises(GitHubError):
            client.comment(7, 'next')
        self.assertEqual(gh.sent, ['record'])
        # Readable again: the first is found, then the new one is sent.
        client.comment(7, 'next')
        self.assertEqual(gh.bodies(7), ['record', 'next'])
        self.assertEqual(gh.sent, ['record', 'next'])
        self.assertEqual(statuses(box), [SENT, SENT])

    def test_another_publication_with_the_same_body_is_not_taken_for_it(self):
        gh = OutboxGH()
        box, client = make(gh)
        client.comment(7, 'same')
        gh.uncertain_writes = ['lost']
        self.assertEqual(client.comment(7, 'same'), FIRST_COMMENT_ID + 1)
        self.assertEqual(gh.bodies(7), ['same', 'same'])
        self.assertEqual([e['sent_id'] for e in box.entries().values()],
                         [FIRST_COMMENT_ID, FIRST_COMMENT_ID + 1])

    def test_a_comment_of_another_author_does_not_count(self):
        gh = OutboxGH()
        gh.comments.append((7, 'record'))  # the same body, written by brushtyler
        box = Outbox(TurnJournal(), lambda a, p: gh, lambda actor: ['devloop-dispatcher[bot]'])
        client = box.client(gh, 'dispatcher')
        gh.uncertain_writes = ['lost', 'lost']
        with self.assertRaises(GitHubUncertain):
            client.comment(7, 'record')
        self.assertEqual(statuses(box), [UNCERTAIN])
        self.assertEqual(len(gh.sent), 2)

    def test_a_certain_failure_is_not_retried(self):
        class Refusing(OutboxGH):
            def comment(self, number, body):
                self.sent.append(body)
                raise GitHubError('HTTP 422')
        gh = Refusing()
        box, client = make(gh)
        with self.assertRaises(GitHubError):
            client.comment(7, 'record')
        self.assertEqual(statuses(box), [FAILED])
        self.assertEqual(box.resume(), [])
        self.assertEqual(gh.sent, ['record'])

    def test_an_uncertain_edit_is_reconciled_on_its_comment(self):
        gh = OutboxGH()
        box, client = make(gh)
        first = client.comment(7, 'v1')
        gh.uncertain_writes = ['applied']
        client.edit_comment(first, 'v2')
        self.assertEqual(gh.sent, ['v1', 'v2'])
        gh.uncertain_writes = ['lost']
        client.edit_comment(first, 'v3')
        self.assertEqual(gh.sent, ['v1', 'v2', 'v3', 'v3'])
        self.assertEqual(gh.bodies(7), ['v3'])
        self.assertEqual(statuses(box), [SENT, SENT, SENT])


class RestartTests(unittest.TestCase):
    """Criterion 3: a restart in the middle resumes from the outbox, without duplicates."""

    def restart(self, gh, state):
        return make(gh, state)[0]

    def test_a_post_cut_short_after_it_took_effect_is_not_sent_again(self):
        gh = OutboxGH()
        with tempfile.TemporaryDirectory() as state:
            _, client = make(gh, state)
            gh.crash = 'applied'
            with self.assertRaises(Crash):
                client.comment(7, 'record')
            box = self.restart(gh, state)
            self.assertEqual(box.resume(), [])
            self.assertEqual(gh.bodies(7), ['record'])
            self.assertEqual(gh.sent, ['record'])
            self.assertEqual(statuses(box), [SENT])
            # A third process finds nothing to do.
            box = self.restart(gh, state)
            self.assertEqual((box.resume(), box.pending()), ([], []))
            self.assertEqual(gh.bodies(7), ['record'])

    def test_a_post_cut_short_before_it_took_effect_is_sent_once(self):
        gh = OutboxGH()
        with tempfile.TemporaryDirectory() as state:
            _, client = make(gh, state)
            gh.crash = 'lost'
            with self.assertRaises(Crash):
                client.comment(7, 'record')
            box = self.restart(gh, state)
            box.resume()
            box = self.restart(gh, state)
            box.resume()
            self.assertEqual(gh.bodies(7), ['record'])
            self.assertEqual(gh.sent, ['record', 'record'])
            self.assertEqual(statuses(box), [SENT])

    def test_a_write_recorded_and_never_sent_is_sent_at_the_restart(self):
        class CrashBeforeSending(TurnJournal):
            def publication(self, kind, data):
                if kind == 'publication_sending':
                    raise Crash()
                return super().publication(kind, data)

        gh = OutboxGH()
        with tempfile.TemporaryDirectory() as state:
            _, client = make(gh, journal=CrashBeforeSending(state))
            with self.assertRaises(Crash):
                client.comment(7, 'record')
            self.assertEqual(gh.sent, [])
            box = self.restart(gh, state)
            box.resume()
            box.resume()
            self.assertEqual(gh.bodies(7), ['record'])
            self.assertEqual(statuses(box), [SENT])

    def test_a_new_write_after_the_restart_settles_the_pending_one_first(self):
        gh = OutboxGH()
        with tempfile.TemporaryDirectory() as state:
            _, client = make(gh, state)
            gh.crash = 'lost'
            with self.assertRaises(Crash):
                client.comment(7, 'first')
            _, client = make(gh, state)
            client.comment(7, 'second')  # no resume: the write settles #7 itself
            self.assertEqual(gh.bodies(7), ['first', 'second'])

    def test_a_ledger_turn_cut_short_is_published_once_and_closed(self):
        gh = OutboxGH()
        with tempfile.TemporaryDirectory() as state:
            box, client = make(gh, state)
            ledger = Ledger(client, state_dir=state)
            ledger.journal = box.journal
            gh.crash = 'applied'
            with self.assertRaises(Crash):
                ledger.open_turn(7, kind='implement', agent='claude', identity=None,
                                 mode='write', model='m', effort=None, fallback_for=None,
                                 label='l', repo=REPO)
            box = self.restart(gh, state)
            self.assertEqual(box.resume(), [])
            [(number, body)] = gh.comments
            self.assertEqual(number, 7)
            self.assertIn('Turno in corso', body)
            # The turn of the journal is left without outcome: the tick makes it uncertain.
            self.assertEqual(box.journal.pending(), [7])

    def test_an_edit_superseded_before_the_restart_is_dropped(self):
        gh = OutboxGH()
        with tempfile.TemporaryDirectory() as state:
            first = gh.comment(7, 'v1')  # a comment the outbox never saw: its issue unknown
            _, client = make(gh, state)
            gh.uncertain_writes = ['lost', 'lost']
            with self.assertRaises(GitHubUncertain):
                client.edit_comment(first, 'v2')
            client.edit_comment(first, 'v3')
            box = self.restart(gh, state)
            self.assertEqual(box.resume(), [])
            # v2 is not applied over v3, which was built after it.
            self.assertEqual(statuses(box), [DROPPED, SENT])
            self.assertEqual(gh.bodies(7), ['v3'])
            self.assertEqual(gh.sent, ['v1', 'v2', 'v2', 'v3'])


class WiringTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(load_data()['publication_outbox'])
        self.assertFalse(parse_config(load_data()).publication_outbox)
        self.assertTrue(parse_config(dict(load_data(), publication_outbox=True))
                        .publication_outbox)
        with self.assertRaises(ConfigError):
            parse_config(dict(load_data(), publication_outbox='yes'))

    def test_identities_write_through_the_outbox_when_it_is_set(self):
        identities = Identities('o/r', {})
        self.assertNotIsInstance(identities.github('dispatcher'), OutboxClient)
        identities.outbox = for_identities(identities, TurnJournal(), ['brushtyler'])
        client = identities.github('claude', {'issues': 'write'})
        self.assertIsInstance(client, OutboxClient)
        self.assertIsInstance(client, ports.Backend)
        for port in ports.PORTS:
            for name in ports.port_methods(port):
                with self.subTest(method=name):
                    self.assertEqual(inspect.signature(getattr(OutboxClient, name)),
                                     inspect.signature(getattr(port, name)))
        self.assertNotIsInstance(identities.direct('claude'), OutboxClient)
        # The ambient login is the operator's: one of the trusted users.
        self.assertEqual(identities.outbox._authors('claude'), ['brushtyler'])

    def test_build_sets_the_outbox_only_when_on_and_with_the_lock(self):
        import dataclasses
        from types import SimpleNamespace
        from unittest import mock

        import devloop.__main__ as devloop_main
        from test_devloop import CFG
        for on, command, dry_run, expected in ((False, 'run', False, False),
                                               (True, 'run', False, True),
                                               (True, 'tick', False, True),
                                               (True, 'tick', True, False),
                                               (True, 'status', False, False)):
            with self.subTest(on=on, command=command, dry_run=dry_run), \
                    tempfile.TemporaryDirectory() as tmp:
                cfg = dataclasses.replace(CFG, publication_outbox=on)
                args = SimpleNamespace(config=None, local_config=None, command=command,
                                       dry_run=dry_run)
                with mock.patch.object(devloop_main, 'load_config', return_value=cfg), \
                        mock.patch.object(devloop_main, 'main_checkout', return_value=Path(tmp)), \
                        mock.patch.object(devloop_main.Identities, 'from_config',
                                          return_value=Identities(cfg.repo, {})):
                    _, _, d = devloop_main.build(args)
                box = d.identities.outbox
                self.assertIs(box is not None, expected)
                self.assertIs(isinstance(d.gh, OutboxClient), expected)
                self.assertIs(d.ledger.gh, d.gh)
                if expected:
                    self.assertIs(box.journal, d.ledger.journal)

    def test_the_tick_resumes_the_outbox(self):
        lines = []

        class Box:
            def resume(self):
                return ['  publication pub-1 (comment on #7) not settled: down']

        class Stub:
            identities = type('I', (), {'outbox': Box()})()
            log = lines.append
        Dispatcher._resume_publications(Stub())
        self.assertEqual(lines, ['  publication pub-1 (comment on #7) not settled: down'])
        # Without an outbox, nothing happens.
        Stub.identities = None
        Dispatcher._resume_publications(Stub())
        self.assertEqual(len(lines), 1)

    def test_the_turns_ignore_the_publications(self):
        journal = TurnJournal()
        box, client = make(OutboxGH(), journal=journal)
        client.comment(7, 'x')
        self.assertEqual((journal.turns(), journal.pending()), ({}, []))
        with self.assertRaises(ValueError):
            journal.publication('outcome', {})
        self.assertEqual(outbox_mod.MAX_SENDS, 2)


if __name__ == '__main__':
    unittest.main()
