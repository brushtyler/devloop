"""`core` gives the engine's results on the same cases (ADR 0004, P2-1).

`core` is written next to the engine, not extracted from it: the engine does not use it until
step 6 of ADR 0001. These tests run the same profiles, states, telemetry records, ledgers and
routes through `core` and through `ssdlc_recovery.py`, `ssdlc_telemetry.py` and the schema
validation of the engine (`ssdlc_tasks.validate_document`), and require the same answers: the
same acceptance or refusal, the same refusal category and message, the same rows and figures.
"""
import copy
import itertools
import json
import tempfile
import unittest
from pathlib import Path

import ssdlc_telemetry
import ssdlc_recovery
from ssdlc_recovery import LimitsStore
from ssdlc_tasks import DevelopmentBlocked, TaskError, validate_document

from core import budget, schema, telemetry
from test_core import PROFILE_SCHEMA, REASON, profile, reserve

DELETE = object()
REPLACEMENTS = (DELETE, None, 0, -1, 1, 1.0, 2.5, True, False, '', ' ', 'x', float('nan'), float('inf'), [], {})


def paths(node, prefix=()):
    if isinstance(node, dict):
        for key in node:
            yield prefix + (key,)
            yield from paths(node[key], prefix + (key,))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield prefix + (index,)
            yield from paths(item, prefix + (index,))


def mutations(document):
    """Every variant of ``document`` with one nested value deleted or replaced."""
    for path in paths(document):
        for value in REPLACEMENTS:
            variant = copy.deepcopy(document)
            parent = variant
            for key in path[:-1]:
                parent = parent[key]
            if value is DELETE:
                del parent[path[-1]]
            else:
                parent[path[-1]] = copy.deepcopy(value)
            yield path, value, variant
    for path in [()] + [p for p in paths(document) if isinstance(_at(document, p), dict)]:
        variant = copy.deepcopy(document)
        _at(variant, path)['unexpected_field'] = 1
        yield path + ('unexpected_field',), 'added', variant


def _at(document, path):
    for key in path:
        document = document[key]
    return document


def full_profile():
    """A profile that uses every budget section of the schema."""
    return profile(live_unknown_usage=dict(reserve(0.5, 'codex'), agy={'model': 'unmetered_quota', 'reason': REASON}),
                   unknown_usage_reserve_model='exclusive_cost_kind_v1',
                   historical_unknown_usage={'invocations': 2, 'metered_usd': 1, 'cli_estimate_usd': 1,
                                             'combined_usd': 2})


def feedback_profile():
    role = {'engine': 'claude', 'model': 'claude-opus', 'effort': 'high'}
    return profile(
        feedback_workflow={'schema_version': 1, 'enabled': True, 'roles': {'planner': role, 'reviewer': dict(role)},
                           'github': {'repo': 'owner/repo', 'issue': 1}},
        feedback_delegation={'schema_version': 1, 'capability': 'feedback-scope-delegation-v1',
                             'allowed_files': ['a.py', 'b/c.py'], 'max_amendments': 1, 'max_amendments_per_task': 1,
                             'max_added_files': 1, 'require_strong_independence': True, 'owner_revalidation': True})


def engine_schema_accepts(document):
    try:
        validate_document('recovery-limits', document)
        return True
    except TaskError:
        return False


class ProfileEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cases = 0

    def engine_authorizes(self, policy):
        """The validation of ``LimitsStore.authorize``, on a fresh ticket directory."""
        self.cases += 1
        store = LimitsStore(Path(self.tmp.name) / str(self.cases), lambda: 100)
        try:
            store.authorize(policy, {'ticket': 'T1', 'controller': 'offline'})
            return True
        except (TaskError, ValueError):
            return False

    def test_schema_validation_matches_the_engine(self):
        checked = 0
        for base in (full_profile(), feedback_profile()):
            self.assertTrue(engine_schema_accepts(base))
            for path, value, variant in mutations(base):
                with self.subTest(path=path, value=value):
                    self.assertEqual(schema.errors(PROFILE_SCHEMA, variant) == [], engine_schema_accepts(variant))
                    checked += 1
        self.assertGreater(checked, 1000)

    def test_profile_validation_matches_the_engine_authorization(self):
        cases = [profile(), full_profile(), {k: v for k, v in profile().items() if k != 'live_unknown_usage'},
                 profile(metered_usd=float('inf')), profile(cli_estimate_usd=float('nan')),
                 profile(live_unknown_usage=reserve(float('inf'), 'codex')),
                 profile(live_unknown_usage=reserve(float('nan'), 'agy')),
                 profile(live_unknown_usage={'codex': {'model': 'unmetered_quota', 'reason': '\t'}})]
        cases += [variant for _, _, variant in mutations(full_profile())]
        accepted = 0
        for policy in cases:
            with self.subTest(policy=policy):
                try:
                    budget.validate_profile(policy, PROFILE_SCHEMA)
                    ours = True
                except ValueError:
                    ours = False
                self.assertEqual(ours, self.engine_authorizes(copy.deepcopy(policy)))
                accepted += ours
        self.assertTrue(0 < accepted < len(cases))


def returned(engine, **extra):
    return dict({'role': '03-dev', 'phase': '03-dev', 'engine': engine, 'status': 'returned',
                 'usage': {'metered_usd': None, 'cli_estimate_usd': None}}, **extra)


ROWS = {
    'claude-estimate': returned('claude', usage={'metered_usd': None, 'cli_estimate_usd': 1.5},
                                reported_tokens={'input_tokens': 100, 'output_tokens': 20}),
    'openrouter-charge': returned('openrouter', role='04-review', phase='04-review',
                                  usage={'metered_usd': 2.0, 'cli_estimate_usd': None}),
    'codex-unmetered': returned('codex', usage_model='unmetered_quota',
                                reported_tokens={'input_tokens': 5000, 'output_tokens': None}),
    'agy-unmetered': returned('agy', usage_model='unmetered_quota',
                              reported_tokens={'input_tokens': 70, 'output_tokens': 11}),
    'codex-reserve': returned('codex', usage_model='reserve_per_invocation',
                              conservative_usage={'metered_usd': 2, 'cli_estimate_usd': 2}),
    'codex-reserve-raised': returned('codex', usage_model='reserve_per_invocation',
                                     usage={'metered_usd': 3.5, 'cli_estimate_usd': None},
                                     conservative_usage={'metered_usd': 2, 'cli_estimate_usd': 2}),
    'codex-uncovered': returned('codex'),
    'agy-terminated-uncovered': returned('agy', status='terminated'),
    'codex-reserved': returned('codex', status='reserved', usage_model='unmetered_quota'),
    'claude-uncertain': returned('claude', status='uncertain'),
    'codex-not-dispatched': returned('codex', status='not_dispatched', usage_model='unmetered_quota'),
    'historical-reserved': returned('claude', status='historical',
                                    conservative_usage={'metered_usd': 1, 'cli_estimate_usd': 1}),
    'historical-unreserved': returned('claude', status='historical'),
    'historical-known': returned('openrouter', status='historical', usage={'metered_usd': 1, 'cli_estimate_usd': None}),
}
STATES = [(), ('claude-estimate',), ('openrouter-charge', 'codex-unmetered', 'agy-unmetered'),
          ('codex-reserve',), ('codex-reserve', 'codex-reserve-raised'), ('codex-reserve',) * 3,
          ('codex-uncovered',), ('agy-terminated-uncovered',), ('codex-reserved',), ('claude-uncertain',),
          ('codex-not-dispatched',), ('historical-reserved',), ('historical-unreserved',), ('historical-known',),
          ('claude-estimate', 'claude-estimate', 'claude-estimate'),
          ('claude-estimate',) * 6 + ('openrouter-charge',) * 4]


def state(names, **extra):
    s = {'started_at': 50, 'deadline_at': 1050,
         'invocations': {f'id{n}': copy.deepcopy(ROWS[name]) for n, name in enumerate(names)},
         'transports': [], 'diagnoses': []}
    s.update(extra)
    return s


def all_states():
    for names in STATES:
        yield names, state(names)
        if names:
            yield names + ('exempt',), state(names, legacy_uncovered_invocations=['id0'])
            yield names + ('combined',), state(names, combined_reported_usd_cap=4)


