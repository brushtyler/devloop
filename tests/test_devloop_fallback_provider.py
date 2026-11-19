"""devloop: no reviewer fallback on the provider of an author of the PR (#410, ADR 0005, P5-6).

With ``review_policy.fallback_provider_ban``, off by default (D10), every agent and every fallback
model declares its provider, so the fallbacks the implementer and the fixers run are recorded with
theirs. At runtime, at both levels and whatever the quorum rule of D8, a reviewer's fallback model
is never of the provider of an author of the PR: when no model of the chain is admitted the turn
fails without a review (``tool_failure``). At startup a reviewer of the pool with a fallback of a
provider other than its own model's and the same as an agent's that may implement stops devloop.
"""

import dataclasses
import unittest

from devloop import agents, config
from devloop.config import ConfigError
from devloop.dispatcher import Dispatcher
from devloop.policy import agent_profile
from helpers import ssdlc_project
from helpers.devloop_fakes import FakeGH
from test_devloop import FakeRunner, FakeWS, raw_review
from test_devloop_implementer_app import IMPL_APP, RoleIdentities
from test_devloop_reviewer_provider import (DATA, HAIKU_FALLBACK, SONNET, change, declared,
                                            quorum_data, unavailable_model)

GPT_FALLBACK = [{'model': 'gpt-6-mini', 'model_label': 'GPT 6 Mini', 'provider': 'openai'}]
CLAUDE_ON_AGY = [{'model': SONNET, 'model_label': 'Claude Sonnet 5.5', 'provider': 'anthropic'}]


def ban_data(agents_=None, ban=True, quorum=False):
    """The keys by role of P5-3 and P5-5, every provider declared, with the ban of P5-6 on."""
    data = quorum_data(agents_, quorum=quorum)
    data['review_policy'] = dict(data['review_policy'], fallback_provider_ban=ban)
    return data


def with_fallbacks(kind, fallbacks, **over):
    """``declared(kind)`` with ``fallbacks`` as its fallback models, their providers as given."""
    return dict(declared(kind, **over), fallback_models=fallbacks)


def runtime_data(ban=True, quorum=False, reviewer=HAIKU_FALLBACK):
    """agy implements, without fallbacks; codex alone reviews, falling back on ``reviewer``: no
    agent that may implement is of Anthropic, so the startup check admits a Claude fallback."""
    agents_ = {'agy-implementer': with_fallbacks('agy', [], app='implementer'),
               'codex-reviewer': with_fallbacks('codex', reviewer, app='codex')}
    return dict(DATA, agents=agents_,
                roles={'implementer': 'agy-implementer', 'reviewers': ['codex-reviewer']},
                review_policy=dict(DATA['review_policy'], provider_quorum=quorum,
                                   fallback_provider_ban=ban))


def parse(data):
    return dataclasses.replace(config.parse_config(data), max_parallel_actions=1)


class ConfigTests(unittest.TestCase):
    def test_off_by_default(self):
        cfg = ssdlc_project.load()
        self.assertFalse(cfg.review_policy.fallback_provider_ban)
        self.assertIn('fallback_provider_ban', config.OVERLAY_KEYS['review_policy'])
        # Off, it stays out of the policy version: no approval is voided.
        on = dataclasses.replace(cfg.review_policy, fallback_provider_ban=True)
        self.assertNotEqual(on.version(), cfg.review_policy.version())
        self.assertEqual(dataclasses.replace(on, fallback_provider_ban=False).version(),
                         cfg.review_policy.version())
        self.assertTrue(parse(ban_data()).review_policy.fallback_provider_ban)

    def test_a_boolean(self):
        with self.assertRaises(ConfigError) as ctx:
            config.parse_config(ban_data(ban='yes'))
        self.assertIn('review_policy.fallback_provider_ban: expected bool', str(ctx.exception))

    def test_the_ban_requires_every_provider(self):
        undeclared = dict(declared('codex', app='codex'))
        del undeclared['provider']
        no_fallback = declared('agy', app='agy')
        del no_fallback['fallback_models'][0]['provider']
        for agents_, error in (
                ({'codex-reviewer': undeclared},
                 'agents.codex-reviewer.provider: required with '
                 'review_policy.fallback_provider_ban'),
                ({'agy-reviewer': no_fallback},
                 'agents.agy-reviewer.fallback_models[0].provider: required with '
                 'review_policy.fallback_provider_ban')):
            with self.subTest(error=error):
                with self.assertRaises(ConfigError) as ctx:
                    config.parse_config(ban_data(agents_))
                self.assertIn(error, str(ctx.exception))
                config.parse_config(ban_data(agents_, ban=False))

    def test_a_reviewer_falling_back_on_an_implementers_provider_is_refused(self):
        # agy falls back on Claude while claude-implementer may implement.
        agy = with_fallbacks('agy', CLAUDE_ON_AGY, app='agy')
        with self.assertRaises(ConfigError) as ctx:
            config.parse_config(ban_data({'agy-reviewer': agy}))
        self.assertIn("agents.agy-reviewer.fallback_models[0].provider: 'anthropic' is the "
                      "provider of 'claude-implementer', which may implement",
                      str(ctx.exception))
        # Off, the configuration loads; with the rules of P5-5 alone too.
        config.parse_config(ban_data({'agy-reviewer': agy}, ban=False))
        config.parse_config(ban_data({'agy-reviewer': agy}, ban=False, quorum=True))
        # Any key an impl:<kind> label may choose counts: codex-implementer is of OpenAI.
        gpt = with_fallbacks('agy', GPT_FALLBACK, app='agy')
        config.parse_config(ban_data({'agy-reviewer': gpt}))
        with self.assertRaises(ConfigError) as ctx:
            config.parse_config(ban_data({
                'agy-reviewer': gpt,
                'codex-implementer': with_fallbacks('codex', [], app='implementer')}))
        self.assertIn("'openai' is the provider of 'codex-implementer'", str(ctx.exception))

    def test_a_fallback_of_the_reviewers_own_provider_is_admitted(self):
        # claude-reviewer falls back on Haiku: the provider of its own model, which the quorum
        # rule judges; the ban at runtime still refuses it on a PR of claude.
        claude = declared('claude', app='claude', model=SONNET, model_label='Claude Sonnet 5.5')
        claude['fallback_models'] = HAIKU_FALLBACK
        cfg = parse(ban_data({'claude-reviewer': claude}, quorum=True))
        self.assertEqual([m.provider for m in cfg.agent('claude-reviewer').chain()],
                         ['anthropic', 'anthropic'])


