"""devloop: the supervisor's ``/devloop rereview`` after a review that failed (#491)."""

import dataclasses
import unittest
from pathlib import Path

from devloop import agents, markdown, supervision
from devloop.config import MergePolicy
from devloop.planning import Action
from helpers.devloop_fakes import FakeIdentities
from test_devloop import CFG, REV, SHA1, finding, norm, pr, raw_review
from test_devloop_quorum import FIRST, SECOND, Quorum
from test_devloop_supervision import (AUTO, HUMAN, REVIEW, SUPER, SUPERVISOR_LOGIN, Case,
                                      crash, header)

REASON = 'le quote di agy erano esaurite; ora ha un modello disponibile'
OTHER = 'claude'  # a second reviewer of a recorded receipt


class NewRoundTests(Quorum):
    """Level 2: the first reviewer approves, the second fails; the supervisor asks for a new
    round, which chooses its reviewers and their models with the configuration of now."""

    def setUp(self):
        cfg = dataclasses.replace(CFG, merge=MergePolicy(mode='auto'), supervisor=FIRST)
        self.make(raw_review(), agents.AgentError(f'{SECOND} timed out'), raw_review(),
                  raw_review(), cfg=cfg)
        # Issue #7 opened by a trusted user, so that its escalations may go to the supervisor.
        history = self.gh.issue_history
        self.gh.issue_history = lambda number: dict(history(number), author='brushtyler')
        self.gh.labeled_events = lambda number: [
            {'label': 'agent:ready', 'by': 'brushtyler', 'at': '2026-11-13T10:00:00Z'}]
        self.supervisor = FakeIdentities.LOGINS[FIRST]
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {SUPER})
        escalation = [r for r in self.records('notice') if r['event'] == 'escalation'][-1]
        self.assertEqual((escalation['class'], escalation['step'], escalation['key']['reviewer']),
                         ('tool_failure', 'review', SECOND))

    def say(self, login, text):
        self.gh.comments.append((100, text))
        self.gh.written.append(login)

    def test_a_rereview_opens_a_new_round_with_the_current_configuration(self):
        # The operator gives the failed reviewer a model that is available.
        cfg = self.d.cfg
        agent = dataclasses.replace(cfg.agents[SECOND], model='modello-disponibile')
        self.d.cfg = dataclasses.replace(cfg, agents={**cfg.agents, SECOND: agent})
        self.say(self.supervisor, header() + f'\n/devloop rereview {REASON}')
        self.assertEqual(self.d.tick(), [Action('supervise', 100, f'rereview by {self.supervisor}')])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        (decision,) = self.records('supervision')
        self.assertEqual({k: decision[k] for k in ('command', 'by', 'outcome', 'round',
                                                   'reviewers', 'reason')},
                         {'command': 'rereview', 'by': self.supervisor, 'outcome': 'applied',
                          'round': 1, 'reviewers': [SECOND], 'reason': REASON})
        # A new round, not the same one: both reviewers review again, under a new receipt.
        self.assertEqual(self.planned(), [Action('review', 100, 'new review requested by a '
                                                 'trusted user or the supervisor')])
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.reviewed(), [FIRST, SECOND, FIRST, SECOND])
        reviews = self.records('review')
        self.assertEqual([(r['reviewer'], r['round']) for r in reviews],
                         [(FIRST, 1), (FIRST, 2), (SECOND, 2)])
        second = reviews[-1]
        self.assertEqual(second['profile'], self.d._profile(SECOND, 'review'))
        self.assertEqual(second['profile']['model'], 'modello-disponibile')
        self.assertEqual(reviews[1]['receipt']['reviewers'], [FIRST, SECOND])
        # The round counts against max_review_rounds, as every round does.
        self.assertIn('round 2 of at most', self.runner.calls[-1][3])
        self.assertIn('agent:ready-to-merge', self.gh.labels[100])

    def test_a_second_rereview_after_a_round_without_records_is_refused(self):
        self.say(self.supervisor, header() + f'\n/devloop rereview {REASON}')
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {REVIEW})
        # The new round fails before any reviewer writes a verdict: it writes no record, so it
        # does not count against max_review_rounds, and round 1 still misses SECOND's verdict.
        self.runner.outputs = [agents.AgentError(f'{FIRST} timed out')]
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual([(r['reviewer'], r['round']) for r in self.records('review')],
                         [(FIRST, 1)])
        escalation = [r for r in self.records('notice') if r['event'] == 'escalation'][-1]
        self.assertEqual((escalation['class'], escalation['key']['reviewer']),
                         ('tool_failure', FIRST))
        calls = len(self.runner.calls)
        # A second rereview would repeat it without limit: refused, the escalation stays.
        self.say(self.supervisor, header() + f'\n/devloop rereview {REASON}')
        self.assertEqual(self.d.tick(), [Action('supervise', 100, f'rereview by {self.supervisor}')])
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual([(d['command'], d['outcome']) for d in self.records('supervision')],
                         [('rereview', 'applied'), ('rereview', 'refused')])
        body = ' '.join(self.gh.comments[-1][1].split())
        self.assertIn('è già stata chiesta una nuova review', body)
        self.assertIn('l\'escalation resta al supervisore', body)
        self.assertEqual(len(self.runner.calls), calls)


