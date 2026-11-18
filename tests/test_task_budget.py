"""Per-task budget and reallocation of a bounded run (#163, decision D4 of ADR 0004)."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest

from ssdlc_limit_extensions import (LimitExtensionError, activate, approve, describe, inspect, propose, status,
                                    unwind)
from ssdlc_recovery import LimitsStore, RecoveryStop
import ssdlc_task_budget as tb
from ssdlc_tasks import DevelopmentBlocked, TaskError, TaskJournal, TaskScheduler, atomic_json, digest, \
    validate_document
from test_recovery_limits import policy


CAPS = {'03-dev': 6, '02-design-arch': 5}
PHASES = {'03-dev': 6, '02-design': 5}
RESERVE = {'model': 'reserve_per_invocation', 'metered_usd': 1, 'cli_estimate_usd': 1,
           'reason': 'Offline fixture: a conservative reserve per call'}


def allocation(**tasks):
    return dict(schema_version=1, capability=tb.CAPABILITY, tasks=tasks)


def budget_policy(**tasks):
    p = policy(role_invocations=dict(CAPS), phase_invocations=dict(PHASES))
    p['live_unknown_usage']['agy'] = dict(RESERVE)
    if tasks:
        p['task_budget'] = allocation(**tasks)
    return p


class Fault(Exception):
    pass


class Run:
    """A stopped native run: three tasks, T03 depends on T01, with attributed consumption."""
    def __init__(self, case, profile):
        self.tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self.tmp.cleanup)
        self.ticket = Path(self.tmp.name) / '.ssdlc' / 'T1'
        self.ticket.mkdir(parents=True)
        self.now = 100
        self.clock = lambda: self.now
        atomic_json(self.ticket / 'state.json', {'status': 'IN_PROGRESS', 'ledger': []})
        atomic_json(self.ticket / '02-design/plan.json', {'tasks': [
            {'task_id': 'T01', 'depends_on': []}, {'task_id': 'T02', 'depends_on': []},
            {'task_id': 'T03', 'depends_on': ['T01']}]})
        self.store = LimitsStore(self.ticket, self.clock)
        self.store.authorize(profile, {'ticket': 'T1', 'controller': 'offline'})
        TaskJournal(self.ticket / '03-dev/tasks').append('INITIALIZED', {
            'tasks': {'T01': {'status': 'COMPLETED', 'attempts': 1}, 'T02': {'status': 'PENDING', 'attempts': 0},
                      'T03': {'status': 'PENDING', 'attempts': 0}},
            'attempt_limit': 2, 'bounded_recovery': 1})

    def call(self, task_id, engine='claude', cost=1.0):
        identity = self.store.reserve('03-dev', '03-dev', engine, task_id)
        usage = {'metered_usd': None, 'cli_estimate_usd': cost if engine == 'claude' else None}
        self.store.finish(identity, 'returned', usage)
        return identity

    def stop(self):
        state = json.loads((self.ticket / 'state.json').read_text())
        atomic_json(self.ticket / 'state.json', dict(state, status='FAILED'))

    def request(self, operation='reallocate_task_budget', **fields):
        value = dict(operation=operation, cause='The completed task leaves part of its cap unused.',
                     evidence=[{'description': 'limits inspect output', 'sha256': 'b' * 64}], actor='operator')
        value.update(fields)
        return value

    def transfer(self, amounts, source='T01', target='T02'):
        return self.request(transfer=dict(from_task=source, to_task=target, amounts=amounts))

    def propose(self, request):
        return propose(self.ticket, request, clock=self.clock)['proposal_sha256']

    def approve(self, sha):
        return approve(self.ticket, sha, expires_at=self.now + 50, actor='operator', source_fenced=True,
                       clock=self.clock)

    def activate(self, sha, **kwargs):
        return activate(self.ticket, sha, clock=self.clock, **kwargs)

    def proposal(self, sha):
        return json.loads((self.ticket / 'limit-extensions' / sha / 'proposal.json').read_text())

    def load(self):
        return LimitsStore(self.ticket, self.clock)._load(repair=False)

    def tasks(self):
        return self.load()[0]['policy']['task_budget']['tasks']

    def events(self):
        raw = (self.ticket / 'limits/task-journal.jsonl').read_text()
        return [json.loads(line)['event'] for line in raw.splitlines()]


class AttributionTests(unittest.TestCase):
    """Criterion 1: reservations and consumptions carry the task; shared calls stay on the run."""
    def test_task_and_shared_calls_record_their_account(self):
        run = Run(self, budget_policy())
        task_call = run.store.reserve('03-dev', '03-dev', 'claude', 'T01')
        rows = run.load()[1]['invocations']
        self.assertEqual((rows[task_call]['account'], rows[task_call]['task_id'], rows[task_call]['status']),
                         ('task', 'T01', 'reserved'))
        run.store.finish(task_call, 'returned', {'metered_usd': None, 'cli_estimate_usd': 0.5})
        shared_call = run.store.reserve('02-design-arch', '02-design', 'codex')
        rows = run.load()[1]['invocations']
        self.assertEqual(rows[shared_call]['account'], 'run')
        self.assertNotIn('task_id', rows[shared_call])
        run.store.finish(shared_call, 'returned')
        rows = run.load()[1]['invocations']
        # The consumption keeps the attribution of its reservation.
        self.assertEqual((rows[task_call]['account'], rows[task_call]['task_id']), ('task', 'T01'))
        self.assertEqual(rows[task_call]['usage']['cli_estimate_usd'], 0.5)
        self.assertEqual(rows[shared_call]['account'], 'run')
        accounts = tb.accounts(run.load()[0]['policy'], run.load()[1])
        self.assertEqual(accounts['run']['invocations'], 1)
        self.assertEqual(accounts['unattributed']['invocations'], 0)
        with self.assertRaises(TaskError):
            run.store.reserve('03-dev', '03-dev', 'codex', '../T01')

    def test_rows_recorded_before_attribution_are_unattributed(self):
        self.assertEqual(tb.account({'role': '03-dev', 'status': 'returned'}), 'unattributed')

    def test_scheduler_preflight_checks_the_task_caps(self):
        calls = []
        fake = types.SimpleNamespace(dispatch_engine=lambda task, attempt: 'codex',
                                     dispatch_budget=lambda engine=None: calls.append(('run', engine)),
                                     task_budget=lambda task_id, engine=None: calls.append((task_id, engine)))
        TaskScheduler._dispatch_capacity(fake, {'task_id': 'T02'}, 1)
        self.assertEqual(calls, [('run', 'codex'), ('T02', 'codex')])
        fake.dispatch_engine, calls[:] = None, []
        fake.dispatch_budget = lambda: calls.append(('run', None))
        TaskScheduler._dispatch_capacity(fake, {'task_id': 'T02'}, 1)
        self.assertEqual(calls, [('run', None), ('T02', None)])


class TaskCapTests(unittest.TestCase):
    """Per-task caps declared in a new profile: overrides on top of the run defaults."""
    def test_task_cap_bounds_only_that_task(self):
        run = Run(self, budget_policy(T01={'invocations': 1}, T02={'role_invocations': {'03-dev': 2}}))
        run.call('T01')
        with self.assertRaisesRegex(RecoveryStop, 'T01 exhausted its invocations cap'):
            run.store.reserve('03-dev', '03-dev', 'claude', 'T01')
        with self.assertRaisesRegex(RecoveryStop, 'T01 exhausted'):
            run.store.require_capacity('03-dev', '03-dev', task_id='T01')
        run.call('T02')
        run.call('T02')
        with self.assertRaisesRegex(RecoveryStop, 'role_invocations.03-dev'):
            run.store.reserve('03-dev', '03-dev', 'claude', 'T02')
        # T03 has no override: the run defaults bind it, and shared calls are not task calls.
        run.call('T03')
        run.call(None)

    def test_task_dollar_cap_with_reserve(self):
        run = Run(self, budget_policy(T01={'metered_usd': 1.5}))
        run.call('T01', engine='agy')
        with self.assertRaisesRegex(RecoveryStop, 'metered_usd cap of task T01'):
            run.store.reserve('03-dev', '03-dev', 'agy', 'T01')
        run.call('T02', engine='agy')

    def test_profile_rules(self):
        store = LimitsStore(Path(tempfile.mkdtemp()))
        bad = [dict(T01={'invocations': 6}, T02={'invocations': 5}),
               dict(T01={'role_invocations': {'04-review': 1}}),
               dict(T01={'development_attempts': 1})]
        for tasks in bad:
            with self.subTest(tasks=tasks):
                with self.assertRaises((ValueError, TaskError)):
                    store.authorize(budget_policy(**tasks), {'ticket': 'T1'})
        validate_document('recovery-limits', budget_policy(T01={'invocations': 3, 'metered_usd': 2.5}))


class ReallocationTests(unittest.TestCase):
    """Criterion 2: atomic reallocation within the run caps; consumed and committed amounts stay."""
    def setUp(self):
        self.run = Run(self, budget_policy(T01={'invocations': 4, 'cli_estimate_usd': 4, 'metered_usd': 3},
                                           T02={'invocations': 2, 'cli_estimate_usd': 2, 'metered_usd': 2}))
        self.run.call('T01', cost=1.5)
        self.run.call('T01', engine='agy')  # Keeps a 1 USD reserve committed in both kinds.
        self.run.call(None)
        self.run.stop()

    def test_reallocation_is_applied_atomically(self):
        run = self.run
        before_policy, before_counters = run.load()
        sha = run.propose(run.transfer({'invocations': 2, 'cli_estimate_usd': 1.5}))
        proposal = run.proposal(sha)
        self.assertEqual(proposal['changes'], [
            dict(dimension='task_budget.T01.cli_estimate_usd', old=4, new=2.5),
            dict(dimension='task_budget.T01.invocations', old=4, new=2),
            dict(dimension='task_budget.T02.cli_estimate_usd', old=2, new=3.5),
            dict(dimension='task_budget.T02.invocations', old=2, new=4)])
        run.approve(sha)
        result = run.activate(sha)
        self.assertEqual(result['status'], 'ACTIVE')
        envelope, counters = run.load()
        self.assertEqual(envelope['policy']['task_budget']['tasks']['T01'],
                         {'invocations': 2, 'cli_estimate_usd': 2.5, 'metered_usd': 3})
        self.assertEqual(envelope['policy']['task_budget']['tasks']['T02'],
                         {'invocations': 4, 'cli_estimate_usd': 3.5, 'metered_usd': 2})
        # No new budget: the run caps and the counters are unchanged.
        for key in ('invocations', 'metered_usd', 'cli_estimate_usd', 'role_invocations', 'phase_invocations'):
            self.assertEqual(envelope['policy'][key], before_policy['policy'][key])
        self.assertEqual(counters['invocations'], before_counters['invocations'])
        self.assertEqual(run.events()[-1], 'TASK_BUDGET_REALLOCATED')
        self.assertEqual(envelope['binding']['limit_extensions'][-1]['operation'], 'reallocate_task_budget')
        self.assertEqual(unwind(envelope, None), before_policy)
        with self.assertRaisesRegex(RecoveryStop, 'T01 exhausted its invocations cap'):
            run.store.require_capacity('03-dev', '03-dev', task_id='T01')

    def test_consumed_or_committed_amounts_are_refused(self):
        run = self.run
        # T01: 2 invocations consumed of 4; 1.5 observed + 1 reserve of 4 USD; 1 reserve of 3 metered USD.
        usage = tb.usage(run.load()[0]['policy'], run.load()[1], 'T01')
        self.assertEqual(usage['cli_estimate_usd'], dict(cap=4, consumed=1.5, committed=1, available=1.5))
        self.assertEqual(usage['metered_usd'], dict(cap=3, consumed=0, committed=1, available=2))
        for amounts in ({'invocations': 3}, {'cli_estimate_usd': 2}, {'metered_usd': 2.5}):
            with self.subTest(amounts=amounts):
                with self.assertRaisesRegex(LimitExtensionError, 'consumed or committed'):
                    run.propose(run.transfer(amounts))
        # An open reservation is committed too: it never moves to another task.
        envelope, counters = run.load()
        counters = copy.deepcopy(counters)
        counters['invocations']['open'] = dict(role='03-dev', phase='03-dev', engine='claude', status='reserved',
                                               usage={'metered_usd': None, 'cli_estimate_usd': None},
                                               account='task', task_id='T01')
        with self.assertRaisesRegex(tb.TaskBudgetError, 'consumed or committed'):
            tb.reallocate(envelope['policy'], counters, dict(from_task='T01', to_task='T02',
                                                               amounts={'invocations': 2}))
        self.assertFalse((run.ticket / 'limit-extensions').exists())

    def test_malformed_transfers_are_refused(self):
        run = self.run
        cases = [(dict(from_task='T01', to_task='T01', amounts={'invocations': 1}), 'two different'),
                 (dict(from_task='T01', to_task='T03', amounts={'invocations': 1}), 'two different'),
                 (dict(from_task='T01', to_task='T02', amounts={'invocations': 0}), 'positive amount'),
                 (dict(from_task='T01', to_task='T02', amounts={'invocations': 1.5}), 'positive amount'),
                 (dict(from_task='T01', to_task='T02', amounts={'role_invocations.03-dev': 1}), 'Both tasks'),
                 (dict(from_task='T01', to_task='T02', amounts={'development_attempts': 1}), 'Not a transferable'),
                 (dict(from_task='T01', to_task='T02'), 'names from_task')]
        for transfer, message in cases:
            with self.subTest(transfer=transfer):
                with self.assertRaisesRegex(LimitExtensionError, message):
                    run.propose(run.request(transfer=transfer))
        with self.assertRaisesRegex(LimitExtensionError, 'host computes the changes'):
            run.propose(run.request(transfer=dict(from_task='T01', to_task='T02', amounts={'invocations': 1}),
                                    changes=[{'dimension': 'task_budget.T02.invocations', 'new': 9}]))
        with self.assertRaisesRegex(LimitExtensionError, 'Only allocate_task_budget'):
            run.propose(run.request(operation='extend_global_limit', allocation={'T01': {'invocations': 1}},
                                    changes=[{'dimension': 'metered_usd', 'new': 20}]))

    def test_edited_proposal_is_refused(self):
        run = self.run
        sha = run.propose(run.transfer({'invocations': 1}))
        proposal = run.proposal(sha)
        proposal['new_profile']['task_budget']['tasks']['T02']['invocations'] = 5
        proposal['changes'][1]['new'] = 5
        forged = digest(proposal)
        (run.ticket / 'limit-extensions' / forged).mkdir()
        atomic_json(run.ticket / 'limit-extensions' / forged / 'proposal.json', proposal)
        run.approve(forged)
        policy_before = (run.ticket / 'limits/policy.json').read_bytes()
        with self.assertRaises(LimitExtensionError):
            run.activate(forged)
        self.assertEqual((run.ticket / 'limits/policy.json').read_bytes(), policy_before)


class ExistingRunTests(unittest.TestCase):
    """Criterion 3: an existing run needs an explicit allocation before any transfer."""
    def setUp(self):
        self.run = Run(self, budget_policy())
        self.prior = self.run.call('T01', cost=2)
        self.run.call(None, cost=1)
        self.run.stop()

    def test_transfers_need_an_explicit_allocation(self):
        run = self.run
        with self.assertRaisesRegex(LimitExtensionError, 'explicit per-task allocation'):
            run.propose(run.transfer({'invocations': 1}))
        self.assertFalse(describe(inspect(run.ticket, clock=run.clock))['task_budget']['transfers_enabled'])
        self.assertFalse((run.ticket / 'limit-extensions').exists())

    def test_allocation_from_the_residual_reattributes_nothing(self):
        run = self.run
        with self.assertRaisesRegex(LimitExtensionError, 'exceed the residual'):
            # 10 invocations, 2 used: the residual is 8.
            run.propose(run.request('allocate_task_budget', allocation={'T01': {'invocations': 5},
                                                                        'T02': {'invocations': 4}}))
        with self.assertRaisesRegex(LimitExtensionError, 'tasks of the plan'):
            run.propose(run.request('allocate_task_budget', allocation={'T09': {'invocations': 1}}))
        sha = run.propose(run.request('allocate_task_budget', allocation={
            'T01': {'invocations': 1, 'cli_estimate_usd': 2}, 'T02': {'invocations': 4, 'cli_estimate_usd': 3}}))
        facts = run.proposal(sha)['task_budget']
        self.assertFalse(facts['reattributed'])
        self.assertEqual(facts['prior_by_task'], {'T01': 1})
        self.assertEqual(facts['prior_accounts']['run']['invocations'], 1)
        self.assertEqual(facts['residual']['invocations'], dict(run_cap=10, used=2, residual=8, allocated=5))
        self.assertEqual(facts['residual']['cli_estimate_usd']['residual'], 7)
        run.approve(sha)
        run.activate(sha)
        envelope, _ = run.load()
        self.assertEqual(envelope['policy']['task_budget']['prior_invocations'], sorted(run.load()[1]['invocations']))
        self.assertEqual(run.events()[-1], 'TASK_BUDGET_ALLOCATED')
        # The call T01 made before the allocation is not charged to its new cap.
        usage = tb.usage(envelope['policy'], run.load()[1], 'T01')
        self.assertEqual(usage['invocations'], dict(cap=1, consumed=0, committed=0, available=1))
        with self.assertRaisesRegex(LimitExtensionError, 'already has a task allocation'):
            run.propose(run.request('allocate_task_budget', allocation={'T03': {'invocations': 1}}))
        # Now transfers are enabled.
        sha = run.propose(run.transfer({'invocations': 1}, source='T02', target='T01'))
        run.approve(sha)
        run.activate(sha)
        self.assertEqual(run.tasks()['T01']['invocations'], 2)
        # The chain undoes both operations exactly.
        self.assertNotIn('task_budget', unwind(run.load()[0], None)['policy'])


class EffectsTests(unittest.TestCase):
    """Criterion 4: overrides stay distinct from the run defaults; effects appear in proposal and status."""
    def test_effects_in_proposal_and_status(self):
        run = Run(self, budget_policy(T01={'invocations': 3}, T02={'invocations': 1}))
        run.call('T01')
        run.stop()
        sha = run.propose(run.transfer({'invocations': 2}))
        facts = run.proposal(sha)['task_budget']
        self.assertEqual(facts['defaults']['invocations'], 10)
        self.assertEqual(facts['defaults']['role_invocations.03-dev'], 6)
        effects = facts['effects']
        self.assertEqual(effects['T01'], dict(status='COMPLETED', changed=True, depends_on_changed=False,
                                              override_before={'invocations': 3}, override_after={'invocations': 1},
                                              available_before={'invocations': 2},
                                              available_after={'invocations': 0}))
        self.assertEqual(effects['T02']['available_after'], {'invocations': 3})
        # T03 has no override (run defaults only) but depends on a changed task: it is listed.
        self.assertEqual(effects['T03'], dict(status='PENDING', changed=False, depends_on_changed=True,
                                              override_before=None, override_after=None,
                                              available_before={}, available_after={}))
        listed = status(run.ticket, clock=run.clock)
        self.assertEqual(listed['proposals'][0]['task_budget_effects'], effects)
        view = listed['task_budget']
        self.assertEqual(view['defaults']['invocations'], 10)
        self.assertEqual(view['tasks']['T01']['override'], {'invocations': 3})
        self.assertIsNone(view['tasks']['T03']['override'])
        self.assertEqual(view['tasks']['T03']['depends_on'], ['T01'])
        run.approve(sha)
        run.activate(sha)
        view = status(run.ticket, clock=run.clock)['task_budget']
        self.assertEqual(view['tasks']['T02']['override'], {'invocations': 3})
        # The run defaults never absorb an override.
        self.assertEqual(view['defaults']['invocations'], 10)
        self.assertEqual(run.load()[0]['policy']['invocations'], 10)


class ConcurrencyTests(unittest.TestCase):
    """Criterion 5: no double assignment under concurrent requests; replay is idempotent."""
    def setUp(self):
        self.run = Run(self, budget_policy(T01={'invocations': 4}, T02={'invocations': 1},
                                           T03={'invocations': 1}))
        self.run.call('T01')
        self.run.stop()

    def test_two_proposals_from_the_same_parent_assign_once(self):
        run = self.run
        first = run.propose(run.transfer({'invocations': 3}, target='T02'))
        second = run.propose(run.transfer({'invocations': 3}, target='T03'))
        run.approve(first)
        run.approve(second)
        outcomes = []

        def attempt(sha):
            try:
                outcomes.append(run.activate(sha)['status'])
            except TaskError as exc:
                outcomes.append(type(exc).__name__)

        threads = [threading.Thread(target=attempt, args=(sha,)) for sha in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # Whichever ran second found the lock held or the parent changed.
        for sha in (first, second):
            try:
                run.activate(sha)
            except TaskError:
                pass
        tasks = run.tasks()
        self.assertEqual(tasks['T01']['invocations'], 1)
        self.assertEqual(tasks['T02']['invocations'] + tasks['T03']['invocations'], 5)
        self.assertEqual(run.events().count('TASK_BUDGET_REALLOCATED'), 1)
        self.assertEqual(sum(1 for item in status(run.ticket, clock=run.clock)['proposals']
                             if item['state'] == 'ACTIVE'), 1)

    def test_activation_fences_other_activations_and_reservations(self):
        run = self.run
        first = run.propose(run.transfer({'invocations': 3}, target='T02'))
        second = run.propose(run.transfer({'invocations': 3}, target='T03'))
        run.approve(first)
        run.approve(second)
        seen = []

        def fault(point):
            if point == 'intent_published':
                for action in (lambda: run.activate(second),
                               lambda: run.store.reserve('03-dev', '03-dev', 'claude', 'T01')):
                    with self.assertRaises((TaskError, DevelopmentBlocked)):
                        action()
                    seen.append(point)
                raise Fault()

        with self.assertRaises(Fault):
            run.activate(first, fault=fault)
        self.assertEqual(len(seen), 2)
        with self.assertRaisesRegex(LimitExtensionError, 'Another limit extension activation is pending'):
            run.activate(second)
        self.assertTrue(run.activate(first)['replayed'])
        with self.assertRaisesRegex(LimitExtensionError, 'changed since the proposal'):
            run.activate(second)
        self.assertEqual(run.tasks()['T01']['invocations'], 1)
        self.assertEqual(run.tasks()['T03']['invocations'], 1)

    def test_replay_is_idempotent_at_every_point(self):
        for point in ('intent_published', 'policy_written', 'journal_appended', 'receipt_written'):
            with self.subTest(point=point):
                run = Run(self, budget_policy(T01={'invocations': 4}, T02={'invocations': 1}))
                run.stop()
                sha = run.propose(run.transfer({'invocations': 2}))
                run.approve(sha)

                def fault(name, point=point):
                    if name == point:
                        raise Fault()

                with self.assertRaises(Fault):
                    run.activate(sha, fault=fault)
                run.now += 100  # The authorization expired: a replay only completes published effects.
                self.assertTrue(run.activate(sha)['replayed'])
                self.assertTrue(run.activate(sha)['replayed'])
                self.assertEqual(run.tasks(), {'T01': {'invocations': 2}, 'T02': {'invocations': 3}})
                self.assertEqual(run.events().count('TASK_BUDGET_REALLOCATED'), 1)

    def test_concurrent_reservations_never_exceed_a_task_cap(self):
        run = Run(self, budget_policy(T01={'invocations': 1}))
        outcomes, barrier = [], threading.Barrier(4)

        def attempt():
            barrier.wait()
            try:
                run.store.reserve('03-dev', '03-dev', 'claude', 'T01')
                outcomes.append('reserved')
            except TaskError:
                outcomes.append('refused')

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        rows = [row for row in run.load()[1]['invocations'].values() if row.get('task_id') == 'T01']
        self.assertEqual(len(rows), 1)
        self.assertEqual(outcomes.count('reserved'), 1)


if __name__ == '__main__':
    unittest.main()
