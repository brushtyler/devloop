"""devloop: escalation classes and the supervisor's decisions in merge mode auto (#182)."""

import dataclasses
import unittest

from devloop import agents, markdown, supervision
from devloop.config import MergePolicy
from devloop.dispatcher import PROMPT_DIR, Action, Dispatcher, plan
from test_devloop import (CFG, REV, SHA1, SHA2, FakeRunner, FakeWS, SnapshotGH, implemented,
                          norm, pr, raw_review, receipt)
from test_devloop_preflight import checked, gap

SUPERVISOR = 'agy'  # neither the implementer nor the level-1 reviewer of the tests
SUPERVISOR_LOGIN = 'agy-agent[bot]'
AUTO = dataclasses.replace(CFG, merge=MergePolicy(mode='auto'), supervisor=SUPERVISOR)
STRICT = dataclasses.replace(CFG, merge=MergePolicy(mode='strict'))
PREFLIGHT_AUTO = dataclasses.replace(AUTO, preflight=REV)
HUMAN, SUPER = 'agent:needs-human', 'agent:needs-supervisor'
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
        d = self.make(AUTO, raw_review('needs_human', human='product call'))
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
                self.assertNotIn('key', record)
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

    def test_commands_are_ignored_outside_auto(self):
        d = self.make(AUTO, crash())
        self.fail_review(d)
        d.cfg = STRICT
        self.gh.command(100, '/devloop retry')
        self.assertEqual(self.tick(d), [])
        self.assertEqual(self.gh.labels[100], {SUPER})

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

    def test_an_issue_retry_on_a_new_base_is_refused(self):
        d = self.make(AUTO, crash(), has_pr=False)
        self.tick(d)
        self.ws.base = 'e' * 40
        self.gh.command(7, '/devloop retry')
        self.tick(d)
        self.assertEqual(self.gh.labels[7], {SUPER})
        self.assertEqual(self.decisions(7)[-1]['outcome'], 'refused')
        self.assertEqual(len(self.runner.calls), 1)

    def test_an_issue_retry_is_bound_to_the_input_the_step_ran_on(self):
        # The base or the request changes while the step runs: the escalation records the
        # input the step got, so the retry refers to a superseded input and is refused.
        new_base = 'e' * 40
        changes = {
            'implement, base': (AUTO, lambda: setattr(self.ws, 'base', new_base), 'base'),
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
        self.gh.command(100, '/devloop escalate')  # without a reason: ignored
        self.assertEqual(self.tick(d), [])
        self.gh.command(100, '/devloop escalate il modello è instabile')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {HUMAN})
        decision = self.decisions(100)[-1]
        self.assertEqual((decision['outcome'], decision['by'], decision['reason']),
                         ('escalated', SUPERVISOR_LOGIN, 'il modello è instabile'))
        record = self.escalations(100)[-1]
        self.assertEqual((record['class'], record['to']), ('supervisor_escalated', 'operator'))
        self.assertIn('il modello è instabile', self.gh.threads[100][-1]['body'])


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


class DocsTests(unittest.TestCase):
    """docs/devloop.md documents the classes, the routing and the commands (#182)."""

    def section(self):
        text = (PROMPT_DIR.parents[1] / 'docs' / 'devloop.md').read_text(encoding='utf-8')
        start = text.index('### Escalations to the supervisor')
        end = text.index('\n## ', start)
        return text[start:end]

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
                      '`agent:needs-supervisor` instead of `agent:needs-human`', text)
        self.assertIn('With `"off"` and `"strict"` every escalation goes to '
                      '`agent:needs-human`', text)
        self.assertIn(f'`{supervision.DESIGN_LABEL}`', text)

    def test_the_commands_are_documented(self):
        text = ' '.join(self.section().split())
        for command in ('/devloop retry', '/devloop accept-defaults',
                        '/devloop escalate <reason>'):
            self.assertIn(f'- `{command}`', self.section())
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
        for body in ('/devloop hold x', 'please /devloop retry', '', None):
            self.assertIsNone(cmd(body))


if __name__ == '__main__':
    unittest.main()
