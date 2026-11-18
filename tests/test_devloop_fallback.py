"""devloop: fallback models when an agent's model is unavailable (#121)."""

import dataclasses
import json
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace

from devloop import activity, agents, config, markdown, policy
from devloop.dispatch_actions import Models
from devloop.dispatcher import Dispatcher
from devloop.planning import Action, Snapshot
from helpers.devloop_fakes import FakeGH, FakeIdentities
from test_devloop import CFG, IMPL, SHA1, FakeRunner, FakeWS, implemented, pr, raw_review
from test_devloop_quorum import FIRST, SECOND, Quorum

# One action at a time, as in test_devloop.CFG: parallel actions (#176) are tested on their own.
DATA = dict(tomllib.loads(config.DEFAULT_CONFIG.read_text()), max_parallel_actions=1)
ARGON, HYDROGEN = 'gemini-4-argon-high', 'gemini-4-hydrogen-high'
QUOTA = 'Error: RESOURCE_EXHAUSTED: quota of gemini-4-argon-high exhausted for today'


def with_agent(key, data=DATA, **table):
    """``data`` with ``[agents.<key>]`` updated by ``table`` (a None value removes the key)."""
    agent = {k: v for k, v in dict(data['agents'][key], **table).items() if v is not None}
    return dict(data, agents=dict(data['agents'], **{key: agent}))


def unavailable(key='agy'):
    return agents.ModelUnavailable(f'{key} exited with 1', reason='`quota` in "quota exhausted"')


class FakeCLI:
    """``subprocess.run`` for agy: each model answers with what ``replies`` maps it to, a
    ``(returncode, stdout, stderr)`` triple, or a valid review."""

    def __init__(self, **replies):
        self.replies = replies
        self.calls = []

    def __call__(self, argv, **kw):
        if argv[-1] == '--version':
            return SimpleNamespace(returncode=0, stdout='agy 1.4.2', stderr='')
        model = argv[argv.index('--model') + 1]
        self.calls.append((model, argv[argv.index('-p') + 1]))
        code, out, err = self.replies.get(model.replace('-', '_'), (0, None, ''))
        if out is None:
            out = json.dumps({'status': 'SUCCESS', 'structured_output': raw_review()})
        if isinstance(code, Exception):
            raise code
        return SimpleNamespace(returncode=code, stdout=out, stderr=err)

    def models(self):
        return [model for model, _ in self.calls]


class FakeCodex:
    """``subprocess.run`` for codex: each model answers with what ``replies`` maps it to, a
    ``(returncode, final message file content or None for no file, stderr)`` triple, or a valid
    review."""

    def __init__(self, **replies):
        self.replies = replies
        self.calls = []

    def __call__(self, argv, **kw):
        if argv[-1] == '--version':
            return SimpleNamespace(returncode=0, stdout='codex-cli 0.130.0', stderr='')
        model = argv[argv.index('-m') + 1]
        self.calls.append(model)
        code, final, err = self.replies.get(model.replace('-', '_').replace('.', '_'),
                                            (0, json.dumps(raw_review()), ''))
        if final is not None:
            Path(argv[argv.index('-o') + 1]).write_text(final, encoding='utf-8')
        return SimpleNamespace(returncode=code, stdout='', stderr=err)


CODEX_FALLBACK = 'gpt-6-fallback'


