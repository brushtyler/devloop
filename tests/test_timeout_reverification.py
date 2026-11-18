"""Operator reverification of the same captured candidate after a verifier timeout (#167)."""
import copy
import json
from pathlib import Path

import replay.repository_fixtures as fixtures
from ssdlc_limit_extensions import LimitExtensionError, activate, approve, propose, status
from ssdlc_recovery import LimitsStore
from ssdlc_tasks import DevelopmentBlocked, TaskError, TaskJournal, atomic_json, digest, git, validate_document
from test_recovery_limits import policy


SLOW = ['slow-suite']        # CHECK-GLOBAL: times out at its 5 s timeout on the candidate
FAST = ['task-check']        # CHECK-T1: always passes


class Interrupted(Exception):
    pass


class TimeoutReverificationTests(fixtures.RepositoryTests):
    def setUp(self):
        super().setUp()
        self.wt = self.worktree()
        self.quality['checks'][0]['argv'] = SLOW
        self.plan['tasks'][0]['verification']['checks'][0]['argv'] = FAST
        atomic_json(self.artifacts / '02-design/plan.json', self.plan)
        atomic_json(self.artifacts / '00-recon/quality-contract.json', self.quality)
        self.now = 1000
        self.clock = lambda: self.now
        atomic_json(self.artifacts / 'state.json', {'status': 'IN_PROGRESS', 'ledger': []})
        LimitsStore(self.artifacts, self.clock).authorize(policy(invocation_timeout_seconds=30),
                                                          {'ticket': 'TKT-1', 'controller': 'offline'})
        self.dispatches, self.calls = [], []
        self.needed = 20   # Seconds the slow suite needs on the candidate.
        self.interrupt = False
        with self.assertRaises(DevelopmentBlocked) as caught:
            self.scheduler_for().run(self.plan['tasks'])
        self.assertEqual(caught.exception.category, 'ENVIRONMENT_DIAGNOSIS')
        self.stop()
        self.failed = self.record()

    # --- fixture helpers ---------------------------------------------------

    def develop(self, task, attempt, prompt, workspace):
        self.dispatches.append(attempt)
        (workspace / 'app.py').write_text('value = 1\n')
        return {'output': 'TASK_DEV_STATUS: COMPLETE', 'exit_code': 0, 'quiescent': True}

    def verify(self, argv, cwd, timeout, workspace):
        candidate = (workspace / 'app.py').read_text() == 'value = 1\n'
        self.calls.append((argv[0], timeout, candidate))
        if self.interrupt and candidate and argv == SLOW:
            raise Interrupted('host stopped during verification')
        if argv == SLOW and candidate and timeout < self.needed:
            return 'partial progress', 'Verification timeout', 124
        return 'Ran 1 test\nOK', '', 0

    def scheduler_for(self):
        return self.scheduler(self.develop, self.verify, isolate_candidates=True,
                              recover_timeouts=True, max_attempts=1)

    def stop(self):
        state = json.loads((self.artifacts / 'state.json').read_text())
        atomic_json(self.artifacts / 'state.json', dict(state, status='FAILED'))

    def record(self):
        return TaskJournal(self.artifacts / '03-dev/tasks').peek()['tasks']['T1']

    def request(self, new=20, **overrides):
        value = dict(operation='reverify_after_timeout', task_id='T1',
                     changes=[{'dimension': 'check_timeout.CHECK-GLOBAL', 'new': new}],
                     cause='The suite outgrew its timeout on the captured candidate.',
                     evidence=[{'description': 'timed-out check log', 'sha256': 'b' * 64}], actor='operator')
        value.update(overrides)
        return value

    def propose(self, request=None):
        return propose(self.artifacts, request or self.request(), clock=self.clock)

    def authorize(self, new=20):
        sha = self.propose(self.request(new))['proposal_sha256']
        approve(self.artifacts, sha, expires_at=self.now + 100, actor='operator', source_fenced=True,
                clock=self.clock)
        activate(self.artifacts, sha, clock=self.clock)
        return sha

    def resume(self):
        self.dispatches.clear(); self.calls.clear()
        return self.scheduler_for().run(self.plan['tasks'])

    def snapshot(self):
        return {name: (self.artifacts / name).read_bytes() for name in
                ('limits/policy.json', 'limits/task-journal.jsonl', '02-design/plan.json',
                 '00-recon/quality-contract.json')}

    # --- criterion 1: same candidate, no inference, no new attempt -------------

    def test_authorized_reverification_reuses_the_same_candidate_without_a_developer(self):
        self.assertEqual(self.failed['attempts'], 1)
        self.assertEqual(len(self.failed['capture_verifications']), 1)
        before = self.snapshot()
        sha = self.authorize()
        # Activation changes no limit and calls no model: only the task journal grant.
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.dispatches, [1])               # Only the original attempt.
        self.assertEqual(status(self.artifacts, clock=self.clock)['chain'], [])
        result = self.resume()
        record = result['tasks']['T1']
        self.assertEqual(self.dispatches, [])               # No developer call.
        self.assertEqual(record['status'], 'COMPLETED')
        self.assertEqual(record['attempts'], 1)              # No new attempt.
        self.assertEqual(record['capture_sha256'], self.failed['capture_sha256'])
        self.assertEqual(record['candidate_tree'], self.failed['failed_attempts'][-1]['tree'])
        self.assertEqual(git(self.wt, 'show', 'HEAD:app.py'), 'value = 1')
        self.assertEqual(git(self.wt, 'rev-parse', 'HEAD^'), self.base)
        self.assertEqual(record['timeout_reverifications'][-1]['proposal_sha256'], sha)
        self.assertEqual(record['timeout_reverifications'][-1]['status'], 'PASSED')
        self.assertEqual(len(record['capture_verifications']), 2)
        self.assertIn('TIMEOUT_REVERIFICATION_STARTED',
                      [json.loads(line)['event'] for line in
                       (self.artifacts / '03-dev/tasks/task-journal.jsonl').read_text().splitlines()])

    def test_proposal_cites_the_timed_out_check_and_its_measured_duration(self):
        result = self.propose()
        proposal = result['proposal']
        validate_document('limit-extension-proposal', proposal)
        self.assertEqual(result['model_dispatches'], 0)
        self.assertEqual(proposal['new_profile'], LimitsStore(self.artifacts, self.clock)._load(repair=False)[0]['policy'])
        facts = proposal['reverification']
        self.assertEqual(facts['attempt'], 1)
        self.assertEqual(facts['failure_sha256'], digest(self.failed['failed_attempts'][-1]))
        self.assertEqual(facts['verifications_used'], 1)
        self.assertEqual(facts['verification_limit'], 3)
        [check] = facts['timed_out_checks']
        self.assertEqual((check['check_id'], check['exit_code'], check['timeout_sec'], check['applied_timeout_sec']),
                         ('CHECK-GLOBAL', 124, 5, 5))
        self.assertIsInstance(check['elapsed_seconds'], (int, float))
        self.assertEqual(proposal['changes'], [dict(dimension='check_timeout.CHECK-GLOBAL', old=5, new=20)])

    def test_next_proposal_cites_the_timeout_applied_by_the_failed_reverification(self):
        self.needed = 25   # Still too slow at 20 s: the reverification times out again.
        self.authorize(20)
        with self.assertRaises(DevelopmentBlocked):
            self.resume()
        self.stop()
        failure = self.record()['failed_attempts'][-1]
        [result] = [c for c in failure['checks'] if c['exit_code'] == 124]
        self.assertEqual(result['applied_timeout_sec'], 20)
        proposal = self.propose(self.request(25))['proposal']
        [check] = proposal['reverification']['timed_out_checks']
        # The evidence of the new failure, with the timeout it actually ran with;
        # the configured timeout stays unchanged.
        self.assertEqual((check['timeout_sec'], check['applied_timeout_sec'], check['log_ref']),
                         (5, 20, result['log_ref']))
        self.assertEqual(proposal['changes'], [dict(dimension='check_timeout.CHECK-GLOBAL', old=5, new=25)])
        for new in (10, 20):   # Not longer than the timeout that just timed out.
            with self.subTest(new=new):
                with self.assertRaisesRegex(LimitExtensionError, 'longer than the timed-out one'):
                    self.propose(self.request(new))
        self.authorize(25)
        self.assertEqual(self.resume()['tasks']['T1']['status'], 'COMPLETED')
        self.assertIn(('slow-suite', 25, True), self.calls)
        self.assertEqual(self.dispatches, [])

    def test_exit_124_without_a_measured_duration_is_refused(self):
        journal = TaskJournal(self.artifacts / '03-dev/tasks')
        state = journal.load()
        for check in state['tasks']['T1']['failed_attempts'][-1]['checks']:
            check.pop('elapsed_seconds', None)
        journal.append('LEGACY_FAILURE_FIXTURE', state)
        with self.assertRaisesRegex(LimitExtensionError, 'Exit 124 alone'):
            self.propose()

    def test_activation_requires_the_operator_authorization(self):
        sha = self.propose()['proposal_sha256']
        with self.assertRaisesRegex(LimitExtensionError, 'exact operator authorization'):
            activate(self.artifacts, sha, clock=self.clock)
        with self.assertRaisesRegex(DevelopmentBlocked, 'explicit reconciliation'):
            self.resume()
        self.assertEqual(self.dispatches, [])
        self.assertNotIn(('slow-suite', 20, True), self.calls)

    # --- criterion 2: only timed-out checks, only for that reverification -------

    def test_only_timed_out_checks_receive_a_bounded_longer_timeout(self):
        cases = {
            'check_timeout.CHECK-T1': 'Only a timed-out check',        # Did not time out.
            'check_timeout.CHECK-MISSING': 'Only a timed-out check',
            'metered_usd': 'Only a timed-out check',                   # No limit through this operation.
        }
        for dimension, message in cases.items():
            with self.subTest(dimension=dimension):
                with self.assertRaisesRegex(LimitExtensionError, message):
                    self.propose(self.request(changes=[{'dimension': dimension, 'new': 20}]))
        for new in (5, 4, 31, 20.5, True):    # Not longer, beyond invocation_timeout_seconds, not integral.
            with self.subTest(new=new):
                with self.assertRaisesRegex(LimitExtensionError, 'within invocation_timeout_seconds'):
                    self.propose(self.request(new))
        with self.assertRaisesRegex(LimitExtensionError, 'names its task'):
            self.propose(self.request(task_id=None))
        self.propose(self.request(30))         # The bound itself is admitted.

    def test_longer_timeout_applies_only_to_the_timed_out_check_in_that_reverification(self):
        before = self.snapshot()
        self.needed = 25   # Still too slow at 20 s: the reverification fails again.
        self.authorize(20)
        with self.assertRaises(DevelopmentBlocked) as caught:
            self.resume()
        self.assertEqual(caught.exception.category, 'ENVIRONMENT_DIAGNOSIS')
        # Preflight at the checkpoint keeps its timeouts; on the candidate only the slow suite gets 20 s.
        self.assertEqual([c for c in self.calls if not c[2]], [('slow-suite', 5, False)])
        self.assertEqual(sorted(c for c in self.calls if c[2]), [('slow-suite', 20, True), ('task-check', 5, True)])
        self.assertEqual(self.dispatches, [])
        record = self.record()
        self.assertEqual(record['timeout_reverifications'][-1]['status'], 'FAILED')
        self.assertEqual(record['failed_attempts'][-1]['origin'], 'timeout_reverification')
        self.assertEqual(record['attempts'], 1)
        self.assertEqual(git(self.wt, 'status', '--porcelain'), '')
        # Commands, plan, quality contract and limits are unchanged.
        self.assertEqual(self.snapshot(), before)
        # The grant is spent: resuming again runs no longer timeout and no developer.
        self.stop()
        with self.assertRaisesRegex(DevelopmentBlocked, 'explicit reconciliation'):
            self.resume()
        self.assertEqual(self.dispatches, [])
        self.assertNotIn(('slow-suite', 20, True), self.calls)

    def test_forged_grant_beyond_the_bound_is_refused_by_the_scheduler(self):
        self.authorize(20)
        journal = TaskJournal(self.artifacts / '03-dev/tasks')
        state = journal.load()
        grant = state['tasks']['T1']['timeout_reverifications'][-1]
        for forged in (dict(grant, timeouts={'CHECK-GLOBAL': dict(old=5, new=31)}),
                       dict(grant, timeouts={'CHECK-T1': dict(old=5, new=20)}),
                       dict(grant, failure_sha256='0' * 64)):
            with self.subTest(forged=forged['timeouts']):
                changed = copy.deepcopy(state)
                changed['tasks']['T1']['timeout_reverifications'][-1] = forged
                journal.append('FORGED_FIXTURE', changed)
                with self.assertRaisesRegex(DevelopmentBlocked, 'differs from its captured failure'):
                    self.resume()
                self.assertFalse([c for c in self.calls if c[2]])
        self.assertEqual(self.dispatches, [])

    def test_grant_is_checked_against_its_authorization_and_the_frozen_policy(self):
        before = self.snapshot()
        self.authorize(20)
        journal = TaskJournal(self.artifacts / '03-dev/tasks')
        state = journal.load()
        grant = state['tasks']['T1']['timeout_reverifications'][-1]
        # Consistent with itself, but not with the proposal, the authorization,
        # the receipt or the policy frozen at 30 s.
        for forged in (dict(grant, invocation_timeout_seconds=300, timeouts={'CHECK-GLOBAL': dict(old=5, new=300)}),
                       dict(grant, timeouts={'CHECK-GLOBAL': dict(old=5, new=25)}),
                       dict(grant, authorization_sha256='d' * 64),
                       dict(grant, proposal_sha256='c' * 64)):
            with self.subTest(forged=forged):
                changed = copy.deepcopy(state)
                changed['tasks']['T1']['timeout_reverifications'][-1] = forged
                journal.append('FORGED_FIXTURE', changed)
                with self.assertRaisesRegex(DevelopmentBlocked, 'differs from its operator authorization'):
                    self.resume()
                self.assertFalse([c for c in self.calls if c[2]])
        self.assertEqual(self.dispatches, [])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.record()['attempts'], 1)
        # The genuine grant still runs once restored.
        journal.append('RESTORED_FIXTURE', state)
        self.assertEqual(self.resume()['tasks']['T1']['status'], 'COMPLETED')
        self.assertIn(('slow-suite', 20, True), self.calls)

    def test_missing_receipt_refuses_the_grant(self):
        sha = self.authorize(20)
        (self.artifacts / 'limit-extensions' / sha / 'receipt.json').unlink()
        with self.assertRaisesRegex(DevelopmentBlocked, 'differs from its operator authorization'):
            self.resume()
        self.assertFalse([c for c in self.calls if c[2]])
        self.assertEqual(self.dispatches, [])

    # --- criterion 3: the fourth verification is refused -----------------------

    def test_fourth_verification_is_refused(self):
        self.needed = 99   # Every reverification times out again.
        for used, new in ((1, 20), (2, 25)):   # Each longer than the timeout that last timed out.
            self.assertEqual(self.propose(self.request(new))['proposal']['reverification']['verifications_used'],
                             used)
            self.authorize(new)
            with self.assertRaises(DevelopmentBlocked):
                self.resume()
            self.stop()
        record = self.record()
        self.assertEqual(len([r for r in record['capture_verifications'] if r['attempt'] == 1]), 3)
        self.assertEqual(record['attempts'], 1)
        with self.assertRaisesRegex(LimitExtensionError, 'already used its 3 verifications'):
            self.propose()
        # A grant written past the allowance is refused by the scheduler as well.
        journal = TaskJournal(self.artifacts / '03-dev/tasks')
        state = journal.load()
        row = state['tasks']['T1']
        row['timeout_reverifications'].append(dict(row['timeout_reverifications'][-1], status='AUTHORIZED',
                                                    failure_sha256=digest(row['failed_attempts'][-1]),
                                                    timeouts={'CHECK-GLOBAL': dict(old=5, new=30)},
                                                    proposal_sha256='c' * 64))
        journal.append('FORGED_FIXTURE', state)
        with self.assertRaisesRegex(DevelopmentBlocked, 'differs from its operator authorization'):
            self.resume()
        self.assertFalse([c for c in self.calls if c[2]])
        self.assertEqual(self.dispatches, [])
        self.assertEqual(len(self.record()['capture_verifications']), 3)

    def test_scheduler_refuses_a_fourth_verification_even_with_a_genuine_grant(self):
        self.needed = 99
        self.authorize()
        with self.assertRaises(DevelopmentBlocked):
            self.resume()
        self.stop()
        self.authorize(25)   # Proposed and activated with two verifications used.
        # A third verification recorded after the activation leaves no room for this one.
        journal = TaskJournal(self.artifacts / '03-dev/tasks')
        state = journal.load()
        state['tasks']['T1']['capture_verifications'].append({'attempt': 1, 'dir': 'T1/attempt-1/verification-3'})
        journal.append('THIRD_VERIFICATION_FIXTURE', state)
        with self.assertRaisesRegex(DevelopmentBlocked, 'verification allowance exhausted'):
            self.resume()
        self.assertFalse([c for c in self.calls if c[2]])
        self.assertEqual(self.dispatches, [])
        self.assertEqual(len(self.record()['capture_verifications']), 3)

    def test_interrupted_reverification_is_counted_and_never_repeated(self):
        self.authorize()
        self.interrupt = True
        with self.assertRaises(Interrupted):
            self.resume()
        self.interrupt = False
        with self.assertRaisesRegex(DevelopmentBlocked, 'interrupted; a new operator authorization'):
            self.resume()
        self.assertFalse([c for c in self.calls if c[2]])
        record = self.record()
        self.assertEqual(record['timeout_reverifications'][-1]['status'], 'INTERRUPTED')
        self.assertEqual(record['status'], 'FAILED')
        self.assertEqual(len(record['capture_verifications']), 2)
        self.assertEqual(git(self.wt, 'status', '--porcelain'), '')
        # A new authorization reverifies the same candidate with the remaining allowance.
        self.stop()
        self.assertEqual(self.propose()['proposal']['reverification']['verifications_used'], 2)
        self.authorize()
        self.assertEqual(self.resume()['tasks']['T1']['status'], 'COMPLETED')
        self.assertEqual(self.dispatches, [])

    def test_activation_replay_records_one_grant(self):
        sha = self.propose()['proposal_sha256']
        approve(self.artifacts, sha, expires_at=self.now + 100, actor='operator', source_fenced=True,
                clock=self.clock)
        def crash(point):
            if point == 'scheduler_updated':
                raise Interrupted(point)
        with self.assertRaises(Interrupted):
            activate(self.artifacts, sha, clock=self.clock, fault=crash)
        with self.assertRaisesRegex(TaskError, 'pending'):
            LimitsStore(self.artifacts, self.clock)._load(repair=False)
        self.now += 500   # A replay completes published effects even after expiry.
        self.assertTrue(activate(self.artifacts, sha, clock=self.clock)['replayed'])
        self.assertTrue(activate(self.artifacts, sha, clock=self.clock)['replayed'])
        self.assertEqual(len(self.record()['timeout_reverifications']), 1)
        self.assertTrue((self.artifacts / 'limit-extensions' / sha / 'receipt.json').is_file())
