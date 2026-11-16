"""Issue #43: a live invocation without a reported dollar cost is never counted as zero."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_recovery_limits import policy
from ssdlc_recovery import LimitsStore, RecoveryStop, observed_tokens, observed_usage
from ssdlc_tasks import TaskError, digest, validate_document

REASON = 'Offline proxy from the mean historical cost per invocation, not observed billing'


def undeclared(**overrides):
    """A profile written before issue #43: no live unknown-usage model at all."""
    p = policy(**overrides)
    p.pop('live_unknown_usage')
    return p


def reserve_model(amount, *engines):
    return {engine: {'model': 'reserve_per_invocation', 'metered_usd': amount, 'cli_estimate_usd': amount,
                     'reason': REASON} for engine in engines}


class LiveUnknownUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 100

    def store(self, profile):
        store = LimitsStore(self.root, lambda: self.now)
        store.authorize(profile, {'ticket': 'T1', 'controller': 'offline'})
        return store

    def rows(self, store):
        return store._load(repair=False)[1]['invocations']

    def dispatch(self, store, fn, *args):
        from orchestrator import _dispatch_context, _dispatch_local
        previous = getattr(_dispatch_local, 'limits', None)
        _dispatch_local.limits = store
        try:
            with _dispatch_context(phase='03-dev'):
                return fn(*args)
        finally:
            _dispatch_local.limits = previous

    def test_schema_requires_explicit_positive_reserve_and_reason_and_defaults_to_absent(self):
        validate_document('recovery-limits', undeclared())
        validate_document('recovery-limits', policy(live_unknown_usage=reserve_model(0.5, 'codex', 'claude')))
        invalid = [
            {'codex': {'model': 'reserve_per_invocation', 'metered_usd': 0, 'cli_estimate_usd': 1, 'reason': REASON}},
            {'codex': {'model': 'reserve_per_invocation', 'metered_usd': -1, 'cli_estimate_usd': 1, 'reason': REASON}},
            {'codex': {'model': 'reserve_per_invocation', 'metered_usd': 1, 'cli_estimate_usd': 1}},
            {'codex': {'model': 'reserve_per_invocation', 'metered_usd': 1, 'reason': REASON}},
            {'codex': {'model': 'unmetered_quota'}},
            {'codex': {'model': 'unmetered_quota', 'reason': '   '}},
            {'codex': {'model': 'unmetered_quota', 'reason': REASON, 'metered_usd': 1}},
            {'codex': {'model': 'implicit_zero', 'reason': REASON}},
            {'other-engine': {'model': 'unmetered_quota', 'reason': REASON}},
            {},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(TaskError):
                validate_document('recovery-limits', policy(live_unknown_usage=value))
        with self.assertRaises(ValueError):
            self.store(policy(live_unknown_usage=reserve_model(float('inf'), 'codex')))

    def test_codex_without_declared_model_is_refused_before_launch(self):
        from orchestrator import _supervised_dispatch
        store = self.store(undeclared())
        for engine in ('codex', 'agy'):
            with self.assertRaisesRegex(RecoveryStop, 'refused before launch') as caught:
                store.reserve('03-dev', '03-dev', engine)
            self.assertEqual(caught.exception.category, 'LIMIT_REACHED')
        launched = []
        @_supervised_dispatch
        def run_codex(prompt, cwd): launched.append(prompt); return 'text', 0
        with self.assertRaisesRegex(RecoveryStop, 'refused before launch'):
            self.dispatch(store, run_codex, 'prompt', self.root)
        self.assertEqual(launched, [])
        self.assertEqual(self.rows(store), {})

    def test_reserve_per_invocation_reaches_the_cap_at_reservation_not_after(self):
        store = self.store(policy(live_unknown_usage=reserve_model(4, 'codex')))
        for _ in range(2):
            identity = store.reserve('03-dev', '03-dev', 'codex')
            row = self.rows(store)[identity]
            self.assertEqual(row['usage_model'], 'reserve_per_invocation')
            self.assertEqual(row['conservative_usage'], {'metered_usd': 4, 'cli_estimate_usd': 4})
            store.finish(identity, 'returned')
        with self.assertRaisesRegex(RecoveryStop, 'reserve would exceed the frozen metered_usd cap'):
            store.reserve('03-dev', '03-dev', 'codex')
        self.assertEqual(len(self.rows(store)), 2)
        self.assertEqual(store.accounted_total(store._load()[1], store._load()[0]['policy']), 8)

    def test_unmetered_quota_dispatch_proceeds_and_the_report_declares_the_exclusion(self):
        store = self.store(policy())
        for _ in range(2):
            identity = store.reserve('03-dev', '03-dev', 'codex')
            store.finish(identity, 'returned')
        report = store.coverage_report()
        codex = report['engines']['codex']
        self.assertEqual((codex['invocations'], codex['unmetered_quota'], codex['uncovered']), (2, 2, 0))
        self.assertEqual(codex['dollar_caps_cover'], 'none')
        self.assertFalse(report['dollar_caps_cover_all_invocations'])
        self.assertEqual(report['declared_models'], {'agy': 'unmetered_quota', 'codex': 'unmetered_quota'})
        self.assertEqual(report['uncovered_invocations'], [])

    def test_claude_with_zero_or_absent_cost_follows_the_same_path(self):
        self.assertIsNone(observed_usage('claude', 'run_claude', ('text', 0, 0.0, 10, 5))['cli_estimate_usd'])
        for outcome, usage in (('returned', observed_usage('claude', 'run_claude', ('text', 0, 0.0, 10, 5))),
                               ('returned', None), ('terminated', None)):
            with self.subTest(outcome=outcome, usage=usage):
                with tempfile.TemporaryDirectory() as tmp:
                    store = LimitsStore(Path(tmp), lambda: self.now)
                    store.authorize(undeclared(), {'ticket': 'T1', 'controller': 'offline'})
                    identity = store.reserve('03-dev', '03-dev', 'claude')
                    store.finish(identity, outcome, usage)
                    with self.assertRaisesRegex(RecoveryStop, 'not counted as zero') as caught:
                        store.reserve('03-dev', '03-dev', 'claude')
                    self.assertEqual(caught.exception.category, 'LIMIT_REACHED')
        store = self.store(policy(live_unknown_usage=reserve_model(1, 'claude')))
        identity = store.reserve('03-dev', '03-dev', 'claude')
        store.finish(identity, 'returned', {'metered_usd': None, 'cli_estimate_usd': 1.5})
        row = self.rows(store)[identity]
        # The observation is recorded apart; it never silently replaces the reserve.
        self.assertEqual((row['conservative_usage']['cli_estimate_usd'], row['usage']['cli_estimate_usd']), (1, 1.5))
        identity = store.reserve('03-dev', '03-dev', 'claude')
        store.finish(identity, 'returned')
        envelope, state = store._load()
        self.assertEqual(store.accounted_total(state, envelope['policy']), 2.5)
        store.reserve('03-dev', '03-dev', 'claude')

    def test_crash_between_reservation_and_response_preserves_the_reserve(self):
        store = self.store(policy(live_unknown_usage=reserve_model(3, 'codex')))
        identity = store.reserve('03-dev', '03-dev', 'codex')
        resumed = LimitsStore(self.root, lambda: self.now)
        envelope, state = resumed._load()
        self.assertEqual(state['invocations'][identity]['status'], 'reserved')
        self.assertEqual(resumed.accounted_total(state, envelope['policy']), 3)
        with self.assertRaisesRegex(RecoveryStop, 'no confirmed outcome'):
            resumed.reserve('03-dev', '03-dev', 'codex')
        resumed.finish(identity, 'uncertain')
        envelope, state = resumed._load()
        self.assertEqual(state['invocations'][identity]['conservative_usage'], {'metered_usd': 3, 'cli_estimate_usd': 3})
        self.assertEqual(resumed.accounted_total(state, envelope['policy']), 3)

    def test_replay_does_not_double_the_reserve(self):
        profile = policy(live_unknown_usage=reserve_model(3, 'codex'))
        store = self.store(profile)
        identity = store.reserve('03-dev', '03-dev', 'codex')
        store.finish(identity, 'returned')
        before = store._load()[1]
        store.finish(identity, 'returned')
        LimitsStore(self.root, lambda: self.now).authorize(profile, {'ticket': 'T1', 'controller': 'offline'})
        envelope, after = LimitsStore(self.root, lambda: self.now)._load()
        self.assertEqual(after, before)
        self.assertEqual(LimitsStore.accounted_total(after, envelope['policy']), 3)

    def test_combined_cap_does_not_count_one_invocation_reserve_twice(self):
        profile = policy(live_unknown_usage=reserve_model(3, 'codex'))
        store = self.store(profile)
        row = {'role': '03-dev', 'phase': '03-dev', 'engine': 'codex', 'status': 'returned',
               'usage': {'metered_usd': None, 'cli_estimate_usd': None}, 'usage_model': 'reserve_per_invocation',
               'conservative_usage': {'metered_usd': 3, 'cli_estimate_usd': 3}}
        state = {'started_at': 0, 'deadline_at': 1000, 'combined_reported_usd_cap': 5, 'invocations': {'a': row}}
        self.assertEqual(LimitsStore.accounted_total(state, profile), 3)
        store.check(state, profile)  # 3 < 5; an additive count (6) would wrongly exhaust it.
        with self.assertRaisesRegex(RecoveryStop, 'reserve would exceed the legacy combined'):
            store._check_dollars(state, profile, [row, dict(row)], reserving=True)
        state['invocations']['b'] = copy.deepcopy(row)
        with self.assertRaisesRegex(RecoveryStop, 'combined reported-cost cap exhausted'):
            store.check(state, profile)

    def test_existing_null_live_row_is_reported_and_only_an_exact_admission_stops_it_blocking(self):
        store = self.store(undeclared())
        identity = store.reserve('03-dev', '03-dev', 'claude')
        store.finish(identity, 'returned')
        report = store.coverage_report()
        self.assertEqual(report['uncovered_invocations'],
                         [{'invocation_id': identity, 'engine': 'claude', 'legacy_admitted': False}])
        self.assertEqual(report['engines']['claude']['dollar_caps_cover'], 'none')
        with self.assertRaises(RecoveryStop):
            store.reserve('03-dev', '03-dev', 'claude')
        envelope, state = store._load()
        store.check(dict(state, legacy_uncovered_invocations=[identity]), envelope['policy'])
        admitted = LimitsStore.coverage(envelope, dict(state, legacy_uncovered_invocations=[identity]))
        self.assertTrue(admitted['uncovered_invocations'][0]['legacy_admitted'])
        self.assertEqual(admitted['engines']['claude']['uncovered'], 1)
        with self.assertRaises(RecoveryStop):
            store.check(dict(state, legacy_uncovered_invocations=['another']), envelope['policy'])

    def test_reported_tokens_are_recorded_apart_and_never_priced(self):
        from orchestrator import _supervised_dispatch
        self.assertEqual(observed_tokens('claude', 'run_claude', ('text', 0, 0.5, 120, 30)),
                         {'input_tokens': 120, 'output_tokens': 30})
        self.assertEqual(observed_tokens('codex', 'run_codex', ('text', 0)),
                         {'input_tokens': None, 'output_tokens': None})
        store = self.store(policy())
        @_supervised_dispatch
        def run_claude(prompt, cwd): return 'text', 0, 0.5, 120, 30
        self.dispatch(store, run_claude, 'prompt', self.root)
        row = next(iter(self.rows(store).values()))
        self.assertEqual(row['usage'], {'metered_usd': None, 'cli_estimate_usd': 0.5})
        self.assertEqual(row['reported_tokens'], {'input_tokens': 120, 'output_tokens': 30})
        envelope, state = store._load()
        self.assertEqual(store.accounted_total(state, envelope['policy']), 0.5)
        self.assertEqual(store.coverage_report()['engines']['claude']['reported_tokens'],
                         {'input_tokens': 120, 'output_tokens': 30})
        identity = store.reserve('03-dev', '03-dev', 'codex')
        for tokens in ({'input_tokens': -1, 'output_tokens': None}, {'input_tokens': 1.5, 'output_tokens': None},
                       {'input_tokens': True, 'output_tokens': None}, {'tokens': 3}):
            with self.subTest(tokens=tokens), self.assertRaises(ValueError):
                store.finish(identity, 'returned', None, tokens)

    def test_codex_reported_tokens_are_recorded_from_simulated_cli_output(self):
        import subprocess
        from orchestrator import EngineRunner
        events = [{'type': 'thread.started', 'thread_id': 't'}, {'type': 'turn.started'},
                  {'type': 'item.completed', 'item': {'id': 'i0', 'type': 'reasoning', 'text': 'thinking'}},
                  {'type': 'item.completed', 'item': {'id': 'i1', 'type': 'agent_message', 'text': 'final answer'}},
                  {'type': 'turn.completed', 'usage': {'input_tokens': 131000, 'cached_input_tokens': 90000,
                                                       'output_tokens': 4200}}]
        stdout = '\n'.join(json.dumps(e) for e in events) + '\n'
        calls = []
        def cli(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout, 'tool transcript')
        store = self.store(policy())
        with patch.object(EngineRunner, '_should_spawn_role_container', return_value=False), \
                patch.object(EngineRunner, '_run_role_command', side_effect=cli):
            result = self.dispatch(store, EngineRunner.run_codex, 'prompt', self.root)
        self.assertIn('--json', calls[0])
        # The phase result still unpacks as (text, code); tokens ride along as telemetry.
        text, code = result
        self.assertEqual((text, code), ('final answer', 0))
        row = next(iter(self.rows(store).values()))
        self.assertEqual(row['engine'], 'codex')
        self.assertEqual(row['usage'], {'metered_usd': None, 'cli_estimate_usd': None})
        self.assertEqual(row['reported_tokens'], {'input_tokens': 131000, 'output_tokens': 4200})
        envelope, state = store._load()
        self.assertEqual(store.accounted_total(state, envelope['policy']), 0)
        codex = store.coverage_report()['engines']['codex']
        self.assertEqual((codex['unmetered_quota'], codex['reported_tokens']),
                         (1, {'input_tokens': 131000, 'output_tokens': 4200}))
        # Failure and non-JSON output: errors and raw text are kept, absent tokens stay unreported.
        failed = '\n'.join(json.dumps(e) for e in (
            {'type': 'turn.failed', 'error': {'message': 'usage limit reached'}},)) + '\n'
        with patch.object(EngineRunner, '_should_spawn_role_container', return_value=False), \
                patch.object(EngineRunner, '_run_role_command',
                             return_value=subprocess.CompletedProcess([], 1, failed, 'stderr')):
            result = EngineRunner.run_codex('prompt', self.root)
        self.assertEqual(tuple(result), ('usage limit reached\nstderr', 1))
        self.assertEqual(observed_tokens('codex', 'run_codex', result), {'input_tokens': None, 'output_tokens': None})
        with patch.object(EngineRunner, '_should_spawn_role_container', return_value=False), \
                patch.object(EngineRunner, '_run_role_command',
                             return_value=subprocess.CompletedProcess([], 0, 'plain text\n', '')):
            result = EngineRunner.run_codex('prompt', self.root)
        self.assertEqual(tuple(result), ('plain text', 0))
        self.assertEqual(observed_tokens('codex', 'run_codex', result), {'input_tokens': None, 'output_tokens': None})

    def test_codex_reported_tokens_survive_invoke_role_to_the_row_and_report(self):
        import subprocess
        from orchestrator import EngineRunner
        events = [{'type': 'item.completed', 'item': {'id': 'i1', 'type': 'agent_message', 'text': 'role answer'}},
                  {'type': 'turn.completed', 'usage': {'input_tokens': 120, 'output_tokens': 30}}]
        stdout = '\n'.join(json.dumps(e) for e in events) + '\n'
        store = self.store(policy())
        with patch.object(EngineRunner, '_should_spawn_role_container', return_value=False), \
                patch.object(EngineRunner, '_run_role_command',
                             return_value=subprocess.CompletedProcess([], 0, stdout, '')):
            result = self.dispatch(store, EngineRunner.invoke_role, 'codex', 'm', 'sys', 'user', self.root,
                                   'P', 'T1', '03-dev')
        # Controller callers still unpack five values; tokens are never priced.
        out, cost, tin, tout, trace = result
        self.assertEqual((out, cost, trace), ('role answer', 0.0, 'P:T1:03-dev'))
        rows = self.rows(store)
        self.assertEqual(len(rows), 1)
        row = next(iter(rows.values()))
        self.assertEqual(row['engine'], 'codex')
        self.assertEqual(row['usage'], {'metered_usd': None, 'cli_estimate_usd': None})
        self.assertEqual(row['reported_tokens'], {'input_tokens': 120, 'output_tokens': 30})
        codex = store.coverage_report()['engines']['codex']
        self.assertEqual(codex['reported_tokens'], {'input_tokens': 120, 'output_tokens': 30})

    def test_status_and_evidence_pack_show_partial_dollar_cap_coverage(self):
        from orchestrator import SSDLCOrchestrator
        import ssdlc_supervisor
        store = self.store(undeclared(live_unknown_usage=reserve_model(1, 'agy')))
        identity = store.reserve('03-dev', '03-dev', 'claude')
        store.finish(identity, 'returned', {'metered_usd': None, 'cli_estimate_usd': 0.2}, {'input_tokens': 9, 'output_tokens': 1})
        identity = store.reserve('03-dev', '03-dev', 'claude')
        store.finish(identity, 'returned')
        with patch('ssdlc_supervisor.canonical_root', return_value=self.root):
            coverage = ssdlc_supervisor.limits_coverage(self.root, 'T1')
        self.assertEqual(coverage['engines']['claude']['dollar_caps_cover'], 'partial')
        self.assertEqual(coverage['engines']['claude']['observed_cost'], 1)
        self.assertFalse(coverage['dollar_caps_cover_all_invocations'])
        text = SSDLCOrchestrator._limits_coverage_disclosure(SimpleNamespace(ssdlc_root=self.root))
        self.assertIn('only part of the invocations', text)
        self.assertIn('| `claude` | 2 | 1 | 0 | 0 | 1 | 0 | 9 / 1 | **partial** |', text)
        self.assertIn(identity, text)
        self.assertEqual(SSDLCOrchestrator._limits_coverage_disclosure(SimpleNamespace(ssdlc_root=self.root / 'none')), '')


class LiveUsageAdmissionTests(unittest.TestCase):
    """An active profile gains a model only through an exact admission; counters stay frozen."""
    def setUp(self):
        import test_scope_amendments as fixtures
        self.case = f = fixtures.ScopeAmendmentTests(); f.initialize(); self.addCleanup(f.doCleanups)
        # A live row recorded with null usage and no model, as on issue #22.
        source_store = LimitsStore(f.source['root'])
        self.legacy = source_store.reserve('03-dev', '03-dev', 'claude')
        source_store.finish(self.legacy, 'returned')
        from ssdlc_scope_amendments import inspect
        self.source = inspect(f.ticket)
        self.profile = copy.deepcopy(self.source['envelope']['policy'])
        self.profile['live_unknown_usage'].update(reserve_model(0.5, 'claude'))

    def stage(self, profile=None):
        import ssdlc_usage_admission as admission
        return admission.prepare(self.case.ticket, self.case.repo, profile or self.profile,
            'Declare the live claude reserve; list legacy null-cost rows as not covered',
            runtime_identity=self.case.identity, controller_snapshot=self.case.new_controller)

    def auth(self, staged, **overrides):
        p = staged['proposal']
        value = dict(schema_version=1, transition_id=p['transition_id'], proposal_sha256=digest(p), authorized=True,
            source_fenced=True, old_controller_sha256=p['old_controller_sha256'],
            new_controller_sha256=p['new_controller_sha256'], profile_sha256=p['profile_sha256'],
            legacy_uncovered_invocations=p['legacy_uncovered_invocations'])
        value.update(overrides)
        return value

    def test_exact_admission_lists_legacy_rows_without_resetting_or_reclassifying(self):
        import ssdlc_usage_admission as admission
        from ssdlc_transitions import runtime_root
        with self.assertRaisesRegex(RecoveryStop, 'not counted as zero'):
            LimitsStore(self.source['root']).reserve('03-dev', '03-dev', 'claude')
        staged = self.stage()
        proposal = staged['proposal']
        self.assertEqual(proposal['authorization_contract'], admission.CONTRACT)
        self.assertEqual(proposal['legacy_uncovered_invocations'], [self.legacy])
        for bad in (dict(legacy_uncovered_invocations=[]), dict(legacy_uncovered_invocations=None),
                    dict(profile_sha256='0' * 64)):
            with self.subTest(bad=bad), self.assertRaises(TaskError):
                admission.activate(self.case.ticket, staged['preparation'], self.auth(staged, **bad))
        admission.activate(self.case.ticket, staged['preparation'], self.auth(staged))
        root = runtime_root(self.case.ticket)
        store = LimitsStore(root)
        envelope, counters = store._load(repair=False)
        self.assertEqual(envelope['policy'], self.profile)
        self.assertEqual(envelope['binding']['legacy_uncovered_invocations'], [self.legacy])
        self.assertEqual(counters, dict(self.source['counters'], policy_sha256=digest(envelope),
                                        legacy_uncovered_invocations=[self.legacy]))
        self.assertEqual(counters['invocations'][self.legacy], self.source['counters']['invocations'][self.legacy])
        report = store.coverage_report()
        self.assertEqual(report['uncovered_invocations'],
                         [{'invocation_id': self.legacy, 'engine': 'claude', 'legacy_admitted': True}])
        identity = store.reserve('03-dev', '03-dev', 'claude')
        self.assertEqual(store._load()[1]['invocations'][identity]['conservative_usage'],
                         {'metered_usd': 0.5, 'cli_estimate_usd': 0.5})
        store.finish(identity, 'returned')
        identity = store.reserve('03-dev', '03-dev', 'openrouter')
        store.finish(identity, 'returned')
        with self.assertRaisesRegex(RecoveryStop, 'not counted as zero'):
            store.reserve('03-dev', '03-dev', 'claude')

    def test_admission_cannot_change_caps_deadline_or_declared_models(self):
        before = copy.deepcopy(self.profile)
        for field in ('wall_seconds', 'invocations', 'metered_usd', 'cli_estimate_usd', 'development_attempts'):
            self.profile = copy.deepcopy(before); self.profile[field] += 1
            with self.subTest(field=field), self.assertRaises(TaskError):
                self.stage(self.profile)
        changed = copy.deepcopy(before); changed['live_unknown_usage']['codex'] = reserve_model(1, 'codex')['codex']
        with self.assertRaises(TaskError):
            self.stage(changed)
        with self.assertRaises(TaskError):
            self.stage(copy.deepcopy(self.source['envelope']['policy']))

    def test_admission_refuses_a_reserve_beyond_the_remaining_budget(self):
        """Issue #128: 3 dollars of claude on 9.27 accounted under a combined cap of 10."""
        from ssdlc_transitions import runtime_root
        import ssdlc_usage_admission as admission
        selected = runtime_root(self.case.ticket)
        envelope, counters = LimitsStore(self.source['root'])._load(repair=False)
        accounted = LimitsStore.accounted_total(counters, envelope['policy'])
        self.assertGreater(accounted + 3, counters['combined_reported_usd_cap'])
        self.assertLessEqual(accounted + 0.5, counters['combined_reported_usd_cap'])
        excessive = copy.deepcopy(self.source['envelope']['policy'])
        excessive['live_unknown_usage'].update(reserve_model(3, 'claude'))
        with self.assertRaisesRegex(RecoveryStop, 'reserve would exceed') as caught:
            self.stage(excessive)
        self.assertEqual(caught.exception.category, 'LIMIT_REACHED')
        self.assertEqual(runtime_root(self.case.ticket), selected)
        # A preparation that skipped the check is still refused at activation.
        with patch.object(LimitsStore, 'check_capacity', lambda *args, **kwargs: None):
            staged = self.stage(excessive)
        active = (self.case.ticket / 'execution/active.json').read_bytes()
        with self.assertRaisesRegex(RecoveryStop, 'reserve would exceed'):
            admission.activate(self.case.ticket, staged['preparation'], self.auth(staged))
        # The selector never moved; the refused preparation stays pending and fenced.
        self.assertEqual((self.case.ticket / 'execution/active.json').read_bytes(), active)
        self.assertEqual(LimitsStore(selected)._load(repair=False)[1], counters)


class ImminentCallCapacityTests(unittest.TestCase):
    """Issue #128: the imminent call is validated before any durable state is written."""

    def feedback_case(self, live):
        import test_feedback_roles as fixtures
        def profile(**overrides):
            value = policy(**overrides)
            value['live_unknown_usage'] = live
            return value
        case = fixtures.FundedFeedbackRoleTests()
        with patch.object(fixtures, 'policy', profile):
            case.initialize()
        self.addCleanup(case.doCleanups)
        # Eight dollars already spent under the ten-dollar caps.
        spent = case.limits.reserve('03-dev', '03-dev', 'claude')
        case.limits.finish(spent, 'returned', {'metered_usd': None, 'cli_estimate_usd': 8})
        return case

    def test_feedback_consultation_is_refused_before_its_role_intent(self):
        agy = {'agy': {'model': 'unmetered_quota', 'reason': REASON}}
        for name, live, message in (('reserve', dict(agy, **reserve_model(3, 'codex')), 'reserve would exceed'),
                                    ('undeclared', agy, 'refused before launch')):
            with self.subTest(name):
                case = self.feedback_case(live)
                before = case.limits._load(repair=False)[1]['invocations']
                with self.assertRaisesRegex(RecoveryStop, message) as caught:
                    case.run_role()
                self.assertEqual(caught.exception.category, 'LIMIT_REACHED')
                self.assertEqual(case.calls, [])
                self.assertEqual(case.fixture.store.inspect()['role_calls'], [])
                self.assertEqual(case.limits._load(repair=False)[1]['invocations'], before)

    def test_scheduler_refuses_before_consuming_an_attempt_or_starting_the_task(self):
        import test_reconciliation as fixtures
        from ssdlc_tasks import TaskJournal, TaskScheduler
        from ssdlc_transitions import runtime_root
        f = fixtures.ReconciliationTests(); f.setUp(); self.addCleanup(f.doCleanups)
        f.request['profile']['live_unknown_usage'].pop('codex')
        f.identity['environment'] = dict(mode='role_container', image_id='offline')
        f.select(f.stage())
        root = runtime_root(f.target)
        limits = LimitsStore(root)
        envelope, counters = limits._load(repair=False)
        journal = TaskJournal(root / '03-dev/tasks')
        before = journal.peek()
        developed, selected = [], []
        def developer(task, attempt, prompt, workspace):
            developed.append(attempt)
            raise AssertionError('No developer may be dispatched')
        def dispatch_engine(task, attempt):
            selected.append((task['task_id'], attempt))
            return 'codex'
        scheduler = TaskScheduler(root / 'worktree', root / '03-dev/tasks',
            json.loads((root / '02-design/plan.json').read_text()),
            json.loads((root / '00-recon/quality-contract.json').read_text()), f.context,
            developer, f.verify, max_attempts=envelope['policy']['development_attempts'], isolate_candidates=True,
            runtime_identity=before['candidate_runtime'], recover_timeouts=True,
            historical_capture_authority=envelope['binding']['historical_capture_bindings_sha256'],
            historical_task_floors=envelope['binding'].get('task_floors'),
            scope_reconciliations=envelope['binding'].get('scope_reconciliations'),
            dispatch_budget=lambda engine=None: limits.require_capacity('03-dev', '03-dev', engine),
            dispatch_engine=dispatch_engine)
        with self.assertRaisesRegex(RecoveryStop, 'refused before launch') as caught:
            scheduler.run(f.new_plan['tasks'])
        self.assertEqual(caught.exception.category, 'LIMIT_REACHED')
        self.assertEqual(developed, [])
        self.assertTrue(selected)
        task_id, attempt = selected[0]
        self.assertEqual(attempt, before['tasks'][task_id]['attempts'] + 1)
        # No attempt consumed, no DISPATCH_RESERVED or TASK_STARTED, no reservation.
        self.assertEqual(journal.peek()['tasks'], before['tasks'])
        self.assertEqual(limits._load(repair=False)[1], counters)


class FeedbackLedgerAdmissionTests(unittest.TestCase):
    """Issue #128 P2: a live-usage admission reconciles an active feedback ledger."""

    def setUp(self):
        import test_feedback_workflow as fixtures
        self.case = case = fixtures.FeedbackWorkflowTests()
        case.initialize(); self.addCleanup(case.doCleanups)
        # A live claude row returned without a cost, and claude has no declared model.
        store = LimitsStore(case.source['root'])
        self.legacy = store.reserve('03-dev', '03-dev', 'claude')
        store.finish(self.legacy, 'returned')

    def admit(self):
        import ssdlc_usage_admission as admission
        from ssdlc_scope_amendments import inspect
        case = self.case
        profile = copy.deepcopy(inspect(case.case.ticket, allow_host_hints=True)['envelope']['policy'])
        profile['live_unknown_usage'].update(reserve_model(0.5, 'claude'))
        with case.host():
            staged = admission.prepare(case.case.ticket, case.case.repo, profile, 'Declare the live claude reserve',
                                       runtime_identity=case.identity, controller_snapshot=case.snapshot)
            p = staged['proposal']
            admission.activate(case.case.ticket, staged['preparation'], dict(
                schema_version=1, transition_id=p['transition_id'], proposal_sha256=digest(p), authorized=True,
                source_fenced=True, old_controller_sha256=p['old_controller_sha256'],
                new_controller_sha256=p['new_controller_sha256'], profile_sha256=p['profile_sha256'],
                legacy_uncovered_invocations=p['legacy_uncovered_invocations']))
        return p['transition_id']

    def test_resume_after_admission_needs_no_manual_feedback_reconciliation(self):
        from ssdlc_feedback import FeedbackStore
        from ssdlc_reconciliation import resolve_execution
        from ssdlc_tasks import atomic_json
        from ssdlc_transitions import runtime_root
        case = self.case
        with self.assertRaisesRegex(RecoveryStop, 'not counted as zero'):
            case.run_flow()
        feedback = FeedbackStore(case.case.ticket)
        stopped = feedback.inspect()
        (request_id,) = stopped['requests']
        self.assertEqual(stopped['role_calls'], [])
        self.assertEqual(case.calls, [])
        transition = self.admit()
        state = feedback.inspect()
        entry = state['admitted_generations'][transition]
        self.assertEqual(entry['carried_requests'], [request_id])
        self.assertEqual(state['requests'], stopped['requests'])
        root = runtime_root(case.case.ticket)
        self.assertEqual(resolve_execution(case.case.ticket), root)
        # The orchestrator escalates the resumed generation to feedback again.
        outer = json.loads((root / 'state.json').read_text())
        outer.update(status='ESCALATED_TO_SUPERVISOR', current_step='feedback')
        atomic_json(root / 'state.json', outer)
        result = case.run_flow()
        self.assertEqual(result['disposition'], 'APPLIED')
        self.assertEqual([name for name, _ in case.calls], ['feedback-planner', 'feedback-reviewer'])
        state = feedback.inspect()
        self.assertEqual(list(state['requests']), [request_id])
        self.assertEqual(state['requests'][request_id]['occurrences'][-1]['carried_by'], transition)
        rows = LimitsStore(Path(result['runtime']))._load(repair=False)[1]['invocations']
        planner = [row for row in rows.values() if row['role'] == 'feedback-planner']
        self.assertEqual(planner[0]['conservative_usage'], {'metered_usd': 0.5, 'cli_estimate_usd': 0.5})

    def test_admission_is_fenced_until_the_feedback_ledger_is_reconciled(self):
        import ssdlc_usage_admission as admission
        from ssdlc_reconciliation import resolve_execution
        with self.assertRaises(RecoveryStop):
            self.case.run_flow()
        with patch.object(admission, 'finalize', side_effect=OSError('crash after selector publish')):
            with self.assertRaises(OSError):
                self.admit()
        with self.assertRaisesRegex(TaskError, 'reconciliation pending'):
            resolve_execution(self.case.case.ticket)


if __name__ == '__main__':
    unittest.main()
