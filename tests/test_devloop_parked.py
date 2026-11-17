"""devloop: PRs waiting for a person do not take the slots of max_open_prs (#199)."""

import dataclasses
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from devloop import config
from devloop.config import ConfigError, MergePolicy
from devloop.dispatcher import Snapshot, parked, plan, pr_counts
from test_devloop import CFG, FakeWS, norm, pr, raw_review, ready
from test_devloop_merge import AUTO, STRICT, Case
from test_devloop_zero import changes

NEEDS_HUMAN, READY = 'agent:needs-human', 'agent:ready-to-merge'
OFF = dataclasses.replace(CFG, merge=MergePolicy(mode='off'), max_open_prs=3, max_parked_prs=3,
                          max_actions_per_tick=10)


def ready_pr(number, mode=None):
    """An approved PR in agent:ready-to-merge, its merge mode as the snapshot computed it."""
    p = pr(number=number, issue=number, labels=[READY], reviews=[norm(raw_review())])
    p.mergeable, p.merge_status, p.merge_mode = 'MERGEABLE', 'CLEAN', mode
    return p


def in_review(number):
    """A PR devloop is working on: in review, waiting for CI."""
    return pr(number=number, issue=number, checks='pending')


def snapshot(prs, *issues):
    return Snapshot(prs=list(prs), ready_issues=list(issues), working_issues=[])


def started(snap, cfg):
    return [a.number for a in plan(snap, cfg) if a.kind == 'implement']


class PlanTests(unittest.TestCase):
    def test_parked_prs_leave_the_slots_of_max_open_prs_free(self):
        # Three open PRs, two of them waiting for a person: with max_open_prs = 3 a new issue
        # starts, as many as the slots left by the PRs in progress.
        prs = [in_review(1), pr(number=2, issue=2, labels=[NEEDS_HUMAN]), ready_pr(3)]
        snap = snapshot(prs, ready(10), ready(11), ready(12))
        self.assertEqual(pr_counts(snap, OFF), (1, 2))
        self.assertEqual(started(snap, OFF), [10, 11])
        # Before #199 every open PR took a slot: the PRs in progress still do.
        snap.prs.append(in_review(4))
        self.assertEqual(started(snap, OFF), [10])
        snap.prs.append(in_review(5))
        self.assertEqual(started(snap, OFF), [])

    def test_no_new_issue_once_max_parked_prs_is_reached(self):
        cfg = dataclasses.replace(OFF, max_parked_prs=2)
        prs = [pr(number=1, issue=1, labels=[NEEDS_HUMAN]), ready_pr(2)]
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
        # With [merge] off every ready PR waits for a person; agent:needs-human always does.
        self.assertTrue(parked(ready_pr(1, None), OFF))
        self.assertTrue(parked(pr(labels=[NEEDS_HUMAN]), STRICT))
        self.assertTrue(parked(pr(labels=[READY, NEEDS_HUMAN]), STRICT))
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


class ConfigTests(unittest.TestCase):
    DATA = tomllib.loads(config.DEFAULT_CONFIG.read_text())

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