POLICIES = {
    'base': profile(),
    'undeclared': {k: v for k, v in profile().items() if k != 'live_unknown_usage'},
    'reserve': profile(live_unknown_usage=reserve(2, 'codex', 'agy')),
    'reserve-tight': profile(metered_usd=5, cli_estimate_usd=5, live_unknown_usage=reserve(2, 'codex', 'claude')),
    'exclusive': profile(unknown_usage_reserve_model='exclusive_cost_kind_v1',
                         live_unknown_usage=dict(reserve(1, 'codex'), agy={'model': 'unmetered_quota',
                                                                           'reason': REASON})),
    'few-invocations': profile(invocations=2, role_invocations={'03-dev': 1, '04-review': 1}),
}
BINDINGS = ({'ticket': 'T1'}, {'ticket': 'T1', 'allowed_phases': ['04-review']})
CALLS = [('03-dev', '03-dev'), ('04-review', '04-review'), ('05-qa', '05-qa'), ('03-dev', '04-review')]
ENGINES = (None, 'claude', 'openrouter', 'codex', 'agy', 'other')
NOWS = (49, 50, 500, 1049, 1050)


def outcome(call):
    try:
        return 'ok', call()
    except (DevelopmentBlocked, budget.Refused) as exc:
        return 'refused', exc.category, str(exc)


class BudgetEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def store(self, now):
        return LimitsStore(Path(self.tmp.name), lambda: now)

    def test_capacity_matches_the_engine(self):
        results = set()
        for (pname, policy), binding, (sname, s) in itertools.product(POLICIES.items(), BINDINGS, all_states()):
            env = {'policy': policy, 'binding': binding}
            for (role, phase), engine, now in itertools.product(CALLS, ENGINES, NOWS):
                with self.subTest(policy=pname, binding=binding, state=sname, role=role, phase=phase,
                                  engine=engine, now=now):
                    ours = outcome(lambda: budget.check_capacity(copy.deepcopy(env), copy.deepcopy(s), role, phase,
                                                                 engine, now=now))
                    theirs = outcome(lambda: self.store(now).check_capacity(copy.deepcopy(env), copy.deepcopy(s),
                                                                            role, phase, engine))
                    self.assertEqual(ours, theirs)
                    results.add(ours[0] if ours[0] == 'ok' else ours[2])
        # The grid reaches every outcome: acceptance and each refusal of the engine.
        self.assertGreaterEqual(len(results), 10, results)

    def test_check_matches_the_engine(self):
        for (pname, policy), (sname, s), now, for_dispatch in itertools.product(
                POLICIES.items(), all_states(), NOWS, (True, False)):
            with self.subTest(policy=pname, state=sname, now=now, for_dispatch=for_dispatch):
                self.assertEqual(outcome(lambda: budget.check(s, policy, now, for_dispatch=for_dispatch)),
                                 outcome(lambda: self.store(now).check(s, policy, for_dispatch=for_dispatch)))

    def test_charges_and_tokens_match_the_engine(self):
        for (pname, policy), (sname, s) in itertools.product(POLICIES.items(), all_states()):
            with self.subTest(policy=pname, state=sname):
                self.assertEqual(budget.accounted_total(s, policy), LimitsStore.accounted_total(s, policy))
                for row in s['invocations'].values():
                    self.assertEqual(budget.combined_charge(row, policy), LimitsStore._combined_charge(row, policy))
                    for kind in budget.COST_KINDS:
                        self.assertEqual(budget.kind_charge(row, kind), LimitsStore._kind_charge(row, kind))
                    self.assertEqual(budget.uncovered(row), ssdlc_recovery.uncovered(row))
                coverage = LimitsStore.coverage({'policy': policy, 'binding': {}}, s)
                self.assertEqual(budget.agent_tokens(s),
                                 {agent: entry['reported_tokens'] for agent, entry in coverage['engines'].items()})

    def test_constants_match_the_engine(self):
        import ssdlc_recovery
        self.assertEqual(budget.COST_KINDS, ssdlc_recovery.COST_KINDS)
        self.assertEqual(budget.REPORTED_COST, ssdlc_recovery.REPORTED_COST)
        self.assertEqual((budget.RESERVE, budget.UNMETERED), (ssdlc_recovery.RESERVE, ssdlc_recovery.UNMETERED))
        self.assertLessEqual(set(budget.CATEGORIES) - {'SCOPE_REQUIRED'}, set(ssdlc_recovery.STOPS))


