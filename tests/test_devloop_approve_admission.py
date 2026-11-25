"""devloop: the operator's ``/devloop approve`` admits an issue that touches the trust policy
(#644).

With ``[roles.coordinator]``, an issue not opened by a trusted user that the readiness check
declares touching the trust policy waits for a trusted user's ``agent:ready`` (#595, #607). It
passes the gate (``_coordinator_gate``) also when all of these hold:

- the check declares ``trust_policy: covered``;
- at least one ``/devloop approve`` on the issue counts (#483: a trusted user in person,
  without the header, after the latest change of the text, not changed after the takeover);
- the latest ``agent:ready`` is the coordinator's or the supervisor's and, on an issue the
  coordinator did not open, a ``/devloop authorize`` of the current text holds, whoever added
  the label (#607).

A ``covered`` without an approve, an ``uncovered`` and anything else stay as before.
"""

import unittest
from pathlib import Path

import test_devloop_authorize as authorize_tests
import test_devloop_preflight_recheck as recheck_tests
from devloop.dispatch_preflight import GAP_TEXTS
from devloop.dispatcher import Dispatcher
from test_devloop_coordinator import COORDINATOR
from test_devloop_supervision import HUMAN, READY, SUPERVISOR_LOGIN, Identities
from test_devloop_supervision import header as supervisor_header

CFG = authorize_tests.CFG
ROOT = Path(__file__).resolve().parents[1]
WORKING = 'agent:working'
APPROVAL = ('/devloop approve\n\nApprovo la modifica della policy di fiducia descritta qui, '
            'nei limiti del suo testo.')


class ApproveGH(authorize_tests.TextGH):
    """``TextGH`` with a history read in full (``complete``, #483), the takeovers of the issue
    (``working_events``) among its ``labeled`` events, and comments that carry their latest
    change (``updated_at``)."""

    def __init__(self):
        super().__init__()
        self.working_events = []

    def issue_history(self, number):
        hist = super().issue_history(number)
        return dict(hist, complete=True,
                    labeled=[*hist['labeled'],
                             *((WORKING, who, at) for who, at in self.working_events)])

    def say(self, number, login, body):
        super().say(number, login, body)
        comment = self.threads[number][-1]
        comment['updated_at'] = comment['created_at']


class Case(authorize_tests.Case):
    def make(self, cfg=CFG):
        self.cfg = cfg
        self.gh, self.ws = ApproveGH(), recheck_tests.CountingWS()
        self.runner, self.logs = recheck_tests.StepRunner(), []
        return Dispatcher(cfg, self.gh, self.ws, self.runner, log=self.logs.append,
                          identities=Identities(self.gh))

    def approve(self, login='brushtyler', body=APPROVAL):
        """``login`` writes ``body`` on issue 7: by default the operator's approve."""
        self.gh.say(7, login, body)
        return self.gh.threads[7][-1]

    def gap(self, record):
        return record['gaps'][0]['description'] if record['gaps'] else None

    def gate(self, d, trust='covered'):
        """The outcome of a positive check that declares ``trust`` through the gate, on the
        issue as it is now."""
        outcome = {'ready': True, 'gaps': [], 'split': '', 'trust_policy': trust,
                   'trust_policy_reason': 'punto 1'}
        return d._coordinator_gate(self.gh.issue_history(7), outcome,
                                   self.gh.issue_comments(7))


