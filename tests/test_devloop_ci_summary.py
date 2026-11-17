"""devloop: the summary of the head's CI in the review and fix prompts (#300).

The agents have no network and cannot run the whole suite in their turn: the dispatcher reads
the CI of the head (each job with its outcome and duration, and the final lines of
``scripts/run-tests.py`` from the job log) and puts it in the review and fix prompts, and in the
``ci_round`` event of the ledger. A log it cannot read leaves the summary out, nothing more.
"""

import unittest
from types import SimpleNamespace

from devloop.context import format_ci_summary
from devloop.dispatcher import Dispatcher
from devloop.github import GitHub, GitHubError, normalize_checks, run_seconds
from devloop.ledger import Ledger
from devloop.localtests import SUITE_LINE_LIMIT, suite_summary
from helpers.devloop_fakes import FakeGH
from test_devloop import (CFG, SHA1, FakeRunner, FakeWS, finding, fixed, norm, pr,
                          raw_review)
from test_devloop_ledger import SUITE_LOG, SUITE_SUMMARY, SnapLedgerGH

NO_STATUS = {'state': 'pending', 'total_count': 0, 'statuses': []}
# The checks of a head: the full suite, a quick job without scripts/run-tests.py, a job still
# running and an external commit status.
RUNS = [
    {'name': 'Unit Tests (Python 3.12)', 'status': 'completed', 'conclusion': 'success',
     'started_at': '2026-11-17T10:00:00Z', 'completed_at': '2026-11-17T10:06:12Z',
     'details_url': 'https://github.com/o/r/actions/runs/43/job/8'},
    {'name': 'devloop tests', 'status': 'completed', 'conclusion': 'success',
     'started_at': '2026-11-17T10:00:00Z', 'completed_at': '2026-11-17T10:00:45Z',
     'details_url': 'https://github.com/o/r/actions/runs/44/job/9'},
    {'name': 'Benchmark', 'status': 'in_progress', 'conclusion': None,
     'started_at': '2026-11-17T10:00:00Z', 'completed_at': None,
     'details_url': 'https://github.com/o/r/actions/runs/43/job/10'}]
DETAILS = normalize_checks(RUNS, {'state': 'success', 'total_count': 1, 'statuses': [
    {'context': 'ext', 'state': 'success', 'target_url': 'https://ci.example'}]})
QUICK_LOG = ('devloop tests\tRun\t2026-11-17T10:00:44Z Ran 812 tests in 30.512s\n'
             'devloop tests\tRun\t2026-11-17T10:00:44Z OK\n')


class SuiteSummaryTests(unittest.TestCase):
    def test_the_final_lines_of_run_tests_are_read_from_a_job_log(self):
        self.assertEqual(suite_summary(SUITE_LOG), SUITE_SUMMARY)
        # Without the gh prefix of each line too.
        plain = '\n'.join(line.split(' ', 1)[1] for line in SUITE_LOG.splitlines())
        self.assertEqual(suite_summary(plain), SUITE_SUMMARY)

    def test_a_log_without_the_final_line_of_run_tests_has_no_summary(self):
        # unittest's own line has no modules nor jobs: that job does not run run-tests.py.
        self.assertIsNone(suite_summary(QUICK_LOG))
        self.assertIsNone(suite_summary(''))
        self.assertIsNone(suite_summary(None))
        # A suite cut short before its end.
        self.assertIsNone(suite_summary('Slowest: test_a 120s\n'))

    def test_the_last_run_counts_and_a_missing_slowest_line_is_none(self):
        log = ('Ran 3 tests in 1.000s (2 modules, 0 split by class, 1 jobs)\n'
               'Ran 9 tests in 2.500s (4 modules, 1 split by class, 2 jobs)\n')
        self.assertEqual(suite_summary(log), {
            'ran': 'Ran 9 tests in 2.500s (4 modules, 1 split by class, 2 jobs)',
            'slowest': None})
        long = 'Slowest: ' + ', '.join(f'test_{i} 1s' for i in range(200)) + '\n'
        found = suite_summary(long + log)
        self.assertEqual(len(found['slowest']), SUITE_LINE_LIMIT)


