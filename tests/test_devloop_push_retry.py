"""devloop: a transient error of GitHub on the push of a step's branch (#645).

The push of an implementation, a fix or a sync is tried once more after a transient error of the
remote (a 5xx answer, a connection lost). When the second attempt fails too, the escalation is
``push_failed``, non-critical, and ``/devloop retry`` pushes again the commits of the turn already
done, without a new turn. Any other refusal of the remote stays a ``failure``, critical."""

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devloop import markdown, supervision
from devloop.dispatch_actions import PendingPush, PendingPushes
from devloop.dispatcher import Dispatcher
from devloop.planning import Action
from devloop.workspace import TransientPushError, Workspace, WorkspaceError, transient_push
from test_devloop import SHA1, SHA2, finding, fixed, implemented, norm, pr, raw_review
from test_devloop_conclave import NEEDS_OPERATOR, proposal, vote
from test_devloop_conclave_resume import DIFF, ResumeBase
from test_devloop_supervision import (AUTO, HUMAN, READY, REVIEW, STRICT, SUPER, Case,
                                      Identities, SupWS)

FIVE_XX = ('git push --force: remote: Internal Server Error\n'
           'To https://github.com/o/r.git\n'
           ' ! [remote rejected] HEAD -> agent/issue-7 (Internal Server Error)\n'
           'error: failed to push some refs to \'https://github.com/o/r.git\'')
LOST = ('git push --force: error: RPC failed; curl 56 Recv failure: Connection reset by peer\n'
        'fatal: the remote end hung up unexpectedly')
RULE = ('git push --force: remote: error: GH013: Repository rule violations found for '
        'refs/heads/agent/issue-7.\n'
        ' ! [remote rejected] HEAD -> agent/issue-7 (push declined due to repository rule '
        'violations)')


def git_error(message):
    """The error ``Workspace.push`` raises for ``message`` (``transient_push``)."""
    return TransientPushError(message) if transient_push(message) else WorkspaceError(message)


class PushWS(SupWS):
    """A workspace whose pushes fail, in order, with ``errors`` (None: a push that succeeds)."""

    def __init__(self, *errors):
        super().__init__()
        self.errors = [git_error(e) if isinstance(e, str) else e for e in errors]
        self.copies = []

    def push(self, path, branch, replace=False, auth=None):
        super().push(path, branch, replace, auth)
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error

    def commit_copy(self, name, commit, branch=None):
        self.copies.append((name, commit))
        return Path('/wt') / name


class Restart:
    """A ``state_dir`` for the pushes kept, and a new dispatcher on it, with the same GitHub,
    checkout and runner: what ``devloop`` has after a restart (#645)."""

    def setUp(self):
        super().setUp()
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)

    def kept(self):
        return PendingPushes(self.state.name)

    def restarted(self):
        d = Dispatcher(self.cfg, self.gh, self.ws, self.runner, log=self.logs.append,
                       identities=Identities(self.gh), pending_pushes=self.kept())
        d.push_retry_pause = 0
        return d


