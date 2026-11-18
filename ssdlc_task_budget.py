"""Per-task budget of a bounded run: attribution, allocation and reallocation (#163).

Decision D4 of ADR 0004, taken by the operator:

- every reservation and every consumption names the task it belongs to
  (``account: task`` and ``task_id``); a call that no task owns (design, plan,
  diagnosis, gates, feedback) stays on the shared run account (``account: run``),
  always counted in the run caps and never transferable to a task;
- only the cumulative dimensions are allocated and transferred: ``invocations``,
  ``role_invocations.<role>``, ``phase_invocations.<phase>``, ``metered_usd`` and
  ``cli_estimate_usd``. ``development_attempts`` stays a per-task cap and the
  deadline moves only with ``extend_deadline``;
- an allocation (``task_budget`` in the profile, versioned) gives some tasks a cap
  on their own cumulative consumption. The run caps stay the defaults and the
  overall limit: a task without an override is bound by them only;
- a new profile declares its allocation, so it is part of the profile hash. An
  existing run gets one only through the explicit ``allocate_task_budget``
  transition, which computes the quotas against the residual, lists what each
  task and the shared account already consumed, and reattributes nothing: the
  invocations that existed at the allocation (``prior_invocations``) never count
  against a task cap;
- ``reallocate_task_budget`` moves only the part of a donor's cap that is neither
  consumed nor committed (an open or uncertain reservation, or a reserve that
  stays charged) to another task with a cap in the same dimension. It creates no
  budget and refunds nothing.

Pure functions: no I/O, no lock, no clock. The limit-extension contract
(``ssdlc_limit_extensions``) authorizes and applies the transitions.
"""
import copy
import math
import re
from decimal import Decimal
from fractions import Fraction


CAPABILITY = 'task-budget-v1'
SCALARS = ('invocations', 'metered_usd', 'cli_estimate_usd')
MONEY = ('metered_usd', 'cli_estimate_usd')
FAMILIES = ('role_invocations', 'phase_invocations')
TASK = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
UNCONFIRMED = ('reserved', 'uncertain')
# Dollar caps and transfer amounts are decimals with at most PLACES places, so a
# transfer moves an exact amount and the sum of the caps is conserved. Run caps
# and recorded charges are read as the decimal their JSON number writes, every
# digit kept and never rounded, so equal recorded numbers compare equal.
PLACES = 6
SCALE = 10 ** PLACES


class TaskBudgetError(ValueError):
    pass


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def exact(value):
    """Exact value of a recorded dollar amount: the decimal the JSON number writes, every digit kept.

    Run caps, observations and reserves take the same conversion as the task
    caps, so equal recorded numbers compare equal: ``0.3`` is 3/10, never the
    binary approximation of the float.
    """
    if type(value) is int:
        return Fraction(value)
    return Fraction(Decimal(repr(value)))


def amount(value):
    """Exact value of a dollar cap or transfer amount: a decimal with at most PLACES places."""
    if type(value) is not int and Decimal(repr(value)).as_tuple().exponent < -PLACES:
        raise TaskBudgetError('A dollar task cap or transfer has at most %d decimal places' % PLACES)
    return exact(value)


def _decimal(value):
    """The JSON number of an exact dollar cap; refused when a float cannot carry it exactly."""
    if value.denominator == 1 and abs(value) < 2 ** 53:
        return int(value)
    result = float(value)
    if exact(result) != value:
        raise TaskBudgetError('A dollar task cap cannot be represented exactly')
    return result


def _up(value):
    """An exact dollar amount shown with PLACES places, never below it (consumption)."""
    return float(Fraction(math.ceil(value * SCALE), SCALE))


def _down(value):
    """An exact dollar amount shown with PLACES places, never above it (availability)."""
    return float(Fraction(math.floor(value * SCALE), SCALE))


def flat(caps):
    """``{'invocations': 3, 'role_invocations': {'03-dev': 2}}`` as ``{'invocations': 3, 'role_invocations.03-dev': 2}``."""
    result = {}
    for key, value in caps.items():
        if key in FAMILIES:
            for name, cap in value.items():
                result[key + '.' + name] = cap
        else:
            result[key] = value
    return result


def nest(values):
    caps = {}
    for dimension, value in values.items():
        family, _, name = dimension.partition('.')
        if family in FAMILIES:
            caps.setdefault(family, {})[name] = value
        else:
            caps[dimension] = value
    return caps