class DurationTests(unittest.TestCase):
    def test_a_completed_check_run_has_its_duration_and_nothing_else_has_one(self):
        self.assertEqual([(c['name'], c['duration_s']) for c in DETAILS],
                         [('Unit Tests (Python 3.12)', 372), ('devloop tests', 45),
                          ('Benchmark', None), ('ext', None)])
        self.assertIsNone(run_seconds({'status': 'completed', 'started_at': 'x',
                                       'completed_at': '2026-11-17T10:00:45Z'}))
        self.assertIsNone(run_seconds({'status': 'completed', 'started_at': None,
                                       'completed_at': None}))

    def test_check_details_read_when_each_run_completed(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        GitHub('o/r', run=fake_run).check_details(SHA1)
        self.assertIn('completed_at', ' '.join(calls[0]))


class JobLogAdapterTests(unittest.TestCase):
    def test_the_whole_log_of_a_job_or_none_when_gh_cannot_read_it(self):
        calls, answers = [], [SimpleNamespace(returncode=0, stdout=SUITE_LOG, stderr=''),
                              SimpleNamespace(returncode=1, stdout='', stderr='log expired')]

        def fake_run(argv, **kw):
            calls.append(argv)
            return answers.pop(0)
        gh = GitHub('o/r', run=fake_run)
        self.assertEqual(gh.job_log(8), SUITE_LOG)
        self.assertEqual(calls[0], ['gh', 'run', 'view', '--job', '8', '-R', 'o/r', '--log'])
        self.assertIsNone(gh.job_log(8))


class FormatTests(unittest.TestCase):
    def jobs(self):
        return [
            {'name': 'Unit Tests (Python 3.12)', 'outcome': 'success', 'bucket': 'pass',
             'duration_s': 372, 'tests': dict(SUITE_SUMMARY)},
            {'name': 'devloop tests', 'outcome': 'success', 'bucket': 'pass',
             'duration_s': 45, 'tests': None},
            {'name': 'Lint `x`', 'outcome': 'failure', 'bucket': 'fail', 'duration_s': 12,
             'log': 'unavailable'},
            {'name': 'Benchmark', 'outcome': 'in_progress', 'bucket': 'pending',
             'duration_s': None},
            {'name': 'ext', 'outcome': 'success', 'bucket': 'pass', 'duration_s': None}]

    def test_each_job_with_its_outcome_duration_and_suite_summary(self):
        text = format_ci_summary(SHA1, 'pending', self.jobs())
        self.assertTrue(text.startswith('## CI of the head\n\n'))
        self.assertIn(f'`{SHA1[:12]}`', text)
        self.assertIn('CI state: **pending**', text)
        self.assertIn('- `Unit Tests (Python 3.12)`: success, 372s (6m 12s)\n'
                      f'  - `{SUITE_SUMMARY["ran"]}`\n'
                      f'  - `{SUITE_SUMMARY["slowest"]}`', text)
        self.assertIn('- `devloop tests`: success, 45s\n- `Lint', text)
        # A name cannot break out of its inline code.
        self.assertIn("- `Lint 'x'`: failure, 12s\n"
                      '  - the job log could not be read: its test summary is not available',
                      text)
        self.assertIn('- `Benchmark`: in_progress, still running', text)
        self.assertIn('- `ext`: success, duration not known', text)

    def test_no_checks_and_checks_not_readable(self):
        self.assertIn('GitHub reports no CI check', format_ci_summary(SHA1, 'none', []))
        self.assertIn('is not available: its checks could not be read',
                      format_ci_summary(SHA1, 'pass', None))


class SummaryGH(SnapLedgerGH):
    """The snapshot of PR #100 with the checks ``DETAILS``; the full suite's job log holds the
    summary of scripts/run-tests.py, the quick job's only unittest's own line."""

    def __init__(self):
        super().__init__()
        self.details = [dict(c) for c in DETAILS]
        self.job_logs = {8: SUITE_LOG, 9: QUICK_LOG}


class PromptTests(unittest.TestCase):
    def make(self, runner, gh=None, ledger=True):
        self.gh = gh or SummaryGH()
        self.gh.labels[100] = {'agent:review'}
        self.logs = []
        return Dispatcher(CFG, self.gh, FakeWS(), runner, log=self.logs.append,
                          ledger=Ledger(self.gh) if ledger else None)

    def ci_events(self):
        return [r for r in self.gh.records(100, 'event') if r['event'] == 'ci_round']

    def assert_summary(self, prompt):
        self.assertIn('## CI of the head', prompt)
        self.assertIn('- `Unit Tests (Python 3.12)`: success, 372s (6m 12s)', prompt)
        self.assertIn(f'  - `{SUITE_SUMMARY["ran"]}`', prompt)
        self.assertIn(f'  - `{SUITE_SUMMARY["slowest"]}`', prompt)
        self.assertIn('- `devloop tests`: success, 45s\n', prompt)
        self.assertIn('- `Benchmark`: in_progress, still running', prompt)
        self.assertNotIn('$ci_summary', prompt)

    def test_the_review_prompt_summarizes_the_ci_of_the_head(self):
        runner = FakeRunner(raw_review())
        d = self.make(runner)
        d._do_review(d.snapshot().prs[0])
        (call,) = runner.calls
        self.assertEqual(call[2], 'review')
        self.assert_summary(call[3])
        # The log of a job still running is not read.
        self.assertEqual(sorted(self.gh.job_log_reads), [8, 9])

    def test_the_round_records_the_durations_and_the_suite_summary(self):
        self.gh = SummaryGH()
        self.gh.details = [dict(c) for c in DETAILS if c['bucket'] != 'pending']
        d = self.make(FakeRunner(raw_review()), self.gh)
        d._do_review(d.snapshot().prs[0])
        (event,) = self.ci_events()
        self.assertEqual([(c['name'], c['duration_s']) for c in event['checks']],
                         [('Unit Tests (Python 3.12)', 372), ('devloop tests', 45),
                          ('ext', None)])
        self.assertEqual(event['tests'], [{'name': 'Unit Tests (Python 3.12)', **SUITE_SUMMARY}])

    def test_the_review_prompt_summarizes_the_ci_without_a_ledger_too(self):
        runner = FakeRunner(raw_review())
        d = self.make(runner, ledger=False)
        d._do_review(d.snapshot().prs[0])
        self.assert_summary(runner.calls[0][3])

    def test_the_fix_prompt_summarizes_the_ci_of_the_reviewed_head(self):
        first = norm(raw_review('request_changes', [finding()]))
        runner = FakeRunner(fixed())
        gh = SummaryGH()
        d = Dispatcher(CFG, gh, FakeWS(), runner, log=lambda m: None)
        d._do_fix(pr(labels=['agent:changes'], reviews=[first]))
        (call,) = runner.calls
        self.assertEqual(call[2], 'fix')
        self.assert_summary(call[3])
        self.assertIn('take it from the CI summary above', call[3])

    def test_the_prompt_of_a_requested_fix_summarizes_the_ci_too(self):
        runner = FakeRunner(fixed())
        gh = SummaryGH()
        d = Dispatcher(CFG, gh, FakeWS(), runner, log=lambda m: None)
        p = pr(labels=['agent:review'])
        p.fix_requests = [{'comment': 1, 'by': 'brushtyler', 'request': 'Measure the suite.'}]
        d._do_requested_fix(p)
        (call,) = runner.calls
        self.assert_summary(call[3])
        self.assertIn('Measure the suite.', call[3])


class UnreadableLogTests(unittest.TestCase):
    """A log that cannot be read does not stop the review: the summary says it is missing."""

    def run_review(self, gh):
        gh.labels[100] = {'agent:review'}
        runner = FakeRunner(raw_review())
        logs = []
        d = Dispatcher(CFG, gh, FakeWS(), runner, log=logs.append, ledger=Ledger(gh))
        d._do_review(d.snapshot().prs[0])
        self.assertEqual(gh.labels[100], {'agent:ready-to-merge'})
        return runner.calls[0][3], logs

    def test_a_log_gh_cannot_read_is_reported_as_not_available(self):
        gh = SnapLedgerGH()
        gh.job_logs = {8: None}
        prompt, _ = self.run_review(gh)
        self.assertIn('- `Unit Tests`: success, 372s (6m 12s)\n'
                      '  - the job log could not be read: its test summary is not available',
                      prompt)
        (event,) = [r for r in gh.records(100, 'event') if r['event'] == 'ci_round']
        self.assertEqual(event['tests'], [{'name': 'Unit Tests', 'log': 'unavailable'}])

    def test_a_failed_read_of_the_log_is_reported_as_not_available(self):
        class RefusingGH(SnapLedgerGH):
            def job_log(self, job_id):
                raise GitHubError('gh run view: HTTP 410')

        prompt, logs = self.run_review(RefusingGH())
        self.assertIn('the job log could not be read: its test summary is not available',
                      prompt)
        self.assertTrue(any('HTTP 410' in line for line in logs))

    def test_checks_that_cannot_be_read_leave_the_summary_out(self):
        class NoChecksGH(FakeGH):
            def check_details(self, sha):
                raise GitHubError('gh api: HTTP 502')

        gh = NoChecksGH()
        gh.labels[100] = {'agent:review'}
        runner = FakeRunner(raw_review())
        Dispatcher(CFG, gh, FakeWS(), runner, log=lambda m: None)._do_review(pr())
        self.assertIn('The summary of the CI of', runner.calls[0][3])
        self.assertIn('is not available: its checks could not be read', runner.calls[0][3])
        self.assertEqual(gh.labels[100], {'agent:ready-to-merge'})


if __name__ == '__main__':
    unittest.main()
