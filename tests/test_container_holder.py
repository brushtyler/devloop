"""PID 1 holder of the writer gate inside a container (#45, part b).

Real processes throughout. The PID namespace of a container is simulated with
``setpriv`` and ``unshare --pid --fork --mount-proc`` when the tests run as root;
tests/test_container_holder_docker.py covers real containers.
"""
import json
import os
from pathlib import Path
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import doc_contracts as docs
import ssdlc_container_holder as ch
import ssdlc_writer_gate as wg
from ssdlc_writer_gate import GateError, open_gate, transition


ROOT = Path(__file__).resolve().parents[1]
HOLDER = ROOT / ch.HOLDER
# Docker's default capability set: no CAP_SYS_PTRACE (bit 19).
DOCKER_STATUS = '\n'.join(['Name:\tpython3', 'CapInh:\t0000000000000000', 'CapPrm:\t00000000a80425fb',
                           'CapEff:\t00000000a80425fb', 'CapBnd:\t00000000a80425fb', 'CapAmb:\t0000000000000000'])

# Holds LOCK_SH like PID 1 and runs the payload through ``supervise``, without the
# UID change (which needs root): descriptor hygiene and signals do not depend on it.
SUPERVISOR = r'''
import sys
import ssdlc_container_holder as ch, ssdlc_writer_gate as wg
from ssdlc_writer_gate import GateError
ticket, mode, payload = sys.argv[1], sys.argv[2], sys.argv[3:]
def drop(uid, gid):
    if mode == 'fail':
        raise PermissionError('simulated')
gate = wg.open_gate(ticket, create=False)
ch.acquire(gate, ticket, '.')
print('HOLDING', flush=True)
try:
    sys.exit(ch.supervise(gate, 65534, 65534, payload, drop=drop))
except GateError as exc:
    sys.exit(ch.refuse(exc, sys.stdout))
'''

# A same-UID payload: it inherits no gate descriptor, and even its own opening of
# the gate cannot unlock PID 1's open file description or take LOCK_EX.
SAME_UID_PAYLOAD = r'''
import fcntl, json, os, sys
target = os.stat(sys.argv[1])
inherited, others = [], []
for name in os.listdir('/proc/self/fd'):
    fd = int(name)
    try:
        info = os.fstat(fd)
    except OSError:
        continue
    if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
        inherited.append(fd)
    if fd > 2:
        others.append(fd)
        for op in (fcntl.LOCK_UN, fcntl.LOCK_EX | fcntl.LOCK_NB):
            try:
                fcntl.flock(fd, op)
            except OSError:
                pass
own = os.open(sys.argv[1], os.O_RDONLY)
fcntl.flock(own, fcntl.LOCK_UN)
try:
    fcntl.flock(own, fcntl.LOCK_EX | fcntl.LOCK_NB)
    exclusive = True
except BlockingIOError:
    exclusive = False
print(json.dumps(dict(inherited=inherited, others=others, exclusive=exclusive)), flush=True)
sys.stdin.readline()
'''

