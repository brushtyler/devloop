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
from devloop.outbox import (DROPPED, FAILED, IN_FLIGHT_SECONDS, SENT, UNCERTAIN, Outbox,
                            OutboxClient, for_identities)
from helpers.devloop_fakes import FIRST_COMMENT_ID, SHA1, ActingAs, FakeGH, FakeIdentities

REPO = 'brushtyler/ssdlc'
START = 1_800_000_000.0
# After a crash the end of the attempt is unknown: absence is conclusive only this much later.
AFTER_A_CRASH = outbox_mod.ATTEMPT_SECONDS + IN_FLIGHT_SECONDS


class Crash(BaseException):
    """The dispatcher stops right there (a kill, a power cut): nothing after it runs."""


class Clock:
    """The time of the outbox and of the fake GitHub; ``sleep`` moves it on."""

    def __init__(self):
        self.now = START
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class OutboxGH(FakeGH):
    """FakeGH with comment ids, whose writes may crash the process (``crash``: ``'applied'``,
    ``'lost'`` or ``'late'``: GitHub applies it 10 seconds later) and whose reads may fail
    (``read_fails``). An uncertain ``comment`` may also be ``'late'``. It records each body
    sent; the pull requests it opens are open."""

    def __init__(self):
        super().__init__()
        self.with_ids = True
        self.clock = Clock()
        self.crash = None
        self.read_fails = 0
        self.sent = []
        self.in_flight = []  # (time it takes effect, how): writes GitHub still serves

    def _write(self, body, apply):
        self.sent.append(body)
        how, self.crash = self.crash, None
        if how is None:
            return apply()
        if how == 'applied':
            apply()
        elif how == 'late':
            self.in_flight.append((self.clock() + 10, apply))
        raise Crash()

    def _land(self):
        for item in [i for i in self.in_flight if i[0] <= self.clock()]:
            self.in_flight.remove(item)
            item[1]()

    def comment(self, number, body):
        if self.uncertain_writes and self.uncertain_writes[0] == 'late':
            self.uncertain_writes = list(self.uncertain_writes[1:])
            self.sent.append(body)
            self.in_flight.append((self.clock() + 10,
                                   lambda: self.comments.append((number, body))))
            raise GitHubUncertain(f'comment on #{number}: timed out')
        return self._write(body, lambda: FakeGH.comment(self, number, body))

    def edit_comment(self, comment_id, body):
        self.sent.append(body)
        return super().edit_comment(comment_id, body)

    def submit_review(self, number, *, commit, event, body):
        return self._write(body, lambda: FakeGH.submit_review(
            self, number, commit=commit, event=event, body=body))

    def create_pr(self, *, base, head, title, body):
        def apply():
            number = 100 + len(self.created)
            self.created.append({'base': base, 'head': head, 'title': title, 'body': body})
            self.open = [*self.open, {'number': number, 'headRefName': head, 'title': title,
                                      'body': body, 'author': {'login': 'brushtyler'}}]
            return number, f'https://example/pr/{number}'
        return self._write(body, apply)

    def issue_comments(self, number):
        self._land()
        if self.read_fails:
            self.read_fails -= 1
            raise GitHubError('read failed')
        return super().issue_comments(number)

    def pr_reviews(self, number):
        self._land()
        return super().pr_reviews(number)

    def open_prs(self):
        self._land()
        return super().open_prs()

    def bodies(self, number):
        return [b for n, b in self.comments if n == number]


class FailingJournal(TurnJournal):
    """A journal that does not take the events whose kind is in ``fail`` (a full disk), or
    stops the process right after writing those in ``crash_after``."""

    fail = ()
    crash_after = ()

    def _write(self, kind, data):
        if kind in self.fail:
            raise JournalError(f'{kind} not written: no space left on device')
        event = super()._write(kind, data)
        if kind in self.crash_after:
            raise Crash()
        return event


def make(gh, state=None, journal=None):
    """``(outbox, client of the dispatcher)`` over ``gh``, its journal in ``state``."""
    journal = journal if journal is not None else TurnJournal(state)
    box = Outbox(journal, lambda actor, permissions: gh, lambda actor: ['brushtyler'],
                 clock=gh.clock, sleep=gh.clock.sleep)
    return box, box.client(gh, 'dispatcher', {'issues': 'write'})


def statuses(box):
    return [e['status'] for e in box.entries().values()]


def journal_events(state):
    return [json.loads(line) for line in (Path(state) / JOURNAL).read_text().splitlines()]


def open_turn(ledger):
    return ledger.open_turn(7, kind='implement', agent='claude', identity=None, mode='write',
                            model='m', effort=None, fallback_for=None, label='l', repo=REPO)


