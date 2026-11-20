"""devloop: escalation classes and the supervisor's decisions in merge mode auto (#182)."""

import dataclasses
import unittest

from devloop import agents, markdown, supervision
from devloop.config import MergePolicy
from devloop.context import PROMPT_DIR, _template, prompt_values
from devloop.dispatcher import Dispatcher
from devloop.github import GitHubError
from devloop.planning import Action, plan
from test_devloop import (CFG, REV, SHA1, SHA2, FakeRunner, FakeWS, SnapshotGH, implemented,
                          norm, pr, raw_review, receipt)
from test_devloop_preflight import checked, gap

SUPERVISOR = 'agy'  # neither the implementer nor the level-1 reviewer of the tests
SUPERVISOR_LOGIN = 'agy-agent[bot]'
AUTO = dataclasses.replace(CFG, merge=MergePolicy(mode='auto'), supervisor=SUPERVISOR)
STRICT = dataclasses.replace(CFG, merge=MergePolicy(mode='strict'))
PREFLIGHT_AUTO = dataclasses.replace(AUTO, preflight=REV)
HUMAN, SUPER = 'agent:needs-operator', 'agent:needs-supervisor'
REVIEW, READY = 'agent:review', 'agent:ready'


def header():
    """The attribution header the supervisor writes above its commands (AGENTS.md)."""
    return markdown.header('Antigravity', '9.9', 'm', 'high')


class SupGH(SnapshotGH):
    """Issue #7, opened by a trusted user, and, with ``has_pr``, its PR #100 at SHA1. Every
    comment lands in the thread of its number with an id larger than every earlier one, as
    GitHub assigns them; devloop's own comments use the ambient (trusted) login."""

    def __init__(self, has_pr=True):
        super().__init__([])
        self.has_pr = has_pr
        self.threads = {7: [], 100: []}
        self.author = 'brushtyler'
        if has_pr:
            self.labels[100] = {REVIEW}
        self.labels[7] = set() if has_pr else {READY}

    def say(self, number, login, body):
        ident = max((c['id'] for t in self.threads.values() for c in t), default=1000) + 1
        self.threads.setdefault(number, []).append(
            {'id': ident, 'author': {'login': login}, 'body': body})

    def comment(self, number, body):
        super().comment(number, body)
        self.say(number, 'brushtyler', body)

    def command(self, number, text, login=SUPERVISOR_LOGIN):
        self.say(number, login, header() + '\n' + text)

    def pr_comments(self, number):
        return [dict(c) for c in self.threads.get(number, [])]

    issue_comments = pr_comments

    def open_prs(self):
        return super().open_prs() if self.has_pr else []

    def pr_state(self, number):
        return {'state': 'OPEN', 'headRefOid': self.head,
                'labels': [{'name': n} for n in sorted(self.labels.get(number, ()))]}

    def issue(self, number):
        return dict(super().issue(number),
                    labels=[{'name': n} for n in sorted(self.labels.get(number, ()))])

    def issue_history(self, number):
        return dict(super().issue_history(number), author=self.author)

    def issues_with_label(self, label):
        if label in self.labels.get(7, ()):
            return [{'number': 7, 'title': 'Issue 7',
                     'labels': [{'name': n} for n in sorted(self.labels[7])]}]
        return []

    def labeled_events(self, number):
        return [{'label': READY, 'by': 'brushtyler', 'at': '2026-11-13T10:00:00Z'}]

    def records(self, number, kind):
        return [r for c in self.threads.get(number, [])
                if (r := markdown.read_marker(c['body'], kind))]


class SupWS(FakeWS):
    """A checkout of the base, for a readiness check or a new branch, starts at the base it
    was created from, until a commit moves it."""

    def __init__(self):
        super().__init__()
        self.started = {}

    def base_head(self):
        return self.base

    def new_branch(self, name, branch):
        path = super().new_branch(name, branch)
        self.started[path] = self.base
        return path

    def base_copy(self, name):
        path = super().base_copy(name)
        self.started[path] = self.base
        return path

    def head(self, path):
        return self.started.get(path) or super().head(path)

    def commit_all(self, path, message, author=None):
        self.started.pop(path, None)
        return super().commit_all(path, message, author)


class StepRunner(FakeRunner):
    """A runner whose outputs may be callables, run as the turn runs (something that changes
    while the agent works) before the next output is returned."""

    def run(self, *args):
        while self.outputs and callable(self.outputs[0]):
            self.outputs.pop(0)()
        return super().run(*args)


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

    def git_auth(self, actor, permissions=None):
        return None


def crash():
    return agents.AgentError('codex crashed')


class Case(unittest.TestCase):
    def make(self, cfg=AUTO, *outputs, has_pr=True):
        self.cfg = cfg
        self.gh, self.ws = SupGH(has_pr), SupWS()
        self.runner, self.logs = StepRunner(*outputs), []
        return Dispatcher(cfg, self.gh, self.ws, self.runner, log=self.logs.append,
                          identities=Identities(self.gh))

    def escalations(self, number):
        return [r for r in self.gh.records(number, 'notice') if r['event'] == 'escalation']

    def decisions(self, number):
        return self.gh.records(number, 'supervision')

    def tick(self, d):
        return [(a.kind, a.number) for a in d.tick()]


