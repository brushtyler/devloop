"""devloop: start order of the ready issues (#146)."""

import dataclasses
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from devloop.dispatcher import Dispatcher, ensure_labels, issue_priority, plan
from devloop.github import GitHub
from test_devloop import CFG, FakeGH, FakeRunner, FakeWS

READY = 'agent:ready'
HIGH, NORMAL, LOW = 'priority:high', 'priority:normal', 'priority:low'
# Room for every ready issue: the order is the only thing that decides.
QCFG = dataclasses.replace(CFG, max_open_prs=10, max_actions_per_tick=10)


def at(day):
    return f'2026-11-{day:02d}T10:00:00Z'


def added(label, day, by='brushtyler'):
    """A ``labeled`` event as ``GitHub.labeled_events`` returns it."""
    return {'label': label, 'by': by, 'at': at(day)}


class QueueGH(FakeGH):
    """Ready issues with their current labels and every ``labeled`` event, oldest first:
    ``{number: (labels, events)}``."""

    def __init__(self, issues):
        super().__init__()
        self.queue = issues

    def issues_with_label(self, label):
        return [{'number': n, 'title': f'Issue {n}', 'labels': [{'name': l} for l in sorted(labels)]}
                for n, (labels, _) in sorted(self.queue.items()) if label in labels]

    def labeled_events(self, number):
        return [dict(event) for event in self.queue[number][1]]


