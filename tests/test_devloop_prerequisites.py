"""devloop: a missing prerequisite becomes a native "blocked by" of the dispatcher, or a
``prerequisite`` escalation to the supervisor, in place of an escalation to the operator (#672)."""

import dataclasses
import json
import unittest
from types import SimpleNamespace

import test_devloop_supervision as sup
from devloop import markdown, schema, supervision
from devloop.github import GitHub, GitHubError, GitHubDuplicate, GitHubUncertain
from devloop.dispatcher import (MAX_ACTIVE_PREREQUISITES, MAX_ADDED_PREREQUISITES,
                                MAX_DEPENDENCY_READS, Dispatcher)
from devloop.markdown import read_marker, render_prerequisite
from devloop.planning import PREREQUISITES_CLOSED, plan, preflight_state
from helpers.devloop_fakes import FakeIdentities, at
from test_devloop import CFG, REV, FakeRunner, FakeWS, implemented
from test_devloop_dependencies import DCFG, REPO, DependencyGH, block, ready
from test_devloop_preflight import PCFG, IssueGH, checked, gap

DISPATCHER_LOGIN = FakeIdentities.LOGINS['dispatcher']
READY, WORKING = 'agent:ready', 'agent:working'
NEEDS_OPERATOR = 'agent:needs-operator'


def record(*numbers, step='preflight'):
    """The dispatcher's comment of the "blocked by" it adds for a prerequisite."""
    return render_prerequisite({'step': step, 'added': list(numbers)})


class PrerequisiteGH(DependencyGH):
    """``DependencyGH`` whose comments keep their author (``writer`` at the time of the write)
    and whose "blocked by" added by the dispatcher join the blocks of the issue, added by
    ``writer``."""

    def __init__(self, issues, blocks=None, writer='brushtyler'):
        super().__init__(issues, blocks)
        self.writer = writer
        self.writers = []

    def say(self, number, body, who):
        self.comments.append((number, body))
        self.writers.append(who)

    def comment(self, number, body):
        self.say(number, body, self.writer)

    def pr_comments(self, number):
        return [{'author': {'login': self.writers[i]}, 'body': body, 'created_at': at(2 * i)}
                for i, (n, body) in enumerate(self.comments) if n == number]

    def add_blocked_by(self, number, issue_id):
        # As GitHub, a block the issue already has is refused (#719).
        if any(b['number'] == issue_id - 100000 for b, _ in self.blocks.get(number, [])):
            raise GitHubDuplicate('gh: Issue is already blocked by this issue (HTTP 422)')
        super().add_blocked_by(number, issue_id)
        self.blocks.setdefault(number, []).append(block(issue_id - 100000, by=self.writer))

    def remove_blocked_by(self, number, issue_id):
        super().remove_blocked_by(number, issue_id)
        self.blocks[number] = [pair for pair in self.blocks.get(number, [])
                               if pair[0]['number'] != issue_id - 100000]

    def prerequisites(self, number):
        return [r for n, body in self.comments
                if n == number and (r := read_marker(body, 'prerequisite'))]