class FallbackConfigTests(unittest.TestCase):
    def test_agy_runs_on_argon_and_falls_back_to_hydrogen(self):
        agy = CFG.agent('agy')
        self.assertEqual((agy.model, agy.effort), (ARGON, 'high'))
        self.assertEqual(agy.fallback_models, (config.FallbackModel(HYDROGEN, 'Gemini 4 Hydrogen',
                                                                    'high'),))
        primary, fallback = agy.chain()
        self.assertIs(primary, agy)
        self.assertEqual((fallback.key, fallback.kind, fallback.model, fallback.model_label,
                          fallback.effort, fallback.fallback_for, fallback.review_args),
                         ('agy', 'agy', HYDROGEN, 'Gemini 4 Hydrogen', 'high', ARGON,
                          agy.review_args))
        self.assertEqual(agy.unavailable_patterns, config.DEFAULT_UNAVAILABLE_PATTERNS)

    def test_agents_without_fallbacks_keep_one_model(self):
        for key in ('claude', 'codex'):
            agent = CFG.agent(key)
            self.assertEqual((agent.fallback_models, agent.chain()), ((), [agent]))

    def test_a_fallback_below_high_is_rejected(self):
        bad = {
            'effort medium': [{'model': 'm2', 'model_label': 'M2', 'effort': 'medium'}],
            'unknown effort': [{'model': 'm2', 'model_label': 'M2', 'effort': 'turbo'}],
            'agy -low model': [{'model': 'gemini-4-hydrogen-low', 'model_label': 'H'}],
            'agy -medium model': [{'model': 'gemini-4-hydrogen-medium', 'model_label': 'H'}],
            'inherited low effort': None,
            'no label': [{'model': 'm2'}],
            'the primary again': [{'model': ARGON, 'model_label': 'A'}],
            'twice': [{'model': 'm2', 'model_label': 'M2'}, {'model': 'm2', 'model_label': 'M'}],
            'not a table': ['m2'],
            'unknown key': [{'model': 'm2', 'model_label': 'M2', 'reasoning': 'high'}],
        }
        for name, fallbacks in bad.items():
            data = (with_agent('agy', effort='low',
                               fallback_models=[{'model': 'm2', 'model_label': 'M2'}])
                    if fallbacks is None else with_agent('agy', fallback_models=fallbacks))
            with self.subTest(name), self.assertRaises(config.ConfigError):
                config.parse_config(data)
        # High and above are accepted; a missing effort is the agent's.
        cfg = config.parse_config(with_agent('codex', fallback_models=[
            {'model': 'gpt-6', 'model_label': 'gpt-6'},
            {'model': 'gpt-5', 'model_label': 'gpt-5', 'effort': 'xhigh'}]))
        self.assertEqual([f.effort for f in cfg.agent('codex').fallback_models], ['high', 'xhigh'])

    def test_unavailability_patterns_and_exit_codes_are_validated(self):
        for table in ({'unavailable_patterns': ['(']}, {'unavailable_patterns': ['']},
                      {'unavailable_patterns': 'quota'}, {'unavailable_exit_codes': [0]},
                      {'unavailable_exit_codes': ['2']}):
            with self.subTest(table), self.assertRaises(config.ConfigError):
                config.parse_config(with_agent('agy', **table))
        cfg = config.parse_config(with_agent('agy', unavailable_patterns=['out of budget'],
                                             unavailable_exit_codes=[75]))
        self.assertEqual((cfg.agent('agy').unavailable_patterns,
                          cfg.agent('agy').unavailable_exit_codes), (('out of budget',), (75,)))

    def test_fallbacks_are_part_of_the_policy_version_only_when_present(self):
        row = ('agy', None, 'agy', ARGON, 'high', ())
        before = policy.ReviewPolicy(reviewers=(row,)).version()
        self.assertEqual(policy.ReviewPolicy(reviewers=(row + ((),),)).version(), before)
        self.assertNotEqual(
            policy.ReviewPolicy(reviewers=(row + (((HYDROGEN, 'high'),),),)).version(), before)
        changed = config.parse_config(with_agent('agy', fallback_models=[
            {'model': 'gemini-4-helium-high', 'model_label': 'He'}]))
        self.assertNotEqual(changed.review_policy.version(), CFG.review_policy.version())


