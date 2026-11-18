"""devloop: the outbox of the publications (#385, ADR 0004, P2-4)."""

import fcntl
import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from devloop import outbox as outbox_mod
from devloop import ports
from devloop.apps import Identities
from devloop.config import ConfigError, parse_config, load_data
from devloop.dispatcher import Dispatcher
from devloop.github import GitHub, GitHubError, GitHubUncertain
from devloop.journal import JOURNAL, JournalError, TurnJournal
from devloop.ledger import Ledger
from devloop.outbox import (DROPPED, FAILED, IN_FLIGHT_SECONDS, SENT, UNCERTAIN, Outbox,
                            OutboxClient, for_identities)
from devloop.planning import Snapshot
from helpers.devloop_fakes import FIRST_COMMENT_ID, SHA1, ActingAs, FakeGH, FakeIdentities

REPO = 'brushtyler/ssdlc'
START = 1_800_000_000.0
# After a crash, the `gh` of an attempt that held the dispatcher lock had ended when the next
# dispatcher started: absence is conclusive this much after the restart (R3-1).
AFTER_A_CRASH = IN_FLIGHT_SECONDS
# An attempt whose `gh` did not hold the lock has no known end: reconciled only this much later.
UNHELD_AFTER_A_CRASH = outbox_mod.ATTEMPT_SECONDS + IN_FLIGHT_SECONDS
ROOT = Path(__file__).resolve().parent.parent


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
    (``read_fails``). An uncertain ``comment``, ``submit_review`` or ``create_pr``
    (``uncertain_writes``) may be ``'applied'``, ``'lost'`` or ``'late'`` too. ``pause``: the
    process stalls that many seconds right before the next POST; ``delay``: how long after it a
    ``'late'`` write lands. It records each body sent; the pull requests it opens are open.
    ``lock_fd``: its writes hold the dispatcher lock (``GitHub.lock_fd``), so they end before
    another dispatcher starts; None, a write may outlive the process that sent it."""

    def __init__(self):
        super().__init__()
        self.with_ids = True
        self.clock = Clock()
        self.crash = None
        self.pause = 0
        self.delay = 10
        self.lock_fd = 3
        self.read_fails = 0
        self.sent = []
        self.in_flight = []  # (time it takes effect, how): writes GitHub still serves

    def _write(self, body, apply):
        self.clock.now += self.pause
        self.pause = 0
        self.sent.append(body)
        if self.uncertain_writes:
            outcome, *self.uncertain_writes = self.uncertain_writes
            if outcome == 'applied':
                apply()
            elif outcome == 'late':
                self.in_flight.append((self.clock() + self.delay, apply))
            raise GitHubUncertain(f'write: outcome unknown ({outcome})')
        how, self.crash = self.crash, None
        if how is None:
            return apply()
        if how == 'applied':
            apply()
        elif how == 'late':
            self.in_flight.append((self.clock() + self.delay, apply))
        raise Crash()

    def _land(self):
        for item in [i for i in self.in_flight if i[0] <= self.clock()]:
            self.in_flight.remove(item)
            item[1]()

    def comment(self, number, body):
        def apply():
            self.comments.append((number, body))
            return FIRST_COMMENT_ID + len(self.comments) - 1
        return self._write(body, apply)

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
    box.entries()  # the process reads the journal as it starts, under the dispatcher lock
    return box, box.client(gh, 'dispatcher', {'issues': 'write'})


def statuses(box):
    return [e['status'] for e in box.entries().values()]


def gh_command(cmd):
    """The `gh` command of ``cmd``, without the ``timeout`` that wraps a write holding the
    dispatcher lock (#397)."""
    return cmd[cmd.index('gh'):] if cmd[0] == 'timeout' else cmd


def journal_events(state):
    return [json.loads(line) for line in (Path(state) / JOURNAL).read_text().splitlines()]


def open_turn(ledger):
    return ledger.open_turn(7, kind='implement', agent='claude', identity=None, mode='write',
                            model='m', effort=None, fallback_for=None, label='l', repo=REPO)


class GhServer:
    """`gh` on a simulated GitHub, for the real ``GitHub`` adapter: the native reviews and the
    open pull requests it holds. Each POST takes the next of ``answers``: ``'ok'``;
    ``'timeout'`` or ``'5xx'`` after GitHub applied it; ``'lost'``, a 5xx without it; ``'4xx'``,
    refused. It counts the POSTs and the reads."""

    def __init__(self):
        self.reviews = []
        self.prs = []
        self.answers = []
        self.posts = 0
        self.reads = 0

    @staticmethod
    def _fields(args):
        return dict(args[i + 1].split('=', 1) for i, a in enumerate(args) if a == '-f')

    def _apply(self, path, fields, body):
        if path.endswith('/reviews'):
            self.reviews.append({'id': 7000 + len(self.reviews), 'login': 'brushtyler',
                                 'state': 'COMMENTED', 'body': body,
                                 'submitted_at': '2026-11-18T15:30:00Z'})
            return {'id': self.reviews[-1]['id']}
        number = 200 + len(self.prs)
        self.prs.append({'number': number, 'title': fields['title'], 'body': body, 'labels': [],
                         'headRefName': fields['head'], 'headRefOid': 'a' * 40,
                         'author': {'login': 'brushtyler', 'is_bot': False},
                         'isCrossRepository': False, 'mergeable': 'MERGEABLE',
                         'mergeStateStatus': 'CLEAN'})
        return {'number': number, 'html_url': f'https://github.com/{REPO}/pull/{number}'}

    def __call__(self, cmd, input=None, timeout=None, **kwargs):
        args = cmd[1:]
        if args[:3] == ['api', '-X', 'POST']:
            self.posts += 1
            answer = self.answers.pop(0) if self.answers else 'ok'
            created = None
            if answer in ('ok', 'timeout', '5xx'):
                created = self._apply(args[3], self._fields(args), input)
            if answer == 'timeout':
                raise subprocess.TimeoutExpired(cmd, timeout)
            if answer in ('5xx', 'lost'):
                return SimpleNamespace(returncode=1, stdout='',
                                       stderr='gh: Server Error (HTTP 502)')
            if answer == '4xx':
                return SimpleNamespace(returncode=1, stdout='',
                                       stderr='gh: Validation Failed (HTTP 422)')
            return SimpleNamespace(returncode=0, stdout=json.dumps(created), stderr='')
        self.reads += 1
        if args[:2] == ['pr', 'list']:
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.prs), stderr='')
        if args[:2] == ['api', '--paginate'] and args[2].split('?')[0].endswith('/reviews'):
            lines = '\n'.join(json.dumps(r) for r in self.reviews)
            return SimpleNamespace(returncode=0, stdout=lines, stderr='')
        raise AssertionError(f'unexpected gh call: {args}')


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

    def test_an_uncertain_review_or_pull_request_is_reconciled(self):
        # R2-1: the native reviews and the pull requests too, applied at once, while the
        # outbox reads (in flight) or never.
        for op in ('review', 'pr'):
            for how, sends in (('applied', 1), ('late', 1), ('lost', 2)):
                with self.subTest(op=op, how=how):
                    gh = OutboxGH()
                    box, _ = make(gh)
                    agent = box.client(gh, 'claude', {'pull_requests': 'write'})
                    gh.uncertain_writes = [how]
                    if op == 'review':
                        self.assertIsNone(agent.submit_review(100, commit=SHA1, event='APPROVE',
                                                              body='review'))
                        self.assertEqual([r['body'] for _, r in gh.native], ['review'])
                    else:
                        self.assertEqual(agent.create_pr(base='main', head='agent/issue-7',
                                                         title='t', body='pr')[0], 100)
                        self.assertEqual([p['head'] for p in gh.created], ['agent/issue-7'])
                    self.assertEqual(len(gh.sent), sends)
                    self.assertEqual(gh.clock.slept, [] if how == 'applied'
                                     else [IN_FLIGHT_SECONDS])
                    self.assertEqual(statuses(box), [SENT])

    def test_an_exception_of_the_client_leaves_the_write_uncertain(self):
        # Applied, but the client fails on the answer with an error that is not a GitHubError:
        # the caller is told it failed, the write is never sent again, only found.
        class Unparsable(OutboxGH):
            def create_pr(self, **kwargs):
                super().create_pr(**kwargs)
                raise KeyError('number')
        gh = Unparsable()
        box, _ = make(gh)
        agent = box.client(gh, 'claude', {'pull_requests': 'write'})
        with self.assertRaises(KeyError):
            agent.create_pr(base='main', head='agent/issue-7', title='t', body='pr')
        self.assertEqual(statuses(box), [UNCERTAIN])
        gh.clock.now += IN_FLIGHT_SECONDS
        self.assertEqual(box.resume(), [])
        self.assertEqual((len(gh.sent), len(gh.created), statuses(box)), (1, 1, [SENT]))


