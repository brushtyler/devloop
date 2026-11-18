"""devloop: memory of the models found unavailable, skipped until they are back (#151)."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import devloop.__main__ as cli_main
from devloop import agents, config, markdown, unavailability
from devloop.dispatch_actions import Models, skipped_models
from devloop.dispatcher import Dispatcher
from helpers.devloop_fakes import FakeGH
from test_devloop import CFG, FakeRunner, FakeWS, implemented, pr
from test_devloop_fallback import ARGON, DATA, HYDROGEN, QUOTA, FakeCLI, unavailable, with_agent

# The real output of agy 1.5.9 on gemini-4-argon-high with its quota exhausted, without the
# error_id: exit code 3, empty stdout, this stderr.
FIXTURE_STDERR = (
    'error: Individual quota reached. Please upgrade your subscription to increase your limits. '
    'Resets in 152h4m35s.\n'
    'AGY_ERROR: {"short_error":"RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please '
    'upgrade your subscription to increase your limits. Resets in 152h4m35s.",'
    '"status":"RESOURCE_EXHAUSTED","error_code":429,"code_kind":"http","retryable":true}\n')
FIXTURE = (3, '', FIXTURE_STDERR)
FIXTURE_RESET = 152 * 3600 + 4 * 60 + 35
T0 = 1_800_000_000  # 2027-01-15T08:00:00Z


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


class ResetFormatTests(unittest.TestCase):
    def test_only_resets_in_with_h_m_s_components_is_recognised(self):
        recognised = {'Resets in 152h4m35s.': FIXTURE_RESET, 'Resets in 4m': 240,
                      'x Resets in 30s, later': 30, 'Resets in 2h': 7200,
                      'Resets in 1h30s': 3630, FIXTURE_STDERR: FIXTURE_RESET}
        for text, seconds in recognised.items():
            with self.subTest(text):
                self.assertEqual(unavailability.reset_seconds(text), seconds)
        for text in ('', 'Resets in 5 minutes', 'Resets in 4m35', 'Resets in 30sec',
                     'resets in 4m', 'Resets at 10:00', 'Resets in 4s3m', 'Resets in ',
                     'quota exhausted', 'Retry in 4m'):
            with self.subTest(text):
                self.assertIsNone(unavailability.reset_seconds(text))

    def test_the_runner_dates_the_reset_from_the_real_fixture(self):
        cli = FakeCLI(gemini_4_argon_high=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(
                agents.ModelUnavailable) as ctx:
            agents.AgentRunner(tmp, 10, run=cli, clock=Clock()).run(
                CFG.agent('agy'), 'review', 'p', 'review', tmp, 'pr-1')
        exc = ctx.exception
        self.assertEqual(str(exc), 'agy exited with 3')
        self.assertEqual(exc.resets_at, T0 + FIXTURE_RESET)
        self.assertIn('RESOURCE_EXHAUSTED', exc.reason)
        # Without a recognised reset the field is empty: the dispatcher uses its default.
        cli = FakeCLI(gemini_4_argon_high=(1, '', QUOTA))
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(
                agents.ModelUnavailable) as ctx:
            agents.AgentRunner(tmp, 10, run=cli, clock=Clock()).run(
                CFG.agent('agy'), 'review', 'p', 'review', tmp, 'pr-1')
        self.assertIsNone(ctx.exception.resets_at)


class MemoryConfigTests(unittest.TestCase):
    def test_fallback_memory_seconds_defaults_to_an_hour_and_is_validated(self):
        self.assertEqual(CFG.agent('agy').fallback_memory_seconds, 3600)
        cfg = config.parse_config(with_agent('agy', fallback_memory_seconds=120))
        self.assertEqual(cfg.agent('agy').fallback_memory_seconds, 120)
        for value in (0, -5, '60', True, 1.5):
            with self.subTest(value), self.assertRaises(config.ConfigError):
                config.parse_config(with_agent('agy', fallback_memory_seconds=value))
        self.assertIn('fallback_memory_seconds', config.OVERLAY_KEYS['agents']['*'])


class MemoryTests(unittest.TestCase):
    """The dispatcher skips a remembered model until its time, with the real fixture."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state'
        self.clock = Clock()

    def tearDown(self):
        self.tmp.cleanup()

    def memory(self):
        # A new instance each time: the memory lives in the file, as across a restart.
        return unavailability.Memory(self.state, clock=self.clock)

    def make(self, runner, cfg=CFG):
        self.gh, self.logs = FakeGH(), []
        return Dispatcher(cfg, self.gh, FakeWS(), runner, log=self.logs.append,
                          memory=self.memory())

    def review(self, cli, cfg=CFG):
        """One review of a PR whose reviewer is agy, by a new dispatcher (a restart)."""
        d = self.make(agents.AgentRunner(Path(self.tmp.name) / 'logs', 10, run=cli,
                                         clock=self.clock), cfg)
        p = pr()
        p.meta['reviewer'] = 'agy'
        self.gh.labels[100] = set(p.labels)
        d._do_review(p)
        return markdown.read_marker(self.gh.comments[0][1], 'review')

    def test_a_recognised_reset_skips_the_primary_until_then_across_restarts(self):
        cli = FakeCLI(gemini_4_argon_high=FIXTURE)
        self.review(cli)
        self.assertEqual(cli.models(), [ARGON, HYDROGEN])
        until = unavailability.iso(T0 + FIXTURE_RESET)
        self.assertIn(f'agy: model {ARGON} unavailable: `RESOURCE_EXHAUSTED`', self.logs[0])
        self.assertIn(f'skipped until {until}', self.logs[0])
        saved = json.loads((self.state / unavailability.FILE).read_text())
        self.assertEqual(saved['agy'][ARGON]['until'], until)
        self.assertEqual(saved['agy'][ARGON]['since'], unavailability.iso(T0))
        self.assertIn('RESOURCE_EXHAUSTED', saved['agy'][ARGON]['reason'])

        # A day later, after a restart: the turn starts from Hydrogen and says why.
        self.clock.now = T0 + 86400
        cli = FakeCLI(gemini_4_argon_high=FIXTURE)
        record = self.review(cli)
        self.assertEqual(cli.models(), [HYDROGEN])
        self.assertNotIn('Previous attempt on another model', cli.calls[0][1])
        log = '\n'.join(self.logs)
        self.assertIn(f'agy: model {ARGON} skipped: unavailable until {until} '
                      '(`RESOURCE_EXHAUSTED`', log)
        self.assertEqual(record['profile']['model'], HYDROGEN)
        (skipped,) = record['skipped_models']
        self.assertEqual((skipped['model'], skipped['until']), (ARGON, until))
        self.assertIn('RESOURCE_EXHAUSTED', skipped['reason'])
        (line,) = unavailability.describe(self.state, now=self.clock.now)
        self.assertIn(f'agy skips model {ARGON} until {until}', line)
        self.assertIn('RESOURCE_EXHAUSTED', line)

        # At the reset the primary is tried again; it answers and is forgotten.
        self.clock.now = T0 + FIXTURE_RESET
        cli = FakeCLI()
        record = self.review(cli)
        self.assertEqual(cli.models(), [ARGON])
        self.assertNotIn('skipped_models', record)
        self.assertFalse((self.state / unavailability.FILE).exists())
        self.assertEqual(unavailability.describe(self.state, now=self.clock.now), [])

    def test_the_real_quota_error_falls_back_to_hydrogen_citing_its_line(self):
        # #148: the first real exhaustion of Argon's quota, exit 3 and the stderr above.
        agy = CFG.agent('agy')
        line = FIXTURE_STDERR.splitlines()[1]
        self.assertTrue(line.startswith('AGY_ERROR: {"short_error":"RESOURCE_EXHAUSTED'))
        reason = f'`RESOURCE_EXHAUSTED` in "{line[:200]}"'
        self.assertEqual(agents.unavailable_reason(agy, 3, f'\n{FIXTURE_STDERR}'), reason)
        self.assertNotIn(3, agy.unavailable_exit_codes)  # the pattern decides, not the code
        # The control: another error with the same exit code is no unavailability.
        self.assertIsNone(agents.unavailable_reason(
            agy, 3, '\nerror: failed to read the workspace settings\n'))
        # A fake agy exiting 3 with that stderr: the turn reruns on Hydrogen.
        cli = FakeCLI(gemini_4_argon_high=FIXTURE)
        self.review(cli)
        self.assertEqual(cli.models(), [ARGON, HYDROGEN])
        self.assertIn('Previous attempt on another model', cli.calls[1][1])
        self.assertIn(f'agy: model {ARGON} unavailable: {reason}', self.logs[0])
        self.assertIn(f'agy ran on fallback model {HYDROGEN}', '\n'.join(self.logs))
        self.assertEqual(self.memory().active('agy', ARGON)['reason'], reason)
        # The next turn skips Argon and its marker records the same reason.
        self.clock.now = T0 + 60
        record = self.review(FakeCLI(gemini_4_argon_high=FIXTURE))
        self.assertEqual(record['profile']['model'], HYDROGEN)
        self.assertEqual(record['skipped_models'],
                         [{'model': ARGON, 'reason': reason,
                           'until': unavailability.iso(T0 + FIXTURE_RESET)}])

    def test_without_a_reset_the_memory_lasts_fallback_memory_seconds(self):
        cfg = config.parse_config(with_agent('agy', fallback_memory_seconds=600))
        cli = FakeCLI(gemini_4_argon_high=(1, '', QUOTA))
        self.review(cli, cfg)
        self.assertEqual(cli.models(), [ARGON, HYDROGEN])
        until = unavailability.iso(T0 + 600)
        self.assertEqual(self.memory().active('agy', ARGON)['until'], until)

        self.clock.now = T0 + 599
        cli = FakeCLI(gemini_4_argon_high=(1, '', QUOTA))
        self.review(cli, cfg)
        self.assertEqual(cli.models(), [HYDROGEN])

        # Expired: the primary is tried again, and a new error renews the memory.
        self.clock.now = T0 + 600
        self.assertIsNone(self.memory().active('agy', ARGON))
        cli = FakeCLI(gemini_4_argon_high=(1, '', QUOTA))
        self.review(cli, cfg)
        self.assertEqual(cli.models(), [ARGON, HYDROGEN])
        self.assertEqual(self.memory().active('agy', ARGON)['until'],
                         unavailability.iso(T0 + 1200))

    def test_the_default_is_an_hour(self):
        d = self.make(FakeRunner(unavailable(), {'status': 'ok'}))
        d._turn(Models(CFG.agent('agy')), 'review', 'p', 'review', '/wt', 'x')
        self.assertEqual(self.memory().active('agy', ARGON)['until'],
                         unavailability.iso(T0 + 3600))

    def test_an_implementer_records_the_skip_in_the_pr_marker(self):
        cfg = config.parse_config(dict(DATA, roles={'implementer': 'agy',
                                                    'reviewers': ['codex']}))
        self.memory().record('agy', ARGON, 'quota', T0 + 60)
        runner = FakeRunner(implemented())
        seen = []
        run = runner.run
        runner.run = lambda agent, *a: (seen.append(agent.model), run(agent, *a))[1]
        self.make(runner, cfg)._do_implement(7)
        self.assertEqual(seen, [HYDROGEN])
        (created,) = self.gh.created
        meta = markdown.read_marker(created['body'], 'pr')
        self.assertEqual(meta['profile']['model'], HYDROGEN)
        self.assertEqual(meta['skipped_models'], [{'model': ARGON, 'reason': 'quota',
                                                   'until': unavailability.iso(T0 + 60)}])

    def test_a_chain_remembered_whole_runs_no_cli_until_the_resets(self):
        memory = self.memory()
        memory.record('agy', ARGON, 'quota', T0 + 60)
        memory.record('agy', HYDROGEN, 'limit', T0 + 120)
        runner = FakeRunner({'status': 'ok'})
        d = self.make(runner)
        with self.assertRaises(agents.ModelsUnavailable) as ctx:
            d._turn(Models(CFG.agent('agy')), 'review', 'p', 'review', '/wt', 'x')
        self.assertEqual(runner.calls, [])
        self.assertEqual(ctx.exception.tried, [
            (ARGON, f'skipped: unavailable until {unavailability.iso(T0 + 60)} (quota)'),
            (HYDROGEN, f'skipped: unavailable until {unavailability.iso(T0 + 120)} (limit)')])
        self.assertIn('no model of `agy` was available', str(ctx.exception))
        # Once the first reset has passed, the primary is tried again; Hydrogen stays skipped.
        self.clock.now = T0 + 60
        runner = FakeRunner({'status': 'ok'})
        output, used = self.make(runner)._turn(Models(CFG.agent('agy')), 'review', 'p',
                                               'review', '/wt', 'x')
        self.assertEqual((output, used.model, used.skipped), ({'status': 'ok'}, ARGON, ()))
        self.assertEqual(len(runner.calls), 1)
        self.assertIsNone(self.memory().active('agy', ARGON))
        self.assertIsNotNone(self.memory().active('agy', HYDROGEN))

    def test_a_chain_found_unavailable_whole_is_not_run_again_before_the_resets(self):
        # Both models of the chain fail, Argon with the real fixture and Hydrogen without a
        # reset: a second later, after a restart, the next turn runs no CLI at all.
        cfg = config.parse_config(with_agent('agy', fallback_memory_seconds=600))
        replies = {'gemini_4_argon_high': FIXTURE, 'gemini_4_hydrogen_high': (1, '', QUOTA)}
        cli = FakeCLI(**replies)
        runner = agents.AgentRunner(Path(self.tmp.name) / 'logs', 10, run=cli, clock=self.clock)
        with self.assertRaises(agents.ModelsUnavailable):
            self.make(runner, cfg)._turn(Models(cfg.agent('agy')), 'review', 'p', 'review',
                                         self.tmp.name, 'x')
        self.assertEqual(cli.models(), [ARGON, HYDROGEN])

        self.clock.now = T0 + 1
        cli = FakeCLI(**replies)
        runner = agents.AgentRunner(Path(self.tmp.name) / 'logs', 10, run=cli, clock=self.clock)
        with self.assertRaises(agents.ModelsUnavailable) as ctx:
            self.make(runner, cfg)._turn(Models(cfg.agent('agy')), 'review', 'p', 'review',
                                         self.tmp.name, 'x')
        self.assertEqual(cli.models(), [])
        (argon, why_argon), (hydrogen, why_hydrogen) = ctx.exception.tried
        self.assertEqual((argon, hydrogen), (ARGON, HYDROGEN))
        self.assertIn(f'until {unavailability.iso(T0 + FIXTURE_RESET)}', why_argon)
        self.assertIn('RESOURCE_EXHAUSTED', why_argon)
        self.assertIn(f'until {unavailability.iso(T0 + 600)}', why_hydrogen)
        log = '\n'.join(self.logs)
        self.assertIn(f'agy: model {HYDROGEN} skipped: unavailable until '
                      f'{unavailability.iso(T0 + 600)}', log)

    def test_a_failure_after_a_skip_lists_the_skipped_model(self):
        self.memory().record('agy', ARGON, 'quota', T0 + 60)
        d = self.make(FakeRunner(agents.AgentError('agy exited with 1')))
        with self.assertRaises(agents.ModelsUnavailable) as ctx:
            d._turn(Models(CFG.agent('agy')), 'review', 'p', 'review', '/wt', 'x')
        self.assertEqual(ctx.exception.tried[0],
                         (ARGON, f'skipped: unavailable until {unavailability.iso(T0 + 60)} '
                                 '(quota)'))
        self.assertEqual(ctx.exception.tried[1], (HYDROGEN, 'agy exited with 1'))

    def test_agents_without_fallbacks_remember_nothing(self):
        # Even a record left in the file does not change their turns.
        self.memory().record('claude', CFG.agent('claude').model, 'quota', T0 + 60)
        runner = FakeRunner(unavailable('claude'))
        d = self.make(runner)
        with self.assertRaises(agents.ModelUnavailable):
            d._turn(Models(CFG.agent('claude')), 'write', 'p', 'implement', '/wt', 'x')
        self.assertEqual(len(runner.calls), 1)
        saved = json.loads((self.state / unavailability.FILE).read_text())
        self.assertEqual(saved['claude'][CFG.agent('claude').model]['until'],
                         unavailability.iso(T0 + 60))  # untouched
        d = self.make(FakeRunner({'status': 'ok'}))
        output, used = d._turn(Models(CFG.agent('claude')), 'write', 'p', 'implement', '/wt',
                               'x')
        self.assertEqual((used.model, used.skipped), (CFG.agent('claude').model, ()))

    def test_without_a_memory_turns_start_from_the_primary(self):
        runner = FakeRunner(unavailable(), {'status': 'ok'})
        d = Dispatcher(CFG, FakeGH(), FakeWS(), runner, log=lambda m: None)
        _, used = d._turn(Models(CFG.agent('agy')), 'review', 'p', 'review', '/wt', 'x')
        self.assertEqual((used.model, skipped_models(used)), (HYDROGEN, {}))
        self.assertEqual(len(runner.calls), 2)

    def test_records_are_per_agent_and_model(self):
        memory = self.memory()
        memory.record('agy', ARGON, 'quota', T0 + 60)
        memory.record('other', ARGON, 'quota', T0 + 120)
        self.assertEqual(self.memory().active('other', ARGON)['until'],
                         unavailability.iso(T0 + 120))
        self.assertIsNone(self.memory().active('agy', HYDROGEN))
        self.assertEqual(self.memory().reset('agy'), [ARGON])
        self.assertIsNone(self.memory().active('agy', ARGON))
        self.assertIsNotNone(self.memory().active('other', ARGON))

    def test_an_unreadable_file_counts_as_empty(self):
        self.state.mkdir(parents=True)
        for content in ('not json', '[]', '{"agy": {"m": {"until": "soon"}}}'):
            with self.subTest(content):
                (self.state / unavailability.FILE).write_text(content)
                self.assertEqual(self.memory().entries(), [])
                self.assertIsNone(self.memory().active('agy', 'm'))


