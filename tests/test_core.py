"""The `core` package (ADR 0004, P2-1): pure budget validators, charges, capacity and telemetry.

These tests use `core` alone. `tests/test_core_equivalence.py` compares it with the engine.
"""
import copy
import json
import unittest
from pathlib import Path

from core import budget, schema, telemetry

ROOT = Path(__file__).resolve().parents[1]
# The engine's schema of the limits profile, kept as a fixture of core's tests when the engine's
# schemas/ left main (ADR 0007, R1).
PROFILE_SCHEMA = json.loads((ROOT / 'tests' / 'fixtures' / 'core' / 'recovery-limits.schema.json')
                            .read_text(encoding='utf-8'))
REASON = 'Subscription CLI bounded by the invocation caps'


def profile(**overrides):
    p = {'schema_version': 1, 'profile_id': 'core-fixture', 'invocations': 10, 'wall_seconds': 1000,
         'role_invocations': {'03-dev': 3, '04-review': 3}, 'phase_invocations': {'03-dev': 3, '04-review': 3},
         'transport_retries': 2, 'design_diagnoses': 1, 'metered_usd': 10, 'cli_estimate_usd': 10,
         'development_attempts': 2, 'design_rounds': 5, 'invocation_timeout_seconds': 30,
         'live_unknown_usage': {agent: {'model': 'unmetered_quota', 'reason': REASON} for agent in ('codex', 'agy')}}
    p.update(overrides)
    return p


def reserve(amount, *agents):
    return {agent: {'model': 'reserve_per_invocation', 'metered_usd': amount, 'cli_estimate_usd': amount,
                    'reason': REASON} for agent in agents}


def state(*rows, **extra):
    s = {'started_at': 0, 'deadline_at': 1000, 'invocations': {f'i{n}': row for n, row in enumerate(rows)},
         'transports': [], 'diagnoses': []}
    s.update(extra)
    return s


def row(engine, status='returned', role='03-dev', phase='03-dev', usage=None, **extra):
    r = {'role': role, 'phase': phase, 'engine': engine, 'status': status,
         'usage': usage or {'metered_usd': None, 'cli_estimate_usd': None}}
    r.update(extra)
    return r


def envelope(policy=None, **binding):
    return {'policy': policy or profile(), 'binding': dict({'ticket': 'T1'}, **binding)}


class SchemaTests(unittest.TestCase):
    def test_types_follow_json_schema(self):
        integer = {'type': 'integer'}
        self.assertEqual(schema.errors(integer, 3), [])
        self.assertEqual(schema.errors(integer, 3.0), [])
        for bad in (True, 3.5, '3', None, float('nan')):
            with self.subTest(bad=bad):
                self.assertTrue(schema.errors(integer, bad))
        self.assertTrue(schema.errors({'type': 'number'}, False))
        self.assertTrue(schema.errors({'const': 1}, True))
        self.assertEqual(schema.errors({'const': 1}, 1.0), [])
        self.assertTrue(schema.errors({'enum': [0, 1]}, False))

    def test_objects_references_and_alternatives(self):
        doc = {'$defs': {'pos': {'type': 'integer', 'minimum': 1}},
               'type': 'object', 'additionalProperties': False, 'required': ['a'],
               'properties': {'a': {'$ref': '#/$defs/pos'},
                              'b': {'oneOf': [{'type': 'string', 'pattern': '^x'}, {'const': 2}]}}}
        self.assertEqual(schema.errors(doc, {'a': 1, 'b': 'xy'}), [])
        self.assertEqual(schema.errors(doc, {'a': 1, 'b': 2}), [])
        for bad in ({}, {'a': 0}, {'a': 1, 'c': 1}, {'a': 1, 'b': 'y'}, {'a': 1, 'b': 3}, []):
            with self.subTest(bad=bad), self.assertRaises(schema.SchemaError):
                schema.validate(doc, bad)

    def test_unknown_keywords_are_refused_not_ignored(self):
        for unknown in ({'type': 'object', 'patternProperties': {}}, {'format': 'email'},
                        {'$ref': 'https://example.com/schema.json'}, {'type': 'decimal'}):
            with self.subTest(schema=unknown), self.assertRaises(schema.SchemaError):
                schema.validate(unknown, {})

    def test_reads_the_limits_schema(self):
        document = profile()
        self.assertIs(schema.validate(PROFILE_SCHEMA, document), document)


