"""Issue #166 (part C of #14): subscription quota snapshots, deltas and explicit quota controls.

The agy fixture is the real output of ``agy -p /usage --output-format json`` (agy 1.5.9,
2026-11-16, ``conversation_id`` removed). The Codex fixture is SYNTHETIC, built from the
documented shape of ``account/rateLimits/read``: the live validation is the operator's (#14).
Every CLI here is a local fake: no model and no real CLI is ever run."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import base64
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import orchestrator
from orchestrator import CostAndTelemetryEntry, EngineRunner
import ssdlc_quota as q
import ssdlc_telemetry as t
from ssdlc_recovery import RecoveryStop

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'quota'
AGY_USAGE = json.loads((FIXTURES / 'agy-usage-1.5.9.json').read_text())
CODEX_FILE = json.loads((FIXTURES / 'codex-rate-limits-synthetic.json').read_text())
CODEX_RESULT = CODEX_FILE['response']['result']
# The moment the agy fixture was read: 3 hours 10 minutes before the 5-hour reset.
T0 = datetime(2026, 11, 16, 13, 29, 5, tzinfo=timezone.utc)
ACCOUNT = 'operator@example.com|/home/operator/.gemini'
SECRETS = ('operator@example.com', '/home/operator', 'conv-1234', 'acct-5678', 'sk-secret-token', '"pro"',
           'planType', 'credits')
ENV_VARIABLES = ('SSDLC_BUDGET_CAP_USD', 'SSDLC_ESTIMATE_CAP_USD', 'SSDLC_UNMETERED_QUOTA', 'SSDLC_QUOTA_CONTROLS',
                 'SSDLC_SUPERVISED_RUN_ID', 'OPENROUTER_API_KEY', 'ANTHROPIC_BASE_URL')


def iso(moment):
    return moment.isoformat().replace('+00:00', 'Z')


def agy_snapshot(fractions=None, *, at=T0, account=ACCOUNT, resets=None, extra=None):
    """The real agy fixture with some remaining fractions, resets or fields replaced."""
    envelope = copy.deepcopy(AGY_USAGE)
    for group in envelope['command']['data']['groups']:
        for bucket in group['buckets']:
            if fractions and bucket['id'] in fractions:
                bucket['remaining_fraction'] = fractions[bucket['id']]
            if resets and bucket['id'] in resets:
                bucket['reset_time'] = resets[bucket['id']]
            bucket.update((extra or {}).get(bucket['id'], {}))
    return q.from_agy_usage(envelope, observed_at=iso(at), account=account)


def controls(*entries):
    return json.dumps([dict(zip(('route', 'bucket', 'min_reserve_percent', 'max_age_seconds'), e)) for e in entries])


class AgyCollectorTests(unittest.TestCase):
    """Criterion 1: the agy collector reads the real fixture."""

    def test_reads_groups_buckets_fraction_reset_and_window_of_the_real_fixture(self):
        snapshot = q.from_agy_usage(AGY_USAGE, observed_at=iso(T0))
        self.assertEqual(snapshot['status'], 'collected')
        self.assertEqual(snapshot['provider'], 'agy')
        self.assertEqual(snapshot['source'], 'agy.usage')
        buckets = {b['id']: b for b in snapshot['buckets']}
        self.assertEqual(list(buckets), ['gemini-weekly', 'gemini-5h', '3p-weekly', '3p-5h', 'gemini-argon-wtu-weekly'])
        self.assertEqual({b['group'] for b in buckets.values()},
                         {'Gemini Models', 'Claude and GPT models', 'Gemini 4 Argon'})
        weekly = buckets['gemini-weekly']
        self.assertEqual((weekly['group'], weekly['name'], weekly['window'], weekly['reset_time']),
                         ('Gemini Models', 'Weekly Limit Remaining', 'weekly', '2026-11-20T22:51:37Z'))
        self.assertAlmostEqual(weekly['remaining_percent'], 83.607894, places=6)
        self.assertAlmostEqual(weekly['used_percent'], 16.392106, places=6)
        self.assertEqual(buckets['gemini-5h']['window'], '5h')
        self.assertAlmostEqual(buckets['gemini-5h']['remaining_percent'], 94.707155, places=6)
        self.assertEqual(buckets['3p-weekly']['remaining_percent'], 100.0)
        self.assertEqual(buckets['3p-5h']['reset_time'], '2026-11-16T18:28:33Z')
        # Argon exhausted: the state behind RESOURCE_EXHAUSTED in #148 and #151. A zero is a value.
        argon = buckets['gemini-argon-wtu-weekly']
        self.assertEqual((argon['remaining_percent'], argon['used_percent'], argon['group']),
                         (0.0, 100.0, 'Gemini 4 Argon'))
        self.assertTrue(all(b['enabled'] for b in buckets.values()))

    def test_the_collector_runs_only_the_usage_command_with_a_time_limit_and_caches(self):
        q.clear_cache()
        calls = []

        def run(argv, stdin, timeout):
            calls.append((argv, stdin, timeout))
            return 0, json.dumps(AGY_USAGE)
        first = q.collect('agy', run, account=ACCOUNT, max_age=60, now=T0)
        self.assertEqual(calls, [(['agy', '-p', '/usage', '--output-format', 'json'], None, q.DEFAULT_TIMEOUT)])
        self.assertEqual(first['status'], 'collected')
        cached = q.collect('agy', run, account=ACCOUNT, max_age=60, now=T0 + timedelta(seconds=30))
        self.assertEqual(len(calls), 1)
        self.assertTrue(cached['cached'])
        q.collect('agy', run, account=ACCOUNT, max_age=60, now=T0 + timedelta(seconds=61))
        self.assertEqual(len(calls), 2, 'an expired cache entry is read again')
        q.collect('agy', run, account=ACCOUNT, max_age=None, now=T0 + timedelta(seconds=62))
        self.assertEqual(len(calls), 3, 'max_age None never reuses the cache')
        q.clear_cache()

    def test_a_cached_snapshot_is_not_reused_once_one_of_its_resets_has_passed(self):
        q.clear_cache()
        calls = []

        def run(argv, stdin, timeout):
            calls.append(argv)
            return 0, json.dumps(AGY_USAGE)
        reset = datetime(2026, 11, 16, 16, 39, 5, tzinfo=timezone.utc)  # gemini-5h
        q.collect('agy', run, account=ACCOUNT, max_age=86400, now=reset - timedelta(minutes=2))
        q.collect('agy', run, account=ACCOUNT, max_age=86400, now=reset - timedelta(minutes=1))
        self.assertEqual(len(calls), 1)
        q.collect('agy', run, account=ACCOUNT, max_age=86400, now=reset)
        self.assertEqual(len(calls), 2, 'within max_age but after a reset: read again')
        q.clear_cache()

    def test_without_an_account_identity_nothing_is_cached(self):
        q.clear_cache()
        calls = []

        def run(argv, stdin, timeout):
            calls.append(argv)
            return 0, json.dumps(AGY_USAGE)
        for _ in range(2):
            snapshot = q.collect('agy', run, account=None, max_age=60, now=T0)
            self.assertNotIn('cached', snapshot)
        self.assertEqual(len(calls), 2)
        q.clear_cache()

    def test_failed_timed_out_or_unreadable_reads_are_unavailable(self):
        q.clear_cache()

        def timeout(argv, stdin, limit):
            raise subprocess.TimeoutExpired(argv, limit)
        for run, reason in ((timeout, 'read_failed'), (lambda *a: (1, ''), 'read_failed'),
                            (lambda *a: (0, 'not json'), 'not_a_successful_usage_envelope'),
                            (lambda *a: (0, json.dumps({'status': 'ERROR'})), 'not_a_successful_usage_envelope')):
            with self.subTest(reason=reason):
                snapshot = q.collect('agy', run, now=T0)
                self.assertEqual((snapshot['status'], snapshot['reason'], snapshot['buckets']),
                                 ('unavailable', reason, []))

    def test_malformed_or_out_of_range_values_are_null_never_zero(self):
        snapshot = agy_snapshot({'gemini-weekly': None, 'gemini-5h': 1.5, '3p-weekly': float('nan')},
                                resets={'3p-5h': 'tomorrow'})
        buckets = {b['id']: b for b in snapshot['buckets']}
        for name in ('gemini-weekly', 'gemini-5h', '3p-weekly'):
            self.assertIsNone(buckets[name]['remaining_percent'])
            self.assertIsNone(buckets[name]['used_percent'])
        self.assertIsNone(buckets['3p-5h']['reset_time'])


class CodexCollectorTests(unittest.TestCase):
    """Criterion 2: the Codex collector reads the declared synthetic fixture."""

    def test_the_fixture_is_declared_synthetic(self):
        self.assertTrue(CODEX_FILE['fixture'].startswith('SYNTHETIC'))
        self.assertIn('operator', CODEX_FILE['fixture'])

    def test_reads_rate_limits_by_limit_id_without_counting_the_alias_twice(self):
        snapshot = q.from_codex_rate_limits(CODEX_RESULT, observed_at=iso(T0), synthetic=True)
        self.assertEqual(snapshot['status'], 'collected')
        self.assertTrue(snapshot['synthetic_fixture'])
        buckets = {b['id']: b for b in snapshot['buckets']}
        # Two limits in rateLimitsByLimitId, one of them aliased by rateLimits: three buckets, not five.
        self.assertEqual(sorted(buckets), ['codex.primary', 'codex.secondary', 'codex_bengalfox.primary'])
        primary = buckets['codex.primary']
        self.assertEqual((primary['used_percent'], primary['remaining_percent'], primary['window'],
                          primary['reset_time']), (20.0, 80.0, '5h', '2026-11-16T16:00:00Z'))
        secondary = buckets['codex.secondary']
        self.assertEqual((secondary['used_percent'], secondary['window'], secondary['reset_time']),
                         (41.5, 'weekly', '2026-11-20T00:00:00Z'))
        spark = buckets['codex_bengalfox.primary']
        self.assertEqual((spark['group'], spark['remaining_percent'], spark['reset_time']),
                         ('GPT-5.3-Codex-Spark', 0.0, '2026-11-16T18:00:00Z'))

    def test_the_alias_alone_is_read_once_and_a_differing_alias_is_not_added(self):
        alone = q.from_codex_rate_limits({'rateLimits': CODEX_RESULT['rateLimits']}, observed_at=iso(T0))
        self.assertEqual([b['id'] for b in alone['buckets']], ['codex.primary', 'codex.secondary'])
        empty_map = q.from_codex_rate_limits({'rateLimits': CODEX_RESULT['rateLimits'], 'rateLimitsByLimitId': {}},
                                             observed_at=iso(T0))
        self.assertEqual(len(empty_map['buckets']), 2)
        # The alias is a view of a limit in the map: even if it lags behind, it is never added.
        lagging = copy.deepcopy(CODEX_RESULT)
        lagging['rateLimits']['primary']['usedPercent'] = 15
        snapshot = q.from_codex_rate_limits(lagging, observed_at=iso(T0))
        self.assertEqual(len(snapshot['buckets']), 3)
        self.assertEqual(next(b for b in snapshot['buckets'] if b['id'] == 'codex.primary')['used_percent'], 20.0)

    def test_malformed_results_are_unavailable(self):
        for result in (None, {}, {'rateLimits': None}, {'rateLimitsByLimitId': {'codex': 'x'}},
                       {'rateLimits': {'primary': 'x'}}):
            with self.subTest(result=result):
                self.assertEqual(q.from_codex_rate_limits(result)['status'], 'unavailable')

    def test_the_collector_sends_only_initialize_and_the_rate_limits_read(self):
        q.clear_cache()
        calls = []

        def run(argv, stdin, timeout):
            calls.append((argv, stdin, timeout))
            return 0, ('{"id": 1, "result": {"userAgent": "x"}}\n'
                       + json.dumps(CODEX_FILE['response']) + '\n')
        snapshot = q.collect('codex', run, account=ACCOUNT, now=T0)
        self.assertEqual(snapshot['status'], 'collected')
        self.assertEqual(len(snapshot['buckets']), 3)
        (argv, stdin, timeout), = calls
        self.assertEqual((argv, timeout), (['codex', 'app-server'], q.DEFAULT_TIMEOUT))
        methods = [json.loads(line)['method'] for line in stdin.splitlines()]
        self.assertEqual(methods, ['initialize', 'initialized', 'account/rateLimits/read'])
        error = q.collect('codex', lambda *a: (0, '{"id": 2, "error": {"message": "no auth"}}\n'), now=T0)
        self.assertEqual(error['status'], 'unavailable')

    def test_claude_has_no_headless_collector(self):
        calls = []
        snapshot = q.collect('claude', lambda *a: calls.append(a), now=T0)
        self.assertEqual((snapshot['status'], snapshot['reason'], snapshot['buckets']),
                         ('unknown', 'no_headless_collector', []))
        self.assertEqual(calls, [])


class DeltaTests(unittest.TestCase):
    """Criterion 3: deltas in percentage points; every doubtful case is unknown."""

    def deltas(self, before, after, *, started=T0, finished=T0 + timedelta(minutes=5), max_age=120, **kwargs):
        return {d['bucket']: d for d in q.delta(before, after, started_at=started, finished_at=finished,
                                                 max_age=max_age, **kwargs)}

    def test_from_20_to_23_percent_used_is_plus_3_points_observed_on_the_account(self):
        before = agy_snapshot({'gemini-weekly': 0.80})
        after = agy_snapshot({'gemini-weekly': 0.77}, at=T0 + timedelta(minutes=5))
        change = self.deltas(before, after)['gemini-weekly']
        self.assertEqual(change, {'bucket': 'gemini-weekly', 'status': 'observed_account_delta', 'delta_pp': 3.0,
                                  'reason': 'concurrent_use_not_excluded', 'call_consumption': None})

    def test_codex_delta_in_points(self):
        before = q.from_codex_rate_limits(CODEX_RESULT, observed_at=iso(T0), account=ACCOUNT)
        later = copy.deepcopy(CODEX_RESULT)
        later['rateLimitsByLimitId']['codex']['primary']['usedPercent'] = 27.5
        after = q.from_codex_rate_limits(later, observed_at=iso(T0 + timedelta(minutes=5)), account=ACCOUNT)
        changes = self.deltas(before, after)
        self.assertEqual(changes['codex.primary']['delta_pp'], 7.5)
        self.assertEqual(changes['codex.secondary']['reason'], 'no_change_observed')

    def test_doubtful_cases_are_unknown_never_negative_or_zero(self):
        later = T0 + timedelta(minutes=5)
        base = agy_snapshot({'gemini-weekly': 0.80})
        cases = {
            'reset': agy_snapshot({'gemini-weekly': 0.99}, at=later, resets={'gemini-weekly': '2026-11-27T22:51:37Z'}),
            'refill': agy_snapshot({'gemini-weekly': 0.90}, at=later),
            'no_change_observed': agy_snapshot({'gemini-weekly': 0.80}, at=later),
            'value_null': agy_snapshot({'gemini-weekly': None}, at=later),
            'bucket_disabled': agy_snapshot({'gemini-weekly': 0.70}, at=later,
                                            extra={'gemini-weekly': {'enabled': False}}),
            'window_changed': agy_snapshot({'gemini-weekly': 0.70}, at=later,
                                           extra={'gemini-weekly': {'window': '5h'}}),
        }
        for reason, after in cases.items():
            with self.subTest(reason=reason):
                change = self.deltas(base, after)['gemini-weekly']
                self.assertEqual((change['status'], change['delta_pp'], change['reason'], change['call_consumption']),
                                 ('unknown', None, reason, None))

    def test_a_disappeared_bucket_is_unknown(self):
        envelope = copy.deepcopy(AGY_USAGE)
        envelope['command']['data']['groups'].pop()
        after = q.from_agy_usage(envelope, observed_at=iso(T0 + timedelta(minutes=5)), account=ACCOUNT)
        change = self.deltas(agy_snapshot(), after)['gemini-argon-wtu-weekly']
        self.assertEqual((change['status'], change['reason']), ('unknown', 'bucket_missing'))

    def test_stale_snapshots_are_unknown(self):
        before = agy_snapshot({'gemini-weekly': 0.80}, at=T0 - timedelta(minutes=10))
        after = agy_snapshot({'gemini-weekly': 0.70}, at=T0 + timedelta(minutes=5))
        self.assertEqual(self.deltas(before, after, max_age=300)['gemini-weekly']['reason'], 'stale_snapshot')
        late = agy_snapshot({'gemini-weekly': 0.70}, at=T0 + timedelta(hours=1))
        self.assertEqual(self.deltas(agy_snapshot({'gemini-weekly': 0.80}), late, max_age=300)['gemini-weekly']['reason'],
                         'stale_snapshot')
        early = agy_snapshot({'gemini-weekly': 0.70}, at=T0 + timedelta(minutes=1))
        self.assertEqual(self.deltas(agy_snapshot({'gemini-weekly': 0.80}), early)['gemini-weekly']['reason'],
                         'stale_snapshot', 'a snapshot taken before the call ended does not close it')

    def test_other_accounts_or_unread_snapshots_are_unknown(self):
        later = T0 + timedelta(minutes=5)
        before = agy_snapshot({'gemini-weekly': 0.80})
        for after, reason in ((agy_snapshot({'gemini-weekly': 0.70}, at=later, account='someone-else'),
                               'account_not_verified'),
                              (agy_snapshot({'gemini-weekly': 0.70}, at=later, account=None), 'account_not_verified'),
                              (q.unavailable('agy', 'read_failed', iso(later)), 'snapshot_not_collected'),
                              (None, 'snapshot_not_collected')):
            with self.subTest(reason=reason):
                change = self.deltas(before, after)['gemini-weekly']
                self.assertEqual((change['status'], change['reason']), ('unknown', reason))

    def test_concurrent_use_is_an_account_delta_never_the_call_consumption(self):
        before = agy_snapshot({'gemini-weekly': 0.80})
        after = agy_snapshot({'gemini-weekly': 0.70}, at=T0 + timedelta(minutes=5))
        change = self.deltas(before, after, concurrent=True)['gemini-weekly']
        self.assertEqual((change['status'], change['delta_pp'], change['reason'], change['call_consumption']),
                         ('observed_account_delta', 10.0, 'concurrent_use', None))

    def test_the_container_refuses_negative_zero_or_attributed_deltas(self):
        before = agy_snapshot({'gemini-weekly': 0.80})
        after = agy_snapshot({'gemini-weekly': 0.77}, at=T0 + timedelta(minutes=5))
        deltas = q.delta(before, after, started_at=T0, finished_at=T0 + timedelta(minutes=5), max_age=60)
        good = q.container('agy_cli', before, after, deltas, [])
        self.assertIs(q.validate_container(good), good)
        for change in ({'status': 'observed_account_delta', 'delta_pp': -2.0},
                       {'status': 'observed_account_delta', 'delta_pp': 0},
                       {'status': 'unknown', 'delta_pp': 0},
                       {'status': 'observed_account_delta', 'delta_pp': 3.0, 'call_consumption': 3.0}):
            with self.subTest(change=change):
                bad = copy.deepcopy(good)
                bad['deltas'][0].update(change)
                with self.assertRaises(ValueError):
                    q.validate_container(bad)


class ControlTests(unittest.TestCase):
    """Criterion 4: an enabled control blocks on a reserve reached or an unverifiable snapshot."""

    def check(self, entries, snapshot, now=T0):
        return q.check(q.parse_controls({'SSDLC_QUOTA_CONTROLS': controls(*entries)}), snapshot, now=now)

    def test_controls_are_explicit_with_mandatory_reserve_and_age(self):
        self.assertEqual(q.parse_controls({}), [])
        self.assertEqual(q.parse_controls({'SSDLC_QUOTA_CONTROLS': ' '}), [])
        parsed = q.parse_controls({'SSDLC_QUOTA_CONTROLS': controls(('agy_cli', 'gemini-weekly', 10, 300))})
        self.assertEqual(parsed, [{'route': 'agy_cli', 'bucket': 'gemini-weekly', 'min_reserve_percent': 10,
                                   'max_age_seconds': 300}])
        for raw in ('not json', '{}', json.dumps([{'route': 'agy_cli', 'bucket': 'gemini-weekly',
                                                   'max_age_seconds': 300}]),
                    json.dumps([{'route': 'agy_cli', 'bucket': 'gemini-weekly', 'min_reserve_percent': 10}]),
                    controls(('agy_cli', 'gemini-weekly', 0, 300)), controls(('agy_cli', 'gemini-weekly', 10, 0)),
                    controls(('agy_cli', 'gemini-weekly', 101, 300)), controls(('agy_cli', '', 10, 300)),
                    controls(('openrouter_api', 'x', 10, 300)), controls(('agy_cli', 'b', True, 300)),
                    controls(('agy_cli', 'b', 10, 300), ('agy_cli', 'b', 20, 300))):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                q.parse_controls({'SSDLC_QUOTA_CONTROLS': raw})

    def test_a_reserve_above_the_minimum_passes(self):
        evidence = self.check([('agy_cli', 'gemini-weekly', 10, 300)], agy_snapshot())
        self.assertEqual(evidence[0]['result'], 'passed')
        self.assertAlmostEqual(evidence[0]['remaining_percent'], 83.607894, places=6)

    def test_a_reserve_reached_blocks_and_credits_never_offset_it(self):
        # Argon at 0%: its description offers AI credits; they are never used.
        with self.assertRaisesRegex(q.QuotaBlocked, 'reserve reached.*no overage, credits or paid route'):
            self.check([('agy_cli', 'gemini-argon-wtu-weekly', 5, 300)], agy_snapshot())
        with self.assertRaisesRegex(q.QuotaBlocked, 'reserve reached'):
            self.check([('agy_cli', 'gemini-weekly', 83.607894, 300)], agy_snapshot())
        # Codex: the Spark limit is used up while the account still holds credits.
        codex = q.from_codex_rate_limits(CODEX_RESULT, observed_at=iso(T0))
        with self.assertRaisesRegex(q.QuotaBlocked, 'codex_bengalfox.primary: reserve reached'):
            self.check([('codex_cli', 'codex_bengalfox.primary', 1, 300)], codex)

    def test_an_unverifiable_snapshot_blocks(self):
        envelope = copy.deepcopy(AGY_USAGE)
        envelope['command']['data']['groups'].pop()
        missing = q.from_agy_usage(envelope, observed_at=iso(T0))
        cases = {
            'missing': (None, 'gemini-weekly', T0),
            'read_failed': (q.unavailable('agy', 'read_failed', iso(T0)), 'gemini-weekly', T0),
            'too old': (agy_snapshot(), 'gemini-weekly', T0 + timedelta(seconds=301)),
            'from the future': (agy_snapshot(at=T0 + timedelta(minutes=5)), 'gemini-weekly', T0),
            'bucket missing': (missing, 'gemini-argon-wtu-weekly', T0),
            'bucket disabled': (agy_snapshot(extra={'gemini-weekly': {'disabled': True}}), 'gemini-weekly', T0),
            'bucket value null': (agy_snapshot({'gemini-weekly': None}), 'gemini-weekly', T0),
            'reset passed': (agy_snapshot(), 'gemini-5h', datetime(2026, 11, 16, 16, 40, tzinfo=timezone.utc)),
            'inference': (q.from_agy_usage(dict(AGY_USAGE, num_turns=1), observed_at=iso(T0)), 'gemini-weekly', T0),
        }
        for name, (snapshot, bucket, now) in cases.items():
            with self.subTest(name=name), self.assertRaises(q.QuotaBlocked):
                self.check([('agy_cli', bucket, 10, 300)], snapshot, now=now)

    def test_a_bucket_without_a_verifiable_window_or_reset_blocks(self):
        agy_cases = {
            'reset missing': {'gemini-weekly': {'reset_time': None}},
            'reset malformed': {'gemini-weekly': {'reset_time': 'invalid'}},
            'reset without timezone': {'gemini-weekly': {'reset_time': '2026-11-20T22:51:37'}},
            'window null': {'gemini-weekly': {'window': None}},
            'window not a string': {'gemini-weekly': {'window': 7}},
            'window empty': {'gemini-weekly': {'window': ''}},
            'window only blanks': {'gemini-weekly': {'window': ' \t\n'}},
        }
        for name, extra in agy_cases.items():
            with self.subTest(name=name), self.assertRaisesRegex(q.QuotaBlocked, 'not verifiable'):
                self.check([('agy_cli', 'gemini-weekly', 10, 300)], agy_snapshot(extra=extra))
        # The parser keeps no blank window, and the check refuses one even if a snapshot carried it.
        self.assertIsNone({b['id']: b for b in agy_snapshot(extra={'gemini-weekly': {'window': '  '}})['buckets']}
                          ['gemini-weekly']['window'])
        for blank in ('', '   '):
            snapshot = agy_snapshot()
            snapshot['buckets'][0]['window'] = blank
            with self.subTest(blank_window_in_snapshot=blank), \
                    self.assertRaisesRegex(q.QuotaBlocked, 'window not verifiable'):
                self.check([('agy_cli', 'gemini-weekly', 10, 300)], snapshot)
        for field in ('reset_time', 'window'):
            envelope = copy.deepcopy(AGY_USAGE)
            del envelope['command']['data']['groups'][0]['buckets'][0][field]
            with self.subTest(absent=field), self.assertRaisesRegex(q.QuotaBlocked, 'not verifiable'):
                self.check([('agy_cli', 'gemini-weekly', 10, 300)], q.from_agy_usage(envelope, observed_at=iso(T0)))
        codex_cases = {
            'reset missing': ('resetsAt', None), 'reset malformed': ('resetsAt', 'soon'),
            'reset absent': ('resetsAt', ...), 'window missing': ('windowDurationMins', None),
            'window malformed': ('windowDurationMins', 0), 'window absent': ('windowDurationMins', ...),
        }
        for name, (field, value) in codex_cases.items():
            result = copy.deepcopy(CODEX_RESULT)
            window = result['rateLimitsByLimitId']['codex']['primary']
            if value is ...:
                del window[field]
            else:
                window[field] = value
            with self.subTest(codex=name), self.assertRaisesRegex(q.QuotaBlocked, 'not verifiable'):
                self.check([('codex_cli', 'codex.primary', 10, 300)], q.from_codex_rate_limits(result, observed_at=iso(T0)))
        # The other buckets of the same snapshots stay usable.
        self.assertEqual(self.check([('agy_cli', 'gemini-5h', 10, 300)],
                                    agy_snapshot(extra={'gemini-weekly': {'reset_time': 'invalid'}}))[0]['result'],
                         'passed')

    def test_a_claude_control_blocks_because_its_quota_is_unknown(self):
        with self.assertRaisesRegex(q.QuotaBlocked, 'not verifiable \\(no_headless_collector\\)'):
            self.check([('claude_cli', 'any', 10, 300)], q.claude_unknown(iso(T0)))


class NoInferenceAndPrivacyTests(unittest.TestCase):
    """Criterion 5: no quota read runs a model; no account identifier reaches the evidence."""

    def test_an_envelope_with_turns_is_not_a_quota_read(self):
        for turns in (1, None, False, '0'):
            with self.subTest(turns=turns):
                envelope = dict(AGY_USAGE, num_turns=turns)
                if turns is None:
                    envelope.pop('num_turns')
                snapshot = q.from_agy_usage(envelope, observed_at=iso(T0))
                self.assertEqual((snapshot['status'], snapshot['buckets']), ('unavailable', []))
        self.assertEqual(q.from_agy_usage(dict(AGY_USAGE, num_turns=1))['reason'], 'inference_detected')

    def test_the_codex_collector_never_starts_a_thread_or_a_turn(self):
        methods = {m['method'] for m in q.CODEX_REQUESTS}
        self.assertEqual(methods, {'initialize', 'initialized', 'account/rateLimits/read'})
        self.assertFalse(any(name.startswith(('thread/', 'turn/')) for name in methods))
        self.assertNotIn('exec', q.CODEX_APP_SERVER_COMMAND)

    def test_public_snapshots_and_containers_carry_no_account_identity(self):
        envelope = copy.deepcopy(AGY_USAGE)
        envelope.update(conversation_id='conv-1234', account_id='acct-5678', email='operator@example.com')
        envelope['command']['data']['groups'][0]['buckets'][0]['account'] = 'acct-5678'
        before = q.from_agy_usage(envelope, observed_at=iso(T0), account=ACCOUNT)
        codex_result = copy.deepcopy(CODEX_RESULT)
        codex_result['rateLimitsByLimitId']['codex'].update(email='operator@example.com', token='sk-secret-token')
        codex = q.from_codex_rate_limits(codex_result, observed_at=iso(T0), account=ACCOUNT)
        self.assertTrue(before['account_ref'])
        self.assertNotIn(ACCOUNT, json.dumps(before))
        after = q.from_agy_usage(envelope, observed_at=iso(T0 + timedelta(minutes=5)), account=ACCOUNT)
        deltas = q.delta(before, after, started_at=T0, finished_at=T0 + timedelta(minutes=5), max_age=60)
        evidence = json.dumps([q.public(before), q.public(codex),
                               q.container('agy_cli', before, after, deltas, [])])
        for secret in SECRETS + (before['account_ref'],):
            self.assertNotIn(secret, evidence)
        for field in ('account_ref', 'account_id', '"account"', 'email', 'token', 'conversation_id'):
            self.assertNotIn(field, evidence)
        leaking = q.container('agy_cli', before, after, deltas, [])
        leaking['snapshots']['before']['account_ref'] = before['account_ref']
        with self.assertRaises(ValueError):
            q.validate_container(leaking)

    def test_the_account_identity_is_keyed_by_a_secret_of_the_process(self):
        import hashlib
        self.assertNotEqual(q.account_ref(ACCOUNT), hashlib.sha256(ACCOUNT.encode()).hexdigest())
        self.assertEqual(q.account_ref(ACCOUNT), q.account_ref(ACCOUNT))
        self.assertIsNone(q.account_ref(''))

    def test_the_identity_follows_the_credentials_the_engine_uses(self):
        def auth(sub, access, refresh='rt-1', account='acct-5678'):
            return json.dumps({'OPENAI_API_KEY': None, 'tokens': {
                'id_token': jwt({'iss': 'https://auth.example', 'sub': sub, 'email': 'operator@example.com'}),
                'access_token': access, 'refresh_token': refresh, 'account_id': account}})
        same = q.credential_identity('codex', auth('user-1', 'at-1'))
        # A token refresh rewrites the file but keeps the account.
        self.assertEqual(same, q.credential_identity('codex', auth('user-1', 'at-2', refresh='rt-2')))
        for other in (auth('user-2', 'at-1'), auth('user-1', 'at-1', account='acct-9999')):
            self.assertNotEqual(q.account_ref(same), q.account_ref(q.credential_identity('codex', other)))
        # Without an id_token: the refresh token; an opaque store: its whole content.
        self.assertNotEqual(q.credential_identity('agy', json.dumps({'refresh_token': 'a', 'access_token': 'x'})),
                            q.credential_identity('agy', json.dumps({'refresh_token': 'b', 'access_token': 'x'})))
        self.assertEqual(q.credential_identity('agy', json.dumps({'refresh_token': 'a', 'access_token': 'x'})),
                         q.credential_identity('agy', json.dumps({'refresh_token': 'a', 'access_token': 'y'})))
        self.assertNotEqual(q.credential_identity('agy', 'opaque-1'), q.credential_identity('agy', 'opaque-2'))
        for missing in (None, '', '  \n'):
            self.assertIsNone(q.credential_identity('codex', missing))
        # The identity only reaches the evidence as nothing at all.
        snapshot = q.from_codex_rate_limits(CODEX_RESULT, observed_at=iso(T0), account=same)
        evidence = json.dumps(q.public(snapshot))
        for secret in ('user-1', 'acct-5678', 'operator@example.com', 'rt-1', snapshot['account_ref']):
            self.assertNotIn(secret, evidence)


def jwt(claims):
    part = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    return part({'alg': 'none'}) + '.' + part(claims) + '.signature'


class QuotaContainerTests(unittest.TestCase):
    """In the role container the quota read receives the authentication of its own engine only."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.home, self.repo = root / 'home', root / 'repo'
        self.codex_home = self.repo / '.ssdlc' / '.runtime' / 'codex_home'
        self.codex_home.mkdir(parents=True)
        (self.codex_home / 'auth.json').write_text(json.dumps({'refresh_token': 'codex-rt'}))
        for name, text in (('.codex/auth.json', '{}'), ('.codex/version.json', '{}'),
                           ('.claude/.credentials.json', '{}'), ('.claude.json', '{}'),
                           ('.gemini/antigravity-cli/antigravity-oauth-token', '{"refresh_token": "agy-rt"}')):
            (self.home / name).parent.mkdir(parents=True, exist_ok=True)
            (self.home / name).write_text(text)
        env = patch.dict(os.environ, {'CODEX_HOME': str(self.codex_home), 'OPENAI_API_KEY': 'sk-secret-token',
                                      'ANTHROPIC_API_KEY': 'sk-ant-secret', 'OPENROUTER_API_KEY': 'sk-or-secret',
                                      'CLAUDE_CONFIG_DIR': str(self.repo / '.ssdlc/.runtime/claude_home')})
        self.commands = []

        def run(cmd, **kwargs):
            self.commands.append(list(cmd))
            stdout = json.dumps(CODEX_FILE['response']) + '\n' if 'app-server' in cmd else json.dumps(AGY_USAGE)
            return subprocess.CompletedProcess(cmd, 0, stdout, '')
        for target in (env, patch('pathlib.Path.home', return_value=self.home),
                       patch.object(EngineRunner, '_should_spawn_role_container', return_value=True),
                       patch.object(EngineRunner, '_run_role_command', side_effect=run)):
            target.start()
            self.addCleanup(target.stop)
        q.clear_cache()
        self.addCleanup(q.clear_cache)

    def test_the_codex_read_gets_the_codex_authentication_of_the_engine_and_nothing_else(self):
        snapshot = orchestrator._quota_snapshot('codex', self.repo, None)
        self.assertEqual(snapshot['status'], 'collected')
        cmd, = self.commands
        self.assertEqual(cmd[:2], ['docker', 'run'])
        self.assertEqual(cmd[-2:], ['codex', 'app-server'])
        self.assertIn('-i', cmd)
        engine = EngineRunner._wrap_in_role_container(['codex', 'exec', '-'], cwd=self.repo, stdin=True)
        # The same authentication as run_codex: the auth.json, the version and the isolated CODEX_HOME.
        for arg in (f"{self.home / '.codex/auth.json'}:/root/.codex/auth.json:ro",
                    f"{self.home / '.codex/version.json'}:/root/.codex/version.json:ro",
                    f'CODEX_HOME={self.codex_home}'):
            self.assertIn(arg, cmd)
            self.assertIn(arg, engine)
        self.assertIn(f'{self.codex_home}:{self.codex_home}:rw', cmd)
        # ...and nothing else: no other engine's store, no API key, no whole runtime directory.
        self.assertIn(f"{self.repo / '.ssdlc'}:rw,size=16m", cmd)
        runtime = self.repo / '.ssdlc' / '.runtime'
        self.assertNotIn(f'{runtime}:{runtime}:rw', cmd)
        text = ' '.join(cmd)
        for secret in ('.claude', '.gemini', 'sk-secret-token', 'sk-ant-secret', 'sk-or-secret', 'CLAUDE_CONFIG_DIR'):
            self.assertNotIn(secret, text)
        # The identity is the one of the CODEX_HOME credentials the engine reads.
        self.assertEqual(orchestrator._quota_credentials('codex', self.repo, True), self.codex_home / 'auth.json')
        self.assertEqual(snapshot['account_ref'], q.account_ref(q.credential_identity('codex', '{"refresh_token": "codex-rt"}')))

    def test_the_agy_read_gets_only_its_oauth_copy(self):
        snapshot = orchestrator._quota_snapshot('agy', self.repo, None)
        self.assertEqual(snapshot['status'], 'collected')
        cmd, = self.commands
        agy_home = self.repo / '.ssdlc/.runtime/agy_auth'
        self.assertIn(f'{agy_home}:/root/.gemini:rw', cmd)
        text = ' '.join(cmd)
        for secret in ('.codex', 'CODEX_HOME', '.claude', 'sk-secret-token', 'sk-ant-secret', 'sk-or-secret'):
            self.assertNotIn(secret, text)
        self.assertEqual(snapshot['account_ref'], q.account_ref(q.credential_identity('agy', '{"refresh_token": "agy-rt"}')))


