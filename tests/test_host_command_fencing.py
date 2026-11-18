"""Host commands of the controller under run_host() (#345): docs/writer-fencing.md, Host writers.

Real orchestrator, real Git, real supervisor processes and real gate; only Docker is simulated.
"""
import copy
import functools
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import doc_contracts as docs
import orchestrator as o
from orchestrator import SSDLCOrchestrator
import ssdlc_fencing_proof as fp
import ssdlc_role_fencing as rf
from ssdlc_fencing_proof import FencingRefused
from ssdlc_tasks import digest, git, ticket_lock
from ssdlc_transitions import TransitionStore, source_binding
import ssdlc_writer_gate as wg
from replay.repository_fixtures import RepositoryTests


class FakeDocker:
    """``docker ps``/``docker inspect`` with no container of the ticket; it never runs Docker."""

    def __call__(self, argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, '' if argv[1] == 'ps' else '[]', '')


@unittest.skipUnless(sys.platform.startswith('linux') and wg.host_tree_support() is None,
                     'host trees need Linux child subreapers')
class HostCommandTestCase(RepositoryTests):
    def setUp(self):
        super().setUp()
        self.ticket = self.repo / '.ssdlc' / 'TKT-1'
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in ('SSDLC_CONTAINER_PAYLOAD_USER', 'SSDLC_SUPERVISED_RUN_ID', 'SSDLC_AUTONOMOUS',
                     'SSDLC_EXECUTION_MODE'):
            os.environ.pop(name, None)
        self.now = [1000.0]

    def orchestrator(self):
        runtime = self.repo / '.ssdlc/.runtime'
        meta = {'project_id': 'test-project', 'codex_home': str(runtime / 'codex'),
                'claude_home': str(runtime / 'claude'), 'cache_dir': str(runtime / 'cache')}
        with patch('orchestrator.ProjectRuntimeIsolation.activate', return_value=meta), \
                patch('orchestrator.get_cli_version', return_value='TEST'):
            return SSDLCOrchestrator(self.repo, 'TKT-1', sync_remote=False)

    def historical(self):
        """A run created before automatic fencing: its state has no marker."""
        (self.ticket / 'state.json').write_text(json.dumps(dict(
            task_engine_v1=True, ticket_id='TKT-1', artifacts={}, ledger=[], gate_history=[],
            base_commit=self.base, status='STOPPED')))
        return self.orchestrator()

    def stored_state(self):
        return json.loads((self.ticket / 'state.json').read_text())

    def registry(self, prefix):
        return sorted(p.name[2:] for p in (self.ticket / wg.REGISTRY_NAME).glob(prefix + '-*.json'))

    def records(self, prefix):
        return [json.loads(p.read_text()) for p in sorted((self.ticket / wg.REGISTRY_NAME).glob(prefix + '-*.json'))]

    # -- a native transition of the ticket's selected generation ('.') ---------------

    def clock(self):
        self.now[0] += 1
        return self.now[0]

    def publish(self, *, manual=False, identity='a' * 32):
        (self.ticket / 'journal.jsonl').write_text('historical journal\n')
        old = {'ledger': [{'cost_usd': 1.25}], 'limits': {'attempts': 3}, 'counters': {'design_rounds': 6},
               'tasks': {'T1': {'attempts': 1, 'status': 'RUNNING'}}}
        new = copy.deepcopy(old)
        new['tasks']['T1']['status'] = 'UNVERIFIED_IMPORT'
        artifacts = {'capture/candidate.json': b'captured candidate'}
        proposal = {'transition_id': identity, 'old_controller_sha256': 'b' * 64,
                    'new_controller_sha256': 'c' * 64, 'projection_sha256': digest(new),
                    'source_files': source_binding(self.ticket, ['journal.jsonl']),
                    'artifacts': {k: hashlib.sha256(v).hexdigest() for k, v in artifacts.items()}}
        authorization = {'schema_version': 1, 'transition_id': identity, 'proposal_sha256': digest(proposal),
                         'authorized': True, 'old_controller_sha256': 'b' * 64, 'new_controller_sha256': 'c' * 64}
        if manual:
            authorization['source_fenced'] = True
        store = TransitionStore(self.ticket / 'continuations',
                                fencing=functools.partial(fp.fence, docker=FakeDocker(), clock=self.clock))
        self.assertEqual(store.publish(self.ticket, proposal, authorization, old, new, artifacts), new)
        return json.loads((self.ticket / 'continuations' / identity / 'intent.json').read_text())


