"""Issue #36: a refused baseline keeps its own evidence (record first, then refuse).

Offline: real Git worktrees and preparation receipts, no providers. Each refusal
must be diagnosable from what the single verifier run left behind.
"""
import json
import os
import shutil
import subprocess
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import ssdlc_candidates

from test_continuations import ContinuationTests
from test_reconciliation import ReconciliationTests
from ssdlc_preparations import discard, receipt_path, unfinished
from ssdlc_reconciliation import prepare
from ssdlc_recovery import RecoveryStop
from ssdlc_tasks import file_hash
from ssdlc_transitions import TransitionError

SECRET = 'ssdlc-test-secret-value-0123456789'
CHECK = 'CHECK-GLOBAL'


def failing(argv, cwd, timeout, workspace):
    return 'Ran 3 tests\nFAILED (failures=1)\nstdout ' + SECRET, 'Traceback: assertion in test_app', 1


def empty(argv, cwd, timeout, workspace):
    return '', 'Ran 0 tests in 0.000s\n\nNO TESTS RAN', 0


def mutating(argv, cwd, timeout, workspace):
    (workspace / 'app.py').write_text('changed by verifier\n')
    (workspace / 'scratch.tmp').write_text('cache')
    return 'Ran 1 test\nOK', 'mutation stderr', 0


def timing_out(argv, cwd, timeout, workspace):
    raise subprocess.TimeoutExpired(argv, timeout, output=b'partial output before timeout', stderr=b'')


def config_changing(argv, cwd, timeout, workspace):
    with (workspace / '.git' / 'config').open('a') as stream:
        stream.write('[core]\n\tfsmonitor = true\n')
    return 'Ran 1 test\nOK', 'config stderr', 0


def metadata_deleting(argv, cwd, timeout, workspace):
    shutil.rmtree(workspace / '.git')
    return 'Ran 1 test\nOK', 'deleted metadata stderr', 0


def index_corrupting(argv, cwd, timeout, workspace):
    (workspace / '.git' / 'index').write_bytes(b'not a git index')
    return 'Ran 1 test\nOK', 'corrupt index stderr', 0


UNREADABLE = []


def metadata_unreadable(argv, cwd, timeout, workspace):
    # From now on a metadata file cannot be read; HEAD and tree stay readable and unchanged.
    UNREADABLE.append(Path(workspace))
    return 'Ran 1 test\nOK', 'unreadable metadata stderr', 0


@contextmanager
def unreadable_metadata_after_verifier():
    """Root ignores file modes: fail git_metadata on the worker only once the verifier ran."""
    original = ssdlc_candidates.git_metadata
    def read(root):
        if Path(root) in UNREADABLE:
            raise PermissionError(13, 'Permission denied')
        return original(root)
    UNREADABLE.clear()
    try:
        with patch.object(ssdlc_candidates, 'git_metadata', read):
            yield
    finally:
        UNREADABLE.clear()


def config_only(test, diff, errors):
    test.assertEqual((diff['modified'], diff['deleted'], diff['added']), (['config'], [], []))
    test.assertNotIn('identity', diff)
    test.assertEqual(errors, {})


def metadata_gone(test, diff, errors):
    test.assertIn('HEAD', diff['deleted'])
    test.assertIn('config', diff['deleted'])
    test.assertEqual(diff['identity']['head']['after'], None)
    test.assertIn('metadata', errors)
    test.assertIn('metadata', diff['inspection_failed'])


def index_unreadable(test, diff, errors):
    # git_metadata binds the index through the tree only: write-tree must fail.
    test.assertEqual(diff['identity']['tree']['after'], None)
    test.assertNotIn('head', diff['identity'])
    test.assertEqual(set(errors), {'tree'})
    test.assertEqual(diff['inspection_failed'], ['tree'])


def metadata_read_failed(test, diff, errors):
    # HEAD and tree are readable and unchanged: the failed read alone is the mutation.
    test.assertEqual(diff, {'inspection_failed': ['metadata']})
    test.assertEqual(errors, {'metadata': 'PermissionError: Permission denied'})


