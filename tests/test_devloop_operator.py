"""devloop: agent:needs-operator, the new name of the label that hands an issue or a PR to the
operator, also for the ready PRs whose merge is the operator's (#248)."""

import dataclasses
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import devloop.__main__ as cli
from devloop import markdown, review
from devloop.schema import load_schema
from devloop.config import LEGACY_NEEDS_OPERATOR, Labels, MergePolicy
from devloop.dispatcher import (Action, Dispatcher, Snapshot, Workers, ensure_labels,
                                label_transition, plan)
from devloop.github import GitHub, GitHubError
from helpers.devloop_fakes import FakeGH
from test_devloop import (CFG, REV_NAME, SHA1, SHA2, FakeRunner, FakeWS, norm, pr, raw_review,
                          ready)
from test_devloop_merge import AUTO, MergeGH, header, records
from test_devloop_merge import Identities as MergeIdentities
import test_devloop_preflight as preflight
from test_devloop_preflight import PCFG, READY as ISSUE_READY, checked
from test_devloop_zero import changes

ROOT = Path(__file__).resolve().parents[1]
OPERATOR, READY, REVIEW = 'agent:needs-operator', 'agent:ready-to-merge', 'agent:review'
OLD = LEGACY_NEEDS_OPERATOR
# The old names, written so that this file does not contain them.
OLD_NAMES = re.compile('needs' + r'[-_]' + 'human', re.IGNORECASE)
TEXT = ('.py', '.md', '.toml', '.json', '.sh', '.txt', '.yml', '.yaml', '.cfg', '.ini')


def transition_section(lines):
    """The line numbers of the section of docs/devloop/guide.md on the rename (#248)."""
    start = next(i for i, line in enumerate(lines)
                 if line.startswith('### From `agent:needs-') and OLD_NAMES.search(line))
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith('#')), len(lines))
    return range(start, end)


def allowed(relative, lines, number):
    """Whether the old name may stay on line ``number`` of ``relative``: the compatibility of
    #248, and nothing else."""
    line = lines[number]
    if relative == 'devloop/config.py':
        return line.startswith('LEGACY_NEEDS_OPERATOR = ')
    if relative == 'devloop/review.py':
        return line.startswith('LEGACY_VERDICT = ')
    if relative == 'docs/devloop/guide.md':
        return number in transition_section(lines)
    if relative == 'tests/fixtures/devloop_split.json':
        # The ledger of the split names that section of the guide.
        return '"Usage > From `agent:needs-' in line
    return False


class NoOldNameTests(unittest.TestCase):
    def files(self):
        """The repository's text files: code, configuration, documents and tests. Hidden
        directories (git, the state of devloop) are not the repository's content."""
        for path in sorted(ROOT.rglob('*')):
            parts = path.relative_to(ROOT).parts
            if any(p.startswith('.') and p != '.github' or p == '__pycache__' for p in parts):
                continue
            if path.is_file() and (path.suffix in TEXT or path.name == 'Dockerfile'):
                yield path

    def test_the_old_name_is_left_only_where_compatibility_needs_it(self):
        found = []
        for path in self.files():
            relative = path.relative_to(ROOT).as_posix()
            lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
            for number, line in enumerate(lines):
                if OLD_NAMES.search(line) and not allowed(relative, lines, number):
                    found.append(f'{relative}:{number + 1}: {line.strip()[:100]}')
        self.assertEqual(found, [])
        # The compatibility itself is still there.
        self.assertEqual((OLD, review.LEGACY_VERDICT), ('agent:needs-' + 'human', 'needs_' + 'human'))

    def test_the_label_and_the_verdict_are_named_after_the_operator(self):
        self.assertEqual(Labels().needs_operator, OPERATOR)
        self.assertIn(OPERATOR, Labels().all())
        self.assertNotIn(OLD, Labels().all())
        self.assertEqual(Labels().operator(), {OPERATOR, OLD})
        self.assertIn('needs_operator', review.VERDICTS)
        self.assertEqual(load_schema('review')['properties']['verdict']['enum'],
                         list(review.VERDICTS))


