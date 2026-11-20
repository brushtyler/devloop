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
  never closed stays ``open`` until the next tick marks it ``incomplete``. Its intent and
  outcome are first in the durable journal of ``state_dir`` (``journal.TurnJournal``, #362),
  the authority of which this record is the projection: the journal also says where turns may
  be left open, whatever labels the issue or PR has later, so that the next tick finds them;
- ``verification``: a test command the dispatcher ran, bound to the commit created right after
  only when the tree before the test, after it and in the commit is the same;
- ``event``: what happens without a CLI, such as a CI round or a sync without conflicts.

The turns of a review round and its ``ci_round`` event share one comment, the round's register
(#325): a ``round`` record whose ``records`` hold each turn and the event in a section of its
own, with the same content and the same persistence as a record of their own (a turn's section
is written ``open`` before its CLI starts and completed when it ends). Every write of a register
re-reads it from GitHub and runs under a lock of the ledger, so that reviewers running in
parallel lose none of each other's updates. Reads expand a register into its records, each
with its ``section`` and, in ``register``, the round: everything else reads them as before.

A fix has a register of its own too (#355), a ``fix`` record with the same rules: its turns, the
``sync`` event of the merge of the base made before it, and the ``verification`` of its local
tests, each in a section. It is written with the first turn, ``open``, before the CLI starts.
Its sections read with ``register`` set to ``FIX``.

A value the CLI or GitHub did not attest is null, never 0, and reads ``non riportato``. Nothing
is reconstructed from what agents wrote: data missing for work started before these records is
missing.
"""

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field

from .agents import TELEMETRY_FIELDS
from .budget import observed
from .github import GitHubError
from .journal import (FAILED, LEGACY, MAX_BYTES, NOT_DISPATCHED, RETURNED, JournalError,
                      TurnJournal)
from .markdown import dispatcher_header, publishable, read_marker
from .ports import Tracker

NOT_REPORTED = 'non riportato'
# The trace of the turns left open before #362, in state_dir: imported once by the journal.
PENDING = LEGACY
# The journal's outcome of a turn closed as ``close_turn`` says.
JOURNAL_OUTCOME = {'success': RETURNED, 'failure': FAILED, 'unavailable': FAILED}
TURN_KINDS = ('implement', 'fix', 'sync', 'review', 'preflight', 'conclave', 'advisory')
REGISTERS = ('round', 'fix')  # the records whose ``records`` hold sections (#325, #355)
FIX = 'fix'  # the ``register`` of the sections of a fix register, as reads expand them
_COMMENT_URL = re.compile(r'#issuecomment-(\d+)$')


def timestamp(clock=time.time):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(clock()))


def request_hash(title, body):
    """The hash of an issue's title and body, as the request snapshot records it."""
    data = json.dumps([title or '', body or ''], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def story_marker(story, number):
    """The ``story`` marker of issue ``number`` in ``story`` (``TurnJournal.stories``): the
    root's carries the hashes of its ``request`` records, a child's none (#363)."""
    mark = {'story': story['story'], 'repo': story['repo'], 'root': story['issue'],
            'issue': number}
    if number == story['issue']:
        mark['hashes'] = list(story['hashes'])
    return mark


def attempt_marker(attempt):
    """The ``attempt`` of a PR marker (#384) for ``attempt`` (``TurnJournal.attempts``): its id,
    the phase ``(story, phase, revision)``, its number in the phase and the key ``(design,
    task)``."""
    return {k: attempt[k] for k in ('attempt', 'story', 'phase', 'revision', 'n', 'design',
                                    'task')}


# The branch of an attempt after the first (#384, ADR 0004 §3.2, recommendation (i)): the
# branch of today followed by its number in the phase.
_ATTEMPT_SUFFIX = re.compile(r'([0-9]+)(?:-attempt-([1-9][0-9]*))?')


def attempt_branch(prefix, issue, n):
    """The branch of attempt number ``n`` of issue ``issue`` (#384): one branch per attempt,
    with its identity in the name. The first keeps the branch of today, ``<prefix><issue>``;
    the next ones are ``<prefix><issue>-attempt-<n>``, so that a new attempt never overwrites
    the branch of the previous one."""
    return f'{prefix}{issue}' if n == 1 else f'{prefix}{issue}-attempt-{n}'


def parse_branch(prefix, ref):
    """``(issue, n)`` of a devloop branch ``ref`` (``attempt_branch``), ``n`` None for the
    branch of today; None for any other branch."""
    suffix = ref[len(prefix):] if ref.startswith(prefix) else ''
    found = _ATTEMPT_SUFFIX.fullmatch(suffix) if suffix.isascii() else None
    if found is None or (found[2] is not None and int(found[2]) < 2):
        return None
    return int(found[1]), int(found[2]) if found[2] is not None else None


def comment_ref(comment):
    """A comment's id: the numeric id of its URL (``gh issue view``), else its ``id``, else
    None."""
    match = _COMMENT_URL.search(comment.get('url') or '')
    if match:
        return int(match.group(1))
    return comment.get('id')


@dataclass
class Entry:
    """A record written on issue or PR ``number`` as comment ``comment_id``; in a register, as
    its section ``section``: ``register`` is the round of a round's register (#325), ``FIX`` for
    a fix register (#355)."""
    number: int
    comment_id: object
    record: dict
    started: float = field(default=None, repr=False)  # monotonic time a turn started
    section: int = None
    register: object = None
    turn: str = field(default=None, repr=False)  # a turn's id in the journal (#362)
    # With the budget on (#365): the outcome of the turn in the journal records its ``usage``.
    measure: bool = field(default=False, repr=False)


@dataclass
class FixRegister:
    """The register of one fix on PR ``number``, answering round ``round`` (0 for a fix asked
    before any review, #195): no comment until its first section is written, then
    ``comment_id`` (#355)."""
    number: int
    round: int
    comment_id: object = None


def fix_register(where):
    """The ``FixRegister`` that ends ``where`` (``(number, kind, …)`` of a step), or None."""
    last = where[-1] if where else None
    return last if isinstance(last, FixRegister) else None


def _register_name(register):
    return 'fix register' if register == FIX else f'register of round {register}'


# The journal of the turns cannot be read or written (#362); before it, the local trace.
LedgerError = JournalError


class Ledger:
    """Writes ledger records with ``gh``, the dispatcher's ``ports.Tracker`` (``comment``
    returns the new comment's id, ``edit_comment`` replaces a body).

    ``state_dir`` holds the journal of the turns (``journal.TurnJournal``, #362): each turn's
    intent, made durable before its record is written, and its outcome, recorded before its
    record is closed. The journal also says where a turn may be left open: a turn without
    outcome, or a record whose closing edit failed, until a tick checks that issue or PR
    (``settle``). It does not depend on labels, which change once a step ends
    (``agent:working`` removed, ``agent:needs-operator`` added). Without ``state_dir`` it
    lasts as long as the process.

    ``signed`` tells whether a login signs as the dispatcher: only a register it signed is
    edited. The dispatcher sets it to its own check; without it every author counts."""

    def __init__(self, gh: Tracker, clock=time.time, monotonic=time.monotonic, state_dir=None,
                 signed=None, read_only=False, journal_max_bytes=MAX_BYTES):
        self.gh = gh
        self._clock = clock
        self._monotonic = monotonic
        self.journal = TurnJournal(state_dir, clock=clock, read_only=read_only,
                                   max_bytes=journal_max_bytes)
        self.signed = signed
        # Reviewers in parallel write the same register (#325): one write at a time, each on
        # the register as GitHub has it right before.
        self._registers = threading.Lock()

    # Turns left open -------------------------------------------------------
    def pending(self):
        """The issues and PRs where a turn may have been left open."""
        return self.journal.pending()

    def settle(self, number):
        """Forget ``number``: a tick found no turn left open there, or marked them incomplete."""
        self.journal.settle(number)

    def _now(self):
        return timestamp(self._clock)

    def _post(self, number, record):
        return Entry(number, self.gh.comment(number, render(record)), record)

    def _edit(self, entry, **changes):
        record = dict(entry.record, **changes)
        if entry.section is not None:
            self._edit_section(entry, changes)
            entry.record = record
            return
        if entry.comment_id is None:
            raise GitHubError(f'ledger record on #{entry.number}: comment id unknown, not edited')
        self.gh.edit_comment(entry.comment_id, render(record))
        entry.record = record

    # Round registers (#325) ------------------------------------------------
    def _register(self, number, round_no, comment_id=None):
        """``(comment, record)`` of a register on ``number`` as GitHub has it now, the
        dispatcher's only: the comment ``comment_id`` when known (a round's register or a fix
        register), otherwise the latest register of round ``round_no``. None when there is
        none."""
        found = None
        for comment in self.gh.issue_comments(number):
            record = read_marker(comment.get('body', ''), 'ledger')
            if record is None or record['type'] not in REGISTERS:
                continue
            if self.signed is not None and not self.signed(
                    (comment.get('author') or {}).get('login')):
                continue
            if comment_id is not None:
                if comment.get('id') == comment_id:
                    return comment, record
            elif record['type'] == 'round' and record['round'] == round_no:
                found = (comment, record)
        return found

    def _add_section(self, number, round_no, section):
        """Add ``section`` to a register, writing it when there is none yet: a turn or an event
        to the register of round ``round_no``, or, when ``round_no`` is a ``FixRegister``
        (#355), a turn, an event or a verification to that fix's register. Returns
        ``(comment id, section number)``."""
        fix = round_no if isinstance(round_no, FixRegister) else None
        name = _register_name(FIX if fix else round_no)
        with self._registers:
            if fix is None:
                found = self._register(number, round_no)
            elif fix.comment_id is None:
                found = None
            else:
                found = self._register(number, None, fix.comment_id)
                if found is None:
                    raise GitHubError(f'{name} on #{number} not found, not edited')
            if found is None:
                record = ({'type': 'fix', 'round': fix.round} if fix
                          else {'type': 'round', 'round': round_no})
                record['records'] = [dict(section, section=1)]
                comment_id = self.gh.comment(number, render(record))
                if fix:
                    fix.comment_id = comment_id
                return comment_id, 1
            comment, record = found
            if comment.get('id') is None:
                raise GitHubError(f'{name} on #{number}: comment id unknown, not edited')
            n = max((s['section'] for s in record['records']), default=0) + 1
            record = dict(record, records=record['records'] + [dict(section, section=n)])
            self.gh.edit_comment(comment['id'], render(record))
            return comment['id'], n

    def _edit_section(self, entry, changes):
        """Apply ``changes`` to the section of ``entry`` in its register, re-read first."""
        with self._registers:
            name = _register_name(entry.register)
            found = self._register(entry.number, entry.register, entry.comment_id)
            if found is None:
                raise GitHubError(f'{name} on #{entry.number} not found, not edited')
            comment, record = found
            sections = record['records']
            if not any(s['section'] == entry.section for s in sections):
                raise GitHubError(f'section {entry.section} of the {name} on #{entry.number} not '
                                  'found, not edited')
            if comment.get('id') is None:
                raise GitHubError(f'{name} on #{entry.number}: comment id unknown, not edited')
            record = dict(record, records=[dict(s, **changes) if s['section'] == entry.section
                                           else s for s in sections])
            self.gh.edit_comment(comment['id'], render(record))

    # Writes ----------------------------------------------------------------
    def request(self, number, *, title, body, labeled, comments, story_repo=None,
                implementer=None):
        """The snapshot of the request at the takeover. ``labeled``: ``(login, at)`` of the
        trusted ``labeled`` event of the ready label, or None; ``comments``: the trusted
        comments given to the implementer; ``implementer``: the key of the implementer chosen
        for the issue at the takeover (ADR 0005, D7, #407), recorded when given.

        With ``story_repo`` (#363, ``story_identity``) issue ``number`` of that repository is
        the root of a story: the journal records it first (``TurnJournal.story``: the story
        assigned now, or the same one with the hash of this record added when it changed), and
        the comment of the record carries its ``story`` marker. If the journal cannot record
        it, ``JournalError`` and no record is written."""
        by, at = labeled or (None, None)
        record = {
            'type': 'request', 'issue': number, 'hash': request_hash(title, body),
            'labeled_by': by, 'labeled_at': at, 'comments': [comment_ref(c) for c in comments],
            'taken_at': self._now()}
        if implementer is not None:
            record['implementer'] = implementer
        if story_repo is None:
            return self._post(number, record)
        story = story_marker(self.journal.story(story_repo, number, record['hash']), number)
        return Entry(number, self.gh.comment(number, render(record, story=story)), record)

    # Stories (#363) --------------------------------------------------------
    def story_of(self, number, comments, repo):
        """The story issue ``number`` of ``repo`` belongs to, as the journal records it, read
        from the ``story`` marker in ``comments`` (its comments): only a marker the dispatcher
        signed counts (``signed``; without it, none does), the latest one. Never the text of the
        issue, of an agent or of a user: a story declared there is not inherited. None without
        such a marker: the issue is handled as before (#363).

        The journal is the authority (ADR 0004, D7): a marker it does not confirm, for another
        issue, a story it does not hold, or an issue that is neither its root nor one of its
        children, raises ``JournalError``. So does the marker of a root whose ``hashes`` are not
        a prefix of the story's in the journal: a marker behind the journal (a write that failed
        after the journal recorded the hash) is valid, one that diverges from it or goes beyond
        it (a journal restored from before) contradicts it."""
        if self.signed is None:
            return None
        found = [read_marker(c.get('body', ''), 'story') for c in comments
                 if self.signed((c.get('author') or {}).get('login'))]
        found = [m for m in found if m is not None]
        if not found:
            return None
        mark = found[-1]
        story = self.journal.stories().get(mark['story'])
        role = self.journal.story_of(repo, number)
        if (mark['issue'] != number or mark['repo'] != repo or story is None
                or story['issue'] != mark['root'] or role is None
                or role[0]['story'] != story['story']):
            raise JournalError(f'#{number}: the dispatcher\'s story marker ({mark["story"]}, '
                               f'root #{mark["root"]}) is not confirmed by the journal: the '
                               'journal is the authority, and the story is not used')
        hashes = mark.get('hashes')
        if hashes is not None and story['hashes'][:len(hashes)] != hashes:
            raise JournalError(f'#{number}: the hashes of the dispatcher\'s story marker '
                               f'({mark["story"]}) are not a prefix of those the journal '
                               'records: the journal is the authority, and the story is not '
                               'used')
        return story

    def mark_child(self, number, story, repo):
        """Associate issue ``number`` of ``repo`` with ``story`` as its child (#363): the journal
        records it first (``TurnJournal.child``), then the dispatcher writes the ``story``
        marker on the issue, from which alone it inherits the story. Returns the comment's id.

        No flow calls it yet: which act authorizes the association of a child is decided with
        step 3 of ADR 0001, and until then no issue gets the marker of a child."""
        current = self.journal.child(story, repo, number)
        mark = story_marker(current, number)
        text = '\n'.join(_render_story(mark))
        return self.gh.comment(number, publishable(dispatcher_header() + '\n' + text,
                                                   'story', mark))

    def fix_register(self, number, round_no):
        """The register of a fix of PR ``number`` answering round ``round_no`` (#355), to pass
        as ``register`` to the writes of that fix: written with its first section."""
        return FixRegister(number, round_no)

    def _section(self, number, record, round_no, register):
        """``record`` written as a section of a register: of round ``round_no``, or of the fix
        register ``register``."""
        where = register if register is not None else round_no
        comment_id, section = self._add_section(number, where, record)
        return Entry(number, comment_id, dict(record, section=section), section=section,
                     register=FIX if register is not None else round_no)

    def open_turn(self, number, *, kind, agent, identity, mode, model, effort, fallback_for,
                  label, round_no=None, register=None, issue=None, repo=None, attempt=None,
                  reserve=None, measure=False, role=None):
        """Write the record of a turn about to start; raises ``GitHubError`` if it cannot, and
        then the turn must not start. Its intent is made durable in the journal first, with
        ``issue``, the issue of its work, and ``repo`` (#362), so that a crash right after
        still leaves it; if the intent cannot be written, ``JournalError`` (a ``GitHubError``)
        and the turn does not start either. If the record cannot be written, the journal says
        the turn was ``not_dispatched``. With ``round_no`` (a review turn, #325) the record is
        a section of that round's register; with ``register`` (a fix turn, #355), of that fix
        register. ``attempt``: the id of the attempt the turn belongs to (#384), in the intent
        and in the record; without one, neither has the field, as before.

        With the budget on (#365): ``reserve`` books the turn in the envelope of its story, in
        its intent (``TurnJournal.intent``), or raises ``BudgetRefused`` and nothing is written;
        ``measure``: its outcome records the dollars the CLI reported (``usage``).

        ``role``: ``findings`` for the turn of a findings-only review (ADR 0005, D8, #416), in
        the record; without one the record has no such field, as before."""
        turn = self.journal.intent(number, issue=number if issue is None else issue, repo=repo,
                                   kind=kind, agent=agent, model=model, label=label,
                                   attempt=attempt, reserve=reserve)
        record = {
            'type': 'turn', 'state': 'open', 'kind': kind, 'agent': agent, 'identity': identity,
            'mode': mode, 'model': model, 'effort': effort, 'fallback_for': fallback_for or None,
            'model_real': None, 'label': label, 'started_at': self._now(), 'ended_at': None,
            'duration_ms': None, 'outcome': None, 'error': None, 'telemetry': None}
        if attempt is not None:
            record['attempt'] = attempt
        if role:
            record['role'] = role
        try:
            if round_no is None and register is None:
                entry = self._post(number, record)
            else:
                entry = self._section(number, record, round_no, register)
        except GitHubError as exc:
            try:
                self.journal.outcome(turn, NOT_DISPATCHED, exc)
            except JournalError:
                pass  # left without outcome: the next tick makes it uncertain
            raise
        entry.started = self._monotonic()
        entry.turn = turn
        entry.measure = measure
        return entry

    def close_turn(self, entry, outcome, report=None, error=None, last_command=None):
        """Close a turn: ``outcome`` is ``success``, ``failure`` or ``unavailable``; ``report``
        the CLI's ``(fields, models)`` (``agents.telemetry``), None when the CLI did not
        complete; ``last_command`` the last command its output exposes, already cleaned
        (``agents.public_command``), None when it exposes none (#215).

        The journal records the outcome first (``returned`` for ``success``, ``failed``
        otherwise); if it cannot, the record stays open and the turn becomes ``uncertain`` at
        the next tick. If the edit of the record fails, the journal keeps the issue or PR for
        the next tick to check (``Ledger.pending``)."""
        fields, models = report or (dict.fromkeys(TELEMETRY_FIELDS), None)
        duration = (int((self._monotonic() - entry.started) * 1000)
                    if entry.started is not None else None)
        changes = {'last_command': last_command} if last_command else {}
        if entry.turn is not None:
            self.journal.outcome(entry.turn, JOURNAL_OUTCOME[outcome], error,
                                 usage=observed(fields) if entry.measure else None)
        try:
            self._edit(entry, state='closed', ended_at=self._now(), duration_ms=duration,
                       outcome=outcome, error=(str(error)[:300] if error else None),
                       telemetry={k: fields.get(k) for k in TELEMETRY_FIELDS},
                       model_real=list(models) if models else None, **changes)
        except GitHubError:
            if entry.turn is not None:
                self.journal.unsettled(entry.number)
            raise

    def verification(self, number, *, kind, attempt, command, passed, duration_ms, tree_before,
                     tree_after, register=None):
        """A test the dispatcher ran; with ``register``, a section of that fix register."""
        record = {
            'type': 'verification', 'kind': kind, 'attempt': attempt, 'command': command,
            'outcome': 'pass' if passed else 'fail', 'duration_ms': duration_ms,
            'tree_before': tree_before, 'tree_after': tree_after, 'commit': None,
            'at': self._now()}
        if register is None:
            return self._post(number, record)
        return self._section(number, record, None, register)

    def bind_commit(self, entry, commit, commit_tree):
        """Bind a passing verification to ``commit`` if its tree is the one tested, before and
        after the test; otherwise the commit stays null. Returns whether it was bound."""
        record = entry.record
        if not (record['outcome'] == 'pass' and record['tree_before']
                and record['tree_before'] == record['tree_after'] == commit_tree):
            return False
        self._edit(entry, commit=commit)
        return True

    def event(self, number, event, round_no=None, register=None, **fields):
        """An event; with ``round_no`` (the ``ci_round`` of a review round, #325) a section of
        that round's register, with ``register`` (the ``sync`` before a fix, #355) of that fix
        register."""
        record = dict(fields, type='event', event=event, at=self._now())
        if round_no is None and register is None:
            return self._post(number, record)
        return self._section(number, record, round_no, register)

    def flag_incomplete(self, number, comment_id, record):
        """Mark a turn that was never closed: end, outcome and telemetry stay null. ``record``
        as ``entries`` reads it: a section of a register is marked in its register."""
        self._edit(Entry(number, comment_id, record, section=record.get('section'),
                         register=record.get('register')),
                   state='incomplete', flagged_at=self._now())


# Reads -------------------------------------------------------------------
def entries(comments, signed):
    """``(comment id, record)`` of the ledger records in ``comments`` whose author ``signed``
    accepts (the dispatcher's identity). A round's register (#325) gives each of its records,
    in order, with its ``section`` and, in ``register``, the round; a fix register (#355) the
    same, with ``register`` set to ``FIX``. Records of their own, as written before, are read
    as they are."""
    found = []
    for comment in comments:
        record = read_marker(comment.get('body', ''), 'ledger')
        if record is None or not signed((comment.get('author') or {}).get('login')):
            continue
        if record['type'] in REGISTERS:
            register = record['round'] if record['type'] == 'round' else FIX
            found += [(comment.get('id'), dict(s, register=register))
                      for s in record['records']]
        else:
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


def _render_turn(r, title=None):
    telemetry = r.get('telemetry') or {}
    parts = [title or f'## Registro devloop: turno `{r["kind"]}` di `{r["agent"]}`', '']
    if _TURN_NOTE[r['state']]:
        parts += [_TURN_NOTE[r['state']], '']
    identity = r.get('identity') or 'login `gh` dell\'operatore'
    parts.append(f'- **Identità GitHub:** {identity}')
    rows = [('Modello configurato', r.get('model')), ('Effort configurato', r.get('effort'))]
    if r.get('fallback_for'):
        rows.append(('Modello di fallback per', r['fallback_for']))
    if r.get('attempt'):
        rows.append(('Tentativo', r['attempt']))
    if r.get('role') == 'findings':
        # #416: its verdict does not count for the quorum.
        rows.append(('Ruolo', 'finding-only'))
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
                   *([('Implementer scelto', r['implementer'])] if 'implementer' in r else []),
                   ('Presa in carico', r.get('taken_at'))])]


def _render_verification(r, title=None):
    return [title or '## Registro devloop: verifica locale', '',
            'Comando eseguito dal dispatcher. Il commit è indicato solo se l\'albero testato, '
            'prima e dopo il test, coincide con quello committato.', '',
            _rows([('Passo', r.get('kind')), ('Tentativo', r.get('attempt')),
                   ('Comando', r.get('command')), ('Esito', r.get('outcome')),
                   ('Durata (ms)', r.get('duration_ms')), ('Albero prima', r.get('tree_before')),
                   ('Albero dopo', r.get('tree_after')), ('Commit', r.get('commit')),
                   ('Eseguita il', r.get('at'))])]


def _render_event(r, title=None):
    parts = [title or f'## Registro devloop: evento `{r["event"]}`', '']
    shown = [(k, v) for k, v in r.items() if k not in ('type', 'event', 'checks', 'section')]
    parts.append(_rows(shown))
    if isinstance(r.get('checks'), list):
        parts += ['', '| Check | Esito | Run di Actions |', '|---|---|---|']
        for c in r['checks']:
            name = str(c.get('name')).replace('|', '\\|')
            parts.append(f'| {name} | {_value(c.get("outcome"))} | {_value(c.get("run_id"))} |')
    return parts


def _render_round(r):
    parts = [f'## Registro devloop: round {r["round"]}', '',
             'Un solo registro per round: ogni turno di review e l\'evento della CI del round '
             'hanno la loro sezione. La sezione di un turno è scritta prima di avviare la CLI e '
             'completata quando finisce.']
    for s in r['records']:
        parts.append('')
        parts += _render_section(s)
    return parts


def _render_section(s):
    if s['type'] == 'turn':
        return _render_turn(s, f'### {s["section"]}. Turno `{s["kind"]}` di `{s["agent"]}`')
    if s['type'] == 'verification':
        return _render_verification(s, f'### {s["section"]}. Verifica locale')
    return _render_event(s, f'### {s["section"]}. Evento `{s["event"]}`')


def _render_fix(r):
    which = f'del round {r["round"]}' if r['round'] else 'prima di ogni review'
    parts = [f'## Registro devloop: correzione {which}', '',
             'Un solo registro per correzione: ogni turno, il merge della base fatto prima della '
             'correzione e ogni verifica locale hanno la loro sezione. La sezione di un turno è '
             'scritta prima di avviare la CLI e completata quando finisce.']
    for s in r['records']:
        parts.append('')
        parts += _render_section(s)
    return parts


def _render_story(s, title='## Registro devloop: storia'):
    """The readable part of a ``story`` marker (#363)."""
    if s['issue'] == s['root']:
        note = (f'Questa issue è la radice della storia `{s["story"]}`, assegnata dal '
                'dispatcher alla presa in carico. Gli hash sono quelli dei record `request` della '
                'radice, dal primo.')
        rows = [('Storia', s['story']), ('Repository', s['repo']),
                ('Hash delle richieste (SHA-256)', s['hashes'])]
    else:
        note = (f'Questa issue è figlia della storia `{s["story"]}` della radice '
                f'#{s["root"]}: la eredita solo da questo marker del dispatcher.')
        rows = [('Storia', s['story']), ('Repository', s['repo']), ('Radice', s['root'])]
    return [title, '', note, '', _rows(rows)]


_RENDER = {'turn': _render_turn, 'request': _render_request,
           'verification': _render_verification, 'event': _render_event,
           'round': _render_round, 'fix': _render_fix}


def render(record, story=None):
    """The comment of a ledger record: the dispatcher's header, a readable summary and the
    marker; with ``story``, the ``story`` marker of the root of a ``request`` record (#363)
    after it."""
    parts = _RENDER[record['type']](record)
    if story is None:
        return publishable(dispatcher_header() + '\n' + '\n'.join(parts), 'ledger', record)
    parts += [''] + _render_story(story, '### Storia')
    return publishable(dispatcher_header() + '\n' + '\n'.join(parts), 'ledger', record,
                       records=[('story', story)])
