"""workflow-status command (#349): four sections, exit codes, --watch, JSON Lines, last log, Evidence Pack."""
import contextlib
import io
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import ssdlc_workflow_cli as cli  # noqa: E402
import ssdlc_workflow_status as ws  # noqa: E402
from test_workflow_liveness import FakeKernel  # noqa: E402
from test_workflow_status import Case, TICKET, tasks_state, tree  # noqa: E402

WRITER = [dict(lock='writer.gate', mode='READ', pid=4242)]
CONTROL = '\x1b]0;owned\x07\x1b[31mred\x1b[0m\r\x08‮'


def with_log(state, task, ref, key='log_ref'):
    state = json.loads(json.dumps(state))
    state['tasks'][task]['checks'] = [{'check_id': 'unit', 'exit_code': 1, key: ref, 'sha256': 'f' * 64}]
    return state


class CliCase(Case):
    def set_state(self, **fields):
        path = self.ticket / 'state.json'
        state = json.loads(path.read_text())
        state.update(fields)
        path.write_text(json.dumps(state))

    def run_cli(self, *args, kernel=None, fault=None, snapshot=None, sleep=None, raw=False):
        """``(code, stdout, stderr)`` of ``workflow-status``, on the test ticket unless ``raw``."""
        kernel = kernel or FakeKernel(holders=WRITER)
        extra = {'fault': fault} if fault else {}
        take = snapshot or (lambda repo, ticket: ws.snapshot(repo, ticket, kernel=kernel, **extra))
        out, err = io.StringIO(), io.StringIO()
        argv = list(args) if raw else ['--ticket', TICKET, '--repo', str(self.repo), *args]
        with contextlib.redirect_stderr(err):
            code = cli.main(argv, out=out, snapshot=take, sleep=sleep or (lambda seconds: None))
        return code, out.getvalue(), err.getvalue()

    def code(self, **kw):
        code, out, _ = self.run_cli('--json', **kw)
        return code, json.loads(out)


