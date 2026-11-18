"""Shared, versioned cost telemetry of one engine invocation (issue #164, part A of #14).

Four measures stay separate and are never added to one another:

- ``spend``: a charge the provider measured or reported, in its own currency;
- ``estimate``: an API-equivalent value estimated by the CLI (Claude ``total_cost_usd``),
  not a charge;
- ``tokens``: raw token counts, with the fields that overlap declared, so that no count is
  added twice, and whether the source reports them per turn or cumulatively;
- ``quota_snapshots``: a versioned container, empty and ``unknown`` unless a quota control is
  enabled for the route of the call; then it holds the public snapshots taken before and after
  the call and their deltas (``ssdlc_quota``, part C, #166).

Every measure has a unit, a timestamp, a source and an availability (``reported``,
``estimated`` or ``unknown``). A value that was not reported is ``unknown`` (``None``),
never 0; a zero the source reported explicitly is a value. Route and billing mode come only
from a verified signal or an explicit configuration of the invocation, never from the engine
label or from an API key in the environment; otherwise they are ``unknown``.

Ledger rows written before this schema are annotated when read, never rewritten: their
``cost_usd`` stays an unclassified legacy figure, neither a charge nor an estimate.
No credential, account or conversation identifier is ever copied into a record.
"""
import math
import uuid
from datetime import datetime, timezone

SCHEMA = 'ssdlc.cost-telemetry'
SCHEMA_VERSION = 1
AVAILABILITY = ('reported', 'estimated', 'unknown')
OUTCOMES = ('completed', 'incomplete')
PROVENANCE = ('verified_signal', 'explicit_config', 'unknown')
TOKEN_FIELDS = ('input', 'cached_input', 'cache_creation', 'output', 'reasoning')
TOKEN_SCOPES = ('per_turn', 'cumulative')
ADAPTERS = {'claude': 'claude_cli', 'codex': 'codex_cli', 'agy': 'agy_cli', 'openrouter': 'openrouter_http'}
QUOTA_SNAPSHOTS_VERSION = 1


def _now():
    return datetime.now(timezone.utc).isoformat()


def _amount(value):
    """A reported money amount: a finite non-negative number, else None (unknown)."""
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return value
    return None


def _count(value):
    """A reported token count: a non-negative integer, else None (unknown)."""
    return value if type(value) is int and value >= 0 else None


def provenance(value=None, source='unknown'):
    """Route or billing mode of one invocation, with how it was established."""
    if value is None or source == 'unknown':
        return {'value': None, 'provenance': 'unknown'}
    if source not in PROVENANCE:
        raise ValueError('Unknown provenance')
    return {'value': value, 'provenance': source}


def spend(amount=None, currency=None, source=None, observed_at=None):
    amount = _amount(amount)
    known = amount is not None and isinstance(currency, str) and currency
    return {'availability': 'reported' if known else 'unknown', 'amount': amount if known else None,
            'unit': currency if known else None, 'source': source if known else None,
            'observed_at': observed_at or _now()}


def estimate(amount=None, source=None, observed_at=None):
    amount = _amount(amount)
    return {'availability': 'estimated' if amount is not None else 'unknown', 'amount': amount,
            'unit': 'USD', 'basis': 'api_equivalent', 'source': source if amount is not None else None,
            'observed_at': observed_at or _now()}


def tokens(counts=None, *, scope=None, source=None, contract=(), overlaps=(), turns=None, observed_at=None):
    """Raw token counts. ``contract`` lists the fields the source defines; ``overlaps`` the
    ``[subset, superset]`` pairs of fields one of which already includes the other."""
    counts = {field: _count((counts or {}).get(field)) for field in TOKEN_FIELDS}
    known = any(value is not None for value in counts.values())
    return {'availability': 'reported' if known else 'unknown', 'unit': 'tokens',
            'source': source if known else None, 'observed_at': observed_at or _now(),
            'scope': scope if known else None, 'turns': _count(turns) if known else None,
            'counts': counts, 'contract': [f for f in TOKEN_FIELDS if f in contract] if known else [],
            'overlaps': [list(pair) for pair in overlaps] if known else []}


def quota_snapshots():
    """The empty container: no quota control is enabled for the route of the call. A collected
    one comes from ``ssdlc_quota.container`` (part C, #166)."""
    return {'availability': 'unknown', 'schema_version': QUOTA_SNAPSHOTS_VERSION,
            'status': 'not_collected', 'snapshots': []}


