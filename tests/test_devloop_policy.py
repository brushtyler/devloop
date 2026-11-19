"""devloop: review level from a trusted policy, with a receipt (#106, part 1 of #66)."""

import dataclasses
import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

from devloop import config, markdown, policy
from devloop.dispatcher import Dispatcher
from devloop.planning import Action, Snapshot, counted_rounds, plan
from devloop.workspace import Workspace
from helpers import ssdlc_project
from helpers.devloop_fakes import FakeGH, SnapshotGH
from test_devloop import (BASE, CFG, SHA1, FakeRunner, FakeWS, finding, fixed, norm, pr,
                          raw_review, receipt)

POLICY = CFG.review_policy
DATA = ssdlc_project.data()
# The ssdlc modules that decide permissions, fencing and authorizations (#171).
SECURITY_MODULES = {
    'ssdlc_writer_gate.py', 'ssdlc_role_fencing.py', 'ssdlc_container_holder.py',
    'ssdlc_locks.py', 'ssdlc_fencing_proof.py', 'ssdlc_transitions.py', 'ssdlc_replay_gate.py', 'ssdlc_grader.py',
    'ssdlc_feedback_delegation.py', 'ssdlc_feedback_admission.py', 'ssdlc_usage_admission.py',
    'scripts/agent-token.py', 'scripts/supervisor-token.py'}


def changes(*paths, links=None):
    return [{'path': p, 'links': list((links or {}).get(p, ()))} for p in paths]


def published(gh, number=100):
    return [body for n, body in gh.comments if n == number]


def policy_records(gh, event=None):
    records = [markdown.read_marker(body, 'policy') for body in published(gh)]
    return [r for r in records if r and (event is None or r['event'] == event)]


class PolicyConfigTests(unittest.TestCase):
    def test_default_policy_lists_the_sensitive_paths(self):
        self.assertEqual(POLICY.default_level, 1)
        self.assertEqual(POLICY.label, 'reviews:2')
        self.assertEqual(set(POLICY.sensitive_paths),
                         {'devloop/', 'AGENTS.md', 'CLAUDE.md', '.github/', 'schemas/'}
                         | SECURITY_MODULES)
        baseline = json.loads((config.INSTALL_ROOT / 'schemas/recovery-grader-baseline.json')
                              .read_text())
        self.assertEqual(set(POLICY.sensitive_files), set(baseline['files']))
        self.assertIn('ssdlc_grader.py', POLICY.sensitive_files)
        # Without the table the code defaults apply: no sensitive path and no list, since they
        # are restrictions of the managed repository that its project file adds (ADR 0006, R3).
        self.assertEqual(policy.ReviewPolicy.sensitive_paths, ())
        self.assertEqual(config.DEFAULT_SENSITIVE_LISTS, ())
        omitted = {k: v for k, v in DATA.items() if k != 'review_policy'}
        self.assertEqual(config.parse_config(omitted).review_policy,
                         dataclasses.replace(POLICY, sensitive_paths=(), sensitive_files=()))

    def test_policy_version_is_the_hash_of_the_loaded_policy(self):
        self.assertRegex(POLICY.version(), r'^[0-9a-f]{64}$')
        self.assertEqual(config.parse_config(DATA).review_policy.version(), POLICY.version())
        weaker = dict(DATA, review_policy=dict(DATA['review_policy'], sensitive_paths=['.github/']))
        self.assertNotEqual(config.parse_config(weaker).review_policy.version(), POLICY.version())
        relabelled = dict(DATA, review_policy=dict(DATA['review_policy'], label='two-reviews'))
        self.assertNotEqual(config.parse_config(relabelled).review_policy.version(),
                            POLICY.version())

    def test_invalid_policies_stop_devloop(self):
        table = DATA['review_policy']
        for change in ({'default_level': 0}, {'default_level': 3}, {'default_level': True},
                       {'default_level': '1'}, {'label': ''}, {'sensitive_paths': 'devloop/'},
                       {'sensitive_path_lists': ['missing.json']}):
            with self.subTest(change=change), self.assertRaises(config.ConfigError):
                config.parse_config(dict(DATA, review_policy=dict(table, **change)))

    def test_path_lists_come_from_the_running_installation(self):
        # The list is read from the dispatcher's root, not from a PR branch. That root holds no
        # documents here: [docs] map (#203, which needs them) has its own tests.
        data = {k: v for k, v in DATA.items() if k != 'docs'}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'schemas').mkdir()
            (Path(tmp) / 'schemas/recovery-grader-baseline.json').write_text(
                json.dumps({'files': {'core.py': 'x'}}))
            self.assertEqual(config.parse_config(data, root=tmp).review_policy.sensitive_files,
                             ('core.py',))
            (Path(tmp) / 'schemas/recovery-grader-baseline.json').write_text('{"files": []}')
            with self.assertRaises(config.ConfigError):
                config.parse_config(data, root=tmp)


