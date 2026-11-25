"""devloop: the supervisor decides the escalations of process on the PRs of its own agent too,
within the caps (#527, principle 4 of ADR 0002 as clarified on #524). It never decides one that
would override or replace a reviewer's verdict: that stays with the operator.

This replaces option (a) of ADR 0005, P5-2 (#406): ``[delegations] retry_own_prs`` has no
effect any more, and is still accepted so that an overlay that sets it keeps loading."""

import dataclasses
import tempfile
import unittest
from pathlib import Path

from devloop import config, markdown
from devloop.config import ConfigError, DelegationPolicy
from devloop.planning import Action
from devloop.supervision import MAX_RETRIES, NON_CRITICAL
from helpers import ssdlc_project
from test_devloop import CFG, REV, norm, pr, raw_review
from test_devloop_supervision import AUTO, HUMAN, REVIEW, SUPER, Case, crash
from test_devloop_supervisor_app import OWN as OWN_ROLE
from test_devloop_supervisor_app import SUPERVISOR_APP

ROOT = Path(__file__).resolve().parents[1]
# The supervisor is the agent that implemented the PRs of the tests (the case of #357).
OWN = dataclasses.replace(AUTO, supervisor=CFG.implementer)
OWN_ON = dataclasses.replace(OWN, delegations=DelegationPolicy(retry_own_prs=True))
TRUSTED = 'brushtyler'  # the supervisor of OWN has no app in the tests: a trusted user commands
REASON = 'le quote del reviewer erano esaurite; ora ha un modello disponibile'
OTHER = 'claude'  # a second reviewer of a recorded receipt


class ConfigTests(unittest.TestCase):
    def test_the_key_is_still_accepted(self):
        self.assertFalse(ssdlc_project.load().delegations.retry_own_prs)
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / 'config.local.toml'
            local.write_text('[delegations]\nretry_own_prs = true\n')
            self.assertTrue(ssdlc_project.load(local).delegations.retry_own_prs)

    def test_invalid_values_still_stop_devloop(self):
        for table, error in (({'retry_own_prs': 1}, 'delegations.retry_own_prs: expected true'),
                             ({'unknown': True}, 'delegations: unknown keys')):
            with self.subTest(table=table), self.assertRaisesRegex(ConfigError, error):
                config.parse_delegations(table)


