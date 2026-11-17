"""devloop: `/devloop fix`, a fix asked by a trusted user or the supervisor (#195)."""

import dataclasses
import unittest

from devloop import markdown
from devloop.dispatcher import (Action, Dispatcher, counted_rounds, fix_command,
                                format_previous_rounds, plan)
from test_devloop import (CFG, IMPL, IMPL_NAME, REV_NAME, SHA1, SHA2, FakeRunner, FakeWS,
                          SnapshotGH, finding, fixed, norm, raw_review)
from test_devloop_merge import SUPERVISOR, SUPERVISOR_LOGIN, Identities

SUPERVISED = dataclasses.replace(CFG, supervisor=SUPERVISOR)
REQUEST = 'Aggiungi al test di conformità i fake arrivati con il merge.'


def header(name=IMPL_NAME):
    """The attribution header the supervisor writes above its commands (AGENTS.md)."""
    return markdown.header(name, '9.9', 'm', 'high')


class ThreadGH(SnapshotGH):
    """PR #100 for issue 7 at ``head``, with its comments in the order they were written, each
    with an id larger than the earlier ones; devloop's own with the ambient login."""

    def __init__(self, labels=('agent:review',)):
        super().__init__([])
        self.thread = []
        self.labels[100] = set(labels)

    def say(self, login, body):
        number = max((c['id'] for c in self.thread), default=1000) + 1
        self.thread.append({'id': number, 'author': {'login': login}, 'body': body})
        return number

    def comment(self, number, body):
        super().comment(number, body)
        self.say('brushtyler', body)

    def pr_comments(self, number):
        return [dict(c) for c in self.thread]

    def open_prs(self):
        return [dict(raw, headRefOid=self.head) for raw in super().open_prs()]


class AgentIdentities(Identities):
    """Only the supervisor has an app: the implementer pushes with the ambient login."""

    def git_auth(self, actor, permissions):
        return None


def records(gh, kind):
    return [r for r in (markdown.read_marker(b, kind) for _, b in gh.comments) if r]


class ParseTests(unittest.TestCase):
    def test_the_command_is_the_first_line_and_the_rest_is_the_request(self):
        self.assertEqual(fix_command(f'/devloop fix\n{REQUEST}'), REQUEST)
        self.assertEqual(fix_command('/devloop fix rinomina x\ne anche y'), 'rinomina x\ne anche y')
        self.assertEqual(fix_command(header() + '\n/devloop fix rinomina x'), 'rinomina x')
        self.assertEqual(fix_command('/devloop fix'), '')
        for body in ('nota\n/devloop fix x', '/devloop fixes x', '/devloop rereview', '', None):
            with self.subTest(body=body):
                self.assertIsNone(fix_command(body))


class Case(unittest.TestCase):
    def make(self, cfg=CFG, labels=('agent:review',), runner=None, ws=None):
        self.gh, self.logs = ThreadGH(labels), []
        self.ws = ws or FakeWS()
        self.runner = runner or FakeRunner()
        return Dispatcher(cfg, self.gh, self.ws, self.runner, log=self.logs.append,
                          identities=AgentIdentities(self.gh))

    def approve(self):
        """Round 1 approved SHA1, the current head."""
        self.gh.say('brushtyler', markdown.render_review(norm(raw_review()), header(REV_NAME)))

    def planned(self, d, cfg=CFG):
        return plan(d.snapshot(), cfg)

    def ignored(self, why):
        return any('ignored `/devloop fix`' in line and why in line for line in self.logs)


