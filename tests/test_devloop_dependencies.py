"""devloop: native dependencies between issues, "blocked by" (#277)."""

import dataclasses
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import doc_contracts as docs
import test_devloop_supervision as supervision
from devloop import markdown
from devloop.dispatcher import Dispatcher, dependency_cycles
from devloop.github import GitHub, dropped_block
from devloop.planning import plan, queue_lines, start_order
from devloop.supervision import NON_CRITICAL, critical
from helpers.devloop_fakes import FakeIdentities
from test_devloop import CFG, FakeRunner, FakeWS, implemented
from test_devloop_preflight import PCFG, checked
from test_devloop_priority import QueueGH, added, at

READY, HIGH = 'agent:ready', 'priority:high'
REPO = CFG.repo
# Room for every ready issue: dependencies and order are the only things that decide.
DCFG = dataclasses.replace(CFG, max_open_prs=10, max_actions_per_tick=10)
DPCFG = dataclasses.replace(PCFG, max_open_prs=10, max_actions_per_tick=10)
SUPERVISOR_LOGIN = FakeIdentities.LOGINS['agy']


def block(number, state='OPEN', by='brushtyler', repo=REPO, reason=None):
    """``(blocker, event)``: issue ``number`` blocks, added by ``by``; ``reason``, the reason of
    its closure (#671)."""
    return ({'repo': repo, 'number': number, 'state': state, 'state_reason': reason},
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

    def test_a_prerequisite_closed_as_not_planned_escalates_to_the_operator(self):
        # #2 waits for #1, closed as not planned; #3 for #9, closed as completed; #4 for an
        # issue of another repository closed as a duplicate; #5 for one closed without a reason.
        blocks = {2: [block(1, 'CLOSED', reason='NOT_PLANNED')],
                  3: [block(9, 'CLOSED', reason='COMPLETED')],
                  4: [block(8, 'CLOSED', repo='other/repo', reason='DUPLICATE')],
                  5: [block(7, 'CLOSED')]}
        d = self.make(ready(2, 3, 4, 5), blocks)
        snap = d.snapshot()
        views = {i['number']: (i['blocked_by'], i['dropped']) for i in snap.ready_issues}
        self.assertEqual(views, {2: (['#1'], [['#1', 'NOT_PLANNED']]), 3: ([], []),
                                 4: (['other/repo#8'], [['other/repo#8', 'DUPLICATE']]),
                                 5: ([], [])})
        self.assertEqual(self.actions(d), [('prerequisite_dropped', 2),
                                           ('prerequisite_dropped', 4),
                                           ('implement', 3), ('implement', 5)])
        self.assertEqual(plan(snap, DCFG)[0].reason,
                         'prerequisite closed without being completed: #1')
        self.assertIn(f'ready 1. #2 priority=normal agent:ready={at(1)} labelled by brushtyler, '
                      'blocked by #1, prerequisite closed without being completed #1 (not '
                      'planned): Issue 2', queue_lines(snap))
        # The action escalates to the operator, critical, without agent:ready.
        d._do_prerequisite_dropped(2)
        (number, body), = self.gh.comments
        self.assertEqual(number, 2)
        notice = markdown.read_marker(body, 'notice')
        self.assertEqual((notice['class'], notice['critical'], notice['to']),
                         ('prerequisite_dropped', True, 'operator'))
        self.assertIn('#1 (not planned)', body)
        self.assertEqual(self.gh.labels[2], {'agent:needs-operator'})
        # Once with the operator, it is not escalated again.
        self.gh.queue[2] = ({READY, 'agent:needs-operator'}, [added(READY, 1)])
        self.assertNotIn(('prerequisite_dropped', 2), self.actions(d))

    def test_a_dropped_prerequisite_goes_to_the_operator_in_auto_too(self):
        self.assertTrue(critical('prerequisite_dropped'))
        self.assertNotIn('prerequisite_dropped', NON_CRITICAL)
        cfg = dataclasses.replace(supervision.AUTO, max_open_prs=10, max_actions_per_tick=10)
        d = self.make(ready(2), {2: [block(1, 'CLOSED', reason='NOT_PLANNED')]}, cfg=cfg)
        self.assertEqual(self.actions(d, cfg), [('prerequisite_dropped', 2)])
        d._do_prerequisite_dropped(2)
        notice = markdown.read_marker(self.gh.comments[0][1], 'notice')
        self.assertEqual(notice['to'], 'operator')
        self.assertEqual(self.gh.labels[2], {'agent:needs-operator'})

    def test_the_escalation_reads_the_block_again(self):
        # Removed, or reopened, since the plan: nothing is escalated.
        d = self.make(ready(2), {2: [block(1, 'CLOSED', reason='NOT_PLANNED')]})
        for blocks in ({}, {2: [block(1)]}):
            with self.subTest(blocks=blocks):
                self.gh.blocks = blocks
                d._do_prerequisite_dropped(2)
                self.assertEqual(self.gh.comments, [])
                self.assertIn('  no dropped prerequisite any more', self.logs)
        # Reopened, it blocks again as an open issue.
        self.assertEqual(d.snapshot().ready_issues[0]['blocked_by'], ['#1'])
        self.assertEqual(self.actions(d), [])

    def test_a_dropped_block_added_by_an_untrusted_author_blocks_nothing(self):
        d = self.make(ready(2), {2: [block(1, 'CLOSED', by='stranger', reason='NOT_PLANNED')]})
        self.assertEqual(self.actions(d), [('implement', 2)])
        self.assertIn('  issue #2: ignored the block by #1, added by stranger: neither a trusted '
                      'user nor the supervisor', self.logs)

    def test_orphan_prerequisites(self):
        # #2 waits for #1 (queued), #3 for #9 (nobody), #4 for #5 (in progress), #6 for
        # other/repo#7 (another repository: not judged) and #8 for #11, closed as not planned.
        issues = dict(ready(1, 2, 3, 4, 6, 8))
        issues[5] = ({'agent:working'}, [])
        blocks = {2: [block(1)], 3: [block(9)], 4: [block(5)], 6: [block(7, repo='other/repo')],
                  8: [block(11, 'CLOSED', reason='NOT_PLANNED')]}
        d = self.make(issues, blocks)
        snap = d.snapshot()
        self.assertEqual({i['number']: i['orphans'] for i in snap.ready_issues if 'orphans' in i},
                         {1: [], 2: [], 3: [9], 4: [], 6: [], 8: []})
        self.assertIn('  issue #3: orphan prerequisite #9: open, not queued, not in progress, '
                      'no open PR', self.logs)
        orphan = ('orphan prerequisite: #3 waits for #9, open, which has neither agent:ready nor '
                  'agent:working, nor an open PR, nor an authorization of the coordinator')
        self.assertEqual([line for line in queue_lines(snap) if line.startswith('orphan')],
                         [orphan])
        # Shown only: no action for it, so no label and no comment.
        self.assertNotIn(3, [a.number for a in plan(snap, DCFG)])
        # An open PR of the prerequisite is work in progress.
        issues_ = [{'number': 3, 'prerequisites': [9]}]
        d._orphan_prerequisites(issues_, set(), set(), {9}, None)
        self.assertEqual(issues_[0]['orphans'], [])
        # With a coordinator, its authorization that holds for the current text is too; the
        # text is read only when there is a record.
        for records, holds, orphans in (([], None, [9]), ([{}], None, [9]), ([{}], {}, [])):
            with self.subTest(records=records, holds=holds):
                issues_ = [{'number': n, 'prerequisites': [9]} for n in (3, 4)]
                with mock.patch.object(d, '_authorize_records', return_value=records), \
                        mock.patch.object(d, '_authorization', return_value=holds) as auth:
                    d._orphan_prerequisites(issues_, set(), set(), set(), 'coord[bot]')
                self.assertEqual([i['orphans'] for i in issues_], [orphans, orphans])
                self.assertEqual(auth.call_count, 1 if records else 0)  # once per prerequisite

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
        # #9 is open and nobody is doing it (#671): one line, after the queue.
        self.assertEqual([line for line in lines if line.startswith('orphan ')], [
            'orphan prerequisite: #3 waits for #9, open, which has neither agent:ready nor '
            'agent:working, nor an open PR, nor an authorization of the coordinator'])
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
            'blocked_by': [{'repo': 'o/r', 'number': 5, 'state': 'OPEN', 'state_reason': None},
                           {'repo': 'x/y', 'number': 8, 'state': 'CLOSED',
                            'state_reason': None}],
            'added': [{'repo': 'o/r', 'number': 5, 'by': 'sup[bot]', 'at': at(3)}]})
        (argv,) = calls
        self.assertIn('graphql', argv)
        self.assertIn('number=7', argv)
        query = next(a for a in argv if a.startswith('query='))
        self.assertIn('blockedBy', query)
        self.assertIn('stateReason', query)  # #671
        self.assertIn('BLOCKED_BY_ADDED_EVENT', query)
        self.assertFalse(any(a.startswith('before=') for a in argv))

    def test_the_events_are_read_back_until_every_open_block_has_its_addition(self):
        # #5, a trusted block still open, was added before 150 other additions: its event is
        # not among the latest 100, and is read on the previous page.
        def event(number, by, day):
            return {'__typename': 'BlockedByAddedEvent', 'createdAt': at(day),
                    'actor': {'__typename': 'User', 'login': by},
                    'blockingIssue': {'number': number, 'repository': {'nameWithOwner': 'o/r'}}}

        old = [event(5, 'brushtyler', 1)] + [event(100 + i, 'brushtyler', 2) for i in range(49)]
        recent = [event(200 + i, 'brushtyler', 3) for i in range(100)]
        blocked_by = [{'number': 5, 'state': 'OPEN', 'repository': {'nameWithOwner': 'o/r'}},
                      {'number': 9, 'state': 'CLOSED', 'repository': {'nameWithOwner': 'o/r'}}]
        pages = {None: (recent, {'hasPreviousPage': True, 'startCursor': 'C2'}),
                 'C2': (old, {'hasPreviousPage': False, 'startCursor': 'C1'})}
        calls = []

        def fake_run(argv, **kw):
            before = next((a[len('before='):] for a in argv if a.startswith('before=')), None)
            calls.append(before)
            nodes, info = pages[before]
            payload = {'data': {'repository': {'issue': {
                'author': {'__typename': 'User', 'login': 'brushtyler'},
                'blockedBy': {'nodes': blocked_by},
                'timelineItems': {'pageInfo': info, 'nodes': nodes}}}}}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr='')

        gh = GitHub('o/r', run=fake_run)
        read = gh.issue_dependencies(7)
        self.assertEqual(calls, [None, 'C2'])
        self.assertEqual(len(read['added']), 150)
        self.assertEqual(read['added'][0], {'repo': 'o/r', 'number': 5, 'by': 'brushtyler',
                                            'at': at(1)})
        self.assertEqual([e['number'] for e in read['added'][-2:]], [298, 299])
        # Through the dispatcher, the open block counts: #7 does not start.
        d = Dispatcher(dataclasses.replace(CFG, repo='o/r'), gh, FakeWS(), FakeRunner(),
                       log=lambda m: None)
        self.assertEqual(d._dependency_view([7])[7]['blocked_by'], ['#5'])
        # Once every open block has its event, no earlier page is read.
        calls.clear()
        blocked_by[0]['number'] = 250
        gh.issue_dependencies(7)
        self.assertEqual(calls, [None])
        # Nor when no block is open.
        calls.clear()
        blocked_by[0]['state'] = 'CLOSED'
        blocked_by[0]['number'] = 1
        gh.issue_dependencies(7)
        self.assertEqual(calls, [None])
        # A block closed as not planned (#671) needs its addition, as an open one does.
        calls.clear()
        blocked_by[0].update(number=5, stateReason='NOT_PLANNED')
        read = gh.issue_dependencies(7)
        self.assertEqual(calls, [None, 'C2'])
        self.assertEqual(read['blocked_by'][0]['state_reason'], 'NOT_PLANNED')
        self.assertEqual(d._dependency_view([7])[7]['dropped'], [['#5', 'NOT_PLANNED']])
        # One closed as completed does not.
        calls.clear()
        blocked_by[0]['stateReason'] = 'COMPLETED'
        gh.issue_dependencies(7)
        self.assertEqual(calls, [None])

    def test_dropped_block(self):
        for state, reason, dropped in (('CLOSED', 'NOT_PLANNED', True),
                                       ('CLOSED', 'DUPLICATE', True),
                                       ('CLOSED', 'not_planned', True),
                                       ('CLOSED', 'COMPLETED', False),
                                       ('CLOSED', None, False), ('CLOSED', '', False),
                                       ('OPEN', 'REOPENED', False), ('OPEN', None, False)):
            with self.subTest(state=state, reason=reason):
                self.assertEqual(dropped_block(state, reason), dropped)


