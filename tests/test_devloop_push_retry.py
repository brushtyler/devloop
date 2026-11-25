"""devloop: a transient error of GitHub on the push of a step's branch (#645).

The push of an implementation, a fix or a sync is tried once more after a transient error of the
remote (a 5xx answer, a connection lost). When the second attempt fails too, the escalation is
``push_failed``, non-critical, and ``/devloop retry`` pushes again the commits of the turn already
done, without a new turn. Any other refusal of the remote stays a ``failure``, critical."""

import unittest
from pathlib import Path

from devloop import markdown, supervision
from devloop.planning import Action
from devloop.workspace import TransientPushError, Workspace, WorkspaceError, transient_push
from test_devloop import SHA1, SHA2, finding, fixed, implemented, norm, pr, raw_review
from test_devloop_supervision import (AUTO, HUMAN, READY, REVIEW, STRICT, SUPER, Case, SupWS)

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


class ImplementTests(Case):
    def start(self, cfg, *errors):
        d = self.make(cfg, implemented(), has_pr=False)
        self.ws = d.ws = PushWS(*errors)
        d.push_retry_pause = 0
        return d

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
        self.assertEqual(d._pending_pushes, {})

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
        self.assertEqual(d._pending_pushes, {})

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
        self.assertEqual(d._pending_pushes, {})

    def test_a_step_that_runs_again_drops_the_push_kept(self):
        # Without a retry (the operator put agent:ready back by hand) the step runs again with
        # a new turn, and the push kept is not used.
        d = self.start(AUTO, FIVE_XX, FIVE_XX, None)
        self.runner.outputs.append(implemented())
        self.tick(d)
        self.assertIn(7, d._pending_pushes)
        self.gh.labels[7] = {READY}
        d._do_implement(7)
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual(len(self.gh.created), 1)
        self.assertEqual(d._pending_pushes, {})
        self.assertIn('kept after a transient error is dropped', '\n'.join(self.logs))


class SyncTests(Case):
    """The push of a PR's step: a sync without conflicts, which runs no agent, and its retry."""

    def test_the_retry_of_a_sync_pushes_the_merge_already_made(self):
        d = self.make(AUTO)
        self.ws = d.ws = PushWS(FIVE_XX, FIVE_XX, None)
        self.ws.conflicts = ()
        d.push_retry_pause = 0
        p = pr(merge='conflict')
        self.gh.labels[100] = set(p.labels)
        with self.assertRaises(TransientPushError) as ctx:
            d._do_sync(p)
        self.ws._head = SHA1  # the branch on GitHub did not move: the push failed
        d._escalate_failure(Action('sync', 100), p, ctx.exception)
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
        self.assertEqual(d._pending_pushes, {})


class FixTests(Case):
    def test_the_retry_of_a_fix_pushes_its_commit_and_records_it_without_a_turn(self):
        d = self.make(STRICT, fixed())
        self.ws = d.ws = PushWS(FIVE_XX, FIVE_XX, None)
        d.push_retry_pause = 0
        p = pr(labels=['agent:changes'], reviews=[norm(raw_review('request_changes',
                                                                  [finding()]))])
        self.gh.labels[100] = set(p.labels)
        with self.assertRaises(TransientPushError) as ctx:
            d._do_fix(p)
        self.assertEqual(self.gh.records(100, 'fix'), [])  # nothing recorded before the push
        self.ws._head = SHA1  # the branch on GitHub did not move: the push failed
        d._escalate_failure(Action('fix', 100), p, ctx.exception)
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
        self.assertEqual(d._pending_pushes, {})


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
