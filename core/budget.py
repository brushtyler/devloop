"""Pure validation of a limits profile and pure charge and capacity calculations (ADR 0004, P2-1).

These are the rules of the engine's ``LimitsStore`` (in the tag ``ssdlc-engine-final``, ADR 0007),
written without I/O, lock or clock: the caller passes the profile, the state of the invocations
and the current time. The profile has the shape of the engine's limits schema
(``recovery-limits.schema.json``, a fixture of core's tests since ADR 0007, R1), which the caller
reads and passes as data.

Rules kept from the engine:

- an agent that reports no dollar cost needs a declared rule for unknown usage,
  ``reserve_per_invocation`` (a positive, finite reserve booked before the call) or
  ``unmetered_quota``; without one its call is refused before launch. A reserve in USD is never
  derived for it, from tokens or from anything else: only the declared rule applies;
- a value that was not reported is unknown, never 0: a returned call with no dollar cost and no
  declared rule blocks the next one;
- a reserve stays charged, and an observed cost can only raise it;
- an invocation whose outcome is not confirmed (``reserved`` or ``uncertain``) blocks any new one;
- tokens are never priced, and the tokens of different agents are never added or compared.

Not covered here: the feedback sections of a profile (``feedback_workflow``,
``feedback_delegation``), which only the engine validates, and everything with state, such as
the journal, the lock and the deadline extensions.
"""
import math

from . import schema as _schema

COST_KINDS = ('metered_usd', 'cli_estimate_usd')
# Agents whose adapters can report a dollar value, and which one. Any other agent never
# reports one, so dispatching it needs an explicit rule for unknown usage.
REPORTED_COST = {'claude': 'cli_estimate_usd', 'openrouter': 'metered_usd'}
RESERVE, UNMETERED = 'reserve_per_invocation', 'unmetered_quota'
USAGE_MODELS = (RESERVE, UNMETERED)
TOKEN_KEYS = ('input_tokens', 'output_tokens')
# The categories of a refusal, with the meaning the engine gives them.
CATEGORIES = ('LIMIT_REACHED', 'OUTCOME_UNCERTAIN', 'SCOPE_REQUIRED')
UNCONFIRMED = ('reserved', 'uncertain')


class Refused(Exception):
    """The imminent call does not fit the profile; ``category`` is one of ``CATEGORIES``."""

    def __init__(self, category, message):
        if category not in CATEGORIES:
            raise ValueError('Unregistered refusal category')
        super().__init__(f'{category}: {message}')
        self.category = category
        self.message = message


def validate_profile(policy, profile_schema):
    """Raise ``ValueError`` unless ``policy`` is a valid limits profile; return it.

    ``profile_schema`` is the content of the limits schema (``recovery-limits.schema.json``).
    Beyond the schema, the dollar caps must be finite and the rules for unknown usage valid."""
    _schema.validate(profile_schema, policy)
    if not all(math.isfinite(policy[kind]) for kind in COST_KINDS):
        raise ValueError('Nonfinite limit')
    validate_live_usage(policy)
    return policy


def validate_live_usage(policy):
    """Declared rules for unknown usage are explicit and finite; an absent rule is not zero."""
    try:
        for rule in policy.get('live_unknown_usage', {}).values():
            if rule['model'] not in USAGE_MODELS:
                raise ValueError('Unknown live unknown-usage model')
            if not rule['reason'].strip():
                raise ValueError('Live unknown-usage rule needs a reason')
            if rule['model'] == RESERVE and not all(
                    type(rule[kind]) in (int, float) and math.isfinite(rule[kind]) and rule[kind] > 0
                    for kind in COST_KINDS):
                raise ValueError('Nonfinite live unknown-usage reserve')
    except (KeyError, TypeError, AttributeError):
        raise ValueError('Malformed live unknown-usage rule') from None


def usage_rule(policy, agent):
    """The declared rule of ``agent`` for unknown usage, or ``None`` if it reports its own cost.

    Refused before launch when the agent reports no dollar cost and the profile declares no
    rule for it: no reserve is derived in its place."""
    rule = policy.get('live_unknown_usage', {}).get(agent)
    if rule is None and agent not in REPORTED_COST:
        raise Refused('LIMIT_REACHED', 'Engine reports no dollar cost and the profile declares no live '
                      'unknown-usage model; dispatch refused before launch')
    return rule


def uncovered(row):
    """A live call that may have spent dollars, reported none and had no declared rule."""
    return (row.get('status') in ('returned', 'terminated') and 'usage_model' not in row
            and all(row.get('usage', {}).get(kind) is None for kind in COST_KINDS))


def kind_charge(row, kind):
    """What one invocation charges to the cap of one cost kind."""
    observed = row.get('usage', {}).get(kind) or 0
    reserve = row.get('conservative_usage', {}).get(kind, 0)
    if row.get('usage_model') == RESERVE:
        # The pre-dispatch reserve stays charged; an observation can only raise it.
        return max(observed, reserve)
    return observed + reserve


def combined_charge(row, policy):
    """What one invocation charges to the legacy combined cap."""
    known = sum(value for value in row.get('usage', {}).values() if value is not None)
    reserves = row.get('conservative_usage', {}).values()
    if row.get('usage_model') == RESERVE:
        # One live invocation belongs to one cost kind (exclusive_cost_kind_v1).
        return max(known, max(reserves, default=0))
    return known + (max(reserves, default=0) if policy.get('unknown_usage_reserve_model') == 'exclusive_cost_kind_v1'
                    else sum(reserves))


