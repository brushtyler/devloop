"""devloop: the implementer's role app, the agent keys by role and the choice of the implementer
(#407, ADR 0005, P5-3).

An agent key names agent and role in full (``claude-implementer``, ``codex-reviewer``) and serves
that role only; it declares the app it signs with (``app``): the implementers the role app
``[apps.implementer]``, the reviewers the app of their agent. The conflict checks compare the agent
in effect, never the keys, and they do so already with the keys of before: the check on the PRs
of the supervisor's agent never stops firing while the keys change. The implementer of an issue is
chosen at its takeover (D7): an ``impl:<kind>`` label, then the area map, then the default. Off by
default (D10): the configuration keeps the keys of before, and nothing signs with the
implementer's app. At startup devloop stops while an open PR names keys it does not have, and the
memory of the unavailable models moves to the new keys.
"""

import dataclasses
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import devloop.__main__ as cli
from devloop import agents, apps, config, markdown, unavailability
from devloop.apps import IMPLEMENT
from devloop.config import AppConfig, ConfigError, SupervisorActor
from devloop.dispatcher import Dispatcher
from devloop.github import GitHubError
from devloop.ledger import Ledger
from devloop.planning import implementer_choice
from devloop.policy import agent_profile
from devloop.watch import Trust
from helpers import ssdlc_project
from helpers.devloop_fakes import META, SHA1, FakeGH, FakeIdentities
from test_devloop import CFG, FakeRunner, FakeWS, implemented, pr, raw_review
from test_devloop_ledger import LedgerGH, ReportingRunner, TreeWS
from test_devloop_quorum import FIRST, SECOND, Quorum

ROOT = Path(__file__).resolve().parents[1]
DATA = ssdlc_project.data()
IMPL_APP = 'implementer[bot]'
NEEDS_OPERATOR = 'agent:needs-operator'


def migrated(agents=None, **roles):
    """config.toml with the keys by role (#407): claude implements with the implementer app,
    codex and agy review with their agents' apps."""
    base = {'claude-implementer': dict(DATA['agents']['claude'], app='implementer'),
            'codex-reviewer': dict(DATA['agents']['codex'], app='codex'),
            'agy-reviewer': dict(DATA['agents']['agy'], app='agy')}
    return dict(DATA, agents=dict(base, **(agents or {})),
                roles=dict({'implementer': 'claude-implementer',
                            'reviewers': ['codex-reviewer', 'agy-reviewer']}, **roles))


def parse(data):
    return dataclasses.replace(config.parse_config(data), max_parallel_actions=1)


# claude as the third reviewer (P5-5), with another model: first of the pool here, to show that
# the same agent is skipped whatever its key.
SONNET = dict(DATA['agents']['claude'], app='claude', model='claude-sonnet-5-5',
              model_label='Claude Sonnet 5.5')
ROLE = parse(migrated())
WITH_CLAUDE = parse(migrated({'claude-reviewer': SONNET},
                             reviewers=['claude-reviewer', 'codex-reviewer', 'agy-reviewer']))
# Three implementers, for the choice of D7.
CODEX_IMPL = dict(DATA['agents']['codex'], app='implementer')
CHOICE = parse(migrated({'codex-implementer': CODEX_IMPL},
                        implementers={'area:docs': 'claude-implementer',
                                      'area:ui': 'codex-implementer',
                                      'area:text': 'claude-implementer'}))


def role_meta(cfg=ROLE, key='claude-implementer'):
    return dict(META, implementer=key, reviewer='codex-reviewer',
                profile=agent_profile(cfg.agent(key), IMPL_APP, 'write'))


class RoleIdentities(FakeIdentities):
    LOGINS = dict(FakeIdentities.LOGINS, implementer=IMPL_APP)


