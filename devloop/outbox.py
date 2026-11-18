"""The outbox of the publications (#385, ADR 0004 sections 3.1 and 4, P2-4).

With ``publication_outbox`` on (off by default: ADR 0004, D8), every ``comment``,
``edit_comment``, ``submit_review`` and ``create_pr`` written with a client of
``apps.Identities.github`` passes through the outbox: the ledger records, the agents' outputs
(the implementer's pull request, the reviewers' native reviews and comments), the receipts and
every other comment of devloop. The journal of the turns (``journal.TurnJournal``) is the
authority; GitHub gets its projection. Its events, each with the write's id ``pub`` and, where
it says so, the ``time`` of the clock it happened at:

- ``publication``: a write about to be sent, its first attempt: ``op`` (``comment``, ``edit``,
  ``review`` or ``pr``), its arguments (``number`` of the issue or PR, None for an edit whose
  comment the outbox has not seen; ``comment_id`` of an edit; ``commit`` and ``event`` of a
  review; ``base``, ``head`` and ``title`` of a pull request), ``actor`` and ``permissions`` of
  the client that writes, the whole ``body`` and ``time``. It is durable **before** anything is
  sent, and nothing is written between it and the send: if it cannot be written, nothing is
  sent, nothing is left to send at a restart, and the write fails as a failed write
  (``journal.JournalError`` is a ``GitHubError``);
- ``publication_sending``: a later attempt about to be sent, with its number and ``time``. From
  ``publication`` or here until an answer is recorded, the outcome is unknown;
- ``publication_sent``: published, with ``id`` (the comment's, the review's when known, the
  pull request's number; None when GitHub did not report it) and ``via``: ``response`` or
  ``reconciliation``. Final;
- ``publication_failed``: the caller was told the write failed, with ``time``. With
  ``uncertain`` false it was certainly not published, and that is final; with ``uncertain``
  true the outcome is unknown and still to reconcile, never to send again;
- ``publication_dropped``: not published and never to be, with the ``reason``. Final.

The rules, on the model of ``ProjectionOutbox`` (ssdlc_feedback_delivery.py):

- **No POST repeated on an uncertain outcome.** A write that raises ``GitHubUncertain`` is
  reconciled (``_reconcile``): a comment with ``ports.reconcile_comment``, on the exact body and
  the login of its author (one of the trusted users for the ambient ``gh`` login), skipping
  what the outbox already knows as another write's publication; an edit on its comment; a review
  among the PR's native reviews; a pull request among the open ones, from its head branch. Found:
  it is published. **Absence alone proves nothing** while the attempt may still take effect: a
  read counts as conclusive only when it starts ``IN_FLIGHT_SECONDS`` after a time at which the
  attempt is known to have ended, never counted from the time recorded before the send (a
  pause of the process may come between the two). In the process that sent it, that time is the
  end of the call, measured once the call returned (its ``gh`` ended, killed at its timeout).
  For an attempt cut short by a stop, whose end is unknown, it is ``ATTEMPT_SECONDS`` (the
  longest a ``gh`` call lasts) after this process first read the journal, under the dispatcher
  lock: the process that made the attempt had ended by then. The write that got the uncertain
  answer waits for that time and reads again; only a conclusive absence lets it be sent once
  more, at most ``MAX_SENDS`` sends in all. A failed read leaves it uncertain, and so does an
  exception of the client that is not a ``GitHubError``.
- **Before every new write** on an issue or PR (on a comment, a head branch), the writes of the
  same one left pending (not running in this process) are settled first (``settle``): reconciled,
  and sent or dropped on a conclusive absence. If one cannot be settled (unreadable, or not
  conclusive yet), the new write is not sent (``GitHubError``).
- **What the caller was told is never contradicted.** A write that failed for its caller is
  never sent again: at most it is found, published by an attempt already made. Its failure is
  recorded with ``defer`` (``TurnJournal.publication``): if the journal cannot take it, it is
  written before any later event of the journal, of the outbox or of the turns, and every write
  waits for it. The same holds for a ``publication_sent`` the journal could not take: the write
  is published, its comment and issue are kept in memory, and no later write, an edit of that
  comment included, is sent before it is durable.
- **A restart resumes** (``resume``, at the start of each tick): a write cut short, its caller
  stopped with the process, is reconciled, and sent once more only on a conclusive absence,
  read ``ATTEMPT_SECONDS`` plus ``IN_FLIGHT_SECONDS`` after the restart read the journal;
  until then it blocks the writes about the same issue, PR, comment or branch. A
  write whose caller was told it failed with an uncertain outcome is only reconciled: found, it
  is recorded as published; conclusively absent, it is dropped. A pending edit superseded by a
  later write on the same comment is dropped: the later one was built from GitHub as it was.

The answer of a reconciliation is as precise as the body: devloop's comments, reviews and pull
requests carry times, heads and digests, so two writes with the same body by the same author
are not expected. An edit is reconciled on its comment, whose issue or PR the outbox knows from
its own writes and from the comments it read; when it does not, an edit, which is never a
duplicate, is applied once more after the same wait.
"""

