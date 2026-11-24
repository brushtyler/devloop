"""Tests red on the base too, and the alert of a red CI on the base branch (issue #459)."""

import dataclasses
import shlex
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from devloop import markdown
from devloop.config import LEGACY_NEEDS_OPERATOR
from devloop.dispatch_base import FLAKY_RETRY_PAUSE, MAX_CASES_LISTED, BaseBroken, MainAlerts
from devloop.dispatcher import Dispatcher
from devloop.github import GitHubError, normalize_checks
from devloop.ledger import Ledger
from devloop.localtests import (Tests, case_commands, case_modules, failed_cases,
                                module_command)
from devloop.planning import BASE_REPAIRED, Action, Snapshot, plan
from devloop.ports import summarize_checks
from devloop.workspace import Workspace

from helpers.devloop_fakes import SHA1, FakeGH, SnapshotGH
from test_devloop import (BASE, CFG, REV, FakeRunner, FakeWS, finding, fixed, implemented, norm,
                          pr, raw_review)
from test_devloop_ledger import LedgerGH, TreeWS
from test_devloop_preflight import PCFG, IssueGH, checked, gap
from test_devloop_supervision import (AUTO, HUMAN, PREFLIGHT_AUTO, SUPER, SUPERVISOR_LOGIN,
                                      Case, crash)

WORKING = 'agent:working'

# Before base_broken and host_tests the failed tests run again alone after a pause (#598): here
# the ``time`` of ``dispatch_base`` is a mock, so no test waits, and ``CLOCK.sleep`` records it.
_CLOCK = mock.patch('devloop.dispatch_base.time')
CLOCK = None


def setUpModule():
    global CLOCK
    CLOCK = _CLOCK.start()


def tearDownModule():
    _CLOCK.stop()

SEP = '=' * 70
DASH = '-' * 70


def unittest_output(*cases, errors=(), summary=True):
    """What ``python3 -m unittest`` prints for the failed tests ``cases`` and ``errors``, each
    ``(name, id)`` or ``(name, id, subtest)``."""
    lines = ['..F.E']
    for kind, items in (('FAIL', cases), ('ERROR', errors)):
        for name, test_id, *sub in items:
            lines += [SEP, f'{kind}: {name} ({test_id})' + (f' {sub[0]}' if sub else ''), DASH,
                      'Traceback (most recent call last):', 'AssertionError: 1 != 2', '']
    lines += [DASH, 'Ran 5 tests in 0.010s', '']
    if summary:
        lines.append(f'FAILED (failures={len(cases)}, errors={len(errors)})')
    return '\n'.join(lines)


class FailedCasesTests(unittest.TestCase):
    def test_the_identities_of_failures_errors_and_subtests(self):
        out = unittest_output(('test_a', 'test_x.Case.test_a'),
                              ('test_s', 'test_x.Case.test_s', '(i=1)'),
                              errors=[('test_e', 'test_y.Case.test_e')])
        cases = failed_cases(out)
        self.assertEqual(cases, ['test_x.Case.test_a', 'test_x.Case.test_s (i=1)',
                                 'test_y.Case.test_e'])
        self.assertEqual(case_modules(cases), ['test_x.py', 'test_y.py'])

    def test_a_module_that_does_not_load_cannot_be_compared(self):
        # Its synthetic id is the same whatever the error: it tells nothing of the tests.
        one = ('test_a', 'test_x.Case.test_a')
        for label, failed in [('3.11 and later', ('test_y', 'unittest.loader._FailedTest.test_y')),
                              ('before 3.11', ('test_y', 'unittest.loader._FailedTest'))]:
            with self.subTest(label):
                self.assertIsNone(failed_cases(unittest_output(one, errors=[failed])))
                self.assertIsNone(failed_cases(unittest_output(errors=[failed])))

    def test_an_id_before_python_3_11_names_the_test_too(self):
        out = unittest_output(('test_a', 'test_x.Case'))
        self.assertEqual(failed_cases(out), ['test_x.Case.test_a'])

    def test_the_failed_modules_of_run_tests_are_read_in_each_module_output(self):
        out = '\n'.join([
            'FAIL test_x (3 tests, 1s)', 'ok   test_z (2 tests, 0s)', '', SEP,
            'FAILED MODULE test_x', SEP, unittest_output(('test_a', 'test_x.Case.test_a')),
            '', 'Slowest: test_x 1s', DASH, 'Ran 5 tests in 1.0s (2 modules, 0 split by class, '
            '2 jobs)', '', 'FAILED (modules=1, worker problems=0)'])
        self.assertEqual(failed_cases(out), ['test_x.Case.test_a'])

    def test_failures_that_cannot_be_told_for_sure_are_none(self):
        one = ('test_a', 'test_x.Case.test_a')
        for label, out in [
                ('no failed test', 'Test command timed out after 10s'),
                ('no summary', unittest_output(one, summary=False)),
                ('a count that differs', unittest_output(one).replace('failures=1',
                                                                      'failures=2')),
                ('a header in the output of a test', unittest_output(one) + '\n' + SEP
                 + '\nFAIL: test_q (test_x.Case.test_q)'),
                ('an unexpected success', unittest_output(one).replace(
                    'errors=0)', 'errors=0, unexpected successes=1)')),
                ('a worker problem', unittest_output(one) + f'\n{SEP}\nWORKER PROBLEM\n{SEP}')]:
            with self.subTest(label):
                self.assertIsNone(failed_cases(out))

    def test_a_test_outside_the_test_modules_has_no_module(self):
        self.assertIsNone(case_modules(['helpers.Case.test_a']))
        self.assertIsNone(case_modules(['test_a']))

    def test_the_workspace_reads_them_from_a_real_unittest_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tests = Path(tmp) / 'tests'
            tests.mkdir()
            (tests / 'test_real.py').write_text(textwrap.dedent('''
                import unittest

                class Case(unittest.TestCase):
                    def test_bad(self):
                        self.assertEqual(1, 2)

                    def test_sub(self):
                        for i in range(2):
                            with self.subTest(i=i):
                                self.assertEqual(i, 0)

                    def test_good(self):
                        pass
            '''))
            (tests / 'test_broken.py').write_text('import not_a_module_459\n')
            ws = Workspace(tmp, '.state', 'main')
            python = shlex.quote(sys.executable)
            command = module_command(['test_real.py']).replace('python3', python)
            self.assertFalse(ws.run_tests(tmp, command, 60)[0])
            self.assertEqual(ws.failed_cases(tmp), ['test_real.Case.test_bad',
                                                    'test_real.Case.test_sub (i=1)'])
            # A module that does not start has no identity to compare.
            command = module_command(['test_broken.py']).replace('python3', python)
            self.assertFalse(ws.run_tests(tmp, command, 60)[0])
            self.assertIsNone(ws.failed_cases(tmp))
            self.assertTrue(ws.run_tests(tmp, 'true', 60)[0])
            self.assertEqual(ws.failed_cases(tmp), [])

    def test_distinct_import_errors_on_branch_and_base_stay_tests_failed(self):
        # The same module fails to import on the branch and on the base, each for its own
        # missing module: real unittest output, read by the workspace, then the dispatcher.
        outputs = {}
        with tempfile.TemporaryDirectory() as tmp:
            python = shlex.quote(sys.executable)
            command = module_command(['test_broken.py']).replace('python3', python)
            for side, missing in (('issue-7', 'missing_on_branch_459'),
                                  ('issue-7-base', 'missing_on_base_459')):
                root = Path(tmp) / side
                (root / 'tests').mkdir(parents=True)
                (root / 'tests' / 'test_broken.py').write_text(f'import {missing}\n')
                ws = Workspace(root, '.state', 'main')
                self.assertFalse(ws.run_tests(root, command, 60)[0])
                outputs[side] = ws.failed_cases(root)
        self.assertEqual(outputs, {'issue-7': None, 'issue-7-base': None})
        test = ImplementTests()
        runner = test.implement(outputs['issue-7'], outputs['issue-7-base'])
        self.assertEqual(len(runner.calls), 1 + CFG.implement_test_retries)
        self.assertEqual(test.gh.labels[7], {'agent:needs-operator'})
        self.assertEqual(markdown.read_marker(test.gh.comments[-1][1], 'notice')['class'],
                         'tests_failed')
        self.assertEqual(test.records(), [])


