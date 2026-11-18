"""devloop: the budget envelope of each story and the booking of each turn (#365, ADR 0004,
P2-5a)."""

import dataclasses
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import budget as core
from devloop import agents, budget, config, markdown, supervision
from devloop.budget import ROLE_NOT_IN_ENVELOPE, BudgetRefused
from devloop.config import BudgetPolicy, ConfigError, load_config, load_data, parse_config
from devloop.dispatch_actions import Models
from devloop.dispatcher import Dispatcher
from devloop.journal import JOURNAL, TurnJournal
from devloop.ledger import Ledger
from devloop.planning import Action
from test_devloop import CFG, implemented, pr, raw_review
from test_devloop_ledger import LedgerGH, ReportingRunner, TreeWS

DATA = load_data()
REPO = CFG.repo
RULE = {'model': 'unmetered_quota', 'reason': 'the CLI reports no cost: the turn caps bound it'}
ENVELOPE = {
    'enabled': True, 'turns': 10, 'wall_seconds': 86400, 'metered_usd': 1,
    'cli_estimate_usd': 5,
    'role_turns': {'implement': 3, 'fix': 3, 'review': 3, 'sync': 3},
    'phase_turns': {'implementation': 10},
    'usage': {'claude': dict(RULE), 'codex': dict(RULE), 'agy': dict(RULE)}}
START = 1_800_000_000  # 2027-01-15T08:00:00Z


def setUpModule():
    # #396: turning the budget on is refused while a story at its limit has no extension
    # (grant-budget, ADR 0005). The tests of the envelope run as if it existed; those of the
    # refusal turn it off again (``BudgetActivationTests``).
    patcher = mock.patch.object(config, 'BUDGET_EXTENSION', True)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def envelope(**changes):
    """``ENVELOPE`` with ``changes``; a None value removes the key."""
    table = dict(ENVELOPE, **changes)
    return {k: v for k, v in table.items() if v is not None}


def cfg_with(table=None, **keys):
    """The distributed configuration with ``story_identity`` on and ``[budget]`` = ``table``."""
    data = dict(DATA, story_identity=True, **keys)
    if table is not None:
        data['budget'] = table
    return dataclasses.replace(parse_config(data), max_parallel_actions=1)


def report(cost=None):
    """The telemetry of a turn (``agents.telemetry``), with the dollars ``cost``."""
    return dict(dict.fromkeys(agents.TELEMETRY_FIELDS), cost_usd=cost), None


def events(state_dir):
    return [json.loads(line) for line in (Path(state_dir) / JOURNAL).read_text().splitlines()]


class BudgetConfigTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertEqual(load_config().budget, BudgetPolicy())
        self.assertFalse(load_config().budget.enabled)
        self.assertEqual(DATA['budget'], {'enabled': False})
        self.assertEqual(parse_config({k: v for k, v in DATA.items() if k != 'budget'}).budget,
                         BudgetPolicy())

    def test_a_complete_envelope_loads(self):
        policy = cfg_with(envelope()).budget
        self.assertTrue(policy.enabled)
        self.assertEqual((policy.turns, policy.wall_seconds, policy.metered_usd,
                          policy.cli_estimate_usd), (10, 86400, 1.0, 5.0))
        self.assertEqual(dict(policy.role_turns),
                         {'implement': 3, 'fix': 3, 'review': 3, 'sync': 3})
        self.assertEqual(dict(policy.phase_turns), {'implementation': 10})
        self.assertEqual(policy.rule('codex'), RULE)
        self.assertEqual(policy.rule('agy'), RULE)
        self.assertEqual(policy.rule('claude'), RULE)  # required too (#396)

    def test_a_missing_value_with_the_budget_on_refuses_the_configuration(self):
        cases = {key: envelope(**{key: None})
                 for key in ('turns', 'wall_seconds', 'metered_usd', 'cli_estimate_usd')}
        for role in ('implement', 'fix', 'review', 'sync'):
            cases[f'role {role}'] = envelope(role_turns={
                k: v for k, v in ENVELOPE['role_turns'].items() if k != role})
        cases['phase'] = envelope(phase_turns={})
        # codex and agy report no cost; claude only when its turn completes (#396): a rule is
        # required for each.
        for agent in ('codex', 'agy', 'claude'):
            cases[f'rule of {agent}'] = envelope(usage={
                k: v for k, v in ENVELOPE['usage'].items() if k != agent})
        cases['reserve without a dollar kind'] = envelope(usage=dict(
            ENVELOPE['usage'], claude={'model': 'reserve_per_invocation', 'reason': 'r',
                                       'metered_usd': 1}))
        for name, table in cases.items():
            with self.subTest(name), self.assertRaisesRegex(ConfigError, 'missing'):
                cfg_with(table)
        # The envelope is per story: without stories it is refused too.
        with self.assertRaisesRegex(ConfigError, 'story_identity'):
            parse_config(dict(DATA, budget=envelope()))
        # The roles that run only when configured are required only then.
        with self.assertRaisesRegex(ConfigError, "missing 'conclave'"):
            cfg_with(envelope(), conclave=['claude', 'codex', 'agy'])
        cfg_with(envelope(role_turns=dict(ENVELOPE['role_turns'], conclave=2)),
                 conclave=['claude', 'codex', 'agy'])
        with self.assertRaisesRegex(ConfigError, "missing 'advisory'"):
            cfg_with(envelope(), review_policy=dict(DATA['review_policy'],
                                                    advisory_reviewer='agy'))
        # Off, nothing is required: the values are the operator's decision (D2).
        self.assertEqual(cfg_with({'enabled': False}).budget, BudgetPolicy())

    def test_token_caps_are_refused_as_not_supported_yet(self):
        for name, table in (
                ('a cap of the story', envelope(tokens=1000)),
                ('a cap, budget off', {'enabled': False, 'output_tokens': 10}),
                ('a cap per role', envelope(role_turns=dict(ENVELOPE['role_turns'],
                                                            review_tokens=5))),
                ('a cap of an agent', envelope(usage=dict(
                    ENVELOPE['usage'], codex=dict(RULE, input_tokens=100))))):
            with self.subTest(name), self.assertRaisesRegex(ConfigError,
                                                            'token caps are not supported yet'):
                cfg_with(table)

    def test_a_token_cap_in_the_local_overlay_is_refused_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / 'config.local.toml'
            local.write_text('[budget]\nenabled = false\ntokens = 5\n')
            with self.assertRaisesRegex(ConfigError, 'token caps are not supported yet'):
                load_config(None, local)
            local.write_text('story_identity = true\n[budget]\nenabled = true\nturns = 4\n'
                             'wall_seconds = 60\nmetered_usd = 1\ncli_estimate_usd = 1\n'
                             '[budget.role_turns]\nimplement = 1\nfix = 1\nreview = 1\n'
                             'sync = 1\n[budget.phase_turns]\nimplementation = 4\n'
                             '[budget.usage.codex]\nmodel = "unmetered_quota"\nreason = "r"\n'
                             '[budget.usage.agy]\nmodel = "unmetered_quota"\nreason = "r"\n'
                             '[budget.usage.claude]\nmodel = "unmetered_quota"\nreason = "r"\n')
            self.assertEqual(load_config(None, local).budget.turns, 4)

    def test_invalid_values_are_refused(self):
        for name, table in (
                ('zero turns', envelope(turns=0)),
                ('a boolean', envelope(turns=True)),
                ('a string', envelope(wall_seconds='60')),
                ('negative dollars', envelope(cli_estimate_usd=-1)),
                # A cap is exhausted at equality: 0 would refuse every turn.
                ('a dollar cap of 0', envelope(metered_usd=0)),
                ('infinite dollars', envelope(metered_usd=math.inf)),
                ('not a number', envelope(cli_estimate_usd=math.nan)),
                ('a negative role cap', envelope(role_turns=dict(ENVELOPE['role_turns'],
                                                                 fix=-1))),
                ('an unknown role', envelope(role_turns=dict(ENVELOPE['role_turns'],
                                                             preflight=1))),
                ('an unknown phase', envelope(phase_turns={'implementation': 1, 'design': 1})),
                ('an unknown key', envelope(dollars=3)),
                ('enabled not a boolean', envelope(enabled='yes')),
                ('an unknown agent', envelope(usage=dict(ENVELOPE['usage'], gpt=RULE))),
                ('an unknown model', envelope(usage=dict(ENVELOPE['usage'],
                                                         codex=dict(RULE, model='free')))),
                ('no reason', envelope(usage=dict(ENVELOPE['usage'],
                                                  codex=dict(RULE, reason=' ')))),
                ('USD with unmetered_quota', envelope(usage=dict(
                    ENVELOPE['usage'], codex=dict(RULE, cli_estimate_usd=1)))),
                ('a reserve of 0', envelope(usage=dict(ENVELOPE['usage'], claude={
                    'model': 'reserve_per_invocation', 'reason': 'r', 'metered_usd': 0,
                    'cli_estimate_usd': 1}))),
                ('not a table', 'on')):
            with self.subTest(name), self.assertRaises(ConfigError):
                cfg_with(table)

    def test_agy_and_codex_accept_no_reserve_in_usd(self):
        # D2: their CLI reports no cost, so their rule is unmetered_quota with the turn caps,
        # never a reserve in USD that nothing attests. claude may declare one.
        reserve = {'model': 'reserve_per_invocation', 'reason': 'r', 'metered_usd': 1,
                   'cli_estimate_usd': 1}
        for agent in ('codex', 'agy'):
            with self.subTest(agent), self.assertRaisesRegex(ConfigError,
                                                             'only unmetered_quota'):
                cfg_with(envelope(usage=dict(ENVELOPE['usage'], **{agent: reserve})))
        self.assertEqual(cfg_with(envelope(usage=dict(ENVELOPE['usage'], claude=reserve)))
                         .budget.rule('claude')['model'], 'reserve_per_invocation')

    def test_the_escalation_class_is_critical(self):
        self.assertTrue(supervision.critical(budget.BUDGET))
        self.assertIn(budget.BUDGET, supervision.CRITICAL)


