"""Common contract for manual limit extensions (#162): propose, authorize, activate."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from ssdlc_deadline_extensions import CAPABILITY as REPAIR_DEADLINE, LIMIT_CAPABILITY, accounting_anchor
from ssdlc_limit_extensions import (CONTRACT, LimitExtensionError, activate, approve, describe, inspect,
                                    main, propose, status, unwind)
from ssdlc_recovery import LimitsStore, RecoveryStop
from ssdlc_tasks import DevelopmentBlocked, TaskError, TaskJournal, atomic_json, digest, validate_document
from test_recovery_limits import policy


POINTS = ('intent_published', 'policy_written', 'journal_appended', 'scheduler_updated',
          'mode_published', 'receipt_written', 'pending_cleared')


class Fault(Exception):
    pass


def events(path):
    return [json.loads(line)['event'] for line in Path(path).read_text().splitlines()]


class NativeFixture:
    """A stopped native ticket with a frozen --limits-profile and real consumption."""
    def __init__(self, case):
        self.tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.ticket = self.repo / '.ssdlc' / 'T1'
        self.ticket.mkdir(parents=True)
        self.now = 100
        self.clock = lambda: self.now
        atomic_json(self.ticket / 'state.json', {'status': 'FAILED', 'ledger': []})
        self.binding = {'ticket': 'T1', 'controller': 'offline'}
        self.store = LimitsStore(self.ticket, self.clock)
        self.store.authorize(policy(), self.binding)
        known = self.store.reserve('03-dev', '03-dev', 'claude')
        self.store.finish(known, 'returned', {'metered_usd': None, 'cli_estimate_usd': 2.5})
        unknown = self.store.reserve('03-dev', '03-dev', 'codex')
        self.store.finish(unknown, 'returned')
        tasks = TaskJournal(self.ticket / '03-dev/tasks')
        tasks.append('INITIALIZED', {'tasks': {'T01': {'status': 'FAILED', 'attempts': 2},
                                               'T02': {'status': 'PENDING', 'attempts': 0}},
                                     'attempt_limit': 2, 'bounded_recovery': 1})

    def request(self, **overrides):
        value = dict(operation='extend_global_limit', changes=[{'dimension': 'metered_usd', 'new': 20}],
                     cause='The reviewed run exhausted its frozen allowance.',
                     evidence=[{'description': 'limits inspect output', 'sha256': 'a' * 64}], actor='operator')
        value.update(overrides)
        return value

    def propose(self, request=None, **kwargs):
        return propose(self.ticket, request or self.request(), clock=self.clock, **kwargs)['proposal_sha256']

    def approve(self, sha, expires=None, **kwargs):
        return approve(self.ticket, sha, expires_at=self.now + 50 if expires is None else expires,
                       actor='operator', source_fenced=True, clock=self.clock, **kwargs)

    def activate(self, sha, **kwargs):
        return activate(self.ticket, sha, clock=self.clock, **kwargs)

    def load(self):
        return LimitsStore(self.ticket, self.clock)._load(repair=False)

    def journal_events(self):
        return events(self.ticket / 'limits/task-journal.jsonl')

    def task_events(self):
        return events(self.ticket / '03-dev/tasks/task-journal.jsonl')

    def snapshot(self):
        return {name: (self.ticket / name).read_bytes() for name in
                ('limits/policy.json', 'limits/task-journal.jsonl', '03-dev/tasks/task-journal.jsonl',
                 'execution-mode.json', 'state.json')}


class ProposalValidationTests(unittest.TestCase):
    """Criterion 1: schema and validators for the two operations."""
    def setUp(self):
        self.f = NativeFixture(self)

    def test_unknown_operation_is_refused_by_validator_and_schema(self):
        with self.assertRaisesRegex(LimitExtensionError, 'Unknown limit extension operation'):
            self.f.propose(self.f.request(operation='extend_everything'))
        sha = self.f.propose()
        proposal = json.loads((self.f.ticket / 'limit-extensions' / sha / 'proposal.json').read_text())
        validate_document('limit-extension-proposal', proposal)
        proposal['operation'] = 'extend_everything'
        with self.assertRaises(TaskError):
            validate_document('limit-extension-proposal', proposal)

    def test_dimensions_outside_the_contract_are_refused(self):
        cases = {
            'transport_retries': 'not admitted', 'design_rounds': 'not admitted',
            'invocation_timeout_seconds': 'not admitted', 'design_diagnoses': 'not admitted',
            'combined_reported_usd_cap': 'combined_reported_usd_cap cannot be modified',
            'wall_seconds': 'only through extend_deadline', 'deadline_at': 'only through extend_deadline',
            'role_invocations.04-review': 'Only an existing cap', 'phase_invocations.missing': 'Only an existing cap',
        }
        for dimension, message in cases.items():
            with self.subTest(dimension=dimension):
                with self.assertRaisesRegex(LimitExtensionError, message):
                    self.f.propose(self.f.request(changes=[{'dimension': dimension, 'new': 99}]))
        sha = self.f.propose()
        proposal = json.loads((self.f.ticket / 'limit-extensions' / sha / 'proposal.json').read_text())
        proposal['changes'][0]['dimension'] = 'transport_retries'
        with self.assertRaises(TaskError):
            validate_document('limit-extension-proposal', proposal)
        self.assertEqual(len(list((self.f.ticket / 'limit-extensions').iterdir())), 1)

    def test_only_increases_of_the_right_type_and_unique_dimensions(self):
        bad = [[{'dimension': 'metered_usd', 'new': 10}], [{'dimension': 'metered_usd', 'new': 5}],
               [{'dimension': 'invocations', 'new': 12.5}], [{'dimension': 'invocations', 'new': True}],
               [{'dimension': 'metered_usd', 'new': float('inf')}],
               [{'dimension': 'metered_usd', 'new': 20}, {'dimension': 'metered_usd', 'new': 30}],
               [{'dimension': 'metered_usd', 'old': 9, 'new': 20}],
               [{'dimension': 'development_attempts', 'new': 11}], []]
        for changes in bad:
            with self.subTest(changes=changes):
                with self.assertRaises(LimitExtensionError):
                    self.f.propose(self.f.request(changes=changes))

    def test_implicit_increase_is_refused(self):
        requested = copy.deepcopy(self.f.load()[0]['policy'])
        requested['metered_usd'] = 20
        requested['invocations'] = 50  # Not named among the changes.
        with self.assertRaisesRegex(LimitExtensionError, 'no implicit increase'):
            self.f.propose(self.f.request(profile=requested))
        # A stored proposal edited to raise an unnamed dimension is refused at activation.
        sha = self.f.propose()
        folder = self.f.ticket / 'limit-extensions'
        proposal = json.loads((folder / sha / 'proposal.json').read_text())
        proposal['new_profile']['invocations'] = 50
        forged = digest(proposal)
        (folder / forged).mkdir()
        atomic_json(folder / forged / 'proposal.json', proposal)
        self.f.approve(forged)
        before = self.f.snapshot()
        with self.assertRaisesRegex(LimitExtensionError, 'no implicit increase'):
            self.f.activate(forged)
        self.assertEqual(self.f.snapshot(), before)

    def test_deadline_operation_changes_only_the_deadline(self):
        for changes in ([{'dimension': 'deadline_at', 'new': 1500}, {'dimension': 'metered_usd', 'new': 20}],
                        [{'dimension': 'metered_usd', 'new': 20}], [{'dimension': 'deadline_at', 'new': 1500.5}],
                        [{'dimension': 'deadline_at', 'new': 1100}]):
            with self.subTest(changes=changes):
                with self.assertRaises(LimitExtensionError):
                    self.f.propose(self.f.request(operation='extend_deadline', changes=changes))

    def test_proposal_records_cause_identity_parent_consumption_and_applicability(self):
        sha = self.f.propose(self.f.request(changes=[{'dimension': 'metered_usd', 'new': 20},
                                                     {'dimension': 'role_invocations.03-dev', 'new': 5},
                                                     {'dimension': 'phase_invocations.03-dev', 'new': 5},
                                                     {'dimension': 'development_attempts', 'new': 3}],
                                            task_id='T01'))
        proposal = json.loads((self.f.ticket / 'limit-extensions' / sha / 'proposal.json').read_text())
        envelope, counters = self.f.load()
        self.assertEqual(proposal['contract'], CONTRACT)
        self.assertEqual(proposal['run'], dict(ticket='T1', source='native', generation=None, task_id='T01'))
        self.assertEqual(proposal['parent']['envelope_sha256'], digest(envelope))
        self.assertEqual(proposal['parent']['counters_sha256'], digest(counters))
        self.assertEqual(proposal['changes'][0], dict(dimension='metered_usd', old=10, new=20))
        consumption = proposal['consumption']
        self.assertEqual(consumption['invocations'], 2)
        self.assertIsNone(consumption['observed_usd']['metered_usd'])  # Unknown stays null, not 0.
        self.assertEqual(consumption['observed_usd']['cli_estimate_usd'], 2.5)
        self.assertEqual(len(consumption['unknown_usage_invocations']), 1)
        self.assertEqual(consumption['development_attempts'], {'T01': 2, 'T02': 0})
        self.assertTrue(proposal['applicability']['resolved_invocations'])
        self.assertEqual(proposal['actor'], 'operator')
        self.assertTrue(proposal['cause'])
        self.assertFalse((self.f.ticket / 'limit-extensions' / sha / 'authorization.json').exists())

    def test_running_or_unresolved_sources_are_refused(self):
        identity = self.f.store.reserve('03-dev', '03-dev', 'codex')
        with self.assertRaisesRegex(LimitExtensionError, 'Unresolved invocation'):
            self.f.propose()
        self.f.store.finish(identity, 'returned')
        state = json.loads((self.f.ticket / 'state.json').read_text())
        atomic_json(self.f.ticket / 'state.json', dict(state, status='IN_PROGRESS'))
        with self.assertRaisesRegex(LimitExtensionError, 'stopped run'):
            self.f.propose()
        atomic_json(self.f.ticket / 'state.json', state)
        (self.f.ticket / 'feedback').mkdir()
        with self.assertRaisesRegex(LimitExtensionError, 'feedback recording ledger'):
            self.f.propose()


class AuthorizationAndActivationTests(unittest.TestCase):
    """Criterion 2: authorization binding, expiry, changed source, idempotent replay."""
    def setUp(self):
        self.f = NativeFixture(self)

    def test_missing_authorization_is_refused(self):
        sha = self.f.propose()
        before = self.f.snapshot()
        with self.assertRaisesRegex(LimitExtensionError, 'exact operator authorization'):
            self.f.activate(sha)
        self.assertEqual(self.f.snapshot(), before)

    def test_authorization_with_another_hash_is_refused(self):
        first = self.f.propose()
        second = self.f.propose(self.f.request(changes=[{'dimension': 'metered_usd', 'new': 30}]))
        self.f.approve(first)
        folder = self.f.ticket / 'limit-extensions'
        (folder / second / 'authorization.json').write_bytes((folder / first / 'authorization.json').read_bytes())
        before = self.f.snapshot()
        with self.assertRaisesRegex(LimitExtensionError, 'different proposal'):
            self.f.activate(second)
        self.assertEqual(self.f.snapshot(), before)
        # The proposal itself is bound by its hash: an edited proposal is another proposal.
        proposal = json.loads((folder / first / 'proposal.json').read_text())
        proposal['changes'][0]['new'] = 40
        atomic_json(folder / first / 'proposal.json', proposal)
        with self.assertRaisesRegex(LimitExtensionError, 'differs from its hash'):
            self.f.activate(first)

    def test_authorization_requires_an_explicit_future_expiry_and_fencing(self):
        sha = self.f.propose()
        with self.assertRaisesRegex(LimitExtensionError, 'already passed'):
            self.f.approve(sha, expires=self.f.now)
        with self.assertRaisesRegex(LimitExtensionError, 'explicit expires_at'):
            approve(self.f.ticket, sha, expires_at=None, actor='operator', source_fenced=True, clock=self.f.clock)
        with self.assertRaisesRegex(LimitExtensionError, 'fenced'):
            approve(self.f.ticket, sha, expires_at=200, actor='operator', source_fenced=False, clock=self.f.clock)
        self.f.approve(sha, expires=200)
        with self.assertRaisesRegex(LimitExtensionError, 'different authorization'):
            self.f.approve(sha, expires=300)

    def test_authorization_expired_before_activation_is_refused(self):
        sha = self.f.propose()
        self.f.approve(sha, expires=self.f.now + 10)
        self.f.now += 10
        before = self.f.snapshot()
        with self.assertRaisesRegex(LimitExtensionError, 'expired before activation'):
            self.f.activate(sha)
        self.assertEqual(self.f.snapshot(), before)
        self.assertEqual(status(self.f.ticket, clock=self.f.clock)['proposals'][0]['state'], 'EXPIRED')

    def test_changed_consumption_inputs_or_generation_are_refused(self):
        sha = self.f.propose()
        self.f.approve(sha)
        identity = self.f.store.reserve('03-dev', '03-dev', 'codex')
        self.f.store.finish(identity, 'returned')
        with self.assertRaisesRegex(LimitExtensionError, 'changed since the proposal'):
            self.f.activate(sha)
        # A proposal for another generation is refused before reading the source.
        sha = self.f.propose()
        folder = self.f.ticket / 'limit-extensions'
        proposal = json.loads((folder / sha / 'proposal.json').read_text())
        proposal['run'].update(source='reconciled', generation='b' * 32)
        other = digest(proposal)
        (folder / other).mkdir()
        atomic_json(folder / other / 'proposal.json', proposal)
        self.f.approve(other)
        with self.assertRaisesRegex(LimitExtensionError, 'another generation'):
            self.f.activate(other)
        # A changed task journal (attempts are relevant consumption) is refused too.
        sha = self.f.propose(self.f.request(changes=[{'dimension': 'development_attempts', 'new': 3}]))
        self.f.approve(sha)
        tasks = TaskJournal(self.f.ticket / '03-dev/tasks')
        state = tasks.load()
        state['tasks']['T02']['attempts'] = 1
        tasks.append('ATTEMPT', state)
        with self.assertRaisesRegex(LimitExtensionError, 'changed since the proposal'):
            self.f.activate(sha)

    def test_activation_extends_exactly_and_conserves_consumption(self):
        envelope, counters = self.f.load()
        sha = self.f.propose(self.f.request(changes=[{'dimension': 'metered_usd', 'new': 20},
                                                     {'dimension': 'invocations', 'new': 12},
                                                     {'dimension': 'role_invocations.03-dev', 'new': 5},
                                                     {'dimension': 'phase_invocations.03-dev', 'new': 5},
                                                     {'dimension': 'development_attempts', 'new': 3}]))
        self.f.approve(sha)
        self.assertEqual(self.f.activate(sha)['replayed'], False)
        extended, after = self.f.load()
        expected = copy.deepcopy(envelope['policy'])
        expected.update(metered_usd=20, invocations=12, development_attempts=3)
        expected['role_invocations']['03-dev'] = 5
        expected['phase_invocations']['03-dev'] = 5
        self.assertEqual(extended['policy'], expected)
        self.assertEqual(unwind(extended, None), envelope)
        self.assertEqual({k: v for k, v in after.items() if k != 'policy_sha256'},
                         {k: v for k, v in counters.items() if k != 'policy_sha256'})
        self.assertIsNone(next(iter(after['invocations'].values()))['usage']['metered_usd'])
        self.assertEqual(TaskJournal(self.f.ticket / '03-dev/tasks').peek()['attempt_limit'], 3)
        self.assertEqual(json.loads((self.f.ticket / 'execution-mode.json').read_text())['policy_sha256'],
                         digest(extended))
        entry = extended['binding']['limit_extensions'][0]
        self.assertEqual((entry['contract'], entry['proposal_sha256']), (CONTRACT, sha))
        self.f.store.reserve('03-dev', '03-dev', 'codex')  # The extended caps now admit a call.
        # Relaunching with the original profile keeps the extension.
        LimitsStore(self.f.ticket, self.f.clock).authorize(policy(), self.f.binding)
        self.assertEqual(self.f.load()[0]['policy']['metered_usd'], 20)
        with self.assertRaises(TaskError):
            LimitsStore(self.f.ticket, self.f.clock).authorize(policy(invocations=99), self.f.binding)

    def test_replay_after_publication_completes_without_double_effect(self):
        sha = self.f.propose(self.f.request(changes=[{'dimension': 'metered_usd', 'new': 20},
                                                     {'dimension': 'development_attempts', 'new': 3}]))
        self.f.approve(sha, expires=self.f.now + 5)
        self.f.activate(sha)
        once = self.f.snapshot()
        self.f.now += 100  # The authorization has expired: a replay only confirms.
        self.assertEqual(self.f.activate(sha), dict(status='ACTIVE', proposal_sha256=sha, replayed=True,
                                                    model_dispatches=0))
        self.assertEqual(self.f.snapshot(), once)
        self.assertEqual(self.f.journal_events().count('LIMITS_EXTENDED'), 1)
        self.assertEqual(self.f.task_events().count('ATTEMPT_LIMIT_EXTENDED'), 1)
        self.assertEqual(self.f.load()[0]['policy']['metered_usd'], 20)
        self.assertEqual(status(self.f.ticket, clock=self.f.clock)['proposals'][0]['state'], 'ACTIVE')
        # The same proposal cannot be authorized again, and a stale one cannot stack.
        with self.assertRaisesRegex(LimitExtensionError, 'already active'):
            self.f.approve(sha, expires=self.f.now + 50)

    def test_tampered_chain_fails_integrity(self):
        sha = self.f.propose()
        self.f.approve(sha)
        self.f.activate(sha)
        envelope, counters = self.f.load()
        envelope['binding']['limit_extensions'][0]['changes'][0]['old'] = 15
        atomic_json(self.f.ticket / 'limits/policy.json', envelope)
        journal = TaskJournal(self.f.ticket / 'limits')
        journal.load()
        journal.append('FORGED', dict(counters, policy_sha256=digest(envelope)))
        mode = dict(kind='bounded-i2', policy_sha256=digest(envelope))
        atomic_json(self.f.ticket / 'execution-mode.json', dict(mode, status='ACTIVE'))
        state = json.loads((self.f.ticket / 'state.json').read_text())
        atomic_json(self.f.ticket / 'state.json', dict(state, bounded_execution=mode))
        with self.assertRaisesRegex(DevelopmentBlocked, 'limit extension chain'):
            self.f.load()


class DeadlineTests(unittest.TestCase):
    """Criterion 3: an expired deadline is extended and its history is preserved."""
    def setUp(self):
        self.f = NativeFixture(self)

    def test_expired_deadline_extends_and_preserves_history(self):
        envelope, counters = self.f.load()
        self.assertEqual((counters['started_at'], counters['deadline_at']), (100, 1100))
        self.f.now = 1500
        with self.assertRaisesRegex(RecoveryStop, 'wall-time'):
            self.f.store.reserve('03-dev', '03-dev', 'codex')
        sha = self.f.propose(self.f.request(operation='extend_deadline',
                                            changes=[{'dimension': 'deadline_at', 'new': 3100}]))
        proposal = json.loads((self.f.ticket / 'limit-extensions' / sha / 'proposal.json').read_text())
        self.assertEqual(proposal['deadline'], dict(started_at=100, old_deadline_at=1100, new_deadline_at=3100,
                                                    old_wall_seconds=1000, new_wall_seconds=3000,
                                                    expired_at_proposal=True))
        self.f.approve(sha)
        self.f.activate(sha)
        extended, after = self.f.load()
        self.assertEqual((after['started_at'], after['deadline_at']), (100, 3100))
        self.assertEqual(after['invocations'], counters['invocations'])
        self.assertEqual(extended['policy']['wall_seconds'], 3000)
        self.assertEqual({k: v for k, v in extended['policy'].items() if k != 'wall_seconds'},
                         {k: v for k, v in envelope['policy'].items() if k != 'wall_seconds'})
        mirrored = extended['binding']['deadline_extensions'][0]
        self.assertEqual((mirrored['capability'], mirrored['old_deadline_at'], mirrored['new_deadline_at']),
                         (LIMIT_CAPABILITY, 1100, 3100))
        # The journal keeps the original authorization and its deadline.
        journal = [json.loads(line) for line in (self.f.ticket / 'limits/task-journal.jsonl').read_text().splitlines()]
        self.assertEqual(journal[0]['state']['deadline_at'], 1100)
        self.assertEqual(journal[-1]['event'], 'LIMITS_EXTENDED')
        self.assertEqual(unwind(extended, None), envelope)
        self.f.store.reserve('03-dev', '03-dev', 'codex')
        # The historical anchor accepts the mirrored entry and its predecessor capability.
        anchored = dict(extended, binding=dict(extended['binding'], accounting_anchor=dict(
            started_at=100, deadline_at=1100, combined_reported_usd_cap=None, diagnoses=[])))
        self.assertEqual(accounting_anchor(anchored)['deadline_at'], 3100)

    def test_specialized_deadline_receipts_stay_valid_and_unknown_capabilities_fail(self):
        envelope = {'policy': {'wall_seconds': 1500}, 'binding': {
            'accounting_anchor': {'started_at': 0, 'deadline_at': 1000},
            'deadline_extensions': [dict(capability=REPAIR_DEADLINE, started_at=0, old_deadline_at=1000,
                                         new_deadline_at=1500, old_wall_seconds=1000, new_wall_seconds=1500,
                                         source_policy_sha256='c' * 64)]}}
        self.assertEqual(accounting_anchor(envelope)['deadline_at'], 1500)
        envelope['binding']['deadline_extensions'][0]['capability'] = 'unknown-v1'
        with self.assertRaises(TaskError):
            accounting_anchor(envelope)


class FaultInjectionTests(unittest.TestCase):
    """Criterion 4: interruptions around proposal, authorization and activation."""
    def test_proposal_interruptions(self):
        f = NativeFixture(self)
        def fail(point):
            if point == 'proposal_built':
                raise Fault(point)
        with self.assertRaises(Fault):
            f.propose(fault=fail)
        self.assertFalse((f.ticket / 'limit-extensions').exists())
        def late(point):
            if point == 'proposal_written':
                raise Fault(point)
        with self.assertRaises(Fault):
            f.propose(fault=late)
        sha = f.propose()  # Same request, same clock: the same stored proposal.
        self.assertEqual(len(list((f.ticket / 'limit-extensions').iterdir())), 1)
        self.assertTrue((f.ticket / 'limit-extensions' / sha / 'proposal.json').is_file())

    def test_authorization_interruptions(self):
        f = NativeFixture(self)
        sha = f.propose()
        def early(point):
            if point == 'authorization_built':
                raise Fault(point)
        with self.assertRaises(Fault):
            f.approve(sha, fault=early)
        with self.assertRaisesRegex(LimitExtensionError, 'exact operator authorization'):
            f.activate(sha)
        def late(point):
            if point == 'authorization_written':
                raise Fault(point)
        with self.assertRaises(Fault):
            f.approve(sha, expires=200, fault=late)
        f.now += 1
        f.approve(sha, expires=200)  # The identical decision is confirmed, not duplicated.
        self.assertEqual(json.loads((f.ticket / 'limit-extensions' / sha / 'authorization.json').read_text())
                         ['approved_at'], 100)
        f.activate(sha)
        self.assertEqual(f.load()[0]['policy']['metered_usd'], 20)

    def test_interruption_before_intent_rechecks_expiry(self):
        f = NativeFixture(self)
        sha = f.propose()
        f.approve(sha, expires=f.now + 5)
        before = f.snapshot()
        def fail(point):
            if point == 'before_intent':
                raise Fault(point)
        with self.assertRaises(Fault):
            f.activate(sha, fault=fail)
        self.assertEqual(f.snapshot(), before)
        f.load()  # Nothing was published: the limits stay usable.
        f.now += 5
        with self.assertRaisesRegex(LimitExtensionError, 'expired'):
            f.activate(sha)

    def test_interruptions_after_the_intent_complete_exactly_once(self):
        for point in POINTS:
            with self.subTest(point=point):
                f = NativeFixture(self)
                sha = f.propose(f.request(changes=[{'dimension': 'metered_usd', 'new': 20},
                                                   {'dimension': 'development_attempts', 'new': 3}]))
                f.approve(sha, expires=f.now + 5)
                def fail(name, point=point):
                    if name == point:
                        raise Fault(name)
                with self.assertRaises(Fault):
                    f.activate(sha, fault=fail)
                if point != 'pending_cleared':
                    with self.assertRaisesRegex(DevelopmentBlocked, 'pending|changed|differs|integrity'):
                        f.load()
                    with self.assertRaisesRegex(LimitExtensionError, 'pending'):
                        f.propose(f.request(changes=[{'dimension': 'invocations', 'new': 12}]))
                    self.assertEqual(status(f.ticket, clock=f.clock)['pending'], sha)
                f.now += 100  # Expired after publication: the replay only completes it.
                f.activate(sha)
                f.activate(sha)
                envelope, _ = f.load()
                self.assertEqual(envelope['policy']['metered_usd'], 20)
                self.assertEqual(envelope['policy']['development_attempts'], 3)
                self.assertEqual(len(envelope['binding']['limit_extensions']), 1)
                self.assertEqual(f.journal_events().count('LIMITS_EXTENDED'), 1)
                self.assertEqual(f.task_events().count('ATTEMPT_LIMIT_EXTENDED'), 1)
                self.assertFalse((f.ticket / 'limits/extension-pending.json').exists())

    def test_interrupted_journal_append_is_recovered_by_replay(self):
        f = NativeFixture(self)
        sha = f.propose()
        f.approve(sha)
        def fail(point):
            if point == 'policy_written':
                raise Fault(point)
        with self.assertRaises(Fault):
            f.activate(sha, fault=fail)
        with (f.ticket / 'limits/task-journal.jsonl').open('ab') as stream:
            stream.write(b'{"partial":')  # A torn append of the extension event.
        f.activate(sha)
        self.assertEqual(f.journal_events().count('LIMITS_EXTENDED'), 1)
        self.assertEqual(f.load()[0]['policy']['metered_usd'], 20)

    def test_another_pending_activation_fences_a_second_proposal(self):
        f = NativeFixture(self)
        first = f.propose()
        second = f.propose(f.request(changes=[{'dimension': 'invocations', 'new': 12}]))
        f.approve(first)
        f.approve(second)
        def fail(point):
            if point == 'intent_published':
                raise Fault(point)
        with self.assertRaises(Fault):
            f.activate(first, fault=fail)
        with self.assertRaisesRegex(LimitExtensionError, 'Another limit extension activation is pending'):
            f.activate(second)
        f.activate(first)
        # The second proposal was bound to the old parent: it is refused, not stacked.
        with self.assertRaisesRegex(LimitExtensionError, 'changed since the proposal'):
            f.activate(second)


class CommandLineTests(unittest.TestCase):
    def test_limits_commands_follow_the_whole_flow(self):
        import contextlib
        import io
        import time
        f = NativeFixture(self)
        def run(*words):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(['limits', *words, '--repo', str(f.repo), '--ticket', 'T1'])
            return code, json.loads(out.getvalue() or err.getvalue())
        code, inspected = run('inspect')
        self.assertEqual((code, inspected['eligible'], inspected['source']), (0, True, 'native'))
        request = f.repo / 'request.json'
        request.write_text(json.dumps(f.request()))
        code, proposed = run('propose', '--request', str(request))
        self.assertEqual(code, 0)
        sha = proposed['proposal_sha256']
        code, refused = run('approve', '--proposal', sha, '--expires-at', str(time.time() + 600), '--actor', 'op')
        self.assertEqual((code, refused['status']), (2, 'LIMITS_REFUSED'))
        code, _ = run('approve', '--proposal', sha, '--expires-at', str(int(time.time()) + 600),
                      '--actor', 'op', '--source-fenced')
        self.assertEqual(code, 0)
        from unittest.mock import patch
        with patch('ssdlc_preparations.docker_quiescent', return_value=True):
            code, activated = run('activate', '--proposal', sha)
        self.assertEqual((code, activated['status']), (0, 'ACTIVE'))
        code, listed = run('status')
        self.assertEqual(listed['proposals'][0]['state'], 'ACTIVE')
        self.assertEqual(len(listed['chain']), 1)
        # A writer that still mounts the run refuses the first activation.
        sha2 = f.propose(f.request(changes=[{'dimension': 'invocations', 'new': 12}]))
        code, _ = run('approve', '--proposal', sha2, '--expires-at', str(int(time.time()) + 600),
                      '--actor', 'op', '--source-fenced')
        with patch('ssdlc_preparations.docker_quiescent', return_value=False):
            code, refused = run('activate', '--proposal', sha2)
        self.assertEqual((code, refused['status']), (2, 'LIMITS_REFUSED'))
        self.assertIn('fence every writer', refused['reason'])

    def test_orchestrator_routes_the_limits_command(self):
        import orchestrator
        from unittest.mock import patch
        with patch('ssdlc_limit_extensions.main', return_value=7) as routed:
            self.assertEqual(orchestrator.main(['limits', 'status', '--ticket', 'T1']), 7)
        routed.assert_called_once_with(['limits', 'status', '--ticket', 'T1'])


class ReconciledGenerationTests(unittest.TestCase):
    """A selected I2 generation is extended in place; its selector stays valid."""
    def test_selected_generation_extension_keeps_resolution_and_anchor(self):
        import test_reconciliation as fixtures
        from ssdlc_transitions import runtime_root
        f = fixtures.ReconciliationTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.select(f.stage())
        ticket = f.target
        root = runtime_root(ticket)
        outer = json.loads((root / 'state.json').read_text())
        atomic_json(root / 'state.json', dict(outer, status='FAILED'))
        envelope, counters = LimitsStore(root)._load(repair=False)
        source = inspect(ticket)
        self.assertEqual((source['source'], source['generation']), ('reconciled', root.name))
        self.assertTrue(describe(source)['eligible'])
        request = dict(operation='extend_deadline', cause='Expired reconciled deadline.',
                       evidence=[{'description': 'limits inspect'}], actor='operator',
                       changes=[{'dimension': 'deadline_at', 'new': counters['deadline_at'] + 3600}])
        first = propose(ticket, request)['proposal_sha256']
        approve(ticket, first, expires_at=counters['deadline_at'] + 10 ** 6, actor='operator', source_fenced=True)
        activate(ticket, first)
        second = propose(ticket, dict(request, operation='extend_global_limit',
                                      changes=[{'dimension': 'phase_invocations.03-dev', 'new': 9}]))['proposal_sha256']
        approve(ticket, second, expires_at=counters['deadline_at'] + 10 ** 6, actor='operator', source_fenced=True)
        activate(ticket, second)
        self.assertEqual(runtime_root(ticket), root)
        extended, after = LimitsStore(root)._load(repair=False)
        self.assertEqual(after['deadline_at'], counters['deadline_at'] + 3600)
        self.assertEqual(after['invocations'], counters['invocations'])
        self.assertEqual(after['historical'], counters['historical'])
        self.assertEqual(extended['policy']['phase_invocations']['03-dev'], 9)
        self.assertEqual(unwind(extended, root.name), envelope)
        self.assertEqual([e['generation'] for e in extended['binding']['limit_extensions']], [root.name] * 2)
        self.assertEqual(activate(ticket, first)['replayed'], True)


if __name__ == '__main__':
    unittest.main()
