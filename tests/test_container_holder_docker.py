"""Docker tests of the PID 1 holder of the writer gate (#45, part b).

Opt-in, run by the operator on the host (as root, with a local Docker daemon and an
image with python3; ``SSDLC_DOCKER_TEST_IMAGE``, default ``ssdlc-runner:latest``):

    SSDLC_DOCKER_TESTS=1 python3 -m unittest discover -s tests -p 'test_container_holder_docker.py' -v

Skipped otherwise. Only containers created here, named ``ssdlc-wg-test-*`` and
labelled for this test, are ever stopped or removed. Until these tests pass on the
operator's host, automatic fencing is not declared reliable.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import unittest
import uuid

import ssdlc_container_holder as ch
import ssdlc_writer_gate as wg
from test_container_holder import PAYLOAD, TicketCase, line, wait_for


ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get('SSDLC_DOCKER_TEST_IMAGE', 'ssdlc-runner:latest')
USER = '65534:65534'

# The design the holder replaces: a host process holds LOCK_SH for a container.
HOST_HOLDER = r'''
import subprocess, sys
import ssdlc_container_holder as ch
from ssdlc_writer_gate import writer
ticket = sys.argv[1]
with writer(ticket, '.', lambda: ch.current_generation(ticket), timeout=5):
    print('HOLDING', flush=True)
    subprocess.run(sys.argv[2:])
'''


def docker(*args, timeout=120):
    return subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)


def _unavailable():
    if os.environ.get('SSDLC_DOCKER_TESTS') != '1':
        return 'opt-in: set SSDLC_DOCKER_TESTS=1 on the operator host'
    if not sys.platform.startswith('linux') or os.geteuid() != 0:
        return 'needs root on Linux: the gate must be root-owned 0600'
    if not shutil.which('docker'):
        return 'docker is not installed'
    try:
        if docker('info', timeout=30).returncode != 0:
            return 'the Docker daemon is not reachable'
        if docker('image', 'inspect', IMAGE, timeout=30).returncode != 0:
            return f'image {IMAGE} is not available locally (SSDLC_DOCKER_TEST_IMAGE)'
    except (OSError, subprocess.SubprocessError) as exc:
        return f'docker failed: {type(exc).__name__}'
    return None


class DockerHolderTests(TicketCase):
    @classmethod
    def setUpClass(cls):
        cls.unavailable = _unavailable()

    def setUp(self):
        if self.unavailable:
            self.skipTest(self.unavailable)
        super().setUp()
        for path in (self.base, self.base / '.ssdlc', self.ticket):
            os.chmod(path, 0o755)  # only the gate's own 0600 keeps the payload out
        self.scratch = self.base / 'scratch'
        self.scratch.mkdir()
        os.chmod(self.scratch, 0o777)
        self.initialize()
        self.label = 'ssdlc.writer_gate.test=' + uuid.uuid4().hex
        self.probe = ch.running_containers(self.label)

    def name(self, kind='run'):
        name = f'ssdlc-wg-test-{kind}-{uuid.uuid4().hex[:12]}'
        self.addCleanup(docker, 'rm', '-f', name)
        return name

    def running(self, name):
        done = docker('inspect', '-f', '{{.State.Running}}', name)
        return done.returncode == 0 and done.stdout.strip() == 'true'

    def gone(self, name):
        return docker('inspect', name).returncode != 0

    def holder_argv(self, name, payload, *, generation='.', extra=(), wait=None, command=None):
        options, holder = ch.holder_command(self.ticket, generation, USER, payload, wait=wait)
        return ['docker', 'run', '--rm', '--name', name, '--label', self.label,
                '--security-opt', 'no-new-privileges:true',
                '-v', f'{ROOT}:/opt/ssdlc:ro', '-v', f'{self.ticket}:{self.ticket}:ro',
                '-v', f'{self.scratch}:{self.scratch}:rw', *extra, *options, IMAGE, *(command or holder)]

    def assert_transition(self, code=None, coverage=None):
        coverage = [self.probe] if coverage is None else coverage
        if code is None:
            with wg.transition(str(self.ticket), self.generation, coverage=coverage) as proof:
                self.assertEqual(proof.check()['kind'], 'writer-gate-exclusive-v1')
            return
        with self.assertRaises(wg.GateError) as caught:
            with wg.transition(str(self.ticket), self.generation, coverage=coverage):
                pass
        self.assertEqual(caught.exception.code, code, caught.exception.detail)

    def test_container_surviving_a_killed_controller_prevents_false_exclusivity(self):
        name = self.name()
        controller = subprocess.Popen(
            [sys.executable, '-c', 'import subprocess, sys; subprocess.run(sys.argv[1:])',
             *self.holder_argv(name, ['sleep', '300'])],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: controller.poll() is None and os.killpg(controller.pid, signal.SIGKILL))
        self.assertTrue(wait_for(self.read_lock_held, 120), 'the holder never took LOCK_SH')
        os.killpg(controller.pid, signal.SIGKILL)  # the controller and its docker client
        controller.wait(timeout=30)
        time.sleep(1)
        self.assertTrue(self.running(name), 'the container must outlive its controller')
        self.assert_transition('writers_active', coverage=[])
        self.assert_transition('writers_active')
        self.assertEqual(docker('stop', '-t', '20', name).returncode, 0)  # SIGTERM, forwarded by PID 1
        self.assertTrue(wait_for(lambda: self.gone(name), 60))
        self.assert_transition()

    def test_death_of_a_host_lock_holder_never_yields_a_false_proof(self):
        name = self.name()
        plain = ['docker', 'run', '--rm', '--name', name, '--label', self.label, '--entrypoint', 'sleep', IMAGE, '300']
        host = subprocess.Popen([sys.executable, '-c', HOST_HOLDER, str(self.ticket), *plain], cwd=ROOT,
                                env=dict(os.environ, PYTHONPATH=str(ROOT)), start_new_session=True,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.addCleanup(lambda: host.poll() is None and os.killpg(host.pid, signal.SIGKILL))
        self.addCleanup(host.stdout.close)
        self.assertEqual(line(host.stdout, 60), 'HOLDING')
        self.assertTrue(wait_for(lambda: self.running(name), 120))
        os.killpg(host.pid, signal.SIGKILL)
        host.wait(timeout=30)
        self.assertTrue(self.running(name), 'the container must outlive the host process that held its lock')
        # LOCK_EX succeeds now, since nobody holds the gate: only Docker sees the container.
        self.assert_transition('uncovered_execution')
        self.assertEqual(docker('rm', '-f', name).returncode, 0)
        self.assertTrue(wait_for(lambda: self.gone(name), 60))
        self.assert_transition()

    def test_foreign_containers_are_never_touched(self):
        foreign = self.name('foreign')
        started = docker('run', '-d', '--name', foreign, '--entrypoint', 'sleep', IMAGE, '300')
        self.assertEqual(started.returncode, 0, started.stderr)
        name = self.name()
        # The payload leaves a daemon behind: PID 1 kills and reaps it before the lock goes.
        done = subprocess.run(self.holder_argv(name, ['/bin/sh', '-c', 'sleep 300 & exit 0']),
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(ch.READY_PREFIX, done.stderr)
        self.assertTrue(wait_for(lambda: self.gone(name), 60))
        self.assertTrue(self.running(foreign), 'a foreign container was touched')
        self.assert_transition()  # the probe ignores containers of other runs
        self.assertTrue(self.running(foreign), 'a foreign container was touched')

    def test_payload_cannot_reach_the_gate(self):
        name = self.name()
        child = subprocess.Popen(
            self.holder_argv(name, ['python3', '-I', '-c', PAYLOAD, str(self.gate), str(self.ticket / wg.ANCHOR_NAME),
                                    str(self.scratch)], extra=['-i']),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.addCleanup(lambda: [stream.close() for stream in (child.stdin, child.stdout, child.stderr)])
        output = line(child.stdout, 180)
        self.assertIsNotNone(output, 'the payload never reported')
        report = json.loads(output)
        self.assertNotEqual(report['pid'], 1)
        self.assertEqual((report['uid'], report['gid'], report['groups']), ([65534] * 3, [65534] * 3, []))
        self.assertEqual(set(report['caps'].values()), {'0000000000000000'})
        self.assertEqual(report['no_new_privs'], '1')
        for attempt in ('open_gate', 'open_anchor', 'holder_fds', 'signal_holder'):
            self.assertEqual(report[attempt], 'PermissionError', attempt)
        self.assertEqual(report['fds'], [])
        self.assertTrue(self.read_lock_held())
        self.assert_transition('writers_active')
        beat = self.scratch / 'beat'
        self.assertTrue(wait_for(beat.exists))
        child.stdin.write(b'\n')
        child.stdin.flush()
        self.assertEqual(child.wait(timeout=60), 0)
        size = beat.stat().st_size
        time.sleep(1)
        self.assertEqual(beat.stat().st_size, size, 'a payload daemon outlived PID 1')
        self.assertTrue(wait_for(lambda: self.gone(name), 60))
        self.assert_transition()

    def test_late_or_stale_containers_never_run_their_payload(self):
        payload = ['/bin/sh', '-c', 'echo PAYLOAD-RAN']
        with wg.transition(str(self.ticket), self.generation, coverage=[self.probe]):
            done = subprocess.run(self.holder_argv(self.name(), payload, wait=2), capture_output=True, text=True,
                                  timeout=180)
        self.assertEqual(done.returncode, ch.REFUSED_EXIT, done.stderr)
        self.assertEqual(ch.parse_refusal(done.stderr).code, 'writer_wait_timeout')
        self.assertNotIn('PAYLOAD-RAN', done.stdout)
        done = subprocess.run(self.holder_argv(self.name(), payload, generation='runtime-generations/' + 'a' * 32),
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(done.returncode, ch.REFUSED_EXIT, done.stderr)
        self.assertEqual(ch.parse_refusal(done.stderr).code, 'stale_generation')
        self.assertNotIn('PAYLOAD-RAN', done.stdout)

    def test_unsafe_containers_are_refused_explicitly(self):
        payload = ['/bin/sh', '-c', 'echo PAYLOAD-RAN']
        done = subprocess.run(self.holder_argv(self.name(), payload, extra=['--cap-add', 'SYS_PTRACE']),
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(done.returncode, ch.REFUSED_EXIT, done.stderr)
        self.assertEqual(ch.parse_refusal(done.stderr).code, ch.PTRACE_CAPABLE)
        self.assertNotIn('PAYLOAD-RAN', done.stdout)
        _, holder = ch.holder_command(self.ticket, '.', USER, payload)
        root = [('0:0' if value == USER else value) for value in holder]
        done = subprocess.run(self.holder_argv(self.name(), payload, command=root), capture_output=True, text=True,
                              timeout=180)
        self.assertEqual(done.returncode, ch.REFUSED_EXIT, done.stderr)
        self.assertEqual(ch.parse_refusal(done.stderr).code, ch.PAYLOAD_PRIVILEGED)
        self.assertNotIn('PAYLOAD-RAN', done.stdout)
        done = subprocess.run(self.holder_argv(self.name(), payload, extra=['--init']),  # tini as PID 1
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(done.returncode, ch.REFUSED_EXIT, done.stderr)
        self.assertEqual(ch.parse_refusal(done.stderr).code, ch.NOT_PID1)
        self.assertNotIn('PAYLOAD-RAN', done.stdout)
        self.assert_transition()


if __name__ == '__main__':
    unittest.main()
