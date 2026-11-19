"""devloop: the reviewers excluded by the agent in effect, and claude as the third reviewer (#409,
ADR 0005, P5-5).

Every agent and every fallback model may declare its ``provider``, checked against a table of the
prefixes of the model names (D8, option (c)). With ``review_policy.provider_quorum``, off by
default (D10), the provider is required and the reviewers are chosen with the quorum rule (ii)
of D8: at level 1 the only reviewer is of another provider than every author, at level 2 at least
one of the pair is; at both levels a reviewer is independent by tuple of every author, so
``claude-reviewer`` with Sonnet may review the code of Opus beside a reviewer of another provider.
A new round skips a reviewer whose whole chain is remembered unavailable, never one that owns an
open finding, and a round keeps its reviewers until it ends: its promotion recomputes them.
"""

import dataclasses
import tempfile
import unittest
from unittest import mock

from devloop import agents, config, markdown, review, unavailability
from devloop.config import ConfigError
from devloop.dispatcher import Dispatcher
from devloop.policy import ReviewPolicy, agent_profile
from helpers import ssdlc_project
from helpers.devloop_fakes import FakeGH, FakeIdentities
from test_devloop import SHA1, FakeRunner, FakeWS, finding, implemented, pr, raw_review
from test_devloop_implementer_app import IMPL_APP, RoleIdentities, role_meta
from test_devloop_quorum import Quorum

DATA = ssdlc_project.data()
OPUS, SONNET = 'claude-opus-5-5', 'claude-sonnet-5-5'
PROVIDERS = {'claude': 'anthropic', 'codex': 'openai', 'agy': 'google'}
NOW = 1_800_000_000


def declared(kind, **over):
    """The table of ``[agents.<kind>]`` of config.toml with its provider, and its fallback
    models' (agy falls back on Gemini), declared."""
    table = dict(DATA['agents'][kind], **dict({'provider': PROVIDERS[kind]}, **over))
    if 'fallback_models' in table:
        table['fallback_models'] = [dict(f, provider=PROVIDERS[kind])
                                    for f in table['fallback_models']]
    return table


def quorum_data(agents=None, quorum=True, **roles):
    """The keys by role of P5-3 with the third reviewer, claude-reviewer with Sonnet, last in the
    pool, every provider declared and the reviewer rules of P5-5 on."""
    base = {'claude-implementer': declared('claude', app='implementer'),
            'codex-reviewer': declared('codex', app='codex'),
            'agy-reviewer': declared('agy', app='agy'),
            'claude-reviewer': declared('claude', app='claude', model=SONNET,
                                        model_label='Claude Sonnet 5.5')}
    return dict(DATA, agents=dict(base, **(agents or {})),
                roles=dict({'implementer': 'claude-implementer',
                            'reviewers': ['codex-reviewer', 'agy-reviewer', 'claude-reviewer']},
                           **roles),
                review_policy=dict(DATA['review_policy'], provider_quorum=quorum))


def parse(data):
    return dataclasses.replace(config.parse_config(data), max_parallel_actions=1)


QUORUM = parse(quorum_data())
# The same configuration with the rules off: the reviewers are chosen as in P5-3.
OFF = parse(quorum_data(quorum=False))
POOL = ['codex-reviewer', 'agy-reviewer', 'claude-reviewer']


def with_pool(cfg, *pool):
    return dataclasses.replace(cfg, reviewers=pool, reviewer=pool[0])


def with_agent(cfg, key, **over):
    return dataclasses.replace(cfg, agents=dict(cfg.agents, **{
        key: dataclasses.replace(cfg.agent(key), **over)}))


def memory_of(tmp, *keys):
    """A memory of the unavailable models where every model of each of ``keys`` is
    remembered."""
    memory = unavailability.Memory(tmp, clock=lambda: NOW)
    for key in keys:
        for agent in QUORUM.agent(key).chain():
            memory.record(key, agent.model, 'quota', NOW + 3600)
    return memory


