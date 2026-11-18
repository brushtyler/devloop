"""devloop: the attempt identity and one active attempt per phase (#384, ADR 0004, P2-3b)."""

import ast
import dataclasses
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import devloop
from devloop import markdown
from devloop.config import ConfigError, load_config, load_data, parse_config
from devloop.dispatcher import Dispatcher
from devloop.journal import JOURNAL, AttemptActive, JournalError, TurnJournal
from devloop.ledger import Ledger, attempt_branch, attempt_marker, parse_branch
from devloop.planning import Action, plan
from helpers.devloop_fakes import META, SHA1
from test_devloop import CFG, implemented
from test_devloop_ledger import ReportingRunner, TreeWS
from test_devloop_story import StoryGH

ATTEMPT = dataclasses.replace(CFG, story_identity=True, attempt_identity=True)
STORY = dataclasses.replace(CFG, story_identity=True)


def journal_events(state_dir, *kinds):
    return [e for e in (json.loads(line) for line in
                        (Path(state_dir) / JOURNAL).read_text().splitlines())
            if e['event'] in kinds]


def raw_pr(meta, state='OPEN', labels=(), number=100, branch='agent/issue-7', created=None):
    """PR 100 of issue 7 as `gh pr list` (and, with ``state``, `find_pr` and `branch_prs`)
    return it; ``created``: its ``createdAt``."""
    raw = {'number': number, 'title': 't', 'body': markdown.publishable('Why.', 'pr', meta),
           'labels': [{'name': name} for name in labels], 'headRefName': branch,
           'headRefOid': SHA1, 'author': {'login': 'brushtyler'}, 'isCrossRepository': False,
           'mergeable': 'MERGEABLE', 'mergeStateStatus': 'CLEAN', 'state': state}
    if created is not None:
        raw['createdAt'] = created
    return raw


def concluded_attempt(state_dir, issue=7, pr=100):
    """The marker of an attempt of issue ``issue`` whose PR ``pr`` was closed without merge."""
    j = TurnJournal(state_dir)
    attempt = j.open_attempt(j.story(CFG.repo, issue, 'h1')['story'], issue)['attempt']
    j.bind_pr(attempt, pr)
    j.conclude(attempt, 'closed', f'PR #{pr} CLOSED')
    return attempt_marker(j.attempts()[attempt])


def candidate(cfg):
    agent = cfg.agent(cfg.reviewer)
    return types.SimpleNamespace(key=agent.key, model=agent.model, effort=None,
                                 fallback_for=None)


class AttemptDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def make(self, cfg=ATTEMPT, outputs=3):
        self.gh = StoryGH()
        self.logs = []
        runner = ReportingRunner(*(implemented() for _ in range(outputs)), gh=self.gh)
        self.ledger = Ledger(self.gh, state_dir=self.state)
        self.d_ws = TreeWS()
        return Dispatcher(cfg, self.gh, self.d_ws, runner, log=self.logs.append,
                          ledger=self.ledger)

    def restart(self, cfg=ATTEMPT, outputs=3):
        """A new dispatcher on the same repository and state directory (a restart)."""
        runner = ReportingRunner(*(implemented() for _ in range(outputs)), gh=self.gh)
        self.ledger = Ledger(self.gh, state_dir=self.state)
        return Dispatcher(cfg, self.gh, self.d_ws, runner, log=self.logs.append,
                          ledger=self.ledger)

    def numbered_prs(self, first=101):
        """``create_pr`` answers with a new number from ``first`` on."""
        created, numbers = self.gh.create_pr, iter(range(first, first + 10))

        def create_pr(**kw):
            created(**kw)
            number = next(numbers)
            return number, f'https://example/pr/{number}'
        self.gh.create_pr = create_pr

    def pr_marker(self, index=-1):
        return markdown.read_marker(self.gh.created[index]['body'], 'pr')

    def test_the_attempt_is_in_the_pr_marker_and_in_the_turn_records(self):
        d = self.make()
        d._do_implement(7)
        story = self.ledger.journal.story_of(CFG.repo, 7)[0]['story']
        (opened,) = journal_events(self.state, 'attempt')
        attempt = opened['data']['attempt']
        self.assertRegex(attempt, r'^attempt-[1-9][0-9]*-[0-9a-f]{8}$')
        # The phase (story, implementation, null), its first attempt, the key (design, task)
        # foreseen and null until step 3.
        self.assertEqual({k: opened['data'][k] for k in ('story', 'phase', 'revision', 'n',
                                                          'repo', 'issue', 'design', 'task')},
                         {'story': story, 'phase': 'implementation', 'revision': None, 'n': 1,
                          'repo': CFG.repo, 'issue': 7, 'design': None, 'task': None})
        (bound,) = journal_events(self.state, 'attempt_pr')
        self.assertEqual((bound['data']['attempt'], bound['data']['pr']), (attempt, 100))
        # The PR marker carries it.
        self.assertEqual(self.pr_marker()['attempt'], {
            'attempt': attempt, 'story': story, 'phase': 'implementation', 'revision': None,
            'n': 1, 'design': None, 'task': None})
        # So do the turn records on GitHub and the intents in the journal.
        turns = self.gh.records(7, 'turn')
        self.assertTrue(turns)
        self.assertEqual({t['attempt'] for t in turns}, {attempt})
        self.assertIn('**Tentativo:**', self.gh.thread[7][1]['body'])
        self.assertEqual({t.get('attempt') for t in TurnJournal(self.state).turns().values()},
                         {attempt})
        # A turn on the PR (review, fix, sync) belongs to the attempt of its marker.
        self.gh.open = [raw_pr(self.pr_marker(), labels=('agent:changes',))]
        d.snapshot()
        entry = d._open_turn((100, 'fix'), candidate(ATTEMPT), 'write', 'agent:changes')
        self.assertEqual(entry.record['attempt'], attempt)
        self.assertEqual(self.ledger.journal.turns()[entry.turn]['attempt'], attempt)
        self.assertEqual(self.ledger.journal.attempts()[attempt]['pr'], 100)

    def test_a_retry_before_the_pr_resumes_the_active_attempt(self):
        d = self.make()
        self.ledger.journal.story(CFG.repo, 7, 'h1')
        first = d._take_attempt(7)
        # A failure before the PR keeps the attempt: the retry runs in the same one.
        self.assertEqual(d._take_attempt(7)['attempt'], first['attempt'])
        self.assertEqual(len(self.ledger.journal.attempts()), 1)
        # Retries and fallbacks of the implementation are turns of the same attempt.
        self.assertEqual(d._turn_attempt(7, 7, 'implement'), first['attempt'])
        self.assertIsNone(d._turn_attempt(7, 7, 'preflight'))

    def test_a_second_active_attempt_in_the_same_phase_is_refused(self):
        d = self.make()
        d._do_implement(7)
        (first,) = self.ledger.journal.attempts().values()
        # The PR of the attempt is still open: a new implementation of the issue is refused
        # before any turn, label or PR.
        self.gh.any_prs = {100: raw_pr(self.pr_marker())}
        turns, created = len(self.gh.records(7, 'turn')), len(self.gh.created)
        labels = set(self.gh.labels.get(7, ()))
        with self.assertRaisesRegex(AttemptActive, 'active attempt') as caught:
            d._do_implement(7)
        self.assertEqual(caught.exception.attempt['attempt'], first['attempt'])
        self.assertEqual((len(self.gh.records(7, 'turn')), len(self.gh.created)),
                         (turns, created))
        self.assertEqual(set(self.gh.labels.get(7, ())), labels)
        self.assertEqual(list(self.ledger.journal.attempts()), [first['attempt']])
        # Closed without merge: the attempt is concluded with its outcome, and only then a new
        # attempt of the same phase starts, with the next number.
        self.gh.any_prs = {100: raw_pr(self.pr_marker(), state='CLOSED')}
        d._do_implement(7)
        old, new = self.ledger.journal.attempts().values()
        self.assertEqual((old['outcome'], old['reason']), ('closed', 'PR #100 CLOSED'))
        self.assertEqual((new['n'], new['outcome'], new['story']), (2, None, first['story']))
        self.assertEqual(self.pr_marker()['attempt']['n'], 2)
        # One branch per attempt (ADR 0004 §3.2 (i)): the first keeps the branch of today, the
        # second has its own, and the branch of the first is never pushed again.
        self.assertEqual([c['head'] for c in self.gh.created],
                         ['agent/issue-7', 'agent/issue-7-attempt-2'])
        self.assertEqual([branch for branch, _ in self.d_ws.pushes],
                         ['agent/issue-7', 'agent/issue-7-attempt-2'])

    def test_the_journal_refuses_a_second_active_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = TurnJournal(tmp)
            story = j.story(CFG.repo, 7, 'h1')['story']
            first = j.open_attempt(story, 7)
            with self.assertRaises(AttemptActive):
                j.open_attempt(story, 7)
            j.bind_pr(first['attempt'], 100)
            with self.assertRaisesRegex(AttemptActive, r'PR #100'):
                j.open_attempt(story, 7)
            # Another phase of the same story (another approved revision) is not refused.
            other = j.open_attempt(story, 7, revision='a' * 40)
            self.assertEqual(other['n'], 1)
            # One PR per attempt; concluded once, with one outcome.
            with self.assertRaises(JournalError):
                j.bind_pr(first['attempt'], 101)
            self.assertTrue(j.conclude(first['attempt'], 'merged'))
            self.assertFalse(j.conclude(first['attempt'], 'merged'))
            with self.assertRaises(JournalError):
                j.conclude(first['attempt'], 'closed')
            second = j.open_attempt(story, 7)
            self.assertEqual(second['n'], 2)
            # Abandoned only with a reason, then the phase may start again.
            with self.assertRaises(JournalError):
                j.abandon(second['attempt'], ' ')
            self.assertTrue(j.abandon(second['attempt'], 'failed before the PR'))
            self.assertEqual(j.open_attempt(story, 7)['n'], 3)
            # An unknown story or phase, a half key, an issue of no story: refused.
            for args, kw in (((story, 8), {}), (('story-99-0123abcd', 7), {}),
                             ((story, 7, 'design'), {}), ((story, 7), {'design': 'a' * 40})):
                with self.subTest(args=args, kw=kw):
                    with self.assertRaises(JournalError):
                        j.open_attempt(*args, **kw)
            # Kept across processes; a read-only journal writes nothing.
            again = TurnJournal(tmp).attempts()
            self.assertEqual([a['outcome'] for a in again.values()],
                             ['merged', None, 'abandoned', None])
            size = (Path(tmp) / JOURNAL).stat().st_size
            reader = TurnJournal(tmp, read_only=True)
            with self.assertRaises(AttemptActive):
                reader.open_attempt(story, 7)
            self.assertEqual((Path(tmp) / JOURNAL).stat().st_size, size)

    def test_a_merged_or_closed_pr_concludes_its_attempt_at_the_tick(self):
        d = self.make()
        d._do_implement(7)
        (attempt,) = self.ledger.journal.attempts()
        # Still open on GitHub: nothing recorded.
        self.gh.any_prs = {100: raw_pr(self.pr_marker())}
        d._settle_attempts(types.SimpleNamespace(prs=[]), False)
        self.assertIsNone(self.ledger.journal.attempts()[attempt]['outcome'])
        # Merged: recorded once; a dry run records nothing.
        self.gh.any_prs = {100: raw_pr(self.pr_marker(), state='MERGED')}
        d._settle_attempts(types.SimpleNamespace(prs=[]), True)
        self.assertIsNone(self.ledger.journal.attempts()[attempt]['outcome'])
        d._settle_attempts(types.SimpleNamespace(prs=[]), False)
        d._settle_attempts(types.SimpleNamespace(prs=[]), False)
        self.assertEqual(self.ledger.journal.attempts()[attempt]['outcome'], 'merged')
        self.assertEqual(len(journal_events(self.state, 'concluded')), 1)

    def test_a_pr_reopened_after_its_attempt_concluded_is_escalated(self):
        d = self.make()
        d._do_implement(7)
        mark = self.pr_marker()
        (attempt,) = self.ledger.journal.attempts()
        self.ledger.journal.conclude(attempt, 'closed', 'PR #100 CLOSED')
        # Reopened: the snapshot finds its attempt concluded, and the plan escalates it before
        # any work or merge.
        self.gh.open = [raw_pr(mark, labels=('agent:review',))]
        self.gh.labels[100] = {'agent:review'}
        snap = d.snapshot()
        (pr,) = snap.prs
        self.assertIn('già concluso', pr.attempt_problem)
        self.assertEqual([(a.kind, a.number) for a in plan(snap, ATTEMPT)], [('attempt', 100)])
        # A turn on it is refused as well.
        with self.assertRaisesRegex(JournalError, 'già concluso'):
            d._open_turn((100, 'review', 1), candidate(ATTEMPT), 'review', 'agent:review')
        d._do_attempt(pr)
        self.assertIn('agent:needs-operator', self.gh.labels[100])
        self.assertNotIn('agent:review', self.gh.labels[100])
        notice = markdown.read_marker(self.gh.thread[100][-1]['body'], 'notice')
        self.assertEqual(notice['class'], 'attempt')
        # A marker the journal does not confirm: an unknown attempt, or another PR's.
        for bad in (dict(mark['attempt'], attempt='attempt-99-0123abcd'),
                    dict(mark['attempt'], n=2)):
            with self.subTest(bad=bad):
                self.assertIn('non è confermato', d._attempt_problem(100, 7, bad))
        self.ledger.journal.open_attempt(mark['attempt']['story'], 7)
        (_, second) = self.ledger.journal.attempts()
        self.ledger.journal.bind_pr(second, 101)
        self.assertIn('PR #101', d._attempt_problem(
            100, 7, attempt_marker(self.ledger.journal.attempts()[second])))

    def test_existing_prs_without_marker_work_as_today(self):
        # Off by default: no attempt in the journal, in the PR marker or in the turn records.
        self.assertFalse(load_config().attempt_identity)
        for cfg in (CFG, STORY):
            with self.subTest(story=cfg.story_identity):
                d = self.make(cfg)
                d._do_implement(7)
                self.assertNotIn('attempt', self.pr_marker())
                self.assertEqual(set(self.pr_marker()), set(META) | {
                    'profile', 'acceptance_map', 'local_tests'})
                self.assertTrue(all('attempt' not in t for t in self.gh.records(7, 'turn')))
                self.assertNotIn('**Tentativo:**', self.gh.thread[7][1]['body'])
                self.assertEqual(journal_events(self.state, 'attempt', 'attempt_pr',
                                                'concluded'), [])
        # On, a PR opened before, whose marker has no attempt, is handled as today: no problem,
        # no action of its own, its turns belong to no attempt, the tick concludes nothing.
        d = self.make()
        self.gh.open = [raw_pr(META, labels=('agent:review',))]
        self.gh.labels[100] = {'agent:review'}
        snap = d.snapshot()
        (pr,) = snap.prs
        self.assertEqual(pr.attempt_problem, '')
        self.assertNotIn('attempt', [a.kind for a in plan(snap, ATTEMPT)])
        entry = d._open_turn((100, 'fix'), candidate(ATTEMPT), 'write', 'agent:changes')
        self.assertNotIn('attempt', entry.record)
        self.assertNotIn('attempt', self.ledger.journal.turns()[entry.turn])
        d._settle_attempts(snap, False)
        self.assertEqual(self.ledger.journal.attempts(), {})
        # Off, a marker with an attempt is not checked either.
        d = self.make(STORY)
        self.gh.open = [raw_pr(dict(META, attempt={
            'attempt': 'attempt-99-0123abcd', 'story': 'story-1-0123abcd',
            'phase': 'implementation', 'revision': None, 'n': 1, 'design': None,
            'task': None}), labels=('agent:review',))]
        (pr,) = d.snapshot().prs
        self.assertEqual(pr.attempt_problem, '')

    def test_a_pr_whose_marker_lost_its_attempt_is_escalated(self):
        d = self.make()
        d._do_implement(7)
        mark = self.pr_marker()
        (attempt,) = self.ledger.journal.attempts()
        self.ledger.journal.conclude(attempt, 'closed', 'PR #100 CLOSED')
        # Reopened with the field removed from its marker: the journal ties the PR to its
        # attempt, concluded. It is not taken for a PR opened before.
        bare = {k: v for k, v in mark.items() if k != 'attempt'}
        self.gh.open = [raw_pr(bare, labels=('agent:review',))]
        self.gh.labels[100] = {'agent:review'}
        snap = d.snapshot()
        (pr,) = snap.prs
        self.assertIn('non ha il tentativo', pr.attempt_problem)
        self.assertIn(attempt, pr.attempt_problem)
        self.assertEqual([(a.kind, a.number) for a in plan(snap, ATTEMPT)], [('attempt', 100)])
        with self.assertRaisesRegex(JournalError, 'non ha il tentativo'):
            d._open_turn((100, 'review', 1), candidate(ATTEMPT), 'review', 'agent:review')
        # Nor is a PR without the field while its issue has an active attempt without a
        # recorded PR: it may be the PR of that attempt.
        self.state = self.state / 'other'
        d = self.make()
        self.ledger.journal.story(CFG.repo, 7, 'h1')
        active = d._take_attempt(7)
        self.gh.open = [raw_pr(META, labels=('agent:review',))]
        self.gh.labels[100] = {'agent:review'}
        (pr,) = d.snapshot().prs
        self.assertIn('senza PR registrata', pr.attempt_problem)
        self.assertIn(active['attempt'], pr.attempt_problem)

    def test_a_pr_not_recorded_is_found_before_the_attempt_resumes(self):
        d = self.make()
        # The PR is opened, the write that records it fails.
        with mock.patch.object(self.ledger.journal, 'bind_pr',
                               side_effect=JournalError('disk full')):
            d._do_implement(7)
        (first,) = self.ledger.journal.attempts().values()
        self.assertIsNone(first['pr'])
        self.assertTrue(any('not recorded' in line for line in self.logs))
        # Closed before any turn on it, then a restart: the tick has nothing to settle, and the
        # next implementation finds the PR on the attempt's branch, records it and its outcome,
        # and only then opens the next attempt, on its own branch. An older PR of the branch,
        # opened before the attempt without one in its marker, does not count.
        self.gh.any_prs = {
            90: raw_pr(META, state='CLOSED', number=90, created='2000-01-01T00:00:00Z'),
            100: raw_pr(self.pr_marker(), state='CLOSED', created='2999-01-01T00:00:00Z')}
        d = self.restart()
        d._settle_attempts(types.SimpleNamespace(prs=[]), False)
        self.assertIsNone(self.ledger.journal.attempts()[first['attempt']]['outcome'])
        self.numbered_prs()
        d._do_implement(7)
        old, new = self.ledger.journal.attempts().values()
        self.assertEqual((old['attempt'], old['pr'], old['outcome']),
                         (first['attempt'], 100, 'closed'))
        self.assertEqual((new['n'], new['pr'], new['outcome']), (2, 101, None))
        self.assertEqual(self.gh.created[-1]['head'], 'agent/issue-7-attempt-2')
        self.assertEqual(self.pr_marker()['attempt']['attempt'], new['attempt'])

    def test_an_attempt_whose_pr_cannot_be_told_is_not_resumed(self):
        d = self.make()
        with mock.patch.object(self.ledger.journal, 'bind_pr',
                               side_effect=JournalError('disk full')):
            d._do_implement(7)
        (first,) = self.ledger.journal.attempts().values()
        # A PR of the attempt's branch, opened since the attempt, whose marker does not name it:
        # it may be its PR with the field removed. No resume, no second PR, no new attempt.
        bare = {k: v for k, v in self.pr_marker().items() if k != 'attempt'}
        self.gh.any_prs = {100: raw_pr(bare, state='CLOSED', created='2999-01-01T00:00:00Z')}
        created, pushes = len(self.gh.created), len(self.d_ws.pushes)
        d = self.restart()
        with self.assertRaisesRegex(JournalError, r'may be its own \(#100\)'):
            d._do_implement(7)
        self.assertEqual((len(self.gh.created), len(self.d_ws.pushes)), (created, pushes))
        self.assertEqual(list(self.ledger.journal.attempts().values()), [first])

    def test_the_branch_of_each_attempt(self):
        self.assertEqual(attempt_branch('agent/issue-', 7, 1), 'agent/issue-7')
        self.assertEqual(attempt_branch('agent/issue-', 7, 2), 'agent/issue-7-attempt-2')
        for ref, parsed in (('agent/issue-7', (7, None)), ('agent/issue-7-attempt-2', (7, 2)),
                            ('agent/issue-7-attempt-12', (7, 12)),
                            ('agent/issue-7-attempt-1', None), ('agent/issue-7-attempt-02', None),
                            ('agent/issue-7-attempt-', None), ('agent/issue-7-x', None),
                            ('agent/issue-x', None), ('other/7', None)):
            with self.subTest(ref=ref):
                self.assertEqual(parse_branch('agent/issue-', ref), parsed)
        # The branch of a later attempt holds only the PR of that attempt; that of today, only
        # a PR of the first attempt or of none (opened before).
        d = self.make()
        first = dict(AttemptRecordTests.GOOD)
        second = dict(AttemptRecordTests.GOOD, n=2)
        for meta, branch, kept in ((dict(META, attempt=second), 'agent/issue-7-attempt-2', True),
                                   (dict(META, attempt=first), 'agent/issue-7', True),
                                   (META, 'agent/issue-7', True),
                                   (META, 'agent/issue-7-attempt-2', False),
                                   (dict(META, attempt=first), 'agent/issue-7-attempt-2', False),
                                   (dict(META, attempt=second), 'agent/issue-7', False)):
            with self.subTest(branch=branch, attempt=meta.get('attempt', {}).get('n')):
                pr = d._devloop_pr(raw_pr(meta, branch=branch))
                self.assertEqual((pr.issue, pr.head_ref, bool(pr.meta)), (7, branch, kept))
        # A PR merged from the branch of a later attempt resolved the issue too.
        j = self.ledger.journal
        story = j.story(CFG.repo, 7, 'h1')['story']
        j.conclude(j.open_attempt(story, 7)['attempt'], 'closed')
        j.open_attempt(story, 7)
        self.gh.merged_raw = [dict(raw_pr(dict(META, attempt=second), state='MERGED', number=101,
                                          branch='agent/issue-7-attempt-2'),
                                   mergeCommit={'oid': SHA1})]
        self.assertEqual(d._merged_prs(7), [{'number': 101, 'merge_commit': SHA1}])
        self.assertEqual(d._issue_branches(7), ['agent/issue-7', 'agent/issue-7-attempt-2'])
        self.assertEqual(self.make(STORY)._issue_branches(7), ['agent/issue-7'])

    def test_an_incomplete_attempt_in_the_marker_does_not_stop_the_snapshot(self):
        d = self.make()
        d._do_implement(7)
        mark = self.pr_marker()
        del mark['attempt']['revision']
        self.gh.open = [raw_pr(mark, labels=('agent:review',)),
                        raw_pr(dict(META, issue=8), labels=('agent:review',), number=101,
                               branch='agent/issue-8')]
        self.gh.labels.update({100: {'agent:review'}, 101: {'agent:review'}})
        snap = d.snapshot()
        prs = {pr.number: pr for pr in snap.prs}
        # The marker does not hold: the PR is one whose marker was lost; the others are planned.
        self.assertEqual((prs[100].meta, prs[101].attempt_problem), ({}, ''))
        self.assertIn(('lost_meta', 100), [(a.kind, a.number) for a in plan(snap, ATTEMPT)])

    def test_a_pr_whose_attempt_does_not_hold_gets_no_conclave(self):
        from helpers.devloop_fakes import FakeIdentities
        from test_devloop import pr as make_pr
        from test_devloop_conclave import CCFG, ConclaveWS, HookRunner, ThreadGH, proposal
        from test_devloop_conclave import Base as ConclaveBase
        mark = concluded_attempt(self.state)
        self.gh, self.logs = ThreadGH(), []
        runner = HookRunner(proposal())
        cfg = dataclasses.replace(CCFG, story_identity=True, attempt_identity=True)
        d = Dispatcher(cfg, self.gh, ConclaveWS(), runner, log=self.logs.append,
                       identities=FakeIdentities(self.gh),
                       ledger=Ledger(self.gh, state_dir=self.state))
        ConclaveBase.block_pr(self)  # PR #100 in agent:needs-operator, its fixer stopped
        target = dataclasses.replace(make_pr(labels=('agent:needs-operator',)),
                                     meta=dict(META, attempt=mark))
        target.attempt_problem = d._attempt_problem(100, 7, mark)
        self.assertIn('già concluso', target.attempt_problem)
        # A conclave would be due, but the PR gets none.
        self.assertIsNotNone(d._conclave_view(100, self.gh.pr_comments(100), target))
        self.assertEqual(d._conclave_items([target]), [])
        # Planned anyway (the snapshot of another tick): no call and nothing recorded, so no
        # member a later tick would count as missing.
        self.assertIsNone(d._run_action(Action('conclave', 100), {100: target}))
        self.assertEqual((runner.calls, self.gh.conclaves(100)), ([], []))
        self.assertTrue(any(line.startswith('  no conclave: ') for line in self.logs))
        # A call reached all the same is refused before it is recorded as started.
        with self.assertRaisesRegex(JournalError, 'già concluso'):
            d._check_attempt(100, 7, mark)

    def test_a_pr_whose_attempt_does_not_hold_gets_no_advisory_review(self):
        from helpers.devloop_fakes import FakeIdentities
        from test_devloop import FakeRunner
        from test_devloop import pr as make_pr
        from test_devloop_advisory import ON, AdvisoryWS, OperatorGH, answer
        mark = concluded_attempt(self.state)
        cfg = dataclasses.replace(ON, story_identity=True, attempt_identity=True)
        self.gh = OperatorGH()
        self.gh.labels[100] = {'agent:needs-operator'}
        ws, runner, logs = AdvisoryWS(), FakeRunner(answer()), []
        d = Dispatcher(cfg, self.gh, ws, runner, log=logs.append,
                       identities=FakeIdentities(self.gh),
                       ledger=Ledger(self.gh, state_dir=self.state))
        target = dataclasses.replace(make_pr(labels=('agent:needs-operator',)),
                                     meta=dict(META, attempt=mark))
        target.attempt_problem = d._attempt_problem(100, 7, mark)
        self.assertEqual(d._advisory_items([target]), [])
        # Without the problem it would get one.
        self.assertEqual(d._advisory_items([dataclasses.replace(target, attempt_problem='')]),
                         [{'number': 100, 'head': SHA1}])
        # Planned anyway: no turn, no checkout, and no failure recorded for a retry.
        self.assertIsNone(d._do_advisory(target))
        self.assertEqual((runner.calls, ws.copies), ([], []))
        self.assertTrue(any(line.startswith('  no advisory review: ') for line in logs))
        self.assertIsNone(d.advisory_failures.get(100, SHA1))