class StartOrderTests(unittest.TestCase):
    def make(self, issues, cfg=QCFG):
        self.gh, self.logs = QueueGH(issues), []
        return Dispatcher(cfg, self.gh, FakeWS(), FakeRunner(), log=self.logs.append)

    def started(self, d, cfg=QCFG):
        return [a.number for a in plan(d.snapshot(), cfg) if a.kind == 'implement']

    def priorities(self, d):
        return {i['number']: i['priority'] for i in d.snapshot().ready_issues}

    def test_high_priority_starts_first_then_normal_then_low(self):
        d = self.make({
            1: ({READY, LOW}, [added(READY, 1), added(LOW, 1)]),
            2: ({READY}, [added(READY, 4)]),
            3: ({READY, HIGH}, [added(READY, 5), added(HIGH, 5)]),
            4: ({READY, NORMAL}, [added(READY, 3), added(NORMAL, 3)]),
            # priority_labels keep counting as high, with the same trust check.
            5: ({READY, 'prerequisito'}, [added(READY, 6), added('prerequisito', 6)]),
        })
        self.assertEqual(self.priorities(d), {1: 'low', 2: 'normal', 3: 'high', 4: 'normal',
                                              5: 'high'})
        self.assertEqual(self.started(d), [3, 5, 4, 2, 1])
        # With fewer free slots, the highest priorities take them.
        cfg = dataclasses.replace(QCFG, max_open_prs=2)
        self.assertEqual(self.started(d, cfg), [3, 5])

    def test_same_priority_starts_by_the_latest_agent_ready_then_number(self):
        d = self.make({
            # Ready since day 1, but the label was removed and added again on day 7: the
            # current admission counts.
            8: ({READY}, [added(READY, 1), added(READY, 7)]),
            9: ({READY}, [added(READY, 3)]),
            5: ({READY}, [added(READY, 3)]),
            6: ({READY}, [added(READY, 2)]),
        })
        snap = d.snapshot()
        self.assertEqual({i['number']: i['ready_at'] for i in snap.ready_issues},
                         {5: at(3), 6: at(2), 8: at(7), 9: at(3)})
        self.assertEqual(self.started(d), [6, 5, 9, 8])

    def test_without_priority_labels_the_order_is_agent_ready_then_number(self):
        d = self.make({n: ({READY}, [added(READY, day)])
                       for n, day in ((1, 9), (2, 8), (3, 8), (4, 1))})
        self.assertEqual(set(self.priorities(d).values()), {'normal'})
        self.assertEqual(self.started(d), [4, 2, 3, 1])

    def test_a_priority_label_from_an_untrusted_user_is_ignored(self):
        d = self.make({
            1: ({READY}, [added(READY, 1)]),
            2: ({READY, HIGH}, [added(READY, 2), added(HIGH, 2, by='stranger')]),
            # Added by a trusted user, removed, added again by a stranger: the latest counts.
            3: ({READY, HIGH}, [added(READY, 3), added(HIGH, 3), added(HIGH, 4, by='stranger')]),
            4: ({READY, 'prerequisito'}, [added(READY, 4), added('prerequisito', 4, by='stranger')]),
            # A low priority from a stranger does not lower it either.
            5: ({READY, LOW}, [added(READY, 5), added(LOW, 5, by='stranger')]),
            # A trusted high priority removed since no longer counts; the trusted low one does.
            6: ({READY, LOW}, [added(READY, 1), added(HIGH, 1), added(LOW, 1)]),
        })
        self.assertEqual(self.priorities(d), {1: 'normal', 2: 'normal', 3: 'normal',
                                              4: 'normal', 5: 'normal', 6: 'low'})
        self.assertEqual(self.started(d), [1, 2, 3, 4, 5, 6])

    def test_issue_priority_takes_the_highest_trusted_label(self):
        trusted = ('brushtyler',)
        events = [added(LOW, 1), added(HIGH, 2), added(NORMAL, 3)]
        self.assertEqual(issue_priority({READY, LOW, HIGH}, events, trusted), 'high')
        self.assertEqual(issue_priority({READY, LOW, NORMAL}, events, trusted), 'normal')
        self.assertEqual(issue_priority({READY, LOW}, events, trusted), 'low')
        self.assertEqual(issue_priority({READY}, events, trusted), 'normal')
        # A label without any labeled event (e.g. events not readable) is not trusted.
        self.assertEqual(issue_priority({READY, HIGH}, [], trusted), 'normal')
        self.assertEqual(issue_priority({READY, 'x'}, [added('x', 1)], trusted, ('x',)), 'high')
        self.assertEqual(issue_priority({READY, 'x'}, [added('x', 1)], trusted), 'normal')

    def test_status_and_dry_run_show_priority_and_order(self):
        import devloop.__main__ as cli
        issues = {
            1: ({READY, LOW}, [added(READY, 1), added(LOW, 1)]),
            2: ({READY}, [added(READY, 4)]),
            3: ({READY, HIGH}, [added(READY, 5), added(HIGH, 5)]),
        }
        expected = [f'ready 1. #3 priority=high agent:ready={at(5)} labelled by brushtyler: Issue 3',
                    f'ready 2. #2 priority=normal agent:ready={at(4)} labelled by brushtyler: Issue 2',
                    f'ready 3. #1 priority=low agent:ready={at(1)} labelled by brushtyler: Issue 1']
        d = self.make(issues)
        lines = []
        with mock.patch.object(cli, 'build', return_value=(QCFG, Path('/state'), d)), \
                mock.patch.object(cli, 'log', lines.append):
            cli.main(['status'])
        self.assertEqual([line for line in lines if line.startswith('ready ')], expected)
        self.assertEqual([line for line in lines if line.startswith('next: ')],
                         ['next: implement #3', 'next: implement #2', 'next: implement #1'])
        d = self.make(issues)
        self.assertEqual([a.number for a in d.tick(dry_run=True)], [3, 2, 1])
        self.assertEqual(self.logs[:3], [f'[dry-run] {line}' for line in expected])
        self.assertEqual(self.logs[3:], ['[dry-run] implement #3', '[dry-run] implement #2',
                                         '[dry-run] implement #1'])
        self.assertEqual((self.gh.comments, self.gh.labels), ([], {}))

    def test_setup_creates_the_priority_labels(self):
        created = []
        ensure_labels(SimpleNamespace(ensure_label=lambda name, *style: created.append(name)),
                      CFG.labels, CFG.review_policy)
        for name in (HIGH, NORMAL, LOW):
            self.assertIn(name, created)


class LabeledEventsTests(unittest.TestCase):
    def test_reads_every_labeled_event_with_rest(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            out = ('{"label":"agent:ready","by":"brushtyler","at":"2026-11-13T10:00:00Z"}\n'
                   '{"label":"priority:high","by":"stranger","at":"2026-11-14T10:00:00Z"}\n')
            return SimpleNamespace(returncode=0, stdout=out, stderr='')

        events = GitHub('o/r', run=fake_run).labeled_events(7)
        self.assertEqual(events, [added(READY, 13), added(HIGH, 14, by='stranger')])
        (argv,) = calls
        self.assertIn('repos/o/r/issues/7/events', argv)
        self.assertIn('--paginate', argv)
        self.assertIn('select(.event == "labeled")', argv[argv.index('--jq') + 1])


if __name__ == '__main__':
    unittest.main()