def accounted_total(state, policy):
    """Known usage plus the reserves of the profile; unknown observations stay unknown."""
    return sum(combined_charge(row, policy) for row in state['invocations'].values())


def check_dollars(state, policy, rows, *, reserving=False):
    """Existing rows exhaust a cap at equality; a new reserve may only fill it."""
    exceeded = (lambda total, cap: total > cap) if reserving else (lambda total, cap: total >= cap)
    combined = state.get('combined_reported_usd_cap')
    if combined is not None and exceeded(sum(combined_charge(row, policy) for row in rows), combined):
        raise Refused('LIMIT_REACHED', 'Live unknown-usage reserve would exceed the legacy combined '
                      'reported-cost cap' if reserving else 'Legacy combined reported-cost cap exhausted')
    for kind in COST_KINDS:
        if exceeded(sum(kind_charge(row, kind) for row in rows), policy[kind]):
            raise Refused('LIMIT_REACHED', ('Live unknown-usage reserve would exceed the frozen ' + kind + ' cap')
                          if reserving else 'Frozen ' + kind + ' cap exhausted')


def check(state, policy, now, *, for_dispatch=True):
    """Refuse a new call on ``state`` at time ``now``: deadline, unconfirmed outcomes, dollars."""
    if now < state['started_at']:
        raise Refused('OUTCOME_UNCERTAIN', 'Clock moved behind authorization; deadline cannot be extended')
    if now >= state['deadline_at']:
        raise Refused('LIMIT_REACHED', 'Frozen ticket wall-time exhausted')
    if any(row['status'] in UNCONFIRMED for row in state['invocations'].values()):
        raise Refused('OUTCOME_UNCERTAIN', 'Prior invocation has no confirmed outcome; no redispatch')
    if not for_dispatch:
        return
    rows = list(state['invocations'].values())
    for row in rows:
        if row.get('status') == 'historical' and all(
                row.get('usage', {}).get(kind) is None for kind in COST_KINDS):
            reserve = row.get('conservative_usage', {})
            if any(type(reserve.get(kind)) not in (int, float) or not math.isfinite(reserve[kind])
                   or reserve[kind] <= 0 for kind in COST_KINDS):
                raise Refused('LIMIT_REACHED', 'Unknown historical usage has no conservative dollar reserve')
    exempt = set(state.get('legacy_uncovered_invocations') or ())
    if any(uncovered(row) and identity not in exempt for identity, row in state['invocations'].items()):
        raise Refused('LIMIT_REACHED', 'Live invocation reported no dollar cost and its engine has no '
                      'declared live unknown-usage model; unknown is not counted as zero')
    check_dollars(state, policy, rows)


def check_capacity(envelope, state, role, phase, engine=None, *, now):
    """Validate the imminent call of ``role`` in ``phase`` on ``engine`` and return its row.

    ``envelope`` is ``{'policy': ..., 'binding': ...}`` and ``state`` the invocations already
    booked. The row is the one the engine would book: with the declared rule of the agent and,
    for ``reserve_per_invocation``, its reserve, which must fit the dollar caps. Without
    ``engine`` only the invocation caps are checked and ``None`` is returned."""
    policy = envelope['policy']
    check(state, policy, now)
    allowed = envelope['binding'].get('allowed_phases')
    if allowed is not None and phase not in allowed:
        raise Refused('SCOPE_REQUIRED', 'Continuation phase is outside the authorized execution scope')
    if role not in policy['role_invocations'] or phase not in policy['phase_invocations']:
        raise Refused('LIMIT_REACHED', 'Missing explicit role/phase invocation cap')
    rows = state['invocations'].values()
    if (len(state['invocations']) >= policy['invocations']
            or sum(row['role'] == role for row in rows) >= policy['role_invocations'][role]
            or sum(row['phase'] == phase for row in rows) >= policy['phase_invocations'][phase]):
        raise Refused('LIMIT_REACHED', 'Frozen invocation allowance exhausted')
    if engine is None:
        return None
    rule = usage_rule(policy, engine)
    row = {'role': role, 'phase': phase, 'engine': engine, 'status': 'reserved',
           'usage': {'metered_usd': None, 'cli_estimate_usd': None}}
    if rule is not None:
        row['usage_model'] = rule['model']
    if rule is not None and rule['model'] == RESERVE:
        row['conservative_usage'] = {kind: rule[kind] for kind in COST_KINDS}
        check_dollars(state, policy, [*rows, row], reserving=True)
    return row


def agent_tokens(state):
    """Reported tokens per agent: never added, compared or priced across agents.

    A count that no invocation of the agent reported stays ``None``, never 0."""
    totals = {}
    for row in state['invocations'].values():
        entry = totals.setdefault(row.get('engine') or 'unknown', dict.fromkeys(TOKEN_KEYS))
        reported = row.get('reported_tokens') or {}
        for key in TOKEN_KEYS:
            if reported.get(key) is not None:
                entry[key] = (entry[key] or 0) + reported[key]
    return dict(sorted(totals.items()))