class SetupTests(unittest.TestCase):
    def make(self, existing):
        gh = FakeGH()
        gh.existing_labels = list(existing)
        gh.labels = {7: {'agent:ready', OLD}, 100: {REVIEW, OLD}}
        return gh

    def test_setup_renames_the_old_label_and_issues_and_prs_keep_it(self):
        gh = self.make([OLD, 'agent:ready'])
        note = ensure_labels(gh, CFG.labels, CFG.review_policy)
        self.assertEqual(gh.renamed_labels, [(OLD, OPERATOR)])
        self.assertEqual(gh.labels, {7: {'agent:ready', OPERATOR}, 100: {REVIEW, OPERATOR}})
        self.assertEqual(gh.label_styles[OPERATOR],
                         ('b60205', 'devloop: serve una decisione dell\'operatore'))
        self.assertNotIn(OLD, gh.label_styles)
        self.assertIn('renamed', note)
        # Once renamed, nothing is left to rename.
        self.assertIsNone(label_transition(gh, CFG.labels))
        self.assertEqual(ensure_labels(gh, CFG.labels, CFG.review_policy), '')
        self.assertEqual(gh.renamed_labels, [(OLD, OPERATOR)])

    def test_with_both_labels_setup_touches_neither_and_says_so(self):
        gh = self.make([OLD, OPERATOR])
        note = ensure_labels(gh, CFG.labels, CFG.review_policy)
        self.assertFalse(hasattr(gh, 'renamed_labels'))
        self.assertEqual(gh.labels, {7: {'agent:ready', OLD}, 100: {REVIEW, OLD}})
        self.assertNotIn(OPERATOR, gh.label_styles)
        self.assertNotIn(OLD, gh.label_styles)
        self.assertIn(REVIEW, gh.label_styles)  # the other labels as usual
        self.assertIn(f'both {OLD} and {OPERATOR} exist', note)

    def test_a_new_repository_gets_the_new_label(self):
        gh = self.make([])
        self.assertEqual(ensure_labels(gh, CFG.labels, CFG.review_policy), '')
        self.assertIn(OPERATOR, gh.label_styles)
        self.assertFalse(hasattr(gh, 'renamed_labels'))

    def test_the_adapter_renames_by_editing_the_label(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout='[{"name": "x"}]', stderr='')
        gh = GitHub('o/r', run=fake_run)
        gh.rename_label(OLD, OPERATOR, 'b60205', 'd')
        self.assertEqual(calls[0], ['gh', 'label', 'edit', OLD, '-R', 'o/r', '--name', OPERATOR,
                                    '--color', 'b60205', '--description', 'd'])
        self.assertEqual(gh.repo_labels(), ['x'])
        self.assertEqual(calls[1][:5], ['gh', 'label', 'list', '-R', 'o/r'])


class CommandTests(unittest.TestCase):
    """``devloop setup`` and ``devloop status`` report the transition in their log."""

    def run_command(self, command, existing):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        gh = FakeGH()
        gh.existing_labels = list(existing)
        gh.labels = {7: {OLD}}
        d = Dispatcher(CFG, gh, FakeWS(), FakeRunner(), log=lambda m: None)
        logged = []
        with mock.patch.object(cli, 'build', return_value=(CFG, tmp, d)), \
                mock.patch.object(cli, 'log', logged.append):
            cli.main([command])
        return gh, logged

    def test_setup_logs_the_rename(self):
        gh, logged = self.run_command('setup', [OLD])
        self.assertEqual(gh.labels[7], {OPERATOR})
        self.assertTrue(any(line.startswith(f'labels: renamed {OLD} to {OPERATOR}')
                            for line in logged), logged)

    def test_setup_and_status_warn_while_both_labels_exist(self):
        for command in ('setup', 'status'):
            with self.subTest(command=command):
                gh, logged = self.run_command(command, [OLD, OPERATOR])
                self.assertEqual(gh.labels[7], {OLD})
                self.assertTrue(any(line.startswith(f'labels: both {OLD} and {OPERATOR} exist')
                                    for line in logged), logged)
        _, logged = self.run_command('status', [OPERATOR])
        self.assertFalse(any(line.startswith('labels:') for line in logged), logged)


