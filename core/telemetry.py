"""Pure classification of the cost telemetry of one invocation (ADR 0004, P2-1).

The rules of the engine's ``ssdlc.cost-telemetry`` schema v1 (``ssdlc_telemetry.py``, #164,
and route admission, #165), without I/O, clock or identifiers: building a record stays in the
engine, which does not use this module yet (step 6 of ADR 0001).
``tests/test_core_equivalence.py`` checks that both give the same results.

Three measures stay separate and are never added to one another: ``spend`` (a charge the
provider measured or reported), ``estimate`` (an API-equivalent value estimated by a CLI, not a
charge) and ``tokens``. A value that was not reported is unknown (``None``), never 0.
"""
import math

SCHEMA = 'ssdlc.cost-telemetry'
SCHEMA_VERSION = 1
AVAILABILITY = ('reported', 'estimated', 'unknown')
OUTCOMES = ('completed', 'incomplete')
PROVENANCE = ('verified_signal', 'explicit_config', 'unknown')
TOKEN_FIELDS = ('input', 'cached_input', 'cache_creation', 'output', 'reasoning')
TOKEN_SCOPES = ('per_turn', 'cumulative')
ADAPTERS = {'claude': 'claude_cli', 'codex': 'codex_cli', 'agy': 'agy_cli', 'openrouter': 'openrouter_http'}
QUOTA_SNAPSHOTS_VERSION = 1
# The public form of a collected quota container (``ssdlc_quota``, #166).
QUOTA_ROUTES = ('agy_cli', 'codex_cli', 'claude_cli')
QUOTA_CONTAINER_FIELDS = ('availability', 'schema_version', 'status', 'route', 'snapshots', 'deltas', 'controls')
PUBLIC_SNAPSHOT_FIELDS = ('schema', 'schema_version', 'provider', 'status', 'reason', 'source', 'observed_at',
                          'buckets', 'synthetic_fixture')
PUBLIC_BUCKET_FIELDS = ('id', 'group', 'name', 'window', 'enabled', 'remaining_percent', 'used_percent',
                        'reset_time')

# The measure each effective route needs. A CLI subscription route leaves the dollar caps only
# with an explicit declaration for its agent.
ROUTE_MEASURES = {'openrouter_api': 'spend', 'claude_cli': 'estimate', 'codex_cli': 'declaration',
                  'agy_cli': 'declaration'}
# The measures each adapter really reports. The Claude CLI estimate is never a charge.
ADAPTER_MEASURES = {'openrouter': ('spend',), 'claude': ('estimate',), 'codex': (), 'agy': ()}


class AdmissionRefused(ValueError):
    """The imminent call cannot be bounded by a configured cap: it is refused before launch."""


def amount(value):
    """A reported money amount: a finite non-negative number, else ``None`` (unknown)."""
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return value
    return None


def count(value):
    """A reported token count: a non-negative integer, else ``None`` (unknown)."""
    return value if type(value) is int and value >= 0 else None


def empty_quota():
    """The quota container of a call with no quota control enabled for its route."""
    return {'availability': 'unknown', 'schema_version': QUOTA_SNAPSHOTS_VERSION,
            'status': 'not_collected', 'snapshots': []}


def validate_quota(value):
    """Raise ``ValueError`` unless ``value`` is a collected container with no account identity."""
    try:
        if (value['availability'] != 'reported' or value['schema_version'] != QUOTA_SNAPSHOTS_VERSION
                or value['status'] != 'collected' or value['route'] not in QUOTA_ROUTES
                or set(value) != set(QUOTA_CONTAINER_FIELDS)):
            raise ValueError('Invalid quota snapshots container')
        for key in ('before', 'after'):
            snapshot = value['snapshots'][key]
            if snapshot is not None and (set(snapshot) != set(PUBLIC_SNAPSHOT_FIELDS)
                                         or any(set(b) != set(PUBLIC_BUCKET_FIELDS) for b in snapshot['buckets'])):
                raise ValueError('A quota snapshot in evidence carries fields outside its public form')
        for delta in value['deltas']:
            if delta['call_consumption'] is not None:
                raise ValueError('A quota delta is never the consumption of the call')
            if delta['status'] == 'observed_account_delta':
                if not (type(delta['delta_pp']) in (int, float) and delta['delta_pp'] > 0):
                    raise ValueError('An observed quota delta is positive')
            elif delta['status'] != 'unknown' or delta['delta_pp'] is not None:
                raise ValueError('Unknown is not zero: quota delta')
    except (KeyError, TypeError, AttributeError):
        raise ValueError('Malformed quota snapshots container') from None
    return value