SCENARIOS = {'failed': (failing, 'FAILED'), 'empty': (empty, 'EMPTY_SUITE'),
             'mutation': (mutating, 'SOURCE_CHANGED'), 'timeout': (timing_out, 'TIMEOUT'),
             'config': (config_changing, 'METADATA_CHANGED'),
             'metadata_deleted': (metadata_deleting, 'METADATA_CHANGED'),
             'index_corrupt': (index_corrupting, 'METADATA_CHANGED'),
             'metadata_unreadable': (metadata_unreadable, 'METADATA_CHANGED')}
METADATA_EXPECTATIONS = {'config': config_only, 'metadata_deleted': metadata_gone, 'index_corrupt': index_unreadable,
                         'metadata_unreadable': metadata_read_failed}
OUTPUTS = {'failed': ['Traceback: assertion in test_app', 'FAILED (failures=1)', '[REDACTED]'],
           'empty': ['NO TESTS RAN'], 'mutation': ['mutation stderr'],
           'timeout': ['partial output before timeout', 'Verification timeout'],
           'config': ['config stderr'], 'metadata_deleted': ['deleted metadata stderr'],
           'index_corrupt': ['corrupt index stderr'], 'metadata_unreadable': ['unreadable metadata stderr']}


class BaselineEvidenceMixin:
    namespace = None
    log_name = None

    def counted(self, verifier):
        calls = []
        def wrapper(*args):
            calls.append(args)
            return verifier(*args)
        return wrapper, calls

    def assert_refusal_evidence(self, identity, classification, calls, verifier_output=None, metadata=None):
        self.assertEqual(len(calls), 1, 'refusal must not need a second verifier run')
        receipt = json.loads(receipt_path(self.target, identity, self.namespace).read_text())
        self.assertEqual(receipt['status'], 'REJECTED')
        runtime = self.target / receipt['runtime']
        preparation = self.target / receipt['preparation']
        refusal_path = preparation / receipt['refusal_ref']
        self.assertEqual(file_hash(refusal_path), receipt['refusal_sha256'])
        refusal = json.loads(refusal_path.read_text())
        self.assertEqual((refusal['reason'], refusal['check_id'], refusal['transition_id']),
                         (classification, CHECK, identity))
        self.assertFalse(refusal['generation_selected'])
        self.assertEqual(refusal['model_dispatches'], 0)
        log = runtime / refusal['log']['ref']
        self.assertEqual(refusal['log']['ref'], '00-recon/baseline/' + self.log_name)
        self.assertEqual(file_hash(log), refusal['log']['sha256'])
        record_path = preparation / refusal['check_record']['ref']
        self.assertEqual(file_hash(record_path), refusal['check_record']['sha256'])
        record = json.loads(record_path.read_text())
        self.assertEqual(record['classification'], classification)
        self.assertEqual(record['exit_code'], refusal['exit_code'])
        self.assertIsInstance(record['duration_sec'], float)
        self.assertGreaterEqual(record['duration_sec'], 0)
        self.assertEqual(record['log_sha256'], refusal['log']['sha256'])
        text = log.read_text()
        self.assertNotIn(SECRET, text)
        if verifier_output:
            for fragment in verifier_output:
                self.assertIn(fragment, text)
        if classification == 'SOURCE_CHANGED':
            diff = refusal['inventory_diff']
            self.assertEqual(diff, record['inventory_diff'])
            self.assertEqual((diff['modified'], diff['deleted'], diff['added']), (['app.py'], [], ['scratch.tmp']))
        else:
            self.assertNotIn('inventory_diff', refusal)
        if classification == 'METADATA_CHANGED':
            self.assertTrue(record['metadata_changed'])
            self.assertEqual(refusal['metadata_diff'], record['metadata_diff'])
            self.assertEqual(refusal.get('inspection_errors', {}), record.get('inspection_errors', {}))
            metadata(self, refusal['metadata_diff'], refusal.get('inspection_errors', {}))
        else:
            self.assertFalse(record['metadata_changed'])
            self.assertNotIn('metadata_diff', refusal)
        self.assertFalse((self.target / self.namespace / 'active.json').exists())
        return receipt, refusal

    def assert_discard_archives(self, identity, refusal):
        discard(self.target, identity, namespace=self.namespace, writers_quiescent=lambda record: True)
        archive = self.target / self.namespace / 'aborted' / identity
        log = archive / 'runtime' / refusal['log']['ref']
        self.assertEqual(file_hash(log), refusal['log']['sha256'])
        self.assertEqual(file_hash(archive / 'preparation' / refusal['check_record']['ref']),
                         refusal['check_record']['sha256'])
        archived = json.loads((archive / 'preparation/refusal.json').read_text())
        self.assertEqual(archived, refusal)
        self.assertEqual(unfinished(self.target, namespace=self.namespace), [])


