"""After a push the review does not run the full suite locally in place of the CI (#263), and
starts while the CI runs: the merge waits for the CI of the same head (#271)."""

import dataclasses
import json
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace

from devloop import config, markdown
from devloop.dispatcher import Dispatcher
from devloop.github import GitHub, GitHubError
from devloop.planning import Action, plan
from devloop.unavailability import iso

from helpers.devloop_fakes import SHA1, SnapshotGH
from test_devloop import CFG, FakeRunner, FakeWS, finding, fixed, norm, pr, raw_review

NOW = 1_795_000_000.0  # the controlled clock: 2026-11-18, any fixed moment will do
SUITE = (Path('/wt/pr-100-review'), CFG.test_cmd)  # the full suite before a review
WAITING = f'waiting for CI on {SHA1[:12]}'


class CIWaitTests(unittest.TestCase):
    """PR #100 in agent:review, with local_tests = "changed" as in this repository."""

    def make(self, committed=NOW - 30, cfg=CFG, ci='none'):
        self.gh = SnapshotGH([])
        self.gh.labels[100] = {'agent:review'}
        self.gh.ci = ci
        if committed is not None:
            self.gh.committed = iso(committed)
        self.ws, self.runner, self.lines = FakeWS(), FakeRunner(raw_review()), []
        self.now = NOW
        self.cfg = cfg
        return Dispatcher(cfg, self.gh, self.ws, self.runner, log=self.lines.append,
                          clock=lambda: self.now)

    def step(self, d):
        """One tick, as plan() and the actions see it: the kinds of the actions run."""
        snap = d.snapshot()
        actions = plan(snap, self.cfg)
        prs = {p.number: p for p in snap.prs}
        for action in actions:
            getattr(d, f'_do_{action.kind}')(prs[action.number])
        return [a.kind for a in actions]

    def waits(self):
        return [line for line in self.lines if WAITING in line]

    def notices(self):
        return [m for _, body in self.gh.comments
                if (m := markdown.read_marker(body, 'notice')) is not None]

    def prompt(self):
        """The prompt of the latest review."""
        return self.runner.calls[-1][3]

    def test_a_head_pushed_30_seconds_ago_is_reviewed_at_once_and_ready_when_the_ci_passes(self):
        # Within the window the review starts as if the CI were pending (#271), without the
        # full suite locally; the approval waits for the CI of the same head.
        self.assertEqual(CFG.local_tests, 'changed')
        d = self.make(committed=NOW - 30)
        self.assertEqual(self.step(d), ['review'])
        self.assertEqual((len(self.runner.calls), self.ws.commands), (1, []))
        self.assertIn('CI status for this commit: **pending**', self.prompt())
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.assertEqual(self.waits(), [f'  PR #100: {WAITING}: pushed 30 s ago, within '
                                        '[ci] grace_minutes = 5'])
        self.assertIn('  not promoted: no CI on ' + SHA1[:12]
                      + ' yet, and the approval did not see it', self.lines)
        # Still within the window a minute later, then the CI registers and runs.
        self.now += 60
        self.assertEqual(self.step(d), [])
        self.assertIn('pushed 90 s ago', self.waits()[-1])
        self.gh.ci = 'pending'
        self.assertEqual(self.step(d), [])
        self.gh.ci = 'pass'
        self.assertEqual(self.step(d), ['apply_review'])
        self.assertEqual(len(self.runner.calls), 1)  # no second review
        self.assertEqual(self.ws.commands, [])  # the CI passed: no local suite
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})

    def test_a_head_without_ci_after_the_window_gets_the_full_suite_as_before(self):
        d = self.make(committed=NOW - 6 * 60)
        self.assertEqual(self.step(d), ['review'])
        self.assertEqual(self.ws.commands, [SUITE])
        self.assertEqual(len(self.runner.calls), 1)
        self.assertIn('CI status for this commit: **none**', self.prompt())
        self.assertEqual(self.waits(), [])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})

    def test_the_window_ends_on_the_controlled_clock(self):
        # Reviewed within the window; once it ends without CI the full suite runs locally
        # before the promotion, not before a second review.
        d = self.make(committed=NOW - 30)
        self.assertEqual(self.step(d), ['review'])
        self.now += 269  # 299 s after the push
        self.assertEqual(self.step(d), [])
        self.assertEqual(self.ws.commands, [])
        self.now += 1    # 300 s: the window is over and no CI appeared
        self.assertEqual(self.step(d), ['apply_review'])
        self.assertEqual(self.ws.commands, [SUITE])
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})

    def test_a_full_suite_that_fails_after_a_blind_approval_escalates(self):
        d = self.make(committed=NOW - 30)
        self.ws.tests = [False]
        self.assertEqual(self.step(d), ['review'])
        self.now += 5 * 60
        self.assertEqual(self.step(d), ['apply_review'])
        self.assertEqual(self.ws.commands, [SUITE])
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        self.assertNotIn((100, 'agent:ready-to-merge'), getattr(self.gh, 'label_log', {}))
        body = self.gh.comments[-1][1]
        self.assertIn('prima della promozione, fallisce', body)
        self.assertEqual(self.notices()[-1]['class'], 'tests_failed')

    def test_the_push_the_dispatcher_recorded_counts_over_the_commit_date(self):
        # A fix that pushed only an older merge commit: the commit date is old, the push new.
        d = self.make(committed=NOW - 3600)
        self.now = NOW - 30
        d._pushed(Path('/wt/pr-100'))
        self.now = NOW
        self.assertEqual(self.step(d), ['review'])
        self.assertIn('pushed 30 s ago', self.waits()[-1])
        self.assertEqual(self.ws.commands, [])
        self.assertEqual(self.gh.labels[100], {'agent:review'})

    def test_a_future_or_unreadable_commit_date_counts_from_the_first_look(self):
        for name in ('future', 'unreadable'):
            with self.subTest(name):
                d = self.make(committed=NOW + 86400)
                if name == 'unreadable':
                    def fail(sha):
                        raise GitHubError('gh api repos/o/r/commits: HTTP 502')
                    self.gh.commit_date = fail
                self.assertEqual(self.step(d), ['review'])
                self.assertIn('pushed 0 s ago', self.waits()[-1])
                self.assertEqual(self.ws.commands, [])
                self.now += 5 * 60
                self.assertEqual(self.step(d), ['apply_review'])
                self.assertEqual(self.ws.commands, [SUITE])

    def test_full_local_tests_never_wait(self):
        cfg = dataclasses.replace(CFG, local_tests='full')
        d = self.make(committed=NOW - 30, cfg=cfg)
        self.assertEqual(self.step(d), ['review'])
        self.assertEqual((self.ws.commands, self.waits()), ([], []))

    def test_a_grace_of_zero_turns_the_window_off(self):
        cfg = dataclasses.replace(CFG, ci=config.CIPolicy(grace_minutes=0))
        d = self.make(committed=NOW, cfg=cfg)
        self.assertEqual(self.step(d), ['review'])
        self.assertEqual(self.ws.commands, [SUITE])

    def test_required_checks_are_waited_for_then_escalated_to_the_operator(self):
        d = self.make(committed=NOW - 6 * 60)
        self.gh.required = ['Unit Tests (Python 3.12)']
        # The review runs while the required checks are awaited (#271); the merge waits.
        self.assertEqual(self.step(d), ['review'])
        self.assertEqual(self.waits()[-1],
                         f'  PR #100: {WAITING}: main requires Unit Tests (Python 3.12), not '
                         'reported 360 s after the push ([ci] required_timeout_minutes = 30)')
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.now += 24 * 60 - 1  # 29:59 after the push
        self.assertEqual(self.step(d), [])
        self.now += 1
        self.assertEqual(self.step(d), ['ci_missing'])
        self.assertEqual((len(self.runner.calls), self.ws.commands), (1, []))
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        body = self.gh.comments[-1][1]
        self.assertIn('30 minuti dopo il push', body)
        self.assertIn('`Unit Tests (Python 3.12)`', body)
        # Not a class of the catalogue: critical, to the operator, never to the supervisor.
        self.assertEqual(self.notices(), [{'event': 'escalation', 'class': 'ci_missing',
                                           'critical': True, 'to': 'operator'}])

    def test_the_required_timeout_prevails_over_a_longer_grace(self):
        cfg = dataclasses.replace(CFG, ci=config.CIPolicy(grace_minutes=60,
                                                          required_timeout_minutes=30))
        d = self.make(committed=NOW - 29 * 60, cfg=cfg)
        self.gh.required = ['Unit Tests (Python 3.12)']
        self.assertEqual(self.step(d), ['review'])
        self.assertIn('within [ci] grace_minutes = 60', self.waits()[-1])
        self.now += 2 * 60  # 31 minutes after the push, still within the grace
        self.assertEqual(self.step(d), ['ci_missing'])
        self.assertEqual((len(self.runner.calls), self.ws.commands), (1, []))
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        self.assertEqual(self.notices()[-1]['class'], 'ci_missing')
        # Without required checks the same head still waits for the whole grace.
        d = self.make(committed=NOW - 31 * 60, cfg=cfg)
        self.assertEqual(self.step(d), ['review'])
        self.assertIn('pushed 1860 s ago, within [ci] grace_minutes = 60', self.waits()[-1])
        self.now += 28 * 60
        self.assertEqual(self.step(d), [])
        self.now += 60  # 60 minutes after the push: the full suite, as before
        self.assertEqual(self.step(d), ['apply_review'])
        self.assertEqual(self.ws.commands, [SUITE])

    def test_required_checks_that_appear_are_reviewed_without_local_suite(self):
        d = self.make(committed=NOW - 6 * 60)
        self.gh.required = ['Unit Tests (Python 3.12)']
        self.assertEqual(self.step(d), ['review'])
        self.gh.ci = 'pass'
        self.assertEqual(self.step(d), ['apply_review'])
        self.assertEqual(self.ws.commands, [])
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})

    def test_ci_that_appears_before_the_escalation_runs_stops_it(self):
        d = self.make(committed=NOW - 31 * 60)
        self.gh.required = ['Unit Tests (Python 3.12)']
        [p] = d.snapshot().prs
        self.assertEqual(p.ci_missing, ['Unit Tests (Python 3.12)'])
        self.gh.ci = 'pending'
        d._do_ci_missing(p)
        self.assertEqual((self.gh.comments, self.gh.labels[100]), ([], {'agent:review'}))

    def test_unreadable_required_checks_count_as_none(self):
        d = self.make(committed=NOW - 6 * 60)

        def fail(branch):
            raise GitHubError('gh api repos/o/r/rules: HTTP 403')
        self.gh.required_checks = fail
        self.assertEqual(self.step(d), ['review'])
        self.assertEqual(self.ws.commands, [SUITE])
        self.assertTrue(any('the checks main requires not read' in line for line in self.lines))