class ProfileTests(unittest.TestCase):
    def test_valid_profiles(self):
        for p in (profile(), profile(live_unknown_usage=reserve(0.5, 'codex', 'claude')),
                  {k: v for k, v in profile().items() if k != 'live_unknown_usage'},
                  profile(unknown_usage_reserve_model='exclusive_cost_kind_v1')):
            with self.subTest(profile=p):
                self.assertIs(budget.validate_profile(p, PROFILE_SCHEMA), p)

    def test_invalid_profiles(self):
        missing = profile()
        del missing['wall_seconds']
        for p in (missing, profile(metered_usd=float('inf')), profile(cli_estimate_usd=0),
                  profile(live_unknown_usage=reserve(float('inf'), 'codex')),
                  profile(live_unknown_usage=reserve(0, 'codex')),
                  profile(live_unknown_usage={'codex': {'model': 'unmetered_quota', 'reason': '  '}}),
                  profile(live_unknown_usage={'codex': {'model': 'implicit_zero', 'reason': REASON}}),
                  profile(live_unknown_usage={'other': {'model': 'unmetered_quota', 'reason': REASON}}),
                  profile(live_unknown_usage={}), profile(invocations=True), profile(extra=1)):
            with self.subTest(profile=p), self.assertRaises(ValueError):
                budget.validate_profile(p, PROFILE_SCHEMA)

    def test_malformed_rule_outside_the_schema_is_a_value_error(self):
        for rule in ({'model': 'unmetered_quota'}, {'reason': REASON}, 'unmetered_quota',
                     {'model': 'reserve_per_invocation', 'reason': REASON, 'metered_usd': True, 'cli_estimate_usd': 1}):
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                budget.validate_live_usage({'live_unknown_usage': {'codex': rule}})


class UnknownUsageTests(unittest.TestCase):
    def test_agents_without_cost_need_a_declared_rule(self):
        undeclared = {k: v for k, v in profile().items() if k != 'live_unknown_usage'}
        for agent in ('codex', 'agy', 'unknown-agent'):
            with self.subTest(agent=agent), self.assertRaises(budget.Refused) as caught:
                budget.check_capacity(envelope(undeclared), state(), '03-dev', '03-dev', agent, now=1)
            self.assertEqual(caught.exception.category, 'LIMIT_REACHED')
            self.assertIn('refused before launch', str(caught.exception))
        # An agent that reports its own cost needs no rule.
        self.assertNotIn('usage_model', budget.check_capacity(envelope(undeclared), state(), '03-dev', '03-dev',
                                                              'claude', now=1))

    def test_unmetered_quota_never_derives_a_dollar_reserve(self):
        for agent in ('codex', 'agy'):
            with self.subTest(agent=agent):
                booked = budget.check_capacity(envelope(), state(), '03-dev', '03-dev', agent, now=1)
                self.assertEqual(booked['usage_model'], 'unmetered_quota')
                self.assertNotIn('conservative_usage', booked)
                self.assertEqual(booked['usage'], {'metered_usd': None, 'cli_estimate_usd': None})
        # Tokens are never priced: a returned unmetered call with many tokens charges no dollars.
        done = row('codex', usage_model='unmetered_quota', reported_tokens={'input_tokens': 10 ** 9,
                                                                          'output_tokens': 10 ** 9})
        self.assertEqual(budget.accounted_total(state(done), profile()), 0)
        self.assertEqual([budget.kind_charge(done, kind) for kind in budget.COST_KINDS], [0, 0])

    def test_a_returned_call_without_cost_or_rule_blocks_the_next(self):
        silent = row('codex')
        self.assertTrue(budget.uncovered(silent))
        with self.assertRaisesRegex(budget.Refused, 'unknown is not counted as zero'):
            budget.check(state(silent), profile(), 1)
        budget.check(state(silent, legacy_uncovered_invocations=['i0']), profile(), 1)
        budget.check(state(silent), profile(), 1, for_dispatch=False)

    def test_reserve_stays_charged_and_an_observation_only_raises_it(self):
        reserved = {'usage_model': 'reserve_per_invocation', 'conservative_usage': {'metered_usd': 2, 'cli_estimate_usd': 2}}
        self.assertEqual(budget.kind_charge(row('codex', **reserved), 'metered_usd'), 2)
        self.assertEqual(budget.kind_charge(row('codex', usage={'metered_usd': 1, 'cli_estimate_usd': None}, **reserved),
                                            'metered_usd'), 2)
        self.assertEqual(budget.kind_charge(row('codex', usage={'metered_usd': 3, 'cli_estimate_usd': None}, **reserved),
                                            'metered_usd'), 3)

    def test_a_reserve_may_fill_a_cap_but_not_exceed_it(self):
        policy = profile(metered_usd=4, cli_estimate_usd=4, live_unknown_usage=reserve(2, 'codex'))
        reserved = row('codex', usage_model='reserve_per_invocation',
                       conservative_usage={'metered_usd': 2, 'cli_estimate_usd': 2})
        self.assertEqual(budget.check_capacity(envelope(policy), state(reserved), '03-dev', '03-dev', 'codex',
                                               now=1)['conservative_usage'], {'metered_usd': 2, 'cli_estimate_usd': 2})
        wider = dict(policy, metered_usd=5, cli_estimate_usd=5)
        with self.assertRaisesRegex(budget.Refused, 'reserve would exceed'):
            budget.check_capacity(envelope(wider), state(reserved, dict(reserved)), '03-dev', '03-dev', 'codex', now=1)


