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
  be on a PR of any work, and with the block it blocks every new turn.

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

Reads verify the whole chain. An incomplete or unreadable last line (a write cut short) is set
aside in ``turn-journal-tail-<sha256>.bin`` and removed, and the valid records before it stay; a
hash that does not match, or an unreadable line before the last, is an explicit error. Next to
the journal a snapshot (``turn-journal-state.json``) keeps the sequence and hash of its latest
event: a snapshot without its journal, or a journal shorter than it or diverging from it, is
refused (ADR 0001, constraint 3). The chain is not keyed: it gives integrity, not provenance.

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
# A turn's id: its sequence (or, for a legacy one, its number) and 8 hex digits that differ
# between journals, so that a command written for the turns of a journal since lost never
# reconciles a turn of a new one.
_TURN_ID = re.compile(r'(turn|legacy)-[1-9][0-9]*-[0-9a-f]{8}')
# A story's id (#363), made the same way.
STORY_ID = re.compile(r'story-[1-9][0-9]*-[0-9a-f]{8}')


class JournalError(GitHubError):
    """The journal cannot be read, verified or written. A ``GitHubError``, so that a step whose
    intent cannot be recorded stops and escalates like a failed write of its ledger record."""


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
    disk; every other record stays in memory. No turn starts from it (``intent``)."""

    def __init__(self, state_dir=None, clock=time.time, read_only=False):
        self.dir = Path(state_dir) if state_dir is not None else None
        self.read_only = read_only
        self._clock = clock
        self._lock = threading.RLock()
        self._loaded = False
        self._events = []
        self._live = set()  # the turns of this process whose outcome was not attempted yet

    # Files -----------------------------------------------------------------
    @property
    def path(self):
        return self.dir / JOURNAL if self.dir is not None else None

    @property
    def snapshot(self):
        return self.dir / SNAPSHOT if self.dir is not None else None

    def _now(self):
        return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self._clock()))

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
                turns[d['turn']].update(issue=d['issue'], repo=d['repo'])
            elif kind == 'intent':
                turns[d['turn']] = dict(d, outcome=None, reconciled=None)
            elif kind == 'outcome' and d['turn'] in turns:
                turns[d['turn']]['outcome'] = d['outcome']
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
    def intent(self, number, *, issue, repo, kind, agent, model, label):
        """Record a turn about to start on ``number``, of the work of issue ``issue``, durable
        before it returns; its id. Raises ``JournalError`` if it cannot: the turn must not
        start, nor from a ``read_only`` journal."""
        if self.read_only:
            raise JournalError(f'{self.path}: read-only journal: no turn starts from it')
        with self._lock:
            self._load()
            turn = f'turn-{len(self._events) + 1}-{secrets.token_hex(4)}'
            self._append('intent', {'turn': turn, 'number': number, 'issue': issue,
                                    'repo': repo, 'kind': kind, 'agent': agent, 'model': model,
                                    'label': label})
            self._live.add(turn)
            return turn

    def outcome(self, turn, outcome, error=None):
        """Record how ``turn`` ended; one outcome per turn. Whether or not it can be written,
        the turn no longer runs (``_live``): without a durable outcome it blocks its work."""
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
                stories[d['story']] = {'story': d['story'], 'repo': d['repo'],
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