class ClassificationTests(unittest.TestCase):
    def test_5xx_and_lost_connections_are_transient(self):
        for message in (FIVE_XX, LOST, 'remote: Bad Gateway', 'HTTP 503 Service Unavailable',
                        'fatal: unable to access \'https://github.com/o/r.git/\': The requested '
                        'URL returned error: 502',
                        'fatal: unable to access \'https://github.com/o/r.git/\': Could not '
                        'resolve host: github.com',
                        'git push --force: timed out after 600s'):
            with self.subTest(message=message):
                self.assertTrue(transient_push(message))

    def test_refusals_are_not_transient(self):
        for message in (RULE,
                        'remote: error: GH006: Protected branch update failed for '
                        'refs/heads/main.',
                        'remote: Permission to o/r.git denied to app[bot].\nfatal: unable to '
                        'access \'https://github.com/o/r.git/\': The requested URL returned '
                        'error: 403',
                        ' ! [rejected] HEAD -> agent/issue-7 (non-fast-forward)',
                        ' ! [rejected] HEAD -> agent/issue-7 (fetch first)',
                        # A refusal prevails over a sign of a transient error in the same text.
                        RULE + '\nremote: Internal Server Error',
                        '', None):
            with self.subTest(message=message):
                self.assertFalse(transient_push(message))

    def test_the_push_of_the_workspace_tells_them_apart(self):
        ws = Workspace.__new__(Workspace)
        for message, error in ((FIVE_XX, TransientPushError), (RULE, WorkspaceError)):
            with self.subTest(error=error.__name__):
                def git(*args, **kwargs):
                    raise WorkspaceError(message)
                ws._git = git
                with self.assertRaises(WorkspaceError) as ctx:
                    ws.push(Path('/wt/issue-7'), 'agent/issue-7', replace=True)
                self.assertIs(type(ctx.exception), error)
                self.assertIn('remote', str(ctx.exception))


