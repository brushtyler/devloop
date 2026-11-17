"""devloop: merge modes off, strict and auto, with hold and release by the supervisor (#172)."""

import dataclasses
import importlib.util
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import devloop.__main__ as devloop_main
from devloop import apps, config, markdown, review
from devloop.config import MergePolicy
from devloop.dispatcher import Action, Dispatcher, Snapshot, ensure_labels, merge_command, plan
from devloop.github import GitHub
from test_devloop import (CFG, IMPL_NAME, REV, REV_NAME, SHA1, SHA2, FakeRunner, FakeWS,
                          SnapshotGH, finding, norm, pr, raw_review, receipt)
from test_devloop_zero import GRANT, SUCCESS, ZCFG, ZERO_LABELS, changes

DATA = tomllib.loads(config.DEFAULT_CONFIG.read_text())
READY = 'agent:ready-to-merge'
HOLD = 'agent:merge-hold'
SUPERVISOR = 'agy'  # neither the implementer nor the level-1 reviewer of the tests
SUPERVISOR_LOGIN = 'agy-agent[bot]'
STRICT = dataclasses.replace(CFG, merge=MergePolicy(mode='strict'))
AUTO = dataclasses.replace(CFG, merge=MergePolicy(mode='auto'), supervisor=SUPERVISOR)
MERGE = Action('merge', 100, 'merge mode strict')


def header(name=IMPL_NAME):
    """The attribution header the supervisor writes above its commands (AGENTS.md)."""
    return markdown.header(name, '9.9', 'm', 'high')


class MergeGH(SnapshotGH):
    """A ready PR #100 at SHA1, approved by the level-1 reviewer, as GitHub shows it: comments
    in the order they were written (devloop's own with the ambient login), the PR's merge state
    and a simulated ``gh pr merge``."""

    def __init__(self):
        approval = markdown.render_review(norm(raw_review()), header(REV_NAME))
        super().__init__([])
        self.thread = []
        self.say('brushtyler', approval)
        self.labels[100] = {READY}
        self.mergeable, self.status, self.state = 'MERGEABLE', 'CLEAN', 'OPEN'
        self.merged = []

    def say(self, login, body):
        """A comment with an id larger than every earlier one, as GitHub assigns them."""
        number = max((c['id'] for c in self.thread), default=1000) + 1
        self.thread.append({'id': number, 'author': {'login': login}, 'body': body})
        return number

    def comment(self, number, body):
        super().comment(number, body)
        self.say('brushtyler', body)

    def pr_comments(self, number):
        return [dict(c) for c in self.thread]

    def open_prs(self):
        return [dict(raw, headRefOid=self.head, mergeable=self.mergeable,
                     mergeStateStatus=self.status) for raw in super().open_prs()]

    def pr_state(self, number):
        return {'state': self.state, 'headRefOid': self.head, 'mergeable': self.mergeable,
                'mergeStateStatus': self.status,
                'labels': [{'name': n} for n in sorted(self.labels.get(number, ()))]}

    def merge_pr(self, number, head):
        self.merged.append((number, head))
        self.state = 'MERGED'


class Identities:
    """Only the supervisor has an app: every other actor uses the ambient (trusted) login."""

    def __init__(self, gh):
        self.gh = gh

    def login(self, actor):
        return SUPERVISOR_LOGIN if actor == SUPERVISOR else None

    def agent_logins(self):
        return {SUPERVISOR_LOGIN}

    def github(self, actor, permissions=None):
        return self.gh


def records(gh, kind):
    return [r for r in (markdown.read_marker(b, kind) for _, b in gh.comments) if r]


class Case(unittest.TestCase):
    def make(self, cfg=STRICT, gh=None, ws=None):
        self.gh, self.ws, self.logs = gh or MergeGH(), ws or FakeWS(), []
        return Dispatcher(cfg, self.gh, self.ws, FakeRunner(), log=self.logs.append,
                          identities=Identities(self.gh))

    def assert_not_merged(self):
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(records(self.gh, 'merged'), [])