def record(provider, *, outcome='completed', spend_measure=None, estimate_measure=None,
           token_measure=None, route=None, billing=None):
    """The telemetry of one invocation. Anything not given is unknown."""
    now = _now()
    result = {
        'schema': SCHEMA, 'schema_version': SCHEMA_VERSION, 'invocation_id': uuid.uuid4().hex,
        'provider': provider, 'adapter': ADAPTERS.get(provider), 'outcome': outcome, 'recorded_at': now,
        'route': route or provenance(), 'billing': billing or provenance(),
        'spend': spend_measure or spend(observed_at=now),
        'estimate': estimate_measure or estimate(observed_at=now),
        'tokens': token_measure or tokens(observed_at=now),
        'quota_snapshots': quota_snapshots(),
    }
    validate(result)
    return result


def provider_for(engine):
    """The telemetry provider of an engine label: a CLI adapter, else the OpenRouter API."""
    engine = str(engine or '').lower().strip()
    return engine if engine in ADAPTERS else 'openrouter'


def incomplete(provider, route=None):
    """A crash or an incomplete call: every measure is unknown."""
    return record(provider, outcome='incomplete', route=route)


def validate(t):
    """Raise ValueError unless ``t`` is a well-formed telemetry record of this schema."""
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
            if measure['amount'] is not None and (_amount(measure['amount']) is None or not measure['unit']):
                raise ValueError('Invalid ' + key + ' amount')
        if t['estimate']['unit'] != 'USD' or t['estimate']['basis'] != 'api_equivalent':
            raise ValueError('An estimate is an API-equivalent USD value')
        measure = t['tokens']
        counts = measure['counts']
        if set(counts) != set(TOKEN_FIELDS) or any(v is not None and _count(v) is None for v in counts.values()):
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
        quota = t['quota_snapshots']
        if quota != quota_snapshots():
            import ssdlc_quota
            ssdlc_quota.validate_container(quota)
    except (KeyError, TypeError, AttributeError):
        raise ValueError('Malformed telemetry record') from None
    return t


def token_total(measure):
    """Tokens of one invocation without double counting, or None when a field the source
    defines was not reported: fields contained in another one are not added again."""
    if measure.get('availability') != 'reported':
        return None
    contained = {pair[0] for pair in measure['overlaps']}
    fields = [f for f in measure['contract'] if f not in contained]
    values = [measure['counts'][f] for f in fields]
    if not fields or any(v is None for v in values):
        return None
    return sum(values)


# ---------------------------------------------------------------------------
# Adapters: parse what each CLI or API reports, without touching its answer.
# ---------------------------------------------------------------------------

def from_claude(envelope, route=None):
    """Claude CLI ``--output-format json``: ``total_cost_usd`` is an API-equivalent
    estimate (on a subscription it is not a charge). ``input_tokens`` excludes cache reads
    and cache creation; thinking is part of ``output_tokens`` and not reported apart."""
    if not isinstance(envelope, dict):
        return record('claude', route=route)
    usage = envelope.get('usage') if isinstance(envelope.get('usage'), dict) else {}
    return record('claude', route=route,
        estimate_measure=estimate(envelope.get('total_cost_usd'), 'claude_cli.total_cost_usd'),
        token_measure=tokens({'input': usage.get('input_tokens'), 'cached_input': usage.get('cache_read_input_tokens'),
                              'cache_creation': usage.get('cache_creation_input_tokens'),
                              'output': usage.get('output_tokens')},
                             scope='cumulative', source='claude_cli.usage', turns=envelope.get('num_turns'),
                             contract=('input', 'cached_input', 'cache_creation', 'output'),
                             overlaps=(('reasoning', 'output'),)))