class ImplementTests(Restart, Case):
    def start(self, cfg, *errors):
        d = self.make(cfg, implemented(), has_pr=False)
        self.ws = d.ws = PushWS(*errors)
        d.push_retry_pause = 0
        d.pending_pushes = self.kept()
        return d

    def test_after_a_restart_the_retry_still_pushes_without_a_turn(self):
        # Criterion 2 of #645 across a restart, or a `devloop tick` of its own: the push is
        # kept in state_dir.
        d = self.start(AUTO, FIVE_XX, FIVE_XX, None)
        self.assertEqual(self.tick(d), [('implement', 7)])
        self.assertEqual(len(self.runner.calls), 1)
        (kept,) = [self.kept().get(n) for n in self.kept().numbers()]
        self.assertEqual((kept.step, kept.number, kept.commit, kept.branch, kept.armed),
                         ('implement', 7, SHA2, 'agent/issue-7', False))
        again = self.restarted()
        self.gh.command(7, '/devloop retry')
        self.assertEqual(self.tick(again), [('supervise', 7)])
        self.assertEqual(self.decisions(7)[-1]['outcome'], 'applied')
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(self.ws.commits), 1)
        self.assertEqual(len(self.ws.pushes), 3)
        self.assertEqual(len(self.gh.created), 1)
        self.assertEqual(self.gh.labels[100], {REVIEW})
        self.assertEqual(self.gh.labels[7], set())
        self.assertEqual(self.kept().numbers(), [])

    def test_one_5xx_then_a_push_opens_the_pr_without_an_escalation(self):
        # Criterion 1 of #645.
        d = self.start(AUTO, FIVE_XX, None)
        self.assertEqual(self.tick(d), [('implement', 7)])
        self.assertEqual(self.ws.pushes, [('agent/issue-7', True)] * 2)
        self.assertEqual(len(self.gh.created), 1)
        self.assertEqual(self.escalations(7), [])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        self.assertEqual(self.gh.labels[7], set())
        # The log of the dispatcher records the second attempt.
        log = '\n'.join(self.logs)
        self.assertIn('push of agent/issue-7 failed with a transient error of the remote', log)
        self.assertIn('Internal Server Error', log)
        self.assertIn('push of agent/issue-7 done at the second attempt', log)
        self.assertEqual(d.pending_pushes.numbers(), [])

    def test_two_5xx_escalate_a_retryable_push_and_the_retry_pushes_without_a_turn(self):
        # Criterion 2 of #645: the supervisor's retry in auto.
        d = self.start(AUTO, FIVE_XX, LOST, None)
        self.assertEqual(self.tick(d), [('implement', 7)])
        self.assertEqual(len(self.ws.pushes), 2)
        self.assertEqual(self.gh.created, [])
        self.assertEqual(self.gh.labels[7], {SUPER})  # agent:ready went at the takeover
        record = self.escalations(7)[-1]
        self.assertEqual({k: record[k] for k in ('class', 'critical', 'to', 'step')},
                         {'class': 'push_failed', 'critical': False, 'to': 'supervisor',
                          'step': 'implement'})
        self.assertEqual(record['key']['step'], 'implement')
        body = self.gh.threads[7][-1]['body']
        self.assertIn('`push_failed`', body)
        self.assertIn(SHA2[:12], body)  # the commit kept
        self.assertIn('/devloop retry', body)
        self.assertEqual(len(self.runner.calls), 1)
        self.gh.command(7, '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.decisions(7)[-1]['outcome'], 'applied')
        # Only the push again: no new turn of the implementer, no new commit.
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(self.ws.commits), 1)
        self.assertEqual(self.ws.pushes, [('agent/issue-7', True)] * 3)
        self.assertEqual(len(self.gh.created), 1)
        self.assertEqual(self.gh.labels[100], {REVIEW})
        self.assertEqual(self.gh.labels[7], set())
        self.assertIn('the retry pushes ' + SHA2[:12] + ' again, without a new turn',
                      '\n'.join(self.logs))
        self.assertEqual(d.pending_pushes.numbers(), [])

    def test_the_operator_retry_pushes_without_a_turn_too(self):
        # In strict the escalation goes to the operator, still non-critical and with its step.
        d = self.start(STRICT, FIVE_XX, FIVE_XX, None)
        self.assertEqual(self.tick(d), [('implement', 7)])
        self.assertEqual(self.gh.labels[7], {HUMAN})
        record = self.escalations(7)[-1]
        self.assertEqual((record['class'], record['critical'], record['step']),
                         ('push_failed', False, 'implement'))
        self.gh.command(7, '/devloop retry', login='brushtyler')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(self.gh.created), 1)
        self.assertNotIn(HUMAN, self.gh.labels[7])

    def test_a_retry_whose_push_fails_again_is_a_new_push_failed(self):
        d = self.start(AUTO, FIVE_XX, FIVE_XX, FIVE_XX, FIVE_XX, None)
        self.tick(d)
        self.gh.command(7, '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(len(self.ws.pushes), 4)
        self.assertEqual([r['class'] for r in self.escalations(7)], ['push_failed'] * 2)
        self.assertEqual(self.gh.labels[7], {SUPER})  # agent:ready went at the takeover
        # Kept again: the second retry pushes the same commit, still without a turn.
        self.gh.command(7, '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.decisions(7)[-1]['attempt'], 2)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(self.gh.created), 1)

    def test_a_worktree_gone_is_recreated_at_the_commit_kept(self):
        d = self.start(AUTO, FIVE_XX, FIVE_XX, None)
        self.tick(d)
        self.ws._head = SHA1  # the worktree moved, or was removed, meanwhile
        self.gh.command(7, '/devloop retry')
        self.tick(d)
        self.assertEqual(self.ws.copies, [('issue-7', SHA2)])
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(self.gh.created), 1)

    def test_a_rule_of_the_branch_stays_a_critical_failure(self):
        # Criterion 3 of #645: no second attempt, and to the operator even in auto.
        d = self.start(AUTO, RULE, None)
        self.assertEqual(self.tick(d), [('implement', 7)])
        self.assertEqual(len(self.ws.pushes), 1)
        self.assertEqual(self.gh.created, [])
        self.assertEqual(self.gh.labels[7], {HUMAN})
        record = self.escalations(7)[-1]
        self.assertEqual((record['class'], record['critical'], record['to']),
                         ('failure', True, 'operator'))
        self.assertNotIn('second attempt', '\n'.join(self.logs))
        self.assertEqual(d.pending_pushes.numbers(), [])

    def test_a_step_that_runs_again_drops_the_push_kept(self):
        # Without a retry (the operator put agent:ready back by hand) the step runs again with
        # a new turn, and the push kept is not used.
        d = self.start(AUTO, FIVE_XX, FIVE_XX, None)
        self.runner.outputs.append(implemented())
        self.tick(d)
        self.assertIn(7, d.pending_pushes.numbers())
        self.gh.labels[7] = {READY}
        d._do_implement(7)
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual(len(self.gh.created), 1)
        self.assertEqual(d.pending_pushes.numbers(), [])
        self.assertIn('kept after a transient error is dropped', '\n'.join(self.logs))


class SyncTests(Restart, Case):
    """The push of a PR's step: a sync without conflicts, which runs no agent, and its retry."""

    def failed(self, *errors, landed=False):
        """The sync of PR #100 whose push failed twice for ``errors``, escalated. ``landed``:
        the push reached the branch anyway, whose head on GitHub is the merge kept (``SHA2``):
        its merge mode, read on a head that is not the snapshot's, is not computable, and the
        escalation goes to the operator."""
        d = self.make(AUTO)
        self.ws = d.ws = PushWS(*errors)
        self.ws.conflicts = ()
        d.push_retry_pause = 0
        d.pending_pushes = self.kept()
        p = pr(merge='conflict')
        self.gh.labels[100] = set(p.labels)
        with self.assertRaises(TransientPushError) as ctx:
            d._do_sync(p)
        if landed:
            self.gh.head = SHA2
        else:
            self.ws._head = SHA1  # the branch on GitHub did not move: the push failed
        d._escalate_failure(Action('sync', 100), p, ctx.exception)
        return d, p

    def test_a_retry_after_a_restart_pushes_the_merge_already_made(self):
        self.failed(FIVE_XX, FIVE_XX, None)
        again = self.restarted()
        self.gh.command(100, '/devloop retry')
        self.assertEqual(self.tick(again), [('supervise', 100)])
        again._do_sync(pr(merge='conflict'))
        self.assertEqual(self.ws.copies, [('pr-100', SHA2)])
        self.assertEqual(len(self.ws.commits), 1)
        self.assertEqual(len(self.ws.pushes), 3)
        self.assertIn('Il merge locale non aveva conflitti.', self.gh.threads[100][-1]['body'])
        self.assertEqual(self.kept().numbers(), [])

    def test_a_push_that_reached_github_is_recorded_by_the_retry_without_a_push(self):
        # Both attempts lost the connection, but the push reached the branch: its head is the
        # merge kept, and the retry records the sync with no new push.
        self.failed(LOST, LOST, landed=True)
        self.assertEqual(self.escalations(100)[-1]['class'], 'push_failed')
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.gh.command(100, '/devloop retry', login='brushtyler')
        self.assertEqual(self.tick(self.restarted()), [('supervise', 100)])
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'applied')
        self.assertEqual(len(self.ws.pushes), 2)
        self.assertEqual(len(self.ws.commits), 1)
        self.assertIn('Il merge locale non aveva conflitti.', self.gh.threads[100][-1]['body'])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        self.assertIn('had already reached agent/issue-7', '\n'.join(self.logs))
        self.assertEqual(self.kept().numbers(), [])

    def test_a_head_that_is_not_the_push_kept_still_refuses_the_retry(self):
        self.failed(LOST, LOST, landed=True)
        self.gh.head = 'f' * 40  # someone else's commit
        self.gh.command(100, '/devloop retry', login='brushtyler')
        self.assertEqual(self.tick(self.restarted()), [('supervise', 100)])
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(len(self.ws.pushes), 2)
        self.assertEqual(self.gh.records(100, 'sync'), [])
        self.assertEqual(self.kept().numbers(), [100])

    def test_the_retry_of_a_sync_pushes_the_merge_already_made(self):
        d, p = self.failed(FIVE_XX, FIVE_XX, None)
        record = self.escalations(100)[-1]
        self.assertEqual((record['class'], record['to'], record['step'], record['key']['head']),
                         ('push_failed', 'supervisor', 'sync', SHA1))
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.gh.command(100, '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        # The sync, planned again, pushes the merge it made, without merging again.
        d._do_sync(p)
        self.assertEqual(self.ws.copies, [('pr-100', SHA2)])
        self.assertEqual(len(self.ws.merge_authors), 1)
        self.assertEqual(len(self.ws.commits), 1)
        self.assertEqual(len(self.ws.pushes), 3)
        self.assertIn('Il merge locale non aveva conflitti.', self.gh.threads[100][-1]['body'])
        self.assertEqual(d.pending_pushes.numbers(), [])


def changes():
    return pr(labels=['agent:changes'], reviews=[norm(raw_review('request_changes',
                                                                 [finding()]))])


class FixTests(Restart, Case):
    def failed(self, *errors, landed=False):
        """The fix of PR #100 whose push failed twice for ``errors``, escalated to the operator
        (``STRICT``). ``landed``: the push reached the branch anyway, whose head on GitHub is
        the commit kept (``SHA2``)."""
        d = self.make(STRICT, fixed())
        self.ws = d.ws = PushWS(*errors)
        d.push_retry_pause = 0
        d.pending_pushes = self.kept()
        p = changes()
        self.gh.labels[100] = set(p.labels)
        with self.assertRaises(TransientPushError) as ctx:
            d._do_fix(p)
        self.assertEqual(self.gh.records(100, 'fix'), [])  # nothing recorded before the push
        if landed:
            self.gh.head = SHA2
        else:
            self.ws._head = SHA1  # the branch on GitHub did not move: the push failed
        d._escalate_failure(Action('fix', 100), p, ctx.exception)
        return d, p

    def test_a_retry_after_a_restart_pushes_the_fix_and_records_it_without_a_turn(self):
        self.failed(FIVE_XX, FIVE_XX, None)
        again = self.restarted()
        self.gh.command(100, '/devloop retry', login='brushtyler')
        self.assertEqual(self.tick(again), [('supervise', 100)])
        again._do_fix(changes())
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(self.ws.commits), 1)
        self.assertEqual(len(self.ws.pushes), 3)
        (fix,) = self.gh.records(100, 'fix')
        self.assertEqual((fix['round'], fix['head_sha']), (1, SHA2))
        self.assertEqual(self.gh.labels[100], {REVIEW})
        self.assertEqual(self.kept().numbers(), [])

    def test_a_push_that_reached_github_is_recorded_by_the_retry_without_a_push(self):
        # Both attempts lost the connection, but the push reached the branch (#645): the
        # retry, refused before as a superseded input, records the fix.
        self.failed(LOST, LOST, landed=True)
        self.gh.command(100, '/devloop retry', login='brushtyler')
        self.assertEqual(self.tick(self.restarted()), [('supervise', 100)])
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'applied')
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(self.ws.pushes), 2)
        (fix,) = self.gh.records(100, 'fix')
        self.assertEqual((fix['round'], fix['head_sha']), (1, SHA2))
        self.assertEqual(self.gh.labels[100], {REVIEW})
        self.assertEqual(self.kept().numbers(), [])

    def test_the_retry_of_a_fix_pushes_its_commit_and_records_it_without_a_turn(self):
        d, p = self.failed(FIVE_XX, FIVE_XX, None)
        record = self.escalations(100)[-1]
        self.assertEqual((record['class'], record['critical'], record['step'], record['resume']),
                         ('push_failed', False, 'fix', 'agent:changes'))
        self.gh.command(100, '/devloop retry', login='brushtyler')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        # The fix, planned again, pushes the commit of its turn and records it.
        d._do_fix(p)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(self.ws.commits), 1)
        self.assertEqual(len(self.ws.pushes), 3)
        (fix,) = self.gh.records(100, 'fix')
        self.assertEqual(fix['round'], 1)
        self.assertEqual(self.gh.labels[100], {REVIEW})
        self.assertEqual(d.pending_pushes.numbers(), [])


