"""The budget envelope of each story and the booking of each turn (#365, ADR 0004, P2-5a).

Section 3.3 of ADR 0004, alternative B: before each turn of an agent CLI the dispatcher books it
in the envelope of its story, with the calculations of ``core.budget``, and a turn the envelope
does not cover is refused before it starts, with an escalation (``BudgetRefused``, class
``budget``). Off by default (``[budget] enabled``, D8): then nothing here runs and devloop
behaves as before.

- **The envelope** (``config.BudgetPolicy``): turns of the story in total, per role (the kind of
  the turn: ``implement``, ``fix``, ``review``, ``sync``, ``conclave``, ``advisory``) and per kind
  of phase (``implementation``), and time (``wall_seconds`` from the story's start): the only
  measures devloop takes itself and that hold for every agent. Dollars count only where they are
  attested, measured spend (``metered_usd``) and estimates (``cli_estimate_usd``) apart: today
  the estimate claude reports. Token caps are not part of it. ``wall_seconds`` runs from the
  first booking of the story with the budget on, not from its takeover (#396).
- **The rule of each agent** for unknown usage, as in the engine: ``unmetered_quota`` (D2, for
  agy and codex, the only rule they accept: no reserve in USD, only the turn caps) or, for claude
  only, ``reserve_per_invocation`` (a reserve in both dollar kinds, booked before the turn).
  Every agent needs one, claude included (#396): a claude turn that ends without its
  ``total_cost_usd`` (a model not available, a timeout, a crash, no output) would otherwise
  leave its story ``uncovered`` for good. Without one the configuration is refused at startup.
  The rule is the agent's and covers every model of its chain: a fallback runs on the same CLI.
- **The booking** is written in the intent of the turn in the journal (``TurnJournal.intent``),
  in the same event and under the same lock as the check: the journal is the authority (D7).
- **The envelope is frozen** at the first booking of a story: every booking records the limits
  of its story (``envelope``: the turn and dollar caps, ``wall_seconds`` and the deadline), and
  the later ones read them from the journal, across restarts. A changed configuration applies
  to new stories only: it never extends an existing one, an operator's decision case by case.
  A role configured after that (the advisory review, the conclave) has no cap in the frozen
  envelope: its turn is refused with a category of its own, ``ROLE_NOT_IN_ENVELOPE``, never
  with a default cap, which would be a value of the budget (D2).
- **Consumption is cumulative**: every turn booked for the story counts, whatever its issue (the
  root or a child), attempt, outcome, model or process. Nothing refunds a booking: a turn that
  failed, was not dispatched or is uncertain stays counted. The dollars a CLI reported are
  recorded with the outcome (``usage``) and can only raise a charge, never lower it below the
  reserve (``core.budget.kind_charge``, D7).
- **Unconfirmed outcomes**: a booked turn without an outcome that does not run in this process,
  or ``uncertain`` and not reconciled, refuses every new turn of its story (``OUTCOME_UNCERTAIN``)
  until a trusted user reconciles it (``/devloop reconcile``). A turn still running in this
  process, in a parallel action of the same story, counts and does not refuse.
- **Outside the envelope**: the readiness check (``preflight``) comes before the takeover; it is
  neither booked nor charged to the story, and its outcome records its ``usage`` like the others
  (how to charge it is decided before the budget is turned on).
- **The state of the budget** (#393, ``view``): per dimension what is consumed (turns with a
  confirmed outcome, dollars observed), reserved (turns without a confirmed outcome, reserves
  no observation covers) and available. A dollar value no turn attests is ``non attestato``,
  never 0. ``devloop status`` shows it for the stories in progress, the PR receipt in a section
  of its own, a refused turn in its escalation; with the budget off, nowhere.
- **The one extension** (#411, ADR 0005, P5-7, ``grant-budget``): the supervisor may extend the
  envelope of a story once, ever, with ``[delegations] budget`` on. It adds to every turn cap
  (total, per role, per kind of phase) and to ``wall_seconds`` ``BUDGET_TOLERANCE_PERCENT`` of
  their frozen value, rounded down (``increments``), and nothing to the dollars, nor to tokens.
  The journal records it (``TurnJournal.extend``) and is its authority: the frozen envelope
  stays the one of the first booking, and the extension is added on top of it when the envelope
  is read (``envelope_of``). An extension that would not cover the refused turn (a nothing
  added, such as 25% of 3 turns, a limit in dollars, an outcome still uncertain) is not
  grantable (``grantable``): it is refused and does not spend the story's extension.
"""

