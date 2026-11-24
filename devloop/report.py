"""``devloop report`` (#256): the summary of a day, generated from devloop's own records.

The sources are the records the dispatcher reads, never a local log: the ``ledger`` records
(#133), ``merged`` and ``notice`` records, all signed by the dispatcher's identity, the ``review``
records of the configured reviewers, and the ``pr`` marker of the PRs opened by an implementer.
Days are UTC days:

- a turn belongs to the day of its ``started_at``, with its whole duration; a ``merged`` record
  to the day GitHub reports for the merge of its PR (#338): without it, the day of its own
  comment only when that comment is not the promotion's, written earlier; any other record to
  the day of its own time (``at``) or, without one, of the creation of its comment;
- the totals count the PRs opened that day, the PRs merged that day (by devloop or by a person),
  the review rounds opened that day (the first review record of the round), the turns run
  with a fallback model (``fallback_for``) and, per model, the turns stopped on a permission
  headless agy denied (``denied_permission``, #479, with or without the command agy reports);
- the grants of the supervisor (#466, ADR 0005, section 3.1, rule 4): every ``grant-round``,
  ``grant-budget`` and ``capacity`` the dispatcher answered that day, from its ``supervision``
  records, with the time, the issue or PR, the first line of the reason, the outcome and the
  caps left (per PR, or per story for the one extension of its budget, and per day), so that
  the coordinator verifies them without reading each thread (rule 5);
- the delegated merges (#486): every ``merge`` and ``merge-budget`` the dispatcher answered
  that day, from its ``supervision`` records, with the time, the PR or the summary issue, the
  author, the first line of the reason, the outcome and the cap of the day;
- the reserved merges of the coordinator (#529): every ``merge`` of the coordinator's identity
  the dispatcher answered that day (its ``supervision`` record with ``role: coordinator``),
  apart from the delegated merges, with the same columns and the coordinator's own cap;
- the findings the coordinator closed on an ``open_findings`` escalation (#606): one row per
  finding of each ``finding`` command it applied that day (``outcome: closed``), from the
  ``finding`` record in the comment of its ``supervision`` record, with the time, the PR, the
  key, the status, the author and the first line of the reason;
- the auto-pins that did not move ``current`` (#453): the ``pin`` record of the dispatcher's alert
  on ``[run].alert_issue``, one per commit, with the time, the commit of ``origin/<base_branch>``
  left behind and the reason the alert shows (the step of the smoke test, or the first line of
  the failed check).

With ``--publish`` the summary goes to ``[report] issue``, a dedicated issue whose body is the
dispatcher's: today's summary on top, then the index of the past days. When the day changes the
summary of the closed day, generated again from the records, is archived in a comment of its own
(a ``report`` record with ``event: archive``), one per day, also for the days in between that had
activity. An archive is recognized only when the dispatcher wrote it: a second run, or one
repeated after a failure, edits it instead of writing another. An archive written while a turn of
its day was still open says so (``open: true``): every publication generates that day again and
edits the archive if the records changed, until no turn of the day is open. ``devloop run`` publishes at the
end of a tick, at most every ``[report] interval`` minutes (``AutoReport``).
"""

import datetime
import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .apps import COORDINATOR, DISPATCHER
from .markdown import dispatcher_header, publishable, read_marker
from .supervision import CAPACITY, FINDING, GRANT_BUDGET, GRANT_ROUND, MERGE, MERGE_BUDGET
from .watch import failure_class