BRANCH = ['test_x.Case.test_a', 'test_x.Case.test_b']


class ImplementTests(unittest.TestCase):
    def make(self, runner, ws, cfg=CFG):
        self.gh, self.ws, self.logs = FakeGH(), ws, []
        return Dispatcher(cfg, self.gh, ws, runner, log=self.logs.append)

    def implement(self, branch, base, tests=(False,), cfg=CFG):
        """``_do_implement`` of issue 7 whose local tests fail with ``branch`` (the cases of the
        worktree), and with ``base`` on the base commit."""
        runner = FakeRunner(implemented(), implemented())
        ws = FakeWS(files=['impact.py'], modules=['test_impact.py'], tests=tests)
        ws.cases = {'issue-7': branch, 'issue-7-base': base}
        self.make(runner, ws, cfg)._do_implement(7)
        return runner

    def records(self, number=7):
        return [markdown.read_marker(body, 'basebroken') for n, body in self.gh.comments
                if n == number and markdown.read_marker(body, 'basebroken')]

    def test_failures_all_reproduced_on_the_base_wait_for_it(self):
        runner = self.implement(BRANCH, BRANCH + ['test_x.Case.test_other'])
        # One turn: no attempt of correction, no escalation, no PR.
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.gh.created, [])
        self.assertEqual(self.gh.labels[7], {'agent:working'})
        self.assertFalse(any(markdown.read_marker(b, 'notice') for _, b in self.gh.comments))
        # The failed modules ran on the base commit, one at a time, in their own copy; then
        # the failed tests once again alone on the branch (#598), red again.
        branch = self.ws.commands[0][0]
        self.assertEqual(self.ws.commands[1:], [(Path('/wt/issue-7-base'),
                                                 module_command(['test_x.py'])),
                                                (branch, *case_commands(BRANCH))])
        self.assertIn('issue-7-base', self.ws.removed)
        self.assertIn('issue-7', self.ws.removed)
        record, = self.records()
        self.assertEqual(record, {'step': 'implement', 'base': BASE, 'head': self.ws._head,
                                  'count': 2, 'cases': BRANCH})
        text = self.gh.comments[-1][1]
        self.assertIn('Test rossi anche sulla base', text)
        self.assertIn(BASE[:12], text)
        self.assertIn('`test_x.Case.test_b`', text)

    def test_a_failure_only_on_the_branch_stays_tests_failed(self):
        runner = self.implement(BRANCH, BRANCH[:1])
        self.assertEqual(len(runner.calls), 1 + CFG.implement_test_retries)
        self.assertEqual(self.gh.labels[7], {'agent:needs-operator'})
        notice = markdown.read_marker(self.gh.comments[-1][1], 'notice')
        self.assertEqual(notice['class'], 'tests_failed')
        self.assertEqual(self.records(), [])

    def test_failures_that_cannot_be_identified_stay_tests_failed(self):
        for label, branch, base in [('on the branch', None, BRANCH),
                                    ('on the base', BRANCH, None),
                                    ('outside the test modules', ['helpers.test_a'],
                                     ['helpers.test_a'])]:
            with self.subTest(label):
                runner = self.implement(branch, base)
                self.assertEqual(len(runner.calls), 1 + CFG.implement_test_retries)
                self.assertEqual(self.gh.labels[7], {'agent:needs-operator'})
                self.assertEqual(markdown.read_marker(self.gh.comments[-1][1],
                                                      'notice')['class'], 'tests_failed')
                self.assertEqual(self.records(), [])
                if branch is None or branch == ['helpers.test_a']:
                    # Nothing ran on the base.
                    self.assertEqual(len(self.ws.commands), 1 + CFG.implement_test_retries)

    def test_without_the_unittest_selection_the_base_is_not_compared(self):
        # Without a selection the configuration makes ``changed`` ``full``.
        cfg = dataclasses.replace(CFG, tests=Tests(), local_tests='full')
        runner = self.implement(BRANCH, BRANCH, cfg=cfg)
        self.assertEqual(len(runner.calls), 1 + CFG.implement_test_retries)
        self.assertEqual(markdown.read_marker(self.gh.comments[-1][1], 'notice')['class'],
                         'tests_failed')
        self.assertEqual(self.records(), [])

    def test_a_base_reproduced_failure_after_a_retry_stops_the_retries(self):
        # The first attempt also broke a test of its own; the second fixed it.
        runner = FakeRunner(implemented(), implemented())
        ws = FakeWS(files=['impact.py'], modules=['test_impact.py'], tests=(False,))
        calls = []

        def cases(path):
            name = Path(path).name
            if name == 'issue-7-base':
                return BRANCH
            calls.append(name)
            return BRANCH + ['test_x.Case.mine'] if len(calls) == 1 else BRANCH
        ws.failed_cases = cases
        self.make(runner, ws)._do_implement(7)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(self.gh.labels[7], {'agent:working'})
        self.assertEqual(len(self.records()), 1)

    def test_the_marker_lists_at_most_the_limit_of_cases(self):
        many = [f'test_x.Case.test_{i:03}' for i in range(MAX_CASES_LISTED + 5)]
        self.implement(many, many)
        record, = self.records()
        self.assertEqual(record['count'], MAX_CASES_LISTED + 5)
        self.assertEqual(len(record['cases']), MAX_CASES_LISTED)
        self.assertIn('… e altri 5', self.gh.comments[-1][1])


class WorkingGH(FakeGH):
    """Issue 7 in ``agent:working``, without a PR."""

    def __init__(self, labels):
        super().__init__()
        self._working = labels

    def issues_with_label(self, label):
        if label in self._working:
            return [{'number': 7, 'title': 'Issue 7',
                     'labels': [{'name': n} for n in sorted(self._working)],
                     'author': {'login': 'brushtyler'}}]
        return []