class ClassTests(Case):
    def test_only_the_non_critical_classes_are_not_critical(self):
        for cls in supervision.NON_CRITICAL:
            self.assertFalse(supervision.critical(cls))
        for cls in (*supervision.CRITICAL, 'mystery', '', None):
            with self.subTest(cls=cls):
                self.assertTrue(supervision.critical(cls))

    def test_every_escalation_records_its_class(self):
        d = self.make()
        d._do_round_limit(pr())
        d._escalate_failure(Action('review', 100), pr(), crash())
        d._blocked(7, CFG.agent(CFG.implementer), 'unclear', remove=[], on_pr=False)
        classes = [r['class'] for r in self.gh.records(100, 'notice')]
        self.assertEqual(classes, ['round_limit', 'tool_failure'])
        self.assertEqual([r['class'] for r in self.gh.records(7, 'notice')], ['blocked'])
        for record in self.gh.records(100, 'notice'):
            self.assertEqual(record['critical'], supervision.critical(record['class']))
        body = self.gh.threads[100][0]['body']
        self.assertIn('**Classe:** `round_limit`', body)

    def test_a_reviewer_asking_for_a_decision_is_a_critical_escalation(self):
        d = self.make(AUTO, raw_review('needs_operator', human='product call'))
        self.assertEqual(self.tick(d), [('review', 100)])
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual([r['class'] for r in self.escalations(100)], ['reviewer_decision'])

    def test_an_unknown_class_is_critical_and_refuses_every_command(self):
        d = self.make()
        self.gh.labels[100] = {SUPER}
        self.gh.say(100, 'brushtyler', markdown.publishable(
            markdown.dispatcher_header() + '\nx', 'notice',
            {'event': 'escalation', 'class': 'mystery', 'to': 'supervisor', 'step': 'review',
             'key': {'step': 'review', 'head': SHA1}, 'resume': REVIEW}))
        self.gh.command(100, '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertEqual(self.escalations(100)[-1]['class'], 'supervisor_refused')
        self.assertEqual(self.runner.calls, [])
        # Routing never sends an unknown class to the supervisor.
        self.assertEqual(d._supervised(100, 'mystery', 'review', pr()), (None, ''))


class RoutingTests(Case):
    def test_in_auto_a_tool_failure_goes_to_the_supervisor(self):
        d = self.make(AUTO, crash())
        self.assertEqual(self.tick(d), [('review', 100)])
        self.assertEqual(self.gh.labels[100], {SUPER})
        (record,) = self.escalations(100)
        self.assertEqual({k: record[k] for k in ('class', 'critical', 'to', 'step', 'resume')},
                         {'class': 'tool_failure', 'critical': False, 'to': 'supervisor',
                          'step': 'review', 'resume': REVIEW})
        self.assertEqual(record['key'], {'step': 'review', 'head': SHA1, 'reviewer': REV})
        self.assertIn('/devloop retry', self.gh.threads[100][-1]['body'])
        # Nothing runs on the PR until the supervisor decides.
        self.assertEqual(self.tick(d), [])

    def test_in_auto_the_round_limit_goes_to_the_operator(self):
        d = self.make(AUTO)
        d._do_round_limit(pr())
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(self.escalations(100)[-1]['to'], 'operator')

    def test_in_strict_and_off_both_go_to_the_operator(self):
        for cfg in (STRICT, CFG):
            with self.subTest(mode=cfg.merge.mode):
                d = self.make(cfg, crash())
                self.tick(d)
                self.assertEqual(self.gh.labels[100], {HUMAN})
                record = self.escalations(100)[-1]
                self.assertEqual((record['class'], record['to']), ('tool_failure', 'operator'))
                # The step stays recorded, for a trusted user's retry (#321).
                self.assertEqual(record['key'], {'step': 'review', 'head': SHA1,
                                                 'reviewer': REV})
                self.assertIn('`/devloop retry`', self.gh.threads[100][-1]['body'])
                d._do_round_limit(pr())
                self.assertEqual(self.escalations(100)[-1]['class'], 'round_limit')
                self.assertEqual(self.gh.labels[100], {HUMAN})

    def test_what_makes_a_tool_failure_critical_in_auto(self):
        cases = {
            'untrusted author': lambda gh, ws: setattr(gh, 'author', 'stranger'),
            'phase:design': lambda gh, ws: gh.labels[7].add('phase:design'),
            # Under operator_only_paths but not a sensitive path: still a level-1 review.
            'operator-only path': lambda gh, ws: setattr(ws, 'diff', [
                {'path': 'docs/adr/0009-x.md', 'links': []}]),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                d = self.make(AUTO, crash())
                change(self.gh, self.ws)
                self.tick(d)
                self.assertEqual(self.gh.labels[100], {HUMAN})
                record = self.escalations(100)[-1]
                self.assertEqual((record['class'], record['to']), ('tool_failure', 'operator'))
                self.assertIn('andrebbe al supervisore', self.gh.threads[100][-1]['body'])

    def test_an_agent_that_could_not_start_is_a_tool_failure_even_in_a_sensitive_sync(self):
        # ``devloop/agents.py``, the file in conflict, is a sensitive path: a sync that fails
        # is ``sync_sensitive``, unless its agent never started (#218).
        cases = {
            'start': (agents.StartFailure('claude could not start (again, after one retry): '
                                          "[Errno 2] No such file or directory: 'claude'"),
                      'tool_failure'),
            'wrapped': (agents.ModelsUnavailable('claude', [('m', 'unavailable')]),
                        'tool_failure'),
            'crash': (crash(), 'sync_sensitive'),
        }
        cases['wrapped'][0].__cause__ = agents.StartFailure('claude could not start')
        for cfg in (AUTO, STRICT):
            for name, (error, cls) in cases.items():
                with self.subTest(mode=cfg.merge.mode, case=name):
                    d = self.make(cfg, error)
                    p = pr(merge='conflict')
                    self.gh.labels[100] = set(p.labels)
                    d._do_sync(p)
                    record = self.escalations(100)[-1]
                    self.assertEqual((record['class'], record['critical']),
                                     (cls, supervision.critical(cls)))
                    body = self.gh.threads[100][-1]['body']
                    self.assertIn('`devloop/agents.py`', body)
                    self.assertEqual(self.ws.pushes, [])
                    if cfg is STRICT:
                        self.assertEqual(self.gh.labels[100], {HUMAN})

    def test_a_pr_the_supervisor_implemented_goes_to_the_operator(self):
        d = self.make(dataclasses.replace(AUTO, supervisor=CFG.implementer), crash())
        self.tick(d)
        self.assertEqual(self.gh.labels[100], {HUMAN})


class RetryTests(Case):
    def fail_review(self, d):
        self.assertEqual(self.tick(d), [('review', 100)])
        self.assertEqual(self.gh.labels[100], {SUPER})

    def test_retry_runs_the_step_again_without_counting_a_round(self):
        d = self.make(AUTO, crash(), raw_review())
        self.fail_review(d)
        self.gh.command(100, '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        (decision,) = self.decisions(100)
        self.assertEqual({k: decision[k] for k in ('command', 'by', 'outcome', 'attempt')},
                         {'command': 'retry', 'by': SUPERVISOR_LOGIN, 'outcome': 'applied',
                          'attempt': 1})
        self.assertEqual(decision['comment'], self.gh.threads[100][-2]['id'])
        # The review runs again: it is round 1, the failed attempt counted no round.
        self.assertEqual(self.tick(d), [('review', 100)])
        (review,) = self.gh.records(100, 'review')
        self.assertEqual(review['round'], 1)
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        # The command is spent: it is never applied again.
        self.assertNotIn(('supervise', 100), self.tick(d))

    def test_the_third_retry_is_not_run_and_goes_to_the_operator(self):
        d = self.make(AUTO, crash(), crash(), crash())
        self.fail_review(d)
        for attempt in (1, 2):
            self.gh.command(100, '/devloop retry')
            self.assertEqual(self.tick(d), [('supervise', 100)])
            self.assertEqual(self.decisions(100)[-1]['attempt'], attempt)
            self.fail_review(d)
        self.assertEqual(len(self.runner.calls), 3)
        self.gh.command(100, '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(len(self.runner.calls), 3)  # not run a fourth time
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'limit')
        self.assertEqual(self.escalations(100)[-1]['class'], 'retry_limit')
        self.assertTrue(self.escalations(100)[-1]['critical'])

    def test_each_reviewer_keeps_its_own_retries(self):
        # Two reviewers fail together, then one of them fails alone: its retries go on from
        # the first one, so it runs at most twice more on the same head.
        d = self.make(AUTO)

        def fail(reviewers):
            exc = crash()
            exc.reviewer = reviewers
            d._escalate_failure(Action('review', 100), pr(), exc)
            self.assertEqual(self.gh.labels[100], {SUPER})
            self.assertEqual(self.escalations(100)[-1]['key'],
                             {'step': 'review', 'head': SHA1, 'reviewer': reviewers})

        fail('codex,claude')
        self.gh.command(100, '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.decisions(100)[-1]['attempt'], 1)
        fail('claude')  # codex reviewed, claude failed again
        self.gh.command(100, '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.decisions(100)[-1]['attempt'], 2)
        self.assertEqual(self.gh.labels[100], {REVIEW})
        fail('claude')
        self.gh.command(100, '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'limit')
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(self.escalations(100)[-1]['class'], 'retry_limit')
        # A reviewer that failed only once has its own count.
        self.assertEqual(supervision.retries_done(
            {'step': 'review', 'head': SHA1, 'reviewer': 'codex'},
            [{'step': 'review', 'head': SHA1, 'reviewer': 'codex,claude'},
             {'step': 'review', 'head': SHA1, 'reviewer': 'claude'}]), 1)

    def test_a_retry_is_refused_when_the_escalation_became_critical(self):
        cases = {
            'phase:design': lambda gh: gh.labels[7].add('phase:design'),
            'untrusted author': lambda gh: setattr(gh, 'author', 'stranger'),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                d = self.make(AUTO, crash())
                self.fail_review(d)
                change(self.gh)  # while the escalation waits for the supervisor
                self.gh.command(100, '/devloop retry')
                self.assertEqual(self.tick(d), [('supervise', 100)])
                self.assertEqual(self.gh.labels[100], {HUMAN})
                self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
                self.assertEqual(self.escalations(100)[-1]['class'], 'supervisor_refused')
                self.assertIn('diventata critica', self.gh.threads[100][-2]['body'])
                self.assertEqual(len(self.runner.calls), 1)  # not run again

    def test_a_retry_checks_the_level_the_thread_records(self):
        # The snapshot does not read the thread of a PR in agent:needs-supervisor: the retry
        # reads it again, so that a level 2 recorded earlier keeps its merge mode.
        cfg = dataclasses.replace(AUTO, merge=MergePolicy(mode='auto', levels=((2, 'strict'),)))
        d = self.make(cfg, crash())
        self.fail_review(d)
        record = norm(raw_review(verdict='request_changes'))
        record['receipt'] = receipt(previous=2)
        self.assertEqual(record['receipt']['level'], 2)
        self.gh.say(100, 'brushtyler', markdown.publishable('review', 'review', record))
        self.gh.command(100, '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('`strict`', self.gh.threads[100][-2]['body'])
        self.assertEqual(len(self.runner.calls), 1)  # not run again

    def test_a_retry_checks_the_labels_of_the_pr_read_now(self):
        d = self.make(AUTO, crash())
        self.fail_review(d)
        self.gh.command(100, '/devloop retry')
        (stale,) = [p for p in d.snapshot().prs if p.number == 100]
        self.gh.labels[100].add('phase:design')  # on the PR, after the snapshot
        d._do_supervise(stale)
        self.assertEqual(self.gh.labels[100], {HUMAN, 'phase:design'})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('phase:design', self.gh.threads[100][-2]['body'])

    def test_a_retry_on_a_pr_that_lost_its_marker_goes_to_the_operator(self):
        d = self.make(AUTO, crash())
        self.fail_review(d)
        raws = SupGH.open_prs(self.gh)
        self.gh.open_prs = lambda: [dict(raw, body='b') for raw in raws]
        self.gh.command(100, '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('marker', self.gh.threads[100][-2]['body'])
        self.assertEqual(len(self.runner.calls), 1)  # not run again

    def test_a_command_from_someone_not_allowed_is_ignored(self):
        d = self.make(AUTO, crash())
        self.fail_review(d)
        self.gh.command(100, '/devloop retry', login='stranger')
        self.assertEqual(self.tick(d), [])
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual(self.decisions(100), [])
        self.assertTrue(any('ignored `/devloop retry` by stranger' in line for line in self.logs))
        # A trusted user may decide as the supervisor does.
        self.gh.command(100, '/devloop retry', login='brushtyler')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {REVIEW})

    def test_a_retry_of_a_superseded_head_is_refused(self):
        d = self.make(AUTO, crash())
        self.fail_review(d)
        self.gh.head = SHA2
        self.gh.command(100, '/devloop retry')
        self.tick(d)
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('input superato', self.gh.threads[100][-1]['body'])

    def test_a_refused_command_says_how_to_resume(self):
        retry_or_escalate = 'commento `/devloop retry` oppure `/devloop escalate <motivo>`.'
        cases = {
            'accept-defaults on a tool failure': ('/devloop accept-defaults', None,
                                                  retry_or_escalate),
            'retry on defaults': ('/devloop retry', None,
                                  'commento `/devloop accept-defaults` oppure `/devloop decide '
                                  '<decisioni>` oppure `/devloop escalate <motivo>`.'),
            'retry of a superseded head': ('/devloop retry', SHA2,
                                           'commento `/devloop escalate <motivo>`.'),
        }
        for name, (text, head, how) in cases.items():
            with self.subTest(case=name):
                if name == 'retry on defaults':
                    number = 7
                    d = self.make(PREFLIGHT_AUTO, checked(False, [gap('default_available')]),
                                  has_pr=False)
                    self.assertEqual(self.tick(d), [('preflight', 7)])
                else:
                    number = 100
                    d = self.make(AUTO, crash())
                    self.fail_review(d)
                if head:
                    self.gh.head = head
                before = set(self.gh.labels[number])
                self.gh.command(number, text)
                self.assertEqual(self.tick(d), [('supervise', number)])
                self.assertEqual(self.gh.labels[number], before)
                self.assertEqual(self.decisions(number)[-1]['outcome'], 'refused')
                body = self.gh.threads[number][-1]['body']
                self.assertIn('l\'escalation resta al supervisore', body)
                self.assertIn(how, ' '.join(body.split()))
                self.assertIn('rimuovere `agent:needs-supervisor`', body)

    def test_commands_are_answered_and_not_applied_outside_auto(self):
        d = self.make(AUTO, crash())
        self.fail_review(d)
        d.cfg = dataclasses.replace(AUTO, merge=MergePolicy(mode='strict'))
        self.gh.command(100, '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertIn('la modalità di merge è `strict`', self.gh.threads[100][-1]['body'])
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertEqual(len(self.runner.calls), 1)  # not run again
        self.assertEqual(self.tick(d), [])

    def test_an_issue_step_is_retried_on_the_same_base_and_request(self):
        d = self.make(AUTO, crash(), implemented(), has_pr=False)
        self.assertEqual(self.tick(d), [('implement', 7)])
        # agent:ready went at the takeover; the issue waits for the supervisor.
        self.assertEqual(self.gh.labels[7], {SUPER})
        record = self.escalations(7)[-1]
        self.assertEqual(record['key']['step'], 'implement')
        self.assertEqual(record['key']['base'], self.ws.base)
        self.gh.command(7, '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(len(self.gh.created), 1)
        self.assertEqual(self.gh.labels[100], {REVIEW})
        self.assertEqual(self.decisions(7)[-1]['outcome'], 'applied')

    def test_a_block_asking_the_operator_about_background_work_stays_critical(self):
        # Issue #219: only a turn that ended with its own work still running is a tool_failure;
        # a request for the operator's decision that mentions background work stays a block.
        reason = ('Serve una decisione dell\'operatore prima che venga introdotto il job in '
                  'background.')
        d = self.make(AUTO, dict(implemented('blocked'), blocked_reason=reason), has_pr=False)
        self.assertEqual(self.tick(d), [('implement', 7)])
        self.assertEqual(self.gh.labels[7], {HUMAN})
        self.assertEqual(self.escalations(7), [])
        (record,) = self.gh.records(7, 'notice')
        self.assertEqual((record['event'], record['class']), ('blocked', 'blocked'))
        self.assertTrue(record['critical'])
        self.assertEqual(self.gh.created, [])

    def test_a_block_for_unfinished_background_work_goes_to_the_supervisor(self):
        reason = ('Il risultato finale è stato richiesto prima che l\'audit parallelo si '
                  'concludesse.')
        d = self.make(AUTO, dict(implemented('blocked'), blocked_reason=reason), has_pr=False)
        self.assertEqual(self.tick(d), [('implement', 7)])
        self.assertEqual(self.gh.labels[7], {SUPER})
        record = self.escalations(7)[-1]
        self.assertEqual(record['class'], 'tool_failure')
        self.assertFalse(record['critical'])

    def test_an_implement_retry_on_a_new_base_is_applied(self):
        # #475: main advanced while the escalation waited. The implementation starts again
        # from the current base: the base is not an input the retry is bound to.
        d = self.make(AUTO, crash(), implemented(), has_pr=False)
        self.assertEqual(self.tick(d), [('implement', 7)])
        old = self.escalations(7)[-1]['key']['base']
        self.ws.base = 'e' * 40
        self.gh.command(7, '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        decision = self.decisions(7)[-1]
        self.assertEqual((decision['outcome'], decision['attempt']), ('applied', 1))
        self.assertEqual(decision['key']['base'], old)
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual(len(self.gh.created), 1)
        self.assertEqual(self.gh.labels[100], {REVIEW})

    def test_an_implement_retry_on_a_new_text_is_refused(self):
        # The text of the issue still binds the retry, whatever the base does.
        d = self.make(AUTO, crash(), has_pr=False)
        self.tick(d)
        self.ws.base = 'e' * 40
        self.gh.say(7, 'brushtyler', 'Anche Y.')
        self.gh.command(7, '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.gh.labels[7], {SUPER})
        self.assertEqual(self.decisions(7)[-1]['outcome'], 'refused')
        body = self.gh.threads[7][-1]['body']
        self.assertIn('(`request`)', body)
        self.assertIn('input superato', body)
        self.assertEqual(len(self.runner.calls), 1)

    def test_implement_retries_count_across_bases(self):
        # A base that moves between two timeouts starts no new count of retries.
        d = self.make(AUTO, crash(), crash(), crash(), has_pr=False)
        self.assertEqual(self.tick(d), [('implement', 7)])
        for attempt, base in ((1, 'e' * 40), (2, 'f' * 40)):
            self.ws.base = base
            self.gh.command(7, '/devloop retry')
            self.assertEqual(self.tick(d), [('supervise', 7)])
            self.assertEqual(self.decisions(7)[-1]['attempt'], attempt)
            self.assertEqual(self.escalations(7)[-1]['key']['base'], base)
        self.ws.base = 'd' * 40
        self.gh.command(7, '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.decisions(7)[-1]['outcome'], 'limit')
        self.assertEqual(len(self.runner.calls), 3)
        self.assertEqual(self.gh.labels[7], {HUMAN})

    def test_only_the_base_of_an_implementation_leaves_its_retry_input(self):
        key = {'step': 'implement', 'base': 'b', 'request': 'r'}
        self.assertEqual(supervision.changed_input(dict(key, base='c'), key), [])
        self.assertEqual(supervision.changed_input(dict(key, base='c', request='s'), key),
                         ['request'])
        check = {'step': 'preflight', 'base': 'b', 'request': 'r'}
        self.assertEqual(supervision.changed_input(dict(check, base='c'), check), ['base'])
        head = {'step': 'fix', 'head': 'h'}
        self.assertEqual(supervision.changed_input(dict(head, head='i'), head), ['head'])

    def test_an_issue_retry_is_bound_to_the_input_the_step_ran_on(self):
        # The request changes while the step runs, or the base of a readiness check: the
        # escalation records the input the step got, so the retry refers to a superseded input
        # and is refused. The base of an implementation does not bind its retry (#475).
        new_base = 'e' * 40
        changes = {
            'implement, request': (AUTO, lambda: self.gh.say(7, 'brushtyler', 'Anche Y.'),
                                   'request'),
            'preflight, base': (PREFLIGHT_AUTO, lambda: setattr(self.ws, 'base', new_base),
                                'base'),
        }
        for name, (cfg, change, field) in changes.items():
            with self.subTest(case=name):
                d = self.make(cfg, change, crash(), implemented(), has_pr=False)
                step = name.split(',')[0]
                self.assertEqual(self.tick(d), [(step, 7)])
                self.assertEqual(self.gh.labels[7] & {SUPER, HUMAN}, {SUPER})
                key = self.escalations(7)[-1]['key']
                self.assertEqual(key['step'], step)
                if field == 'base':
                    self.assertNotEqual(key['base'], new_base)
                self.gh.command(7, '/devloop retry')
                self.assertEqual(self.tick(d), [('supervise', 7)])
                decision = self.decisions(7)[-1]
                self.assertEqual(decision['outcome'], 'refused')
                self.assertIn(f'`{field}`', self.gh.threads[7][-1]['body'])
                self.assertEqual(len(self.runner.calls), 1)
                self.assertEqual(self.gh.created, [])


class EscalateTests(Case):
    def test_escalate_hands_the_escalation_to_the_operator(self):
        d = self.make(AUTO, crash())
        self.tick(d)
        # Without a reason it is not applied, but answered (#321): the escalation stays.
        self.gh.command(100, '/devloop escalate')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('`/devloop escalate <testo>`', self.gh.threads[100][-1]['body'])
        self.assertEqual(self.tick(d), [])  # answered once
        self.gh.command(100, '/devloop escalate il modello è instabile')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {HUMAN})
        decision = self.decisions(100)[-1]
        self.assertEqual((decision['outcome'], decision['by'], decision['reason']),
                         ('escalated', SUPERVISOR_LOGIN, 'il modello è instabile'))
        record = self.escalations(100)[-1]
        self.assertEqual((record['class'], record['to']), ('supervisor_escalated', 'operator'))
        self.assertIn('il modello è instabile', self.gh.threads[100][-1]['body'])
        # The step stays recorded, for the operator's retry (#321).
        self.assertEqual((record['step'], record['resume']), ('review', REVIEW))
        self.assertIn('/devloop retry', self.gh.threads[100][-1]['body'])


class OperatorCommandTests(Case):
    """Commands on the escalations to the operator (#321)."""

    # The case of PR #318: in "auto", but the supervisor implemented the PR.
    OWN = dataclasses.replace(AUTO, supervisor=CFG.implementer)

    def fail_review(self, d):
        self.assertEqual(self.tick(d), [('review', 100)])
        self.assertEqual(self.gh.labels[100], {HUMAN})
        record = self.escalations(100)[-1]
        self.assertEqual((record['class'], record['to']), ('tool_failure', 'operator'))

    def answer(self, number):
        """The body of the dispatcher's answer to the latest command, which it spent."""
        decision = self.decisions(number)[-1]
        self.assertEqual(decision['outcome'], 'refused')
        self.assertEqual(decision['comment'], self.gh.threads[number][-2]['id'])
        return self.gh.threads[number][-1]['body']

    def test_a_trusted_users_retry_resumes_a_tool_failure_escalated_to_the_operator(self):
        for cfg in (self.OWN, STRICT, CFG):
            with self.subTest(mode=cfg.merge.mode, supervisor=cfg.supervisor):
                d = self.make(cfg, crash(), raw_review())
                self.fail_review(d)
                self.assertEqual(self.tick(d), [])  # nothing runs until someone decides
                self.gh.command(100, '/devloop retry', login='brushtyler')
                self.assertEqual(self.tick(d), [('supervise', 100)])
                self.assertEqual(self.gh.labels[100], {REVIEW})
                (decision,) = self.decisions(100)
                self.assertEqual({k: decision[k] for k in ('command', 'by', 'outcome', 'attempt')},
                                 {'command': 'retry', 'by': 'brushtyler', 'outcome': 'applied',
                                  'attempt': 1})
                self.assertIn('## Comando su un\'escalation all\'operatore',
                              self.gh.threads[100][-1]['body'])
                # The review runs again, as round 1: the failed attempt counted no round.
                self.assertEqual(self.tick(d), [('review', 100)])
                (review,) = self.gh.records(100, 'review')
                self.assertEqual(review['round'], 1)
                self.assertNotIn(('supervise', 100), self.tick(d))

    def test_the_operators_retries_share_the_limit(self):
        d = self.make(STRICT, crash(), crash(), crash())
        self.fail_review(d)
        for attempt in (1, 2):
            self.gh.command(100, '/devloop retry', login='brushtyler')
            self.assertEqual(self.tick(d), [('supervise', 100)])
            self.assertEqual(self.decisions(100)[-1]['attempt'], attempt)
            self.fail_review(d)
        self.gh.command(100, '/devloop retry', login='brushtyler')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(len(self.runner.calls), 3)  # not run a fourth time
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'limit')
        self.assertIn('rimuovere `agent:needs-operator`', self.gh.threads[100][-1]['body'])

    def test_the_operator_may_retry_a_critical_escalation(self):
        # The supervisor may not decide it; a trusted user may run the step again.
        d = self.make(AUTO, crash(), raw_review())
        d._escalate_failure(Action('review', 100), pr(), GitHubError('gh timed out'))
        record = self.escalations(100)[-1]
        self.assertEqual((record['class'], record['critical'], record['step']),
                         ('failure', True, 'review'))
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.gh.command(100, '/devloop retry', login='brushtyler')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'applied')

    def test_the_operator_retries_after_the_supervisor_escalated(self):
        d = self.make(AUTO, crash(), raw_review())
        self.assertEqual(self.tick(d), [('review', 100)])
        self.gh.command(100, '/devloop escalate serve l\'operatore')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.gh.command(100, '/devloop retry', login='brushtyler')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'applied')

    def test_an_operator_retry_of_an_issue_runs_its_step(self):
        d = self.make(STRICT, crash(), implemented(), has_pr=False)
        self.assertEqual(self.tick(d), [('implement', 7)])
        self.assertEqual(self.gh.labels[7], {HUMAN})
        self.assertEqual(self.escalations(7)[-1]['key']['step'], 'implement')
        self.gh.command(7, '/devloop retry', login='brushtyler')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(len(self.gh.created), 1)
        self.assertNotIn(HUMAN, self.gh.labels[7])
        self.assertEqual(self.decisions(7)[-1]['outcome'], 'applied')

    def test_an_operator_retry_of_an_implementation_after_main_advanced_runs_it(self):
        # #475: the operator answers hours later, after several merges on main.
        d = self.make(STRICT, crash(), implemented(), has_pr=False)
        self.assertEqual(self.tick(d), [('implement', 7)])
        self.ws.base = 'e' * 40
        self.gh.command(7, '/devloop retry', login='brushtyler')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.decisions(7)[-1]['outcome'], 'applied')
        self.assertEqual(len(self.gh.created), 1)
        self.assertNotIn(HUMAN, self.gh.labels[7])

    def test_an_operator_retry_of_an_implementation_on_a_new_text_is_answered(self):
        d = self.make(STRICT, crash(), has_pr=False)
        self.assertEqual(self.tick(d), [('implement', 7)])
        self.ws.base = 'e' * 40
        self.gh.say(7, 'brushtyler', 'Anche Y.')
        self.gh.command(7, '/devloop retry', login='brushtyler')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.gh.labels[7], {HUMAN})
        body = self.answer(7)
        self.assertIn('(`request`)', body)
        self.assertIn('input superato', body)
        self.assertEqual(len(self.runner.calls), 1)

    def test_a_retry_of_a_superseded_head_is_answered(self):
        d = self.make(STRICT, crash())
        self.fail_review(d)
        self.gh.head = SHA2
        self.gh.command(100, '/devloop retry', login='brushtyler')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {HUMAN})
        body = self.answer(100)
        self.assertIn('input superato', body)
        self.assertIn('rimuovere `agent:needs-operator`', body)
        self.assertEqual(len(self.runner.calls), 1)

    def test_a_command_that_does_not_apply_is_answered_with_how_to_resume(self):
        cases = {
            # An escalation that records no step to run again.
            'retry, round limit': (lambda d: d._do_round_limit(pr()), '/devloop retry',
                                   'brushtyler', 'che registri un passo da ripetere'),
            'escalate': (lambda d: self.fail_review(d), '/devloop escalate x', 'brushtyler',
                         'già all\'operatore'),
            'accept-defaults on a PR': (lambda d: self.fail_review(d),
                                        '/devloop accept-defaults', 'brushtyler',
                                        '`default_available`'),
            'decide without text': (lambda d: self.fail_review(d), '/devloop decide',
                                    'brushtyler', '`/devloop decide <testo>`'),
            # The supervisor's identity does not decide an escalation to the operator.
            'supervisor': (lambda d: d._do_round_limit(pr()), '/devloop retry',
                           SUPERVISOR_LOGIN, 'affidata all\'operatore'),
        }
        for name, (escalate, text, login, why) in cases.items():
            with self.subTest(case=name):
                d = self.make(AUTO if name == 'supervisor' else STRICT, crash())
                escalate(d)
                self.assertEqual(self.gh.labels[100], {HUMAN})
                self.gh.command(100, text, login=login)
                self.assertEqual(self.tick(d), [('supervise', 100)])
                self.assertEqual(self.gh.labels[100], {HUMAN})
                body = self.answer(100)
                self.assertIn('non applicabile', body)
                self.assertIn(why, body)
                self.assertIn('rimuovere `agent:needs-operator`', body)
                self.assertIn('resta in `agent:needs-operator`', body)
                self.assertEqual(len(self.runner.calls),
                                 0 if name in ('retry, round limit', 'supervisor') else 1)
                # Answered once: the command is spent.
                self.assertEqual(self.tick(d), [])

    def test_a_command_from_an_untrusted_author_is_only_logged(self):
        d = self.make(STRICT, crash())
        self.fail_review(d)
        self.gh.command(100, '/devloop retry', login='stranger')
        self.assertEqual(self.tick(d), [])
        self.assertEqual(self.decisions(100), [])
        self.assertTrue(any('ignored `/devloop retry` by stranger' in line for line in self.logs))

    def test_a_command_on_a_failed_readiness_check_of_the_operator(self):
        cfg = dataclasses.replace(STRICT, preflight=REV)
        d = self.make(cfg, checked(False, [gap('default_available', 'JSON')]), has_pr=False)
        self.assertEqual(self.tick(d), [('preflight', 7)])
        self.assertEqual(self.gh.labels[7], {READY, HUMAN})
        # A retry does not run a readiness check again: the answer says how.
        self.gh.command(7, '/devloop retry', login='brushtyler')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertIn('/devloop recheck', self.answer(7))
        # The defaults are accepted as the supervisor would in "auto".
        self.gh.command(7, '/devloop accept-defaults', login='brushtyler')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.decisions(7)[-1]['outcome'], 'accepted')
        self.assertEqual(self.gh.labels[7], {READY})
        self.runner.outputs.append(checked(True))
        self.assertEqual(self.tick(d), [('preflight', 7)])
        self.assertIn('Accepted default: JSON', self.runner.calls[-1][3])

    def test_a_refused_command_leaves_the_readiness_block_to_the_check(self):
        cfg = dataclasses.replace(STRICT, preflight=REV)
        resumes = {'recheck': '/devloop recheck', 'decision in a comment': 'Usa JSON.'}
        for name, text in resumes.items():
            with self.subTest(resume=name):
                d = self.make(cfg, checked(False, [gap('default_available', 'JSON')]),
                              has_pr=False)
                self.assertEqual(self.tick(d), [('preflight', 7)])
                self.assertEqual(self.gh.labels[7], {READY, HUMAN})
                # The operator's command as written in person, without the attribution header.
                self.gh.say(7, 'brushtyler', '/devloop retry')
                self.assertEqual(self.tick(d), [('supervise', 7)])
                self.assertIn('/devloop recheck', self.answer(7))
                # The answer is no escalation: the block stays the check's.
                self.assertTrue(d._preflight_view(7, self.gh.labels[7])['owned'])
                self.gh.say(7, 'brushtyler', text)
                self.runner.outputs.append(checked(True))
                self.assertEqual(self.tick(d), [('preflight', 7)])
                self.assertEqual(self.gh.labels[7], {READY})
                self.assertTrue(self.gh.records(7, 'preflight')[-1]['ready'])
                self.assertEqual([a.kind for a in plan(d.snapshot(), cfg)], ['implement'])

    def test_an_operator_retry_runs_a_readiness_check_that_failed(self):
        # To the operator at once (strict), or after the supervisor escalated it (auto).
        def supervisor_escalates(d):
            self.assertEqual(self.gh.labels[7], {READY, SUPER})
            self.gh.command(7, '/devloop escalate serve l\'operatore')
            self.assertEqual(self.tick(d), [('supervise', 7)])

        cases = {'strict': (dataclasses.replace(STRICT, preflight=REV), lambda d: None),
                 'auto': (PREFLIGHT_AUTO, supervisor_escalates)}
        for name, (cfg, escalate) in cases.items():
            with self.subTest(mode=name):
                d = self.make(cfg, crash(), checked(True), has_pr=False)
                self.assertEqual(self.tick(d), [('preflight', 7)])
                escalate(d)
                # The issue keeps agent:ready: the check runs again only with it.
                self.assertEqual(self.gh.labels[7], {READY, HUMAN})
                record = self.escalations(7)[-1]
                self.assertEqual((record['to'], record['step']), ('operator', 'preflight'))
                self.assertIn('/devloop retry', self.gh.threads[7][-1]['body'])
                self.assertEqual(self.tick(d), [])  # it waits for the operator
                self.gh.say(7, 'brushtyler', '/devloop retry')
                self.assertEqual(self.tick(d), [('supervise', 7)])
                self.assertEqual(self.decisions(7)[-1]['outcome'], 'applied')
                self.assertTrue(self.gh.records(7, 'preflight')[-1]['ready'])
                self.assertEqual(self.gh.labels[7], {READY})

    def test_defaults_the_supervisor_handed_over_are_the_operators_to_decide(self):
        d = self.make(PREFLIGHT_AUTO, checked(False, [gap('default_available', 'JSON')]),
                      has_pr=False)
        self.assertEqual(self.tick(d), [('preflight', 7)])
        self.gh.command(7, '/devloop escalate serve l\'operatore')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.gh.labels[7], {READY, HUMAN})
        # The routing key of the defaults is no failed step: no retry is offered.
        record = self.escalations(7)[-1]
        self.assertEqual(record['class'], 'supervisor_escalated')
        self.assertNotIn('step', record)
        self.assertNotIn('/devloop retry', self.gh.threads[7][-1]['body'])
        self.gh.say(7, 'brushtyler', '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        body = self.answer(7)
        self.assertIn('che registri un passo da ripetere', body)
        self.assertIn('`/devloop accept-defaults`', body)
        self.assertEqual(len(self.runner.calls), 1)
        self.gh.say(7, 'brushtyler', '/devloop accept-defaults')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.decisions(7)[-1]['outcome'], 'accepted')
        self.assertEqual(self.gh.labels[7], {READY})

    def test_a_retry_answers_the_record_that_put_the_item_there(self):
        # An escalation of the review level, a policy record, after a tool failure on the head.
        d = self.make(STRICT, crash())
        self.fail_review(d)
        d._policy_comment(pr(), 'Livello', 'x', receipt(), 'level', cls='review_level')
        self.gh.say(100, 'brushtyler', '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertIn('che registri un passo da ripetere', self.answer(100))
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(len(self.runner.calls), 1)
        # A tool failure already resumed, then agent:needs-operator added by hand.
        d = self.make(STRICT, crash(), raw_review())
        self.fail_review(d)
        self.gh.say(100, 'brushtyler', '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.tick(d), [('review', 100)])
        self.gh.labels[100] = {HUMAN}
        self.gh.say(100, 'brushtyler', '/devloop retry')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertIn('che registri un passo da ripetere', self.answer(100))
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(len(self.runner.calls), 2)

    def test_an_operator_merge_notice_counts_from_its_promotion(self):
        d = self.make(STRICT)
        promoted = {'event': 'promoted', 'receipt': receipt(), 'checks': []}
        say = lambda kind, body, payload: self.gh.say(100, 'brushtyler', markdown.publishable(
            markdown.dispatcher_header() + '\n' + body, kind, payload))
        say('policy', 'Promossa', promoted)
        # Written for the first promotion, edited in place for the second one.
        self.gh.say(100, 'brushtyler', markdown.render_operator_merge(SHA1, 0, 2, 'x', HUMAN))
        d._escalate_failure(Action('review', 100), pr(), crash())
        say('policy', 'Promossa di nuovo', promoted)
        start, anchor = d._operator_anchor(self.gh.pr_comments(100))
        self.assertEqual(start, 3)
        self.assertEqual(anchor['record']['event'], 'operator_merge')


class AcceptDefaultsTests(Case):
    def blocked(self, *gaps):
        d = self.make(PREFLIGHT_AUTO, checked(False, list(gaps)), has_pr=False)
        self.assertEqual(self.tick(d), [('preflight', 7)])
        return d

    def test_defaults_are_accepted_and_the_issue_goes_back_in_the_queue(self):
        d = self.blocked(gap('default_available', 'JSON'))
        self.assertEqual(self.gh.labels[7], {READY, SUPER})
        self.assertEqual(self.escalations(7)[-1]['class'], 'preflight_default')
        self.assertEqual(self.gh.records(7, 'preflight')[-1]['class'], 'preflight_default')
        self.assertEqual(self.tick(d), [])  # the issue waits for the supervisor
        self.gh.command(7, '/devloop accept-defaults')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.gh.labels[7], {READY})
        decision = self.decisions(7)[-1]
        self.assertEqual((decision['outcome'], decision['defaults']),
                         ('accepted', [{'description': 'Quale formato usare?',
                                        'proposed_default': 'JSON'}]))
        # The acceptance counts as a trusted comment: the check runs again and sees it.
        self.runner.outputs.append(checked(True))
        self.assertEqual(self.tick(d), [('preflight', 7)])
        prompt = self.runner.calls[-1][3]
        self.assertIn('Defaults accepted by the supervisor', prompt)
        self.assertIn('Accepted default: JSON', prompt)
        self.assertEqual(self.gh.labels[7], {READY})
        self.assertEqual([a.kind for a in plan(d.snapshot(), PREFLIGHT_AUTO)], ['implement'])

    def test_an_operator_decision_never_reaches_the_supervisor(self):
        self.blocked(gap('default_available', 'JSON'), gap('operator_decision'))
        self.assertEqual(self.gh.labels[7], {READY, HUMAN})
        self.assertEqual(self.gh.records(7, 'preflight')[-1]['class'], 'operator_decision')
        self.assertEqual(self.escalations(7), [])

    def test_accept_defaults_is_refused_on_an_operator_decision(self):
        d = self.blocked(gap('default_available', 'JSON'))
        # A later check of the same text found a gap only the operator can decide.
        record = dict(self.gh.records(7, 'preflight')[-1],
                      gaps=[gap('default_available', 'JSON'), gap('operator_decision')])
        self.gh.say(7, 'brushtyler', markdown.render_preflight(record, header()))
        self.gh.command(7, '/devloop accept-defaults')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.gh.labels[7], {READY, HUMAN})
        self.assertEqual(self.decisions(7)[-1]['outcome'], 'refused')
        self.assertEqual(self.escalations(7)[-1]['class'], 'operator_decision')
        self.assertNotIn('Defaults accepted', ''.join(c[3] for c in self.runner.calls))

    def test_accept_defaults_is_refused_on_a_tool_failure(self):
        d = self.make(AUTO, crash())
        self.tick(d)
        self.gh.command(100, '/devloop accept-defaults')
        self.tick(d)
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')

    def test_the_prompt_reserves_the_adr_0001_decisions_to_the_operator(self):
        text = (PROMPT_DIR / 'preflight.md').read_text(encoding='utf-8')
        self.assertIn('**always**\n`operator_decision`, never `default_available`', text)
        self.assertIn('operator_only_paths', text)

    def test_the_prompt_counts_decisions_recorded_in_an_adr_on_the_base_branch(self):
        # #367: a decision recorded in an ADR on the base branch is the operator's; a point the
        # ADR leaves open is still a gap. Since ADR 0006, R4, the ADRs' directory and words are
        # ssdlc's data in the prompts, from its project file.
        text = _template('preflight', **prompt_values(CFG))
        self.assertIn('"Decise dall\'operatore"', text)
        self.assertIn('`docs/adr/` reaches `$base_branch` only through the operator\'s merge', text)
        self.assertIn('stays\n`operator_decision`', text)
        cycle = (PROMPT_DIR.parent.parent / 'docs' / 'adr' / 'README.md').read_text(encoding='utf-8')
        self.assertIn('Decise dall\'operatore il <data>', cycle)
        self.assertIn('**citano la sezione dell\'ADR su `main`**', cycle)


DECISION = ('/devloop decide Formato: YAML invece di JSON.\n\n'
            '- Il resto dei default va bene.')
DECISION_TEXT = 'Formato: YAML invece di JSON.\n\n- Il resto dei default va bene.'


class DecideTests(Case):
    """``/devloop decide`` (#258): the supervisor decides the gaps of a ``preflight_default``
    escalation otherwise than the defaults."""

    def blocked(self, *gaps):
        d = self.make(PREFLIGHT_AUTO, checked(False, list(gaps)), has_pr=False)
        self.assertEqual(self.tick(d), [('preflight', 7)])
        return d

    def test_a_decision_goes_back_to_the_readiness_check_with_its_text(self):
        d = self.blocked(gap('default_available', 'JSON'))
        self.assertEqual(self.gh.labels[7], {READY, SUPER})
        self.assertIn('/devloop decide', self.gh.threads[7][-1]['body'])
        self.gh.command(7, DECISION)
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.gh.labels[7], {READY})
        decision = self.decisions(7)[-1]
        self.assertEqual({k: decision[k] for k in ('command', 'by', 'outcome', 'reason', 'gaps')},
                         {'command': 'decide', 'by': SUPERVISOR_LOGIN, 'outcome': 'decided',
                          'reason': DECISION_TEXT,
                          'gaps': [{'description': 'Quale formato usare?',
                                    'proposed_default': 'JSON'}]})
        self.assertEqual(decision['comment'], self.gh.threads[7][-2]['id'])
        self.assertEqual(decision['class'], 'preflight_default')
        self.assertIn('YAML invece di JSON', self.gh.threads[7][-1]['body'])
        # The record changes the request: the check runs again and gets the decision.
        self.runner.outputs.append(checked(True))
        self.assertEqual(self.tick(d), [('preflight', 7)])
        prompt = self.runner.calls[-1][3]
        self.assertIn('Decision of the supervisor', prompt)
        self.assertIn(DECISION_TEXT, prompt)
        self.assertIn('Proposed default: JSON', prompt)
        self.assertEqual(self.gh.labels[7], {READY})
        # The command is spent: the issue goes on to the implementer.
        self.assertEqual([a.kind for a in plan(d.snapshot(), PREFLIGHT_AUTO)], ['implement'])
        self.assertEqual(len(self.decisions(7)), 1)

    def test_a_decision_without_text_is_answered_and_not_applied(self):
        d = self.blocked(gap('default_available', 'JSON'))
        self.gh.command(7, '/devloop decide')
        # Not applied, but answered (#321): the issue stays with the supervisor.
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.gh.labels[7], {READY, SUPER})
        (decision,) = self.decisions(7)
        self.assertEqual((decision['outcome'], decision['reason']), ('refused', ''))
        self.assertIn('`/devloop decide <testo>`', self.gh.threads[7][-1]['body'])
        self.assertTrue(any('malformed, expected `/devloop decide <text>`' in line
                            for line in self.logs))
        self.assertEqual(self.tick(d), [])  # answered once

    def test_a_decision_from_someone_not_allowed_is_ignored(self):
        d = self.blocked(gap('default_available', 'JSON'))
        self.gh.command(7, DECISION, login='stranger')
        self.assertEqual(self.tick(d), [])
        self.assertEqual(self.gh.labels[7], {READY, SUPER})
        self.assertEqual(self.decisions(7), [])
        self.assertTrue(any('ignored `/devloop decide` by stranger' in line
                            for line in self.logs))
        # Outside "auto" not even the supervisor decides: the command is answered (#321).
        d.cfg = dataclasses.replace(PREFLIGHT_AUTO, merge=MergePolicy(mode='strict'))
        self.gh.command(7, DECISION)
        self.assertEqual(self.tick(d), [('supervise', 7)])
        (decision,) = self.decisions(7)
        self.assertEqual(decision['outcome'], 'refused')
        body = self.gh.threads[7][-1]['body']
        self.assertIn('la modalità di merge è `strict`, non `auto`', body)
        self.assertIn('rimuovere `agent:needs-supervisor`', body)
        self.assertEqual(self.gh.labels[7], {READY, SUPER})
        # A trusted user may decide as the supervisor does.
        d.cfg = PREFLIGHT_AUTO
        self.gh.command(7, DECISION, login='brushtyler')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.decisions(7)[-1]['outcome'], 'decided')
        self.assertEqual(self.gh.labels[7], {READY})

    def test_a_decision_is_refused_on_a_critical_escalation(self):
        d = self.make(PREFLIGHT_AUTO, has_pr=False)
        self.gh.labels[7] = {READY, SUPER}
        self.gh.say(7, 'brushtyler', markdown.publishable(
            markdown.dispatcher_header() + '\nx', 'notice',
            {'event': 'escalation', 'class': 'operator_decision', 'to': 'supervisor',
             'step': 'preflight', 'key': {'step': 'preflight'}}))
        self.gh.command(7, DECISION)
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.gh.labels[7], {HUMAN})
        self.assertEqual(self.decisions(7)[-1]['outcome'], 'refused')
        self.assertEqual(self.escalations(7)[-1]['class'], 'supervisor_refused')
        self.assertNotIn(DECISION_TEXT, ''.join(c[3] for c in self.runner.calls))
        self.assertEqual(self.runner.calls, [])

    def test_a_decision_is_refused_when_the_operator_must_decide(self):
        cases = {
            'operator_decision gap': lambda gh: gh.say(7, 'brushtyler', markdown.render_preflight(
                dict(gh.records(7, 'preflight')[-1],
                     gaps=[gap('default_available', 'JSON'), gap('operator_decision')]),
                header())),
            'phase:design': lambda gh: gh.labels[7].add('phase:design'),
            'untrusted author': lambda gh: setattr(gh, 'author', 'stranger'),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                d = self.blocked(gap('default_available', 'JSON'))
                change(self.gh)
                self.gh.command(7, DECISION)
                self.assertEqual(self.tick(d), [('supervise', 7)])
                self.assertIn(HUMAN, self.gh.labels[7])
                self.assertNotIn(SUPER, self.gh.labels[7])
                self.assertEqual(self.decisions(7)[-1]['outcome'], 'refused')
                self.assertEqual(self.escalations(7)[-1]['class'], 'operator_decision')
                self.assertEqual(len(self.runner.calls), 1)

    def test_a_decision_is_refused_on_a_tool_failure(self):
        d = self.make(AUTO, crash())
        self.tick(d)
        self.gh.command(100, DECISION)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertEqual(len(self.runner.calls), 1)

    def test_a_decision_on_a_superseded_check_is_refused(self):
        d = self.blocked(gap('default_available', 'JSON'))
        self.gh.say(7, 'brushtyler', 'Anche Y.')  # a trusted comment changes the request
        self.gh.command(7, DECISION)
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.gh.labels[7], {READY, SUPER})
        self.assertEqual(self.decisions(7)[-1]['outcome'], 'refused')
        self.assertIn('input superato', self.gh.threads[7][-1]['body'])

    def test_the_check_gets_the_decision_only_from_the_dispatchers_record(self):
        d = self.blocked(gap('default_available', 'JSON'))
        # An ordinary comment of the supervisor, and a record it forged, are not trusted.
        self.gh.say(7, SUPERVISOR_LOGIN, header() + '\nUsa XML, non JSON.')
        self.gh.say(7, SUPERVISOR_LOGIN, markdown.render_supervision(
            {'command': 'decide', 'by': SUPERVISOR_LOGIN, 'reason': 'Usa TOML.',
             'outcome': 'decided', 'comment': 1, 'gaps': []}, 'Usa TOML.'))
        self.runner.outputs.append(checked(False, [gap('default_available', 'JSON')]))
        d._do_preflight(7)
        prompt = self.runner.calls[-1][3]
        for text in ('XML', 'TOML', 'Decision of the supervisor'):
            self.assertNotIn(text, prompt)
        # The same text, recorded by the dispatcher after `/devloop decide`, is.
        self.gh.labels[7] = {READY, SUPER}
        self.gh.command(7, '/devloop decide Usa XML, non JSON.')
        self.assertEqual(self.tick(d), [('supervise', 7)])
        self.assertEqual(self.decisions(7)[-1]['outcome'], 'decided')
        self.runner.outputs.append(checked(True))
        self.assertEqual(self.tick(d), [('preflight', 7)])
        prompt = self.runner.calls[-1][3]
        self.assertIn('Decision of the supervisor (agy-agent[bot])', prompt)
        self.assertIn('Usa XML, non JSON.', prompt)
        self.assertNotIn('TOML', prompt)