class RefusalTests(Case):
    """Level 1, as in test_devloop_supervision: the round of ``REV`` and a tool failure of a
    review on the same head, waiting for the supervisor."""

    def escalate(self, d, verdict=None):
        if verdict is not None:
            record = norm(raw_review(
                verdict, [finding()] if verdict == 'request_changes' else (),
                human='serve una decisione' if verdict == 'needs_operator' else ''))
            self.gh.say(100, 'brushtyler', markdown.publishable('review', 'review', record))
        exc = crash()
        exc.reviewer = REV
        d._escalate_failure(Action('review', 100), pr(), exc)
        self.gh.labels[100] = {SUPER}
        self.assertEqual(self.escalations(100)[-1]['key'],
                         {'step': 'review', 'head': SHA1, 'reviewer': REV})

    def test_a_rereview_with_every_verdict_is_refused_with_the_reason(self):
        for verdict, why in (('approve', 'ha già un verdetto'),
                             ('request_changes', 'ha già un verdetto'),
                             ('needs_operator', 'ha chiesto una decisione dell\'operatore')):
            with self.subTest(verdict=verdict):
                d = self.make(AUTO)
                self.escalate(d, verdict)
                self.gh.command(100, f'/devloop rereview {REASON}')
                self.assertEqual(self.tick(d), [('supervise', 100)])
                # Refused and answered: the escalation stays with the supervisor.
                self.assertEqual(self.gh.labels[100], {SUPER})
                decision = self.decisions(100)[-1]
                self.assertEqual((decision['command'], decision['outcome']),
                                 ('rereview', 'refused'))
                body = ' '.join(self.gh.threads[100][-1]['body'].split())
                self.assertIn(why, body)
                self.assertIn(f'`{REV}`', body)
                self.assertIn('l\'escalation resta al supervisore', body)
                self.assertEqual(self.runner.calls, [])
                # Spent: not answered again, and nothing runs.
                self.assertEqual(self.tick(d), [])

    def test_a_rereview_without_a_round_of_this_head_is_refused(self):
        d = self.make(AUTO)
        self.escalate(d)
        self.gh.command(100, f'/devloop rereview {REASON}')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('`/devloop retry` apre già un round',
                      ' '.join(self.gh.threads[100][-1]['body'].split()))

    def test_a_rereview_without_a_reason_is_answered_and_not_applied(self):
        d = self.make(AUTO)
        self.escalate(d, 'approve')
        self.gh.command(100, '/devloop rereview')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('`/devloop rereview <testo>`', self.gh.threads[100][-1]['body'])

    def test_a_rereview_on_the_failure_of_another_step_is_refused(self):
        d = self.make(AUTO)
        d._escalate_failure(Action('fix', 100), pr(labels=('agent:changes',)), crash())
        self.assertEqual(self.escalations(100)[-1]['class'], 'tool_failure')
        self.gh.labels[100] = {SUPER}
        self.gh.command(100, f'/devloop rereview {REASON}')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('della review di una PR', self.gh.threads[100][-1]['body'])


