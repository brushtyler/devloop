"""Conformance of the ports of devloop (#138, ADR 0001).

The ports ``Tracker``, ``CodeReview`` and ``CI`` declare only the methods the dispatcher calls;
the ``GitHub`` adapter and every fake of the tests expose each of them with the same signature
(``inspect.signature``).
"""

import ast
import inspect
import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

from devloop import ports
from devloop.apps import Identities
from devloop.github import GitHub, GitHubError, GitHubNotFound, GitHubUncertain
from helpers.devloop_fakes import (FIRST_COMMENT_ID, ActingAs, FakeGH, FakeIdentities,
                                   SnapshotGH)
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
    ports.CodeReview: ['open_prs', 'merged_prs', 'find_pr', 'branch_prs', 'merge_state', 'pr_head', 'pr_state', 'merge_pr',
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


class ScriptedGh:
    """The ``run`` of a ``GitHub`` adapter: a repository's comments, written by ``login``, and
    the failure of the next write. ``fail`` is ``(stderr, applied)``: the write exits 1 with
    ``stderr`` after taking effect or not; ``timeout`` with ``applied`` raises TimeoutExpired
    instead. The comments are read back as ``gh api --paginate`` prints them."""

    def __init__(self, login='brushtyler'):
        self.login, self.comments, self.calls = login, [], []
        self.fail = None

    def __call__(self, cmd, input=None, timeout=None, **_):
        self.calls.append(cmd)
        if 'POST' in cmd or 'PATCH' in cmd:
            return self._write(cmd, input, timeout)
        lines = [json.dumps({'id': c['id'], 'author': {'login': c['login']}, 'body': c['body'],
                             'created_at': '2026-11-18T10:00:00Z'})
                 for c in self.comments if f'/issues/{c["number"]}/' in cmd[-3]]
        return SimpleNamespace(returncode=0, stdout='\n'.join(lines), stderr='')

    def _write(self, cmd, body, timeout):
        fail, self.fail = self.fail, None
        if fail is None or fail[1]:
            if 'POST' in cmd:
                number = int(cmd[4].split('/')[-2])
                self.comments.append({'id': 9000 + len(self.comments), 'number': number,
                                      'login': self.login, 'body': body})
            else:
                comment_id = int(cmd[4].rsplit('/', 1)[-1])
                for c in self.comments:
                    if c['id'] == comment_id:
                        c['body'] = body
        if fail is None:
            return SimpleNamespace(returncode=0, stdout=json.dumps({'id': self.comments[-1]['id']}),
                                   stderr='')
        if fail[0] == 'timeout':
            raise subprocess.TimeoutExpired(cmd, timeout)
        return SimpleNamespace(returncode=1, stdout='', stderr=fail[0])


def adapter():
    run = ScriptedGh()
    return GitHub('o/r', run=run), run


def fake():
    gh = FakeGH()
    gh.with_ids = True
    return gh, gh


# What `gh` reports for a failed write, and whether the write may have taken effect anyway.
UNCERTAIN = ('gh: Server Error (HTTP 502)',
             'HTTP 503: Service Unavailable (https://api.github.com/repos/o/r/issues/7/comments)',
             'Post "https://api.github.com/repos/o/r/issues/7/comments": read tcp '
             '10.0.0.2:51234->140.82.121.6:443: read: connection reset by peer',
             'Post "https://api.github.com/repos/o/r/issues/7/comments": unexpected EOF',
             'timeout')
CERTAIN = ('gh: Validation Failed (HTTP 422)',
           'gh: Resource not accessible by integration (HTTP 403)',
           'Post "https://api.github.com/repos/o/r/issues/7/comments": dial tcp: lookup '
           'api.github.com: no such host',
           'Post "https://api.github.com/repos/o/r/issues/7/comments": dial tcp '
           '140.82.121.6:443: connect: connection refused',
           'error connecting to api.github.com\ncheck your internet connection',
           'Post "https://api.github.com/repos/o/r/issues/7/comments": net/http: TLS handshake '
           'timeout')


class UncertainOutcomeTests(unittest.TestCase):
    """``comment`` and ``edit_comment`` tell an uncertain outcome apart (#364), in the adapter
    and in the fake, and ``ports.reconcile_comment`` tells whether it took effect."""

    def test_an_uncertain_outcome_is_a_github_error(self):
        # The callers that handle a failed write handle an uncertain one the same way.
        self.assertTrue(issubclass(GitHubUncertain, GitHubError))
        self.assertFalse(issubclass(GitHubUncertain, GitHubNotFound))

    def test_the_adapter_tells_uncertain_writes_apart(self):
        for stderr, uncertain in [*((s, True) for s in UNCERTAIN),
                                  *((s, False) for s in CERTAIN)]:
            for write in ('comment', 'edit_comment'):
                with self.subTest(stderr=stderr, write=write):
                    gh, run = adapter()
                    run.comments.append({'id': 9000, 'number': 7, 'login': 'brushtyler',
                                         'body': 'old'})
                    run.fail = (stderr, False)
                    with self.assertRaises(GitHubError) as raised:
                        gh.comment(7, 'x') if write == 'comment' else gh.edit_comment(9000, 'x')
                    self.assertIs(isinstance(raised.exception, GitHubUncertain), uncertain)

    def test_the_adapter_keeps_certain_failures(self):
        gh, run = adapter()
        run.fail = ('gh: Not Found (HTTP 404)', False)
        with self.assertRaises(GitHubNotFound) as raised:
            gh.edit_comment(9000, 'x')
        self.assertNotIsInstance(raised.exception, GitHubUncertain)

        def cannot_start(*args, **kwargs):
            raise OSError('gh: not found')
        for build in (lambda: GitHub('o/r', run=cannot_start),
                      lambda: GitHub('o/r', run=ScriptedGh(), time_left=lambda: 0)):
            gh = build()
            for write in (lambda: gh.comment(7, 'x'), lambda: gh.edit_comment(9000, 'x')):
                with self.subTest(gh=gh), self.assertRaises(GitHubError) as raised:
                    write()
                self.assertNotIsInstance(raised.exception, GitHubUncertain)

    def test_other_calls_keep_their_errors(self):
        # Only comment and edit_comment have an uncertain outcome (#364): the other writes and
        # the reads report every failure as before.
        def timed_out(cmd, timeout=None, **kwargs):
            raise subprocess.TimeoutExpired(cmd, timeout)

        def server_error(cmd, **kwargs):
            return SimpleNamespace(returncode=1, stdout='', stderr='gh: Server Error (HTTP 502)')
        for run in (timed_out, server_error):
            gh = GitHub('o/r', run=run)
            for call in (lambda: gh.pr_comments(7), lambda: gh.edit_labels(7, add=['x']),
                         lambda: gh.close_issue(7),
                         lambda: gh.submit_review(7, commit='a', event='COMMENT', body='b')):
                with self.subTest(run=run.__name__), self.assertRaises(GitHubError) as raised:
                    call()
                self.assertNotIsInstance(raised.exception, GitHubUncertain)

    def test_the_adapter_and_the_fake_agree_on_uncertain_writes(self):
        # The same uncertain write, published or not, then reconciled: the adapter on a
        # scripted `gh`, the fake in memory.
        for build in (adapter, fake):
            for applied in (True, False):
                with self.subTest(implementation=build.__name__, applied=applied):
                    gh, backend = build()
                    published = gh.comment(7, 'first')
                    self.assertIsNotNone(published)
                    self.assertEqual(ports.reconcile_comment(gh, 7, 'brushtyler', 'first')['id'],
                                     published)
                    self.uncertain(backend, 'timeout', applied)
                    with self.assertRaises(GitHubUncertain):
                        gh.comment(7, 'second')
                    found = ports.reconcile_comment(gh, 7, 'brushtyler', 'second')
                    self.assertEqual(found is not None, applied)
                    self.uncertain(backend, 'gh: Server Error (HTTP 502)', applied)
                    with self.assertRaises(GitHubUncertain):
                        gh.edit_comment(published, 'first, edited')
                    found = ports.reconcile_comment(gh, 7, 'brushtyler', 'first, edited',
                                                    comment_id=published)
                    self.assertEqual(found['id'] if found else None,
                                     published if applied else None)

    @staticmethod
    def uncertain(backend, stderr, applied):
        if isinstance(backend, FakeGH):
            backend.uncertain_writes = ['applied' if applied else 'lost']
        else:
            backend.fail = (stderr, applied)


class ReconciliationTests(unittest.TestCase):
    """``ports.reconcile_comment`` with the fake (#364)."""

    def test_a_published_comment_is_found(self):
        gh = FakeGH()
        gh.uncertain_writes = ['applied']
        with self.assertRaises(GitHubUncertain):
            gh.comment(7, 'record')
        self.assertEqual(gh.comments, [(7, 'record')])
        found = ports.reconcile_comment(gh, 7, 'brushtyler', 'record')
        self.assertEqual((found['author']['login'], found['body']), ('brushtyler', 'record'))
        # Another issue or PR does not carry it.
        self.assertIsNone(ports.reconcile_comment(gh, 8, 'brushtyler', 'record'))

    def test_a_lost_comment_is_not_published(self):
        gh = FakeGH()
        gh.uncertain_writes = ['lost']
        with self.assertRaises(GitHubUncertain):
            gh.comment(7, 'record')
        self.assertEqual(gh.comments, [])
        self.assertIsNone(ports.reconcile_comment(gh, 7, 'brushtyler', 'record'))
        # The next write is not uncertain any more.
        gh.comment(7, 'record')
        self.assertIsNotNone(ports.reconcile_comment(gh, 7, 'brushtyler', 'record'))

    def test_only_the_same_author_counts(self):
        gh = SnapshotGH([{'author': {'login': 'stranger'}, 'body': 'record <!-- m:1 -->',
                          'created_at': '2026-01-01T00:00:00Z'}])
        gh.uncertain_writes = ['lost']
        with self.assertRaises(GitHubUncertain):
            gh.comment(100, 'record <!-- m:1 -->')
        self.assertIsNone(ports.reconcile_comment(gh, 100, 'brushtyler', 'record <!-- m:1 -->'))
        self.assertIsNone(ports.reconcile_comment(gh, 100, 'brushtyler', 'x', marker='m:1'))
        self.assertIsNotNone(ports.reconcile_comment(gh, 100, 'stranger', 'record <!-- m:1 -->'))

    def test_a_marker_finds_the_comment(self):
        gh = FakeGH()
        gh.uncertain_writes = ['applied']
        with self.assertRaises(GitHubUncertain):
            gh.comment(7, 'header\nrecord <!-- devloop:outbox 42 -->')
        found = ports.reconcile_comment(gh, 7, 'brushtyler', 'other text',
                                        marker='<!-- devloop:outbox 42 -->')
        self.assertEqual(found['body'], 'header\nrecord <!-- devloop:outbox 42 -->')
        self.assertIsNone(ports.reconcile_comment(gh, 7, 'brushtyler', 'other text',
                                                  marker='<!-- devloop:outbox 43 -->'))
        # An empty marker matches nothing.
        self.assertIsNone(ports.reconcile_comment(gh, 7, 'brushtyler', 'other text', marker=''))

    def test_an_edit_is_reconciled_on_its_comment(self):
        gh = FakeGH()
        gh.with_ids = True
        first, second = gh.comment(7, 'v1'), gh.comment(7, 'v2')
        self.assertEqual((first, second), (FIRST_COMMENT_ID, FIRST_COMMENT_ID + 1))
        gh.uncertain_writes = ['lost', 'applied']
        with self.assertRaises(GitHubUncertain):
            gh.edit_comment(first, 'v2')
        # Comment ``second`` has the same body, but it is not the edited comment.
        self.assertIsNone(ports.reconcile_comment(gh, 7, 'brushtyler', 'v2', comment_id=first))
        self.assertEqual(ports.reconcile_comment(gh, 7, 'brushtyler', 'v2')['id'], second)
        with self.assertRaises(GitHubUncertain):
            gh.edit_comment(first, 'v3')
        self.assertEqual(gh.comment_edits, [(first, 'v3')])
        self.assertEqual(
            ports.reconcile_comment(gh, 7, 'brushtyler', 'v3', comment_id=first)['id'], first)
        with self.assertRaises(GitHubNotFound):
            gh.edit_comment(FIRST_COMMENT_ID + 5, 'v4')

    def test_several_authors_and_excluded_comments(self):
        gh = FakeGH()
        gh.with_ids = True
        first, second = gh.comment(7, 'record'), gh.comment(7, 'record')
        # One of several logins counts (#385: the ambient login, one of the trusted users).
        self.assertEqual(
            ports.reconcile_comment(gh, 7, ['someone', 'brushtyler'], 'record')['id'], first)
        self.assertIsNone(ports.reconcile_comment(gh, 7, ['someone'], 'record'))
        # A comment known as another write's publication is skipped.
        self.assertEqual(
            ports.reconcile_comment(gh, 7, 'brushtyler', 'record', exclude={first})['id'], second)
        self.assertIsNone(
            ports.reconcile_comment(gh, 7, 'brushtyler', 'record', exclude={first, second}))

    def test_an_actor_client_reports_the_uncertain_outcome(self):
        fake = FakeGH()
        fake.uncertain_writes = ['applied']
        client = FakeIdentities(fake).github('claude', {'issues': 'write'})
        with self.assertRaises(GitHubUncertain):
            client.comment(7, 'x')
        self.assertIsNotNone(ports.reconcile_comment(client, 7, 'brushtyler', 'x'))

    def test_the_fake_rejects_an_unknown_outcome(self):
        gh = FakeGH()
        gh.uncertain_writes = ['maybe']
        with self.assertRaises(ValueError):
            gh.comment(7, 'x')


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