def run_cap(policy, dimension):
    """The run cap of a dimension (the default every task without an override has)."""
    family, _, name = dimension.partition('.')
    if dimension in SCALARS:
        return policy[dimension]
    if family in FAMILIES and name:
        return policy[family].get(name)
    raise TaskBudgetError('Not a transferable dimension: ' + str(dimension)[:80])


def validate(policy):
    """Cross-field rules of ``task_budget`` beyond the schema; absent is valid."""
    budget = policy.get('task_budget')
    if budget is None:
        return
    try:
        if budget['capability'] != CAPABILITY or budget['schema_version'] != 1:
            raise TaskBudgetError('Unsupported task budget')
        prior = budget.get('prior_invocations', [])
        if prior != sorted(set(prior)):
            raise TaskBudgetError('Prior invocations of a task budget are not unique and sorted')
        totals = {}
        for task_id, caps in budget['tasks'].items():
            if not TASK.match(task_id):
                raise TaskBudgetError('Invalid task identity in the task budget')
            for dimension, value in flat(caps).items():
                cap = run_cap(policy, dimension)
                if cap is None:
                    raise TaskBudgetError('A task cap needs the run cap of the same dimension: ' + dimension[:80])
                if not _number(value) or value < 0 or dimension not in MONEY and type(value) is not int:
                    raise TaskBudgetError('Invalid task cap: ' + dimension[:80])
                totals.setdefault(dimension, []).append(amount(value) if dimension in MONEY else value)
        for dimension, values in totals.items():
            if sum(values) > (exact(run_cap(policy, dimension)) if dimension in MONEY else run_cap(policy, dimension)):
                raise TaskBudgetError('Task caps exceed the run cap: ' + dimension[:80])
    except (KeyError, TypeError, AttributeError):
        raise TaskBudgetError('Malformed task budget') from None


def attribute(task_id):
    """The account fields of a new invocation row; ``None`` is the shared run account."""
    if task_id is None:
        return {'account': 'run'}
    if not isinstance(task_id, str) or not TASK.match(task_id):
        raise TaskBudgetError('Invalid task identity for a reservation')
    return {'account': 'task', 'task_id': task_id}


def account(row):
    """``task``, ``run`` (shared) or ``unattributed`` (a row recorded before attribution)."""
    if row.get('account') == 'task' and isinstance(row.get('task_id'), str):
        return 'task'
    if row.get('account') == 'run':
        return 'run'
    return 'unattributed'


def _charge(row, kind):
    """Exact dollar charge of ``row``: the rule of ``LimitsStore._kind_charge`` without float rounding."""
    from ssdlc_recovery import RESERVE
    observed = exact(row.get('usage', {}).get(kind) or 0)
    reserve = exact(row.get('conservative_usage', {}).get(kind, 0))
    if row.get('usage_model') == RESERVE:
        return max(observed, reserve)
    return observed + reserve


def _counts(row, dimension, role=None, phase=None):
    """Whether ``row`` (or the imminent call of ``role`` and ``phase``) counts in a count dimension."""
    family, _, name = dimension.partition('.')
    if dimension == 'invocations':
        return True
    if family == 'role_invocations':
        return (row.get('role') if row is not None else role) == name
    return (row.get('phase') if row is not None else phase) == name


def _measure(rows, dimensions):
    """Consumed and committed amounts of ``rows`` in ``dimensions``.

    An open or uncertain reservation is committed. So is a dollar reserve that
    stays charged after the call: an observation can only raise it. Dollar
    amounts are exact fractions, never rounded.
    """
    consumed = {dimension: 0 for dimension in dimensions}
    committed = {dimension: 0 for dimension in dimensions}
    for row in rows:
        open_row = row.get('status') in UNCONFIRMED
        for dimension in dimensions:
            if dimension in MONEY:
                charge = _charge(row, dimension)
                reserve = min(exact(row.get('conservative_usage', {}).get(dimension, 0)), charge)
                if open_row:
                    committed[dimension] += charge
                else:
                    committed[dimension] += reserve
                    consumed[dimension] += charge - reserve
            elif _counts(row, dimension):
                (committed if open_row else consumed)[dimension] += 1
    return consumed, committed


def _shown(values):
    """Consumed or committed amounts for display: dollars rounded up, never down."""
    return {d: _up(v) if d in MONEY else v for d, v in values.items()}


