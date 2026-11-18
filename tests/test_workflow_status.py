"""WorkflowSnapshot (#346): read without writing, coherent or TORN, metrics with coverage."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ssdlc_workflow_status as ws  # noqa: E402
from ssdlc_tasks import digest  # noqa: E402
import ssdlc_telemetry as telemetry  # noqa: E402
from ssdlc_transitions import TransitionStore, source_binding  # noqa: E402

TICKET = 'T-346'
PLAN = {'schema_version': '1.0', 'plan_revision': 2, 'ticket_id': TICKET, 'tasks': [
    {'task_id': 'A', 'title': 'first', 'depends_on': []},
    {'task_id': 'B', 'title': 'second', 'depends_on': ['A']},
    {'task_id': 'C', 'title': 'third', 'depends_on': ['A', 'B']},
]}


def tasks_state(a='PENDING', b='PENDING', c='PENDING', attempts=0):
    return {'schema_version': '1.0', 'tasks': {
        'A': {'status': a, 'attempts': attempts}, 'B': {'status': b, 'attempts': 0},
        'C': {'status': c, 'attempts': 0}}}


class Journal:
    """Writes hash-chained task journal events, as ``TaskJournal.append`` does."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sequence, self.previous, self.states = 0, '', {}

    def line(self, state, kind='EVENT'):
        event = {'sequence': self.sequence + 1, 'previous': self.previous, 'event': kind, 'state': state}
        event['sha256'] = digest(event)
        self.sequence, self.previous = event['sequence'], event['sha256']
        self.states[self.sequence] = json.loads(json.dumps(state))
        return (json.dumps(event, sort_keys=True) + '\n').encode()

    def append(self, state, kind='EVENT'):
        with self.path.open('ab') as stream:
            stream.write(self.line(state, kind))


def tree(root):
    """Every entry under ``root``: type, mode, size, mtime, inode and content hash."""
    result = {}
    for directory, names, files in os.walk(root):
        for name in names + files:
            path = Path(directory) / name
            info = path.lstat()
            entry = (info.st_mode, info.st_size, info.st_mtime_ns, info.st_ino)
            if path.is_file() and not path.is_symlink():
                entry += (hashlib.sha256(path.read_bytes()).hexdigest(),)
            result[str(path.relative_to(root))] = entry
    info = Path(root).lstat()
    result['.'] = (info.st_mode, info.st_mtime_ns, info.st_ino)
    return result


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.ticket = self.repo / '.ssdlc' / TICKET
        self.writer = self.make_run(self.ticket)

    def make_run(self, root, *, ledger=None, plan=PLAN):
        """A run at ``root``, rewritten from scratch; returns the writer of its journal."""
        root.mkdir(parents=True, exist_ok=True)
        (root / 'state.json').write_text(json.dumps({
            'status': 'IN_PROGRESS', 'current_phase': '03-dev', 'current_step': 'tasks',
            'ledger': ledger if ledger is not None else []}))
        (root / '02-design').mkdir(exist_ok=True)
        (root / '02-design/plan.json').write_text(json.dumps(plan))
        journal = Journal(root / '03-dev/tasks/task-journal.jsonl')
        journal.path.unlink(missing_ok=True)
        journal.append(tasks_state(), 'INITIALIZED')
        journal.append(tasks_state(a='COMPLETED', attempts=1), 'TASK_COMPLETED')
        return journal

    def snap(self, **kw):
        return ws.snapshot(self.repo, TICKET, clock=lambda: 1_800_000_000, **kw)

    def publish(self, namespace, runtimes, *, ledger=(), authorized=False):
        """A real generation published by ``TransitionStore`` and its runtime; returns both."""
        identity = 'e' * 32
        binding = {'generation': identity}
        if authorized:
            binding['model_dispatch_authorized'] = True
        source = self.repo / 'source'
        source.mkdir(exist_ok=True)
        (source / 'journal.jsonl').write_text('historical journal\n')
        old = {'ledger': list(ledger), 'tasks': {}}
        new = dict(old, transition_id=identity, runtime_binding=binding)
        artifacts = {'original-state.json': b'original bytes'}
        proposal = {'transition_id': identity, 'old_controller_sha256': 'b' * 64,
                    'new_controller_sha256': 'c' * 64, 'projection_sha256': digest(new),
                    'source_files': source_binding(source, ['journal.jsonl']),
                    'artifacts': {k: hashlib.sha256(v).hexdigest() for k, v in artifacts.items()}}
        authorization = {'schema_version': 1, 'transition_id': identity, 'proposal_sha256': digest(proposal),
                         'authorized': True, 'source_fenced': True, 'old_controller_sha256': 'b' * 64,
                         'new_controller_sha256': 'c' * 64}
        TransitionStore(self.ticket / namespace).publish(source, proposal, authorization, old, new, artifacts)
        runtime = self.ticket / runtimes / identity
        self.make_run(runtime, ledger=list(ledger))
        (runtime / 'generation.json').write_text(json.dumps(binding))
        return identity, runtime