class BlockTests(unittest.TestCase):
    def make(self, blocks=None, identities=False, cfg=DCFG):
        self.gh = PrerequisiteGH(ready(2), blocks,
                                 writer=DISPATCHER_LOGIN if identities else 'brushtyler')
        self.logs = []
        return Dispatcher(cfg, self.gh, FakeWS(), FakeRunner(), log=self.logs.append,
                          identities=FakeIdentities(self.gh) if identities else None)

    def blocked_by(self, d, number=2):
        return d._dependency_view([number])[number]['blocked_by']

    def test_a_block_of_the_dispatcher_counts_only_with_its_record(self):
        d = self.make({2: [block(1, by=DISPATCHER_LOGIN)]}, identities=True)
        self.assertEqual(self.blocked_by(d), [])
        self.assertTrue(any('ignored the block by #1' in line for line in self.logs))
        # A record of another identity, or one that names another issue, does not count.
        self.gh.say(2, record(1), 'stranger')
        self.gh.say(2, record(3), DISPATCHER_LOGIN)
        self.assertEqual(self.blocked_by(d), [])
        self.gh.say(2, record(1, step='implement'), DISPATCHER_LOGIN)
        self.assertEqual(self.blocked_by(d), ['#1'])
        # The record makes only the dispatcher's own block count, never anyone else's.
        self.gh.blocks = {2: [block(1, by='stranger')]}
        self.assertEqual(self.blocked_by(d), [])

    def test_the_record_is_written_first_then_the_blocks(self):
        d = self.make(identities=True)
        self.assertEqual(d._add_prerequisites(2, [5, 4, 5], 'preflight'), ([4, 5], ''))
        self.assertEqual(self.gh.prerequisites(2), [{'step': 'preflight', 'added': [4, 5]}])
        self.assertEqual(self.gh.writers, [DISPATCHER_LOGIN])
        self.assertEqual(self.gh.added_blocks, [(2, 100004), (2, 100005)])
        body = self.gh.comments[-1][1]
        self.assertIn('#4, #5', body)
        self.assertTrue(body.lstrip().startswith('> [!NOTE]'))
        # The blocks the dispatcher added count: the issue waits for them.
        self.assertEqual(self.blocked_by(d), ['#4', '#5'])

    def test_a_block_that_does_not_count_is_replaced_by_the_dispatchers(self):
        # #719: a block of #5 added by a login whose blocks are ignored (the coordinator's app,
        # or anyone else's) does not stop the dispatcher's.
        d = self.make({2: [block(5, by='stranger')]}, identities=True)
        self.assertEqual(self.blocked_by(d), [])
        self.assertEqual(d._add_prerequisites(2, [5, 6], 'preflight'), ([5, 6], ''))
        self.assertEqual(self.gh.prerequisites(2),
                         [{'step': 'preflight', 'added': [5, 6], 'replaced': [5]}])
        self.assertIn('non contano: #5.', ' '.join(self.gh.comments[-1][1].split()))
        # GitHub refused the block it already had: the dispatcher removed it and added it again.
        self.assertEqual(self.gh.removed_blocks, [(2, 100005)])
        self.assertEqual(self.gh.added_blocks, [(2, 100005), (2, 100006)])
        self.assertTrue(any('in place of the blocks that do not count of #5' in line
                            for line in self.logs))
        # Now it is the dispatcher's, with its record: it counts, and the issue waits for it.
        self.assertEqual(self.blocked_by(d), ['#5', '#6'])
        added, why = d._add_prerequisites(2, [5], 'implement')
        self.assertEqual(added, [])
        self.assertIn('#5', why)

    def test_a_block_that_does_not_count_and_is_not_refused_is_not_removed(self):
        d = self.make({2: [block(5, by='stranger')]}, identities=True)
        accepted = []
        self.gh.add_blocked_by = lambda number, issue_id: accepted.append((number, issue_id))
        self.assertEqual(d._add_prerequisites(2, [5], 'preflight'), ([5], ''))
        self.assertEqual(getattr(self.gh, 'removed_blocks', []), [])
        self.assertEqual(accepted, [(2, 100005)])
        self.assertEqual(self.gh.prerequisites(2),
                         [{'step': 'preflight', 'added': [5], 'replaced': [5]}])

    def test_another_refusal_is_no_replacement(self):
        # A block that is not there: a refusal of GitHub is an error, as before #719.
        d = self.make(identities=True)

        def refuse(number, issue_id):
            raise GitHubError('gh: (HTTP 403)')

        self.gh.add_blocked_by = refuse
        with self.assertRaises(GitHubError):
            d._add_prerequisites(2, [5], 'preflight')
        self.assertEqual(getattr(self.gh, 'removed_blocks', []), [])

    def test_only_a_refusal_as_a_duplicate_replaces_a_block_that_does_not_count(self):
        # A block of #5 that does not count is there: a failure other than GitHub's 422 (403,
        # 5xx, a timeout) proves no duplicate. It propagates, and the block stays.
        failures = {'403': GitHubError('gh: Forbidden (HTTP 403)'),
                    '503': GitHubError('gh: Service Unavailable (HTTP 503)'),
                    'uncertain': GitHubUncertain('gh api: timed out after 30s')}
        for name, failure in failures.items():
            with self.subTest(failure=name):
                d = self.make({2: [block(5, by='stranger')]}, identities=True)
                calls = []

                def fail(number, issue_id, failure=failure):
                    calls.append((number, issue_id))
                    raise failure

                self.gh.add_blocked_by = fail
                with self.assertRaises(GitHubError) as raised:
                    d._add_prerequisites(2, [5], 'preflight')
                self.assertIs(raised.exception, failure)
                self.assertEqual(getattr(self.gh, 'removed_blocks', []), [])
                self.assertEqual(calls, [(2, 100005)])
                self.assertEqual([b['number'] for b, _ in self.gh.blocks[2]], [5])

    def test_a_condition_that_fails_adds_nothing(self):
        closed = {'id': 9, 'state': 'CLOSED', 'pull_request': False}
        cases = {
            'no number': ([0], {}, {}, ''),
            'the issue itself': ([2], {}, {}, '#2'),
            'not found': ([5], {5: None}, {}, '#5'),
            'a pull request': ([5], {5: {'id': 9, 'state': 'OPEN', 'pull_request': True}}, {},
                               'pull request'),
            'closed': ([5], {5: closed}, {}, '#5'),
            # A block that counts, of a trusted user (#719: one of any other login does not).
            'already a block': ([5], {}, {2: [block(5)]}, '#5'),
            # #5 is blocked by #2 already: #2 blocked by #5 would close a cycle.
            'a cycle': ([5], {}, {5: [block(2)]}, '#2, #5'),
            # GitHub follows the transfer of #5: the issue it answers with is elsewhere.
            'transferred': ([5], {5: {'id': 900099, 'repo': 'example/other', 'number': 99}},
                            {}, '#5'),
            'another number': ([5], {5: {'number': 99}}, {}, '#5'),
            # #10 -> #11 -> ... -> #70 -> #2: the way back is beyond the issues read, so a
            # cycle cannot be ruled out.
            'a cycle beyond the reads': (
                [10], {}, {**{n: [block(n + 1)] for n in range(10, 70)}, 70: [block(2)]},
                str(MAX_DEPENDENCY_READS)),
        }
        for name, (wanted, targets, blocks, text) in cases.items():
            with self.subTest(case=name):
                d = self.make(blocks)
                self.gh.targets = targets
                added, why = d._add_prerequisites(2, wanted, 'implement')
                self.assertEqual(added, [])
                self.assertIn(text, why)
                self.assertEqual(self.gh.comments, [])
                self.assertEqual(getattr(self.gh, 'added_blocks', []), [])

    def test_the_caps_of_the_blocks_of_one_issue(self):
        self.assertEqual((MAX_ACTIVE_PREREQUISITES, MAX_ADDED_PREREQUISITES), (3, 5))
        # Three blocks of the dispatcher open together: a fourth is refused.
        d = self.make({2: [block(11), block(12), block(13)]})
        for n in (11, 12, 13):
            self.gh.say(2, record(n), 'brushtyler')
        added, why = d._add_prerequisites(2, [14], 'preflight')
        self.assertEqual(added, [])
        self.assertIn('3', why)
        # One closed as completed leaves room for another.
        self.gh.blocks[2][0] = block(11, state='CLOSED', reason='COMPLETED')
        self.assertEqual(d._add_prerequisites(2, [14], 'preflight'), ([14], ''))
        # Five added in all, those added again included: a sixth is refused, even once the
        # earlier ones are closed or were removed.
        d = self.make()
        for _ in range(5):
            self.gh.say(2, record(11), 'brushtyler')
        added, why = d._add_prerequisites(2, [11], 'implement')
        self.assertEqual(added, [])
        self.assertIn('5', why)
        d = self.make()
        for n in (11, 12, 13, 14):
            self.gh.say(2, record(n), 'brushtyler')
        self.assertEqual(d._add_prerequisites(2, [15, 16], 'preflight')[0], [])
        self.assertEqual(d._add_prerequisites(2, [15], 'preflight'), ([15], ''))


