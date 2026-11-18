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
  the estimate claude reports. Token caps are not part of it.
- **The rule of each agent** for unknown usage, as in the engine: ``unmetered_quota`` (D2, for
  agy and codex, the only rule they accept: no reserve in USD, only the turn caps) or, for claude
  only, ``reserve_per_invocation`` (a reserve in both dollar kinds, booked before the turn). An
  agent whose CLI reports no dollar figure has no turn without one: the configuration is refused
  at startup. The rule is the agent's and
  covers every model of its chain: a fallback runs on the same CLI.
- **The booking** is written in the intent of the turn in the journal (``TurnJournal.intent``),
  in the same event and under the same lock as the check: the journal is the authority (D7).
- **The envelope is frozen** at the first booking of a story: every booking records the limits
  of its story (``envelope``: the turn and dollar caps, ``wall_seconds`` and the deadline), and
  the later ones read them from the journal, across restarts. A changed configuration applies
  to new stories only: it never extends an existing one, an operator's decision case by case.
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
"""

import calendar
import dataclasses
import math
import time

from core import budget as core

from .journal import FAILED, IMPLEMENTATION, NOT_DISPATCHED, RETURNED, UNCERTAIN, TurnBlocked

BUDGET = 'budget'  # the class of the escalation of a refused turn
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
    ``core.budget`` (``LIMIT_REACHED``, ``OUTCOME_UNCERTAIN``, ``SCOPE_REQUIRED``)."""

    def __init__(self, message, category):
        super().__init__(message, [])
        self.category = category


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


def limits(budget, story):
    """The limits of ``budget`` for ``story`` (``TurnJournal.stories``) as a booking records
    them (``envelope``): the turn caps, the dollar caps, ``wall_seconds`` and the deadline it
    gives the story."""
    return {'turns': budget.turns, 'role_turns': dict(budget.role_turns),
            'phase_turns': dict(budget.phase_turns), 'wall_seconds': budget.wall_seconds,
            'deadline_at': _iso(_seconds(story['at']) + budget.wall_seconds),
            'metered_usd': budget.metered_usd, 'cli_estimate_usd': budget.cli_estimate_usd}


def frozen(budget, story, turns):
    """The envelope of ``story``: the limits recorded by its first booking among ``turns``
    (``TurnJournal.turns``), as a ``config.BudgetPolicy`` with the rules of ``budget``; before
    its first booking, ``budget`` itself. A later configuration applies to new stories only:
    it never extends, nor shrinks, the envelope of a story already booked."""
    for turn in turns.values():
        booked = turn.get('budget')
        if booked and booked.get('story') == story['story'] and 'envelope' in booked:
            found = booked['envelope']
            return dataclasses.replace(
                budget, turns=found['turns'], role_turns=tuple(found['role_turns'].items()),
                phase_turns=tuple(found['phase_turns'].items()),
                wall_seconds=found['wall_seconds'], metered_usd=found['metered_usd'],
                cli_estimate_usd=found['cli_estimate_usd'])
    return budget


def state(story, turns, live, wall_seconds):
    """The state of ``core.budget`` of ``story`` (``TurnJournal.stories``): the turns booked for
    it among ``turns`` (``TurnJournal.turns``), ``live`` those still running in this process,
    and its deadline, ``wall_seconds`` after its start."""
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
    start = _seconds(story['at'])
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


def view(budget, story, current, now, role=None):
    """The state of the budget of ``story`` (#393): its ``current`` state (``state``) against
    its envelope ``budget`` (``frozen``) at ``now``, per dimension: the turns in total, per
    role and per kind of phase, the dollars of each kind and the time. ``role``: a role shown
    even without a cap of its own (the one of a refused turn). A value that is not attested is
    None, never 0. The data of ``describe``, of ``devloop status`` and of the PR receipt."""
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
                 'available': max(deadline - now, 0)}}


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
    return '; '.join(parts)


