"""Re-run of a CI failure in test modules not pertinent to the PR, without a round (issue #200)."""

import dataclasses
import unittest
from types import SimpleNamespace

from devloop import config, markdown
from devloop.dispatcher import Dispatcher
from devloop.github import GitHub, GitHubError, job_id
from devloop.localtests import failed_modules
from devloop.planning import plan
from devloop.ports import summarize_checks

from helpers import ssdlc_project
from helpers.devloop_fakes import SHA1, FakeIdentities, SnapshotGH
from test_devloop import CFG, FakeRunner, FakeWS, norm, raw_review

RUN = 42
LINK = f'https://github.com/o/r/actions/runs/{RUN}/job/1'
RERUN_LINK = f'https://github.com/o/r/actions/runs/{RUN}/job/2'
# The tail of `gh run view --log-failed` for scripts/run-tests.py: job, step, timestamp, text.
PREFIX = 'Unit Tests (Python 3.12)\tRun tests\t2026-11-16T10:00:00.0000000Z '


def suite_log(*modules, problems=0):
    lines = [PREFIX + 'FAIL test_devloop_parallel (12 tests, 30s)']
    for module in modules:
        lines += [PREFIX + '=' * 70, PREFIX + f'FAILED MODULE {module}', PREFIX + '=' * 70,
                  PREFIX + 'AssertionError: flaky']
    lines += [PREFIX + '-' * 70, PREFIX + 'Ran 900 tests in 120.000s (300 modules, 4 jobs)',
              PREFIX, PREFIX + f'FAILED (modules={len(modules)}, worker problems={problems})']
    return '\n'.join(lines)


def failed(link=LINK, run_id=RUN, name='Unit Tests (Python 3.12)'):
    return {'name': name, 'bucket': 'fail', 'link': link, 'conclusion': 'failure',
            'run_id': run_id}


class CIGH(SnapshotGH):
    """PR #100 in agent:review whose CI is what ``details`` says, as `gh` would report it."""

    def __init__(self, earlier=(), labels=('agent:review',)):
        super().__init__(list(earlier))
        self.labels[100] = set(labels)
        self.details = [failed()]
        # The failed log of each job, as `gh run view --job <id> --log-failed` reads it.
        self.logs = {(RUN, 1): suite_log('test_devloop_parallel')}

    def checks(self, sha):
        return summarize_checks(self.check_details(sha))


class FailedModulesTests(unittest.TestCase):
    def test_the_modules_of_a_gh_failed_log(self):
        log = suite_log('test_devloop_parallel', 'tests.test_impact')
        self.assertEqual(failed_modules(log), ['test_devloop_parallel.py', 'test_impact.py'])

    def test_a_log_that_does_not_say_which_modules_failed(self):
        self.assertIsNone(failed_modules(''))
        self.assertIsNone(failed_modules('log of 42'))
        # No final summary: the suite did not finish (timeout, crash).
        self.assertIsNone(failed_modules(suite_log('test_x').rsplit('\n', 1)[0]))
        # A worker that stopped: its modules may have failed unlisted.
        self.assertIsNone(failed_modules(suite_log('test_x', problems=1)))
        # A module's output that prints the same text: the count no longer matches.
        self.assertIsNone(failed_modules(suite_log('test_x') + '\n' + PREFIX
                                         + 'FAILED MODULE test_y'))
        self.assertIsNone(failed_modules(suite_log('#3')))
        # unittest's own summary is not the suite's.
        self.assertIsNone(failed_modules(PREFIX + 'FAILED (failures=1)'))