class IssueTargetTests(unittest.TestCase):
    def target(self, raw):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout=json.dumps(raw) + '\n', stderr='')

        found = GitHub('brushtyler/ssdlc', run=run).issue_target(5)
        self.assertIn('repos/brushtyler/ssdlc/issues/5', calls[-1])
        return found

    def test_the_repository_and_number_it_is_in_now(self):
        url = 'https://api.github.com/repos/{}'
        self.assertEqual(
            self.target({'id': 7, 'number': 5, 'repository_url': url.format('brushtyler/ssdlc'),
                         'state': 'open', 'pull_request': False}),
            {'id': 7, 'repo': 'brushtyler/ssdlc', 'number': 5, 'state': 'OPEN',
             'pull_request': False})
        # An issue transferred elsewhere: GitHub follows the transfer.
        moved = self.target({'id': 900099, 'number': 99, 'state': 'open',
                             'repository_url': url.format('example/other'),
                             'pull_request': False})
        self.assertEqual((moved['repo'], moved['number']), ('example/other', 99))
        self.assertIsNone(self.target({'id': 7, 'number': 5, 'state': 'open'})['repo'])

    def test_only_a_422_that_says_already_is_a_duplicate(self):
        # #719: a 422 whose message, in the stderr or in the body `gh` prints, says the block is
        # already there is the refusal of a duplicate; any other failure, a 422 for another
        # cause included, keeps its own error, which the dispatcher does not take for one.
        def adding(stderr, stdout=''):
            def run(argv, **kw):
                return SimpleNamespace(returncode=1, stdout=stdout, stderr=stderr)
            return GitHub('brushtyler/ssdlc', run=run)

        duplicates = {
            'message': ('gh: Issue is already blocked by this issue (HTTP 422)\n', ''),
            'code': ('gh: Validation Failed (HTTP 422)\n',
                     '{"message":"Validation Failed","errors":[{"resource":"IssueDependency",'
                     '"field":"issue_id","code":"already_exists"}]}'),
        }
        for name, (stderr, stdout) in duplicates.items():
            with self.subTest(duplicate=name), self.assertRaises(GitHubDuplicate):
                adding(stderr, stdout).add_blocked_by(2, 100005)
        for stderr in ('gh: Validation Failed (HTTP 422)\n',
                       'gh: endpoint has been spammed (HTTP 422)\n',
                       'gh: Forbidden (HTTP 403)\n', 'gh: Service Unavailable (HTTP 503)\n',
                       'gh: Not Found (HTTP 404)\n', 'connection reset\n',
                       # "already" outside a 422 is no refusal of a duplicate.
                       'gh: already rate limited (HTTP 403)\n'):
            with self.subTest(stderr=stderr):
                with self.assertRaises(GitHubError) as raised:
                    adding(stderr).add_blocked_by(2, 100005)
                self.assertNotIsInstance(raised.exception, GitHubDuplicate)
        with self.assertRaises(GitHubError) as raised:
            adding('gh: Not Found (HTTP 404)\n').add_blocked_by(2, 100005)
        self.assertEqual(type(raised.exception).__name__, 'GitHubNotFound')

    def test_a_422_for_another_cause_removes_no_block_that_does_not_count(self):
        # #719: a block of #5 that does not count is there, and GitHub answers the dispatcher's
        # with a 422 that is no duplicate: the real adapter raises its error, the dispatcher
        # removes nothing and adds nothing again.
        for stderr in ('gh: endpoint has been spammed (HTTP 422)\n',
                       'gh: Validation Failed (HTTP 422)\n'):
            with self.subTest(stderr=stderr):
                d = BlockTests.make(self, {2: [block(5, by='stranger')]}, identities=True)
                calls = []

                def run(argv, stderr=stderr, **kw):
                    calls.append(argv[argv.index('-X') + 1])
                    return SimpleNamespace(returncode=1, stdout='', stderr=stderr)

                adapter = GitHub('brushtyler/ssdlc', run=run)
                self.gh.add_blocked_by = adapter.add_blocked_by
                self.gh.remove_blocked_by = adapter.remove_blocked_by
                with self.assertRaises(GitHubError) as raised:
                    d._add_prerequisites(2, [5], 'preflight')
                self.assertNotIsInstance(raised.exception, GitHubDuplicate)
                self.assertEqual(calls, ['POST'])
                self.assertEqual([b['number'] for b, _ in self.gh.blocks[2]], [5])