# In state_dir: the time of the last automatic attempt (``AutoReport``), and the lock that keeps
# two publications from running at once.
STATE_FILE = 'report-state.json'
LOCK_FILE = 'report.lock'
# The days a publication archives at most when it catches up after a pause.
MAX_CATCH_UP_DAYS = 31
TITLE = 'Riepilogo devloop'
TODAY_HEADING = f'## {TITLE} di oggi'
INDEX_HEADING = '## Giorni precedenti'
NOT_REPORTED = 'non riportato'
_DESTINATION = {'operator': 'all\'operatore', 'supervisor': 'al supervisore'}
_STATE = {'open': 'in corso', 'incomplete': 'incompleto'}
# The commands of the supervisor's delegations the summary lists (#466), and their outcomes.
GRANT_COMMANDS = (GRANT_ROUND, GRANT_BUDGET, CAPACITY)
_GRANT_OUTCOME = {'granted': 'concessa', 'refused': 'rifiutata'}
# The delegated merges and the extensions of their daily cap (#486), and their outcomes.
MERGE_COMMANDS = (MERGE, MERGE_BUDGET)
_MERGE_OUTCOME = {'merged': 'mergiata', 'granted': 'concessa', 'refused': 'rifiutato'}
REASON_CHARS = 120  # the first line of a reason, at most
# The status of a finding the coordinator closed (#606).
_CLOSED = {'resolved': 'risolto', 'withdrawn': 'ritirato'}


class ReportError(Exception):
    """The report cannot be produced or published as asked."""


def parse_day(text):
    """The ``datetime.date`` of ``YYYY-MM-DD``; ``ValueError`` otherwise."""
    if not isinstance(text, str) or len(text) != 10:
        raise ValueError(f'not a day in the form YYYY-MM-DD: {text!r}')
    return datetime.date.fromisoformat(text)


def utc_today(clock=time.time):
    return datetime.datetime.fromtimestamp(clock(), datetime.timezone.utc).date()


def _day_of(stamp):
    """The UTC day of an ISO 8601 time as GitHub and the ledger write it, or None."""
    return stamp[:10] if isinstance(stamp, str) and len(stamp) >= 10 else None


def _hour(stamp):
    return stamp[11:16] if isinstance(stamp, str) and len(stamp) >= 16 else '--:--'


def duration(ms):
    """``ms`` milliseconds in a readable form, or ``non riportato``."""
    if not isinstance(ms, int) or isinstance(ms, bool) or ms < 0:
        return NOT_REPORTED
    seconds = ms // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f'{hours}h {minutes:02d}m'
    if minutes:
        return f'{minutes}m {seconds:02d}s'
    return f'{seconds}s'


@dataclass
class Summary:
    """What devloop recorded on ``day``."""
    day: str
    actions: list = field(default_factory=list)      # (time, number, kind, agent, outcome, ms)
    merges: list = field(default_factory=list)       # (time, number, mode, level)
    escalations: list = field(default_factory=list)  # (time, number, class, destination)
    ci: list = field(default_factory=list)           # (time, number, text)
    # (time, number, command, reason, outcome, caps left): the grants of the supervisor (#466)
    grants: list = field(default_factory=list)
    # (time, number, command, author, reason, outcome, cap): the delegated merges and the
    # extensions of their daily cap (#486)
    delegated: list = field(default_factory=list)
    # The same rows for the reserved merges of the coordinator (#529)
    coordinator: list = field(default_factory=list)
    # (time, number, key, status, author, reason): the findings the coordinator closed (#606)
    closures: list = field(default_factory=list)
    # (time, number, ref, commit, reason): the auto-pins that did not move ``current`` (#453)
    pins: list = field(default_factory=list)
    opened: list = field(default_factory=list)       # PR numbers
    merged: list = field(default_factory=list)       # PR numbers
    rounds: int = 0
    fallbacks: int = 0
    # {model: turns}: the turns stopped on a permission headless agy denied (#479)
    denied: dict = field(default_factory=dict)
    open: int = 0                                    # turns not closed yet

    def empty(self):
        return not (self.actions or self.merges or self.escalations or self.ci or self.grants
                    or self.delegated or self.coordinator or self.closures or self.pins
                    or self.opened
                    or self.merged or self.rounds)


