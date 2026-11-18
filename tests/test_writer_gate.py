"""Writer gate for automatic fencing (#45 part a): real processes, real kernel flock."""
import contextlib
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import doc_contracts as docs
import ssdlc_writer_gate as wg
from ssdlc_locks import current_lock, transition_lock
from ssdlc_supervisor import Proc
from ssdlc_tasks import TaskError, ticket_lock
from ssdlc_writer_gate import GateError, handoff, holders, open_gate, run_host, transition, writer


ROOT = Path(__file__).resolve().parents[1]
UNVERIFIABLE = 'HOST_TREE_UNVERIFIABLE'

WRITER = r'''
import os, sys
from pathlib import Path
from ssdlc_writer_gate import GateError, writer
ticket, selector, expected, timeout = sys.argv[1], Path(sys.argv[2]), sys.argv[3], float(sys.argv[4])
try:
    with writer(ticket, expected, selector.read_text, timeout=timeout) as lease:
        print('READY', os.getpid(), flush=True)
        sys.stdin.read()
        lease.check()
        print('DONE', flush=True)
except GateError as exc:
    print('REFUSED', exc.code, flush=True)
    sys.exit(3)
'''

HANDOFF = r'''
import sys
from pathlib import Path
from ssdlc_locks import current_lock
from ssdlc_tasks import ticket_lock
from ssdlc_writer_gate import handoff, writer
ticket, selector = Path(sys.argv[1]), Path(sys.argv[2])
with ticket_lock(ticket / 'ticket.lock') as canonical, ticket_lock(ticket / 'scheduler.lock') as scheduler:
    with writer(str(ticket), 'gen-1', selector.read_text, timeout=5) as lease:
        def publish(proof):
            proof.check()
            selector.write_text('gen-2')
            return 'gen-2'
        new = handoff(lease, publish, held=(canonical, scheduler))
        lease.check()
        assert current_lock(ticket / 'ticket.lock') is canonical
        assert current_lock(ticket / 'scheduler.lock') is scheduler
        print('HANDOFF', new, lease.generation, flush=True)
'''

# A controller that runs one trusted host command (its remaining arguments).
CONTROLLER = r'''
import sys
from pathlib import Path
from ssdlc_writer_gate import run_host, writer
ticket, selector = sys.argv[1], Path(sys.argv[2])
with writer(ticket, 'gen-1', selector.read_text, timeout=5) as lease:
    run_host(lease, sys.argv[3:])
'''

# A host command that keeps writing, without daemonizing.
BEATING = ("import os, sys, time\n"
           "while True:\n"
           "    with open(sys.argv[1], 'a') as out:\n"
           "        out.write(str(os.getpid()) + '\\n')\n"
           "    time.sleep(0.05)\n")


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


class GateCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.ticket = Path(temp.name).resolve() / '.ssdlc' / 'T-1'
        self.ticket.mkdir(parents=True)
        fd = os.open(self.ticket, os.O_RDONLY)
        try:
            wg.check_filesystem(fd)
        except GateError as exc:
            self.skipTest(f'temporary directory cannot host the gate: {exc}')
        finally:
            os.close(fd)
        self.selector = self.ticket / 'selector'
        self.selector.write_text('gen-1')
        self.gate = self.ticket / wg.GATE_NAME

    def read(self):
        return self.selector.read_text()

    def spawn(self, generation, timeout=10):
        child = subprocess.Popen([sys.executable, '-c', WRITER, str(self.ticket), str(self.selector), generation,
                                  str(timeout)], cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT)),
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)

        def stop():
            if child.poll() is None:
                child.kill()
            child.wait()
            for stream in (child.stdin, child.stdout):
                stream.close()
        self.addCleanup(stop)
        return child

    def line(self, child, timeout=15):
        ready, _, _ = select.select([child.stdout], [], [], timeout)
        return child.stdout.readline().decode().strip() if ready else None

    def ready(self, generation='gen-1'):
        child = self.spawn(generation)
        self.assertEqual(self.line(child).split()[0], 'READY')
        return child

    def finish(self, child):
        child.stdin.close()
        self.assertEqual(self.line(child), 'DONE')
        self.assertEqual(child.wait(timeout=15), 0)

    def require_subreaper(self):
        reason = wg.host_tree_support()
        if reason:
            self.skipTest(f'host trees cannot be proven here: {reason}')

    def assert_refused(self, code, **kwargs):
        with self.assertRaises(GateError) as raised:
            with transition(str(self.ticket), self.read, **kwargs):
                self.fail('a refused gate must never yield a proof')
        self.assertEqual(raised.exception.code, code)
        return raised.exception