class ConfigTests(unittest.TestCase):
    def test_off_by_default(self):
        cfg = ssdlc_project.load()
        self.assertFalse(cfg.review_policy.provider_quorum)
        self.assertEqual({agent.provider for agent in cfg.agents.values()}, {''})
        # Without a declared provider the version is the one before: the providers enter it only
        # when declared, the rule only when on.
        before = dataclasses.replace(cfg.review_policy, reviewers=tuple(
            r[:8] for r in cfg.review_policy.reviewers))
        self.assertEqual(cfg.review_policy.version(), before.version())
        # And the profiles record no provider.
        self.assertNotIn('provider', agent_profile(cfg.agent('claude'), None, 'write'))

    def test_the_declared_provider(self):
        cfg = parse(quorum_data({'agy-reviewer': declared('agy', app='agy', provider='Google')}))
        agy = cfg.agent('agy-reviewer')
        self.assertEqual(agy.provider, 'google')
        self.assertEqual([m.provider for m in agy.chain()], ['google', 'google'])
        self.assertEqual(config.provider_of(agy), 'google')
        self.assertEqual(agent_profile(agy, 'agy-agent[bot]', 'review')['provider'], 'google')
        # A fallback has its own provider, never the agent's: agy may fall back on Claude.
        table = declared('agy', app='agy')
        table['fallback_models'] = [{'model': SONNET, 'model_label': 'Claude Sonnet 5.5',
                                     'provider': 'anthropic'}]
        cfg = parse(quorum_data({'agy-reviewer': table}))
        self.assertEqual([m.provider for m in cfg.agent('agy-reviewer').chain()],
                         ['google', 'anthropic'])
        # A model of an unknown prefix takes the provider it declares.
        table = declared('codex', app='codex', model='o9-mystery', provider='openai')
        self.assertEqual(parse(quorum_data({'codex-reviewer': table})).agent(
            'codex-reviewer').provider, 'openai')

    def test_the_prefixes_check_the_declared_provider(self):
        claude_on_agy = declared('agy', app='agy', model=SONNET)
        fallback = declared('agy', app='agy')
        fallback['fallback_models'] = [{'model': OPUS, 'model_label': 'Claude Opus 5.5',
                                        'provider': 'google'}]
        for agents_, error in (
                ({'agy-reviewer': claude_on_agy},
                 "agents.agy-reviewer.provider: 'claude-sonnet-5-5' is a model of 'anthropic', "
                 "not of 'google'"),
                ({'agy-reviewer': fallback},
                 "agents.agy-reviewer.fallback_models[0].provider: 'claude-opus-5-5' is a model "
                 "of 'anthropic'"),
                ({'codex-reviewer': declared('codex', app='codex', provider=' ')},
                 'agents.codex-reviewer.provider: must not be empty'),
                ({'codex-reviewer': declared('codex', app='codex', provider=1)},
                 'agents.codex-reviewer.provider: expected str')):
            with self.subTest(error=error):
                with self.assertRaises(ConfigError) as ctx:
                    config.parse_config(quorum_data(agents_))
                self.assertIn(error, str(ctx.exception))
        # The check holds with the rules off too: a declared provider is always checked.
        with self.assertRaises(ConfigError):
            config.parse_config(quorum_data({'agy-reviewer': claude_on_agy}, quorum=False))

    def test_the_rules_require_every_provider(self):
        undeclared = dict(declared('codex', app='codex'))
        del undeclared['provider']
        no_fallback = declared('agy', app='agy')
        del no_fallback['fallback_models'][0]['provider']
        for agents_, error in (
                ({'codex-reviewer': undeclared},
                 'agents.codex-reviewer.provider: required with review_policy.provider_quorum'),
                ({'agy-reviewer': no_fallback},
                 'agents.agy-reviewer.fallback_models[0].provider: required with '
                 'review_policy.provider_quorum')):
            with self.subTest(error=error):
                with self.assertRaises(ConfigError) as ctx:
                    config.parse_config(quorum_data(agents_))
                self.assertIn(error, str(ctx.exception))
                # Off, the provider is optional: the prefix of the model's name gives it.
                config.parse_config(quorum_data(agents_, quorum=False))
        with self.assertRaises(ConfigError) as ctx:
            config.parse_config(dict(DATA, review_policy=dict(DATA['review_policy'],
                                                              provider_quorum='yes')))
        self.assertIn('review_policy.provider_quorum: expected bool', str(ctx.exception))

    def test_the_policy_version_covers_the_rules_and_the_providers(self):
        self.assertNotEqual(QUORUM.review_policy.version(), OFF.review_policy.version())
        undeclared = dict(declared('codex', app='codex'))
        del undeclared['provider']
        other = parse(quorum_data({'codex-reviewer': undeclared}, quorum=False))
        self.assertNotEqual(OFF.review_policy.version(), other.review_policy.version())
        changed = parse(quorum_data({'claude-reviewer': declared(
            'claude', app='claude', model='claude-mystery', model_label='m',
            provider='anthropic')}))
        self.assertNotEqual(QUORUM.review_policy.version(), changed.review_policy.version())
        # A declared provider is tagged: it never reads as fallback models or commands.
        policy = ReviewPolicy(reviewers=(('k', None, 'codex', 'm', 'high', (), (), (), ('x',)),))
        self.assertNotEqual(policy.version(), ReviewPolicy(reviewers=(
            ('k', None, 'codex', 'm', 'high', (), (), ('x',)),)).version())

    def test_the_overlay_turns_the_rules_on(self):
        self.assertIn('provider', config.OVERLAY_KEYS['agents']['*'])
        self.assertIn('provider_quorum', config.OVERLAY_KEYS['review_policy'])
        with tempfile.TemporaryDirectory() as tmp:
            local = f'{tmp}/config.local.toml'
            with open(local, 'w') as fh:
                fh.write('[review_policy]\nprovider_quorum = true\n'
                         '[agents.claude]\nprovider = "anthropic"\n'
                         '[agents.codex]\nprovider = "openai"\n'
                         '[agents.agy]\nprovider = "google"\n'
                         'fallback_models = [{ model = "gemini-4-hydrogen-high", '
                         'model_label = "Gemini 4 Hydrogen", provider = "google" }]\n')
            cfg = ssdlc_project.load(local)
        self.assertTrue(cfg.review_policy.provider_quorum)
        self.assertEqual(cfg.agent('codex').provider, 'openai')

    def test_the_third_reviewer_is_last_in_the_pool(self):
        self.assertEqual(QUORUM.pool(), tuple(POOL))
        self.assertEqual(QUORUM.app_of('claude-reviewer'), 'claude')
        self.assertFalse(QUORUM.signs_for_role('claude-reviewer'))
        # Before the migration, claude-agent signs the implementer's work: claude-reviewer
        # cannot sign with it beside the key claude.
        data = quorum_data({'claude': declared('claude')}, implementer='claude')
        with self.assertRaises(ConfigError) as ctx:
            config.parse_config(data)
        self.assertIn('apps.claude: each app belongs to one actor', str(ctx.exception))


