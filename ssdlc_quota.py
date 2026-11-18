"""Subscription quota snapshots, deltas and explicit quota controls (issue #166, part C of #14).

Reading a quota never runs a model:

- agy: ``agy -p /usage --output-format json``, a local command that reports ``num_turns = 0``.
  An envelope with any other ``num_turns`` (or none) is not a quota read and is refused;
- Codex: the app-server method ``account/rateLimits/read``. ``rateLimitsByLimitId`` holds one
  snapshot per limit; ``rateLimits`` is the backward-compatible alias of one of them and is
  never counted a second time;
- Claude: no headless collector. Its quota is ``unknown``.

Each read has a time limit and is cached; a cached snapshot is reused only while it is younger
than the maximum age the caller accepts.

A snapshot is compared with another only for the same account identity, bucket, window and
reset. The delta is in percentage points of the bucket used (from 20% to 23% used: +3 pp). It is
an observation on the whole account (other clients may use it at the same time), never the
consumption of one call: resets, refills, disappeared or disabled buckets, ``null`` values,
stale snapshots, negative and zero deltas all give an ``unknown`` delta.

Quota controls are enabled explicitly, per route and per bucket, each with a mandatory minimum
reserve and a mandatory maximum snapshot age. An enabled control blocks the call when its
snapshot cannot be verified (missing, too old, invalid) or when the reserve is reached. Credits,
overage and paid routes are never used to avoid a block.

The account identity stays private: it is kept as a digest keyed with a secret of this process,
which is never written. The public form of a snapshot, the one that goes to ledgers and evidence,
holds no account identifier, email, plan or authentication payload.
"""
import hashlib
import hmac
import json
import math
import os
import threading
from datetime import datetime, timezone

SNAPSHOT_SCHEMA = 'ssdlc.quota-snapshot'
SCHEMA_VERSION = 1
ROUTES = {'agy_cli': 'agy', 'codex_cli': 'codex', 'claude_cli': 'claude'}
ENV = 'SSDLC_QUOTA_CONTROLS'
DEFAULT_TIMEOUT = 30
# Codex window lengths (minutes) with a conventional name; any other length is named in minutes.
CODEX_WINDOWS = {300: '5h', 10080: 'weekly'}
# Tolerated clock skew of a snapshot observed "in the future", in seconds.
CLOCK_SKEW = 5
_SECRET = os.urandom(32)
_cache, _cache_lock = {}, threading.Lock()


class QuotaBlocked(ValueError):
    """An enabled quota control blocks the call: reserve reached or snapshot not verifiable."""