# The real payload, unprivileged, in the simulated container namespace.
PAYLOAD = r'''
import fcntl, json, os, signal, subprocess, sys
gate, anchor, scratch = sys.argv[1:4]
status = dict(line.split(':', 1) for line in open('/proc/self/status') if ':' in line)
def attempt(action):
    try:
        action()
        return 'allowed'
    except OSError as exc:
        return type(exc).__name__
result = dict(pid=os.getpid(), uid=os.getresuid(), gid=os.getresgid(), groups=os.getgroups(),
              caps={k: status[k].strip() for k in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')},
              no_new_privs=status['NoNewPrivs'].strip(),
              open_gate=attempt(lambda: os.open(gate, os.O_RDONLY)),
              open_anchor=attempt(lambda: os.open(anchor, os.O_RDONLY)),
              holder_fds=attempt(lambda: os.listdir('/proc/1/fd')),
              signal_holder=attempt(lambda: os.kill(1, signal.SIGKILL)))
fds = []
for name in os.listdir('/proc/self/fd'):
    fd = int(name)
    try:
        os.fstat(fd)
    except OSError:
        continue
    if fd > 2:
        fds.append(fd)
        for op in (fcntl.LOCK_UN, fcntl.LOCK_EX | fcntl.LOCK_NB):
            try:
                fcntl.flock(fd, op)
            except OSError:
                pass
result['fds'] = fds
# A daemon that outlives the payload: only PID 1 ending the namespace stops it.
subprocess.Popen(['/bin/sh', '-c', 'while :; do echo x >> "$0"; sleep 0.05; done', scratch + '/beat'],
                 start_new_session=True)
print(json.dumps(result), flush=True)
sys.stdin.readline()
'''


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def line(stream, timeout=30):
    ready, _, _ = select.select([stream], [], [], timeout)
    return stream.readline().decode().strip() if ready else None


class TicketCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.ticket = self.base / '.ssdlc' / 'T-1'
        self.ticket.mkdir(parents=True)
        fd = os.open(self.ticket, os.O_RDONLY)
        try:
            wg.check_filesystem(fd)
        except GateError as exc:
            self.skipTest(f'temporary directory cannot host the gate: {exc}')
        finally:
            os.close(fd)
        self.gate = self.ticket / wg.GATE_NAME
        self.state = self.ticket / 'state.json'
        self.state.write_text(json.dumps({'ticket_id': 'T-1', 'status': 'IN_PROGRESS'}))

    def initialize(self):
        open_gate(str(self.ticket)).close()

    def generation(self):
        return ch.current_generation(self.ticket)

    def assert_transition(self, code=None):
        if code is None:
            with transition(str(self.ticket), self.generation) as proof:
                self.assertEqual(proof.check()['kind'], 'writer-gate-exclusive-v1')
            return
        with self.assertRaises(GateError) as caught:
            with transition(str(self.ticket), self.generation):
                pass
        self.assertEqual(caught.exception.code, code)

    def read_lock_held(self):
        gate = open_gate(str(self.ticket), create=False)
        try:
            return any(held['mode'] == 'READ' for held in wg._kernel_holders(gate))
        finally:
            gate.close()


