"""Accounting and projections of the WorkflowSnapshot (#348): coverage, scenario, warnings."""
import json
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ssdlc_workflow_projection as wp  # noqa: E402
import ssdlc_workflow_status as ws  # noqa: E402
import ssdlc_telemetry as telemetry  # noqa: E402
from ssdlc_recovery import LimitsStore  # noqa: E402
from test_workflow_status import Case, tree  # noqa: E402


def claude(estimate):
    return telemetry.record('claude', estimate_measure=telemetry.estimate(estimate, source='claude_cli'))


def openrouter(charge):
    return telemetry.record('openrouter', spend_measure=telemetry.spend(charge, 'USD', 'openrouter'),
                            route=telemetry.provenance('openrouter_api', 'explicit_config'))


def row(phase, record=None, **extra):
    result = {'phase': phase, 'telemetry_schema_version': 1, 'telemetry': record, 'duration_sec': 1.0}
    if record is None:  # A legacy row: no telemetry schema, an unclassified cost.
        result = {'phase': phase, 'engine': 'claude', 'cost_usd': 0.5}
    result.update(extra)
    return result


# Task A completed with one Claude call and one OpenRouter call; B and C remain.
LEDGER = [row('03-dev-A-a1', claude(0.30)), row('03-dev-A-a2', openrouter(0.60))]


def policy(**overrides):
    p = {'schema_version': 1, 'profile_id': 'projection', 'invocations': 10, 'wall_seconds': 100000,
         'role_invocations': {'03-dev': 10}, 'phase_invocations': {'03-dev': 10}, 'transport_retries': 0,
         'design_diagnoses': 0, 'metered_usd': 10, 'cli_estimate_usd': 10, 'development_attempts': 2,
         'design_rounds': 1, 'invocation_timeout_seconds': 30}
    p.update(overrides)
    return p


class ProjectionCase(Case):
    def run_with(self, ledger, limits=None, **state):
        self.make_run(self.ticket, ledger=ledger)
        if state:
            data = json.loads((self.ticket / 'state.json').read_text())
            data.update(state)
            (self.ticket / 'state.json').write_text(json.dumps(data))
        if limits is not None:
            store = LimitsStore(self.ticket)
            store.authorize(limits, {'ticket': 'T-346'})
            for engine, usage in (('claude', {'metered_usd': None, 'cli_estimate_usd': 0.3}),
                                  ('openrouter', {'metered_usd': 0.6, 'cli_estimate_usd': None})):
                store.finish(store.reserve('03-dev', '03-dev', engine), 'returned', usage)
            return store

    def assert_parts(self, projection):
        for measure in projection['measures'].values():
            for name in ('spent', 'development', 'gates', 'central'):
                part = measure[name]
                if part['coverage'] != wp.COMPLETE:
                    self.assertTrue(part['reason'], name)
                if part['coverage'] == wp.NONE:
                    self.assertIsNone(part['value'], name)