class MergeConfigTests(unittest.TestCase):
    def parse(self, merge=None, supervisor=None):
        data = dict(DATA)
        if merge is not None:
            data['merge'] = merge
        if supervisor is not None:
            data['roles'] = dict(DATA['roles'], supervisor=supervisor)
        return config.parse_config(data)

    def test_the_distributed_config_is_off(self):
        cfg = config.load_config()
        self.assertEqual(cfg.merge, MergePolicy(mode='off'))
        self.assertFalse(cfg.merge.enabled())
        self.assertEqual(cfg.merge.operator_only_paths, ('docs/adr/', 'AGENTS.md', 'CLAUDE.md'))
        self.assertEqual(cfg.supervisor, '')
        self.assertEqual(DATA['merge']['mode'], 'off')
        # Without a [merge] table, too.
        data = {k: v for k, v in DATA.items() if k != 'merge'}
        self.assertEqual(config.parse_config(data).merge, MergePolicy())

    def test_auto_needs_a_supervisor(self):
        for merge in ({'mode': 'auto'}, {'mode': 'strict', 'levels': {'1': 'auto'}}):
            with self.subTest(merge=merge):
                with self.assertRaisesRegex(config.ConfigError, 'supervisor'):
                    self.parse(merge)
        cfg = self.parse({'mode': 'auto', 'levels': {'2': 'off'}}, supervisor='claude')
        self.assertEqual((cfg.merge.mode, cfg.merge.levels, cfg.supervisor),
                         ('auto', ((2, 'off'),), 'claude'))

    def test_invalid_merge_tables_stop_devloop(self):
        for merge, supervisor in (({'mode': 'yes'}, None), ({'mode': 'auto'}, 'nobody'),
                                  ({'levels': {'3': 'off'}}, None),
                                  ({'levels': {'x': 'off'}}, None),
                                  ({'levels': {'1': 'maybe'}}, None), ({'levels': 'off'}, None),
                                  ({'operator_only_paths': '/'}, None),
                                  ({'operator_only_paths': ['../x']}, None),
                                  ({'operator_only_paths': ['docs/*']}, None),
                                  ({'method': 'squash'}, None), ('off', None)):
            with self.subTest(merge=merge, supervisor=supervisor):
                with self.assertRaises(config.ConfigError):
                    self.parse(merge, supervisor)

    def test_level_overrides_take_the_most_restrictive_mode(self):
        auto = MergePolicy(mode='auto', levels=((2, 'off'), (0, 'strict')))
        self.assertEqual([auto.mode_for(level, ['x.py']) for level in (0, 1, 2)],
                         ['strict', 'auto', 'off'])
        # An override never makes a mode less restrictive.
        strict = MergePolicy(mode='strict', levels=((1, 'auto'),))
        self.assertEqual(strict.mode_for(1, ['x.py']), 'strict')
        self.assertEqual(MergePolicy(mode='off', levels=((1, 'auto'),)).mode_for(1, []), 'off')

    def test_operator_only_paths_turn_the_merge_off(self):
        auto = MergePolicy(mode='auto')
        for path in ('docs/adr/0002-ruoli.md', 'AGENTS.md', 'CLAUDE.md', 'docs/adr'):
            with self.subTest(path=path):
                self.assertEqual(auto.mode_for(1, ['x.py', path]), 'off')
        for path in ('docs/adrx.md', 'docs/AGENTS.md.bak', 'tests/AGENTS.md'):
            with self.subTest(path=path):
                self.assertEqual(auto.mode_for(1, [path]), 'auto')
        custom = MergePolicy(mode='strict', operator_only_paths=('ssdlc_locks.py',))
        self.assertEqual(custom.mode_for(1, ['ssdlc_locks.py']), 'off')
        self.assertEqual(custom.mode_for(1, ['AGENTS.md']), 'strict')

    def test_setup_creates_the_hold_label(self):
        created = []
        ensure_labels(SimpleNamespace(ensure_label=lambda name, *style: created.append(name)),
                      CFG.labels, CFG.review_policy)
        self.assertIn(HOLD, created)
        self.assertIn(HOLD, CFG.labels.all())


class AdapterTests(unittest.TestCase):
    def test_merge_and_state_read_through_simulated_gh(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv, kw))
            return SimpleNamespace(returncode=0, stdout='{"state": "OPEN"}', stderr='')

        gh = GitHub('o/r', run=fake_run)
        gh.merge_pr(100, SHA1)
        self.assertEqual(calls[0][0], ['gh', 'pr', 'merge', '100', '-R', 'o/r', '--merge',
                                       '--match-head-commit', SHA1])
        # No token: the merge runs with the local gh login, and the branch is kept.
        self.assertNotIn('env', calls[0][1])
        self.assertEqual(gh.pr_state(100), {'state': 'OPEN'})
        argv = calls[1][0]
        self.assertEqual(argv[:6], ['gh', 'pr', 'view', '100', '-R', 'o/r'])
        self.assertEqual(argv[argv.index('--json') + 1],
                         'state,headRefOid,mergeable,mergeStateStatus,labels')

    def test_commands(self):
        self.assertEqual(merge_command('/devloop hold aspetto la #182'), ('hold', 'aspetto la #182'))
        self.assertEqual(merge_command(header() + '\n/devloop hold motivo\naltro'),
                         ('hold', 'motivo'))
        self.assertEqual(merge_command('/devloop release'), ('release', ''))
        self.assertEqual(merge_command('/devloop hold'), ('hold', None))
        for body in ('/devloop holder x', 'nota\n/devloop hold x', '/devloop rereview', '', None):
            with self.subTest(body=body):
                self.assertIsNone(merge_command(body))