import calendar
import dataclasses
import math
import time

from core import budget as core

from .journal import FAILED, IMPLEMENTATION, NOT_DISPATCHED, RETURNED, UNCERTAIN, TurnBlocked
from .supervision import BUDGET_TOLERANCE_PERCENT

BUDGET = 'budget'  # the class of the escalation of a refused turn
# The refusal of a turn whose role has no cap in the frozen envelope of its story (#396): the
# role was configured after its first booking. devloop's own category, beside those of ``core``.
ROLE_NOT_IN_ENVELOPE = 'ROLE_NOT_IN_ENVELOPE'
OUTSIDE = ('preflight',)  # the kinds of turn outside the envelope
# A booked turn as a row of ``core.budget``: the status the engine would give its outcome.
STATUS = {RETURNED: 'returned', FAILED: 'terminated', NOT_DISPATCHED: 'not_dispatched',
          UNCERTAIN: 'uncertain'}
# A booked turn still running in this process: charged, and no unconfirmed outcome.
RUNNING = 'running'
NO_USAGE = {'metered_usd': None, 'cli_estimate_usd': None}
# The statuses of a booked turn with a confirmed outcome: consumed. The others are reserved.
CONFIRMED = ('returned', 'terminated', 'not_dispatched')
NOT_ATTESTED = 'non attestato'  # a value no turn attests: never 0


class BudgetRefused(TurnBlocked):
    """A turn refused before its intent because the envelope of its story does not cover it
    (#365). A ``TurnBlocked`` with no turns: the step stops and escalates, as for the block on
    uncertain turns, and nothing is recorded as started. ``category``: the refusal of
    ``core.budget`` (``LIMIT_REACHED``, ``OUTCOME_UNCERTAIN``, ``SCOPE_REQUIRED``), or
    ``ROLE_NOT_IN_ENVELOPE`` for a role without a cap in the frozen envelope (#396). ``story``,
    ``kind`` and ``agent``: the id of the story, the kind of the turn and the key of its agent,
    for the extension of the envelope (#411); ``story`` is None for an issue of no story."""

    def __init__(self, message, category, story=None, kind=None, agent=None):
        super().__init__(message, [])
        self.category = category
        self.story = story
        self.kind = kind
        self.agent = agent


def observed(fields):
    """The dollars of a turn's telemetry (``agents.telemetry``) as ``usage``: the cost claude
    reports is an estimate (``cli_estimate_usd``); no CLI of devloop reports a measured spend.
    A value not reported, or not a finite number of at least 0, is None, never 0."""
    cost = (fields or {}).get('cost_usd')
    valid = type(cost) in (int, float) and math.isfinite(cost) and cost >= 0
    return {'metered_usd': None, 'cli_estimate_usd': float(cost) if valid else None}


def _seconds(at):
    return calendar.timegm(time.strptime(at, '%Y-%m-%dT%H:%M:%SZ'))


def _iso(seconds):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(seconds))


def policy(budget, engine=None, rule=None):
    """The limits profile of ``core.budget`` for ``budget`` (``config.BudgetPolicy``): with the
    declared ``rule`` of the agent about to run, under its CLI kind ``engine``."""
    profile = {'invocations': budget.turns, 'role_invocations': dict(budget.role_turns),
               'phase_invocations': dict(budget.phase_turns),
               'metered_usd': budget.metered_usd, 'cli_estimate_usd': budget.cli_estimate_usd}
    if rule is not None:
        profile['live_unknown_usage'] = {engine: rule}
    return profile