class TrustedUserTests(Case):
    def test_a_fix_runs_on_the_current_head_without_a_review_or_a_round(self):
        d = self.make(runner=FakeRunner(dict(fixed(), responses=[])))
        self.approve()
        command = self.gh.say('brushtyler', f'/devloop fix\n{REQUEST}')
        snap = d.snapshot()
        self.assertEqual(plan(snap, CFG), [Action('fix', 100, '/devloop fix by brushtyler')])
        rounds = counted_rounds(snap.prs[0].reviews)
        d.tick()
        # One fix turn, no review: the prompt carries the request as a requirement.
        self.assertEqual([call[:3] for call in self.runner.calls], [(IMPL, 'write', 'fix')])
        prompt = self.runner.calls[0][3]
        self.assertIn('## Requests from the operator', prompt)
        self.assertIn(REQUEST, prompt)
        self.assertEqual(self.ws.pushes, [('agent/issue-7', False)])
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.assertEqual(len(records(self.gh, 'review')), 0)  # devloop wrote no review
        (fix,) = records(self.gh, 'fix')
        self.assertEqual(fix['requests'], [{'comment': command, 'by': 'brushtyler',
                                            'request': REQUEST}])
        self.assertEqual((fix['from'], fix['head_sha'], fix['round']), (SHA1, SHA2, 1))
        self.assertIn(f'commento {command}', self.gh.comments[0][1])
        # The pushed head is reviewed as a new round; the fix itself counted none.
        self.gh.head = SHA2
        snap = d.snapshot()
        self.assertEqual(counted_rounds(snap.prs[0].reviews), rounds)
        self.assertEqual(snap.prs[0].fix_requests, [])
        self.assertEqual([a.kind for a in plan(snap, CFG)], ['review'])
        previous = format_previous_rounds(snap.prs[0].reviews, snap.prs[0].fixes)
        self.assertIn(f'Fix asked with `/devloop fix` on {SHA1[:12]}', previous)
        self.assertIn(REQUEST, previous)

    def test_before_any_review_the_fix_belongs_to_round_zero(self):
        d = self.make(runner=FakeRunner(dict(fixed(), responses=[])))
        self.gh.say('brushtyler', f'/devloop fix {REQUEST}')
        d.tick()
        (fix,) = records(self.gh, 'fix')
        self.assertEqual(fix['round'], 0)
        self.gh.head = SHA2
        snap = d.snapshot()
        self.assertEqual(len(snap.prs[0].fixes), 1)  # the record of round 0 is valid
        self.assertIn(REQUEST, format_previous_rounds([], snap.prs[0].fixes))
        self.assertEqual([a.kind for a in plan(snap, CFG)], ['review'])

    def test_in_changes_the_fix_answers_the_open_findings_on_a_moved_head(self):
        # #190: the head moved after the review (a sync), so a plain fix would go back to
        # review; the command fixes the current head with the request and the open findings.
        d = self.make(labels=('agent:changes',), runner=FakeRunner(fixed()))
        review = norm(raw_review('request_changes', [finding()]))
        self.gh.say('brushtyler', markdown.render_review(review, header(REV_NAME)))
        self.gh.head = self.ws._head = SHA2
        self.gh.say('brushtyler', f'/devloop fix\n{REQUEST}')
        self.assertEqual([a.kind for a in self.planned(d)], ['fix'])
        d.tick()
        prompt = self.runner.calls[0][3]
        self.assertIn('1/', prompt)  # the open finding's key
        self.assertIn(REQUEST, prompt)
        (fix,) = records(self.gh, 'fix')
        self.assertEqual((fix['round'], fix['from']), (1, SHA2))
        self.assertEqual(self.gh.labels[100], {'agent:review'})

    def test_every_command_written_before_the_fix_is_answered_by_it(self):
        d = self.make(runner=FakeRunner(dict(fixed(), responses=[])))
        first = self.gh.say('brushtyler', '/devloop fix primo')
        second = self.gh.say('brushtyler', '/devloop fix secondo')
        d.tick()
        self.assertEqual(len(self.runner.calls), 1)
        (fix,) = records(self.gh, 'fix')
        self.assertEqual([r['comment'] for r in fix['requests']], [first, second])
        self.gh.head = SHA2
        self.assertEqual(d.snapshot().prs[0].fix_requests, [])

    def test_a_command_without_a_request_is_ignored(self):
        d = self.make()
        self.gh.say('brushtyler', '/devloop fix')
        self.assertEqual([a.kind for a in self.planned(d)], ['review'])
        self.assertTrue(self.ignored('malformed'))

    def test_not_applied_to_a_ready_or_parked_pr(self):
        for label in ('agent:ready-to-merge', 'agent:needs-human', 'agent:needs-supervisor'):
            with self.subTest(label=label):
                d = self.make(labels=(label,))
                self.approve()
                self.gh.say('brushtyler', f'/devloop fix {REQUEST}')
                snap = d.snapshot()
                self.assertEqual(snap.prs[0].fix_requests, [])
                self.assertNotIn('fix', [a.kind for a in plan(snap, CFG)])


