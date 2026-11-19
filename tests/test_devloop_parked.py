"""devloop: PRs waiting for a person do not take the slots of max_open_prs (#199)."""

import dataclasses
import unittest
from pathlib import Path
from unittest import mock

from devloop import config
from devloop.config import ConfigError, MergePolicy
from devloop.planning import Snapshot, issue_priority, parked, plan, pr_counts
from helpers import ssdlc_project
from test_devloop import CFG, FakeWS, norm, pr, raw_review, ready
from test_devloop_merge import AUTO, STRICT, Case
from test_devloop_zero import changes

NEEDS_OPERATOR, READY = 'agent:needs-operator', 'agent:ready-to-merge'
OFF = dataclasses.replace(CFG, merge=MergePolicy(mode='off'), max_open_prs=3, max_parked_prs=3,
                          max_actions_per_tick=10)


def ready_pr(number, mode=None):
    """An approved PR in agent:ready-to-merge, its merge mode as the snapshot computed it. A
    merge that is the operator's was already told (#248)."""
    p = pr(number=number, issue=number, labels=[READY], reviews=[norm(raw_review())])
    p.mergeable, p.merge_status, p.merge_mode = 'MERGEABLE', 'CLEAN', mode
    p.operator_merges = [p.head_sha]
    return p


def in_review(number):
    """A PR devloop is working on: in review, its approval waiting for CI (#271)."""
    return pr(number=number, issue=number, checks='pending', reviews=[norm(raw_review())])


def snapshot(prs, *issues):
    return Snapshot(prs=list(prs), ready_issues=list(issues), working_issues=[])


def started(snap, cfg):
    return [a.number for a in plan(snap, cfg) if a.kind == 'implement']


