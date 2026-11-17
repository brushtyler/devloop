"""devloop: the issue of a merged PR is closed by the dispatcher, and a ready issue that a
merged devloop PR resolved is never started again (#228)."""

import dataclasses
import unittest
from types import SimpleNamespace

from devloop import markdown
from devloop.dispatcher import Action, Dispatcher, Snapshot, plan
from devloop.github import GitHub, GitHubError
from test_devloop import CFG, REV, SHA1, FakeRunner, FakeWS, implemented
from test_devloop_merge import AUTO, STRICT, Identities, MergeGH
from test_devloop_supervision import AUTO as SUP_AUTO
from test_devloop_supervision import PREFLIGHT_AUTO, SUPERVISOR_LOGIN, crash
from test_devloop_supervision import Case as SupervisionCase

MERGE_COMMIT = 'c' * 40
READY = 'agent:ready'
HUMAN, SUPER = 'agent:needs-operator', 'agent:needs-supervisor'


class ClosingGH(MergeGH):
    """MergeGH whose PR #100 resolves issue #7, open with ``agent:ready`` until something
    closes it; once merged the PR reports its merge commit."""

    def __init__(self, issue_state='OPEN'):
        super().__init__()
        self.issue_state = issue_state
        self.labels[7] = {READY}
        self.closed = []

    def issue(self, number):
        return dict(super().issue(number), state=self.issue_state,
                    labels=[{'name': n} for n in sorted(self.labels.get(number, ()))])

    def pr_state(self, number):
        merged = self.state == 'MERGED'
        return dict(super().pr_state(number),
                    mergeCommit={'oid': MERGE_COMMIT} if merged else None)

    def close_issue(self, number):
        self.closed.append(number)
        self.issue_state = 'CLOSED'


def issue_records(gh, number, kind):
    return [r for n, body in gh.comments if n == number
            if (r := markdown.read_marker(body, kind))]


class CloseAfterMergeTests(unittest.TestCase):
    def make(self, cfg, gh, merger=None):
        self.gh, self.logs = gh, []
        return Dispatcher(cfg, gh, FakeWS(), FakeRunner(), log=self.logs.append,
                          identities=Identities(gh), merger=merger)

    def test_strict_and_auto_close_the_issue_with_a_record_and_drop_agent_ready(self):
        for cfg in (STRICT, AUTO):
            with self.subTest(mode=cfg.merge.mode):
                merger = SimpleNamespace(merged=[])
                merger.merge_pr = lambda number, head: (merger.merged.append((number, head)),
                                                        setattr(gh, 'state', 'MERGED'))
                gh = ClosingGH()
                d = self.make(cfg, gh, merger)
                self.assertEqual([a.kind for a in d.tick()], ['merge'])
                self.assertEqual(merger.merged, [(100, SHA1)])
                # The dispatcher, not the merger, closes the issue of the PR's marker.
                self.assertEqual(gh.closed, [7])
                self.assertNotIn(READY, gh.labels[7])
                (record,) = issue_records(gh, 7, 'closed')
                self.assertEqual(record, {'pr': 100, 'head': SHA1,
                                          'merge_commit': MERGE_COMMIT,
                                          'mode': cfg.merge.mode})
                body = [b for n, b in gh.comments if n == 7][-1]
                self.assertTrue(body.startswith('> [!NOTE]\n> **Bot:** devloop dispatcher'))
                self.assertIn('#100', body)
                self.assertIn(MERGE_COMMIT[:12], body)
                self.assertIn(f'`{cfg.merge.mode}`', body)
                # The PR keeps its own merged record.
                self.assertEqual(len(issue_records(gh, 100, 'merged')), 1)

    def test_an_issue_already_closed_is_not_touched(self):
        for cfg in (STRICT, AUTO):
            with self.subTest(mode=cfg.merge.mode):
                gh = ClosingGH(issue_state='CLOSED')
                d = self.make(cfg, gh)
                self.assertEqual([a.kind for a in d.tick()], ['merge'])
                self.assertEqual(gh.merged, [(100, SHA1)])
                self.assertEqual(gh.closed, [])
                self.assertEqual([b for n, b in gh.comments if n == 7], [])
                self.assertEqual(gh.labels[7], {READY})
                self.assertTrue(any('issue #7 is CLOSED: left alone' in line
                                    for line in self.logs))

    def test_a_failed_closure_is_logged_and_the_merge_stands(self):
        gh = ClosingGH()

        def refuse(number):
            raise GitHubError('gh api -X PATCH: 403')
        gh.close_issue = refuse
        d = self.make(STRICT, gh)
        d.tick()
        self.assertEqual(gh.merged, [(100, SHA1)])
        self.assertEqual(len(issue_records(gh, 100, 'merged')), 1)
        self.assertNotIn(HUMAN, gh.labels[100])
        # agent:ready stays, so that the planner's safety net sees the open issue.
        self.assertEqual(gh.labels[7], {READY})
        self.assertTrue(any('issue #7 NOT closed' in line for line in self.logs))

    def test_close_and_merged_prs_through_simulated_gh(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout='[]', stderr='')

        gh = GitHub('o/r', run=fake_run)
        gh.close_issue(7)
        self.assertEqual(calls[0], ['gh', 'api', '-X', 'PATCH', 'repos/o/r/issues/7',
                                    '-f', 'state=closed', '-f', 'state_reason=completed'])
        self.assertEqual(gh.merged_prs('agent/issue-7'), [])
        argv = calls[1]
        self.assertEqual(argv[:9], ['gh', 'pr', 'list', '-R', 'o/r', '--state', 'merged',
                                    '--head', 'agent/issue-7'])
        self.assertIn('mergeCommit', argv[argv.index('--json') + 1].split(','))


