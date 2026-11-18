"""Accounting and projections of a run, with declared coverage (#46, #348).

Part 3 of the ``workflow-status`` CLI. ``project`` is pure: it reads the ledger,
the task DAG and the frozen limits the ``WorkflowSnapshot`` has already collected,
and returns data. It never changes a budget, a limit or the run: its warnings are
data for the operator, never a stop.

The monetary measures stay separate, each next to the frozen cap of its kind:

- ``spend_usd`` (charges reported in USD) against ``metered_usd``;
- ``estimate_usd`` (API-equivalent estimates) against ``cli_estimate_usd``.

For each measure the central estimate is the spend so far, plus the development
component (the average per completed task, times the remaining tasks) and the gate
component (one call for each remaining mandatory global gate). The scenario is the
accounted charge of the frozen limits plus, for each remaining invocation, the
largest frozen reserve or observed charge of one invocation: it is not a
guaranteed financial maximum. A component without data is ``null`` and makes the
projection partial.

Only observations count: a call whose measure is unknown (a legacy row, a call that
reported nothing on a declared route, a Claude call without an estimate) and a completed
task without a known call are left out of the averages, never counted as zeros, and make
the component partial; with no observation at all the component is ``null``.
"""
import re

COMPLETE, PARTIAL, NONE = 'complete', 'partial', 'none'
MEASURES = (('spend_usd', 'metered_usd'), ('estimate_usd', 'cli_estimate_usd'))
NAMES = tuple(measure for measure, _ in MEASURES)
# The mandatory global gates, in the order the pipeline runs them (orchestrator.run_pipeline).
GATES = ('04-review', '05-qa', '05b-appsec')
PHASE_ORDER = ('00-recon', '01-po', '02-design', '03-dev', '04-review', '05-qa', '05b-appsec',
               '06-ops', '07-docs', '08-supervisor')
# Statuses in which every global gate has passed.
GATES_PASSED = ('AWAITING_SUPERVISOR_SIGNOFF', 'APPROVED_AND_MERGED')
DEV_ROW = re.compile(r'03-dev-(.+)-a[1-9][0-9]*')
GATE_ROW = re.compile(r'(04-review|05-qa|05b-appsec)-a[1-9][0-9]*')
SOURCE = 'state.json ledger telemetry'


def component(value, coverage, source, reason=None, **extra):
    if coverage != COMPLETE and not reason:
        raise ValueError('a component without complete coverage needs a reason')
    if coverage == NONE:
        value = None
    return dict(value=None if value is None else round(value, 6), unit='USD', coverage=coverage,
                source=source, reason=reason, **extra)


def _label(measure):
    return measure.replace('_usd', '')


def _observe(rows):
    """The calls of ledger ``rows``, each invocation once: per call, the amount of each measure, or None if unknown.

    A zero is an observation. A measure the call's route does not report (the spend of a Claude
    CLI call, the estimate of an OpenRouter charge) is a known zero once the call has a measure
    of its own, reported or required by its route. Everything else is unknown, never a zero: a
    legacy row or an unclassified figure (whose ``cost_usd`` belongs to no measure), a call on a
    declared or unknown route that reported nothing (Codex, Antigravity), the measure a route
    requires and the call did not report.
    """
    from ssdlc_telemetry import ROUTE_MEASURES, _amount, classify
    calls, seen = [], set()
    for row in rows:
        kind, t = classify(row)
        if kind != 'invocation':
            # A row of this schema without telemetry and without a figure is no engine call.
            if kind == 'legacy' or row.get('telemetry') is not None or _amount(row.get('cost_usd')):
                calls.append(dict.fromkeys(NAMES))
            continue
        if t['invocation_id'] in seen:
            continue
        seen.add(t['invocation_id'])
        values = {'spend_usd': t['spend']['amount'] if t['spend']['availability'] == 'reported'
                  and t['spend']['unit'] == 'USD' else None,
                  'estimate_usd': t['estimate']['amount'] if t['estimate']['availability'] == 'estimated' else None}
        # A Claude CLI call records no route of its own: its route needs an estimate (as ledger_measures).
        route = t['route']['value']
        required = {'spend': 'spend_usd', 'estimate': 'estimate_usd'}.get(
            ROUTE_MEASURES.get(route) if route else ('estimate' if t['provider'] == 'claude' else None))
        own = required or next((m for m in NAMES if values[m] is not None), None)
        if own is not None:
            values = {m: 0 if m != own and v is None else v for m, v in values.items()}
        calls.append(values)
    return calls


def _known(calls, measure):
    return [c[measure] for c in calls if c[measure] is not None]