class BudgetActivationTests(unittest.TestCase):
    """#396 (2): without an extension of a story at its limit (grant-budget, ADR 0005) the
    budget cannot be turned on: the configuration is refused at startup."""

    def setUp(self):
        patcher = mock.patch.object(config, 'BUDGET_EXTENSION', False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_budget_on_is_refused_at_startup_without_an_extension(self):
        with self.assertRaisesRegex(ConfigError, 'budget.enabled: the budget cannot be turned '
                                    'on yet') as caught:
            cfg_with(envelope())
        self.assertIn('grant-budget, ADR 0005', str(caught.exception))
        self.assertIn('enabled = false', str(caught.exception))
        # From the local overlay too, as devloop loads it at startup.
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / 'config.local.toml'
            local.write_text('story_identity = true\n[budget]\nenabled = true\n')
            with self.assertRaisesRegex(ConfigError, 'cannot be turned on yet'):
                load_config(None, local)
        # Off, the configuration loads as before, values written or not.
        self.assertFalse(cfg_with(envelope(enabled=False)).budget.enabled)
        self.assertFalse(load_config().budget.enabled)

    def test_the_constant_is_off_in_the_distributed_code(self):
        # Only the tests of the envelope turn it on (``setUpModule``).
        source = Path(config.__file__).read_text()
        self.assertIn('\nBUDGET_EXTENSION = False\n', source)


class EnvelopeTests(unittest.TestCase):
    """The booking on the journal, with ``core.budget``."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.now = START
        self.journal = self.open()
        self.story = self.journal.story(REPO, 7, 'h1')['story']

    def open(self):
        """The journal of ``state``, as a new process reads it."""
        return TurnJournal(self.state, clock=lambda: self.now)

    def book(self, cfg, kind='review', agent='codex', issue=7, number=None, journal=None,
             attempt=None):
        """A turn booked through ``TurnJournal.intent``; its id."""
        journal = journal or self.journal
        candidate = cfg.agent(agent)
        return journal.intent(
            number or issue, issue=issue, repo=REPO, kind=kind, agent=agent,
            model=candidate.model, label='l', attempt=attempt,
            reserve=lambda j: budget.reserve(cfg.budget, j, repo=REPO, issue=issue, kind=kind,
                                             agent=candidate))

    def booked(self, turn, journal=None):
        return (journal or self.journal).turns()[turn]['budget']

    def test_unmetered_quota_with_the_turn_caps_for_agy_and_codex_without_usd(self):
        # No dollar: the turns of agy and codex are bounded by the turn caps alone, with the
        # smallest dollar caps, which they never touch.
        cfg = cfg_with(envelope(metered_usd=0.01, cli_estimate_usd=0.01,
                                role_turns=dict(ENVELOPE['role_turns'], review=3)))
        turns = []
        for agent in ('codex', 'agy', 'codex'):
            with self.subTest(agent=agent):
                turn = self.book(cfg, agent=agent)
                self.journal.outcome(turn, 'returned', usage=budget.observed(report()[0]))
                booked = dict(self.booked(turn))
                self.assertEqual(booked.pop('envelope')['turns'], 10)
                self.assertEqual(booked, {
                    'story': self.story, 'role': 'review', 'phase': 'implementation',
                    'engine': cfg.agent(agent).kind, 'usage_model': 'unmetered_quota'})
                turns.append(turn)
        # No reserve in USD, invented or derived: they charge no dollar.
        current = budget.state(self.journal.stories()[self.story], self.journal.turns(), set(),
                               START, cfg.budget.wall_seconds)
        for kind in core.COST_KINDS:
            self.assertEqual(sum(core.kind_charge(r, kind)
                                 for r in current['invocations'].values()), 0)
        self.assertTrue(all('conservative_usage' not in self.booked(t) for t in turns))
        # The fourth review is past its cap: refused before its intent, for either agent.
        for agent in ('agy', 'codex'):
            with self.subTest(refused=agent):
                with self.assertRaises(BudgetRefused) as caught:
                    self.book(cfg, agent=agent)
                self.assertEqual(caught.exception.category, 'LIMIT_REACHED')
                self.assertIn('turni `review` 3 di 3', str(caught.exception))
        self.assertEqual(len(self.journal.turns()), 3)  # nothing written for the refusals
        # The total cap counts too: another role is refused once the story has used it all.
        small = cfg_with(envelope(turns=3, metered_usd=0.01, cli_estimate_usd=0.01))
        self.journal.story(REPO, 8, 'h2')
        for agent in ('codex', 'agy', 'codex'):
            self.journal.outcome(self.book(small, agent=agent, issue=8), 'returned',
                                 usage=budget.observed(report()[0]))
        with self.assertRaisesRegex(BudgetRefused, 'turni 3 di 3'):
            self.book(small, kind='fix', agent='agy', issue=8)

    def test_a_reserve_is_booked_and_telemetry_only_raises_it(self):
        # Only claude may declare a reserve: agy and codex accept unmetered_quota alone (D2).
        reserve = {'model': 'reserve_per_invocation', 'reason': 'r', 'metered_usd': 1,
                   'cli_estimate_usd': 2}
        cfg = cfg_with(envelope(cli_estimate_usd=5, metered_usd=10,
                                usage=dict(ENVELOPE['usage'], claude=reserve)))
        first = self.book(cfg, agent='claude')
        self.assertEqual(self.booked(first)['conservative_usage'],
                         {'metered_usd': 1.0, 'cli_estimate_usd': 2.0})
        # A lower observation does not lower the charge; a higher one raises it (D7).
        self.journal.outcome(first, 'returned', usage={'metered_usd': None,
                                                       'cli_estimate_usd': 0.5})
        second = self.book(cfg, agent='claude')
        self.journal.outcome(second, 'returned', usage={'metered_usd': None,
                                                        'cli_estimate_usd': 2.5})
        current = budget.state(self.journal.stories()[self.story], self.journal.turns(), set(),
                               START, cfg.budget.wall_seconds)
        self.assertEqual(sum(core.kind_charge(r, 'cli_estimate_usd')
                             for r in current['invocations'].values()), 4.5)
        # A third reserve of 2 would pass the cap of 5: refused before its intent.
        with self.assertRaisesRegex(BudgetRefused, 'cli_estimate_usd cap'):
            self.book(cfg, agent='claude')

    def test_claude_with_its_rule_counts_its_estimate(self):
        cfg = cfg_with(envelope(cli_estimate_usd=2))
        turn = self.book(cfg, kind='implement', agent='claude')
        self.assertEqual(self.booked(turn)['usage_model'], 'unmetered_quota')
        self.journal.outcome(turn, 'returned', usage=budget.observed(report(1.5)[0]))
        self.assertEqual(self.journal.turns()[turn]['usage'],
                         {'metered_usd': None, 'cli_estimate_usd': 1.5})
        turn = self.book(cfg, kind='implement', agent='claude')
        self.journal.outcome(turn, 'returned', usage=budget.observed(report(0.6)[0]))
        with self.assertRaisesRegex(BudgetRefused, 'cli_estimate_usd cap exhausted'):
            self.book(cfg, kind='review')

    def test_a_claude_turn_without_its_cost_does_not_block_the_story(self):
        # #396 (1): a claude turn that ends without total_cost_usd (a model not available, a
        # timeout, a crash, no output) is covered by the rule claude must now declare: the
        # next turns of the story, the fallback in the same step included, still start.
        reserve = {'model': 'reserve_per_invocation', 'reason': 'r', 'metered_usd': 1,
                   'cli_estimate_usd': 1}
        for name, rule in (('unmetered_quota', RULE), ('reserve_per_invocation', reserve)):
            with self.subTest(name):
                self.tmp.cleanup()
                self.state.mkdir()
                self.journal = self.open()
                story = self.journal.story(REPO, 7, 'h1')['story']
                cfg = cfg_with(envelope(metered_usd=10, cli_estimate_usd=10,
                                        role_turns=dict(ENVELOPE['role_turns'], implement=5),
                                        usage=dict(ENVELOPE['usage'], claude=rule)))
                for outcome in ('failed', 'returned', 'failed'):
                    turn = self.book(cfg, kind='implement', agent='claude')
                    self.journal.outcome(turn, outcome, usage=budget.observed(report()[0]))
                    self.assertEqual(self.booked(turn)['usage_model'], name)
                # Nothing invented: the reserve declared by the operator, or no dollar at all.
                current = budget.state(self.journal.stories()[story],
                                       self.journal.turns(), set(), START, 86400)
                self.assertEqual(sum(core.kind_charge(r, 'cli_estimate_usd')
                                     for r in current['invocations'].values()),
                                 3.0 if name == 'reserve_per_invocation' else 0)
                self.book(cfg, kind='implement', agent='claude')
        # Without the rule (a configuration the startup now refuses, or a booking from before)
        # the engine's rule still holds: unknown is not 0, and the story is blocked.
        self.tmp.cleanup()
        self.state.mkdir()
        self.journal = self.open()
        self.journal.story(REPO, 7, 'h1')
        bare = dataclasses.replace(cfg.budget, usage=tuple(
            (agent, rule) for agent, rule in cfg.budget.usage if agent != 'claude'))
        cfg = dataclasses.replace(cfg, budget=bare)
        turn = self.book(cfg, kind='implement', agent='claude')
        self.journal.outcome(turn, 'failed', usage=budget.observed(report()[0]))
        with self.assertRaisesRegex(BudgetRefused, 'unknown is not counted as zero'):
            self.book(cfg, kind='implement', agent='claude')
        with self.assertRaisesRegex(ConfigError, r'budget\.usage\.claude: missing'):
            cfg_with(envelope(usage={k: v for k, v in ENVELOPE['usage'].items()
                                     if k != 'claude'}))

    def test_consumption_accumulates_across_attempts_children_restarts_and_outcomes(self):
        cfg = cfg_with(envelope(turns=6, role_turns=dict(ENVELOPE['role_turns'], review=6)))
        # Attempt 1: one turn, and its PR closed.
        first = self.journal.open_attempt(self.story, 7)
        turn = self.book(cfg, attempt=first['attempt'])
        self.journal.outcome(turn, 'failed')
        self.journal.bind_pr(first['attempt'], 100)
        self.journal.conclude(first['attempt'], 'closed')
        # Attempt 2: a turn whose record could not be written, never started.
        second = self.journal.open_attempt(self.story, 7)
        self.assertEqual(second['n'], 2)
        turn = self.book(cfg, kind='fix', agent='agy', number=101, attempt=second['attempt'])
        self.journal.outcome(turn, 'not_dispatched')
        # A child issue of the story is charged to the same envelope.
        self.journal.child(self.story, REPO, 8)
        turn = self.book(cfg, kind='sync', agent='agy', issue=8)
        self.journal.outcome(turn, 'returned')
        # A restart: a new process reads the same consumption from the journal.
        restarted = self.open()
        turn = self.book(cfg, journal=restarted)
        restarted.outcome(turn, 'returned')
        # A turn left without outcome by a process that stopped: uncertain, it refuses every
        # new turn of the story until it is reconciled, and it stays counted.
        left = self.book(cfg, journal=restarted)
        again = self.open()
        with self.assertRaises(BudgetRefused) as caught:
            self.book(cfg, journal=again)
        self.assertEqual(caught.exception.category, 'OUTCOME_UNCERTAIN')
        again.uncertain(7)
        again.reconcile(left, 'failed', by='brushtyler', comment=1, note='ended')
        self.book(cfg, journal=again)
        # Six turns booked: whatever attempt, issue, outcome or process, nothing refunded.
        with self.assertRaisesRegex(BudgetRefused, 'turni 6 di 6'):
            self.book(cfg, journal=again)
        stories = {t['budget']['story'] for t in again.turns().values()}
        self.assertEqual(stories, {self.story})

    def test_the_state_of_the_budget_per_dimension(self):
        # #393: consumed, reserved and available per dimension, from the journal.
        cfg = cfg_with(envelope(wall_seconds=3600))
        # A story with no turn has consumed nothing: 0 is attested there.
        text = budget.describe(budget.of_issue(cfg.budget, self.journal, REPO, 7))
        self.assertIn('turni 0 di 10 (consumati 0, prenotati 0, disponibili 10)', text)
        self.assertIn('`metered_usd` consumati 0.00 USD, prenotati 0.00 USD, disponibili '
                      '1.00 USD di 1.00 USD', text)
        claude = self.book(cfg, kind='implement', agent='claude')
        self.journal.outcome(claude, 'returned', usage=budget.observed(report(1.5)[0]))
        codex = self.book(cfg, agent='codex')
        self.journal.outcome(codex, 'returned', usage=budget.observed(report()[0]))
        fix = self.book(cfg, kind='fix', agent='codex')
        self.journal.outcome(fix, 'not_dispatched')
        self.book(cfg, agent='agy')  # no outcome yet: reserved, running or not
        self.now = START + 600
        found = budget.of_issue(cfg.budget, self.open(), REPO, 7)
        self.assertEqual((found['story'], found['issue'], found['at']),
                         (self.story, 7, '2027-01-15T08:10:00Z'))
        self.assertEqual(found['turns'],
                         {'consumed': 3, 'reserved': 1, 'cap': 10, 'available': 6})
        self.assertEqual(found['roles'], {
            'implement': {'consumed': 1, 'reserved': 0, 'cap': 3, 'available': 2},
            'fix': {'consumed': 1, 'reserved': 0, 'cap': 3, 'available': 2},
            'review': {'consumed': 1, 'reserved': 1, 'cap': 3, 'available': 1},
            'sync': {'consumed': 0, 'reserved': 0, 'cap': 3, 'available': 3}})
        self.assertEqual(found['phases'], {
            'implementation': {'consumed': 3, 'reserved': 1, 'cap': 10, 'available': 6}})
        # The estimate claude reported is known; codex and agy report none: a lower bound,
        # and nothing available that can be stated. No CLI reports a measured spend.
        self.assertEqual(found['usd'], {
            'cli_estimate_usd': {'consumed': 1.5, 'unattested': 2, 'uncovered': 2,
                                 'reserved': 0.0, 'cap': 5, 'available': None},
            'metered_usd': {'consumed': None, 'unattested': 3, 'uncovered': 3,
                            'reserved': 0.0, 'cap': 1, 'available': None}})
        self.assertEqual(found['time'], {
            'started_at': '2027-01-15T08:00:00Z', 'deadline_at': '2027-01-15T09:00:00Z',
            'cap': 3600, 'consumed': 600, 'available': 3000})
        text = budget.describe(found)
        self.assertIn('turni 4 di 10 (consumati 3, prenotati 1, disponibili 6)', text)
        self.assertIn('turni `review` 2 di 3 (consumati 1, prenotati 1, disponibili 1)', text)
        self.assertIn('`cli_estimate_usd` consumati almeno 1.50 USD (turni senza valore '
                      'attestato: 2), prenotati 0.00 USD, disponibili non attestato di 5.00 USD',
                      text)
        self.assertIn('`metered_usd` consumati non attestato (turni senza valore attestato: 3), '
                      'prenotati 0.00 USD, disponibili non attestato di 1.00 USD', text)
        self.assertNotIn('consumati 0.00 USD', text)
        self.assertIn('tempo trascorso 600 s di 3600 s, disponibili 3000 s, scadenza '
                      '2027-01-15T09:00:00Z', text)
        # An issue of no story has no envelope.
        self.assertIsNone(budget.of_issue(cfg.budget, self.journal, REPO, 9))

    def test_a_value_not_attested_is_shown_as_such_never_as_0(self):
        # A reserve bounds what the turn did not report: the envelope states what is left, but
        # what was consumed stays not attested.
        reserve = {'model': 'reserve_per_invocation', 'reason': 'r', 'metered_usd': 1,
                   'cli_estimate_usd': 2}
        cfg = cfg_with(envelope(cli_estimate_usd=5, metered_usd=10,
                                usage=dict(ENVELOPE['usage'], claude=reserve)))
        turn = self.book(cfg, agent='claude')
        self.journal.outcome(turn, 'returned', usage={'metered_usd': None,
                                                      'cli_estimate_usd': 0.5})
        found = budget.of_issue(cfg.budget, self.journal, REPO, 7)
        self.assertEqual(found['usd'], {
            'cli_estimate_usd': {'consumed': 0.5, 'unattested': 0, 'uncovered': 0,
                                 'reserved': 1.5, 'cap': 5, 'available': 3.0},
            'metered_usd': {'consumed': None, 'unattested': 1, 'uncovered': 0,
                            'reserved': 1.0, 'cap': 10, 'available': 9.0}})
        text = budget.describe(found)
        self.assertIn('`cli_estimate_usd` consumati 0.50 USD, prenotati 1.50 USD, disponibili '
                      '3.00 USD di 5.00 USD', text)
        self.assertIn('`metered_usd` consumati non attestato (turni senza valore attestato: 1), '
                      'prenotati 1.00 USD, disponibili 9.00 USD di 10.00 USD', text)
        self.assertNotIn('consumati 0.00', text)

    def test_a_turn_running_in_parallel_counts_and_does_not_refuse(self):
        cfg = cfg_with(envelope(role_turns=dict(ENVELOPE['role_turns'], review=2)))
        self.book(cfg, agent='codex')
        self.book(cfg, agent='agy')  # the first one still runs in this process
        with self.assertRaisesRegex(BudgetRefused, 'turni `review` 2 di 2'):
            self.book(cfg, agent='codex')

    def test_the_wall_time_of_the_story(self):
        cfg = cfg_with(envelope(wall_seconds=3600))
        self.book(cfg)
        self.now = START + 3600
        with self.assertRaisesRegex(BudgetRefused, 'wall-time exhausted'):
            self.book(cfg)

    def test_the_wall_time_starts_at_the_first_booking_not_at_the_takeover(self):
        # #396 (3): a story taken over long before the budget was on is not frozen at its first
        # turn with a deadline already past: its time starts at its first booking.
        cfg = cfg_with(envelope(wall_seconds=3600))
        self.now = START + 10 * 86400
        first = self.book(cfg)
        self.journal.outcome(first, 'returned', usage=budget.observed(report()[0]))
        recorded = self.booked(first)['envelope']
        self.assertEqual((recorded['started_at'], recorded['deadline_at']),
                         ('2027-01-25T08:00:00Z', '2027-01-25T09:00:00Z'))
        # Later bookings, across a restart, keep that start.
        self.now = START + 10 * 86400 + 3599
        later = self.book(cfg, journal=self.open())
        self.assertEqual(self.booked(later, self.open())['envelope']['started_at'],
                         '2027-01-25T08:00:00Z')
        self.open().outcome(later, 'returned', usage=budget.observed(report()[0]))
        self.now = START + 10 * 86400 + 3600
        with self.assertRaisesRegex(BudgetRefused, 'wall-time exhausted'):
            self.book(cfg, journal=self.open())
        # A booking recorded before ``started_at`` existed keeps the start of its deadline.
        story = self.journal.stories()[self.story]
        old = {'t': {'budget': {'story': self.story, 'envelope': {
            'wall_seconds': 60, 'deadline_at': '2027-01-15T08:01:00Z'}}}}
        self.assertEqual(budget.started(story, old, START + 999), START)
        self.assertEqual(budget.started(story, {}, START + 999), START + 999)

    def test_an_issue_without_a_story_is_not_covered(self):
        # #396 (3): an issue taken over before story_identity is excluded, with no migration,
        # and the refusal says so.
        cfg = cfg_with(envelope())
        with self.assertRaises(BudgetRefused) as caught:
            self.book(cfg, issue=9)
        self.assertEqual(caught.exception.category, 'SCOPE_REQUIRED')
        self.assertEqual(len(self.journal.turns()), 0)
        self.assertIn(budget.no_story(9), str(caught.exception))
        self.assertIn('presa in carico prima', budget.no_story(9))
        self.assertIn('senza migrazione', budget.no_story(9))
        # ``devloop status`` gives the same reason, for those issues only, with the budget on.
        lines = budget.status_lines(cfg.budget, self.journal, REPO, [9, 7, 9])
        self.assertEqual([line for line in lines if line.startswith('budget #')],
                         [f'budget #9: {budget.no_story(9)}'])
        self.assertEqual(budget.status_lines(BudgetPolicy(), self.journal, REPO, [9]), [])

    def test_a_role_configured_after_the_freeze_is_a_dedicated_refusal(self):
        # #396 (4): the story keeps its frozen envelope, with no default cap for the new role,
        # and the refusal names the role instead of a generic LIMIT_REACHED.
        base = cfg_with(envelope())
        self.journal.outcome(self.book(base), 'returned', usage=budget.observed(report()[0]))
        later = cfg_with(envelope(role_turns=dict(ENVELOPE['role_turns'], advisory=2)),
                         review_policy=dict(DATA['review_policy'], advisory_reviewer='agy'))
        with self.assertRaises(BudgetRefused) as caught:
            self.book(later, kind='advisory', agent='agy')
        self.assertEqual(caught.exception.category, ROLE_NOT_IN_ENVELOPE)
        text = str(caught.exception)
        self.assertIn('il ruolo `advisory` non ha un tetto', text)
        self.assertIn('La configurazione attuale gli dà 2 turni', text)
        self.assertIn('nessun tetto predefinito', text)
        self.assertNotIn('LIMIT_REACHED', text)
        self.assertEqual(len(self.journal.turns()), 1)  # nothing written for the refusal
        # The frozen roles still book, and a new story gets the new role.
        self.book(later, kind='review')
        self.journal.story(REPO, 8, 'h2')
        turn = self.book(later, kind='advisory', agent='agy', issue=8)
        self.assertEqual(self.booked(turn)['envelope']['role_turns']['advisory'], 2)

    def test_the_envelope_of_a_story_is_frozen_across_a_restart_with_a_new_configuration(self):
        first = cfg_with(envelope(turns=1, wall_seconds=60))
        turn = self.book(first)
        self.journal.outcome(turn, 'returned', usage=budget.observed(report()[0]))
        # The booking records the limits of the story, the deadline included.
        recorded = self.booked(turn)['envelope']
        self.assertEqual((recorded['turns'], recorded['wall_seconds'], recorded['deadline_at']),
                         (1, 60, '2027-01-15T08:01:00Z'))
        self.assertEqual(recorded['role_turns'], ENVELOPE['role_turns'])
        # A restart with larger limits: the story keeps its own, the turn cap and the deadline.
        wider = cfg_with(envelope(turns=2, wall_seconds=600))
        self.now = START + 30
        restarted = self.open()
        with self.assertRaises(BudgetRefused) as caught:
            self.book(wider, journal=restarted)
        self.assertIn('turni 1 di 1', str(caught.exception))
        self.assertIn('valgono solo per le storie nuove', str(caught.exception))
        self.now = START + 120
        with self.assertRaisesRegex(BudgetRefused, 'wall-time exhausted'):
            self.book(wider, journal=self.open())
        self.assertEqual(len(self.open().turns()), 1)  # no turn past the original limits
        # A new story gets the configuration in force at its first booking, and keeps it: a
        # smaller one after a restart does not shrink it either.
        journal = self.open()
        journal.story(REPO, 8, 'h2')
        journal.outcome(self.book(wider, issue=8, journal=journal), 'returned',
                        usage=budget.observed(report()[0]))
        journal = self.open()
        smaller = cfg_with(envelope(turns=1, wall_seconds=60))
        journal.outcome(self.book(smaller, issue=8, journal=journal), 'returned',
                        usage=budget.observed(report()[0]))
        with self.assertRaisesRegex(BudgetRefused, 'turni 2 di 2'):
            self.book(smaller, issue=8, journal=journal)


class DispatcherBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def make(self, cfg, *outputs, reports=(), number=7):
        self.gh = LedgerGH()
        self.runner = ReportingRunner(*outputs, reports=reports, gh=self.gh, number=number)
        self.logs = []
        return Dispatcher(cfg, self.gh, TreeWS(), self.runner, log=self.logs.append,
                          ledger=Ledger(self.gh, state_dir=self.state))

    def journal(self):
        return TurnJournal(self.state)

    def escalations(self, number):
        found = [markdown.read_marker(c['body'], 'notice') for c in self.gh.thread.get(number, [])]
        return [n for n in found if n and n.get('event') == 'escalation']

    def shapes(self):
        """The kinds and keys of the journal's events, without their values."""
        return [(e['event'], sorted(e['data'])) for e in events(self.state)]

    def implement(self, d, cfg):
        """Issue 7 taken over (its story) and its implement turn, whose PR is #100; the PR as
        the snapshot sees it."""
        d.ledger.journal.story(REPO, 7, 'h1')
        d._turn(Models(cfg.agent(cfg.implementer)), 'write', 'p', 'implement', self.state,
                'issue-7-implement', where=(7, 'implement'))
        p = pr()
        self.gh.labels[100] = set(p.labels)
        d._issue_of[100] = 7
        return p

    def test_with_the_budget_off_the_behaviour_is_the_one_of_today(self):
        found = []
        off = (cfg_with(), cfg_with({'enabled': False}),
               # Values written but the budget off: nothing read, nothing booked.
               cfg_with(envelope(enabled=False, turns=1)))
        for cfg in off:
            with self.subTest(budget=cfg.budget):
                self.tmp.cleanup()
                self.state.mkdir()
                d = self.make(cfg, implemented(), raw_review(), raw_review(),
                              reports=[report(1.0)], number=100)
                p = self.implement(d, cfg)
                self.assertIsNone(d._run_action(Action('review', 100), {100: p}), self.logs)
                # Past the cap of 1 turn written in the table: it is off.
                d._turn(Models(cfg.agent(cfg.reviewer)), 'review', 'p', 'review', self.state,
                        'pr-100-review', where=(100, 'review', 1))
                found.append(self.shapes())
                (intent, *_) = [e['data'] for e in events(self.state) if e['event'] == 'intent']
                self.assertNotIn('budget', intent)
                self.assertTrue(all('usage' not in e['data'] for e in events(self.state)))
                self.assertFalse([m for m in self.logs if 'booked' in m or 'budget' in m])
                self.assertEqual(len(self.runner.calls), 3)
                self.assertEqual(self.escalations(100), [])
        self.assertEqual(found[0], found[1])
        self.assertEqual(found[0], found[2])
        self.assertEqual([kind for kind, _ in found[0]],
                         ['story', 'intent', 'outcome', 'intent', 'outcome', 'intent',
                          'outcome'])

    def test_a_turn_not_covered_is_refused_before_it_starts_with_an_escalation(self):
        cfg = cfg_with(envelope(turns=1))
        d = self.make(cfg, implemented(), raw_review(), reports=[report(0.4)], number=100)
        p = self.implement(d, cfg)
        (turn,) = self.journal().turns().values()
        self.assertEqual(turn['budget']['role'], 'implement')
        self.assertEqual(turn['usage'], {'metered_usd': None, 'cli_estimate_usd': 0.4})
        self.assertTrue(any('booked in story' in m for m in self.logs))
        # The review would be the second turn of a story with one: refused before its intent.
        error = d._run_action(Action('review', 100), {100: p})
        self.assertIsInstance(error, BudgetRefused)
        self.assertEqual(len(self.runner.calls), 1)  # no CLI started
        self.assertEqual(self.gh.records(100, 'turn'), [])  # no record
        self.assertEqual(list(self.journal().turns()), [turn['turn']])  # no intent
        (escalation,) = self.escalations(100)
        self.assertEqual(escalation['class'], 'budget')
        self.assertNotIn('turns', escalation)
        body = self.gh.thread[100][-1]['body']
        self.assertIn('LIMIT_REACHED', body)
        self.assertIn(turn['budget']['story'], body)
        self.assertIn('turni 1 di 1', body)
        # The state of the budget at the refusal (#393), the dollars not reported as such.
        self.assertIn('Stato del budget della storia al rifiuto: turni 1 di 1 (consumati 1, '
                      'prenotati 0, disponibili 0)', body)
        self.assertIn('`metered_usd` consumati non attestato', body)
        self.assertIn('agent:needs-operator', self.gh.labels[100])

    def test_every_fallback_model_is_booked_and_a_restart_keeps_the_count(self):
        cfg = cfg_with(envelope(role_turns=dict(ENVELOPE['role_turns'], review=2)))
        agy = cfg.agent('agy')
        self.assertTrue(agy.fallback_models)
        unavailable = agents.ModelUnavailable('agy exited with 1', reason='quota exhausted')
        d = self.make(cfg, implemented(), unavailable, raw_review(), reports=[report(0.1)])
        self.implement(d, cfg)
        _, used = d._turn(Models(agy), 'review', 'p', 'review', self.state, 'pr-100-review',
                          where=(100, 'review', 1))
        self.assertEqual(used.fallback_for, agy.model)  # ran on the fallback model
        reviews = [t for t in self.journal().turns().values() if t['kind'] == 'review']
        self.assertEqual([(t['model'], t['outcome'], t['budget']['usage_model'])
                          for t in reviews],
                         [(agy.model, 'failed', 'unmetered_quota'),
                          (agy.fallback_models[0].model, 'returned', 'unmetered_quota')])
        # A new process: the two review turns are still counted, and a third is refused.
        restarted = self.make(cfg, raw_review())
        restarted._issue_of[100] = 7
        with self.assertRaisesRegex(BudgetRefused, 'turni `review` 2 di 2'):
            restarted._do_review(pr())
        self.assertEqual(self.runner.calls, [])

    def test_an_advisory_review_refused_by_the_budget_escalates_once_and_waits(self):
        from devloop.planning import plan
        from test_devloop import FakeRunner
        from test_devloop_advisory import SHA2, ON, AdvisoryWS, OperatorGH, answer
        from helpers.devloop_fakes import SHA1, FakeIdentities
        roles = dict(ENVELOPE['role_turns'], advisory=0)
        policy = cfg_with(envelope(role_turns=roles),
                          review_policy=dict(DATA['review_policy'],
                                             advisory_reviewer=ON.review_policy
                                             .advisory_reviewer)).budget
        cfg = dataclasses.replace(ON, story_identity=True, budget=policy)
        TurnJournal(self.state).story(REPO, 7, 'h1')
        self.gh = OperatorGH()
        self.gh.labels[100] = {'agent:needs-operator'}
        ws, runner = AdvisoryWS(), FakeRunner(answer())
        d = Dispatcher(cfg, self.gh, ws, runner, log=lambda m: None,
                       identities=FakeIdentities(self.gh),
                       ledger=Ledger(self.gh, state_dir=self.state))

        def tick():
            snap = d.snapshot()
            prs = {p.number: p for p in snap.prs}
            errors = [d._run_action(a, prs) for a in plan(snap, cfg) if a.kind == 'advisory']
            return snap, errors

        snap, (error,) = tick()
        self.assertIsInstance(error, BudgetRefused)
        self.assertEqual((runner.calls, ws.copies), ([], []))  # refused before the checkout
        (escalation,) = self.escalations(100)
        self.assertEqual((escalation['class'], escalation['category'], escalation['head']),
                         ('budget', 'LIMIT_REACHED', SHA1))
        self.assertNotIn('turns', escalation)
        self.assertIn(f'head `{SHA1[:12]}` resta sospesa', self.gh.thread[100][-1]['body'])
        # The refusal already escalated: no other action nor escalation, tick after tick.
        for _ in range(3):
            snap, errors = tick()
            self.assertEqual((snap.advisories, errors), ([], []))
        self.assertEqual(d._do_advisory(snap.prs[0]), None)
        self.assertEqual((runner.calls, ws.copies, len(self.escalations(100))), ([], [], 1))
        self.assertEqual(d.advisory_failures.get(100, snap.prs[0].head_sha), None)
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        # #396 (5): the wait looks only at the escalations of the current head. A new head is
        # not held in silence: its refusal escalates once more, with its head, then waits.
        self.gh.head = SHA2
        snap, (error,) = tick()
        self.assertIsInstance(error, BudgetRefused)
        self.assertEqual([e['head'] for e in self.escalations(100)], [SHA1, SHA2])
        for _ in range(2):
            snap, errors = tick()
            self.assertEqual((snap.advisories, errors), ([], []))
        self.assertEqual((runner.calls, len(self.escalations(100))), ([], 2))
        # An escalation of the same class without a head (or of another one) holds nothing.
        def escalation_of(head):
            return next(c for c in self.gh.thread[100]
                        if (markdown.read_marker(c['body'], 'notice') or {}).get('head') == head)
        last = escalation_of(SHA2)
        self.assertTrue(d._advisory_held(100, [last], SHA2))
        headless = dict(last, body=markdown.render_escalation(
            'r', cls='budget', extra={'category': 'LIMIT_REACHED'}))
        for comments in ([headless], [escalation_of(SHA1)]):
            with self.subTest(comments=comments), self.assertRaises(BudgetRefused):
                d._advisory_held(100, comments, SHA2)

    def test_a_conclave_call_refused_by_the_budget_records_nothing_as_started(self):
        from test_devloop_conclave import CCFG, ConclaveWS, HookRunner, ThreadGH
        from test_devloop_conclave import Base as ConclaveBase
        from test_devloop_conclave import proposal
        from helpers.devloop_fakes import FakeIdentities
        roles = dict(ENVELOPE['role_turns'], conclave=0)
        policy = cfg_with(envelope(role_turns=roles), conclave=list(CCFG.conclave)).budget
        cfg = dataclasses.replace(CCFG, story_identity=True, budget=policy)
        TurnJournal(self.state).story(REPO, 7, 'h1')
        self.gh = ThreadGH()
        self.runner = HookRunner(proposal())
        d = Dispatcher(cfg, self.gh, ConclaveWS(), self.runner, log=lambda m: None,
                       identities=FakeIdentities(self.gh),
                       ledger=Ledger(self.gh, state_dir=self.state))
        d._issue_of[100] = 7
        ConclaveBase.block_pr(self)  # PR #100 in agent:needs-operator, its fixer stopped
        target = pr(labels=('agent:needs-operator',))
        self.assertIsNotNone(d._conclave_view(100, self.gh.pr_comments(100), target))
        error = d._run_action(Action('conclave', 100), {100: target})
        self.assertIsInstance(error, BudgetRefused)
        self.assertEqual(self.runner.calls, [])
        # Nothing recorded as started on GitHub, no intent in the journal.
        self.assertEqual(self.gh.conclaves(100), [])
        self.assertEqual(self.journal().turns(), {})
        (escalation,) = self.escalations(100)
        self.assertEqual((escalation['class'], escalation['category']),
                         ('budget', 'LIMIT_REACHED'))
        self.assertIn('turni `conclave` 0 di 0', self.gh.thread[100][-1]['body'])
        self.assertIn('agent:needs-operator', self.gh.labels[100])

    def test_a_role_added_after_the_freeze_escalates_with_its_own_category(self):
        # #396 (4): the envelope of the story was frozen without `advisory`; the advisory
        # reviewer configured afterwards gets a dedicated escalation, not LIMIT_REACHED.
        cfg = cfg_with(envelope())
        d = self.make(cfg, implemented(), reports=[report(0.1)], number=100)
        p = self.implement(d, cfg)
        later = cfg_with(envelope(role_turns=dict(ENVELOPE['role_turns'], advisory=1)),
                         review_policy=dict(DATA['review_policy'], advisory_reviewer='agy'))
        d = self.make(later)
        d._issue_of[100] = 7
        with self.assertRaises(BudgetRefused) as caught:
            d._turn(Models(later.agent('agy')), 'review', 'p', 'advisory', self.state,
                    'pr-100-advisory', where=(100, 'advisory'))
        self.assertEqual(self.runner.calls, [])
        d._escalate_failure(Action('advisory', 100), p, caught.exception)
        (escalation,) = self.escalations(100)
        self.assertEqual((escalation['class'], escalation['category'], escalation['head']),
                         ('budget', ROLE_NOT_IN_ENVELOPE, p.head_sha))
        body = self.gh.thread[100][-1]['body']
        self.assertIn('il ruolo `advisory` non ha un tetto', body)
        self.assertNotIn('LIMIT_REACHED', body)

    def test_status_names_the_issues_without_a_story(self):
        # #396 (3): the issues taken over before story_identity are excluded; status says why.
        from types import SimpleNamespace
        snap = SimpleNamespace(working_issues=[9], prs=[pr(issue=7), pr(number=101, issue=11)])
        cfg = cfg_with(envelope())
        d = self.make(cfg)
        d.ledger.journal.story(REPO, 7, 'h1')
        issues = list(snap.working_issues) + [p.issue for p in snap.prs]
        lines = d.budget_status(issues)
        self.assertIn(f'budget #9: {budget.no_story(9)}', lines)
        self.assertIn(f'budget #11: {budget.no_story(11)}', lines)
        self.assertFalse([line for line in lines if line.startswith('budget #7:')])
        self.assertEqual(self.make(cfg_with()).budget_status(issues), [])  # budget off

    def test_status_shows_the_budget_of_a_story_and_nothing_with_the_budget_off(self):
        import devloop.__main__ as cli
        from unittest import mock
        from devloop.planning import Snapshot
        shown = {}
        for on, cfg in ((False, cfg_with()), (False, cfg_with(envelope(enabled=False))),
                        (True, cfg_with(envelope()))):
            with self.subTest(on=on, budget=cfg.budget):
                self.tmp.cleanup()
                self.state.mkdir()
                d = self.make(cfg, implemented(), reports=[report(0.4)], number=100)
                p = self.implement(d, cfg)
                # Issue 7 in progress and the issue of its PR, issue 9 of no story.
                snap = Snapshot(prs=[p], ready_issues=[], working_issues=[7, 9])
                lines = []
                with mock.patch.object(cli, 'build', return_value=(cfg, self.state, d)), \
                        mock.patch.object(cli, 'log', lines.append), \
                        mock.patch.object(d, 'snapshot', return_value=snap):
                    cli.main(['status'])
                self.assertTrue(any(line.startswith('PR #100 ') for line in lines), lines)
                found = [line for line in lines if line.startswith('budget')]
                shown[on] = found
                if not on:
                    self.assertEqual(found, [])
                    self.assertFalse([line for line in lines if 'attestato' in line])
        (story,) = self.journal().stories()
        (line, none) = shown[True]  # the story once, for the issue and for its PR
        self.assertTrue(line.startswith(f'budget {story} (radice #7, al '), line)
        self.assertIn('turni 1 di 10 (consumati 1, prenotati 0, disponibili 9)', line)
        self.assertIn('turni `implement` 1 di 3 (consumati 1, prenotati 0, disponibili 2)',
                      line)
        self.assertIn('`cli_estimate_usd` consumati 0.40 USD, prenotati 0.00 USD, '
                      'disponibili 4.60 USD di 5.00 USD', line)
        self.assertIn('`metered_usd` consumati non attestato (turni senza valore attestato: 1)',
                      line)
        self.assertIn('tempo trascorso ', line)
        self.assertEqual(none, f'budget #9: {budget.no_story(9)}')

    def test_the_readiness_check_is_outside_the_envelope(self):
        cfg = cfg_with(envelope(turns=1))
        d = self.make(cfg, {'ready': True}, reports=[report(0.2)])
        # No story yet (before the takeover), and no place left would not matter: not booked.
        d._turn(Models(cfg.agent('codex')), 'review', 'p', 'preflight', self.state,
                'issue-9-preflight', where=(9, 'preflight'), note='preflight')
        (turn,) = self.journal().turns().values()
        self.assertNotIn('budget', turn)
        # Its consumption is recorded all the same.
        self.assertEqual(turn['usage'], {'metered_usd': None, 'cli_estimate_usd': 0.2})


if __name__ == '__main__':
    unittest.main()