class LevelTests(unittest.TestCase):
    def level(self, paths=('x.py',), links=None, labelers=(), previous=0, pol=POLICY):
        return policy.evaluate(pol, pr=100, base=BASE, head=SHA1,
                               changes=changes(*paths, links=links), labelers=list(labelers),
                               trusted_users=CFG.trusted_users, previous_level=previous,
                               select=lambda level: ['codex'] if level == 1 else [])

    def test_sensitive_paths(self):
        for path in ('devloop/dispatcher.py', 'AGENTS.md', 'CLAUDE.md', '.github/workflows/t.yml',
                     'schemas/plan.schema.json', 'ssdlc_grader.py', 'tests/replay/benchmark.py'):
            with self.subTest(path=path):
                self.assertTrue(POLICY.covers(path))
        for path in ('devloopx/a.py', 'docs/AGENTS.md', 'README.md', 'tests/test_devloop.py',
                     'ssdlc_grader.py.orig'):
            with self.subTest(path=path):
                self.assertFalse(POLICY.covers(path))

    def test_level_one_by_default(self):
        result = self.level()
        self.assertEqual((result['level'], result['reviewers']), (1, ['codex']))
        self.assertEqual(result['reasons'], [{'kind': 'default', 'level': 1}])
        self.assertEqual(result['policy'], POLICY.version())

    def test_security_modules_get_level_two(self):
        # #171: the code that decides permissions, fencing and authorizations needs two reviews.
        for path in sorted(SECURITY_MODULES):
            with self.subTest(path=path):
                result = self.level((path, 'docs/devloop.md'))
                self.assertEqual(result['level'], 2)
                self.assertEqual(result['reasons'][-1]['kind'], 'paths')
        # A listed file that does not exist in the tree is just a path, never an error.
        missing = 'ssdlc_not_yet_merged.py'
        self.assertFalse((config.INSTALL_ROOT / missing).exists())
        table = dict(DATA['review_policy'],
                     sensitive_paths=DATA['review_policy']['sensitive_paths'] + [missing])
        loaded = config.parse_config(dict(DATA, review_policy=table)).review_policy
        self.assertEqual(self.level((missing,), pol=loaded)['level'], 2)
        # Other ssdlc modules stay at level 1.
        result = self.level(('ssdlc_impact.py',))
        self.assertEqual((result['level'], result['reviewers']), (1, ['codex']))
        self.assertEqual(result['reasons'], [{'kind': 'default', 'level': 1}])

    def test_deletions_renames_and_symlinks_count(self):
        # A deletion and both sides of a rename are paths of the diff (renames are not detected).
        for paths in (('AGENTS.md',), ('schemas/plan.schema.json', 'docs/plan.schema.json'),
                      ('docs/old.md', 'devloop/new.md')):
            with self.subTest(paths=paths):
                result = self.level(paths)
                self.assertEqual(result['level'], 2)
                self.assertEqual(result['reviewers'], [])
                self.assertEqual(result['reasons'][-1]['kind'], 'paths')
        for target, level in (('../devloop/config.toml', 2), ('..', 2), ('/etc/passwd', 2),
                              ('../../outside', 2), ('../README.md', 1), ('sibling.py', 1)):
            with self.subTest(target=target):
                result = self.level(('docs/link',), links={'docs/link': [target]})
                self.assertEqual(result['level'], level)

    def test_trusted_label_raises_and_history_keeps_it(self):
        self.assertEqual(self.level(labelers=['stranger'])['level'], 1)
        raised = self.level(labelers=['brushtyler'])
        self.assertEqual(raised['level'], 2)
        self.assertIn({'kind': 'label', 'level': 2, 'label': 'reviews:2', 'by': 'brushtyler'},
                      raised['reasons'])
        # Re-added by someone untrusted after a trusted user: the trusted event still counts.
        self.assertEqual(self.level(labelers=['brushtyler', 'stranger'])['level'], 2)

    def test_levels_never_go_down_nor_below_one(self):
        self.assertEqual(self.level(previous=2)['level'], 2)
        self.assertEqual(self.level(previous=2)['reasons'][-1], {'kind': 'previous', 'level': 2})
        self.assertEqual(self.level(previous=0)['level'], 1)
        zero = dataclasses.replace(POLICY, default_level=0)
        self.assertEqual(self.level(pol=zero)['level'], 1)

    def test_receipts_bind_base_policy_level_and_reviewers(self):
        current = receipt()
        self.assertEqual(policy.differences(receipt(), current), [])
        self.assertEqual(policy.differences(None, current), list(policy.BINDING))
        self.assertEqual(policy.differences(receipt(base='e' * 40), current), ['base'])
        self.assertEqual(policy.differences(dict(current, policy='0' * 64), current), ['policy'])
        self.assertEqual(policy.differences(dict(current, reviewers=['agy']), current),
                         ['reviewers'])
        self.assertEqual(policy.differences(receipt(files=['AGENTS.md']), current),
                         ['level', 'reviewers', 'profiles'])
        changed = [dict(current['profiles'][0], model='other')]
        self.assertEqual(policy.differences(dict(current, profiles=changed), current),
                         ['profiles'])
        # A receipt from before profiles were bound keeps its level but matches nothing.
        legacy = {k: v for k, v in current.items() if k != 'profiles'}
        self.assertEqual(policy.receipt_of({'receipt': legacy}), legacy)
        self.assertEqual(policy.differences(legacy, current), ['profiles'])
        # A record without a receipt, or with a malformed one, has none.
        self.assertIsNone(policy.receipt_of(norm(raw_review(), bound=False)))
        self.assertIsNone(policy.receipt_of({'receipt': dict(current, level=5)}))
        self.assertEqual(policy.recorded_level([norm(raw_review()), {'receipt': receipt(
            files=['AGENTS.md'])}]), 2)