def script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'),
                                                  ROOT / 'scripts' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConfigTests(unittest.TestCase):
    def test_off_by_default_with_the_app_declared(self):
        cfg = ssdlc_project.load()
        self.assertEqual((cfg.implementer, cfg.reviewers), ('claude', ('codex', 'agy')))
        self.assertEqual(cfg.implementers, ())
        self.assertEqual(cfg.apps['implementer'],
                         AppConfig(5824194, 'brushtyler-devloop-implementer'))
        self.assertEqual({key: cfg.app_of(key) for key in cfg.agents},
                         {'claude': 'claude', 'codex': 'codex', 'agy': 'agy'})
        # Nothing signs with the implementer app: devloop does not even read its key.
        with tempfile.TemporaryDirectory() as tmp:
            for actor, app in cfg.apps.items():
                if actor not in (apps.SUPERVISOR, apps.IMPLEMENTER):
                    key = Path(tmp) / f'{app.slug}.2026-11-19.private-key.pem'
                    key.write_text('k')
                    key.chmod(0o600)
            with mock.patch.dict(os.environ, {'DEVLOOP_APP_KEY_DIR': tmp}):
                identities = apps.Identities.from_config(cfg)
        self.assertNotIn(apps.IMPLEMENTER, identities.apps)
        self.assertNotIn('brushtyler-devloop-implementer[bot]', Trust.from_config(cfg).apps)

    def test_keys_by_role_declare_their_app(self):
        self.assertEqual({key: ROLE.app_of(key) for key in ROLE.agents},
                         {'claude-implementer': 'implementer', 'codex-reviewer': 'codex',
                          'agy-reviewer': 'agy'})
        self.assertEqual(ROLE.implementer, 'claude-implementer')
        self.assertTrue(ROLE.signs_for_role('claude-implementer'))
        self.assertFalse(ROLE.signs_for_role('codex-reviewer'))
        # Several implementers share the one implementer app.
        self.assertEqual(CHOICE.app_of('codex-implementer'), 'implementer')
        with tempfile.TemporaryDirectory() as tmp:
            for app in ROLE.apps.values():
                key = Path(tmp) / f'{app.slug}.2026-11-19.private-key.pem'
                key.write_text('k')
                key.chmod(0o600)
            with mock.patch.dict(os.environ, {'DEVLOOP_APP_KEY_DIR': tmp}):
                identities = apps.Identities.from_config(ROLE)
        self.assertEqual(identities.login(ROLE.app_of('claude-implementer')),
                         'brushtyler-devloop-implementer[bot]')

    def test_a_key_used_in_another_role_stops_devloop(self):
        reviewer = {'claude-reviewer': SONNET}
        for data, error in (
                (migrated(reviewer, implementer='claude-reviewer'),
                 "roles.implementer: 'claude-reviewer' is a reviewer key"),
                (migrated(reviewers=['codex-reviewer', 'claude-implementer']),
                 "'claude-implementer' is a implementer key"),
                (dict(migrated(), review_policy=dict(DATA['review_policy'],
                                                     advisory_reviewer='claude-implementer')),
                 'advisory_reviewer: .* implementer key'),
                (dict(migrated(), preflight='codex-reviewer'),
                 "preflight: 'codex-reviewer' is a reviewer key"),
                (dict(migrated(), conclave=['claude-implementer', 'codex-reviewer',
                                            'agy-reviewer']), 'conclave: .* key'),
                (migrated({'codex-implementer': dict(DATA['agents']['claude'],
                                                     app='implementer')}),
                 "names the agent and the role in full: 'claude-implementer'")):
            with self.subTest(error=error), self.assertRaisesRegex(ConfigError, error):
                config.parse_config(data)

    def test_the_declared_apps(self):
        agents = migrated()['agents']
        for change, error in (
                ({'codex-reviewer': dict(agents['codex-reviewer'], app='implementer')},
                 'only a <kind>-implementer key may declare the implementer app'),
                ({'claude-implementer': dict(agents['claude-implementer'], app='claude')},
                 'an implementer signs with the implementer app'),
                ({'agy-reviewer': dict(agents['agy-reviewer'], app='codex')},
                 'each app belongs to one actor'),
                ({'agy-reviewer': dict(agents['agy-reviewer'], app='supervisor')},
                 'reserved to devloop'),
                ({'agy-reviewer': dict(agents['agy-reviewer'], app='merger')},
                 'reserved to devloop'),
                ({'agy-reviewer': dict(agents['agy-reviewer'], app='nowhere')},
                 r'no \[apps.nowhere\] table'),
                ({'agy-reviewer': dict(agents['agy-reviewer'], app=' ')}, 'must not be empty')):
            with self.subTest(error=error), self.assertRaisesRegex(ConfigError, error):
                config.parse_config(dict(migrated(), agents=dict(agents, **change)))
        # A key of before may not sign with the implementer app either.
        legacy = dict(DATA['agents'], claude=dict(DATA['agents']['claude'], app='implementer'))
        with self.assertRaisesRegex(ConfigError, 'only a <kind>-implementer key'):
            config.parse_config(dict(DATA, agents=legacy))
        # An app table nobody declares is refused, the role apps aside.
        with self.assertRaisesRegex(ConfigError, 'apps.claude-reviewer: not the dispatcher'):
            config.parse_config(dict(migrated(), apps=dict(
                DATA['apps'], **{'claude-reviewer': {'app_id': 99, 'slug': 'x'}})))

    def test_the_pool_is_no_longer_refused_by_key(self):
        # claude reviews with another model: the check is per PR, by agent (D6).
        self.assertEqual(WITH_CLAUDE.reviewers,
                         ('claude-reviewer', 'codex-reviewer', 'agy-reviewer'))
        # A key still serves one role.
        with self.assertRaisesRegex(ConfigError, 'different agent from the implementer'):
            config.parse_config(dict(DATA, roles=dict(DATA['roles'],
                                                      reviewers=['claude', 'codex'])))

    def test_the_area_map(self):
        self.assertEqual(CHOICE.implementers, (('area:docs', 'claude-implementer'),
                                               ('area:ui', 'codex-implementer'),
                                               ('area:text', 'claude-implementer')))
        for table, error in (({'area:x': 'nobody'}, "unknown agent 'nobody'"),
                             ({'area:x': 'codex-reviewer'}, 'is a reviewer key'),
                             ({'impl:codex': 'claude-implementer'}, 'expected an area label'),
                             ({'area:x': 3}, 'expected the key of an implementer'),
                             ('x', 'expected a table')):
            with self.subTest(table=table), self.assertRaisesRegex(ConfigError, error):
                config.parse_config(migrated(implementers=table))

    def test_the_overlay_sets_the_app_and_the_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / 'config.local.toml'
            local.write_text('[roles.implementers]\n"area:docs" = "claude"\n'
                             '[agents.claude]\napp = "claude"\n')
            cfg = ssdlc_project.load(local)
        self.assertEqual(cfg.implementers, (('area:docs', 'claude'),))
        self.assertEqual(cfg.app_of('claude'), 'claude')

    def test_the_history_reads_the_keys_of_before(self):
        self.assertEqual(config.legacy_key('claude', 'implementer', ROLE.agents),
                         'claude-implementer')
        self.assertEqual(config.legacy_key('codex', 'reviewer', ROLE.agents), 'codex-reviewer')
        self.assertEqual(config.legacy_key('codex', 'reviewer', CFG.agents), 'codex')
        self.assertEqual(config.legacy_key('claude', 'reviewer', ROLE.agents), 'claude')
        # The reviews of the daily summary: the old keys whose reviewer is configured, never
        # the implementer's key.
        self.assertEqual(ROLE.review_keys(), {'codex-reviewer', 'agy-reviewer', 'codex', 'agy'})
        self.assertEqual(CFG.review_keys(), {'claude', 'codex', 'agy'})