class ExclusionTests(GateCase):
    def test_live_shared_writer_blocks_the_exclusive_proof(self):
        child = self.ready()
        inode = self.gate.stat().st_ino
        refused = self.assert_refused('writers_active')
        self.assertIn('READ pid', refused.detail)
        self.assertIn('registered writer', refused.detail)
        # The serialization lock is not a quiescence proof.
        with ticket_lock(self.ticket / 'ticket.lock'):
            self.assert_refused('writers_active')
        self.finish(child)
        with transition(str(self.ticket), self.read) as proof:
            receipt = proof.check()
            self.assertEqual(receipt['kind'], 'writer-gate-exclusive-v1')
            self.assertEqual(receipt['inode'], inode)
            self.assertEqual(proof.generation, 'gen-1')
            self.assertIs(current_lock(self.gate), proof.lease)
            self.assertFalse(proof.lease.shared)
        self.assertEqual(self.gate.stat().st_ino, inode)
        with self.assertRaisesRegex(GateError, 'closed'):
            proof.check()

    def test_registration_cannot_slip_between_check_and_publication(self):
        with transition(str(self.ticket), self.read) as proof:
            proof.check()  # source revalidation happens under the exclusive lock
            old = self.spawn('gen-1', timeout=20)
            self.assertIsNone(self.line(old, timeout=0.5), 'a writer registered during the transition')
            self.selector.write_text('gen-2')  # selector publication
        self.assertEqual(self.line(old), 'REFUSED stale_generation')
        self.assertEqual(old.wait(timeout=15), 3)
        new = self.ready('gen-2')
        self.assert_refused('writers_active')
        self.finish(new)

    def test_previous_generation_writer_refuses_to_write(self):
        with writer(str(self.ticket), 'gen-1', self.read) as lease:
            self.assertEqual(lease.check()['kind'], 'writer-gate-shared-v1')
            self.selector.write_text('gen-2')  # a non-conforming publisher
            with self.assertRaises(GateError) as raised:
                lease.check()
            self.assertEqual(raised.exception.code, 'stale_generation')
        with self.assertRaises(GateError) as raised:
            with writer(str(self.ticket), 'gen-1', self.read):
                self.fail('an old-generation writer must not restart')
        self.assertEqual(raised.exception.code, 'stale_generation')
        with self.assertRaises(GateError) as raised:
            lease.check()
        self.assertEqual(raised.exception.code, 'lease_lost')

    def test_conversion_is_impossible_in_process(self):
        with writer(str(self.ticket), 'gen-1', self.read) as lease:
            self.assertTrue(lease.lease.shared)
            self.assert_refused('writers_active')
            with self.assertRaisesRegex(TaskError, 'Shared lock'):
                with transition_lock(self.gate, (lease.lease,)):
                    self.fail('a shared lease cannot authorize a transition')

    def test_writer_wait_is_bounded_and_contention_is_logged(self):
        gate = open_gate(str(self.ticket))
        self.addCleanup(gate.close)
        fcntl.flock(gate.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        with self.assertLogs('ssdlc_writer_gate', 'WARNING') as logs, self.assertRaises(GateError) as raised:
            with writer(str(self.ticket), 'gen-1', self.read, timeout=0.2, poll=0.02):
                self.fail('writer must not register while a transition holds the gate')
        self.assertEqual(raised.exception.code, 'writer_wait_timeout')
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn('request=shared', '\n'.join(logs.output))
        with self.assertRaises(GateError) as raised:
            with writer(str(self.ticket), 'gen-1', self.read, timeout=wg.MAX_WAIT + 1):
                pass
        self.assertEqual(raised.exception.code, 'invalid_wait')

    def test_generation_must_be_serializable(self):
        with self.assertRaises(GateError) as raised:
            with writer(str(self.ticket), object(), self.read):
                self.fail('an unserializable generation cannot be compared across processes')
        self.assertEqual(raised.exception.code, 'invalid_generation')
        with self.assertRaises(GateError) as raised:
            with transition(str(self.ticket), object):
                self.fail('an unserializable generation never yields a proof')
        self.assertEqual(raised.exception.code, 'invalid_generation')

    def test_coverage_probes_refuse_uncovered_execution_trees(self):
        self.assert_refused('uncovered_execution', coverage=[lambda: 'container abc is not covered'])

        def broken():
            raise RuntimeError('docker unavailable')
        self.assert_refused('uncovered_execution', coverage=[broken])
        with transition(str(self.ticket), self.read, coverage=[lambda: None]) as proof:
            proof.check()


class CrashAndMetadataTests(GateCase):
    def test_sigkill_of_last_writer_releases_without_removing_the_gate(self):
        record = self.ready()
        inode, content = self.gate.stat().st_ino, self.gate.read_bytes()
        record.send_signal(signal.SIGKILL)
        record.wait(timeout=15)
        self.assertEqual((self.gate.stat().st_ino, self.gate.read_bytes()), (inode, content))
        residual = list((self.ticket / wg.REGISTRY_NAME).glob('w-*.json'))
        self.assertEqual(len(residual), 1)
        with transition(str(self.ticket), self.read) as proof:
            found = holders(proof._gate)
            self.assertEqual(found['stale_records'], [residual[0].name])
            self.assertEqual([h['mode'] for h in found['kernel']], ['WRITE'])
        self.assertEqual(self.gate.stat().st_ino, inode)

    def test_residual_metadata_or_reused_pid_neither_authorizes_nor_blocks(self):
        me = wg.self_identity()[0]
        gate = open_gate(str(self.ticket))
        self.addCleanup(gate.close)
        reused = dict(me, start_ticks=me['start_ticks'] + 1)  # same PID, another process life
        wg._register(gate, reused)
        wg._register(gate, dict(me, boot_id='another-boot'))
        wg._register(gate, me)  # live identity, but no kernel lock
        self.assertFalse(wg.is_live(reused))
        with transition(str(self.ticket), self.read) as proof:
            found = holders(proof._gate)
            self.assertEqual(len(found['stale_records']), 2)
            self.assertEqual(found['host_markers'], [])
            self.assertEqual([h['registered'] for h in found['kernel']], [True])  # only the proof's own flock
        # A forged receipt is not ownership either.
        with writer(str(self.ticket), 'gen-1', self.read) as lease:
            with self.assertRaisesRegex(TaskError, 'no current'):
                replace(lease.lease).validate(self.gate)
            self.assert_refused('writers_active')

    def test_unreadable_registry_entry_refuses_instead_of_being_ignored(self):
        with transition(str(self.ticket), self.read):
            pass
        registry = self.ticket / wg.REGISTRY_NAME
        (registry / wg.host_marker_name(wg.generation_key('gen-1'))).write_text('{truncated')
        self.assertIn('registry_corrupt', str(self.assert_refused('registry_corrupt')))
        for entry in registry.iterdir():
            entry.unlink()
        (registry / 'h-foreign.json').symlink_to(self.selector)
        self.assert_refused('registry_corrupt')


class GateIdentityTests(GateCase):
    def record(self, inode):
        return json.dumps(dict(kind=wg.KIND, schema_version=1, inode=inode, created_at=0.0, nonce='x'))

    def assert_all_refuse(self, code):
        self.assert_refused(code)
        with self.assertRaises(GateError) as raised:
            with writer(str(self.ticket), 'gen-1', self.read):
                self.fail('a refused gate must not admit writers')
        self.assertEqual(raised.exception.code, code)

    def test_gate_is_created_once_and_reused(self):
        with transition(str(self.ticket), self.read):
            pass
        inode = self.gate.stat().st_ino
        self.assertEqual(json.loads(self.gate.read_text())['inode'], inode)
        with writer(str(self.ticket), 'gen-1', self.read) as lease:
            self.assertEqual(lease.identity['inode'], inode)
        self.assertEqual(self.gate.stat().st_ino, inode)
        self.assertEqual([p.name for p in self.ticket.iterdir() if p.name.startswith('.')], [])

    def test_symlinked_gate_is_refused(self):
        target = self.ticket / 'elsewhere'
        target.write_text(self.record(0))
        self.gate.symlink_to(target)
        self.assert_all_refuse('gate_symlink')

    def test_substituted_gate_is_refused(self):
        with transition(str(self.ticket), self.read):
            pass
        copy = self.ticket / 'copy'
        copy.write_bytes(self.gate.read_bytes())  # same record, another inode
        os.replace(copy, self.gate)
        self.assert_all_refuse('gate_replaced')

    def test_foreign_or_empty_gate_is_refused(self):
        self.gate.write_text('')
        self.assert_all_refuse('gate_corrupt')
        self.gate.unlink()
        self.gate.mkdir()
        self.assert_all_refuse('gate_not_regular')

    def test_bound_identity_and_replacement_under_a_live_lease(self):
        with writer(str(self.ticket), 'gen-1', self.read) as lease:
            bound = dict(lease.identity)
            fresh = self.ticket / 'fresh'
            fresh.write_text('pending')
            fresh.write_text(self.record(fresh.stat().st_ino))
            os.replace(fresh, self.gate)
            with self.assertRaises(GateError) as raised:
                lease.check()
            self.assertEqual(raised.exception.code, 'gate_replaced')
        self.assert_refused('gate_replaced', expected_identity=bound)

    def test_deleted_gate_under_a_live_writer_is_never_recreated(self):
        child = self.ready()
        anchor = self.ticket / wg.ANCHOR_NAME
        inode = self.gate.stat().st_ino
        self.assertEqual(anchor.stat().st_ino, inode)
        self.gate.unlink()
        # No bound identity: the anchor alone tells a deleted gate from a first creation.
        refused = self.assert_refused('gate_missing')
        self.assertIn('never recreated', refused.detail)
        self.assertFalse(self.gate.exists())
        anchor.unlink()  # even without the anchor, the registry shows a past initialization
        self.assert_all_refuse('gate_missing')
        self.assertFalse(self.gate.exists() or anchor.exists())
        child.stdin.close()
        self.assertEqual(self.line(child), 'REFUSED gate_missing')
        self.assertEqual(child.wait(timeout=15), 3)

    def test_missing_or_foreign_anchor_is_refused(self):
        with transition(str(self.ticket), self.read):
            pass
        anchor = self.ticket / wg.ANCHOR_NAME
        anchor.unlink()
        self.assert_all_refuse('gate_anchor_missing')
        anchor.write_bytes(self.gate.read_bytes())  # a copy is not the anchored inode
        self.assert_all_refuse('gate_replaced')

    def test_concurrent_first_initialization_converges_on_one_gate(self):
        children = [self.spawn('gen-1') for _ in range(4)]
        for child in children:
            self.assertEqual(self.line(child).split()[0], 'READY')
        inode = self.gate.stat().st_ino
        self.assertEqual((self.ticket / wg.ANCHOR_NAME).stat().st_ino, inode)
        self.assertEqual(json.loads(self.gate.read_text())['inode'], inode)
        for child in children:
            self.finish(child)

    def test_symlinked_or_relative_ticket_root_is_refused(self):
        link = self.ticket.parent / 'T-link'
        link.symlink_to(self.ticket)
        for root in (str(link), os.path.relpath(self.ticket)):
            with self.subTest(root=root), self.assertRaises(GateError) as raised:
                with transition(root, self.read):
                    self.fail('no proof through an unverified ticket root')
            self.assertEqual(raised.exception.code, 'ticket_root_invalid')

    def test_unsupported_filesystem_is_refused_with_a_reason(self):
        for magic, name in ((0x6969, 'nfs'), (0x65735546, 'fuse'), (0x794C7630, 'overlayfs'), (0x1234, 'unknown')):
            with self.subTest(name=name), mock.patch.object(wg, 'filesystem_type', return_value=magic):
                refused = self.assert_refused('unsupported_filesystem')
                self.assertIn(name, refused.detail)
        self.assertFalse(self.gate.exists(), 'an unsupported filesystem must not get a gate')


class HandoffTests(GateCase):
    def test_delegated_handoff_holding_ticket_lock_does_not_deadlock(self):
        done = subprocess.run([sys.executable, '-c', HANDOFF, str(self.ticket), str(self.selector)], cwd=ROOT,
                              env=dict(os.environ, PYTHONPATH=str(ROOT)), capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), 'HANDOFF gen-2 gen-2')
        self.assertEqual(self.read(), 'gen-2')

    def test_refused_handoff_resumes_on_the_unchanged_generation(self):
        other = self.ready()
        calls = []
        with ticket_lock(self.ticket / 'ticket.lock') as canonical, writer(str(self.ticket), 'gen-1', self.read) as lease:
            with self.assertRaises(GateError) as raised:
                handoff(lease, calls.append, held=(canonical,))
            self.assertEqual(raised.exception.code, 'writers_active')
            self.assertEqual((calls, lease.generation), ([], 'gen-1'))
            lease.check()
            self.assertIs(current_lock(self.ticket / 'ticket.lock'), canonical)
        self.finish(other)

    def test_failed_publication_does_not_restart_the_old_writer(self):
        def publish(proof):
            proof.check()
            self.selector.write_text('gen-2')
            raise RuntimeError('crash after selector replacement')
        with writer(str(self.ticket), 'gen-1', self.read) as lease:
            with self.assertRaises(GateError) as raised:
                handoff(lease, publish)
            self.assertEqual(raised.exception.code, 'stale_generation')
            self.assertIsInstance(raised.exception.__context__, RuntimeError)
            with self.assertRaises(GateError):
                lease.check()

    def test_handoff_rejects_changed_serialization_receipts(self):
        with ticket_lock(self.ticket / 'ticket.lock') as canonical:
            stale = replace(canonical)
            with writer(str(self.ticket), 'gen-1', self.read) as lease:
                with self.assertRaises(GateError) as raised:
                    handoff(lease, lambda proof: 'gen-2', held=(stale,))
                self.assertEqual(raised.exception.code, 'lease_lost')
                lease.check()
        self.assertEqual(self.read(), 'gen-1')

    def test_handoff_after_unprovable_host_commands_is_refused_and_resumes(self):
        calls = []
        with writer(str(self.ticket), 'gen-1', self.read) as lease:
            with mock.patch.object(wg, 'host_tree_support', return_value='simulated: no subreaper'):
                run_host(lease, ['true'])
            with self.assertRaises(GateError) as raised:
                handoff(lease, calls.append)
            self.assertEqual(raised.exception.code, UNVERIFIABLE)
            self.assertIn('simulated: no subreaper', raised.exception.detail)
            self.assertEqual((calls, lease.generation), ([], 'gen-1'))
            lease.check()

    def test_handoff_after_proven_host_commands_publishes(self):
        self.require_subreaper()

        def publish(proof):
            proof.check()
            self.selector.write_text('gen-2')
            return 'gen-2'
        with writer(str(self.ticket), 'gen-1', self.read) as lease:
            run_host(lease, ['true'])
            self.assertEqual(handoff(lease, publish), 'gen-2')
            lease.check()