def utc_now():
    """The clock of every snapshot, check and delta (one source, so tests can fix it)."""
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _parse_time(value):
    """An ISO 8601 instant with a timezone, as a UTC ISO string; else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return _iso(moment) if moment.tzinfo is not None else None


def _epoch_time(value):
    """A Unix timestamp in seconds, as a UTC ISO string; else None."""
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        return None
    try:
        return _iso(datetime.fromtimestamp(value, timezone.utc))
    except (OverflowError, OSError, ValueError):
        return None


def _instant(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def _percent(value):
    """A percentage in [0, 100], else None (unknown, never 0)."""
    if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100:
        return round(float(value), 6)
    return None


def account_ref(key):
    """The private identity of an account: a digest keyed with a secret of this process, so
    that two snapshots of this run can be compared without ever writing the identity."""
    if not isinstance(key, str) or not key:
        return None
    return hmac.new(_SECRET, key.encode(), hashlib.sha256).hexdigest()


def _snapshot(provider, status, *, buckets=(), observed_at=None, account=None, reason=None, source=None,
              synthetic=False):
    return {'schema': SNAPSHOT_SCHEMA, 'schema_version': SCHEMA_VERSION, 'provider': provider,
            'status': status, 'reason': reason, 'source': source,
            'observed_at': observed_at or _iso(utc_now()), 'buckets': list(buckets),
            'synthetic_fixture': bool(synthetic), 'account_ref': account_ref(account)}


def unavailable(provider, reason, observed_at=None, source=None):
    """A snapshot that could not be read: every bucket is unknown."""
    return _snapshot(provider, 'unavailable', reason=reason, observed_at=observed_at, source=source)


def _bucket(bucket_id, *, group, name, window, remaining, reset_time, enabled=True):
    remaining = _percent(remaining)
    return {'id': bucket_id, 'group': group, 'name': name, 'window': window, 'enabled': bool(enabled),
            'remaining_percent': remaining, 'used_percent': None if remaining is None else round(100 - remaining, 6),
            'reset_time': reset_time}


def from_agy_usage(envelope, *, observed_at=None, account=None):
    """Snapshot of ``agy -p /usage --output-format json`` (contract observed on agy 1.5.9).

    Only ``command.data.groups[].buckets[]`` is read; the tabular ``response`` text and every
    identifier of the envelope are ignored. ``remaining_fraction`` is a fraction in [0, 1]."""
    source = 'agy.usage'
    if not isinstance(envelope, dict) or envelope.get('status') != 'SUCCESS':
        return unavailable('agy', 'not_a_successful_usage_envelope', observed_at, source)
    if envelope.get('num_turns') != 0 or isinstance(envelope.get('num_turns'), bool):
        # Any turn means a model ran: this is not a quota read.
        return unavailable('agy', 'inference_detected', observed_at, source)
    command = envelope.get('command') if isinstance(envelope.get('command'), dict) else {}
    data = command.get('data') if isinstance(command.get('data'), dict) else {}
    groups = data.get('groups')
    if command.get('name') != 'usage' or not isinstance(groups, list):
        return unavailable('agy', 'malformed_usage_envelope', observed_at, source)
    buckets, seen = [], set()
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get('buckets'), list):
            return unavailable('agy', 'malformed_usage_envelope', observed_at, source)
        for raw in group['buckets']:
            bucket_id = raw.get('id') if isinstance(raw, dict) else None
            if not isinstance(bucket_id, str) or not bucket_id or bucket_id in seen:
                return unavailable('agy', 'malformed_usage_envelope', observed_at, source)
            seen.add(bucket_id)
            fraction = raw.get('remaining_fraction')
            valid = type(fraction) in (int, float) and math.isfinite(fraction) and 0 <= fraction <= 1
            buckets.append(_bucket(bucket_id, group=group.get('name') if isinstance(group.get('name'), str) else None,
                                   name=raw.get('name') if isinstance(raw.get('name'), str) else None,
                                   window=raw.get('window') if isinstance(raw.get('window'), str) else None,
                                   remaining=fraction * 100 if valid else None,
                                   reset_time=_parse_time(raw.get('reset_time')),
                                   # agy 1.5.9 reports no such flag; an explicit one is honoured.
                                   enabled=raw.get('enabled') is not False and raw.get('disabled') is not True))
    return _snapshot('agy', 'collected', buckets=buckets, observed_at=observed_at, account=account, source=source)


def _codex_window(minutes):
    if type(minutes) is not int or minutes <= 0:
        return None
    return CODEX_WINDOWS.get(minutes, f'{minutes}m')


def _codex_limit_buckets(limit_id, limit):
    buckets = []
    name = limit.get('limitName') if isinstance(limit.get('limitName'), str) else None
    for slot in ('primary', 'secondary'):
        window = limit.get(slot)
        if window is None:
            continue
        if not isinstance(window, dict):
            raise ValueError('malformed rate limit window')
        buckets.append(_bucket(f'{limit_id}.{slot}', group=name or limit_id, name=slot,
                               window=_codex_window(window.get('windowDurationMins')),
                               remaining=(100 - window['usedPercent'] if _percent(window.get('usedPercent')) is not None
                                          else None),
                               reset_time=_epoch_time(window.get('resetsAt'))))
    return buckets


def from_codex_rate_limits(result, *, observed_at=None, account=None, synthetic=False):
    """Snapshot of the Codex app-server ``account/rateLimits/read`` result.

    With ``rateLimitsByLimitId`` each limit is read once from it, and ``rateLimits`` (the
    backward-compatible alias of one of those limits) is not counted again. The alias is read
    alone only when the map is absent or empty. Credits, plan and account fields are ignored:
    a credit balance never offsets a bucket."""
    source = 'codex_app_server.account/rateLimits/read'
    if not isinstance(result, dict):
        return unavailable('codex', 'malformed_rate_limits', observed_at, source)
    by_id = result.get('rateLimitsByLimitId')
    alias = result.get('rateLimits')
    try:
        if isinstance(by_id, dict) and by_id:
            limits = list(by_id.items())
            if any(not isinstance(k, str) or not k or not isinstance(v, dict) for k, v in limits):
                raise ValueError('malformed rate limit map')
        elif isinstance(alias, dict):
            limit_id = alias.get('limitId') if isinstance(alias.get('limitId'), str) and alias.get('limitId') else 'codex'
            limits = [(limit_id, alias)]
        else:
            raise ValueError('no rate limits')
        buckets = [bucket for limit_id, limit in sorted(limits) for bucket in _codex_limit_buckets(limit_id, limit)]
    except (ValueError, KeyError, TypeError):
        return unavailable('codex', 'malformed_rate_limits', observed_at, source)
    return _snapshot('codex', 'collected', buckets=buckets, observed_at=observed_at, account=account, source=source,
                     synthetic=synthetic)


def claude_unknown(observed_at=None):
    """Claude has no headless quota collector: its quota is unknown."""
    return _snapshot('claude', 'unknown', reason='no_headless_collector', observed_at=observed_at)


# ---------------------------------------------------------------------------
# Collectors: one read with a time limit, cached.
# ---------------------------------------------------------------------------

AGY_USAGE_COMMAND = ('agy', '-p', '/usage', '--output-format', 'json')
CODEX_APP_SERVER_COMMAND = ('codex', 'app-server')
# The only JSON-RPC messages the Codex collector sends: no thread, no turn, no model.
CODEX_REQUESTS = (
    {'method': 'initialize', 'id': 1, 'params': {'clientInfo': {'name': 'ssdlc-quota', 'version': '1'}}},
    {'method': 'initialized'},
    {'method': 'account/rateLimits/read', 'id': 2},
)


def codex_request_stream():
    return ''.join(json.dumps(message) + '\n' for message in CODEX_REQUESTS)


def _codex_response(stdout):
    for line in (stdout or '').splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get('id') == 2:
            return message.get('result') if 'error' not in message else None
    return None


def collect(provider, run, *, account=None, max_age=None, timeout=DEFAULT_TIMEOUT, now=None):
    """One snapshot of ``provider`` through ``run(argv, stdin_text, timeout) -> (rc, stdout)``.

    A cached snapshot of the same provider and account is reused while it is at most
    ``max_age`` seconds old (``None``: never reused). A failed, timed out or unreadable read is
    an ``unavailable`` snapshot, never an exception and never a guess."""
    now = now or utc_now()
    key = (provider, account_ref(account))
    if max_age is not None:
        with _cache_lock:
            cached = _cache.get(key)
        if cached is not None and 0 <= (now - _instant(cached['observed_at'])).total_seconds() <= max_age:
            return dict(cached, cached=True)
    observed_at = _iso(now)
    if provider == 'claude':
        return claude_unknown(observed_at)
    if provider not in ('agy', 'codex'):
        return unavailable(provider, 'no_collector', observed_at)
    argv, stdin = ((list(AGY_USAGE_COMMAND), None) if provider == 'agy'
                   else (list(CODEX_APP_SERVER_COMMAND), codex_request_stream()))
    try:
        returncode, stdout = run(argv, stdin, timeout)
    except Exception:  # noqa: BLE001 - any failure of the read leaves the quota unverifiable
        return unavailable(provider, 'read_failed', observed_at)
    if returncode != 0:
        return unavailable(provider, 'read_failed', observed_at)
    if provider == 'agy':
        try:
            envelope = json.loads(stdout or '')
        except ValueError:
            envelope = None
        snapshot = from_agy_usage(envelope, observed_at=observed_at, account=account)
    else:
        snapshot = from_codex_rate_limits(_codex_response(stdout), observed_at=observed_at, account=account)
    if snapshot['status'] == 'collected':
        with _cache_lock:
            _cache[key] = snapshot
    return snapshot


def clear_cache():
    with _cache_lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# Deltas.
# ---------------------------------------------------------------------------

def _unknown_delta(bucket_id, reason):
    return {'bucket': bucket_id, 'status': 'unknown', 'delta_pp': None, 'reason': reason, 'call_consumption': None}


def delta(before, after, *, started_at, finished_at, max_age, concurrent=False):
    """Per-bucket change between a snapshot taken before a call and one taken after it.

    The delta is in percentage points of the bucket used. It is ``observed_account_delta`` only
    when it is positive and both snapshots belong to the same account, bucket, window and
    reset, are valid and are at most ``max_age`` seconds from the call. Otherwise it is
    ``unknown``: never a negative or a zero consumption. It is never the consumption of the
    call (``call_consumption`` stays ``None``): other clients of the account may use it."""
    ids = sorted({b['id'] for s in (before, after) if isinstance(s, dict) for b in s.get('buckets') or ()})
    for snapshot in (before, after):
        if not isinstance(snapshot, dict) or snapshot.get('status') != 'collected':
            return [_unknown_delta(i, 'snapshot_not_collected') for i in ids]
    if before['provider'] != after['provider'] or not before.get('account_ref') \
            or before['account_ref'] != after.get('account_ref'):
        return [_unknown_delta(i, 'account_not_verified') for i in ids]
    observed_before, observed_after = _instant(before['observed_at']), _instant(after['observed_at'])
    stale = ((started_at - observed_before).total_seconds() > max_age
             or (observed_after - finished_at).total_seconds() > max_age
             or (observed_before - started_at).total_seconds() > CLOCK_SKEW
             or (finished_at - observed_after).total_seconds() > CLOCK_SKEW)
    old = {b['id']: b for b in before['buckets']}
    new = {b['id']: b for b in after['buckets']}
    result = []
    for bucket_id in ids:
        a, b = old.get(bucket_id), new.get(bucket_id)
        if stale:
            result.append(_unknown_delta(bucket_id, 'stale_snapshot'))
        elif a is None or b is None:
            result.append(_unknown_delta(bucket_id, 'bucket_missing'))
        elif not a['enabled'] or not b['enabled']:
            result.append(_unknown_delta(bucket_id, 'bucket_disabled'))
        elif a['window'] is None or a['window'] != b['window']:
            result.append(_unknown_delta(bucket_id, 'window_changed'))
        elif a['reset_time'] is None or a['reset_time'] != b['reset_time']:
            result.append(_unknown_delta(bucket_id, 'reset'))
        elif a['used_percent'] is None or b['used_percent'] is None:
            result.append(_unknown_delta(bucket_id, 'value_null'))
        else:
            change = round(b['used_percent'] - a['used_percent'], 6)
            if change < 0:
                result.append(_unknown_delta(bucket_id, 'refill'))
            elif change == 0:
                result.append(_unknown_delta(bucket_id, 'no_change_observed'))
            else:
                result.append({'bucket': bucket_id, 'status': 'observed_account_delta', 'delta_pp': change,
                               'reason': 'concurrent_use' if concurrent else 'concurrent_use_not_excluded',
                               'call_consumption': None})
    return result


# ---------------------------------------------------------------------------
# Explicit controls.
# ---------------------------------------------------------------------------

def parse_controls(env):
    """``SSDLC_QUOTA_CONTROLS``: a JSON list of explicitly enabled controls, each
    ``{"route": "agy_cli"|"codex_cli"|"claude_cli", "bucket": "<bucket id>",
    "min_reserve_percent": 0 < n <= 100, "max_age_seconds": n > 0}``. Unset or empty: no
    control. Every field is mandatory; anything else raises ``ValueError``."""
    raw = (env.get('SSDLC_QUOTA_CONTROLS') or '').strip()
    if not raw:
        return []
    try:
        controls = json.loads(raw)
    except ValueError:
        raise ValueError(ENV + ' is not valid JSON') from None
    if not isinstance(controls, list):
        raise ValueError(ENV + ' must be a JSON list')
    result, seen = [], set()
    for control in controls:
        if not isinstance(control, dict) or set(control) != {'route', 'bucket', 'min_reserve_percent',
                                                              'max_age_seconds'}:
            raise ValueError(ENV + ': each control needs exactly route, bucket, min_reserve_percent '
                             'and max_age_seconds')
        route, bucket = control['route'], control['bucket']
        reserve, age = control['min_reserve_percent'], control['max_age_seconds']
        if route not in ROUTES or not isinstance(bucket, str) or not bucket.strip():
            raise ValueError(ENV + ': unknown route or empty bucket')
        if type(reserve) not in (int, float) or not math.isfinite(reserve) or not 0 < reserve <= 100:
            raise ValueError(ENV + ': min_reserve_percent must be in (0, 100]')
        if type(age) not in (int, float) or not math.isfinite(age) or age <= 0:
            raise ValueError(ENV + ': max_age_seconds must be positive')
        if (route, bucket) in seen:
            raise ValueError(ENV + ': duplicate control for ' + route + ' ' + bucket)
        seen.add((route, bucket))
        result.append({'route': route, 'bucket': bucket, 'min_reserve_percent': reserve, 'max_age_seconds': age})
    return result


def controls_for(controls, route):
    return [c for c in controls if c['route'] == route]


def check(controls, snapshot, *, now=None):
    """Apply the enabled ``controls`` of one route to ``snapshot``.

    Returns the evidence of each control when all pass. Raises ``QuotaBlocked`` when the
    snapshot cannot be verified for a control (not collected, too old, from the future, bucket
    missing, disabled or null, reset already passed) or when its reserve is reached. Nothing is
    ever offset by credits, overage or another route."""
    now = now or utc_now()
    evidence, blocked = [], []
    for control in controls:
        bucket_id, reserve = control['bucket'], control['min_reserve_percent']
        entry = {'route': control['route'], 'bucket': bucket_id, 'min_reserve_percent': reserve,
                 'max_age_seconds': control['max_age_seconds'], 'remaining_percent': None}
        reason = None
        if not isinstance(snapshot, dict) or snapshot.get('status') != 'collected':
            reason = 'snapshot not verifiable (' + str((snapshot or {}).get('reason') or 'missing') + ')'
        else:
            age = (now - _instant(snapshot['observed_at'])).total_seconds()
            bucket = next((b for b in snapshot['buckets'] if b['id'] == bucket_id), None)
            if age > control['max_age_seconds'] or age < -CLOCK_SKEW:
                reason = 'snapshot too old'
            elif bucket is None:
                reason = 'bucket missing'
            elif not bucket['enabled']:
                reason = 'bucket disabled'
            elif bucket['remaining_percent'] is None:
                reason = 'bucket value null'
            elif bucket['reset_time'] is not None and _instant(bucket['reset_time']) <= now:
                reason = 'bucket reset passed, snapshot outdated'
            else:
                entry['remaining_percent'] = bucket['remaining_percent']
                if bucket['remaining_percent'] <= reserve:
                    reason = f"reserve reached ({bucket['remaining_percent']:g}% remaining, minimum {reserve:g}%)"
        entry['result'] = 'blocked' if reason else 'passed'
        entry['reason'] = reason
        evidence.append(entry)
        if reason:
            blocked.append(f"{control['route']} {bucket_id}: {reason}")
    if blocked:
        raise QuotaBlocked('Quota control blocks the call: ' + '; '.join(blocked)
                           + '; no overage, credits or paid route is used instead; dispatch refused before launch')
    return evidence


# ---------------------------------------------------------------------------
# Evidence: the public form, without any account identity.
# ---------------------------------------------------------------------------

PUBLIC_SNAPSHOT_FIELDS = ('schema', 'schema_version', 'provider', 'status', 'reason', 'source', 'observed_at',
                          'buckets', 'synthetic_fixture')
PUBLIC_BUCKET_FIELDS = ('id', 'group', 'name', 'window', 'enabled', 'remaining_percent', 'used_percent',
                        'reset_time')


def public(snapshot):
    """The snapshot as it may be written to ledgers and evidence: no account identity."""
    if not isinstance(snapshot, dict):
        return None
    result = {k: snapshot.get(k) for k in PUBLIC_SNAPSHOT_FIELDS}
    result['buckets'] = [{k: b.get(k) for k in PUBLIC_BUCKET_FIELDS} for b in snapshot.get('buckets') or ()]
    return result


def container(route, before, after, deltas, controls_evidence):
    """The ``quota_snapshots`` container of one invocation's telemetry (version 1)."""
    return {'availability': 'reported', 'schema_version': SCHEMA_VERSION, 'status': 'collected', 'route': route,
            'snapshots': {'before': public(before), 'after': public(after)}, 'deltas': list(deltas),
            'controls': list(controls_evidence)}


