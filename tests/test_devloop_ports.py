"""Conformance of the ports of devloop (#138, ADR 0001).

The ports ``Tracker``, ``CodeReview`` and ``CI`` declare only the methods the dispatcher calls;
the ``GitHub`` adapter and every fake of the tests expose each of them with the same signature
(``inspect.signature``).
"""

import ast
import inspect
import unittest
from pathlib import Path

from devloop import ports
from devloop.apps import Identities
from devloop.github import GitHub
from helpers.devloop_fakes import ActingAs, FakeGH, FakeIdentities, SnapshotGH
from test_devloop_acceptance import IssueGH as AcceptanceIssueGH
from test_devloop_acceptance import SnapshotIssueGH
from test_devloop_ledger import LedgerGH, SnapLedgerGH
from test_devloop_merge import Identities as MergeIdentities
from test_devloop_merge import MergeGH
from test_devloop_preflight import IssueGH as PreflightIssueGH
from test_devloop_priority import QueueGH
from test_devloop_quorum import Identities as QuorumIdentities
from test_devloop_quorum import QuorumGH
from test_devloop_supervisor_labels import HistoryGH
from test_devloop_supervisor_labels import QueueGH as SupervisorQueueGH
from test_devloop_ports_equivalence import TrackerGH

PACKAGE = Path(__file__).resolve().parents[1] / 'devloop'
# The modules that act through the ports: the dispatcher (with ensure_labels) and its ledger.
CLIENTS = ('dispatcher.py', 'ledger.py', 'context.py', 'dispatch_supervision.py',
           'dispatch_preflight.py', 'dispatch_merge.py', 'dispatch_actions.py')

EXPECTED = {
    ports.Tracker: ['issues_with_label', 'issue', 'issue_history', 'issue_comments',
                    'pr_comments', 'issue_dependencies', 'labelers', 'label_events', 'labeled_events', 'edit_labels',
                    'comment', 'close_issue', 'edit_comment', 'ensure_label', 'repo_labels',
                    'rename_label'],
    ports.CodeReview: ['open_prs', 'merged_prs', 'find_pr', 'merge_state', 'pr_head', 'pr_state', 'merge_pr',
                       'pr_reviews', 'create_pr', 'submit_review'],
    ports.CI: ['checks', 'check_details', 'check_runs', 'failed_log', 'job_log', 'rerun_failed_jobs',
               'commit_date', 'required_checks'],
}
# Fakes defined as classes: their methods are compared as the adapter's, unbound.
FAKE_CLASSES = (FakeGH, SnapshotGH, AcceptanceIssueGH, SnapshotIssueGH, LedgerGH, SnapLedgerGH,
                PreflightIssueGH, QueueGH, QuorumGH, TrackerGH, MergeGH, SupervisorQueueGH,
                HistoryGH)
# The same fakes as the tests build them: an instance attribute must not hide a port method.
FAKE_INSTANCES = (FakeGH, lambda: SnapshotGH([]), lambda: AcceptanceIssueGH('body'),
                  lambda: SnapshotIssueGH('body'), LedgerGH, SnapLedgerGH, PreflightIssueGH,
                  lambda: QueueGH({}), QuorumGH, TrackerGH, MergeGH,
                  lambda: SupervisorQueueGH({}),
                  lambda: HistoryGH('brushtyler', 'brushtyler-devloop-agy-agent[bot]'))


def called_attributes(path):
    """The names of every ``x.name(...)`` call in ``path``."""
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    return {node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}


def unbound(signature):
    """``signature`` without its first parameter (``self``)."""
    return signature.replace(parameters=list(signature.parameters.values())[1:])


def public_methods(cls):
    return {name for name in dir(cls)
            if not name.startswith('_') and callable(getattr(cls, name))}


class PortDeclarationTests(unittest.TestCase):
    def test_each_port_declares_its_methods(self):
        for port, names in EXPECTED.items():
            with self.subTest(port=port.__name__):
                self.assertEqual(ports.port_methods(port), names)
        everything = [name for port in ports.PORTS for name in ports.port_methods(port)]
        self.assertEqual(len(everything), len(set(everything)), 'a method in two ports')
        self.assertEqual(set(ports.port_methods(ports.Backend)), set())
        self.assertEqual(public_methods(ports.Backend), set(everything))

    def test_the_ports_hold_only_what_the_dispatcher_calls(self):
        called = set().union(*(called_attributes(PACKAGE / name) for name in CLIENTS))
        declared = {name for port in ports.PORTS for name in ports.port_methods(port)}
        self.assertEqual(declared - called, set(), 'port methods the dispatcher never calls')
        # Whatever of the adapter the dispatcher calls is in a port.
        self.assertEqual((public_methods(GitHub) & called) - declared, set())
        # The adapter's methods outside the ports, documented in devloop/ports.py.
        self.assertEqual(public_methods(GitHub) - declared,
                         {'last_labeler', 'open_issues', 'comments_since', 'issue_state',
                          'issue_author', 'dated_comments', 'search_prs', 'edit_issue_body'})

    def test_the_dispatcher_depends_on_the_ports(self):
        from devloop import dispatcher, ledger
        hints = inspect.get_annotations(dispatcher.Dispatcher.__init__)
        self.assertIs(hints['gh'], ports.Backend)
        self.assertIs(inspect.get_annotations(dispatcher.Dispatcher._as)['return'], ports.Backend)
        self.assertIs(inspect.get_annotations(dispatcher.ensure_labels)['gh'], ports.Tracker)
        self.assertIs(inspect.get_annotations(ledger.Ledger.__init__)['gh'], ports.Tracker)
        self.assertIs(inspect.get_annotations(Identities.github)['return'], ports.Backend)
        source = (PACKAGE / 'dispatcher.py').read_text(encoding='utf-8')
        self.assertNotIn('import GitHub,', source)
        self.assertNotIn('GitHub(', source)