class PreflightGH(IssueGH):
    """Issue #7 of the readiness check tests, with its native dependencies: the "blocked by"
    devloop adds join them, added by the operator's login (the dispatcher's without apps)."""

    def __init__(self, labels=(READY,)):
        super().__init__(labels)
        self.blocks = []

    def issue_dependencies(self, number):
        pairs = self.blocks if number == 7 else []
        return {'author': 'brushtyler', 'blocked_by': [dict(b) for b, _ in pairs],
                'added': [dict(e) for _, e in pairs]}

    def add_blocked_by(self, number, issue_id):
        # As GitHub, a block the issue already has is refused (#719).
        if any(b['number'] == issue_id - 100000 for b, _ in self.blocks):
            raise GitHubDuplicate('gh: Issue is already blocked by this issue (HTTP 422)')
        super().add_blocked_by(number, issue_id)
        self.blocks.append(block(issue_id - 100000))

    def remove_blocked_by(self, number, issue_id):
        super().remove_blocked_by(number, issue_id)
        self.blocks = [pair for pair in self.blocks if pair[0]['number'] != issue_id - 100000]

    def records(self, kind):
        return [r for c in self.thread if (r := read_marker(c['body'], kind))]


class PreflightPrerequisiteTests(unittest.TestCase):
    def make(self, *outputs, cfg=PCFG):
        self.gh, self.ws = PreflightGH(), FakeWS()
        self.ws.base_head = lambda: 'b' * 40  # the base stays where the check ran
        self.runner, self.logs = FakeRunner(*outputs), []
        return Dispatcher(cfg, self.gh, self.ws, self.runner, log=self.logs.append)

    def kinds(self, d):
        return [(a.kind, a.number) for a in d.tick()]

    def test_prerequisites_with_their_numbers_become_blocks_instead_of_the_operator(self):
        d = self.make(checked(False, [gap(repository=True, prerequisite='il parser', blocked_by=5),
                                      gap(repository=True, prerequisite='lo schema',
                                          blocked_by=6)]))
        self.assertEqual(self.kinds(d), [('preflight', 7)])
        self.assertEqual(self.gh.labels[7], {READY})
        self.assertEqual(self.gh.added_blocks, [(7, 100005), (7, 100006)])
        self.assertEqual(self.gh.records('prerequisite'), [{'step': 'preflight', 'added': [5, 6]}])
        outcome = self.gh.records('preflight')[-1]
        self.assertEqual((outcome['ready'], outcome['blocked'], outcome['class'],
                          outcome['prerequisites']), (False, False, 'prerequisite', [5, 6]))
        body = self.gh.thread[-1]['body']
        self.assertIn('*Prerequisito mancante:* il parser (#5)', body)
        self.assertIn('"blocked by" nativo #5, #6', body)
        self.assertNotIn('/devloop recheck', body)
        # The issue leaves the queue: no check, no implementation, while a block is open.
        self.assertEqual(self.kinds(d), [])
        self.assertEqual(self.kinds(d), [])
        self.assertEqual(len(self.runner.calls), 1)
        # When they are all closed, the check runs again, without anyone asking for it.
        self.gh.blocks = [block(5, state='CLOSED', reason='COMPLETED'),
                          block(6, state='CLOSED', reason='COMPLETED')]
        self.runner.outputs.append(checked(True))
        self.assertEqual(self.kinds(d), [('preflight', 7)])
        self.assertTrue(self.gh.records('preflight')[-1]['ready'])
        self.assertEqual([a.kind for a in plan(d.snapshot(), PCFG)], ['implement'])

    def test_a_block_that_does_not_count_does_not_send_the_issue_to_the_operator(self):
        # #719: the block of #5 was added by a login whose blocks do not count (the coordinator's
        # app, as on #718): the plan ignores it and the check runs; with that prerequisite as
        # its only gap, the dispatcher adds its own block in place of agent:needs-operator.
        d = self.make(checked(False, [gap(repository=True, prerequisite='il parser',
                                          blocked_by=5)]))
        self.gh.blocks = [block(5, by='stranger')]
        self.assertEqual(self.kinds(d), [('preflight', 7)])
        self.assertEqual(self.gh.labels[7], {READY})
        self.assertEqual(self.gh.removed_blocks, [(7, 100005)])
        self.assertEqual(self.gh.added_blocks, [(7, 100005)])
        self.assertEqual(self.gh.records('prerequisite'),
                         [{'step': 'preflight', 'added': [5], 'replaced': [5]}])
        outcome = self.gh.records('preflight')[-1]
        self.assertEqual((outcome['blocked'], outcome['class'], outcome['prerequisites']),
                         (False, 'prerequisite', [5]))
        # The block is now one that counts: the issue waits for it.
        self.assertEqual(self.kinds(d), [])
        self.assertEqual(len(self.runner.calls), 1)

    def test_a_failed_check_with_prerequisites_runs_again_once_unblocked(self):
        view = {'record': {'ready': False, 'hash': 'h', 'gaps': [], 'prerequisites': [5]},
                'hash': 'h', 'recheck': False}
        self.assertEqual(preflight_state(view), ('run', PREREQUISITES_CLOSED))
        view['record'] = dict(view['record'], prerequisites=[])
        self.assertEqual(preflight_state(view)[0], 'not_ready')

    def test_other_gaps_or_a_failed_condition_keep_the_operator(self):
        prerequisite = gap(repository=True, prerequisite='il parser', blocked_by=5)
        cases = {
            'another gap': (checked(False, [prerequisite, gap()]), {}, ''),
            'a split': (checked(False, [prerequisite], 'Dividere in due.'), {}, ''),
            'the trust policy': (checked(False, [prerequisite], trust='uncovered',
                                         reason='tocca la fiducia'), {}, ''),
            'a closed prerequisite': (checked(False, [prerequisite]),
                                      {5: {'id': 9, 'state': 'CLOSED', 'pull_request': False}},
                                      'non aggiunge il loro "blocked by": #5 non è aperta'),
            'no number, not in auto': (checked(False, [gap(prerequisite='il parser')]), {}, ''),
        }
        for name, (output, targets, note) in cases.items():
            with self.subTest(case=name):
                d = self.make(output)
                self.gh.targets = targets
                self.assertEqual(self.kinds(d), [('preflight', 7)])
                self.assertEqual(self.gh.labels[7], {READY, NEEDS_OPERATOR})
                self.assertEqual(getattr(self.gh, 'added_blocks', []), [])
                self.assertEqual(self.gh.records('prerequisite'), [])
                outcome = self.gh.records('preflight')[-1]
                self.assertTrue(outcome['blocked'])
                self.assertEqual(outcome['class'], 'operator_decision')
                self.assertIn(note, ' '.join(self.gh.thread[-1]['body'].split()))