def cli(stdout, code=0):
    return subprocess.CompletedProcess([], code, stdout, '')


AGY_ANSWER = {'status': 'SUCCESS', 'num_turns': 2, 'response': 'agy answer',
              'usage': {'input_tokens': 10, 'output_tokens': 5, 'thinking_tokens': 1, 'cache_read_tokens': 0}}


class OrchestratorQuotaTests(unittest.TestCase):
    """The controls applied before the engine is launched, and the snapshots in its telemetry."""

    def setUp(self):
        self.patcher = patch.dict(os.environ)
        self.patcher.start()
        for name in ENV_VARIABLES:
            os.environ.pop(name, None)
        os.environ.update(SSDLC_UNMETERED_QUOTA='agy,codex')
        q.clear_cache()
        self.now = [T0]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # The authentication file the engine uses: the account identity is derived from it.
        self.credentials = Path(tmp.name) / 'credentials.json'
        self.credentials.write_text(json.dumps({'refresh_token': 'operator-refresh-token'}))
        for target in (patch.object(q, 'utc_now', side_effect=lambda: self.now[0]),
                       patch.object(EngineRunner, '_should_spawn_role_container', return_value=False),
                       patch.object(orchestrator, '_quota_credentials', side_effect=lambda *a: self.credentials)):
            target.start()
            self.addCleanup(target.stop)

    def tearDown(self):
        self.patcher.stop()
        q.clear_cache()

    def fake(self, usage_before, usage_after=None, answer=AGY_ANSWER, during=None):
        """subprocess.run of the role commands: /usage reads and the agy call itself."""
        self.commands = []
        reads = [usage_before, usage_after if usage_after is not None else usage_before]

        def run(cmd, **kwargs):
            self.commands.append(list(cmd))
            if cmd[:3] == ['agy', '-p', '/usage']:
                return cli(json.dumps(reads.pop(0)))
            if cmd[:2] == ['codex', 'app-server']:
                return cli(json.dumps(reads.pop(0)) + '\n')
            self.now[0] += timedelta(minutes=5)
            if during:
                during()
            return cli(json.dumps(answer))
        return patch('orchestrator.subprocess.run', side_effect=run)

    def test_without_a_control_no_quota_is_read(self):
        with self.fake(AGY_USAGE):
            out = EngineRunner.run_agy('task', Path.cwd())
        self.assertEqual(out[0], 'agy answer')
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.commands[0][:2], ['agy', '-p'])
        self.assertNotEqual(self.commands[0][2], '/usage')
        self.assertEqual(out.telemetry['quota_snapshots'], t.quota_snapshots())

    def test_a_reserve_reached_refuses_the_call_before_launch_without_overage(self):
        os.environ['SSDLC_QUOTA_CONTROLS'] = controls(('agy_cli', 'gemini-argon-wtu-weekly', 5, 300))
        with self.fake(AGY_USAGE), patch.object(EngineRunner, 'call_openrouter') as paid, \
                patch.object(EngineRunner, 'run_codex') as other:
            with self.assertRaises(RecoveryStop) as stop:
                EngineRunner.run_agy('task', Path.cwd(), model='gemini-4-argon-high')
        self.assertEqual(stop.exception.category, 'LIMIT_REACHED')
        self.assertIn('reserve reached', str(stop.exception))
        self.assertIn('refused before launch', str(stop.exception))
        self.assertEqual(self.commands, [['agy', '-p', '/usage', '--output-format', 'json']])
        paid.assert_not_called()
        other.assert_not_called()

    def test_an_unverifiable_snapshot_refuses_the_call(self):
        os.environ['SSDLC_QUOTA_CONTROLS'] = controls(('agy_cli', 'gemini-weekly', 10, 300))
        unverifiable_buckets = []
        for field, value in (('window', ''), ('window', '  '), ('window', None), ('reset_time', 'invalid')):
            envelope = copy.deepcopy(AGY_USAGE)
            envelope['command']['data']['groups'][0]['buckets'][0][field] = value
            unverifiable_buckets.append(envelope)
        for usage in ({'status': 'ERROR'}, dict(AGY_USAGE, num_turns=1), *unverifiable_buckets):
            # Only the /usage read runs: the model command is never launched.
            with self.subTest(usage=usage), self.fake(usage):
                q.clear_cache()
                with self.assertRaisesRegex(RecoveryStop, 'not verifiable'):
                    EngineRunner.run_agy('task', Path.cwd())
                self.assertEqual(len(self.commands), 1)
        self.now[0] = datetime(2026, 11, 16, 16, 40, tzinfo=timezone.utc)
        os.environ['SSDLC_QUOTA_CONTROLS'] = controls(('agy_cli', 'gemini-5h', 10, 300))
        with self.fake(AGY_USAGE), self.assertRaisesRegex(RecoveryStop, 'reset passed'):
            EngineRunner.run_agy('task', Path.cwd())

    def test_a_malformed_configuration_refuses_every_call(self):
        os.environ['SSDLC_QUOTA_CONTROLS'] = controls(('agy_cli', 'gemini-weekly', 10, None))
        with self.fake(AGY_USAGE), self.assertRaisesRegex(RecoveryStop, 'max_age_seconds'):
            EngineRunner.run_agy('task', Path.cwd())
        self.assertEqual(self.commands, [])

    def test_a_claude_control_refuses_the_claude_cli(self):
        os.environ.update(SSDLC_QUOTA_CONTROLS=controls(('claude_cli', 'weekly', 10, 300)))
        with self.fake(AGY_USAGE), self.assertRaisesRegex(RecoveryStop, 'no_headless_collector'):
            EngineRunner.run_claude('task', Path.cwd())
        self.assertEqual(self.commands, [])

    def test_a_codex_limit_used_up_refuses_codex_even_with_credits(self):
        os.environ['SSDLC_QUOTA_CONTROLS'] = controls(('codex_cli', 'codex_bengalfox.primary', 1, 300))
        with self.fake(CODEX_FILE['response']), self.assertRaisesRegex(RecoveryStop, 'reserve reached'):
            EngineRunner.run_codex('task', Path.cwd())
        self.assertEqual([c[:2] for c in self.commands], [['codex', 'app-server']])

    def test_snapshots_before_and_after_and_their_delta_go_to_the_telemetry_without_identity(self):
        os.environ['SSDLC_QUOTA_CONTROLS'] = controls(('agy_cli', 'gemini-weekly', 10, 300))
        after = copy.deepcopy(AGY_USAGE)
        after['command']['data']['groups'][0]['buckets'][0]['remaining_fraction'] = 0.806078941822052
        after['conversation_id'] = 'conv-1234'
        with self.fake(AGY_USAGE, after):
            out = EngineRunner.run_agy('task', Path.cwd())
        self.assertEqual(out[0], 'agy answer')
        self.assertEqual([c[2] for c in self.commands], ['/usage', 'task', '/usage'])
        quota = out.telemetry['quota_snapshots']
        self.assertEqual((quota['status'], quota['route']), ('collected', 'agy_cli'))
        changes = {d['bucket']: d for d in quota['deltas']}
        self.assertEqual(changes['gemini-weekly']['status'], 'observed_account_delta')
        self.assertAlmostEqual(changes['gemini-weekly']['delta_pp'], 3.0, places=6)
        self.assertEqual(changes['gemini-5h']['reason'], 'no_change_observed')
        self.assertEqual(quota['controls'][0]['result'], 'passed')
        row = CostAndTelemetryEntry('03-dev', 'Role', 'agy', 'm', 'GENERATED', 1.0,
                                    telemetry=out.telemetry).to_dict()
        self.assertEqual(row['telemetry']['quota_snapshots'], quota)
        evidence = json.dumps(row) + t.render(t.breakdown([row]))
        for secret in ('conv-1234', 'account_ref', str(Path.home())):
            self.assertNotIn(secret, evidence)
        report = t.breakdown([row])['quota_snapshots']
        self.assertEqual((report['invocations_with_snapshots'], report['observed_account_deltas'],
                          report['unknown_deltas']), (1, 1, 4))
        self.assertIn('1 bucket deltas observed on the account', t.render(t.breakdown([row])))

    def test_a_failed_read_after_the_call_leaves_the_delta_unknown(self):
        os.environ['SSDLC_QUOTA_CONTROLS'] = controls(('agy_cli', 'gemini-weekly', 10, 300))
        with self.fake(AGY_USAGE, {'status': 'ERROR'}):
            out = EngineRunner.run_agy('task', Path.cwd())
        self.assertEqual(out[0], 'agy answer')
        quota = out.telemetry['quota_snapshots']
        self.assertEqual(quota['snapshots']['after']['status'], 'unavailable')
        self.assertTrue(all(d['status'] == 'unknown' and d['delta_pp'] is None for d in quota['deltas']))

    def test_another_call_on_the_engine_during_this_one_is_concurrent_use(self):
        seen = []
        gate = {'provider': 'agy'}
        with patch.object(orchestrator, '_with_quota',
                          side_effect=lambda result, gate, started, concurrent: seen.append(concurrent) or result):
            orchestrator._quota_call(lambda: orchestrator._quota_call(lambda: 'inner', gate, (), {}), gate, (), {})
            orchestrator._quota_call(lambda: 'alone', gate, (), {})
        self.assertEqual(seen, [True, True, False])

    def test_a_nested_dispatch_is_gated_and_read_once(self):
        # invoke_role -> run_agy, unsupervised and without a frozen profile.
        os.environ['SSDLC_QUOTA_CONTROLS'] = controls(('agy_cli', 'gemini-weekly', 10, 300))
        after = copy.deepcopy(AGY_USAGE)
        after['command']['data']['groups'][0]['buckets'][0]['remaining_fraction'] = 0.806078941822052
        with self.fake(AGY_USAGE, after):
            out = EngineRunner.invoke_role('agy', 'gemini-3.8-flash-low', 'system', 'user', Path.cwd(), 'p', 't', 'review')
        self.assertEqual(out[0], 'agy answer')
        self.assertEqual([c[2] == '/usage' for c in self.commands], [True, False, True],
                         'one read before and one after the whole call, not two of each')
        change = {d['bucket']: d for d in out.telemetry['quota_snapshots']['deltas']}['gemini-weekly']
        self.assertEqual((change['status'], change['reason']), ('observed_account_delta', 'concurrent_use_not_excluded'))
        self.assertAlmostEqual(change['delta_pp'], 3.0, places=6)
        self.assertEqual(getattr(orchestrator._dispatch_local, 'depth', 0), 0)

    def test_changed_credentials_in_the_same_file_are_another_account(self):
        os.environ['SSDLC_QUOTA_CONTROLS'] = controls(('agy_cli', 'gemini-weekly', 10, 3600))
        after = copy.deepcopy(AGY_USAGE)
        after['command']['data']['groups'][0]['buckets'][0]['remaining_fraction'] = 0.806078941822052

        def switch():
            self.credentials.write_text(json.dumps({'refresh_token': 'another-account-refresh-token'}))
        with self.fake(AGY_USAGE, after, during=switch):
            out = EngineRunner.run_agy('task', Path.cwd())
        deltas = out.telemetry['quota_snapshots']['deltas']
        self.assertTrue(all((d['status'], d['reason']) == ('unknown', 'account_not_verified') for d in deltas))
        # The snapshot read after the call belongs to the new account: reused within its age.
        with self.fake(after):
            EngineRunner.run_agy('task', Path.cwd())
        self.assertEqual([c[2] for c in self.commands], ['task', '/usage'])
        # Same path, other credentials: the cached snapshot is not reused, the quota is read again.
        self.credentials.write_text(json.dumps({'refresh_token': 'a-third-account-refresh-token'}))
        with self.fake(after):
            EngineRunner.run_agy('task', Path.cwd())
        self.assertEqual([c[2] for c in self.commands], ['/usage', 'task', '/usage'])

    def test_without_verifiable_credentials_nothing_is_cached_and_deltas_are_unknown(self):
        os.environ['SSDLC_QUOTA_CONTROLS'] = controls(('agy_cli', 'gemini-weekly', 10, 3600))
        self.credentials.unlink()
        after = copy.deepcopy(AGY_USAGE)
        after['command']['data']['groups'][0]['buckets'][0]['remaining_fraction'] = 0.806078941822052
        with self.fake(AGY_USAGE, after):
            out = EngineRunner.run_agy('task', Path.cwd())
        deltas = out.telemetry['quota_snapshots']['deltas']
        self.assertTrue(all((d['status'], d['reason']) == ('unknown', 'account_not_verified') for d in deltas))
        with self.fake(after):
            EngineRunner.run_agy('task', Path.cwd())
        self.assertEqual([c[2] for c in self.commands], ['/usage', 'task', '/usage'])

    def test_the_variable_is_a_known_project_setting(self):
        self.assertIn('SSDLC_QUOTA_CONTROLS', orchestrator.KNOWN_PROJECT_ENV_VARS)


if __name__ == '__main__':
    unittest.main()