def from_codex(usages, route=None):
    """``codex exec --json``: one ``turn.completed.usage`` per turn, summed here.
    ``input_tokens`` includes ``cached_input_tokens``; ``output_tokens`` includes
    ``reasoning_output_tokens``. Codex reports no dollar value.

    A field is summed only when every turn reports it: if one turn does not, its share is
    unknown and so is the field of the whole invocation, never a partial sum."""
    names = {'input': 'input_tokens', 'cached_input': 'cached_input_tokens',
             'output': 'output_tokens', 'reasoning': 'reasoning_output_tokens'}
    usages = list(usages or ())
    counts = dict.fromkeys(names)
    for field, name in names.items():
        values = [_count(usage.get(name)) if isinstance(usage, dict) else None for usage in usages]
        if values and all(value is not None for value in values):
            counts[field] = sum(values)
    return record('codex', route=route,
        token_measure=tokens(counts, scope='per_turn', source='codex_exec.turn.completed.usage',
                             turns=len(usages), contract=tuple(names),
                             overlaps=(('cached_input', 'input'), ('reasoning', 'output'))))


def from_agy(envelope, route=None):
    """agy ``--output-format json`` (contract observed on agy 1.5.9): cumulative over the
    invocation. ``thinking_tokens`` and ``cache_read_tokens`` are separate categories, not
    included in ``input_tokens``/``total_tokens``. agy reports no dollar value."""
    usage = envelope.get('usage') if isinstance(envelope, dict) and isinstance(envelope.get('usage'), dict) else {}
    return record('agy', route=route,
        token_measure=tokens({'input': usage.get('input_tokens'), 'cached_input': usage.get('cache_read_tokens'),
                              'output': usage.get('output_tokens'), 'reasoning': usage.get('thinking_tokens')},
                             scope='cumulative', source='agy.usage', turns=envelope.get('num_turns') if isinstance(envelope, dict) else None,
                             contract=('input', 'cached_input', 'output', 'reasoning')))


def from_openrouter(usage):
    """OpenRouter chat completion ``usage``: ``cost`` is the charge reported by the API in
    USD; cached and reasoning tokens are details included in prompt and completion tokens.
    The adapter itself sent the request to the OpenRouter API: a verified route."""
    usage = usage if isinstance(usage, dict) else {}
    prompt = usage.get('prompt_tokens_details') if isinstance(usage.get('prompt_tokens_details'), dict) else {}
    completion = (usage.get('completion_tokens_details')
                  if isinstance(usage.get('completion_tokens_details'), dict) else {})
    return record('openrouter', route=provenance('openrouter_api', 'verified_signal'),
        spend_measure=spend(usage.get('cost'), 'USD', 'openrouter.usage.cost'),
        token_measure=tokens({'input': usage.get('prompt_tokens'), 'cached_input': prompt.get('cached_tokens'),
                              'output': usage.get('completion_tokens'), 'reasoning': completion.get('reasoning_tokens')},
                             scope='cumulative', source='openrouter.usage',
                             contract=('input', 'cached_input', 'output', 'reasoning'),
                             overlaps=(('cached_input', 'input'), ('reasoning', 'output'))))


# ---------------------------------------------------------------------------
# Ledger: classification, legacy annotation and breakdown.
# ---------------------------------------------------------------------------

def classify(row):
    """``(kind, telemetry)`` of one ledger row, without changing it.

    ``invocation``: a row of this schema with its telemetry; ``no_invocation``: a row of
    this schema without telemetry (no engine call, or none it could attach); ``legacy``: a
    row written before this schema, with a conservative read-only annotation."""
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
    """A conservative annotation of a historical row: no measure is inferred from it.

    Its ``cost_usd`` may be a charge, an estimate or a default zero: it stays an
    unclassified legacy figure, and every measure of the schema is ``unknown``."""
    cost = _amount(row.get('cost_usd'))
    return {'schema': SCHEMA, 'schema_version': SCHEMA_VERSION, 'annotation': 'legacy_unversioned',
            'provider': row.get('engine') or 'unknown', 'outcome': 'unknown',
            'legacy_cost_usd': {'amount': cost, 'classification': 'unclassified'},
            'spend': 'unknown', 'estimate': 'unknown', 'tokens': 'unknown', 'rewritten': False}


def _provider_entry():
    return {'invocations': {'completed': 0, 'incomplete': 0},
            'spend': {'reported': 0, 'unknown': 0, 'by_unit': {}},
            'estimate': {'estimated': 0, 'unknown': 0, 'usd': None},
            'estimate_coverage': {'available': 0, 'completed': 0, 'ratio': None, 'incomplete_unknown': 0},
            'tokens': {'reported': 0, 'unknown': 0, 'counts': dict.fromkeys(TOKEN_FIELDS),
                       'scopes': [], 'total': None, 'total_invocations': 0, 'total_unknown': 0},
            'legacy': {'rows': 0, 'unclassified_usd': None, 'unknown_cost_rows': 0},
            'rows_without_telemetry': 0}