class Records:
    """The records of the days from ``first`` to ``last`` (``datetime.date``), read once with
    ``gh`` (``github.GitHub``). ``trust`` is a ``watch.Trust``: who may write each record;
    ``agents`` the keys of the only reviewers whose reviews count (``Config.review_keys``: the
    configured agents that do not sign with a role app, and the keys before the migration of
    #407 that one of them stands for)."""

    def __init__(self, gh, trust, first, last, agents=()):
        self.gh = gh
        self.trust = trust
        self.agents = frozenset(agents)
        since = f'{first.isoformat()}T00:00:00Z'
        self.comments = gh.dated_comments(since=since)
        # The search reads dates in its own time zone: one more day on each side, then the UTC
        # day of each PR decides.
        span = (f'{(first - datetime.timedelta(days=1)).isoformat()}..'
                f'{(last + datetime.timedelta(days=1)).isoformat()}')
        self.created = self._devloop_prs(gh.search_prs(f'created:{span}'))
        merged = gh.search_prs(f'merged:{span}')
        self.merged = self._devloop_prs(merged)
        # When each PR merged in the span merged, whoever opened it: a ``merged`` record belongs
        # to the day of its merge. A PR missing here merged outside the days read.
        self._merged_at = {pr['number']: pr['mergedAt'] for pr in merged
                           if isinstance(pr.get('mergedAt'), str)}
        self._threads = {}

    def _devloop_prs(self, prs):
        """The PRs whose ``pr`` marker the implementer it names wrote, as ``(number, created,
        merged)`` times."""
        found = []
        for pr in prs:
            meta = read_marker(pr.get('body') or '', 'pr')
            if meta and self.trust.wrote((pr.get('author') or {}).get('login'),
                                         meta['implementer']):
                found.append((pr['number'], pr.get('createdAt'), pr.get('mergedAt')))
        return found

    def _signed(self, comment):
        return self.trust.wrote(comment.get('login'), DISPATCHER)

    def _review(self, comment):
        """The review record of ``comment`` that counts for its round: a CI round of the
        dispatcher, or a review of a configured agent written with that agent's identity."""
        record = read_marker(comment.get('body') or '', 'review')
        if record is None:
            return None
        if record['source'] == 'ci':
            actor = DISPATCHER
        else:
            actor = record.get('reviewer')
            if actor not in self.agents:
                return None
        return record if self.trust.wrote(comment.get('login'), actor) else None

    def _register(self, comment):
        """The round whose register (#325) ``comment`` is, when the dispatcher wrote it and the
        round was evaluated: a review turn ended with success, or the CI of the round was
        recorded. None otherwise. A review an agent publishes as a native review is not among
        the comments the summary reads: its round is counted from the register."""
        if not self._signed(comment):
            return None
        record = read_marker(comment.get('body') or '', 'ledger')
        if record is None or record['type'] != 'round':
            return None
        evaluated = any(s['type'] == 'event' or s.get('outcome') == 'success'
                        for s in record['records'])
        return record['round'] if evaluated else None

    def _round_opened(self, number, round_no):
        """When the first review record, or the register (#325), of round ``round_no`` of PR
        ``number`` was written."""
        if number not in self._threads:
            self._threads[number] = self.gh.dated_comments(number=number)
        times = [c.get('created_at') for c in self._threads[number]
                 if ((r := self._review(c)) is not None and r['round'] == round_no)
                 or self._register(c) == round_no]
        return min((t for t in times if isinstance(t, str)), default=None)

    def summary(self, day):
        """The ``Summary`` of ``day`` (``datetime.date``)."""
        day = day.isoformat()
        out = Summary(day)
        rounds = set()
        for comment in self.comments:
            body, created = comment.get('body') or '', comment.get('created_at')
            number = comment.get('number')
            review = self._review(comment)
            if review is not None and _day_of(created) == day:
                rounds.add((number, review['round']))
            registered = self._register(comment)
            if registered is not None and _day_of(created) == day:
                rounds.add((number, registered))
            if not self._signed(comment):
                continue
            ledger = read_marker(body, 'ledger')
            if ledger is not None:
                # A round's register (#325) holds the records of its turns and of its CI, a fix
                # register (#355) those of its turns, sync and local tests.
                for record in (ledger['records'] if ledger['type'] in ('round', 'fix')
                               else [ledger]):
                    self._ledger(out, day, number, record, created)
                continue
            merged = read_marker(body, 'merged')
            if merged is not None:
                # A ``merged`` record belongs to the day of the merge. Since #338 it is added to
                # the comment of the promotion, written when the PR was promoted, maybe days
                # before: that comment's time is never the merge's, and without the merge's own
                # time from the search the merge fell outside the days read. A comment of its
                # own, as before #338, was written at the merge.
                at = self._merged_at.get(number)
                if at is None and read_marker(body, 'policy') is None:
                    at = created
                if at is not None and _day_of(at) == day:
                    out.merges.append((_hour(at), number, merged['mode'], merged['level']))
                continue
            pin = read_marker(body, 'pin')
            if pin is not None:
                if _day_of(pin['at']) == day:
                    out.pins.append((_hour(pin['at']), number, pin['ref'], pin['sha'],
                                     first_line(pin['reason'])))
                continue
            supervision = read_marker(body, 'supervision')
            if supervision is not None:
                if supervision['command'] in GRANT_COMMANDS and _day_of(created) == day:
                    out.grants.append(_grant_row(_hour(created), number, supervision))
                elif supervision['command'] in MERGE_COMMANDS and _day_of(created) == day:
                    rows = (out.coordinator if supervision.get('role') == COORDINATOR
                            else out.delegated)
                    rows.append(_delegated_row(_hour(created), number, supervision))
                elif (supervision['command'] == FINDING and supervision['outcome'] == 'closed'
                      and _day_of(created) == day):
                    # The coordinator's closures (#606): the record in the same comment.
                    closed = (read_marker(body, 'finding') or {}).get('closed', [])
                    out.closures += [(_hour(created), number, c['key'], c['status'],
                                      c['author'], first_line(c['reason'])) for c in closed]
                continue
            notice = read_marker(body, 'notice')
            if (notice is not None and notice.get('event') == 'escalation'
                    and _day_of(created) == day):
                out.escalations.append((_hour(created), number, notice.get('class') or 'failure',
                                        notice.get('to') or 'operator'))
        out.rounds = sum(1 for number, round_no in rounds
                         if _day_of(self._round_opened(number, round_no)) == day)
        out.opened = sorted(n for n, created, _ in self.created if _day_of(created) == day)
        merged_numbers = {n for n, _, merged in self.merged if _day_of(merged) == day}
        by_devloop = {number for _, number, _, _ in out.merges}
        for number, _, merged in self.merged:
            if _day_of(merged) == day and number not in by_devloop:
                out.merges.append((_hour(merged), number, None, None))
        out.merged = sorted(merged_numbers | by_devloop)
        for rows in (out.actions, out.merges, out.escalations, out.ci, out.grants, out.delegated,
                     out.coordinator, out.closures, out.pins):
            rows.sort(key=lambda row: (row[0], row[1]))
        return out

    @staticmethod
    def _ledger(out, day, number, record, created):
        if record['type'] == 'turn':
            if _day_of(record['started_at']) != day:
                return
            outcome = record.get('outcome') or _STATE.get(record['state'], NOT_REPORTED)
            kind = record['kind'] + (' (fallback)' if record.get('fallback_for') else '')
            out.actions.append((_hour(record['started_at']), number, kind, record['agent'],
                                outcome, record.get('duration_ms')))
            if record.get('fallback_for'):
                out.fallbacks += 1
            if record.get('denied_permission'):
                model = str(record.get('model') or NOT_REPORTED)
                out.denied[model] = out.denied.get(model, 0) + 1
            if record['state'] == 'open':
                out.open += 1
            return
        if record['type'] != 'event':
            return
        at = record.get('at') if isinstance(record.get('at'), str) else created
        if _day_of(at) != day:
            return
        event = record['event']
        if event == 'ci_round':
            if record.get('state') == 'fail':
                head = str(record.get('head') or '')[:12] or NOT_REPORTED
                out.ci.append((_hour(at), number, f'blocco: CI `fail` sul commit `{head}`'))
        elif event == 'ci_rerun':
            runs = ', '.join(str(r) for r in record.get('runs') or []) or NOT_REPORTED
            out.ci.append((_hour(at), number, f'rilancio dei job falliti (run {runs})'))
        else:
            out.actions.append((_hour(at), number, f'evento {event}', None, None, None))


