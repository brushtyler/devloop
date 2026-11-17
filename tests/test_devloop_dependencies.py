"""devloop: native dependencies between issues, "blocked by" (#277)."""

import dataclasses
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import doc_contracts as docs
from devloop import markdown
from devloop.dispatcher import Dispatcher, dependency_cycles, plan, queue_lines, start_order
from devloop.github import GitHub
from helpers.devloop_fakes import FakeIdentities
from test_devloop import CFG, FakeRunner, FakeWS
from test_devloop_preflight import PCFG
from test_devloop_priority import QueueGH, added, at

READY, HIGH = 'agent:ready', 'priority:high'
REPO = CFG.repo
# Room for every ready issue: dependencies and order are the only things that decide.
DCFG = dataclasses.replace(CFG, max_open_prs=10, max_actions_per_tick=10)
DPCFG = dataclasses.replace(PCFG, max_open_prs=10, max_actions_per_tick=10)
SUPERVISOR_LOGIN = FakeIdentities.LOGINS['agy']


def block(number, state='OPEN', by='brushtyler', repo=REPO):
    """``(blocker, event)``: issue ``number`` blocks, added by ``by``."""
    return ({'repo': repo, 'number': number, 'state': state},
            {'repo': repo, 'number': number, 'by': by, 'at': at(10)})


class DependencyGH(QueueGH):
    """``QueueGH`` with native dependencies: ``blocks`` maps an issue to its blocks, as
    ``block`` returns them; ``authors`` to its author (``brushtyler`` by default)."""

    def __init__(self, issues, blocks=None, authors=None):
        super().__init__(issues)
        self.blocks = blocks or {}
        self.authors = authors or {}
        self.dependency_reads = []

    def issue_dependencies(self, number):
        self.dependency_reads.append(number)
        pairs = self.blocks.get(number, [])
        return {'author': self.authors.get(number, 'brushtyler'),
                'blocked_by': [dict(b) for b, _ in pairs], 'added': [dict(e) for _, e in pairs]}

    def issue_history(self, number):
        self.history_reads = getattr(self, 'history_reads', []) + [number]
        return super().issue_history(number)


def ready(*numbers, day=1):
    return {n: ({READY}, [added(READY, day)]) for n in numbers}


