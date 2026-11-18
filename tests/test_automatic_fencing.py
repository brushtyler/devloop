"""Automatic fencing proof in native transitions (#45 part c2, #158): real gate, fake Docker."""
import copy
import fcntl
import functools
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import ssdlc_fencing_proof as fp
import ssdlc_writer_gate as wg
from ssdlc_fencing_proof import FencingRefused
from ssdlc_tasks import digest, file_hash
from ssdlc_transitions import (TransitionError, TransitionStore, source_binding, validate_intent,
                               validate_operator_authorization)


class FakeDocker:
    """``docker ps``/``docker inspect`` answers; it never runs Docker."""

    def __init__(self):
        self.containers, self.fail, self.calls = [], False, []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if self.fail:
            raise subprocess.CalledProcessError(1, argv)
        if argv[1] == 'ps':
            return subprocess.CompletedProcess(argv, 0, '\n'.join(c['Id'] for c in self.containers), '')
        return subprocess.CompletedProcess(argv, 0, json.dumps(self.containers), '')


def crash_at(point):
    def fault(reached):
        if reached == point:
            raise OSError('crash at ' + point)
    return fault


class AutomaticFencingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ticket = Path(tmp.name).absolute() / '.ssdlc' / 'T-1'
        self.ticket.mkdir(parents=True)
        self.state(self.ticket)
        (self.ticket / 'journal.jsonl').write_text('historical journal\n')
        self.old = {'ledger': [{'cost_usd': 1.25}], 'limits': {'attempts': 3},
                    'counters': {'design_rounds': 6}, 'tasks': {'T1': {'attempts': 1, 'status': 'RUNNING'}}}
        self.new = copy.deepcopy(self.old)
        self.new['tasks']['T1']['status'] = 'UNVERIFIED_IMPORT'
        self.artifacts = {'capture/candidate.json': b'captured candidate'}
        self.proposal = self.make_proposal('a' * 32, self.ticket)
        self.manual = self.make_authorization(self.proposal)
        self.automatic = {k: v for k, v in self.manual.items() if k != 'source_fenced'}
        self.docker = FakeDocker()
        self.now = [1000.0]
        self.writers = set()
        self.addCleanup(self.close_writers)

    # -- fixtures ---------------------------------------------------------------

    def state(self, root, **extra):
        state = dict({'ticket_id': 'T-1', 'writer_fencing': 'v1'}, **extra)
        (root / 'state.json').write_text(json.dumps(state))

    def make_proposal(self, identity, source, **extra):
        return dict({'transition_id': identity, 'old_controller_sha256': 'b' * 64,
                     'new_controller_sha256': 'c' * 64, 'projection_sha256': digest(self.new),
                     'source_files': source_binding(source, ['journal.jsonl']),
                     'artifacts': {k: hashlib.sha256(v).hexdigest() for k, v in self.artifacts.items()}}, **extra)

    def make_authorization(self, proposal):
        return {'schema_version': 1, 'transition_id': proposal['transition_id'],
                'proposal_sha256': digest(proposal), 'authorized': True, 'source_fenced': True,
                'old_controller_sha256': 'b' * 64, 'new_controller_sha256': 'c' * 64}

    def clock(self):
        self.now[0] += 1
        return self.now[0]

    def store(self, namespace='continuations', fault=lambda point: None):
        return TransitionStore(self.ticket / namespace, fault,
                               fencing=functools.partial(fp.fence, docker=self.docker, clock=self.clock))

    def publish(self, authorization=None, store=None, proposal=None, source=None):
        return (store or self.store()).publish(source or self.ticket, proposal or self.proposal,
                                               self.automatic if authorization is None else authorization,
                                               self.old, self.new, self.artifacts)

    def intent(self, namespace='continuations', identity='a' * 32):
        return json.loads((self.ticket / namespace / identity / 'intent.json').read_text())

    def hold_writer(self):
        """A writer holding LOCK_SH on the gate, like a role container's PID 1 holder."""
        gate = wg.open_gate(self.ticket)
        gate.close()
        fd = os.open(self.ticket / wg.GATE_NAME, os.O_RDONLY | os.O_CLOEXEC)
        fcntl.flock(fd, fcntl.LOCK_SH)
        self.writers.add(fd)
        return fd

    def release(self, fd):
        self.writers.discard(fd)
        os.close(fd)

    def close_writers(self):
        for fd in list(self.writers):
            self.release(fd)

    # -- criterion 1: admitted run, no writer -------------------------------------

    def test_admitted_run_without_writers_uses_automatic_proof(self):
        self.assertEqual(self.publish(), self.new)
        intent = self.intent()
        self.assertNotIn('source_fenced', intent['authorization'])
        self.assertEqual(intent['authorization'], self.automatic)
        proof = fp.validate_proof(intent['fencing'])
        gate = os.stat(self.ticket / wg.GATE_NAME)
        self.assertEqual(proof['kind'], 'ssdlc-automatic-fencing-proof')
        self.assertEqual(proof['schema_version'], 1)
        self.assertEqual(proof['gate'], {'device': gate.st_dev, 'inode': gate.st_ino})
        self.assertEqual(proof['generation'], '.')
        self.assertEqual(proof['lock']['operation'], 'LOCK_EX|LOCK_NB')
        self.assertIs(proof['lock']['acquired'], True)
        self.assertEqual(proof['lock']['holder']['pid'], os.getpid())
        self.assertEqual(proof['docker']['ticket_containers'], 0)
        self.assertEqual(proof['verified_at'], 1001.0)
        self.assertEqual(len(self.docker.calls), 1)
        # Operator authorization and host proof are separate evidence.
        self.assertEqual(fp.evidence(intent), {'mode': 'automatic', 'operator_attested': False, 'automatic': proof})
        self.assertEqual(TransitionStore(self.ticket / 'continuations').load(), self.new)

    def test_automatic_proof_does_not_relax_other_exact_authorizations(self):
        changes = {
            'proposal_sha256': lambda a: a.update(proposal_sha256='0' * 64),
            'authorized': lambda a: a.update(authorized=False),
            'old_controller_sha256': lambda a: a.pop('old_controller_sha256'),
            'new_controller_sha256': lambda a: a.update(new_controller_sha256='d' * 64),
            'transition_id': lambda a: a.update(transition_id='f' * 32),
            'extra field': lambda a: a.update(profile_sha256='e' * 64),
            'schema_version': lambda a: a.update(schema_version=2),
        }
        for name, change in changes.items():
            with self.subTest(field=name):
                authorization = copy.deepcopy(self.automatic)
                change(authorization)
                with self.assertRaises(TransitionError):
                    self.publish(authorization)
                self.assertFalse((self.ticket / 'continuations' / 'active.json').exists())
        self.assertEqual(self.docker.calls, [])  # refused before any fence

    def test_scope_expansion_still_needs_its_exact_authorization(self):
        proposal = self.make_proposal('a' * 32, self.ticket, scope_diff={'T1': {'added': ['x.py'], 'removed': []}})
        automatic = {k: v for k, v in self.make_authorization(proposal).items() if k != 'source_fenced'}
        with self.assertRaisesRegex(TransitionError, 'Scope expansion'):
            validate_operator_authorization(automatic, proposal)
        automatic['scope_diff_sha256'] = digest(proposal['scope_diff'])
        validate_operator_authorization(automatic, proposal)

    def test_proof_and_operator_attestation_are_never_both_accepted(self):
        proof = self.publish() and self.intent()['fencing']
        with self.assertRaisesRegex(TransitionError, 'never both'):
            validate_intent({'proposal': self.proposal, 'authorization': self.manual, 'fencing': proof})

    # -- criterion 2: active writer, non-admitted run -----------------------------

    def test_active_writer_refuses_the_transition(self):
        fd = self.hold_writer()
        with self.assertRaises(FencingRefused) as refused:
            self.publish()
        self.assertEqual(refused.exception.code, 'writers_active')
        self.assertIn('manual path', str(refused.exception))
        self.assertFalse((self.ticket / 'continuations' / ('a' * 32)).exists())
        self.assertFalse((self.ticket / 'continuations' / 'active.json').exists())
        self.release(fd)
        self.assertEqual(self.publish(), self.new)

    def test_container_of_the_ticket_or_docker_failure_refuses(self):
        self.docker.containers = [{'Id': 'c1', 'Mounts': [
            {'Source': str(self.ticket), 'Destination': str(self.ticket), 'RW': False}]}]
        with self.assertRaises(FencingRefused) as refused:
            self.publish()
        self.assertEqual(refused.exception.code, 'uncovered_execution')
        self.docker.containers, self.docker.fail = [], True
        with self.assertRaises(FencingRefused) as refused:
            self.publish()
        self.assertEqual(refused.exception.code, 'uncovered_execution')
        self.assertFalse((self.ticket / 'continuations' / 'active.json').exists())

    def test_docker_check_counts_only_containers_that_reach_the_ticket(self):
        repo = self.ticket.parent.parent
        cases = {
            'inside ticket': ([{'Source': str(self.ticket / 'worktree'), 'Destination': '/w', 'RW': True}], 1),
            'repo read-write': ([{'Source': str(repo), 'Destination': str(repo), 'RW': True}], 1),
            'repo read-only': ([{'Source': str(repo), 'Destination': str(repo), 'RW': False}], 0),
            'other ticket': ([{'Source': str(repo / '.ssdlc' / 'T-2'), 'Destination': '/t', 'RW': True}], 0),
            'unreadable': ([None], 1),
        }
        for name, (mounts, expected) in cases.items():
            with self.subTest(name):
                self.docker.containers = [{'Id': 'c1', 'Mounts': mounts}]
                reason, observed = fp.ticket_containers(self.ticket, run=self.docker)
                self.assertEqual(observed['ticket_containers'], expected)
                self.assertEqual(reason is None, expected == 0)

    def test_run_not_admitted_refuses_automatic_and_keeps_manual_path(self):
        for name, extra in (('never admitted', None), ('refused since', {'code': 'ROLE_OUTSIDE_CONTAINER'})):
            with self.subTest(name):
                if extra is None:
                    (self.ticket / 'state.json').write_text(json.dumps({'ticket_id': 'T-1'}))
                else:
                    self.state(self.ticket, writer_fencing_refusal=extra)
                with self.assertRaises(FencingRefused) as refused:
                    self.publish()
                self.assertEqual(refused.exception.code, 'RUN_NOT_ADMITTED')
                self.assertFalse((self.ticket / wg.GATE_NAME).exists())
        self.assertEqual(self.publish(self.manual), self.new)
        intent = self.intent()
        self.assertEqual(set(intent), {'proposal', 'authorization'})
        self.assertEqual(fp.evidence(intent), {'mode': 'manual', 'operator_attested': True, 'automatic': None})
        # The manual path never opens the gate nor asks Docker.
        self.assertFalse((self.ticket / wg.GATE_NAME).exists())
        self.assertEqual(self.docker.calls, [])

    def test_refusal_recorded_before_the_lock_is_seen_under_it(self):
        with mock.patch.object(fp, 'admitted', side_effect=[True, False]), \
                self.assertRaises(FencingRefused) as refused:
            self.publish()
        self.assertEqual(refused.exception.code, 'RUN_NOT_ADMITTED')
        self.assertFalse((self.ticket / 'continuations' / ('a' * 32)).exists())

    def test_manual_authorization_of_an_admitted_run_is_unchanged(self):
        self.assertEqual(self.publish(self.manual), self.new)
        self.assertEqual(set(self.intent()), {'proposal', 'authorization'})
        self.assertFalse((self.ticket / wg.GATE_NAME).exists())

    def test_unsupported_environment_refuses_with_its_code(self):
        nfs = wg.GateError('unsupported_filesystem', 'nfs is not a supported local filesystem with kernel flock')
        with mock.patch.object(wg, 'check_filesystem', side_effect=nfs), \
                self.assertRaises(FencingRefused) as refused:
            self.publish()
        self.assertEqual(refused.exception.code, 'unsupported_filesystem')
        self.assertFalse((self.ticket / 'continuations' / 'active.json').exists())
        self.assertEqual(self.publish(self.manual), self.new)  # the manual path stays available

    # -- source must be the selected generation -----------------------------------

    def test_proof_names_the_selected_generation_and_refuses_another(self):
        self.publish()
        runtime = self.ticket / 'runtime-generations' / ('a' * 32)
        runtime.mkdir(parents=True)
        self.state(runtime)
        (runtime / 'journal.jsonl').write_text('runtime journal\n')
        child = self.make_proposal('d' * 32, runtime)
        automatic = {k: v for k, v in self.make_authorization(child).items() if k != 'source_fenced'}
        stale = self.make_proposal('e' * 32, self.ticket)
        stale_auth = {k: v for k, v in self.make_authorization(stale).items() if k != 'source_fenced'}
        with self.assertRaises(FencingRefused) as refused:
            self.publish(stale_auth, self.store('execution'), stale, self.ticket)
        self.assertEqual(refused.exception.code, 'stale_generation')
        self.assertEqual(self.publish(automatic, self.store('execution'), child, runtime), self.new)
        self.assertEqual(self.intent('execution', 'd' * 32)['fencing']['generation'],
                         'runtime-generations/' + 'a' * 32)

    # -- criterion 3: historical intents ------------------------------------------

    def test_historical_manual_intent_validates_as_before_and_is_not_automatic(self):
        self.publish(self.manual)  # same bytes as an intent written before part c2
        intent = self.intent()
        self.assertEqual(intent, {'proposal': self.proposal, 'authorization': self.manual})
        validate_intent(intent)
        self.assertEqual(TransitionStore(self.ticket / 'continuations').load(), self.new)
        self.assertEqual(fp.evidence(intent)['automatic'], None)
        self.assertEqual(fp.evidence(intent)['mode'], 'manual')
        # Without its attestation and without a proof, it is not read as automatic: refused.
        stripped = dict(intent, authorization=self.automatic)
        with self.assertRaisesRegex(TransitionError, 'fenced operator authorization'):
            validate_intent(stripped)
        path = self.ticket / 'continuations' / ('a' * 32) / 'intent.json'
        path.write_text(json.dumps(stripped))
        pointer = json.loads((self.ticket / 'continuations' / 'active.json').read_text())
        pointer['intent_sha256'] = file_hash(path)
        (self.ticket / 'continuations' / 'active.json').write_text(json.dumps(pointer))
        with self.assertRaisesRegex(TransitionError, 'fenced operator authorization'):
            TransitionStore(self.ticket / 'continuations').load()

    def test_recorded_proof_must_be_the_versioned_format(self):
        self.publish()
        intent = self.intent()
        for name, change in (('version', lambda p: p.update(schema_version=2)),
                             ('kind', lambda p: p.update(kind='writer-gate-exclusive-v1')),
                             ('containers', lambda p: p['docker'].update(ticket_containers=1)),
                             ('lock', lambda p: p['lock'].update(acquired=False)),
                             ('missing', lambda p: p.pop('verified_at'))):
            with self.subTest(name):
                broken = copy.deepcopy(intent)
                change(broken['fencing'])
                with self.assertRaisesRegex(TransitionError, 'automatic fencing proof'):
                    validate_intent(broken)

    # -- criterion 4: replay after a crash ----------------------------------------

    def test_crash_before_selector_publication_replays_coherently(self):
        for point in ('intent_published', 'generation_staged', 'before_activation'):
            with self.subTest(point=point):
                self.setUp()
                journal = (self.ticket / 'journal.jsonl').read_bytes()
                with self.assertRaises(OSError):
                    self.publish(store=self.store(fault=crash_at(point)))
                self.assertFalse((self.ticket / 'continuations' / 'active.json').exists())
                first = (self.ticket / 'continuations' / ('a' * 32) / 'intent.json').read_bytes()
                # The crash released the lock: the replay takes a fresh proof, and keeps the
                # intent written first, which is never rewritten.
                self.assertEqual(self.publish(), self.new)
                self.assertEqual(len(self.docker.calls), 2)
                self.assertEqual((self.ticket / 'continuations' / ('a' * 32) / 'intent.json').read_bytes(), first)
                self.assertEqual(self.intent()['fencing']['verified_at'], 1001.0)
                generations = [p.name for p in (self.ticket / 'continuations').iterdir() if p.is_dir()]
                self.assertEqual(generations, ['a' * 32])
                selected = TransitionStore(self.ticket / 'continuations').load()
                self.assertEqual(selected['tasks']['T1']['attempts'], 1)
                self.assertEqual(selected['ledger'], self.old['ledger'])
                self.assertEqual(selected['counters'], self.old['counters'])
                self.assertEqual((self.ticket / 'journal.jsonl').read_bytes(), journal)
                self.assertEqual((self.ticket / 'continuations' / ('a' * 32) / 'capture/candidate.json').read_bytes(),
                                 b'captured candidate')

    def test_crash_after_selector_publication_replays_without_a_new_fence(self):
        with self.assertRaises(OSError):
            self.publish(store=self.store(fault=crash_at('after_activation')))
        pointer = (self.ticket / 'continuations' / 'active.json').read_bytes()
        intent = (self.ticket / 'continuations' / ('a' * 32) / 'intent.json').read_bytes()
        calls = len(self.docker.calls)
        fd = self.hold_writer()  # the published generation's writers may already run
        self.assertEqual(self.publish(), self.new)
        self.release(fd)
        self.assertEqual(len(self.docker.calls), calls)
        self.assertEqual((self.ticket / 'continuations' / 'active.json').read_bytes(), pointer)
        self.assertEqual((self.ticket / 'continuations' / ('a' * 32) / 'intent.json').read_bytes(), intent)
        with self.assertRaisesRegex(TransitionError, 'different inputs'):
            self.publish(self.manual)

    def test_replay_with_an_active_writer_refuses_and_publishes_nothing(self):
        with self.assertRaises(OSError):
            self.publish(store=self.store(fault=crash_at('generation_staged')))
        first = self.intent()
        fd = self.hold_writer()
        with self.assertRaises(FencingRefused):
            self.publish()
        self.assertFalse((self.ticket / 'continuations' / 'active.json').exists())
        self.release(fd)
        self.assertEqual(self.publish(), self.new)
        self.assertEqual(self.intent(), first)

    def test_replay_cannot_switch_between_manual_and_automatic(self):
        with self.assertRaises(OSError):
            self.publish(store=self.store(fault=crash_at('intent_published')))
        with self.assertRaisesRegex(TransitionError, 'different inputs'):
            self.publish(self.manual)
        self.assertFalse((self.ticket / 'continuations' / 'active.json').exists())

    # -- delegated contracts are strengthened, not equated ------------------------

    def test_delegated_contract_needs_the_proof_only_for_admitted_runs(self):
        proposal = dict(self.proposal, authorization_contract='feedback-scope-delegation-v1')
        delegated_auth = {'schema_version': 1, 'contract': 'feedback-scope-delegation-v1', 'proof': {}}
        self.assertFalse(fp.requested(delegated_auth, proposal))
        store = self.store()
        self.assertIsNotNone(store._fence(self.ticket, proposal, delegated_auth))
        (self.ticket / 'state.json').write_text(json.dumps({'ticket_id': 'T-1'}))
        self.assertIsNone(store._fence(self.ticket, proposal, delegated_auth))
        self.assertEqual(fp.evidence({'proposal': proposal, 'authorization': delegated_auth})['mode'], 'delegated')