class VetoTests(ResumeBase):
    def test_a_veto_written_during_the_pause_stops_the_second_attempt(self):
        # Nothing produced with a vetoed resolution is published (#142), not even by the second
        # attempt of a push (#645).
        cid = self.unanimous_issue(implemented(), diff=DIFF)
        errors, push = [git_error(FIVE_XX)], self.ws.push

        def failing(path, branch, replace=False, auth=None):
            push(path, branch, replace, auth)
            if errors:
                raise errors.pop(0)

        self.ws.push = failing
        self.d.push_retry_pause = 0

        def pause(seconds):
            self.gh.say(7, '/devloop veto', 'brushtyler')

        with mock.patch('devloop.dispatch_actions.time.sleep', pause):
            self.d._do_conclave(7)
        self.assertEqual(len(self.ws.pushes), 1)
        self.assertEqual(self.gh.created, [])
        self.assertEqual(self.gh.labels[7], {NEEDS_OPERATOR})
        (escalation,) = self.escalations(7)
        self.assertEqual((escalation['class'], escalation['veto_of']), ('conclave_veto', cid))
        self.assertIn('second attempt of the push of agent/issue-7 not made', '\n'.join(self.logs))
        self.assertEqual(self.d.pending_pushes.numbers(), [])

    def landed_fix(self):
        """PR #100 whose fix, run with the resolution of a unanimous conclave, lost the
        connection on both attempts of its push, which reached the branch anyway: escalated as
        ``push_failed``, its commit kept. Returns the conclave comment and the PR."""
        self.make(proposal(), vote(), vote(), fixed())
        self.block_pr()
        self.run_conclave()
        cid = self.conclave_comment(100)
        self.d._do_conclave(self.target())
        errors, push = [git_error(LOST), git_error(LOST)], self.ws.push

        def failing(path, branch, replace=False, auth=None):
            push(path, branch, replace, auth)
            if errors:
                raise errors.pop(0)

        self.ws.push = failing
        self.d.push_retry_pause = 0
        p = pr(labels=['agent:changes'], reviews=[norm(raw_review('request_changes',
                                                                  [finding()]))])
        self.gh.labels[100] = set(p.labels)
        with self.assertRaises(TransientPushError) as ctx:
            self.d._do_fix(p)
        (kept,) = [self.d.pending_pushes.get(n) for n in self.d.pending_pushes.numbers()]
        self.gh.head = kept.commit  # the push reached the branch
        self.d._escalate_failure(Action('fix', 100), p, ctx.exception)
        self.assertEqual(self.escalations(100)[-1]['class'], 'push_failed')
        return cid, p

    def fixes(self):
        return [c for c in self.gh.thread.get(100, []) if markdown.read_marker(c['body'], 'fix')]

    def test_a_veto_before_a_retry_completes_a_landed_push_publishes_nothing(self):
        # The retry finds the push already on the branch (#645): a veto written after the push
        # suspends the step, as before any other publication of the resolution's work (#142).
        cid, p = self.landed_fix()
        arm = self.d._arm_pending_push

        def armed(*args):
            pending = arm(*args)
            self.gh.say(7, '/devloop veto', 'brushtyler')
            return pending

        self.d._arm_pending_push = armed
        self.gh.say(100, '/devloop retry', 'brushtyler')
        self.d._do_supervise(pr(labels=[NEEDS_OPERATOR]))
        self.assertEqual(self.fixes(), [])
        self.assertEqual(self.gh.labels[100], {NEEDS_OPERATOR})
        self.assertEqual([(e['class'], e.get('veto_of')) for e in self.escalations(100)][-1],
                         ('conclave_veto', cid))
        self.assertEqual(self.d.pending_pushes.numbers(), [])

    def test_a_veto_before_the_fix_completes_a_landed_push_publishes_nothing(self):
        # The same push completed by the fix the retry planned again (``_resume_push``).
        cid, p = self.landed_fix()
        self.d._arm_pending_push(100, 'fix', {'head': p.head_sha})
        self.gh.say(7, '/devloop veto', 'brushtyler')
        self.d._do_fix(dataclasses.replace(p, head_sha=self.gh.head))
        self.assertEqual(self.fixes(), [])
        self.assertEqual(self.gh.labels[100], {NEEDS_OPERATOR})
        self.assertEqual([(e['class'], e.get('veto_of')) for e in self.escalations(100)][-1],
                         ('conclave_veto', cid))
        self.assertEqual(self.d.pending_pushes.numbers(), [])