def _available(cap, consumed, committed, dimension):
    """Exact availability of a cap: what is neither consumed nor committed."""
    return (amount(cap) if dimension in MONEY else cap) - consumed - committed


def task_rows(policy, state, task_id):
    """Rows charged to ``task_id`` under the allocation: never those prior to it."""
    prior = set((policy.get('task_budget') or {}).get('prior_invocations', []))
    return [row for identity, row in state['invocations'].items()
            if identity not in prior and account(row) == 'task' and row['task_id'] == task_id]


def usage(policy, state, task_id):
    """Per dimension of the task's caps: cap, consumed, committed and available."""
    caps = flat(policy['task_budget']['tasks'][task_id])
    consumed, committed = _measure(task_rows(policy, state, task_id), caps)
    shown_consumed, shown_committed = _shown(consumed), _shown(committed)
    result = {}
    for dimension, cap in sorted(caps.items()):
        available = _available(cap, consumed[dimension], committed[dimension], dimension)
        # Dollars: consumption shown rounded up and availability down, so the
        # view never offers more than the exact amount a transfer may take.
        result[dimension] = dict(cap=cap, consumed=shown_consumed[dimension], committed=shown_committed[dimension],
                                 available=_down(available) if dimension in MONEY else available)
    return result


def exhausted(policy, state, task_id, role, phase, candidate=None):
    """Why the task cannot take the imminent call of ``role`` in ``phase``, or None.

    Only the caps the call counts in apply. Existing rows exhaust a cap at
    equality; a new dollar reserve may only fill it (the rule of the run caps).
    """
    budget = policy.get('task_budget')
    if task_id is None or budget is None or task_id not in budget['tasks']:
        return None
    rows = task_rows(policy, state, task_id)
    caps = flat(budget['tasks'][task_id])
    for dimension, cap in sorted(caps.items()):
        if dimension in MONEY or not _counts(None, dimension, role, phase):
            continue
        if sum(_counts(row, dimension) for row in rows) >= cap:
            return 'Task ' + task_id + ' exhausted its ' + dimension + ' cap'
    for kind in MONEY:
        if kind not in caps:
            continue
        total, cap = sum(_charge(row, kind) for row in rows), amount(caps[kind])
        if candidate is not None and candidate.get('conservative_usage'):
            if total + _charge(candidate, kind) > cap:
                return 'Live unknown-usage reserve would exceed the ' + kind + ' cap of task ' + task_id
        elif total >= cap:
            return 'Task ' + task_id + ' exhausted its ' + kind + ' cap'
    return None


def _run_used(policy, state):
    """Run-wide consumed plus committed amounts (exact) in every dimension a task can be capped in."""
    dimensions = list(SCALARS) + [family + '.' + name for family in FAMILIES for name in policy[family]]
    consumed, committed = _measure(list(state['invocations'].values()), dimensions)
    return {d: consumed[d] + committed[d] for d in dimensions}


def accounts(policy, state):
    """Consumption of the shared run account and of the rows recorded before attribution."""
    result = {}
    for name in ('run', 'unattributed'):
        rows = [row for row in state['invocations'].values() if account(row) == name]
        consumed, committed = _measure(rows, SCALARS)
        result[name] = dict(invocations=len(rows), consumed=_shown(consumed), committed=_shown(committed))
    return result


def view(policy, state, tasks=None, plan=None):
    """Read-only per-task view for proposals, inspect and status.

    ``defaults`` are the run caps; ``overrides`` the per-task caps of the
    allocation, never merged into the defaults. ``tasks`` (scheduler rows) and
    ``plan`` add the status and the dependencies of each task.
    """
    budget = policy.get('task_budget')
    defaults = {d: run_cap(policy, d) for d in list(SCALARS)
                + [family + '.' + name for family in FAMILIES for name in sorted(policy[family])]}
    names = set((budget or {}).get('tasks', {})) | set((tasks or {})) | set(_dependencies(plan))
    rows = {}
    for task_id in sorted(names):
        allocated = budget is not None and task_id in budget['tasks']
        rows[task_id] = dict(
            status=(tasks or {}).get(task_id, {}).get('status'),
            depends_on=_dependencies(plan).get(task_id, []),
            override=copy.deepcopy(budget['tasks'][task_id]) if allocated else None,
            usage=usage(policy, state, task_id) if allocated else None,
            invocations=len([row for row in state['invocations'].values()
                             if account(row) == 'task' and row['task_id'] == task_id]))
    return dict(capability=CAPABILITY, allocated=budget is not None, defaults=defaults,
                prior_invocations=len((budget or {}).get('prior_invocations', [])),
                transfers_enabled=budget is not None, tasks=rows, accounts=accounts(policy, state))


