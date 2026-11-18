"""The outbox of the publications (#385, ADR 0004 sections 3.1 and 4, P2-4).

With ``publication_outbox`` on (off by default: ADR 0004, D8), every ``comment`` and
``edit_comment`` written with a client of ``apps.Identities.github`` passes through the outbox:
the ledger records, the agents' outputs, the receipts and every other comment of devloop. The
journal of the turns (``journal.TurnJournal``) is the authority; GitHub gets its projection.
Its events, each with the write's id ``pub``:

- ``publication``: a write about to be sent: ``op`` (``comment`` or ``edit``), ``number`` (the
  issue or PR; for an edit, None when the outbox has not seen the comment), ``comment_id`` of an
  edit, ``actor`` and ``permissions`` of the client that writes, and the whole ``body``. It is
  durable **before** anything is sent: if it cannot be written, nothing is sent and the write
  fails as a failed write (``journal.JournalError`` is a ``GitHubError``);
- ``publication_sending``: an attempt about to be sent, with its number. From here until an
  answer is recorded, the outcome is unknown;
- ``publication_sent``: published, with the ``comment_id`` (None when GitHub did not report it)
  and ``via``: ``response`` or ``reconciliation``. Final;
- ``publication_failed``: the caller was told the write failed. With ``uncertain`` false GitHub
  certainly did not write it, and that is final; with ``uncertain`` true the outcome is unknown
  and still to reconcile;
- ``publication_dropped``: not published and never to be, with the ``reason``. Final.

The rules, on the model of ``ProjectionOutbox`` (ssdlc_feedback_delivery.py):

- **No POST repeated on an uncertain outcome.** A write that raises ``GitHubUncertain`` is
  reconciled at once with ``ports.reconcile_comment``, on the exact body and the login of its
  author (one of the trusted users for the ambient ``gh`` login), skipping the comments the
  outbox already knows as another write's publication. Found: it is published. Not found: it is
  sent once more, at most ``MAX_SENDS`` sends in all, each after a reconciliation that did not
  find it. A failed read leaves it uncertain.
- **Before every new write on an issue or PR**, the writes of that number left pending (not
  running in this process) are settled first (``settle``): reconciled, and sent or dropped. If
  one cannot be settled, the new write is not sent (``GitHubError``).
- **A restart resumes** (``resume``, at the start of each tick): a write left ``publication``
  is sent; one left ``publication_sending`` is reconciled, and sent once more only when not
  found. A write whose caller was told it failed with an uncertain outcome is only reconciled:
  found, it is recorded as published; not found, it is dropped, since its caller already
  handled it as not published. A pending edit superseded by a later write on the same comment
  is dropped: the later one was built from GitHub as it was.

The answer of a reconciliation is as precise as the body: devloop's comments carry times,
heads and digests, so two writes with the same body by the same author are not expected. An
edit is reconciled on its comment, whose issue or PR the outbox knows from its own writes and
from the comments it read; when it does not, an edit, which is never a duplicate, is applied
once more.
"""

import functools
import secrets
import threading

from .github import GitHubError, GitHubUncertain
from .journal import PUBLICATION, JournalError
from .ports import PORTS, port_methods, reconcile_comment

COMMENT, EDIT = 'comment', 'edit'
PREPARED, SENDING, SENT, FAILED, UNCERTAIN, DROPPED = (
    'prepared', 'sending', 'sent', 'failed', 'uncertain', 'dropped')
PENDING = (PREPARED, SENDING, UNCERTAIN)
MAX_SENDS = 2  # sends of one write: the first and, after a reconciliation, one more