class RerunTests(unittest.TestCase):
    def make(self, gh=None, ws=None, cfg=CFG, identities=None):
        self.gh = gh or CIGH()
        self.ws = ws or FakeWS()  # the PR's diff: x.py
        self.lines = []
        return Dispatcher(cfg, self.gh, self.ws, FakeRunner(), log=self.lines.append,
                          identities=identities(self.gh) if identities else None)

    def step(self, d, cfg=CFG):
        """One tick, as plan() and the actions see it: the kinds of the actions run."""
        snap = d.snapshot()
        actions = plan(snap, cfg)
        prs = {p.number: p for p in snap.prs}
        for action in actions:
            getattr(d, f'_do_{action.kind}')(prs[action.number])
        return [a.kind for a in actions]

    def reviews(self):
        return [r for _, body in self.gh.comments
                if (r := markdown.read_marker(body, 'review')) is not None]

    def reruns(self):
        return [r for _, body in self.gh.comments
                if (r := markdown.read_marker(body, 'rerun')) is not None]

    def test_unrelated_failure_is_rerun_once_without_a_round_then_a_second_one_is_a_round(self):
        d = self.make()
        self.assertEqual(self.step(d), ['ci_failed'])
        self.assertEqual(self.gh.reruns, [RUN])
        self.assertEqual(self.reviews(), [])  # no round
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        [record] = self.reruns()
        self.assertEqual(record, {'head': SHA1, 'runs': [RUN],
                                  'modules': ['test_devloop_parallel.py'], 'checks': [LINK]})
        self.assertIn('> **Bot:** devloop dispatcher', self.gh.comments[-1][1])
        self.assertIn('CI rilanciata', self.gh.comments[-1][1])
        # GitHub still shows the failed jobs the re-run replaces: devloop waits.
        self.assertEqual(self.step(d), [])
        # The re-run fails too, with new jobs: a normal CI round, which cites the re-run.
        self.gh.details = [failed(RERUN_LINK)]
        self.assertEqual(self.step(d), ['ci_failed'])
        self.assertEqual(self.gh.reruns, [RUN])  # one re-run per head
        [round_] = self.reviews()
        self.assertEqual((round_['source'], round_['round']), ('ci', 1))
        self.assertIn('rilanciati una volta', round_['note'])
        self.assertIn('test_devloop_parallel.py', round_['note'])
        self.assertEqual(self.gh.labels[100], {'agent:changes'})

    def test_a_rerun_that_passes_goes_on_to_review(self):
        d = self.make()
        self.step(d)
        # Once the re-run started the review no longer waits for it (#271): the merge does.
        self.gh.details = [dict(failed(RERUN_LINK), bucket='pending', conclusion='in_progress')]
        self.assertEqual(self.kinds(d), ['review'])
        self.gh.details = [dict(failed(RERUN_LINK), bucket='pass', conclusion='success')]
        self.assertEqual(self.kinds(d), ['review'])
        self.assertEqual(self.reviews(), [])

    def kinds(self, d):
        return [a.kind for a in plan(d.snapshot(), CFG)]

    def test_the_rerun_runs_with_the_dispatchers_app_and_only_actions_write(self):
        d = self.make(identities=FakeIdentities)
        d._wrote = lambda login, actor: True  # the fake's comments are all the operator's
        self.step(d)
        self.assertIn(('dispatcher', {'actions': 'write'}, 'rerun_failed_jobs'), self.gh.acts)

    def test_pertinent_failure_is_a_round_at_once(self):
        # ssdlc_impact.py makes test_impact*.py pertinent, as with local_tests = "changed".
        gh = CIGH()
        gh.logs[(RUN, 1)] = suite_log('test_devloop_parallel', 'test_impact_more')
        ws = FakeWS()
        ws.diff = [{'path': 'ssdlc_impact.py', 'links': []}]
        d = self.make(gh, ws)
        self.assertEqual(self.step(d), ['ci_failed'])
        self.assertFalse(hasattr(self.gh, 'reruns'))
        self.assertEqual(self.reruns(), [])
        self.assertEqual(len(self.reviews()), 1)
        self.assertEqual(self.reviews()[0]['note'], '')
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        self.assertTrue(any('pertinent to the PR: test_impact_more.py' in line
                            for line in self.lines))

    def test_a_changed_test_module_that_fails_is_pertinent(self):
        ws = FakeWS()
        ws.diff = [{'path': 'tests/test_devloop_parallel.py', 'links': []}]
        d = self.make(ws=ws)
        self.step(d)
        self.assertFalse(hasattr(self.gh, 'reruns'))
        self.assertEqual(self.gh.labels[100], {'agent:changes'})

    def test_undeterminable_failure_is_a_round_at_once(self):
        cases = {
            'no modules in the log': lambda gh: gh.logs.update({(RUN, 1): 'Error: lint failed'}),
            'a worker problem': lambda gh: gh.logs.update(
                {(RUN, 1): suite_log('test_devloop_parallel', problems=1)}),
            'a check link without a job': lambda gh: gh.details.__setitem__(
                0, failed(f'https://github.com/o/r/actions/runs/{RUN}')),
            'a commit status': lambda gh: gh.details.append(
                {'name': 'ci/other', 'bucket': 'fail', 'link': None, 'conclusion': 'failure',
                 'run_id': None}),
            'a run without the suite': lambda gh: gh.details.append(
                failed('https://github.com/o/r/actions/runs/43/job/9', 43, 'devloop tests')),
        }
        for name, setup in cases.items():
            with self.subTest(name):
                gh = CIGH()
                setup(gh)
                d = self.make(gh)
                self.assertEqual(self.step(d), ['ci_failed'])
                self.assertFalse(hasattr(gh, 'reruns'))
                self.assertEqual(len(self.reviews()), 1)
                self.assertEqual(gh.labels[100], {'agent:changes'})

    def test_another_failed_job_of_the_same_run_without_modules_is_a_round_at_once(self):
        # test.yml: the suite fails only in a module not pertinent to the PR, and in the same
        # run the A5 benchmark fails without naming any module. The run's whole log would hold
        # the suite's summary; the benchmark's own log does not.
        bench = f'https://github.com/o/r/actions/runs/{RUN}/job/3'
        gh = CIGH()
        gh.details.append(failed(bench, RUN, 'Benchmark A5'))
        gh.logs[(RUN, 3)] = PREFIX.replace('Unit Tests (Python 3.12)', 'Benchmark A5') + (
            'A5 benchmark: precision 0.81 below 0.85')
        gh.logs[RUN] = gh.logs[(RUN, 1)] + '\n' + gh.logs[(RUN, 3)]
        d = self.make(gh)
        self.assertEqual(self.step(d), ['ci_failed'])
        self.assertFalse(hasattr(gh, 'reruns'))
        self.assertEqual(self.reruns(), [])
        [round_] = self.reviews()
        self.assertEqual((round_['source'], round_['round'], round_['note']), ('ci', 1, ''))
        self.assertEqual(gh.labels[100], {'agent:changes'})
        self.assertTrue(any('failed job Benchmark A5 (run 42) does not tell' in line
                            for line in self.lines))

    def test_a_403_on_the_rerun_is_a_round_as_before_with_a_log_line(self):
        gh = CIGH()
        gh.rerun_error = GitHubError('gh api -X POST: Resource not accessible by integration '
                                     '(HTTP 403)')
        d = self.make(gh)
        self.assertEqual(self.step(d), ['ci_failed'])
        self.assertEqual(self.reruns(), [])
        [round_] = self.reviews()
        self.assertEqual(round_['note'], '')
        self.assertEqual(gh.labels[100], {'agent:changes'})
        [line] = [line for line in self.lines if 'CI rerun refused' in line]
        self.assertIn('actions: write', line)
        self.assertIn('HTTP 403', line)

    def test_the_option_off_keeps_the_round(self):
        cfg = dataclasses.replace(CFG, ci_rerun_unrelated=False)
        d = self.make(cfg=cfg)
        self.assertEqual(self.step(d, cfg), ['ci_failed'])
        self.assertFalse(hasattr(self.gh, 'reruns'))
        self.assertEqual(len(self.reviews()), 1)

    def test_without_the_format_or_the_selection_of_the_project_it_keeps_the_round(self):
        # ADR 0006, R5: nothing tells the failed modules, or their pertinence, so no re-run,
        # even with the option on and no log read.
        for tests in (dataclasses.replace(CFG.tests, ci_log_format=''),
                      dataclasses.replace(CFG.tests, selection='', source_prefixes=(),
                                          dir_modules=())):
            with self.subTest(tests=tests):
                cfg = dataclasses.replace(CFG, tests=tests)
                self.assertTrue(cfg.ci_rerun_unrelated)
                d = self.make(cfg=cfg)
                self.assertEqual(self.step(d, cfg), ['ci_failed'])
                self.assertFalse(hasattr(self.gh, 'reruns'))
                self.assertEqual(len(self.reviews()), 1)

    def test_a_rerun_marker_from_another_identity_does_not_count(self):
        body = markdown.publishable('x', 'rerun', {'head': SHA1, 'runs': [RUN], 'modules': [],
                                                   'checks': [LINK]})
        d = self.make(CIGH([{'author': {'login': 'stranger'}, 'body': body}]))
        self.assertEqual(self.step(d), ['ci_failed'])
        self.assertEqual(self.gh.reruns, [RUN])  # the first re-run of the head
        self.assertEqual(self.reviews(), [])

    def test_a_ready_pr_is_rerun_and_not_merged_meanwhile(self):
        approval = markdown.render_review(norm(raw_review()), markdown.header('R', '9.9', 'm',
                                                                             'high'))
        gh = CIGH([{'author': {'login': 'brushtyler'}, 'body': approval}],
                  labels=('agent:ready-to-merge',))
        d = self.make(gh)
        self.assertEqual(self.step(d), ['ci_failed'])
        self.assertEqual(gh.reruns, [RUN])
        self.assertEqual(gh.labels[100], {'agent:ready-to-merge'})
        self.assertEqual(self.kinds(d), [])  # waiting for the re-run