class Refusing(OutboxGH):
    def comment(self, number, body):
        self.sent.append(body)
        raise GitHubError('HTTP 422')


class JournalBeforePublicationTests(unittest.TestCase):
    """Criterion 1: each record, output and receipt is in the journal before it is sent."""

    def test_every_write_is_durable_before_it_is_sent(self):
        with tempfile.TemporaryDirectory() as state:
            seen = []

            def journaled(body):
                # Read from disk: the event is durable, not only in memory.
                return any(e['event'] == 'publication' and e['data']['body'] == body
                           for e in journal_events(state))

            class Checking(OutboxGH):
                def comment(self, number, body):
                    seen.append(journaled(body))
                    return super().comment(number, body)

                def edit_comment(self, comment_id, body):
                    seen.append(journaled(body))
                    return super().edit_comment(comment_id, body)

                def submit_review(self, number, *, commit, event, body):
                    seen.append(journaled(body))
                    return super().submit_review(number, commit=commit, event=event, body=body)

                def create_pr(self, *, base, head, title, body):
                    seen.append(journaled(body))
                    return super().create_pr(base=base, head=head, title=title, body=body)

            gh = Checking()
            box, client = make(gh, state)
            ledger = Ledger(client, state_dir=state)
            ledger.journal = box.journal
            # Ledger records: the request, a turn opened and closed (an edit), an event.
            ledger.request(7, title='t', body='b', labeled=None, comments=[])
            entry = open_turn(ledger)
            ledger.close_turn(entry, 'success')
            ledger.event(100, 'ci_round', round_no=1, head='a' * 40, state='pass')
            # The agents' outputs, each written with its own client: the implementer's pull
            # request, a native review, a review comment; then a receipt.
            agent = box.client(gh, 'claude', {'pull_requests': 'write'})
            self.assertEqual(agent.create_pr(base='main', head='agent/issue-7', title='t',
                                             body='pr of claude'),
                             (100, 'https://example/pr/100'))
            self.assertIsNone(agent.submit_review(100, commit=SHA1, event='APPROVE',
                                                  body='native review of claude'))
            agent.comment(100, 'review of claude')
            client.comment(100, 'receipt of the promotion')
            self.assertEqual(len(seen), 8)
            self.assertTrue(all(seen), seen)
            self.assertEqual(statuses(box), [SENT] * 8)
            published = [e['data'] for e in journal_events(state)
                         if e['event'] == 'publication']
            self.assertEqual([(p['actor'], p['op']) for p in published],
                             [('dispatcher', 'comment'), ('dispatcher', 'comment'),
                              ('dispatcher', 'edit'), ('dispatcher', 'comment'),
                              ('claude', 'pr'), ('claude', 'review'), ('claude', 'comment'),
                              ('dispatcher', 'comment')])
            # The turn's record is edited on the comment the outbox published.
            self.assertEqual(published[2]['comment_id'], entry.comment_id)
            self.assertEqual(published[2]['number'], 7)
            self.assertEqual((published[4]['base'], published[4]['head']),
                             ('main', 'agent/issue-7'))
            self.assertEqual((published[5]['number'], published[5]['commit'],
                              published[5]['event']), (100, SHA1, 'APPROVE'))

    def test_the_dispatcher_s_pull_request_and_native_review_go_through_the_outbox(self):
        # The paths the outputs take in the dispatcher (R1-1): the implementer opens its PR,
        # the reviewer with an app submits its native review, each with its own client.
        from test_devloop import CFG, FakeRunner, FakeWS, implemented, pr, raw_review

        with tempfile.TemporaryDirectory() as state:
            seen = []

            def journaled(op, body):
                return any(e['event'] == 'publication' and e['data']['op'] == op
                           and e['data']['body'] == body for e in journal_events(state))

            class Checking(FakeGH):
                def create_pr(self, *, base, head, title, body):
                    seen.append(('pr', journaled('pr', body)))
                    return super().create_pr(base=base, head=head, title=title, body=body)

                def submit_review(self, number, *, commit, event, body):
                    seen.append(('review', journaled('review', body)))
                    return super().submit_review(number, commit=commit, event=event,
                                                 body=body)

            gh = Checking()

            class WithOutbox(FakeIdentities):
                def __init__(self, fake):
                    super().__init__(fake)
                    self.outbox = Outbox(TurnJournal(state),
                                         lambda a, p: ActingAs(fake, a, p),
                                         lambda a: [self.LOGINS.get(a, 'brushtyler')])

                def github(self, actor, permissions=None):
                    return self.outbox.client(ActingAs(self.fake, actor, permissions), actor,
                                              permissions)

            ids = WithOutbox(gh)
            Dispatcher(CFG, gh, FakeWS(), FakeRunner(implemented()), log=lambda m: None,
                       identities=ids)._do_implement(7)
            Dispatcher(CFG, gh, FakeWS(), FakeRunner(raw_review()), log=lambda m: None,
                       identities=ids)._do_review(pr())
            self.assertEqual(seen, [('pr', True), ('review', True)])
            self.assertEqual((len(gh.created), len(gh.native)), (1, 1))
            entries = list(ids.outbox.entries().values())
            self.assertEqual([(e['actor'], e['op'], e['status']) for e in entries],
                             [(CFG.implementer, 'pr', SENT), (CFG.reviewer, 'review', SENT)])
            self.assertEqual(entries[0]['sent_id'], 100)

    def test_nothing_is_sent_when_the_journal_cannot_be_written(self):
        gh = OutboxGH()
        with tempfile.TemporaryDirectory() as state:
            _, client = make(gh, journal=TurnJournal(state, read_only=True))
            with self.assertRaises(JournalError):
                client.comment(7, 'record')
            self.assertEqual(gh.sent, [])
        # A JournalError is a GitHubError: the caller handles it as a failed write.
        self.assertTrue(issubclass(JournalError, GitHubError))

    def test_a_write_its_caller_was_told_failed_is_never_sent_at_the_restart(self):
        # R1-4: the journal does not take the write; its caller gets the error, and nothing is
        # left for a restart to send.
        gh = OutboxGH()
        with tempfile.TemporaryDirectory() as state:
            journal = FailingJournal(state)
            journal.fail = {'publication'}
            _, client = make(gh, journal=journal)
            with self.assertRaises(JournalError):
                client.comment(7, 'record')
            box, _ = make(gh, state)
            gh.clock.now += AFTER_A_CRASH
            self.assertEqual((box.resume(), box.entries()), ([], {}))
            self.assertEqual(gh.sent, [])

    def test_a_failure_not_recorded_comes_before_any_later_event(self):
        # The caller of an open_turn told the record failed records not_dispatched: never
        # durable without the failure of the record, which keeps a restart from sending it.
        gh = Refusing()
        with tempfile.TemporaryDirectory() as state:
            journal = FailingJournal(state)
            journal.fail = {'publication_failed'}
            box, client = make(gh, journal=journal)
            ledger = Ledger(client, state_dir=state)
            ledger.journal = journal
            with self.assertRaises(GitHubError):
                open_turn(ledger)
            self.assertEqual([e['event'] for e in journal_events(state)],
                             ['intent', 'publication'])
            self.assertEqual(statuses(box), [FAILED])
            # The journal takes events again: the failure is written first.
            journal.fail = ()
            self.assertEqual(box.resume(), [])
            self.assertEqual([e['event'] for e in journal_events(state)],
                             ['intent', 'publication', 'publication_failed'])
            box, _ = make(gh, state)
            gh.clock.now += AFTER_A_CRASH
            self.assertEqual((box.resume(), statuses(box)), ([], [FAILED]))
            self.assertEqual(len(gh.sent), 1)

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
        self.assertEqual(gh.clock.slept, [])  # found at once: no wait
        [entry] = box.entries().values()
        self.assertEqual((entry['status'], entry['sends']), (SENT, 1))

    def test_a_post_still_in_flight_is_waited_for_and_not_sent_again(self):
        # R1-2: GitHub applies the POST after the first read: absent then, it is found once
        # the attempt can no longer take effect, and never sent twice.
        gh = OutboxGH()
        gh.uncertain_writes = ['late']
        box, client = make(gh)
        self.assertEqual(client.comment(7, 'record'), FIRST_COMMENT_ID)
        self.assertEqual(gh.sent, ['record'])
        self.assertEqual(gh.bodies(7), ['record'])
        self.assertEqual(gh.clock.slept, [IN_FLIGHT_SECONDS])
        [entry] = box.entries().values()
        self.assertEqual((entry['status'], entry['sends']), (SENT, 1))

    def test_an_uncertain_post_that_was_lost_is_published_once(self):
        gh = OutboxGH()
        gh.uncertain_writes = ['lost']
        box, client = make(gh)
        self.assertEqual(client.comment(7, 'record'), FIRST_COMMENT_ID)
        # Sent again only after a read that started when the first could no longer land.
        self.assertEqual(gh.clock.slept, [IN_FLIGHT_SECONDS])
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
        # The second send may still land: the next write on #7 is not sent before it cannot.
        with self.assertRaises(GitHubUncertain):
            client.comment(7, 'next')
        self.assertEqual(len(gh.sent), 2)
        # Then not there, and its caller was told it failed: dropped, not sent a third time.
        gh.clock.now += IN_FLIGHT_SECONDS
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
        box = Outbox(TurnJournal(), lambda a, p: gh, lambda actor: ['devloop-dispatcher[bot]'],
                     clock=gh.clock, sleep=gh.clock.sleep)
        client = box.client(gh, 'dispatcher')
        gh.uncertain_writes = ['lost', 'lost']
        with self.assertRaises(GitHubUncertain):
            client.comment(7, 'record')
        self.assertEqual(statuses(box), [UNCERTAIN])
        self.assertEqual(len(gh.sent), 2)

    def test_a_certain_failure_is_not_retried(self):
        gh = Refusing()
        box, client = make(gh)
        with self.assertRaises(GitHubError):
            client.comment(7, 'record')
        self.assertEqual(statuses(box), [FAILED])
        gh.clock.now += AFTER_A_CRASH
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
            # Right after the restart, the cut attempt may still land: nothing is sent.
            [line] = box.resume()
            self.assertIn('not settled', line)
            self.assertEqual(gh.sent, ['record'])
            gh.clock.now += AFTER_A_CRASH
            self.assertEqual(box.resume(), [])
            box = self.restart(gh, state)
            self.assertEqual(box.resume(), [])
            self.assertEqual(gh.bodies(7), ['record'])
            self.assertEqual(gh.sent, ['record', 'record'])
            self.assertEqual(statuses(box), [SENT])

    def test_a_post_cut_short_still_in_flight_blocks_new_posts_until_found(self):
        # R1-2 after a restart: absence right after it is no proof, and blocks #7.
        gh = OutboxGH()
        with tempfile.TemporaryDirectory() as state:
            _, client = make(gh, state)
            gh.crash = 'late'
            with self.assertRaises(Crash):
                client.comment(7, 'record')
            box, client = make(gh, state)
            self.assertEqual(len(box.resume()), 1)
            with self.assertRaises(GitHubUncertain):
                client.comment(7, 'next')
            self.assertEqual(gh.sent, ['record'])
            gh.clock.now += AFTER_A_CRASH  # it landed meanwhile
            client.comment(7, 'next')
            self.assertEqual(gh.bodies(7), ['record', 'next'])
            self.assertEqual(gh.sent, ['record', 'next'])
            self.assertEqual(statuses(box), [SENT, SENT])

    def test_a_write_recorded_and_never_sent_is_sent_at_the_restart(self):
        # The process stops right after the record, before the send (R1-4: not an error its
        # caller was told).
        gh = OutboxGH()
        with tempfile.TemporaryDirectory() as state:
            journal = FailingJournal(state)
            journal.crash_after = {'publication'}
            _, client = make(gh, journal=journal)
            with self.assertRaises(Crash):
                client.comment(7, 'record')
            self.assertEqual(gh.sent, [])
            box = self.restart(gh, state)
            gh.clock.now += AFTER_A_CRASH
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
            gh.clock.now += AFTER_A_CRASH
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
                open_turn(ledger)
            box = self.restart(gh, state)
            self.assertEqual(box.resume(), [])
            [(number, body)] = gh.comments
            self.assertEqual(number, 7)
            self.assertIn('Turno in corso', body)
            # The turn of the journal is left without outcome: the tick makes it uncertain.
            self.assertEqual(box.journal.pending(), [7])

    def test_a_native_review_cut_short_is_found_or_submitted_once(self):
        for how, sent in (('applied', 1), ('lost', 2)):
            with self.subTest(how=how), tempfile.TemporaryDirectory() as state:
                gh = OutboxGH()
                box, _ = make(gh, state)
                agent = box.client(gh, 'claude', {'pull_requests': 'write'})
                gh.crash = how
                with self.assertRaises(Crash):
                    agent.submit_review(100, commit=SHA1, event='APPROVE', body='review')
                box = self.restart(gh, state)
                gh.clock.now += AFTER_A_CRASH
                self.assertEqual(box.resume(), [])
                self.assertEqual(box.resume(), [])
                self.assertEqual([r['body'] for _, r in gh.native], ['review'])
                self.assertEqual((len(gh.sent), statuses(box)), (sent, [SENT]))

    def test_a_pull_request_cut_short_is_found_or_opened_once(self):
        for how, sent in (('applied', 1), ('lost', 2)):
            with self.subTest(how=how), tempfile.TemporaryDirectory() as state:
                gh = OutboxGH()
                box, _ = make(gh, state)
                agent = box.client(gh, 'claude', {'pull_requests': 'write'})
                gh.crash = how
                with self.assertRaises(Crash):
                    agent.create_pr(base='main', head='agent/issue-7', title='t', body='pr')
                box = self.restart(gh, state)
                gh.clock.now += AFTER_A_CRASH
                self.assertEqual(box.resume(), [])
                self.assertEqual(box.resume(), [])
                self.assertEqual([p['head'] for p in gh.created], ['agent/issue-7'])
                self.assertEqual((len(gh.sent), statuses(box)), (sent, [SENT]))
                [entry] = box.entries().values()
                self.assertEqual(entry['sent_id'], 100)

    def test_a_post_whose_confirmation_is_not_recorded_blocks_the_next_write(self):
        # R1-3: published, but the journal does not take publication_sent. Its edit is not
        # sent: a restart finds the comment as it was published, never sends it again.
        gh = OutboxGH()
        with tempfile.TemporaryDirectory() as state:
            journal = FailingJournal(state)
            journal.fail = {'publication_sent'}
            box, client = make(gh, journal=journal)
            first = client.comment(7, 'v1')
            self.assertEqual(first, FIRST_COMMENT_ID)
            self.assertEqual(statuses(box), [SENT])  # in memory, with its comment
            self.assertEqual(box.number_of(first), 7)
            with self.assertRaises(JournalError):
                client.edit_comment(first, 'v2')
            with self.assertRaises(JournalError):
                client.comment(8, 'elsewhere')
            self.assertEqual(gh.sent, ['v1'])
            box = self.restart(gh, state)
            gh.clock.now += AFTER_A_CRASH
            self.assertEqual(box.resume(), [])
            self.assertEqual((gh.bodies(7), gh.sent), (['v1'], ['v1']))
            self.assertEqual(statuses(box), [SENT])

    def test_a_confirmation_not_recorded_is_written_before_the_next_write(self):
        # R1-3, the journal recovers in the same process: the confirmation of v1 is written
        # first, then the edit; a restart has nothing to send.
        gh = OutboxGH()
        with tempfile.TemporaryDirectory() as state:
            journal = FailingJournal(state)
            journal.fail = {'publication_sent'}
            _, client = make(gh, journal=journal)
            first = client.comment(7, 'v1')
            journal.fail = ()
            client.edit_comment(first, 'v2')
            kinds = [(e['event'], e['data'].get('op')) for e in journal_events(state)]
            self.assertEqual(kinds, [('publication', 'comment'), ('publication_sent', None),
                                     ('publication', 'edit'), ('publication_sent', None)])
            box = self.restart(gh, state)
            gh.clock.now += AFTER_A_CRASH
            self.assertEqual((box.resume(), box.pending()), ([], []))
            self.assertEqual((gh.bodies(7), gh.sent), (['v2'], ['v1', 'v2']))

    def test_an_edit_superseded_before_the_restart_is_dropped(self):
        # Two edits of one comment running at once in two actions: the later one is
        # recorded and applied while the first is still being sent, then the process stops.
        gh = OutboxGH()
        with tempfile.TemporaryDirectory() as state:
            first = gh.comment(7, 'v1')  # a comment the outbox never saw: its issue unknown
            journal = TurnJournal(state)
            for pub, body in (('pub-1', 'v2'), ('pub-2', 'v3')):
                journal.publication('publication', {
                    'pub': pub, 'op': 'edit', 'comment_id': first, 'number': None,
                    'actor': 'dispatcher', 'permissions': None, 'body': body, 'time': START})
            journal.publication('publication_sent', {'pub': 'pub-2', 'id': first,
                                                     'via': 'response'})
            gh.edit_comment(first, 'v3')
            box = self.restart(gh, state)
            gh.clock.now += AFTER_A_CRASH
            self.assertEqual(box.resume(), [])
            # v2 is not applied over v3, which was built after it.
            self.assertEqual(statuses(box), [DROPPED, SENT])
            self.assertEqual(gh.bodies(7), ['v3'])
            self.assertEqual(gh.sent, ['v1', 'v3'])


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
        # The writes of the outputs pass through the outbox, not around it.
        for name in ('comment', 'edit_comment', 'submit_review', 'create_pr'):
            self.assertIn(name, vars(OutboxClient))
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
        gh = OutboxGH()
        box, client = make(gh, journal=journal)
        client.comment(7, 'x')
        self.assertEqual((journal.turns(), journal.pending()), ({}, []))
        with self.assertRaises(ValueError):
            journal.publication('outcome', {})
        self.assertEqual(outbox_mod.MAX_SENDS, 2)


if __name__ == '__main__':
    unittest.main()