class DependencyTests(unittest.TestCase):
    def make(self, issues, blocks=None, cfg=DCFG, **kw):
        self.gh, self.logs = DependencyGH(issues, blocks, **kw), []
        return Dispatcher(cfg, self.gh, FakeWS(), FakeRunner(), log=self.logs.append,
                          identities=kw.get('identities'))

    def actions(self, d, cfg=DCFG):
        return [(a.kind, a.number) for a in plan(d.snapshot(), cfg)]

    def test_an_issue_blocked_by_an_open_issue_does_not_start_until_the_block_closes(self):
        # #2 is blocked by #1 (in the queue) and #3 by #9 (not in the queue).
        blocks = {2: [block(1)], 3: [block(9)]}
        d = self.make(ready(1, 2, 3), blocks)
        snap = d.snapshot()
        self.assertEqual({i['number']: i['blocked_by'] for i in snap.ready_issues},
                         {1: [], 2: ['#1'], 3: ['#9']})
        self.assertEqual(self.actions(d), [('implement', 1)])
        # The prerequisites outside the queue are read too, to look for cycles.
        self.assertEqual(sorted(set(self.gh.dependency_reads)), [1, 2, 3, 9])
        # A blocked issue takes no slot: with one slot, the free issue gets it.
        one = dataclasses.replace(DCFG, max_open_prs=1)
        self.assertEqual(self.actions(d, one), [('implement', 1)])
        # #1 is done and closed, #9 too: both start, with nobody queuing them again.
        self.gh.queue.pop(1)
        blocks[2] = [block(1, 'CLOSED')]
        blocks[3] = [block(9, 'CLOSED')]
        self.assertEqual(self.actions(d), [('implement', 2), ('implement', 3)])

    def test_a_blocked_issue_is_not_checked_by_the_readiness_check(self):
        d = self.make(ready(1, 2), {2: [block(1)]}, cfg=DPCFG)
        snap = d.snapshot()
        self.assertEqual(self.actions(d, DPCFG), [('preflight', 1)])
        # Its check is not even read.
        self.assertNotIn('preflight', next(i for i in snap.ready_issues if i['number'] == 2))
        self.assertNotIn(2, self.gh.history_reads)
        self.gh.blocks[2] = [block(1, 'CLOSED')]
        self.assertEqual(self.actions(d, DPCFG), [('preflight', 1), ('preflight', 2)])

    def test_a_dry_run_tick_starts_nothing_blocked(self):
        d = self.make(ready(1, 2), {2: [block(1)]})
        self.assertEqual([(a.kind, a.number) for a in d.tick(dry_run=True)], [('implement', 1)])
        self.assertIn(f'[dry-run] ready 2. #2 priority=normal agent:ready={at(1)} labelled by '
                      'brushtyler, blocked by #1: Issue 2', self.logs)

    def test_a_block_added_by_an_untrusted_author_blocks_nothing(self):
        blocks = {2: [block(1, by='stranger')], 3: [block(1, by=None)]}
        d = self.make(ready(1, 2, 3), blocks)
        self.assertEqual(self.actions(d), [('implement', 1), ('implement', 2), ('implement', 3)])
        self.assertIn('  issue #2: ignored the block by #1, added by stranger: neither a trusted '
                      'user nor the supervisor', self.logs)
        self.assertIn('  issue #3: ignored the block by #1, added by an unknown author: neither '
                      'a trusted user nor the supervisor', self.logs)
        # The latest event that added the block decides: a stranger's re-addition voids it.
        blocker, trusted = block(1)
        _, stranger = block(1, by='stranger')
        self.gh.blocks = {2: [(blocker, trusted)]}
        self.gh.blocks[2].append((blocker, stranger))
        snap = d.snapshot()
        self.assertEqual(next(i for i in snap.ready_issues if i['number'] == 2)['blocked_by'], [])

    def test_a_block_from_the_supervisor_counts_only_on_issues_it_may_queue(self):
        cfg = dataclasses.replace(DCFG, supervisor='agy')
        gh = DependencyGH(ready(1, 2, 3), {2: [block(1, by=SUPERVISOR_LOGIN)],
                                           3: [block(1, by=SUPERVISOR_LOGIN)]},
                          authors={3: 'stranger'})
        d = Dispatcher(cfg, gh, FakeWS(), FakeRunner(), log=lambda m: None,
                       identities=FakeIdentities(gh))
        self.assertEqual({i['number']: i['blocked_by'] for i in d.snapshot().ready_issues},
                         {1: [], 2: ['#1'], 3: []})

    def test_an_issue_that_blocks_ready_issues_starts_first_with_the_same_priority(self):
        issues = {1: ({READY}, [added(READY, 1)]),
                  2: ({READY}, [added(READY, 5)]),
                  3: ({READY}, [added(READY, 2)]),
                  4: ({READY, HIGH}, [added(READY, 9), added(HIGH, 9)])}
        # #3 is blocked by #2: #2 goes before #1, although #1 was queued earlier; priority still
        # comes first.
        d = self.make(issues, {3: [block(2)]})
        snap = d.snapshot()
        self.assertEqual({i['number']: i['blocks'] for i in snap.ready_issues},
                         {1: [], 2: [3], 3: [], 4: []})
        self.assertEqual([a.number for a in plan(snap, DCFG)], [4, 2, 1])
        self.assertEqual([i['number'] for i in sorted(snap.ready_issues, key=start_order)],
                         [4, 2, 1, 3])
        # Without the dependency, the time of agent:ready decides.
        self.gh.blocks = {}
        self.assertEqual([a.number for a in plan(d.snapshot(), DCFG)], [4, 1, 3, 2])

    def test_a_cycle_is_reported_once_and_its_issues_do_not_start(self):
        d = self.make(ready(1, 2, 3), {1: [block(2)], 2: [block(1)]})
        snap = d.snapshot()
        self.assertEqual({i['number']: i['cycle'] for i in snap.ready_issues},
                         {1: ['#1', '#2'], 2: ['#1', '#2'], 3: []})
        self.assertIn('  issue #1: dependency cycle #1, #2: it does not start', self.logs)
        self.assertEqual(self.actions(d), [('dependency_cycle', 1), ('dependency_cycle', 2),
                                           ('implement', 3)])
        self.assertEqual(plan(snap, DCFG)[0].reason, 'dependency cycle #1, #2')
        # The tick reports it, with a dispatcher comment and a cycle record, and adds no label.
        self.gh.queue.pop(3)
        d.tick()
        self.assertEqual([n for n, _ in self.gh.comments], [1, 2])
        for _, body in self.gh.comments:
            self.assertEqual(markdown.read_marker(body, 'cycle'), {'cycle': ['#1', '#2']})
            self.assertIn('Ciclo di dipendenze', body)
        self.assertEqual(self.gh.labels, {})
        # Reported once: the next tick plans nothing.
        self.assertEqual(self.actions(d), [])
        # The cycle is removed: both become eligible by themselves, #1 first since it blocks #2.
        self.gh.blocks = {2: [block(1)]}
        self.assertEqual(self.actions(d), [('implement', 1)])

    def test_a_cycle_through_an_issue_outside_the_queue_is_found(self):
        # Only #1 is ready; #5 has no agent:ready, and #1 blocks it back.
        d = self.make(ready(1), {1: [block(5)], 5: [block(1)]})
        snap = d.snapshot()
        self.assertEqual(snap.ready_issues[0]['cycle'], ['#1', '#5'])
        self.assertEqual(self.actions(d), [('dependency_cycle', 1)])

    def test_a_cycle_record_from_someone_else_does_not_count(self):
        d = self.make(ready(1, 2), {1: [block(2)], 2: [block(1)]})
        forged = markdown.render_cycle({'cycle': ['#1', '#2']})
        # Without apps the dispatcher signs with a trusted login; a stranger's record is ignored.
        self.gh.issue_comments = lambda n: [{'author': {'login': 'stranger'}, 'body': forged}]
        self.assertEqual(self.actions(d), [('dependency_cycle', 1), ('dependency_cycle', 2)])
        self.gh.issue_comments = lambda n: [{'author': {'login': 'brushtyler'}, 'body': forged}]
        self.assertEqual(self.actions(d), [])
        # Another cycle on the same issue is reported again.
        self.gh.blocks = {1: [block(2)], 2: [block(3)], 3: [block(1)]}
        self.gh.queue.update(ready(3))
        self.assertEqual(self.actions(d), [('dependency_cycle', 1), ('dependency_cycle', 2),
                                           ('dependency_cycle', 3)])

    def test_the_cycle_action_writes_nothing_when_the_cycle_is_gone(self):
        d = self.make(ready(1, 2), {1: [block(2)], 2: [block(1)]})
        self.gh.blocks = {}
        d._do_dependency_cycle(1)
        self.assertEqual(self.gh.comments, [])
        self.assertIn('  no longer on a dependency cycle', self.logs)

    def test_a_block_of_another_repository_counts_by_its_state_as_a_leaf(self):
        blocks = {1: [block(4, repo='other/repo')], 2: [block(4, 'CLOSED', repo='other/repo')]}
        d = self.make(ready(1, 2), blocks)
        snap = d.snapshot()
        self.assertEqual({i['number']: i['blocked_by'] for i in snap.ready_issues},
                         {1: ['other/repo#4'], 2: []})
        self.assertEqual(sorted(set(self.gh.dependency_reads)), [1, 2])
        self.assertEqual(self.actions(d), [('implement', 2)])
        # The same repository, written with another case, is this one.
        self.gh.blocks = {1: [block(2, repo=REPO.upper())]}
        self.assertEqual(d.snapshot().ready_issues[0]['blocked_by'], ['#2'])

    def test_status_shows_blocked_issues_and_by_whom(self):
        import devloop.__main__ as cli
        d = self.make(ready(1, 2, 3), {2: [block(1)], 3: [block(1), block(9)]})
        lines = []
        with mock.patch.object(cli, 'build', return_value=(DCFG, Path('/state'), d)), \
                mock.patch.object(cli, 'log', lines.append):
            cli.main(['status'])
        self.assertEqual([line for line in lines if line.startswith('ready ')], [
            f'ready 1. #1 priority=normal agent:ready={at(1)} labelled by brushtyler, '
            'blocks #2, #3: Issue 1',
            f'ready 2. #2 priority=normal agent:ready={at(1)} labelled by brushtyler, '
            'blocked by #1: Issue 2',
            f'ready 3. #3 priority=normal agent:ready={at(1)} labelled by brushtyler, '
            'blocked by #1, #9: Issue 3'])
        self.assertEqual([line for line in lines if line.startswith('next: ')],
                         ['next: implement #1'])

    def test_queue_lines_show_a_cycle(self):
        snap = SimpleNamespace(ready_issues=[{
            'number': 1, 'title': 'T', 'labeler': 'brushtyler', 'ready_at': at(1),
            'blocked_by': ['#2'], 'blocks': [2], 'cycle': ['#1', '#2']}])
        self.assertEqual(queue_lines(snap), [
            f'ready 1. #1 priority=normal agent:ready={at(1)} labelled by brushtyler, blocked '
            'by #2, blocks #2, dependency cycle #1, #2: T'])