class WaitTests(unittest.TestCase):
    """The step waits for the base branch, and starts again once it advances."""

    def snapshot(self, current, comments, labels=('agent:working',)):
        gh = WorkingGH(labels)
        gh.comments = list(comments)
        ws = FakeWS()
        ws.base_head = lambda: current
        logs = []
        d = Dispatcher(CFG, gh, ws, FakeRunner(), log=logs.append)
        return d.snapshot(), logs

    def broken_comment(self):
        """The comment of an implementation of issue 7 whose failures the base reproduced."""
        gh, ws = FakeGH(), FakeWS(files=['impact.py'], modules=['test_impact.py'],
                                  tests=(False,))
        ws.cases = {'issue-7': BRANCH, 'issue-7-base': BRANCH}
        Dispatcher(CFG, gh, ws, FakeRunner(implemented()), log=lambda m: None)._do_implement(7)
        return gh.comments[-1]

    def test_the_issue_waits_while_the_base_stays(self):
        snap, logs = self.snapshot(BASE, [self.broken_comment()])
        self.assertEqual(snap.base_waits, {7: {'holds': True, 'labels': {'agent:working'},
                                               'preflight': None, 'ci': ''}})
        self.assertEqual([(a.kind, a.number) for a in plan(snap, CFG)], [])
        self.assertTrue(any('waits for main' in line for line in logs))

    def test_the_issue_starts_again_once_the_base_advances(self):
        snap, _ = self.snapshot('e' * 40, [self.broken_comment()])
        self.assertEqual(snap.base_waits, {7: {'holds': False, 'labels': {'agent:working'},
                                               'preflight': None, 'ci': ''}})
        actions = plan(snap, CFG)
        self.assertEqual([(a.kind, a.number, a.reason) for a in actions],
                         [('implement', 7, BASE_REPAIRED)])
        # Not while it still runs, nor without a free slot.
        self.assertEqual(plan(snap, CFG, busy=frozenset({7})), [])
        self.assertEqual(plan(snap, dataclasses.replace(CFG, max_open_prs=0)), [])

    def test_a_block_of_the_operator_or_the_supervisor_holds_the_restart(self):
        for block in ('agent:needs-operator', LEGACY_NEEDS_OPERATOR, 'agent:needs-supervisor',
                      'bloccata'):
            with self.subTest(block):
                labels = ('agent:working', block)
                snap, _ = self.snapshot('e' * 40, [self.broken_comment()], labels=labels)
                self.assertEqual(snap.base_waits[7]['labels'], set(labels))
                self.assertEqual(plan(snap, CFG), [])
                # A whole tick: no turn, no PR, the block stays.
                gh = WorkingGH(labels)
                gh.comments = [self.broken_comment()]
                gh.labels[7] = set(labels)
                ws = FakeWS(files=['impact.py'], modules=['test_impact.py'])
                ws.base_head = lambda: 'e' * 40
                runner = FakeRunner(implemented())
                Dispatcher(CFG, gh, ws, runner, log=lambda m: None).tick()
                self.assertEqual(runner.calls, [])
                self.assertEqual(gh.created, [])
                self.assertEqual(gh.labels[7], set(labels))

    def test_a_notice_after_the_record_ends_the_wait(self):
        notice = (7, markdown.render_escalation('stop', cls='failure'))
        snap, _ = self.snapshot(BASE, [self.broken_comment(), notice])
        self.assertEqual(snap.base_waits, {})
        self.assertEqual([(a.kind, a.number) for a in plan(snap, CFG)], [('stale', 7)])

    def test_a_working_issue_without_record_is_stale_as_before(self):
        snap, _ = self.snapshot(BASE, [])
        self.assertEqual([(a.kind, a.number) for a in plan(snap, CFG)], [('stale', 7)])

    def test_the_restart_implements_again(self):
        runner = FakeRunner(implemented())
        gh, ws = FakeGH(), FakeWS(files=['impact.py'], modules=['test_impact.py'])
        gh.comments = [self.broken_comment()]
        gh.labels[7] = {'agent:working'}
        Dispatcher(CFG, gh, ws, runner, log=lambda m: None)._do_implement(7)
        self.assertEqual(len(gh.created), 1)
        self.assertEqual(gh.labels[7], set())


def check_view(record=None, owned=False, **extra):
    """The view of a readiness check (``_preflight_view``) for the hash ``h``."""
    return dict({'hash': 'h', 'record': record, 'recheck': False, 'owned': owned,
                 'relabeled': None, 'refused': None, 'base': None}, **extra)


def check_record(ready):
    return {'hash': 'h', 'ready': ready, 'gaps': [] if ready else [{'kind': 'x'}],
            'split': '', 'agent': REV}


class PreflightRestartTests(unittest.TestCase):
    """With the readiness check the restart goes through it, as a ready issue does."""

    def restart(self, view, labels=('agent:working',), cfg=None):
        cfg = cfg or PCFG
        wait = {'holds': False, 'labels': set(labels), 'preflight': view}
        return [(a.kind, a.number) for a in plan(Snapshot([], [], [7], base_waits={7: wait}),
                                                  cfg)]

    def test_the_restart_follows_the_state_of_the_check(self):
        self.assertEqual(self.restart(check_view(check_record(True))), [('implement', 7)])
        self.assertEqual(self.restart(check_view()), [('preflight', 7)])
        self.assertEqual(self.restart(check_view(dict(check_record(True), hash='old'))),
                         [('preflight', 7)])
        self.assertEqual(self.restart(check_view(check_record(False))),
                         [('preflight_block', 7)])
        # A view not read: nothing is known about the check.
        self.assertEqual(self.restart(None), [])
        # Without free slots no check nor implementation starts.
        full = dataclasses.replace(PCFG, max_open_prs=0)
        self.assertEqual(self.restart(check_view(), cfg=full), [])

    def test_under_a_block_only_a_check_owning_it_runs_on_a_changed_text(self):
        blocked = ('agent:working', 'agent:needs-operator')
        changed = check_record(False) | {'hash': 'old'}
        self.assertEqual(self.restart(check_view(changed, owned=True), blocked),
                         [('preflight', 7)])
        self.assertEqual(self.restart(check_view(changed), blocked), [])
        self.assertEqual(self.restart(check_view(check_record(True)), blocked), [])
        self.assertEqual(self.restart(check_view(check_record(False), owned=True), blocked), [])

    def test_a_request_added_during_the_wait_is_checked_before_the_restart(self):
        gh = IssueGH()
        ws = FakeWS(files=['impact.py'], modules=['test_impact.py'], tests=(False,))
        ws.cases = {'issue-7': BRANCH, 'issue-7-base': BRANCH}
        current = [BASE]
        ws.base_head = lambda: current[0]
        runner = FakeRunner(checked(True), implemented())
        d = Dispatcher(PCFG, gh, ws, runner, log=lambda m: None)

        def planned():
            return [(a.kind, a.number) for a in plan(d.snapshot(), PCFG)]

        self.assertEqual([a.kind for a in d.tick()], ['preflight'])
        self.assertEqual([a.kind for a in d.tick()], ['implement'])
        self.assertEqual(gh.labels[7], {'agent:working'})
        self.assertTrue(any(markdown.read_marker(c['body'], 'basebroken') for c in gh.thread))
        gh.say('Un requisito in più, scritto durante l\'attesa.')
        self.assertEqual(planned(), [])
        # The base advances: the changed request is checked first, then implemented.
        current[0] = 'e' * 40
        ws.tests = [True]
        self.assertEqual(planned(), [('preflight', 7)])
        runner.outputs += [checked(True), implemented()]
        self.assertEqual([a.kind for a in d.tick()], ['preflight'])
        self.assertIn('Un requisito in più', runner.calls[-1][3])
        self.assertEqual(planned(), [('implement', 7)])
        self.assertEqual([a.kind for a in d.tick()], ['implement'])
        self.assertEqual(len(gh.created), 1)
        self.assertEqual(planned(), [])