def _development(ledger, dag):
    """Per measure: the average per completed task with observations, times the remaining tasks."""
    if dag is None:
        return {m: component(None, NONE, 'plan and task journal', 'no task plan in this run') for m, _ in MEASURES}
    completed = [n['task_id'] for n in dag['nodes'] if n['status'] == 'COMPLETED']
    remaining = len(dag['nodes']) - len(completed)
    extra = dict(tasks_completed=len(completed), tasks_remaining=remaining)
    if not remaining:
        return {m: component(0, COMPLETE, 'plan and task journal', per_task=None, tasks_observed=None, **extra)
                for m, _ in MEASURES}
    if not completed:
        return {m: component(None, NONE, SOURCE, 'no completed task to average', per_task=None,
                             tasks_observed=0, **extra) for m, _ in MEASURES}
    rows = {task: [] for task in completed}
    for r in ledger:
        if (match := DEV_ROW.fullmatch(str(r.get('phase', '')))) and match.group(1) in rows:
            rows[match.group(1)].append(r)
    calls = {task: _observe(task_rows) for task, task_rows in rows.items()}
    out = {}
    for measure, _ in MEASURES:
        label = _label(measure)
        observed, missing, incomplete = {}, [], []
        for task in completed:
            known = _known(calls[task], measure)
            if not known:  # Its cost is unknown: it does not count as a zero in the average.
                missing.append(task)
                continue
            observed[task] = sum(known)
            if len(known) < len(calls[task]):
                incomplete.append(task)
        if not observed:
            out[measure] = component(None, NONE, SOURCE, f'no completed task has a development call with a known '
                                     f'{label} in the ledger', per_task=None, tasks_observed=0, **extra)
            continue
        reasons = []
        if missing:
            reasons.append(f'{len(missing)} of {len(completed)} completed tasks have no development call with a '
                           f'known {label} ({", ".join(missing)}): the average is of the others')
        if incomplete:
            reasons.append(f'{len(incomplete)} completed tasks have development calls without a known {label} '
                           f'({", ".join(incomplete)}): only their known part is counted')
        per_task = sum(observed.values()) / len(observed)
        out[measure] = component(per_task * remaining, PARTIAL if reasons else COMPLETE, SOURCE,
                                 '; '.join(reasons) or None, per_task=round(per_task, 6),
                                 tasks_observed=len(observed), **extra)
    return out


def remaining_gates(run):
    """The mandatory global gates still to pass, from the phase the engine recorded; None if unknown."""
    run = run or {}
    if run.get('status') in GATES_PASSED:
        return []
    phase = run.get('current_phase')
    if phase is None:
        return list(GATES)
    if phase not in PHASE_ORDER:
        return None
    index = PHASE_ORDER.index(phase)
    return [gate for gate in GATES if PHASE_ORDER.index(gate) >= index]


def _gates(ledger, run):
    """Per measure: one call for each remaining mandatory gate, at the average cost of one call."""
    gates = remaining_gates(run)
    if gates is None:
        return {m: component(None, NONE, 'state.json current_phase', 'the recorded phase is not a known phase',
                             gates_remaining=None, per_call=None, basis=None) for m, _ in MEASURES}, None
    if not gates:
        return {m: component(0, COMPLETE, 'state.json', gates_remaining=[], per_call=None, basis=None)
                for m, _ in MEASURES}, gates
    basis = 'gate_calls'
    calls = _observe([r for r in ledger if GATE_ROW.fullmatch(str(r.get('phase', '')))])
    if not calls:  # No gate has run yet: one call of the run, on average, is the best estimate.
        basis = 'all_calls'
        calls = _observe(ledger)
    out = {}
    for measure, _ in MEASURES:
        known = _known(calls, measure)
        extra = dict(gates_remaining=list(gates), basis=basis if calls else None, calls_observed=len(known),
                     calls_unknown=len(calls) - len(known))
        if not known:
            reason = (f'none of the {len(calls)} calls in the sample has a known {_label(measure)}' if calls
                      else 'no call in the ledger to estimate one gate call')
            out[measure] = component(None, NONE, SOURCE, reason, per_call=None, **extra)
            continue
        per_call = sum(known) / len(known)
        reason = (f'{len(calls) - len(known)} of {len(calls)} calls in the sample have no known {_label(measure)}: '
                  'the average is of the others') if len(known) < len(calls) else None
        out[measure] = component(per_call * len(gates), PARTIAL if reason else COMPLETE, SOURCE, reason,
                                 per_call=round(per_call, 6), **extra)
    return out, gates


def _one_call(calls, view, measure, kind):
    """``(amount, basis)`` of one call, the threshold of an imminent exhaustion, whatever phase the run is in.

    The average known amount of one call of the run; without observations, the largest frozen
    reserve or charge of one invocation; else ``(None, None)``."""
    known = _known(calls, measure)
    if known:
        return round(sum(known) / len(known), 6), 'observed_calls'
    if view['kinds'][kind]['per_invocation'] is not None:
        return view['kinds'][kind]['per_invocation'], 'frozen_limits'
    return None, None