class AdapterTests(unittest.TestCase):
    def make(self, rc, stderr=''):
        self.calls = []

        def run(argv, **kw):
            self.calls.append(argv)
            return SimpleNamespace(returncode=rc, stdout='', stderr=stderr)
        return GitHub('o/r', run=run)

    def test_rerun_posts_to_the_failed_jobs_endpoint(self):
        self.make(0).rerun_failed_jobs(RUN)
        self.assertEqual(self.calls, [['gh', 'api', '-X', 'POST',
                                       f'repos/o/r/actions/runs/{RUN}/rerun-failed-jobs']])

    def test_a_403_raises(self):
        gh = self.make(1, 'gh: Resource not accessible by integration (HTTP 403)')
        with self.assertRaisesRegex(GitHubError, 'HTTP 403'):
            gh.rerun_failed_jobs(RUN)

    def test_the_whole_failed_log(self):
        gh = GitHub('o/r', run=lambda argv, **kw: SimpleNamespace(
            returncode=0, stdout='\n'.join(f'line {i}' for i in range(200)), stderr=''))
        self.assertEqual(len(gh.failed_log(RUN).splitlines()), 80)
        self.assertEqual(len(gh.failed_log(RUN, max_lines=None).splitlines()), 200)

    def test_the_failed_log_of_one_job(self):
        gh = self.make(0)
        gh.failed_log(RUN, max_lines=None, job_id=7)
        self.assertEqual(self.calls, [['gh', 'run', 'view', '--job', '7', '-R', 'o/r',
                                       '--log-failed']])
        gh.failed_log(RUN)
        self.assertEqual(self.calls[-1], ['gh', 'run', 'view', str(RUN), '-R', 'o/r',
                                          '--log-failed'])

    def test_the_job_of_a_check_link(self):
        self.assertEqual(job_id(LINK), 1)
        self.assertIsNone(job_id(f'https://github.com/o/r/actions/runs/{RUN}'))
        self.assertIsNone(job_id(None))