class RestartEscalationTests(Case):
    """An escalation of the readiness check that restarts the implementation keeps the wait and
    ``agent:working``: the issue is never ``stale``, and resumes once the block is lifted."""

    def waiting(self, cfg, *outputs):
        """Issue 7 in ``agent:working`` whose implementation waits for a base that has already
        moved on, with the readiness check of ``cfg``."""
        d = self.make(cfg, *outputs, has_pr=False)
        self.gh.labels[7] = {WORKING}
        d._wait_base(7, 'implement', BaseBroken('f' * 40, BRANCH), 'f' * 40)
        return d

    def planned(self, d):
        return [(a.kind, a.number) for a in plan(d.snapshot(), self.cfg)]

    def implemented(self, d):
        self.assertEqual(self.planned(d), [('implement', 7)])
        self.runner.outputs.append(implemented())
        self.assertEqual(self.tick(d), [('implement', 7)])
        self.assertEqual(len(self.gh.created), 1)

    def test_defaults_to_the_supervisor_wait_for_its_decision(self):
        for how in ('accept-defaults', 'decide', 'escalate'):
            with self.subTest(how):
                d = self.waiting(PREFLIGHT_AUTO, checked(False, [gap('default_available',
                                                                     'JSON')]))
                self.assertEqual(self.tick(d), [('preflight', 7)])
                self.assertEqual(self.gh.labels[7], {WORKING, SUPER})
                self.assertEqual(self.escalations(7)[-1]['class'], 'preflight_default')
                # Not stale: the issue waits for the supervisor.
                self.assertEqual(self.planned(d), [])
                if how == 'escalate':
                    self.gh.command(7, '/devloop escalate da vedere')
                    self.assertEqual(self.tick(d), [('supervise', 7)])
                    self.assertEqual(self.gh.labels[7], {WORKING, HUMAN})
                    self.assertEqual(self.planned(d), [])
                    self.gh.command(7, '/devloop accept-defaults', login='brushtyler')
                else:
                    self.gh.command(7, f'/devloop {how} JSON')
                self.assertEqual(self.tick(d), [('supervise', 7)])
                self.assertEqual(self.gh.labels[7], {WORKING})
                # The check runs again with the decision, then the implementation.
                self.runner.outputs.append(checked(True))
                self.assertEqual(self.tick(d), [('preflight', 7)])
                self.assertEqual(self.gh.labels[7], {WORKING})
                self.implemented(d)

    def test_a_failed_check_keeps_the_issue_and_a_retry_resumes_it(self):
        for cfg, label, by in [(dataclasses.replace(CFG, preflight=REV), HUMAN, 'brushtyler'),
                               (PREFLIGHT_AUTO, SUPER, SUPERVISOR_LOGIN)]:
            with self.subTest(label):
                d = self.waiting(cfg, crash())
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

    def test_a_failure_without_the_step_ends_the_wait_as_before(self):
        d = self.waiting(dataclasses.replace(CFG, preflight=REV))
        d._escalate_failure(Action('preflight', 7), None, GitHubError('down'))
        self.assertEqual(self.gh.labels[7], {HUMAN})
        self.assertEqual(self.planned(d), [])


class FixTests(unittest.TestCase):
    def fix(self, branch, base):
        first = norm(raw_review('request_changes', [finding()]))
        self.gh = FakeGH()
        self.ws = FakeWS(files=['devloop/review.py'], modules=['test_devloop.py'],
                         tests=(False,))
        self.ws.cases = {'pr-100': branch, 'pr-100-base': base}
        self.runner = FakeRunner(fixed(), fixed())
        self.pr = pr(labels=['agent:changes'], reviews=[first])
        self.gh.labels[100] = {'agent:changes'}
        Dispatcher(CFG, self.gh, self.ws, self.runner, log=lambda m: None)._do_fix(self.pr)

    def test_a_fix_failing_as_the_base_waits_without_push_nor_escalation(self):
        self.fix(BRANCH, BRANCH)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.ws.pushes, [])
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        record = markdown.read_marker(self.gh.comments[-1][1], 'basebroken')
        self.assertEqual((record['step'], record['head'], record['base']),
                         ('fix', self.pr.head_sha, BASE))
        # The fix of the same head waits; a new base lets it run.
        self.pr.base_broken = record
        self.assertEqual(plan(Snapshot([self.pr], [], []), CFG), [])
        self.pr.base_broken = None
        self.assertEqual([a.kind for a in plan(Snapshot([self.pr], [], []), CFG)], ['fix'])

    def test_a_fix_failing_only_on_the_branch_escalates_as_before(self):
        self.fix(BRANCH, [])
        self.assertEqual(len(self.runner.calls), 1 + CFG.implement_test_retries)
        self.assertIn('agent:needs-operator', self.gh.labels[100])
        self.assertEqual(markdown.read_marker(self.gh.comments[-1][1], 'notice')['class'],
                         'tests_failed')

    def test_the_wait_holds_for_the_current_head_and_base_only(self):
        self.fix(BRANCH, BRANCH)
        body = self.gh.comments[-1][1]
        thread = [{'author': {'login': 'brushtyler'}, 'body': body}]
        d = Dispatcher(CFG, self.gh, self.ws, FakeRunner(), log=lambda m: None)
        for current, head, waits in [(BASE, self.pr.head_sha, True),
                                     ('e' * 40, self.pr.head_sha, False),
                                     (BASE, 'f' * 40, False)]:
            with self.subTest(current=current[:1], head=head[:1]):
                wait = d._base_wait(thread, 'fix', head)
                self.assertEqual(wait is not None and d._base_holds(wait, lambda c=current: c),
                                 waits)


class MainWS(FakeWS):
    """The base branch at its first commit, with a first-parent chain."""

    def __init__(self, chain):
        super().__init__()
        self.chain = list(chain)

    def base_head(self):
        return self.chain[0][0]

    def first_parents(self, commit, limit):
        return self.chain[:limit]


class MainGH(FakeGH):
    """The checks of each commit, by sha."""

    def __init__(self, checks):
        super().__init__()
        self.by_sha = checks
        self.reads = []

    def check_details(self, sha):
        self.reads.append(sha)
        return [dict(c) for c in self.by_sha.get(sha, ())]


RED, GREEN, OLD = 'b' * 40, 'c' * 40, 'e' * 40
FAILED = [{'name': 'Unit Tests (Python 3.12)', 'bucket': 'fail', 'conclusion': 'failure'},
          {'name': 'devloop tests', 'bucket': 'pass', 'conclusion': 'success'}]
PASSED = [{'name': 'Unit Tests (Python 3.12)', 'bucket': 'pass', 'conclusion': 'success'}]
CHAIN = [(RED, 'Merge pull request #451 from x/y'), (OLD, 'Merge pull request #450 from x/z'),
         (GREEN, 'Merge pull request #449 from x/w')]


