"""Writer gate for automatic fencing (#45 part a): real processes, real kernel flock."""
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
from ssdlc_tasks import TaskError, ticket_lock
from ssdlc_writer_gate import GateError, handoff, holders, open_gate, run_host, transition, writer


ROOT = Path(__file__).resolve().parents[1]

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

    def assert_refused(self, code, **kwargs):
        with self.assertRaises(GateError) as raised:
            with transition(str(self.ticket), **kwargs):
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
        with transition(str(self.ticket)) as proof:
            receipt = proof.check()
            self.assertEqual(receipt['kind'], 'writer-gate-exclusive-v1')
            self.assertEqual(receipt['inode'], inode)
            self.assertIs(current_lock(self.gate), proof.lease)
            self.assertFalse(proof.lease.shared)
        self.assertEqual(self.gate.stat().st_ino, inode)
        with self.assertRaisesRegex(GateError, 'closed'):
            proof.check()

    def test_registration_cannot_slip_between_check_and_publication(self):
        with transition(str(self.ticket)) as proof:
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

    def test_coverage_probes_refuse_uncovered_execution_trees(self):
        self.assert_refused('uncovered_execution', coverage=[lambda: 'container abc is not covered'])

        def broken():
            raise RuntimeError('docker unavailable')
        self.assert_refused('uncovered_execution', coverage=[broken])
        with transition(str(self.ticket), coverage=[lambda: None]) as proof:
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
        with transition(str(self.ticket)) as proof:
            found = holders(proof._gate)
            self.assertEqual(found['stale_records'], [residual[0].name])
            self.assertEqual([h['mode'] for h in found['kernel']], ['WRITE'])
        self.assertEqual(self.gate.stat().st_ino, inode)

    def test_residual_metadata_or_reused_pid_neither_authorizes_nor_blocks(self):
        me = wg.self_identity()[0]
        gate = open_gate(str(self.ticket))
        self.addCleanup(gate.close)
        reused = dict(me, start_ticks=me['start_ticks'] + 1)  # same PID, another process life
        wg._register(gate, 'writer', reused)
        wg._register(gate, 'uncovered', reused)
        wg._register(gate, 'writer', dict(me, boot_id='another-boot'))
        wg._register(gate, 'writer', me)  # live identity, but no kernel lock
        self.assertFalse(wg.is_live(reused))
        with transition(str(self.ticket)) as proof:
            found = holders(proof._gate)
            self.assertEqual(len(found['stale_records']), 3)
            self.assertEqual(found['uncovered'], [])
            self.assertEqual([h['registered'] for h in found['kernel']], [True])  # only the proof's own flock
        # A forged receipt is not ownership either.
        with writer(str(self.ticket), 'gen-1', self.read) as lease:
            with self.assertRaisesRegex(TaskError, 'no current'):
                replace(lease.lease).validate(self.gate)


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
        with transition(str(self.ticket)):
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
        with transition(str(self.ticket)):
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

    def test_symlinked_or_relative_ticket_root_is_refused(self):
        link = self.ticket.parent / 'T-link'
        link.symlink_to(self.ticket)
        for root in (str(link), os.path.relpath(self.ticket)):
            with self.subTest(root=root), self.assertRaises(GateError) as raised:
                with transition(root):
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


class HostWriterTests(GateCase):
    def test_host_commands_run_inside_the_lease_without_the_gate_descriptor(self):
        probe = ("import os\n"
                 "links = []\n"
                 "for f in os.listdir('/proc/self/fd'):\n"
                 "    try:\n"
                 "        links.append(os.readlink('/proc/self/fd/' + f))\n"
                 "    except OSError:\n"
                 "        pass\n"
                 "print('leak' if any(l.endswith('writer.gate') for l in links) else 'clean')\n"
                 "print(os.getsid(0) == os.getpid())")
        with writer(str(self.ticket), 'gen-1', self.read) as lease:
            done = run_host(lease, [sys.executable, '-c', probe], capture_output=True, text=True)
            self.assertEqual(done.stdout.split(), ['clean', 'True'])
            self.assertEqual(run_host(lease, ['false']).returncode, 1)
        with self.assertRaises(GateError) as raised:
            run_host(lease, ['true'])
        self.assertEqual(raised.exception.code, 'lease_lost')

    def test_escaped_descendant_refuses_automatic_fencing_while_alive(self):
        with writer(str(self.ticket), 'gen-1', self.read) as lease:
            with self.assertRaises(GateError) as raised:
                run_host(lease, ['sh', '-c', 'sleep 60 >/dev/null 2>&1 & exit 0'])
            self.assertEqual(raised.exception.code, 'uncovered_execution')
        refused = self.assert_refused('uncovered_execution')
        self.assertIn('escaped a writer lease', refused.detail)
        records = [json.loads(p.read_text()) for p in (self.ticket / wg.REGISTRY_NAME).glob('u-*.json')]
        self.assertEqual(len(records), 1)
        survivor = records[0]['identity']
        os.kill(survivor['pid'], signal.SIGKILL)
        deadline = time.monotonic() + 15
        while wg.is_live(survivor) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(wg.is_live(survivor))
        with transition(str(self.ticket)) as proof:
            proof.check()


class ProtocolDocumentationTests(unittest.TestCase):
    def test_protocol_and_limits_are_documented(self):
        docs.assert_documented(self, docs.user_facing_docs(), [
            'writer.gate', 'LOCK_SH', 'LOCK_EX | LOCK_NB', 'never deleted or recreated',
            'Linux only', 'local filesystem', 'Docker on the same host',
            'ticket.lock is not a quiescence proof', 'not converted from shared to exclusive',
            'PID, boot id and start time', 'SSDLCOrchestrator.__init__',
        ], issues=(45,))


if __name__ == '__main__':
    unittest.main()