class HostRequirementTests(unittest.TestCase):
    def test_docker_default_capabilities_are_accepted(self):
        ch.check_host_requirements(DOCKER_STATUS)

    def test_ptrace_capability_is_refused_explicitly(self):
        for field in ('CapBnd', 'CapEff', 'CapInh', 'CapAmb'):
            # 0xa80c25fb is Docker's default set plus bit 19, CAP_SYS_PTRACE.
            status = '\n'.join(f'{field}:\t00000000a80c25fb' if row.startswith(field + ':') else row
                               for row in DOCKER_STATUS.splitlines())
            with self.subTest(field=field), self.assertRaises(GateError) as caught:
                ch.check_host_requirements(status)
            self.assertEqual(caught.exception.code, ch.PTRACE_CAPABLE)
            self.assertIn(field, caught.exception.detail)

    def test_unreadable_capabilities_are_refused(self):
        with self.assertRaises(GateError) as caught:
            ch.check_host_requirements('Name:\tpython3\n')
        self.assertEqual(caught.exception.code, ch.PTRACE_CAPABLE)

    def test_non_linux_hosts_are_refused(self):
        with mock.patch.object(wg.sys, 'platform', 'darwin'), self.assertRaises(GateError) as caught:
            ch.check_host_requirements(DOCKER_STATUS)
        self.assertEqual(caught.exception.code, 'unsupported_platform')

    def test_root_payload_is_refused_explicitly(self):
        for user in (None, '0:0', '0:1000', '1000:0'):
            with self.subTest(user=user), self.assertRaises(GateError) as caught:
                ch.parse_payload_user(user)
            self.assertEqual(caught.exception.code, ch.PAYLOAD_PRIVILEGED)
        for user in ('root', 'nobody:nogroup', '1000', '-1:5'):
            with self.subTest(user=user), self.assertRaises(GateError) as caught:
                ch.parse_payload_user(user)
            self.assertEqual(caught.exception.code, 'invalid_payload_user')
        self.assertEqual(ch.parse_payload_user('65534:65534'), (65534, 65534))
        with self.assertRaises(GateError) as caught:
            ch.holder_command('/t', '.', '0:0', ['true'])
        self.assertEqual(caught.exception.code, ch.PAYLOAD_PRIVILEGED)

    def test_holder_command_starts_the_holder_without_init(self):
        options, command = ch.holder_command('/repo/.ssdlc/T-1', '.', '1000:1000', ['sleep', 5],
                                             identity=dict(device=2049, inode=17))
        self.assertEqual(options, ['--entrypoint', 'python3'])
        self.assertEqual(command, ['-I', '/opt/ssdlc/ssdlc_container_holder.py', '--ticket-root', '/repo/.ssdlc/T-1',
                                   '--generation', '"."', '--payload-user', '1000:1000',
                                   '--gate-identity', '2049:17', '--', 'sleep', '5'])
        self.assertNotIn('--init', options + command)

    def test_holder_refuses_outside_pid_1_before_any_payload(self):
        with tempfile.TemporaryDirectory() as scratch:
            marker = Path(scratch) / 'ran'
            done = subprocess.run([sys.executable, '-I', str(HOLDER), '--ticket-root', scratch, '--generation', '"."',
                                   '--payload-user', '65534:65534', '--', 'touch', str(marker)],
                                  capture_output=True, text=True, timeout=60, cwd='/')
            self.assertEqual(done.returncode, ch.REFUSED_EXIT, done.stderr)
            self.assertEqual(ch.parse_refusal(done.stderr).code, ch.NOT_PID1)
            self.assertFalse(marker.exists())
            done = subprocess.run([sys.executable, '-I', str(HOLDER), '--ticket-root', scratch, '--generation', '"."',
                                   '--payload-user', '0:0', '--', 'touch', str(marker)],
                                  capture_output=True, text=True, timeout=60, cwd='/')
            self.assertEqual(ch.parse_refusal(done.stderr).code, ch.PAYLOAD_PRIVILEGED)
            self.assertFalse(marker.exists())

    def test_refusal_report_round_trip(self):
        import io
        stream = io.StringIO()
        self.assertEqual(ch.refuse(GateError('stale_generation', 'moved'), stream), ch.REFUSED_EXIT)
        refused = ch.parse_refusal('payload noise\n' + stream.getvalue())
        self.assertEqual((refused.code, refused.detail), ('stale_generation', 'moved'))
        self.assertIsNone(ch.parse_refusal('no refusal here'))
        self.assertEqual(ch.parse_refusal(ch.REFUSED_PREFIX + '{').code, 'holder_report_invalid')


class RunningContainerProbeTests(unittest.TestCase):
    def test_probe_lists_only_the_run_and_never_signals(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, 'abc\ndef\n', '')
        reason = ch.running_containers('ssdlc.run_id=R-1', run=run)()
        self.assertIn('2 container(s) labelled ssdlc.run_id=R-1', reason)
        [argv] = calls
        self.assertEqual(argv[:3], ['docker', 'ps', '-a'])
        self.assertIn('label=ssdlc.run_id=R-1', argv)
        self.assertTrue({'status=created', 'status=running', 'status=paused', 'status=restarting'} <= set(argv))
        self.assertFalse({'kill', 'rm', 'stop'} & set(argv))

    def test_no_container_and_docker_failures(self):
        empty = ch.running_containers('k=v', run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, '\n', ''))
        self.assertIsNone(empty())

        def broken(argv, **kwargs):
            raise subprocess.CalledProcessError(1, argv)
        self.assertIn('could not list', ch.running_containers('k=v', run=broken)())
        for label in ('', 'novalue', '=v', None):
            with self.subTest(label=label), self.assertRaises(GateError):
                ch.running_containers(label)