def change(cfg=QUORUM, key='claude-implementer', reviewer='codex-reviewer', reviews=(),
           fixes=()):
    made = pr(reviews=reviews, fixes=fixes)
    made.meta = dict(role_meta(cfg, key), reviewer=reviewer)
    return made


class SelectionTests(unittest.TestCase):
    def make(self, cfg=QUORUM, memory=None):
        self.logs = []
        gh = FakeGH()
        return Dispatcher(cfg, gh, FakeWS(), FakeRunner(), log=self.logs.append,
                          identities=RoleIdentities(gh), memory=memory)

    def test_claude_reviews_claudes_code_beside_another_provider(self):
        d = self.make()
        self.assertEqual(d._select(change(), 1), (['codex-reviewer'], ''))
        self.assertEqual(d._select(change(), 2), (['codex-reviewer', 'agy-reviewer'], ''))
        first = self.make(with_pool(QUORUM, 'claude-reviewer', 'codex-reviewer', 'agy-reviewer'))
        # Level 2: claude-reviewer, Sonnet, beside codex, of another provider (rule (ii)).
        self.assertEqual(first._select(change(), 2), (['claude-reviewer', 'codex-reviewer'], ''))
        # Level 1: the only reviewer is of another provider, even when the PR records claude.
        self.assertEqual(first._select(change(reviewer='claude-reviewer'), 1),
                         (['codex-reviewer'], ''))
        # Off, claude-reviewer is the same agent as the implementer, at both levels (P5-3).
        off = self.make(with_pool(OFF, 'claude-reviewer', 'codex-reviewer', 'agy-reviewer'))
        self.assertEqual(off._select(change(OFF), 2), (['codex-reviewer', 'agy-reviewer'], ''))

    def test_no_reviewer_of_another_provider(self):
        alone = self.make(with_pool(QUORUM, 'claude-reviewer'))
        reviewers, problem = alone._select(change(reviewer='claude-reviewer'), 1)
        self.assertEqual(reviewers, [])
        self.assertIn('almeno un reviewer deve essere di un fornitore diverso', problem)
        # Level 2 with agy run on Claude: two reviewers, both of the authors' provider.
        cfg = with_agent(with_pool(QUORUM, 'claude-reviewer', 'agy-reviewer'), 'agy-reviewer',
                         model='claude-haiku-5-5', provider='anthropic', fallback_models=())
        reviewers, problem = self.make(cfg)._select(change(cfg), 2)
        self.assertEqual(reviewers, [])
        self.assertIn('non ne ha abbastanza', problem)

    def test_independence_by_tuple_at_both_levels(self):
        # claude-reviewer with the implementer's very tuple: never, whatever the other one is.
        cfg = with_agent(with_pool(QUORUM, 'claude-reviewer', 'codex-reviewer', 'agy-reviewer'),
                         'claude-reviewer', model=OPUS)
        self.assertEqual(self.make(cfg)._select(change(cfg), 2),
                         (['codex-reviewer', 'agy-reviewer'], ''))
        # Level 1 compares every author, not only the implementer: agy implements, and a fix
        # recorded with codex's tuple leaves claude-reviewer, of a third provider.
        cfg = parse(quorum_data({'agy-implementer': declared('agy', app='implementer')}))
        fix = {'profile': agent_profile(cfg.agent('codex-reviewer'), IMPL_APP, 'write')}
        made = change(cfg, 'agy-implementer', fixes=[fix])
        self.assertEqual(self.make(cfg)._select(made, 1), (['claude-reviewer'], ''))
        # Without the fix codex reviews it, first of the pool: agy-reviewer is agy's tuple.
        self.assertEqual(self.make(cfg)._select(change(cfg, 'agy-implementer'), 1),
                         (['codex-reviewer'], ''))
        self.assertEqual(self.make(cfg)._select(change(cfg, 'agy-implementer'), 2),
                         (['codex-reviewer', 'claude-reviewer'], ''))

    def test_an_unavailable_reviewer_is_skipped_at_a_new_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self.make(memory=memory_of(tmp, 'agy-reviewer'))
            self.assertEqual(d._select(change(), 2, fresh=True),
                             (['codex-reviewer', 'claude-reviewer'], ''))
            self.assertIn('reviewer agy-reviewer skipped: every model of its chain is '
                          'remembered unavailable', '\n'.join(self.logs))
            self.assertEqual(d._select(change(reviewer='agy-reviewer'), 1, fresh=True),
                             (['codex-reviewer'], ''))
            # When every valid pair has it, the first stays, as before: the turn fails.
            pair = self.make(with_pool(QUORUM, 'codex-reviewer', 'agy-reviewer'),
                             memory=memory_of(tmp, 'agy-reviewer'))
            self.assertEqual(pair._select(change(), 2, fresh=True),
                             (['codex-reviewer', 'agy-reviewer'], ''))
            # Off, nothing is skipped.
            off = self.make(OFF, memory=memory_of(tmp, 'agy-reviewer'))
            self.assertEqual(off._select(change(OFF), 2),
                             (['codex-reviewer', 'agy-reviewer'], ''))

    def test_an_agent_without_fallbacks_is_never_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = unavailability.Memory(tmp, clock=lambda: NOW)
            memory.record('codex-reviewer', QUORUM.agent('codex-reviewer').model, 'q',
                          NOW + 3600)
            d = self.make(memory=memory)
            self.assertEqual(d._select(change(), 2, fresh=True),
                             (['codex-reviewer', 'agy-reviewer'], ''))

    def round_of(self, d, reviewers, verdicts, round_no=1):
        """The records of a level 2 round of ``reviewers``, with ``verdicts``."""
        receipt = dict(d._receipt(change()), level=2, reviewers=list(reviewers))
        records = []
        for key, verdict in zip(reviewers, verdicts):
            raw = (raw_review('request_changes', blocking=[finding()]) if verdict == 'changes'
                   else raw_review())
            record = review.normalize_review(raw, previous_open=[], round_no=round_no,
                                             max_rounds=3, head_sha=SHA1, ci='pass',
                                             reviewer=key)
            record['receipt'] = receipt
            records.append(record)
        return records

    def test_a_round_keeps_its_reviewers(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self.make(memory=memory_of(tmp, 'agy-reviewer'))
            # A round that started with agy keeps it: never replaced in the middle of a round.
            started = change(reviews=self.round_of(d, ['codex-reviewer', 'agy-reviewer'],
                                                   ['approve'])[:1])
            self.assertEqual(d._select(started, 2), (['codex-reviewer', 'agy-reviewer'], ''))
            # A round approved with claude-reviewer in agy's place keeps it when agy is back:
            # the promotion recomputes the receipt the round was approved with.
            approved = change(reviews=self.round_of(d, ['codex-reviewer', 'claude-reviewer'],
                                                    ['approve', 'approve']))
            back = self.make()
            self.assertEqual(back._select(approved, 2),
                             (['codex-reviewer', 'claude-reviewer'], ''))
            # A new round chooses again.
            self.assertEqual(back._select(approved, 2, fresh=True),
                             (['codex-reviewer', 'agy-reviewer'], ''))
            # A kept group that no longer meets the rules is chosen again.
            self.assertEqual(self.make(with_pool(QUORUM, 'codex-reviewer', 'agy-reviewer'))
                             ._select(approved, 2), (['codex-reviewer', 'agy-reviewer'], ''))

    def test_the_owner_of_an_open_finding_stays(self):
        d = self.make()
        # claude-reviewer replaced agy and opened a finding: only it may close it.
        replaced = change(reviews=self.round_of(d, ['codex-reviewer', 'claude-reviewer'],
                                                ['approve', 'changes']))
        self.assertEqual(d._select(replaced, 2, fresh=True),
                         (['codex-reviewer', 'claude-reviewer'], ''))
        # An unavailable reviewer with an open finding is not skipped.
        with tempfile.TemporaryDirectory() as tmp:
            out = self.make(memory=memory_of(tmp, 'agy-reviewer'))
            owned = change(reviews=self.round_of(d, ['codex-reviewer', 'agy-reviewer'],
                                                 ['approve', 'changes']))
            self.assertEqual(out._select(owned, 2, fresh=True),
                             (['codex-reviewer', 'agy-reviewer'], ''))

    def test_the_marker_names_a_reviewer_of_another_provider(self):
        gh = FakeGH()
        cfg = with_pool(QUORUM, 'claude-reviewer', 'codex-reviewer', 'agy-reviewer')
        d = Dispatcher(cfg, gh, FakeWS(), FakeRunner(implemented()), log=lambda m: None,
                       identities=RoleIdentities(gh))
        d._do_implement(7)
        meta = markdown.read_marker(gh.created[0]['body'], 'pr')
        self.assertEqual(meta['reviewer'], 'codex-reviewer')
        self.assertEqual(meta['profile']['provider'], 'anthropic')


def unavailable_model(key):
    return agents.ModelUnavailable(f'{key} exited with 1', reason='`quota` in "quota exhausted"')


class FallbackTests(unittest.TestCase):
    def test_level_one_falls_back_only_on_another_provider(self):
        table = declared('agy', app='agy')
        table['fallback_models'] = [{'model': SONNET, 'model_label': 'Claude Sonnet 5.5',
                                     'provider': 'anthropic'}]
        cfg = parse(quorum_data({'agy-reviewer': table}))
        gh = FakeGH()
        runner = FakeRunner(unavailable_model('agy-reviewer'))
        d = Dispatcher(cfg, gh, FakeWS(), runner, log=lambda m: None,
                       identities=RoleIdentities(gh))
        made = change(cfg, reviewer='agy-reviewer')
        gh.labels[100] = set(made.labels)
        self.assertEqual(d._select(made, 1), (['agy-reviewer'], ''))
        with self.assertRaises(agents.ModelsUnavailable) as ctx:
            d._do_review(made)
        # Sonnet is independent of Opus by tuple, but of the implementer's provider.
        self.assertEqual([call[0] for call in runner.calls], ['agy-reviewer'])
        self.assertEqual(ctx.exception.tried[-1][0], SONNET)
        self.assertIn('not independent', ctx.exception.tried[-1][1])
        self.assertEqual(gh.comments, [])


SONNET_KEY = 'sonnet'


def legacy_quorum(codex=None):
    """config.toml with the keys of before, every provider declared, the rules of P5-5 on and
    a fourth key of claude with Sonnet, last in the pool: the third reviewer of these tests."""
    agents_ = {kind: declared(kind) for kind in PROVIDERS}
    agents_[SONNET_KEY] = declared('claude', model=SONNET, model_label='Claude Sonnet 5.5')
    if codex:
        agents_['codex'] = dict(agents_['codex'], **codex)
    return parse(dict(DATA, agents=agents_,
                      roles=dict(DATA['roles'], reviewers=['codex', 'agy', SONNET_KEY]),
                      review_policy=dict(DATA['review_policy'], provider_quorum=True)))


class ThirdReviewerTests(Quorum):
    """A level 2 round, end to end: the third reviewer takes the place of the unavailable one,
    and the promotion recomputes the round's receipt, whatever the memory says by then."""

    def setUp(self):
        patcher = mock.patch.dict(FakeIdentities.LOGINS, {SONNET_KEY: 'sonnet-agent[bot]'})
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.memory = unavailability.Memory(tmp.name, clock=lambda: NOW)

    def forget_agy(self, cfg):
        for agent in cfg.agent('agy').chain():
            self.memory.forget('agy', agent.model)

    def test_the_third_reviewer_replaces_an_unavailable_one(self):
        cfg = legacy_quorum()
        for agent in cfg.agent('agy').chain():
            self.memory.record('agy', agent.model, 'quota', NOW + 3600)
        self.make(raw_review(), raw_review(), cfg=cfg)
        self.d.memory = self.memory
        self.d.tick()
        # agy is back between the two reviews: the round keeps sonnet.
        self.forget_agy(cfg)
        self.d.tick()
        self.assertEqual(self.reviewed(), ['codex', SONNET_KEY])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        for record in self.records('review'):
            self.assertEqual(record['receipt']['reviewers'], ['codex', SONNET_KEY])
        self.assertEqual([r['event'] for r in self.records('policy')], ['promoted'])
        self.assertIn('reviewer agy skipped', '\n'.join(self.logs))

    def test_the_quorum_holds_on_every_model_a_turn_runs(self):
        # codex falls back on a Claude model: with sonnet beside it, no reviewer of the round
        # would be of another provider than the implementer.
        cfg = legacy_quorum({'fallback_models': [
            {'model': 'claude-haiku-5-5', 'model_label': 'Claude Haiku 5.5',
             'provider': 'anthropic'}]})
        for agent in cfg.agent('agy').chain():
            self.memory.record('agy', agent.model, 'quota', NOW + 3600)
        self.make(unavailable_model('codex'), cfg=cfg)
        self.d.memory = self.memory
        self.d.tick()
        self.assertEqual(self.reviewed(), ['codex'])
        self.assertIn('- `claude-haiku-5-5`: not independent', self.gh.comments[-1][1])
        self.assertEqual(self.records('review'), [])


if __name__ == '__main__':
    unittest.main()