class ImplementGH(PreflightGH):
    """Issue #7 in the queue, without the readiness check, and in ``agent:working`` once taken."""

    def issues_with_label(self, label):
        if label in self.labels.get(7, ()):
            return [{'number': 7, 'title': 'Issue 7', 'author': {'login': 'brushtyler'},
                     'labels': [{'name': n} for n in sorted(self.labels[7])]}]
        return []


def blocked(number=0, prerequisite='il parser di #5'):
    """An implementer that stopped for a missing prerequisite."""
    return dict(implemented('blocked'), blocked_reason='Serve prima il parser.',
                prerequisite=prerequisite, blocked_by=number)


class ImplementPrerequisiteTests(unittest.TestCase):
    def make(self, *outputs):
        self.gh, self.ws = ImplementGH(), FakeWS()
        self.runner, self.logs = FakeRunner(*outputs), []
        return Dispatcher(CFG, self.gh, self.ws, self.runner, log=self.logs.append)

    def test_the_implementation_waits_for_the_block_then_starts_again(self):
        d = self.make(blocked(5))
        d._do_implement(7)
        self.assertEqual(self.gh.labels[7], {WORKING})
        self.assertEqual(self.gh.added_blocks, [(7, 100005)])
        self.assertEqual(self.gh.records('prerequisite'), [{'step': 'implement', 'added': [5]}])
        self.assertEqual(self.gh.records('notice'), [])  # no escalation, no block notice
        self.assertIn('issue-7', self.ws.removed)
        # While the block is open the issue waits in agent:working: not stale, not restarted.
        snap = d.snapshot()
        self.assertTrue(snap.base_waits[7]['holds'])
        self.assertEqual(plan(snap, CFG), [])
        # Once it closes, the implementation starts again.
        self.gh.blocks = [block(5, state='CLOSED', reason='COMPLETED')]
        snap = d.snapshot()
        self.assertFalse(snap.base_waits[7]['holds'])
        self.assertEqual([(a.kind, a.number, a.reason) for a in plan(snap, CFG)],
                         [('implement', 7, PREREQUISITES_CLOSED)])
        # Closed without being completed, it escalates to the operator instead.
        self.gh.blocks = [block(5, state='CLOSED', reason='NOT_PLANNED')]
        snap = d.snapshot()
        self.assertEqual([(a.kind, a.number) for a in plan(snap, CFG)],
                         [('prerequisite_dropped', 7)])
        d._do_prerequisite_dropped(7)
        self.assertEqual(self.gh.labels[7], {NEEDS_OPERATOR})
        self.assertEqual(d.snapshot().base_waits, {})

    def test_a_failed_condition_keeps_the_block_of_before_with_why(self):
        d = self.make(blocked(5))
        self.gh.targets = {5: {'id': 9, 'state': 'CLOSED', 'pull_request': False}}
        d._do_implement(7)
        self.assertEqual(self.gh.labels[7], {NEEDS_OPERATOR})
        (notice,) = self.gh.records('notice')
        self.assertEqual(notice['class'], 'blocked')
        self.assertIn('Serve prima il parser.', notice['reason'])
        self.assertIn('#5 non è aperta', notice['reason'])
        self.assertEqual(self.gh.records('prerequisite'), [])

    def test_without_a_number_outside_auto_the_block_goes_to_the_operator(self):
        d = self.make(blocked())
        d._do_implement(7)
        self.assertEqual(self.gh.labels[7], {NEEDS_OPERATOR})
        self.assertEqual([n['class'] for n in self.gh.records('notice')], ['blocked'])
        self.assertEqual(getattr(self.gh, 'added_blocks', []), [])