class ReadOnlyTest(Case):
    def test_snapshot_writes_nothing(self):
        # A torn journal tail, a pending selector and an unreadable feedback ledger are
        # the states the engine's own readers repair or refuse: the snapshot only reports.
        with (self.ticket / '03-dev/tasks/task-journal.jsonl').open('ab') as stream:
            stream.write(b'{"sequence": 3, "torn')
        (self.ticket / 'execution').mkdir()
        (self.ticket / 'execution/pending.json').write_text(json.dumps({'transition_id': 'a' * 32}))
        (self.ticket / 'feedback').mkdir()
        (self.ticket / 'feedback/authority.json').write_text('{"status": "ACTIVE"}')
        before = tree(self.repo)
        time.sleep(0.01)  # A write after this point would move an mtime.
        result = self.snap()
        self.assertEqual(tree(self.repo), before)
        self.assertEqual(result['read']['status'], ws.COHERENT)
        self.assertEqual(result['journal']['interrupted_tail_bytes'], len(b'{"sequence": 3, "torn'))
        self.assertEqual(result['journal']['sequence'], 2)
        self.assertTrue(result['generation']['dispatch_fenced'])
        self.assertEqual(result['feedback']['status'], 'unavailable')
        self.assertFalse(list((self.ticket / '03-dev/tasks').glob('interrupted-tail-*')))
        self.assertFalse((self.ticket / '03-dev/tasks/tasks-state.json').exists())

    def test_refusals_write_nothing(self):
        (self.ticket / 'execution').mkdir()
        (self.ticket / 'execution/active.json').write_text('{"schema_version": 1}')
        before = tree(self.repo)
        result = self.snap()
        self.assertEqual(tree(self.repo), before)
        self.assertEqual(result['read']['status'], ws.UNVERIFIABLE)
        self.assertIn('selector does not validate', result['read']['reason'])
        self.assertIsNone(result['tasks'])

    def test_missing_ticket_creates_nothing(self):
        before = tree(self.repo)
        result = ws.snapshot(self.repo, 'OTHER')
        self.assertEqual((result['read']['status'], result['read']['reason']), (ws.UNVERIFIABLE, 'ticket not found'))
        self.assertEqual(ws.snapshot(self.repo, '../x')['read']['reason'], 'invalid ticket name')
        self.assertEqual(tree(self.repo), before)

    def test_symlinked_file_is_refused_not_followed(self):
        outside = self.repo / 'outside.json'
        outside.write_text(json.dumps(PLAN))
        (self.ticket / '02-design/plan.json').unlink()
        (self.ticket / '02-design/plan.json').symlink_to(outside)
        result = self.snap()
        self.assertEqual(result['read']['status'], ws.UNVERIFIABLE)
        self.assertIn('02-design/plan.json', result['read']['reason'])