class MainAlertTests(unittest.TestCase):
    def make(self, checks, chain=CHAIN, alert_issue=459, alerts=None):
        cfg = dataclasses.replace(CFG, run=dataclasses.replace(CFG.run, alert_issue=alert_issue))
        self.gh, self.ws, self.logs = MainGH(checks), MainWS(chain), []
        return Dispatcher(cfg, self.gh, self.ws, FakeRunner(), log=self.logs.append,
                          main_alerts=alerts)

    def test_a_failed_check_gives_one_alert_per_commit(self):
        d = self.make({RED: FAILED, OLD: FAILED, GREEN: PASSED})
        d._watch_main()
        d._watch_main()
        self.assertEqual(len(self.gh.comments), 1)
        number, body = self.gh.comments[0]
        self.assertEqual(number, 459)
        self.assertIn('CI rossa su `main`', body)
        self.assertIn(RED[:12], body)
        self.assertIn('`Unit Tests (Python 3.12)`', body)
        self.assertNotIn('`devloop tests`', body)
        self.assertIn(f'dall\'ultimo commit verde, `{GREEN[:12]}`', body)
        self.assertIn(f'`{RED[:12]}` Merge pull request #451', body)
        self.assertIn(f'`{OLD[:12]}` Merge pull request #450', body)
        self.assertNotIn('#449', body)
        # Only a report: no label, no PR, no merge.
        self.assertEqual(self.gh.labels, {})
        self.assertEqual(self.gh.created, [])
        self.assertFalse(getattr(self.gh, 'merged', []))
        # A new red commit gives a new alert.
        new = 'f' * 40
        self.ws.chain = [(new, 'Merge pull request #452 from x/v')] + CHAIN
        self.gh.by_sha[new] = FAILED
        d._watch_main()
        self.assertEqual(len(self.gh.comments), 2)
        self.assertIn(new[:12], self.gh.comments[1][1])

    def test_pending_missing_or_passed_checks_give_no_alert(self):
        pending = [{'name': 'Unit Tests', 'bucket': 'pending', 'conclusion': None}]
        for label, checks in [('pending', pending), ('missing', []), ('passed', PASSED)]:
            with self.subTest(label):
                self.make({RED: checks})._watch_main()
                self.assertEqual(self.gh.comments, [])

    def test_unreadable_checks_give_no_alert(self):
        d = self.make({})

        def fail(sha):
            raise GitHubError('rate limited')
        self.gh.check_details = fail
        d._watch_main()
        self.assertEqual(self.gh.comments, [])

    def test_an_error_of_the_watch_never_stops_the_tick(self):
        d = self.make({RED: FAILED})

        def broken():
            raise RuntimeError('secret output')
        self.ws.base_head = broken
        d._watch_main()
        self.assertEqual(self.gh.comments, [])
        self.assertTrue(any('RuntimeError' in line and 'secret' not in line
                            for line in self.logs))

    def test_without_alert_issue_nothing_is_read(self):
        self.make({RED: FAILED}, alert_issue=0)._watch_main()
        self.assertEqual((self.gh.comments, self.gh.reads), ([], []))

    def test_the_alerted_commit_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as state:
            self.make({RED: FAILED, GREEN: PASSED}, alerts=MainAlerts(state))._watch_main()
            self.assertEqual(len(self.gh.comments), 1)
            self.make({RED: FAILED, GREEN: PASSED}, alerts=MainAlerts(state))._watch_main()
            self.assertEqual(self.gh.comments, [])

    def test_no_green_commit_within_the_history_read(self):
        self.make({RED: FAILED, OLD: FAILED, GREEN: FAILED})._watch_main()
        body = self.gh.comments[0][1]
        self.assertIn('Nessun commit verde', body)
        self.assertIn('#449', body)

    def test_a_failed_publication_is_tried_again(self):
        d = self.make({RED: FAILED, GREEN: PASSED})
        original = self.gh.comment

        def fail(number, body):
            raise RuntimeError('down')
        self.gh.comment = fail
        d._watch_main()
        self.assertEqual(self.gh.comments, [])
        self.gh.comment = original
        d._watch_main()
        self.assertEqual(len(self.gh.comments), 1)

    def test_a_tick_publishes_it_and_a_dry_run_does_not(self):
        d = self.make({RED: FAILED, GREEN: PASSED})
        d.tick(dry_run=True)
        self.assertEqual(self.gh.comments, [])
        d.tick()
        self.assertEqual([n for n, _ in self.gh.comments], [459])


PENDING = [{'name': 'Unit Tests (Python 3.12)', 'bucket': 'pending', 'conclusion': ''}]


class HostTestsTests(unittest.TestCase):
    """#497: tests red on the branch and on its base commit, green in the CI of that commit, are
    the host's failure (``host_tests``), not the base's (``base_broken``)."""

    LINK = f'https://github.com/{CFG.repo}/commit/{BASE}/checks'

    def implement(self, checks, cfg=CFG, gh=None):
        """``_do_implement`` of issue 7 whose local tests fail with ``BRANCH`` on the branch and
        on the base commit ``BASE``, whose CI has ``checks``."""
        self.gh = gh or MainGH({BASE: checks})
        self.runner = FakeRunner(implemented(), implemented())
        self.ws = FakeWS(files=['impact.py'], modules=['test_impact.py'], tests=(False,))
        self.ws.cases = {'issue-7': BRANCH, 'issue-7-base': BRANCH}
        Dispatcher(cfg, self.gh, self.ws, self.runner, log=lambda m: None)._do_implement(7)

    def notices(self, number):
        return [markdown.read_marker(b, 'notice') for n, b in self.gh.comments
                if n == number and markdown.read_marker(b, 'notice')]

    def basebroken(self, number):
        return [markdown.read_marker(b, 'basebroken') for n, b in self.gh.comments
                if n == number and markdown.read_marker(b, 'basebroken')]

    def test_a_green_ci_of_the_base_escalates_host_tests_to_the_operator(self):
        # Even in merge mode auto: a new class is critical, never the supervisor's.
        for label, cfg in (('off', CFG), ('auto', AUTO)):
            with self.subTest(label):
                self.implement(PASSED, cfg)
                self.assertIn(BASE, self.gh.reads)
                # One turn: no other attempt of the agent, no PR, no wait for the base.
                self.assertEqual(len(self.runner.calls), 1)
                self.assertEqual(self.gh.created, [])
                self.assertEqual(self.gh.labels[7], {'agent:needs-operator'})
                self.assertEqual(self.basebroken(7), [])
                notice, = self.notices(7)
                self.assertEqual((notice['class'], notice['critical'], notice['to']),
                                 ('host_tests', True, 'operator'))
                self.assertEqual(notice['host_tests'], {
                    'step': 'implement', 'base': BASE, 'ci': self.LINK, 'count': 2,
                    'cases': BRANCH})
                text = self.gh.comments[-1][1]
                self.assertIn(self.LINK, text)
                self.assertIn(BASE[:12], text)
                self.assertIn('`test_x.Case.test_b`', text)
                self.assertIn('**Classe:** `host_tests`', text)
                self.assertIn('critica', text)

    def test_a_red_ci_of_the_base_stays_base_broken(self):
        self.implement(FAILED)
        self.assertIn(BASE, self.gh.reads)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.gh.labels[7], {'agent:working'})
        self.assertEqual(self.notices(7), [])
        record, = self.basebroken(7)
        self.assertEqual((record['step'], record['base'], record['cases']),
                         ('implement', BASE, BRANCH))

    def test_a_ci_not_known_green_stays_base_broken(self):
        unreadable = MainGH({})

        def fail(sha):
            raise GitHubError('HTTP 502')
        unreadable.check_details = fail
        for label, checks, gh in (('pending', PENDING, None), ('missing', [], None),
                                  ('unreadable', None, unreadable)):
            with self.subTest(label):
                self.implement(checks, gh=gh)
                self.assertEqual(self.gh.labels[7], {'agent:working'})
                self.assertEqual(self.notices(7), [])
                self.assertEqual(len(self.basebroken(7)), 1)

    def test_a_fix_with_a_green_ci_of_the_base_escalates_without_push(self):
        first = norm(raw_review('request_changes', [finding()]))
        for checks, cls in ((PASSED, 'host_tests'), (FAILED, None)):
            with self.subTest(cls=cls):
                self.gh = MainGH({BASE: checks})
                ws = FakeWS(files=['devloop/review.py'], modules=['test_devloop.py'],
                            tests=(False,))
                ws.cases = {'pr-100': BRANCH, 'pr-100-base': BRANCH}
                runner = FakeRunner(fixed(), fixed())
                item = pr(labels=['agent:changes'], reviews=[first])
                self.gh.labels[100] = {'agent:changes'}
                Dispatcher(CFG, self.gh, ws, runner, log=lambda m: None)._do_fix(item)
                self.assertEqual(len(runner.calls), 1)
                self.assertEqual(ws.pushes, [])
                if cls is None:
                    self.assertEqual(self.gh.labels[100], {'agent:changes'})
                    self.assertEqual(len(self.basebroken(100)), 1)
                    continue
                self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
                self.assertEqual(self.basebroken(100), [])
                notice, = self.notices(100)
                self.assertEqual(notice['class'], 'host_tests')
                self.assertEqual(notice['host_tests']['step'], 'fix')
                self.assertEqual(notice['host_tests']['ci'], self.LINK)

    def test_checks_without_a_success_are_not_a_green_ci(self):
        # As GitHub reports them (``normalize_checks``): a skipped or neutral job counts as
        # passed for the gates of a PR, but does not prove that the tests ran.
        def checks(*conclusions):
            runs = [{'id': i, 'name': f'Unit Tests {i}', 'app_id': 15368, 'status': 'completed',
                     'conclusion': c} for i, c in enumerate(conclusions)]
            return normalize_checks(runs, {'statuses': [], 'total_count': 0})
        self.assertEqual(summarize_checks(checks('skipped'))[0], 'pass')
        for label, details in (('skipped', checks('skipped')), ('neutral', checks('neutral')),
                               ('success and skipped', checks('success', 'skipped')),
                               ('no check run', checks())):
            with self.subTest(label):
                self.implement(details)
                self.assertEqual(self.gh.labels[7], {'agent:working'})
                self.assertEqual(self.notices(7), [])
                self.assertEqual(len(self.basebroken(7)), 1)
        self.implement(checks('success', 'success'))
        self.assertEqual(self.notices(7)[0]['class'], 'host_tests')