def booked_envelope(story, turns):
    """The limits recorded by the first booking of ``story`` (``TurnJournal.stories``) among
    ``turns`` (``TurnJournal.turns``), as ``limits`` gives them, or None before it."""
    for turn in turns.values():
        booked = turn.get('budget')
        if booked and booked.get('story') == story['story'] and 'envelope' in booked:
            return booked['envelope']
    return None


def started(story, turns, now):
    """The start of the time of ``story``, in seconds (#396): its first booking with the budget
    on, recorded in its envelope (``started_at``), never its takeover; ``now`` before its first
    booking. A booking recorded without ``started_at`` gives it from its deadline."""
    found = booked_envelope(story, turns)
    if found is None:
        return now
    if 'started_at' in found:
        return _seconds(found['started_at'])
    return _seconds(found['deadline_at']) - found['wall_seconds']


def limits(budget, start):
    """The limits of ``budget`` for a story whose time started at ``start`` (``started``) as a
    booking records them (``envelope``): the turn caps, the dollar caps, ``wall_seconds``, the
    start and the deadline it gives the story."""
    return {'turns': budget.turns, 'role_turns': dict(budget.role_turns),
            'phase_turns': dict(budget.phase_turns), 'wall_seconds': budget.wall_seconds,
            'started_at': _iso(start), 'deadline_at': _iso(start + budget.wall_seconds),
            'metered_usd': budget.metered_usd, 'cli_estimate_usd': budget.cli_estimate_usd}


def frozen(budget, story, turns):
    """The envelope of ``story``: the limits recorded by its first booking among ``turns``
    (``TurnJournal.turns``), as a ``config.BudgetPolicy`` with the rules of ``budget``; before
    its first booking, ``budget`` itself. A later configuration applies to new stories only:
    it never extends, nor shrinks, the envelope of a story already booked."""
    found = booked_envelope(story, turns)
    if found is None:
        return budget
    return dataclasses.replace(
        budget, turns=found['turns'], role_turns=tuple(found['role_turns'].items()),
        phase_turns=tuple(found['phase_turns'].items()),
        wall_seconds=found['wall_seconds'], metered_usd=found['metered_usd'],
        cli_estimate_usd=found['cli_estimate_usd'])


def _share(value):
    """``BUDGET_TOLERANCE_PERCENT`` of ``value``, rounded down: never beyond the tolerance."""
    return value * BUDGET_TOLERANCE_PERCENT // 100


def increments(envelope):
    """The extension of ``envelope`` (``frozen``, without an extension) (#411, D3): what it adds
    to each turn cap, the total (``turns``), per role and per kind of phase, and to
    ``wall_seconds``, each ``BUDGET_TOLERANCE_PERCENT`` of the value rounded down, so 0 for 3
    turns. Nothing for the dollars, nor for tokens."""
    return {'percent': BUDGET_TOLERANCE_PERCENT, 'turns': _share(envelope.turns),
            'role_turns': {role: _share(cap) for role, cap in envelope.role_turns},
            'phase_turns': {phase: _share(cap) for phase, cap in envelope.phase_turns},
            'wall_seconds': _share(envelope.wall_seconds)}


def extended(envelope, extension):
    """``envelope`` with ``extension`` (``increments``) added to its turn caps and its time;
    ``envelope`` itself without one. A role or a phase the extension does not name keeps its
    cap; the dollars are never extended."""
    if not extension:
        return envelope
    roles, phases = extension.get('role_turns', {}), extension.get('phase_turns', {})
    return dataclasses.replace(
        envelope, turns=envelope.turns + extension.get('turns', 0),
        role_turns=tuple((r, c + roles.get(r, 0)) for r, c in envelope.role_turns),
        phase_turns=tuple((p, c + phases.get(p, 0)) for p, c in envelope.phase_turns),
        wall_seconds=envelope.wall_seconds + extension.get('wall_seconds', 0))


