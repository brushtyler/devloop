"""devloop: the review after a sync of an approved PR (#235)."""

import dataclasses
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devloop import markdown
from devloop.dispatcher import pr_status_line, round_limit_reason
from devloop.planning import (RESYNC_AFTER_APPROVAL, Action, counted_rounds, round_reasons,
                              waived_rounds)
from devloop.workspace import Workspace, WorkspaceError
from test_devloop import CFG, SHA1, SHA2, finding, fixed, pr, raw_review, receipt
from test_devloop_quorum import FIRST, SECOND, Quorum

LEVEL1 = [{'path': 'x.py', 'links': []}]  # a diff reviewed at level 1
ONE_ROUND = dataclasses.replace(CFG, max_review_rounds=1)
SYNCED = {'status': 'completed', 'summary': 'Kept both sides.', 'blocked_reason': ''}


class ResyncTests(Quorum):
    def approved_then_synced(self, *outputs, conflicts=(), cfg=ONE_ROUND):
        """Round 1 approves SHA1 and the PR becomes ready; then main conflicts with it, and the
        sync merges main (``conflicts``: the files an agent resolves) into the new head SHA2."""
        self.make(raw_review(), *outputs, cfg=cfg, diff=LEVEL1)
        self.assertEqual(self.d.tick(), [Action('review', 100)])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        pr = self.d.snapshot().prs[0]
        pr.merge_state = 'conflict'
        self.ws.conflicts = conflicts
        self.d._do_sync(pr)
        self.assertEqual(self.ws.pushes, [('agent/issue-7', False)])
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.gh.head = SHA2
        # What git says of the new head: a clean merge of main, unless an agent resolved
        # conflicts in it.
        self.ws.resynced = not conflicts

    def test_a_clean_sync_of_an_approved_pr_is_reviewed_without_spending_a_round(self):
        # The case of #230: approved in the last round allowed, then synced without conflicts.
        self.approved_then_synced(raw_review())
        self.assertEqual(self.planned(), [
            Action('review', 100, 'the approved head was synced with the base')])
        self.assertEqual(self.ws.resync_checks[-1], ('agent/issue-7', SHA2, SHA1))
        self.d.tick()
        first, second = self.records('review')
        self.assertEqual((second['round'], second['head_sha']), (2, SHA2))
        self.assertEqual(second['resync'], {'reason': RESYNC_AFTER_APPROVAL, 'round': 1,
                                            'from': SHA1})
        self.assertNotIn('resync', first)
        # The round shown as the limit takes the round that did not count into account.
        self.assertEqual(second['max_rounds'], 2)
        self.assertIn('round 2 of at most 2', self.runner.calls[-1][3])
        self.assertIn('questo round non conta per il limite', self.gh.comments[-2][1])
        reviews = self.d.snapshot().prs[0].reviews
        self.assertEqual((waived_rounds(reviews), counted_rounds(reviews)), ({2}, 1))
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})

    def test_every_clean_sync_after_an_approval_is_free(self):
        self.approved_then_synced(raw_review(), raw_review())
        self.d.tick()
        # Main conflicts again with the approved SHA2: a second clean sync, a third review.
        pr = self.d.snapshot().prs[0]
        pr.merge_state = 'conflict'
        self.ws._head = SHA2
        self.d._do_sync(pr)
        self.gh.head = self.ws._head = 'c' * 40
        self.assertEqual(self.planned(), [
            Action('review', 100, 'the approved head was synced with the base')])
        self.d.tick()
        third = self.records('review')[-1]
        self.assertEqual(third['resync'], {'reason': RESYNC_AFTER_APPROVAL, 'round': 2,
                                           'from': SHA2})
        reviews = self.d.snapshot().prs[0].reviews
        self.assertEqual((waived_rounds(reviews), counted_rounds(reviews)), ({2, 3}, 1))
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})

    def test_the_comment_of_a_clean_sync_says_the_review_does_not_count(self):
        # #289: the sync comment says what the next review spends.
        self.approved_then_synced()
        body = self.gh.comments[-1][1]
        self.assertIn('la sincronizzazione non consuma un round di review', body)
        self.assertIn('La review del nuovo head **non conterà** come round', body)
        self.assertNotIn('**conterà**', body)

    def test_the_comment_of_a_sync_with_conflicts_says_the_review_counts(self):
        # #289: with no round left, the comment says the operator decides; with rounds left,
        # it says which round the review is and how many remain after it.
        self.approved_then_synced(SYNCED, conflicts=('x.py',))
        body = self.gh.comments[-1][1]
        self.assertIn('La review successiva **conterà** come round, perché l\'agent ha risolto '
                      'dei conflitti', body)
        self.assertIn('contati già 1 round di 1 (`max_review_rounds`); nessun round rimasto: la '
                      'review successiva richiederà una decisione dell\'operatore', body)
        self.assertNotIn('non conterà', body)
        for limit, left in ((2, 'round 2 di 2 (`max_review_rounds`), l\'ultimo rimasto'),
                            (4, 'round 2 di 4 (`max_review_rounds`); dopo questo ne resteranno '
                                '2')):
            with self.subTest(limit=limit):
                cfg = dataclasses.replace(CFG, max_review_rounds=limit)
                self.approved_then_synced(SYNCED, conflicts=('x.py',), cfg=cfg)
                self.assertIn(left, self.gh.comments[-1][1])

    def test_a_sync_whose_conflicts_an_agent_resolved_counts(self):
        # At the limit: the content of the PR changed, so the review is a new round.
        self.approved_then_synced(SYNCED, conflicts=('x.py',))
        self.assertEqual(self.planned(), [Action('round_limit', 100)])
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        self.assertEqual(len(self.records('review')), 1)
        # #289: the escalation lists the rounds that count, with their reason.
        body = self.gh.comments[-1][1]
        self.assertIn('**Classe:** `round_limit`', body)
        self.assertIn('Round contati sul limite (1 di 1, `max_review_rounds`):\n\n'
                      '- round 1: prima review', body)
        self.assertIn('alzando `max_review_rounds`', body)

    def status(self):
        """The PR lines ``devloop status`` logs, on the real snapshot of the dispatcher."""
        import devloop.__main__ as cli
        lines = []
        with mock.patch.object(cli, 'build', return_value=(self.d.cfg, Path('/state'), self.d)), \
                mock.patch.object(cli, 'log', lines.append):
            cli.main(['status'])
        return [line for line in lines if line.startswith('PR #100 ')]

    def test_status_shows_the_rounds_of_a_pr_stopped_at_the_round_limit(self):
        # #289: escalated to agent:needs-operator the snapshot does not read the thread, yet
        # ``status`` still shows the rounds that reached the limit.
        self.approved_then_synced(SYNCED, conflicts=('x.py',))
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        self.assertEqual(self.d.snapshot().prs[0].reviews, [])  # tick still skips the thread
        (line,) = self.status()
        self.assertIn('[agent:needs-operator] rounds=1/1 ci=', line)

    def test_status_shows_the_rounds_not_counted_of_an_escalated_pr(self):
        cfg = dataclasses.replace(CFG, max_review_rounds=2)
        changes = raw_review('request_changes', [finding()])
        self.approved_then_synced(changes, fixed(), changes, cfg=cfg)
        self.d.tick()
        self.ws.resynced = False
        self.d.tick()
        self.gh.head = self.ws._head
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        (line,) = self.status()
        self.assertIn('[agent:needs-operator] rounds=2/2 (+1 not counted) ci=', line)

    def test_a_sync_whose_conflicts_an_agent_resolved_spends_a_round(self):
        # With rounds left the review runs, and counts, without ``resync``.
        self.approved_then_synced(SYNCED, raw_review(), conflicts=('x.py',), cfg=CFG)
        self.assertEqual(self.planned(), [Action('review', 100)])
        self.d.tick()
        second = self.records('review')[-1]
        self.assertEqual(second['round'], 2)
        self.assertNotIn('resync', second)
        self.assertEqual(counted_rounds(self.d.snapshot().prs[0].reviews), 2)

    def test_a_request_for_changes_after_the_resync_keeps_the_limit(self):
        # With one round allowed and spent, a request for changes in the free round reaches
        # the limit at once: its fix would need a round that counts.
        self.approved_then_synced(raw_review('request_changes', [finding()]))
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        self.assertIn('Raggiunto il limite di 1 round', self.gh.comments[-1][1])

    def test_the_fix_after_a_resync_is_reviewed_in_a_round_that_counts(self):
        cfg = dataclasses.replace(CFG, max_review_rounds=2)
        changes = raw_review('request_changes', [finding()])
        self.approved_then_synced(changes, fixed(), changes, cfg=cfg)
        self.d.tick()  # round 2, free, asks for changes
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        self.ws.resynced = False  # git: the fix is not a merge of main
        self.d.tick()  # the implementer's fix
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.gh.head = self.ws._head
        self.assertEqual(self.planned(), [Action('review', 100)])
        self.d.tick()  # round 3 counts, the second of two
        last = self.records('review')[-1]
        self.assertEqual(last['round'], 3)
        self.assertNotIn('resync', last)
        reviews = self.records('review')
        self.assertEqual((waived_rounds(reviews), counted_rounds(reviews)), ({2}, 2))
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        body = self.gh.comments[-1][1]
        self.assertIn('Raggiunto il limite di 2 round', body)
        # #289: the rounds that count and those that do not, each with its reason.
        self.assertIn('Round contati sul limite (2 di 2, `max_review_rounds`):\n\n'
                      '- round 1: prima review\n- round 3: review dopo una correzione', body)
        self.assertIn('Non contano:\n\n- round 2: review di un head approvato, spostato solo da '
                      'una sync senza conflitti', body)

    def test_a_rereview_or_a_ci_failure_after_the_sync_counts(self):
        self.approved_then_synced()
        self.gh.ci = 'fail'
        self.assertEqual(self.planned(), [Action('round_limit', 100)])
        self.gh.ci = 'pass'
        self.gh.comments.append((100, '/devloop rereview\n\nDi nuovo.'))
        self.gh.written.append('brushtyler')
        self.assertEqual(self.planned(), [Action('round_limit', 100)])

    def test_a_resync_git_cannot_verify_counts(self):
        self.approved_then_synced()
        self.ws.resynced = WorkspaceError('missing commit')
        self.assertEqual(self.planned(), [Action('round_limit', 100)])
        self.assertTrue(any('resync of #100 not verifiable' in m for m in self.logs))

    def test_no_check_without_an_approved_round_before_the_head(self):
        self.make(raw_review('request_changes', [finding()]), cfg=ONE_ROUND, diff=LEVEL1)
        self.d.tick()
        self.gh.labels[100] = {'agent:review'}
        self.gh.head = SHA2
        self.ws.resynced = True
        self.assertEqual(self.planned(), [Action('round_limit', 100)])
        self.assertFalse(getattr(self.ws, 'resync_checks', []))