class CompleteDataTest(ProjectionCase):
    def test_projection_with_complete_data(self):
        self.run_with(LEDGER, policy())
        result = self.snap()
        self.assertEqual(result['read']['status'], ws.COHERENT)
        projection = result['projection']
        self.assert_parts(projection)
        self.assertEqual(projection['coverage'], wp.COMPLETE)
        self.assertEqual(projection['tasks_remaining'], 2)
        self.assertEqual(projection['gates_remaining'], ['04-review', '05-qa', '05b-appsec'])
        self.assertEqual(projection['limits']['status'], 'active')
        self.assertEqual(projection['limits']['invocations'], {'cap': 10, 'used': 2, 'remaining': 8})
        self.assertEqual(projection['warnings'], [])
        spend, estimate = projection['measures']['spend_usd'], projection['measures']['estimate_usd']
        # The measures stay separate, each against the cap of its own kind.
        self.assertEqual((spend['cap_kind'], estimate['cap_kind']), ('metered_usd', 'cli_estimate_usd'))
        # Development: the average per completed task (A) times the two remaining tasks.
        self.assertEqual((spend['development']['per_task'], spend['development']['value']), (0.6, 1.2))
        self.assertEqual((estimate['development']['per_task'], estimate['development']['value']), (0.3, 0.6))
        # Gates: one call per remaining mandatory gate; no gate ran yet, so one call of the run.
        self.assertEqual((spend['gates']['basis'], spend['gates']['per_call'], spend['gates']['value']),
                         ('all_calls', 0.3, 0.9))
        self.assertEqual((estimate['gates']['per_call'], estimate['gates']['value']), (0.15, 0.45))
        self.assertEqual((spend['central']['value'], spend['central']['coverage']), (2.7, wp.COMPLETE))
        self.assertEqual((estimate['central']['value'], estimate['central']['coverage']), (1.35, wp.COMPLETE))
        self.assertEqual(spend['budget'], dict(cap=10, accounted=0.6, remaining=9.4, projected=2.7,
                                               projected_coverage=wp.COMPLETE, over_cap=False))
        self.assertEqual(estimate['budget']['projected'], 1.35)
        # The scenario: remaining invocations at the largest charge of one, never a guaranteed maximum.
        self.assertEqual(spend['scenario'], dict(
            value=5.4, unit='USD', coverage=wp.COMPLETE, source='frozen limits', reason=None,
            remaining_invocations=8, per_invocation=0.6, guaranteed_maximum=False))
        self.assertEqual(estimate['scenario']['value'], 2.7)
        json.dumps(result)

    def test_gate_calls_price_the_remaining_gates_once_each(self):
        ledger = LEDGER + [row('04-review-a1', claude(0.10)), row('04-review-a1', claude(0.20))]
        self.run_with(ledger, current_phase='05-qa')
        projection = self.snap()['projection']
        estimate = projection['measures']['estimate_usd']
        self.assertEqual(projection['gates_remaining'], ['05-qa', '05b-appsec'])
        self.assertEqual((estimate['gates']['basis'], estimate['gates']['per_call'], estimate['gates']['value']),
                         ('gate_calls', 0.15, 0.3))
        self.assertEqual(projection['limits'], dict(status='absent', reason='no frozen limits profile in this run',
                                                    invocations=None))
        self.assertIsNone(estimate['budget'])
        self.assertIsNone(estimate['scenario'])

    def test_a_declared_frozen_reserve_prices_the_scenario(self):
        reserve = {'model': 'reserve_per_invocation', 'metered_usd': 1.5, 'cli_estimate_usd': 0.5,
                   'reason': 'codex reports no dollar cost'}
        self.run_with(LEDGER, policy(live_unknown_usage={'codex': reserve}))
        measures = self.snap()['projection']['measures']
        self.assertEqual((measures['spend_usd']['scenario']['per_invocation'],
                          measures['spend_usd']['scenario']['value']), (1.5, 0.6 + 8 * 1.5))
        self.assertEqual(measures['estimate_usd']['scenario']['per_invocation'], 0.5)

    def test_a_finished_run_has_nothing_left_to_project(self):
        self.make_run(self.ticket, ledger=LEDGER)
        self.writer.append({'schema_version': '1.0', 'tasks': {
            t: {'status': 'COMPLETED', 'attempts': 1} for t in 'ABC'}}, 'TASK_COMPLETED')
        data = json.loads((self.ticket / 'state.json').read_text())
        data.update(status='AWAITING_SUPERVISOR_SIGNOFF', current_phase='07-docs')
        (self.ticket / 'state.json').write_text(json.dumps(data))
        projection = self.snap()['projection']
        self.assertEqual((projection['coverage'], projection['tasks_remaining'], projection['gates_remaining']),
                         (wp.COMPLETE, 0, []))
        self.assertEqual(projection['measures']['spend_usd']['central']['value'], 0.6)


class PartialDataTest(ProjectionCase):
    def test_components_without_data_are_null_and_the_projection_partial(self):
        # The only call of task A is a legacy row: no measure, and no call to price a gate.
        self.run_with([row('03-dev-A-a1')])
        projection = self.snap()['projection']
        self.assert_parts(projection)
        self.assertEqual(projection['coverage'], wp.PARTIAL)
        for name, measure in projection['measures'].items():
            self.assertEqual(measure['development']['coverage'], wp.PARTIAL, name)
            self.assertIn('unclassified', measure['development']['reason'])
            self.assertEqual((measure['gates']['value'], measure['gates']['coverage']), (None, wp.NONE), name)
            self.assertIn('no call in the ledger', measure['gates']['reason'])
            self.assertEqual(measure['central']['coverage'], wp.PARTIAL, name)
            self.assertIsNotNone(measure['central']['value'])
            self.assertIsNone(measure['budget'])

    def test_unmeasured_calls_make_the_measure_partial(self):
        self.run_with([row('03-dev-A-a1', claude(None)), row('03-dev-A-a2', openrouter(0.6))])
        measures = self.snap()['projection']['measures']
        self.assertEqual(measures['spend_usd']['development']['coverage'], wp.COMPLETE)
        self.assertEqual(measures['estimate_usd']['development']['coverage'], wp.PARTIAL)
        self.assertIn('reported no estimate', measures['estimate_usd']['development']['reason'])

    def test_without_completed_task_plan_or_known_phase_the_components_are_null(self):
        self.make_run(self.ticket, ledger=LEDGER)
        self.writer.append({'schema_version': '1.0', 'tasks': {
            t: {'status': 'PENDING', 'attempts': 0} for t in 'ABC'}}, 'TASK_RESET')
        projection = self.snap()['projection']
        self.assertIn('no completed task', projection['measures']['spend_usd']['development']['reason'])
        self.assertEqual(projection['coverage'], wp.PARTIAL)
        (self.ticket / '02-design/plan.json').unlink()
        (self.ticket / '03-dev/tasks/task-journal.jsonl').unlink()
        (self.ticket / 'state.json').write_text(json.dumps({'status': 'X', 'current_phase': 'unknown', 'ledger': []}))
        projection = self.snap()['projection']
        self.assert_parts(projection)
        self.assertEqual((projection['coverage'], projection['tasks_remaining'], projection['gates_remaining']),
                         (wp.NONE, None, None))
        for measure in projection['measures'].values():
            self.assertEqual(measure['central']['coverage'], wp.PARTIAL)  # The spend so far is known: zero.
            self.assertIn('no task plan', measure['development']['reason'])
            self.assertIn('not a known phase', measure['gates']['reason'])

    def test_unverifiable_limits_leave_budget_and_scenario_null(self):
        self.run_with(LEDGER, policy())
        (self.ticket / 'limits/extension-pending.json').write_text('{}')
        result = self.snap()
        self.assertEqual(result['read']['status'], ws.COHERENT)
        self.assertEqual(result['projection']['limits']['status'], 'unavailable')
        self.assertIn('pending', result['projection']['limits']['reason'])
        self.assertIsNone(result['projection']['measures']['spend_usd']['budget'])
        (self.ticket / 'limits/extension-pending.json').unlink()
        envelope = json.loads((self.ticket / 'limits/policy.json').read_text())
        envelope['policy']['metered_usd'] = 1000
        (self.ticket / 'limits/policy.json').write_text(json.dumps(envelope))
        limits = self.snap()['projection']['limits']
        self.assertEqual((limits['status'], limits['invocations']), ('unavailable', None))
        self.assertIn('does not match', limits['reason'])

    def test_limits_written_during_collection_are_retried(self):
        store = self.run_with(LEDGER, policy())

        def fault(point, attempt):
            if attempt == 1:
                store.finish(store.reserve('03-dev', '03-dev', 'claude'), 'returned',
                             {'metered_usd': None, 'cli_estimate_usd': 0.3})
        result = self.snap(fault=fault)
        self.assertEqual((result['read']['status'], result['read']['attempts']), (ws.COHERENT, 2))
        self.assertEqual(result['projection']['limits']['invocations']['used'], 3)