class AdmittedRunTests(HostCommandTestCase):
    def test_host_commands_of_an_admitted_run_run_under_the_subreaper(self):
        orch = self.orchestrator()
        self.assertTrue(rf.admitted(orch.state))
        late = self.repo / 'late.txt'
        with patch.object(wg, 'run_host', wraps=wg.run_host) as run_host, o._writer_fencing_scope(orch):
            # The controller's own Git helpers, as the pipeline calls them.
            self.assertEqual(git(orch.repo_root, 'rev-parse', 'HEAD'), self.base)
            self.assertEqual(orch._get_head_sha(orch.repo_root), self.base)
            # A tree whose grandchild outlives the command: it returns only once the whole tree ended.
            done = rf.host_command(['sh', '-c', '(sleep 0.3; echo late > "$1") & echo started', 'sh', str(late)],
                                   capture_output=True, text=True)
        self.assertEqual(run_host.call_count, 3)
        self.assertEqual((done.returncode, done.stdout), (0, 'started\n'))
        self.assertEqual(late.read_text(), 'late\n')
        trees = self.records('t')
        self.assertEqual(len(trees), 3)
        for tree in trees:
            self.assertEqual(tree['mechanism'], wg.TREE_MECHANISM)
            self.assertEqual(tree['generation'], wg.generation_key('.'))
        self.assertEqual(self.registry('t'), self.registry('c'))
        self.assertEqual({c['status'] for c in self.records('c')}, {'exit 0'})
        # No writer lease outlives its command: the controller does not hold the gate.
        self.assertEqual(self.registry('w'), [])
        self.assertNotIn(rf.REFUSAL_KEY, self.stored_state())
        with wg.transition(self.ticket, lambda: fp.selected_generation(self.ticket)) as proof:
            self.assertEqual(proof.generation, '.')

    def test_a_native_transition_of_a_new_run_uses_the_automatic_proof(self):
        orch = self.orchestrator()
        with o._writer_fencing_scope(orch):
            git(orch.repo_root, 'status', '--porcelain')
            orch._ensure_worktree()
        self.assertTrue(self.registry('t'))
        intent = self.publish()
        self.assertNotIn('source_fenced', intent['authorization'])
        proof = fp.validate_proof(intent[fp.INTENT_KEY])
        self.assertEqual(proof['generation'], '.')
        self.assertEqual(fp.evidence(intent)['mode'], 'automatic')
        self.assertFalse(fp.evidence(intent)['operator_attested'])

    def test_a_tree_run_without_the_subreaper_disables_the_automatic_proof(self):
        orch = self.orchestrator()
        with patch.object(wg, 'host_tree_support', return_value='test host without subreaper'), \
                o._writer_fencing_scope(orch):
            self.assertEqual(git(orch.repo_root, 'rev-parse', 'HEAD'), self.base)
        self.assertEqual(self.records('t')[0]['reason'], 'test host without subreaper')
        with self.assertRaises(FencingRefused) as caught:
            self.publish()
        self.assertEqual(caught.exception.code, wg.HOST_TREE_UNVERIFIABLE)
        self.assertFalse((self.ticket / 'continuations' / ('a' * 32)).exists())
        # The manual path stays available.
        self.assertNotIn(fp.INTENT_KEY, self.publish(manual=True))

    def test_a_refusal_before_the_command_runs_is_recorded_and_the_command_runs_plainly(self):
        orch = self.orchestrator()
        refused = wg.GateError('unsupported_filesystem', 'test filesystem')
        out = []
        with patch.object(wg, 'check_filesystem', side_effect=refused), \
                patch.object(rf, 'run_fenced', wraps=rf.run_fenced) as fenced, \
                patch('sys.stdout.write', side_effect=out.append), o._writer_fencing_scope(orch):
            self.assertEqual(git(orch.repo_root, 'rev-parse', 'HEAD'), self.base)
            self.assertEqual(git(orch.repo_root, 'rev-parse', 'HEAD'), self.base)
        # Recorded once, before the plain run; the second command no longer tries the gate.
        self.assertEqual(fenced.call_count, 1)
        refusal = self.stored_state()[rf.REFUSAL_KEY]
        self.assertEqual(refusal['code'], 'unsupported_filesystem')
        self.assertIn('Automatic fencing refused (unsupported_filesystem', ''.join(out))
        self.assertFalse(rf.admitted(self.stored_state()))
        with self.assertRaises(FencingRefused) as caught:
            self.publish()
        self.assertEqual(caught.exception.code, fp.NOT_ADMITTED)

    def test_a_refusal_after_the_command_ran_is_recorded_and_raised_without_a_second_run(self):
        orch = self.orchestrator()
        ran = self.repo / 'ran.txt'

        def moved(lease, argv, **kwargs):
            subprocess.run(argv, **kwargs)
            raise wg.GateError('stale_generation', 'generation changed under a live writer lease')
        with patch.object(wg, 'run_host', side_effect=moved), patch('sys.stdout.write'), \
                o._writer_fencing_scope(orch), self.assertRaises(wg.GateError) as caught:
            rf.host_command(['sh', '-c', 'echo once >> "$1"', 'sh', str(ran)])
        self.assertNotIsInstance(caught.exception, wg.HostCommandNotRun)
        self.assertEqual(ran.read_text(), 'once\n')
        self.assertEqual(self.stored_state()[rf.REFUSAL_KEY]['code'], 'stale_generation')

    def test_run_fenced_keeps_the_subprocess_contract(self):
        self.orchestrator()
        with self.assertRaises(subprocess.CalledProcessError):
            rf.run_fenced(self.ticket, self.ticket, ['false'], check=True)
        done = rf.run_fenced(self.ticket, self.ticket, ['cat'], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout), (0, ''))
        with self.assertRaises(ValueError):
            rf.run_fenced(self.ticket, self.ticket, ['true'], stdin=subprocess.PIPE)
        # A runtime outside the canonical ticket never runs.
        with self.assertRaises(wg.HostCommandNotRun) as caught:
            rf.run_fenced(self.ticket, self.repo, ['true'])
        self.assertEqual(caught.exception.code, 'generation_invalid')

    def test_a_pending_transition_fences_host_commands_only_outside_the_ticket_lock(self):
        orch = self.orchestrator()
        (self.ticket / 'execution').mkdir()
        (self.ticket / 'execution/pending.json').write_text('{}')
        # Dispatch and verification stay fenced, and so does anyone without the ticket lock.
        with self.assertRaises(wg.HostCommandNotRun) as caught:
            rf.run_fenced(self.ticket, self.ticket, ['true'])
        self.assertEqual(caught.exception.code, 'generation_invalid')
        # The controller preparing and activating the transition holds it: the selected generation.
        with ticket_lock(orch.ticket_lock_path):
            self.assertEqual(rf.host_generation(self.ticket), '.')
            self.assertEqual(rf.run_fenced(self.ticket, self.ticket, ['true']).returncode, 0)
            with self.assertRaises(wg.HostCommandNotRun) as caught:
                rf.run_fenced(self.ticket, self.ticket / 'execution-runtimes' / ('a' * 32), ['true'])
            self.assertEqual(caught.exception.code, 'stale_generation')
        self.assertEqual(self.registry('t'), self.registry('c'))
        self.assertEqual(len(self.registry('t')), 1)

    def test_outside_an_entrypoint_host_commands_run_plainly(self):
        orch = self.orchestrator()
        with patch.object(wg, 'run_host') as run_host:
            self.assertEqual(git(orch.repo_root, 'rev-parse', 'HEAD'), self.base)
        run_host.assert_not_called()
        self.assertFalse(wg.gate_path(self.ticket).exists())