class CountingTests(unittest.TestCase):
    def record(self, round_no, reviewer, verdict='approve', head=SHA1, resync=None):
        r = {'round': round_no, 'source': 'agent', 'reviewer': reviewer, 'verdict': verdict,
             'head_sha': head}
        if resync is not None:
            r['resync'] = resync
        return r

    def resync(self, round_no=1, head=SHA1, reason=RESYNC_AFTER_APPROVAL):
        return {'reason': reason, 'round': round_no, 'from': head}

    def test_only_the_round_after_an_approval_of_its_previous_head_is_waived(self):
        good = [self.record(1, FIRST), self.record(2, FIRST, head=SHA2, resync=self.resync())]
        self.assertEqual((waived_rounds(good), counted_rounds(good)), ({2}, 1))
        for name, reviews in {
            'not approved': [self.record(1, FIRST, 'request_changes'),
                             self.record(2, FIRST, head=SHA2, resync=self.resync())],
            'other head': [self.record(1, FIRST),
                           self.record(2, FIRST, head=SHA2, resync=self.resync(head='c' * 40))],
            'same head': [self.record(1, FIRST), self.record(2, FIRST, resync=self.resync())],
            'not the previous round': [
                self.record(1, FIRST), self.record(2, FIRST, head=SHA2),
                self.record(3, FIRST, head='c' * 40, resync=self.resync())],
            'other reason': [self.record(1, FIRST),
                             self.record(2, FIRST, head=SHA2,
                                         resync=self.resync(reason='other'))],
            'ci round': [dict(self.record(1, 'ci'), source='ci'),
                         self.record(2, FIRST, head=SHA2, resync=self.resync())],
            'bool round': [self.record(1, FIRST),
                           self.record(2, FIRST, head=SHA2, resync=self.resync(round_no=True))],
        }.items():
            with self.subTest(name):
                self.assertEqual(waived_rounds(reviews), set())

    def test_an_incomplete_approval_is_not_a_resync(self):
        bound = dict(receipt(), level=2, reviewers=[FIRST, SECOND])
        reviews = [dict(self.record(1, FIRST), receipt=bound),
                   self.record(2, FIRST, head=SHA2, resync=self.resync())]
        self.assertEqual(waived_rounds(reviews), set())

    def test_each_round_has_a_reason(self):
        # #289: why each round counts, or does not, as the round_limit escalation lists it.
        third, fourth, fifth = 'c' * 40, 'd' * 40, 'e' * 40
        reviews = [
            self.record(1, FIRST, 'request_changes'),
            dict(self.record(2, 'ci', 'request_changes', head=SHA2), source='ci'),
            self.record(3, FIRST, head=third),
            self.record(4, FIRST, head=fourth, resync=self.resync(3, third)),
            self.record(5, FIRST, 'request_changes', head=fifth),
            self.record(6, FIRST, 'request_changes', head=fifth),
            self.record(7, FIRST, 'request_changes', head='f' * 40),
        ]
        fixes = [{'round': 1, 'head_sha': SHA2}, {'round': 2, 'head_sha': third}]
        syncs = [{'head_sha': fifth, 'from': fourth}]
        self.assertEqual(round_reasons(reviews, fixes, syncs), [
            (1, 'prima review', True),
            (2, 'CI fallita', True),
            (3, 'review dopo una correzione', True),
            (4, 'review di un head approvato, spostato solo da una sync senza conflitti', False),
            (5, 'review dopo una sync con conflitti risolti dall\'agent', True),
            (6, 'review dopo una sync con conflitti risolti dall\'agent', True),
            (7, 'review di un nuovo head', True)])
        restarted = [dict(self.record(1, FIRST), receipt=dict(receipt(), level=2,
                                                              reviewers=[FIRST, SECOND])),
                     dict(self.record(2, FIRST), restarts={'round': 1, 'bases': []}),
                     self.record(3, FIRST)]
        self.assertEqual(round_reasons(restarted), [
            (1, 'round interrotto dall\'avanzare della base e sostituito dal successivo', False),
            (2, 'prima review', True), (3, 'nuova review dello stesso head', True)])
        text = round_limit_reason('Limite.', reviews, fixes, syncs, 6)
        self.assertTrue(text.startswith('Limite.\n\nRound contati sul limite (6 di 6, '))
        self.assertIn('- round 2: CI fallita\n', text)
        self.assertIn('Non contano:\n\n- round 4: ', text)

    def test_status_shows_the_rounds_counted_against_the_limit(self):
        # #289: ``devloop status`` shows the counted rounds of each PR on the limit.
        reviews = [self.record(1, FIRST), self.record(2, FIRST, head=SHA2, resync=self.resync())]
        line = pr_status_line(pr(reviews=reviews, labels=['agent:ready-to-merge']), CFG)
        self.assertEqual(line, 'PR #100 (issue #7) [agent:ready-to-merge] rounds=1/3 '
                               '(+1 not counted) ci=pass merge=clean')
        line = pr_status_line(pr(reviews=reviews[:1], merge='conflict'), ONE_ROUND)
        self.assertEqual(line, 'PR #100 (issue #7) [agent:review] rounds=1/1 ci=pass '
                               'merge=conflict CONFLICT with main')
        self.assertIn(' rounds=0/3 ', pr_status_line(pr(), CFG))

    def test_the_review_shows_why_it_does_not_count(self):
        record = dict(self.record(2, FIRST, head=SHA2, resync=self.resync()), max_rounds=2,
                      ci='pass', summary='ok', human_reason='', blocking_findings=[],
                      non_blocking_findings=[], previous_findings=[], checks_performed=[])
        body = markdown.render_review(record, '')
        self.assertIn(f'Il round 1 aveva approvato `{SHA1[:12]}`', body)