class IdentityTests(unittest.TestCase):
    def make(self, cfg=ROLE, runner=None, gh=None):
        self.gh, self.ws = gh or FakeGH(), FakeWS()
        self.runner = runner or FakeRunner()
        self.logs = []
        return Dispatcher(cfg, self.gh, self.ws, self.runner, log=self.logs.append,
                          identities=RoleIdentities(self.gh))

    def test_the_implementer_signs_with_the_role_app(self):
        d = self.make(runner=FakeRunner(implemented()))
        d._do_implement(7)
        self.assertEqual([a['name'] for a in self.ws.authors], [IMPL_APP])
        self.assertEqual([a['name'] for a in self.ws.auths], [IMPL_APP])
        self.assertIn(('implementer', IMPLEMENT, 'create_pr'), self.gh.acts)
        (created,) = self.gh.created
        meta = markdown.read_marker(created['body'], 'pr')
        self.assertEqual((meta['implementer'], meta['reviewer']),
                         ('claude-implementer', 'codex-reviewer'))
        self.assertEqual((meta['profile']['identity'], meta['profile']['kind']),
                         (IMPL_APP, 'claude'))
        self.assertEqual(self.runner.calls[0][0], 'claude-implementer')

    def test_records_of_the_role_app(self):
        d = self.make()
        # The fixes and syncs of the PR's implementer come from the implementer app.
        self.assertTrue(d._wrote(IMPL_APP, 'claude-implementer'))
        self.assertFalse(d._wrote('claude-agent[bot]', 'claude-implementer'))
        # Never a review, a readiness check or a conclave's record.
        self.assertFalse(d._wrote_as_agent(IMPL_APP, 'claude-implementer'))
        self.assertTrue(d._wrote_as_agent('codex-agent[bot]', 'codex-reviewer'))
        # The marker of a PR opened before the migration names claude, whose app was then the
        # implementer's: read as it is.
        self.assertTrue(d._wrote('claude-agent[bot]', 'claude'))

    def test_a_review_signed_by_the_role_app_does_not_count(self):
        d = self.make(cfg=CHOICE)
        record = {'source': 'agent', 'reviewer': 'codex-implementer', 'round': 1,
                  'verdict': 'approve', 'head_sha': SHA1, 'blocking_findings': [],
                  'non_blocking_findings': [], 'summary': 's'}
        change = pr()
        change.meta = role_meta(CHOICE)
        comments = [{'author': {'login': IMPL_APP},
                     'body': markdown.publishable('x', 'review', record)}]
        self.assertEqual(d._marker_entries(change, comments, 'review'), [])

    def test_the_watcher_and_the_summary_map_keys_to_apps(self):
        trust = Trust.from_config(ROLE)
        login = 'brushtyler-devloop-implementer[bot]'
        self.assertTrue(trust.wrote(login, 'claude-implementer'))
        self.assertTrue(trust.wrote('brushtyler-devloop-codex-agent[bot]', 'codex-reviewer'))
        # The records of before, read with the app their key names.
        self.assertTrue(trust.wrote('brushtyler-devloop-claude-agent[bot]', 'claude'))
        self.assertFalse(trust.wrote(login, 'codex-reviewer'))