class ContractTest(Case):
    def test_coherent_snapshot_contract_and_dag(self):
        result = self.snap()
        self.assertEqual((result['schema'], result['schema_version']), ('ssdlc.workflow-snapshot', 1))
        self.assertEqual(result['read'], dict(status=ws.COHERENT, attempts=1, max_attempts=3,
                                              observed_at='2027-01-15T08:00:00+00:00', reason=None))
        self.assertEqual(result['run'], {'status': 'IN_PROGRESS', 'current_phase': '03-dev', 'current_step': 'tasks'})
        self.assertEqual(result['generation']['kind'], 'base')
        self.assertEqual(result['generation']['root'], '.')
        self.assertFalse(result['generation']['dispatch_fenced'])
        nodes = {n['task_id']: n for n in result['tasks']['nodes']}
        self.assertEqual(result['tasks']['plan_revision'], 2)
        self.assertEqual((nodes['A']['status'], nodes['A']['ready'], nodes['A']['attempts']), ('COMPLETED', False, 1))
        self.assertEqual((nodes['B']['ready'], nodes['B']['waiting_on']), (True, []))
        self.assertEqual((nodes['C']['ready'], nodes['C']['waiting_on']), (False, ['B']))
        self.assertEqual(result['tasks']['counts'], {'COMPLETED': 1, 'PENDING': 2})
        self.assertEqual(result['feedback'], {'status': 'absent', 'projection': None, 'reason': None})
        self.assertEqual(set(result['sources']), {
            'state.json', '02-design/plan.json', '03-dev/tasks/task-journal.jsonl', 'execution/active.json',
            'execution/pending.json', 'continuations/active.json', 'continuations/pending.json',
            'feedback/authority.json', 'feedback/task-journal.jsonl'})
        self.assertIsNone(result['sources']['execution/active.json'])
        self.assertEqual(result['sources']['state.json'],
                         hashlib.sha256((self.ticket / 'state.json').read_bytes()).hexdigest())
        json.dumps(result)  # The contract is plain JSON.

    def test_broken_journal_chain_is_unverifiable_at_once(self):
        path = self.ticket / '03-dev/tasks/task-journal.jsonl'
        path.write_bytes(path.read_bytes().replace(b'COMPLETED', b'FAILED'))
        result = self.snap()
        self.assertEqual((result['read']['status'], result['read']['attempts']), (ws.UNVERIFIABLE, 1))
        self.assertIn('hash chain', result['read']['reason'])

    def test_selected_generation_is_read_from_its_runtime(self):
        identity = 'b' * 32
        binding = {'generation': identity, 'model_dispatch_authorized': True}
        (self.ticket / 'execution').mkdir()
        (self.ticket / 'execution/active.json').write_text(json.dumps({'transition_id': identity}))
        runtime = self.ticket / 'execution-runtimes' / identity
        journal = self.make_run(runtime, plan=dict(PLAN, plan_revision=5))
        journal.append(tasks_state(a='COMPLETED', b='COMPLETED', attempts=1))
        (runtime / 'generation.json').write_text(json.dumps(binding))
        projection = {'transition_id': identity, 'runtime_binding': binding, 'ledger': []}
        with mock.patch('ssdlc_transitions.TransitionStore.load', return_value=projection):
            result = self.snap()
            self.assertEqual(result['read']['status'], ws.COHERENT)
            self.assertEqual((result['generation']['kind'], result['generation']['root'],
                              result['generation']['transition_id']),
                             ('execution', f'execution-runtimes/{identity}', identity))
            self.assertEqual((result['tasks']['plan_revision'], result['journal']['sequence']), (5, 3))
            self.assertIn(f'execution-runtimes/{identity}/generation.json', result['sources'])
            (runtime / 'generation.json').write_text(json.dumps(dict(binding, generation='c' * 32)))
            result = self.snap()
        self.assertEqual(result['read']['status'], ws.UNVERIFIABLE)
        self.assertIn('binding changed', result['read']['reason'])

    def test_a_real_generation_is_read_with_every_file_it_seals(self):
        identity, _ = self.publish('continuations', 'runtime-generations')
        result = self.snap()
        self.assertEqual(result['read']['status'], ws.COHERENT)
        self.assertEqual((result['generation']['kind'], result['generation']['transition_id']),
                         ('continuation', identity))
        for name in ('intent.json', 'manifest.json', 'projection.json', 'original-state.json'):
            self.assertIn(f'continuations/{identity}/{name}', result['sources'], name)

    def test_a_null_selector_is_refused_not_read_as_absent(self):
        for store in ('execution', 'continuations'):
            for name in ('active', 'pending'):
                with self.subTest(selector=f'{store}/{name}.json'):
                    directory = self.ticket / store
                    directory.mkdir(exist_ok=True)
                    (directory / f'{name}.json').write_text('null')
                    try:
                        result = self.snap()
                    finally:
                        (directory / f'{name}.json').unlink()
                    self.assertEqual(result['read']['status'], ws.UNVERIFIABLE)
                    self.assertIn(f'{store}/{name}.json: selector does not validate', result['read']['reason'])
                    self.assertIsNone(result['generation'])

    def test_the_historical_ledger_prefix_is_compared_row_by_row(self):
        rows = [{'engine': 'claude', 'cost_usd': 1.0}, {'engine': 'codex', 'cost_usd': 2.0}]
        _, runtime = self.publish('execution', 'execution-runtimes', ledger=rows, authorized=True)
        def with_ledger(ledger):
            state = json.loads((runtime / 'state.json').read_text())
            (runtime / 'state.json').write_text(json.dumps(dict(state, ledger=ledger)))
            return self.snap()
        extended = with_ledger(rows + [{'engine': 'claude', 'cost_usd': 4.0}])
        self.assertEqual(extended['read']['status'], ws.COHERENT)
        self.assertEqual(extended['generation']['ledger_prefix'], 2)
        self.assertEqual(extended['metrics']['unclassified_usd']['value'], 7.0)
        for label, ledger in (('replaced', [{'engine': 'claude', 'cost_usd': 0.0}, rows[1]]),
                              ('reordered', [rows[1], rows[0]]), ('truncated', rows[:1])):
            with self.subTest(label):
                result = with_ledger(ledger)
                self.assertEqual(result['read']['status'], ws.UNVERIFIABLE)
                self.assertIn('historical ledger prefix', result['read']['reason'])
                self.assertIsNone(result['metrics'])

    def test_malformed_journal_records_are_unverifiable(self):
        path = self.ticket / '03-dev/tasks/task-journal.jsonl'
        cases = {
            'null record': lambda: path.write_bytes(path.read_bytes() + b'null\n'),
            'state not an object': lambda: Journal(path).append([], 'INITIALIZED'),
            'record not an object': lambda: Journal(path).append({'tasks': {'A': None}}, 'INITIALIZED'),
            'status not a string': lambda: Journal(path).append({'tasks': {'A': {'status': ['X']}}}, 'INITIALIZED'),
            'attempts not a number': lambda: Journal(path).append(
                {'tasks': {'A': {'status': 'PENDING', 'attempts': 'one'}}}, 'INITIALIZED'),
        }
        for label, write in cases.items():
            with self.subTest(label):
                self.make_run(self.ticket)
                if label != 'null record':
                    path.unlink()
                write()
                result = self.snap()
                self.assertEqual((result['read']['status'], result['read']['attempts']), (ws.UNVERIFIABLE, 1))
                self.assertIsNone(result['tasks'])