class CleanResyncTests(unittest.TestCase):
    """``Workspace.clean_resync`` on a real repository, ``origin`` being the repository
    itself."""

    def git(self, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@x',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@x')
        return subprocess.run(['git', *args], cwd=self.repo, env=env, capture_output=True,
                              text=True, check=True).stdout.strip()

    def commit(self, message, **files):
        for name, text in files.items():
            (self.repo / f'{name}.py').write_text(text)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.git('init', '-q', '-b', 'main')
        self.commit('root', a='a\n', b='b\n')
        self.git('remote', 'add', 'origin', str(self.repo))
        self.git('checkout', '-q', '-b', 'pr')
        self.approved = self.commit('pr', a='pr\n')
        self.git('checkout', '-q', 'main')
        self.commit('main moves', b='main\n')
        self.ws = Workspace(self.repo, '.state', 'main')

    def merge_main(self, resolve=None):
        """Merge main into the PR branch, resolving a conflict with ``resolve`` (text of a)."""
        self.git('checkout', '-q', 'pr')
        done = subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@x', 'merge',
                               '-q', '--no-ff', '--no-edit', 'main'], cwd=self.repo,
                              capture_output=True, text=True)
        if done.returncode:
            (self.repo / 'a.py').write_text(resolve)
            self.git('add', '-A')
            self.git('commit', '-q', '--no-edit')
        head = self.git('rev-parse', 'HEAD')
        self.git('checkout', '-q', 'main')
        return head

    def check(self, head, approved=None):
        return self.ws.clean_resync('pr', head, approved or self.approved)

    def test_a_clean_merge_of_main_is_a_resync(self):
        head = self.merge_main()
        self.assertTrue(self.check(head))
        self.assertFalse(self.check(self.approved))  # nothing changed: no resync

    def test_successive_clean_merges_are_a_resync(self):
        self.merge_main()
        self.commit('main again', b='again\n')
        self.assertTrue(self.check(self.merge_main()))

    def test_a_merge_whose_conflicts_were_resolved_is_not(self):
        self.commit('main changes a', a='main\n')
        head = self.merge_main(resolve='pr\nmain\n')
        self.assertFalse(self.check(head))

    def test_a_merge_with_another_change_or_a_commit_is_not(self):
        self.git('checkout', '-q', 'pr')
        self.git('merge', '-q', '--no-ff', '--no-commit', 'main')
        (self.repo / 'a.py').write_text('changed in the merge\n')
        self.git('add', '-A')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@x', 'commit', '-q', '--no-edit')
        amended = self.git('rev-parse', 'HEAD')
        self.assertFalse(self.check(amended))
        fix = self.commit('fix', a='fixed\n')
        self.assertFalse(self.check(fix))
        self.git('checkout', '-q', 'main')

    def test_a_merge_of_a_branch_other_than_main_is_not(self):
        self.git('checkout', '-q', '-b', 'side', self.approved)
        self.commit('side', c='c\n')
        self.git('checkout', '-q', 'pr')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@x', 'merge', '-q', '--no-ff',
                 '--no-edit', 'side')
        head = self.git('rev-parse', 'HEAD')
        self.git('checkout', '-q', 'main')
        self.assertFalse(self.check(head))

    def test_an_unknown_commit_cannot_be_verified(self):
        head = self.merge_main()
        with self.assertRaises(WorkspaceError):
            self.check('f' * 40)
        self.assertFalse(self.check(head, approved='f' * 40))


if __name__ == '__main__':
    unittest.main()