def ready_issue(number, merged=()):
    return {'number': number, 'title': f'Issue {number}', 'labels': {READY},
            'labeler': 'brushtyler', 'ready_at': '2026-11-13T10:00:00Z', 'admitted': True,
            'priority': 'normal', 'merged': list(merged),
            'preflight': {'record': None, 'recheck': False, 'hash': 'h', 'owned': False}}


def merged_pr(author='brushtyler', branch='agent/issue-7', cross=False):
    return {'number': 99, 'title': 't', 'body': 'no marker', 'labels': [],
            'headRefName': branch, 'headRefOid': SHA1, 'author': {'login': author},
            'isCrossRepository': cross, 'mergeCommit': {'oid': MERGE_COMMIT}}


class PlannerTests(unittest.TestCase):
    def test_a_ready_issue_with_a_merged_pr_is_never_started(self):
        preflight = dataclasses.replace(CFG, preflight=REV)
        snap = Snapshot([], [ready_issue(7, merged=[99]), ready_issue(8)], [])
        for cfg in (CFG, preflight):
            with self.subTest(preflight=bool(cfg.preflight)):
                actions = plan(snap, cfg)
                self.assertEqual(actions[0], Action('merged_issue', 7, 'resolved by merged PR #99'))
                self.assertNotIn(7, [a.number for a in actions if a.kind != 'merged_issue'])
                # Another ready issue still starts.
                self.assertEqual([(a.kind, a.number) for a in actions[1:]],
                                 [('preflight' if cfg.preflight else 'implement', 8)])

    def test_an_escalated_one_is_left_alone(self):
        for label in (HUMAN, SUPER):
            with self.subTest(label=label):
                issue = ready_issue(7, merged=[99])
                issue['labels'] = {READY, label}
                self.assertEqual(plan(Snapshot([], [issue], []), CFG), [])


class TrackerGH(MergeGH):
    """No open PR; issue #7 open with ``agent:ready`` from a trusted user, and the merged PRs
    of ``merged_raw``."""

    def open_prs(self):
        return []

    def issues_with_label(self, label):
        return [{'number': n, 'title': f'Issue {n}', 'author': {'login': 'brushtyler'},
                 'labels': [{'name': x} for x in sorted(names)]}
                for n, names in sorted(self.labels.items()) if label in names]

    def labeled_events(self, number):
        return [{'label': READY, 'by': 'brushtyler', 'at': '2026-11-13T10:00:00Z'}]