def validate_container(value):
    """Raise ValueError unless ``value`` is a collected container with no account identity."""
    try:
        if (value['availability'] != 'reported' or value['schema_version'] != SCHEMA_VERSION
                or value['status'] != 'collected' or value['route'] not in ROUTES
                or set(value) != {'availability', 'schema_version', 'status', 'route', 'snapshots', 'deltas',
                                  'controls'}):
            raise ValueError('Invalid quota snapshots container')
        for key in ('before', 'after'):
            snapshot = value['snapshots'][key]
            if snapshot is not None and (set(snapshot) != set(PUBLIC_SNAPSHOT_FIELDS)
                                         or any(set(b) != set(PUBLIC_BUCKET_FIELDS) for b in snapshot['buckets'])):
                raise ValueError('A quota snapshot in evidence carries fields outside its public form')
        for d in value['deltas']:
            if d['call_consumption'] is not None:
                raise ValueError('A quota delta is never the consumption of the call')
            if d['status'] == 'observed_account_delta':
                if not (type(d['delta_pp']) in (int, float) and d['delta_pp'] > 0):
                    raise ValueError('An observed quota delta is positive')
            elif d['status'] != 'unknown' or d['delta_pp'] is not None:
                raise ValueError('Unknown is not zero: quota delta')
    except (KeyError, TypeError, AttributeError):
        raise ValueError('Malformed quota snapshots container') from None
    return value