def breakdown(ledger):
    """Per-provider and per-measure view of a ledger. Each invocation is counted once, so a
    resumed or replayed record of the same invocation is not added twice."""
    providers, seen, duplicates = {}, set(), 0
    quota = {'schema_version': QUOTA_SNAPSHOTS_VERSION, 'invocations_with_snapshots': 0,
             'observed_account_deltas': 0, 'unknown_deltas': 0}
    for row in ledger or ():
        if not isinstance(row, dict):
            continue
        kind, t = classify(row)
        if kind == 'legacy':
            entry = providers.setdefault(t['provider'], _provider_entry())['legacy']
            entry['rows'] += 1
            amount = t['legacy_cost_usd']['amount']
            if amount is None:
                entry['unknown_cost_rows'] += 1
            else:
                entry['unclassified_usd'] = (entry['unclassified_usd'] or 0) + amount
            continue
        if kind == 'no_invocation':
            providers.setdefault(row.get('engine') or 'unknown', _provider_entry())['rows_without_telemetry'] += 1
            continue
        if t['invocation_id'] in seen:
            duplicates += 1
            continue
        seen.add(t['invocation_id'])
        if t['quota_snapshots']['status'] == 'collected':
            quota['invocations_with_snapshots'] += 1
            for change in t['quota_snapshots']['deltas']:
                quota['observed_account_deltas' if change['status'] == 'observed_account_delta'
                      else 'unknown_deltas'] += 1
        entry = providers.setdefault(t['provider'], _provider_entry())
        entry['invocations'][t['outcome']] += 1
        if t['outcome'] == 'incomplete':
            entry['estimate_coverage']['incomplete_unknown'] += 1
        else:
            entry['estimate_coverage']['completed'] += 1
            if t['estimate']['availability'] == 'estimated':
                entry['estimate_coverage']['available'] += 1
        if t['spend']['availability'] == 'reported':
            entry['spend']['reported'] += 1
            unit = t['spend']['unit']
            entry['spend']['by_unit'][unit] = entry['spend']['by_unit'].get(unit, 0) + t['spend']['amount']
        else:
            entry['spend']['unknown'] += 1
        if t['estimate']['availability'] == 'estimated':
            entry['estimate']['estimated'] += 1
            entry['estimate']['usd'] = (entry['estimate']['usd'] or 0) + t['estimate']['amount']
        else:
            entry['estimate']['unknown'] += 1
        measure, totals = t['tokens'], entry['tokens']
        if measure['availability'] != 'reported':
            totals['unknown'] += 1
        else:
            totals['reported'] += 1
            if measure['scope'] not in totals['scopes']:
                totals['scopes'].append(measure['scope'])
            for field, value in measure['counts'].items():
                if value is not None:
                    totals['counts'][field] = (totals['counts'][field] or 0) + value
        total = token_total(measure)
        if total is None:
            totals['total_unknown'] += 1
        else:
            totals['total'] = (totals['total'] or 0) + total
            totals['total_invocations'] += 1
    for entry in providers.values():
        coverage = entry['estimate_coverage']
        if coverage['completed']:
            coverage['ratio'] = coverage['available'] / coverage['completed']
    return {'schema': SCHEMA, 'schema_version': SCHEMA_VERSION, 'providers': dict(sorted(providers.items())),
            'duplicate_records_ignored': duplicates, 'measures_summed_together': False,
            'quota_snapshots': quota}


def _number(value, fmt='{:,}'):
    return 'unknown' if value is None else fmt.format(value)


def row_cost_text(row):
    """The cost cell of one ledger row: the measure it is, never a mixed figure."""
    kind, t = classify(row)
    if kind == 'legacy':
        amount = t['legacy_cost_usd']['amount']
        return 'unknown (legacy)' if amount is None else f'${amount:.4f} (legacy, unclassified)'
    if kind == 'no_invocation':
        return 'n/a'
    parts = []
    if t['spend']['availability'] == 'reported':
        parts.append(f"charge {t['spend']['amount']:.4f} {t['spend']['unit']}")
    if t['estimate']['availability'] == 'estimated':
        parts.append(f"estimate ${t['estimate']['amount']:.4f} (API-equivalent)")
    return '; '.join(parts) or 'unknown'