def envelope_of(budget, journal, story, turns):
    """``(frozen, grant, envelope)`` of ``story`` among ``turns``: its frozen envelope
    (``frozen``), the ``grant`` of the journal that extended it (``TurnJournal.extension``,
    #411) or None, and the envelope that applies, the frozen one with that extension."""
    base = frozen(budget, story, turns)
    grant = journal.extension(story['story'])
    return base, grant, extended(base, grant['extension'] if grant else None)


def extension_text(extension):
    """The extension of an envelope (``increments``) in Italian, for comments."""
    parts = [f'turni +{extension["turns"]}']
    parts += [f'turni `{r}` +{n}' for r, n in extension['role_turns'].items()]
    parts += [f'fase `{p}` +{n}' for p, n in extension['phase_turns'].items()]
    parts.append(f'tempo +{extension["wall_seconds"]} s')
    return ', '.join(parts)


def state(story, turns, live, start, wall_seconds):
    """The state of ``core.budget`` of ``story`` (``TurnJournal.stories``): the turns booked for
    it among ``turns`` (``TurnJournal.turns``), ``live`` those still running in this process,
    and its deadline, ``wall_seconds`` after ``start`` (``started``)."""
    rows = {}
    for turn in turns.values():
        booked = turn.get('budget')
        if not booked or booked.get('story') != story['story']:
            continue
        outcome = turn['outcome']
        if outcome == UNCERTAIN and turn['reconciled'] is not None:
            outcome = turn['reconciled']['outcome']
        if outcome is None:
            status = RUNNING if turn['turn'] in live else UNCERTAIN
        else:
            status = STATUS[outcome]
        row = {'role': booked['role'], 'phase': booked['phase'], 'engine': booked['engine'],
               'status': status, 'usage': dict(turn.get('usage') or NO_USAGE)}
        for key in ('usage_model', 'conservative_usage'):
            if key in booked:
                row[key] = booked[key]
        rows[turn['turn']] = row
    return {'started_at': start, 'deadline_at': start + wall_seconds, 'invocations': rows}


def _count(rows, cap):
    """A turn dimension of ``view``: ``consumed`` the turns with a confirmed outcome,
    ``reserved`` those booked without one (running, uncertain or without outcome), both
    charged; ``available`` what is left of ``cap``, None without a cap."""
    reserved = sum(r['status'] not in CONFIRMED for r in rows)
    return {'consumed': len(rows) - reserved, 'reserved': reserved, 'cap': cap,
            'available': None if cap is None else max(cap - len(rows), 0)}


def _dollars(rows, kind, cap):
    """A dollar dimension of ``view``. ``unattested``: the turns that report no value of
    ``kind`` (a turn not dispatched spent nothing); ``uncovered``: those of them without a
    reserve of ``kind`` either. ``consumed``: the sum of the observed values; None when some
    turn reports none and no turn reports one, never 0 for nothing; with ``unattested``, a lower
    bound. ``reserved``: the part of the reserves no observation covers. ``available``: what the
    envelope leaves of ``cap``; None while a turn is ``uncovered``: it is not known."""
    def seen(row):
        return row['usage'].get(kind)

    unattested = [r for r in rows if seen(r) is None and r['status'] != STATUS[NOT_DISPATCHED]]
    uncovered = sum(kind not in r.get('conservative_usage', {}) for r in unattested)
    known = [seen(r) for r in rows if seen(r) is not None]
    charged = sum(core.kind_charge(r, kind) for r in rows)
    return {'consumed': float(sum(known)) if known or not unattested else None,
            'unattested': len(unattested), 'uncovered': uncovered,
            'reserved': float(sum(core.kind_charge(r, kind) - (seen(r) or 0) for r in rows)),
            'cap': cap, 'available': None if uncovered else float(max(cap - charged, 0))}


