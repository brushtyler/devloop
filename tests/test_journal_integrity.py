"""Adversarial tests for the task journal's hash chain (issue #3).

The chain is unkeyed. These tests pin down both sides of that fact: the edits
it refuses on load, and the rewrites it accepts unless a journal head recorded
outside the attacker's reach is compared. See docs/architecture.md, "Task
journal: guarantees and threat model".
"""
import json
import tempfile
import unittest
from pathlib import Path

import doc_contracts as docs
from orchestrator import EngineRunner
from ssdlc_tasks import TaskError, TaskJournal, digest


def state(step):
    return {'tasks': {'T1': {'status': 'PENDING' if step < 3 else 'COMPLETED', 'attempts': step}}}


def rechain(events):
    """What an attacker with write access does: recompute every link from scratch."""
    previous, lines = '', []
    for sequence, event in enumerate(events, start=1):
        body = {'sequence': sequence, 'previous': previous, 'event': event['event'], 'state': event['state']}
        body['sha256'] = previous = digest(body)  # evaluated before the key is added
        lines.append(json.dumps(body, sort_keys=True))
    return ('\n'.join(lines) + '\n').encode()


class JournalCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name) / '03-dev/tasks'
        self.journal = TaskJournal(self.directory)
        for step, kind in enumerate(('INITIALIZED', 'ATTEMPT_STARTED', 'ATTEMPT_FAILED', 'TASK_COMPLETED')):
            self.journal.append(kind, state(step))
        self.original = self.journal.path.read_bytes()

    def events(self, data=None):
        return [json.loads(line) for line in (data or self.journal.path.read_bytes()).splitlines()]

    def write(self, events):
        self.journal.path.write_bytes(b''.join(json.dumps(e, sort_keys=True).encode() + b'\n' for e in events))

    def assert_refused(self):
        with self.assertRaisesRegex(TaskError, 'Corrupt task journal'):
            TaskJournal(self.directory).load()
        with self.assertRaisesRegex(TaskError, 'Corrupt task journal'):
            TaskJournal.head(self.journal.path)

    def load(self):
        return TaskJournal(self.directory).load()


class DetectedByTheChain(JournalCase):
    """Inconsistent edits: refused without any external reference."""

    def test_edit_without_recomputing_the_event_digest(self):
        events = self.events()
        events[1]['state']['tasks']['T1']['attempts'] = 0
        self.write(events)
        self.assert_refused()

    def test_edit_with_own_digest_recomputed_breaks_the_next_link(self):
        events = self.events()
        events[1]['state']['tasks']['T1']['attempts'] = 0
        events[1]['sha256'] = digest({k: v for k, v in events[1].items() if k != 'sha256'})
        self.write(events)
        self.assert_refused()

    def test_edit_of_the_last_event_alone_is_refused(self):
        events = self.events()
        events[-1]['state']['tasks']['T1']['status'] = 'FAILED'
        self.write(events)
        self.assert_refused()

    def test_deleted_reordered_or_duplicated_middle_events(self):
        events = self.events()
        for label, changed in (('deleted', events[:1] + events[2:]),
                               ('reordered', [events[0], events[2], events[1], events[3]]),
                               ('duplicated', events[:2] + [events[1]] + events[2:])):
            with self.subTest(label):
                self.write(changed)
                self.assert_refused()

    def test_event_spliced_from_another_journal(self):
        other = TaskJournal(Path(self._tmp.name) / 'other')
        other.append('INITIALIZED', state(0))
        other.append('TASK_COMPLETED', state(3))
        foreign = self.events(other.path.read_bytes())[1]
        events = self.events()
        self.write(events[:1] + [foreign] + events[2:])
        self.assert_refused()

    def test_incomplete_tail_is_cut_and_archived_not_accepted(self):
        torn = b'{"sequence": 5, "previous": "'
        self.journal.path.write_bytes(self.original + torn)
        self.assertEqual(self.load(), state(3))
        self.assertEqual(self.journal.path.read_bytes(), self.original)
        archived = list(self.directory.glob('interrupted-tail-*.bin'))
        self.assertEqual([p.read_bytes() for p in archived], [torn])

    def test_snapshot_is_a_projection_not_a_source_of_truth(self):
        forged = {'tasks': {'T1': {'status': 'COMPLETED', 'attempts': 99}}}
        self.journal.snapshot.write_text(json.dumps(forged))
        self.assertEqual(self.load(), state(3))
        self.assertEqual(json.loads(self.journal.snapshot.read_text()), state(3))
        self.journal.path.unlink()
        with self.assertRaisesRegex(TaskError, 'journal is missing'):
            self.load()