class RetryTests(supervision.Case):
    """A supervisor's ``/devloop retry`` of an issue's step waits while the issue is blocked
    (#277), with no slot taken and no attempt spent, and runs once the block closes."""

    def blocked(self, state='OPEN'):
        """Issue #7 is blocked by #1, added by a trusted user, in ``state``."""
        pair = block(1, state)
        self.gh.issue_dependencies = lambda n: (
            {'author': 'brushtyler', 'blocked_by': [dict(pair[0])], 'added': [dict(pair[1])]}
            if n == 7 else {'author': 'brushtyler', 'blocked_by': [], 'added': []})

    def test_a_retry_of_a_blocked_issue_waits_for_the_block_to_close(self):
        cases = {'preflight': (supervision.PREFLIGHT_AUTO, checked(True)),
                 'implement': (supervision.AUTO, implemented())}
        for step, (cfg, output) in cases.items():
            with self.subTest(step=step):
                d = self.make(cfg, supervision.crash(), output, has_pr=False)
                self.assertEqual(self.tick(d), [(step, 7)])
                self.assertEqual(self.gh.labels[7] & {supervision.SUPER, supervision.HUMAN},
                                 {supervision.SUPER})
                # A trusted block is added after the failure, then the supervisor retries.
                self.blocked()
                self.gh.command(7, '/devloop retry')
                snap = d.snapshot()
                self.assertEqual(snap.supervised[0]['blocked_by'], ['#1'])
                self.assertEqual(plan(snap, cfg), [])
                self.assertEqual(self.tick(d), [])
                self.assertIn('  issue #7: the retry waits, blocked by #1', self.logs)
                # Nothing ran, nothing was spent.
                self.assertEqual(len(self.runner.calls), 1)
                self.assertEqual(self.decisions(7), [])
                self.assertIn(supervision.SUPER, self.gh.labels[7])
                # Run directly, as a plan made before the block would, it waits too.
                d._do_supervise(7)
                self.assertIn('  the retry waits, blocked by #1', self.logs)
                self.assertEqual(len(self.runner.calls), 1)
                self.assertEqual(self.decisions(7), [])
                self.assertEqual(self.gh.created, [])
                # The block closes: the same command runs, with nobody repeating it.
                self.blocked('CLOSED')
                self.assertEqual(self.tick(d), [('supervise', 7)])
                self.assertEqual(len(self.runner.calls), 2)
                decision = self.decisions(7)[-1]
                self.assertEqual((decision['outcome'], decision['attempt']), ('applied', 1))
                if step == 'implement':
                    self.assertEqual(len(self.gh.created), 1)

    def test_a_blocked_retry_takes_no_slot(self):
        d = self.make(supervision.AUTO, supervision.crash(), has_pr=False)
        self.tick(d)
        self.blocked()
        self.gh.command(7, '/devloop retry')
        snap = d.snapshot()
        snap.ready_issues.append({'number': 8, 'title': 'Issue 8', 'labels': {READY},
                                  'labeler': 'brushtyler', 'admitted': True, 'merged': [],
                                  'blocked_by': []})
        one = dataclasses.replace(supervision.AUTO, max_open_prs=1)
        self.assertEqual([(a.kind, a.number) for a in plan(snap, one)], [('implement', 8)])


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

    def test_dropped_and_orphan_prerequisites_are_documented(self):
        section = docs.plan_section('docs/devloop/guide.md', 'Dependencies between issues',
                                    root=docs.ROOT)
        docs.assert_documented(self, section, [
            'stateReason', 'not planned', 'prerequisite_dropped', 'critical class',
            'orphan prerequisite', 'devloop status', 'devloop watch', 'once per pair',
            'no comment written'], issues=(671,))
        protocol = docs.plan_section('docs/devloop/protocol.md', 'Dropped prerequisites',
                                     root=docs.ROOT)
        docs.assert_documented(self, protocol, ['prerequisite_dropped', 'NOT_PLANNED',
                                                'agent:needs-operator'], issues=(671,))

    def test_a_prerequisite_in_words_is_written_as_a_native_block(self):
        # #670: the guide recommends the native block and the readiness check.
        section = docs.plan_section('docs/devloop/guide.md', 'Dependencies between issues',
                                    root=docs.ROOT)
        docs.assert_documented(self, section, [
            'write a prerequisite as a native block', 'blocked escalation',
            'supervisor adds the block itself', 'readiness check', 'reviewer key',
            'repository_dependent', "operator's choice, per project"], issues=(670,))
        quick = docs.plan_section('docs/devloop/quickstart.md', '4. Project file and overlay',
                                  root=docs.ROOT)
        docs.assert_documented(self, quick, [
            'readiness check is recommended for a new project', 'reviewer key',
            'repository_dependent', "operator's choice, per project", 'native block'],
            issues=(670,))
        # The supervisor adds the block with its app, through the REST API, and writes to the
        # coordinator when the prerequisite is not an issue.
        runbook = docs.plan_section('docs/devloop/roles/supervisor.md', 'Tasks',
                                    root=docs.ROOT)
        docs.assert_documented(self, runbook, [
            'a prerequisite written in the text becomes a native block', 'with your app',
            'gh has no command for the blocks', 'scripts/supervisor-token.py',
            "repos/{owner}/{repo}/issues/<ISSUE>/dependencies/blocked_by",
            '-F issue_id=<ID>', 'write to the coordinator in the mailbox'], issues=(670,))


if __name__ == '__main__':
    unittest.main()