class ConflictCheckTests(unittest.TestCase):
    """The conflict checks compare the agent in effect, never the keys, and with the keys of
    before too: renaming the keys by role must never stop them firing (the order of the
    migration of #407)."""

    def make(self, cfg):
        gh = FakeGH()
        return Dispatcher(cfg, gh, FakeWS(), FakeRunner(), log=lambda m: None,
                          identities=RoleIdentities(gh))

    def own(self, cfg, meta):
        change = pr()
        change.meta = meta
        return self.make(cfg)._supervisor_implemented(change)

    def test_the_supervisors_own_prs_before_and_after_the_rename(self):
        legacy = dataclasses.replace(CFG, supervisor='claude')
        before = dict(META, profile=agent_profile(CFG.agent('claude'), 'claude-agent[bot]',
                                                  'write'))
        after = role_meta()
        # The agent key of before, with the PR's key of before and with the renamed one.
        self.assertTrue(self.own(legacy, before))
        self.assertTrue(self.own(dataclasses.replace(legacy, agents=dict(
            CFG.agents, **{'claude-implementer': ROLE.agent('claude-implementer')})), after))
        # The supervisor actor of P5-4 with the renamed keys.
        actor = SupervisorActor('claude-supervisor', 'claude', 'anthropic', 'claude-opus-5-5')
        self.assertTrue(self.own(dataclasses.replace(ROLE, supervisor=actor.name,
                                                     supervisor_actor=actor), after))
        # Another agent's PR stays out, before and after.
        codex = dataclasses.replace(CFG, supervisor='codex')
        self.assertFalse(self.own(codex, before))
        self.assertFalse(self.own(codex, after))

    def test_the_reviewer_of_level_one_is_another_agent(self):
        d = self.make(WITH_CLAUDE)
        change = pr()
        change.meta = dict(role_meta(WITH_CLAUDE), reviewer='claude-reviewer')
        # claude-reviewer is another key, but the same agent as claude-implementer.
        self.assertEqual(d._select(change, 1), (['codex-reviewer'], ''))
        self.assertEqual(d._select(change, 2), (['codex-reviewer', 'agy-reviewer'], ''))
        # The same with the keys of before: the agent decides, not the key.
        legacy = self.make(dataclasses.replace(CFG, reviewers=('claude2', 'codex'), agents=dict(
            CFG.agents, claude2=dataclasses.replace(CFG.agent('claude'), key='claude2',
                                                    model='claude-sonnet-5-5'))))
        change = pr()
        change.meta = dict(META, reviewer='claude2')
        self.assertEqual(legacy._select(change, 1), (['codex'], ''))

    def test_an_unknown_implementer_has_no_reviewer(self):
        d = self.make(ROLE)
        change = pr()
        change.meta = dict(META, implementer='gone')
        reviewers, problem = d._select(change, 1)
        self.assertEqual(reviewers, [])
        self.assertIn('non è verificabile', problem)

    def test_the_marker_names_the_first_reviewer_of_another_agent(self):
        gh = FakeGH()
        d = Dispatcher(WITH_CLAUDE, gh, FakeWS(), FakeRunner(implemented()),
                       log=lambda m: None, identities=RoleIdentities(gh))
        d._do_implement(7)
        meta = markdown.read_marker(gh.created[0]['body'], 'pr')
        self.assertEqual(meta['reviewer'], 'codex-reviewer')