class AdapterTests(unittest.TestCase):
    """R2-1: the real ``GitHub`` adapter on a simulated `gh`: a native review or a pull request
    whose answer times out or is a 5xx is uncertain, reconciled, and never duplicated."""

    def make(self, answers):
        server = GhServer()
        server.answers = list(answers)
        gh = GitHub(REPO, run=server)
        gh.uncertain_publications = True  # as ``Identities`` sets it with the outbox (#397)
        clock = Clock()
        box = Outbox(TurnJournal(), lambda actor, permissions: gh, lambda actor: ['brushtyler'],
                     clock=clock, sleep=clock.sleep)
        return server, box, box.client(gh, 'claude', {'pull_requests': 'write'})

    @staticmethod
    def write(op, client):
        if op == 'review':
            return client.submit_review(100, commit=SHA1, event='COMMENT', body='review')
        return client.create_pr(base='main', head='agent/issue-7', title='t', body='pr')

    @staticmethod
    def published(op, server):
        return len(server.reviews if op == 'review' else server.prs)

    def test_an_applied_write_whose_answer_is_lost_is_found_and_not_sent_again(self):
        for op in ('review', 'pr'):
            for answer in ('timeout', '5xx'):
                with self.subTest(op=op, answer=answer):
                    server, box, client = self.make([answer])
                    result = self.write(op, client)
                    if op == 'pr':
                        self.assertEqual(result, (200, f'https://github.com/{REPO}/pull/200'))
                    self.assertEqual((server.posts, self.published(op, server)), (1, 1))
                    self.assertGreater(server.reads, 0)  # reconciled
                    [entry] = box.entries().values()
                    self.assertEqual((entry['status'], entry['sends']), (SENT, 1))

    def test_a_lost_write_is_sent_once_more(self):
        for op in ('review', 'pr'):
            with self.subTest(op=op):
                server, box, client = self.make(['lost'])
                self.write(op, client)
                self.assertEqual((server.posts, self.published(op, server)), (2, 1))
                self.assertEqual(statuses(box), [SENT])

    def test_a_refused_write_is_certain_and_not_reconciled(self):
        for op in ('review', 'pr'):
            with self.subTest(op=op):
                server, box, client = self.make(['4xx'])
                with self.assertRaises(GitHubError) as raised:
                    self.write(op, client)
                self.assertNotIsInstance(raised.exception, GitHubUncertain)
                self.assertEqual((server.posts, server.reads), (1, 0))
                self.assertEqual(statuses(box), [FAILED])