def view(budget, story, current, now, role=None, extension=None):
    """The state of the budget of ``story`` (#393): its ``current`` state (``state``) against
    its envelope ``budget`` (``envelope_of``) at ``now``, per dimension: the turns in total, per
    role and per kind of phase, the dollars of each kind and the time. ``role``: a role shown
    even without a cap of its own (the one of a refused turn). A value that is not attested is
    None, never 0. ``extension``: the extension ``budget`` includes (#411, ``increments``), or
    None. The data of ``describe``, of ``devloop status`` and of the PR receipt."""
    rows = list(current['invocations'].values())
    roles = list(budget.role_turns)
    if role is not None and role not in dict(roles):
        roles.append((role, None))
    now = int(now)
    start, deadline = current['started_at'], current['deadline_at']
    return {
        'story': story['story'], 'issue': story['issue'], 'at': _iso(now),
        'turns': _count(rows, budget.turns),
        'roles': {name: _count([r for r in rows if r['role'] == name], cap)
                  for name, cap in roles},
        'phases': {name: _count([r for r in rows if r['phase'] == name], cap)
                   for name, cap in budget.phase_turns},
        'usd': {kind: _dollars(rows, kind, getattr(budget, kind)) for kind in core.COST_KINDS},
        'time': {'started_at': _iso(start), 'deadline_at': _iso(deadline),
                 'cap': budget.wall_seconds,
                 'consumed': min(max(now - start, 0), budget.wall_seconds),
                 'available': max(deadline - now, 0)},
        'extension': extension}


def _count_text(label, count):
    cap = 'nessun tetto' if count['cap'] is None else count['cap']
    available = ('' if count['available'] is None
                 else f', disponibili {count["available"]}')
    return (f'{label} {count["consumed"] + count["reserved"]} di {cap} (consumati '
            f'{count["consumed"]}, prenotati {count["reserved"]}{available})')


def usd(value):
    """A dollar value as the budget shows it: ``non attestato`` when it is None, never 0."""
    return NOT_ATTESTED if value is None else f'{value:.2f} USD'


def consumed_usd(dollars):
    """The dollars consumed in one kind (``view``) as the budget shows them: ``non attestato``,
    a lower bound (``almeno``) while some turn attests no value, or the sum."""
    if dollars['consumed'] is not None and dollars['unattested']:
        return f'almeno {usd(dollars["consumed"])}'
    return usd(dollars['consumed'])


def describe(found):
    """The state of the budget of a story (``view``) on one line, in Italian, as the refusal
    of a turn and ``devloop status`` show it."""
    parts = [_count_text('turni', found['turns'])]
    parts += [_count_text(f'turni `{name}`', c) for name, c in found['roles'].items()]
    parts += [_count_text(f'fase `{name}`', c) for name, c in found['phases'].items()]
    for kind, d in found['usd'].items():
        gap = (f' (turni senza valore attestato: {d["unattested"]})' if d['unattested']
               else '')
        parts.append(f'`{kind}` consumati {consumed_usd(d)}{gap}, prenotati '
                     f'{usd(d["reserved"])}, disponibili {usd(d["available"])} di '
                     f'{usd(d["cap"])}')
    t = found['time']
    parts.append(f'tempo trascorso {t["consumed"]} s di {t["cap"]} s, disponibili '
                 f'{t["available"]} s, scadenza {t["deadline_at"]}')
    if found.get('extension'):
        parts.append('tetti già estesi una volta dal supervisore (`grant-budget`): '
                     + extension_text(found['extension']))
    return '; '.join(parts)


def no_story(issue):
    """Why issue ``issue``, held by no story of the journal, gets no turn with the budget on
    (#396): the refusal (``SCOPE_REQUIRED``) and ``devloop status`` say the same."""
    return (f'l\'issue #{issue} non appartiene a nessuna storia del journal: una storia nasce '
            'alla presa in carico con `story_identity` acceso, e un\'issue presa in carico prima '
            'resta esclusa dal budget, senza migrazione (`SCOPE_REQUIRED`). Con il budget acceso '
            'nessun suo turno parte, e un turno senza storia non si addebita a nessun envelope; '
            'decide l\'operatore')