class AttemptRecordTests(unittest.TestCase):
    GOOD = {'attempt': 'attempt-3-0123abcd', 'story': 'story-1-0123abcd',
            'phase': 'implementation', 'revision': None, 'n': 1, 'design': None, 'task': None}

    def test_the_pr_marker_structure(self):
        for meta in (META, dict(META, attempt=self.GOOD),
                     dict(META, attempt=dict(self.GOOD, design='a' * 40, task='T1'))):
            self.assertEqual(markdown.read_marker(markdown.publishable('x', 'pr', meta), 'pr'),
                             meta)
        for bad in (None, 'attempt-3-0123abcd', dict(self.GOOD, attempt='turn-3-0123abcd'),
                    dict(self.GOOD, story='x'), dict(self.GOOD, phase='design'),
                    dict(self.GOOD, n=0), dict(self.GOOD, revision=1),
                    dict(self.GOOD, design='a' * 40),
                    # Every field is required, the null ones included.
                    *({k: v for k, v in self.GOOD.items() if k != missing}
                      for missing in self.GOOD)):
            with self.subTest(bad=bad):
                self.assertIsNone(markdown.read_marker(
                    markdown.publishable('x', 'pr', dict(META, attempt=bad)), 'pr'))

    def test_the_turn_record_structure(self):
        turn = {'type': 'turn', 'state': 'open', 'kind': 'implement', 'agent': 'a',
                'started_at': 't', 'ended_at': None, 'outcome': None, 'telemetry': None}
        for record in (turn, dict(turn, attempt='attempt-3-0123abcd')):
            self.assertEqual(markdown.read_marker(markdown.publishable('x', 'ledger', record),
                                                  'ledger'), record)
        self.assertIsNone(markdown.read_marker(markdown.publishable(
            'x', 'ledger', dict(turn, attempt='story-3-0123abcd')), 'ledger'))

    def test_the_configuration_key(self):
        data = load_data()
        data.pop('attempt_identity', None)
        self.assertFalse(parse_config(data).attempt_identity)
        on = dict(data, story_identity=True, attempt_identity=True)
        self.assertTrue(parse_config(on).attempt_identity)
        for bad in (dict(on, attempt_identity='yes'), dict(on, story_identity=False)):
            with self.subTest(bad=bad['attempt_identity']):
                with self.assertRaises(ConfigError):
                    parse_config(bad)

    def test_no_flow_abandons_an_attempt(self):
        # The abandonment is an internal primitive (#384): no command and no flow calls it yet.
        package = Path(devloop.__file__).parent
        uses = []
        for path in sorted(package.rglob('*.py')):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if (isinstance(node, ast.Attribute) and node.attr == 'abandon'
                        or isinstance(node, ast.Constant) and node.value == 'abandon'):
                    uses.append(f'{path.relative_to(package)}:{node.lineno}')
        self.assertEqual(uses, [])


if __name__ == '__main__':
    unittest.main()