# agy run with a Claude model: another CLI and another model, the same provider as claude.
AGY_CLAUDE = parse(migrated({'agy-reviewer': dict(DATA['agents']['agy'], app='agy',
                                                  model='claude-sonnet-5-5',
                                                  model_label='Claude Sonnet 5.5')}))
OPUS = 'claude-opus-5-5'


class ProviderTests(unittest.TestCase):
    """The conflict checks compare the provider too (ADR 0005, D8, P5-3, #407): another CLI with
    another model of the same provider is the same agent in effect."""

    def make(self, cfg):
        gh = FakeGH()
        return Dispatcher(cfg, gh, FakeWS(), FakeRunner(), log=lambda m: None,
                          identities=RoleIdentities(gh))

    def test_same_agent_compares_the_provider(self):
        opus = {'kind': 'claude', 'model': OPUS}
        for other, same in (({'kind': 'agy', 'model': 'claude-sonnet-5-5'}, True),
                            ({'kind': 'codex', 'model': 'Claude-Haiku-5-5'}, True),
                            ({'kind': 'agy', 'model': 'gemini-4-argon-high'}, False),
                            ({'kind': 'codex', 'model': 'gpt-6.1-sol'}, False),
                            # An unknown prefix: compared by kind and model only.
                            ({'kind': 'agy', 'model': 'mystery-1'}, False),
                            ({'kind': 'agy', 'model': OPUS}, True),
                            ({'kind': 'claude', 'model': 'mystery-1'}, True)):
            with self.subTest(other=other):
                self.assertEqual(config.same_agent(other, opus), same)
                self.assertEqual(config.same_agent(opus, other), same)
        self.assertEqual(config.provider_of(AGY_CLAUDE.agent('agy-reviewer')), 'anthropic')
        self.assertEqual(config.provider_of(ROLE.agent('codex-reviewer')), 'openai')
        self.assertEqual(config.provider_of(ROLE.agent('agy-reviewer')), 'google')
        # A declared provider prevails over the prefix (the supervisor actor of P5-4).
        actor = SupervisorActor('claude-supervisor', 'claude', 'Anthropic', 'mystery-1')
        self.assertEqual(config.provider_of(actor), 'anthropic')
        self.assertTrue(config.same_agent({'kind': 'agy', 'model': 'claude-sonnet-5-5'}, actor))

    def test_a_reviewer_of_the_implementers_provider_is_excluded(self):
        d = self.make(AGY_CLAUDE)
        change = pr()
        change.meta = dict(role_meta(AGY_CLAUDE), reviewer='agy-reviewer')
        # Level 1: agy with Claude Sonnet does not review the code of Claude Opus.
        self.assertEqual(d._select(change, 1), (['codex-reviewer'], ''))
        # Level 2: no pair without it.
        reviewers, problem = d._select(change, 2)
        self.assertEqual(reviewers, [])
        self.assertIn('non ne ha abbastanza', problem)
        # With only agy in the pool, level 1 has no reviewer at all.
        alone = self.make(dataclasses.replace(AGY_CLAUDE, reviewers=('agy-reviewer',),
                                              reviewer='agy-reviewer'))
        reviewers, problem = alone._select(change, 1)
        self.assertEqual(reviewers, [])
        self.assertIn('nessun reviewer configurato è un agent diverso', problem)
        # agy with Gemini still reviews it.
        self.assertEqual(self.make(ROLE)._select(change, 1), (['agy-reviewer'], ''))

    def test_the_supervisors_own_prs_by_provider(self):
        actor = SupervisorActor('claude-supervisor', 'claude', 'anthropic', OPUS)
        cfg = dataclasses.replace(ROLE, supervisor=actor.name, supervisor_actor=actor)
        change = pr()
        for model, own in (('claude-sonnet-5-5', True), ('gemini-4-argon-high', False)):
            with self.subTest(model=model):
                change.meta = dict(META, implementer='agy-implementer',
                                   profile=dict(role_meta()['profile'], kind='agy', model=model))
                self.assertEqual(self.make(cfg)._supervisor_implemented(change), own)


def claude_fallback(cfg, key):
    """``cfg`` with the reviewer ``key`` (agy, Gemini) falling back to the implementer's very
    model, Claude Opus: another CLI, the same model."""
    agent = dataclasses.replace(cfg.agent(key), fallback_models=(
        config.FallbackModel(OPUS, 'Claude Opus 5.5', 'high'),))
    return dataclasses.replace(cfg, agents=dict(cfg.agents, **{key: agent}))


