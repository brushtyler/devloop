"""Behavioral regressions for the independent PR 71 review."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import orchestrator as o
import ssdlc_bootstrap as bootstrap
import ssdlc_supervisor as supervisor
from ssdlc_tasks import DevelopmentBlocked, TaskError, TaskJournal, TaskScheduler, redact_sensitive
import test_bootstrap as bootstrap_fixtures
import test_dev_repair as repair_fixtures
import test_unified_execution as execution_fixtures
import test_legacy_compat as legacy_fixtures
import test_corrective_creation as creation_fixtures
import test_corrective_executor as executor_fixtures
import ssdlc_corrective as corrective


class ReviewFixTests(unittest.TestCase):
    def fixture(self, cls):
        f = cls()
        self.addCleanup(f.doCleanups)
        f.setUp()
        return f

    def test_long_ordinary_retry_logs_keep_tail_and_all_three_dispatches(self):
        f = self.fixture(repair_fixtures.DevelopmentRepairTests)
        def verify(argv, cwd, timeout):
            if argv == ['task-check'] and len(f.calls) in (1, 2):
                return 'diagnostic line\n' * 1000 + 'AssertionError: actual final failure', '', 1
            return 'successful check output\n' * 1000 + 'Ran 1 test\nOK', '', 0
        engine = f.scheduler(f.developer, verify, max_attempts=3)
        result = engine.run(f.plan['tasks'])
        self.assertEqual(result['tasks']['T1']['status'], 'COMPLETED')
        self.assertEqual([n for n, _ in f.calls], [1, 2, 3])
        for _, prompt in f.calls[1:]:
            self.assertIn('AssertionError: actual final failure', prompt)
            repair = json.loads(prompt.split('\nREPAIR CONTEXT:\n')[1])
            excerpts = [c.get('log_excerpt', '') for row in repair['history'] for c in row['checks']]
            self.assertNotIn('successful check output', ''.join(excerpts))
            self.assertTrue(any(excerpts))
            excerpts += [c.get('log_excerpt', '') for c in repair['checks']]
            self.assertLessEqual(sum(map(len, excerpts)), 24000)

    def test_clean_native_worktree_before_journal_initialization_can_resume(self):
        f = self.fixture(execution_fixtures.UnifiedExecutionTests)
        orch = f.make_orchestrator()
        f.seed_inputs(orch)
        orch._ensure_worktree()
        TaskJournal(orch.dir_dev / 'tasks')
        (orch.dir_dev / 'tasks/scheduler.lock').touch()
        before = {str(p): p.read_bytes() for p in orch.ssdlc_root.rglob('*') if p.is_file()}
        result = o.execution_compatibility(f.repo, orch.ticket_id, 'run')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['cls'], 'initializing_task')
        self.assertTrue(o.resume_compatibility(f.repo, orch.ticket_id, 'run', '03-dev')['ok'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in orch.ssdlc_root.rglob('*') if p.is_file()})
        (orch.worktree_dir / 'app.py').write_text('dirty')
        result = o.execution_compatibility(f.repo, orch.ticket_id, 'run')
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'initialization_not_ready')

    def test_pre_isolation_failed_task_cannot_dispatch_or_consume_attempt(self):
        f = self.fixture(execution_fixtures.UnifiedExecutionTests)
        orch = f.make_orchestrator()
        f.seed_inputs(orch)
        wt = orch._ensure_worktree()
        orch._ensure_controller_contract()
        def develop(*args):
            (wt / 'app.py').write_text('value = 1\n')
            return dict(output='TASK_DEV_STATUS: COMPLETE', exit_code=0)
        old = TaskScheduler(wt, orch.dir_dev / 'tasks', f.plan, f.quality,
                            o.build_task_context(orch.ssdlc_root), develop,
                            lambda *args: ('Ran 1 test', '', int((wt / 'app.py').read_text() == 'value = 1\n')),
                            max_attempts=1)
        with self.assertRaises(TaskError):
            old.run(f.plan['tasks'])
        before = old.journal.path.read_bytes()
        with patch.object(o.EngineRunner, 'run_codex', side_effect=AssertionError('dispatch')), \
             patch.object(o.EngineRunner, 'run_sandboxed_cmd', return_value=('Ran 1 test', '', 0)):
            with self.assertRaisesRegex(DevelopmentBlocked, 'Pre-isolation'):
                orch._make_task_scheduler()
        self.assertEqual(before, old.journal.path.read_bytes())
        self.assertEqual(old.journal.peek()['tasks']['T1']['attempts'], 1)

    def test_approved_instructions_keep_bearer_prose_paths_and_task_ids(self):
        f = self.fixture(repair_fixtures.DevelopmentRepairTests)
        engine = f.scheduler(f.developer, f.verifier)
        engine.state = {'tasks': {}}
        prose = 'Reject requests whose Bearer token is expired; see src/sk-utils.py and task T-sk-1'
        engine.context['instructions'] = prose
        prompt = engine._developer_prompt(f.plan['tasks'][0], {})
        self.assertIn(prose, prompt)
        self.assertEqual(redact_sensitive(prose), prose)

    def test_github_app_and_user_tokens_are_redacted_without_environment_copy(self):
        for prefix in ('ghs_', 'gho_', 'github_pat_', 'ghp_'):
            token = prefix + 'x' * 40
            with self.subTest(prefix=prefix):
                self.assertEqual(redact_sensitive('credential: ' + token), 'credential: [REDACTED]')
                self.assertEqual(redact_sensitive('https://' + token + '@example.invalid'),
                                 'https://[REDACTED]@example.invalid')
        key = 'sk-' + 'x1' * 20
        self.assertEqual(redact_sensitive(key), '[REDACTED]')

    def test_initial_commit_with_unittest_default_filename_is_not_greenfield(self):
        for name in ('tests/tests.py', 'app/tests.py', 'spec/model_spec.rb', 'src/FooTest.java'):
            f = self.fixture(bootstrap_fixtures.BootstrapTests)
            path = f.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('import unittest\nclass T(unittest.TestCase):\n def test_x(self): pass\n')
            f.run_git('add', '.')
            f.run_git('commit', '--amend', '--no-edit', '-q')
            base = o.git(f.repo, 'rev-parse', 'HEAD')
            with self.subTest(name=name), self.assertRaises(bootstrap.BootstrapError) as error:
                bootstrap.decide(f.repo, base, f.profile)
            self.assertEqual(error.exception.code, 'existing_tests')

    def test_pytest_custom_discovery_pattern_is_not_greenfield(self):
        f = self.fixture(bootstrap_fixtures.BootstrapTests)
        (f.repo / 'pytest.ini').write_text('[pytest]\npython_files = check_*.py\n')
        (f.repo / 'check_behavior.py').write_text('def test_existing(): pass\n')
        f.run_git('add', '.')
        f.run_git('commit', '--amend', '--no-edit', '-q')
        with self.assertRaises(bootstrap.BootstrapError) as error:
            bootstrap.decide(f.repo, o.git(f.repo, 'rev-parse', 'HEAD'), f.profile)
        self.assertEqual(error.exception.code, 'existing_tests')

    def test_shallow_history_is_not_an_initial_commit(self):
        f = self.fixture(bootstrap_fixtures.BootstrapTests)
        (f.repo / 'app.py').write_text('value = 1\n')
        f.run_git('add', '.')
        f.run_git('commit', '-qm', 'second')
        with tempfile.TemporaryDirectory() as temp:
            clone = Path(temp) / 'clone'
            subprocess.run(['git', 'clone', '-q', '--depth', '1', f.repo.as_uri(), str(clone)], check=True)
            with self.assertRaises(bootstrap.BootstrapError) as error:
                bootstrap.decide(clone, o.git(clone, 'rev-parse', 'HEAD'), f.profile)
            self.assertEqual(error.exception.code, 'not_initial_commit')

    def test_malformed_discovery_returns_stable_bootstrap_diagnosis(self):
        for filename, data, reason in [('package.json', b'{', 'profile_invalid'),
                                      ('tests/test_bad.py', b'\xff', 'profile_invalid'),
                                      ('package.json', b'{"scripts":{"lint":"true"}}', 'policy_conflict')]:
            f = self.fixture(bootstrap_fixtures.BootstrapTests)
            path = f.repo / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            f.run_git('add', '.')
            f.run_git('commit', '--amend', '--no-edit', '-q')
            with self.subTest(filename=filename), self.assertRaises(bootstrap.BootstrapError) as error:
                bootstrap.decide(f.repo, o.git(f.repo, 'rev-parse', 'HEAD'), f.profile)
            self.assertEqual(error.exception.code, reason)

    def test_real_unittest_expected_failures_do_not_certify_passing_tests(self):
        command = ['python3', '-c', 'import unittest\nclass T(unittest.TestCase):\n @unittest.expectedFailure\n def test_x(self): self.fail()\nunittest.main()']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(bootstrap.positive_test_count(result.stdout, result.stderr), 0)
        self.assertEqual(bootstrap.positive_test_count('', 'Ran 3 tests\nOK (skipped=1, expected failures=1)'), 1)

    def test_cargo_maven_rspec_and_go_positive_and_empty_summaries(self):
        cases = [('test result: ok. 2 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out', 2),
                 ('test result: ok. 1 passed; 0 failed\ntest result: ok. 0 passed; 0 failed', 1),
                 ('[INFO] Tests run: 2, Failures: 0, Errors: 0, Skipped: 1', 1),
                 ('Tests run: 2, Failures: 0, Errors: 0, Skipped: 2', 0),
                 ('2 examples, 0 failures, 1 pending', 1),
                 ('1 example, 0 failures, 1 pending', 0),
                 (json.dumps(dict(Action='pass', Package='example', Test='TestX')), 1),
                 (json.dumps(dict(Action='pass', Package='example')), 0)]
        for output, expected in cases:
            with self.subTest(output=output):
                self.assertEqual(bootstrap.positive_test_count(output, ''), expected)

    def test_go_quality_contract_uses_json_without_mutating_user_policy(self):
        f = self.fixture(bootstrap_fixtures.BootstrapTests)
        (f.repo / '.ssdlc/bootstrap-profile.json').unlink()
        policy = dict(schema_version='1.0', contract_revision=1, base_commit=f.base,
                      sources=[], standards=[], limitations=[], exceptions=[], baseline=[],
                      checks=[dict(check_id='CHECK-GO', argv=['go', 'test', './...'], cwd='.',
                                   timeout_sec=5, kind='test', required=True)])
        path = f.repo / '.ssdlc/quality-contract.json'
        path.write_text(json.dumps(policy))
        orch = f.contract_orchestrator()
        orch.check_budget_circuit_breaker = lambda: None
        with patch.object(o.EngineRunner, 'run_sandboxed_cmd', return_value=(json.dumps(
                dict(Action='pass', Package='example', Test='TestX')), '', 0)) as run:
            contract = orch._ensure_quality_contract()
        self.assertEqual(run.call_args.args[0], ['go', 'test', '-json', './...'])
        self.assertEqual(contract['checks'][0]['argv'], ['go', 'test', '-json', './...'])
        self.assertEqual(json.loads(path.read_text()), policy)

    def test_historical_status_through_repository_alias_remains_read_only(self):
        f = self.fixture(legacy_fixtures.CompatibilityTests)
        with tempfile.TemporaryDirectory() as temp:
            alias = Path(temp) / 'repo'
            alias.symlink_to(f.repo, target_is_directory=True)
            before = f.snapshot()
            self.assertEqual(supervisor.status(alias, 'OLD')['status'], 'historical')
            self.assertEqual(before, f.snapshot())

    def test_missing_final_gate_provenance_is_approval_not_ready_not_legacy(self):
        f = self.fixture(legacy_fixtures.CompatibilityTests)
        f.seal()
        before = f.snapshot()
        result = o.execution_compatibility(f.repo, 'OLD', 'approve')
        self.assertEqual(result['code'], 'approval_not_ready')
        count = o.LEGACY_COUNTERS['legacy_refusals']
        with patch.object(o, 'SSDLCOrchestrator', side_effect=AssertionError('constructor')) as ctor, \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            ctor.PHASE_ORDER = ['00-recon', '03-dev']
            self.assertEqual(o._foreground_main(['approve', 'OLD', '--repo', str(f.repo)]), 25)
        self.assertIn('approval_not_ready', stderr.getvalue())
        self.assertNotIn('legacy run', stderr.getvalue())
        self.assertEqual(o.LEGACY_COUNTERS['legacy_refusals'], count)
        self.assertEqual(before, f.snapshot())

    def test_creation_budget_stop_retains_budget_diagnosis(self):
        f = self.fixture(creation_fixtures.CreationTests)
        preview = f.preview()
        with patch.object(f.orch, '_corrective_accounted_spend', return_value=1000), \
             patch.object(f.orch, '_corrective_stage') as stage:
            with self.assertRaisesRegex(TaskError, 'Circuit Breaker') as error:
                f.execute(preview)
        self.assertNotIsInstance(error.exception, corrective.CorrectiveError)
        self.assertEqual(stage.call_args.kwargs['reason'], 'budget_interrupted')
        self.assertNotIn('evidence_mutated', str(error.exception))

    def test_recovered_gate_attempt_above_ceiling_reports_exhaustion(self):
        f = self.fixture(executor_fixtures.ExecutorTests)
        with f.fakes(), patch.object(f.orch, '_reconcile_open_txn', return_value={'next_gate_attempt': 2}), \
             patch.dict(os.environ, {'SSDLC_MAX_DEV_RETRIES': '1'}), \
             patch.object(f.orch, 'run_phase_dev', side_effect=AssertionError('dispatch')):
            with self.assertRaises(corrective.CorrectiveError) as error:
                f.orch.run_pipeline('fix', from_phase='03-dev')
        self.assertEqual(error.exception.code, 'gate_attempts_exhausted')

    def test_initialization_recovery_reaches_an_isolated_checkpoint(self):
        f = self.fixture(execution_fixtures.UnifiedExecutionTests)
        orch = f.make_orchestrator()
        f.seed_inputs(orch)
        orch._ensure_worktree()
        TaskJournal(orch.dir_dev / 'tasks')
        self.assertEqual(o.startup_preflight(f.repo, orch.ticket_id, 'run')['reason'],
                         'resume_from_03_dev_required')
        self.assertTrue(o.startup_preflight(f.repo, orch.ticket_id, 'run', {'from_phase': '03-dev'})['ok'])
        calls = []
        def develop(task, attempt, prompt, workspace):
            calls.append(attempt)
            (workspace / 'app.py').write_text('value = 1\n')
            return dict(output='TASK_DEV_STATUS: COMPLETE', exit_code=0, quiescent=True)
        with patch.object(o.EngineRunner, '_should_spawn_role_container', return_value=True), \
             patch.object(orch, '_verification_environment', return_value={'runtime': 'fixture'}), \
             patch.object(o.EngineRunner, 'run_sandboxed_cmd', return_value=('Ran 1 test\nOK', '', 0)):
            engine, ordered = orch._make_task_scheduler()
            engine.developer = develop
            result = engine.run(ordered)
        self.assertTrue(result['candidate_isolation'])
        self.assertEqual(result['tasks']['T1']['status'], 'COMPLETED')
        self.assertEqual(calls, [1])
