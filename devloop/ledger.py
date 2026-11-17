"""Evidence and telemetry the dispatcher records for itself (#133).

Each record is a dispatcher comment that ends with a ``ledger`` marker, signed with the
dispatcher's identity like its other records, on the issue (implementation, readiness check) or
on the PR (review, fix, sync, CI):

- ``request``: a snapshot of the issue when the dispatcher takes it over (``agent:ready`` to
  ``agent:working``): hash of title and body, author and time of the trusted ``labeled`` event,
  ids of the trusted comments given to the implementer. It is taken at the takeover, not at the
  time of the label;
- ``turn``: one invocation of an agent CLI, retries and failures included. It is written before
  the CLI starts (if that write fails the CLI does not start) and edited when it ends; a turn
  never closed stays ``open`` until the next tick marks it ``incomplete``. Where turns may be
  left open is also kept in ``state_dir`` (``PENDING``), whatever labels the issue or PR has
  later, so that the next tick finds them;
- ``verification``: a test command the dispatcher ran, bound to the commit created right after
  only when the tree before the test, after it and in the commit is the same;
- ``event``: what happens without a CLI, such as a CI round or a sync without conflicts.

A value the CLI or GitHub did not attest is null, never 0, and reads ``non riportato``. Nothing
is reconstructed from what agents wrote: data missing for work started before these records is
missing.
"""

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .agents import TELEMETRY_FIELDS
from .github import GitHubError
from .markdown import dispatcher_header, publishable, read_marker
from .ports import Tracker

NOT_REPORTED = 'non riportato'
PENDING = 'ledger-open-turns.json'  # in state_dir: issues and PRs with turns not closed
TURN_KINDS = ('implement', 'fix', 'sync', 'review', 'preflight')
_COMMENT_URL = re.compile(r'#issuecomment-(\d+)$')


def timestamp(clock=time.time):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(clock()))