def unavailable_model(key):
    return agents.ModelUnavailable(f'{key} exited with 1', reason='`quota` in "quota exhausted"')


class FallbackSameAgentTests(unittest.TestCase):
    """Every model a review turn actually runs, fallbacks included, is checked against the
    authors (#407): the selection checks only the primary model."""

    def test_level_one_never_falls_back_on_the_implementers_agent(self):
        cfg = claude_fallback(ROLE, 'agy-reviewer')
        gh = FakeGH()
        runner = FakeRunner(unavailable_model('agy-reviewer'))
        logs = []
        d = Dispatcher(cfg, gh, FakeWS(), runner, log=logs.append,
                       identities=RoleIdentities(gh))
        change = pr()
        change.meta = dict(role_meta(cfg), reviewer='agy-reviewer')
        gh.labels[100] = set(change.labels)
        self.assertEqual(d._select(change, 1), (['agy-reviewer'], ''))
        with self.assertRaises(agents.ModelsUnavailable) as ctx:
            d._do_review(change)
        # Gemini ran and was unavailable; Claude Opus never ran, and no review is published.
        self.assertEqual([call[0] for call in runner.calls], ['agy-reviewer'])
        self.assertEqual(ctx.exception.tried[-1][0], OPUS)
        self.assertIn('not independent', ctx.exception.tried[-1][1])
        self.assertEqual(gh.comments, [])
        self.assertEqual(gh.labels[100], {'agent:review'})
        self.assertIn(f'model {OPUS} skipped', '\n'.join(logs))


class FallbackSameAgentQuorumTests(Quorum):
    def test_level_two_never_falls_back_on_an_authors_agent(self):
        # The fallback differs from the implementer by kind, so the tuple alone would admit it.
        self.assertEqual((CFG.implementer, CFG.agent(CFG.implementer).model), ('claude', OPUS))
        cfg = claude_fallback(CFG, SECOND)
        self.make(raw_review(), unavailable_model(SECOND), cfg=cfg)
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.reviewed(), [FIRST, SECOND])  # the fallback never ran
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        self.assertIn(f'- `{OPUS}`: not independent', self.gh.comments[-1][1])
        self.assertEqual([r['reviewer'] for r in self.records('review')], [FIRST])


class ChoiceTests(unittest.TestCase):
    """``implementer_choice``: ``impl:<kind>``, then the area map, then the default (D7)."""

    TRUSTED = {'brushtyler'}

    def choose(self, labels, by='brushtyler', cfg=CHOICE, events=None, admitters=None):
        events = events if events is not None else [(name, by, 't') for name in labels]
        return implementer_choice(cfg, [{'name': n} for n in labels], events,
                                  admitters or self.TRUSTED)

    def test_the_default(self):
        self.assertEqual(self.choose([]), ('claude-implementer', 'default', ''))
        self.assertEqual(self.choose(['bug']), ('claude-implementer', 'default', ''))
        self.assertEqual(self.choose([], cfg=CFG), ('claude', 'default', ''))

    def test_the_label_chooses_and_prevails(self):
        self.assertEqual(self.choose(['impl:codex'])[:2], ('codex-implementer',
                                                           'label `impl:codex`'))
        self.assertEqual(self.choose(['impl:codex', 'area:docs'])[0], 'codex-implementer')
        self.assertEqual(self.choose(['impl:claude', 'area:ui'])[0], 'claude-implementer')

    def test_the_area_map(self):
        self.assertEqual(self.choose(['area:ui'])[0], 'codex-implementer')
        # Two areas of the same implementer agree.
        self.assertEqual(self.choose(['area:docs', 'area:text'])[0], 'claude-implementer')

    def test_only_labels_of_the_admitters_count(self):
        self.assertEqual(self.choose(['impl:codex'], by='stranger')[0], 'claude-implementer')
        self.assertEqual(self.choose(['area:ui'], by='stranger')[0], 'claude-implementer')
        # The latest labeled event decides: a trusted label added again by a stranger no
        # longer counts.
        events = [('impl:codex', 'brushtyler', 't1'), ('impl:codex', 'stranger', 't2')]
        self.assertEqual(self.choose(['impl:codex'], events=events)[0], 'claude-implementer')
        # A label the issue no longer has does not count.
        self.assertEqual(self.choose([], events=[('impl:codex', 'brushtyler', 't')])[0],
                         'claude-implementer')
        # The supervisor, where it admits labels (``label_admitters``).
        sup = 'brushtyler-devloop-supervisor[bot]'
        self.assertEqual(self.choose(['impl:codex'], by=sup, admitters={'brushtyler', sup})[0],
                         'codex-implementer')

    def test_what_goes_to_the_operator(self):
        for labels, text in ((['impl:codex', 'impl:claude'], 'più label'),
                             (['impl:agy'], '`agy-implementer`, che non è configurato'),
                             (['area:docs', 'area:ui'], 'implementer diversi')):
            with self.subTest(labels=labels):
                key, why, problem = self.choose(labels)
                self.assertIsNone(key)
                self.assertIn(text, problem)