def _dependencies(plan):
    result = {}
    for task in (plan or {}).get('tasks', []):
        if isinstance(task, dict) and isinstance(task.get('task_id'), str):
            result[task['task_id']] = sorted(d for d in task.get('depends_on', []) if isinstance(d, str))
    return result


def _dependents(plan, roots):
    """Tasks that transitively depend on any of ``roots``."""
    graph = _dependencies(plan)
    found, changed = set(), True
    while changed:
        changed = False
        for task_id, needs in graph.items():
            if task_id not in found and (set(needs) & (set(roots) | found)):
                found.add(task_id)
                changed = True
    return sorted(found - set(roots))


def _changes(old_tasks, new_tasks):
    changes = []
    for task_id in sorted(set(old_tasks) | set(new_tasks)):
        before, after = flat(old_tasks.get(task_id, {})), flat(new_tasks.get(task_id, {}))
        for dimension in sorted(set(before) | set(after)):
            if before.get(dimension) != after.get(dimension):
                changes.append(dict(dimension='task_budget.%s.%s' % (task_id, dimension),
                                    old=before.get(dimension), new=after.get(dimension)))
    return changes


def effects(before, after, state, tasks, plan, changed):
    """What the transition does to every task: caps and availability before and after.

    The tasks it changes, the other pending tasks and the tasks that depend on a
    changed one are all listed, so the operator authorizes them with the proposal.
    """
    old, new = view(before, state, tasks, plan)['tasks'], view(after, state, tasks, plan)['tasks']
    dependents = set(_dependents(plan, changed))
    rows = {}
    for task_id in sorted(set(old) | set(new)):
        rows[task_id] = dict(status=new.get(task_id, old.get(task_id, {})).get('status'),
                             changed=task_id in changed, depends_on_changed=task_id in dependents,
                             override_before=old.get(task_id, {}).get('override'),
                             override_after=new.get(task_id, {}).get('override'),
                             available_before={d: u['available'] for d, u in (old.get(task_id, {}).get('usage') or {}).items()},
                             available_after={d: u['available'] for d, u in (new.get(task_id, {}).get('usage') or {}).items()})
    return rows


def allocation_changes(tasks):
    """The changes an allocation of ``tasks`` records: one per task and dimension, ``old`` null."""
    return _changes({}, tasks)


def allocate(policy, state, allocation, plan_tasks):
    """The ``task_budget`` of an existing run without one, from the residual.

    Returns the new profile, the changes and the facts the proposal shows. The
    invocations already recorded are listed and never charged to a task.
    """
    if policy.get('task_budget') is not None:
        raise TaskBudgetError('The run already has a task allocation; use reallocate_task_budget')
    if not isinstance(allocation, dict) or not allocation or len(allocation) > 200:
        raise TaskBudgetError('An allocation names at least one task and its caps')
    tasks = {}
    for task_id, caps in allocation.items():
        if not isinstance(task_id, str) or task_id not in plan_tasks:
            raise TaskBudgetError('An allocation names tasks of the plan: ' + str(task_id)[:80])
        if not isinstance(caps, dict) or not caps:
            raise TaskBudgetError('A task allocation names its caps')
        for key, value in caps.items():
            if key in FAMILIES:
                if not isinstance(value, dict) or not value:
                    raise TaskBudgetError('A family of task caps names its keys')
            elif key not in SCALARS:
                raise TaskBudgetError('Not a transferable dimension: ' + str(key)[:80])
        tasks[task_id] = copy.deepcopy(caps)
    profile = copy.deepcopy(policy)
    profile['task_budget'] = dict(schema_version=1, capability=CAPABILITY, tasks=tasks,
                                  prior_invocations=sorted(state['invocations']))
    validate(profile)
    used = _run_used(policy, state)
    residual = {}
    for dimension in sorted({d for caps in tasks.values() for d in flat(caps)}):
        money = dimension in MONEY
        # Exact comparison: the run cap less everything consumed or committed.
        left = (exact(run_cap(policy, dimension)) if money else run_cap(policy, dimension)) - used[dimension]
        total = sum(amount(c[dimension]) if money else c[dimension]
                    for c in map(flat, tasks.values()) if dimension in c)
        residual[dimension] = dict(run_cap=run_cap(policy, dimension),
                                   used=_up(used[dimension]) if money else used[dimension],
                                   residual=_down(left) if money else left,
                                   allocated=_decimal(total) if money else total)
        if total > left:
            raise TaskBudgetError('Task quotas exceed the residual of the run: ' + dimension[:80])
    facts = dict(allocation=copy.deepcopy(tasks), residual=residual,
                 prior_invocations=len(state['invocations']), prior_accounts=accounts(policy, state),
                 prior_by_task=_prior_by_task(state), reattributed=False)
    return profile, _changes({}, tasks), facts