def kept_push(number=7, **changes):
    fields = dict(step='implement', number=number, key={'step': 'implement'}, name='issue-7',
                  path='/wt/issue-7', commit=SHA2, branch='agent/issue-7', agent='claude',
                  data={'conclave': None, 'pr': {'title': 't', 'body': 'b', 'attempt': None}},
                  replace=True)
    fields.update(changes)
    return PendingPush(**fields)


class StoreTests(unittest.TestCase):
    """The pushes kept in ``state_dir`` (#645, ``PendingPushes``)."""

    def setUp(self):
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)
        self.file = Path(self.state.name) / PendingPushes.FILE

    def test_a_push_kept_survives_a_new_store(self):
        PendingPushes(self.state.name).put(kept_push())
        self.assertEqual(PendingPushes(self.state.name).get(7), kept_push())
        self.assertTrue(PendingPushes(self.state.name).drop(7))
        self.assertIsNone(PendingPushes(self.state.name).get(7))
        self.assertEqual(json.loads(self.file.read_text()), {})

    def test_an_entry_that_is_not_valid_counts_as_missing(self):
        for entry in ({'step': 'implement'}, [], dict(vars(kept_push()), step='review'),
                      dict(vars(kept_push()), commit='')):
            with self.subTest(entry=entry):
                self.file.write_text(json.dumps({'7': entry}))
                self.assertIsNone(PendingPushes(self.state.name).get(7))
        self.file.write_text('not json')
        self.assertEqual(PendingPushes(self.state.name).numbers(), [])

    def test_a_push_that_cannot_be_written_lasts_in_the_process(self):
        store = PendingPushes(self.state.name)
        with mock.patch.object(PendingPushes, '_store', side_effect=OSError('full')):
            self.assertFalse(store.put(kept_push()))
            self.assertEqual(store.get(7), kept_push())
            self.assertIsNone(PendingPushes(self.state.name).get(7))
        store.put(kept_push(number=8))
        with mock.patch.object(PendingPushes, '_store', side_effect=OSError('full')):
            self.assertFalse(store.drop(8))
        # Dropped in this process, even if the file still has it.
        self.assertIsNone(store.get(8))
        self.assertIsNotNone(PendingPushes(self.state.name).get(8))

    def test_without_state_dir_it_lasts_in_memory(self):
        store = PendingPushes()
        self.assertTrue(store.put(kept_push()))
        self.assertEqual(store.numbers(), [7])
        store.drop(7)
        self.assertEqual(store.numbers(), [])


class ClassTests(unittest.TestCase):
    def test_push_failed_is_non_critical_and_retryable(self):
        self.assertFalse(supervision.critical(supervision.PUSH_FAILED))
        self.assertIn(supervision.PUSH_FAILED, supervision.RETRY_CLASSES)
        self.assertIn('GitHub', supervision.describe('push_failed', 'en'))
        self.assertIn('GitHub', supervision.describe('push_failed', 'it'))

    def test_the_escalation_comment_names_the_class(self):
        body = markdown.render_escalation('x', cls='push_failed')
        self.assertIn('`push_failed`', body)


if __name__ == '__main__':
    unittest.main()