class UnavailabilityTests(unittest.TestCase):
    """The runner tells an unavailable model only from a failed turn, never from a valid one."""

    def run_agy(self, reply, agent=None):
        cli = FakeCLI(gemini_4_argon_high=reply)
        with tempfile.TemporaryDirectory() as tmp:
            return agents.AgentRunner(tmp, 10, run=cli).run(
                agent or CFG.agent('agy'), 'review', 'p', 'review', tmp, 'pr-1')

    def assertFails(self, reply, unavailable, agent=None):
        with self.assertRaises(agents.AgentError) as ctx:
            self.run_agy(reply, agent)
        self.assertEqual(isinstance(ctx.exception, agents.ModelUnavailable), unavailable,
                         str(ctx.exception))
        return ctx.exception

    def test_failed_turns_matching_a_pattern_are_unavailability(self):
        exc = self.assertFails((1, '', QUOTA), True)
        self.assertEqual(str(exc), 'agy exited with 1')  # the message of the plain failure
        self.assertIn('RESOURCE_EXHAUSTED', exc.reason)
        self.assertTrue(exc.log.name.endswith('.log'))
        for err in ('HTTP 429 Too Many Requests', 'Model gemini-4-argon-high is not available'):
            with self.subTest(err):
                self.assertFails((2, '', err), True)
        # An explicit error envelope counts too.
        envelope = json.dumps({'status': 'FAILED', 'response': 'Quota exceeded for the model'})
        exc = self.assertFails((0, envelope, ''), True)
        self.assertTrue(str(exc).startswith('agy status FAILED'))
        # So does an exit code the configuration lists.
        agent = dataclasses.replace(CFG.agent('agy'), unavailable_exit_codes=(75,))
        self.assertFails((75, '', 'try later'), True, agent)

    def test_other_failures_never_count_whatever_the_text(self):
        self.assertFails((1, '', 'TypeError: something broke'), False)
        self.assertFails((0, 'quota 429 RESOURCE_EXHAUSTED: not JSON', ''), False)
        schema_error = json.dumps({'status': 'SUCCESS', 'structured_output': {'quota': 429}})
        self.assertFails((0, schema_error, ''), False)
        self.assertFails((subprocess.TimeoutExpired('agy', 10), '', ''), False)
        # A valid result wins over any match.
        valid = json.dumps({'status': 'SUCCESS', 'structured_output': dict(
            raw_review(), summary='RESOURCE_EXHAUSTED quota 429 handled')})
        self.assertEqual(self.run_agy((0, valid, QUOTA))['verdict'], 'approve')

    def test_a_result_wins_over_a_match_even_with_a_non_zero_exit(self):
        valid = json.dumps({'status': 'SUCCESS', 'structured_output': raw_review()})
        schema_error = json.dumps({'status': 'SUCCESS', 'structured_output': {'quota': 429}})
        agent = dataclasses.replace(CFG.agent('agy'), unavailable_exit_codes=(75,))
        for name, reply in {'valid result': (1, valid, QUOTA),
                            'schema error': (1, schema_error, QUOTA),
                            'listed exit code': (75, valid, QUOTA)}.items():
            with self.subTest(name):
                exc = self.assertFails(reply, False, agent)
                self.assertTrue(str(exc).startswith('agy exited with'), str(exc))
        # Without a result, the match counts as before.
        no_output = json.dumps({'status': 'SUCCESS'})
        self.assertFails((1, no_output, QUOTA), True)

    def test_a_parse_error_wins_over_a_match_even_with_a_non_zero_exit(self):
        for name, out in {'not JSON': 'quota exhausted {', 'list envelope': '[]',
                          'null envelope': 'null', 'string envelope': '"quota"'}.items():
            with self.subTest(name):
                exc = self.assertFails((1, out, QUOTA), False)
                self.assertEqual(str(exc), 'agy exited with 1')
                # With a clean exit the envelope is an error too, not a crash.
                self.assertFails((0, out, QUOTA), False)

    def test_a_malformed_codex_final_message_never_counts(self):
        agent = dataclasses.replace(CFG.agent('codex'), fallback_models=(
            config.FallbackModel(CODEX_FALLBACK, 'fallback', 'high'),))
        model = agent.model.replace('-', '_').replace('.', '_')
        for name, (final, unavailable) in {'malformed': ('{"verdict": ', False),
                                           'no file': (None, True),
                                           'empty file': ('', True)}.items():
            cli = FakeCodex(**{model: (1, final, QUOTA)})
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(agents.AgentError) as ctx:
                    agents.AgentRunner(tmp, 10, run=cli).run(agent, 'review', 'p', 'review',
                                                             tmp, 'pr-1')
                self.assertEqual(isinstance(ctx.exception, agents.ModelUnavailable),
                                 unavailable, str(ctx.exception))