class CycleTests(unittest.TestCase):
    def test_dependency_cycles(self):
        a, b, c, d = (('', n) for n in (1, 2, 3, 4))
        self.assertEqual(dependency_cycles({a: {b}, b: {c}, c: set()}), {})
        self.assertEqual(dependency_cycles({a: {b}, b: {a}, c: {a}}), {a: [a, b], b: [a, b]})
        self.assertEqual(dependency_cycles({a: {b}, b: {c}, c: {a}, d: {d}}),
                         {a: [a, b, c], b: [a, b, c], c: [a, b, c], d: [d]})
        # A block whose own blocks were not read ends the walk.
        self.assertEqual(dependency_cycles({a: {('x/y', 1)}}), {})


class MarkerTests(unittest.TestCase):
    def test_cycle_marker(self):
        body = markdown.render_cycle({'cycle': ['#1', '#2']})
        self.assertEqual(markdown.read_marker(body, 'cycle'), {'cycle': ['#1', '#2']})
        self.assertTrue(markdown.carries_record(body))
        for bad in ({}, {'cycle': []}, {'cycle': [1]}, {'cycle': ['']}, {'cycle': '#1'}):
            self.assertIsNone(markdown.read_marker(markdown.publishable('x', 'cycle', bad),
                                                   'cycle'))