class DocsTests(unittest.TestCase):
    """docs/devloop/guide.md documents the classes, the routing and the commands (#182)."""

    def section(self):
        text = (PROMPT_DIR.parents[1] / 'docs' / 'devloop' / 'guide.md').read_text(encoding='utf-8')
        start = text.index('### Escalations to the supervisor')
        end = text.index('\n## ', start)
        return text[start:end]

    def test_the_commands_on_escalations_to_the_operator_are_documented(self):
        text = self.section()
        start = text.index('### Commands on escalations to the operator')
        operator = ' '.join(text[start:].split())
        self.assertIn('- `/devloop retry`, when', text[start:])
        self.assertIn('- `/devloop accept-defaults` and `/devloop decide <decisions>`, when',
                      operator)
        self.assertIn('`/devloop escalate` never applies', operator)
        self.assertIn('in `trusted_users`', operator)
        self.assertIn('**two** retries run for the same key, the supervisor\'s and the '
                      'operator\'s together', operator)
        self.assertIn('Every command that does not apply gets a dispatcher comment with the '
                      'reason and how to resume', operator)
        # The supervisor's commands that do not apply are answered too.
        self.assertIn('always gets an answer', ' '.join(text.split()))

    def test_the_classes_are_documented_with_whether_they_are_critical(self):
        rows = {}
        for line in self.section().splitlines():
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            if line.startswith('|') and len(cells) == 3 and cells[2] in ('Yes', 'No'):
                for name in cells[0].split(','):
                    rows[name.strip().strip('`')] = cells[2]
        expected = {cls: 'No' for cls in supervision.NON_CRITICAL}
        expected.update({cls: 'Yes' for cls in supervision.CRITICAL})
        self.assertEqual(rows, expected)
        self.assertIn('Any other class, or none', self.section())

    def test_the_routing_is_documented(self):
        text = ' '.join(self.section().split())
        self.assertIn('With `[merge].mode = "auto"` a non-critical escalation adds '
                      '`agent:needs-supervisor` instead of `agent:needs-operator`', text)
        self.assertIn('With `"off"` and `"strict"` every escalation goes to '
                      '`agent:needs-operator`', text)
        self.assertIn(f'`{supervision.DESIGN_LABEL}`', text)

    def test_the_commands_are_documented(self):
        text = ' '.join(self.section().split())
        for command in ('/devloop retry', '/devloop accept-defaults',
                        '/devloop decide <decisions>', '/devloop escalate <reason>'):
            self.assertIn(f'- `{command}`', self.section())
            self.assertIn(f'\n{command}\n', self.section())
        self.assertIn('**only through that record**', text)
        self.assertIn('from the identity of `[roles].supervisor`', text)
        self.assertIn('**counts no round**', text)
        self.assertIn('**two** retries', text)
        self.assertIn('counted per reviewer', text)
        self.assertIn('checked again', text)