class OffTests(Case):
    def test_off_never_merges(self):
        d = self.make(CFG)
        snap = d.snapshot()
        self.assertIsNone(snap.prs[0].merge_mode)
        self.assertEqual(plan(snap, CFG), [])
        self.assertEqual(d.tick(), [])
        self.assertEqual((self.gh.merged, self.gh.comments), ([], []))
        self.assertEqual(self.gh.labels[100], {READY})

    def test_commands_are_ignored_and_logged_outside_auto(self):
        for cfg in (CFG, STRICT):
            with self.subTest(mode=cfg.merge.mode):
                d = self.make(cfg)
                self.gh.say('brushtyler', '/devloop hold aspetto')
                actions = d.tick()
                self.assertNotIn(HOLD, self.gh.labels[100])
                self.assertEqual(records(self.gh, 'hold'), [])
                self.assertTrue(any('ignored `/devloop hold`' in line and 'not "auto"' in line
                                    for line in self.logs))
                self.assertEqual(actions, [] if cfg is CFG else [MERGE])


class StrictTests(Case):
    def test_strict_merges_a_ready_pr_and_records_it(self):
        d = self.make()
        self.assertEqual(d.tick(), [MERGE])
        self.assertEqual(self.gh.merged, [(100, SHA1)])
        (record,) = records(self.gh, 'merged')
        self.assertEqual(record, {'head': SHA1, 'level': 1, 'mode': 'strict',
                                  'receipt': receipt()})
        body = self.gh.comments[-1][1]
        self.assertTrue(body.startswith('> [!NOTE]\n> **Agent:** devloop dispatcher'))
        self.assertIn('`--match-head-commit`', body)

    def test_the_merge_uses_its_own_client(self):
        merger = SimpleNamespace(merged=[])
        merger.merge_pr = lambda number, head: merger.merged.append((number, head))
        gh, ws = MergeGH(), FakeWS()
        d = Dispatcher(STRICT, gh, ws, FakeRunner(), log=lambda m: None,
                       identities=Identities(gh), merger=merger)
        d.tick()
        self.assertEqual((merger.merged, gh.merged), ([(100, SHA1)], []))

    def test_a_merge_takes_a_slot(self):
        def ready_pr(number):
            p = pr(number=number, issue=number, labels=[READY], reviews=[norm(raw_review())])
            p.mergeable, p.merge_status, p.merge_mode = 'MERGEABLE', 'CLEAN', 'strict'
            return p
        snap = Snapshot([ready_pr(100), ready_pr(101)], [], [])
        self.assertEqual([a.kind for a in plan(snap, STRICT)], ['merge', 'merge'])
        one = dataclasses.replace(STRICT, max_actions_per_tick=1)
        self.assertEqual([(a.kind, a.number) for a in plan(snap, one)], [('merge', 100)])

    def test_a_level_override_or_an_operator_only_path_leaves_the_merge_to_a_person(self):
        overridden = dataclasses.replace(
            STRICT, merge=MergePolicy(mode='strict', levels=((1, 'off'),)))
        for cfg, diff in ((overridden, ['x.py']), (STRICT, ['x.py', 'docs/adr/0002.md']),
                          (STRICT, ['AGENTS.md'])):
            with self.subTest(levels=cfg.merge.levels, diff=diff):
                ws = FakeWS()
                ws.diff = changes(*diff)
                d = self.make(cfg, ws=ws)
                self.assertEqual(d.snapshot().prs[0].merge_mode, 'off')
                self.assertEqual(d.tick(), [])
                self.assert_not_merged()