class PolicyDispatcherTests(unittest.TestCase):
    def make(self, runner=None, ws=None, cfg=CFG):
        self.gh, self.ws = FakeGH(), ws or FakeWS()
        self.runner = runner or FakeRunner()
        return Dispatcher(cfg, self.gh, self.ws, self.runner, log=lambda m: None)

    def assert_escalated_at_level_2(self, why):
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        body = self.gh.comments[-1][1]
        self.assertTrue(body.startswith('> [!NOTE]\n> **Bot:** devloop dispatcher'))
        self.assertIn('livello 2', body)
        self.assertIn(why, body)
        record = markdown.read_marker(body, 'policy')
        self.assertEqual((record['event'], record['receipt']['level']), ('level', 2))

    def test_sensitive_diff_goes_to_a_human_without_review(self):
        for diff, why in ((changes('x.py', 'AGENTS.md'), '`AGENTS.md`'),           # deletion
                          (changes('schemas/a.json', 'docs/a.json'), '`schemas/a.json`'),  # rename
                          (changes('link', links={'link': ['devloop/config.toml']}), '`link`')):
            with self.subTest(diff=diff):
                ws = FakeWS()
                ws.diff = diff
                d = self.make(FakeRunner(raw_review()), ws)
                p = pr()
                self.gh.labels[100] = set(p.labels)
                d._do_review(p)
                self.assert_escalated_at_level_2(why)

    def test_trusted_level_label_raises_the_level_untrusted_is_ignored(self):
        d = self.make(FakeRunner(raw_review()))
        self.gh.level_labelers = ['stranger']
        p = pr()
        self.gh.labels[100] = set(p.labels)
        d._do_review(p)
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        recorded = markdown.read_marker(self.gh.comments[0][1], 'review')['receipt']
        self.assertEqual((recorded['level'], recorded['reviewers']), (1, [CFG.reviewer]))
        # The label history, not the current labels: a removed label still counts.
        for history in (['brushtyler'], ['brushtyler', 'stranger']):
            with self.subTest(history=history):
                d = self.make(FakeRunner(raw_review()))
                self.gh.level_labelers = history
                self.gh.labels[100] = set(p.labels)
                d._do_review(p)
                self.assert_escalated_at_level_2('Label `reviews:2` messa da `brushtyler`')

    def test_level_one_promotion_records_a_receipt(self):
        d = self.make(FakeRunner(raw_review()))
        p = pr()
        self.gh.labels[100] = set(p.labels)
        d._do_review(p)
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        review_body, promotion = published(self.gh)
        self.assertIn('**Livello di review:** 1', review_body)
        self.assertEqual(markdown.read_marker(review_body, 'review')['receipt'], receipt())
        record = markdown.read_marker(promotion, 'policy')
        self.assertEqual((record['event'], record['receipt']), ('promoted', receipt()))
        self.assertTrue(promotion.startswith('> [!NOTE]\n> **Bot:** devloop dispatcher'))

    def test_ci_round_records_a_receipt(self):
        d = self.make()
        d._do_ci_failed(pr(checks='fail'))
        self.assertEqual(markdown.read_marker(self.gh.comments[0][1], 'review')['receipt'],
                         receipt())
        self.assertEqual(self.gh.labels[100], {'agent:changes'})

    def test_changed_receipt_needs_a_new_review(self):
        cases = {'base': ({'base': 'e' * 40}, CFG, 'commit di base'),
                 'policy': ({}, dataclasses.replace(CFG, review_policy=dataclasses.replace(
                     POLICY, sensitive_paths=('devloop/',))), 'versione della policy'),
                 'reviewers': ({}, dataclasses.replace(CFG, reviewer='agy', reviewers=('agy',)),
                               'reviewer richiesti')}
        for case, (ws_change, cfg, why) in cases.items():
            with self.subTest(case=case):
                ws = FakeWS()
                vars(ws).update(ws_change)
                d = self.make(ws=ws, cfg=cfg)
                approval = norm(raw_review())
                p = pr(reviews=[approval])
                p.meta = dict(p.meta, reviewer='gone')  # the configured reviewer reviews
                self.gh.labels[100] = set(p.labels)
                self.assertEqual(plan(Snapshot([p], [], []), cfg), [
                    Action('apply_review', 100, 'approval already recorded')])
                d._do_apply_review(p)
                self.assertEqual(self.gh.labels[100], {'agent:review'})
                (stale,) = policy_records(self.gh)
                self.assertEqual(stale['event'], 'stale')
                self.assertIn(why, published(self.gh)[-1])

    def test_stale_or_missing_receipt_is_not_replayed(self):
        legacy = norm(raw_review(), bound=False)
        self.assertEqual(plan(Snapshot([pr(reviews=[legacy])], [], []), CFG),
                         [Action('review', 100, 'the approval has no matching policy receipt')])
        stale = pr(reviews=[norm(raw_review())])
        stale.stale_receipt = True
        self.assertEqual([a.kind for a in plan(Snapshot([stale], [], []), CFG)], ['review'])
        # Rounds and open findings are unchanged: the new review is an ordinary round.
        rounds = [dict(norm(raw_review(), round_no=i), round=i) for i in (1, 2, 3)]
        self.assertEqual([a.kind for a in plan(Snapshot([pr(reviews=rounds[:2] + [
            dict(legacy, round=3)])], [], []), CFG)], ['round_limit'])
        # A legacy approval reaching promotion is refused too.
        d = self.make()
        self.gh.labels[100] = {'agent:review'}
        d._do_apply_review(pr(reviews=[legacy]))
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.assertIn('non ha una ricevuta', published(self.gh)[-1])