class ReviewDuringCITests(unittest.TestCase):
    """The review starts with the CI of the head running; the merge waits for it (#271)."""

    make, step, prompt = CIWaitTests.make, CIWaitTests.step, CIWaitTests.prompt

    def test_with_ci_pending_the_review_runs_and_the_approval_waits_for_the_ci(self):
        d = self.make(ci='pending')
        self.assertEqual(self.step(d), ['review'])
        self.assertEqual((len(self.runner.calls), self.ws.commands), (1, []))
        self.assertIn('CI status for this commit: **pending**', self.prompt())
        self.assertIn('With `pending`\nthe outcome of the tests is not known yet', self.prompt())
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.assertIn('  not promoted: CI is pending', self.lines)
        [record] = [m for _, body in self.gh.comments
                    if (m := markdown.read_marker(body, 'review')) is not None]
        self.assertEqual((record['verdict'], record['ci']), ('approve', 'pending'))
        # While the CI runs, nothing more: no second review, no local suite.
        self.assertEqual(self.step(d), [])
        self.gh.ci = 'pass'
        self.assertEqual(self.step(d), ['apply_review'])
        self.assertEqual(len(self.runner.calls), 1)  # promoted without a second review
        self.assertEqual(self.ws.commands, [])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})

    def test_an_approval_whose_ci_then_fails_goes_to_the_fix_without_merge(self):
        cfg = dataclasses.replace(CFG, merge=config.MergePolicy(mode='strict'))
        d = self.make(ci='pending', cfg=cfg)
        self.assertEqual(self.step(d), ['review'])
        self.gh.ci = 'fail'
        self.assertEqual(self.step(d), ['ci_failed'])
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        self.assertNotIn((100, 'agent:ready-to-merge'), getattr(self.gh, 'label_log', {}))
        self.assertEqual(getattr(self.gh, 'merged', []), [])
        self.assertEqual((len(self.runner.calls), self.ws.commands), (1, []))
        records = [m for _, body in self.gh.comments
                   if (m := markdown.read_marker(body, 'review')) is not None]
        # The CI failure is a round of its own after the review's, as today.
        self.assertEqual([(r['source'], r['round'], r['verdict']) for r in records],
                         [('agent', 1, 'approve'), ('ci', 2, 'request_changes')])
        snap = d.snapshot()
        self.assertEqual([a.kind for a in plan(snap, cfg)], ['fix'])

    def changes(self, ci='pending', committed=NOW - 6 * 60):
        """PR #100 after a review that asked for changes before its CI was known."""
        d = self.make(ci=ci, committed=committed)
        self.runner.outputs = [raw_review('request_changes', [finding('reviewer bug')]),
                               fixed()]
        self.assertEqual(self.step(d), ['review'])
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        return d

    def test_changes_asked_with_ci_pending_wait_and_a_failure_joins_the_same_fix(self):
        d = self.changes()
        # While the CI of the reviewed head runs, the fix does not start.
        self.assertEqual(self.step(d), [])
        self.assertEqual(len(self.runner.calls), 1)
        self.gh.ci = 'fail'
        self.assertEqual(self.step(d), ['ci_failed'])
        records = [m for _, body in self.gh.comments
                   if (m := markdown.read_marker(body, 'review')) is not None]
        self.assertEqual([(r['source'], r['round'], r['verdict']) for r in records],
                         [('agent', 1, 'request_changes'), ('ci', 2, 'request_changes')])
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        # One fix, with the reviewer's finding and the CI failure.
        snap = d.snapshot()
        self.assertEqual([a.kind for a in plan(snap, self.cfg)], ['fix'])
        d._do_fix(snap.prs[0])
        self.assertEqual(len(self.runner.calls), 2)
        prompt = self.prompt()
        self.assertIn('reviewer bug', prompt)
        self.assertIn('CI fallita', prompt)

    def test_changes_asked_with_ci_pending_go_to_the_fix_once_the_ci_passes(self):
        d = self.changes()
        self.gh.ci = 'pass'
        self.assertEqual(self.step(d), ['fix'])
        self.assertEqual(len(self.runner.calls), 2)
        self.assertNotIn('CI fallita', self.prompt())

    def test_changes_asked_within_the_window_wait_for_it(self):
        d = self.changes(ci='none', committed=NOW - 30)
        self.assertEqual(self.step(d), [])
        self.assertEqual(self.ws.commands, [])
        self.gh.ci = 'fail'
        self.assertEqual(self.step(d), ['ci_failed'])
        # Past the window without any CI, the fix starts: no local suite in its place.
        d = self.changes(ci='none', committed=NOW - 30)
        self.now += 5 * 60
        self.assertEqual(self.step(d), ['fix'])
        self.assertEqual(self.ws.commands.count(SUITE), 0)

    def test_a_ci_failed_before_the_review_takes_the_path_of_today(self):
        d = self.make(ci='fail')
        self.assertEqual(self.step(d), ['ci_failed'])
        self.assertEqual((self.runner.calls, self.ws.commands), ([], []))
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        records = [m for _, body in self.gh.comments
                   if (m := markdown.read_marker(body, 'review')) is not None]
        self.assertEqual([(r['source'], r['round']) for r in records], [('ci', 1)])

    def test_no_local_full_suite_while_the_ci_runs_or_within_the_window(self):
        for ci, committed in (('pending', NOW - 6 * 60), ('none', NOW - 30)):
            with self.subTest(ci=ci):
                d = self.make(ci=ci, committed=committed)
                self.assertEqual(self.step(d), ['review'])
                self.assertEqual(self.step(d), [])
                self.assertEqual(self.ws.commands, [])
                self.assertEqual(len(self.runner.calls), 1)
                self.assertEqual(self.gh.labels[100], {'agent:review'})