def of_issue(budget, journal, repo, issue):
    """The state of the budget (``view``) of the story of issue ``issue`` of ``repo`` as the
    journal records it now, or None when the issue belongs to no story. It only reads the
    journal: a booked turn without outcome counts as reserved, running or not."""
    found = journal.story_of(repo, issue)
    if found is None:
        return None
    story = found[0]
    turns = journal.turns()
    envelope = frozen(budget, story, turns)
    current = state(story, turns, set(), envelope.wall_seconds)
    return view(envelope, story, current, journal.seconds())


def status_lines(budget, journal, repo, issues):
    """``devloop status`` (#393): with the budget on, one line per story of ``issues`` (the
    numbers of the issues in progress and of those of the open PRs), each story once; nothing
    with the budget off."""
    if not budget.enabled:
        return []
    lines, seen = [], set()
    for issue in dict.fromkeys(i for i in issues if i is not None):
        found = of_issue(budget, journal, repo, issue)
        if found is None:
            lines.append(f'budget #{issue}: nessuna storia nel journal, nessun envelope')
        elif found['story'] not in seen:
            seen.add(found['story'])
            lines.append(f'budget {found["story"]} (radice #{found["issue"]}, al '
                         f'{found["at"]}): {describe(found)}')
    return lines


def reserve(budget, journal, *, repo, issue, kind, agent):
    """The booking of a turn of ``kind`` of ``agent`` (``AgentConfig``) on the work of issue
    ``issue`` of ``repo``, in the envelope of its story, for ``TurnJournal.intent``; raises
    ``BudgetRefused`` when the envelope does not cover it. Called under the journal's lock.
    The messages end without a full stop: the escalation adds it (``_failure_text``)."""
    found = journal.story_of(repo, issue)
    where = f'il turno `{kind}` di `{agent.key}` (modello `{agent.model}`) sull\'issue #{issue}'
    if found is None:
        raise BudgetRefused(
            f'{where} non è coperto dall\'envelope di budget: l\'issue non appartiene a nessuna '
            'storia del journal (una storia nasce alla presa in carico, con `story_identity`), '
            'e un turno senza storia non si addebita a nessun envelope. Nessun turno parte; '
            'decide l\'operatore', 'SCOPE_REQUIRED')
    story = found[0]
    turns = journal.turns()
    envelope = frozen(budget, story, turns)
    current = state(story, turns, journal.live(), envelope.wall_seconds)
    rule = budget.rule(agent.key)
    try:
        row = core.check_capacity({'policy': policy(envelope, agent.kind, rule), 'binding': {}},
                                  current, kind, IMPLEMENTATION, agent.kind,
                                  now=journal.seconds())
    except core.Refused as exc:
        kept = ('' if limits(envelope, story) == limits(budget, story) else
                ' La configurazione attuale ha limiti diversi, che valgono solo per le storie '
                'nuove: questa storia conserva quelli fissati alla sua prima prenotazione.')
        raise BudgetRefused(
            f'{where} non è coperto dall\'envelope di budget della storia `{story["story"]}` '
            f'(radice #{story["issue"]}): `{exc.category}`, {exc.message}. Stato del budget '
            f'della storia al rifiuto: '
            f'{describe(view(envelope, story, current, journal.seconds(), kind))}.{kept} '
            'Nessun turno parte, e nessuna '
            'prenotazione viene rimborsata: un turno fallito, non avviato o incerto resta '
            'addebitato. Un turno incerto si riconcilia con `/devloop reconcile`; i valori '
            'dell\'envelope e le sue estensioni sono decisioni dell\'operatore (ADR 0004, D2)',
            exc.category) from exc
    booking = {'story': story['story'], 'role': kind, 'phase': IMPLEMENTATION,
               'engine': agent.kind, 'envelope': limits(envelope, story)}
    for key in ('usage_model', 'conservative_usage'):
        if key in row:
            booking[key] = row[key]
    return booking