class UntrustedTests(Case):
    """Only the supervisor's identity uses ``/devloop rereview`` as a command of an
    escalation: from an agent or an untrusted user it is ignored, as before; a trusted user's
    is the request of a new review it always was."""

    def setUp(self):
        self.d = self.make(AUTO, crash())
        self.assertEqual(self.tick(self.d), [('review', 100)])
        self.assertEqual(self.gh.labels[100], {SUPER})

    def test_a_rereview_of_an_agent_or_an_untrusted_user_is_ignored(self):
        for login in ('stranger', 'codex-agent[bot]'):
            with self.subTest(login=login):
                self.gh.command(100, f'/devloop rereview {REASON}', login=login)
                self.assertEqual(self.tick(self.d), [])
                self.assertEqual(self.gh.labels[100], {SUPER})
                self.assertEqual(self.decisions(100), [])
                self.assertTrue(any(f'ignored `/devloop rereview` by {login}' in line
                                    for line in self.logs))
                snapshot = [p for p in self.d.snapshot().prs if p.number == 100]
                self.assertFalse(any(p.rereview for p in snapshot))
        self.assertEqual(len(self.runner.calls), 1)

    def test_a_trusted_users_rereview_is_not_a_command_of_the_escalation(self):
        self.gh.command(100, '/devloop rereview', login='brushtyler')
        self.assertEqual(self.tick(self.d), [])
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual(self.decisions(100), [])
        self.assertEqual(self.gh.threads[100][-1]['author']['login'], 'brushtyler')

    def test_on_another_escalation_to_the_operator_the_supervisor_is_refused(self):
        self.d._do_round_limit(pr())  # critical: the operator's
        self.gh.labels[100] = {HUMAN}
        self.gh.command(100, f'/devloop rereview {REASON}', login=SUPERVISOR_LOGIN)
        self.assertEqual(self.tick(self.d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('affidata all\'operatore', self.gh.threads[100][-1]['body'])
        self.assertEqual(len(self.runner.calls), 1)


class OperatorMergeTests(Case):
    """A PR whose merge is the operator's (``operator_only_paths``): its tool failure goes to
    the operator, and the supervisor's rereview still applies, without touching the merge."""

    def fail(self, d, change=None):
        self.ws.diff = [{'path': 'docs/adr/0009-x.md', 'links': []}]
        record = norm(raw_review())
        record['receipt']['reviewers'] = [REV, OTHER]  # OTHER's review failed
        self.gh.say(100, 'brushtyler', markdown.publishable('review', 'review', record))
        if change:
            change()
        exc = crash()
        exc.reviewer = OTHER
        d._escalate_failure(Action('review', 100), pr(), exc)
        self.assertEqual(self.gh.labels[100], {HUMAN})
        escalation = self.escalations(100)[-1]
        self.assertEqual((escalation['class'], escalation['to']), ('tool_failure', 'operator'))

    def test_a_rereview_applies_and_the_merge_stays_the_operators(self):
        d = self.make(AUTO, raw_review())
        self.fail(d)
        self.gh.command(100, f'/devloop rereview {REASON}')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        decision = self.decisions(100)[-1]
        self.assertEqual((decision['outcome'], decision['reviewers']), ('applied', [OTHER]))
        self.assertEqual(self.tick(d), [('review', 100)])
        self.assertEqual([r['round'] for r in self.gh.records(100, 'review')], [1, 2])
        # Approved, the PR waits for the operator's merge: devloop does not merge it.
        self.assertEqual([a for a in self.tick(d) if a[0] == 'merge'], [])
        self.assertEqual(getattr(self.gh, 'merged', []), [])
        self.assertIn(HUMAN, self.gh.labels[100])

    def test_a_rereview_is_refused_when_the_escalation_is_critical_otherwise(self):
        d = self.make(AUTO)
        self.fail(d, lambda: self.gh.labels[7].add('phase:design'))
        self.gh.command(100, f'/devloop rereview {REASON}')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('phase:design', self.gh.threads[100][-1]['body'])
        self.assertEqual(self.runner.calls, [])

    def test_a_trusted_users_command_waiting_there_wins(self):
        d = self.make(AUTO)
        self.fail(d)
        self.gh.command(100, '/devloop retry', login='brushtyler')
        self.gh.command(100, f'/devloop rereview {REASON}')
        view = d._supervisor_view(100, self.gh.pr_comments(100), operator=True)
        self.assertEqual((view['command'], view['by']), ('retry', 'brushtyler'))


class DocsTests(unittest.TestCase):
    """The guide and ADR 0005 record the command and the operator's decision on #491."""

    ROOT = Path(__file__).resolve().parents[1]

    def read(self, path):
        return ' '.join((self.ROOT / path).read_text(encoding='utf-8').split())

    def test_the_guide_documents_the_supervisors_rereview(self):
        guide = (self.ROOT / 'docs/devloop/guide.md').read_text(encoding='utf-8')
        section = guide[guide.index('### Escalations to the supervisor'):]
        self.assertIn('\n/devloop rereview <reason>\n', section)
        self.assertIn('- `/devloop rereview <reason>` opens a **new review round**', section)
        text = self.read('docs/devloop/guide.md')
        self.assertIn('**The supervisor\'s rereview** (#491)', text)
        self.assertIn('**counts against `max_review_rounds`**', text)

    def test_adr_0005_records_the_operators_decision(self):
        text = self.read('docs/adr/0005-deleghe-e-ruoli-degli-agent.md')
        self.assertIn('**Precisazione del 2026-11-21, su #491**', text)
        self.assertIn('https://github.com/brushtyler/ssdlc/issues/491#issuecomment-7054509850',
                      text)


class ParsingTests(unittest.TestCase):
    def test_parsing(self):
        cmd = supervision.supervisor_command
        self.assertEqual(cmd(header() + f'\n/devloop rereview {REASON}\naltro'),
                         ('rereview', f'{REASON}\naltro'))
        self.assertEqual(cmd('/devloop rereview'), ('rereview', None))


if __name__ == '__main__':
    unittest.main()
