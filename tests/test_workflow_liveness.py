"""Liveness, coverage, activity and next action of a WorkflowSnapshot (#347)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ssdlc_workflow_liveness as lv  # noqa: E402
import ssdlc_workflow_status as ws  # noqa: E402
import ssdlc_writer_gate as wg  # noqa: E402
from test_workflow_status import Case, TICKET, tasks_state, tree  # noqa: E402

COVERED = {name: None for name in ('controller', 'host_trees', 'containers')}
AT_REST = ('PAUSED_AT_PHASE_BOUNDARY', 'AWAITING_SUPERVISOR_SIGNOFF', 'APPROVED_AND_MERGED', 'PLAN_READY',
           'FAILED', 'HALTED_BUDGET_EXCEEDED', 'ESCALATED_TO_SUPERVISOR')
GATE_READ = dict(lock=wg.GATE_NAME, mode='READ', pid=4242)

# Holds a flock on argv[1] (SH or EX) until its stdin closes.
HOLDER = r'''
import fcntl, sys
stream = open(sys.argv[1], 'rb')
fcntl.flock(stream, fcntl.LOCK_SH if sys.argv[2] == 'SH' else fcntl.LOCK_EX)
print('READY', flush=True)
sys.stdin.read()
'''

# Rewrites state.json without taking any lock: a writer the gate does not cover.
UNCOVERED = r'''
import json, sys, time
path, deadline = sys.argv[1], time.time() + 3
n = 0
while time.time() < deadline:
    n += 1
    with open(path + '.tmp', 'w') as stream:
        json.dump({'status': 'IN_PROGRESS', 'current_phase': '03-dev', 'current_step': str(n), 'ledger': []}, stream)
    import os
    os.replace(path + '.tmp', path)
'''


class FakeKernel:
    """Simulated writers and coverage."""

    def __init__(self, holders=(), reason=None, components=COVERED):
        self.holders, self.reason, self.components = list(holders), reason, dict(components)

    def writers(self, ticket_root):
        return dict(holders=list(self.holders), reason=self.reason)

    def coverage(self, ticket_root, generation, fencing):
        return dict(self.components)


class LivenessCase(Case):
    def set_state(self, **fields):
        path = self.ticket / 'state.json'
        state = json.loads(path.read_text())
        state.update(fields)
        path.write_text(json.dumps(state))

    def status(self, kernel, **kw):
        result = self.snap(kernel=kernel, **kw)
        self.assertEqual(result['read']['status'], ws.COHERENT, result['read'])
        return result['status']


class ClassificationTest(LivenessCase):
    def test_a_live_writer_is_running_whatever_the_coverage(self):
        for components in (COVERED, dict(COVERED, controller='not declared')):
            for status in ('IN_PROGRESS', 'PAUSED_AT_PHASE_BOUNDARY', 'FAILED'):
                with self.subTest(components=components, status=status):
                    self.set_state(status=status)
                    result = self.status(FakeKernel([GATE_READ], components=components))
                    self.assertEqual(result['liveness'], lv.RUNNING)
                    self.assertEqual(result['state'], status)  # the native state is kept apart
                    self.assertEqual(result['writers'], [GATE_READ])
                    self.assertIsNone(result['next_action']['command'])

    def test_no_writer_with_full_coverage_is_stale_or_quiescent(self):
        result = self.status(FakeKernel())
        self.assertTrue(result['coverage']['complete'])
        self.assertEqual((result['state'], result['liveness']), ('IN_PROGRESS', lv.STALE))
        self.assertEqual(result['stop']['class'], 'uncertain_outcome')
        for status in AT_REST:
            with self.subTest(status=status):
                self.set_state(status=status)
                self.assertEqual(self.status(FakeKernel())['liveness'], lv.QUIESCENT)

    def test_without_full_coverage_it_is_never_quiescent(self):
        uncovered = [dict(reason='/proc/locks is not readable')]
        uncovered += [dict(components=dict(COVERED, **{name: 'not covered'})) for name in COVERED]
        uncovered.append(dict(components={k: v for k, v in COVERED.items() if k != 'containers'}))
        for status in AT_REST + ('IN_PROGRESS', 'SOMETHING_NEW'):
            for kw in uncovered:
                with self.subTest(status=status, kernel=kw):
                    self.set_state(status=status)
                    result = self.status(FakeKernel(**kw))
                    self.assertFalse(result['coverage']['complete'])
                    self.assertEqual(result['liveness'], lv.UNVERIFIED)
        (self.ticket / 'state.json').unlink()
        self.assertEqual(self.status(FakeKernel())['liveness'], lv.UNVERIFIED)  # no controller declaration

    def test_real_runs_without_the_gate_or_the_controller_declaration_are_unverified(self):
        for status in AT_REST + ('IN_PROGRESS',):
            with self.subTest(status=status):
                self.set_state(status=status, writer_fencing='v1')
                result = self.status(lv.Kernel(docker=self.no_docker))
                self.assertEqual(result['liveness'], lv.UNVERIFIED)
                components = result['coverage']['components']
                self.assertFalse(components['writers']['covered'])
                self.assertIn('#345', components['controller']['reason'])

    @staticmethod
    def no_docker(*args, **kw):
        raise AssertionError('Docker is consulted only when every other component is covered')

    def test_native_classification_of_stops(self):
        cases = [
            (dict(status='HALTED_BUDGET_EXCEEDED'), 'limits', 'budget_exceeded'),
            (dict(status='ESCALATED_TO_SUPERVISOR'), 'operator_decision', 'operator_decision'),
            (dict(status='FAILED', failure={'category': 'LIMIT_REACHED'}), 'limits', 'limit_reached'),
            (dict(status='FAILED', failure={'category': 'OUTCOME_UNCERTAIN'}), 'uncertain_outcome', 'outcome_uncertain'),
            (dict(status='FAILED', failure={'category': 'AUTH_EXPIRED'}), 'authentication', 'auth_expired'),
            (dict(status='FAILED', failure={'category': 'INTEGRITY'}), 'integrity', 'integrity_failure'),
            (dict(status='FAILED', failure={'category': 'ENVIRONMENT_RESOURCE'}), 'limits', 'resource_limit'),
            # Bound to an outcome only by the supervisor's events: unbound here, as its fallback row.
            (dict(status='FAILED', failure={'category': 'CHECK_FAILURE'}), 'uncertain_outcome', 'terminal_unbound'),
            (dict(status='FAILED', failure=None), 'uncertain_outcome', 'terminal_unbound'),
        ]
        from ssdlc_supervisor import RECOVERY_MATRIX
        for fields, cls, code in cases:
            with self.subTest(fields=fields):
                self.set_state(**fields)
                result = self.status(FakeKernel())
                self.assertEqual((result['stop']['class'], result['stop']['reason_code']), (cls, code))
                self.assertEqual(result['next_action']['action'], RECOVERY_MATRIX[cls]['action'])
                self.assertEqual(result['next_action']['mode'], RECOVERY_MATRIX[cls]['mode'])
        for status in ('PAUSED_AT_PHASE_BOUNDARY', 'AWAITING_SUPERVISOR_SIGNOFF', 'APPROVED_AND_MERGED'):
            self.set_state(status=status, failure=None)
            self.assertIsNone(self.status(FakeKernel())['stop'])

    def test_a_recorded_category_takes_the_native_precedence_over_escalation_and_budget(self):
        # The pipeline records ESCALATED_TO_SUPERVISOR also for these categories: the
        # supervisor's table classifies the category first, and so does the status.
        from ssdlc_supervisor import RECOVERY_MATRIX
        cases = [
            ('LIMIT_REACHED', 'limits', 'limit_reached', 'a limit explicitly raised by the operator'),
            ('OUTCOME_UNCERTAIN', 'uncertain_outcome', 'outcome_uncertain', 'reconciles the invocation outcome'),
            ('AUTH_EXPIRED', 'authentication', 'auth_expired', 'authentication refreshed out of band'),
            ('INTEGRITY', 'integrity', 'integrity_failure', 'reconciles integrity'),
            ('ENVIRONMENT_RESOURCE', 'limits', 'resource_limit', 'a limit explicitly raised by the operator'),
            ('ENVIRONMENT_DIAGNOSIS', 'transient_environment_provider', 'environment_diagnosis',
             'retries read-only preflight'),
        ]
        for status in ('ESCALATED_TO_SUPERVISOR', 'HALTED_BUDGET_EXCEEDED'):
            for category, cls, code, prerequisite in cases:
                with self.subTest(status=status, category=category):
                    self.set_state(status=status, failure={'category': category})
                    result = self.status(FakeKernel())
                    self.assertEqual((result['stop']['class'], result['stop']['reason_code']), (cls, code))
                    action = result['next_action']
                    self.assertEqual((action['action'], action['mode']),
                                     (RECOVERY_MATRIX[cls]['action'], RECOVERY_MATRIX[cls]['mode']))
                    self.assertIsNone(action['command'])
                    self.assertTrue(any(prerequisite in p for p in action['prerequisites']), action)
                    self.assertIn(lv.RESUME, action['prerequisites'])
        # A category the supervisor binds only through its events keeps the status's class.
        for status, cls, code in (('ESCALATED_TO_SUPERVISOR', 'operator_decision', 'operator_decision'),
                                  ('HALTED_BUDGET_EXCEEDED', 'limits', 'budget_exceeded')):
            self.set_state(status=status, failure={'category': 'CHECK_FAILURE'})
            stop = self.status(FakeKernel())['stop']
            self.assertEqual((stop['class'], stop['reason_code']), (cls, code))

    def test_activity_is_what_the_records_say_and_idle_only_without_writers(self):
        self.writer.append(tasks_state(a='COMPLETED', b='RUNNING', attempts=1), 'TASK_STARTED')
        running = self.status(FakeKernel([GATE_READ]))['activity']
        self.assertEqual(running['value'], lv.INVOCATION)
        self.assertEqual(running['invocations'], [dict(source='task journal', task_id='B')])
        self.writer.append(tasks_state(a='COMPLETED', b='VERIFYING', attempts=1), 'VERIFYING')
        self.assertEqual(self.status(FakeKernel([GATE_READ]))['activity']['value'], lv.VERIFICATION)
        stale = self.status(FakeKernel())
        self.assertEqual((stale['liveness'], stale['activity']['value']), (lv.STALE, lv.IDLE))
        self.assertIn('interrupted', stale['activity']['reason'])
        self.writer.append(tasks_state(a='COMPLETED', attempts=1), 'RESET')
        unknown = self.status(FakeKernel(components=dict(COVERED, controller='x')))['activity']
        self.assertIsNone(unknown['value'])  # nothing recorded, but inactivity is not proven
        self.assertNotIn('WAITING_PROVIDER', json.dumps(unknown))


class NextActionTest(LivenessCase):
    def test_no_authorization_limit_or_resume_parameter_is_invented(self):
        goal = 'secret goal text 7319'
        self.set_state(goal=goal, phase_boundary={'next_phase': '04-review'})
        commands = {}
        for status, failure in [('PAUSED_AT_PHASE_BOUNDARY', None), ('PLAN_READY', None), ('IN_PROGRESS', None),
                                ('HALTED_BUDGET_EXCEEDED', None), ('ESCALATED_TO_SUPERVISOR', None),
                                ('FAILED', {'category': 'LIMIT_REACHED'}), ('FAILED', {'category': 'AUTH_MISSING'}),
                                ('FAILED', {'category': 'OUTCOME_UNCERTAIN'}), ('FAILED', {'category': 'INTEGRITY'}),
                                ('FAILED', {'category': 'CHECK_FAILURE'}), ('AWAITING_SUPERVISOR_SIGNOFF', None),
                                ('APPROVED_AND_MERGED', None)]:
            for kernel in (FakeKernel(), FakeKernel(components=dict(COVERED, controller='not declared'))):
                with self.subTest(status=status, failure=failure, kernel=kernel.components):
                    self.set_state(status=status, failure=failure)
                    action = self.status(kernel)['next_action']
                    text = json.dumps(action)
                    self.assertNotIn(goal, text)
                    self.assertNotIn('source_fenced', text)
                    commands[status] = action['command']
                    if action['command'] is None:
                        continue
                    # The only command: every argument is the ticket's own, nothing is added.
                    self.assertEqual(action['command'], dict(argv=['./ssdlc', 'approve', TICKET], cwd=str(self.repo)))
        self.assertEqual({k for k, v in commands.items() if v is not None}, {'AWAITING_SUPERVISOR_SIGNOFF'})

    def test_a_limits_stop_proposes_no_value(self):
        self.set_state(status='HALTED_BUDGET_EXCEEDED')
        action = self.status(FakeKernel())['next_action']
        self.assertIsNone(action['command'])
        self.assertEqual(action['mode'], 'operator-only')
        self.assertTrue(any('no value is proposed' in p for p in action['prerequisites']))
        self.assertFalse(any(ch.isdigit() for ch in json.dumps(action['prerequisites'])))

    def test_a_pause_names_the_next_phase_but_no_command(self):
        self.set_state(status='PAUSED_AT_PHASE_BOUNDARY', goal='g', phase_boundary={'next_phase': '04-review'})
        action = self.status(FakeKernel())['next_action']
        self.assertIn('--from-phase 04-review', action['action'])
        self.assertIsNone(action['command'])
        self.assertIn(lv.RESUME, action['prerequisites'])

    def test_unverified_liveness_asks_to_confirm_first_and_running_asks_to_wait(self):
        self.set_state(status='FAILED', failure={'category': 'LIMIT_REACHED'})
        action = self.status(FakeKernel(components=dict(COVERED, host_trees='tree t ran without a kernel mechanism')))
        self.assertTrue(action['next_action']['prerequisites'][0].startswith('confirm that no process'))
        self.assertIn('tree t ran without a kernel mechanism', action['next_action']['prerequisites'][0])
        running = self.status(FakeKernel([GATE_READ]))['next_action']
        self.assertEqual((running['mode'], running['command'], running['prerequisites']), ('none', None, []))


class KernelTest(LivenessCase):
    """Real kernel locks held by real processes, with simulated or real coverage."""

    def setUp(self):
        super().setUp()
        try:
            gate = wg.open_gate(self.ticket.absolute())
        except wg.GateError as exc:
            self.skipTest(f'writer gate unavailable here: {exc.code}')
        self.gate_identity = gate.identity
        gate.close()
        (self.ticket / lv.TICKET_LOCK).touch()

    def hold(self, name, mode='SH'):
        process = subprocess.Popen([sys.executable, '-c', HOLDER, str(self.ticket / name), mode],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, process)
        self.assertEqual(process.stdout.readline(), 'READY\n')
        return process

    @staticmethod
    def stop(process, kill=False):
        if process.poll() is None:
            if kill:
                process.kill()
            else:
                process.stdin.close()
            process.wait(timeout=10)
        for stream in (process.stdin, process.stdout):
            if stream and not stream.closed:
                stream.close()

    def kernel(self, components=COVERED):
        kernel = lv.Kernel()
        kernel.coverage = lambda *args: dict(components)
        return kernel

    def test_live_absent_and_uncovered_writers(self):
        writer = self.hold(wg.GATE_NAME)
        for components in (COVERED, dict(COVERED, controller='not declared')):
            result = self.status(self.kernel(components))
            self.assertEqual(result['liveness'], lv.RUNNING)
            self.assertIn(dict(lock=wg.GATE_NAME, mode='READ', pid=writer.pid), result['writers'])
        self.stop(writer, kill=True)  # SIGKILL: the kernel releases the lock
        self.assertEqual(self.status(self.kernel())['liveness'], lv.STALE)
        self.assertEqual(self.status(self.kernel(dict(COVERED, controller='not declared')))['liveness'], lv.UNVERIFIED)
        self.set_state(status='PAUSED_AT_PHASE_BOUNDARY')
        self.assertEqual(self.status(self.kernel())['liveness'], lv.QUIESCENT)

    def test_a_ticket_lock_holder_is_a_running_writer(self):
        holder = self.hold(lv.TICKET_LOCK, 'EX')
        result = self.status(self.kernel())
        self.assertEqual(result['liveness'], lv.RUNNING)
        self.assertEqual(result['writers'], [dict(lock=lv.TICKET_LOCK, mode='WRITE', pid=holder.pid)])

    def test_a_writer_that_starts_during_the_collection_is_never_missed(self):
        started = []

        def fault(point, attempt):
            if not started:
                started.append(self.hold(wg.GATE_NAME))
        result = self.status(self.kernel(), fault=fault)
        self.assertEqual(result['liveness'], lv.RUNNING)
        self.assertEqual(result['writers'][0]['pid'], started[0].pid)

    def test_a_writer_seen_before_the_collection_still_counts_when_it_ends_during_it(self):
        writer = self.hold(wg.GATE_NAME)
        result = self.status(self.kernel(), fault=lambda point, attempt: self.stop(writer, kill=True))
        self.assertEqual(result['liveness'], lv.RUNNING)

    def test_concurrent_writers_never_give_quiescent_or_stale_while_they_hold_the_gate(self):
        self.set_state(status='PAUSED_AT_PHASE_BOUNDARY')
        writers = [self.hold(wg.GATE_NAME) for _ in range(3)]
        seen = set()
        for index, writer in enumerate(writers):
            result = self.status(self.kernel())
            self.assertEqual(result['liveness'], lv.RUNNING)
            seen.update(w['pid'] for w in result['writers'])
            self.stop(writer, kill=index % 2 == 0)
        self.assertEqual(seen, {w.pid for w in writers})
        self.assertEqual(self.status(self.kernel())['liveness'], lv.QUIESCENT)

    def test_an_uncovered_writer_is_never_quiescent(self):
        # It takes no lock: the kernel cannot see it, and real coverage is incomplete.
        self.set_state(status='PAUSED_AT_PHASE_BOUNDARY', writer_fencing='v1')
        process = subprocess.Popen([sys.executable, '-c', UNCOVERED, str(self.ticket / 'state.json')])
        self.addCleanup(process.wait)
        deadline = time.monotonic() + 2
        seen = set()
        while time.monotonic() < deadline:
            result = ws.snapshot(self.repo, TICKET, kernel=lv.Kernel(docker=ClassificationTest.no_docker))
            if result['read']['status'] == ws.COHERENT:
                seen.add(result['status']['liveness'])
                self.assertFalse(result['status']['coverage']['complete'])
            else:
                seen.add(result['read']['status'])
        self.assertTrue(seen)
        self.assertLessEqual(seen, {lv.UNVERIFIED, ws.TORN})

    def test_real_coverage_complete_and_an_unproven_host_tree(self):
        self.set_state(status='PAUSED_AT_PHASE_BOUNDARY', writer_fencing='v1',
                       **{lv.CONTROLLER_KEY: lv.CONTROLLER_PROTOCOL})
        listed = []

        def docker(argv, **kw):
            listed.append(argv[:2])
            return types.SimpleNamespace(stdout='')
        result = self.status(lv.Kernel(docker=docker))
        self.assertTrue(result['coverage']['complete'], result['coverage'])
        self.assertEqual(result['liveness'], lv.QUIESCENT)
        self.assertEqual(listed, [['docker', 'ps']])
        gate = wg.open_gate(self.ticket.absolute())
        try:
            identity = wg.self_identity()[0]
            wg._record_tree(gate, 'f' * 32, wg.generation_key('.'), identity, identity, None, 'prctl refused')
        finally:
            gate.close()
        result = self.status(lv.Kernel(docker=docker))
        self.assertEqual(result['liveness'], lv.UNVERIFIED)
        self.assertIn('ran without a kernel mechanism: prctl refused',
                      result['coverage']['components']['host_trees']['reason'])

    def test_a_docker_container_of_the_ticket_or_a_docker_failure_is_not_coverage(self):
        self.set_state(status='PAUSED_AT_PHASE_BOUNDARY', writer_fencing='v1',
                       **{lv.CONTROLLER_KEY: lv.CONTROLLER_PROTOCOL})

        def failing(argv, **kw):
            raise OSError('no docker')
        result = self.status(lv.Kernel(docker=failing))
        self.assertEqual(result['liveness'], lv.UNVERIFIED)
        self.assertIn('docker', result['coverage']['components']['containers']['reason'])
        self.set_state(writer_fencing_refusal={'code': 'x'})
        result = self.status(lv.Kernel(docker=ClassificationTest.no_docker))
        self.assertEqual(result['liveness'], lv.UNVERIFIED)

    def test_observation_writes_nothing(self):
        self.set_state(writer_fencing='v1', **{lv.CONTROLLER_KEY: lv.CONTROLLER_PROTOCOL})
        writer = self.hold(wg.GATE_NAME)
        before = tree(self.repo)
        time.sleep(0.01)
        result = self.status(lv.Kernel(docker=lambda argv, **kw: types.SimpleNamespace(stdout='')))
        self.assertEqual(result['liveness'], lv.RUNNING)
        self.assertEqual(tree(self.repo), before)
        self.assertFalse((self.ticket / wg.REGISTRY_NAME).exists())
        self.stop(writer)


if __name__ == '__main__':
    unittest.main()