CLAUDE = {'num_turns': 3, 'total_cost_usd': 0.42, 'usage': {'input_tokens': 12, 'cache_creation_input_tokens': 3000,
                                                            'cache_read_input_tokens': 45000, 'output_tokens': 800}}
CODEX = [{'input_tokens': 1000, 'cached_input_tokens': 600, 'output_tokens': 300, 'reasoning_output_tokens': 120},
         {'input_tokens': 500, 'cached_input_tokens': 400, 'output_tokens': 100}]
AGY = {'num_turns': 1, 'usage': {'input_tokens': 68991, 'output_tokens': 1147, 'thinking_tokens': 536,
                                 'cache_read_tokens': 88505, 'total_tokens': 70138}}
OPENROUTER = {'prompt_tokens': 100, 'completion_tokens': 50, 'cost': 0.01,
              'prompt_tokens_details': {'cached_tokens': 10}, 'completion_tokens_details': {'reasoning_tokens': 5}}
QUOTA = {'availability': 'reported', 'schema_version': 1, 'status': 'collected', 'route': 'codex_cli',
         'snapshots': {'before': None, 'after': None},
         'deltas': [{'status': 'unknown', 'delta_pp': None, 'call_consumption': None},
                    {'status': 'observed_account_delta', 'delta_pp': 2.5, 'call_consumption': None}],
         'controls': []}


def records():
    t = ssdlc_telemetry
    collected = t.from_codex(CODEX)
    collected['quota_snapshots'] = copy.deepcopy(QUOTA)
    return [t.from_claude(CLAUDE), t.from_claude(dict(CLAUDE, total_cost_usd=None)), t.from_claude(None),
            t.from_claude(dict(CLAUDE, total_cost_usd=0)),
            t.from_codex(CODEX), t.from_codex(CODEX[1:]), t.from_codex([]), t.from_agy(AGY), t.from_agy({}),
            t.from_openrouter(OPENROUTER), t.from_openrouter({'prompt_tokens': 1}), t.incomplete('claude'),
            t.incomplete('codex', t.provenance('codex_cli', 'explicit_config')),
            t.record('claude', route=t.provenance('openrouter_api', 'explicit_config')),
            t.record('openrouter', route=t.provenance('openrouter_api', 'verified_signal'),
                     spend_measure=t.spend(1.5, 'EUR', 'x')),
            collected]


def engine_validates(record):
    try:
        ssdlc_telemetry.validate(record)
        return True
    except ValueError:
        return False


def core_validates(record):
    try:
        telemetry.validate(record)
        return True
    except ValueError:
        return False