def first_line(text):
    """The first non-empty line of ``text``, at most ``REASON_CHARS`` characters, or ``—``."""
    line = next((ln.strip() for ln in str(text or '').splitlines() if ln.strip()), '')
    if len(line) > REASON_CHARS:
        line = line[:REASON_CHARS - 1].rstrip() + '…'
    return line or '—'


def _count(record, left, cap):
    """``left`` of ``cap`` as ``record`` gives them, or None when it does not give both as
    integers."""
    values = [record.get(left), record.get(cap)]
    if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return f'{max(values[0], 0)} di {values[1]}'
    return None


def remaining(record):
    """The caps a command of the supervisor's delegations (its ``supervision`` record) leaves,
    in Italian (#466): per PR the review rounds (``grant-round``), per story the one extension
    of its budget envelope (``grant-budget``), the limit in force for a ``capacity``, and per
    day the grants of every delegation together. A refused command records none: ``—``."""
    parts = []
    if record.get('outcome') == 'granted':
        if record['command'] == GRANT_ROUND and (rounds := _count(record, 'left', 'cap')):
            parts.append(f'PR: {rounds} round')
        elif record['command'] == GRANT_BUDGET:
            parts.append('storia: 0 di 1 estensioni (prenotata fino all\'avvio del turno)')
        elif record['command'] == CAPACITY and isinstance(record.get('setting'), str):
            parts.append(f'`{record["setting"]}` = {record.get("value")} fino a '
                         f'{record.get("until") or NOT_REPORTED}')
    if daily := _count(record, 'daily_left', 'daily_cap'):
        parts.append(f'giorno: {daily}')
    return '; '.join(parts) or '—'