import datetime
import functools
import secrets
import threading
import time

from .github import GitHub, GitHubError, GitHubUncertain
from .journal import PUBLICATION, JournalError
from .ports import PORTS, port_methods, reconcile_comment

COMMENT, EDIT, REVIEW, PR = 'comment', 'edit', 'review', 'pr'
SENDING, SENT, FAILED, UNCERTAIN, DROPPED = 'sending', 'sent', 'failed', 'uncertain', 'dropped'
PENDING = (SENDING, UNCERTAIN)
MAX_SENDS = 2  # sends of one write: the first and, after a conclusive absence, one more
# How long GitHub may still apply an attempt after it ended: it ends the requests it takes more
# than 10 seconds to serve; the rest is margin, for the reads that follow a write.
IN_FLIGHT_SECONDS = 30
# The longest an attempt lasts: the timeout of each `gh` call. After a crash its end is unknown.
ATTEMPT_SECONDS = GitHub.timeout
SENDING_EVENT, SENT_EVENT, FAILED_EVENT, DROPPED_EVENT = (
    f'{PUBLICATION}_sending', f'{PUBLICATION}_sent', f'{PUBLICATION}_failed',
    f'{PUBLICATION}_dropped')


def _iso(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%SZ')


class Outbox:
    """The outbox of one dispatcher process, over the journal of the turns ``journal``.

    ``connect(actor, permissions)`` returns the client of ``actor`` without the outbox, to send
    the writes a previous process left; ``authors(actor)`` the logins that may have written as
    ``actor``. ``log`` takes a line; ``clock`` and ``sleep`` are ``time.time`` and
    ``time.sleep``."""

    def __init__(self, journal, connect, authors, log=None, clock=time.time, sleep=time.sleep):
        self.journal = journal
        self._connect = connect
        self._authors = authors
        self._log = log or (lambda line: None)
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()
        self._entries = None  # the writes, by ``pub``, in journal order
        self._numbers = {}  # comment id -> issue or PR, from the writes and the reads
        self._live = set()  # the writes this process is sending or settling
        # When this process first read the journal (``_load``), under the dispatcher lock: any
        # previous dispatcher had ended, and none of its attempts could start later (R2-2).
        self._since = None

    def client(self, tracker, actor, permissions=None):
        """``tracker``, the client of ``actor``, with its writes through the outbox."""
        return OutboxClient(self, tracker, actor, permissions)

    # State ---------------------------------------------------------------
    def _load(self):
        if self._entries is not None:
            return
        self._since = self._clock()
        entries = {}
        for event in self.journal.publications():
            self._apply(entries, event['event'], event['data'])
        self._entries = entries

    def _apply(self, entries, kind, data):
        if kind == PUBLICATION:
            entries[data['pub']] = dict(data, status=SENDING, sends=1, sent_id=None,
                                        started=data.get('time'), ended=None)
            if data['op'] == EDIT and data.get('number') is not None:
                self._numbers[data['comment_id']] = data['number']
            return
        entry = entries.get(data['pub'])
        if entry is None:
            return
        if kind == SENDING_EVENT:
            entry.update(status=SENDING, sends=entry['sends'] + 1, started=data.get('time'),
                         ended=None)
        elif kind == SENT_EVENT:
            entry.update(status=SENT, sent_id=data.get('id'))
            if (entry['op'] == COMMENT and data.get('id') is not None
                    and entry.get('number') is not None):
                self._numbers[data['id']] = entry['number']
        elif kind == FAILED_EVENT:
            entry.update(status=UNCERTAIN if data['uncertain'] else FAILED,
                         ended=data.get('time'))
        elif kind == DROPPED_EVENT:
            entry['status'] = DROPPED

    def _record(self, kind, data, defer=False):
        """Write an event in the journal, then apply it. With ``defer`` (an outcome already
        acted on) it is applied even when the journal cannot take it now, which then writes it
        before any later event; ``JournalError`` is raised all the same."""
        with self._lock:
            self._load()
            try:
                event = self.journal.publication(kind, data, defer=defer)
            except JournalError:
                if defer:
                    self._apply(self._entries, kind, data)
                raise
            self._apply(self._entries, kind, event['data'])

    def entries(self):
        """Every write, by id: its ``publication`` event with ``status`` and ``sends``."""
        with self._lock:
            self._load()
            return {pub: dict(entry) for pub, entry in self._entries.items()}

    def pending(self):
        """The writes still to settle, oldest first: sent without an answer, or failed with an
        uncertain outcome."""
        return [e for e in self.entries().values() if e['status'] in PENDING]

    def seen(self, number, comments):
        """Remember the issue or PR of the comments read, to reconcile their edits."""
        with self._lock:
            for comment in comments or ():
                if comment.get('id') is not None:
                    self._numbers[comment['id']] = number

    def number_of(self, comment_id):
        with self._lock:
            return self._numbers.get(comment_id)

    def _keys(self, entry):
        """What a write is about: its issue or PR, the comment of an edit, the head branch of a
        pull request. A new write first settles the pending ones it shares a key with."""
        if entry['op'] == PR:
            return {('head', entry['head'])}
        keys = set()
        number = entry.get('number')
        if entry['op'] == EDIT:
            keys.add(('comment', entry['comment_id']))
            if number is None:
                number = self._numbers.get(entry['comment_id'])
        if number is not None:
            keys.add(('number', number))
        return keys

    # Writes --------------------------------------------------------------
    def write(self, tracker, actor, permissions, op, body, **fields):
        """Write through the outbox: settle the pending writes about the same issue, PR,
        comment or branch, record the write, then send it. Returns what the client returns:
        the comment's id for a ``comment``, ``(number, url)`` for a ``pr``, None otherwise.
        Raises the ``GitHubError`` of a write that failed, ``GitHubUncertain`` when its outcome
        is still unknown."""
        if op == EDIT:
            fields['number'] = self.number_of(fields['comment_id'])
        with self._lock:
            keys = self._keys(dict(fields, op=op))
        self.settle(keys=keys)
        pub = f'pub-{secrets.token_hex(8)}'
        with self._lock:
            self._record(PUBLICATION, {
                'pub': pub, 'op': op, **fields, 'actor': actor, 'permissions': permissions,
                'body': body, 'time': self._clock()})
            self._live.add(pub)
            entry = self._entries[pub]
        try:
            return self._attempt(entry, tracker)
        finally:
            with self._lock:
                self._live.discard(pub)

    def _call(self, entry, tracker):
        op, body = entry['op'], entry['body']
        if op == COMMENT:
            return tracker.comment(entry['number'], body)
        if op == EDIT:
            return tracker.edit_comment(entry['comment_id'], body)
        if op == REVIEW:
            return tracker.submit_review(entry['number'], commit=entry['commit'],
                                         event=entry['event'], body=body)
        return tracker.create_pr(base=entry['base'], head=entry['head'], title=entry['title'],
                                 body=body)

    def _attempt(self, entry, tracker):
        """Send ``entry``, whose attempt is recorded, until it is published or fails. After an
        uncertain outcome it is reconciled, and sent again only on a conclusive absence, at
        most ``MAX_SENDS`` times."""
        pub = entry['pub']
        while True:
            try:
                result = self._call(entry, tracker)
            except GitHubUncertain as exc:
                with self._lock:
                    entry['ended'] = self._clock()  # after the call: its `gh` has ended
                try:
                    found, conclusive = self._look(entry, tracker, wait=True)
                except GitHubError:
                    self._failed(entry, exc, uncertain=True)
                    raise exc from None
                if found is not None:
                    return self._found(entry, found, tracker)
                if not conclusive or entry['sends'] >= MAX_SENDS:
                    self._failed(entry, exc, uncertain=True)
                    raise
                self._log(f'  {pub}: uncertain {entry["op"]} {self._where(entry)} not found on '
                          'GitHub once it could no longer take effect: sent once more')
                try:
                    self._record(SENDING_EVENT, {'pub': pub, 'attempt': entry['sends'] + 1,
                                                 'time': self._clock()})
                except JournalError as error:
                    # Not sent again, and none of its sends took effect: failed for certain.
                    self._failed(entry, error, uncertain=False)
                    raise
                continue
            except GitHubError as exc:
                self._failed(entry, exc, uncertain=False)
                raise
            except Exception as exc:
                # Not an answer of the client (an answer it could not parse, a bug): the
                # write may have been sent. Its caller is told it failed: only reconciled.
                with self._lock:
                    entry['ended'] = self._clock()
                self._failed(entry, exc, uncertain=True)
                raise
            return self._answered(entry, result)

    def _quiet_at(self, entry):
        """The time from which the last attempt of ``entry`` can no longer take effect: what
        GitHub may still apply (``IN_FLIGHT_SECONDS``) after a time at which the attempt is
        known to have ended. Never from the time recorded before the send, which a pause of
        the process may separate from the send by any amount (R2-2).

        In this process that time is the end of the call, measured once it returned, or the
        ``time`` of its ``publication_failed``. An attempt cut short by a stop has no known
        end: its process was gone when this one first read the journal (``_since``, under the
        dispatcher lock), so it cannot have started after that, and ``ATTEMPT_SECONDS``, the
        longest a `gh` call lasts, covers a `gh` it left running."""
        if entry['ended'] is not None:
            return entry['ended'] + IN_FLIGHT_SECONDS
        since = self._since if self._since is not None else self._clock()
        return max(entry['started'] or 0, since) + ATTEMPT_SECONDS + IN_FLIGHT_SECONDS

    def _look(self, entry, tracker, wait=False):
        """``(found, conclusive)``: what carries ``entry`` on GitHub, or None, and whether that
        absence is conclusive, the read having started when no attempt could still take effect.
        With ``wait``, an absence that is not conclusive yet is read again once it is."""
        quiet = self._quiet_at(entry)
        start = self._clock()
        found = self._reconcile(entry, tracker)
        if found is None and start < quiet and wait:
            self._sleep(max(0.0, quiet - self._clock()))
            start = self._clock()
            found = self._reconcile(entry, tracker)
        return found, found is not None or start >= quiet

    def _reconcile(self, entry, tracker):
        """What carries ``entry`` on GitHub: the comment, the native review or the open pull
        request, or None when it is not there. An edit whose issue or PR is not known is not
        found: applying it again is no duplicate."""
        op, body, authors = entry['op'], entry['body'], self._authors(entry['actor'])
        if op == EDIT:
            number = entry.get('number')
            if number is None:
                number = self.number_of(entry['comment_id'])
            if number is None:
                return None
            return reconcile_comment(tracker, number, authors, body,
                                     comment_id=entry['comment_id'])
        with self._lock:
            claimed = {e['sent_id'] for e in self._entries.values()
                       if e['status'] == SENT and e['op'] == op and e['sent_id'] is not None}
        if op == COMMENT:
            return reconcile_comment(tracker, entry['number'], authors, body, exclude=claimed)
        if op == REVIEW:
            return next((r for r in tracker.pr_reviews(entry['number'])
                         if r.get('login') in authors and r.get('body') == body
                         and r.get('id') not in claimed), None)
        return next((p for p in tracker.open_prs()
                     if p.get('headRefName') == entry['head'] and p.get('title') == entry['title']
                     and p.get('body') == body and p.get('number') not in claimed
                     and (p.get('author') or {}).get('login') in authors), None)

    def _answered(self, entry, result):
        """Published, as the client answered: what the caller gets."""
        op = entry['op']
        if op == COMMENT:
            sent_id = result
        elif op == EDIT:
            sent_id = entry['comment_id']
        elif op == PR:
            sent_id = result[0] if result else None
        else:
            sent_id = None  # a native review: the client does not report its id
        self._sent(entry, sent_id, 'response')
        return result

    def _found(self, entry, found, tracker):
        """Published, as a reconciliation found it: what the caller gets."""
        op = entry['op']
        if op == PR:
            number = found.get('number')
            self._sent(entry, number, 'reconciliation')
            repo = getattr(tracker, 'repo', None)
            return number, found.get('url') or (f'https://github.com/{repo}/pull/{number}'
                                                if repo else None)
        sent_id = entry['comment_id'] if op == EDIT else found.get('id')
        self._sent(entry, sent_id, 'reconciliation')
        return sent_id if op == COMMENT else None

    def _sent(self, entry, sent_id, via):
        try:
            self._record(SENT_EVENT, {'pub': entry['pub'], 'id': sent_id, 'via': via},
                         defer=True)
        except JournalError as exc:
            self._log(f'  {entry["pub"]}: published, not recorded in the journal yet: no write '
                      f'is sent before it is: {exc}')

    def _failed(self, entry, error, *, uncertain):
        try:
            self._record(FAILED_EVENT, {'pub': entry['pub'], 'uncertain': uncertain,
                                        'error': str(error)[:300], 'time': self._clock()},
                         defer=True)
        except JournalError as exc:
            self._log(f'  {entry["pub"]}: failure not recorded in the journal yet: no write is '
                      f'sent before it is: {exc}')

    def _drop(self, entry, reason):
        self._record(DROPPED_EVENT, {'pub': entry['pub'], 'reason': reason})
        self._log(f'  {entry["pub"]}: {entry["op"]} {self._where(entry)} dropped: {reason}')

    @staticmethod
    def _where(entry):
        if entry['op'] == PR:
            return f'of {entry["head"]}'
        if entry['op'] == EDIT:
            return f'of comment {entry["comment_id"]}'
        return f'on #{entry["number"]}'

    # Settling ------------------------------------------------------------
    def _claim(self, keys=None):
        """The pending writes sharing one of ``keys`` (every one with None) not running in this
        process, oldest first, now marked running."""
        with self._lock:
            self._load()
            found = [e for e in self._entries.values()
                     if e['status'] in PENDING and e['pub'] not in self._live
                     and (keys is None or self._keys(e) & keys)]
            self._live.update(e['pub'] for e in found)
            return found

    def _release(self, entries):
        with self._lock:
            self._live.difference_update(e['pub'] for e in entries)

    def settle(self, number=None, *, keys=None):
        """Settle the pending writes about issue or PR ``number`` (or sharing one of ``keys``),
        oldest first. Raises the ``GitHubError`` of the first that cannot be settled: what
        follows waits."""
        if keys is None:
            keys = {('number', number)}
        claimed = self._claim(keys)
        try:
            for entry in claimed:
                self._settle(entry)
        finally:
            self._release(claimed)

    def resume(self):
        """Write what the journal could not take, then settle every pending write, as at the
        start of a tick after a restart. Returns the lines to log: one for each write that
        could not be settled."""
        try:
            self.journal.flush()
        except JournalError as exc:
            return [f'  outbox of the publications: the journal does not take its events yet: '
                    f'nothing is sent: {exc}']
        lines = []
        claimed = self._claim()
        try:
            for entry in claimed:
                try:
                    self._settle(entry)
                except GitHubError as exc:
                    lines.append(f'  publication {entry["pub"]} ({entry["op"]} '
                                 f'{self._where(entry)}) not settled: {exc}')
        finally:
            self._release(claimed)
        return lines

    def _superseded(self, entry):
        if entry['op'] != EDIT:
            return False
        with self._lock:
            later = list(self._entries)[list(self._entries).index(entry['pub']) + 1:]
            return any(self._entries[p]['op'] == EDIT
                       and self._entries[p]['comment_id'] == entry['comment_id'] for p in later)

    def _settle(self, entry):
        with self._lock:
            self._load()
            entry = self._entries[entry['pub']]
            if entry['status'] not in PENDING:
                return
        if self._superseded(entry):
            self._drop(entry, 'superseded by a later edit of the same comment')
            return
        tracker = self._connect(entry['actor'], entry['permissions'])
        found, conclusive = self._look(entry, tracker)
        if found is not None:
            self._found(entry, found, tracker)
            return
        if not conclusive:
            # The attempt may still take effect: absence proves nothing yet (R1-2).
            raise GitHubUncertain(
                f'{entry["pub"]}: {entry["op"]} {self._where(entry)} not found on GitHub, but '
                f'it may still take effect until {_iso(self._quiet_at(entry))}: nothing is '
                'sent on it before')
        if entry['status'] == UNCERTAIN:
            self._drop(entry, 'not found on GitHub, and its caller was told it failed')
        elif entry['sends'] >= MAX_SENDS:
            self._drop(entry, f'not found on GitHub after {MAX_SENDS} sends')
        else:
            # Cut short by a restart, its caller gone: sent once more.
            self._record(SENDING_EVENT, {'pub': entry['pub'], 'attempt': entry['sends'] + 1,
                                         'time': self._clock()})
            self._attempt(entry, tracker)


def for_identities(identities, journal, trusted_users, log=None):
    """The outbox of ``identities`` (``apps.Identities``) over ``journal``: an actor with an app
    writes as the app's login, one without it as the ambient ``gh`` login, the operator's, one
    of ``trusted_users``."""
    def authors(actor):
        login = identities.login(actor)
        return [login] if login else list(trusted_users)
    return Outbox(journal, identities.direct, authors, log=log)


class OutboxClient:
    """The client of one actor with its ``comment``, ``edit_comment``, ``submit_review`` and
    ``create_pr`` through the outbox (#385); everything else reaches the client as it is."""

    def __init__(self, outbox, tracker, actor, permissions):
        object.__setattr__(self, '_outbox', outbox)
        object.__setattr__(self, '_tracker', tracker)
        object.__setattr__(self, '_actor', actor)
        object.__setattr__(self, '_permissions', permissions)

    def __getattr__(self, name):
        return getattr(self._tracker, name)

    def __setattr__(self, name, value):
        setattr(self._tracker, name, value)  # ``time_left``, set by ``devloop watch``

    def _write(self, op, body, **fields):
        return self._outbox.write(self._tracker, self._actor, self._permissions, op, body,
                                  **fields)

    def comment(self, number, body):
        return self._write(COMMENT, body, number=number)

    def edit_comment(self, comment_id, body):
        self._write(EDIT, body, comment_id=comment_id)

    def submit_review(self, number, *, commit, event, body):
        self._write(REVIEW, body, number=number, commit=commit, event=event)

    def create_pr(self, *, base, head, title, body):
        return self._write(PR, body, base=base, head=head, title=title)

    def issue_comments(self, number):
        comments = self._tracker.issue_comments(number)
        self._outbox.seen(number, comments)
        return comments

    def pr_comments(self, number):
        comments = self._tracker.pr_comments(number)
        self._outbox.seen(number, comments)
        return comments


def _delegate(port, name):
    @functools.wraps(getattr(port, name))
    def method(self, *args, **kwargs):
        return getattr(self._tracker, name)(*args, **kwargs)
    return method


# Every other method of the ports reaches the client as it is, with its signature: the client
# is a ``ports.Backend`` like the one it wraps.
for _port in PORTS:
    for _name in port_methods(_port):
        if _name not in vars(OutboxClient):
            setattr(OutboxClient, _name, _delegate(_port, _name))
