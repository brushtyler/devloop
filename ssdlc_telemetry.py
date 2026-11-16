"""Shared, versioned cost telemetry of one engine invocation (issue #164, part A of #14).

Four measures stay separate and are never added to one another:

- ``spend``: a charge the provider measured or reported, in its own currency;
- ``estimate``: an API-equivalent value estimated by the CLI (Claude ``total_cost_usd``),
  not a charge;
- ``tokens``: raw token counts, with the fields that overlap declared, so that no count is
  added twice, and whether the source reports them per turn or cumulatively;
- ``quota_snapshots``: a versioned container, always empty and ``unknown`` here; part C
  (#166) defines and collects its fields.

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
    """Empty and unavailable in part A: part C (#166) defines and collects the snapshots."""
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
            raise ValueError('Quota snapshots are not collected by this schema version')
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
            'quota_snapshots': quota_snapshots()}


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
            "annotated as legacy and never reclassified. Quota snapshots: not collected.\n\n"
            "| Provider | Calls completed / incomplete | Charges (measured or reported) | API-equivalent estimate | "
            "Estimate coverage | Tokens input / cached input / cache creation / output / reasoning | "
            "Tokens total (no double counting) | Legacy rows | Rows without telemetry |\n"
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n" + ('\n'.join(rows) or '| none | | | | | | | | |')
            + f"\n\nDuplicate records of the same invocation ignored: {report['duplicate_records_ignored']}.\n")