class CapacityTests(unittest.TestCase):
    def test_unconfirmed_outcomes_block_any_new_call(self):
        for status in ('reserved', 'uncertain'):
            with self.subTest(status=status), self.assertRaises(budget.Refused) as caught:
                budget.check_capacity(envelope(), state(row('codex', status, usage_model='unmetered_quota')),
                                      '03-dev', '03-dev', 'codex', now=1)
            self.assertEqual(caught.exception.category, 'OUTCOME_UNCERTAIN')

    def test_time_is_an_argument(self):
        with self.assertRaisesRegex(budget.Refused, 'wall-time exhausted'):
            budget.check_capacity(envelope(), state(), '03-dev', '03-dev', 'codex', now=1000)
        with self.assertRaisesRegex(budget.Refused, 'Clock moved behind'):
            budget.check_capacity(envelope(), state(started_at=5), '03-dev', '03-dev', 'codex', now=4)

    def test_invocation_caps_and_scope(self):
        done = row('codex', usage_model='unmetered_quota')
        with self.assertRaisesRegex(budget.Refused, 'allowance exhausted'):
            budget.check_capacity(envelope(), state(done, done, done), '03-dev', '03-dev', 'codex', now=1)
        self.assertIsNone(budget.check_capacity(envelope(), state(done, done), '03-dev', '03-dev', now=1))
        with self.assertRaisesRegex(budget.Refused, 'Missing explicit role/phase'):
            budget.check_capacity(envelope(), state(), '05-qa', '03-dev', 'codex', now=1)
        with self.assertRaises(budget.Refused) as caught:
            budget.check_capacity(envelope(allowed_phases=['04-review']), state(), '03-dev', '03-dev', 'codex', now=1)
        self.assertEqual(caught.exception.category, 'SCOPE_REQUIRED')

    def test_functions_do_not_change_their_inputs(self):
        env = envelope(profile(live_unknown_usage=reserve(1, 'codex')))
        s = state(row('claude', usage={'metered_usd': None, 'cli_estimate_usd': 0.5}))
        before = copy.deepcopy((env, s))
        budget.check_capacity(env, s, '03-dev', '03-dev', 'codex', now=1)
        budget.accounted_total(s, env['policy'])
        budget.agent_tokens(s)
        self.assertEqual((env, s), before)

    def test_tokens_stay_per_agent_and_unknown_is_not_zero(self):
        s = state(row('codex', reported_tokens={'input_tokens': 10, 'output_tokens': None}),
                  row('codex', reported_tokens={'input_tokens': 5, 'output_tokens': None}),
                  row('agy', reported_tokens={'input_tokens': 7, 'output_tokens': 3}),
                  row('claude'))
        self.assertEqual(budget.agent_tokens(s), {
            'agy': {'input_tokens': 7, 'output_tokens': 3},
            'claude': {'input_tokens': None, 'output_tokens': None},
            'codex': {'input_tokens': 15, 'output_tokens': None}})