class FallbackTurnTests(unittest.TestCase):
    """The dispatcher reruns the turn on the next model only when the model is unavailable."""

    def make(self, runner, cfg=CFG, ws=None):
        self.gh, self.ws, self.logs = FakeGH(), ws or FakeWS(), []
        return Dispatcher(cfg, self.gh, self.ws, runner, log=self.logs.append)

    def agy_pr(self):
        p = pr()
        p.meta['reviewer'] = 'agy'  # the PR's level-1 reviewer
        self.gh.labels[100] = set(p.labels)
        return p

    def test_quota_on_argon_reruns_the_review_on_hydrogen(self):
        cli = FakeCLI(gemini_4_argon_high=(1, '', QUOTA))
        with tempfile.TemporaryDirectory() as tmp:
            d = self.make(agents.AgentRunner(tmp, 10, run=cli))
            d._do_review(self.agy_pr())
        self.assertEqual(cli.models(), [ARGON, HYDROGEN])
        self.assertNotIn('Previous attempt on another model', cli.calls[0][1])
        self.assertIn('Previous attempt on another model', cli.calls[1][1])
        body = self.gh.comments[0][1]
        self.assertIn('> **Agent:** Antigravity (CLI v1.4.2)\n> **Model:** Gemini 4 Hydrogen\n'
                      '> **Reasoning Level:** High\n', body)
        record = markdown.read_marker(body, 'review')
        self.assertEqual((record['reviewer'], record['verdict'], record['profile']['model']),
                         ('agy', 'approve', HYDROGEN))
        # The receipt still binds the configured reviewer.
        self.assertEqual(record['receipt']['profiles'][0]['model'], ARGON)
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        log = '\n'.join(self.logs)
        self.assertIn(f'agy: model {ARGON} unavailable', log)
        self.assertIn(f'agy ran on fallback model {HYDROGEN}', log)

    def test_an_implementer_on_a_fallback_records_it_everywhere(self):
        data = dict(DATA, roles={'implementer': 'agy', 'reviewers': ['codex']})
        cfg = config.parse_config(data)
        runner = FakeRunner(unavailable(), implemented(), implemented())
        seen = []
        run = runner.run
        runner.run = lambda agent, *a: (seen.append((agent.model, a[1])), run(agent, *a))[1]
        d = self.make(runner, cfg, FakeWS(tests=(False, True)))
        d._do_implement(7)
        # Argon once; Hydrogen for the first attempt and for the retry after the tests failed.
        self.assertEqual([m for m, _ in seen], [ARGON, HYDROGEN, HYDROGEN])
        self.assertIn('Previous attempt on another model', seen[1][1])
        self.assertNotIn('Previous attempt on another model', seen[2][1])
        (created,) = self.gh.created
        self.assertIn('> **Model:** Gemini 4 Hydrogen\n', created['body'])
        self.assertEqual(markdown.read_marker(created['body'], 'pr')['profile']['model'],
                         HYDROGEN)

    def test_other_errors_do_not_fall_back(self):
        cases = {'cli error': FakeCLI(gemini_4_argon_high=(1, '', 'segmentation fault')),
                 'invalid output': FakeCLI(gemini_4_argon_high=(0, 'quota: not JSON', '')),
                 'timeout': FakeCLI(gemini_4_argon_high=(subprocess.TimeoutExpired('agy', 1),
                                                         '', ''))}
        for name, cli in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                d = self.make(agents.AgentRunner(tmp, 10, run=cli))
                p = self.agy_pr()
                d.snapshot = lambda: Snapshot([p], [], [])
                self.assertEqual(d.tick(), [Action('review', 100)])
                self.assertEqual(cli.models(), [ARGON])
                self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
                self.assertNotIn(HYDROGEN, self.gh.comments[-1][1])

    def test_unparsable_output_on_a_failed_turn_does_not_fall_back(self):
        cases = {'not JSON': '{"status": ', 'list envelope': '[]', 'null envelope': 'null'}
        for name, out in cases.items():
            cli = FakeCLI(gemini_4_argon_high=(1, out, QUOTA))
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                d = self.make(agents.AgentRunner(tmp, 10, run=cli))
                p = self.agy_pr()
                d.snapshot = lambda: Snapshot([p], [], [])
                self.assertEqual(d.tick(), [Action('review', 100)])
                self.assertEqual(cli.models(), [ARGON])
                self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
                body = self.gh.comments[-1][1]
                self.assertIn('Il passo `review` è fallito: agy exited with 1.', body)
                self.assertNotIn(HYDROGEN, body)

    def test_a_malformed_codex_final_message_does_not_fall_back(self):
        cfg = config.parse_config(with_agent('codex', fallback_models=[
            {'model': CODEX_FALLBACK, 'model_label': 'fallback'}]))
        model = cfg.agent('codex').model
        cli = FakeCodex(**{model.replace('-', '_').replace('.', '_'): (1, '{"verdict": ', QUOTA)})
        with tempfile.TemporaryDirectory() as tmp:
            d = self.make(agents.AgentRunner(tmp, 10, run=cli), cfg)
            p = pr()
            self.gh.labels[100] = set(p.labels)
            d.snapshot = lambda: Snapshot([p], [], [])
            self.assertEqual(d.tick(), [Action('review', 100)])
        self.assertEqual(cli.calls, [model])
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        body = self.gh.comments[-1][1]
        self.assertIn('Il passo `review` è fallito: codex exited with 1.', body)
        self.assertNotIn(CODEX_FALLBACK, body)

    def test_every_model_unavailable_goes_to_a_human_with_the_models_tried(self):
        cli = FakeCLI(gemini_4_argon_high=(1, '', QUOTA),
                      gemini_4_hydrogen_high=(1, '', 'HTTP 429: rate limit, giving up'))
        with tempfile.TemporaryDirectory() as tmp:
            d = self.make(agents.AgentRunner(tmp, 10, run=cli))
            p = self.agy_pr()
            d.snapshot = lambda: Snapshot([p], [], [])
            d.tick()
        self.assertEqual(cli.models(), [ARGON, HYDROGEN])  # once each, no retry on a model
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        body = self.gh.comments[-1][1]
        self.assertIn('Il passo `review` è fallito: no model of `agy` was available', body)
        self.assertIn(f'- `{ARGON}`: agy exited with 1 (`RESOURCE_EXHAUSTED`', body)
        self.assertIn(f'- `{HYDROGEN}`: agy exited with 1 (`429`', body)
        self.assertIn('Log locale:', body)

    def test_another_error_on_the_fallback_lists_the_models_tried(self):
        cases = {'cli error': ((1, '', 'segmentation fault'), 'agy exited with 1'),
                 'invalid output': ((0, 'not JSON', ''), 'agy output is not JSON'),
                 'timeout': ((subprocess.TimeoutExpired('agy', 1), '', ''), 'agy timed out')}
        for name, (reply, why) in cases.items():
            cli = FakeCLI(gemini_4_argon_high=(1, '', QUOTA), gemini_4_hydrogen_high=reply)
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                d = self.make(agents.AgentRunner(tmp, 10, run=cli))
                p = self.agy_pr()
                d.snapshot = lambda: Snapshot([p], [], [])
                d.tick()
                self.assertEqual(cli.models(), [ARGON, HYDROGEN])
                self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
                body = self.gh.comments[-1][1]
                self.assertIn('Il passo `review` è fallito: the last model tried for `agy` failed',
                              body)
                self.assertIn(f'- `{ARGON}`: agy exited with 1 (`RESOURCE_EXHAUSTED`', body)
                self.assertIn(f'- `{HYDROGEN}`: {why}', body)
                self.assertIn('Log locale:', body)

    def test_an_agent_without_fallbacks_fails_as_before(self):
        runner = FakeRunner(unavailable('claude'))
        d = self.make(runner)
        d.snapshot = lambda: Snapshot([], [{'number': 7, 'title': 't', 'labels': {'agent:ready'},
                                            'labeler': 'brushtyler', 'admitted': True}], [])
        self.assertEqual(d.tick(), [Action('implement', 7)])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.gh.labels[7], {'agent:needs-operator'})
        self.assertIn('Il passo `implement` è fallito: claude exited with 1.', self.gh.comments[-1][1])
        self.assertNotIn('models tried', self.gh.comments[-1][1])

    def test_a_turn_tries_each_model_once(self):
        d = self.make(FakeRunner(unavailable()))
        models = Models(CFG.agent('agy'))
        with self.assertRaises(agents.ModelsUnavailable) as ctx:
            d._turn(models, 'review', 'p', 'review', '/wt', 'x',
                    allowed=lambda agent: not agent.fallback_for)
        self.assertEqual([m for m, _ in ctx.exception.tried], [ARGON, HYDROGEN])
        self.assertIn('not independent', ctx.exception.tried[1][1])
        self.assertEqual(models.left, [])