class ConfigTests(unittest.TestCase):
    def data(self):
        return ssdlc_project.data()

    def test_on_by_default_and_a_boolean(self):
        self.assertTrue(config.Config.ci_rerun_unrelated)
        self.assertTrue(ssdlc_project.load().ci_rerun_unrelated)
        data = self.data()
        data.pop('ci_rerun_unrelated', None)
        self.assertTrue(config.parse_config(data).ci_rerun_unrelated)
        data['ci_rerun_unrelated'] = False
        self.assertFalse(config.parse_config(data).ci_rerun_unrelated)
        data['ci_rerun_unrelated'] = 'yes'
        with self.assertRaisesRegex(config.ConfigError, 'ci_rerun_unrelated'):
            config.parse_config(data)

    def test_off_without_the_format_of_the_ci_logs_or_the_selection(self):
        # ADR 0006, R5: the project declares both; without either the option is off, whatever
        # the installation or the overlay say.
        data = self.data()
        data['ci_rerun_unrelated'] = True
        self.assertEqual(config.parse_config(data).tests.ci_log_format, 'run-tests')
        self.assertTrue(config.parse_config(data).ci_rerun_unrelated)
        no_format = self.data()
        del no_format['tests']['ci_log_format']
        no_selection = self.data()
        no_selection['tests'] = {'ci_log_format': 'run-tests'}
        for name, table in (('format', no_format), ('selection', no_selection),
                            ('tests', {k: v for k, v in data.items() if k != 'tests'})):
            with self.subTest(without=name):
                self.assertFalse(config.parse_config(table).ci_rerun_unrelated)


if __name__ == '__main__':
    unittest.main()