def _quota_text(quota):
    """Quota snapshots in the Evidence Pack: collected only for routes with an enabled control."""
    if not isinstance(quota, dict) or not quota.get('invocations_with_snapshots'):
        return 'Quota snapshots: not collected (no quota control enabled for the calls).'
    return (f"Quota snapshots: {quota['invocations_with_snapshots']} calls with snapshots before and after; "
            f"{quota['observed_account_deltas']} bucket deltas observed on the account (never the consumption "
            f"of the call), {quota['unknown_deltas']} unknown.")


def render(report):
    """Markdown section of the Evidence Pack."""
    rows = []
    for provider, e in report['providers'].items():
        spend_text = ('; '.join(f'{amount:.4f} {unit}' for unit, amount in sorted(e['spend']['by_unit'].items()))
                      or 'unknown') + f" ({e['spend']['reported']} reported, {e['spend']['unknown']} unknown)"
        estimate_text = (_number(e['estimate']['usd'], '${:.4f}')
                         + f" ({e['estimate']['estimated']} estimated, {e['estimate']['unknown']} unknown)")
        c = e['estimate_coverage']
        coverage_text = (f"{c['available']}/{c['completed']}" + (f" ({c['ratio']:.0%})" if c['ratio'] is not None else '')
                         + f"; incomplete (unknown): {c['incomplete_unknown']}")
        tk = e['tokens']
        token_text = ' / '.join(_number(tk['counts'][f]) for f in TOKEN_FIELDS) + (
            f" ({tk['reported']} reported, {tk['unknown']} unknown; {', '.join(tk['scopes']) or 'no scope'})")
        total_text = _number(tk['total']) + f" ({tk['total_invocations']} calls; {tk['total_unknown']} unknown)"
        legacy = e['legacy']
        legacy_text = (f"{legacy['rows']} rows, " + _number(legacy['unclassified_usd'], '${:.4f}')
                       + f" unclassified; {legacy['unknown_cost_rows']} unknown") if legacy['rows'] else 'none'
        rows.append(f"| `{provider}` | {e['invocations']['completed']} / {e['invocations']['incomplete']} | {spend_text} | "
                    f"{estimate_text} | {coverage_text} | {token_text} | {total_text} | {legacy_text} | "
                    f"{e['rows_without_telemetry']} |")
    return (f"Schema `{report['schema']}` v{report['schema_version']}. Charges, API-equivalent estimates and tokens "
            "are separate measures and are never added together; `unknown` means not reported, never zero. "
            "Estimate coverage: completed calls with an estimate, over all completed calls. Historical rows are "
            "annotated as legacy and never reclassified. " + _quota_text(report.get('quota_snapshots')) + "\n\n"
            "| Provider | Calls completed / incomplete | Charges (measured or reported) | API-equivalent estimate | "
            "Estimate coverage | Tokens input / cached input / cache creation / output / reasoning | "
            "Tokens total (no double counting) | Legacy rows | Rows without telemetry |\n"
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n" + ('\n'.join(rows) or '| none | | | | | | | | |')
            + f"\n\nDuplicate records of the same invocation ignored: {report['duplicate_records_ignored']}.\n")


# ---------------------------------------------------------------------------
# Separate caps and route admission (issue #165, part B1 of #14).
# ---------------------------------------------------------------------------

OPENROUTER_ANTHROPIC_URL = 'https://openrouter.ai/api'
# The measure each effective route needs. A CLI subscription route is excluded from the
# dollar caps only by an explicit declaration: the ``live_unknown_usage`` of the frozen
# profile or, without one, ``SSDLC_UNMETERED_QUOTA``.
ROUTE_MEASURES = {'openrouter_api': 'spend', 'claude_cli': 'estimate', 'codex_cli': 'declaration',
                  'agy_cli': 'declaration'}
# The measures each adapter really reports. The Claude CLI estimate is never a charge, even
# when the CLI is routed to OpenRouter.
ADAPTER_MEASURES = {'openrouter': ('spend',), 'claude': ('estimate',), 'codex': (), 'agy': ()}
ESTIMATE_CAP_DEFAULTS = {'S': 3., 'M': 6., 'L': 20.}
# Engines whose subscription CLI route an operator may declare as an unmetered quota.
UNMETERED_ENGINES = ('codex', 'agy')


