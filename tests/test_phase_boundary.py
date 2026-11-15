"""Native --until-phase stop at the 03-dev boundary and resume from 04-review; no provider calls."""
import json
import os
from pathlib import Path
from unittest.mock import patch

import orchestrator as o
import ssdlc_supervisor as s
from orchestrator import SSDLCOrchestrator, TaskError, git
from replay.repository_fixtures import RepositoryTests
from test_supervisor_core import CoreCase
import test_supervisor_cancel_resume as supervised
import test_task_bootstrap as bootstrap_tests


def generate(prompt, **kwargs):
    (kwargs['cwd'] / 'app.py').write_text('value = 1\n')
    return 'Done', 0


def verify_rejecting_candidate(argv, cwd, timeout, **kwargs):
    # The clean baseline passes; the developer candidate fails its checks.
    if (Path(cwd) / 'app.py').read_text() == 'value = 1\n':
        return 'Ran 1 test\nFAILED (failures=1)', '', 1
    return 'Ran 1 test\nOK', '', 0


@patch('orchestrator.EngineRunner._should_spawn_role_container', return_value=True)
class PhaseBoundaryTests(RepositoryTests):
    make_orchestrator = bootstrap_tests.OrchestratorBootstrapTests.make_orchestrator
    seed_inputs = bootstrap_tests.OrchestratorBootstrapTests.seed_inputs

    def setUp(self):
        super().setUp()
        for target in (patch.object(SSDLCOrchestrator, '_verification_environment', return_value={
                'mode': 'role_container', 'platform': 'fixture',
                'image_id': 'sha256:' + '1' * 64, 'reuse_eligible': True}),
                patch.dict(os.environ, {'SSDLC_ENABLE_TASK_BREAKDOWN': 'true', 'SSDLC_EXECUTION_MODE': 'local'}),
                patch('orchestrator.SquadDispatcher.dispatch_role', return_value=('codex', 'fake', 'low', 'test'))):
            target.start()
            self.addCleanup(target.stop)
        self.orch = self.make_orchestrator()
        self.seed_inputs(self.orch)
        self.orch.state.update(risk_tier='S', goal='Scoped goal')
        self.orch._save_state()

    def gates(self, orch, review=True):
        return (patch.object(orch, 'run_phase_review', return_value=(review, 'Review result')),
                patch.object(orch, 'run_phase_qa', return_value=(True, 'QA passed')),
                patch.object(orch, 'run_phase_appsec', return_value=(True, 'AppSec passed')))

    def pause(self):
        review, qa, appsec = self.gates(self.orch)
        with patch('orchestrator.EngineRunner.run_codex', side_effect=generate), \
                patch('orchestrator.EngineRunner.run_sandboxed_cmd', return_value=('Ran 1 test\nOK', '', 0)), \
                review as review, qa as qa, appsec as appsec:
            result = self.orch.run_pipeline('Scoped goal', from_phase='03-dev', until_phase='03-dev')
            review.assert_not_called()
            qa.assert_not_called()
            appsec.assert_not_called()
        return result

    def test_stop_after_dev_with_all_tasks_completed(self, _runtime):
        result = self.pause()
        state = json.loads(self.orch.state_file.read_text())
        head = git(self.orch.worktree_dir, 'rev-parse', 'HEAD')
        self.assertEqual(result, self.orch.state_file)
        self.assertEqual(state['status'], o.PAUSED_AT_PHASE_BOUNDARY)
        self.assertEqual(state['current_phase'], '03-dev')
        self.assertNotIn('failure', state)
        boundary = state['phase_boundary']
        self.assertEqual((boundary['phase'], boundary['next_phase']), ('03-dev', '04-review'))
        self.assertEqual(boundary['verified_head'], head)
        self.assertEqual(state['task_verified_head'], head)
        self.assertNotEqual(head, self.base)
        self.assertTrue(boundary['worktree_clean'])
        self.assertEqual(git(self.orch.worktree_dir, 'status', '--porcelain'), '')
        self.assertEqual(boundary['completed_tasks'], ['T1'])
        self.assertEqual(set(boundary['usage']), {'ledger_entries', 'reported_cost_usd', 'unknown_cost_rows', 'accounted_usd'})
        self.assertEqual(boundary['usage']['ledger_entries'], len(state['ledger']))
        # No readiness, sign-off, failure gate or evidence pack.
        self.assertFalse((self.orch.ssdlc_root / 'EVIDENCE_PACK.md').exists())
        self.assertTrue(all(g['passed'] for g in state['gate_history']))
        self.assertFalse(any(g['gate'] == 'SUPERVISOR_SIGNOFF' for g in state['gate_history']))
        with self.assertRaisesRegex(TaskError, 'not passed the global gates'):
            self.orch.approve_and_merge()
        self.assertEqual(o.execution_compatibility(self.repo, 'TKT-1', 'approve')['code'], 'approval_not_ready')

    def failed_task_outcome(self, until_phase):
        review, qa, appsec = self.gates(self.orch)
        with patch.dict(os.environ, {'SSDLC_MAX_DEV_RETRIES': '1'}), \
                patch('orchestrator.EngineRunner.run_codex', side_effect=generate), \
                patch('orchestrator.EngineRunner.run_sandboxed_cmd', side_effect=verify_rejecting_candidate), \
                review as review, qa as qa, appsec as appsec:
            with self.assertRaises(TaskError) as raised:
                self.orch.run_pipeline('Scoped goal', from_phase='03-dev', until_phase=until_phase)
            review.assert_not_called()
        state = json.loads(self.orch.state_file.read_text())
        self.assertTrue((self.orch.ssdlc_root / 'EVIDENCE_PACK.md').exists())
        return (type(raised.exception), state['status'], state['current_phase'], state['failure'],
                [(g['gate'], g['passed'], g['details']) for g in state['gate_history']], 'phase_boundary' in state)

    def test_failed_task_keeps_the_ordinary_failure_outcome(self, _runtime):
        bounded = self.failed_task_outcome('03-dev')
        self.assertEqual(bounded[3]['phase'], '03-dev')
        self.assertEqual(bounded[4][-1][:2], ('TASK_EXECUTION_GATE', False))
        self.assertFalse(bounded[5])
        with self.assertRaisesRegex(TaskError, 'paused at the preceding phase boundary'):
            o._resume_run_refusal(self.orch.ssdlc_root, json.loads(self.orch.state_file.read_text()), '04-review')
        # The same wrapper records the same outcome as the unbounded path.
        self.doCleanups()
        self.setUp()
        self.assertEqual(self.failed_task_outcome(None), bounded)

    def test_resume_from_review_runs_global_gates_without_development(self, _runtime):
        self.assertFalse(o.resume_compatibility(self.repo, 'TKT-1', 'run', '04-review')['ok'])
        self.pause()
        # The supervisor's read-only admission uses the same refusal rules.
        self.assertTrue(o.resume_compatibility(self.repo, 'TKT-1', 'run', '04-review')['ok'])
        orch = self.make_orchestrator()
        review, qa, appsec = self.gates(orch)
        with patch.object(orch, 'run_phase_dev', side_effect=AssertionError('development repeated')), \
                patch('orchestrator.EngineRunner.run_codex', side_effect=AssertionError('provider called')), \
                review as review, qa as qa, appsec as appsec:
            evidence = orch.run_pipeline('Scoped goal', from_phase='04-review')
            review.assert_called_once_with(attempt=1)
            qa.assert_called_once()
            appsec.assert_called_once_with(attempt=1)
        self.assertTrue(evidence.exists())
        self.assertEqual(orch.state['status'], 'AWAITING_SUPERVISOR_SIGNOFF')
        self.assertEqual(orch.state['phase_boundary']['phase'], '03-dev')

    def test_resume_from_review_reproves_the_boundary(self, _runtime):
        self.pause()
        (self.orch.worktree_dir / 'app.py').write_text('value = 2\n')
        orch = self.make_orchestrator()
        review, qa, appsec = self.gates(orch)
        with review as review, qa, appsec:
            with self.assertRaisesRegex(TaskError, 'clean verified task checkpoint'):
                orch.run_pipeline('Scoped goal', from_phase='04-review')
            review.assert_not_called()
        self.assertEqual(orch.state['failure']['phase'], '03-dev')
        self.assertEqual(orch.state['gate_history'][-1]['gate'], 'TASK_EXECUTION_GATE')
        self.assertNotEqual(orch.state['status'], o.PAUSED_AT_PHASE_BOUNDARY)

    def test_resume_from_review_requires_a_pause_and_a_valid_range(self, _runtime):
        with self.assertRaisesRegex(TaskError, 'paused at the preceding phase boundary'):
            o._resume_run_refusal(self.orch.ssdlc_root, self.orch.state, '04-review')
        for start, until in [('04-review', '03-dev'), ('03-dev', '04-review'), ('03-dev', '05-qa')]:
            with self.subTest(start=start, until=until), patch.object(self.orch, '_run_pipeline', side_effect=AssertionError):
                with self.assertRaisesRegex(TaskError, 'Unsupported phase range'):
                    self.orch.run_pipeline('Scoped goal', from_phase=start, until_phase=until)
        self.assertNotIn('failure', self.orch.state)