class PlanTests(unittest.TestCase):
    def test_parked_prs_leave_the_slots_of_max_open_prs_free(self):
        # Three open PRs, two of them waiting for a person: with max_open_prs = 3 a new issue
        # starts, as many as the slots left by the PRs in progress.
        prs = [in_review(1), pr(number=2, issue=2, labels=[NEEDS_OPERATOR]), ready_pr(3)]
        snap = snapshot(prs, ready(10), ready(11), ready(12))
        self.assertEqual(pr_counts(snap, OFF), (1, 2))
        self.assertEqual(started(snap, OFF), [10, 11])
        # Before #199 every open PR took a slot: the PRs in progress still do.
        snap.prs.append(in_review(4))
        self.assertEqual(started(snap, OFF), [10])
        snap.prs.append(in_review(5))
        self.assertEqual(started(snap, OFF), [])

    def test_the_operator_merge_label_does_not_change_the_count(self):
        # A ready PR devloop does not merge was already parked (#199); the label the dispatcher
        # adds to it (#248) does not count it twice, nor does an escalation's label.
        told = ready_pr(2)
        told.labels.add(NEEDS_OPERATOR)
        told.operator_owned = True
        for prs in ([told], [ready_pr(2)], [pr(number=2, issue=2, labels=[NEEDS_OPERATOR])]):
            with self.subTest(labels=prs[0].labels):
                self.assertEqual(pr_counts(snapshot(prs), OFF), (0, 1))

    def test_no_new_issue_once_max_parked_prs_is_reached(self):
        cfg = dataclasses.replace(OFF, max_parked_prs=2)
        prs = [pr(number=1, issue=1, labels=[NEEDS_OPERATOR]), ready_pr(2)]
        snap = snapshot(prs, ready(10))
        self.assertEqual(pr_counts(snap, cfg), (0, 2))
        self.assertEqual(started(snap, cfg), [])
        # One parked PR fewer, and the issue starts again.
        self.assertEqual(started(snapshot(prs[:1], ready(10)), cfg), [10])
        # The same with the readiness check: no check of a new issue starts either.
        checked = dataclasses.replace(cfg, preflight='codex')
        issue = dict(ready(10), preflight={'record': None, 'hash': 'h', 'recheck': False,
                                           'owned': False})
        self.assertEqual(plan(snapshot(prs, issue), checked), [])
        self.assertEqual([(a.kind, a.number) for a in plan(snapshot(prs[:1], issue), checked)],
                         [('preflight', 10)])

    def test_a_high_priority_issue_starts_with_max_parked_prs_reached(self):
        # #274: three PRs parked (3/3) waiting for the fix of the same test, and the fix is a
        # ready priority:high issue: it starts, a normal or low one waits as before.
        prs = [pr(number=n, issue=n, labels=[NEEDS_OPERATOR]) for n in (1, 2, 3)]
        snap = snapshot(prs, ready(10), ready(11, priority='high'), ready(12, priority='low'))
        self.assertEqual(pr_counts(snap, OFF), (0, 3))
        actions = [a for a in plan(snap, OFF) if a.kind == 'implement']
        self.assertEqual([a.number for a in actions], [11])
        self.assertEqual(actions[0].reason,
                         'priority high: starts although 3/3 PRs are parked (max_parked_prs)')
        self.assertEqual(started(snapshot(prs, ready(10), ready(12, priority='low')), OFF), [])
        # A priority_labels label (prerequisito) counts as high, as it does for the order.
        labels, events = {'agent:ready', 'prerequisito'}, [{'label': 'prerequisito',
                                                            'by': 'brushtyler', 'at': 't'}]
        priority = issue_priority(labels, events, OFF.trusted_users, ('prerequisito',))
        self.assertEqual(started(snapshot(prs, ready(10), ready(13, ['prerequisito'],
                                                                priority=priority)), OFF), [13])
        # Below the limit nothing changes: every issue starts, without the reason.
        actions = plan(snapshot(prs[:2], ready(10), ready(11, priority='high')), OFF)
        self.assertEqual([(a.kind, a.number, a.reason) for a in actions],
                         [('implement', 11, ''), ('implement', 10, '')])
        # The same with the readiness check: only the high issue is checked, with the reason.
        checked = dataclasses.replace(OFF, preflight='codex')
        view = {'record': None, 'hash': 'h', 'recheck': False, 'owned': False}
        issues = [dict(ready(10), preflight=view), dict(ready(11, priority='high'), preflight=view)]
        self.assertEqual([(a.kind, a.number, a.reason) for a in plan(snapshot(prs, *issues),
                                                                     checked)],
                         [('preflight', 11, 'no readiness check yet; priority high: starts '
                                            'although 3/3 PRs are parked (max_parked_prs)')])
        passed = dict(view, record={'hash': 'h', 'ready': True})
        issues = [dict(ready(10), preflight=passed),
                  dict(ready(11, priority='high'), preflight=passed)]
        self.assertEqual([(a.kind, a.number) for a in plan(snapshot(prs, *issues), checked)],
                         [('implement', 11)])

    def test_a_high_priority_issue_still_respects_max_open_prs(self):
        # Parked limit reached and every slot of max_open_prs taken: the high issue waits too.
        parked_prs = [pr(number=n, issue=n, labels=[NEEDS_OPERATOR]) for n in (1, 2, 3)]
        busy = parked_prs + [in_review(4), in_review(5), in_review(6)]
        high = [ready(10, priority='high'), ready(11, priority='high')]
        self.assertEqual(pr_counts(snapshot(busy), OFF), (3, 3))
        self.assertEqual(started(snapshot(busy, *high), OFF), [])
        # One slot free: one high issue starts, not both.
        self.assertEqual(started(snapshot(busy[:-1], *high), OFF), [10])
        # A slot reserved by an action still running (continuous mode) counts as taken.
        self.assertEqual([a.number for a in plan(snapshot(busy[:-1], *high), OFF, reserved=1)
                          if a.kind == 'implement'], [])
        # The same with the readiness check.
        checked = dataclasses.replace(OFF, preflight='codex')
        view = {'record': None, 'hash': 'h', 'recheck': False, 'owned': False}
        self.assertEqual(plan(snapshot(busy, dict(high[0], preflight=view)), checked), [])

    def test_a_ready_pr_devloop_merges_itself_is_not_parked(self):
        for cfg in (dataclasses.replace(OFF, merge=STRICT.merge),
                    dataclasses.replace(OFF, merge=AUTO.merge, supervisor=AUTO.supervisor)):
            mode = cfg.merge.mode
            with self.subTest(mode=mode):
                p = ready_pr(1, mode)
                self.assertFalse(parked(p, cfg))
                # It takes a slot of max_open_prs while devloop merges it.
                one = dataclasses.replace(cfg, max_open_prs=1)
                snap = snapshot([p], ready(10))
                self.assertEqual(pr_counts(snap, one), (1, 0))
                self.assertEqual([(a.kind, a.number) for a in plan(snap, one)], [('merge', 1)])
                # A mode "off" for this PR (level override, operator-only path) parks it.
                self.assertTrue(parked(ready_pr(1, 'off'), cfg))
                self.assertEqual(started(snapshot([ready_pr(1, 'off')], ready(10)), one), [10])
                # A mode not computable in this tick: devloop may still merge it.
                self.assertFalse(parked(ready_pr(1, None), cfg))
        # With [merge] off every ready PR waits for a person; agent:needs-operator always does.
        self.assertTrue(parked(ready_pr(1, None), OFF))
        self.assertTrue(parked(pr(labels=[NEEDS_OPERATOR]), STRICT))
        self.assertTrue(parked(pr(labels=[READY, NEEDS_OPERATOR]), STRICT))
        for labels in (['agent:review'], ['agent:changes'], []):
            with self.subTest(labels=labels):
                self.assertFalse(parked(pr(labels=labels), OFF))