class TickTransitionTests(unittest.TestCase):
    """The first tick of a process renames the old label as ``devloop setup`` does, in
    ``devloop tick`` and in the continuous mode of ``devloop run``; a dry run only says so."""

    def make(self, existing, workers=None):
        self.gh = FakeGH()
        self.gh.existing_labels = list(existing)
        self.gh.labels = {7: {OLD}, 100: {READY, OLD}}
        self.logs = []
        d = Dispatcher(CFG, self.gh, FakeWS(), FakeRunner(), log=self.logs.append)
        d.workers = workers
        return d

    def renamed(self):
        return any(line.startswith(f'labels: renamed {OLD} to {OPERATOR}') for line in self.logs)

    def test_the_first_tick_renames_the_old_label(self):
        for workers in (None, Workers(2)):
            with self.subTest(continuous=workers is not None):
                d = self.make([OLD], workers)
                d.tick()
                self.assertEqual(self.gh.renamed_labels, [(OLD, OPERATOR)])
                self.assertEqual(self.gh.labels, {7: {OPERATOR}, 100: {READY, OPERATOR}})
                self.assertEqual(self.gh.label_styles[OPERATOR][1],
                                 'devloop: serve una decisione dell\'operatore')
                self.assertTrue(self.renamed(), self.logs)
                # Once per process: the next ticks do not read the labels again.
                with mock.patch.object(self.gh, 'repo_labels',
                                       side_effect=AssertionError('read again')):
                    d.tick()

    def test_with_both_labels_the_tick_touches_neither_and_says_so(self):
        d = self.make([OLD, OPERATOR])
        d.tick()
        self.assertFalse(hasattr(self.gh, 'renamed_labels'))
        self.assertEqual(self.gh.labels, {7: {OLD}, 100: {READY, OLD}})
        self.assertTrue(any(line.startswith(f'labels: both {OLD} and {OPERATOR} exist')
                            for line in self.logs), self.logs)

    def test_a_dry_run_only_says_what_it_would_rename(self):
        d = self.make([OLD])
        d.tick(dry_run=True)
        self.assertFalse(hasattr(self.gh, 'renamed_labels'))
        self.assertEqual(self.gh.labels[7], {OLD})
        self.assertIn(f'[dry-run] labels: would rename {OLD} to {OPERATOR}', self.logs)
        # The real tick that follows still renames it.
        d.tick()
        self.assertEqual(self.gh.renamed_labels, [(OLD, OPERATOR)])

    def test_nothing_to_rename_says_nothing(self):
        d = self.make([OPERATOR])
        d.tick()
        self.assertFalse(hasattr(self.gh, 'renamed_labels'))
        self.assertFalse(any(line.startswith('labels:') for line in self.logs), self.logs)

    def test_a_failed_rename_is_tried_again_at_the_next_tick(self):
        d = self.make([OLD])
        with mock.patch.object(self.gh, 'rename_label', side_effect=GitHubError('boom')):
            d.tick()
        self.assertEqual(self.gh.labels[7], {OLD})
        self.assertTrue(any('could not be checked' in line for line in self.logs), self.logs)
        d.tick()
        self.assertEqual(self.gh.renamed_labels, [(OLD, OPERATOR)])

    def test_devloop_tick_runs_the_transition(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        d = self.make([OLD])
        with mock.patch.object(cli, 'build', return_value=(CFG, tmp, d)):
            cli.main(['tick'])
        self.assertEqual(self.gh.renamed_labels, [(OLD, OPERATOR)])


class LegacyRecordTests(unittest.TestCase):
    """Records and verdicts published before #248 are read as before."""

    def legacy_review(self):
        record = norm(raw_review('needs_operator', human='Una decisione di prodotto.'))
        record['verdict'] = review.LEGACY_VERDICT
        return markdown.publishable(header(REV_NAME) + '\nReview di prima di #248.', 'review',
                                    record)

    def test_a_legacy_verdict_is_read_as_needs_operator(self):
        record = markdown.read_marker(self.legacy_review(), 'review')
        self.assertEqual(record['verdict'], 'needs_operator')
        self.assertEqual(review.round_verdict([{'verdict': review.LEGACY_VERDICT},
                                               {'verdict': 'approve'}]), 'needs_operator')
        self.assertIsNone(markdown.read_marker(
            markdown.publishable('x', 'review', dict(record, verdict='needs_somebody')), 'review'))

    def test_a_legacy_verdict_sends_the_pr_to_the_operator(self):
        gh = MergeGH()
        gh.thread = []
        gh.say('brushtyler', self.legacy_review())
        gh.labels[100] = {REVIEW}
        d = Dispatcher(CFG, gh, FakeWS(), FakeRunner(), log=lambda m: None,
                       identities=MergeIdentities(gh))
        self.assertEqual(d.tick(), [Action('apply_review', 100, 'verdict already recorded')])
        self.assertEqual(gh.labels[100], {OPERATOR})

    def test_an_operator_merge_notice_needs_its_head_and_count(self):
        body = markdown.render_operator_merge(SHA1, 2, 1, '- motivo', OPERATOR)
        self.assertEqual(markdown.read_marker(body, 'notice'),
                         {'event': 'operator_merge', 'head': SHA1, 'labeled': 2, 'promotion': 1})
        for payload in ({'event': 'operator_merge', 'head': SHA1, 'promotion': 0},
                        {'event': 'operator_merge', 'labeled': 0, 'promotion': 0},
                        {'event': 'operator_merge', 'head': SHA1, 'labeled': 0}):
            with self.subTest(payload=payload):
                self.assertIsNone(markdown.read_marker(
                    markdown.publishable('x', 'notice', payload), 'notice'))


class LegacyLabelTests(unittest.TestCase):
    """Until the rename the old label blocks exactly as agent:needs-operator."""

    def test_an_issue_with_the_old_label_does_not_start(self):
        snap = Snapshot([], [ready(5, [OLD]), ready(6)], [])
        self.assertEqual([(a.kind, a.number) for a in plan(snap, CFG)], [('implement', 6)])
        merged = dict(ready(5, [OLD]), merged=[90])
        self.assertEqual(plan(Snapshot([], [merged], []), CFG), [])
        checked_issue = dict(ready(5, [OLD]), preflight={'record': None, 'hash': 'h',
                                                         'recheck': False, 'owned': False})
        self.assertEqual(plan(Snapshot([], [checked_issue], []), PCFG), [])

    def test_a_pr_with_the_old_label_is_left_alone_and_never_merged(self):
        for labels in ([REVIEW, OLD], [READY, OLD]):
            with self.subTest(labels=labels):
                p = pr(labels=labels, reviews=[norm(raw_review())], head=SHA2)
                p.mergeable, p.merge_status, p.merge_mode = 'MERGEABLE', 'CLEAN', 'auto'
                self.assertEqual(plan(Snapshot([p], [], []), AUTO), [])
        gh = MergeGH()
        gh.labels[100] = {READY, OLD}
        d = Dispatcher(AUTO, gh, FakeWS(), FakeRunner(), log=lambda m: None,
                       identities=MergeIdentities(gh))
        self.assertEqual(d.tick(), [])
        self.assertEqual(gh.merged, [])
        self.assertEqual(gh.labels[100], {READY, OLD})


class LegacyPreflightTests(unittest.TestCase):
    """A block the readiness check added before the rename, under the old name: its events count,
    and a passing check removes it."""

    make = preflight.PreflightTests.make
    planned = preflight.PreflightTests.planned
    blocked = preflight.PreflightTests.blocked

    def test_a_block_under_the_old_name_is_removed_by_a_passing_check(self):
        d = self.make()
        self.blocked(d)
        # Before #248 the label had its old name, and so did its `labeled` event.
        self.gh.labels[7] = {ISSUE_READY, OLD}
        self.gh.needs_operator_labelers = []
        self.gh.label_log = {(7, OLD): ['brushtyler']}
        self.assertEqual(self.planned(d), [])
        self.gh.say('/devloop recheck')
        self.assertEqual(self.planned(d), [('preflight', 7)])
        self.runner.outputs.append(checked())
        d.tick()
        self.assertEqual(self.gh.labels[7], {ISSUE_READY})

    def test_a_block_under_the_old_name_set_by_someone_else_stays(self):
        d = self.make()
        self.blocked(d)
        self.gh.labels[7] = {ISSUE_READY, OLD}
        self.gh.needs_operator_labelers = []
        # Someone added the old label again after the check's own.
        self.gh.label_log = {(7, OLD): ['brushtyler', 'brushtyler']}
        self.gh.say('/devloop recheck')
        self.assertEqual(self.planned(d), [])


class GitIdentities(MergeIdentities):
    """The identities of the merge tests, with the ambient git login for every actor."""

    def git_auth(self, actor, permissions=None):
        return None


class NoticeGH(MergeGH):
    """``MergeGH`` whose ``edit_comment`` replaces the body of a comment of the thread, as
    GitHub does: the comment keeps its id, its author and its place."""

    def edit_comment(self, comment_id, body):
        super().edit_comment(comment_id, body)
        for c in self.thread:
            if c['id'] == comment_id:
                self.comments = [(n, body if b == c['body'] else b) for n, b in self.comments]
                c['body'] = body


class ReadyPrCase(unittest.TestCase):
    """PR #100, approved and ready, in merge mode "auto"."""

    def make(self, diff=('x.py', 'AGENTS.md'), cfg=AUTO):
        self.gh, self.ws = NoticeGH(), FakeWS(conflicts=())
        self.ws.diff = changes(*diff)
        self.logs = []
        return Dispatcher(cfg, self.gh, self.ws, FakeRunner(), log=self.logs.append,
                          identities=GitIdentities(self.gh))

    def told(self, **kw):
        d = self.make(**kw)
        self.assertEqual(d.tick(), [Action('operator_merge', 100, 'merge mode off')])
        return d

    def notices(self):
        return [r for r in records(self.gh, 'notice') if r['event'] == 'operator_merge']


class OperatorMergeTests(ReadyPrCase):
    """In "auto", a ready PR that touches AGENTS.md is the operator's to merge."""

    def test_the_pr_gets_the_label_and_the_comment_and_is_not_merged(self):
        d = self.told()
        self.assertEqual(self.gh.labels[100], {READY, OPERATOR})
        self.assertEqual(self.gh.merged, [])
        body = self.gh.comments[-1][1]
        self.assertIn('## Il merge spetta all\'operatore', body)
        self.assertIn('`AGENTS.md`', body)
        self.assertNotIn('`x.py`', body)
        self.assertEqual(self.notices(), [{'event': 'operator_merge', 'head': SHA1, 'labeled': 0,
                                           'promotion': 0}])
        # devloop keeps following the PR, and neither merges it nor tells it again.
        snap = d.snapshot()
        self.assertTrue(snap.prs[0].operator_owned)
        self.assertEqual(snap.prs[0].merge_mode, 'off')
        self.assertEqual(d.tick(), [])
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.gh.merged, [])

    def test_a_new_commit_removes_the_label_and_sends_the_pr_back_to_review(self):
        d = self.told()
        self.gh.head = SHA2
        self.assertEqual(d.tick(), [Action('revoke', 100, 'head is not the approved commit')])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        self.assertIn(f'`{OPERATOR}`', self.gh.comments[-1][1])

    def test_a_conflict_is_synced(self):
        d = self.told()
        self.gh.mergeable = 'CONFLICTING'
        self.assertEqual(d.tick(), [Action('sync', 100, 'conflicts with the base branch')])
        self.assertEqual(self.ws.pushes, [('agent/issue-7', False)])
        self.assertEqual(self.gh.labels[100], {REVIEW})

    def test_a_failed_ci_round_removes_the_label(self):
        # A level "off" (AGENTS.md is also a sensitive path: its CI round at level 2 without
        # independent reviewers is an escalation, which keeps the label).
        off = dataclasses.replace(AUTO, merge=MergePolicy(mode='auto', levels=((1, 'off'),)))
        d = self.told(cfg=off, diff=('x.py',))
        self.assertIn('livello di review 1', self.gh.comments[-1][1])
        self.gh.ci = 'fail'
        self.assertEqual([a.kind for a in d.tick()], ['ci_failed'])
        self.assertEqual(self.gh.labels[100], {'agent:changes'})

    def test_an_escalation_on_a_pr_of_the_operator_keeps_the_label(self):
        d = self.told()
        self.gh.ci = 'fail'
        self.assertEqual([a.kind for a in d.tick()], ['ci_failed'])
        self.assertEqual(self.gh.labels[100], {OPERATOR})
        self.assertIn('Review di livello 2', self.gh.comments[-1][1])

    def test_a_head_promoted_again_keeps_its_one_comment(self):
        # A level "off", as above: AGENTS.md would need level 2 at the new promotion.
        off = dataclasses.replace(AUTO, merge=MergePolicy(mode='auto', levels=((1, 'off'),)))
        d = self.told(cfg=off, diff=('x.py',))
        (told,) = [c['id'] for c in self.gh.thread
                   if (markdown.read_marker(c['body'], 'notice') or {}).get('event')
                   == 'operator_merge']
        # The CI runs again on the same commit: the readiness decays, and the dispatcher's
        # label goes with it.
        self.gh.ci = 'pending'
        self.assertEqual(d.tick(), [Action('revoke', 100, 'CI pending on the approved commit')])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        # The CI passes: the same head is promoted and made ready again.
        self.gh.ci = 'pass'
        self.assertEqual([a.kind for a in d.tick()], ['apply_review'])
        self.assertEqual(self.gh.labels[100], {READY})
        self.assertEqual(d.tick(), [Action('operator_merge', 100, 'merge mode off')])
        self.assertEqual(self.gh.labels[100], {READY, OPERATOR})
        # One comment for the head: the notice is edited, not written again.
        self.assertEqual(self.notices(), [{'event': 'operator_merge', 'head': SHA1,
                                           'labeled': 1, 'promotion': 1}])
        self.assertEqual([cid for cid, _ in self.gh.comment_edits], [told])
        # The label is still the dispatcher's: a new commit removes it.
        self.assertTrue(d.snapshot().prs[0].operator_owned)
        self.assertEqual(d.tick(), [])
        self.gh.head = SHA2
        self.assertEqual(d.tick(), [Action('revoke', 100, 'head is not the approved commit')])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        self.assertEqual(len(self.notices()), 1)

    def test_a_label_the_operator_removes_is_not_added_again(self):
        d = self.told()
        self.gh.edit_labels(100, remove=[OPERATOR])
        self.assertEqual(d.tick(), [])
        self.assertEqual(self.gh.labels[100], {READY})
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.gh.merged, [])

    def test_without_operator_only_paths_auto_still_merges(self):
        d = self.make(diff=('x.py',))
        self.assertEqual([a.kind for a in d.tick()], ['merge'])
        self.assertEqual(self.notices(), [])