class AdmissionRefused(ValueError):
    """The imminent call cannot be bounded by a configured cap: it is refused before launch."""


def estimate_cap(env, tier):
    """``SSDLC_ESTIMATE_CAP_USD``: cap on API-equivalent estimates, independent of
    ``SSDLC_BUDGET_CAP_USD`` and ``SSDLC_DEV_BUDGET_USD``. Unset: tier default (S 3, M 6,
    L 20 USD). Zero or less disables it; a non-finite value is refused."""
    raw = (env.get('SSDLC_ESTIMATE_CAP_USD') or '').strip()
    cap = float(raw) if raw else ESTIMATE_CAP_DEFAULTS.get((tier or 'M').upper(), 6.)
    if not math.isfinite(cap):
        raise ValueError('Non-finite SSDLC_ESTIMATE_CAP_USD')
    return cap if cap > 0 else None


def unmetered_declarations(env):
    """The operator's explicit ``unmetered_quota`` declarations for a run without a frozen
    profile: ``SSDLC_UNMETERED_QUOTA``, a comma-separated list of ``codex`` and ``agy``.
    Unset or empty: no declaration. Any other engine is refused (``ValueError``)."""
    engines = [name.strip().lower() for name in (env.get('SSDLC_UNMETERED_QUOTA') or '').split(',')
               if name.strip()]
    unknown = sorted(set(engines) - set(UNMETERED_ENGINES))
    if unknown:
        raise ValueError('SSDLC_UNMETERED_QUOTA names engines without a subscription route: ' + ', '.join(unknown))
    return {name: {'model': 'unmetered_quota', 'reason': 'SSDLC_UNMETERED_QUOTA'} for name in engines}


def admit(provider, route, *, spend_cap, estimate_cap_active, declarations=None):
    """The measure that bounds an imminent call on its effective ``route`` (``None``: unknown).

    A route that cannot be determined, or a measured route whose adapter reports no charge,
    is refused when a cap it would escape is in force: an estimate never stands for a charge.
    A CLI subscription route reports no dollar measure: it leaves the dollar caps only with
    an explicit declaration for its engine (``declarations``), and is refused without one
    while either cap is in force (see docs/cost-telemetry.md)."""
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


def ledger_measures(ledger):
    """The USD figures the separate caps read from a ledger, each invocation once.

    ``spend_usd``: charges reported in USD; ``estimate_usd``: API-equivalent estimates;
    ``unclassified_usd``: ``cost_usd`` of legacy rows, of rows without telemetry and of calls
    with neither a charge nor an estimate (incomplete ones included), which keeps counting against the spend cap as it always did (it is not
    reinterpreted as an estimate); ``unmeasured_spend``: completed calls on a measured route
    that reported no charge in USD; ``unmeasured_estimate``: completed calls on a route that
    needs an estimate (the Claude CLI) that reported none."""
    spend_usd = estimate_usd = unclassified_usd = 0
    unmeasured, unestimated, seen = [], [], set()
    for row in ledger or ():
        if not isinstance(row, dict):
            continue
        kind, t = classify(row)
        if kind != 'invocation':
            unclassified_usd += _amount(row.get('cost_usd')) or 0
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
            # Neither measure is known (an incomplete call, or a result recorded with every
            # measure unknown): its figure stays an unclassified one, never dropped.
            unclassified_usd += _amount(row.get('cost_usd')) or 0
        # A Claude CLI call records no route of its own: its route needs an estimate.
        route = t['route']['value']
        required = ROUTE_MEASURES.get(route) if route else ('estimate' if t['provider'] == 'claude' else None)
        if t['outcome'] == 'completed' and required == 'spend' and not charged:
            unmeasured.append(t['invocation_id'])
        if t['outcome'] == 'completed' and required == 'estimate' and not estimated:
            unestimated.append(t['invocation_id'])
    return {'spend_usd': spend_usd, 'estimate_usd': estimate_usd, 'unclassified_usd': unclassified_usd,
            'unmeasured_spend': unmeasured, 'unmeasured_estimate': unestimated}