class LabeledGH(FakeGH):
    """FakeGH whose issue 7 has ``issue_labels``, each added by ``labeler``."""

    def __init__(self, labels=(), labeler='brushtyler'):
        super().__init__()
        self.issue_labels, self.labeler_of = list(labels), labeler

    def issue(self, number):
        return dict(super().issue(number), labels=[{'name': n} for n in self.issue_labels])

    def issue_history(self, number):
        history = super().issue_history(number)
        history['labeled'] += [(n, self.labeler_of, '2026-11-13T10:00:01Z')
                               for n in self.issue_labels]
        return history


class TakeoverTests(unittest.TestCase):
    def make(self, cfg, labels=(), runner=None):
        self.gh = LabeledGH(labels)
        self.gh.labels[7] = {'agent:ready'}
        self.runner = runner or FakeRunner(implemented())
        return Dispatcher(cfg, self.gh, FakeWS(), self.runner, log=lambda m: None,
                          identities=RoleIdentities(self.gh))

    def test_the_label_chooses_the_implementer_at_the_takeover(self):
        d = self.make(CHOICE, ['impl:codex'])
        d._do_implement(7)
        self.assertEqual(self.runner.calls[0][0], 'codex-implementer')
        meta = markdown.read_marker(self.gh.created[0]['body'], 'pr')
        self.assertEqual(meta['implementer'], 'codex-implementer')
        # Another implementer, the same app.
        self.assertEqual(meta['profile']['identity'], IMPL_APP)
        # The reviewer recorded is not codex, the implementer's agent.
        self.assertEqual(meta['reviewer'], 'agy-reviewer')

    def test_a_conflict_is_the_operators_configuration(self):
        d = self.make(CHOICE, ['area:docs', 'area:ui'])
        d._do_implement(7)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.gh.created, [])
        self.assertEqual(self.gh.labels[7], {NEEDS_OPERATOR})
        number, body = self.gh.comments[-1]
        self.assertEqual(number, 7)
        self.assertEqual(markdown.read_marker(body, 'notice')['class'], 'configuration')
        self.assertIn('`area:ui` → `codex-implementer`', body)

    def test_the_implementer_differs_from_the_preflight_for_each_issue(self):
        checker = dict(DATA['agents']['codex'], model='gpt-6.1-check')
        cfg = parse(dict(migrated({'codex-implementer': CODEX_IMPL, 'checker': checker}),
                         preflight='checker'))
        with mock.patch.object(Dispatcher, '_preflight_view', return_value=None), \
                mock.patch('devloop.dispatch_actions.preflight_state',
                           return_value=('ready', None)):
            d = self.make(cfg, ['impl:codex'])
            d._do_implement(7)
            self.assertEqual(self.runner.calls, [])
            self.assertIn('stesso agent del controllo di prontezza', self.gh.comments[-1][1])
            d = self.make(cfg)
            d._do_implement(7)
            self.assertEqual(self.runner.calls[0][0], 'claude-implementer')

    def test_the_request_records_the_implementer(self):
        gh = LedgerGH()
        d = Dispatcher(CHOICE, gh, TreeWS(), ReportingRunner(implemented(), gh=gh),
                       log=lambda m: None, ledger=Ledger(gh))
        d._do_implement(7)
        (request,) = gh.records(7, 'request')
        self.assertEqual(request['implementer'], 'claude-implementer')
        self.assertIn('Implementer scelto', gh.thread[7][0]['body'])