class Outbox:
    """The outbox of one dispatcher process, over the journal of the turns ``journal``.

    ``connect(actor, permissions)`` returns the client of ``actor`` without the outbox, to send
    the writes a previous process left; ``authors(actor)`` the logins that may have written as
    ``actor``. ``log`` takes a line."""

    def __init__(self, journal, connect, authors, log=None):
        self.journal = journal
        self._connect = connect
        self._authors = authors
        self._log = log or (lambda line: None)
        self._lock = threading.RLock()
        self._entries = None  # the writes, by ``pub``, in journal order
        self._numbers = {}  # comment id -> issue or PR, from the writes and the reads
        self._live = set()  # the writes this process is sending or settling

    def client(self, tracker, actor, permissions=None):
        """``tracker``, the client of ``actor``, with its writes through the outbox."""
        return OutboxClient(self, tracker, actor, permissions)

    # State ---------------------------------------------------------------
    def _load(self):
        if self._entries is not None:
            return
        entries = {}
        for event in self.journal.publications():
            self._apply(entries, event['event'], event['data'])
        self._entries = entries

    def _apply(self, entries, kind, data):
        if kind == PUBLICATION:
            entries[data['pub']] = dict(data, status=PREPARED, sends=0, sent_id=None)
            if data['op'] == EDIT and data['number'] is not None:
                self._numbers[data['comment_id']] = data['number']
            return
        entry = entries.get(data['pub'])
        if entry is None:
            return
        if kind == f'{PUBLICATION}_sending':
            entry.update(status=SENDING, sends=entry['sends'] + 1)
        elif kind == f'{PUBLICATION}_sent':
            entry.update(status=SENT, sent_id=data['comment_id'])
            if data['comment_id'] is not None and entry['number'] is not None:
                self._numbers[data['comment_id']] = entry['number']
        elif kind == f'{PUBLICATION}_failed':
            entry['status'] = UNCERTAIN if data['uncertain'] else FAILED
        elif kind == f'{PUBLICATION}_dropped':
            entry['status'] = DROPPED

    def _record(self, kind, data):
        with self._lock:
            self._load()
            event = self.journal.publication(kind, data)
            self._apply(self._entries, kind, event['data'])

    def entries(self):
        """Every write, by id: its ``publication`` event with ``status`` and ``sends``."""
        with self._lock:
            self._load()
            return {pub: dict(entry) for pub, entry in self._entries.items()}

    def pending(self):
        """The writes still to settle, oldest first: never sent, sent without an answer, or
        failed with an uncertain outcome."""
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

    # Writes --------------------------------------------------------------
    def write(self, tracker, actor, permissions, op, body, *, number=None, comment_id=None):
        """Write through the outbox: settle the pending writes of the same issue or PR, record
        the write, then send it. Returns the comment's id for a ``comment``, None for an
        edit. Raises the ``GitHubError`` of a write that failed, ``GitHubUncertain`` when its
        outcome is still unknown."""
        if op == EDIT:
            number = self.number_of(comment_id)
        if number is not None:
            self.settle(number)
        pub = f'pub-{secrets.token_hex(8)}'
        with self._lock:
            self._record(PUBLICATION, {
                'pub': pub, 'op': op, 'number': number, 'comment_id': comment_id,
                'actor': actor, 'permissions': permissions, 'body': body})
            self._live.add(pub)
            entry = self._entries[pub]
        try:
            return self._send(entry, tracker)
        finally:
            with self._lock:
                self._live.discard(pub)

    def _send(self, entry, tracker):
        """Send ``entry`` until it is published, or until it fails: reconciled after each
        uncertain outcome, and sent again only when not found, at most ``MAX_SENDS`` times."""
        pub = entry['pub']
        while True:
            try:
                self._record(f'{PUBLICATION}_sending', {'pub': pub, 'attempt': entry['sends'] + 1})
            except JournalError:
                entry['status'] = FAILED  # nothing sent; its caller is told it failed
                raise
            try:
                if entry['op'] == COMMENT:
                    result = tracker.comment(entry['number'], entry['body'])
                else:
                    tracker.edit_comment(entry['comment_id'], entry['body'])
                    result = entry['comment_id']
            except GitHubUncertain as exc:
                try:
                    found = self._reconcile(entry, tracker)
                except GitHubError:
                    self._failed(entry, exc, uncertain=True)
                    raise exc from None
                if found is not None:
                    return self._sent(entry, found.get('id'), 'reconciliation')
                if entry['sends'] >= MAX_SENDS:
                    self._failed(entry, exc, uncertain=True)
                    raise
                self._log(f'  {pub}: uncertain {entry["op"]} on #{entry["number"]} not found '
                          'on GitHub: sent once more')
                continue
            except GitHubError as exc:
                self._failed(entry, exc, uncertain=False)
                raise
            return self._sent(entry, result, 'response')

    def _reconcile(self, entry, tracker):
        """The comment that carries ``entry`` on GitHub, or None when it is not there. An edit
        whose issue or PR is not known is not found: applying it again is no duplicate."""
        number = entry['number']
        if entry['op'] == EDIT:
            number = number if number is not None else self.number_of(entry['comment_id'])
            if number is None:
                return None
            return reconcile_comment(tracker, number, self._authors(entry['actor']),
                                     entry['body'], comment_id=entry['comment_id'])
        with self._lock:
            claimed = {e['sent_id'] for e in self._entries.values()
                       if e['status'] == SENT and e['op'] == COMMENT and e['sent_id'] is not None}
        return reconcile_comment(tracker, number, self._authors(entry['actor']), entry['body'],
                                 exclude=claimed)

    def _sent(self, entry, comment_id, via):
        try:
            self._record(f'{PUBLICATION}_sent', {'pub': entry['pub'], 'comment_id': comment_id,
                                                 'via': via})
        except JournalError as exc:
            # Published all the same: left ``sending``, a later settle finds it on GitHub.
            self._log(f'  {entry["pub"]}: published, not recorded in the journal: {exc}')
        return comment_id if entry['op'] == COMMENT else None

    def _failed(self, entry, error, *, uncertain):
        try:
            self._record(f'{PUBLICATION}_failed', {'pub': entry['pub'], 'uncertain': uncertain,
                                                   'error': str(error)[:300]})
        except JournalError as exc:
            self._log(f'  {entry["pub"]}: failure not recorded in the journal: {exc}')

    def _drop(self, entry, reason):
        self._record(f'{PUBLICATION}_dropped', {'pub': entry['pub'], 'reason': reason})
        self._log(f'  {entry["pub"]}: {entry["op"]} on #{entry["number"]} dropped: {reason}')

    # Settling ------------------------------------------------------------
    def _claim(self, number=None, every=False):
        """The pending writes of ``number`` (of every number with ``every``) not running in
        this process, oldest first, now marked running."""
        with self._lock:
            self._load()
            found = [e for e in self._entries.values()
                     if e['status'] in PENDING and e['pub'] not in self._live
                     and (every or e['number'] == number
                          or e['op'] == EDIT and self._numbers.get(e['comment_id']) == number)]
            self._live.update(e['pub'] for e in found)
            return found

    def _release(self, entries):
        with self._lock:
            self._live.difference_update(e['pub'] for e in entries)

    def settle(self, number):
        """Settle the pending writes of issue or PR ``number``, oldest first. Raises the
        ``GitHubError`` of the first that cannot be settled: what follows waits."""
        claimed = self._claim(number)
        try:
            for entry in claimed:
                self._settle(entry)
        finally:
            self._release(claimed)

    def resume(self):
        """Settle every pending write, as at the start of a tick after a restart. Returns the
        lines to log: one for each write that could not be settled."""
        lines = []
        claimed = self._claim(every=True)
        try:
            for entry in claimed:
                try:
                    self._settle(entry)
                except GitHubError as exc:
                    lines.append(f'  publication {entry["pub"]} ({entry["op"]} on '
                                 f'#{entry["number"]}) not settled: {exc}')
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
        current = self.entries()[entry['pub']]
        if current['status'] not in PENDING:
            return
        if self._superseded(current):
            self._drop(current, 'superseded by a later edit of the same comment')
            return
        tracker = self._connect(current['actor'], current['permissions'])
        if current['status'] == PREPARED:
            # Recorded, never sent: a restart sends it.
            self._send(self._entries[current['pub']], tracker)
            return
        found = self._reconcile(current, tracker)
        if found is not None:
            self._sent(self._entries[current['pub']], found.get('id'), 'reconciliation')
            return
        if current['status'] == UNCERTAIN:
            self._drop(current, 'not found on GitHub, and its caller was told it failed')
        elif current['sends'] >= MAX_SENDS:
            self._drop(current, f'not found on GitHub after {MAX_SENDS} sends')
        else:
            self._send(self._entries[current['pub']], tracker)