def validate(t):
    """Raise ``ValueError`` unless ``t`` is a well-formed telemetry record; return it."""
    try:
        if t['schema'] != SCHEMA or t['schema_version'] != SCHEMA_VERSION:
            raise ValueError('Unsupported telemetry schema')
        if t['provider'] not in ADAPTERS or t['adapter'] != ADAPTERS[t['provider']] or t['outcome'] not in OUTCOMES:
            raise ValueError('Invalid telemetry provider or outcome')
        if not isinstance(t['invocation_id'], str) or not t['invocation_id']:
            raise ValueError('Invalid invocation id')
        for key in ('route', 'billing'):
            entry = t[key]
            if entry['provenance'] not in PROVENANCE or (entry['provenance'] == 'unknown') != (entry['value'] is None):
                raise ValueError('Invalid ' + key + ' provenance')
        for key, available in (('spend', 'reported'), ('estimate', 'estimated'), ('tokens', 'reported')):
            measure = t[key]
            if measure['availability'] not in (available, 'unknown') or not measure.get('observed_at'):
                raise ValueError('Invalid ' + key + ' availability')
        for key in ('spend', 'estimate'):
            measure = t[key]
            if (measure['availability'] == 'unknown') != (measure['amount'] is None):
                raise ValueError('Unknown is not zero: ' + key)
            if measure['amount'] is not None and (amount(measure['amount']) is None or not measure['unit']):
                raise ValueError('Invalid ' + key + ' amount')
        if t['estimate']['unit'] != 'USD' or t['estimate']['basis'] != 'api_equivalent':
            raise ValueError('An estimate is an API-equivalent USD value')
        measure = t['tokens']
        counts = measure['counts']
        if set(counts) != set(TOKEN_FIELDS) or any(v is not None and count(v) is None for v in counts.values()):
            raise ValueError('Invalid token counts')
        known = any(v is not None for v in counts.values())
        if (measure['availability'] == 'reported') != known:
            raise ValueError('Unknown is not zero: tokens')
        if known and measure['scope'] not in TOKEN_SCOPES:
            raise ValueError('Token counts need a per-turn or cumulative scope')
        if not set(measure['contract']) <= set(TOKEN_FIELDS) or any(
                len(pair) != 2 or not set(pair) <= set(TOKEN_FIELDS) for pair in measure['overlaps']):
            raise ValueError('Invalid token overlaps')
        if t['outcome'] == 'incomplete' and any(t[k]['availability'] != 'unknown' for k in ('spend', 'estimate', 'tokens')):
            raise ValueError('An incomplete call has unknown telemetry')
        if t['quota_snapshots'] != empty_quota():
            validate_quota(t['quota_snapshots'])
    except (KeyError, TypeError, AttributeError):
        raise ValueError('Malformed telemetry record') from None
    return t


def token_total(measure):
    """Tokens of one invocation without double counting, or ``None`` when a field the source
    defines was not reported: fields contained in another one are not added again."""
    if measure.get('availability') != 'reported':
        return None
    contained = {pair[0] for pair in measure['overlaps']}
    fields = [f for f in measure['contract'] if f not in contained]
    values = [measure['counts'][f] for f in fields]
    if not fields or any(v is None for v in values):
        return None
    return sum(values)


def measures(t):
    """The three measures of a valid record, apart: ``spend`` with its unit, ``estimate_usd``
    and ``tokens`` (the total without double counting). Each is ``None`` when unknown."""
    validate(t)
    reported = t['spend']['availability'] == 'reported'
    return {'spend': {'amount': t['spend']['amount'], 'unit': t['spend']['unit']} if reported else None,
            'estimate_usd': t['estimate']['amount'] if t['estimate']['availability'] == 'estimated' else None,
            'tokens': token_total(t['tokens'])}


def classify(row):
    """``(kind, telemetry)`` of one ledger row, without changing it.

    ``invocation``: a row of this schema with valid telemetry; ``no_invocation``: a row of this
    schema without it; ``legacy``: a row written before this schema, with a read-only annotation."""
    if row.get('telemetry_schema_version') == SCHEMA_VERSION:
        telemetry = row.get('telemetry')
        if telemetry is None:
            return 'no_invocation', None
        try:
            return 'invocation', validate(telemetry)
        except ValueError:
            return 'no_invocation', None
    return 'legacy', annotate_legacy(row)