class AdapterTests(unittest.TestCase):
    def test_dependencies_are_one_graphql_read(self):
        calls = []
        payload = {'data': {'repository': {'issue': {
            'author': {'__typename': 'User', 'login': 'brushtyler'},
            'blockedBy': {'nodes': [
                {'number': 5, 'state': 'OPEN', 'repository': {'nameWithOwner': 'o/r'}},
                {'number': 8, 'state': 'CLOSED', 'repository': {'nameWithOwner': 'x/y'}}]},
            'timelineItems': {'nodes': [
                {'__typename': 'BlockedByAddedEvent', 'createdAt': at(3),
                 'actor': {'__typename': 'Bot', 'login': 'sup'},
                 'blockingIssue': {'number': 5, 'repository': {'nameWithOwner': 'o/r'}}},
                {'__typename': 'BlockedByAddedEvent', 'createdAt': at(4), 'actor': None,
                 'blockingIssue': None}]}}}}}

        def fake_run(argv, **kw):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr='')

        self.assertEqual(GitHub('o/r', run=fake_run).issue_dependencies(7), {
            'author': 'brushtyler',
            'blocked_by': [{'repo': 'o/r', 'number': 5, 'state': 'OPEN'},
                           {'repo': 'x/y', 'number': 8, 'state': 'CLOSED'}],
            'added': [{'repo': 'o/r', 'number': 5, 'by': 'sup[bot]', 'at': at(3)}]})
        (argv,) = calls
        self.assertIn('graphql', argv)
        self.assertIn('number=7', argv)
        query = next(a for a in argv if a.startswith('query='))
        self.assertIn('blockedBy', query)
        self.assertIn('BLOCKED_BY_ADDED_EVENT', query)


class DocumentationTests(unittest.TestCase):
    def test_dependencies_are_documented(self):
        section = docs.plan_section('docs/devloop/guide.md', 'Dependencies between issues',
                                    root=docs.ROOT)
        docs.assert_documented(self, section, [
            'blocked by', 'does not start', 'readiness check', 'becomes eligible by itself',
            'trusted_users', 'supervisor', 'ignored', 'blocks other ready issues',
            'cycle', 'no label is added'], issues=(277,))
        protocol = docs.plan_section('docs/devloop/protocol.md', 'Dependency cycles',
                                     root=docs.ROOT)
        docs.assert_documented(self, protocol, ['cycle marker', 'dependency_cycle'],
                               issues=(277,))


if __name__ == '__main__':
    unittest.main()
