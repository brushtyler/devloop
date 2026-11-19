"""Tests red on the base too, and the alert of a red CI on the base branch (issue #459)."""

import dataclasses
import shlex
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from devloop import markdown
from devloop.dispatch_base import MAX_CASES_LISTED, MainAlerts
from devloop.dispatcher import Dispatcher
from devloop.github import GitHubError
from devloop.localtests import Tests, case_modules, failed_cases, module_command
from devloop.planning import BASE_REPAIRED, Snapshot, plan
from devloop.workspace import Workspace

from helpers.devloop_fakes import FakeGH
from test_devloop import (BASE, CFG, FakeRunner, FakeWS, finding, fixed, implemented, norm, pr,
                          raw_review)

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
                              errors=[('test_y', 'unittest.loader._FailedTest.test_y')])
        cases = failed_cases(out)
        self.assertEqual(cases, ['test_x.Case.test_a', 'test_x.Case.test_s (i=1)',
                                 'unittest.loader._FailedTest.test_y'])
        self.assertEqual(case_modules(cases), ['test_x.py', 'test_y.py'])

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
            command = module_command(['test_broken.py']).replace('python3', python)
            self.assertFalse(ws.run_tests(tmp, command, 60)[0])
            cases = ws.failed_cases(tmp)
            self.assertEqual(case_modules(cases), ['test_broken.py'])
            self.assertTrue(ws.run_tests(tmp, 'true', 60)[0])
            self.assertEqual(ws.failed_cases(tmp), [])


BRANCH = ['test_x.Case.test_a', 'test_x.Case.test_b']


class ImplementTests(unittest.TestCase):
    def make(self, runner, ws, cfg=CFG):
        self.gh, self.ws, self.logs = FakeGH(), ws, []
        return Dispatcher(cfg, self.gh, ws, runner, log=self.logs.append)

    def implement(self, branch, base, tests=(False,), cfg=CFG):
        """``_do_implement`` of issue 7 whose local tests fail with ``branch`` (the cases of the
        worktree), and with ``base`` on the base commit."""
        runner = FakeRunner(implemented(), implemented())
        ws = FakeWS(files=['ssdlc_impact.py'], modules=['test_impact.py'], tests=tests)
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
        # The failed modules ran on the base commit, one at a time, in their own copy.
        self.assertEqual(self.ws.commands[1:], [(Path('/wt/issue-7-base'),
                                                 module_command(['test_x.py']))])
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
        ws = FakeWS(files=['ssdlc_impact.py'], modules=['test_impact.py'], tests=(False,))
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
            return [{'number': 7, 'title': 'Issue 7', 'labels': [{'name': label}],
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
        gh, ws = FakeGH(), FakeWS(files=['ssdlc_impact.py'], modules=['test_impact.py'],
                                  tests=(False,))
        ws.cases = {'issue-7': BRANCH, 'issue-7-base': BRANCH}
        Dispatcher(CFG, gh, ws, FakeRunner(implemented()), log=lambda m: None)._do_implement(7)
        return gh.comments[-1]

    def test_the_issue_waits_while_the_base_stays(self):
        snap, logs = self.snapshot(BASE, [self.broken_comment()])
        self.assertEqual(snap.base_waits, {7: True})
        self.assertEqual([(a.kind, a.number) for a in plan(snap, CFG)], [])
        self.assertTrue(any('waits for main' in line for line in logs))

    def test_the_issue_starts_again_once_the_base_advances(self):
        snap, _ = self.snapshot('e' * 40, [self.broken_comment()])
        self.assertEqual(snap.base_waits, {7: False})
        actions = plan(snap, CFG)
        self.assertEqual([(a.kind, a.number, a.reason) for a in actions],
                         [('implement', 7, BASE_REPAIRED)])
        # Not while it still runs, nor without a free slot.
        self.assertEqual(plan(snap, CFG, busy=frozenset({7})), [])
        self.assertEqual(plan(snap, dataclasses.replace(CFG, max_open_prs=0)), [])

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
        gh, ws = FakeGH(), FakeWS(files=['ssdlc_impact.py'], modules=['test_impact.py'])
        gh.comments = [self.broken_comment()]
        gh.labels[7] = {'agent:working'}
        Dispatcher(CFG, gh, ws, runner, log=lambda m: None)._do_implement(7)
        self.assertEqual(len(gh.created), 1)
        self.assertEqual(gh.labels[7], set())


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


if __name__ == '__main__':
    unittest.main()