class AutoTests(Case):
    def test_the_supervisor_holds_and_releases(self):
        d = self.make(AUTO)
        command = self.gh.say(SUPERVISOR_LOGIN,
                              header() + '/devloop hold aspetto la decisione su #182')
        self.assertEqual(d.tick(), [Action('hold', 100, f'hold by {SUPERVISOR_LOGIN}')])
        self.assertEqual(self.gh.labels[100], {READY, HOLD})
        (record,) = records(self.gh, 'hold')
        self.assertEqual(record, {'event': 'hold', 'by': SUPERVISOR_LOGIN,
                                  'reason': 'aspetto la decisione su #182', 'head': SHA1,
                                  'comment': command})
        # The command is spent, and the held PR is not merged.
        self.assertIsNone(d.snapshot().prs[0].hold)
        self.assertEqual(d.tick(), [])
        self.assert_not_merged()
        self.gh.say(SUPERVISOR_LOGIN, header() + '/devloop release')
        self.assertEqual(d.tick(), [Action('hold', 100, f'release by {SUPERVISOR_LOGIN}')])
        self.assertEqual(self.gh.labels[100], {READY})
        self.assertEqual(records(self.gh, 'hold')[-1]['event'], 'release')
        self.assertEqual(d.tick(), [Action('merge', 100, 'merge mode auto')])
        self.assertEqual(self.gh.merged, [(100, SHA1)])
        self.assertEqual(records(self.gh, 'merged')[0]['mode'], 'auto')

    def test_a_trusted_user_may_hold_too(self):
        d = self.make(AUTO)
        self.gh.say('brushtyler', '/devloop hold controllo io')
        self.assertEqual([a.kind for a in d.tick()], ['hold'])
        self.assertIn(HOLD, self.gh.labels[100])
        self.assertEqual(d.tick(), [])
        self.assert_not_merged()

    def test_the_same_command_from_anyone_else_is_ignored(self):
        for login in ('stranger', 'codex-agent[bot]', 'someone[bot]'):
            with self.subTest(login=login):
                d = self.make(AUTO)
                self.gh.say(login, header() + '/devloop hold fermo tutto')
                self.assertEqual(d.tick(), [Action('merge', 100, 'merge mode auto')])
                self.assertNotIn(HOLD, self.gh.labels[100])
                self.assertEqual(records(self.gh, 'hold'), [])
                self.assertTrue(any(f'by {login}' in line and 'neither the supervisor' in line
                                    for line in self.logs))

    def test_a_release_from_anyone_else_keeps_the_hold(self):
        d = self.make(AUTO)
        self.gh.say(SUPERVISOR_LOGIN, header() + '/devloop hold aspetto')
        d.tick()
        self.gh.say('stranger', '/devloop release')
        self.assertEqual(d.tick(), [])
        self.assertIn(HOLD, self.gh.labels[100])
        self.assert_not_merged()

    def test_a_hold_without_a_reason_is_ignored(self):
        d = self.make(AUTO)
        self.gh.say(SUPERVISOR_LOGIN, header() + '/devloop hold')
        self.assertEqual([a.kind for a in d.tick()], ['merge'])
        self.assertTrue(any('malformed' in line for line in self.logs))

    def test_a_hold_written_after_the_snapshot_stops_the_merge(self):
        d = self.make(AUTO)
        p = d.snapshot().prs[0]
        self.assertEqual(plan(Snapshot([p], [], []), AUTO), [Action('merge', 100, 'merge mode auto')])
        self.gh.say(SUPERVISOR_LOGIN, header() + '/devloop hold aspetta')
        d._do_merge(p)
        self.assert_not_merged()

    def test_a_hold_written_while_a_release_is_applied_stays_to_apply(self):
        d = self.make(AUTO)
        self.gh.say(SUPERVISOR_LOGIN, header() + '/devloop hold aspetto')
        d.tick()
        release = self.gh.say(SUPERVISOR_LOGIN, header() + '/devloop release')
        p = d.snapshot().prs[0]
        self.assertEqual(p.hold['event'], 'release')
        # A new hold arrives after the snapshot, before the release is recorded.
        hold = self.gh.say(SUPERVISOR_LOGIN, header() + '/devloop hold di nuovo')
        d._do_hold(p)
        self.assertEqual(records(self.gh, 'hold')[-1]['comment'], release)
        self.assertNotIn(HOLD, self.gh.labels[100])
        # The record spends the release, not the hold written after it.
        self.assertEqual(d.tick(), [Action('hold', 100, f'hold by {SUPERVISOR_LOGIN}')])
        self.assertEqual(records(self.gh, 'hold')[-1]['comment'], hold)
        self.assertIn(HOLD, self.gh.labels[100])
        self.assertEqual(d.tick(), [])
        self.assert_not_merged()

    def test_commands_follow_the_merge_mode_of_the_pr(self):
        """With "auto" in [merge], a level override or an operator-only path that makes the PR
        strict or off makes its hold and release ignored, and logged."""
        strict = dataclasses.replace(
            AUTO, merge=MergePolicy(mode='auto', levels=((1, 'strict'),)))
        for cfg, diff, mode, merges in ((strict, ['x.py'], 'strict', True),
                                        (AUTO, ['x.py', 'AGENTS.md'], 'off', False)):
            with self.subTest(mode=mode):
                ws = FakeWS()
                ws.diff = changes(*diff)
                d = self.make(cfg, ws=ws)
                self.gh.say(SUPERVISOR_LOGIN, header() + '/devloop hold aspetto')
                actions = d.tick()
                self.assertNotIn(HOLD, self.gh.labels[100])
                self.assertEqual(records(self.gh, 'hold'), [])
                self.assertTrue(any('ignored `/devloop hold`' in line
                                    and f'merge mode of this PR is "{mode}"' in line
                                    for line in self.logs))
                self.assertEqual([a.kind for a in actions], ['merge'] if merges else [])