class ReviewRetryTests(unittest.TestCase):
    """R2-2 (agy): the retries of ``_submit_review`` never resubmit a native review whose
    outcome the outbox left uncertain."""

    def run_submit(self, outbox, errors, reviewer_gh=None):
        calls = []

        def submit(number, **kwargs):
            calls.append(number)
            if errors:
                raise errors.pop(0)
        stub = SimpleNamespace(identities=SimpleNamespace(outbox=outbox), review_retry_pause=0,
                               log=lambda line: None, _app_login=lambda key: 'rev[bot]',
                               gh=SimpleNamespace(pr_reviews=lambda number: []))
        reviewer_gh = reviewer_gh or SimpleNamespace(submit_review=submit)
        pr = SimpleNamespace(number=100, head_sha=SHA1)
        try:
            Dispatcher._submit_review(stub, reviewer_gh, pr, 'codex',
                                      {'event': 'COMMENT', 'body': 'review'})
            return calls, None
        except GitHubError as exc:
            return calls, exc

    def test_an_uncertain_outcome_ends_the_retries_with_the_outbox(self):
        calls, error = self.run_submit(object(), [GitHubUncertain('timed out')] * 3)
        self.assertEqual(len(calls), 1)
        self.assertIn('uncertain', str(error))

    def test_a_certain_failure_is_still_retried_with_the_outbox(self):
        calls, error = self.run_submit(object(), [GitHubError('HTTP 422')])
        self.assertEqual((len(calls), error), (2, None))

    def test_without_the_outbox_the_retries_are_as_before(self):
        calls, error = self.run_submit(None, [GitHubUncertain('timed out')] * 3)
        self.assertEqual(len(calls), 3)
        self.assertIn('failed 3 times', str(error))

    def test_through_the_outbox_a_lost_review_is_sent_at_most_twice(self):
        gh = OutboxGH()
        box, _ = make(gh)
        gh.uncertain_writes = ['lost', 'lost']
        calls, error = self.run_submit(box, [], box.client(gh, 'codex',
                                                           {'pull_requests': 'write'}))
        self.assertIsNotNone(error)
        self.assertEqual(len(gh.sent), outbox_mod.MAX_SENDS)
        self.assertEqual(statuses(box), [UNCERTAIN])


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
            [entry] = box.entries().values()
            self.assertTrue(entry['held'])  # its `gh` ended before this process started
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

    def test_a_post_cut_short_whose_gh_did_not_hold_the_lock_is_never_sent_again(self):
        # R3-1: without the lock, the `gh` of the cut attempt may outlive the dispatcher, past
        # any timeout: no wait proves it ended. Found, it is published; absent, it is dropped.
        for how, comments in (('applied', ['record']), ('lost', []), ('late', ['record'])):
            with self.subTest(how=how), tempfile.TemporaryDirectory() as state:
                gh = OutboxGH()
                gh.lock_fd = None
                gh.delay = 10 * UNHELD_AFTER_A_CRASH  # resumed long after the restart
                _, client = make(gh, state)
                gh.crash = how
                with self.assertRaises(Crash):
                    client.comment(7, 'record')
                box = self.restart(gh, state)
                [entry] = box.entries().values()
                self.assertFalse(entry['held'])
                if how != 'applied':
                    gh.clock.now += AFTER_A_CRASH
                    self.assertEqual(len(box.resume()), 1)  # still unknown: blocks #7
                    gh.clock.now += UNHELD_AFTER_A_CRASH - AFTER_A_CRASH
                self.assertEqual(box.resume(), [])
                self.assertEqual(statuses(box), [SENT if how == 'applied' else DROPPED])
                gh.clock.now += gh.delay
                gh.issue_comments(7)  # the orphan `gh` publishes it at last
                self.assertEqual((gh.sent, gh.bodies(7)), (['record'], comments))

    def test_a_pause_between_the_record_and_the_post_does_not_shorten_the_wait(self):
        # R2-2: recorded at START, the process stalls 400 seconds, then sends the POST, which
        # GitHub applies 10 seconds later; the process stops and restarts at once. The time
        # recorded before the send proves nothing: the wait counts from the restart.
        gh = OutboxGH()
        with tempfile.TemporaryDirectory() as state:
            _, client = make(gh, state)
            gh.pause, gh.crash = 400, 'late'
            with self.assertRaises(Crash):
                client.comment(7, 'record')
            self.assertGreater(gh.clock.now, START + AFTER_A_CRASH)
            box, client = make(gh, state)
            self.assertEqual(len(box.resume()), 1)
            with self.assertRaises(GitHubUncertain):
                client.comment(7, 'next')
            self.assertEqual(gh.sent, ['record'])
            gh.clock.now += AFTER_A_CRASH
            self.assertEqual(box.resume(), [])
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