class ConcurrencyTest(Case):
    def journal(self):
        return self.writer

    def test_a_write_during_collection_is_retried_into_a_coherent_snapshot(self):
        journal = self.journal()
        def fault(point, attempt):
            if attempt == 1:
                journal.append(tasks_state(a='COMPLETED', b='RUNNING', attempts=1))
        result = self.snap(fault=fault)
        self.assertEqual((result['read']['status'], result['read']['attempts']), (ws.COHERENT, 2))
        self.assertEqual(result['journal']['sequence'], 3)
        self.assertEqual(result['tasks']['nodes'][1]['status'], 'RUNNING')

    def test_a_selector_published_during_collection_is_seen(self):
        def fault(point, attempt):
            if attempt == 1:
                (self.ticket / 'execution').mkdir()
                (self.ticket / 'execution/pending.json').write_text(json.dumps({'transition_id': 'd' * 32}))
        result = self.snap(fault=fault)
        self.assertEqual((result['read']['status'], result['read']['attempts']), (ws.COHERENT, 2))
        self.assertEqual(result['generation']['selectors']['execution']['pending'], 'd' * 32)
        self.assertTrue(result['generation']['dispatch_fenced'])

    def test_an_atomic_replace_with_the_same_content_still_counts_as_a_change(self):
        def fault(point, attempt):
            path = self.ticket / 'state.json'
            data = path.read_bytes()
            path.unlink()
            path.write_bytes(data)
        result = self.snap(fault=fault)
        self.assertEqual(result['read']['status'], ws.TORN)

    def test_a_sealed_artifact_replaced_during_collection_is_retried(self):
        identity, _ = self.publish('continuations', 'runtime-generations')
        artifact = self.ticket / 'continuations' / identity / 'original-state.json'
        def fault(point, attempt):
            if attempt == 1:  # The same bytes in a new file: only the final check can see it.
                data = artifact.read_bytes()
                artifact.unlink()
                artifact.write_bytes(data)
        result = self.snap(fault=fault)
        self.assertEqual((result['read']['status'], result['read']['attempts']), (ws.COHERENT, 2))

    def test_a_projection_changed_during_collection_is_never_coherent(self):
        identity, _ = self.publish('continuations', 'runtime-generations')
        projection = self.ticket / 'continuations' / identity / 'projection.json'
        def fault(point, attempt):
            if attempt == 1:
                value = json.loads(projection.read_text())
                projection.write_text(json.dumps(dict(value, ledger=[{'cost_usd': 9.0}]), sort_keys=True) + '\n')
        result = self.snap(fault=fault)
        # The retry reads the changed projection, which the engine's loader refuses.
        self.assertEqual((result['read']['status'], result['read']['attempts']), (ws.UNVERIFIABLE, 2))
        self.assertIn('selector does not validate', result['read']['reason'])

    def test_files_that_keep_changing_are_torn_after_three_attempts(self):
        journal, calls = self.journal(), []
        def fault(point, attempt):
            calls.append((point, attempt))
            journal.append(tasks_state(a='COMPLETED', attempts=attempt + 1))
        result = self.snap(fault=fault)
        self.assertEqual(calls, [('collected', 1), ('collected', 2), ('collected', 3)])
        self.assertEqual(result['read']['status'], ws.TORN)
        self.assertEqual((result['read']['attempts'], result['read']['max_attempts']), (3, 3))
        self.assertTrue(result['read']['reason'])
        for key in ('run', 'generation', 'journal', 'tasks', 'feedback', 'metrics'):
            self.assertIsNone(result[key], key)
        self.assertEqual(result['sources'], {})

    def test_concurrent_writer_gives_coherent_or_torn_snapshots(self):
        """A writer appends events in two halves, so readers meet torn tails too."""
        journal, stop = self.journal(), threading.Event()
        def writer():
            n = 0
            while not stop.is_set():
                n += 1
                line = journal.line(tasks_state(a='COMPLETED', b='RUNNING', attempts=n))
                with journal.path.open('ab') as stream:
                    stream.write(line[:len(line) // 2])
                    stream.flush()
                    time.sleep(0.0005)
                    stream.write(line[len(line) // 2:])
        thread = threading.Thread(target=writer)
        thread.start()
        try:
            results = [self.snap() for _ in range(40)]
        finally:
            stop.set()
            thread.join()
        self.assertGreater(journal.sequence, 2)  # The writer ran while the snapshots were read.
        statuses = {r['read']['status'] for r in results}
        self.assertLessEqual(statuses, {ws.COHERENT, ws.TORN})
        for result in results:
            if result['read']['status'] == ws.TORN:
                self.assertEqual(result['read']['attempts'], 3)
                self.assertIsNone(result['tasks'])
                continue
            sequence = result['journal']['sequence']
            expected = journal.states[sequence]['tasks']
            self.assertEqual({n['task_id']: (n['status'], n['attempts']) for n in result['tasks']['nodes']},
                             {t: (r['status'], r['attempts']) for t, r in expected.items()})


class MetricsTest(Case):
    def assert_coverage(self, metrics):
        self.assertTrue(metrics)
        for name, entry in metrics.items():
            self.assertEqual(set(entry), {'value', 'unit', 'coverage', 'source', 'reason'}, name)
            self.assertIn(entry['coverage'], (ws.COMPLETE, ws.PARTIAL, ws.NONE), name)
            if entry['coverage'] != ws.COMPLETE:
                self.assertTrue(entry['reason'], name)
            if entry['coverage'] == ws.NONE:
                self.assertIsNone(entry['value'], name)

    def test_every_metric_declares_its_coverage(self):
        claude = telemetry.record('claude', estimate_measure=telemetry.estimate(0.25, source='claude_cli'),
                                  token_measure=telemetry.tokens({'input': 10, 'output': 5}, scope='cumulative',
                                                                 source='claude_cli', contract=('input', 'output')))
        ledger = [
            {'telemetry_schema_version': 1, 'telemetry': claude, 'duration_sec': 3.5, 'cost_usd': 0.25},
            {'telemetry_schema_version': 1, 'telemetry': telemetry.incomplete('codex'), 'duration_sec': 1.0},
            {'engine': 'claude', 'cost_usd': 0.5},  # A legacy row: no telemetry, no duration.
        ]
        self.make_run(self.ticket, ledger=ledger)
        metrics = self.snap()['metrics']
        self.assert_coverage(metrics)
        self.assertEqual(metrics['invocations']['value'], 2)
        self.assertEqual(metrics['invocations']['coverage'], ws.PARTIAL)
        self.assertEqual(metrics['model_duration_sec']['value'], 4.5)
        self.assertEqual(metrics['model_duration_sec']['coverage'], ws.PARTIAL)
        self.assertEqual((metrics['estimate_usd']['value'], metrics['estimate_usd']['coverage']), (0.25, ws.COMPLETE))
        self.assertEqual(metrics['unclassified_usd']['value'], 0.5)
        self.assertEqual((metrics['tokens_total']['value'], metrics['tokens_total']['coverage']), (15, ws.PARTIAL))
        self.assertEqual((metrics['tasks_total']['value'], metrics['tasks_completed']['value'],
                          metrics['tasks_remaining']['value'], metrics['task_attempts']['value']), (3, 1, 2, 1))
        self.assertEqual((metrics['interventions']['value'], metrics['interventions']['coverage']), (None, ws.NONE))
        for name in ws.NOT_OBSERVABLE:
            self.assertEqual((metrics[name]['value'], metrics[name]['coverage']), (None, ws.NONE))
            self.assertIn('mtime' if 'elapsed' in name else 'persist', metrics[name]['reason'])

    def test_a_run_without_plan_or_ledger_reports_none(self):
        (self.ticket / '02-design/plan.json').unlink()
        (self.ticket / '03-dev/tasks/task-journal.jsonl').unlink()
        (self.ticket / 'state.json').write_text(json.dumps({'status': 'PO_READY'}))
        result = self.snap()
        self.assertEqual(result['read']['status'], ws.COHERENT)
        self.assertIsNone(result['tasks'])
        self.assert_coverage(result['metrics'])
        for name in ('tasks_total', 'task_attempts', 'invocations', 'spend_usd', 'tokens_total'):
            self.assertEqual(result['metrics'][name]['coverage'], ws.NONE, name)

    def test_a_ledger_without_any_duration_has_no_duration(self):
        self.make_run(self.ticket, ledger=[{'engine': 'claude', 'cost_usd': 0.5}, {'engine': 'codex'}])
        metrics = self.snap()['metrics']
        self.assert_coverage(metrics)
        duration = metrics['model_duration_sec']
        self.assertEqual((duration['value'], duration['coverage']), (None, ws.NONE))
        self.assertIn('2 ledger rows', duration['reason'])
        self.make_run(self.ticket, ledger=[])
        duration = self.snap()['metrics']['model_duration_sec']
        self.assertEqual((duration['value'], duration['coverage']), (0, ws.COMPLETE))

    def test_feedback_interventions_are_only_the_recorded_ones(self):
        projection = {'requests': [{'rounds': 2}, {'rounds': 1}], 'operator_owner_amendments': 1,
                      'operator_repair_returns': 2}
        (self.ticket / 'feedback').mkdir()
        (self.ticket / 'feedback/authority.json').write_text('{}')
        with mock.patch('ssdlc_feedback.FeedbackStore.public_projection', return_value=projection):
            result = self.snap()
        self.assertEqual(result['feedback']['status'], 'active')
        self.assert_coverage(result['metrics'])
        self.assertEqual(result['metrics']['feedback_rounds']['value'], 3)
        self.assertEqual((result['metrics']['interventions']['value'],
                          result['metrics']['interventions']['coverage']), (3, ws.PARTIAL))

    def test_a_metric_without_complete_coverage_needs_a_reason(self):
        with self.assertRaises(ValueError):
            ws.metric(None, ws.NONE, 'x')


if __name__ == '__main__':
    unittest.main()