class HoldTests(TicketCase):
    def test_new_gate_is_private_and_exposed_gates_are_refused(self):
        self.initialize()
        self.assertEqual(stat.S_IMODE(self.gate.stat().st_mode), 0o600)
        if os.geteuid() == 0:
            os.chmod(self.gate, 0o640)  # the anchor is the same inode
        with self.assertRaises(GateError) as caught:
            ch.hold(str(self.ticket), '.')
        self.assertEqual(caught.exception.code, ch.GATE_EXPOSED)
        self.assertFalse(self.read_lock_held())

    def test_holder_never_creates_a_missing_gate(self):
        with self.assertRaises(GateError) as caught:
            ch.hold(str(self.ticket), '.')
        self.assertEqual(caught.exception.code, 'gate_missing')
        self.assertEqual(os.listdir(self.ticket), ['state.json'])

    def test_unsupported_filesystem_is_refused(self):
        self.initialize()
        with mock.patch.object(wg, 'filesystem_type', return_value=0x6969), self.assertRaises(GateError) as caught:
            ch.hold(str(self.ticket), '.')
        self.assertEqual(caught.exception.code, 'unsupported_filesystem')

    def test_generation_is_rechecked_under_the_lock(self):
        self.initialize()
        self.assertEqual(self.generation(), '.')
        gate = open_gate(str(self.ticket), create=False)
        self.addCleanup(gate.close)
        with self.assertRaises(GateError) as caught:
            ch.acquire(gate, str(self.ticket), 'runtime-generations/' + 'a' * 32)
        self.assertEqual(caught.exception.code, 'stale_generation')
        (self.ticket / 'continuations').mkdir()
        (self.ticket / 'continuations' / 'pending.json').write_text('{}')
        with self.assertRaises(GateError) as caught:
            ch.acquire(gate, str(self.ticket), '.')
        self.assertEqual(caught.exception.code, 'generation_invalid')
        with self.assertRaises(GateError) as caught:
            ch.current_generation(self.ticket / 'missing')
        self.assertEqual(caught.exception.code, 'generation_invalid')

    def test_missing_or_invalid_state_is_refused_before_the_payload(self):
        self.initialize()
        foreign = self.base / 'foreign.json'
        foreign.write_text(json.dumps({'ticket_id': 'T-1'}))
        cases = dict(missing=None, truncated='{"ticket_id": "T-', null='null', array='[]',
                     foreign_ticket=json.dumps({'ticket_id': 'T-2'}), symlink=foreign)
        if os.geteuid() != 0:
            # Without root the gate cannot be root-owned, so check_private would refuse
            # first; test_new_gate_is_private_and_exposed_gates_are_refused covers it.
            patcher = mock.patch.object(ch, 'check_private')
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, content in cases.items():
            with self.subTest(name):
                self.state.unlink(missing_ok=True)
                if isinstance(content, Path):
                    self.state.symlink_to(content)
                elif content is not None:
                    self.state.write_text(content)
                with self.assertRaises(GateError) as caught:
                    ch.hold(str(self.ticket), '.')
                self.assertEqual(caught.exception.code, 'generation_invalid')
                self.assertFalse(self.read_lock_held())

    def test_shared_lock_excludes_transitions_until_closed(self):
        self.initialize()
        gate = open_gate(str(self.ticket), create=False)
        ch.acquire(gate, str(self.ticket), '.')
        self.assertTrue(self.read_lock_held())
        self.assert_transition('writers_active')
        gate.close()
        self.assert_transition()

    def test_wait_for_the_shared_lock_is_bounded(self):
        self.initialize()
        gate = open_gate(str(self.ticket), create=False)
        self.addCleanup(gate.close)
        with transition(str(self.ticket), self.generation):
            with self.assertRaises(GateError) as caught:
                ch.acquire(gate, str(self.ticket), '.', timeout=0.2)
        self.assertEqual(caught.exception.code, 'writer_wait_timeout')
        with self.assertRaises(GateError):
            ch.acquire(gate, str(self.ticket), '.', timeout=wg.MAX_WAIT + 1)


