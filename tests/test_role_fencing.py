"""Automatic fencing of role containers (#45, part c1): docs/writer-fencing.md."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import doc_contracts as docs
import orchestrator as o
from orchestrator import EngineRunner, SSDLCOrchestrator
import ssdlc_container_holder as holder
import ssdlc_role_fencing as rf
from ssdlc_recovery import NotDispatched
import ssdlc_writer_gate as wg
from replay.repository_fixtures import RepositoryTests


PAYLOAD = '1000:1000'


def ready(generation='.'):
    return holder.READY_PREFIX + json.dumps(dict(generation=generation, device=1, inode=2)) + '\n'


class Runner:
    """Simulated Docker client: records each argv and answers like a role container."""

    def __init__(self, returncode=0, stderr=None, stdout=None):
        self.calls, self.returncode = [], returncode
        self.stderr = stderr
        self.stdout = json.dumps({'result': 'DONE'}) if stdout is None else stdout

    def __call__(self, cmd, **kwargs):
        if cmd[:2] == ['docker', 'run']:
            self.calls.append(list(cmd))
            stderr = (ready() if rf.HOLDER_PATH in cmd else '') if self.stderr is None else self.stderr
            return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, stderr)
        return subprocess.CompletedProcess(cmd, 0, '', '')


class FencingTestCase(RepositoryTests):
    def setUp(self):
        super().setUp()
        self.ticket = self.repo / '.ssdlc' / 'TKT-1'
        patcher = patch.object(EngineRunner, '_should_spawn_role_container', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in ('SSDLC_CONTAINER_PAYLOAD_USER', 'SSDLC_SUPERVISED_RUN_ID', 'SSDLC_AUTONOMOUS',
                     'SSDLC_EXECUTION_MODE'):
            os.environ.pop(name, None)
        if os.geteuid() != 0:
            # Only root can own the gate or hand a new cache to the payload identity; the
            # ownership check has its own tests in test_container_holder and a failed
            # handover its own test below. Here the host side is under test.
            for target, name in ((rf.holder, 'check_private'), (o.os, 'chown')):
                stub = patch.object(target, name)
                setattr(self, f'stub_{name}', stub.start())
                self.addCleanup(stub.stop)

    def orchestrator(self):
        runtime = self.repo / '.ssdlc/.runtime'
        meta = {'project_id': 'test-project', 'codex_home': str(runtime / 'codex'),
                'claude_home': str(runtime / 'claude'), 'cache_dir': str(runtime / 'cache')}
        with patch('orchestrator.ProjectRuntimeIsolation.activate', return_value=meta), \
                patch('orchestrator.get_cli_version', return_value='TEST'):
            return SSDLCOrchestrator(self.repo, 'TKT-1', sync_remote=False)

    def run_role(self, orch, runner, *, probe=None):
        """One AI role invocation from a phase of ``orch``, as the pipeline makes it."""
        def phase(self):
            return EngineRunner.run_claude('Review', self.repo_root)
        out = io.StringIO()
        with patch('orchestrator.subprocess.run', side_effect=runner), \
                patch.object(rf, 'probe_access', side_effect=probe or (lambda uid, gid, needs: None)) as probed, \
                contextlib.redirect_stdout(out):
            result = o._supervised_phase('04-review')(phase)(orch)
        return result, probed, out.getvalue()

    def stored_state(self):
        return json.loads((self.ticket / 'state.json').read_text())


class AdmissionTests(FencingTestCase):
    def test_a_new_run_is_admitted_from_its_creation(self):
        orch = self.orchestrator()
        self.assertEqual(self.stored_state()[rf.MARKER_KEY], rf.MARKER)
        self.assertTrue(rf.admitted(orch.state))

    def test_admission_needs_the_marker_and_no_refusal(self):
        self.assertFalse(rf.admitted({}))
        self.assertFalse(rf.admitted({rf.MARKER_KEY: 'v2'}))
        self.assertFalse(rf.admitted({rf.MARKER_KEY: True}))
        self.assertFalse(rf.admitted({rf.MARKER_KEY: rf.MARKER, rf.REFUSAL_KEY: {'code': 'x'}}))
        self.assertFalse(rf.admitted(None))

    def test_a_preexisting_run_stays_manual_after_resume_even_with_a_gate(self):
        self.ticket.mkdir(parents=True, exist_ok=True)
        state = dict(task_engine_v1=True, ticket_id='TKT-1', artifacts={}, ledger=[], gate_history=[],
                     base_commit=self.base, status='STOPPED')
        (self.ticket / 'state.json').write_text(json.dumps(state))
        os.environ['SSDLC_CONTAINER_PAYLOAD_USER'] = PAYLOAD
        # An initialized gate is no admission: open_gate never marks a run.
        wg.open_gate(self.ticket).close()
        for _resume in range(2):
            orch = self.orchestrator()
            self.assertNotIn(rf.MARKER_KEY, orch.state)
            runner = Runner()
            with patch.object(rf, 'prepare') as prepare:
                self.run_role(orch, runner)
            prepare.assert_not_called()
            cmd = runner.calls[0]
            self.assertIn('--init', cmd)
            self.assertNotIn(rf.HOLDER_PATH, cmd)
            self.assertEqual(cmd[cmd.index('--entrypoint') + 1], '/bin/sh')
            self.assertNotIn(rf.MARKER_KEY, self.stored_state())
            self.assertNotIn(rf.REFUSAL_KEY, self.stored_state())

    def test_containers_outside_a_run_scope_keep_the_manual_path(self):
        self.orchestrator()
        os.environ['SSDLC_CONTAINER_PAYLOAD_USER'] = PAYLOAD
        cmd = EngineRunner._wrap_in_role_container(['true'], self.repo, pass_ai_keys=False)
        self.assertIn('--init', cmd)
        self.assertNotIn(rf.HOLDER_PATH, cmd)


class FencedContainerTests(FencingTestCase):
    def setUp(self):
        super().setUp()
        os.environ['SSDLC_CONTAINER_PAYLOAD_USER'] = PAYLOAD
        self.orch = self.orchestrator()

    def test_admitted_run_runs_roles_under_the_holder(self):
        runner = Runner()
        result, probed, printed = self.run_role(self.orch, runner)
        self.assertNotIn(rf.REFUSAL_KEY, self.stored_state(), printed)
        self.assertEqual(result[:2], ('DONE', 0))
        cmd = runner.calls[0]
        self.assertNotIn('--init', cmd)
        self.assertNotIn('--user', cmd)
        self.assertEqual(cmd[cmd.index('--entrypoint') + 1], 'python3')
        image = cmd.index(EngineRunner.IMAGE_NAME)
        command = cmd[image + 1:]
        self.assertEqual(command[:2], ['-I', rf.HOLDER_PATH])
        self.assertEqual(command[command.index('--ticket-root') + 1], str(self.ticket))
        self.assertEqual(command[command.index('--generation') + 1], '"."')
        self.assertEqual(command[command.index('--payload-user') + 1], PAYLOAD)
        gate = os.stat(wg.gate_path(self.ticket))
        self.assertEqual(command[command.index('--gate-identity') + 1], f'{gate.st_dev}:{gate.st_ino}')
        payload = command[command.index('--') + 1:]
        self.assertEqual(payload[:2], ['/bin/sh', '-c'])
        self.assertEqual(payload[3:5], ['ssdlc-role', 'claude'])
        self.assertIn(f'{self.ticket}:{self.ticket}:ro', cmd[:image])
        # The access probe saw every host bind mount, as the payload identity.
        uid, gid, needs = probed.call_args.args
        self.assertEqual((uid, gid), (1000, 1000))
        mounts = {cmd[i + 1].split(':', 1)[0] for i in range(image) if cmd[i] == '-v' and cmd[i + 1].startswith('/')}
        self.assertLessEqual(mounts - {str(self.ticket)}, {str(p) for p in needs})
        self.assertEqual(needs[self.repo], rf.READ)
        self.assertEqual(needs[self.repo / '.ssdlc/.runtime'], rf.WRITE)
        # The per-invocation cache is new and belongs to the payload.
        cache = next(Path(v.split('=', 1)[1]).parent for k, v in zip(cmd, cmd[1:])
                     if k == '-e' and v.startswith('PYTHONPYCACHEPREFIX='))
        if hasattr(self, 'stub_chown'):
            self.assertIn(((cache, 1000, 1000), {'follow_symlinks': False}), self.stub_chown.call_args_list)
        else:
            self.assertEqual((cache.stat().st_uid, cache.stat().st_gid), (1000, 1000))
        self.assertNotIn(rf.REFUSAL_KEY, self.stored_state())
        self.assertTrue(rf.admitted(self.stored_state()))

    def test_verification_containers_are_fenced_and_see_the_ticket(self):
        runner = Runner(stdout='0\n')

        def phase(self):
            return EngineRunner.run_sandboxed_cmd(['id', '-u'], self.repo_root, 30)
        with patch('orchestrator.subprocess.run', side_effect=runner), \
                patch.object(rf, 'probe_access', return_value=None):
            out, err, rc = o._supervised_phase('05-qa')(phase)(self.orch)
        self.assertEqual((out, rc), ('0\n', 0))
        self.assertNotIn(holder.READY_PREFIX, err)
        cmd = runner.calls[0]
        image = cmd.index(EngineRunner.IMAGE_NAME)
        self.assertIn(rf.HOLDER_PATH, cmd)
        # .ssdlc stays masked for test code; only the canonical ticket is mounted, read-only.
        self.assertIn(f'{self.repo / ".ssdlc"}:rw,size=16m', cmd[:image])
        self.assertIn(f'{self.ticket}:{self.ticket}:ro', cmd[:image])

    def test_resumed_planning_runs_planner_and_reviewer_under_the_holder(self):
        # Seed an approved design, so that only planning resumes.
        for name, content in [('01-po/requirements.md', 'AC-1 SEC-1'), ('02-design/adr.md', '# Approved design'),
                              ('02-design/threat-model.md', 'DESIGN_STATUS: APPROVED')]:
            path = self.orch.ssdlc_root / name
            path.write_text(content)
            self.orch.record_artifact(name, path)
        path = self.orch.dir_recon / 'quality-contract.json'
        path.write_text(json.dumps(self.quality))
        self.orch.record_artifact('00-recon/quality-contract.json', path)
        self.orch.record_gate('PAIR_DESIGN_GATE', True, 'Approved')
        runner = Runner()

        def plan(**kwargs):
            # The planner and reviewer roles the planning step dispatches.
            EngineRunner.run_claude('Plan', self.orch.repo_root)
            EngineRunner.run_codex('Review the plan', self.orch.repo_root)
        with patch('orchestrator.subprocess.run', side_effect=runner), \
                patch.object(rf, 'probe_access', return_value=None), \
                patch.object(self.orch, '_ensure_task_plan', side_effect=plan), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            self.orch.resume_task_planning()
        self.assertEqual(len(runner.calls), 2)
        for cmd in runner.calls:
            self.assertIn(rf.HOLDER_PATH, cmd)
            self.assertNotIn('--init', cmd)
        stored = self.stored_state()
        self.assertNotIn(rf.REFUSAL_KEY, stored, printed.getvalue())
        self.assertTrue(rf.admitted(stored))

    def test_holder_refusal_before_the_payload_is_not_a_role_result(self):
        refusal = holder.REFUSED_PREFIX + json.dumps(dict(code='stale_generation', detail='moved')) + '\n'
        with self.assertRaises(NotDispatched) as caught:
            self.run_role(self.orch, Runner(returncode=holder.REFUSED_EXIT, stderr=refusal, stdout=''))
        self.assertIn('stale_generation', str(caught.exception))

    def test_a_refusal_printed_after_the_payload_started_stays_a_role_result(self):
        forged = ready() + holder.REFUSED_PREFIX + json.dumps(dict(code='x', detail='y')) + '\n'
        result, _, _ = self.run_role(self.orch, Runner(returncode=holder.REFUSED_EXIT, stderr=forged))
        self.assertEqual(result[1], holder.REFUSED_EXIT)

    def test_a_moved_generation_is_refused_on_the_host(self):
        with patch.object(rf.holder, 'current_generation', return_value='runtime-generations/' + 'a' * 32):
            runner = Runner()
            _, _, printed = self.run_role(self.orch, runner)
        self.assertIn('--init', runner.calls[0])
        self.assertEqual(self.stored_state()[rf.REFUSAL_KEY]['code'], 'stale_generation')
        self.assertIn('stale_generation', printed)


class RefusalTests(FencingTestCase):
    def assert_manual(self, runner, code):
        cmd = runner.calls[0]
        self.assertIn('--init', cmd)
        self.assertNotIn(rf.HOLDER_PATH, cmd)
        self.assertEqual(cmd[cmd.index('--entrypoint') + 1], '/bin/sh')
        stored = self.stored_state()
        self.assertEqual(stored[rf.MARKER_KEY], rf.MARKER)
        self.assertEqual(stored[rf.REFUSAL_KEY]['code'], code)
        self.assertFalse(rf.admitted(stored))
        return stored[rf.REFUSAL_KEY]

    def test_missing_identity_is_refused_and_the_run_stays_manual(self):
        orch = self.orchestrator()
        runner = Runner()
        _, probed, printed = self.run_role(orch, runner)
        self.assert_manual(runner, rf.IDENTITY_MISSING)
        probed.assert_not_called()
        self.assertIn(rf.IDENTITY_MISSING, printed)
        self.assertFalse(wg.gate_path(self.ticket).exists())
        # Configuring the identity later does not readmit the run.
        os.environ['SSDLC_CONTAINER_PAYLOAD_USER'] = PAYLOAD
        again = Runner()
        self.run_role(self.orchestrator(), again)
        self.assertIn('--init', again.calls[0])

    def test_root_identity_is_refused(self):
        for value in ('0:0', '1000:0', '0:1000'):
            with self.subTest(value=value):
                shutil.rmtree(self.ticket, ignore_errors=True)
                os.environ['SSDLC_CONTAINER_PAYLOAD_USER'] = value
                runner = Runner()
                self.run_role(self.orchestrator(), runner)
                self.assert_manual(runner, holder.PAYLOAD_PRIVILEGED)

    def test_malformed_identity_is_refused(self):
        os.environ['SSDLC_CONTAINER_PAYLOAD_USER'] = 'builder'
        runner = Runner()
        self.run_role(self.orchestrator(), runner)
        self.assert_manual(runner, 'invalid_payload_user')

    def test_insufficient_access_is_refused_without_changing_permissions(self):
        os.environ['SSDLC_CONTAINER_PAYLOAD_USER'] = PAYLOAD
        orch = self.orchestrator()
        runtime = self.repo / '.ssdlc/.runtime'

        def modes():
            return {p: ((s := os.lstat(p)).st_mode, s.st_uid, s.st_gid)
                    for p in [self.repo, *self.repo.rglob('*')] if 'invocation-' not in str(p)}
        before = modes()
        runner = Runner()
        _, _, printed = self.run_role(orch, runner, probe=lambda uid, gid, needs: str(runtime))
        refusal = self.assert_manual(runner, rf.ACCESS_DENIED)
        self.assertEqual(refusal['detail'], str(runtime))
        self.assertIn(f'{rf.ACCESS_DENIED}: {runtime}', printed)
        after = modes()
        self.assertEqual({p: v for p, v in after.items() if p in before and p.name != 'state.json'},
                         {p: v for p, v in before.items() if p.name != 'state.json'})
        self.assertFalse(wg.gate_path(self.ticket).exists())

    def test_a_new_cache_the_controller_cannot_hand_over_is_refused(self):
        os.environ['SSDLC_CONTAINER_PAYLOAD_USER'] = PAYLOAD
        orch = self.orchestrator()
        runner = Runner()
        with patch('orchestrator.os.chown', side_effect=PermissionError('not root')):
            self.run_role(orch, runner)
        refusal = self.assert_manual(runner, rf.ACCESS_DENIED)
        self.assertIn('invocation-', refusal['detail'])

    def test_refusal_is_recorded_before_the_container_starts(self):
        os.environ['SSDLC_CONTAINER_PAYLOAD_USER'] = '0:0'
        orch = self.orchestrator()
        seen = []

        def runner(cmd, **kwargs):
            if cmd[:2] == ['docker', 'run']:
                seen.append(rf.REFUSAL_KEY in self.stored_state())
            return subprocess.CompletedProcess(cmd, 0, json.dumps({'result': 'DONE'}), '')
        self.run_role(orch, runner)
        self.assertEqual(seen, [True])

    def test_a_role_run_without_a_container_is_refused_and_a_docker_resume_stays_manual(self):
        os.environ['SSDLC_CONTAINER_PAYLOAD_USER'] = PAYLOAD
        seen = []

        def local(cmd, **kwargs):
            if cmd[:1] == ['claude']:
                seen.append(self.stored_state().get(rf.REFUSAL_KEY, {}).get('code'))
            return subprocess.CompletedProcess(cmd, 0, json.dumps({'result': 'DONE'}), '')
        for mode, docker in (('local', False), ('docker', False)):
            with self.subTest(mode=mode):
                shutil.rmtree(self.ticket, ignore_errors=True)
                os.environ['SSDLC_EXECUTION_MODE'] = mode
                orch, seen[:] = self.orchestrator(), []
                with patch.object(EngineRunner, '_should_spawn_role_container', return_value=docker):
                    result, probed, printed = self.run_role(orch, local)
                self.assertEqual(result[:2], ('DONE', 0))
                # Recorded before the role ran on the host.
                self.assertEqual(seen, [rf.OUTSIDE_CONTAINER])
                probed.assert_not_called()
                stored = self.stored_state()
                self.assertEqual(stored[rf.MARKER_KEY], rf.MARKER)
                self.assertIn(f'SSDLC_EXECUTION_MODE={mode}', stored[rf.REFUSAL_KEY]['detail'])
                self.assertIn(rf.OUTSIDE_CONTAINER, printed)
                self.assertFalse(rf.admitted(stored))
                # A later resume with Docker keeps the manual path.
                os.environ['SSDLC_EXECUTION_MODE'] = 'docker'
                runner = Runner()
                self.run_role(self.orchestrator(), runner)
                self.assert_manual(runner, rf.OUTSIDE_CONTAINER)

    def test_a_refusal_that_cannot_be_recorded_starts_nothing(self):
        orch = self.orchestrator()
        runner = Runner()
        with patch.object(orch, '_save_state', side_effect=OSError('read-only')), self.assertRaises(OSError):
            self.run_role(orch, runner)
        self.assertEqual(runner.calls, [])


class ProbeTests(unittest.TestCase):
    def test_probe_arguments_and_failures(self):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, '/denied\n', '')
        self.assertEqual(rf.probe_access(1000, 1001, {Path('/a'): rf.READ}, run=run), '/denied')
        argv, kwargs = calls[0]
        self.assertEqual(argv[:2], ['/bin/sh', '-c'])
        self.assertEqual(argv[4:], ['-r', '/a', '-x', '/a'])
        self.assertEqual((kwargs['user'], kwargs['group'], kwargs['extra_groups']), (1000, 1001, []))
        failed = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 2, '', '')
        with self.assertRaises(wg.GateError) as caught:
            rf.probe_access(1000, 1000, {}, run=failed)
        self.assertEqual(caught.exception.code, rf.ACCESS_DENIED)

        def unavailable(argv, **kwargs):
            raise PermissionError('not root')
        with self.assertRaises(wg.GateError) as caught:
            rf.probe_access(1000, 1000, {}, run=unavailable)
        self.assertEqual(caught.exception.code, rf.ACCESS_DENIED)

    @unittest.skipUnless(os.geteuid() == 0 and os.path.exists('/bin/sh'), 'the real probe needs root')
    def test_real_probe_judges_access_as_the_payload_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            closed, open_ = Path(tmp, 'closed'), Path(tmp, 'open')
            closed.mkdir(mode=0o700)
            open_.mkdir(mode=0o755)
            os.chmod(tmp, 0o755)
            os.chmod(open_, 0o755)
            os.chmod(closed, 0o700)
            before = os.stat(closed)
            self.assertIsNone(rf.probe_access(65534, 65534, {open_: rf.READ}))
            self.assertEqual(rf.probe_access(65534, 65534, {open_: rf.WRITE}), str(open_))
            self.assertEqual(rf.probe_access(65534, 65534, {closed: rf.READ}), str(closed))
            os.chown(open_, 65534, 65534)
            self.assertIsNone(rf.probe_access(65534, 65534, {open_: rf.WRITE}))
            self.assertEqual(os.stat(closed), before)


class DocumentationTests(unittest.TestCase):
    def test_activation_identity_and_admission_are_documented(self):
        docs.assert_documented(self, docs.user_facing_docs(), [
            'ssdlc_role_fencing.py', '"writer_fencing": "v1"', 'writer_fencing_refusal',
            'SSDLC_CONTAINER_PAYLOAD_USER', 'CONTAINER_PAYLOAD_IDENTITY_MISSING',
            'CONTAINER_PAYLOAD_ACCESS_DENIED: <path>', 'never changes', 'stay on the manual path',
            'ROLE_OUTSIDE_CONTAINER', 'resume_task_planning()',
        ], issues=(45, 158))


class ConfigurationTests(unittest.TestCase):
    def test_payload_identity_is_a_known_setting(self):
        self.assertIn(rf.PAYLOAD_USER_ENV, o.KNOWN_PROJECT_ENV_VARS)
        self.assertEqual(rf.payload_identity({rf.PAYLOAD_USER_ENV: ' 1000:1001 '}), (1000, 1001))
        with self.assertRaises(wg.GateError) as caught:
            rf.payload_identity({rf.PAYLOAD_USER_ENV: ''})
        self.assertEqual(caught.exception.code, rf.IDENTITY_MISSING)


if __name__ == '__main__':
    unittest.main()