class WarningsTest(ProjectionCase):
    def test_imminent_exhaustion_warns_without_effects_on_the_run(self):
        store = self.run_with(LEDGER, policy(invocations=3, metered_usd=0.8))
        before = tree(self.repo)
        limits_before = store._load(repair=False)
        time.sleep(0.01)  # A write after this point would move an mtime.
        result = self.snap()
        projection = result['projection']
        codes = {(w['code'], w['measure']) for w in projection['warnings']}
        self.assertEqual(codes, {('BUDGET_NEARLY_EXHAUSTED', 'spend_usd'), ('FORECAST_OVER_CAP', 'spend_usd'),
                                 ('INVOCATIONS_NEARLY_EXHAUSTED', 'invocations')})
        nearly = next(w for w in projection['warnings'] if w['code'] == 'BUDGET_NEARLY_EXHAUSTED')
        self.assertEqual((nearly['remaining'], nearly['one_call']), (0.2, 0.3))
        self.assertTrue(all(w['effect'] == 'none' for w in projection['warnings']))
        self.assertTrue(projection['measures']['spend_usd']['budget']['over_cap'])
        # No effect: no file written, the frozen budget and the run's status are unchanged, and
        # the engine still admits the next call the limits allow.
        self.assertEqual(tree(self.repo), before)
        self.assertEqual(store._load(repair=False), limits_before)
        self.assertEqual(result['run']['status'], 'IN_PROGRESS')
        self.assertEqual(json.loads((self.ticket / 'state.json').read_text())['status'], 'IN_PROGRESS')
        self.assertIsNotNone(store.require_capacity('03-dev', '03-dev', 'claude'))

    def test_a_projection_is_pure(self):
        run = {'status': 'IN_PROGRESS', 'current_phase': '03-dev'}
        dag = {'nodes': [{'task_id': 'A', 'status': 'COMPLETED'}, {'task_id': 'B', 'status': 'PENDING'}]}
        spent = {m: ws.metric(0.6, ws.COMPLETE, 'ledger', 'USD') for m in ('spend_usd', 'estimate_usd')}
        limits = {'status': 'active', 'reason': None, 'policy': policy(invocations=2, metered_usd=0.7),
                  'state': {'invocations': {'x': {'usage': {'metered_usd': 0.6, 'cli_estimate_usd': None},
                                                  'status': 'returned', 'engine': 'openrouter'}}}}
        arguments = json.dumps([run, LEDGER, dag, limits, spent], sort_keys=True)
        first = wp.project(run, LEDGER, dag, limits, spent)
        self.assertEqual(json.dumps([run, LEDGER, dag, limits, spent], sort_keys=True), arguments)
        self.assertEqual(wp.project(run, LEDGER, dag, limits, spent), first)
        self.assertIn('INVOCATIONS_NEARLY_EXHAUSTED', {w['code'] for w in first['warnings']})

    def test_gates_match_the_pipeline(self):
        import orchestrator
        self.assertEqual(wp.PHASE_ORDER, tuple(orchestrator.SSDLCOrchestrator.PHASE_ORDER))


if __name__ == '__main__':
    unittest.main()