class DirectiveTests(Case):
    """Every directive blocks the merge in every mode, also when it changes between the
    snapshot and the merge."""

    MODES = (STRICT, AUTO)

    def ready(self, cfg):
        d = self.make(cfg)
        p = d.snapshot().prs[0]
        self.assertEqual([a.kind for a in plan(Snapshot([p], [], []), cfg)], ['merge'])
        return d, p

    def test_changes_after_the_snapshot_block_the_merge(self):
        changes_ = {
            'head': lambda gh, ws: setattr(gh, 'head', SHA2),
            'ci pending': lambda gh, ws: setattr(gh, 'ci', 'pending'),
            'ci fail': lambda gh, ws: setattr(gh, 'ci', 'fail'),
            'ci none': lambda gh, ws: setattr(gh, 'ci', 'none'),
            'blocked': lambda gh, ws: setattr(gh, 'status', 'BLOCKED'),
            'unstable': lambda gh, ws: setattr(gh, 'status', 'UNSTABLE'),
            'behind': lambda gh, ws: setattr(gh, 'status', 'BEHIND'),
            'conflicting': lambda gh, ws: setattr(gh, 'mergeable', 'CONFLICTING'),
            'needs-human': lambda gh, ws: gh.labels[100].add('agent:needs-human'),
            'merge-hold': lambda gh, ws: gh.labels[100].add(HOLD),
            'not ready': lambda gh, ws: gh.labels[100].discard(READY),
            'closed': lambda gh, ws: setattr(gh, 'state', 'CLOSED'),
        }
        for cfg in self.MODES:
            for name, change in changes_.items():
                with self.subTest(mode=cfg.merge.mode, change=name):
                    d, p = self.ready(cfg)
                    change(self.gh, self.ws)
                    d._do_merge(p)
                    self.assert_not_merged()

    def test_the_snapshot_plans_no_merge_when_a_directive_fails(self):
        changes_ = {
            'ci pending': lambda gh: setattr(gh, 'ci', 'pending'),
            'ci fail': lambda gh: setattr(gh, 'ci', 'fail'),
            'ci none': lambda gh: setattr(gh, 'ci', 'none'),
            'blocked': lambda gh: setattr(gh, 'status', 'BLOCKED'),
            'behind': lambda gh: setattr(gh, 'status', 'BEHIND'),
            'unknown': lambda gh: setattr(gh, 'mergeable', 'UNKNOWN'),
            'needs-human': lambda gh: gh.labels[100].add('agent:needs-human'),
            'merge-hold': lambda gh: gh.labels[100].add(HOLD),
        }
        for cfg in self.MODES:
            for name, change in changes_.items():
                with self.subTest(mode=cfg.merge.mode, change=name):
                    d = self.make(cfg)
                    change(self.gh)
                    self.assertNotIn('merge', [a.kind for a in d.tick()])
                    self.assert_not_merged()

    def test_a_head_that_is_not_the_approved_commit_is_revoked_not_merged(self):
        for cfg in self.MODES:
            with self.subTest(mode=cfg.merge.mode):
                d = self.make(cfg)
                self.gh.head = SHA2
                self.assertEqual([a.kind for a in d.tick()], ['revoke'])
                self.assert_not_merged()
                self.assertEqual(self.gh.labels[100], {'agent:review'})

    def test_a_receipt_that_no_longer_matches_is_stale(self):
        for cfg in self.MODES:
            with self.subTest(mode=cfg.merge.mode):
                d, p = self.ready(cfg)
                # The base advanced onto the PR's files since the approval.
                self.ws.base = 'e' * 40
                d._do_merge(p)
                self.assert_not_merged()
                self.assertEqual(records(self.gh, 'policy')[-1]['event'], 'stale')
                self.assertIn('Approvazione non valida per il merge', self.gh.comments[-1][1])
                self.assertEqual(self.gh.labels[100], {'agent:review'})

    def test_an_open_finding_blocks(self):
        ci = review.ci_failure_review(failed_checks=['Unit Tests'], log_tail='boom',
                                      previous_open=[], round_no=1, max_rounds=3, head_sha=SHA1)
        approval = norm(raw_review(), round_no=2)
        for cfg in self.MODES:
            with self.subTest(mode=cfg.merge.mode):
                d = self.make(cfg)
                self.gh.thread = []
                for record in (ci, approval):
                    self.gh.say('brushtyler', markdown.render_review(record, header(REV_NAME)))
                p = d.snapshot().prs[0]
                self.assertEqual([r['round'] for r in p.reviews], [1, 2])
                p.mergeable, p.merge_status, p.merge_mode = 'MERGEABLE', 'CLEAN', cfg.merge.mode
                self.assertEqual(plan(Snapshot([p], [], []), cfg), [])
                d._do_merge(p)
                self.assert_not_merged()
                self.assertEqual(self.gh.labels[100], {'agent:needs-human'})
                self.assertIn('1/ci/R1-1', self.gh.comments[-1][1])

    def test_reviews_written_after_the_snapshot_count(self):
        """The merge reads reviews, closures and policy records again: a finding or a
        revoked approval recorded after the snapshot stops it, with the same identity checks."""
        blocking = markdown.render_review(
            norm(raw_review('request_changes', blocking=[finding()]), round_no=2),
            header(REV_NAME))
        ci = markdown.render_review(
            review.ci_failure_review(failed_checks=['Unit Tests'], log_tail='boom',
                                     previous_open=[], round_no=2, max_rounds=3, head_sha=SHA1),
            header(REV_NAME))
        for cfg in self.MODES:
            for name, login, body in (('finding', 'brushtyler', blocking),
                                      ('ci finding', 'brushtyler', ci)):
                with self.subTest(mode=cfg.merge.mode, change=name):
                    d, p = self.ready(cfg)
                    self.gh.say(login, body)
                    d._do_merge(p)
                    self.assert_not_merged()
                    self.assertEqual(self.gh.labels[100], {'agent:needs-human'})
            with self.subTest(mode=cfg.merge.mode, change='new round not approved yet'):
                # A second round that has not approved the head: the approval no longer stands.
                d, p = self.ready(cfg)
                self.gh.say('brushtyler', markdown.render_review(
                    norm(raw_review(), round_no=2, head=SHA2), header(REV_NAME)))
                d._do_merge(p)
                self.assert_not_merged()
            with self.subTest(mode=cfg.merge.mode, change='stale receipt'):
                d, p = self.ready(cfg)
                self.gh.say('brushtyler', markdown.publishable(
                    markdown.dispatcher_header() + '\n## Ricevuta non valida',
                    'policy', {'event': 'stale', 'receipt': receipt()}))
                d._do_merge(p)
                self.assert_not_merged()
            with self.subTest(mode=cfg.merge.mode, change='review by an untrusted author'):
                # Someone else's review record is not a review: the merge goes ahead.
                d, p = self.ready(cfg)
                self.gh.say('stranger', blocking)
                d._do_merge(p)
                self.assertEqual(self.gh.merged, [(100, SHA1)])