class SnapshotTests(Case):
    def test_the_merge_mode_of_a_ready_pr_is_known_even_when_it_cannot_be_merged_now(self):
        # Not mergeable now (a required review missing): devloop still merges it later.
        d = self.make(STRICT)
        self.gh.status = 'BLOCKED'
        snap = d.snapshot()
        self.assertEqual(snap.prs[0].merge_mode, 'strict')
        self.assertEqual(pr_counts(snap, STRICT), (1, 0))
        self.assertEqual(plan(snap, STRICT), [])
        # The same PR touching an operator-only path is the operator's to merge: parked.
        ws = FakeWS()
        ws.diff = changes('x.py', 'docs/adr/0003.md')
        d = self.make(STRICT, ws=ws)
        self.gh.status = 'BLOCKED'
        snap = d.snapshot()
        self.assertEqual(snap.prs[0].merge_mode, 'off')
        self.assertEqual(pr_counts(snap, STRICT), (0, 1))

    def test_tick_log_and_status_show_both_counters(self):
        import devloop.__main__ as cli
        line = ('open PRs: 0/2 in progress (max_open_prs), 1/3 parked (max_parked_prs)')
        d = self.make(CFG)  # [merge] off: the ready PR waits for a person
        d.tick(dry_run=True)
        self.assertEqual(self.logs[0], f'[dry-run] {line}')
        d = self.make(CFG)
        d.tick()
        self.assertEqual(self.logs[0], line)
        lines = []
        with mock.patch.object(cli, 'build', return_value=(CFG, Path('/state'), d)), \
                mock.patch.object(cli, 'log', lines.append):
            cli.main(['status'])
        self.assertIn(line, lines)

    def test_tick_log_and_status_say_why_a_high_issue_starts_over_the_parked_limit(self):
        import devloop.__main__ as cli
        cfg = dataclasses.replace(CFG, max_parked_prs=1)
        reason = 'priority high: starts although 1/1 PRs are parked (max_parked_prs)'
        snap = snapshot([pr(number=1, issue=1, labels=[NEEDS_OPERATOR])],
                        ready(10), ready(11, priority='high'))
        d = self.make(cfg)
        with mock.patch.object(d, 'snapshot', return_value=snap):
            d.tick(dry_run=True)
        self.assertIn(f'[dry-run] implement #11: {reason}', self.logs)
        self.assertNotIn('[dry-run] implement #10', self.logs)
        lines = []
        with mock.patch.object(cli, 'build', return_value=(cfg, Path('/state'), d)), \
                mock.patch.object(cli, 'log', lines.append), \
                mock.patch.object(d, 'snapshot', return_value=snap):
            cli.main(['status'])
        self.assertEqual([line for line in lines if line.startswith('next: ')],
                         [f'next: implement #11: {reason}'])


class ConfigTests(unittest.TestCase):
    DATA = ssdlc_project.data()

    def test_max_parked_prs_is_configured_and_checked(self):
        self.assertEqual(config.Config.max_parked_prs, 3)
        self.assertEqual(self.DATA['max_parked_prs'], 3)
        self.assertEqual(config.parse_config(dict(self.DATA, max_parked_prs=5)).max_parked_prs, 5)
        without = {k: v for k, v in self.DATA.items() if k != 'max_parked_prs'}
        self.assertEqual(config.parse_config(without).max_parked_prs, 3)
        for value in (0, -1, '3', True):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    config.parse_config(dict(self.DATA, max_parked_prs=value))
        self.assertIn('max_parked_prs', config.OVERLAY_KEYS)

    def test_documented(self):
        root = Path(__file__).resolve().parent.parent
        self.assertIn('max_parked_prs', (root / 'docs' / 'devloop' / 'guide.md').read_text())
        self.assertIn('max_parked_prs', (root / 'devloop' / 'config.toml').read_text())


if __name__ == '__main__':
    unittest.main()