def for_identities(identities, journal, trusted_users, log=None):
    """The outbox of ``identities`` (``apps.Identities``) over ``journal``: an actor with an app
    writes as the app's login, one without it as the ambient ``gh`` login, the operator's, one
    of ``trusted_users``."""
    def authors(actor):
        login = identities.login(actor)
        return [login] if login else list(trusted_users)
    return Outbox(journal, identities.direct, authors, log=log)


class OutboxClient:
    """The client of one actor with its ``comment`` and ``edit_comment`` through the outbox
    (#385); everything else reaches the client as it is."""

    def __init__(self, outbox, tracker, actor, permissions):
        object.__setattr__(self, '_outbox', outbox)
        object.__setattr__(self, '_tracker', tracker)
        object.__setattr__(self, '_actor', actor)
        object.__setattr__(self, '_permissions', permissions)

    def __getattr__(self, name):
        return getattr(self._tracker, name)

    def __setattr__(self, name, value):
        setattr(self._tracker, name, value)  # ``time_left``, set by ``devloop watch``

    def comment(self, number, body):
        return self._outbox.write(self._tracker, self._actor, self._permissions, COMMENT, body,
                                   number=number)

    def edit_comment(self, comment_id, body):
        self._outbox.write(self._tracker, self._actor, self._permissions, EDIT, body,
                           comment_id=comment_id)

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