class SupervisorTests(TicketCase):
    def start(self, mode, payload):
        self.initialize()
        child = subprocess.Popen([sys.executable, '-c', SUPERVISOR, str(self.ticket), mode, *payload], cwd=ROOT,
                                 env=dict(os.environ, PYTHONPATH=str(ROOT)), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, bufsize=0)

        def stop():
            if child.poll() is None:
                child.kill()
            child.wait()
            child.stdin.close()
            child.stdout.close()
        self.addCleanup(stop)
        self.assertEqual(line(child.stdout), 'HOLDING')
        return child

    def test_payload_never_sees_the_gate_descriptor_nor_releases_the_lock(self):
        child = self.start('keep', [sys.executable, '-c', SAME_UID_PAYLOAD, str(self.gate)])
        report = json.loads(line(child.stdout))
        self.assertEqual(report['inherited'], [], 'the gate descriptor reached the payload')
        self.assertEqual(report['others'], [], 'the payload inherited descriptors besides stdio')
        self.assertFalse(report['exclusive'])
        self.assertTrue(self.read_lock_held())
        self.assert_transition('writers_active')
        child.stdin.write(b'\n')
        self.assertEqual(child.wait(timeout=30), 0)
        self.assert_transition()

    def test_signals_are_forwarded_to_the_payload(self):
        child = self.start('keep', ['sleep', '60'])
        time.sleep(0.2)
        child.send_signal(signal.SIGTERM)
        self.assertEqual(child.wait(timeout=30), 128 + signal.SIGTERM)
        self.assert_transition()

    def test_failed_privilege_drop_never_runs_the_payload(self):
        marker = self.base / 'ran'
        child = self.start('fail', ['touch', str(marker)])
        self.assertEqual(child.wait(timeout=30), ch.REFUSED_EXIT)
        self.assertEqual(ch.parse_refusal(child.stdout.read().decode()).code, ch.DROP_FAILED)
        self.assertFalse(marker.exists())
        self.assert_transition()