MERGER_LOGIN = 'brushtyler-devloop-merger[bot]'


class StubApp:
    """An app as ``Identities`` uses it: a login and tokens, each request recorded."""

    def __init__(self, login, token):
        self.login, self._token, self.asked = login, token, []

    def token(self, permissions=None):
        self.asked.append(permissions)
        return self._token


class SimulatedGh:
    """``subprocess.run`` as ``gh`` sees it: the argv and the environment of every call."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw.get('env')))
        return SimpleNamespace(returncode=0, stdout='{}', stderr='')


class MergerIdentities(Identities):
    """The test identities, plus the merger's app (#201)."""

    def login(self, actor):
        return MERGER_LOGIN if actor == apps.MERGER else super().login(actor)


class MergerAppTests(unittest.TestCase):
    """Merges with the ``merger`` app (#201), or with the ambient login without it."""

    def merge_with(self, real_ids, identities):
        gh = MergeGH()
        d = Dispatcher(STRICT, gh, FakeWS(), FakeRunner(), log=lambda m: None,
                       identities=identities(gh), merger=real_ids.merger())
        self.assertEqual(d.tick(), [MERGE])
        return gh

    def test_with_the_merger_app_the_merge_runs_with_its_token(self):
        merger, dispatcher = StubApp(MERGER_LOGIN, 'merger-tok'), StubApp('d[bot]', 'disp-tok')
        run = SimulatedGh()
        ids = apps.Identities('o/r', {'merger': merger, 'dispatcher': dispatcher}, run=run)
        client = ids.merger()
        self.assertEqual(merger.asked, [])  # the token is asked for when the merge runs
        gh = self.merge_with(ids, MergerIdentities)
        (argv, env), = run.calls
        self.assertEqual(argv, ['gh', 'pr', 'merge', '100', '-R', 'o/r', '--merge',
                                '--match-head-commit', SHA1])
        self.assertEqual(env['GH_TOKEN'], 'merger-tok')
        # Only the permissions a merge needs, and no other app is asked for a token.
        self.assertEqual(merger.asked, [{'contents': 'write', 'pull_requests': 'write'}])
        self.assertEqual(apps.MERGE, {'contents': 'write', 'pull_requests': 'write'})
        self.assertEqual(dispatcher.asked, [])
        # The fake GitHub of the dispatcher did not merge: the merger's client did.
        self.assertEqual(gh.merged, [])
        (record,) = records(gh, 'merged')
        self.assertEqual(record['merged_by'], MERGER_LOGIN)
        self.assertIsInstance(client, GitHub)

    def test_without_the_merger_app_the_merge_uses_the_ambient_login(self):
        dispatcher = StubApp('d[bot]', 'disp-tok')
        run = SimulatedGh()
        ids = apps.Identities('o/r', {'dispatcher': dispatcher}, run=run)
        gh = self.merge_with(ids, Identities)
        (argv, env), = run.calls
        self.assertEqual(argv, ['gh', 'pr', 'merge', '100', '-R', 'o/r', '--merge',
                                '--match-head-commit', SHA1])
        self.assertIsNone(env)  # no GH_TOKEN: the local gh login, as before #201
        self.assertEqual(dispatcher.asked, [])
        # The record is the one of #172, without merged_by.
        (record,) = records(gh, 'merged')
        self.assertEqual(record, {'head': SHA1, 'level': 1, 'mode': 'strict',
                                  'receipt': receipt()})

    def test_build_hands_the_merger_client_to_the_dispatcher(self):
        merger = StubApp(MERGER_LOGIN, 'merger-tok')
        for configured in ({'merger': merger}, {}):
            with self.subTest(merger=bool(configured)), tempfile.TemporaryDirectory() as tmp:
                run = SimulatedGh()
                ids = apps.Identities(CFG.repo, configured, run=run)
                args = SimpleNamespace(config=None, local_config=None)
                with mock.patch.object(devloop_main, 'load_config', return_value=CFG), \
                        mock.patch.object(devloop_main, 'main_checkout', return_value=Path(tmp)), \
                        mock.patch.object(devloop_main.Identities, 'from_config',
                                          return_value=ids):
                    _, _, dispatcher = devloop_main.build(args)
                dispatcher.merger.merge_pr(7, SHA1)
                (argv, env), = run.calls
                self.assertEqual(argv[:4], ['gh', 'pr', 'merge', '7'])
                if configured:
                    self.assertEqual(env['GH_TOKEN'], 'merger-tok')
                else:
                    self.assertIsNone(env)

    def test_the_merger_is_not_an_agent(self):
        ids = apps.Identities('o/r', {'merger': StubApp(MERGER_LOGIN, 't'),
                                      'claude': StubApp('c[bot]', 't')})
        self.assertEqual(ids.agent_logins(), {'c[bot]'})
        self.assertEqual(ids.login('merger'), MERGER_LOGIN)

    def test_the_merger_is_configured_as_an_app_only(self):
        cfg = config.load_config()
        self.assertEqual(cfg.apps['merger'], config.AppConfig(5792929, 'brushtyler-devloop-merger'))
        self.assertNotIn('merger', cfg.agents)
        agents = dict(DATA['agents'], merger=DATA['agents']['codex'])
        with self.assertRaisesRegex(config.ConfigError, 'reserved'):
            config.parse_config(dict(DATA, agents=agents))
        without = {k: v for k, v in DATA['apps'].items() if k != 'merger'}
        self.assertNotIn('merger', config.parse_config(dict(DATA, apps=without)).apps)

    def test_a_merged_record_with_a_bad_signature_is_not_a_record(self):
        good = {'head': SHA1, 'level': 1, 'mode': 'strict', 'receipt': receipt()}
        text = lambda payload: markdown.publishable('x', 'merged', payload)
        self.assertEqual(markdown.read_marker(text(dict(good, merged_by=MERGER_LOGIN)), 'merged'),
                         dict(good, merged_by=MERGER_LOGIN))
        self.assertIsNone(markdown.read_marker(text(dict(good, merged_by=7)), 'merged'))


