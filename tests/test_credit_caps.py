"""Issue #214 (part B2 of #14): caps in credits and the explicit transition of an existing
profile, with the operator's decision D6 of ADR 0004.

- Only a profile with a credit cap needs the transition: USD profiles stay as they are.
- A credit cap adds to the USD caps. It replaces the USD obligation of a route only after
  the operator-authorized ``substitute_credit_cap`` transition of that profile, which keeps
  the journal and the authorizations. Every provider is a local fake."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import orchestrator
from orchestrator import EngineRunner, _dispatch_context
import ssdlc_telemetry as t
from ssdlc_limit_extensions import LimitExtensionError, activate, approve, propose, status, unwind
from ssdlc_recovery import LimitsStore, RecoveryStop
from ssdlc_tasks import DevelopmentBlocked, TaskError, TaskJournal, atomic_json, digest
from test_recovery_limits import policy
from test_separate_caps import ROOT, environment

CAPS = dict(spend_cap=True, estimate_cap_active=True)


def credit_cap(**overrides):
    cap = {'unit': 'agy_credits', 'cap': 3, 'per_invocation': 1,
           'reason': 'Operator fixture: the agy plan charges AI credits, not USD'}
    cap.update(overrides)
    return cap


def credit_policy(**caps):
    """The #43 fixture profile with a credit cap for agy and no USD rule for its route."""
    p = policy(credit_caps=caps or {'agy': credit_cap()})
    del p['live_unknown_usage']['agy']
    return p


def events(path):
    return [json.loads(line)['event'] for line in Path(path).read_text().splitlines()]


class Ticket:
    """A stopped native ticket with a frozen --limits-profile that has a credit cap."""

    def __init__(self, case, profile=None):
        self.tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self.tmp.cleanup)
        self.ticket = Path(self.tmp.name) / '.ssdlc' / 'T1'
        self.ticket.mkdir(parents=True)
        self.now = 100
        self.clock = lambda: self.now
        atomic_json(self.ticket / 'state.json', {'status': 'FAILED', 'ledger': []})
        self.store = LimitsStore(self.ticket, self.clock)
        self.store.authorize(profile or credit_policy(), {'ticket': 'T1', 'controller': 'offline'})
        known = self.store.reserve('03-dev', '03-dev', 'claude')
        self.store.finish(known, 'returned', {'metered_usd': None, 'cli_estimate_usd': 1.5})
        self.store.finish(self.store.reserve('03-dev', '03-dev', 'codex'), 'returned')

    def journal(self):
        return self.ticket / 'limits/task-journal.jsonl'

    def request(self, **overrides):
        value = dict(operation='substitute_credit_cap',
                     changes=[{'dimension': 'credit_caps.agy.replaces_usd', 'new': True}],
                     cause='The operator accepts the agy credit cap in place of a USD rule for this run.',
                     evidence=[{'description': 'agy plan terms', 'sha256': 'b' * 64}], actor='operator')
        value.update(overrides)
        return value

    def transition(self, request=None):
        sha = propose(self.ticket, request or self.request(), clock=self.clock)['proposal_sha256']
        approve(self.ticket, sha, expires_at=self.now + 50, actor='operator', source_fenced=True, clock=self.clock)
        activate(self.ticket, sha, clock=self.clock)
        return sha