FAKE_GH = '''#!{python}
"""`gh` on a GitHub made of files in $GH_FAKE_DIR: a POST of a comment says it started, waits
for `go` while `hold` exists, then publishes; a read lists the comments."""
import json, os, pathlib, sys, time
root = pathlib.Path(os.environ['GH_FAKE_DIR'])
comments = root / 'comments.jsonl'
args = sys.argv[1:]
if args[:3] == ['api', '-X', 'POST']:
    (root / 'started').write_text(str(os.getpid()))
    deadline = time.monotonic() + 60
    while (root / 'hold').exists() and not (root / 'go').exists():
        if time.monotonic() > deadline:
            sys.exit(1)
        time.sleep(0.02)
    lines = comments.read_text().splitlines() if comments.exists() else []
    new = {{'id': 5000 + len(lines), 'author': {{'login': 'brushtyler'}},
            'body': sys.stdin.read(), 'created_at': '2026-11-18T15:30:00Z'}}
    with comments.open('a') as fh:
        fh.write(json.dumps(new) + '\\n')
    with (root / 'posts').open('a') as fh:
        fh.write('post\\n')
    try:
        print(json.dumps({{'id': new['id']}}), flush=True)
    except BrokenPipeError:
        pass  # its dispatcher is gone
elif args[:2] == ['api', '--paginate']:
    print(comments.read_text() if comments.exists() else '', end='')
else:
    sys.exit(f'unexpected gh call: {{args}}')
'''

DISPATCHER = '''
import pathlib, sys
sys.path.insert(0, {root!r})
from devloop.__main__ import single_instance
from devloop.apps import Identities
from devloop.journal import TurnJournal
from devloop.outbox import for_identities
state = pathlib.Path(sys.argv[1])
with single_instance(state / 'dispatcher.lock') as lock:
    identities = Identities('o/r', {{}})
    identities.outbox = for_identities(identities, TurnJournal(state), ['brushtyler'])
    identities.hold(lock.fileno())
    identities.github('dispatcher').comment(7, 'record')
'''