class FixMarkerTests(unittest.TestCase):
    def test_the_fix_marker_keeps_the_skipped_models(self):
        skipped = [{'model': ARGON, 'reason': 'quota', 'until': '2027-01-15T08:01:00Z'}]
        fix = {'summary': 's', 'responses': [], 'head_sha': 'a' * 40}
        self.assertNotIn('skipped_models',
                         markdown.read_marker(markdown.render_fix(fix, 'h', 1), 'fix'))
        record = markdown.read_marker(
            markdown.render_fix(dict(fix, skipped_models=skipped), 'h', 1), 'fix')
        self.assertEqual(record['skipped_models'], skipped)


class ResetCommandTests(unittest.TestCase):
    def run_main(self, root, *argv):
        out = io.StringIO()
        with mock.patch.object(cli_main, 'main_checkout', return_value=root), \
                contextlib.redirect_stdout(out):
            cli_main.main(list(argv))
        return out.getvalue()

    def test_reset_forgets_the_agent_records_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / CFG.state_dir
            memory = unavailability.Memory(state)
            far = memory.now() + 86400
            memory.record('agy', ARGON, 'quota', far)
            memory.record('agy', HYDROGEN, 'quota', far)
            memory.record('codex', ARGON, 'quota', far)
            out = self.run_main(root, 'fallback', 'reset', 'agy')
            self.assertIn(f'agy: forgot the unavailability of {ARGON}, {HYDROGEN}', out)
            memory = unavailability.Memory(state)
            self.assertEqual([(k, m) for k, m, _ in memory.entries()], [('codex', ARGON)])
            out = self.run_main(root, 'fallback', 'reset', 'agy')
            self.assertIn('agy: no model remembered unavailable', out)
            with self.assertRaises(SystemExit) as ctx:
                self.run_main(root, 'fallback', 'reset', 'nobody')
            self.assertIn('unknown agent', str(ctx.exception.code))


if __name__ == '__main__':
    unittest.main()