class MergerDocsTests(unittest.TestCase):
    def test_the_guide_documents_creating_and_configuring_the_merger_app(self):
        text = (Path(__file__).resolve().parents[1] / 'docs' / 'devloop.md').read_text()
        section = ' '.join(text.split('\n### The merger app\n', 1)[1].split('\n## ', 1)[0].split())
        for phrase in ('`Contents: Read and write`', '`Pull requests: Read and write`',
                       '`Metadata: Read-only`', 'Only select repositories', 'chmod 600',
                       '<slug>.<date>.private-key.pem', '[apps.merger]', 'app_id = 5792929',
                       'slug = "brushtyler-devloop-merger"', 'python3 -m devloop apps',
                       '`scripts/agent-token.py` refuses it', '`merged_by`',
                       'Without `[apps.merger]` the merges use the local `gh` login'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section)


class AgentTokenTests(unittest.TestCase):
    """``scripts/agent-token.py`` never exposes devloop's own apps (#201)."""

    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / 'scripts' / 'agent-token.py'
        spec = importlib.util.spec_from_file_location('agent_token', path)
        cls.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.script)

    def test_the_merger_and_the_dispatcher_are_refused(self):
        for actor in ('merger', 'MERGER', 'dispatcher'):
            with self.subTest(actor=actor), mock.patch.object(self.script, 'load_config') as load, \
                    mock.patch.object(self.script.subprocess, 'run') as run:
                with self.assertRaisesRegex(SystemExit, 'reserved to devloop'):
                    self.script.main([actor, '--', 'gh', 'pr', 'merge', '1'])
                load.assert_not_called()
                run.assert_not_called()

    def test_the_agents_are_still_accepted(self):
        self.assertEqual(self.script.parse(['codex', '--', 'gh', 'x']),
                         ('codex', {'issues': 'write', 'pull_requests': 'write'}, ['gh', 'x']))

    def test_only_the_actors_key_is_read(self):
        # The merger's key lives with devloop: an agent's token needs only the agent's key.
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / 'brushtyler-devloop-codex-agent.2026-11-17.private-key.pem'
            key.write_text('k')
            key.chmod(0o600)
            seen = {}

            def fake_run(command, env):
                seen.update(command=command, token=env['GH_TOKEN'])
                return SimpleNamespace(returncode=0)
            with mock.patch.dict(os.environ, {'DEVLOOP_APP_KEY_DIR': tmp}), \
                    mock.patch.object(self.script.AppAuth, 'token', return_value='codex-tok'), \
                    mock.patch.object(self.script.subprocess, 'run', fake_run):
                self.assertEqual(self.script.main(['codex', '--', 'gh', 'x']), 0)
            self.assertEqual(seen, {'command': ['gh', 'x'], 'token': 'codex-tok'})