class RuntimeTests(unittest.TestCase):
    def review(self, cfg, outputs, made=None):
        gh = FakeGH()
        runner = FakeRunner(*outputs)
        d = Dispatcher(cfg, gh, FakeWS(), runner, log=lambda m: None,
                       identities=RoleIdentities(gh))
        made = made or change(cfg, 'agy-implementer')
        gh.labels[made.number] = set(made.labels)
        self.assertEqual(d._select(made, 1), (['codex-reviewer'], ''))
        return d, gh, runner, made

    def claude_fix(self):
        """A fix recorded by claude, whose agent may no longer be configured: an author."""
        claude = parse(quorum_data()).agent('claude-implementer')
        return {'profile': agent_profile(claude, IMPL_APP, 'write')}

    def test_level_one_refuses_a_fallback_of_a_fixers_provider(self):
        cfg = parse(runtime_data())
        d, gh, runner, made = self.review(
            cfg, [unavailable_model('codex-reviewer')],
            change(cfg, 'agy-implementer', fixes=[self.claude_fix()]))
        with self.assertRaises(agents.ModelsUnavailable) as ctx:
            d._do_review(made)
        # Haiku never runs: no review, the turn fails as a failure of the tool.
        self.assertEqual([call[0] for call in runner.calls], ['codex-reviewer'])
        self.assertEqual(ctx.exception.tried[-1][0], 'claude-haiku-5-5')
        self.assertIn('not independent', ctx.exception.tried[-1][1])
        self.assertEqual(gh.comments, [])

    def test_off_the_same_fallback_runs(self):
        # Before P5-6, level 1 compared only the implementer: a fixer's provider did not count.
        cfg = parse(runtime_data(ban=False))
        d, gh, runner, made = self.review(
            cfg, [unavailable_model('codex-reviewer'), raw_review()],
            change(cfg, 'agy-implementer', fixes=[self.claude_fix()]))
        d._do_review(made)
        self.assertEqual([call[0] for call in runner.calls], ['codex-reviewer'] * 2)

    def test_a_fallback_of_another_provider_runs(self):
        cfg = parse(runtime_data(reviewer=GPT_FALLBACK))
        d, gh, runner, made = self.review(
            cfg, [unavailable_model('codex-reviewer'), raw_review()],
            change(cfg, 'agy-implementer', fixes=[self.claude_fix()]))
        d._do_review(made)
        self.assertEqual([call[0] for call in runner.calls], ['codex-reviewer'] * 2)

    def test_the_primary_model_is_left_to_the_other_rules(self):
        cfg = parse(runtime_data())
        d, gh, runner, made = self.review(
            cfg, [raw_review()], change(cfg, 'agy-implementer', fixes=[self.claude_fix()]))
        d._do_review(made)
        self.assertEqual([call[0] for call in runner.calls], ['codex-reviewer'])

    def test_unknown_authors_admit_no_fallback(self):
        # A marker that does not record who wrote the code: no fallback is verifiable.
        cfg = parse(runtime_data(reviewer=GPT_FALLBACK))
        made = change(cfg, 'agy-implementer')
        del made.meta['profile']
        d, gh, runner, made = self.review(cfg, [unavailable_model('codex-reviewer')], made)
        with self.assertRaises(agents.ModelsUnavailable):
            d._do_review(made)
        self.assertEqual([call[0] for call in runner.calls], ['codex-reviewer'])
        # Off, the same fallback runs.
        off = parse(runtime_data(ban=False, reviewer=GPT_FALLBACK))
        made = change(off, 'agy-implementer')
        del made.meta['profile']
        d, gh, runner, made = self.review(
            off, [unavailable_model('codex-reviewer'), raw_review()], made)
        d._do_review(made)
        self.assertEqual([call[0] for call in runner.calls], ['codex-reviewer'] * 2)


if __name__ == '__main__':
    unittest.main()