class StickyLevelTests(unittest.TestCase):
    """The level survives the label's removal and a narrower diff, through the records."""

    def make(self, ws, runner):
        self.gh = SnapshotGH([])
        self.gh.labels[100] = {'agent:review'}
        self.ws, self.runner = ws, runner
        return Dispatcher(CFG, self.gh, ws, runner, log=lambda m: None)

    def test_a_fix_that_widens_the_diff_raises_the_level_for_good(self):
        ws = FakeWS()
        d = self.make(ws, FakeRunner(raw_review('request_changes', [finding()]), fixed()))
        self.assertEqual(d.tick(), [Action('review', 100)])            # round 1, level 1
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        self.assertEqual(d.tick(), [Action('fix', 100)])
        # The fix touches the core: the next review stops at level 2.
        ws.diff = changes('x.py', 'devloop/dispatcher.py')
        snap = d.snapshot()
        snap.prs[0].head_sha = ws._head
        d.snapshot = lambda: snap
        self.assertEqual(d.tick(), [Action('review', 100)])
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        self.assertIn('`devloop/dispatcher.py`', published(self.gh)[-1])
        del d.snapshot
        # The operator resumes the PR and the core change is gone: the level stays 2.
        ws.diff = changes('x.py')
        self.gh.labels[100] = {'agent:review'}
        snap = d.snapshot()
        self.assertEqual(policy.recorded_level(snap.prs[0].reviews + snap.prs[0].policy), 2)
        d._do_review(dataclasses.replace(snap.prs[0], head_sha=ws._head))
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        self.assertIn('già richiesto in precedenza', published(self.gh)[-1])
        self.assertEqual(len(self.runner.calls), 2)  # the first review and the fix only

    def test_a_removed_label_does_not_lower_the_level(self):
        d = self.make(FakeWS(), FakeRunner(raw_review()))
        self.gh.level_labelers = ['brushtyler']
        self.assertEqual(d.tick(), [Action('review', 100)])
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        # Even if the events were no longer visible, the recorded level holds.
        self.gh.level_labelers = []
        self.gh.labels[100] = {'agent:review'}
        d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        self.assertEqual(self.runner.calls, [])
        self.assertEqual([r['receipt']['level'] for r in policy_records(self.gh)], [2, 2])

    def test_a_stale_receipt_voids_the_recorded_approval(self):
        ws = FakeWS()
        d = self.make(ws, FakeRunner(raw_review(), raw_review()))
        d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        # CI re-runs, the PR goes back to review; meanwhile the base advanced.
        self.gh.ci = 'pending'
        self.assertEqual([a.kind for a in d.tick()], ['revoke'])
        self.gh.ci, ws.base = 'pass', 'e' * 40
        self.assertEqual(d.tick(), [Action('apply_review', 100, 'approval already recorded')])
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.assertTrue(d.snapshot().prs[0].stale_receipt)
        # The base touched the PR's files: the review of the same head is free (#252).
        self.assertEqual(d.tick(), [Action('review', 100, 'the approval was revoked because '
                                                          'the base touched the PR')])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        reviews = d.snapshot().prs[0].reviews
        self.assertEqual([r['round'] for r in reviews], [1, 2])
        self.assertEqual(reviews[-1]['receipt']['base'], 'e' * 40)
        self.assertEqual(counted_rounds(reviews), 1)