class SupervisedPrerequisiteTests(sup.Case):
    """A prerequisite named only in words goes to the supervisor in "auto"."""

    def blocks(self, *pairs):
        self.gh.issue_dependencies = lambda number: {
            'author': 'brushtyler', 'blocked_by': [dict(b) for b, _ in pairs] if number == 7
            else [], 'added': [dict(e) for _, e in pairs] if number == 7 else []}

    def test_the_check_escalates_and_the_retry_waits_for_the_block_of_the_supervisor(self):
        d = self.make(sup.PREFLIGHT_AUTO,
                      checked(False, [gap(repository=True, prerequisite='il nuovo parser')]),
                      has_pr=False)
        self.assertEqual(self.tick(d), [('preflight', 7)])
        self.assertEqual(self.gh.labels[7], {READY, sup.SUPER})
        (escalation,) = self.escalations(7)
        self.assertEqual((escalation['class'], escalation['to'], escalation['step']),
                         ('prerequisite', 'supervisor', 'preflight'))
        self.assertEqual(self.gh.records(7, 'preflight')[-1]['class'], 'prerequisite')
        body = ' '.join(self.gh.threads[7][-1]['body'].split())
        self.assertIn('«il nuovo parser»', body)
        self.assertIn('/devloop retry', body)
        # The supervisor adds the block with its app, then asks for a retry: it waits.
        self.blocks(block(5, by=sup.SUPERVISOR_LOGIN))
        self.gh.command(7, '/devloop retry')
        self.assertEqual(self.tick(d), [])
        self.assertEqual(self.decisions(7), [])
        # The prerequisite is merged: the block closes and the base advances. The base does not
        # bind the retry of this class, which runs the check again.
        self.blocks(block(5, state='CLOSED', reason='COMPLETED', by=sup.SUPERVISOR_LOGIN))
        self.ws.base = 'f' * 40
        self.runner.outputs.append(checked(True))
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.decisions(7)[-1]['outcome'], 'applied')
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual(self.gh.labels[7], {READY})
        self.assertTrue(self.gh.records(7, 'preflight')[-1]['ready'])

    def test_handed_to_the_operator_the_base_still_does_not_bind_the_retry(self):
        prerequisite = checked(False, [gap(repository=True, prerequisite='il nuovo parser')])
        d = self.make(sup.PREFLIGHT_AUTO, prerequisite, has_pr=False)
        self.assertEqual(self.tick(d), [('preflight', 7)])
        # A retry of the supervisor on another base: the check finds the prerequisite again.
        self.ws.base = 'e' * 40
        self.runner.outputs.append(prerequisite)
        self.gh.command(7, '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.decisions(7)[-1]['attempt'], 1)
        self.assertEqual(self.escalations(7)[-1]['class'], 'prerequisite')
        self.gh.command(7, '/devloop escalate serve l\'operatore')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.escalations(7)[-1]['class'], 'supervisor_escalated')
        self.assertEqual(self.gh.labels[7], {READY, NEEDS_OPERATOR})
        # The prerequisite is merged and the base advances: the operator's retry is not
        # superseded, and counts the supervisor's retry made on the base of before.
        self.ws.base = 'f' * 40
        self.runner.outputs.append(checked(True))
        self.gh.command(7, '/devloop retry', login='brushtyler')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual((self.decisions(7)[-1]['outcome'], self.decisions(7)[-1]['attempt']),
                         ('applied', 2))
        self.assertEqual(len(self.runner.calls), 3)
        self.assertEqual(self.gh.labels[7], {READY})

    def test_the_implementer_escalates_to_the_supervisor(self):
        d = self.make(sup.AUTO, blocked(), has_pr=False)
        self.assertEqual(self.tick(d), [('implement', 7)])
        self.assertEqual(self.gh.labels[7], {sup.SUPER})
        (escalation,) = self.escalations(7)
        self.assertEqual((escalation['class'], escalation['step']), ('prerequisite', 'implement'))
        body = self.gh.threads[7][-1]['body']
        self.assertIn('«il parser di #5»', body)
        self.assertIn('Serve prima il parser.', body)