class EscalatedPrTests(ReadyPrCase):
    """agent:needs-operator from an escalation, or from a person, still stops devloop."""

    def test_an_escalation_on_a_ready_pr_is_left_alone(self):
        d = self.make()
        d._escalate(100, 'Serve una decisione.', remove=[REVIEW, 'agent:changes'], cls='failure')
        self.assertEqual(self.gh.labels[100], {READY, OPERATOR})
        self.assertFalse(d.snapshot().prs[0].operator_owned)
        self.gh.head, self.gh.mergeable = SHA2, 'CONFLICTING'
        self.assertEqual(d.tick(), [])
        self.assertEqual(self.gh.labels[100], {READY, OPERATOR})
        self.assertEqual(self.ws.pushes, [])

    def test_an_escalation_after_the_operator_merge_takes_the_pr_back(self):
        d = self.told()
        d._escalate(100, 'Serve una decisione.', remove=[], cls='failure')
        self.assertFalse(d.snapshot().prs[0].operator_owned)
        self.gh.head = SHA2
        self.assertEqual(d.tick(), [])
        self.assertEqual(self.gh.labels[100], {READY, OPERATOR})

    def test_a_label_a_person_added_again_is_not_the_dispatchers(self):
        d = self.told()
        self.gh.edit_labels(100, remove=[OPERATOR])
        self.gh.edit_labels(100, add=[OPERATOR])  # a second `labeled` event
        self.assertFalse(d.snapshot().prs[0].operator_owned)
        self.gh.head = SHA2
        self.assertEqual(d.tick(), [])
        self.assertEqual(self.gh.labels[100], {READY, OPERATOR})


if __name__ == '__main__':
    unittest.main()