def _namespace_prefix():
    """``setpriv``/``unshare`` simulating a container's PID 1, or None when unavailable."""
    if os.geteuid() != 0 or not shutil.which('setpriv') or not shutil.which('unshare'):
        return None
    prefix = ['setpriv', '--bounding-set', '-sys_ptrace', 'unshare', '--pid', '--fork', '--mount-proc']
    try:
        probe = subprocess.run(prefix + [sys.executable, '-c', 'import os; print(os.getpid())'],
                               capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return prefix if probe.returncode == 0 and probe.stdout.strip() == '1' else None


class NamespaceTests(TicketCase):
    """The holder as PID 1 of a PID namespace, as root, with an unprivileged payload."""

    @classmethod
    def setUpClass(cls):
        cls.prefix = _namespace_prefix()
        cls.python = '/usr/bin/python3' if os.access('/usr/bin/python3', os.X_OK) else None

    def setUp(self):
        if self.prefix is None or self.python is None:
            self.skipTest('needs root, setpriv, unshare --pid and /usr/bin/python3')
        super().setUp()
        # Only the gate's own 0600 mode stands between the payload and the gate.
        for path in (self.base, self.base / '.ssdlc', self.ticket):
            os.chmod(path, 0o755)
        self.scratch = self.base / 'scratch'
        self.scratch.mkdir(mode=0o777)
        os.chmod(self.scratch, 0o777)
        self.initialize()

    def holder(self, generation='"."', payload=None, prefix=None):
        payload = payload or [self.python, '-I', '-c', PAYLOAD, str(self.gate),
                              str(self.ticket / wg.ANCHOR_NAME), str(self.scratch)]
        child = subprocess.Popen((prefix or self.prefix) + [
            sys.executable, '-I', str(HOLDER), '--ticket-root', str(self.ticket), '--generation', generation,
            '--payload-user', '65534:65534', '--', *payload],
            cwd='/', stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

        def stop():
            if child.poll() is None:
                child.kill()
            child.wait()
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()
        self.addCleanup(stop)
        return child

    def test_pid_1_keeps_the_lock_from_an_unprivileged_payload(self):
        child = self.holder()
        output = line(child.stdout)
        self.assertIsNotNone(output, child.stderr.read().decode() if child.poll() is not None else 'no report')
        report = json.loads(output)
        self.assertNotEqual(report['pid'], 1)
        self.assertEqual((report['uid'], report['gid'], report['groups']), ([65534] * 3, [65534] * 3, []))
        self.assertEqual(set(report['caps'].values()), {'0000000000000000'})
        self.assertEqual(report['no_new_privs'], '1')
        for attempt in ('open_gate', 'open_anchor', 'holder_fds', 'signal_holder'):
            self.assertEqual(report[attempt], 'PermissionError', attempt)
        self.assertEqual(report['fds'], [], 'the payload inherited descriptors besides stdio')
        self.assertTrue(self.read_lock_held())
        self.assert_transition('writers_active')
        beat = self.scratch / 'beat'
        self.assertTrue(wait_for(beat.exists))
        child.stdin.write(b'\n')
        self.assertEqual(child.wait(timeout=30), 0)
        self.assertIn(ch.READY_PREFIX, child.stderr.read().decode())
        size = beat.stat().st_size
        time.sleep(0.5)
        self.assertEqual(beat.stat().st_size, size, 'a payload daemon outlived PID 1')
        self.assert_transition()

    def test_stale_generation_never_starts_the_payload(self):
        marker = self.scratch / 'ran'
        child = self.holder('"runtime-generations/' + 'a' * 32 + '"', ['touch', str(marker)])
        self.assertEqual(child.wait(timeout=30), ch.REFUSED_EXIT)
        self.assertEqual(ch.parse_refusal(child.stderr.read().decode()).code, 'stale_generation')
        self.assertFalse(marker.exists())
        self.assert_transition()

    def test_missing_state_never_starts_the_payload(self):
        marker = self.scratch / 'ran'
        self.state.unlink()
        child = self.holder(payload=['touch', str(marker)])
        self.assertEqual(child.wait(timeout=30), ch.REFUSED_EXIT)
        self.assertEqual(ch.parse_refusal(child.stderr.read().decode()).code, 'generation_invalid')
        self.assertFalse(marker.exists())

    def test_ptrace_capable_container_is_refused(self):
        if not ch.capabilities()['CapBnd'] >> ch.CAP_SYS_PTRACE & 1:
            self.skipTest('this host already lacks CAP_SYS_PTRACE')
        marker = self.scratch / 'ran'
        child = self.holder(payload=['touch', str(marker)], prefix=self.prefix[3:])  # without setpriv
        self.assertEqual(child.wait(timeout=30), ch.REFUSED_EXIT)
        self.assertEqual(ch.parse_refusal(child.stderr.read().decode()).code, ch.PTRACE_CAPABLE)
        self.assertFalse(marker.exists())


class DocumentationTests(unittest.TestCase):
    def test_container_holder_is_documented(self):
        docs.assert_documented(self, docs.user_facing_docs(), [
            'ssdlc_container_holder.py', 'PID 1', 'root-owned 0600', 'CONTAINER_PAYLOAD_PRIVILEGED',
            'CONTAINER_PTRACE_CAPABLE', 'no_new_privs', 'O_CLOEXEC', 'SSDLC_DOCKER_TESTS',
            'test_container_holder_docker.py',
        ], issues=(45,))


if __name__ == '__main__':
    unittest.main()
