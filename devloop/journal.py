"""The durable journal of the turns' intents and outcomes (#362, ADR 0004, P2-2).

The journal in ``state_dir`` is the authority on the turns of the agent CLIs; the ``turn``
records of the ledger on GitHub are its published projection, in the same format as before.
It is append-only, one JSON event per line, each with its ``sequence``, the hash of the event
before it (``previous``) and its own (``sha256``), like the journal of the local engine:

- ``intent``: a turn about to start, written and made durable (``fsync``) **before** its CLI
  starts. If it cannot be written, the turn does not start;
- ``outcome``: how a turn ended: ``returned`` (the CLI returned), ``failed`` (it failed),
  ``not_dispatched`` (its record could not be written, the CLI never started) or ``uncertain``
  (no outcome was recorded: the dispatcher stopped while the turn ran, or the outcome could not
  be written). Nothing is ever retried automatically because a turn is uncertain;
- ``reconciled``: the outcome of an uncertain turn, decided by a trusted user with
  ``/devloop reconcile`` (``reconcile_command``), once, with author, comment, note and time.
  Nothing is refunded: the turn stays counted;
- ``refused``: a reconciliation command not applied, with why, so that it is answered once;
- ``unsettled`` and ``settled``: an issue or PR whose ``turn`` record on GitHub was left open
  (its closing edit failed), and its check by a later tick (``Ledger.pending``). A turn that
  becomes ``uncertain`` keeps its number in that trace too, until a tick checks it (``settle``);
- ``migrated``: the counts of ``ledger-open-turns.json``, the trace this journal replaces,
  imported once as legacy turns explicitly ``uncertain`` (``legacy-<number>-<hex>``), with
  number and count and no outcome made up;
- ``linked``: the issue of the work of a legacy turn, as the dispatcher recognises it from the
  branch and marker of its PR, open or closed (the number itself for an issue), kept so that
  the turn blocks the whole work also after a restart. Until it is linked, a legacy turn may
  be on a PR of any work, and with the block it blocks every new turn;
- ``publication`` and the other events whose kind starts with it: the outbox of the
  publications (``outbox.Outbox``, #385), with ``publication_outbox`` on. Each comment, edit,
  native review and pull request devloop writes on GitHub is here, body included, before it is
  sent. The turns ignore them. An outcome the outbox already acted on and the journal could not
  take is written before any later event (``publication``, ``defer``).
- ``size_warning``: the operator was warned on ``[run].alert_issue`` that the journal reached
  ``WARN_RATIO`` of its limit (#397), with its ``size``, the ``threshold`` and the ``limit``, in
  bytes: once per threshold.
- ``grant``: a grant of a delegation of the operator to the supervisor (#405, ADR 0005, P5-1),
  such as a review round granted with ``/devloop grant-round``: the ``delegation``, the issue or
  PR ``number``, the ``comment`` of the command and its author (``by``). It is written before
  the grant applies, and the grants of a UTC day count against the daily cap of the delegations.
  The extension of a budget envelope (``/devloop grant-budget``, #411, ADR 0005, P5-7) is a
  ``grant`` of the delegation ``budget`` that also records the ``story`` and the ``extension``
  (the turns and seconds it adds), with the ``reason`` of the command: one per story, ever, and
  the authority on the envelope of that story (``devloop.budget``). The grant books the
  extension; the first turn of the story booked after it spends it (#466). A review round
  granted with ``/devloop grant-round`` on a ``round_limit`` escalation to the operator (#553)
  is a ``grant`` of the delegation ``operator_rounds``, which counts against no daily cap;
- ``grant_lapsed``: the extension of a story whose first turn after the grant was refused all
  the same, before any turn used it (#466): its ``story``, the ``comment`` of the command, the
  issue or PR ``number`` and the ``category`` of the refusal. It no longer extends the envelope
  and does not spend the story's one extension; the grant still counts against the daily cap of
  its day.
- ``delegated_merge``: a merge of a PR whose merge was the operator's, delegated to the
  supervisor with ``/devloop merge`` (#486): the PR ``number``, the ``comment`` of the command,
  its author (``by``) and the ``head`` it merges. It is written before the merge, once every
  condition holds, and the delegated merges of a UTC day count against their own daily cap,
  never against the one of the ``grant`` events.
- ``coordinator_merge``: a reserved merge of the coordinator (#529, ADR 0002, precision of
  2026-11-22), written with ``/devloop merge`` from the coordinator's identity, with the data
  of a ``delegated_merge``. The merges of the coordinator of a UTC day count against a daily cap
  of their own, never against the one of the delegated merges nor of the ``grant`` events.
- ``config``: the configuration the dispatcher read (ADR 0006, R2, trace T1 of K1), at its start
  (``when: start``) and at each reload of ``devloop run`` that found the files changed
  (``when: reload``): the SHA-256 of the local overlay and of the project file (null without the
  file), that of the effective configuration (null, with the ``error``, for one that does not
  load) and the trust keys whose value changed since the previous ``config`` event, each with its
  ``key``, ``old`` and ``new`` value (null when not set). The first one lists every trust key.

With ``story_identity`` on (#363, ADR 0004, P2-3a) it also holds the stories, the unit of the
budget:

- ``story``: a story the dispatcher assigned at the takeover of its root issue, with its id
  (``story-<sequence>-<hex>``), the repository and number of the root and the hash of the
  ``request`` record of that takeover;
- ``request``: the hash of a later ``request`` record of the root that differs from the last
  one (a trusted user changed the issue): the story keeps its id and every earlier hash;
- ``child``: an issue of the same repository associated with a story. Only the dispatcher writes
  it (``Ledger.mark_child``), and no flow does yet: which act authorizes it is decided with step
  3 of ADR 0001.

With ``attempt_identity`` on as well (#384, ADR 0004, P2-3b) it holds the attempts of each
phase, recommendation (i) of section 3.2 with one active attempt per phase:

- ``attempt``: an attempt opened in a phase of a story, the triple ``(story, phase,
  revision)``; in the current flow the phase is ``implementation`` and the revision null. Its
  id (``attempt-<sequence>-<hex>``), its number in the phase (``n``, from 1), the issue it
  works on, and the key ``(design, task)`` of a unit generated by a plan (the sha of the
  approved design and the task id), both null until step 3. It is refused while the phase has
  an active attempt (``AttemptActive``);
- ``attempt_pr``: the PR opened for an attempt, one per attempt;
- ``concluded``: the outcome that closes an attempt, once: ``merged`` or ``closed`` (its PR
  merged, or closed without merge) or ``abandoned`` (``abandon``, an internal primitive no flow
  calls yet). Only after it may the phase open a new attempt.

With the budget on (#365, ADR 0004, P2-5a, ``devloop.budget``), the intent of a turn charged to
a story carries its booking (``budget``: story, role, phase, engine and the declared rule for
unknown usage with its reserve), written in the same event, and the outcome of every turn the
dollars its CLI reported (``usage``). Nothing else changes: with the budget off the events are
those above.

Reads verify the whole chain. An incomplete or unreadable last line (a write cut short) is set
aside in ``turn-journal-tail-<sha256>.bin`` and removed, and the valid records before it stay; a
hash that does not match, or an unreadable line before the last, is an explicit error. Next to
the journal a snapshot (``turn-journal-state.json``) keeps the sequence and hash of its latest
event: a snapshot without its journal, or a journal shorter than it or diverging from it, is
refused (ADR 0001, constraint 3). The chain is not keyed: it gives integrity, not provenance.

The file has a limit (#397, ``journal_max_mib``, 64 MiB by default, ``max_bytes``): its size is
checked before it is loaded and before each append. A journal beyond the limit is not loaded,
and an event that would take it beyond is not written: ``JournalError``, as for any journal that
cannot be read or written. Nothing is ever removed, cut or replaced to stay under it, and the
checks of the chain and of the snapshot stay the same. From ``WARN_RATIO`` of the limit
(``warn_bytes``) the dispatcher warns the operator.

Only the dispatcher that holds ``dispatcher.lock`` writes the journal. ``devloop status`` and
``devloop tick --dry-run`` run beside it without the lock: they read it ``read_only``, with no
effect on the files. They neither set aside a tail nor import or remove the old trace, and what
their planning records (a link, an uncertain turn) stays in their memory. A record they appended
to the file would break the chain the running dispatcher keeps in memory, and with it the
journal.
"""

import hashlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from .github import GitHubError

JOURNAL = 'turn-journal.jsonl'
SNAPSHOT = 'turn-journal-state.json'
LEGACY = 'ledger-open-turns.json'  # the trace this journal replaces (#133)
RETURNED, FAILED, UNCERTAIN, NOT_DISPATCHED = 'returned', 'failed', 'uncertain', 'not_dispatched'
OUTCOMES = (RETURNED, FAILED, UNCERTAIN, NOT_DISPATCHED)
# The outcomes a trusted user may give an uncertain turn: never ``uncertain`` again.
RECONCILED_OUTCOMES = (RETURNED, FAILED, NOT_DISPATCHED)
RECONCILE_COMMAND = '/devloop reconcile'
PUBLICATION = 'publication'  # the prefix of the events of the outbox (#385, ``outbox``)
MIB = 1024 * 1024
# The limit of the journal's file (#397, ``journal_max_mib``): a journal beyond it is not loaded,
# and an event that would take it beyond is not written. Nothing is ever removed to stay under.
MAX_BYTES = 64 * MIB
# The share of the limit at which devloop warns the operator, once per threshold (#397).
WARN_RATIO = 0.8
SIZE_WARNING = 'size_warning'  # the event of that warning
GRANT = 'grant'  # a grant of a delegation to the supervisor (#405), for the daily cap
BUDGET = 'budget'  # the delegation of the extension of a budget envelope (#411)
# A review round granted on an escalation to the operator (#553): outside the daily cap.
OPERATOR_ROUNDS = 'operator_rounds'
LAPSED = 'grant_lapsed'  # an extension no turn used, its first turn refused all the same (#466)
DELEGATED_MERGE = 'delegated_merge'  # a merge delegated to the supervisor (#486), for its cap
COORDINATOR_MERGE = 'coordinator_merge'  # a reserved merge of the coordinator (#529), for its cap
CONFIG = 'config'  # the configuration read at a start or a reload (ADR 0006, T1)
CONFIG_HASHES = ('overlay', 'project', 'effective')
# A turn's id: its sequence (or, for a legacy one, its number) and 8 hex digits that differ
# between journals, so that a command written for the turns of a journal since lost never
# reconciles a turn of a new one.
_TURN_ID = re.compile(r'(turn|legacy)-[1-9][0-9]*-[0-9a-f]{8}')
# A story's id (#363), made the same way.
STORY_ID = re.compile(r'story-[1-9][0-9]*-[0-9a-f]{8}')
# An attempt's id (#384), made the same way.
ATTEMPT_ID = re.compile(r'attempt-[1-9][0-9]*-[0-9a-f]{8}')
# The kinds of phase (#384): the current flow has only the implementation; step 3 adds the others.
IMPLEMENTATION = 'implementation'
PHASES = (IMPLEMENTATION,)
# The outcomes that conclude an attempt (#384).
MERGED, CLOSED, ABANDONED = 'merged', 'closed', 'abandoned'
ATTEMPT_OUTCOMES = (MERGED, CLOSED, ABANDONED)


class JournalError(GitHubError):
    """The journal cannot be read, verified or written. A ``GitHubError``, so that a step whose
    intent cannot be recorded stops and escalates like a failed write of its ledger record."""


class AttemptActive(JournalError):
    """A new attempt refused: its phase has an active attempt, not concluded yet (#384)."""

    def __init__(self, message, attempt):
        super().__init__(message)
        self.attempt = attempt


class TurnBlocked(JournalError):
    """A new turn refused: the same work has uncertain turns not reconciled yet (#362)."""

    def __init__(self, message, turns):
        super().__init__(message)
        self.turns = turns


def digest(event):
    data = json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def reconcile_command(body):
    """The reconciliation command of a comment (#362): ``(turn, outcome, note)`` when the first
    line after the attribution header is ``/devloop reconcile <turn> <outcome>``, None when the
    comment is no such command. The note is every line after the first, stripped; the first
    line holds the command alone. A command with a missing or extra word on its first line, an
    outcome that is not ``returned``, ``failed`` or ``not_dispatched``, or no note, has None in
    the parts that are not valid: it is malformed."""
    lines = (body or '').strip().splitlines()
    while lines and (not lines[0].strip() or lines[0].strip().startswith('>')):
        lines.pop(0)
    tokens = lines[0].split() if lines else []
    if tokens[:2] != RECONCILE_COMMAND.split():
        return None
    note = '\n'.join(lines[1:]).strip() or None
    if len(tokens) != 4:
        return None, None, note
    turn = tokens[2] if _TURN_ID.fullmatch(tokens[2]) else None
    outcome = tokens[3] if tokens[3] in RECONCILED_OUTCOMES else None
    return turn, outcome, note


def describe_turn(turn):
    """A short Italian description of a turn of ``TurnJournal.turns``."""
    if turn.get('legacy') is not None:
        text = (f'`{turn["turn"]}`: turni aperti importati da `{LEGACY}` su #{turn["number"]} '
                f'({turn["legacy"]}), senza esito noto')
        if turn.get('issue') is None:
            text += (': non si è ancora potuto leggere su GitHub a quale lavoro appartiene, e '
                     'finché non lo si legge blocca ogni nuovo turno')
        return text
    text = (f'`{turn["turn"]}`: turno `{turn["kind"]}` di `{turn["agent"]}` su '
            f'#{turn["number"]}, iniziato {turn["at"]}')
    if turn.get('outcome') is None:
        text += (', terminato senza che il suo esito sia stato scritto: diventa '
                 f'`{UNCERTAIN}` al prossimo tick, e da lì si riconcilia')
    return text