class DocumentationTests(unittest.TestCase):
    def test_proof_compatibility_and_replay_are_documented(self):
        import doc_contracts as docs
        docs.assert_documented(self, docs.user_facing_docs(), [
            'ssdlc_fencing_proof.py', 'ssdlc-automatic-fencing-proof', 'RUN_NOT_ADMITTED',
            'without** `source_fenced`', 'beside** the operator', 'Historical intents', 'Replay after a crash',
            'strengthened, not equated', 'FencingRefused',
        ], issues=(45, 158))


class ReconciliationAuthorizationTests(unittest.TestCase):
    def test_automatic_form_keeps_every_other_exact_field(self):
        from ssdlc_reconciliation import _authorization
        decisions = {'unknown_limits': [], 'opaque_counters': [], 'attributions': [], 'extensions': []}
        proposal = {'transition_id': 'a' * 32, 'profile_sha256': 'b' * 64, 'old_controller_sha256': 'c' * 64,
                    'new_controller_sha256': 'd' * 64, 'decisions': decisions}
        authorization = {'schema_version': 1, 'transition_id': 'a' * 32, 'proposal_sha256': digest(proposal),
                         'profile_sha256': 'b' * 64, 'decisions_sha256': digest(decisions), 'authorized': True,
                         'old_controller_sha256': 'c' * 64, 'new_controller_sha256': 'd' * 64,
                         'accept_unknown_history': False, 'authorize_extensions': False}
        self.assertEqual(_authorization(authorization, proposal), authorization)
        self.assertEqual(_authorization(dict(authorization, source_fenced=True), proposal)['source_fenced'], True)
        for change in ({'source_fenced': False}, {'profile_sha256': 'e' * 64}, {'authorized': False}):
            with self.subTest(change=change), self.assertRaises(Exception):
                _authorization(dict(authorization, **change), proposal)


if __name__ == '__main__':
    unittest.main()