def of_issue(budget, journal, repo, issue):
    """The state of the budget (``view``) of the story of issue ``issue`` of ``repo`` as the
    journal records it now, or None when the issue belongs to no story. It only reads the
    journal: a booked turn without outcome counts as reserved, running or not."""
    found = journal.story_of(repo, issue)
    if found is None:
        return None
    story = found[0]
    turns = journal.turns()
    now = journal.seconds()
    _, grant, envelope = envelope_of(budget, journal, story, turns)
    current = state(story, turns, set(), started(story, turns, now), envelope.wall_seconds)
    return view(envelope, story, current, now,
                extension=grant['extension'] if grant else None)


def status_lines(budget, journal, repo, issues):
    """``devloop status`` (#393): with the budget on, one line per story of ``issues`` (the
    numbers of the issues in progress and of those of the open PRs), each story once, and for
    an issue of no story the reason its turns are refused (``no_story``, #396); nothing with
    the budget off."""
    if not budget.enabled:
        return []
    lines, seen = [], set()
    for issue in dict.fromkeys(i for i in issues if i is not None):
        found = of_issue(budget, journal, repo, issue)
        if found is None:
            lines.append(f'budget #{issue}: {no_story(issue)}')
        elif found['story'] not in seen:
            seen.add(found['story'])
            lines.append(f'budget {found["story"]} (radice #{found["issue"]}, al '
                         f'{found["at"]}): {describe(found)}')
    return lines


def reserve(budget, journal, *, repo, issue, kind, agent, extension=None):
    """The booking of a turn of ``kind`` of ``agent`` (``AgentConfig``) on the work of issue
    ``issue`` of ``repo``, in the envelope of its story, for ``TurnJournal.intent``; raises
    ``BudgetRefused`` when the envelope does not cover it. Called under the journal's lock.
    The messages end without a full stop: the escalation adds it (``_failure_text``).

    The envelope is the frozen one with the extension the journal records for the story
    (``envelope_of``, #411); ``extension``, when given, is checked instead of it, and nothing
    is written (``grantable``). The booking records the frozen limits, never the extension.

    A role without a cap in the envelope, configured after the first booking of the story, is
    refused as ``ROLE_NOT_IN_ENVELOPE`` (#396): the story keeps its envelope, with no default
    cap added."""
    found = journal.story_of(repo, issue)
    where = f'il turno `{kind}` di `{agent.key}` (modello `{agent.model}`) sull\'issue #{issue}'
    if found is None:
        raise BudgetRefused(f'{where} non è coperto dall\'envelope di budget: {no_story(issue)}',
                            'SCOPE_REQUIRED', kind=kind, agent=agent.key)
    story = found[0]
    turns = journal.turns()
    now = journal.seconds()
    base, grant, envelope = envelope_of(budget, journal, story, turns)
    granted = grant['extension'] if grant else None
    if extension is not None:
        envelope, granted = extended(base, extension), extension
    start = started(story, turns, now)

    def refused(message, category):
        return BudgetRefused(message, category, story=story['story'], kind=kind,
                             agent=agent.key)

    if kind not in dict(envelope.role_turns):
        configured = dict(budget.role_turns).get(kind)
        later = ('' if configured is None else
                 f' La configurazione attuale gli dà {configured} turni, che valgono solo per '
                 'le storie nuove.')
        raise refused(
            f'{where} non è coperto dall\'envelope di budget della storia `{story["story"]}` '
            f'(radice #{story["issue"]}): il ruolo `{kind}` non ha un tetto nell\'envelope '
            'fissato alla sua prima prenotazione, perché è stato configurato dopo '
            f'(`{ROLE_NOT_IN_ENVELOPE}`).{later} La storia conserva il suo envelope, e nessun '
            'tetto predefinito si aggiunge: sarebbe un valore di budget. Nessun turno parte; '
            'decide l\'operatore (ADR 0004, D2)', ROLE_NOT_IN_ENVELOPE)
    current = state(story, turns, journal.live(), start, envelope.wall_seconds)
    rule = budget.rule(agent.key)
    try:
        row = core.check_capacity({'policy': policy(envelope, agent.kind, rule), 'binding': {}},
                                  current, kind, IMPLEMENTATION, agent.kind, now=now)
    except core.Refused as exc:
        kept = ('' if limits(base, start) == limits(budget, start) else
                ' La configurazione attuale ha limiti diversi, che valgono solo per le storie '
                'nuove: questa storia conserva quelli fissati alla sua prima prenotazione.')
        state_now = view(envelope, story, current, journal.seconds(), kind, extension=granted)
        raise refused(
            f'{where} non è coperto dall\'envelope di budget della storia `{story["story"]}` '
            f'(radice #{story["issue"]}): `{exc.category}`, {exc.message}. Stato del budget '
            f'della storia al rifiuto: {describe(state_now)}.{kept} '
            'Nessun turno parte, e nessuna '
            'prenotazione viene rimborsata: un turno fallito, non avviato o incerto resta '
            'addebitato. Un turno incerto si riconcilia con `/devloop reconcile`; i valori '
            'dell\'envelope e le sue estensioni sono decisioni dell\'operatore (ADR 0004, D2), '
            'salvo l\'unica estensione in turni e tempo che il supervisore può concedere con la '
            'delega `[delegations] budget` (ADR 0005)', exc.category) from exc
    booking = {'story': story['story'], 'role': kind, 'phase': IMPLEMENTATION,
               'engine': agent.kind, 'envelope': limits(base, start)}
    for key in ('usage_model', 'conservative_usage'):
        if key in row:
            booking[key] = row[key]
    return booking


