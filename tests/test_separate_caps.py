"""Issue #165 (part B1 of #14): separate spend and estimate caps, measures per route,
unknown routes refused before dispatch. Every provider is a local fake."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import orchestrator
from orchestrator import CostAndTelemetryEntry, EngineRunner, SSDLCOrchestrator, _dispatch_context
import ssdlc_telemetry as t
from ssdlc_recovery import LimitsStore, RecoveryStop
from ssdlc_tasks import TaskError
from test_recovery_limits import policy

ROOT = Path(__file__).resolve().parents[1]
CAP_VARIABLES = ('SSDLC_BUDGET_CAP_USD', 'SSDLC_ESTIMATE_CAP_USD', 'SSDLC_DEV_BUDGET_USD', 'SSDLC_MAX_DEV_RETRIES',
                 'SSDLC_SUPERVISED_RUN_ID', 'OPENROUTER_API_KEY', 'ANTHROPIC_BASE_URL')
CLAUDE = {'type': 'result', 'result': 'claude answer', 'num_turns': 1, 'total_cost_usd': 0.5,
          'usage': {'input_tokens': 1, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0,
                    'output_tokens': 1}}


def environment(**values):
    """os.environ without any cap or route variable, plus ``values``."""
    patcher = patch.dict(os.environ)
    patcher.start()
    for name in CAP_VARIABLES:
        os.environ.pop(name, None)
    os.environ.update(values)
    return patcher


def entry(engine, telemetry, cost=0.0):
    return CostAndTelemetryEntry('03-dev', 'Role', engine, 'm', 'GENERATED', 1.0, cost,
                                 telemetry=telemetry).to_dict()


def subscription_estimate(amount):
    """A Claude CLI call on a subscription: its total_cost_usd is an API-equivalent estimate."""
    return entry('claude', t.from_claude(dict(CLAUDE, total_cost_usd=amount)), amount)


def openrouter_charge(amount):
    usage = {'prompt_tokens': 10, 'completion_tokens': 5}
    if amount is not None:
        usage['cost'] = amount
    return entry('openrouter', t.from_openrouter(usage), amount or 0.0)


def codex_call():
    return entry('codex', t.from_codex([{'input_tokens': 10, 'output_tokens': 5}]))


class BreakerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        limits = patch.object(orchestrator._dispatch_local, 'limits', None, create=True)
        limits.start(); self.addCleanup(limits.stop)

    def orch(self, ledger, tier='M'):
        orch = SSDLCOrchestrator.__new__(SSDLCOrchestrator)
        orch.ssdlc_root = Path(self.tmp.name)
        orch.state = dict(artifacts={}, ledger=list(ledger), risk_tier=tier)
        orch.record_gate = Mock()
        orch._save_state = Mock()
        return orch

    def env(self, **values):
        self.addCleanup(environment(**values).stop)

    def halted(self, orch, gate):
        with self.assertRaises(TaskError) as caught:
            orch.check_budget_circuit_breaker()
        self.assertEqual(caught.exception.category, 'LIMIT_REACHED')
        self.assertEqual(orch.record_gate.call_args.args[:2], (gate, False))
        self.assertEqual(orch.state['status'], 'HALTED_BUDGET_EXCEEDED')
        return str(caught.exception)


class BreakerTests(BreakerCase):
    def test_subscription_estimates_never_trip_the_measured_spend_cap_with_mixed_routes(self):
        self.env(SSDLC_BUDGET_CAP_USD='1', SSDLC_ESTIMATE_CAP_USD='50')
        # $9 of Claude estimates, codex subscription calls and $0.40 of OpenRouter charges.
        ledger = [subscription_estimate(4.5), codex_call(), openrouter_charge(0.4), subscription_estimate(4.5)]
        orch = self.orch(ledger)
        orch.check_budget_circuit_breaker()
        orch.record_gate.assert_not_called()
        self.assertEqual(t.ledger_measures(ledger), {'spend_usd': 0.4, 'estimate_usd': 9.0,
                                                     'unclassified_usd': 0, 'unmeasured_spend': []})
        # The measured route keeps applying the cap.
        orch = self.orch(ledger + [openrouter_charge(0.6)])
        self.assertIn('Measured spend ($1.00)', self.halted(orch, 'BUDGET_CAP_GATE'))

    def test_measured_route_without_a_charge_is_not_counted_as_zero(self):
        self.env(SSDLC_BUDGET_CAP_USD='5')
        orch = self.orch([subscription_estimate(1), openrouter_charge(None)])
        self.assertIn('measured spend is unknown', self.halted(orch, 'BUDGET_CAP_GATE'))
        # Claude routed to OpenRouter: its estimate is not a charge either.
        routed = entry('claude', t.from_claude(CLAUDE, route=t.provenance('openrouter_api', 'explicit_config')), 0.5)
        self.assertEqual(t.ledger_measures([routed])['unmeasured_spend'], [routed['telemetry']['invocation_id']])
        self.halted(self.orch([routed]), 'BUDGET_CAP_GATE')
        # Only an explicit zero disables the spend cap.
        self.env(SSDLC_BUDGET_CAP_USD='0')
        self.orch([routed, openrouter_charge(None)]).check_budget_circuit_breaker()

    def test_legacy_figures_keep_counting_as_spend_and_are_not_reinterpreted(self):
        self.env(SSDLC_BUDGET_CAP_USD='2', SSDLC_ESTIMATE_CAP_USD='1')
        legacy = {'phase': '03-dev', 'engine': 'claude', 'cost_usd': 1.5}
        unknown = entry('openrouter', t.record('openrouter'), 0.7)
        self.assertEqual(t.ledger_measures([legacy, unknown])['unclassified_usd'], 2.2)
        self.assertIn('Measured spend ($2.20)', self.halted(self.orch([legacy, unknown]), 'BUDGET_CAP_GATE'))
        # A legacy figure does not consume the estimate cap.
        self.env(SSDLC_BUDGET_CAP_USD='0', SSDLC_ESTIMATE_CAP_USD='1')
        self.orch([legacy, unknown]).check_budget_circuit_breaker()

    def test_each_invocation_counts_once(self):
        record = t.from_openrouter({'cost': 0.6, 'prompt_tokens': 1, 'completion_tokens': 1})
        rows = [entry('openrouter', record, 0.6), entry('openrouter', record, 0.6)]
        self.assertEqual(t.ledger_measures(rows)['spend_usd'], 0.6)
        self.env(SSDLC_BUDGET_CAP_USD='1')
        self.orch(rows).check_budget_circuit_breaker()


class EstimateCapTests(BreakerCase):
    def test_estimate_cap_has_tier_defaults_and_is_independent(self):
        for tier, default in (('S', 3.), ('M', 6.), ('L', 20.), ('?', 6.)):
            self.assertEqual(t.estimate_cap({}, tier), default)
        self.assertEqual(t.estimate_cap({'SSDLC_ESTIMATE_CAP_USD': '12.5', 'SSDLC_BUDGET_CAP_USD': '1',
                                         'SSDLC_DEV_BUDGET_USD': '9'}, 'S'), 12.5)
        self.assertIsNone(t.estimate_cap({'SSDLC_ESTIMATE_CAP_USD': '0'}, 'L'))
        for bad in ('inf', 'nan', 'invalid'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                t.estimate_cap({'SSDLC_ESTIMATE_CAP_USD': bad}, 'M')

    def test_estimate_cap_stays_active_with_the_tier_default(self):
        self.env()
        ledger = [subscription_estimate(1.5), subscription_estimate(1.5)]
        message = self.halted(self.orch(ledger, tier='S'), 'ESTIMATE_CAP_GATE')
        self.assertIn('estimate cap ($3.00)', message)
        self.orch(ledger, tier='M').check_budget_circuit_breaker()
        self.env(SSDLC_ESTIMATE_CAP_USD='2')
        self.halted(self.orch(ledger, tier='L'), 'ESTIMATE_CAP_GATE')
        # Disabling the spend cap leaves the estimate cap in force.
        self.env(SSDLC_BUDGET_CAP_USD='0', SSDLC_ESTIMATE_CAP_USD='')
        self.halted(self.orch(ledger, tier='S'), 'ESTIMATE_CAP_GATE')
        self.env(SSDLC_ESTIMATE_CAP_USD='inf')
        with self.assertRaises(TaskError):
            self.orch(ledger).check_budget_circuit_breaker()

    def test_developer_max_budget_keeps_its_tier_defaults(self):
        self.env(SSDLC_BUDGET_CAP_USD='0', SSDLC_ESTIMATE_CAP_USD='0')
        self.assertEqual([orchestrator._dev_budget_usd(tier) for tier in ('S', 'M', 'L', '?')], [1.0, 2.5, 8.0, 2.5])
        calls = []
        def fake(cmd, **_):
            calls.append(cmd)
            return subprocess.CompletedProcess([], 0, '{}', '')
        with patch.object(EngineRunner, '_should_spawn_role_container', return_value=False), \
             patch.object(EngineRunner, '_run_role_command', side_effect=fake):
            EngineRunner.run_claude('prompt', Path(self.tmp.name), max_budget_usd=orchestrator._dev_budget_usd('S'))
        self.assertEqual(calls[0][calls[0].index('--max-budget-usd') + 1], '1.00')
        self.env(SSDLC_DEV_BUDGET_USD='4')
        self.assertEqual(orchestrator._dev_budget_usd('L'), 4.0)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.addCleanup(environment(OPENROUTER_API_KEY='k').stop)
        limits = patch.object(orchestrator._dispatch_local, 'limits', None, create=True)
        limits.start(); self.addCleanup(limits.stop)
        self.calls = []
        command = patch.object(EngineRunner, '_run_role_command', side_effect=self.fake)
        command.start(); self.addCleanup(command.stop)
        self.container = patch.object(EngineRunner, '_should_spawn_role_container', return_value=False)
        self.spawn = self.container.start(); self.addCleanup(self.container.stop)

    def fake(self, cmd, **_):
        self.calls.append(cmd)
        return subprocess.CompletedProcess([], 0, '', '')

    def refused(self, fn, *args, **kwargs):
        with self.assertRaisesRegex(RecoveryStop, 'refused before launch') as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.category, 'LIMIT_REACHED')
        self.assertEqual(self.calls, [])

    def test_measures_required_per_route(self):
        caps = dict(spend_cap=True, estimate_cap_active=True)
        self.assertEqual(t.admit('openrouter', 'openrouter_api', **caps)['measure'], 'spend')
        self.assertEqual(t.admit('claude', 'claude_cli', **caps)['measure'], 'estimate')
        declared = policy()['live_unknown_usage']
        self.assertEqual(t.admit('codex', 'codex_cli', declarations=declared, **caps)['measure'], 'unmetered_quota')
        self.assertIsNone(t.admit('agy', 'agy_cli', **caps)['measure'])
        # An estimate never stands for the charge a measured route needs.
        for provider in ('claude', 'codex'):
            with self.assertRaises(t.AdmissionRefused):
                t.admit(provider, 'openrouter_api', declarations=declared, **caps)
        self.assertIsNone(t.admit('claude', 'openrouter_api', spend_cap=False, estimate_cap_active=True)['measure'])
        for spend, estimate in ((True, False), (False, True), (True, True)):
            with self.assertRaises(t.AdmissionRefused):
                t.admit('claude', None, spend_cap=spend, estimate_cap_active=estimate)
        self.assertEqual(t.admit('claude', None, spend_cap=False, estimate_cap_active=False)['route'], 'unknown')
        with self.assertRaises(t.AdmissionRefused):
            t.admit('claude', 'some_gateway', **caps)

    def test_unknown_route_with_a_cap_in_force_is_refused_before_dispatch(self):
        # OpenRouter requested, but the role container would not receive its base URL.
        self.spawn.return_value = True
        self.refused(EngineRunner.run_claude, 'prompt', self.root, use_openrouter=True)
        self.spawn.return_value = False
        # Another base URL the Claude CLI would receive: neither subscription nor a measured route.
        os.environ['ANTHROPIC_BASE_URL'] = 'https://gateway.example/api'
        self.refused(EngineRunner.run_claude, 'prompt', self.root)
        self.refused(EngineRunner.invoke_role, 'claude', 'm', 's', 'u', self.root, 'P', 'T', '03-dev')
        # The spend cap alone is enough to refuse it; only both caps explicitly at zero admit it.
        os.environ['SSDLC_ESTIMATE_CAP_USD'] = '0'
        self.refused(EngineRunner.run_claude, 'prompt', self.root)
        os.environ['SSDLC_BUDGET_CAP_USD'] = '0'
        EngineRunner.run_claude('prompt', self.root)
        self.assertEqual(len(self.calls), 1)

    def test_measured_route_without_its_measure_is_refused_and_known_routes_proceed(self):
        self.refused(EngineRunner.run_claude, 'prompt', self.root, use_openrouter=True)
        self.refused(EngineRunner.run_codex, 'prompt', self.root, use_openrouter=True)
        EngineRunner.run_claude('prompt', self.root)
        EngineRunner.run_codex('prompt', self.root)
        self.assertEqual(len(self.calls), 2)

    def test_frozen_profile_refuses_before_reserve(self):
        store = LimitsStore(self.root, lambda: 100)
        store.authorize(policy(), {'ticket': 'T1', 'controller': 'offline'})
        rows = lambda: store._load(repair=False)[1]['invocations']
        # Even with the environment caps disabled, the profile caps are mandatory.
        os.environ.update(SSDLC_BUDGET_CAP_USD='0', SSDLC_ESTIMATE_CAP_USD='0')
        with patch.object(orchestrator._dispatch_local, 'limits', store), _dispatch_context(phase='03-dev'):
            self.refused(EngineRunner.run_claude, 'prompt', self.root, use_openrouter=True)
            # codex declares unmetered_quota for its subscription route, not for OpenRouter.
            self.refused(EngineRunner.run_codex, 'prompt', self.root, use_openrouter=True)
            self.spawn.return_value = True
            self.refused(EngineRunner.run_claude, 'prompt', self.root, use_openrouter=True)
            self.assertEqual(rows(), {})
            self.spawn.return_value = False
            EngineRunner.run_codex('prompt', self.root)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([(r['engine'], r['usage_model'], r['status']) for r in rows().values()],
                         [('codex', 'unmetered_quota', 'returned')])


class ReplayBaselineTests(unittest.TestCase):
    def test_replay_gate_baseline_is_unchanged(self):
        from ssdlc_grader import verify_baseline
        self.assertTrue(verify_baseline(ROOT))


if __name__ == '__main__':
    unittest.main()