class StartupTests(unittest.TestCase):
    def raw(self, meta, author='brushtyler', cross=False, branch='agent/issue-7', number=100):
        return {'number': number, 'title': 't', 'body': markdown.publishable('b', 'pr', meta),
                'labels': [], 'headRefName': branch, 'headRefOid': SHA1,
                'author': {'login': author}, 'isCrossRepository': cross}

    def problems(self, cfg, *raws):
        gh = FakeGH()
        gh.open = raws
        return Dispatcher(cfg, gh, FakeWS(), FakeRunner(), log=lambda m: None).key_problems()

    def test_open_prs_with_the_keys_of_before_stop_devloop(self):
        (problem,) = self.problems(ROLE, self.raw(dict(META, reviewer='codex-reviewer')))
        self.assertIn("PR #100: its marker names the implementer 'claude'", problem)
        self.assertEqual(len(self.problems(ROLE, self.raw(META))), 2)
        # With the keys it has, nothing.
        self.assertEqual(self.problems(ROLE, self.raw(role_meta())), [])
        self.assertEqual(self.problems(CFG, self.raw(META)), [])

    def test_a_key_of_another_role(self):
        meta = dict(role_meta(), reviewer='claude-implementer')
        (problem,) = self.problems(ROLE, self.raw(meta))
        self.assertIn("the reviewer 'claude-implementer', a implementer key", problem)

    def test_nobody_elses_pr_stops_devloop(self):
        self.assertEqual(self.problems(ROLE, self.raw(META, author='stranger'),
                                       self.raw(META, cross=True),
                                       self.raw(META, branch='feature/x')), [])

    def test_the_startup_step(self):
        logs = []
        migrate = mock.Mock(return_value=[('claude', ['claude-implementer'])])
        d = SimpleNamespace(cfg=ROLE, memory=SimpleNamespace(migrate=migrate),
                            key_problems=lambda: ['PR #100: x'])
        with self.assertRaises(SystemExit) as ctx:
            cli.check_keys(d, log=logs.append)
        self.assertIn('PR #100: x', str(ctx.exception.code))
        migrate.assert_called_once_with(ROLE.agents)
        self.assertIn('now hold for claude-implementer', logs[0])

        def unreadable():
            raise GitHubError('HTTP 502')
        with self.assertRaisesRegex(SystemExit, 'cannot be checked'):
            cli.check_keys(SimpleNamespace(cfg=ROLE, key_problems=unreadable), log=logs.append)
        cli.check_keys(SimpleNamespace(cfg=ROLE, key_problems=lambda: []), log=logs.append)
        cli.check_keys(SimpleNamespace(cfg=ROLE), log=logs.append)  # a stand-in: nothing


class MemoryTests(unittest.TestCase):
    def test_the_unavailable_models_move_to_the_keys_by_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = unavailability.Memory(tmp, clock=lambda: 1000.0)
            memory.record('claude', 'claude-opus-5-5', 'quota', 5000)
            memory.record('agy', 'gemini-4-argon-high', 'quota', 9000)
            memory.record('claude-reviewer', 'claude-opus-5-5', 'quota', 9000)
            agents = dict(ROLE.agents, **{'claude-reviewer': None})
            moved = memory.migrate(agents)
            self.assertEqual(moved, [('agy', ['agy-reviewer']),
                                     ('claude', ['claude-implementer', 'claude-reviewer'])])
            self.assertIsNone(memory.active('claude', 'claude-opus-5-5'))
            self.assertEqual(memory.active('claude-implementer', 'claude-opus-5-5')['reason'],
                             'quota')
            # Of two records, the later until stays.
            self.assertEqual(memory.active('claude-reviewer', 'claude-opus-5-5')['until'],
                             unavailability.iso(9000))
            self.assertIsNotNone(memory.active('agy-reviewer', 'gemini-4-argon-high'))
            self.assertEqual(memory.migrate(agents), [])
            # Keys still configured stay as they are.
            memory.record('codex', 'gpt-6.1-sol', 'quota', 5000)
            self.assertEqual(memory.migrate(CFG.agents), [])
            self.assertIsNotNone(memory.active('codex', 'gpt-6.1-sol'))


class TokenAndDocsTests(unittest.TestCase):
    def test_agent_token_does_not_expose_the_implementer_app(self):
        token = script('agent-token')
        for actor in ('implementer', 'IMPLEMENTER'):
            with self.subTest(actor=actor), \
                    mock.patch.object(token, 'load_config') as load, \
                    mock.patch.object(token.subprocess, 'run') as run:
                with self.assertRaisesRegex(SystemExit, 'reserved to devloop'):
                    token.main([actor, '--', 'gh', 'pr', 'create'])
                load.assert_not_called()
                run.assert_not_called()

    def test_agents_md_lists_the_implementer_app(self):
        text = (ROOT / 'AGENTS.md').read_text(encoding='utf-8')
        self.assertIn('`brushtyler-devloop-implementer[bot]`', text)


if __name__ == '__main__':
    unittest.main()
