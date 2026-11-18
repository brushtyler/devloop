"""devloop: the attempt identity and one active attempt per phase (#384, ADR 0004, P2-3b)."""

import ast
import dataclasses
import json
import tempfile
import types
import unittest
from pathlib import Path

import devloop
from devloop import markdown
from devloop.config import ConfigError, load_config, load_data, parse_config
from devloop.dispatcher import Dispatcher
from devloop.journal import JOURNAL, AttemptActive, JournalError, TurnJournal
from devloop.ledger import Ledger, attempt_marker
from devloop.planning import plan
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


def raw_pr(meta, state='OPEN', labels=()):
    """PR 100 of issue 7 as `gh pr list` (and, with ``state``, `find_pr`) return it."""
    return {'number': 100, 'title': 't', 'body': markdown.publishable('Why.', 'pr', meta),
            'labels': [{'name': name} for name in labels], 'headRefName': 'agent/issue-7',
            'headRefOid': SHA1, 'author': {'login': 'brushtyler'}, 'isCrossRepository': False,
            'mergeable': 'MERGEABLE', 'mergeStateStatus': 'CLEAN', 'state': state}


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
        return Dispatcher(cfg, self.gh, TreeWS(), runner, log=self.logs.append,
                          ledger=self.ledger)

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
                    dict(self.GOOD, design='a' * 40)):
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