def _limits_view(limits):
    """Accounted charge, caps and remaining invocations of the frozen limits, reusing ``LimitsStore``."""
    from ssdlc_recovery import COST_KINDS, LimitsStore, RESERVE, uncovered
    policy, state = limits['policy'], limits['state']
    rows = list(state['invocations'].values())
    used = len(rows)
    view = dict(invocations=dict(cap=policy['invocations'], used=used, remaining=max(policy['invocations'] - used, 0)),
                kinds={})
    declared = policy.get('live_unknown_usage', {})
    unknown = sum(uncovered(row) or (row.get('status') in ('reserved', 'uncertain')
                                     and not row.get('conservative_usage')) for row in rows)
    for kind in COST_KINDS:
        charges = [LimitsStore._kind_charge(row, kind) for row in rows]
        reserves = [rule[kind] for rule in declared.values() if rule.get('model') == RESERVE]
        view['kinds'][kind] = dict(cap=policy[kind], accounted=round(sum(charges), 6),
                                   per_invocation=max([*reserves, *charges], default=None), unknown=unknown)
    return view


def _scenario(view, kind):
    """Accounted charge plus the remaining invocations at the largest frozen or observed charge of one."""
    entry, remaining = view['kinds'][kind], view['invocations']['remaining']
    extra = dict(remaining_invocations=remaining, per_invocation=entry['per_invocation'], guaranteed_maximum=False)
    reasons = []
    if entry['unknown']:
        reasons.append(f'{entry["unknown"]} invocations have no dollar figure and no frozen reserve')
    if remaining and not entry['per_invocation']:
        return component(None, NONE, 'frozen limits', '; '.join(
            [*reasons, 'no frozen reserve or observed charge to price a remaining invocation']), **extra)
    value = entry['accounted'] + remaining * (entry['per_invocation'] or 0)
    return component(value, PARTIAL if reasons else COMPLETE, 'frozen limits', '; '.join(reasons) or None, **extra)


def _total(parts, source):
    known = [p for p in parts if p['value'] is not None]
    missing = [p['reason'] for p in parts if p['coverage'] != COMPLETE]
    coverage = COMPLETE if not missing else (PARTIAL if known else NONE)
    return component(sum(p['value'] for p in known) if known else None, coverage, source, '; '.join(missing) or None)


def project(run, ledger, dag, limits, spent):
    """The projection of a run; pure and without effects.

    ``run``: ``state.json`` status and phase; ``ledger``: its rows; ``dag``: the
    snapshot's task DAG or None; ``limits``: ``{'status', 'reason', 'policy',
    'state'}`` of the frozen limits; ``spent``: the snapshot's ``spend_usd`` and
    ``estimate_usd`` metrics.
    """
    ledger = [r for r in ledger or () if isinstance(r, dict)]
    development = _development(ledger, dag)
    gates, gates_left = _gates(ledger, run)
    view = _limits_view(limits) if limits['status'] == 'active' else None
    calls = _observe(ledger) if view is not None else []
    measures, warnings = {}, []
    for measure, kind in MEASURES:
        spent_part = component(spent[measure]['value'], spent[measure]['coverage'], spent[measure]['source'],
                               spent[measure]['reason'])
        future = _total([development[measure], gates[measure]], SOURCE)
        central = _total([spent_part, development[measure], gates[measure]], SOURCE)
        entry = dict(cap_kind=kind, spent=spent_part, development=development[measure], gates=gates[measure],
                     central=central, budget=None, scenario=None)
        if view is not None:
            limit = view['kinds'][kind]
            remaining = round(limit['cap'] - limit['accounted'], 6)
            projected = None if future['value'] is None else round(limit['accounted'] + future['value'], 6)
            entry['budget'] = dict(cap=limit['cap'], accounted=limit['accounted'], remaining=remaining,
                                   projected=projected, projected_coverage=future['coverage'],
                                   over_cap=None if projected is None else projected > limit['cap'])
            entry['scenario'] = _scenario(view, kind)
            one_call, basis = _one_call(calls, view, measure, kind)
            if remaining <= 0 or (one_call is not None and remaining <= one_call):
                warnings.append(dict(code='BUDGET_NEARLY_EXHAUSTED', measure=measure, cap_kind=kind,
                                     remaining=remaining, one_call=one_call, one_call_basis=basis))
            if entry['budget']['over_cap']:
                warnings.append(dict(code='FORECAST_OVER_CAP', measure=measure, cap_kind=kind,
                                     cap=limit['cap'], projected=projected))
        measures[measure] = entry
    if view is not None and view['invocations']['remaining'] <= 1:
        warnings.append(dict(code='INVOCATIONS_NEARLY_EXHAUSTED', measure='invocations',
                             remaining=view['invocations']['remaining']))
    parts = [part for entry in measures.values() for part in (entry['development'], entry['gates'])]
    coverage = (COMPLETE if all(p['coverage'] == COMPLETE for p in parts)
                else NONE if all(p['value'] is None for p in parts) else PARTIAL)
    return dict(coverage=coverage, tasks_remaining=development['spend_usd'].get('tasks_remaining'),
                gates_remaining=gates_left,
                limits=dict(status=limits['status'], reason=limits['reason'],
                            invocations=view['invocations'] if view else None),
                measures=measures,
                # Warnings are data: nothing here changes a budget, a limit or the run.
                warnings=[dict(w, effect='none') for w in warnings])