class AdmissionTests(Case):
    def test_a_valid_approve_admits_the_supervisors_issue(self):
        d = self.make()
        self.authorize(d)
        self.gh.ready()
        self.approve()
        self.assertEqual(self.planned(d), [('preflight', 7)])
        record = self.check(d, 'covered')
        self.assertTrue(record['ready'])
        self.assertEqual(record['gaps'], [])
        self.assertEqual(self.gh.labels[7], {READY})
        self.assertEqual(self.planned(d), [('implement', 7)])

    def test_a_valid_approve_admits_the_coordinators_issue_without_authorize(self):
        d = self.make()
        self.gh.author = COORDINATOR
        self.gh.labels[7] = set()
        self.gh.ready(COORDINATOR)
        self.approve()
        self.assertTrue(self.check(d, 'covered')['ready'])
        self.assertEqual(self.planned(d), [('implement', 7)])

    def test_an_approve_written_after_the_block_admits_the_issue(self):
        # The gate blocked it; the operator's approve changes the request: a new check admits it.
        d = self.make()
        self.authorize(d)
        self.gh.ready()
        record = self.check(d, 'covered')
        self.assertFalse(record['ready'])
        self.assertEqual(self.gh.labels[7], {READY, HUMAN})
        self.approve()
        self.assertEqual(self.planned(d), [('preflight', 7)])
        self.assertTrue(self.check(d, 'covered')['ready'])
        self.assertEqual(self.gh.labels[7], {READY})
        self.assertEqual(self.planned(d), [('implement', 7)])

    def test_an_approve_written_before_a_change_of_the_text_does_not_admit(self):
        d = self.make()
        self.approve()
        self.gh.edit('Do another thing.')
        self.authorize(d)
        self.gh.ready()
        record = self.check(d, 'covered')
        self.assertFalse(record['ready'])
        self.assertEqual(self.gap(record), GAP_TEXTS.texts(CFG.language)('authorized_gap'))
        self.assertEqual(self.gh.labels[7], {READY, HUMAN})
        # A new approve, of the text read now, admits it.
        self.approve()
        self.assertTrue(self.check(d, 'covered')['ready'])

    def test_an_approve_changed_after_the_takeover_does_not_admit(self):
        d = self.make()
        self.authorize(d)
        self.gh.ready()
        approval = self.approve()
        self.gh.working_events.append(('brushtyler', self.gh.now()))
        self.assertTrue(self.gate(d)['ready'])
        approval['updated_at'] = self.gh.now()
        gated = self.gate(d)
        self.assertFalse(gated['ready'])
        self.assertEqual(gated['gaps'][0]['kind'], 'operator_decision')

    def test_covered_without_an_approve_stays_the_operators(self):
        for author in (SUPERVISOR_LOGIN, COORDINATOR):
            with self.subTest(author=author):
                d = self.make()
                self.gh.author = author
                self.authorize(d)
                self.gh.ready()
                record = self.check(d, 'covered')
                self.assertFalse(record['ready'])
                self.assertEqual(record['gaps'][0]['kind'], 'operator_decision')
                self.assertEqual(self.gh.labels[7], {READY, HUMAN})

    def test_uncovered_with_an_approve_stays_not_ready(self):
        d = self.make()
        self.authorize(d)
        self.gh.ready()
        self.approve()
        record = self.check(d, 'uncovered')
        self.assertFalse(record['ready'])
        self.assertEqual(record['gaps'][0]['kind'], 'operator_decision')
        self.assertEqual(self.gh.labels[7], {READY, HUMAN})
        self.assertFalse(self.gate(d, 'uncovered')['ready'])
        # A check that does not declare the trust policy fails closed.
        self.assertFalse(self.gate(d, None)['ready'])

    def test_the_supervisors_ready_without_authorize_does_not_admit(self):
        d = self.make()
        self.gh.labels[7] = set()
        self.gh.ready()
        self.approve()
        # Planning ignores the label (#607), and the gate does not admit it either.
        self.assertEqual(self.planned(d), [])
        self.assertFalse(self.gate(d)['ready'])
        # The coordinator's authorization of the current text makes the approve count.
        self.authorize(d)
        self.assertTrue(self.gate(d)['ready'])

    def test_the_coordinators_ready_on_the_supervisors_issue_needs_authorize(self):
        # The authorization is the issue's, not the label's: the coordinator's agent:ready on
        # an issue it did not open admits it only with a /devloop authorize of the current text.
        d = self.make()
        self.gh.labels[7] = set()
        self.gh.ready(COORDINATOR)
        self.approve()
        self.assertEqual(self.gh.issue_history(7)['author'], SUPERVISOR_LOGIN)
        # No authorization.
        record = self.check(d, 'covered')
        self.assertFalse(record['ready'])
        self.assertEqual(self.gap(record), GAP_TEXTS.texts(CFG.language)('authorized_gap'))
        self.assertFalse(self.gate(d)['ready'])

    def test_the_coordinators_ready_with_a_stale_authorize_does_not_admit(self):
        d = self.make()
        self.authorize(d)
        self.gh.edit('Do another thing.')
        self.gh.labels[7] = set()
        self.gh.ready(COORDINATOR)
        self.approve()
        self.assertIsNone(d._authorization(self.gh.issue_history(7), self.gh.issue_comments(7)))
        self.assertFalse(self.gate(d)['ready'])

    def test_the_coordinators_ready_with_a_valid_authorize_admits(self):
        d = self.make()
        self.authorize(d)
        self.gh.labels[7] = set()
        self.gh.ready(COORDINATOR)
        self.approve()
        self.assertTrue(self.check(d, 'covered')['ready'])
        self.assertEqual(self.planned(d), [('implement', 7)])

    def test_an_approve_that_does_not_count_does_not_admit(self):
        cases = (('header', 'brushtyler', supervisor_header() + '\n' + APPROVAL),
                 ('supervisor', SUPERVISOR_LOGIN, APPROVAL),
                 ('stranger', 'stranger', APPROVAL),
                 ('no text', 'brushtyler', '/devloop approve'))
        for name, login, body in cases:
            with self.subTest(case=name):
                d = self.make()
                self.authorize(d)
                self.gh.ready()
                self.approve(login, body)
                self.assertFalse(self.gate(d)['ready'])

    def test_a_label_of_someone_else_does_not_admit(self):
        # The latest agent:ready must be the coordinator's or the supervisor's.
        d = self.make()
        self.authorize(d)
        self.gh.ready('stranger')
        self.approve()
        self.assertFalse(self.gate(d)['ready'])

    def test_a_strangers_issue_is_not_admitted(self):
        d = self.make()
        self.gh.author = 'stranger'
        self.gh.labels[7] = set()
        self.gh.ready(COORDINATOR)
        self.approve()
        self.assertFalse(self.gate(d)['ready'])

    def test_a_reused_check_holds_with_the_approve(self):
        d = self.make()
        self.gh.author = COORDINATOR
        self.gh.labels[7] = set()
        self.gh.ready('brushtyler')
        self.approve()
        self.assertTrue(self.check(d, 'covered')['ready'])
        # The operator's agent:ready replaced by the coordinator's: the approve still admits it.
        self.gh.ready(COORDINATOR)
        self.assertEqual(self.planned(d), [('implement', 7)])


class GapTextTests(unittest.TestCase):
    def test_the_gaps_name_the_ready_label_and_the_approve(self):
        for language, ready, approve in (('Italian', 'rimettendo `agent:ready`', 'oppure'),
                                         ('English', 'adding `agent:ready`', ' or ')):
            t = GAP_TEXTS.texts(language)
            for key in ('coordinator_gap', 'authorized_gap'):
                with self.subTest(language=language, key=key):
                    self.assertIn(ready, t(key))
                    self.assertIn(approve, t(key))
                    self.assertIn('`/devloop approve`', t(key))


class DocsTests(unittest.TestCase):
    def test_the_guide_and_the_runbooks_describe_the_admission(self):
        for path in ('docs/devloop/guide.md', 'docs/devloop/roles/coordinator.md',
                     'docs/devloop/roles/supervisor.md'):
            with self.subTest(path=path):
                text = ' '.join((ROOT / path).read_text(encoding='utf-8').split())
                self.assertIn('#644', text)


if __name__ == '__main__':
    unittest.main()