class ProcessTests(Case):
    """Criterion 2 of #527: the escalations of process on the supervisor's agent's PRs go to the
    supervisor, within the caps."""

    def fail_review(self, d, label):
        self.assertEqual(self.tick(d), [('review', 100)])
        self.assertEqual(self.gh.labels[100], {label})
        record = self.escalations(100)[-1]
        self.assertEqual(record['class'], 'tool_failure')
        return record

    def test_a_tool_failure_goes_to_the_supervisor_whatever_the_key(self):
        for cfg in (OWN, OWN_ON):
            with self.subTest(retry_own_prs=cfg.delegations.retry_own_prs):
                d = self.make(cfg, crash(), raw_review())
                record = self.fail_review(d, SUPER)
                self.assertEqual((record['to'], record['step'], record['resume']),
                                 ('supervisor', 'review', REVIEW))
                self.assertNotIn('il supervisore ha implementato',
                                 self.gh.threads[100][-1]['body'])
                self.gh.command(100, '/devloop retry', login=TRUSTED)
                self.assertEqual(self.tick(d), [('supervise', 100)])
                self.assertEqual(self.gh.labels[100], {REVIEW})
                self.assertEqual(self.decisions(100)[-1]['outcome'], 'applied')
                self.assertEqual(self.tick(d), [('review', 100)])
                (review,) = self.gh.records(100, 'review')
                self.assertEqual(review['round'], 1)  # the retry counted no round

    def test_every_class_of_process_goes_to_the_supervisor(self):
        d = self.make(OWN)
        for cls, step in (('tool_failure', 'review'), ('sync_failed', 'sync'),
                          ('preflight_default', 'preflight'), ('merged_issue', 'implement'),
                          ('round_grantable', 'review'), ('push_failed', 'fix')):
            with self.subTest(cls=cls):
                route, why = d._supervised(100, cls, step, pr())
                self.assertIsNotNone(route)
                self.assertEqual(why, '')
        # push_failed (#645): a retry of the push, decided by the supervisor (the operator's
        # /devloop approve on #645). prerequisite (#672): a prerequisite named without the
        # number of its issue, an escalation of issues only (the operator's /devloop approve on
        # #672).
        self.assertEqual({'tool_failure', 'sync_failed', 'preflight_default', 'merged_issue',
                          'round_grantable', 'budget_grantable', 'push_failed', 'prerequisite'},
                         set(NON_CRITICAL))

    def test_a_budget_extension_is_not_among_them(self):
        # The list the operator approved on #524 does not name grant-budget.
        d = self.make(OWN)
        route, why = d._supervised(100, 'budget_grantable', 'review', pr())
        self.assertIsNone(route)
        self.assertIn('il supervisore ha implementato questa PR', why)
        self.assertIn('concessione di budget', why)
        # On another agent's PR it goes to the supervisor, as before.
        self.assertIsNotNone(self.make(AUTO)._supervised(100, 'budget_grantable', 'review',
                                                         pr())[0])

    def test_the_limit_of_two_retries_is_unchanged_and_shared_with_the_operator(self):
        d = self.make(OWN, crash(), crash(), crash())
        self.fail_review(d, SUPER)
        self.gh.command(100, '/devloop retry', login=TRUSTED)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.decisions(100)[-1]['attempt'], 1)
        # The second failure goes to the supervisor, who hands it to the operator: the
        # operator's retry is the second of the same two.
        self.fail_review(d, SUPER)
        self.gh.command(100, '/devloop escalate serve l\'operatore', login=TRUSTED)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.gh.command(100, '/devloop retry', login=TRUSTED)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.decisions(100)[-1]['attempt'], MAX_RETRIES)
        self.fail_review(d, SUPER)
        self.gh.command(100, '/devloop retry', login=TRUSTED)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(len(self.runner.calls), 3)  # not run a fourth time
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'limit')
        self.assertEqual(self.escalations(100)[-1]['class'], 'retry_limit')

    def test_turning_the_key_off_no_longer_refuses_a_retry_waiting_for_the_supervisor(self):
        d = self.make(OWN_ON, crash(), raw_review())
        self.fail_review(d, SUPER)
        d.cfg = OWN
        self.gh.command(100, '/devloop retry', login=TRUSTED)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'applied')


class RereviewTests(Case):
    """The supervisor's rereview (#491) on its agent's PRs: it applies when a reviewer of the
    quorum has no verdict (criterion 2), and is refused when it would override one (criterion
    3)."""

    def escalate(self, d, record, failed):
        self.gh.say(100, TRUSTED, markdown.publishable('review', 'review', record))
        exc = crash()
        exc.reviewer = failed
        d._escalate_failure(Action('review', 100), pr(), exc)
        escalation = self.escalations(100)[-1]
        self.assertEqual((escalation['class'], escalation['to']), ('tool_failure', 'supervisor'))
        self.gh.labels[100] = {SUPER}

    def test_a_rereview_applies_when_a_verdict_is_missing(self):
        d = self.make(OWN_ROLE, raw_review())
        record = norm(raw_review())
        record['receipt']['reviewers'] = [REV, OTHER]  # OTHER's review failed
        self.escalate(d, record, OTHER)
        self.gh.command(100, f'/devloop rereview {REASON}', login=SUPERVISOR_APP)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        decision = self.decisions(100)[-1]
        self.assertEqual((decision['by'], decision['outcome'], decision['reviewers']),
                         (SUPERVISOR_APP, 'applied', [OTHER]))

    def test_a_rereview_that_would_override_a_verdict_is_refused(self):
        for verdict, why in (('approve', 'ha già un verdetto'),
                             ('needs_operator', 'ha chiesto una decisione dell\'operatore')):
            with self.subTest(verdict=verdict):
                d = self.make(OWN_ROLE)
                self.escalate(d, norm(raw_review(
                    verdict, human='serve una decisione' if verdict == 'needs_operator'
                    else '')), REV)
                self.gh.command(100, f'/devloop rereview {REASON}', login=SUPERVISOR_APP)
                self.assertEqual(self.tick(d), [('supervise', 100)])
                self.assertEqual(self.gh.labels[100], {SUPER})
                decision = self.decisions(100)[-1]
                self.assertEqual((decision['command'], decision['outcome']),
                                 ('rereview', 'refused'))
                self.assertIn(why, ' '.join(self.gh.threads[100][-1]['body'].split()))
                self.assertEqual(self.runner.calls, [])