def _grant_row(at, number, record):
    """A row of the grants of the supervisor (#466) from its ``supervision`` record."""
    outcome = _GRANT_OUTCOME.get(record['outcome'], record['outcome'])
    if record.get('trace_only'):
        outcome += ' (traccia completata)'
    return (at, number, record['command'], first_line(record.get('reason')), outcome,
            remaining(record))


def _delegated_row(at, number, record):
    """A row of the delegated merges and of the extensions of their cap (#486), from the
    dispatcher's ``supervision`` record: the cap of the day as it stood, or the extension."""
    outcome = _MERGE_OUTCOME.get(record['outcome'], record['outcome'])
    if record['command'] == MERGE_BUDGET and record['outcome'] == 'granted':
        cap = (f'+{record.get("value")} il {record.get("day") or NOT_REPORTED}: tetto '
               f'{record.get("cap")}')
    else:
        cap = _count(record, 'daily', 'daily_cap')
        cap = f'giorno: {cap}' if cap else '—'
    return (at, number, record['command'], record.get('by') or NOT_REPORTED,
            first_line(record.get('reason')), outcome, cap)


def _cell(value):
    return str(value).replace('|', '\\|').replace('\n', ' ')


def _code(value):
    """``value`` inside a code span: no backtick, no line break."""
    return str(value).replace('`', "'").replace('\n', ' ')