class HostWriterTests(GateCase):
    def host_markers(self):
        return [json.loads(p.read_text()) for p in (self.ticket / wg.REGISTRY_NAME).glob('h-*.json')]

    def registry_files(self, prefix):
        return sorted((self.ticket / wg.REGISTRY_NAME).glob(f'{prefix}-*.json'))

    def kill_session(self, leader):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(leader, signal.SIGKILL)

    def kill(self, pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)

    def controller(self, *command):
        child = subprocess.Popen([sys.executable, '-c', CONTROLLER, str(self.ticket), str(self.selector), *command],
                                 cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT)))
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.addCleanup(lambda: child.poll() is None or child.wait())
        return child

    def test_host_commands_run_inside_the_lease_without_the_gate_descriptor(self):
        self.require_subreaper()
        probe = ("import os\n"
                 "links = []\n"
                 "for f in os.listdir('/proc/self/fd'):\n"
                 "    try:\n"
                 "        links.append(os.readlink('/proc/self/fd/' + f))\n"
                 "    except OSError:\n"
                 "        pass\n"
                 "print('leak' if any(l.endswith('writer.gate') for l in links) else 'clean')\n"
                 "print(os.getsid(0) == os.getppid(), os.getpgid(0) == os.getpid())")
        with writer(str(self.ticket), 'gen-1', self.read) as lease:
            done = run_host(lease, [sys.executable, '-c', probe], capture_output=True, text=True)
            # The supervisor leads its own session; the command leads its own group in it.
            self.assertEqual(done.stdout.split(), ['clean', 'True', 'True'])
            self.assertEqual(run_host(lease, ['false']).returncode, 1)
            self.assertEqual(run_host(lease, ['sh', '-c', 'kill -TERM $$']).returncode, -signal.SIGTERM)
        with self.assertRaises(GateError) as raised:
            run_host(lease, ['true'])
        self.assertEqual(raised.exception.code, 'lease_lost')

    def test_finished_host_trees_are_proven_by_their_subreaper(self):
        """Criterion 2: with every tree concluded, the kernel's proof allows the transition."""
        self.require_subreaper()
        with writer(str(self.ticket), 'gen-1', self.read) as lease:
            self.assertEqual(run_host(lease, ['true']).returncode, 0)
            run_host(lease, ['sh', '-c', 'exit 3'])
        [marker] = self.host_markers()  # one marker per generation
        self.assertEqual((marker['generation'], marker['trees']), (wg.generation_key('gen-1'), wg.TREE_PROTOCOL))
        trees = [json.loads(p.read_text()) for p in self.registry_files('t')]
        done = [json.loads(p.read_text()) for p in self.registry_files('c')]
        self.assertEqual([t['mechanism'] for t in trees], ['subreaper', 'subreaper'])
        self.assertEqual(sorted(t['tree'] for t in trees), sorted(c['tree'] for c in done))
        self.assertEqual(sorted(c['status'] for c in done), ['exit 0', 'exit 3'])
        with transition(str(self.ticket), self.read) as proof:
            proof.check()
            found = holders(proof._gate)['host_trees']
            self.assertEqual([(t['concluded'], t['supervisor_live']) for t in found], [(True, False)] * 2)

    def test_mechanism_unavailable_refuses_with_an_explicit_reason(self):
        """Criterion 3: without the subreaper the command runs, but fencing is refused and says why."""
        reason = 'simulated: PR_SET_CHILD_SUBREAPER unsupported'
        with writer(str(self.ticket), 'gen-1', self.read) as lease:
            with mock.patch.object(wg, 'host_tree_support', return_value=reason):
                done = run_host(lease, ['sh', '-c', 'echo ran'], capture_output=True, text=True)
            self.assertEqual((done.returncode, done.stdout), (0, 'ran\n'))
            if wg.host_tree_support() is None:
                run_host(lease, ['true'])  # a later proven tree does not clear the refusal
        [tree] = [json.loads(p.read_text()) for p in self.registry_files('t')
                  if json.loads(p.read_text())['mechanism'] is None]
        self.assertEqual(tree['reason'], reason)
        refused = self.assert_refused(UNVERIFIABLE)
        self.assertIn(f'ran without a kernel mechanism: {reason}', refused.detail)
        self.assertIn('manual path', refused.detail)
        # Only a publication outside the gate (the manual path) moves the generation;
        # the old records then stay as diagnostics and no longer refuse.
        self.selector.write_text('gen-2')
        with transition(str(self.ticket), self.read) as proof:
            proof.check()
            [diagnostic] = holders(proof._gate)['host_markers']
            self.assertEqual(diagnostic['generation'], wg.generation_key('gen-1'))
        # The availability check itself names the failing kernel interface.
        with mock.patch.object(wg, '_prctl', side_effect=OSError(22, 'Invalid argument')):
            self.assertEqual(wg.host_tree_support(), 'PR_GET_CHILD_SUBREAPER unavailable: Invalid argument')
        with mock.patch.object(wg.sys, 'platform', 'darwin'):
            self.assertEqual(wg.host_tree_support(), 'child subreapers need Linux')

    def test_tree_without_its_own_conclusion_refuses(self):
        self.require_subreaper()
        with writer(str(self.ticket), 'gen-1', self.read) as lease:
            run_host(lease, ['true'])
        [conclusion] = self.registry_files('c')
        record = json.loads(conclusion.read_text())
        # A supervisor that died before concluding leaves no record.
        conclusion.unlink()
        self.assertIn('ended without recording the proof', self.assert_refused(UNVERIFIABLE).detail)
        # A conclusion of another supervisor or generation is not this tree's proof.
        for forged in (dict(record, supervisor=dict(record['supervisor'], start_ticks=0)),
                       dict(record, generation=wg.generation_key('gen-0'))):
            with self.subTest(forged=forged):
                conclusion.write_text(json.dumps(forged))
                self.assert_refused(UNVERIFIABLE)
        conclusion.write_text(json.dumps(record))
        with transition(str(self.ticket), self.read) as proof:
            proof.check()
        # A tree record whose generation cannot be read counts as current.
        [tree] = self.registry_files('t')
        tree.write_text(json.dumps(dict(json.loads(tree.read_text()), generation='gen-1')))
        self.assertIn('has no readable generation', self.assert_refused(UNVERIFIABLE).detail)

    def test_marker_written_before_tree_records_refuses(self):
        with transition(str(self.ticket), self.read):
            pass
        key = wg.generation_key('gen-1')
        (self.ticket / wg.REGISTRY_NAME / wg.host_marker_name(key)).write_text(
            json.dumps(dict(role='host', generation=key, identity=wg.self_identity()[0])))
        self.assertIn('has no tree records', self.assert_refused(UNVERIFIABLE).detail)

    def test_marker_without_a_readable_generation_refuses(self):
        with transition(str(self.ticket), self.read):
            pass
        marker = self.ticket / wg.REGISTRY_NAME / 'h-legacy.json'
        marker.write_text(json.dumps(dict(role='host', identity=wg.self_identity()[0])))
        self.assert_refused(UNVERIFIABLE)

    def test_marker_with_an_undecodable_or_inconsistent_generation_refuses(self):
        registry = self.ticket / wg.REGISTRY_NAME
        current = wg.host_marker_name(wg.generation_key('gen-1'))
        identity = wg.self_identity()[0]
        cases = [(current, ''), (current, 'gen-1'), (current, '"gen-1" '), (current, wg.generation_key('gen-0')),
                 (wg.host_marker_name(wg.generation_key('gen-0')), ''),
                 ('h-other.json', wg.generation_key('gen-0'))]
        with transition(str(self.ticket), self.read):
            pass
        for name, generation in cases:
            with self.subTest(name=name, generation=generation):
                for entry in registry.iterdir():
                    entry.unlink()
                (registry / name).write_text(json.dumps(dict(role='host', generation=generation, identity=identity)))
                self.assert_refused(UNVERIFIABLE)
        # A well-formed marker of a past generation is history, not a refusal.
        for entry in registry.iterdir():
            entry.unlink()
        past = wg.generation_key('gen-0')
        (registry / wg.host_marker_name(past)).write_text(json.dumps(dict(role='host', generation=past,
                                                                          identity=identity)))
        with transition(str(self.ticket), self.read) as proof:
            proof.check()

    def test_preexisting_malformed_marker_stops_the_host_command(self):
        """The reviewer's case: h-<current digest>.json with generation '' never lets the command run."""
        ran = self.ticket.parent / 'ran'
        registry = self.ticket / wg.REGISTRY_NAME
        with transition(str(self.ticket), self.read):
            pass
        marker = registry / wg.host_marker_name(wg.generation_key('gen-1'))
        for generation in ('', wg.generation_key('gen-0')):
            with self.subTest(generation=generation):
                marker.write_text(json.dumps(dict(role='host', generation=generation,
                                                  identity=wg.self_identity()[0])))
                with writer(str(self.ticket), 'gen-1', self.read) as lease:
                    with self.assertRaises(GateError) as raised:
                        run_host(lease, [sys.executable, '-c', 'import sys; open(sys.argv[1], "w")', str(ran)])
                    self.assertEqual(raised.exception.code, 'registry_invalid')
                self.assertFalse(ran.exists(), 'the command ran on a malformed marker')
                self.assert_refused(UNVERIFIABLE)

    def test_grandchild_spawned_between_pid_listing_and_state_reads_still_refuses(self):
        """Criterion 1, the race of PR #127: a process scan misses the grandchild, the subreaper does not."""
        self.require_subreaper()
        work = self.ticket.parent
        started, go, spawned, beat = (work / name for name in ('started', 'go', 'grandchild', 'beat'))
        # The leader announces itself, waits for the scan to list PIDs, starts a writer
        # in its own process group and exits: nothing daemonizes or leaves the group.
        script = ('echo $$ > "$1.tmp" && mv "$1.tmp" "$1"; '
                  'while [ ! -e "$2" ]; do sleep 0.02; done; '
                  '(while :; do echo x >> "$4"; sleep 0.05; done) </dev/null >/dev/null 2>&1 & '
                  'echo $! > "$3.tmp" && mv "$3.tmp" "$3"; exit 0')
        controller = self.controller('sh', '-c', script, 'sh', str(started), str(go), str(spawned), str(beat))
        scan = {}
        try:
            self.assertTrue(wait_for(started.exists), 'the host command never started')
            leader = int(started.read_text())
            identity = wg.process_identity(leader)[0]
            proc = Proc()
            listed = proc.pids()            # 1. the scan lists the PIDs
        finally:
            go.touch()                      # the leader starts the grandchild and exits
        self.assertTrue(wait_for(spawned.exists) and wait_for(lambda: not wg.is_live(identity)))
        grandchild = int(spawned.read_text())
        self.addCleanup(self.kill, grandchild)
        members = []
        for pid in listed:                  # 2. the scan reads the states
            try:
                ident, state, _ = proc.stat(pid)
            except (OSError, ValueError, IndexError):
                continue
            if state != 'Z' and ident.pgid == leader:
                members.append(pid)
        self.assertNotIn(grandchild, listed)
        self.assertEqual(members, [], 'the scan believes the command group is empty')
        # The kernel reparented the orphan to the subreaper, which outlives its controller.
        supervisor = proc.stat(grandchild)[2]
        [marker] = self.host_markers()
        self.assertEqual(marker['identity']['pid'], supervisor)
        controller.send_signal(signal.SIGKILL)
        controller.wait(timeout=15)
        self.assertIsNotNone(wg.process_identity(grandchild), 'the grandchild must still be running')
        written = beat.stat().st_size if beat.exists() else 0
        self.assertTrue(wait_for(lambda: beat.exists() and beat.stat().st_size > written))
        # The gate never consults a process scan: the tree has no conclusion yet.
        with mock.patch.object(Proc, 'pids', side_effect=AssertionError('process scan used as evidence')):
            refused = self.assert_refused(UNVERIFIABLE)
        self.assertIn('cannot be proven finished', refused.detail)
        self.assertIn('supervisor still running', refused.detail)
        self.assertEqual(self.registry_files('c'), [])
        # Once the grandchild ends, the subreaper gets ECHILD and records it: the proof follows.
        self.kill(grandchild)
        self.assertTrue(wait_for(lambda: self.registry_files('c')), 'the subreaper never concluded')
        self.assertTrue(wait_for(lambda: not wg.is_live(marker['identity'])))
        with mock.patch.object(Proc, 'pids', side_effect=AssertionError('process scan used as evidence')):
            with transition(str(self.ticket), self.read) as proof:
                proof.check()

    def test_host_command_stays_refused_after_its_controller_is_killed(self):
        self.require_subreaper()
        beat = self.ticket.parent / 'beat'
        controller = self.controller(sys.executable, '-c', BEATING, str(beat))
        self.assertTrue(wait_for(lambda: beat.exists() and beat.read_text().strip()))
        command = wg.process_identity(int(beat.read_text().split()[0]))[0]
        self.addCleanup(lambda: wg.is_live(command) and os.kill(command['pid'], signal.SIGKILL))
        controller.send_signal(signal.SIGKILL)  # killed while blocked in communicate()
        controller.wait(timeout=15)
        written = len(beat.read_text())
        self.assertTrue(wait_for(lambda: len(beat.read_text()) > written), 'the host command keeps writing')
        self.assert_refused(UNVERIFIABLE)
        gate = open_gate(str(self.ticket))
        self.addCleanup(gate.close)
        [marker] = holders(gate)['host_markers']
        supervisor = Proc().stat(command['pid'])[2]
        self.assertEqual((marker['leader']['pid'], marker['leader_live']), (supervisor, True))
        os.kill(command['pid'], signal.SIGKILL)
        self.assertTrue(wait_for(lambda: self.registry_files('c')))
        with transition(str(self.ticket), self.read) as proof:
            proof.check()

    def test_killing_the_supervisor_keeps_the_generation_refused(self):
        """If the supervisor dies first, its orphans escape it and no proof is ever written."""
        self.require_subreaper()
        beat = self.ticket.parent / 'beat'
        controller = self.controller(sys.executable, '-c', BEATING, str(beat))
        self.assertTrue(wait_for(lambda: beat.exists() and beat.read_text().strip()))
        command = int(beat.read_text().split()[0])
        self.addCleanup(self.kill, command)
        supervisor = Proc().stat(command)[2]
        os.kill(supervisor, signal.SIGKILL)
        controller.wait(timeout=15)
        self.kill(command)
        self.assertTrue(wait_for(lambda: wg.process_identity(command) is None))
        self.assertEqual(self.registry_files('c'), [])
        self.assertIn('ended without recording the proof', self.assert_refused(UNVERIFIABLE).detail)

    def test_timeout_kills_the_whole_tree_then_concludes(self):
        self.require_subreaper()
        escaped = self.ticket.parent / 'escaped'
        # A grandchild in its own session: killing the command's group cannot reach it.
        script = (f'"{sys.executable}" -c "import os, sys, time; os.setsid(); '
                  'open(sys.argv[1] + \'.tmp\', \'w\').write(str(os.getpid())); '
                  'os.rename(sys.argv[1] + \'.tmp\', sys.argv[1]); time.sleep(600)" "$1" '
                  '</dev/null >/dev/null 2>&1 & while :; do sleep 0.05; done')
        with writer(str(self.ticket), 'gen-1', self.read) as lease:
            with self.assertRaises(subprocess.TimeoutExpired):
                run_host(lease, ['sh', '-c', script, 'sh', str(escaped)], timeout=3)
        self.assertTrue(escaped.exists(), 'the escaping grandchild never started')
        self.assertIsNone(wg.process_identity(int(escaped.read_text())))
        [conclusion] = [json.loads(p.read_text()) for p in self.registry_files('c')]
        self.assertEqual(conclusion['status'], f'signal {int(signal.SIGKILL)}')
        with transition(str(self.ticket), self.read) as proof:
            proof.check()

    def test_host_command_never_runs_before_its_marker(self):
        marker = self.ticket.parent / 'ran'
        with writer(str(self.ticket), 'gen-1', self.read) as lease:
            refusal = GateError('registry_invalid', 'simulated')
            for step in ('_mark_host', '_record_tree'):
                with self.subTest(step=step):
                    with mock.patch.object(wg, step, side_effect=refusal), self.assertRaises(GateError):
                        run_host(lease, [sys.executable, '-c', 'import sys; open(sys.argv[1], "w")', str(marker)])
                    self.assertFalse(marker.exists(), 'the command ran before its tree was recorded')
                    self.assertEqual(self.registry_files('t'), [])
                    if step == '_mark_host':
                        self.assertEqual(self.host_markers(), [])
            [marked] = self.host_markers()  # by the attempt that failed to record its tree
            self.assertEqual(marked['trees'], wg.TREE_PROTOCOL)
            done = run_host(lease, ['sh', '-c', 'read line; echo "$line"'], input='payload\n',
                            capture_output=True, text=True)
            self.assertEqual(done.stdout, 'payload\n')
            self.assertEqual(done.args, ['sh', '-c', 'read line; echo "$line"'])
            self.assertEqual(len(self.host_markers()), 1)
            with self.assertRaises(FileNotFoundError):
                run_host(lease, ['ssdlc-no-such-command'])


class ProtocolDocumentationTests(unittest.TestCase):
    def test_protocol_and_limits_are_documented(self):
        docs.assert_documented(self, docs.user_facing_docs(), [
            'writer.gate', 'LOCK_SH', 'LOCK_EX | LOCK_NB', 'never deleted or recreated',
            'Linux only', 'local filesystem', 'Docker on the same host',
            'ticket.lock is not a quiescence proof', 'not converted from shared to exclusive',
            'PID, boot id and start time', 'SSDLCOrchestrator.__init__',
            'HOST_TREE_UNVERIFIABLE', 'Process scans are never quiescence evidence',
            'PR_SET_CHILD_SUBREAPER', 'ECHILD', 'If the supervisor dies', 'cgroup v2 was not chosen',
        ], issues=(45, 132))


if __name__ == '__main__':
    unittest.main()