class SupervisedPhaseLimitTests(CoreCase):
    baseline = supervised.TestResumeAndArgv.baseline
    terminal = supervised.TestResumeAndArgv.terminal

    def bounded(self):
        self.baseline()
        self.args.update(from_phase='03-dev', until_phase='03-dev')
        return self.terminal()

    def test_limit_is_authorized_and_passed_to_the_controller(self):
        store = self.bounded()
        record = store.read()
        self.assertEqual(record['authorized']['until_phase'], '03-dev')
        self.assertEqual(record['fingerprints']['inputs_sha256'], s.digest(json.dumps(record['authorized'], sort_keys=True)))
        argv = s.build_argv('orchestrator.run', self.args)
        self.assertEqual(argv[argv.index('--until-phase') + 1], '03-dev')
        self.assertNotIn('--until-phase', s.build_argv('orchestrator.run', dict(self.args, until_phase=None)))
        for bad in [dict(until_phase='05-qa'), dict(from_phase='04-review'), dict(entrypoint='plan')]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                s.validate_args(dict(self.args, **bad))
        # Only a resume of the recorded run may start after the boundary.
        with patch.object(s, 'lock', side_effect=AssertionError), self.assertRaises(ValueError):
            s.start(dict(self.args, from_phase='04-review', until_phase=None))

    def test_resume_cannot_exceed_the_limit_silently(self):
        store = self.bounded()
        original = store.read()
        with patch.object(s, 'spawn', side_effect=AssertionError):
            out = s.resume(dict(self.args, entrypoint='run', until_phase=None), self.clock)
        self.assertEqual(out['status_reason'], 'fingerprint_mismatch')
        self.assertEqual(store.read(), original)
        with patch.object(s, 'start', return_value={'status': 'running'}) as launch:
            s.resume(dict(self.args, entrypoint='run'), self.clock)
        self.assertEqual(launch.call_args.args[0]['until_phase'], '03-dev')
        self.assertNotIn('_reauthorization', launch.call_args.args[0])
        with patch.object(s, 'start', return_value={'status': 'running'}) as launch:
            s.resume(dict(self.args, entrypoint='run', until_phase=None, reauthorize_fingerprints=True), self.clock)
        self.assertEqual(launch.call_args.args[0]['_reauthorization'][0]['fields'], ['until_phase'])

    def test_resume_after_the_boundary_is_the_explicit_next_phase(self):
        old = {'authorized': dict(from_phase='03-dev', until_phase='03-dev'), 'fingerprints': {}}
        new = {'authorized': dict(from_phase='04-review'), 'fingerprints': {}}
        self.assertEqual(s.fingerprint_changes(old, new), [])
        self.assertEqual([c['field'] for c in s.fingerprint_changes(old, dict(new, authorized=dict(from_phase='03-dev')))],
                         ['until_phase'])

    def test_launch_payload_cannot_move_the_authorized_range(self):
        store = self.bounded()
        d = store.read()
        a = s.validate_args(self.args)
        self.assertFalse(s.phase_range_changed(a, d))
        self.assertTrue(s.phase_range_changed(dict(a, until_phase=None), d))
        self.assertTrue(s.phase_range_changed(dict(a, from_phase='00-recon'), d))
        tampered = dict(d, authorized=dict(d['authorized'], until_phase='05-qa'))
        with self.assertRaises(s.SupervisorIntegrityError):
            s.validate_record(tampered)

    def test_completion_requires_the_authorized_boundary(self):
        store = self.bounded()
        base = 'b' * 40  # the fixture repository has no commit
        d = dict(store.read(), base_commit_at_launch=base)
        ready = dict(base_commit=base, status='AWAITING_SUPERVISOR_SIGNOFF', task_verified_head='h')
        wrong = dict(ready, status=o.PAUSED_AT_PHASE_BOUNDARY, phase_boundary=dict(phase='04-review', verified_head='h'))
        paused = dict(wrong, phase_boundary=dict(phase='03-dev', verified_head='h'))
        (s.pinned_root(self.repo, 'T-1', d, observe=True) / 'worktree').mkdir()
        def git_read(repo, *args):
            return base if Path(repo) == self.repo else 'h' if args[0] == 'rev-parse' else ''
        with patch.object(s, 'git_read', side_effect=git_read):
            self.assertFalse(s.completion(store, d, ready))
            self.assertFalse(s.completion(store, d, wrong))
            self.assertTrue(s.completion(store, d, paused))