class OrphanGhTests(unittest.TestCase):
    """R3-1, with real processes: the dispatcher is killed while the `gh` of its POST runs (here
    held back, as a stopped `gh` would be). The `gh` outlives it, with no timeout left; the next
    dispatcher cannot start until it has ended, and then finds its comment: one comment."""

    def wait_for(self, condition, what):
        deadline = time.monotonic() + 30
        while not condition():
            if time.monotonic() > deadline:
                self.fail(f'timed out waiting for {what}')
            time.sleep(0.02)

    @staticmethod
    def lock_free(path):
        with open(path, 'w') as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(fh, fcntl.LOCK_UN)
            return True

    def test_a_gh_that_outlived_its_dispatcher_holds_the_lock_and_is_not_duplicated(self):
        import devloop.__main__ as devloop_main
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state, fake, bindir = tmp / 'state', tmp / 'github', tmp / 'bin'
            for d in (state, fake, bindir):
                d.mkdir()
            gh_path = bindir / 'gh'
            gh_path.write_text(FAKE_GH.format(python=sys.executable))
            gh_path.chmod(0o755)
            (fake / 'hold').touch()
            env = dict(os.environ, PATH=f'{bindir}{os.pathsep}{os.environ.get("PATH", "")}',
                       GH_FAKE_DIR=str(fake))
            env.pop(devloop_main.LOCK_FD, None)
            dispatcher = subprocess.Popen(
                [sys.executable, '-c', DISPATCHER.format(root=str(ROOT)), str(state)], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                box = self.kill_and_restart(dispatcher, state, fake, env)
            finally:
                (fake / 'go').touch()  # never leave the `gh` waiting
                if dispatcher.poll() is None:
                    dispatcher.kill()
                    dispatcher.wait()
            bodies = [json.loads(line)['body']
                      for line in (fake / 'comments.jsonl').read_text().splitlines()]
            self.assertEqual(bodies, ['record', 'next'])
            self.assertEqual(len((fake / 'posts').read_text().splitlines()), 2)
            self.assertEqual(statuses(box), [SENT, SENT])

    def kill_and_restart(self, dispatcher, state, fake, env):
        """Kill the dispatcher while its `gh` runs, then start the next one: the outbox of the
        new dispatcher, which has resumed and written once more on #7."""
        import devloop.__main__ as devloop_main
        self.wait_for(lambda: (fake / 'started').exists(), 'the POST')
        dispatcher.kill()
        dispatcher.wait()
        lock = state / 'dispatcher.lock'
        # The `gh` runs on, its parent gone: the next dispatcher does not start.
        self.assertFalse(self.lock_free(lock))
        with mock.patch.dict(os.environ, env), \
                self.assertRaisesRegex(SystemExit, 'another dispatcher is running'):
            with devloop_main.single_instance(lock):
                pass
        (fake / 'go').touch()  # the `gh` publishes, then ends
        self.wait_for(lambda: self.lock_free(lock), 'the end of the orphan gh')
        with mock.patch.dict(os.environ, env), devloop_main.single_instance(lock) as held:
            identities = Identities('o/r', {})
            box = for_identities(identities, TurnJournal(state), ['brushtyler'])
            identities.outbox = box
            identities.hold(held.fileno())
            [entry] = box.entries().values()
            self.assertTrue(entry['held'])
            # Found at once: no wait, no second POST of the record.
            self.assertEqual(box.resume(), [])
            identities.github('dispatcher').comment(7, 'next')
        return box


class WiringTests(unittest.TestCase):
    def test_the_gh_of_each_write_holds_the_dispatcher_lock(self):
        calls = []

        def run(cmd, **kwargs):
            calls.append((cmd[:cmd.index('gh')], gh_command(cmd)[1:4], kwargs.get('pass_fds'),
                          kwargs.get('timeout')))
            return SimpleNamespace(returncode=0, stdout='{"id": 1}', stderr='')
        app = SimpleNamespace(token=lambda permissions, time_left=None: 'tok')
        identities = Identities('o/r', {'claude': app}, run=run)
        self.assertIsNone(identities.direct('claude').lock_fd)
        identities.hold(9)
        for actor in ('dispatcher', 'claude'):
            with self.subTest(actor=actor):
                calls.clear()
                client = identities.direct(actor)
                self.assertEqual(client.lock_fd, 9)
                client.comment(7, 'x')
                client.issue_comments(7)
                # The write runs under a timeout of its own (#397), which this process
                # outlasts: SIGTERM at 300 s, SIGKILL 5 s later. A read is not wrapped.
                self.assertEqual(calls, [
                    (['timeout', '-k', '5', '300'], ['api', '-X', 'POST'], (9,),
                     300 + GitHub.WRAPPED_GRACE),
                    ([], ['api', '--paginate', 'repos/o/r/issues/7/comments'], None, 300)])

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

    def test_the_app_clients_build_creates_get_the_lock_held_after_it(self):
        # R4-1: build() creates the clients of the dispatcher's and the merger's apps, then
        # main() takes the lock and calls hold(). Their writes hold it too: a POST cut short
        # before it took effect is `held`, and the next dispatcher sends it once more.
        import dataclasses

        import devloop.__main__ as devloop_main
        from test_devloop import CFG
        posts, comments = [], []

        def run(cmd, **kwargs):
            if gh_command(cmd)[1:4] == ['api', '-X', 'POST']:
                posts.append(kwargs.get('pass_fds'))
                if len(posts) == 1:
                    raise Crash  # the dispatcher stops before GitHub applied it
                comments.append({'id': 4000 + len(comments),
                                 'author': {'login': 'devloop-dispatcher[bot]'},
                                 'body': kwargs['input'], 'created_at': '2026-11-18T15:30:00Z'})
                return SimpleNamespace(returncode=0,
                                       stdout=json.dumps({'id': comments[-1]['id']}), stderr='')
            return SimpleNamespace(returncode=0, stdout=''.join(
                json.dumps(c) + '\n' for c in comments), stderr='')
        app = SimpleNamespace(token=lambda permissions, time_left=None: 'tok',
                              login='devloop-dispatcher[bot]')
        apps = {'dispatcher': app, 'merger': app}
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dataclasses.replace(CFG, publication_outbox=True)
            args = SimpleNamespace(config=None, local_config=None, command='run', dry_run=False)
            with mock.patch.object(devloop_main, 'load_config', return_value=cfg), \
                    mock.patch.object(devloop_main, 'main_checkout', return_value=Path(tmp)), \
                    mock.patch.object(devloop_main.Identities, 'from_config',
                                      return_value=Identities(cfg.repo, apps, run=run)):
                _, state, d = devloop_main.build(args)
            self.assertTrue(devloop_main.hold_lock(d, 9))  # as main() does, once it holds it
            self.assertEqual((d.gh.lock_fd, d.merger.lock_fd), (9, 9))
            with self.assertRaises(Crash):
                d.gh.comment(7, 'record')
            self.assertEqual(posts, [(9,)])
            # The next dispatcher.
            clock = Clock()
            clock.now = time.time()
            identities = Identities(cfg.repo, apps, run=run)
            identities.hold(9)
            box = Outbox(TurnJournal(state), identities.direct, lambda actor: [app.login],
                         clock=clock, sleep=clock.sleep)
            [entry] = box.entries().values()
            self.assertTrue(entry['held'])
            clock.now += AFTER_A_CRASH
            self.assertEqual(box.resume(), [])
            self.assertEqual(statuses(box), [SENT])
            self.assertEqual(posts, [(9,), (9,)])
            self.assertEqual([c['body'] for c in comments], ['record'])

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


# A dispatcher of real processes (#397): it takes the lock, passes it on as main() does
# (``hold_lock``), with the outbox ``on`` or ``off``, and writes one comment, whose `gh` (FAKE_GH)
# waits while ``hold`` exists. Its calls time out after ``argv[3]`` seconds.
HOLDING_DISPATCHER = '''
import pathlib, sys
from types import SimpleNamespace
sys.path.insert(0, {root!r})
from devloop.__main__ import hold_lock, single_instance
from devloop.apps import Identities
from devloop.github import GitHub
from devloop.journal import TurnJournal
from devloop.outbox import for_identities
state = pathlib.Path(sys.argv[1])
GitHub.timeout = int(sys.argv[3])
with single_instance(state / 'dispatcher.lock') as lock:
    identities = Identities('o/r', {{}})
    if sys.argv[2] == 'on':
        identities.outbox = for_identities(identities, TurnJournal(state), ['brushtyler'])
    hold_lock(SimpleNamespace(identities=identities), lock.fileno())
    identities.github('dispatcher').comment(7, 'record')
'''


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class LockAndTimeoutTests(unittest.TestCase):
    """#397: with the outbox off no `gh` gets the dispatcher lock, so a dispatcher killed while
    one of its `gh` runs restarts at once; with it on, a `gh` of a write that holds the lock
    has a timeout of its own, so it does not hold it for ever after its dispatcher died."""

    def setUp(self):
        import devloop.__main__ as devloop_main
        self.main = devloop_main
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp = Path(tmp.name)
        self.state, self.fake, bindir = tmp / 'state', tmp / 'github', tmp / 'bin'
        for d in (self.state, self.fake, bindir):
            d.mkdir()
        gh_path = bindir / 'gh'
        gh_path.write_text(FAKE_GH.format(python=sys.executable))
        gh_path.chmod(0o755)
        (self.fake / 'hold').touch()
        # Never leave a `gh` waiting.
        self.addCleanup(lambda: (self.fake / 'go').touch())
        self.env = dict(os.environ, PATH=f'{bindir}{os.pathsep}{os.environ.get("PATH", "")}',
                        GH_FAKE_DIR=str(self.fake))
        self.env.pop(devloop_main.LOCK_FD, None)
        self.lock = self.state / 'dispatcher.lock'

    def start(self, outbox, timeout):
        dispatcher = subprocess.Popen(
            [sys.executable, '-c', HOLDING_DISPATCHER.format(root=str(ROOT)), str(self.state),
             outbox, str(timeout)], env=self.env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)

        def stop():
            if dispatcher.poll() is None:
                dispatcher.kill()
                dispatcher.wait()
        self.addCleanup(stop)
        OrphanGhTests.wait_for(self, lambda: (self.fake / 'started').exists(), 'the POST')
        return dispatcher, int((self.fake / 'started').read_text())

    def test_with_the_outbox_off_a_killed_dispatcher_restarts_at_once(self):
        dispatcher, gh_pid = self.start('off', 300)
        dispatcher.kill()
        dispatcher.wait()
        # Its `gh` runs on, stuck: it does not hold the lock, and the next dispatcher starts.
        self.assertTrue(alive(gh_pid))
        self.assertTrue(OrphanGhTests.lock_free(self.lock))
        with mock.patch.dict(os.environ, self.env), self.main.single_instance(self.lock):
            pass
        self.assertTrue(alive(gh_pid))
        self.assertFalse((self.fake / 'posts').exists())

    def test_with_the_outbox_on_the_gh_of_a_write_has_a_timeout_of_its_own(self):
        dispatcher, gh_pid = self.start('on', 2)
        dispatcher.kill()
        dispatcher.wait()
        # The `gh` holds the lock while it runs, its dispatcher gone...
        with mock.patch.dict(os.environ, self.env), \
                self.assertRaisesRegex(SystemExit, 'another dispatcher is running'):
            with self.main.single_instance(self.lock):
                pass
        # ... until its own timeout stops it, with nobody to let it go on: the lock is free.
        OrphanGhTests.wait_for(self, lambda: OrphanGhTests.lock_free(self.lock),
                               'the timeout of the orphan gh')
        self.assertFalse(alive(gh_pid))
        self.assertFalse((self.fake / 'posts').exists())
        with mock.patch.dict(os.environ, self.env), self.main.single_instance(self.lock):
            pass

    def test_a_write_stopped_by_its_own_timeout_is_uncertain(self):
        with open(self.lock, 'w') as lock, mock.patch.dict(os.environ, self.env):
            gh = GitHub('o/r')
            gh.timeout = 1
            gh.lock_fd = lock.fileno()
            started = time.monotonic()
            with self.assertRaisesRegex(GitHubUncertain, 'timed out after 1s'):
                gh.comment(7, 'x')
            self.assertLess(time.monotonic() - started, gh.WRAPPED_GRACE)
        self.assertFalse((self.fake / 'posts').exists())

    def test_hold_lock_only_with_the_outbox(self):
        for outbox in (None, object()):
            with self.subTest(outbox=outbox):
                identities = Identities('o/r', {})
                identities.outbox = outbox
                self.assertIs(self.main.hold_lock(SimpleNamespace(identities=identities), 9),
                              outbox is not None)
                self.assertEqual(identities.direct('dispatcher').lock_fd,
                                 9 if outbox is not None else None)
        self.assertFalse(self.main.hold_lock(SimpleNamespace(identities=None), 9))

    def test_with_the_outbox_off_devloop_writes_as_before_it(self):
        # build() and main() with publication_outbox = false: no lock, no wrapper, and a review
        # or pull request whose outcome is unknown is a failed write, not an uncertain one.
        import dataclasses

        from test_devloop import CFG
        calls = []

        def run(cmd, **kwargs):
            calls.append((cmd, kwargs.get('pass_fds'), kwargs.get('timeout')))
            if cmd[1:4] == ['api', '-X', 'POST'] and cmd[4].endswith(('/pulls', '/reviews')):
                return SimpleNamespace(returncode=1, stdout='',
                                       stderr='gh: Server Error (HTTP 502)')
            return SimpleNamespace(returncode=0, stdout='{"id": 1}', stderr='')
        app = SimpleNamespace(token=lambda permissions, time_left=None: 'tok',
                              login='devloop-dispatcher[bot]')
        cfg = dataclasses.replace(CFG, publication_outbox=False)
        args = SimpleNamespace(config=None, local_config=None, command='run', dry_run=False)
        with mock.patch.object(self.main, 'load_config', return_value=cfg), \
                mock.patch.object(self.main, 'main_checkout', return_value=self.state), \
                mock.patch.object(self.main.Identities, 'from_config',
                                  return_value=Identities(cfg.repo, {'dispatcher': app},
                                                          run=run)):
            _, _, d = self.main.build(args)
        self.assertFalse(self.main.hold_lock(d, 9))
        for client in (d.gh, d.merger, d.identities.github('claude')):
            self.assertIsNone(client.lock_fd)
            self.assertFalse(client.uncertain_publications)
        d.gh.comment(7, 'x')
        self.assertEqual(calls[-1][0][0], 'gh')
        self.assertEqual(calls[-1][1:], (None, GitHub.timeout))
        for write in (lambda: d.gh.submit_review(7, commit='a', event='COMMENT', body='b'),
                      lambda: d.gh.create_pr(base='main', head='h', title='t', body='b')):
            with self.assertRaises(GitHubError) as raised:
                write()
            self.assertNotIsInstance(raised.exception, GitHubUncertain)
        # With it on, the same clients tell an uncertain outcome apart.
        d.identities.outbox = object()
        self.assertTrue(d.gh.uncertain_publications)
        self.assertTrue(d.identities.direct('claude').uncertain_publications)


class DroppedSignalTests(unittest.TestCase):
    """#397: a write the outbox dropped reaches the operator: once on ``[run].alert_issue``,
    recorded in the journal; without it, a warning in the log of each process."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)

    def dropped(self):
        """An outbox over the journal of ``state`` with one write dropped: uncertain twice,
        then absent once nothing could take effect."""
        gh = OutboxGH()
        gh.uncertain_writes = ['lost', 'lost']
        box, client = make(gh, self.state)
        with self.assertRaises(GitHubUncertain):
            client.comment(7, 'the body of the write')
        gh.clock.now += IN_FLIGHT_SECONDS
        self.assertEqual(box.resume(), [])
        self.assertEqual(statuses(box), [DROPPED])
        return gh, box, client

    def dispatcher(self, box, client, alert_issue):
        import dataclasses

        from devloop.config import RunPolicy
        from test_devloop import CFG
        lines = []
        cfg = dataclasses.replace(CFG, run=RunPolicy(alert_issue=alert_issue))
        return Dispatcher(cfg, client, None, None, log=lines.append,
                          identities=SimpleNamespace(outbox=box)), lines

    def test_a_dropped_write_is_reported_once_on_the_alert_issue(self):
        gh, box, client = self.dropped()
        [pub] = box.entries()
        d, lines = self.dispatcher(box, client, 76)
        d._signal_dropped()
        [alert] = gh.bodies(76)
        self.assertIn('## Pubblicazioni abbandonate', alert)
        self.assertIn(f'`{pub}`: comment on #7, di `dispatcher`', alert)
        self.assertIn('turn-journal.jsonl', alert)
        self.assertNotIn('the body of the write', alert)  # never the body
        self.assertTrue(any(f'publication {pub} (comment on #7) dropped' in line
                            for line in lines))
        self.assertIn('  alert of 1 dropped publications published on #76', lines)
        self.assertEqual(box.unsignalled(), [])
        self.assertIn('publication_signalled', [e['event'] for e in journal_events(self.state)])
        # Never again: not at the next tick, nor from the journal after a restart.
        d._signal_dropped()
        self.assertEqual(len(gh.bodies(76)), 1)
        again = Outbox(TurnJournal(self.state), lambda actor, permissions: gh,
                       lambda actor: ['brushtyler'])
        self.assertEqual(again.unsignalled(), [])
        d2, _ = self.dispatcher(again, again.client(gh, 'dispatcher'), 76)
        d2._signal_dropped()
        self.assertEqual(len(gh.bodies(76)), 1)

    def test_a_failed_alert_is_tried_again(self):
        gh, box, client = self.dropped()
        d, lines = self.dispatcher(box, client, 76)
        with mock.patch.object(gh, 'comment', side_effect=GitHubError('down')):
            d._signal_dropped()
        self.assertEqual(len(box.unsignalled()), 1)
        self.assertTrue(any('could not be published (GitHubError)' in line for line in lines))
        d._signal_dropped()
        self.assertEqual(len(gh.bodies(76)), 1)
        self.assertEqual(box.unsignalled(), [])

    def test_an_alert_applied_with_an_uncertain_outcome_is_never_sent_twice(self):
        """R1-1: GitHub applies the alert, its answer is uncertain and the first
        reconciliation cannot read: the drop is not reported again, neither at the next tick
        nor after a restart; once the outbox finds the alert, the drop is recorded reported."""
        gh, box, client = self.dropped()
        [pub] = box.entries()
        gh.uncertain_writes, gh.read_fails = ['applied'], 1
        d, lines = self.dispatcher(box, client, 76)
        d._signal_dropped()
        self.assertEqual(len(gh.bodies(76)), 1)
        self.assertTrue(any('has an uncertain outcome (GitHubUncertain)' in line
                            for line in lines))
        alert = [e for e in box.entries().values() if e['pub'] != pub]
        self.assertEqual([(e['status'], e['covers']) for e in alert], [(UNCERTAIN, [pub])])
        self.assertEqual(box.unsignalled(), [])
        d._signal_dropped()  # the next tick, before the outbox settles it
        self.assertEqual(len(gh.bodies(76)), 1)
        # A restart: a new process reads the journal, reports nothing before settling.
        again, client2 = make(gh, self.state)
        d2, lines2 = self.dispatcher(again, client2, 76)
        d2._signal_dropped()
        self.assertEqual(len(gh.bodies(76)), 1)
        self.assertEqual(again.resume(), [])  # the next tick settles it: found, published
        d2._signal_dropped()
        self.assertEqual(len(gh.bodies(76)), 1)
        self.assertEqual(again.unsignalled(), [])
        self.assertTrue(again.entries()[pub]['signalled'])
        signalled = [e['data']['pub'] for e in journal_events(self.state)
                     if e['event'] == 'publication_signalled']
        self.assertEqual(signalled, [pub])
        self.assertEqual(len(gh.sent), 3)  # the write twice, the alert once: never resent
        self.assertFalse(any('WARNING' in line for line in lines2))

    def test_an_alert_never_published_reports_its_drops_again(self):
        """An alert with an uncertain outcome that the outbox drops, absent from GitHub,
        covers nothing: a new alert reports its drops, and the dropped alert with them."""
        gh, box, client = self.dropped()
        [pub] = box.entries()
        gh.uncertain_writes, gh.read_fails = ['lost'], 1
        d, _ = self.dispatcher(box, client, 76)
        d._signal_dropped()
        self.assertEqual(gh.bodies(76), [])
        [alert] = [p for p in box.entries() if p != pub]
        self.assertEqual(box.unsignalled(), [])
        gh.clock.now += IN_FLIGHT_SECONDS
        again, client2 = make(gh, self.state)
        gh.clock.now += IN_FLIGHT_SECONDS
        self.assertEqual(again.resume(), [])
        self.assertEqual(again.entries()[alert]['status'], DROPPED)
        d2, _ = self.dispatcher(again, client2, 76)
        d2._signal_dropped()
        [body] = gh.bodies(76)
        self.assertIn(f'`{pub}`', body)
        self.assertIn(f'`{alert}`', body)
        self.assertEqual(again.unsignalled(), [])

    def test_without_an_alert_issue_the_log_reports_it_at_each_start(self):
        gh, box, client = self.dropped()
        [pub] = box.entries()
        for start in range(2):
            with self.subTest(start=start):
                d, lines = self.dispatcher(box, client, 0)
                d._signal_dropped()
                d._signal_dropped()  # once per process
                warnings = [line for line in lines if f'publication {pub}' in line]
                self.assertEqual(len(warnings), 1)
                self.assertIn('no [run].alert_issue is configured', warnings[0])
                self.assertEqual(gh.bodies(76), [])
                self.assertEqual(len(box.unsignalled()), 1)

    def test_nothing_without_the_outbox_or_without_drops(self):
        gh = OutboxGH()
        box, client = make(gh, self.state)
        client.comment(7, 'record')
        for identities in (None, SimpleNamespace(outbox=None), SimpleNamespace(outbox=box)):
            d, lines = self.dispatcher(box, client, 76)
            d.identities = identities
            d._signal_dropped()
            self.assertEqual((lines, gh.bodies(76)), ([], []))

    def test_each_tick_reports_the_drops_after_settling(self):
        calls = []
        d, _ = self.dispatcher(None, FakeGH(), 76)
        for name in ('_housekeeping', '_watch_journal', '_resume_publications',
                     '_signal_dropped'):
            setattr(d, name, (lambda name: lambda: calls.append(name))(name))
        d.snapshot = lambda: Snapshot([], [], [])
        d.tick()
        self.assertEqual(calls, ['_housekeeping', '_watch_journal', '_resume_publications',
                                 '_signal_dropped'])
        calls.clear()
        d.tick(dry_run=True)
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