def blocked_text(issue, turns):
    """Why a new turn of the work of issue ``issue`` is refused, in Italian: its uncertain
    ``turns`` and how a trusted user reconciles them."""
    listed = ''.join(f'\n- {describe_turn(t)}' for t in turns)
    return (f'il lavoro dell\'issue #{issue} ha turni senza esito registrato (`{UNCERTAIN}`, o '
            'terminati senza che il loro esito sia stato scritto), e '
            'il blocco sugli esiti incerti (`block_uncertain_turns`) è attivo. Nessun nuovo turno '
            'parte, e nessuno viene ripetuto in automatico, finché un utente fidato non li '
            f'riconcilia:{listed}\n\nPer ognuno, un commento con prima riga '
            f'`{RECONCILE_COMMAND} <turno> <{"|".join(RECONCILED_OUTCOMES)}>` e, nelle righe '
            'successive, la nota obbligatoria con il motivo. Il turno resta addebitato.')


class TurnJournal:
    """The journal of ``state_dir``; without one, it lives as long as the process (tests).

    It is loaded and verified on first use; then it keeps its state in memory and appends to
    the file, one event at a time under a lock: parallel actions (#176) write it from several
    threads. One dispatcher runs at a time (its lock), so no other process writes it.

    It also knows, in memory, the turns of this process still running (``_live``): from their
    intent until their outcome is recorded or attempted. A turn without outcome that is not
    running ended without a durable one, or a previous process left it.

    ``read_only``: for a process without the dispatcher lock (``status``, ``tick --dry-run``).
    It reads and verifies the journal and changes no file: an incomplete last line is ignored,
    not set aside; the old trace is imported in memory, under ids of its own, and stays on
    disk; every other record stays in memory. No turn starts from it (``intent``).

    ``aliases`` (#540, ADR 0007): ``{former name: name}`` of a repository renamed, from
    ``repo_aliases``. A turn, a story or an attempt recorded under a former name reads as one
    of the repository's current name: the records written before the rename stay valid. The
    file is never rewritten."""

    def __init__(self, state_dir=None, clock=time.time, read_only=False, max_bytes=MAX_BYTES,
                 aliases=None):
        self.dir = Path(state_dir) if state_dir is not None else None
        self.aliases = dict(aliases or {})
        self.read_only = read_only
        self.max_bytes = max_bytes
        self._clock = clock
        self._lock = threading.RLock()
        self._loaded = False
        self._events = []
        self._live = set()  # the turns of this process whose outcome was not attempted yet
        # Events of the outbox (#385) not written when they happened: each is written before
        # any later event, of the outbox or of the turns (``publication``, ``defer``).
        self._deferred = []

    # Files -----------------------------------------------------------------
    @property
    def path(self):
        return self.dir / JOURNAL if self.dir is not None else None

    @property
    def snapshot(self):
        return self.dir / SNAPSHOT if self.dir is not None else None

    def _now(self):
        return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self._clock()))

    def repo_name(self, repo):
        """The current name of the repository ``repo`` (``aliases``): ``repo`` itself unless
        it is a former name."""
        return self.aliases.get(repo, repo)

    def size(self):
        """The size of the journal's file in bytes: 0 without one, or without ``state_dir``."""
        if self.dir is None:
            return 0
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise JournalError(f'{self.path}: size not readable: {exc}') from exc

    def warn_bytes(self):
        """The size from which devloop warns the operator (``WARN_RATIO`` of the limit), or None
        without a limit."""
        return int(self.max_bytes * WARN_RATIO) if self.max_bytes is not None else None

    def _over(self, size):
        return self.max_bytes is not None and size > self.max_bytes

    def _limit_text(self, size):
        return (f'{size} bytes, beyond the limit of {self.max_bytes} bytes (journal_max_mib): '
                'nothing is removed from it, and no turn starts until the limit is raised')

    def _read(self):
        """The events of the file, verified; an incomplete or unreadable last line is set
        aside and cut off. Raises ``JournalError`` on a broken chain."""
        try:
            data = self.path.read_bytes() if self.path.exists() else None
        except OSError as exc:
            raise JournalError(f'{self.path}: not readable: {exc}') from exc
        if data is None:
            return []
        lines = data.split(b'\n')
        complete = lines[:-1]  # the part after the last newline is never a whole record
        tail = lines[-1]
        if complete and not tail:
            try:
                json.loads(complete[-1])
            except ValueError:
                tail = complete.pop() + b'\n'
        if tail and not self.read_only:
            self._set_aside(data, tail)
        events, previous = [], ''
        for n, line in enumerate(complete, 1):
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError('not an object')
            except ValueError as exc:
                raise JournalError(f'{self.path}: line {n} is not a record ({exc}): the journal '
                                   'is corrupt and is not used') from exc
            body = {k: v for k, v in event.items() if k != 'sha256'}
            if (event.get('sha256') != digest(body) or event.get('sequence') != n
                    or event.get('previous') != previous):
                raise JournalError(f'{self.path}: the hash chain breaks at line {n}: the journal '
                                   'was altered and is not used')
            events.append(event)
            previous = event['sha256']
        return events

    def _set_aside(self, data, tail):
        """Keep the incomplete tail for audit, then cut it off the journal."""
        aside = self.dir / f'turn-journal-tail-{hashlib.sha256(tail).hexdigest()}.bin'
        try:
            aside.write_bytes(tail)
            with self.path.open('r+b') as stream:
                stream.truncate(len(data) - len(tail))
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise JournalError(f'{self.path}: incomplete last record not set aside: {exc}') from exc

    def _read_snapshot(self):
        """``(sequence, sha256)`` of the snapshot, or None without one."""
        if not self.snapshot.exists():
            return None
        try:
            head = json.loads(self.snapshot.read_text(encoding='utf-8'))
            return head['sequence'], head['sha256']
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise JournalError(f'{self.snapshot}: not readable: {exc}') from exc

    def _check_snapshot(self, events, head):
        """Refuse a snapshot without its journal, or a journal behind or off it."""
        if head is None:
            return
        sequence, sha = head
        if not events:
            raise JournalError(f'{self.snapshot} exists but the journal {self.path.name} is '
                               'missing or empty: the state is refused without its journal')
        if (not isinstance(sequence, int) or not 0 < sequence <= len(events)
                or events[sequence - 1]['sha256'] != sha):
            raise JournalError(f'{self.path}: does not reach its snapshot (event {sequence}): '
                               'the journal was cut or replaced and is not used')

    def _write_snapshot(self):
        last = self._events[-1]
        tmp = self.snapshot.with_name(f'.{SNAPSHOT}.{os.getpid()}.tmp')
        try:
            tmp.write_text(json.dumps({'sequence': last['sequence'], 'sha256': last['sha256']})
                           + '\n', encoding='utf-8')
            os.replace(tmp, self.snapshot)
        except OSError:
            pass  # the snapshot may lag behind the journal; it is written again next time

    def _load(self):
        if self._loaded:
            return
        if self.dir is not None:
            # Its size before anything is read (#397): a journal beyond the limit is not loaded.
            size = self.size()
            if self._over(size):
                raise JournalError(f'{self.path}: not loaded: {self._limit_text(size)}')
            # The snapshot first: the dispatcher writes it after the journal, so a process that
            # reads beside it (``read_only``) never finds the journal behind it.
            head = self._read_snapshot()
            events = self._read()
            self._check_snapshot(events, head)
            self._events = events
        self._migrate()
        self._loaded = True

    def _migrate(self):
        """Import ``ledger-open-turns.json`` once, then remove it."""
        if self.dir is None:
            return
        legacy = self.dir / LEGACY
        if not legacy.exists():
            return
        if not any(e['event'] == 'migrated' for e in self._events):
            try:
                data = json.loads(legacy.read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                raise JournalError(f'{legacy}: not readable: {exc}') from exc
            if not isinstance(data, dict):
                raise JournalError(f'{legacy}: not a JSON object')
            counts = {str(k): v for k, v in data.items()
                      if str(k).isdigit() and int(k) > 0
                      and isinstance(v, int) and not isinstance(v, bool) and v > 0}
            self._append('migrated', {'source': LEGACY, 'counts': counts})
        if self.read_only:
            return  # imported in memory alone: the dispatcher imports it
        try:
            legacy.unlink()
        except OSError:
            pass  # imported already: ignored from now on

    def _append(self, kind, data):
        """Append an event, after the deferred ones (``publication``): a deferred event that
        still cannot be written fails this one too, which is not written either."""
        self._flush()
        return self._write(kind, data)

    def _flush(self):
        while self._deferred:
            self._write(*self._deferred[0])
            self._deferred.pop(0)

    def _write(self, kind, data):
        last = self._events[-1] if self._events else None
        event = {'sequence': (last['sequence'] if last else 0) + 1,
                 'previous': last['sha256'] if last else '', 'event': kind,
                 'data': dict(data, at=self._now())}
        event['sha256'] = digest(event)
        if self.dir is not None and not self.read_only:
            line = (json.dumps(event, sort_keys=True, ensure_ascii=False) + '\n').encode('utf-8')
            size = None
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                created = not self.path.exists()
                size = 0 if created else self.path.stat().st_size
                after = size + len(line)
                if self._over(after):
                    # Refused whole, before a byte is written: nothing to cut back (#397).
                    size = None
                    raise JournalError(f'{self.path}: {kind} not written: the journal would be '
                                       f'{self._limit_text(after)}')
                with self.path.open('ab') as stream:
                    stream.write(line)
                    stream.flush()
                    os.fsync(stream.fileno())
                if created:
                    fd = os.open(self.dir, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            except OSError as exc:
                # A line written in part would break the chain for the next records: cut back.
                if size is not None:
                    try:
                        os.truncate(self.path, size)
                    except OSError:
                        pass  # left as an incomplete tail, set aside at the next read
                raise JournalError(f'{self.path}: {kind} not written: {exc}') from exc
        self._events.append(event)
        if self.dir is not None and not self.read_only:
            self._write_snapshot()
        return event

    # State -----------------------------------------------------------------
    def _state(self):
        """``(turns, trace)``: each turn by id, and the numbers whose ``turn`` record on GitHub
        was left open and not checked yet."""
        turns, trace = {}, {}
        for e in self._events:
            kind, d = e['event'], e['data']
            if kind == 'migrated':
                for number, count in d['counts'].items():
                    turn = f'legacy-{number}-{e["sha256"][:8]}'
                    turns[turn] = {
                        'turn': turn, 'number': int(number), 'issue': None,
                        'repo': None, 'legacy': count, 'outcome': UNCERTAIN, 'at': d['at'],
                        'reconciled': None}
                    trace[int(number)] = True
            elif kind == 'linked' and d['turn'] in turns:
                turns[d['turn']].update(issue=d['issue'], repo=self.repo_name(d['repo']))
            elif kind == 'intent':
                turns[d['turn']] = dict(d, outcome=None, reconciled=None)
                if 'repo' in d:
                    turns[d['turn']]['repo'] = self.repo_name(d['repo'])
            elif kind == 'outcome' and d['turn'] in turns:
                turns[d['turn']]['outcome'] = d['outcome']
                if 'usage' in d:  # what the CLI reported, with the budget on (#365)
                    turns[d['turn']]['usage'] = d['usage']
                if d['outcome'] == UNCERTAIN:
                    # Its records on GitHub are still to check: the number stays traced until a
                    # tick has checked them (``settle``).
                    trace[turns[d['turn']]['number']] = True
            elif kind == 'reconciled' and d['turn'] in turns:
                turns[d['turn']]['reconciled'] = d
            elif kind == 'unsettled':
                trace[d['number']] = True
            elif kind == 'settled':
                trace.pop(d['number'], None)
        return turns, trace

    def turns(self):
        """Every turn, by id: its intent with ``outcome`` (None while it has none) and
        ``reconciled`` (the reconciliation, or None)."""
        with self._lock:
            self._load()
            return self._state()[0]

    def handled(self, comment):
        """Whether the command of comment ``comment`` was applied or refused already."""
        with self._lock:
            self._load()
            return any(e['event'] in ('reconciled', 'refused') and e['data']['comment'] == comment
                       for e in self._events)

    def pending(self):
        """The issues and PRs where a turn may have been left open: a turn without outcome, or
        a ``turn`` record on GitHub whose closing edit failed, not checked since."""
        with self._lock:
            self._load()
            turns, trace = self._state()
            found = [t['number'] for t in turns.values() if t['outcome'] is None]
            return list(dict.fromkeys(found + list(trace)))

    def live(self):
        """The turns of this process still running (#365): booked, and not uncertain."""
        with self._lock:
            return set(self._live)

    def seconds(self):
        """The journal's clock, in seconds since the epoch (#365)."""
        return self._clock()

    def waiting(self):
        """The uncertain turns not reconciled yet, oldest first."""
        return [t for t in self.turns().values()
                if t['outcome'] == UNCERTAIN and t['reconciled'] is None]

    def blocking(self, repo, issue, number, issue_of=None):
        """The turns that block the work of issue ``issue`` of ``repo``, whose turn is about to
        start on ``number`` (the issue or one of its PRs): every uncertain turn not reconciled
        of that work, whatever its kind, agent or round, and every turn of it without outcome
        that is not running in this process (``_live``): it ended without a durable outcome
        (the write failed), or a previous process left it and the tick could not make it
        uncertain.

        A legacy turn has only its number: it belongs to the work of ``issue`` when it is on
        the issue or on ``number``, or on a PR of that issue as its ``linked`` event or
        ``issue_of`` (``{PR: issue}``, the dispatcher's map) say. One whose work neither says
        (``unlinked``) may be on a PR of any work: it blocks every new turn until it is
        linked."""
        issue_of = issue_of or {}
        with self._lock:
            self._load()
            turns = self._state()[0]
            live = set(self._live)
        found = []
        for t in turns.values():
            if t['outcome'] == UNCERTAIN:
                if t['reconciled'] is not None:
                    continue
            elif t['outcome'] is not None or t['turn'] in live:
                continue
            if t.get('legacy') is not None:
                work = t['issue'] if t['issue'] is not None else issue_of.get(t['number'])
                if t['number'] in (issue, number) or work in (issue, None):
                    found.append(t)
            elif t.get('repo') == repo and (t.get('issue') == issue or t['number'] == number):
                found.append(t)
        return found

    # Writes ----------------------------------------------------------------
    def intent(self, number, *, issue, repo, kind, agent, model, label, attempt=None,
               reserve=None):
        """Record a turn about to start on ``number``, of the work of issue ``issue``, durable
        before it returns; its id. ``attempt``: the id of the attempt the turn belongs to
        (#384), recorded only when there is one. Raises ``JournalError`` if it cannot: the turn
        must not start, nor from a ``read_only`` journal.

        ``reserve`` (#365, the budget on): called with the journal, under its lock, before the
        intent is written; it returns the booking of the turn in the envelope of its story,
        recorded in the intent as ``budget``, or raises and nothing is written. Check and
        booking are one step: two parallel turns of a story never both take its last place."""
        if self.read_only:
            raise JournalError(f'{self.path}: read-only journal: no turn starts from it')
        with self._lock:
            self._load()
            turn = f'turn-{len(self._events) + 1}-{secrets.token_hex(4)}'
            data = {'turn': turn, 'number': number, 'issue': issue, 'repo': repo, 'kind': kind,
                    'agent': agent, 'model': model, 'label': label}
            if attempt is not None:
                data['attempt'] = attempt
            if reserve is not None:
                data['budget'] = reserve(self)
            self._append('intent', data)
            self._live.add(turn)
            return turn

    def outcome(self, turn, outcome, error=None, usage=None):
        """Record how ``turn`` ended; one outcome per turn. Whether or not it can be written,
        the turn no longer runs (``_live``): without a durable outcome it blocks its work.
        ``usage`` (#365, the budget on): the dollars the CLI reported, ``metered_usd`` and
        ``cli_estimate_usd``, None where it reported none, recorded with the outcome."""
        if outcome not in OUTCOMES:
            raise ValueError(f'unknown outcome {outcome!r}')
        with self._lock:
            try:
                self._load()
                current = self._state()[0].get(turn)
                if current is None:
                    raise JournalError(f'turn {turn} not in the journal')
                if current['outcome'] is not None:
                    raise JournalError(f'turn {turn} already has outcome {current["outcome"]}')
                data = {'turn': turn, 'outcome': outcome}
                if error:
                    data['error'] = str(error)[:300]
                if usage is not None:
                    data['usage'] = dict(usage)
                self._append('outcome', data)
            finally:
                self._live.discard(turn)

    def unsettled(self, number):
        """A ``turn`` record on GitHub on ``number`` was left open: check it next tick."""
        with self._lock:
            self._load()
            self._append('unsettled', {'number': number})

    def uncertain(self, number):
        """Each turn on ``number`` without outcome becomes ``uncertain``: a tick found it so,
        with no action running there. Returns those turns. ``number`` stays in the trace
        (``pending``) until a tick has checked it (``settle``)."""
        with self._lock:
            self._load()
            flagged = [t for t in self._state()[0].values()
                       if t['number'] == number and t['outcome'] is None]
            for t in flagged:
                self._append('outcome', {'turn': t['turn'], 'outcome': UNCERTAIN})
            return flagged

    def unlinked(self):
        """The numbers of the legacy turns not reconciled whose work is not recorded yet."""
        with self._lock:
            self._load()
            return list(dict.fromkeys(
                t['number'] for t in self._state()[0].values()
                if t.get('legacy') is not None and t['reconciled'] is None
                and t['issue'] is None))

    def link(self, issue_of, repo):
        """Record the work of each legacy turn not reconciled whose number is in ``issue_of``
        (``{number: issue}``: the issue of a PR as the dispatcher recognises it from its branch
        and marker, or the number itself for an issue or a PR of no work), so that it blocks
        the whole work also after a restart, once the PR is closed. Returns the turns linked."""
        with self._lock:
            self._load()
            linked = []
            for t in self._state()[0].values():
                work = issue_of.get(t['number'])
                if (t.get('legacy') is None or t['reconciled'] is not None
                        or t['issue'] is not None or work is None):
                    continue
                self._append('linked', {'turn': t['turn'], 'number': t['number'],
                                        'issue': work, 'repo': repo})
                linked.append(t['turn'])
            return linked

    def settle(self, number):
        """Close the trace of ``number`` once a tick checked its records on GitHub."""
        with self._lock:
            self._load()
            if number in self._state()[1]:
                self._append('settled', {'number': number})

    def reconcile(self, turn, outcome, *, by, comment, note):
        """Apply a trusted user's reconciliation of the uncertain ``turn``: True when recorded,
        False when the same outcome was recorded already. Raises ``ValueError`` with the reason,
        in Italian, when it is refused: an unknown turn, one that is not uncertain, an outcome
        not admitted, no note, or a turn already reconciled with another outcome."""
        if outcome not in RECONCILED_OUTCOMES:
            raise ValueError(f'esito `{outcome}` non ammesso: usare uno fra '
                             + ', '.join(f'`{o}`' for o in RECONCILED_OUTCOMES))
        if not (note or '').strip():
            raise ValueError('manca la nota con il motivo, obbligatoria dopo la prima riga')
        with self._lock:
            self._load()
            current = self._state()[0].get(turn)
            if current is None:
                raise ValueError(f'il turno `{turn}` non è nel journal')
            if current['outcome'] != UNCERTAIN:
                raise ValueError(f'il turno `{turn}` non è incerto (esito: '
                                 f'`{current["outcome"] or "in corso"}`)')
            done = current['reconciled']
            if done is not None:
                if done['outcome'] == outcome:
                    return False
                raise ValueError(f'il turno `{turn}` è già stato riconciliato come '
                                 f'`{done["outcome"]}` da `{done["by"]}` (commento '
                                 f'{done["comment"]}): un esito diverso è rifiutato')
            self._append('reconciled', {'turn': turn, 'outcome': outcome, 'by': by,
                                        'comment': comment, 'note': note.strip()})
            return True

    def refuse(self, comment, by, turn, reason):
        """Record that the command of ``comment`` was refused, so that it is answered once."""
        with self._lock:
            self._load()
            self._append('refused', {'comment': comment, 'by': by, 'turn': turn,
                                     'reason': reason})

    # Stories (#363) --------------------------------------------------------
    def _stories(self):
        stories = {}
        for e in self._events:
            kind, d = e['event'], e['data']
            if kind == 'story':
                stories[d['story']] = {'story': d['story'], 'repo': self.repo_name(d['repo']),
                                       'issue': d['issue'], 'hashes': [d['hash']],
                                       'children': [], 'at': d['at']}
            elif kind == 'request' and d['story'] in stories:
                stories[d['story']]['hashes'].append(d['hash'])
            elif kind == 'child' and d['story'] in stories:
                stories[d['story']]['children'].append(d['issue'])
        return stories

    def stories(self):
        """Every story, by id: ``repo`` and ``issue`` of its root, ``hashes`` of the ``request``
        records of the root, oldest first, and ``children``, the numbers of its child issues."""
        with self._lock:
            self._load()
            return self._stories()

    @staticmethod
    def _find(stories, repo, issue):
        """``(story, role)`` of issue ``issue`` of ``repo``: ``root`` or ``child``; or None."""
        for s in stories.values():
            if s['repo'] == repo and s['issue'] == issue:
                return s, 'root'
            if s['repo'] == repo and issue in s['children']:
                return s, 'child'
        return None

    def story_of(self, repo, issue):
        """``(story, role)`` of issue ``issue`` of ``repo`` as the journal records it, or
        None: an issue no story holds."""
        with self._lock:
            self._load()
            return self._find(self._stories(), repo, issue)

    def story(self, repo, issue, request):
        """The story whose root is issue ``issue`` of ``repo``, taken over with the request
        hashed ``request`` (the ``hash`` of its ``request`` record): assigned now when there is
        none, with ``request`` as its first hash; otherwise the same story, with ``request``
        added after the others when it differs from the last one. Raises ``JournalError`` when
        the issue is a child of a story: it never becomes a root."""
        with self._lock:
            self._load()
            found = self._find(self._stories(), repo, issue)
            if found is None:
                story = f'story-{len(self._events) + 1}-{secrets.token_hex(4)}'
                self._append('story', {'story': story, 'repo': repo, 'issue': issue,
                                       'hash': request})
                return self._stories()[story]
            story, role = found
            if role != 'root':
                raise JournalError(f'{repo}#{issue} is a child of story {story["story"]}: it '
                                   'does not become the root of a story')
            if story['hashes'][-1] != request:
                self._append('request', {'story': story['story'], 'hash': request})
            return self._stories()[story['story']]

    def child(self, story, repo, issue):
        """Associate issue ``issue`` of ``repo`` with ``story`` as its child; the story. Raises
        ``JournalError`` for an unknown story, another repository, or an issue that is already
        the root or the child of a story (the same story's child is recorded once)."""
        with self._lock:
            self._load()
            stories = self._stories()
            current = stories.get(story)
            if current is None:
                raise JournalError(f'story {story} not in the journal')
            if current['repo'] != repo:
                raise JournalError(f'story {story} is of {current["repo"]}, not of {repo}')
            found = self._find(stories, repo, issue)
            if found is not None:
                if found == (current, 'child'):
                    return current
                raise JournalError(f'{repo}#{issue} is already the {found[1]} of story '
                                   f'{found[0]["story"]}')
            self._append('child', {'story': story, 'repo': repo, 'issue': issue})
            return self._stories()[story]

    # Publications (#385) -----------------------------------------------------
    def publication(self, kind, data, *, defer=False):
        """Append an event of the outbox of the publications (``outbox.Outbox``), durable
        before it returns: its kind starts with ``publication``, and the turns ignore it.
        Raises ``JournalError`` if it cannot be written, and from a ``read_only`` journal.

        With ``defer`` (an outcome the outbox already acted on: its caller is told it), an
        event that cannot be written is kept, and written before any later event of the
        journal: no later event, a turn's ``not_dispatched`` included, is ever durable without
        it. It raises ``JournalError`` all the same."""
        if not kind.startswith(PUBLICATION):
            raise ValueError(f'not an event of the outbox: {kind!r}')
        if self.read_only:
            raise JournalError(f'{self.path}: read-only journal: nothing is published from it')
        with self._lock:
            self._load()
            try:
                return self._append(kind, data)
            except JournalError:
                if defer:
                    self._deferred.append((kind, data))
                raise

    def flush(self):
        """Write the deferred events of the outbox; raises ``JournalError`` if one still
        cannot be written."""
        with self._lock:
            self._load()
            self._flush()

    def deferred(self):
        """The events of the outbox still to write, as ``(kind, data)``, oldest first."""
        with self._lock:
            return list(self._deferred)

    def publications(self):
        """The events of the outbox of the publications, oldest first."""
        with self._lock:
            self._load()
            return [e for e in self._events if e['event'].startswith(PUBLICATION)]

    # Size (#397) ------------------------------------------------------------
    def size_warned(self, threshold):
        """Whether the operator was warned already that the journal reached ``threshold``
        bytes (``warn_bytes``): a ``size_warning`` of that threshold."""
        with self._lock:
            self._load()
            return any(e['event'] == SIZE_WARNING and e['data'].get('threshold') == threshold
                       for e in self._events)

    def size_warning(self, size, threshold):
        """Record that the operator was warned that the journal, ``size`` bytes, reached
        ``threshold``: once per threshold. Raises ``JournalError`` if it cannot be written."""
        if self.read_only:
            raise JournalError(f'{self.path}: read-only journal: no warning is recorded in it')
        with self._lock:
            self._load()
            self._append(SIZE_WARNING, {'size': size, 'threshold': threshold,
                                        'limit': self.max_bytes})

    # Delegations (#405) ----------------------------------------------------
    def grants(self, day=None):
        """The grants of the supervisor's delegations recorded on the UTC ``day``
        (``YYYY-MM-DD``, today by default), one per command comment, oldest first."""
        with self._lock:
            self._load()
            return list(self._grants(day or self._now()[:10]).values())

    def _grants(self, day):
        """The grants of the UTC ``day`` by command comment, the first of each (under the
        lock)."""
        found = {}
        for e in self._events:
            if (e['event'] == GRANT and str(e['data'].get('at', ''))[:10] == day
                    and e['data'].get('delegation') != OPERATOR_ROUNDS):
                found.setdefault(e['data'].get('comment'), e['data'])
        return found

    def operator_grants(self):
        """The review rounds granted on an escalation to the operator (#553,
        ``operator_grant``), every day, one per command comment, oldest first: outside the
        daily cap of the delegations."""
        with self._lock:
            self._load()
            found = {}
            for e in self._events:
                if e['event'] == GRANT and e['data'].get('delegation') == OPERATOR_ROUNDS:
                    found.setdefault(e['data'].get('comment'), e['data'])
            return list(found.values())

    def operator_grant(self, number, comment, by):
        """Record a review round granted on PR ``number`` at a ``round_limit`` escalation to
        the operator (#553), by the command of comment ``comment`` of ``by``, before it
        applies: a ``grant`` of the delegation ``OPERATOR_ROUNDS``, which counts against no
        daily cap (``_grants`` skips it). The same comment is recorded once. Raises
        ``JournalError`` if it cannot be written, and then the round is not granted."""
        if self.read_only:
            raise JournalError(f'{self.path}: read-only journal: no grant is recorded in it')
        with self._lock:
            self._load()
            if not any(e['event'] == GRANT and e['data'].get('delegation') == OPERATOR_ROUNDS
                       and e['data'].get('comment') == comment for e in self._events):
                self._append(GRANT, {'delegation': OPERATOR_ROUNDS, 'number': number,
                                     'comment': comment, 'by': by})

    def grant(self, delegation, number, comment, by, cap):
        """Record a grant of the delegation ``delegation`` (``rounds``) on issue or PR
        ``number``, by the command of comment ``comment`` of ``by``, before it applies, within
        the daily cap ``cap``: it counts against the cap from now on.

        The cap is checked and the grant recorded in one step under the journal's lock, so that
        two commands applied together never pass it (#405). Returns the grants of the UTC day
        with this one, or None when the others of the day already reached ``cap``: nothing is
        recorded, and the grant does not apply. A grant of the same comment already recorded
        today is neither recorded nor counted twice. Raises ``JournalError`` if it cannot be
        written, and then the grant does not apply."""
        if self.read_only:
            raise JournalError(f'{self.path}: read-only journal: no grant is recorded in it')
        with self._lock:
            self._load()
            today = self._grants(self._now()[:10])
            others = sum(1 for c in today if c != comment)
            if others >= cap:
                return None
            if comment not in today:
                self._append(GRANT, {'delegation': delegation, 'number': number,
                                     'comment': comment, 'by': by})
            return others + 1

    def delegated_merges(self, day=None, event=DELEGATED_MERGE):
        """The merges delegated to the supervisor recorded on the UTC ``day`` (``YYYY-MM-DD``,
        today by default), one per command comment, oldest first (#486); with ``event``
        ``COORDINATOR_MERGE``, the reserved merges of the coordinator (#529)."""
        with self._lock:
            self._load()
            return list(self._delegated_merges(day or self._now()[:10], event).values())

    def _delegated_merges(self, day, event=DELEGATED_MERGE):
        found = {}
        for e in self._events:
            if e['event'] == event and str(e['data'].get('at', ''))[:10] == day:
                found.setdefault(e['data'].get('comment'), e['data'])
        return found

    def delegated_merge(self, number, comment, by, head, cap, event=DELEGATED_MERGE):
        """Record the merge of PR ``number`` at ``head``, delegated to the supervisor by the
        command of comment ``comment`` of ``by`` (#486), before it runs, within the daily cap
        ``cap`` of the delegated merges: a cap of its own, apart from the grants of ``grant``.
        With ``event`` ``COORDINATOR_MERGE``, a reserved merge of the coordinator (#529),
        within the cap of those, apart from the delegated merges.

        As ``grant``: the cap is checked and the merge recorded in one step under the
        journal's lock, so that two commands never pass it. Returns the merges of ``event`` of
        the UTC day with this one, or None when the others of the day already reached ``cap``:
        nothing is recorded, and the merge does not run. The same command recorded already
        today is neither recorded nor counted twice. Raises ``JournalError`` if it cannot be
        written, and then the merge does not run."""
        if event not in (DELEGATED_MERGE, COORDINATOR_MERGE):
            raise ValueError(f'not a merge event of the journal: {event!r}')
        if self.read_only:
            raise JournalError(f'{self.path}: read-only journal: no merge is recorded in it')
        with self._lock:
            self._load()
            today = self._delegated_merges(self._now()[:10], event)
            others = sum(1 for c in today if c != comment)
            if others >= cap:
                return None
            if comment not in today:
                self._append(event, {'number': number, 'comment': comment, 'by': by,
                                     'head': head})
            return others + 1

    def _lapsed(self, story):
        """The comments of the commands whose extension of ``story`` lapsed (#466)."""
        return {e['data'].get('comment') for e in self._events
                if e['event'] == LAPSED and e['data'].get('story') == story}

    def _extension_event(self, story):
        """The event of the extension of the budget envelope of ``story`` (under the lock): the
        first ``grant`` of the delegation ``budget`` for it, of any day, that did not lapse
        (#466), or None."""
        lapsed = self._lapsed(story)
        for e in self._events:
            d = e['data']
            if (e['event'] == GRANT and d.get('delegation') == BUDGET and d.get('story') == story
                    and d.get('comment') not in lapsed):
                return e
        return None

    def _extension(self, story):
        """The extension of the budget envelope of ``story`` (under the lock), or None."""
        found = self._extension_event(story)
        return None if found is None else found['data']

    def extension(self, story):
        """The extension of the budget envelope of story ``story`` (#411): the ``grant`` that
        recorded it, with its ``extension`` and ``at``, or None when the story has none or its
        extension lapsed (``lapse``, #466)."""
        with self._lock:
            self._load()
            return self._extension(story)

    def _used(self, found):
        """Whether a turn of the story of the extension event ``found`` was booked after it."""
        story = found['data'].get('story')
        return any(e['event'] == 'intent' and e['sequence'] > found['sequence']
                   and (e['data'].get('budget') or {}).get('story') == story
                   for e in self._events)

    def extension_used(self, story):
        """Whether the extension of the budget envelope of ``story`` is spent (#466): a turn of
        the story was booked after its grant. Until then the grant only books it, and a refusal
        of the first turn makes it lapse (``lapse``). False when the story has no extension."""
        with self._lock:
            self._load()
            found = self._extension_event(story)
            return found is not None and self._used(found)

    def lapse(self, story, comment, category):
        """Record that the extension of the budget envelope of ``story``, granted by the command
        of comment ``comment``, lapsed (#466): the first turn of the story after the grant was
        refused all the same (``category``), so no turn used it. From then on it does not extend
        the envelope, and the story keeps its one extension: another grant may give it, under
        the same conditions. The check and the record are one step under the journal's lock,
        as the booking of a turn: an extension a turn used never lapses. Returns whether it
        lapsed. Raises ``JournalError`` if it cannot be written."""
        if self.read_only:
            raise JournalError(f'{self.path}: read-only journal: no lapse is recorded in it')
        with self._lock:
            self._load()
            found = self._extension_event(story)
            if found is None or found['data'].get('comment') != comment or self._used(found):
                return False
            self._append(LAPSED, {'story': story, 'comment': comment,
                                  'number': found['data'].get('number'), 'category': category})
            return True

    def extend(self, story, extension, number, comment, by, cap, reason=None):
        """Record the extension ``extension`` of the budget envelope of story ``story`` (#411,
        ADR 0005, P5-7), granted on issue or PR ``number`` by the command of comment ``comment``
        of ``by``, before it applies: a ``grant`` of the delegation ``budget``, within the daily
        cap ``cap`` of the delegations, as ``grant``. ``reason``, the text of the command (the
        analysis of the causes), is recorded with it: a run stopped before the records of the
        grant completes them with the original command, whatever the thread holds by then.

        Once per story, ever: the story's extension, the cap and the record are one step under
        the journal's lock, so that two commands applied together never extend a story twice
        nor pass the cap. Returns ``(grants of the UTC day with this one, None)``, or ``(None,
        why)`` when nothing is recorded and the extension does not apply: ``'spent'`` when the
        story already has an extension of another command, ``'lapsed'`` when the extension of
        this same command lapsed already (#466), ``'daily'`` when the grants of the day already
        reached ``cap``. The same command recorded already is not recorded again.
        Raises ``JournalError`` if it cannot be written, and then the extension does not
        apply."""
        if self.read_only:
            raise JournalError(f'{self.path}: read-only journal: no grant is recorded in it')
        with self._lock:
            self._load()
            found = self._extension(story)
            if found is not None and found.get('comment') != comment:
                return None, 'spent'
            if comment in self._lapsed(story):
                return None, 'lapsed'
            today = self._grants(self._now()[:10])
            others = sum(1 for c in today if c != comment)
            if found is None and others >= cap:
                return None, 'daily'
            if found is None:
                data = {'delegation': BUDGET, 'number': number, 'comment': comment, 'by': by,
                        'story': story, 'extension': extension}
                if isinstance(reason, str):
                    data['reason'] = reason
                self._append(GRANT, data)
            return others + 1, None

    # Configuration (ADR 0006, R2, T1) ---------------------------------------
    def _configurations(self):
        return [e['data'] for e in self._events if e['event'] == CONFIG]

    def configurations(self):
        """The ``config`` events, oldest first."""
        with self._lock:
            self._load()
            return self._configurations()

    def trust(self):
        """The trust values as the ``config`` events recorded them, by dotted key."""
        with self._lock:
            self._load()
            return self._trust()

    def _trust(self):
        values = {}
        for data in self._configurations():
            for change in data.get('changes') or ():
                if change.get('new') is None:
                    values.pop(change.get('key'), None)
                else:
                    values[change.get('key')] = change['new']
        return values

    def configuration(self, when, fingerprint, only_if_changed=False):
        """Record the configuration of ``fingerprint`` (``config.fingerprint``, or the hashes of
        a configuration that does not load with ``trust`` None and its ``error``) read at
        ``when`` (``start`` or ``reload``): its hashes and the trust values that changed since
        the previous ``config`` event, before and after. With ``only_if_changed``, nothing is
        written when the hashes are those of the previous ``config`` event. Returns the changes
        recorded, or None when nothing was written. Raises ``JournalError`` if it cannot be
        written."""
        if self.read_only:
            raise JournalError(f'{self.path}: read-only journal: no configuration is recorded '
                               'in it')
        hashes = {key: fingerprint.get(key) for key in CONFIG_HASHES}
        with self._lock:
            self._load()
            previous = self._configurations()
            if (only_if_changed and previous
                    and all(previous[-1].get(k) == v for k, v in hashes.items())):
                return None
            changes = []
            trust = fingerprint.get('trust')
            if trust is not None:
                before = self._trust()
                changes = [{'key': key, 'old': before.get(key), 'new': trust.get(key)}
                           for key in sorted(set(before) | set(trust))
                           if before.get(key) != trust.get(key)]
            data = {'when': when, **hashes, 'changes': changes}
            if fingerprint.get('error'):
                data['error'] = fingerprint['error']
            self._append(CONFIG, data)
            return changes

    # Attempts (#384) -------------------------------------------------------
    def _attempts(self):
        attempts = {}
        for e in self._events:
            kind, d = e['event'], e['data']
            if kind == 'attempt':
                attempts[d['attempt']] = dict(d, pr=None, outcome=None, reason=None)
                if 'repo' in d:
                    attempts[d['attempt']]['repo'] = self.repo_name(d['repo'])
            elif kind == 'attempt_pr' and d['attempt'] in attempts:
                attempts[d['attempt']]['pr'] = d['pr']
            elif kind == 'concluded' and d['attempt'] in attempts:
                attempts[d['attempt']].update(outcome=d['outcome'], reason=d.get('reason'))
        return attempts

    def attempts(self):
        """Every attempt, by id, oldest first: ``story``, ``phase``, ``revision``, ``n``,
        ``repo`` and ``issue``, the key ``design`` and ``task``, ``pr`` (its PR, or None) and
        ``outcome`` (None while it is active) with its ``reason``."""
        with self._lock:
            self._load()
            return self._attempts()

    @staticmethod
    def _active(attempts, story, phase, revision):
        return next((a for a in attempts.values()
                     if (a['story'], a['phase'], a['revision']) == (story, phase, revision)
                     and a['outcome'] is None), None)

    def active_attempt(self, story, phase=IMPLEMENTATION, revision=None):
        """The active attempt of the phase ``(story, phase, revision)``, or None."""
        with self._lock:
            self._load()
            return self._active(self._attempts(), story, phase, revision)

    def active_attempt_of(self, repo, issue):
        """The latest active attempt that works on issue ``issue`` of ``repo``, or None."""
        with self._lock:
            self._load()
            found = [a for a in self._attempts().values()
                     if a['repo'] == repo and a['issue'] == issue and a['outcome'] is None]
            return found[-1] if found else None

    def open_attempt(self, story, issue, phase=IMPLEMENTATION, revision=None, *, design=None,
                     task=None):
        """Open the next attempt of the phase ``(story, phase, revision)`` on issue ``issue``,
        the root or a child of ``story``; the attempt. ``design`` and ``task``: the key of a
        unit generated by a plan, both null or both set. Raises ``AttemptActive`` while the phase
        has an active attempt: a new one is opened only after the previous one is concluded
        with a recorded outcome. Raises ``JournalError`` for an unknown story or phase, or an
        issue that does not belong to the story."""
        if phase not in PHASES:
            raise JournalError(f'unknown phase {phase!r}')
        if (design is None) != (task is None):
            raise JournalError('the key (design, task) is set in full or not at all')
        with self._lock:
            self._load()
            current = self._stories().get(story)
            if current is None:
                raise JournalError(f'story {story} not in the journal')
            if issue != current['issue'] and issue not in current['children']:
                raise JournalError(f'{current["repo"]}#{issue} does not belong to story {story}')
            attempts = self._attempts()
            active = self._active(attempts, story, phase, revision)
            if active is not None:
                where = f' (PR #{active["pr"]})' if active['pr'] is not None else ''
                raise AttemptActive(
                    f'phase {phase} of story {story} has the active attempt {active["attempt"]}'
                    f'{where}: a new attempt starts only after it is concluded with a recorded '
                    'outcome', active)
            n = 1 + sum(1 for a in attempts.values()
                        if (a['story'], a['phase'], a['revision']) == (story, phase, revision))
            attempt = f'attempt-{len(self._events) + 1}-{secrets.token_hex(4)}'
            self._append('attempt', {'attempt': attempt, 'story': story, 'phase': phase,
                                     'revision': revision, 'n': n, 'repo': current['repo'],
                                     'issue': issue, 'design': design, 'task': task})
            return self._attempts()[attempt]

    def bind_pr(self, attempt, pr):
        """Record ``pr`` as the PR of the active ``attempt``; the attempt. The same PR again
        adds nothing. Raises ``JournalError`` for an unknown or concluded attempt, or one that
        has another PR: one PR per attempt."""
        with self._lock:
            self._load()
            current = self._attempts().get(attempt)
            if current is None:
                raise JournalError(f'attempt {attempt} not in the journal')
            if current['pr'] == pr:
                return current
            if current['outcome'] is not None:
                raise JournalError(f'attempt {attempt} is concluded ({current["outcome"]})')
            if current['pr'] is not None:
                raise JournalError(f'attempt {attempt} has PR #{current["pr"]}, not #{pr}')
            self._append('attempt_pr', {'attempt': attempt, 'pr': pr})
            return self._attempts()[attempt]

    def conclude(self, attempt, outcome, reason=None):
        """Close ``attempt`` with ``outcome`` (``merged``, ``closed`` or ``abandoned``): True when
        recorded, False when it has that outcome already. Raises ``JournalError`` for an unknown
        attempt, an unknown outcome, or one concluded with another outcome."""
        if outcome not in ATTEMPT_OUTCOMES:
            raise JournalError(f'unknown outcome of an attempt {outcome!r}')
        with self._lock:
            self._load()
            current = self._attempts().get(attempt)
            if current is None:
                raise JournalError(f'attempt {attempt} not in the journal')
            if current['outcome'] is not None:
                if current['outcome'] == outcome:
                    return False
                raise JournalError(f'attempt {attempt} is concluded already '
                                   f'({current["outcome"]})')
            data = {'attempt': attempt, 'outcome': outcome}
            if reason:
                data['reason'] = str(reason)[:300]
            self._append('concluded', data)
            return True

    def abandon(self, attempt, reason):
        """Abandon ``attempt`` with the ``reason``, mandatory: an internal primitive, which no
        command and no flow calls yet (#384)."""
        if not (reason or '').strip():
            raise JournalError('an attempt is abandoned only with a reason')
        return self.conclude(attempt, ABANDONED, reason.strip())