class ExitCodeTest(CliCase):
    def test_2_invalid_arguments(self):
        for argv in (['--ticket', '../x'], [], ['--ticket', TICKET, '--watch', '--interval', '0.5'],
                     ['--ticket', TICKET, '--watch', '--interval', 'nan'], ['--ticket', TICKET, '--interval', '2'],
                     ['--ticket', TICKET, '--bogus']):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv, raw=True,
                                              snapshot=lambda *a: self.fail('no read on invalid arguments'))
                self.assertEqual(code, cli.INVALID_ARGUMENTS)
                self.assertEqual(out, '')
                self.assertIn('usage: workflow-status', err)
                self.assertNotIn('Traceback', err)

    def test_5_torn(self):
        journal = self.writer
        def fault(point, attempt):
            journal.append(tasks_state(a='COMPLETED', attempts=attempt + 1))
        code, snap = self.code(fault=fault)
        self.assertEqual((code, snap['read']['status']), (cli.TORN, ws.TORN))

    def test_6_unverifiable_and_read_error(self):
        path = self.ticket / '03-dev/tasks/task-journal.jsonl'
        path.write_bytes(path.read_bytes().replace(b'COMPLETED', b'FAILED'))
        code, snap = self.code()
        self.assertEqual((code, snap['read']['status']), (cli.UNVERIFIABLE, ws.UNVERIFIABLE))
        code, out, _ = self.run_cli('--ticket', 'MISSING', '--repo', str(self.repo), '--json', raw=True)
        self.assertEqual((code, json.loads(out)['read']['reason']), (cli.UNVERIFIABLE, 'ticket not found'))

        def broken(repo, ticket):
            raise PermissionError('denied')
        code, out, err = self.run_cli('--json', snapshot=broken)
        snap = json.loads(out)
        self.assertEqual(code, cli.UNVERIFIABLE)
        self.assertEqual(snap['read']['status'], ws.UNVERIFIABLE)
        self.assertEqual(snap['read']['reason'], 'read error: PermissionError: denied')
        self.assertNotIn('Traceback', err)

    def test_4_stale(self):
        code, snap = self.code(kernel=FakeKernel())
        self.assertEqual((code, snap['status']['liveness']), (cli.STALE, 'STALE'))

    def test_3_stopped_or_fenced(self):
        self.set_state(status='FAILED')
        for kernel in (FakeKernel(holders=WRITER), FakeKernel(), FakeKernel(reason='no gate')):
            with self.subTest(liveness=kernel.reason or bool(kernel.holders)):
                code, snap = self.code(kernel=kernel)
                self.assertEqual((code, snap['status']['outcome']), (cli.STOPPED, 'stopped'))
        self.set_state(status='IN_PROGRESS')
        (self.ticket / 'execution').mkdir()
        (self.ticket / 'execution/pending.json').write_text(json.dumps({'transition_id': 'a' * 32}))
        code, snap = self.code()
        self.assertTrue(snap['generation']['dispatch_fenced'])
        self.assertEqual((code, snap['status']['liveness']), (cli.STOPPED, 'RUNNING'))

    def test_0_operational_or_completed(self):
        code, snap = self.code()  # IN_PROGRESS with a writer holding the gate
        self.assertEqual((code, snap['status']['liveness']), (cli.OK, 'RUNNING'))
        self.set_state(status='PAUSED_AT_PHASE_BOUNDARY')
        code, snap = self.code(kernel=FakeKernel())
        self.assertEqual((code, snap['status']['liveness']), (cli.OK, 'QUIESCENT'))
        self.set_state(status='APPROVED_AND_MERGED')
        code, snap = self.code(kernel=FakeKernel(reason='no gate'))  # completed comes before UNVERIFIED
        self.assertEqual((code, snap['status']['liveness']), (cli.OK, 'UNVERIFIED'))

    def test_6_unverified_or_unknown_outcome(self):
        code, snap = self.code(kernel=FakeKernel(reason='no gate'))
        self.assertEqual((code, snap['status']['liveness']), (cli.UNVERIFIABLE, 'UNVERIFIED'))
        self.set_state(status='SOMETHING_NEW')
        code, snap = self.code()
        self.assertEqual((code, snap['status']['outcome']), (cli.UNVERIFIABLE, 'unknown'))
        (self.ticket / 'state.json').unlink()
        code, snap = self.code()
        self.assertEqual((code, snap['status']['outcome']), (cli.UNVERIFIABLE, 'absent'))

    def test_precedence(self):
        def snap(read=ws.COHERENT, liveness='RUNNING', outcome='in_progress', fenced=False):
            return dict(read=dict(status=read), generation=dict(dispatch_fenced=fenced),
                        status=dict(liveness=liveness, outcome=outcome))
        cases = [
            (snap(read=ws.TORN), 5), (snap(read=ws.UNVERIFIABLE), 6),
            (snap(liveness='STALE', outcome='unknown', fenced=True), 4),
            (snap(liveness='UNVERIFIED', outcome='stopped'), 3), (snap(liveness='UNVERIFIED', fenced=True), 3),
            (snap(liveness='UNVERIFIED', outcome='completed'), 0), (snap(liveness='UNVERIFIED'), 6),
            (snap(outcome='absent'), 6), (snap(outcome='unknown'), 6),
            (snap(outcome='awaiting_signoff', liveness='QUIESCENT'), 0), (snap(), 0),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(cli.exit_code(value), expected)

    def test_partial_metrics_alone_do_not_change_the_code(self):
        code, snap = self.code()
        self.assertTrue(any(m['coverage'] != ws.COMPLETE for m in snap['metrics'].values()))
        self.assertEqual(code, cli.OK)

    def test_130_sigint(self):
        def interrupt(seconds):
            raise KeyboardInterrupt
        code, out, err = self.run_cli('--watch', sleep=interrupt)
        self.assertEqual(code, cli.INTERRUPTED)
        self.assertNotIn('Traceback', err)

        def interrupted_read(repo, ticket):
            raise KeyboardInterrupt
        code, out, err = self.run_cli('--json', snapshot=interrupted_read)
        self.assertEqual((code, out, err), (cli.INTERRUPTED, '', ''))


class WatchTest(CliCase):
    def test_watch_json_is_json_lines_and_keeps_sampling_after_terminal_and_error_snapshots(self):
        states, sleeps = iter(['APPROVED_AND_MERGED', 'FAILED', None, 'IN_PROGRESS']), []

        def sleep(seconds):
            sleeps.append(seconds)
            following = next(states, StopIteration)
            if following is StopIteration:
                raise KeyboardInterrupt
            if following is None:  # an unreadable snapshot does not end the watch
                (self.ticket / 'state.json').write_text('{not json')
            else:
                (self.ticket / 'state.json').write_text(json.dumps(
                    {'status': following, 'current_phase': '03-dev', 'ledger': []}))
        code, out, err = self.run_cli('--watch', '--json', sleep=sleep)
        self.assertEqual(code, cli.INTERRUPTED)
        self.assertNotIn('Traceback', err)
        self.assertTrue(out.endswith('\n'))
        lines = out.splitlines()
        self.assertEqual(len(lines), 5)
        snaps = [json.loads(line) for line in lines]  # every line is one whole JSON document
        self.assertTrue(all(s['schema'] == ws.SCHEMA and s['schema_version'] == 1 for s in snaps))
        self.assertEqual([cli.exit_code(s) for s in snaps], [0, 0, 3, 6, 0])
        self.assertEqual(sleeps, [cli.DEFAULT_INTERVAL] * 5)

    def test_interval_is_honoured_and_at_least_one_second(self):
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            raise KeyboardInterrupt
        self.assertEqual(self.run_cli('--watch', '--interval', '1', sleep=sleep)[0], cli.INTERRUPTED)
        self.assertEqual(sleeps, [1.0])
        self.assertEqual(self.run_cli('--watch', '--interval', '0.99', sleep=sleep)[0], cli.INVALID_ARGUMENTS)

    def test_sigint_ends_the_real_command_without_traceback(self):
        """``./ssdlc workflow-status --watch``, through orchestrator.py, interrupted by a real SIGINT."""
        for mode in (['--json'], []):
            with self.subTest(mode=mode):
                proc = subprocess.Popen(
                    [sys.executable, str(ROOT / 'orchestrator.py'), 'workflow-status', '--ticket', TICKET,
                     '--watch', '--interval', '1', *mode, '--repo', str(self.repo)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.repo)
                self.addCleanup(lambda p=proc: p.poll() is None and p.kill())
                first = b''
                with selectors.DefaultSelector() as selector:
                    selector.register(proc.stdout, selectors.EVENT_READ)
                    deadline = time.monotonic() + 60
                    while not first.endswith(b'\n') and time.monotonic() < deadline:
                        if selector.select(timeout=1):
                            chunk = os.read(proc.stdout.fileno(), 1)
                            if not chunk:
                                break
                            first += chunk
                self.assertTrue(first.endswith(b'\n'), proc.stderr.read() if proc.poll() is not None else first)
                proc.send_signal(signal.SIGINT)
                rest, err = proc.communicate(timeout=30)
                self.assertEqual(proc.returncode, 130, err)
                self.assertNotIn(b'Traceback', err)
                self.assertNotIn(b'KeyboardInterrupt', err)
                if mode:
                    for line in (first + rest).splitlines():
                        self.assertEqual(json.loads(line)['ticket'], TICKET)


class OutputTest(CliCase):
    def test_four_sections_and_no_control_character_reaches_the_terminal(self):
        self.set_state(current_step='tasks' + CONTROL)
        log = self.ticket / '03-dev/tasks/A/check-0.log'
        log.parent.mkdir(parents=True)
        log.write_bytes(('line one\n' + CONTROL + '\nlast line').encode())
        self.writer.append(with_log(tasks_state(a='COMPLETED', attempts=1), 'A', 'A/check-0.log'))
        code, out, _ = self.run_cli()
        self.assertEqual(code, cli.OK)
        titles = [line for line in out.splitlines() if line.startswith('== ')]
        self.assertEqual(titles, ['== 1. Run, generation and liveness ==', '== 2. Tasks and feedback ==',
                                  '== 3. Metrics, budget and projections ==',
                                  '== 4. Next action, prerequisites and last log =='])
        for character in '\x1b\x07\r\x08‮':
            self.assertNotIn(character, out)
        self.assertIn('phase 03-dev, step tasks', out)
        self.assertIn('Last log    : 03-dev/tasks/A/check-0.log (task journal, journal event 3 EVENT)', out)
        self.assertIn('  | line one\n', out)
        self.assertIn('  | last line\n', out)
        self.assertIn('Liveness    : RUNNING; writers: writer.gate READ pid 4242', out)
        self.assertIn('spend_usd: spent 0 USD', out)

    def test_signoff_command_and_unavailable_sections(self):
        self.set_state(status='AWAITING_SUPERVISOR_SIGNOFF', current_phase='08-supervisor')
        code, out, _ = self.run_cli(kernel=FakeKernel())
        self.assertEqual(code, cli.OK)
        self.assertIn(f'Command     : ./ssdlc approve {TICKET} (in {self.repo.absolute()})', out)
        code, out, _ = self.run_cli('--ticket', 'MISSING', '--repo', str(self.repo), raw=True)
        self.assertEqual(code, cli.UNVERIFIABLE)
        self.assertEqual(out.count('no data: the snapshot is UNVERIFIABLE'), 4)

    def test_single_json_keeps_the_snapshot_contract_with_the_log(self):
        code, out, _ = self.run_cli('--json')
        snap = json.loads(out)
        self.assertEqual(set(snap), {'schema', 'schema_version', 'ticket', 'read', 'sources', 'run', 'generation',
                                     'journal', 'tasks', 'feedback', 'metrics', 'projection', 'status', 'log'})
        self.assertGreater(len(out.splitlines()), 1)  # one indented document, not JSON Lines

    def test_the_command_writes_nothing(self):
        before = tree(self.repo)
        time.sleep(0.01)
        self.run_cli()
        self.run_cli('--json', '--watch', sleep=lambda s: (_ for _ in ()).throw(KeyboardInterrupt))
        self.assertEqual(tree(self.repo), before)


class LastLogTest(CliCase):
    def log(self, **kw):
        result = self.snap(kernel=FakeKernel(holders=WRITER), **kw)
        self.assertEqual(result['read']['status'], ws.COHERENT, result['read'])
        return result['log']

    def write(self, relative, data):
        path = self.ticket / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_no_reference_is_null_with_a_reason(self):
        log = self.log()
        self.assertIsNone(log['ref'])
        self.assertIsNone(log['text'])
        self.assertEqual(log['coverage'], ws.NONE)
        self.assertIn('no log reference', log['reason'])

    def test_the_last_journal_event_that_introduces_or_updates_a_reference_wins(self):
        self.write('03-dev/tasks/A/check-0.log', b'first')
        self.write('03-dev/tasks/B/check-0.log', b'second')
        self.write('03-dev/tasks/B/developer.log', b'third')
        first = with_log(tasks_state(a='COMPLETED', attempts=1), 'A', 'A/check-0.log')
        self.writer.append(first, 'A_CHECKED')
        second = with_log(first, 'B', 'B/check-0.log')
        self.writer.append(second, 'B_CHECKED')
        self.writer.append(dict(second, schema_version='1.0'), 'NO_NEW_REFERENCE')
        log = self.log()
        self.assertEqual((log['ref'], log['source'], log['sequence'], log['event']),
                         ('03-dev/tasks/B/check-0.log', 'task journal', 4, 'B_CHECKED'))
        self.assertEqual((log['text'], log['coverage']), ('second', ws.PARTIAL))
        self.assertIn("stdout and stderr are not kept", log['reason'])
        # Another key naming a log, updated in place, is an update.
        self.writer.append(with_log(second, 'B', 'B/developer.log', key='developer_log_ref'), 'B_RECOVERED')
        log = self.log()
        self.assertEqual((log['ref'], log['sequence'], log['text']), ('03-dev/tasks/B/developer.log', 6, 'third'))

    def test_only_the_last_8192_bytes_are_read(self):
        data = b'x' * 20000 + b'\n' + b'y' * 100
        self.write('03-dev/tasks/A/check-0.log', data)
        self.writer.append(with_log(tasks_state(a='COMPLETED', attempts=1), 'A', 'A/check-0.log'))
        with mock.patch('os.pread', wraps=os.pread) as pread:
            log = self.log()
        self.assertEqual([c.args[1] for c in pread.call_args_list], [ws.TAIL_BYTES])
        self.assertEqual((log['size_bytes'], log['tail_bytes'], log['truncated']), (len(data), 8192, True))
        self.assertEqual(log['text'].encode(), data[-8192:])

    def test_the_tail_is_sanitized(self):
        self.write('03-dev/tasks/A/check-0.log', ('ok\n' + CONTROL + '\tend\n').encode() + b'\xff')
        self.writer.append(with_log(tasks_state(a='COMPLETED', attempts=1), 'A', 'A/check-0.log'))
        text = self.log()['text']
        for character in '\x1b\x07\r\x08‮':
            self.assertNotIn(character, text)
        self.assertTrue(text.startswith('ok\n'))
        self.assertIn('red', text)
        self.assertIn('\tend\n', text)

    def test_a_symlinked_or_unsafe_reference_is_never_followed(self):
        outside = self.repo / 'secret.log'
        outside.write_text('secret')
        (self.ticket / '03-dev/tasks/A').mkdir(parents=True)
        (self.ticket / '03-dev/tasks/A/check-0.log').symlink_to(outside)
        self.writer.append(with_log(tasks_state(a='COMPLETED', attempts=1), 'A', 'A/check-0.log'))
        log = self.log()
        self.assertEqual(log['ref'], '03-dev/tasks/A/check-0.log')
        self.assertIsNone(log['text'])
        self.assertIn('not readable', log['reason'])
        self.writer.append(with_log(tasks_state(a='COMPLETED', attempts=1), 'A', '../../../secret.log'))
        log = self.log()
        self.assertEqual(log['ref'], '03-dev/tasks/A/check-0.log')  # the unsafe one is skipped
        self.assertIn('1 task journal log references are not plain relative paths', log['reason'])

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'needs FIFOs')
    def test_a_fifo_without_writer_is_refused_without_blocking(self):
        (self.ticket / '03-dev/tasks/A').mkdir(parents=True)
        fifo = self.ticket / '03-dev/tasks/A/check-0.log'
        os.mkfifo(fifo)
        self.writer.append(with_log(tasks_state(a='COMPLETED', attempts=1), 'A', 'A/check-0.log'))
        result = {}
        worker = threading.Thread(target=lambda: result.update(log=self.log()), daemon=True)
        worker.start()
        worker.join(10)
        if worker.is_alive():
            # Release the blocked open before failing, so the thread does not outlive the test.
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            worker.join(10)
            self.fail('reading a FIFO log without a writer blocked the snapshot')
        log = result['log']
        self.assertEqual(log['ref'], '03-dev/tasks/A/check-0.log')
        self.assertIsNone(log['text'])
        self.assertIn('not readable', log['reason'])
        self.assertIn('not a regular file', log['reason'])

    def test_without_journal_references_the_latest_registered_log_by_recorded_at_then_path(self):
        paths = {name: self.write(name, name.encode()) for name in ('05-qa/a.log', '05-qa/b.log', '00-recon/old.log')}
        # mtimes say the opposite of recorded_at: they are never used.
        os.utime(paths['00-recon/old.log'], (2_000_000_000, 2_000_000_000))
        os.utime(paths['05-qa/b.log'], (1_000_000_000, 1_000_000_000))
        artifacts = {
            'old': {'path': '00-recon/old.log', 'recorded_at': '2026-01-01T00:00:00Z'},
            'a': {'path': '05-qa/a.log', 'recorded_at': '2026-02-01T00:00:00Z'},
            'b': {'path': '05-qa/b.log', 'recorded_at': '2026-02-01T00:00:00Z'},
            'newer-json': {'path': '05-qa/report.json', 'recorded_at': '2026-03-01T00:00:00Z'},
            'unordered': {'path': '05-qa/c.log', 'recorded_at': 'yesterday'},
        }
        self.set_state(artifacts=artifacts)
        log = self.log()
        self.assertEqual((log['ref'], log['source'], log['recorded_at'], log['text']),
                         ('05-qa/b.log', 'artifacts', '2026-02-01T00:00:00Z', '05-qa/b.log'))
        self.assertIn('1 registered logs have no usable recorded_at', log['reason'])

    def test_a_generation_log_is_read_from_its_runtime(self):
        identity, runtime = self.publish('execution', 'execution-runtimes', authorized=True)
        (runtime / '03-dev/tasks/A').mkdir(parents=True)
        (runtime / '03-dev/tasks/A/check-0.log').write_text('generation log')
        journal = type(self.writer)(runtime / '03-dev/tasks/task-journal.jsonl')
        journal.path.unlink()
        journal.append(with_log(tasks_state(a='COMPLETED', attempts=1), 'A', 'A/check-0.log'))
        log = self.log()
        self.assertEqual((log['ref'], log['text']),
                         (f'execution-runtimes/{identity}/03-dev/tasks/A/check-0.log', 'generation log'))


class EvidencePackTest(unittest.TestCase):
    def test_the_pack_records_the_workflow_status_locally(self):
        import test_unified_integration as journeys
        case = journeys.StartupJourneys()
        self.addCleanup(case.doCleanups)
        case.setUp()
        case.new_run()
        owner = case.active_orch
        text = owner.compile_evidence_pack(True).read_text()
        path = owner.ssdlc_root / 'workflow-status.json'
        snap = json.loads(path.read_text())
        self.assertEqual(snap['schema'], ws.SCHEMA)
        self.assertEqual(snap['ticket'], owner.ticket_id)
        self.assertIn('workflow-status.json', owner.state['artifacts'])
        self.assertIn('[workflow-status.json](./workflow-status.json)', text)
        line = next(line for line in text.splitlines() if line.startswith('Workflow status at compilation'))
        self.assertIn(f"read `{snap['read']['status']}`", line)
        self.assertIn(f'exit code {cli.exit_code(snap)}', line)


if __name__ == '__main__':
    unittest.main()