class CreditProfileTests(unittest.TestCase):
    """The profile: explicit, finite credit caps; a substitution is never declared directly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def authorize(self, profile):
        LimitsStore(self.root / str(len(list(self.root.iterdir()))), lambda: 100).authorize(
            profile, {'ticket': 'T1', 'controller': 'offline'})

    def test_new_profile_cannot_declare_the_substitution_without_a_transition(self):
        self.authorize(credit_policy())
        with self.assertRaisesRegex(ValueError, 'only through an operator-authorized substitute_credit_cap'):
            self.authorize(credit_policy(agy=credit_cap(replaces_usd=True)))
        # A chain entry of another operation does not authorize it either.
        store = LimitsStore(self.root / 'chain', lambda: 100)
        with self.assertRaisesRegex(ValueError, 'substitute_credit_cap'):
            store.authorize(credit_policy(agy=credit_cap(replaces_usd=True)), {'ticket': 'T1', 'limit_extensions': [
                {'operation': 'extend_global_limit',
                 'changes': [{'dimension': 'credit_caps.agy.replaces_usd', 'old': False, 'new': True}]}]})

    def test_credit_caps_are_explicit_finite_and_never_in_usd(self):
        for engine, cap in (('claude', credit_cap()), ('openrouter', credit_cap()), ('agy', credit_cap(unit='usd')),
                            ('agy', credit_cap(unit='USD')), ('agy', credit_cap(cap=0)),
                            ('agy', credit_cap(per_invocation=0)), ('agy', credit_cap(reason=' ')),
                            ('agy', credit_cap(cap=float('inf')))):
            with self.subTest(engine=engine, cap=cap), self.assertRaises((TaskError, ValueError)):
                self.authorize(credit_policy(**{engine: cap}))


class CreditAdmissionTests(unittest.TestCase):
    """A USD cap in force without a comparable measure blocks the route; credits add."""

    def test_route_admission(self):
        added = {'agy': credit_cap()}
        substituted = {'agy': credit_cap(replaces_usd=True)}
        # A credit cap alone is not a measure comparable with the USD caps: refused.
        with self.assertRaisesRegex(t.AdmissionRefused, 'charges in credits.*substitute_credit_cap'):
            t.admit('agy', 'agy_cli', credit_caps=added, **CAPS)
        # Only the substitution authorized for the profile admits it, on its subscription route.
        self.assertEqual(t.admit('agy', 'agy_cli', credit_caps=substituted, **CAPS)['measure'], 'credit_cap')
        # A USD rule of its own keeps applying: the credit cap is then an additional bound.
        declared = policy()['live_unknown_usage']
        self.assertEqual(t.admit('agy', 'agy_cli', declarations=declared, credit_caps=added, **CAPS)['measure'],
                         'unmetered_quota')
        # The credits of an engine never cover its OpenRouter route, nor another engine.
        with self.assertRaises(t.AdmissionRefused):
            t.admit('codex', 'openrouter_api', credit_caps={'codex': credit_cap(replaces_usd=True)}, **CAPS)
        with self.assertRaisesRegex(t.AdmissionRefused, 'no unmetered_quota declaration'):
            t.admit('codex', 'codex_cli', credit_caps=substituted, **CAPS)

    def test_store_refuses_before_reserve_without_the_transition(self):
        ticket = Ticket(self)
        policy_before = (ticket.ticket / 'limits/policy.json').read_bytes()
        journal_before = ticket.journal().read_bytes()
        with self.assertRaisesRegex(RecoveryStop, 'charges in credits.*does not replace.*substitute_credit_cap') as caught:
            ticket.store.reserve('03-dev', '03-dev', 'agy')
        self.assertEqual(caught.exception.category, 'LIMIT_REACHED')
        # The profile is not reinterpreted: nothing is written.
        self.assertEqual((ticket.ticket / 'limits/policy.json').read_bytes(), policy_before)
        self.assertEqual(ticket.journal().read_bytes(), journal_before)

    def test_credit_cap_adds_to_a_usd_rule_and_bounds_the_calls(self):
        root = Path(tempfile.mkdtemp()); self.addCleanup(__import__('shutil').rmtree, root)
        store = LimitsStore(root, lambda: 100)
        store.authorize(policy(credit_caps={'agy': credit_cap(cap=2.5)}), {'ticket': 'T1'})
        for _ in range(2):
            store.finish(store.reserve('03-dev', '03-dev', 'agy'), 'returned')
        rows = list(store._load(repair=False)[1]['invocations'].values())
        self.assertEqual([(r['usage_model'], r['credit_reserve']) for r in rows],
                         [('unmetered_quota', {'unit': 'agy_credits', 'amount': 1})] * 2)
        # A third booking of 1 credit would exceed 2.5 credits: refused before launch.
        with self.assertRaisesRegex(RecoveryStop, 'credit cap of agy'):
            store.reserve('03-dev', '03-dev', 'agy')
        # Another engine is not bound by the agy credits.
        store.finish(store.reserve('03-dev', '03-dev', 'codex'), 'returned')

    def test_fractional_credits_are_summed_as_exact_decimals(self):
        def store_with(cap, per_invocation):
            root = Path(tempfile.mkdtemp()); self.addCleanup(__import__('shutil').rmtree, root)
            store = LimitsStore(root, lambda: 100)
            store.authorize(policy(credit_caps={'agy': credit_cap(cap=cap, per_invocation=per_invocation)}),
                            {'ticket': 'T1'})
            return store
        # Three bookings of 0.1 fill a cap of 0.3 exactly (as floats they sum above it).
        store = store_with(0.3, 0.1)
        for _ in range(3):
            store.finish(store.reserve('03-dev', '03-dev', 'agy'), 'returned')
        # The cap is then reached: no further call of any engine is dispatched.
        with self.assertRaisesRegex(RecoveryStop, 'Frozen credit cap exhausted for agy'):
            store.reserve('03-dev', '03-dev', 'codex')
        # Three bookings of 0.3 reach a cap of 0.9 exactly (as floats they sum below it).
        store = store_with(0.9, 0.3)
        for _ in range(3):
            store.finish(store.reserve('03-dev', '03-dev', 'agy'), 'returned')
        with self.assertRaisesRegex(RecoveryStop, 'Frozen credit cap exhausted for agy'):
            store.reserve('03-dev', '03-dev', 'codex')
        with self.assertRaisesRegex(RecoveryStop, 'Frozen credit cap exhausted for agy'):
            store.reserve('03-dev', '03-dev', 'agy')


class TransitionTests(unittest.TestCase):
    """The explicit transition keeps journal and authorizations and leaves a trace."""

    def test_transition_keeps_journal_and_authorizations_and_admits_the_route(self):
        ticket = Ticket(self)
        before_envelope, before_counters = ticket.store._load(repair=False)
        journal_before = ticket.journal().read_bytes()
        with self.assertRaisesRegex(RecoveryStop, 'substitute_credit_cap'):
            ticket.store.reserve('03-dev', '03-dev', 'agy')
        sha = ticket.transition()
        envelope, counters = ticket.store._load(repair=False)
        # The journal is kept: the old history is an exact prefix, the transition one more event.
        journal = ticket.journal().read_bytes()
        self.assertTrue(journal.startswith(journal_before))
        names = events(ticket.journal())
        self.assertEqual(names[0], 'LIMITS_AUTHORIZED')
        self.assertEqual(names[-1], 'CREDIT_CAP_SUBSTITUTED')
        self.assertEqual({k: v for k, v in counters.items() if k != 'policy_sha256'},
                         {k: v for k, v in before_counters.items() if k != 'policy_sha256'})
        # The authorizations are kept: the original envelope is restored by the bound chain.
        self.assertEqual(unwind(envelope, None), before_envelope)
        self.assertEqual(envelope['binding']['ticket'], 'T1')
        (entry,) = envelope['binding']['limit_extensions']
        self.assertEqual((entry['operation'], entry['proposal_sha256'], entry['changes']),
                         ('substitute_credit_cap', sha,
                          [{'dimension': 'credit_caps.agy.replaces_usd', 'old': False, 'new': True}]))
        self.assertEqual(entry['source_policy_sha256'], digest(before_envelope))
        # Only the flag changed in the profile.
        expected = copy.deepcopy(before_envelope['policy'])
        expected['credit_caps']['agy']['replaces_usd'] = True
        self.assertEqual(envelope['policy'], expected)
        # The trace: the authorization and the receipt next to the proposal.
        folder = ticket.ticket / 'limit-extensions' / sha
        self.assertTrue((folder / 'authorization.json').is_file() and (folder / 'receipt.json').is_file())
        self.assertEqual([p['state'] for p in status(ticket.ticket, clock=ticket.clock)['proposals']], ['ACTIVE'])
        # The route is now bounded by its credit cap: 3 credits, 1 booked per call. A returned
        # call without a dollar cost is covered by the cap, not counted as an unknown.
        for _ in range(3):
            ticket.store.finish(ticket.store.reserve('02-design-arch', '02-design', 'agy'), 'returned')
        rows = [r for r in ticket.store._load(repair=False)[1]['invocations'].values() if r['engine'] == 'agy']
        self.assertEqual({r['usage_model'] for r in rows}, {'credit_cap'})
        coverage = ticket.store.coverage_report()['engines']['agy']
        self.assertEqual((coverage['credit_cap'], coverage['dollar_caps_cover']), (3, 'none'))
        with self.assertRaisesRegex(RecoveryStop, 'Frozen credit cap exhausted for agy'):
            ticket.store.reserve('02-design-arch', '02-design', 'agy')
        # The original profile still resumes the run: the transition stays in force.
        LimitsStore(ticket.ticket, ticket.clock).authorize(before_envelope['policy'], before_envelope['binding'])
        self.assertEqual(ticket.store._load(repair=False)[0], envelope)

    def test_transition_refusals(self):
        ticket = Ticket(self, policy(credit_caps={'agy': credit_cap()}))
        refusals = (
            # A USD profile is never migrated: there is no credit cap to substitute.
            ([{'dimension': 'credit_caps.codex.replaces_usd', 'new': True}], 'No credit cap is configured'),
            # agy keeps its own USD rule here: nothing to replace.
            ([{'dimension': 'credit_caps.agy.replaces_usd', 'new': True}], 'already has a USD rule'),
            ([{'dimension': 'credit_caps.agy.replaces_usd', 'new': False}], 'from false to true'),
            ([{'dimension': 'credit_caps.agy.cap', 'new': 9}], 'changes only credit_caps'),
            ([{'dimension': 'metered_usd', 'new': 20}], 'changes only credit_caps'),
        )
        for changes, message in refusals:
            with self.subTest(changes=changes), self.assertRaisesRegex(LimitExtensionError, message):
                propose(ticket.ticket, ticket.request(changes=changes), clock=ticket.clock)
        self.assertFalse((ticket.ticket / 'limit-extensions').exists())

    def test_a_substitution_without_its_bound_chain_entry_is_refused_on_load(self):
        ticket = Ticket(self)
        ticket.transition()
        path = ticket.ticket / 'limits/policy.json'
        envelope = json.loads(path.read_text())
        # Dropping the chain entry while keeping the flag cannot pass as a declared profile.
        del envelope['binding']['limit_extensions']
        atomic_json(path, envelope)
        with self.assertRaises(DevelopmentBlocked):
            ticket.store._load(repair=False)


class OrchestratorAdmissionTests(unittest.TestCase):
    """The dispatch refuses the credit route before reserve and launch, and admits it after
    the transition."""

    def setUp(self):
        self.addCleanup(environment().stop)
        self.calls = []
        for target, kwargs in ((EngineRunner, dict(attribute='_run_role_command', side_effect=self.fake)),
                               (EngineRunner, dict(attribute='_should_spawn_role_container', return_value=False))):
            patcher = patch.object(target, **kwargs)
            patcher.start(); self.addCleanup(patcher.stop)
        self.ticket = Ticket(self)

    def fake(self, cmd, **_):
        self.calls.append(cmd)
        return subprocess.CompletedProcess([], 0, '', '')

    def run_agy(self):
        with patch.object(orchestrator._dispatch_local, 'limits', self.ticket.store, create=True), \
                _dispatch_context(phase='03-dev'):
            return EngineRunner.run_agy('prompt', self.ticket.ticket)

    def agy_rows(self):
        return [r for r in self.ticket.store._load(repair=False)[1]['invocations'].values() if r['engine'] == 'agy']

    def test_credit_route_is_refused_before_launch_and_admitted_after_the_transition(self):
        with self.assertRaisesRegex(RecoveryStop, 'refused before launch') as caught:
            self.run_agy()
        self.assertEqual(caught.exception.category, 'LIMIT_REACHED')
        self.assertIn('substitute_credit_cap', str(caught.exception))
        self.assertEqual((self.calls, self.agy_rows()), ([], []))
        self.ticket.transition()
        self.run_agy()
        self.assertEqual([cmd[0] for cmd in self.calls], ['agy'])
        self.assertEqual([(r['usage_model'], r['status']) for r in self.agy_rows()], [('credit_cap', 'returned')])


class ReplayBaselineTests(unittest.TestCase):
    def test_replay_gate_baseline_is_unchanged(self):
        from ssdlc_grader import verify_baseline
        self.assertTrue(verify_baseline(ROOT))


if __name__ == '__main__':
    unittest.main()