def render(summary):
    """The summary in Markdown, without heading."""
    if summary.empty():
        return 'Nessuna attività registrata da devloop.'
    parts = []
    if summary.actions:
        parts += ['### Azioni', '', '| Ora (UTC) | Numero | Tipo | Agent | Esito | Durata |',
                  '|---|---|---|---|---|---|']
        for at, number, kind, agent, outcome, ms in summary.actions:
            parts.append(f'| {at} | #{number} | {_cell(kind)} | {_cell(agent or "—")} '
                         f'| {_cell(outcome or "—")} | '
                         f'{duration(ms) if agent else "—"} |')
        parts.append('')
    if summary.merges:
        parts += ['### Merge', '']
        for at, number, mode, level in summary.merges:
            if mode is None:
                parts.append(f'- {at} #{number}: merge di una persona (nessun record di devloop)')
            else:
                parts.append(f'- {at} #{number}: modalità `{mode}`, livello {level}')
        parts.append('')
    if summary.escalations:
        parts += ['### Escalation', '']
        for at, number, cls, to in summary.escalations:
            parts.append(f'- {at} #{number}: classe `{cls}`, {_DESTINATION.get(to, to)}')
        parts.append('')
    if summary.ci:
        parts += ['### CI', '']
        parts += [f'- {at} #{number}: {text}' for at, number, text in summary.ci]
        parts.append('')
    if summary.grants:
        parts += ['### Concessioni del supervisore', '',
                  '| Ora (UTC) | Numero | Comando | Motivo | Esito | Tetti residui |',
                  '|---|---|---|---|---|---|']
        for at, number, command, reason, outcome, left in summary.grants:
            parts.append(f'| {at} | #{number} | `{command}` | {_cell(reason)} '
                         f'| {_cell(outcome)} | {_cell(left)} |')
        parts.append('')
    for title, rows in (('Merge delegati al supervisore', summary.delegated),
                        ('Merge riservati del coordinatore', summary.coordinator)):
        if not rows:
            continue
        parts += [f'### {title}', '',
                  '| Ora (UTC) | Numero | Comando | Autore | Motivo | Esito | Tetto |',
                  '|---|---|---|---|---|---|---|']
        for at, number, command, by, reason, outcome, cap in rows:
            parts.append(f'| {at} | #{number} | `{command}` | `{_code(by)}` | {_cell(reason)} '
                         f'| {_cell(outcome)} | {_cell(cap)} |')
        parts.append('')
    if summary.closures:
        parts += ['### Finding chiusi dal coordinatore', '',
                  '| Ora (UTC) | Numero | Finding | Esito | Autore | Motivo |',
                  '|---|---|---|---|---|---|']
        for at, number, key, status, by, reason in summary.closures:
            outcome = _CLOSED.get(status, status)
            parts.append(f'| {at} | #{number} | `{_code(key)}` | {_cell(outcome)} '
                         f'| `{_code(by)}` | {_cell(reason)} |')
        parts.append('')
    if summary.pins:
        parts += ['### Pin automatico non riuscito', '']
        for at, number, ref, sha, reason in summary.pins:
            parts.append(f'- {at} #{number}: `{_code(ref)}` al commit `{sha[:12]}` non è '
                         'fissato, la copia in esecuzione resta indietro. Motivo: '
                         f'`{_code(reason)}`')
        parts.append('')

    def listed(numbers):
        return f'{len(numbers)}' + (f' ({", ".join(f"#{n}" for n in numbers)})' if numbers else '')
    denied = ', '.join(f'`{_code(model)}`: {count}'
                       for model, count in sorted(summary.denied.items()))
    parts += ['### Totali', '',
              f'- PR aperte: {listed(summary.opened)}',
              f'- PR mergiate: {listed(summary.merged)}',
              f'- Round di review aperti: {summary.rounds}',
              f'- Turni con un modello di fallback: {summary.fallbacks}',
              f'- Permessi negati per modello: {sum(summary.denied.values())}'
              + (f' ({denied})' if denied else '')]
    return '\n'.join(parts)


def render_text(summary):
    """What ``devloop report`` prints."""
    return f'# {TITLE} del {summary.day} (UTC)\n\n{render(summary)}\n'