class SnapshotTests(unittest.TestCase):
    def make(self, cfg):
        self.gh = TrackerGH()
        self.gh.labels = {7: {READY}}
        return Dispatcher(cfg, self.gh, FakeWS(), FakeRunner(), log=lambda m: None,
                          identities=Identities(self.gh))

    def test_outside_auto_it_goes_to_the_operator_without_agent_ready(self):
        for cfg in (CFG, STRICT):
            with self.subTest(mode=cfg.merge.mode):
                d = self.make(cfg)
                self.gh.merged_raw = (merged_pr(),)
                self.assertEqual(d.snapshot().ready_issues[0]['merged'], [99])
                self.assertEqual(d.tick(), [Action('merged_issue', 7, 'resolved by merged PR #99')])
                self.assertEqual(self.gh.labels[7], {HUMAN})
                (notice,) = issue_records(self.gh, 7, 'notice')
                self.assertEqual((notice['class'], notice['critical'], notice['to']),
                                 ('merged_issue', False, 'operator'))
                self.assertIn('#99', [b for n, b in self.gh.comments if n == 7][0])
                self.assertEqual(self.gh.created, [])
                # Not escalated twice, and still not started.
                self.gh.labels[7].add(READY)
                self.assertEqual(d.tick(), [])
                self.assertEqual(self.gh.created, [])

    def test_a_merged_pr_devloop_does_not_recognise_does_not_count(self):
        for raw in (merged_pr(author='stranger'), merged_pr(cross=True),
                    merged_pr(branch='agent/issue-70')):
            with self.subTest(raw=raw):
                d = self.make(CFG)
                self.gh.merged_raw = (raw,)
                snap = d.snapshot()
                self.assertEqual(snap.ready_issues[0]['merged'], [])
                self.assertEqual(plan(snap, CFG), [Action('implement', 7)])


class SupervisorTests(SupervisionCase):
    def test_in_auto_it_goes_to_the_supervisor_and_a_retry_is_refused(self):
        d = self.make(SUP_AUTO, has_pr=False)
        self.gh.merged_raw = (merged_pr(),)
        self.assertEqual(self.tick(d), [('merged_issue', 7)])
        self.assertEqual(self.gh.labels[7], {READY, SUPER})
        (escalation,) = self.escalations(7)
        self.assertEqual((escalation['class'], escalation['to'], escalation['step']),
                         ('merged_issue', 'supervisor', 'implement'))
        self.assertEqual(self.tick(d), [])
        # A retry would implement it again: refused, and the escalation stays.
        self.gh.command(7, '/devloop retry', SUPERVISOR_LOGIN)
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.decisions(7)[-1]['outcome'], 'refused')
        self.assertEqual(self.gh.labels[7], {READY, SUPER})
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.tick(d), [])

    def test_a_retry_of_an_earlier_tool_failure_is_refused_once_a_pr_merged(self):
        # A tool failure escalated before the PR merged (e.g. a duplicate readiness check
        # started after the merge): request and base are unchanged, but the step never runs.
        for name, cfg in (('preflight', PREFLIGHT_AUTO), ('implement', SUP_AUTO)):
            with self.subTest(step=name):
                d = self.make(cfg, crash(), implemented(), has_pr=False)
                self.assertEqual(self.tick(d), [(name, 7)])
                (failure,) = self.escalations(7)
                self.assertEqual((failure['class'], failure['step']), ('tool_failure', name))
                self.gh.merged_raw = (merged_pr(),)
                self.gh.command(7, '/devloop retry', SUPERVISOR_LOGIN)
                self.assertEqual(self.tick(d), [('supervise', 7)])
                decision = self.decisions(7)[-1]
                self.assertEqual((decision['command'], decision['outcome']), ('retry', 'refused'))
                self.assertIn('#99', [c['body'] for c in self.gh.threads[7]
                                      if markdown.read_marker(c['body'], 'supervision')][-1])
                self.assertEqual(len(self.runner.calls), 1)
                self.assertEqual(self.gh.created, [])
                escalation = self.escalations(7)[-1]
                self.assertEqual((escalation['class'], escalation['to']),
                                 ('merged_issue', 'supervisor'))
                self.assertIn(SUPER, self.gh.labels[7])
                self.assertNotIn(HUMAN, self.gh.labels[7])
                # Another retry is refused for the new class, and nothing runs.
                self.gh.command(7, '/devloop retry', SUPERVISOR_LOGIN)
                self.assertEqual(self.tick(d), [('supervise', 7)])
                self.assertEqual(self.decisions(7)[-1]['outcome'], 'refused')
                self.assertEqual(len(self.runner.calls), 1)

    def test_a_refused_retry_goes_to_the_operator_when_the_supervisor_cannot_decide(self):
        d = self.make(SUP_AUTO, crash(), has_pr=False)
        self.assertEqual(self.tick(d), [('implement', 7)])
        self.gh.merged_raw = (merged_pr(),)
        self.gh.author = 'stranger'
        self.gh.command(7, '/devloop retry', SUPERVISOR_LOGIN)
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.decisions(7)[-1]['outcome'], 'refused')
        escalation = self.escalations(7)[-1]
        self.assertEqual((escalation['class'], escalation['to']), ('merged_issue', 'operator'))
        self.assertEqual(self.gh.labels[7], {HUMAN})
        self.assertEqual(len(self.runner.calls), 1)


if __name__ == '__main__':
    unittest.main()