def grantable(budget, journal, *, repo, issue, kind, agent):
    """``(story, extension, why)`` of the one extension of the envelope of the story of issue
    ``issue`` of ``repo`` (#411, ADR 0005, P5-7), for a turn of ``kind`` of ``agent``
    (``AgentConfig``) that it refuses: ``extension`` (``increments`` of the frozen envelope)
    when the delegation can apply, otherwise None and ``why``, in Italian. ``story``: the id of
    the story, None for an issue of no story. It only reads the journal.

    The extension applies only when the story was never extended, the envelope refuses the turn
    now at a limit (``LIMIT_REACHED``), and the envelope with the extension would cover it now:
    an extension that adds nothing to the exhausted limit (25% of 3 turns, rounded down, is 0),
    a limit in dollars, which it never extends, or an outcome still uncertain make it useless,
    so it is not granted and the story keeps its extension."""
    found = journal.story_of(repo, issue)
    if found is None:
        return None, None, no_story(issue)
    story = found[0]['story']
    grant = journal.extension(story)
    if grant is not None:
        return story, None, (f'la storia `{story}` ha già avuto la sua unica estensione '
                             f'({extension_text(grant["extension"])}): una seconda è '
                             'dell\'operatore')
    try:
        reserve(budget, journal, repo=repo, issue=issue, kind=kind, agent=agent)
    except BudgetRefused as exc:
        if exc.category != 'LIMIT_REACHED':
            return story, None, (f'il turno è rifiutato come `{exc.category}`, non per un '
                                 'limite raggiunto: un\'estensione non lo risolve')
    else:
        return story, None, 'l\'envelope della storia copre già il turno, senza estensione'
    extension = increments(frozen(budget, found[0], journal.turns()))
    try:
        reserve(budget, journal, repo=repo, issue=issue, kind=kind, agent=agent,
                extension=extension)
    except BudgetRefused as exc:
        return story, None, (
            f'con l\'estensione ({extension_text(extension)}: il {BUDGET_TOLERANCE_PERCENT}% '
            'di ogni tetto di turni e del tempo, arrotondato per difetto, mai dollari né token) '
            f'l\'envelope non coprirebbe comunque il turno `{kind}` (`{exc.category}`): '
            'l\'estensione non si concede e la storia la conserva')
    return story, extension, ''