class PlanTests(unittest.TestCase):
    def test_a_waiting_head_is_reviewed_and_a_missing_ci_escalates(self):
        waiting = dataclasses.replace(pr(checks='none'), ci_wait=f'{WAITING}: pushed 30 s ago')
        self.assertEqual(plan(SimpleNamespace(prs=[waiting], ready_issues=[], working_issues=[],
                                              supervised=[], conclaves=[]), CFG),
                         [Action('review', 100, 'CI pending: the merge waits for it')])
        # A recorded approval of the head waits for its CI (#271), as with CI pending.
        approval = norm(raw_review())
        for checks, wait in (('none', 'waiting'), ('pending', '')):
            approved = dataclasses.replace(pr(checks=checks, reviews=[approval]), ci_wait=wait)
            self.assertEqual(plan(SimpleNamespace(prs=[approved], ready_issues=[],
                                                  working_issues=[], supervised=[],
                                                  conclaves=[]), CFG), [])
        # Changes asked before the CI was known (#271): the fix waits for the CI of the head,
        # also for a re-run of #200; a failure is a CI round, then the same fix.
        seen = norm(raw_review('request_changes', [finding()]))
        blind = dict(seen, ci='pending')
        cases = (('pending', '', False, []), ('none', 'waiting', False, []),
                 ('fail', '', True, []), ('fail', '', False, ['ci_failed']),
                 ('pass', '', False, ['fix']), ('none', '', False, ['fix']))
        for checks, wait, rerun, kinds in cases:
            with self.subTest(checks=checks, wait=wait, rerun=rerun):
                waiting = dataclasses.replace(pr(checks=checks, reviews=[blind],
                                                 labels={'agent:changes'}),
                                              ci_wait=wait, rerun_waiting=rerun)
                self.assertEqual([a.kind for a in plan(SimpleNamespace(
                    prs=[waiting], ready_issues=[], working_issues=[], supervised=[],
                    conclaves=[]), CFG)], kinds)
        # A review that saw its CI goes to the fix at once, as before.
        known = pr(checks='pending', reviews=[seen], labels={'agent:changes'})
        self.assertEqual([a.kind for a in plan(SimpleNamespace(
            prs=[known], ready_issues=[], working_issues=[], supervised=[], conclaves=[]),
            CFG)], ['fix'])
        missing = dataclasses.replace(pr(checks='none'), ci_missing=['unittest'])
        self.assertEqual(plan(SimpleNamespace(prs=[missing], ready_issues=[], working_issues=[],
                                              supervised=[], conclaves=[]), CFG),
                         [Action('ci_missing', 100, 'required checks missing: unittest')])