def request_hash(title, body):
    """The hash of an issue's title and body, as the request snapshot records it."""
    data = json.dumps([title or '', body or ''], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def comment_ref(comment):
    """A comment's id: the numeric id of its URL (``gh issue view``), else its ``id``, else
    None."""
    match = _COMMENT_URL.search(comment.get('url') or '')
    if match:
        return int(match.group(1))
    return comment.get('id')


@dataclass
class Entry:
    """A record written on issue or PR ``number`` as comment ``comment_id``."""
    number: int
    comment_id: object
    record: dict
    started: float = field(default=None, repr=False)  # monotonic time a turn started


class LedgerError(GitHubError):
    """The local trace of the turns left open cannot be read or written."""


class Ledger:
    """Writes ledger records with ``gh``, the dispatcher's ``ports.Tracker`` (``comment``
    returns the new comment's id, ``edit_comment`` replaces a body).

    ``state_dir`` keeps, in ``PENDING``, the issues and PRs where a turn was opened and not
    closed yet, with how many: a number is counted before the turn's record is written and
    uncounted when the turn is closed; a tick that has marked its turns incomplete forgets it.
    The trace does not depend on labels, which change once a step ends (``agent:working``
    removed, ``agent:needs-human`` added). Without ``state_dir`` it lasts as long as the
    process."""

    def __init__(self, gh: Tracker, clock=time.time, monotonic=time.monotonic, state_dir=None):
        self.gh = gh
        self._clock = clock
        self._monotonic = monotonic
        self._path = Path(state_dir) / PENDING if state_dir is not None else None
        self._memory = {}
        # Parallel actions (#176) update the trace from several threads.
        self._lock = threading.Lock()

    # Turns left open -------------------------------------------------------
    def _load(self):
        if self._path is None:
            return dict(self._memory)
        try:
            data = json.loads(self._path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise LedgerError(f'{self._path}: not readable: {exc}') from exc
        if not isinstance(data, dict):
            raise LedgerError(f'{self._path}: not a JSON object')
        return {str(k): v for k, v in data.items()
                if isinstance(v, int) and not isinstance(v, bool) and v > 0}

    def _store(self, data):
        if self._path is None:
            self._memory = dict(data)
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(f'.{self._path.name}.{os.getpid()}.tmp')
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')
            os.replace(tmp, self._path)
        except OSError as exc:
            raise LedgerError(f'{self._path}: not written: {exc}') from exc

    def _count(self, number, delta):
        with self._lock:
            data = self._load()
            left = data.get(str(number), 0) + delta
            if left > 0:
                data[str(number)] = left
            else:
                data.pop(str(number), None)
            self._store(data)

    def pending(self):
        """The issues and PRs where a turn may have been left open."""
        return [int(k) for k in self._load()]

    def settle(self, number):
        """Forget ``number``: a tick found no turn left open there, or marked them incomplete."""
        with self._lock:
            data = self._load()
            if data.pop(str(number), None) is not None:
                self._store(data)

    def _now(self):
        return timestamp(self._clock)

    def _post(self, number, record):
        return Entry(number, self.gh.comment(number, render(record)), record)

    def _edit(self, entry, **changes):
        record = dict(entry.record, **changes)
        if entry.comment_id is None:
            raise GitHubError(f'ledger record on #{entry.number}: comment id unknown, not edited')
        self.gh.edit_comment(entry.comment_id, render(record))
        entry.record = record

    # Writes ----------------------------------------------------------------
    def request(self, number, *, title, body, labeled, comments):
        """The snapshot of the request at the takeover. ``labeled``: ``(login, at)`` of the
        trusted ``labeled`` event of the ready label, or None; ``comments``: the trusted
        comments given to the implementer."""
        by, at = labeled or (None, None)
        return self._post(number, {
            'type': 'request', 'issue': number, 'hash': request_hash(title, body),
            'labeled_by': by, 'labeled_at': at, 'comments': [comment_ref(c) for c in comments],
            'taken_at': self._now()})

    def open_turn(self, number, *, kind, agent, identity, mode, model, effort, fallback_for,
                  label):
        """Write the record of a turn about to start; raises ``GitHubError`` if it cannot, and
        then the turn must not start. ``number`` is traced first (``PENDING``), so that a crash
        right after the record is written still leaves the trace; if the trace cannot be
        written, ``LedgerError`` (a ``GitHubError``) and the turn does not start either."""
        self._count(number, +1)
        entry = self._post(number, {
            'type': 'turn', 'state': 'open', 'kind': kind, 'agent': agent, 'identity': identity,
            'mode': mode, 'model': model, 'effort': effort, 'fallback_for': fallback_for or None,
            'model_real': None, 'label': label, 'started_at': self._now(), 'ended_at': None,
            'duration_ms': None, 'outcome': None, 'error': None, 'telemetry': None})
        entry.started = self._monotonic()
        return entry

    def close_turn(self, entry, outcome, report=None, error=None, last_command=None):
        """Close a turn: ``outcome`` is ``success``, ``failure`` or ``unavailable``; ``report``
        the CLI's ``(fields, models)`` (``agents.telemetry``), None when the CLI did not
        complete; ``last_command`` the last command its output exposes, already cleaned
        (``agents.public_command``), None when it exposes none (#215)."""
        fields, models = report or (dict.fromkeys(TELEMETRY_FIELDS), None)
        duration = (int((self._monotonic() - entry.started) * 1000)
                    if entry.started is not None else None)
        changes = {'last_command': last_command} if last_command else {}
        self._edit(entry, state='closed', ended_at=self._now(), duration_ms=duration,
                   outcome=outcome, error=(str(error)[:300] if error else None),
                   telemetry={k: fields.get(k) for k in TELEMETRY_FIELDS},
                   model_real=list(models) if models else None, **changes)
        # Only a closed turn leaves the trace: a failed edit above keeps it for the next tick.
        self._count(entry.number, -1)

    def verification(self, number, *, kind, attempt, command, passed, duration_ms, tree_before,
                     tree_after):
        return self._post(number, {
            'type': 'verification', 'kind': kind, 'attempt': attempt, 'command': command,
            'outcome': 'pass' if passed else 'fail', 'duration_ms': duration_ms,
            'tree_before': tree_before, 'tree_after': tree_after, 'commit': None,
            'at': self._now()})

    def bind_commit(self, entry, commit, commit_tree):
        """Bind a passing verification to ``commit`` if its tree is the one tested, before and
        after the test; otherwise the commit stays null. Returns whether it was bound."""
        record = entry.record
        if not (record['outcome'] == 'pass' and record['tree_before']
                and record['tree_before'] == record['tree_after'] == commit_tree):
            return False
        self._edit(entry, commit=commit)
        return True

    def event(self, number, event, **fields):
        return self._post(number, dict(fields, type='event', event=event, at=self._now()))

    def flag_incomplete(self, number, comment_id, record):
        """Mark a turn that was never closed: end, outcome and telemetry stay null."""
        self._edit(Entry(number, comment_id, record), state='incomplete',
                   flagged_at=self._now())


# Reads -------------------------------------------------------------------
def entries(comments, signed):
    """``(comment id, record)`` of the ledger records in ``comments`` whose author ``signed``
    accepts (the dispatcher's identity)."""
    found = []
    for comment in comments:
        record = read_marker(comment.get('body', ''), 'ledger')
        if record is not None and signed((comment.get('author') or {}).get('login')):
            found.append((comment.get('id'), record))
    return found


def open_turns(comments, signed):
    """The turns in ``comments`` that were opened and never closed nor marked incomplete."""
    return [(cid, r) for cid, r in entries(comments, signed)
            if r['type'] == 'turn' and r['state'] == 'open']


def collect(comments, signed):
    """The records of one issue or PR, by type: ``request`` is the latest snapshot or None when
    none was recorded (work started before #133, for example). Missing data stays missing."""
    found = [r for _, r in entries(comments, signed)]
    requests = [r for r in found if r['type'] == 'request']
    return {'request': requests[-1] if requests else None,
            'turns': [r for r in found if r['type'] == 'turn'],
            'verifications': [r for r in found if r['type'] == 'verification'],
            'events': [r for r in found if r['type'] == 'event']}


# Rendering ---------------------------------------------------------------
def _value(value):
    if value is None:
        return NOT_REPORTED
    if isinstance(value, list):
        return ', '.join(f'`{v}`' for v in value) if value else NOT_REPORTED
    return f'`{value}`'


def _rows(pairs):
    return '\n'.join(f'- **{name}:** {_value(value)}' for name, value in pairs)


_TURN_NOTE = {
    'open': ('Turno in corso: il record è scritto prima di avviare la CLI. Fine, esito e '
             'telemetria si aggiungono alla chiusura.'),
    'closed': '',
    'incomplete': ('**Turno incompleto:** nessuna chiusura registrata (crash del dispatcher o '
                   'scrittura finale fallita). Fine, esito e telemetria restano non riportati.'),
}


def _render_turn(r):
    telemetry = r.get('telemetry') or {}
    parts = [f'## Registro devloop: turno `{r["kind"]}` di `{r["agent"]}`', '']
    if _TURN_NOTE[r['state']]:
        parts += [_TURN_NOTE[r['state']], '']
    identity = r.get('identity') or 'login `gh` dell\'operatore'
    parts.append(f'- **Identità GitHub:** {identity}')
    rows = [('Modello configurato', r.get('model')), ('Effort configurato', r.get('effort'))]
    if r.get('fallback_for'):
        rows.append(('Modello di fallback per', r['fallback_for']))
    rows += [('Modello reale (attestato dalla CLI)', r.get('model_real')),
             ('Inizio', r.get('started_at')), ('Fine', r.get('ended_at')),
             ('Durata (ms)', r.get('duration_ms')), ('Esito', r.get('outcome'))]
    parts.append(_rows(rows))
    if r.get('last_command'):
        command = ' '.join(str(r['last_command']).split())
        fence = '`' * (max((len(run) for run in re.findall('`+', command)), default=0) + 1)
        parts.append(f'- **Ultimo comando (esposto dalla CLI):** {fence} {command} {fence}')
    if r.get('error'):
        parts.append(f'- **Errore:** {" ".join(str(r["error"]).split())}')
    parts += ['', '**Telemetria** (dichiarata dalla CLI, non autenticata):', '',
              _rows([(k, telemetry.get(k)) for k in TELEMETRY_FIELDS])]
    return parts


def _render_request(r):
    return ['## Registro devloop: snapshot della richiesta', '',
            'Snapshot **alla presa in carico** dell\'issue (`agent:ready` → `agent:working`), '
            'non all\'istante della label.', '',
            _rows([('Hash di titolo e corpo (SHA-256)', r.get('hash')),
                   ('Label messa da', r.get('labeled_by')), ('Label messa il', r.get('labeled_at')),
                   ('Commenti fidati considerati', r.get('comments')),
                   ('Presa in carico', r.get('taken_at'))])]


def _render_verification(r):
    return ['## Registro devloop: verifica locale', '',
            'Comando eseguito dal dispatcher. Il commit è indicato solo se l\'albero testato, '
            'prima e dopo il test, coincide con quello committato.', '',
            _rows([('Passo', r.get('kind')), ('Tentativo', r.get('attempt')),
                   ('Comando', r.get('command')), ('Esito', r.get('outcome')),
                   ('Durata (ms)', r.get('duration_ms')), ('Albero prima', r.get('tree_before')),
                   ('Albero dopo', r.get('tree_after')), ('Commit', r.get('commit')),
                   ('Eseguita il', r.get('at'))])]


def _render_event(r):
    parts = [f'## Registro devloop: evento `{r["event"]}`', '']
    shown = [(k, v) for k, v in r.items() if k not in ('type', 'event', 'checks')]
    parts.append(_rows(shown))
    if isinstance(r.get('checks'), list):
        parts += ['', '| Check | Esito | Run di Actions |', '|---|---|---|']
        for c in r['checks']:
            name = str(c.get('name')).replace('|', '\\|')
            parts.append(f'| {name} | {_value(c.get("outcome"))} | {_value(c.get("run_id"))} |')
    return parts


_RENDER = {'turn': _render_turn, 'request': _render_request,
           'verification': _render_verification, 'event': _render_event}


def render(record):
    """The comment of a ledger record: the dispatcher's header, a readable summary and the
    marker."""
    return publishable(dispatcher_header() + '\n' + '\n'.join(_RENDER[record['type']](record)),
                       'ledger', record)