class RestartEscalationTests(sup.Case):
    """An escalation of the readiness check that restarts an implementation waiting for its
    prerequisites keeps the wait and ``agent:working``, as for a wait for the base: the issue is
    never ``stale``, and resumes once the escalation is answered."""

    def waiting(self, cfg, *outputs):
        """Issue 7 in ``agent:working`` whose implementation waits for #5, closed already."""
        d = self.make(cfg, *outputs, has_pr=False)
        self.gh.labels[7] = {WORKING}
        self.assertEqual(d._add_prerequisites(7, [5], 'implement'), ([5], ''))
        closed = block(5, state='CLOSED', reason='COMPLETED')
        self.gh.issue_dependencies = lambda number: {
            'author': 'brushtyler', 'blocked_by': [dict(closed[0])] if number == 7 else [],
            'added': [dict(closed[1])] if number == 7 else []}
        return d

    def planned(self, d):
        return [(a.kind, a.number) for a in plan(d.snapshot(), self.cfg)]

    def implemented(self, d):
        self.assertEqual(self.planned(d), [('implement', 7)])
        self.runner.outputs.append(implemented())
        self.assertEqual(self.tick(d), [('implement', 7)])
        self.assertEqual(len(self.gh.created), 1)

    def test_defaults_to_the_supervisor_wait_for_its_decision(self):
        d = self.waiting(sup.PREFLIGHT_AUTO, checked(False, [gap('default_available',
                                                                 'JSON')]))
        self.assertEqual(self.tick(d), [('preflight', 7)])
        self.assertEqual(self.gh.labels[7], {WORKING, sup.SUPER})
        self.assertEqual(self.escalations(7)[-1]['class'], 'preflight_default')
        # Not stale: the issue waits for the supervisor.
        self.assertEqual(self.planned(d), [])
        self.gh.command(7, '/devloop accept-defaults')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.gh.labels[7], {WORKING})
        # The check runs again with the decision, then the implementation.
        self.runner.outputs.append(checked(True))
        self.assertEqual(self.tick(d), [('preflight', 7)])
        self.assertEqual(self.gh.labels[7], {WORKING})
        self.implemented(d)

    def test_a_failed_check_keeps_the_issue_and_a_retry_resumes_it(self):
        for cfg, label, by in [(dataclasses.replace(CFG, preflight=REV), NEEDS_OPERATOR,
                                'brushtyler'),
                               (sup.PREFLIGHT_AUTO, sup.SUPER, sup.SUPERVISOR_LOGIN)]:
            with self.subTest(label):
                d = self.waiting(cfg, sup.crash())
                self.assertEqual(self.tick(d), [('preflight', 7)])
                self.assertEqual(self.gh.labels[7], {WORKING, label})
                self.assertEqual(self.escalations(7)[-1]['step'], 'preflight')
                self.assertEqual(self.planned(d), [])
                self.runner.outputs.append(checked(True))
                self.gh.command(7, '/devloop retry', login=by)
                self.assertEqual(self.tick(d), [('supervise', 7)])
                self.assertEqual(self.decisions(7)[-1]['outcome'], 'applied')
                self.assertEqual(self.gh.labels[7], {WORKING})
                self.implemented(d)

    def test_an_escalation_of_the_implementation_ends_the_wait(self):
        d = self.waiting(sup.PREFLIGHT_AUTO)
        d._escalate(7, 'altro', remove=[WORKING], cls='blocked')
        self.assertIsNone(d._prerequisite_wait(self.gh.issue_comments(7)))