class ConfigTests(unittest.TestCase):
    def data(self):
        with open(config.DEFAULT_CONFIG, 'rb') as fh:
            return tomllib.load(fh)

    def test_defaults_and_values(self):
        self.assertEqual(config.CIPolicy(), config.CIPolicy(5, 30))
        self.assertEqual(config.load_config().ci, config.CIPolicy(5, 30))
        data = self.data()
        data.pop('ci')
        self.assertEqual(config.parse_config(data).ci, config.CIPolicy(5, 30))
        data['ci'] = {'grace_minutes': 0, 'required_timeout_minutes': 60}
        self.assertEqual(config.parse_config(data).ci, config.CIPolicy(0, 60))

    def test_invalid_values_and_unknown_keys_stop_devloop(self):
        bad = [{'grace_minutes': -1}, {'grace_minutes': 1.5}, {'grace_minutes': '5'},
               {'grace_minutes': True}, {'required_timeout_minutes': 0},
               {'timeout': 3}, 'yes']
        for table in bad:
            with self.subTest(table=table), self.assertRaises(config.ConfigError):
                config.parse_config(dict(self.data(), ci=table))

    def test_the_local_overlay_may_set_them(self):
        data = config.apply_overlay(self.data(), {'ci': {'grace_minutes': 2}})
        self.assertEqual(config.parse_config(data).ci, config.CIPolicy(2, 30))
        with self.assertRaises(config.ConfigError):
            config.apply_overlay(self.data(), {'ci': {'other': 1}})