def archive_body(summary):
    return publishable(dispatcher_header() + f'\n## {TITLE} del {summary.day} (UTC)\n\n'
                       + render(summary), 'report',
                       {'event': 'archive', 'day': summary.day, 'open': summary.open > 0})


@contextmanager
def locked(path):
    """Hold ``path`` exclusively: two publications never run at once."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


class Publisher:
    """Publishes the summaries on issue ``issue`` of ``repo`` with ``gh``, the dispatcher's
    client; ``trust`` is a ``watch.Trust``. ``clock`` gives the current time (epoch seconds)."""

    def __init__(self, gh, trust, repo, issue, *, agents=(), log=print, clock=time.time,
                 lock=None):
        self.agents = tuple(agents)
        self.gh = gh
        self.trust = trust
        self.repo = repo
        self.issue = issue
        self.log = log
        self._clock = clock
        self._lock = lock

    def _url(self, comment_id):
        return f'https://github.com/{self.repo}/issues/{self.issue}#issuecomment-{comment_id}'

    def _archives(self):
        """``{day: comment}``: the first archive of each day the dispatcher wrote."""
        found = {}
        for comment in self.gh.dated_comments(number=self.issue):
            record = read_marker(comment.get('body') or '', 'report')
            if (record is not None and record['event'] == 'archive'
                    and self.trust.wrote(comment.get('login'), DISPATCHER)):
                found.setdefault(record['day'], comment)
        return found

    def _body(self):
        """``(body, day shown)`` of the issue: the day only when the dispatcher wrote the body
        last, with a ``today`` record."""
        history = self.gh.issue_history(self.issue)
        writer = history.get('editor') if history.get('last_edited_at') else history.get('author')
        record = read_marker(history.get('body') or '', 'report')
        if (record is None or record['event'] != 'today'
                or not self.trust.wrote(writer, DISPATCHER)):
            return history.get('body') or '', None
        return history['body'], record['day']

    def _archive(self, summary, archives):
        body = archive_body(summary)
        existing = archives.get(summary.day)
        if existing is None:
            comment_id = self.gh.comment(self.issue, body)
            archives[summary.day] = {'id': comment_id, 'body': body}
            self.log(f'report: {summary.day} archived on #{self.issue}')
        elif (existing.get('body') or '').strip() != body.strip():
            self.gh.edit_comment(existing['id'], body)
            existing['body'] = body
            self.log(f'report: archive of {summary.day} updated')

    def _index(self, archives):
        lines = [INDEX_HEADING, '']
        days = sorted((d for d in archives if archives[d].get('id') is not None), reverse=True)
        lines += [f'- [{d}]({self._url(archives[d]["id"])})' for d in days] or ['Nessuno.']
        return '\n'.join(lines)

    def _today_section(self, summary):
        stamp = time.strftime('%H:%M', time.gmtime(self._clock()))
        return (dispatcher_header() + f'\n{TODAY_HEADING}: {summary.day} (UTC)\n\n'
                f'Generato dai record di devloop, aggiornato alle {stamp} UTC. Ogni giorno chiuso '
                'resta in un commento, linkato qui sotto.\n\n' + render(summary))

    def publish(self, day=None):
        """Publish the summary of ``day`` (default: today). Today's goes to the body, after the
        closed days are archived; a past day's only to its archive and the index. A future day
        is refused."""
        if self._lock is None:
            return self._publish(day)
        with locked(self._lock):
            return self._publish(day)

    def _publish(self, day):
        today = utc_today(self._clock)
        day = day or today
        if day > today:
            raise ReportError(f'{day} is in the future: only today or a past day is published')
        body, shown = self._body()
        archives = self._archives()
        if day < today:
            head = body.split('\n' + INDEX_HEADING, 1)[0].rstrip() if shown else None
            records = Records(self.gh, self.trust, day, day if shown else today, self.agents)
            self._archive(records.summary(day), archives)
            if head is None:
                head = self._today_section(records.summary(today))
                shown = today.isoformat()
            self._write_body(head, archives, shown, body)
            return
        days = sorted(set(self._closed_days(today, shown, archives))
                      | set(self._open_archives(today, archives)))
        records = Records(self.gh, self.trust, days[0] if days else today, today, self.agents)
        for closed in days:
            summary = records.summary(closed)
            # The day shown, and a day already archived (one with a turn still open, say), are
            # archived again; the days in between only with activity.
            if closed.isoformat() in (shown, *archives) or not summary.empty():
                self._archive(summary, archives)
        self._write_body(self._today_section(records.summary(today)), archives,
                         today.isoformat(), body)

    def _closed_days(self, today, shown, archives):
        """The closed days to archive: from the day the body shows, or the day after the last
        archive, up to yesterday, at most ``MAX_CATCH_UP_DAYS``."""
        starts = []
        if shown:
            starts.append(parse_day(shown))
        if archives:
            starts.append(parse_day(max(archives)) + datetime.timedelta(days=1))
        if not starts:
            return []
        first = max(min(starts), today - datetime.timedelta(days=MAX_CATCH_UP_DAYS))
        days = []
        while first < today:
            days.append(first)
            first += datetime.timedelta(days=1)
        return days

    def _open_archives(self, today, archives):
        """The archived days, in the last ``MAX_CATCH_UP_DAYS``, that had a turn still open when
        they were archived last: their records may have changed since."""
        first = today - datetime.timedelta(days=MAX_CATCH_UP_DAYS)
        days = []
        for day, comment in archives.items():
            record = read_marker(comment.get('body') or '', 'report')
            if record is not None and record.get('open') is True:
                date = parse_day(day)
                if first <= date < today:
                    days.append(date)
        return days

    def _write_body(self, head, archives, shown, before):
        body = publishable(head + '\n\n' + self._index(archives), 'report',
                           {'event': 'today', 'day': shown})
        if body.strip() != before.strip():
            self.gh.edit_issue_body(self.issue, body)
            self.log(f'report: body of #{self.issue} updated')


class AutoReport:
    """``devloop run``: publish at the end of a tick, at most every ``interval_minutes``. The
    time of the last attempt stays in ``state_dir`` (``STATE_FILE``), so the interval holds
    across restarts; the first attempt of a new state runs at once. A failure is logged and
    retried at the next interval allowed: it never fails the tick."""

    def __init__(self, publisher, state_dir, interval_minutes, *, log, clock=time.time):
        self.publisher = publisher
        self.path = Path(state_dir) / STATE_FILE
        self.interval = interval_minutes * 60
        self.log = log
        self._clock = clock

    def _last(self):
        try:
            value = json.loads(self.path.read_text(encoding='utf-8')).get('last_attempt')
        except (OSError, ValueError, AttributeError):
            return None
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def _store(self, now):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f'.{self.path.name}.{os.getpid()}.tmp')
        tmp.write_text(json.dumps({'last_attempt': now}) + '\n', encoding='utf-8')
        os.replace(tmp, self.path)

    def after_tick(self):
        """Publish if the interval since the last attempt has passed; whether it tried."""
        now = self._clock()
        last = self._last()
        if last is not None and 0 <= now - last < self.interval:
            return False
        try:
            self._store(now)
            self.publisher.publish()
        except Exception as exc:  # logged, retried at the next interval: never a failed tick
            self.log(f'report: not published ({failure_class(exc)}): {exc}; next attempt in '
                     f'{self.interval // 60} minutes')
        return True


def build(cfg, gh, trust, state_dir, *, log):
    """The ``Publisher`` of ``[report] issue``, or None when no issue is configured."""
    if not cfg.report.issue:
        return None
    return Publisher(gh, trust, cfg.repo, cfg.report.issue, agents=cfg.review_keys(), log=log,
                     lock=Path(state_dir) / LOCK_FILE)