class FallbackIndependenceTests(Quorum):
    def test_a_fallback_that_would_break_the_pair_is_skipped(self):
        # The second reviewer's fallback is the first reviewer's very tuple: not distinct.
        first = CFG.agent(FIRST)
        second = dataclasses.replace(
            first, key=SECOND, model='other-model',
            fallback_models=(config.FallbackModel(first.model, 'same', first.effort),))
        cfg = dataclasses.replace(CFG, agents=dict(CFG.agents, **{SECOND: second}))
        self.make(raw_review(), unavailable(SECOND), cfg=cfg)
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.reviewed(), [FIRST, SECOND])  # the fallback never ran
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        self.assertIn(f'- `{first.model}`: not independent', self.gh.comments[-1][1])


class FallbackStatusTests(unittest.TestCase):
    def test_status_shows_the_fallback_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = activity.Activity(tmp, clock=lambda: 1_000_000)
            current.start('review', 100)
            current.turn('agy', '/logs/a.prompt.md', HYDROGEN)
            line = activity.describe(tmp, now=1_000_010, held=lambda path: True,
                                     alive=lambda pid: True)
            self.assertIn(f'review #100 with agy on fallback model {HYDROGEN}', line)
            current.turn('agy', '/logs/b.prompt.md')
            self.assertNotIn('fallback', activity.describe(tmp, now=1_000_010,
                                                           held=lambda path: True,
                                                           alive=lambda pid: True))


if __name__ == '__main__':
    unittest.main()
