"""devloop: retry of a tool failure on a PR implemented by the supervisor's agent (#406, ADR 0005,
P5-2), behind the revocation key ``[delegations] retry_own_prs``, off by default (D10)."""

import dataclasses
import tempfile
import unittest
from pathlib import Path

from devloop import config
from devloop.config import ConfigError, DelegationPolicy
from devloop.supervision import MAX_RETRIES
from test_devloop import CFG, pr, raw_review
from test_devloop_supervision import AUTO, HUMAN, REVIEW, SUPER, Case, crash

# The supervisor is the agent that implemented the PRs of the tests (the case of #357).
OWN = dataclasses.replace(AUTO, supervisor=CFG.implementer)
OWN_ON = dataclasses.replace(OWN, delegations=DelegationPolicy(retry_own_prs=True))
TRUSTED = 'brushtyler'  # the supervisor of OWN has no app in the tests: a trusted user commands


class ConfigTests(unittest.TestCase):
    def test_the_delegation_is_off_by_default(self):
        self.assertFalse(config.load_config().delegations.retry_own_prs)
        self.assertFalse(CFG.delegations.retry_own_prs)

    def test_the_overlay_turns_it_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / 'config.local.toml'
            local.write_text('[delegations]\nretry_own_prs = true\n')
            self.assertTrue(config.load_config(None, local).delegations.retry_own_prs)

    def test_invalid_values_stop_devloop(self):
        for table, error in (({'retry_own_prs': 1}, 'delegations.retry_own_prs: expected true'),
                             ({'retry_own_prs': 'yes'}, 'delegations.retry_own_prs'),
                             ({'capacity': True}, 'delegations: unknown keys'),
                             ([], 'delegations: expected a table')):
            with self.subTest(table=table), self.assertRaisesRegex(ConfigError, error):
                config.parse_delegations(table)

    def test_the_overlay_refuses_an_unknown_key(self):
        with self.assertRaisesRegex(ConfigError, 'delegations.capacity'):
            config.apply_overlay({}, {'delegations': {'capacity': True}})


class RoutingTests(Case):
    def fail_review(self, d, label):
        self.assertEqual(self.tick(d), [('review', 100)])
        self.assertEqual(self.gh.labels[100], {label})
        record = self.escalations(100)[-1]
        self.assertEqual(record['class'], 'tool_failure')
        return record

    def test_off_a_tool_failure_on_the_supervisors_pr_goes_to_the_operator(self):
        d = self.make(OWN, crash())
        record = self.fail_review(d, HUMAN)
        self.assertEqual(record['to'], 'operator')
        self.assertIn('`[delegations] retry_own_prs` è spenta', self.gh.threads[100][-1]['body'])

    def test_on_a_tool_failure_on_the_supervisors_pr_goes_to_the_supervisor(self):
        d = self.make(OWN_ON, crash(), raw_review())
        record = self.fail_review(d, SUPER)
        self.assertEqual((record['to'], record['step'], record['resume']),
                         ('supervisor', 'review', REVIEW))
        self.gh.command(100, '/devloop retry', login=TRUSTED)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'applied')
        self.assertEqual(self.tick(d), [('review', 100)])
        (review,) = self.gh.records(100, 'review')
        self.assertEqual(review['round'], 1)  # the retry counted no round

    def test_on_the_other_classes_still_go_to_the_operator(self):
        d = self.make(OWN_ON)
        for cls, step in (('sync_failed', 'sync'), ('preflight_default', 'preflight'),
                          ('merged_issue', 'implement')):
            with self.subTest(cls=cls):
                self.assertEqual(d._supervised(100, cls, step, pr()),
                                 (None, 'il supervisore ha implementato questa PR'))
        # A critical class never reaches the supervisor.
        self.assertEqual(d._supervised(100, 'round_limit', 'review', pr()), (None, ''))

    def test_the_limit_of_two_retries_is_unchanged_and_shared_with_the_operator(self):
        d = self.make(OWN_ON, crash(), crash(), crash())
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

    def test_a_revoked_delegation_refuses_the_retry_waiting_for_the_supervisor(self):
        d = self.make(OWN_ON, crash())
        self.fail_review(d, SUPER)
        d.cfg = OWN  # the operator turns the delegation off while the escalation waits
        self.gh.command(100, '/devloop retry', login=TRUSTED)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertEqual(self.escalations(100)[-1]['class'], 'supervisor_refused')
        self.assertIn('retry_own_prs', self.gh.threads[100][-2]['body'])
        self.assertEqual(len(self.runner.calls), 1)  # not run again


if __name__ == '__main__':
    unittest.main()