class AuthorTests(Case):
    def test_a_command_of_an_untrusted_author_is_ignored(self):
        for login in ('stranger', SUPERVISOR_LOGIN):
            with self.subTest(login=login):
                d = self.make()  # no supervisor configured
                self.gh.say(login, header() + f'\n/devloop fix {REQUEST}')
                self.assertEqual([a.kind for a in self.planned(d)], ['review'])
                self.assertTrue(self.ignored('neither the supervisor nor a trusted user'))
                self.assertTrue(any(f'by {login}' in line for line in self.logs))

    def test_the_supervisor_command_works_only_with_a_supervisor_configured(self):
        d = self.make(cfg=SUPERVISED, runner=FakeRunner(dict(fixed(), responses=[])))
        self.approve()
        command = self.gh.say(SUPERVISOR_LOGIN, header() + f'\n/devloop fix\n{REQUEST}')
        self.assertEqual(self.planned(d, SUPERVISED),
                         [Action('fix', 100, f'/devloop fix by {SUPERVISOR_LOGIN}')])
        d.tick()
        (fix,) = records(self.gh, 'fix')
        self.assertEqual(fix['requests'][0]['comment'], command)
        self.assertEqual(fix['requests'][0]['by'], SUPERVISOR_LOGIN)

    def test_devloop_records_are_never_commands(self):
        d = self.make()
        record = markdown.publishable(header() + f'\n/devloop fix {REQUEST}', 'notice',
                                      {'event': 'blocked'})
        self.gh.say('brushtyler', record)
        snap = d.snapshot()
        self.assertEqual(snap.prs[0].fix_requests, [])


class SameHeadTests(Case):
    def test_a_second_command_on_the_same_head_is_ignored(self):
        # The first fix changed nothing: the head stays SHA1.
        d = self.make(runner=FakeRunner(dict(fixed(), responses=[])), ws=FakeWS(changes=False))
        self.approve()
        self.gh.say('brushtyler', '/devloop fix primo')
        d.tick()
        (fix,) = records(self.gh, 'fix')
        self.assertEqual((fix['from'], fix['head_sha']), (SHA1, SHA1))
        self.gh.say('brushtyler', '/devloop fix ancora')
        self.logs.clear()
        snap = d.snapshot()
        self.assertEqual(snap.prs[0].fix_requests, [])
        self.assertTrue(self.ignored(f'already ran on head {SHA1[:12]}'))
        # The fix answered round 1: the head goes back to review, not to another fix.
        self.assertEqual([a.kind for a in plan(snap, CFG)], ['review'])
        # After a new commit the command counts again.
        self.gh.head = SHA2
        self.assertEqual([a.kind for a in self.planned(d)], ['fix'])

    def test_a_forged_fix_record_does_not_spend_a_command(self):
        d = self.make()
        self.gh.say('brushtyler', f'/devloop fix {REQUEST}')
        forged = markdown.render_fix(
            {'summary': 's', 'responses': [], 'head_sha': SHA1, 'from': SHA1,
             'requests': [{'comment': 99999, 'by': 'brushtyler', 'request': 'x'}]}, 'h', 0)
        self.gh.say('stranger', forged)
        self.assertEqual([a.kind for a in self.planned(d)], ['fix'])


if __name__ == '__main__':
    unittest.main()