class TelemetryTests(unittest.TestCase):
    def record(self, **changes):
        t = {'schema': 'ssdlc.cost-telemetry', 'schema_version': 1, 'invocation_id': 'abc', 'provider': 'claude',
             'adapter': 'claude_cli', 'outcome': 'completed', 'recorded_at': 'now',
             'route': {'value': None, 'provenance': 'unknown'}, 'billing': {'value': None, 'provenance': 'unknown'},
             'spend': {'availability': 'unknown', 'amount': None, 'unit': None, 'source': None, 'observed_at': 'now'},
             'estimate': {'availability': 'estimated', 'amount': 0.4, 'unit': 'USD', 'basis': 'api_equivalent',
                          'source': 'claude_cli.total_cost_usd', 'observed_at': 'now'},
             'tokens': {'availability': 'reported', 'unit': 'tokens', 'source': 'claude_cli.usage', 'observed_at': 'now',
                        'scope': 'cumulative', 'turns': 1,
                        'counts': {'input': 10, 'cached_input': 5, 'cache_creation': 0, 'output': 7, 'reasoning': None},
                        'contract': ['input', 'cached_input', 'cache_creation', 'output'],
                        'overlaps': [['reasoning', 'output']]},
             'quota_snapshots': telemetry.empty_quota()}
        for key, value in changes.items():
            t[key] = dict(t[key], **value) if isinstance(value, dict) else value
        return t

    def test_measures_stay_apart(self):
        self.assertEqual(telemetry.measures(self.record()),
                         {'spend': None, 'estimate_usd': 0.4, 'tokens': 22})
        charged = self.record(provider='openrouter', adapter='openrouter_http',
                              spend={'availability': 'reported', 'amount': 0.1, 'unit': 'USD', 'source': 'x'})
        self.assertEqual(telemetry.measures(charged)['spend'], {'amount': 0.1, 'unit': 'USD'})

    def test_unknown_is_never_zero(self):
        unknown = self.record(estimate={'availability': 'unknown', 'amount': None, 'source': None})
        self.assertIsNone(telemetry.measures(unknown)['estimate_usd'])
        self.assertEqual(telemetry.measures(self.record(estimate={'amount': 0}))['estimate_usd'], 0)
        for bad in (self.record(estimate={'availability': 'unknown', 'amount': 0}),
                    self.record(tokens={'availability': 'unknown'}),
                    self.record(tokens={'counts': dict.fromkeys(telemetry.TOKEN_FIELDS)}),
                    self.record(outcome='incomplete'), self.record(estimate={'amount': -1}),
                    self.record(estimate={'amount': float('nan')})):
            with self.subTest(record=bad), self.assertRaises(ValueError):
                telemetry.validate(bad)
        missing = self.record(tokens={'counts': dict(self.record()['tokens']['counts'], output=None)})
        self.assertIsNone(telemetry.token_total(missing['tokens']))

    def test_classify_and_ledger(self):
        ledger = [{'telemetry_schema_version': 1, 'telemetry': self.record(), 'cost_usd': 0.4},
                  {'telemetry_schema_version': 1, 'telemetry': self.record(), 'cost_usd': 0.4},
                  {'telemetry_schema_version': 1, 'telemetry': None, 'cost_usd': 0.2},
                  {'engine': 'codex', 'cost_usd': 1.5}, 'not-a-row']
        self.assertEqual(telemetry.classify(ledger[0])[0], 'invocation')
        self.assertEqual(telemetry.classify(ledger[2]), ('no_invocation', None))
        kind, note = telemetry.classify(ledger[3])
        self.assertEqual((kind, note['legacy_cost_usd']), ('legacy', {'amount': 1.5, 'classification': 'unclassified'}))
        self.assertEqual(telemetry.ledger_measures(ledger), {
            'spend_usd': 0, 'estimate_usd': 0.4, 'unclassified_usd': 1.7, 'unmeasured_spend': [],
            'unmeasured_estimate': []})

    def test_route_admission(self):
        declared = {'codex': {'model': 'unmetered_quota', 'reason': REASON}}
        self.assertEqual(telemetry.admit('codex', 'codex_cli', spend_cap=10, estimate_cap_active=True,
                                         declarations=declared), {'route': 'codex_cli', 'measure': 'unmetered_quota'})
        for provider, route in (('agy', 'agy_cli'), ('claude', 'openrouter_api'), ('claude', None), ('x', 'other')):
            with self.subTest(provider=provider, route=route), self.assertRaises(telemetry.AdmissionRefused):
                telemetry.admit(provider, route, spend_cap=10, estimate_cap_active=True, declarations=declared)


if __name__ == '__main__':
    unittest.main()