class NeedsAnIndependentTrustAnchor(JournalCase):
    """Rewrites a writer can make without any secret: the chain alone accepts them."""

    def test_whole_log_rewrite_with_recomputed_chain_loads_as_valid(self):
        forged = [dict(e, state={'tasks': {'T1': {'status': 'COMPLETED', 'attempts': 0}}}) for e in self.events()]
        self.journal.path.write_bytes(rechain(forged))
        self.assertEqual(self.load(), {'tasks': {'T1': {'status': 'COMPLETED', 'attempts': 0}}})

    def test_truncation_of_trailing_events_loads_as_valid(self):
        self.journal.path.write_bytes(b''.join(self.original.splitlines(keepends=True)[:2]))
        self.assertEqual(self.load(), state(1))
        self.assertEqual(TaskJournal.head(self.journal.path)['sequence'], 2)

    def test_rollback_to_an_older_copy_loads_as_valid(self):
        older = self.original
        self.journal.load()
        self.journal.append('ATTEMPT_STARTED', state(4))
        self.journal.path.write_bytes(older)
        self.assertEqual(self.load(), state(3))

    def test_recorded_head_detects_rewrite_truncation_and_rollback(self):
        anchor = TaskJournal.head(self.journal.path)
        rewritten = rechain([dict(e, state=state(0)) for e in self.events()])
        truncated = b''.join(self.original.splitlines(keepends=True)[:3])
        for label, data in (('rewrite', rewritten), ('truncation', truncated)):
            with self.subTest(label):
                self.journal.path.write_bytes(data)
                self.assertNotEqual(TaskJournal.head(self.journal.path), anchor)
                with self.assertRaisesRegex(TaskError, 'anchor is not on the chain'):
                    TaskJournal.replay_range(self.journal.path, anchor['sequence'], anchor['sha256'])
        # A later anchor exposes a rollback to a copy taken before it.
        self.journal.path.write_bytes(self.original)
        self.journal.load()
        self.journal.append('ATTEMPT_STARTED', state(4))
        later = TaskJournal.head(self.journal.path)
        self.journal.path.write_bytes(self.original)
        self.assertNotEqual(TaskJournal.head(self.journal.path), later)

    def test_anchor_only_covers_the_prefix_it_names(self):
        anchor = TaskJournal.head(self.journal.path)
        self.journal.load()
        self.journal.append('ATTEMPT_STARTED', state(4))
        self.journal.append('TASK_COMPLETED', state(5))
        self.journal.path.write_bytes(b''.join(self.journal.path.read_bytes().splitlines(keepends=True)[:5]))
        anchored, after = TaskJournal.replay_range(self.journal.path, anchor['sequence'], anchor['sha256'])
        self.assertEqual((anchored, len(after)), (state(3), 1))

    def test_anchor_in_writable_storage_is_rewritten_with_the_journal(self):
        anchor_file = self.directory / 'anchor.json'
        anchor_file.write_text(json.dumps(TaskJournal.head(self.journal.path)))
        self.journal.path.write_bytes(rechain([dict(e, state=state(0)) for e in self.events()]))
        anchor_file.write_text(json.dumps(TaskJournal.head(self.journal.path)))
        anchor = json.loads(anchor_file.read_text())
        self.assertEqual(TaskJournal.head(self.journal.path), anchor)
        TaskJournal.replay_range(self.journal.path, anchor['sequence'], anchor['sha256'])
        self.assertEqual(self.load(), state(0))


class DockerTrustBoundary(unittest.TestCase):
    """The chain relies on sandboxes having no write path to the ticket directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name).resolve() / 'repo'
        self.ticket = self.repo / '.ssdlc/TKT-001'
        (self.ticket / '03-dev/tasks').mkdir(parents=True)
        (self.ticket / 'worktree').mkdir()

    @staticmethod
    def mounts(cmd):
        return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == '-v']

    def test_role_container_cannot_write_control_artifacts(self):
        cmd = EngineRunner._wrap_in_role_container(
            ['true'], self.ticket / 'worktree', writable_worktree=self.ticket / 'worktree')
        self.assertIn(f'{self.repo}:{self.repo}:ro', self.mounts(cmd))
        journal = self.ticket / '03-dev/tasks/task-journal.jsonl'
        for mount in self.mounts(cmd):
            source, _, mode = mount.rpartition(':')
            if mode == 'rw' and not source.startswith('ssdlc-shared-'):
                with self.subTest(mount=mount):
                    self.assertFalse(journal.is_relative_to(Path(source.partition(':')[0])))

    def test_verification_container_hides_the_ssdlc_directory(self):
        cmd = EngineRunner._wrap_in_role_container(['true'], self.repo, pass_ai_keys=False)
        self.assertIn(f'{self.repo}:{self.repo}:ro', self.mounts(cmd))
        self.assertIn(f'{self.repo / ".ssdlc"}:rw,size=16m', cmd)


class ThreatModelIsDocumented(unittest.TestCase):
    def test_limits_and_non_claims_are_documented(self):
        corpus = docs.plan_section('docs/architecture.md', 'Task journal: guarantees and threat model',
                                   root=docs.ROOT)
        docs.assert_documented(self, corpus, [
            'integrity checks, not authenticated provenance', 'not signatures', 'Whole-log rewrite',
            'Truncation', 'Rollback and replay', 'independent trust anchor', 'Trust boundary',
            'outside every developer and test sandbox', 'product decision'])
        docs.assert_documented(self, docs.user_facing_docs(), ['task journal threat model'])


if __name__ == '__main__':
    unittest.main()