class ContinuationBaselineEvidenceTests(BaselineEvidenceMixin, ContinuationTests):
    namespace = 'continuations'
    log_name = 'continuation-' + CHECK + '.log'
    messages = {'failed': 'baseline failed', 'empty': 'baseline failed', 'timeout': 'baseline failed',
                'mutation': 'changed source', 'config': 'changed checkout metadata',
                'metadata_deleted': 'changed checkout metadata', 'index_corrupt': 'changed checkout metadata',
                'metadata_unreadable': 'changed checkout metadata'}

    def test_refused_baseline_keeps_log_record_and_refusal(self):
        for name, (verifier, classification) in SCENARIOS.items():
            with self.subTest(name=name), patch.dict(os.environ, {'SSDLC_TEST_TOKEN': SECRET}):
                wrapped, calls = self.counted(verifier)
                with unreadable_metadata_after_verifier(), \
                        self.assertRaisesRegex(TransitionError, self.messages[name]):
                    self.prepare(verifier=wrapped)
                ids = unfinished(self.target)
                self.assertEqual(len(ids), 1)
                _, refusal = self.assert_refusal_evidence(ids[0], classification, calls, OUTPUTS[name],
                                                          METADATA_EXPECTATIONS.get(name))
                self.assert_discard_archives(ids[0], refusal)

    def test_passing_baseline_records_evidence_without_refusal(self):
        prepared = self.prepare()
        preparation = self.target / 'continuations' / ('prepared-' + prepared['proposal']['transition_id'])
        record = json.loads((preparation / 'baseline' / (CHECK + '.json')).read_text())
        self.assertEqual((record['classification'], record['exit_code']), ('PASSED', 0))
        self.assertFalse((preparation / 'refusal.json').exists())


class ReconciliationBaselineEvidenceTests(BaselineEvidenceMixin, ReconciliationTests):
    namespace = 'execution'
    log_name = 'reconciliation-' + CHECK + '.log'

    def test_refused_reconciliation_baseline_keeps_log_record_and_refusal(self):
        for name, (verifier, classification) in SCENARIOS.items():
            with self.subTest(name=name), patch.dict(os.environ, {'SSDLC_TEST_TOKEN': SECRET}):
                wrapped, calls = self.counted(verifier)
                expected = ((TransitionError, 'mutated source/metadata') if classification.endswith('_CHANGED')
                            else (RecoveryStop, 'baseline failed'))
                with unreadable_metadata_after_verifier(), self.assertRaisesRegex(*expected):
                    prepare(self.target, self.repo, self.request, runtime_identity=self.identity,
                            controller_snapshot=self.controller, verifier=wrapped)
                ids = unfinished(self.target, namespace='execution')
                self.assertEqual(len(ids), 1)
                _, refusal = self.assert_refusal_evidence(ids[0], classification, calls, OUTPUTS[name],
                                                          METADATA_EXPECTATIONS.get(name))
                self.assert_discard_archives(ids[0], refusal)
        self.select(self.stage())


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(cls(name) for cls in (ContinuationBaselineEvidenceTests, ReconciliationBaselineEvidenceTests)
                              for name in cls.__dict__ if name.startswith('test_'))