class ConformanceTests(unittest.TestCase):
    def assert_conforms(self, implementation, *, bound):
        for port in ports.PORTS:
            for name in ports.port_methods(port):
                with self.subTest(implementation=implementation, port=port.__name__, method=name):
                    method = getattr(implementation, name, None)
                    self.assertTrue(callable(method), f'{name} missing')
                    expected = inspect.signature(getattr(port, name))
                    self.assertEqual(inspect.signature(method),
                                     unbound(expected) if bound else expected)

    def test_the_github_adapter_implements_every_port(self):
        self.assert_conforms(GitHub, bound=False)
        gh = GitHub('o/r', run=None)
        self.assert_conforms(gh, bound=True)
        for port in (*ports.PORTS, ports.Backend):
            self.assertIsInstance(gh, port)
        # Identities hands out the adapter, for the ambient login as for an app.
        self.assertIsInstance(Identities('o/r', {}).github('dispatcher'), ports.Backend)

    def test_every_fake_matches_the_adapter(self):
        for fake in FAKE_CLASSES:
            with self.subTest(fake=fake.__name__):
                self.assert_conforms(fake, bound=False)
        for build in FAKE_INSTANCES:
            instance = build()
            with self.subTest(fake=type(instance).__name__):
                self.assert_conforms(instance, bound=True)
                self.assertIsInstance(instance, ports.Backend)
        # The level-0 events of the preflight fake are callable, not its own list (R2-1).
        self.assertEqual(PreflightIssueGH().label_events(7, 'reviews:0'), [])
        # An actor's client resolves the port methods on access (isinstance cannot see them).
        fake, quorum = FakeGH(), QuorumGH()
        for instance in (ActingAs(fake, 'claude', None),
                         FakeIdentities(fake).github('codex', {'issues': 'write'}),
                         QuorumIdentities(quorum).github('claude', {'issues': 'write'}),
                         MergeIdentities(MergeGH()).github('agy', {'issues': 'write'})):
            with self.subTest(fake=type(instance).__name__):
                self.assert_conforms(instance, bound=True)
        # The quorum's client still writes with its actor's login, and reaches only the ports.
        client = QuorumIdentities(quorum).github('claude')
        client.comment(100, 'x')
        self.assertEqual((quorum.written, quorum.actor), (['claude-agent[bot]'], 'dispatcher'))
        with self.assertRaises(AttributeError):
            client.written

    def test_an_actor_reaches_only_the_ports(self):
        fake = FakeGH()
        client = FakeIdentities(fake).github('claude', {'issues': 'write'})
        client.comment(7, 'x')
        self.assertEqual(fake.comments, [(7, 'x')])
        self.assertEqual(fake.acts, [('claude', {'issues': 'write'}, 'comment')])
        for name in ('labels', 'comments', 'created', 'last_labeler'):
            with self.subTest(name=name), self.assertRaises(AttributeError):
                getattr(client, name)

    def test_the_fake_writes_stay_in_memory(self):
        fake = FakeGH()
        fake.edit_comment(5, 'new')
        fake.ensure_label('agent:ready', '0e8a16', 'd')
        self.assertEqual(fake.comment_edits, [(5, 'new')])
        self.assertEqual(fake.label_styles, {'agent:ready': ('0e8a16', 'd')})
        self.assertEqual(fake.issue_comments(7), [])
        fake.comment(7, 'hello')
        self.assertEqual(fake.issue_comments(7), fake.pr_comments(7))
        self.assertEqual(fake.merge_state({'mergeable': 'CONFLICTING'}), 'conflict')
        self.assertEqual(fake.merge_state({}), 'unknown')
        self.assertEqual((fake.labeled_events(7), fake.check_details('a' * 40)), ([], []))


class SummaryTests(unittest.TestCase):
    def test_the_adapter_module_keeps_the_ports_summary(self):
        from devloop import github
        self.assertIs(github.summarize_checks, ports.summarize_checks)

    def test_merge_state_of_the_adapter(self):
        gh = GitHub('o/r', run=None)
        cases = [({'mergeable': 'CONFLICTING', 'mergeStateStatus': 'UNKNOWN'}, 'conflict'),
                 ({'mergeable': 'MERGEABLE', 'mergeStateStatus': 'DIRTY'}, 'conflict'),
                 ({'mergeable': 'MERGEABLE', 'mergeStateStatus': 'UNKNOWN'}, 'unknown'),
                 ({}, 'unknown'),
                 ({'mergeable': 'MERGEABLE', 'mergeStateStatus': 'BLOCKED'}, 'clean')]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(gh.merge_state(raw), expected)


if __name__ == '__main__':
    unittest.main()