def _prior_by_task(state):
    result = {}
    for row in state['invocations'].values():
        if account(row) == 'task':
            result[row['task_id']] = result.get(row['task_id'], 0) + 1
    return dict(sorted(result.items()))


def reallocate(policy, state, transfer):
    """Move unconsumed, uncommitted cap from one task to another; the totals stay the same."""
    budget = policy.get('task_budget')
    if budget is None:
        raise TaskBudgetError('Transfers require an explicit per-task allocation of the run '
                              '(declared in the profile or with allocate_task_budget)')
    if (not isinstance(transfer, dict) or set(transfer) != {'from_task', 'to_task', 'amounts'}
            or not isinstance(transfer['amounts'], dict) or not transfer['amounts'] or len(transfer['amounts']) > 32):
        raise TaskBudgetError('A transfer names from_task, to_task and the amounts')
    donor, recipient = transfer['from_task'], transfer['to_task']
    if donor == recipient or donor not in budget['tasks'] or recipient not in budget['tasks']:
        raise TaskBudgetError('A transfer moves cap between two different allocated tasks')
    available = usage(policy, state, donor)
    tasks = copy.deepcopy(budget['tasks'])
    give, take = flat(tasks[donor]), flat(tasks[recipient])
    consumed, committed = _measure(task_rows(policy, state, donor), give)
    for dimension, value in transfer['amounts'].items():
        run_cap(policy, dimension)
        if dimension not in give or dimension not in take:
            raise TaskBudgetError('Both tasks need a cap in ' + str(dimension)[:80] + ' to transfer it')
        money = dimension in MONEY
        if not _number(value) or value <= 0 or not money and type(value) is not int:
            raise TaskBudgetError('A transfer moves a positive amount of ' + dimension[:80])
        moved = amount(value) if money else value
        # Against the exact cap that is neither consumed nor committed, never a rounded view.
        if moved > _available(give[dimension], consumed[dimension], committed[dimension], dimension):
            raise TaskBudgetError('The transfer would take consumed or committed amounts of ' + donor
                                  + ' in ' + dimension[:80] + ': only %s is available' % available[dimension]['available'])
        if money:
            # Exact decimals: the donor loses exactly what the recipient gains.
            before = amount(give[dimension]) + amount(take[dimension])
            give[dimension] = _decimal(amount(give[dimension]) - moved)
            take[dimension] = _decimal(amount(take[dimension]) + moved)
            if amount(give[dimension]) + amount(take[dimension]) != before:
                raise TaskBudgetError('The transfer would not conserve the ' + dimension[:80] + ' caps')
        else:
            give[dimension] -= moved
            take[dimension] += moved
    tasks[donor], tasks[recipient] = nest(give), nest(take)
    profile = copy.deepcopy(policy)
    profile['task_budget']['tasks'] = tasks
    validate(profile)
    facts = dict(transfer=copy.deepcopy(transfer), donor_before=available,
                 donor_after=usage(profile, state, donor), recipient_before=usage(policy, state, recipient),
                 recipient_after=usage(profile, state, recipient))
    return profile, _changes(budget['tasks'], tasks), facts


def locate(policy, dimension):
    """Container and key of ``task_budget.<task>.<dimension>`` in a profile."""
    parts = dimension.split('.') if isinstance(dimension, str) else []
    try:
        if len(parts) not in (3, 4) or parts[0] != 'task_budget':
            raise KeyError(dimension)
        caps = policy['task_budget']['tasks'][parts[1]]
        if len(parts) == 4:
            if parts[2] not in FAMILIES:
                raise KeyError(dimension)
            return caps[parts[2]], parts[3]
        if parts[2] not in SCALARS:
            raise KeyError(dimension)
        return caps, parts[2]
    except (KeyError, TypeError):
        raise TaskBudgetError('Unknown task budget dimension') from None