class TelemetryEquivalenceTests(unittest.TestCase):
    def test_validation_matches_the_engine(self):
        checked = 0
        for record in records():
            self.assertTrue(engine_validates(record))
            for path, value, variant in mutations(record):
                with self.subTest(provider=record['provider'], path=path, value=value):
                    self.assertEqual(core_validates(variant), engine_validates(variant))
                    checked += 1
        self.assertGreater(checked, 5000)

    def test_token_totals_match_the_engine(self):
        for record in records():
            measures = [record['tokens']] + [variant['tokens'] for path, _, variant in mutations(record)
                                             if path[:2] == ('tokens', 'counts') and engine_validates(variant)]
            for measure in measures:
                with self.subTest(measure=measure):
                    self.assertEqual(telemetry.token_total(measure), ssdlc_telemetry.token_total(measure))

    def test_classification_and_ledger_measures_match_the_engine(self):
        valid = records()
        broken = copy.deepcopy(valid[0])
        broken['estimate']['amount'] = None
        rows = [{'telemetry_schema_version': 1, 'telemetry': r, 'cost_usd': cost, 'engine': r['provider']}
                for r, cost in zip(valid, itertools.cycle((0.5, None, 0, 2)))]
        rows += [rows[0], rows[4],
                 {'telemetry_schema_version': 1, 'telemetry': broken, 'cost_usd': 0.3, 'engine': 'claude'},
                 {'telemetry_schema_version': 1, 'telemetry': None, 'cost_usd': 0.2, 'engine': 'codex'},
                 {'telemetry_schema_version': 2, 'telemetry': valid[0], 'cost_usd': 0.1},
                 {'engine': 'claude', 'cost_usd': 1.25}, {'engine': 'codex', 'cost_usd': -1}, {'cost_usd': float('nan')},
                 {}, 'not-a-row', None]
        for row in rows:
            if isinstance(row, dict):
                with self.subTest(row=row):
                    self.assertEqual(telemetry.classify(row), ssdlc_telemetry.classify(row))
            with self.subTest(ledger=[row]):
                self.assertEqual(telemetry.ledger_measures([row]), ssdlc_telemetry.ledger_measures([row]))
        self.assertEqual(telemetry.ledger_measures(rows), ssdlc_telemetry.ledger_measures(rows))
        self.assertEqual(telemetry.ledger_measures(None), ssdlc_telemetry.ledger_measures(None))

    def test_measures_stay_apart_as_in_the_engine_breakdown(self):
        for record in records():
            with self.subTest(provider=record['provider'], record=record['invocation_id']):
                ours = telemetry.measures(record)
                entry = ssdlc_telemetry.breakdown([{'telemetry_schema_version': 1, 'telemetry': record}])
                entry = entry['providers'][record['provider']]
                self.assertEqual(ours['estimate_usd'], entry['estimate']['usd'])
                self.assertEqual(ours['tokens'], entry['tokens']['total'])
                self.assertEqual({ours['spend']['unit']: ours['spend']['amount']} if ours['spend'] else {},
                                 entry['spend']['by_unit'])

    def test_route_admission_matches_the_engine(self):
        declarations = (None, {}, {'codex': {'model': 'unmetered_quota', 'reason': REASON}},
                        ssdlc_telemetry.unmetered_declarations({'SSDLC_UNMETERED_QUOTA': 'agy,codex'}),
                        reserve(1, 'codex', 'agy'))
        routes = (None, 'openrouter_api', 'claude_cli', 'codex_cli', 'agy_cli', 'bogus')
        for provider, route, spend_cap, estimate_cap, declared in itertools.product(
                ('claude', 'codex', 'agy', 'openrouter', 'other'), routes, (None, 0, 5.0), (True, False), declarations):
            with self.subTest(provider=provider, route=route, spend_cap=spend_cap, estimate_cap=estimate_cap,
                              declared=declared):
                def run(module):
                    try:
                        return 'ok', module.admit(provider, route, spend_cap=spend_cap,
                                                  estimate_cap_active=estimate_cap, declarations=declared)
                    except ValueError as exc:
                        return type(exc).__name__, str(exc)
                self.assertEqual(run(telemetry), run(ssdlc_telemetry))

    def test_constants_match_the_engine(self):
        import ssdlc_quota
        for name in ('SCHEMA', 'SCHEMA_VERSION', 'AVAILABILITY', 'OUTCOMES', 'PROVENANCE', 'TOKEN_FIELDS',
                     'TOKEN_SCOPES', 'ADAPTERS', 'QUOTA_SNAPSHOTS_VERSION', 'ROUTE_MEASURES', 'ADAPTER_MEASURES'):
            with self.subTest(name=name):
                self.assertEqual(getattr(telemetry, name), getattr(ssdlc_telemetry, name))
        self.assertEqual(telemetry.empty_quota(), ssdlc_telemetry.quota_snapshots())
        self.assertEqual(set(telemetry.QUOTA_ROUTES), set(ssdlc_quota.ROUTES))
        self.assertEqual(telemetry.PUBLIC_SNAPSHOT_FIELDS, ssdlc_quota.PUBLIC_SNAPSHOT_FIELDS)
        self.assertEqual(telemetry.PUBLIC_BUCKET_FIELDS, ssdlc_quota.PUBLIC_BUCKET_FIELDS)
        self.assertEqual(telemetry.QUOTA_SNAPSHOTS_VERSION, ssdlc_quota.SCHEMA_VERSION)


if __name__ == '__main__':
    unittest.main()