class AdapterTests(unittest.TestCase):
    def make(self, outputs):
        self.calls = []

        def run(argv, **kw):
            self.calls.append(argv)
            return SimpleNamespace(returncode=0, stdout=outputs.pop(0), stderr='')
        return GitHub('o/r', run=run)

    def test_the_commit_date(self):
        gh = self.make([json.dumps({'date': '2026-11-17T11:50:20Z'})])
        self.assertEqual(gh.commit_date(SHA1), '2026-11-17T11:50:20Z')
        self.assertEqual(self.calls, [['gh', 'api', f'repos/o/r/commits/{SHA1}', '--jq',
                                       '{date: .commit.committer.date}']])

    def test_the_required_checks_of_rulesets_and_branch_protection(self):
        rules = '\n'.join(json.dumps({'context': c}) for c in ('unittest', 'lint'))
        gh = self.make([rules, json.dumps({'contexts': ['lint', 'legacy']})])
        self.assertEqual(gh.required_checks('main'), ['legacy', 'lint', 'unittest'])
        self.assertEqual([argv[:4] for argv in self.calls],
                         [['gh', 'api', '--paginate', 'repos/o/r/rules/branches/main?per_page=100'],
                          ['gh', 'api', 'repos/o/r/branches/main', '--jq']])
        gh = self.make(['', json.dumps({'contexts': None})])
        self.assertEqual(gh.required_checks('main'), [])


if __name__ == '__main__':
    unittest.main()
