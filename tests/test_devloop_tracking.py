"""devloop status: the label tracking:attiva of the tracking issues in use, which may stay closed
(#690)."""

import dataclasses
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import devloop.__main__ as cli  # noqa: E402
from devloop.config import DirectionPolicy, ReportPolicy, RunPolicy  # noqa: E402
from devloop.github import GitHub, GitHubError  # noqa: E402

from test_devloop import CFG  # noqa: E402

LABEL = 'tracking:attiva'
REPORT, ALERT, DIRECTION, OLD = 10, 11, 12, 20


def configured(report=REPORT, alert=ALERT, direction=DIRECTION, cfg=CFG):
    return dataclasses.replace(cfg, report=ReportPolicy(issue=report),
                               run=RunPolicy(alert_issue=alert),
                               direction=DirectionPolicy(issue=direction))


class Tracking:
    """The issues of a repository, open or closed, with their labels; reads only: a write of a
    label fails with AttributeError."""

    def __init__(self, issues, labels=('tracking', LABEL), error=None):
        self.issues, self.labels, self.error = issues, list(labels), error
        self.listed = []

    def repo_labels(self):
        if self.error:
            raise self.error
        return self.labels

    def labelled_numbers(self, label):
        self.listed.append(label)
        return [n for n, (_state, names) in self.issues.items() if label in names]

    def issue_comments(self, number):
        return []


# The diary and the summary closed, as the operator left them on 2026-11-26.
ISSUES = {REPORT: ('closed', {'tracking', LABEL}),
          ALERT: ('open', {'tracking', LABEL}),
          DIRECTION: ('closed', {'tracking'}),
          OLD: ('closed', {'tracking', LABEL}),
          30: ('closed', {'tracking'})}


class TrackingLinesTests(unittest.TestCase):
    def test_with_and_without_the_label_open_or_closed(self):
        lines = cli.tracking_lines(configured(), Tracking(ISSUES))
        self.assertEqual(lines, [
            f'tracking: issue #{DIRECTION} ([direction] issue) has no label {LABEL}: add it; '
            'the issue may stay closed',
            f'tracking: #{OLD} has the label {LABEL} and no key of the configuration '
            '([report] issue, [run].alert_issue, [direction] issue) names it: remove the label '
            'if it is no longer in use'])

    def test_nothing_to_say_when_every_issue_in_use_has_it(self):
        issues = {REPORT: ('closed', {LABEL}), ALERT: ('closed', {LABEL}),
                  DIRECTION: ('open', {LABEL}), 30: ('closed', {'tracking'})}
        self.assertEqual(cli.tracking_lines(configured(), Tracking(issues)), [])

    def test_an_issue_named_by_two_keys_once(self):
        # The diary is also the issue of the alerts.
        gh = Tracking({REPORT: ('closed', {LABEL})})
        self.assertEqual(cli.tracking_lines(configured(alert=ALERT, report=ALERT, direction=0),
                                            gh),
                         [f'tracking: issue #{ALERT} ([report] issue, [run].alert_issue) has no '
                          f'label {LABEL}: add it; the issue may stay closed',
                          f'tracking: #{REPORT} has the label {LABEL} and no key of the '
                          'configuration ([report] issue, [run].alert_issue, [direction] issue) '
                          'names it: remove the label if it is no longer in use'])

    def test_the_label_left_after_a_change_of_the_configuration(self):
        # [report] issue moved from 20 to 10: the label stays on 20 until the operator moves it.
        gh = Tracking({REPORT: ('open', set()), OLD: ('closed', {LABEL})})
        lines = cli.tracking_lines(configured(alert=0, direction=0), gh)
        self.assertEqual(len(lines), 2)
        self.assertIn(f'issue #{REPORT} ([report] issue) has no label', lines[0])
        self.assertIn(f'#{OLD} has the label {LABEL}', lines[1])
        # Even with no tracking issue left in the configuration.
        self.assertEqual(len(cli.tracking_lines(CFG, gh)), 1)

    def test_the_label_missing_from_the_repository_once(self):
        gh = Tracking(ISSUES, labels=('tracking',))
        self.assertEqual(cli.tracking_lines(configured(), gh), [
            f'tracking: the label {LABEL} does not exist in {CFG.repo}: create it (gh label '
            f'create {LABEL} -R {CFG.repo}) and add it to the tracking issues in use: '
            f'#{REPORT}, #{ALERT}, #{DIRECTION}'])
        self.assertEqual(gh.listed, [])
        # Without a tracking issue in the configuration, a missing label is no news.
        self.assertEqual(cli.tracking_lines(CFG, gh), [])

    def test_a_failed_read_is_a_line(self):
        gh = Tracking(ISSUES, error=GitHubError('HTTP 502'))
        self.assertEqual(cli.tracking_lines(configured(), gh),
                         [f'tracking: the label {LABEL} not checked: HTTP 502'])


class StatusTests(unittest.TestCase):
    def test_status_warns_and_touches_nothing(self):
        lines = []
        gh = Tracking(ISSUES)

        def stop():
            raise RuntimeError('stop')
        d = SimpleNamespace(gh=gh, pinner=None, snapshot=stop)
        with mock.patch.object(cli, 'build', return_value=(configured(), Path('/state'), d)), \
                mock.patch.object(cli, 'log', lines.append), \
                mock.patch.object(cli, 'label_transition', return_value=None), \
                self.assertRaises(RuntimeError):
            cli.main(['status'])
        warned = [line for line in lines if line.startswith('tracking: ')]
        self.assertEqual(len(warned), 2, lines)
        self.assertIn(f'#{DIRECTION} ([direction] issue) has no label', warned[0])
        self.assertIn(f'#{OLD} has the label', warned[1])
        self.assertEqual(gh.listed, [LABEL])


class AdapterTests(unittest.TestCase):
    def test_issues_with_the_label_open_or_closed(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout='10\n20\n', stderr='')
        self.assertEqual(GitHub('o/r', run=fake_run).labelled_numbers(LABEL), [10, 20])
        self.assertEqual(calls[0], ['gh', 'api', '--paginate',
                                    'repos/o/r/issues?labels=tracking%3Aattiva&state=all'
                                    '&per_page=100', '--jq', '.[].number'])


if __name__ == '__main__':
    unittest.main()