class GreenBaseLedgerGH(LedgerGH):
    """``LedgerGH`` whose every commit has a green CI."""

    def check_details(self, sha):
        return [dict(c) for c in PASSED]


class TimedWS(FakeWS):
    """``FakeWS`` that records the time limit of each test command."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.timeouts = []

    def run_tests(self, path, command, timeout):
        self.timeouts.append((command, timeout))
        return super().run_tests(path, command, timeout)


class UnstableTests(unittest.TestCase):
    """#598: before ``host_tests`` and ``base_broken`` the failed tests run once again alone on
    the branch, after a pause; when they pass they are unstable under load, not a failure of the
    host nor of the base."""

    def setUp(self):
        CLOCK.reset_mock()
        self.sleep = CLOCK.sleep

    def implement(self, tests):
        """``_do_implement`` of issue 7 with a ledger, whose local tests fail with ``BRANCH`` on
        the branch and on its base commit, whose CI is green; ``tests``, the outcome of each
        test command: the branch's, the base's, then the retry alone."""
        self.gh = GreenBaseLedgerGH()
        self.ws = TreeWS(files=['impact.py'], modules=['test_impact.py'], tests=tests)
        self.ws.cases = {'issue-7': BRANCH, 'issue-7-base': BRANCH}
        self.runner = FakeRunner(implemented(), implemented())
        Dispatcher(CFG, self.gh, self.ws, self.runner, log=lambda m: None,
                   ledger=Ledger(self.gh))._do_implement(7)

    def notices(self, number):
        return [markdown.read_marker(c['body'], 'notice') for c in self.gh.thread.get(number, [])
                if markdown.read_marker(c['body'], 'notice')]

    def verification_bodies(self, number):
        return [c['body'] for c in self.gh.thread[number]
                if (markdown.read_marker(c['body'], 'ledger') or {}).get('type')
                == 'verification']

    def test_tests_passing_alone_are_unstable_and_the_implementation_goes_on(self):
        self.implement((False, False, True))
        # The pause, then the failed tests alone on the branch, after the base.
        self.sleep.assert_called_once_with(FLAKY_RETRY_PAUSE)
        branch = self.ws.commands[0][0]
        self.assertEqual(self.ws.commands[1:], [
            (Path('/wt/issue-7-base'), module_command(['test_x.py'])),
            (branch, *case_commands(BRANCH))])
        # No escalation, no other turn of the agent: the PR opens.
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.notices(7), [])
        self.assertEqual(len(self.gh.created), 1)
        # The failed verification stays; the retry names the unstable tests and is the one
        # bound to the commit.
        failed, retry = self.gh.records(7, 'verification')
        self.assertEqual((failed['outcome'], failed['commit'], 'retried' in failed),
                         ('fail', None, False))
        self.assertEqual((retry['outcome'], retry['attempt'], retry['retried'],
                          retry['command']), ('pass', 1, BRANCH, *case_commands(BRANCH)))
        self.assertIsNotNone(retry['commit'])
        body = self.verification_bodies(7)[-1]
        self.assertIn('**Test instabili**', body)
        self.assertIn('`test_x.Case.test_b`', body)

    def test_tests_failing_alone_too_escalate_host_tests(self):
        self.implement((False,))
        self.sleep.assert_called_once_with(FLAKY_RETRY_PAUSE)
        self.assertEqual(self.ws.commands[-1][1], *case_commands(BRANCH))
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.gh.created, [])
        self.assertEqual(self.gh.labels[7], {'agent:needs-operator'})
        notice, = self.notices(7)
        self.assertEqual((notice['class'], notice['critical'], notice['to']),
                         ('host_tests', True, 'operator'))
        self.assertEqual(notice['host_tests']['cases'], BRANCH)
        failed, retry = self.gh.records(7, 'verification')
        self.assertEqual((failed['outcome'], retry['outcome'], retry['retried']),
                         ('fail', 'fail', BRANCH))
        body = self.verification_bodies(7)[-1]
        self.assertIn('**Test rilanciati da soli**, falliscono ancora', body)
        self.assertNotIn('Test instabili', body)

    def test_a_fix_whose_tests_pass_alone_is_pushed_without_escalation(self):
        first = norm(raw_review('request_changes', [finding()]))
        gh = MainGH({BASE: PASSED})
        ws = FakeWS(files=['devloop/review.py'], modules=['test_devloop.py'],
                    tests=(False, False, True))
        ws.cases = {'pr-100': BRANCH, 'pr-100-base': BRANCH}
        runner = FakeRunner(fixed(), fixed())
        gh.labels[100] = {'agent:changes'}
        Dispatcher(CFG, gh, ws, runner, log=lambda m: None)._do_fix(
            pr(labels=['agent:changes'], reviews=[first]))
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(ws.commands[-1][1], *case_commands(BRANCH))
        self.assertEqual(len(ws.pushes), 1)
        self.assertFalse(any(markdown.read_marker(b, 'notice') for _, b in gh.comments))
        self.assertNotIn('agent:needs-operator', gh.labels[100])

    def base_broken(self, tests):
        """``_do_implement`` of issue 7 whose local tests fail with ``BRANCH`` on the branch and
        on its base commit, whose CI failed; ``tests`` as in ``implement``."""
        gh = MainGH({BASE: FAILED})
        ws = FakeWS(files=['impact.py'], modules=['test_impact.py'], tests=tests)
        ws.cases = {'issue-7': BRANCH, 'issue-7-base': BRANCH}
        Dispatcher(CFG, gh, ws, FakeRunner(implemented()), log=lambda m: None)._do_implement(7)
        records = [n for n, b in gh.comments if markdown.read_marker(b, 'basebroken')]
        return gh, ws, records

    def test_a_base_broken_failure_passing_alone_does_not_wait(self):
        # A red CI of the base: the failed tests run again alone first, and pass.
        gh, ws, records = self.base_broken((False, False, True))
        self.sleep.assert_called_once_with(FLAKY_RETRY_PAUSE)
        self.assertEqual(ws.commands[-1][1], *case_commands(BRANCH))
        self.assertEqual(records, [])
        self.assertEqual(len(gh.created), 1)
        self.assertFalse(any(markdown.read_marker(b, 'notice') for _, b in gh.comments))

    def test_a_base_broken_failure_failing_alone_too_waits_for_the_base(self):
        gh, ws, records = self.base_broken((False,))
        self.sleep.assert_called_once_with(FLAKY_RETRY_PAUSE)
        self.assertEqual(len(ws.commands), 3)
        self.assertEqual(records, [7])
        self.assertEqual(gh.created, [])
        self.assertEqual(gh.labels[7], {WORKING})

    def test_each_module_of_the_retry_has_its_own_time_limit(self):
        # Two modules: two commands, each with the whole test_timeout_seconds, not one command
        # sharing it; the retry stops at the first that fails.
        cases = ['test_x.Case.test_a', 'test_y.Case.test_b']
        cfg = dataclasses.replace(CFG, test_timeout_seconds=7)
        for tests, ran in [((True,), 2), ((False,), 1)]:
            with self.subTest(tests=tests):
                ws = TimedWS(tests=tests)
                d = Dispatcher(cfg, FakeGH(), ws, FakeRunner(), log=lambda m: None)
                passed, entry = d._retry_alone(None, Path('/wt/issue-7'), 1,
                                               BaseBroken(BASE, cases))
                self.assertEqual(passed, tests[0])
                self.assertIsNone(entry)
                self.assertEqual(ws.timeouts, [(c, 7) for c in case_commands(cases)[:ran]])

    def test_the_ledger_records_the_commands_of_the_retry_as_one(self):
        cases = ['test_x.Case.test_a', 'test_y.Case.test_b']
        gh = GreenBaseLedgerGH()
        ws = TreeWS(files=['impact.py'], modules=['test_impact.py'])
        d = Dispatcher(CFG, gh, ws, FakeRunner(), log=lambda m: None, ledger=Ledger(gh))
        passed, entry = d._retry_alone((7, 'implement'), Path('/wt/issue-7'), 2,
                                       BaseBroken(BASE, cases))
        self.assertTrue(passed)
        self.assertEqual([c for _, c in ws.commands], case_commands(cases))
        retry, = gh.records(7, 'verification')
        self.assertEqual((retry['command'], retry['retried'], retry['attempt']),
                         (' && '.join(case_commands(cases)), cases, 2))


FIXTURE_MODULE = '''
import os
import unittest

MARK = os.path.join(os.path.dirname(__file__), 'ran')


def first():
    """Whether this is the first run: a ``transient`` fixture fails only then."""
    if os.path.exists(MARK):
        return False
    open(MARK, 'w').close()
    return True


def setUpModule():
    if {module!r} == 'always' or {module!r} == 'transient' and first():
        raise RuntimeError('setUpModule')


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if {cls!r} == 'always' or {cls!r} == 'transient' and first():
            raise RuntimeError('setUpClass')

    def test_a(self):
        pass


class Other(unittest.TestCase):
    def test_b(self):
        pass
'''


class CaseCommandTests(unittest.TestCase):
    """#598: ``case_commands`` run only the failed tests, each by its whole id, one command
    per module."""

    def test_one_command_per_module_each_test_once(self):
        self.assertEqual(
            case_commands(['test_y.Case.test_c', 'test_x.Case.test_a (i=1)',
                           'test_x.Case.test_a (i=2)']),
            ["python3 -m unittest discover -s tests -p test_x.py -k '*test_x.Case.test_a'",
             "python3 -m unittest discover -s tests -p test_y.py -k '*test_y.Case.test_c'"])

    def test_a_failed_fixture_runs_its_class_or_its_module(self):
        self.assertEqual(
            case_commands(['test_x.Case.setUpClass', 'test_x.Case.test_a',
                           'test_x.Other.tearDownClass', 'test_y.setUpModule',
                           'test_y.Case.test_c', 'test_z.tearDownModule']),
            ["python3 -m unittest discover -s tests -p test_x.py -k '*test_x.Case.*' "
             "-k '*test_x.Case.test_a' -k '*test_x.Other.*'",
             'python3 -m unittest discover -s tests -p test_y.py',
             'python3 -m unittest discover -s tests -p test_z.py'])

    def run_fixture(self, module='never', cls='never'):
        """A real run of a module whose ``setUpModule`` and ``setUpClass`` fail ``always``,
        ``never`` or only the first time (``transient``): ``(cases, passed, tail)``, the failed
        tests of the first run, then the outcome of their run alone."""
        with tempfile.TemporaryDirectory() as tmp:
            tests = Path(tmp) / 'tests'
            tests.mkdir()
            (tests / 'test_fixture.py').write_text(FIXTURE_MODULE.format(module=module, cls=cls))
            ws = Workspace(tmp, '.state', 'main')
            python = shlex.quote(sys.executable)
            command = module_command(['test_fixture.py']).replace('python3', python)
            self.assertFalse(ws.run_tests(tmp, command, 60)[0])
            cases = ws.failed_cases(tmp)
            passed, tail = True, ''
            for command in case_commands(cases):
                passed, tail = ws.run_tests(tmp, command.replace('python3', python), 60)
                if not passed:
                    break
            return cases, passed, tail

    def test_a_transient_fixture_passes_run_again(self):
        for where, kwargs, case, ran in [
                ('class', {'cls': 'transient'}, 'test_fixture.Case.setUpClass', 'Ran 1 test'),
                ('module', {'module': 'transient'}, 'test_fixture.setUpModule', 'Ran 2 tests')]:
            with self.subTest(where):
                cases, passed, tail = self.run_fixture(**kwargs)
                self.assertEqual(cases, [case])
                self.assertTrue(passed, tail)
                self.assertIn(ran, tail)

    def test_a_persistent_fixture_fails_run_again(self):
        for where, kwargs, case in [('class', {'cls': 'always'}, 'test_fixture.Case.setUpClass'),
                                    ('module', {'module': 'always'}, 'test_fixture.setUpModule')]:
            with self.subTest(where):
                cases, passed, tail = self.run_fixture(**kwargs)
                self.assertEqual(cases, [case])
                self.assertFalse(passed)
                self.assertIn(case.rsplit('.', 1)[1], tail)

    def test_a_real_run_selects_only_those_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            tests = Path(tmp) / 'tests'
            tests.mkdir()
            (tests / 'test_load.py').write_text(textwrap.dedent('''
                import unittest

                class Case(unittest.TestCase):
                    def test_a(self):
                        pass

                    def test_ab(self):
                        self.fail('not selected')

                    def test_b(self):
                        self.fail('red')
            '''))
            ws = Workspace(tmp, '.state', 'main')
            python = shlex.quote(sys.executable)
            passed, tail = ws.run_tests(
                tmp, case_commands(['test_load.Case.test_a'])[0].replace('python3', python), 60)
            self.assertTrue(passed, tail)
            self.assertIn('Ran 1 test', tail)
            command, = case_commands(['test_load.Case.test_b', 'test_load.Case.test_a'])
            self.assertFalse(ws.run_tests(tmp, command.replace('python3', python), 60)[0])
            self.assertEqual(ws.failed_cases(tmp), ['test_load.Case.test_b'])


class WaitingGH(MainGH):
    """Issue 7, in ``agent:working`` while its labels have it, with the checks of each commit."""

    def issues_with_label(self, label):
        if label in self.labels.get(7, ()):
            return [{'number': 7, 'title': 'Issue 7',
                     'labels': [{'name': n} for n in sorted(self.labels[7])],
                     'author': {'login': 'brushtyler'}}]
        return []


class WaitingPRGH(SnapshotGH):
    """PR #100 at ``SHA1`` (``SnapshotGH``), with the checks of each commit."""

    def __init__(self, checks):
        super().__init__([])
        self.by_sha = checks
        self.reads = []

    check_details = MainGH.check_details