class ReviewDiffTests(unittest.TestCase):
    def git(self, *args, cwd):
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@x',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@x')
        return subprocess.run(['git', *args], cwd=cwd, env=env, capture_output=True, text=True,
                              check=True).stdout.strip()

    def test_full_diff_against_the_base_with_deletions_renames_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin, repo = Path(tmp) / 'origin.git', Path(tmp) / 'repo'
            self.git('init', '-q', '--bare', str(origin), cwd=tmp)
            self.git('init', '-q', '-b', 'main', str(repo), cwd=tmp)
            for name, text in (('AGENTS.md', 'a\n'), ('schemas/s.json', '{}\n'),
                               ('keep.py', 'k\n'), ('old_link', None)):
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                if text is None:
                    path.symlink_to('keep.py')
                else:
                    path.write_text(text)
            self.git('add', '-A', cwd=repo)
            self.git('commit', '-q', '-m', 'base', cwd=repo)
            self.git('remote', 'add', 'origin', str(origin), cwd=repo)
            self.git('push', '-q', 'origin', 'main', cwd=repo)
            self.git('checkout', '-q', '-b', 'agent/issue-7', cwd=repo)
            (repo / 'AGENTS.md').unlink()                                     # deletion
            self.git('mv', 'schemas/s.json', 'docs.json', cwd=repo)          # rename
            (repo / 'new_link').symlink_to('devloop/config.toml')            # symlink
            (repo / 'old_link').unlink()
            (repo / 'old_link').symlink_to('README.md')                      # retargeted
            self.git('add', '-A', cwd=repo)
            self.git('commit', '-q', '-m', 'pr', cwd=repo)
            self.git('push', '-q', 'origin', 'agent/issue-7', cwd=repo)
            # main advances elsewhere: its change is not the PR's.
            self.git('checkout', '-q', 'main', cwd=repo)
            (repo / 'CLAUDE.md').write_text('c\n')
            self.git('add', '-A', cwd=repo)
            self.git('commit', '-q', '-m', 'main', cwd=repo)
            self.git('push', '-q', 'origin', 'main', cwd=repo)
            ws = Workspace(repo, '.state', 'main')
            base, head, diff = ws.review_diff('agent/issue-7')
            self.assertEqual(base, self.git('rev-parse', 'main', cwd=repo))
            self.assertEqual(head, self.git('rev-parse', 'agent/issue-7', cwd=repo))
            by_path = {c['path']: c['links'] for c in diff}
            self.assertEqual(by_path, {'AGENTS.md': [], 'schemas/s.json': [], 'docs.json': [],
                                       'new_link': ['devloop/config.toml'],
                                       'old_link': ['keep.py', 'README.md']})
            self.assertEqual(policy.sensitive_changes(POLICY, diff),
                             ['AGENTS.md', 'new_link', 'schemas/s.json'])


if __name__ == '__main__':
    unittest.main()