class VerdictTests(Case):
    """Criterion 3 of #527: an escalation that would override a reviewer's verdict stays with
    the operator, on the supervisor's agent's PRs too."""

    def test_a_reviewer_asking_for_the_operator_stays_with_the_operator(self):
        d = self.make(OWN_ROLE, raw_review('needs_operator', human='product call'))
        self.assertEqual(self.tick(d), [('review', 100)])
        self.assertEqual(self.gh.labels[100], {HUMAN})
        record = self.escalations(100)[-1]
        self.assertEqual((record['class'], record['critical'], record['to']),
                         ('reviewer_decision', True, 'operator'))
        self.assertEqual(d._supervised(100, 'reviewer_decision', 'review', pr()), (None, ''))
        # The supervisor's commands do not decide it: refused, the PR stays with the operator.
        for command in ('/devloop retry', f'/devloop rereview {REASON}'):
            with self.subTest(command=command):
                self.gh.command(100, command, login=SUPERVISOR_APP)
                self.assertEqual(self.tick(d), [('supervise', 100)])
                self.assertEqual(self.gh.labels[100], {HUMAN})
                self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertEqual(len(self.runner.calls), 1)  # the review is not run again

    def test_a_round_limit_stays_with_the_operator(self):
        d = self.make(OWN)
        self.assertEqual(d._supervised(100, 'round_limit', 'review', pr()), (None, ''))


class DocsTests(unittest.TestCase):
    """Criterion 1 of #527: ADR 0002 and ADR 0005 record the clarification with the links to
    the operator's approvals on #524 and #527."""

    APPROVALS = ('https://github.com/brushtyler/ssdlc/issues/524#issuecomment-7082340609',
                 'https://github.com/brushtyler/ssdlc/issues/527#issuecomment-7082800200')

    def read(self, path):
        return ' '.join((ROOT / path).read_text(encoding='utf-8').split())

    def test_adr_0002_records_the_four_roles_and_principle_4(self):
        text = self.read('docs/adr/0002-ruoli-e-responsabilita.md')
        self.assertIn('### Precisazione del 2026-11-22: modello a quattro ruoli (#524, #527)',
                      text)
        for link in self.APPROVALS:
            self.assertIn(link, text)
        for role in ('**Operatore**', '**Coordinatore**', '**Supervisore**',
                     '**Agent dell\'host**'):
            self.assertIn(role, text)
        self.assertIn('il supervisore non decide escalation **che superino o sostituiscano un '
                      'verdetto dei reviewer**', text)
        self.assertIn('Il merge contro un verdetto negativo resta solo dell\'operatore', text)
        self.assertIn('`check-host.sh --read-only`', text)
        self.assertIn('fallisce chiuso', text)

    def test_adr_0005_records_the_clarification(self):
        text = self.read('docs/adr/0005-deleghe-e-ruoli-degli-agent.md')
        self.assertIn('**Precisazione del 2026-11-22, su #524 e #527**', text)
        for link in self.APPROVALS:
            self.assertIn(link, text)
        self.assertIn('**rivede l\'opzione (a)**', text)
        self.assertIn('0002-ruoli-e-responsabilita.md#precisazione-del-2026-11-22-modello-a-'
                      'quattro-ruoli-524-527', text)


if __name__ == '__main__':
    unittest.main()