class HostTestsDuringTheWaitTests(unittest.TestCase):
    """#497: a step that waits for the base branch because the CI of the base commit was not
    known green escalates ``host_tests`` once that CI passes, without a new commit of the base
    branch: the snapshot reads it again at each tick while the wait holds."""

    def ws(self, **kwargs):
        """A ``FakeWS`` whose base branch stays at ``BASE``."""
        ws = FakeWS(**kwargs)
        ws.base_head = lambda: BASE
        return ws

    def tick(self, d):
        return [(a.kind, a.number) for a in d.tick()]

    def notices(self, gh, number):
        return [markdown.read_marker(b, 'notice') for n, b in gh.comments
                if n == number and markdown.read_marker(b, 'notice')]

    def waiting(self, gh, d):
        """Tick ``d`` while the CI of ``BASE`` is pending, then unreadable: the step waits."""
        number = 100 if isinstance(gh, WaitingPRGH) else 7
        labels = set(gh.labels[number])
        for state in ('pending', 'unreadable'):
            with self.subTest(state):
                if state == 'unreadable':
                    def fail(sha):
                        raise GitHubError('HTTP 502')
                    gh.check_details = fail
                self.assertEqual(self.tick(d), [])
                self.assertEqual(gh.labels[number], labels)
                self.assertEqual(self.notices(gh, number), [])
        del gh.check_details
        gh.reads.clear()

    def test_an_implementation_escalates_once_the_ci_of_the_base_passes(self):
        gh = WaitingGH({BASE: PENDING})
        ws = self.ws(files=['impact.py'], modules=['test_impact.py'], tests=(False,))
        ws.cases = {'issue-7': BRANCH, 'issue-7-base': BRANCH}
        runner = FakeRunner(implemented())
        d = Dispatcher(CFG, gh, ws, runner, log=lambda m: None)
        gh.labels[7] = {WORKING}
        d._do_implement(7)
        self.assertEqual(gh.labels[7], {WORKING})
        self.assertTrue(any(markdown.read_marker(b, 'basebroken') for _, b in gh.comments))
        # The failed tests ran again alone before the wait (#598), and failed again.
        self.assertEqual(ws.commands[-1][1], *case_commands(BRANCH))
        self.assertEqual(len(ws.commands), 3)
        self.waiting(gh, d)
        # The same base commit, its CI now green.
        gh.by_sha[BASE] = PASSED
        self.assertEqual(self.tick(d), [('host_tests', 7)])
        self.assertIn(BASE, gh.reads)
        self.assertEqual(gh.labels[7], {'agent:needs-operator'})
        notice, = self.notices(gh, 7)
        self.assertEqual((notice['class'], notice['critical'], notice['to']),
                         ('host_tests', True, 'operator'))
        self.assertEqual(notice['host_tests'], {
            'step': 'implement', 'base': BASE, 'ci': HostTestsTests.LINK, 'count': 2,
            'cases': BRANCH})
        self.assertIn('mentre il passo attendeva', gh.comments[-1][1])
        # Once: no other turn, no PR, and nothing more to do.
        self.assertEqual(self.tick(d), [])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(gh.created, [])

    def test_tests_passing_alone_while_the_ci_of_the_base_is_pending_never_escalate(self):
        # #598: the CI of the base is pending, so the step would wait as base_broken and
        # escalate host_tests once it turns green; the failed tests run again alone first, pass,
        # and the implementation goes on: no wait, so no host_tests later.
        gh = WaitingGH({BASE: PENDING})
        ws = self.ws(files=['impact.py'], modules=['test_impact.py'], tests=(False, False, True))
        ws.cases = {'issue-7': BRANCH, 'issue-7-base': BRANCH}
        runner = FakeRunner(implemented())
        d = Dispatcher(CFG, gh, ws, runner, log=lambda m: None)
        gh.labels[7] = {WORKING}
        d._do_implement(7)
        self.assertEqual(ws.commands[-1][1], *case_commands(BRANCH))
        self.assertEqual(len(ws.commands), 3)
        self.assertFalse(any(markdown.read_marker(b, 'basebroken') for _, b in gh.comments))
        self.assertEqual(len(gh.created), 1)
        gh.by_sha[BASE] = PASSED
        self.assertNotIn(('host_tests', 7), self.tick(d))
        self.assertEqual(self.notices(gh, 7), [])
        self.assertEqual(len(runner.calls), 1)

    def test_a_blocked_issue_escalates_once_its_block_is_lifted(self):
        wait = {'holds': True, 'labels': {WORKING, 'agent:needs-supervisor'}, 'preflight': None,
                'ci': HostTestsTests.LINK}
        snap = Snapshot([], [], [7], base_waits={7: wait})
        self.assertEqual(plan(snap, CFG), [])
        wait['labels'] = {WORKING}
        self.assertEqual([(a.kind, a.number) for a in plan(snap, CFG)], [('host_tests', 7)])
        # It takes no slot.
        self.assertEqual([a.kind for a in plan(snap, dataclasses.replace(CFG, max_open_prs=0))],
                         ['host_tests'])

    def test_a_fix_escalates_once_the_ci_of_the_base_passes(self):
        gh = WaitingPRGH({BASE: PENDING})
        gh.labels[100] = {'agent:changes'}
        d = Dispatcher(CFG, gh, self.ws(), FakeRunner(), log=lambda m: None)
        d._wait_base(100, 'fix', BaseBroken(BASE, BRANCH), SHA1)
        self.waiting(gh, d)
        gh.by_sha[BASE] = PASSED
        self.assertEqual(self.tick(d), [('host_tests', 100)])
        self.assertEqual(gh.labels[100], {'agent:needs-operator'})
        notice, = self.notices(gh, 100)
        self.assertEqual(notice['class'], 'host_tests')
        self.assertEqual(notice['host_tests']['step'], 'fix')
        self.assertEqual(notice['host_tests']['ci'], HostTestsTests.LINK)
        self.assertEqual(self.tick(d), [])

    def test_a_record_with_more_tests_than_listed_keeps_their_count(self):
        many = [f'test_x.Case.test_{i:03}' for i in range(MAX_CASES_LISTED + 5)]
        gh = WaitingGH({BASE: PASSED})
        gh.labels[7] = {WORKING}
        d = Dispatcher(CFG, gh, self.ws(), FakeRunner(), log=lambda m: None)
        d._wait_base(7, 'implement', BaseBroken(BASE, many), BASE)
        self.assertEqual(self.tick(d), [('host_tests', 7)])
        notice, = self.notices(gh, 7)
        self.assertEqual(notice['host_tests']['count'], MAX_CASES_LISTED + 5)
        self.assertIn('… e altri 5', gh.comments[-1][1])


if __name__ == '__main__':
    unittest.main()