class HistoricalRunTests(HostCommandTestCase):
    def test_a_historical_run_keeps_plain_host_commands_and_the_manual_path(self):
        orch = self.historical()
        self.assertFalse(rf.admitted(orch.state))
        with patch.object(wg, 'run_host') as run_host, o._writer_fencing_scope(orch):
            self.assertEqual(git(orch.repo_root, 'rev-parse', 'HEAD'), self.base)
            self.assertEqual(orch._get_head_sha(orch.repo_root), self.base)
        run_host.assert_not_called()
        self.assertFalse(wg.gate_path(self.ticket).exists())
        self.assertNotIn(rf.MARKER_KEY, self.stored_state())
        self.assertNotIn(rf.REFUSAL_KEY, self.stored_state())
        with self.assertRaises(FencingRefused) as caught:
            self.publish()
        self.assertEqual(caught.exception.code, fp.NOT_ADMITTED)
        self.assertFalse(wg.gate_path(self.ticket).exists())
        intent = self.publish(manual=True)
        self.assertNotIn(fp.INTENT_KEY, intent)
        self.assertEqual(fp.evidence(intent)['mode'], 'manual')


class DocumentationTests(unittest.TestCase):
    def test_host_commands_and_their_limits_are_documented(self):
        docs.assert_documented(self, docs.user_facing_docs(), [
            'ssdlc_role_fencing.host_command()', 'HostCommandNotRun', 'scheduler.lock',
        ], issues=(345,))
        text = (Path(docs.ROOT) / 'docs/writer-fencing.md').read_text()
        self.assertNotIn('not yet wired into the controller', text)
        self.assertNotIn('not yet used by the controller', text)


if __name__ == '__main__':
    unittest.main()