def annotate_legacy(row):
    """A conservative annotation of a historical row: no measure is inferred from it. Its
    ``cost_usd`` may be a charge, an estimate or a default zero: it stays unclassified."""
    return {'schema': SCHEMA, 'schema_version': SCHEMA_VERSION, 'annotation': 'legacy_unversioned',
            'provider': row.get('engine') or 'unknown', 'outcome': 'unknown',
            'legacy_cost_usd': {'amount': amount(row.get('cost_usd')), 'classification': 'unclassified'},
            'spend': 'unknown', 'estimate': 'unknown', 'tokens': 'unknown', 'rewritten': False}


def ledger_measures(ledger):
    """The USD figures the separate caps read from a ledger, each invocation once.

    ``spend_usd``: charges reported in USD; ``estimate_usd``: API-equivalent estimates;
    ``unclassified_usd``: ``cost_usd`` of legacy rows, of rows without telemetry and of calls
    with neither a charge nor an estimate; ``unmeasured_spend`` and ``unmeasured_estimate``:
    completed calls on a route that needs a charge or an estimate and reported none."""
    spend_usd = estimate_usd = unclassified_usd = 0
    unmeasured, unestimated, seen = [], [], set()
    for row in ledger or ():
        if not isinstance(row, dict):
            continue
        kind, t = classify(row)
        if kind != 'invocation':
            unclassified_usd += amount(row.get('cost_usd')) or 0
            continue
        if t['invocation_id'] in seen:
            continue
        seen.add(t['invocation_id'])
        charged = t['spend']['availability'] == 'reported' and t['spend']['unit'] == 'USD'
        if charged:
            spend_usd += t['spend']['amount']
        estimated = t['estimate']['availability'] == 'estimated'
        if estimated:
            estimate_usd += t['estimate']['amount']
        elif not charged:
            unclassified_usd += amount(row.get('cost_usd')) or 0
        # A Claude CLI call records no route of its own: its route needs an estimate.
        route = t['route']['value']
        required = ROUTE_MEASURES.get(route) if route else ('estimate' if t['provider'] == 'claude' else None)
        if t['outcome'] == 'completed' and required == 'spend' and not charged:
            unmeasured.append(t['invocation_id'])
        if t['outcome'] == 'completed' and required == 'estimate' and not estimated:
            unestimated.append(t['invocation_id'])
    return {'spend_usd': spend_usd, 'estimate_usd': estimate_usd, 'unclassified_usd': unclassified_usd,
            'unmeasured_spend': unmeasured, 'unmeasured_estimate': unestimated}


def admit(provider, route, *, spend_cap, estimate_cap_active, declarations=None):
    """The measure that bounds an imminent call on its effective ``route`` (``None``: unknown).

    A route that cannot be determined, or a measured route whose adapter reports no charge, is
    refused when a cap it would escape is in force: an estimate never stands for a charge. A
    CLI subscription route reports no dollar measure: it leaves the dollar caps only with an
    explicit declaration for its agent, and is refused without one while either cap is in force."""
    if route is None:
        if spend_cap or estimate_cap_active:
            raise AdmissionRefused('The route of the call cannot be determined, so the configured cost caps '
                                   'cannot be applied; dispatch refused before launch')
        return {'route': 'unknown', 'measure': None}
    required = ROUTE_MEASURES.get(route)
    if required in ('spend', 'estimate'):
        if required in ADAPTER_MEASURES.get(provider, ()):
            return {'route': route, 'measure': required}
        if spend_cap if required == 'spend' else estimate_cap_active:
            raise AdmissionRefused(f'Route {route} needs a measured {required}, which the {provider} adapter does '
                                   'not report (an estimate is not a charge); dispatch refused before launch')
        return {'route': route, 'measure': None}
    if required is None:
        raise AdmissionRefused('Unregistered route ' + str(route) + '; dispatch refused before launch')
    rule = (declarations or {}).get(provider)
    if rule:
        return {'route': route, 'measure': rule['model']}
    if spend_cap or estimate_cap_active:
        raise AdmissionRefused(f'Route {route} reports no dollar measure and no unmetered_quota declaration '
                               f'applies to {provider}, so the configured cost caps cannot be applied; '
                               'dispatch refused before launch')
    return {'route': route, 'measure': None}
