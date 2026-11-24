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
  only when the tree before the test, after it and in the commit is the same. The retry alone
  of failed tests that would escalate ``host_tests`` or wait as ``base_broken`` (#598) names
  them in ``retried``;
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

A value the CLI or GitHub did not attest is null, never 0, and reads ``not reported``. Nothing
is reconstructed from what agents wrote: data missing for work started before these records is
missing.

The readable part of a record follows the project's ``language`` (#631, ``i18n``): Italian or
English, English for any other language. Its marker does not, so reads never depend on it.
"""

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field

from .agents import TELEMETRY_FIELDS
from .budget import observed
from .generated import capped
from .github import GitHubError
from .i18n import ENGLISH, Catalog
from .journal import (FAILED, LEGACY, MAX_BYTES, NOT_DISPATCHED, RETURNED, JournalError,
                      TurnJournal)
from .markdown import dispatcher_header, publishable, read_marker
from .ports import Tracker

# The trace of the turns left open before #362, in state_dir: imported once by the journal.
PENDING = LEGACY
# The journal's outcome of a turn closed as ``close_turn`` says.
# A turn ``cancelled`` (#521) ended without a result, stopped by devloop: ``failed`` for the
# journal, which records whether the CLI returned, never ``uncertain``.
JOURNAL_OUTCOME = {'success': RETURNED, 'failure': FAILED, 'unavailable': FAILED,
                   'cancelled': FAILED}
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
    edited. The dispatcher sets it to its own check; without it every author counts.

    ``language``: the project's (``Config.language``, #631), the language of the readable part
    of the records it writes (``render``); the dispatcher sets it to its configuration's."""

    def __init__(self, gh: Tracker, clock=time.time, monotonic=time.monotonic, state_dir=None,
                 signed=None, read_only=False, journal_max_bytes=MAX_BYTES, repo_aliases=None,
                 language=ENGLISH):
        self.gh = gh
        self.language = language
        self._clock = clock
        self._monotonic = monotonic
        # ``repo_aliases``: ``{former name: name}`` of a renamed repository (#540): the records
        # of the journal and the markers written under a former name stay the repository's.
        self.journal = TurnJournal(state_dir, clock=clock, read_only=read_only,
                                   max_bytes=journal_max_bytes, aliases=repo_aliases)
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

    def _render(self, record, story=None):
        return render(record, story=story, language=self.language)

    def _post(self, number, record):
        return Entry(number, self.gh.comment(number, self._render(record)), record)

    def _edit(self, entry, **changes):
        record = dict(entry.record, **changes)
        if entry.section is not None:
            self._edit_section(entry, changes)
            entry.record = record
            return
        if entry.comment_id is None:
            raise GitHubError(f'ledger record on #{entry.number}: comment id unknown, not edited')
        self.gh.edit_comment(entry.comment_id, self._render(record))
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
                comment_id = self.gh.comment(number, self._render(record))
                if fix:
                    fix.comment_id = comment_id
                return comment_id, 1
            comment, record = found
            if comment.get('id') is None:
                raise GitHubError(f'{name} on #{number}: comment id unknown, not edited')
            n = max((s['section'] for s in record['records']), default=0) + 1
            record = dict(record, records=record['records'] + [dict(section, section=n)])
            self.gh.edit_comment(comment['id'], self._render(record))
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
            self.gh.edit_comment(comment['id'], self._render(record))

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
        return Entry(number, self.gh.comment(number, self._render(record, story=story)), record)

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
        # A marker written under a former name of the repository (#540) is the repository's.
        if (mark['issue'] != number or self.journal.repo_name(mark['repo']) != repo
                or story is None
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
        text = '\n'.join(_render_story(TEXTS.texts(self.language), mark))
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

    def close_turn(self, entry, outcome, report=None, error=None, last_command=None,
                   denied_command=None, denied_permission=None):
        """Close a turn: ``outcome`` is ``success``, ``failure``, ``unavailable`` or
        ``cancelled`` (stopped because its result was no longer needed, #521); ``report``
        the CLI's ``(fields, models)`` (``agents.telemetry``), None when the CLI did not
        complete; ``last_command`` the last command its output exposes, already cleaned
        (``agents.public_command``), None when it exposes none (#215); ``denied_command`` the
        command headless agy denied, already cleaned (``agents.denied_command``), None when agy
        does not report it (#479); ``denied_permission`` the permission headless agy denied, for
        a turn stopped on one, None otherwise: the daily summary counts those turns per model
        from it, whether agy reports the command or not (#479).

        The journal records the outcome first (``returned`` for ``success``, ``failed``
        otherwise); if it cannot, the record stays open and the turn becomes ``uncertain`` at
        the next tick. If the edit of the record fails, the journal keeps the issue or PR for
        the next tick to check (``Ledger.pending``)."""
        fields, models = report or (dict.fromkeys(TELEMETRY_FIELDS), None)
        duration = (int((self._monotonic() - entry.started) * 1000)
                    if entry.started is not None else None)
        changes = {'last_command': last_command} if last_command else {}
        if denied_command:
            changes['denied_command'] = denied_command
        if denied_permission:
            changes['denied_permission'] = denied_permission
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
                     tree_after, register=None, retried=None, changed_by_tests=None):
        """A test the dispatcher ran; with ``register``, a section of that fix register.
        ``retried``: the failed tests of the attempt that the command ran again alone (#598),
        unstable when it passed. ``changed_by_tests``: the files whose content the command
        changed, from ``tree_before`` to ``tree_after`` (#627), recorded when there are any:
        those ``generated.capped`` keeps, and how many it left out in
        ``changed_by_tests_omitted``."""
        record = {
            'type': 'verification', 'kind': kind, 'attempt': attempt, 'command': command,
            'outcome': 'pass' if passed else 'fail', 'duration_ms': duration_ms,
            'tree_before': tree_before, 'tree_after': tree_after, 'commit': None,
            'at': self._now()}
        if retried is not None:
            record['retried'] = list(retried)
        if changed_by_tests:
            # At most what ``capped`` keeps, the others counted: a test that creates many caches
            # must not take the comment beyond the limit of a GitHub text.
            kept = capped(changed_by_tests)
            record['changed_by_tests'] = kept
            if len(kept) < len(changed_by_tests):
                record['changed_by_tests_omitted'] = len(changed_by_tests) - len(kept)
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
# The readable part of the records, in the project's ``language`` (#631, ``i18n``); English
# for a language without translation. The markers, and so every read, do not depend on it.
TEXTS = Catalog(
    en={
        'title': '## devloop ledger',
        'not_reported': 'not reported',
        'turn': 'turn `{kind}` of `{agent}`',
        'turn_open': ('Turn in progress: the record is written before the CLI starts. End, '
                      'outcome and telemetry are added when it closes.'),
        'turn_incomplete': ('**Incomplete turn:** no closing recorded (crash of the dispatcher '
                            'or failed final write). End, outcome and telemetry stay not '
                            'reported.'),
        'operator_login': 'the operator\'s `gh` login',
        'identity': 'GitHub identity',
        'model': 'Configured model',
        'effort': 'Configured effort',
        'fallback_for': 'Fallback model for',
        'attempt': 'Attempt',
        'role': 'Role',
        'model_real': 'Real model (attested by the CLI)',
        'started_at': 'Start',
        'ended_at': 'End',
        'duration_ms': 'Duration (ms)',
        'outcome': 'Outcome',
        'last_command': 'Last command (exposed by the CLI)',
        'denied_command': 'Denied command (reported by agy)',
        'error': 'Error',
        'telemetry': '**Telemetry** (declared by the CLI, not authenticated):',
        'request': 'request snapshot',
        'request_note': ('Snapshot **at the takeover** of the issue (`agent:ready` → '
                         '`agent:working`), not at the time of the label.'),
        'hash': 'Hash of title and body (SHA-256)',
        'labeled_by': 'Label added by',
        'labeled_at': 'Label added at',
        'comments': 'Trusted comments considered',
        'implementer': 'Implementer chosen',
        'taken_at': 'Taken over at',
        'verification': 'local verification',
        'verification_section': 'Local verification',
        'verification_note': ('Command run by the dispatcher. The commit is given only if the '
                              'tree tested, before and after the test, is the one committed.'),
        'kind': 'Step',
        'command': 'Command',
        'tree_before': 'Tree before',
        'tree_after': 'Tree after',
        'commit': 'Commit',
        'at': 'Run at',
        'unstable': ('**Unstable tests**: failed in the attempt and on the base commit, they '
                     'pass when run again alone. The step goes on; the test must be fixed:'),
        'still_failing': '**Tests run again alone**, they still fail:',
        'changed_by_tests': ('**Files changed by the test**: their content after the test is '
                             'not the one before, and ends up in the commit that follows:'),
        'changed_by_tests_omitted': '… and {count} more',
        'event': 'event `{event}`',
        'event_section': 'Event `{event}`',
        'checks': '| Check | Outcome | Actions run |',
        'round': 'round {round}',
        'round_note': ('One register per round: each review turn and the CI event of the round '
                       'have their own section. The section of a turn is written before the '
                       'CLI starts and completed when it ends.'),
        'section_turn': 'Turn `{kind}` of `{agent}`',
        'fix_of_round': 'fix of round {round}',
        'fix_first': 'fix before any review',
        'fix_note': ('One register per fix: each turn, the merge of the base made before the '
                     'fix and each local verification have their own section. The section of a '
                     'turn is written before the CLI starts and completed when it ends.'),
        'story': 'story',
        'story_section': '### Story',
        'story_root': ('This issue is the root of story `{story}`, assigned by the dispatcher '
                       'at the takeover. The hashes are those of the `request` records of the '
                       'root, from the first.'),
        'story_child': ('This issue is a child of story `{story}` of root #{root}: it inherits '
                        'it only from this marker of the dispatcher.'),
        'story_id': 'Story',
        'repo': 'Repository',
        'hashes': 'Hashes of the requests (SHA-256)',
        'root': 'Root',
    },
    it={
        'title': '## Registro devloop',
        'not_reported': 'non riportato',
        'turn': 'turno `{kind}` di `{agent}`',
        'turn_open': ('Turno in corso: il record è scritto prima di avviare la CLI. Fine, esito '
                      'e telemetria si aggiungono alla chiusura.'),
        'turn_incomplete': ('**Turno incompleto:** nessuna chiusura registrata (crash del '
                            'dispatcher o scrittura finale fallita). Fine, esito e telemetria '
                            'restano non riportati.'),
        'operator_login': 'login `gh` dell\'operatore',
        'identity': 'Identità GitHub',
        'model': 'Modello configurato',
        'effort': 'Effort configurato',
        'fallback_for': 'Modello di fallback per',
        'attempt': 'Tentativo',
        'role': 'Ruolo',
        'model_real': 'Modello reale (attestato dalla CLI)',
        'started_at': 'Inizio',
        'ended_at': 'Fine',
        'duration_ms': 'Durata (ms)',
        'outcome': 'Esito',
        'last_command': 'Ultimo comando (esposto dalla CLI)',
        'denied_command': 'Comando negato (riportato da agy)',
        'error': 'Errore',
        'telemetry': '**Telemetria** (dichiarata dalla CLI, non autenticata):',
        'request': 'snapshot della richiesta',
        'request_note': ('Snapshot **alla presa in carico** dell\'issue (`agent:ready` → '
                         '`agent:working`), non all\'istante della label.'),
        'hash': 'Hash di titolo e corpo (SHA-256)',
        'labeled_by': 'Label messa da',
        'labeled_at': 'Label messa il',
        'comments': 'Commenti fidati considerati',
        'implementer': 'Implementer scelto',
        'taken_at': 'Presa in carico',
        'verification': 'verifica locale',
        'verification_section': 'Verifica locale',
        'verification_note': ('Comando eseguito dal dispatcher. Il commit è indicato solo se '
                              'l\'albero testato, prima e dopo il test, coincide con quello '
                              'committato.'),
        'kind': 'Passo',
        'command': 'Comando',
        'tree_before': 'Albero prima',
        'tree_after': 'Albero dopo',
        'commit': 'Commit',
        'at': 'Eseguita il',
        'unstable': ('**Test instabili**: falliti nel tentativo e sul commit base, passano '
                     'rilanciati da soli. Il passo prosegue; il test va corretto:'),
        'still_failing': '**Test rilanciati da soli**, falliscono ancora:',
        'changed_by_tests': ('**File modificati dal test**: il loro contenuto dopo il test non è '
                             'quello di prima, e finisce nel commit che segue:'),
        'changed_by_tests_omitted': '… e altri {count}',
        'event': 'evento `{event}`',
        'event_section': 'Evento `{event}`',
        'checks': '| Check | Esito | Run di Actions |',
        'round': 'round {round}',
        'round_note': ('Un solo registro per round: ogni turno di review e l\'evento della CI '
                       'del round hanno la loro sezione. La sezione di un turno è scritta prima '
                       'di avviare la CLI e completata quando finisce.'),
        'section_turn': 'Turno `{kind}` di `{agent}`',
        'fix_of_round': 'correzione del round {round}',
        'fix_first': 'correzione prima di ogni review',
        'fix_note': ('Un solo registro per correzione: ogni turno, il merge della base fatto '
                     'prima della correzione e ogni verifica locale hanno la loro sezione. La '
                     'sezione di un turno è scritta prima di avviare la CLI e completata quando '
                     'finisce.'),
        'story': 'storia',
        'story_section': '### Storia',
        'story_root': ('Questa issue è la radice della storia `{story}`, assegnata dal '
                       'dispatcher alla presa in carico. Gli hash sono quelli dei record '
                       '`request` della radice, dal primo.'),
        'story_child': ('Questa issue è figlia della storia `{story}` della radice #{root}: la '
                        'eredita solo da questo marker del dispatcher.'),
        'story_id': 'Storia',
        'repo': 'Repository',
        'hashes': 'Hash delle richieste (SHA-256)',
        'root': 'Radice',
    })
# The first line of a record in every language (``watch`` leaves them out, #133): a record
# written before a change of ``language`` is still recognised.
TITLES = TEXTS.variants('title')


def _title(t, key, **fields):
    return f'{t("title")}: {t(key, **fields)}'


def _value(t, value):
    if value is None:
        return t('not_reported')
    if isinstance(value, list):
        return ', '.join(f'`{v}`' for v in value) if value else t('not_reported')
    return f'`{value}`'


def _rows(t, pairs):
    return '\n'.join(f'- **{name}:** {_value(t, value)}' for name, value in pairs)


def _labelled(t, r, keys):
    """The rows of ``keys`` of ``r``, each named by the text of its key."""
    return [(t(key), r.get(key)) for key in keys]


_TURN_NOTE = {'open': 'turn_open', 'closed': None, 'incomplete': 'turn_incomplete'}


def _render_turn(t, r, title=None):
    telemetry = r.get('telemetry') or {}
    parts = [title or _title(t, 'turn', kind=r['kind'], agent=r['agent']), '']
    if _TURN_NOTE[r['state']]:
        parts += [t(_TURN_NOTE[r['state']]), '']
    identity = r.get('identity') or t('operator_login')
    parts.append(f'- **{t("identity")}:** {identity}')
    rows = _labelled(t, r, ('model', 'effort'))
    if r.get('fallback_for'):
        rows.append((t('fallback_for'), r['fallback_for']))
    if r.get('attempt'):
        rows.append((t('attempt'), r['attempt']))
    if r.get('role') == 'findings':
        # #416: its verdict does not count for the quorum.
        rows.append((t('role'), 'finding-only'))
    rows += _labelled(t, r, ('model_real', 'started_at', 'ended_at', 'duration_ms', 'outcome'))
    parts.append(_rows(t, rows))
    for key in ('last_command', 'denied_command'):
        if r.get(key):
            command = ' '.join(str(r[key]).split())
            fence = '`' * (max((len(run) for run in re.findall('`+', command)), default=0) + 1)
            parts.append(f'- **{t(key)}:** {fence} {command} {fence}')
    if r.get('error'):
        parts.append(f'- **{t("error")}:** {" ".join(str(r["error"]).split())}')
    parts += ['', t('telemetry'), '',
              _rows(t, [(k, telemetry.get(k)) for k in TELEMETRY_FIELDS])]
    return parts


def _render_request(t, r):
    keys = ('hash', 'labeled_by', 'labeled_at', 'comments',
            *(('implementer',) if 'implementer' in r else ()), 'taken_at')
    return [_title(t, 'request'), '', t('request_note'), '', _rows(t, _labelled(t, r, keys))]


def _render_verification(t, r, title=None):
    parts = [title or _title(t, 'verification'), '', t('verification_note'), '',
             _rows(t, _labelled(t, r, ('kind', 'attempt', 'command', 'outcome', 'duration_ms',
                                       'tree_before', 'tree_after', 'commit', 'at')))]
    if isinstance(r.get('retried'), list):
        # #598: the failed tests of the attempt, which failed on the base commit too, run
        # again alone before the escalation host_tests or the wait base_broken.
        cases = ''.join(f'\n- `{str(c).replace("`", "")}`' for c in r['retried'])
        parts += ['', t('unstable' if r.get('outcome') == 'pass' else 'still_failing') + cases]
    if isinstance(r.get('changed_by_tests'), list):
        # #627: the test changed these files, so the tree after it is not the one before.
        files = ''.join(f'\n- `{str(f).replace("`", "")}`' for f in r['changed_by_tests'])
        if isinstance(r.get('changed_by_tests_omitted'), int):
            files += '\n- ' + t('changed_by_tests_omitted', count=r['changed_by_tests_omitted'])
        parts += ['', t('changed_by_tests') + files]
    return parts


def _render_event(t, r, title=None):
    parts = [title or _title(t, 'event', event=r['event']), '']
    shown = [(k, v) for k, v in r.items() if k not in ('type', 'event', 'checks', 'section')]
    parts.append(_rows(t, shown))
    if isinstance(r.get('checks'), list):
        parts += ['', t('checks'), '|---|---|---|']
        for c in r['checks']:
            name = str(c.get('name')).replace('|', '\\|')
            parts.append(f'| {name} | {_value(t, c.get("outcome"))} | '
                         f'{_value(t, c.get("run_id"))} |')
    return parts


def _render_round(t, r):
    parts = [_title(t, 'round', round=r['round']), '', t('round_note')]
    for s in r['records']:
        parts.append('')
        parts += _render_section(t, s)
    return parts


def _render_section(t, s):
    number = f'### {s["section"]}.'
    if s['type'] == 'turn':
        title = t('section_turn', kind=s['kind'], agent=s['agent'])
        return _render_turn(t, s, f'{number} {title}')
    if s['type'] == 'verification':
        return _render_verification(t, s, f'{number} {t("verification_section")}')
    return _render_event(t, s, f'{number} {t("event_section", event=s["event"])}')


def _render_fix(t, r):
    which = _title(t, 'fix_of_round', round=r['round']) if r['round'] else _title(t, 'fix_first')
    parts = [which, '', t('fix_note')]
    for s in r['records']:
        parts.append('')
        parts += _render_section(t, s)
    return parts


def _render_story(t, s, title=None):
    """The readable part of a ``story`` marker (#363)."""
    if s['issue'] == s['root']:
        note = t('story_root', story=s['story'])
        rows = [(t('story_id'), s['story']), (t('repo'), s['repo']), (t('hashes'), s['hashes'])]
    else:
        note = t('story_child', story=s['story'], root=s['root'])
        rows = [(t('story_id'), s['story']), (t('repo'), s['repo']), (t('root'), s['root'])]
    return [title or _title(t, 'story'), '', note, '', _rows(t, rows)]


_RENDER = {'turn': _render_turn, 'request': _render_request,
           'verification': _render_verification, 'event': _render_event,
           'round': _render_round, 'fix': _render_fix}


def render(record, story=None, language=ENGLISH):
    """The comment of a ledger record: the dispatcher's header, a readable summary in
    ``language`` (the project's, #631) and the marker; with ``story``, the ``story`` marker of
    the root of a ``request`` record (#363) after it."""
    t = TEXTS.texts(language)
    parts = _RENDER[record['type']](t, record)
    if story is None:
        return publishable(dispatcher_header() + '\n' + '\n'.join(parts), 'ledger', record)
    parts += [''] + _render_story(t, story, t('story_section'))
    return publishable(dispatcher_header() + '\n' + '\n'.join(parts), 'ledger', record,
                       records=[('story', story)])