class LevelZeroTests(unittest.TestCase):
    def promoted(self, cfg):
        gh = MergeGH()
        gh.thread = []
        gh.labels[100] = set(ZERO_LABELS)
        gh.zero_events, gh.runs = list(GRANT), list(SUCCESS)
        ws = FakeWS()
        ws.diff = changes('docs/a.txt')
        d = Dispatcher(cfg, gh, ws, FakeRunner(), log=lambda m: None)
        self.assertEqual(d.tick(), [Action('review', 100)])  # promoted without a review
        self.assertEqual(gh.labels[100], {'reviews:0', READY})
        return d, gh

    def test_a_level_zero_merge_needs_the_checks_of_its_promotion(self):
        cfg = dataclasses.replace(ZCFG, merge=MergePolicy(mode='strict'))
        d, gh = self.promoted(cfg)
        self.assertEqual(d.tick(), [MERGE])
        self.assertEqual(gh.merged, [(100, SHA1)])
        self.assertEqual(records(gh, 'merged')[0]['level'], 0)

    def test_a_failed_expected_check_revokes_instead_of_merging(self):
        cfg = dataclasses.replace(ZCFG, merge=MergePolicy(mode='strict'))
        d, gh = self.promoted(cfg)
        p = d.snapshot().prs[0]
        gh.runs = [dict(SUCCESS[0], conclusion='failure')]
        d._do_merge(p)
        self.assertEqual(gh.merged, [])
        self.assertEqual(gh.labels[100], {'reviews:0', 'agent:review'})

    def test_a_level_zero_override(self):
        cfg = dataclasses.replace(ZCFG, merge=MergePolicy(mode='strict', levels=((0, 'off'),)))
        d, gh = self.promoted(cfg)
        self.assertEqual(d.tick(), [])
        self.assertEqual(gh.merged, [])


if __name__ == '__main__':
    unittest.main()