class RecordTests(unittest.TestCase):
    def test_the_class_is_not_critical_and_its_retry_is_not_bound_to_the_base(self):
        self.assertFalse(supervision.critical(supervision.PREREQUISITE))
        self.assertIn(supervision.PREREQUISITE, supervision.RETRY_CLASSES)
        key = {'step': 'preflight', 'base': 'a' * 40, 'request': 'h'}
        now = dict(key, base='b' * 40)
        self.assertEqual(supervision.changed_input(now, key), ['base'])
        self.assertEqual(supervision.changed_input(now, key, supervision.PREREQUISITE), [])
        self.assertEqual(supervision.changed_input(dict(now, request='g'), key,
                                                   supervision.PREREQUISITE), ['request'])
        self.assertEqual(supervision.retries_done(now, [key]), 0)
        self.assertEqual(supervision.retries_done(now, [key], supervision.PREREQUISITE), 1)
        self.assertIn('prerequisit', supervision.describe(supervision.PREREQUISITE, 'it'))
        self.assertIn('prerequisite', supervision.describe(supervision.PREREQUISITE))

    def test_the_record_of_the_blocks(self):
        good = {'step': 'implement', 'added': [5, 6]}
        self.assertEqual(read_marker(render_prerequisite(good), 'prerequisite'), good)
        replaced = dict(good, replaced=[6])  # #719
        self.assertEqual(read_marker(render_prerequisite(replaced), 'prerequisite'), replaced)
        self.assertIn('#6', render_prerequisite(replaced, 'it').split('contano: ')[1])
        for bad in ({'step': 'review', 'added': [5]}, {'step': 'preflight', 'added': []},
                    dict(good, replaced=[7]), dict(good, replaced=['5']),
                    dict(good, replaced=5),
                    {'step': 'preflight', 'added': [0]}, {'step': 'preflight', 'added': ['5']},
                    {'step': 'preflight', 'added': [True]}, {'step': 'preflight'}):
            with self.subTest(record=bad):
                self.assertIsNone(read_marker(markdown.publishable('x', 'prerequisite', bad),
                                              'prerequisite'))

    def test_the_preflight_record_lists_the_blocks_only_when_failed_and_not_blocked(self):
        base = {'hash': 'h', 'agent': 'a', 'split': '', 'ready': False, 'blocked': False,
                'labeled': 0, 'gaps': [gap(prerequisite='x', blocked_by=5)]}
        self.assertTrue(markdown._valid_preflight(dict(base, prerequisites=[5])))
        self.assertTrue(markdown._valid_preflight(base))
        for bad in (dict(base, prerequisites=[5], blocked=True),
                    dict(base, prerequisites=['5']),
                    dict(base, gaps=[gap(blocked_by='5')]),
                    dict(base, gaps=[gap(prerequisite=None)])):
            with self.subTest(record=bad):
                self.assertFalse(markdown._valid_preflight(bad))

    def test_an_implementation_written_before_names_no_prerequisite(self):
        old = {k: v for k, v in implemented().items()}
        s = schema.load_schema('implement')
        schema.validate(schema.with_defaults('implement', old), s)
        self.assertEqual(schema.with_defaults('implement', old)['blocked_by'], 0)
        with self.assertRaises(schema.SchemaError):
            schema.validate(dict(old, prerequisite='', blocked_by='5'), s)


if __name__ == '__main__':
    unittest.main()