class CommandTests(unittest.TestCase):
    def test_parsing(self):
        cmd = supervision.supervisor_command
        self.assertEqual(cmd(header() + '\n/devloop retry'), ('retry', ''))
        self.assertEqual(cmd('/devloop accept-defaults'), ('accept-defaults', ''))
        self.assertEqual(cmd('/devloop escalate serve il parere\naltro'),
                         ('escalate', 'serve il parere'))
        self.assertEqual(cmd('/devloop escalate'), ('escalate', None))
        # A decision is the rest of the comment, its first line included (#258).
        self.assertEqual(cmd(header() + '\n' + DECISION), ('decide', DECISION_TEXT))
        self.assertEqual(cmd('/devloop decide\n\nUsa YAML.\n'), ('decide', 'Usa YAML.'))
        self.assertEqual(cmd('/devloop decide  \n\n'), ('decide', None))
        for body in ('/devloop hold x', 'please /devloop retry', '', None):
            self.assertIsNone(cmd(body))

    def test_a_decision_record_carries_its_text(self):
        def read(**fields):
            record = dict({'command': 'decide', 'by': 'x', 'reason': 'Usa YAML.',
                           'outcome': 'decided', 'gaps': []}, **fields)
            return markdown.read_marker(markdown.render_supervision(record, 't'), 'supervision')

        self.assertIsNotNone(read())
        self.assertIsNotNone(read(outcome='refused', reason=''))
        self.assertIsNone(read(reason=' '))
        self.assertIsNone(read(command='retry'))
        self.assertIsNone(read(gaps=[{'description': 'd'}]))


if __name__ == '__main__':
    unittest.main()
