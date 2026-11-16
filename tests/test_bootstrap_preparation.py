"""Greenfield bootstrap: resumable preparation, single first-attempt reservation (#72).

Pre-development authority (``bootstrap-preparation.json``) is distinct from the
first-checkpoint exemption. These tests inject faults before/after every
persistence boundary, race two reservations and drive real CLI/supervisor
entry points with offline fakes.
"""
import contextlib
import copy
import inspect
import io
import json
import os
import shutil
import threading
import unittest
from unittest.mock import patch

import orchestrator as o
import ssdlc_bootstrap as bootstrap
import ssdlc_supervisor as supervisor
from ssdlc_tasks import TaskJournal, atomic_json, digest, git
import test_bootstrap as fixtures
import test_unified_integration as integration
from test_full_checkpoint_review import framed


class Crash(BaseException):
    """A process stop: not handled by the pipeline's TaskError recovery."""


def crash_at(point):
    def fault(name):
        if name == point:
            raise Crash(name)
    return patch.object(bootstrap, 'fault', side_effect=fault)


class PreparationTests(unittest.TestCase):
    setUp = fixtures.BootstrapTests.setUp
    run_git = fixtures.BootstrapTests.run_git
    write_profile = fixtures.BootstrapTests.write_profile
    snapshot = fixtures.BootstrapTests.snapshot

    @property
    def root(self):
        return self.repo / '.ssdlc/T'

    def orchestrator(self, state=None):
        orch = o.SSDLCOrchestrator.__new__(o.SSDLCOrchestrator)
        orch.repo_root, orch.ticket_id, orch.ssdlc_root = self.repo, 'T', self.root
        orch.dir_recon = self.root / '00-recon'
        orch.dir_recon.mkdir(parents=True, exist_ok=True)
        orch.state = state if state is not None else dict(base_commit=self.base, artifacts={})

        def record(name, path):
            orch.state['artifacts'][name] = {'path': name, 'sha256': o.sha256_file(path)}
        orch.record_artifact = record
        return orch

    def sealed(self):
        orch = self.orchestrator()
        contract = orch._ensure_quality_contract()
        return orch, contract

    def assert_resumable(self, orch, contract):
        bootstrap.require_unproven_run_is_current(self.root)
        record = bootstrap.read_preparation(self.root)
        self.assertEqual(record['status'], 'PREPARING')
        self.assertEqual(record['contract_sha256'], digest(contract))
        self.assertEqual(record['binding'], contract['bootstrap'])
        orch._verify_bootstrap_preparation()
        self.assertFalse(bootstrap.development_started(self.root))

    def test_record_precedes_and_binds_contract(self):
        order = []
        real = o.atomic_json
        with patch.object(o, 'atomic_json', side_effect=lambda path, value: (order.append(path.name), real(path, value))), \
             patch.object(bootstrap, 'atomic_json', side_effect=lambda path, value: (order.append(path.name), real(path, value))):
            orch, contract = self.sealed()
        self.assertEqual(order, ['bootstrap-preparation.json', 'quality-contract.json'])
        record = bootstrap.read_preparation(self.root)
        self.assertEqual(set(record), bootstrap.PREPARATION_KEYS)
        self.assertEqual((record['format'], record['ticket'], record['limits_policy_sha256']),
                         (bootstrap.PREPARATION_FORMAT, 'T', None))
        self.assertEqual(record['controller'], o.controller_snapshot(o.Path(o.__file__).resolve().parent))
        self.assert_resumable(orch, contract)
        # The sealed contract format is unchanged: earlier controllers read the
        # same contract, ignore the record and keep refusing an unproven run.
        self.assertNotIn('preparation', json.dumps(contract))

    def test_interrupted_sealing_rolls_forward_without_regenerating_authority(self):
        for point in ('before_preparation_record', 'after_preparation_record',
                      'after_contract', 'after_contract_registration'):
            with self.subTest(point=point):
                shutil.rmtree(self.root, ignore_errors=True)
                orch = self.orchestrator()
                with crash_at(point), self.assertRaises(Crash):
                    orch._ensure_quality_contract()
                persisted = copy.deepcopy(orch.state)
                path = self.root / bootstrap.PREPARATION
                record = path.read_bytes() if path.exists() else None
                retry = self.orchestrator(persisted)
                contract = retry._ensure_quality_contract()
                self.assertIn('00-recon/quality-contract.json', retry.state['artifacts'])
                if record is not None:
                    self.assertEqual(path.read_bytes(), record)
                self.assert_resumable(retry, contract)

    def test_interrupted_sealing_refuses_mutated_inputs(self):
        pytest_profile = dict(self.profile, profile_id='python-pytest')
        # Preparation written, contract not: a different binding is refused.
        orch = self.orchestrator()
        with crash_at('after_preparation_record'), self.assertRaises(Crash):
            orch._ensure_quality_contract()
        self.write_profile(pytest_profile)
        with self.assertRaises(bootstrap.BootstrapError) as raised:
            self.orchestrator(copy.deepcopy(orch.state))._ensure_quality_contract()
        self.assertEqual(raised.exception.code, 'inputs_changed')
        self.assertFalse((self.root / '00-recon/quality-contract.json').exists())
        # A removed profile cannot turn the preparation into a strict run.
        (self.repo / '.ssdlc/bootstrap-profile.json').unlink()
        with self.assertRaises(bootstrap.BootstrapError) as raised:
            self.orchestrator(copy.deepcopy(orch.state))._ensure_quality_contract()
        self.assertEqual(raised.exception.code, 'inputs_changed')
        self.assertFalse((self.root / '00-recon/quality-contract.json').exists())
        # Contract written, unregistered: rolled forward, then full verification refuses.
        shutil.rmtree(self.root)
        self.write_profile(self.profile)
        orch = self.orchestrator()
        with crash_at('after_contract'), self.assertRaises(Crash):
            orch._ensure_quality_contract()
        self.write_profile(pytest_profile)
        retry = self.orchestrator(copy.deepcopy(orch.state))
        retry._ensure_quality_contract()
        with self.assertRaises(bootstrap.BootstrapError) as raised:
            retry._verify_bootstrap_preparation()
        self.assertEqual(raised.exception.code, 'inputs_changed')

    def test_verification_refuses_each_drift_and_resets_nothing(self):
        orch, contract = self.sealed()
        requirements = self.root / '01-po/requirements.md'
        requirements.parent.mkdir()
        requirements.write_text('AC-1: Behavior\n')
        orch.record_artifact('01-po/requirements.md', requirements)
        record = (self.root / bootstrap.PREPARATION).read_bytes()

        def tamper_requirements():
            requirements.write_text('AC-1: Other\n')
            return lambda: requirements.write_text('AC-1: Behavior\n')

        def dirty_repo():
            (self.repo / 'new.py').write_text('x=1')
            return (self.repo / 'new.py').unlink

        def changed_profile():
            self.write_profile(dict(self.profile, profile_id='python-pytest'))
            return lambda: self.write_profile(self.profile)

        def foreign_ticket():
            orch.ticket_id = 'U'
            return lambda: setattr(orch, 'ticket_id', 'T')

        def patched(target, name, **kwargs):
            def apply():
                patcher = patch.object(target, name, **kwargs)
                patcher.start()
                return patcher.stop
            return apply

        def changed_policy():
            patcher = patch.dict(bootstrap.PROFILES, {'python-unittest': dict(
                bootstrap.PROFILES['python-unittest'], argv=['python3', '-m', 'unittest'])})
            patcher.start()
            return patcher.stop

        def rewrite_record(value):
            def apply():
                (self.root / bootstrap.PREPARATION).write_text(value)
                return lambda: (self.root / bootstrap.PREPARATION).write_bytes(record)
            return apply

        def historical():
            (self.root / bootstrap.PREPARATION).unlink()
            return lambda: (self.root / bootstrap.PREPARATION).write_bytes(record)

        unknown = dict(json.loads(record), format='bootstrap-preparation-v2')
        def changed_limits():
            # Controller: live frozen store; preflight: the persisted policy file.
            stop = patched(o.SSDLCOrchestrator, '_limits_policy_digest', return_value='0' * 64)()
            policy = self.root / 'limits/policy.json'
            policy.parent.mkdir()
            policy.write_text(json.dumps({'policy': {'design_rounds': 9}}))
            return lambda: (stop(), shutil.rmtree(policy.parent))

        cases = [
            ('inputs_changed', tamper_requirements), ('dirty_tree', dirty_repo),
            ('inputs_changed', changed_profile), ('preparation_invalid', foreign_ticket),
            ('controller_changed', patched(o, 'controller_snapshot', return_value={'head': '0' * 40})),
            ('limits_changed', changed_limits),
            ('policy_changed', changed_policy),
            ('preparation_invalid', rewrite_record('{')),
            ('preparation_invalid', rewrite_record(json.dumps(unknown))),
            ('interrupted', historical),
        ]
        # The shared CLI/supervisor preflight reads the persisted run state.
        atomic_json(self.root / 'state.json', orch.state)
        checks = (('controller', lambda: orch._verify_bootstrap_preparation()),
                  ('preflight', lambda: o.verify_bootstrap_preparation_readonly(
                      self.repo, self.root, orch.ticket_id)))
        for code, mutate in cases:
            for where, verify in checks:
                with self.subTest(code=code, mutate=mutate.__name__, where=where):
                    undo = mutate()
                    try:
                        with self.assertRaises(bootstrap.BootstrapError) as raised:
                            verify()
                        self.assertEqual(raised.exception.code, code)
                        self.assertFalse(bootstrap.development_started(self.root))
                    finally:
                        undo()
                    verify()
        self.assertEqual((self.root / bootstrap.PREPARATION).read_bytes(), record)
        self.assertEqual(json.loads((self.root / '00-recon/quality-contract.json').read_text()), contract)

    def test_reservation_boundaries_never_renew_the_exemption(self):
        for point, renewable in (('before_reservation', True), ('reservation_created', False),
                                 ('after_reservation', False)):
            with self.subTest(point=point):
                shutil.rmtree(self.root, ignore_errors=True)
                orch, contract = self.sealed()
                with crash_at(point), self.assertRaises(Crash):
                    bootstrap.reserve_first_attempt(self.root, 'T')
                # No worktree and no journal exist in any case.
                self.assertFalse((self.root / 'worktree').exists())
                self.assertFalse((self.root / '03-dev/tasks/task-journal.jsonl').exists())
                if renewable:
                    self.assert_resumable(orch, contract)
                    bootstrap.reserve_first_attempt(self.root, 'T')
                for check in (lambda: bootstrap.require_unproven_run_is_current(self.root),
                              orch._verify_bootstrap_preparation,
                              lambda: bootstrap.reserve_first_attempt(self.root, 'T'),
                              lambda: bootstrap.seal_preparation(self.root, bootstrap.read_preparation(self.root))):
                    with self.assertRaises(bootstrap.BootstrapError) as raised:
                        check()
                    self.assertEqual(raised.exception.code, 'window_spent')

    def test_development_evidence_without_reservation_is_not_resumable(self):
        for name in ('worktree', '03-dev/tasks/task-journal.jsonl', '03-dev/implementation.diff'):
            with self.subTest(name=name):
                shutil.rmtree(self.root, ignore_errors=True)
                self.sealed()
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.mkdir() if name == 'worktree' else path.write_bytes(b'')
                with self.assertRaises(bootstrap.BootstrapError) as raised:
                    bootstrap.require_unproven_run_is_current(self.root)
                self.assertEqual(raised.exception.code, 'window_spent')

    def test_two_concurrent_reservations_claim_one_window(self):
        self.sealed()
        barrier = threading.Barrier(2)
        outcomes = []

        def fault(point):
            if point == 'before_reservation':
                barrier.wait(timeout=10)

        def claim():
            try:
                bootstrap.reserve_first_attempt(self.root, 'T')
                outcomes.append('reserved')
            except bootstrap.BootstrapError as exc:
                outcomes.append(exc.code)

        with patch.object(bootstrap, 'fault', side_effect=fault):
            threads = [threading.Thread(target=claim) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        self.assertEqual(sorted(outcomes), ['reserved', 'window_spent'])
        reservation = json.loads((self.root / bootstrap.RESERVATION).read_text())
        self.assertEqual(reservation['format'], bootstrap.RESERVATION_FORMAT)
        self.assertEqual(reservation['preparation_sha256'],
                         o.sha256_file(self.root / bootstrap.PREPARATION))


class PreparationJourneys(integration.StartupJourneys):
    """CLI and supervisor journeys: stop in recon/PO/design, resume, refuse after the first attempt."""

    def start(self):
        atomic_json(self.repo / '.ssdlc/bootstrap-profile.json', dict(
            schema_version='1.0', profile_id='python-unittest', window_tasks=1))
        orch = self.make_orchestrator()
        orch.state['specialists_triggered'] = []
        orch._save_state()
        planned = copy.deepcopy(self.plan)
        planned['base_commit'] = orch.state['base_commit']
        planned['tasks'][0]['verification']['quality_check_refs'] = ['CHECK-TESTS']
        planned['tasks'][0]['target_files'].append('tests/test_app.py')
        self.developers = []
        self.phases = []
        return orch, planned

    def cli(self, orch, planned, stop=None, expected=0, variant=False):
        def dispatch(role, *args, **kwargs):
            return ('codex' if role == '03-dev' else 'claude'), ('reviewer' if role.endswith('sec') else 'planner'), 'low', 'fixture'

        def claude(prompt, **kwargs):
            phase = next(f for f in inspect.stack() if f.function == 'invoke_role').frame.f_locals['phase']
            self.phases.append(phase)
            if phase == stop:
                raise Crash(phase)
            # A variant provider answers differently with the same requirement IDs.
            if phase == '00-recon':
                out = ('Different recon\n' if variant else 'Small scoped change\n') + 'RISK_TIER: S\nDEV_PROFILE: ALGORITHMIC'
            elif phase == '01-po':
                out = ('AC-1: Other behavior\nSEC-1: Other control\n' if variant
                       else 'AC-1: Correct behavior\nSEC-1: Secure behavior\n')
            elif phase == '02-design-task-plan':
                out = json.dumps(planned)
            elif phase == '02-design-task-plan-review':
                out = json.dumps(dict(verdict='APPROVED', findings=[]))
            elif phase in ('06-ops', '07-docs'):
                out = 'Verified candidate documentation'
            else:
                out = framed({'04-review': 'review', '05-qa': 'qa', '05b-appsec': 'appsec'}[orch.state['current_phase']], orch)
            return out, 0, 0, 0, 0

        def develop(prompt, **kwargs):
            self.developers.append(kwargs['cwd'])
            if stop == 'developer':
                raise Crash('developer')
            (kwargs['cwd'] / 'app.py').write_text('value = 1\n')
            tests = kwargs['cwd'] / 'tests'
            tests.mkdir(exist_ok=True)
            (tests / 'test_app.py').write_text('import unittest\nclass TestApp(unittest.TestCase):\n    def test_value(self): self.assertTrue(True)\n')
            return 'TASK_DEV_STATUS: COMPLETE', 0

        def check(argv, *args, **kwargs):
            return ('1000', '', 0) if argv == ['id', '-u'] else ('Ran 1 test\nOK', '', 0)

        env = dict(os.environ, SSDLC_EXECUTION_MODE='local')
        err = io.StringIO()
        with patch.dict(os.environ, env, clear=True), integration.existing_host(orch), \
             patch.object(o.SquadDispatcher, 'dispatch_role', side_effect=dispatch), \
             patch.object(o.SquadDispatcher, 'resolve_review_models', return_value=(['claude:reviewer'], 'low')), \
             patch.object(o.EngineRunner, 'run_claude', side_effect=claude), \
             patch.object(o.EngineRunner, 'run_codex', side_effect=develop), \
             patch.object(o.EngineRunner, 'run_sandboxed_cmd', side_effect=check), \
             patch.object(orch, '_run_appsec_scans', return_value=[]), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            argv = ['run', '--repo', str(self.repo), '--ticket', 'TKT-1', '--goal', 'Scoped change',
                    '--tier', 'S', '--no-sync-remote']
            if stop is not None and stop != 'resume':
                with self.assertRaises(Crash):
                    o._foreground_main(argv)
            else:
                self.assertEqual(o._foreground_main(argv), expected)
        return err.getvalue()

    def evidence(self, orch):
        root = orch.ssdlc_root
        return {name: (root / name).read_bytes() for name in (
            bootstrap.PREPARATION, '00-recon/quality-contract.json') if (root / name).exists()}

    APPROVED = {'00-recon': '00-recon/context-and-blast-radius.md', '01-po': '01-po/requirements.md'}

    def stop_and_resume(self, stop):
        orch, planned = self.start()
        if stop == 'before_reservation':
            with crash_at(stop):
                self.cli(orch, planned, stop=stop)
        else:
            self.cli(orch, planned, stop=stop)
        root = orch.ssdlc_root
        self.assertTrue((root / bootstrap.PREPARATION).is_file())
        self.assertFalse(bootstrap.development_started(root))
        self.assertEqual(self.developers, [])
        # Authorized read-only admission for the CLI and the supervisor.
        self.assertTrue(o.startup_preflight(self.repo, 'TKT-1', 'run')['ok'])
        self.assertTrue(o.resume_compatibility(self.repo, 'TKT-1', 'run', '00-recon')['ok'])
        sealed = self.evidence(orch)
        persisted = json.loads(orch.state_file.read_text())
        ledger, gates = copy.deepcopy(persisted['ledger']), copy.deepcopy(persisted['gate_history'])
        # Phases approved before the stop are reused, never dispatched again,
        # even though the provider now answers differently with the same IDs.
        reused = {'00-recon': (), '01-po': ('00-recon',), '02-design-task-plan': ('00-recon', '01-po'),
                  'before_reservation': ('00-recon', '01-po')}[stop]
        approved = {phase: (root / self.APPROVED[phase]).read_bytes() for phase in reused}
        self.phases.clear()
        self.cli(orch, planned, stop='resume', variant=True)
        for phase in reused:
            self.assertNotIn(phase, self.phases)
            self.assertEqual((root / self.APPROVED[phase]).read_bytes(), approved[phase])
        if stop == 'before_reservation':
            self.assertNotIn('02-design-task-plan', self.phases)
        self.assertEqual(len(self.developers), 1)
        self.assertEqual(self.evidence(orch), sealed)
        self.assertEqual(orch.state['ledger'][:len(ledger)], ledger)
        self.assertEqual(orch.state['gate_history'][:len(gates)], gates)
        self.assertTrue((root / bootstrap.RESERVATION).is_file())
        journal = TaskJournal(orch.dir_dev / 'tasks').peek()
        self.assertEqual(journal['bootstrap']['status'], 'PROVEN')
        self.assertEqual(journal['candidate_isolation'], 1)
        self.assertNotEqual(self.developers[0], orch.worktree_dir)
        self.assertEqual(orch.state['status'], 'AWAITING_SUPERVISOR_SIGNOFF')
        # After proof, strict journaled resume is unchanged.
        self.assertTrue(o.startup_preflight(self.repo, 'TKT-1', 'run', {'from_phase': '03-dev'})['ok'])
        return orch

    def test_stop_during_recon_resumes(self):
        self.stop_and_resume('00-recon')

    def test_stop_during_po_resumes(self):
        self.stop_and_resume('01-po')

    def test_stop_during_design_resumes(self):
        self.stop_and_resume('02-design-task-plan')

    def test_stop_before_reservation_keeps_approved_plan_inputs(self):
        self.stop_and_resume('before_reservation')

    def test_mutated_inputs_refuse_resume_without_writes(self):
        orch, planned = self.start()
        self.cli(orch, planned, stop='02-design-task-plan')
        requirements = orch.dir_po / 'requirements.md'
        original = requirements.read_bytes()
        stray = self.repo / 'stray.py'

        def tamper():
            requirements.write_bytes(original + b'AC-2: Injected\n')
            return lambda: requirements.write_bytes(original)

        def dirty():
            stray.write_text('x=1\n')
            return stray.unlink

        def controller():
            patcher = patch.object(o, 'controller_snapshot', return_value={'head': '0' * 40})
            patcher.start()
            return patcher.stop

        for reason, mutate in (('inputs_changed', tamper), ('dirty_tree', dirty), ('controller_changed', controller)):
            with self.subTest(reason=reason):
                before = self.evidence(orch)
                undo = mutate()
                try:
                    err = self.cli(orch, planned, stop='resume', expected=24)
                    self.assert_preflight_refuses(reason)
                finally:
                    undo()
                self.assertIn('bootstrap_ineligible: ' + reason, err)
                self.assertEqual(self.developers, [])
                self.assertFalse(bootstrap.development_started(orch.ssdlc_root))
                self.assertEqual(self.evidence(orch), before)
        # Restored inputs keep the original authority; nothing was reset.
        self.cli(orch, planned, stop='resume')
        self.assertEqual(len(self.developers), 1)

    def assert_preflight_refuses(self, reason):
        """CLI preflight, supervisor start and resume refuse before any write or launch."""
        for check in (o.startup_preflight(self.repo, 'TKT-1', 'run'),
                      o.resume_compatibility(self.repo, 'TKT-1', 'run', '00-recon')):
            self.assertEqual((check['ok'], check['code'], check['reason']), (False, 'bootstrap_ineligible', reason))
        with patch.object(supervisor.RunStore, 'prepare', side_effect=AssertionError('supervisor wrote')), \
             patch.object(supervisor, 'spawn', side_effect=AssertionError('supervisor launched')):
            for entry in (supervisor.start, supervisor.resume):
                result = entry(dict(repo=str(self.repo), ticket='TKT-1', goal='Scoped change'))
                self.assertEqual((result['status_reason'], result['bootstrap_reason']), ('bootstrap_ineligible', reason))
                self.assertEqual(supervisor.exit_code(result), 24)

    def assert_not_resumable(self, orch, planned, reason='window_spent'):
        dispatched = len(self.developers)
        journal = orch.dir_dev / 'tasks/task-journal.jsonl'
        before = journal.read_bytes() if journal.exists() else None
        for _ in range(2):  # two successive resumes: no dispatch, no renewal
            err = self.cli(orch, planned, stop='resume', expected=24)
            self.assertIn('bootstrap_ineligible: ' + reason, err)
            self.assertIn('not renewed', err)
        self.assertEqual(len(self.developers), dispatched)
        self.assertEqual(journal.read_bytes() if journal.exists() else None, before)
        check = o.resume_compatibility(self.repo, 'TKT-1', 'run', '00-recon')
        self.assertEqual((check['ok'], check['code'], check['reason']), (False, 'bootstrap_ineligible', reason))
        with patch.object(supervisor.RunStore, 'prepare', side_effect=AssertionError('supervisor wrote')):
            for entry in (supervisor.start, supervisor.resume):
                result = entry(dict(repo=str(self.repo), ticket='TKT-1', goal='Scoped change'))
                self.assertEqual((result['status_reason'], result['bootstrap_reason']), ('bootstrap_ineligible', reason))
                self.assertEqual(supervisor.exit_code(result), 24)

    def test_stop_after_reservation_before_journal_refuses(self):
        orch, planned = self.start()
        with crash_at('after_reservation'):
            self.cli(orch, planned, stop='after_reservation')
        self.assertFalse((orch.ssdlc_root / 'worktree').exists())
        self.assertFalse((orch.dir_dev / 'tasks/task-journal.jsonl').exists())
        self.assertEqual(self.developers, [])
        self.assert_not_resumable(orch, planned)

    def test_stop_during_first_attempt_refuses(self):
        orch, planned = self.start()
        self.cli(orch, planned, stop='developer')
        self.assertEqual(len(self.developers), 1)
        self.assert_not_resumable(orch, planned)

    def test_stop_before_reservation_resumes_with_one_dispatch(self):
        orch, planned = self.start()
        with crash_at('before_reservation'):
            self.cli(orch, planned, stop='before_reservation')
        self.assertFalse(bootstrap.development_started(orch.ssdlc_root))
        self.cli(orch, planned, stop='resume')
        self.assertEqual(len(self.developers), 1)


def load_tests(loader, standard_tests, pattern):
    # PreparationJourneys inherits fixture TestCases; select only its own tests.
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(PreparationTests))
    for name in PreparationJourneys.__dict__:
        if name.startswith('test_'):
            suite.addTest(PreparationJourneys(name))
    return suite


if __name__ == '__main__':
    unittest.main()
