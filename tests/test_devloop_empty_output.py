"""devloop: an agy turn that ends with no output and no error moves to the next model (#222)."""

import json
import tempfile
import unittest
from pathlib import Path

from devloop import agents, config, markdown, unavailability
from devloop.dispatcher import Action, Dispatcher, Models, Snapshot
from helpers.devloop_fakes import FakeGH
from test_devloop import CFG, FakeWS, pr
from test_devloop_agy_permissions import DENIED_ENVELOPE, JETSKI, SequenceCLI
from test_devloop_fallback import ARGON, DATA, HYDROGEN, QUOTA, FakeCLI, with_agent
from test_devloop_fallback_memory import T0, Clock

# What agy printed on 2026-11-17 for the reviews of PR #217 and PR #220 on Argon: exit 0, empty
# stdout, this stderr.
WARNING = 'warning: run ended with no output and no recorded error'
EMPTY = (0, '', WARNING + '\n')
# The same on a JSON envelope: no result, no error.
NO_RESULT = (0, json.dumps({'status': 'SUCCESS', 'response': ''}), '')


class RunnerTests(unittest.TestCase):
    """The runner tells an empty run from the other stops without output."""

    def run_agy(self, reply, agent=None):
        cli = FakeCLI(gemini_4_argon_high=reply)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(agents.AgentError) as ctx:
                agents.AgentRunner(tmp, 10, run=cli).run(agent or CFG.agent('agy'), 'review',
                                                         'p', 'review', tmp, 'pr-1')
            self.log = ctx.exception.log.read_text(encoding='utf-8')
        return ctx.exception

    def test_an_empty_run_is_no_output_not_an_unavailable_model(self):
        exc = self.run_agy(EMPTY)
        self.assertIsInstance(exc, agents.NoOutput)
        self.assertNotIsInstance(exc, agents.ModelUnavailable)
        self.assertEqual(str(exc), f'agy returned no output: {WARNING}')
        self.assertIn(f'exit 0\n--- stdout\n\n--- stderr\n{WARNING}', self.log)
        exc = self.run_agy(NO_RESULT)
        self.assertIsInstance(exc, agents.NoOutput)
        self.assertEqual(str(exc), 'agy returned no structured_output')

    def test_other_stops_are_not_empty_runs(self):
        cases = {'silent': (0, '', ''),
                 'jetski': (0, '', f'{JETSKI}\n{WARNING}'),
                 'denied actions': (0, DENIED_ENVELOPE, ''),
                 'non-zero exit': (1, '', WARNING),
                 'error envelope': (0, json.dumps({'status': 'FAILED', 'response': 'x'}), ''),
                 'output on stdout': (0, f'{WARNING}\n', '')}
        for name, reply in cases.items():
            with self.subTest(name):
                self.assertNotIsInstance(self.run_agy(reply), agents.NoOutput)
        self.assertIsNone(agents.empty_run('claude', '', WARNING))
        with self.assertRaises(agents.AgentError) as ctx:
            agents.extract_output('claude', json.dumps({'result': 'x'}), None)
        self.assertNotIsInstance(ctx.exception, agents.NoOutput)


class EmptyRunTests(unittest.TestCase):
    """The dispatcher runs the next model after an empty run, and retries the last one once."""

    def tick(self, cli, cfg=CFG, memory=None):
        self.gh, self.logs = FakeGH(), []
        with tempfile.TemporaryDirectory() as tmp:
            d = Dispatcher(cfg, self.gh, FakeWS(), agents.AgentRunner(tmp, 10, run=cli),
                           log=self.logs.append, memory=memory)
            p = pr()
            p.meta['reviewer'] = 'agy'
            self.gh.labels[100] = set(p.labels)
            d.snapshot = lambda: Snapshot([p], [], [])
            self.assertEqual(d.tick(), [Action('review', 100)])
            self.log_files = sorted(f.name for f in Path(tmp).glob('*.log'))
        return '\n'.join(self.logs)

    def test_an_empty_run_on_the_primary_falls_back_and_its_verdict_closes_the_round(self):
        for name, reply in {'empty stdout': EMPTY, 'no structured_output': NO_RESULT}.items():
            with self.subTest(name):
                cli = FakeCLI(gemini_4_argon_high=reply)
                log = self.tick(cli)
                self.assertEqual(cli.models(), [ARGON, HYDROGEN])
                self.assertIn('Previous attempt on another model', cli.calls[1][1])
                comment = self.gh.comments[0]
                record = markdown.read_marker(comment[1], 'review')
                self.assertEqual((record['verdict'], record['profile']['model']),
                                 ('approve', HYDROGEN))
                self.assertIn('> **Model:** Gemini 4 Hydrogen\n', comment[1])
                self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
                self.assertIn(f'agy: model {ARGON} returned no output; trying {HYDROGEN}', log)
                self.assertIn(f'agy ran on fallback model {HYDROGEN}', log)
                self.assertNotIn('unavailable', log)

    def test_an_empty_run_on_the_last_model_is_retried_once(self):
        cli = SequenceCLI(gemini_4_argon_high=EMPTY, gemini_4_hydrogen_high=[EMPTY])
        log = self.tick(cli)
        self.assertEqual(cli.models(), [ARGON, HYDROGEN, HYDROGEN])
        self.assertEqual(cli.calls[1][1], cli.calls[2][1])  # the same prompt
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        self.assertIn(f'agy: model {HYDROGEN} returned no output; trying {HYDROGEN} again', log)
        # Each attempt keeps its own log.
        self.assertEqual(len(self.log_files), 3)
        self.assertEqual(sum(name.endswith(f'-{HYDROGEN}-retry-agy.log')
                             for name in self.log_files), 1)

    def test_a_second_empty_run_on_the_last_model_is_a_tool_failure(self):
        cli = FakeCLI(gemini_4_argon_high=EMPTY, gemini_4_hydrogen_high=EMPTY)
        log = self.tick(cli)
        self.assertEqual(cli.models(), [ARGON, HYDROGEN, HYDROGEN])
        self.assertEqual(self.gh.labels[100], {'agent:needs-human'})
        body = self.gh.comments[-1][1]
        self.assertEqual(markdown.read_marker(body, 'notice')['class'], 'tool_failure')
        self.assertIn('Il passo `review` è fallito: the last model tried for `agy` failed', body)
        self.assertIn(f'- `{ARGON}`: agy returned no output: {WARNING}', body)
        self.assertIn(f'- `{HYDROGEN}`: agy returned no output: {WARNING}; no output again after '
                      'one retry', body)
        self.assertIn(f'agy: model {HYDROGEN} returned no output again', log)

    def test_an_agent_without_fallbacks_retries_its_model_once(self):
        cfg = config.parse_config(with_agent('agy', fallback_models=None))
        self.assertEqual(cfg.agent('agy').fallback_models, ())
        cli = FakeCLI(gemini_4_argon_high=EMPTY)
        self.tick(cli, cfg)
        self.assertEqual(cli.models(), [ARGON, ARGON])
        self.assertEqual(self.gh.labels[100], {'agent:needs-human'})
        body = self.gh.comments[-1][1]
        self.assertEqual(markdown.read_marker(body, 'notice')['class'], 'tool_failure')
        self.assertIn(f'Il passo `review` è fallito: agy returned no output: {WARNING}; no output '
                      'again after one retry', body)
        self.assertNotIn('models tried', body)

    def test_an_unavailable_primary_then_an_empty_fallback(self):
        cli = SequenceCLI(gemini_4_argon_high=(1, '', QUOTA), gemini_4_hydrogen_high=[EMPTY])
        self.tick(cli)
        self.assertEqual(cli.models(), [ARGON, HYDROGEN, HYDROGEN])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})


class MemoryTests(unittest.TestCase):
    """An empty run is never remembered as unavailability (#151)."""

    def test_an_empty_run_is_not_remembered(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, clock = Path(tmp) / 'state', Clock()
            for _ in range(2):  # the next review starts from the primary again
                gh = FakeGH()
                cli = FakeCLI(gemini_4_argon_high=EMPTY)
                memory = unavailability.Memory(state, clock=clock)
                d = Dispatcher(CFG, gh, FakeWS(), agents.AgentRunner(
                    Path(tmp) / 'logs', 10, run=cli, clock=clock), log=lambda _: None,
                    memory=memory)
                p = pr()
                p.meta['reviewer'] = 'agy'
                gh.labels[100] = set(p.labels)
                d._do_review(p)
                self.assertEqual(cli.models(), [ARGON, HYDROGEN])
                record = markdown.read_marker(gh.comments[0][1], 'review')
                self.assertEqual(record['profile']['model'], HYDROGEN)
                self.assertNotIn('skipped_models', record)
                self.assertFalse((state / unavailability.FILE).exists())
                self.assertIsNone(memory.active('agy', ARGON))
                self.assertEqual(unavailability.describe(state, now=T0), [])

    def test_a_quota_with_a_reset_is_still_remembered(self):
        # The same chain, with Argon exhausted instead of empty: recorded as before.
        with tempfile.TemporaryDirectory() as tmp:
            state, clock = Path(tmp) / 'state', Clock()
            cli = FakeCLI(gemini_4_argon_high=(3, '', 'RESOURCE_EXHAUSTED. Resets in 1h.'))
            memory = unavailability.Memory(state, clock=clock)
            d = Dispatcher(config.parse_config(DATA), FakeGH(), FakeWS(), agents.AgentRunner(
                Path(tmp) / 'logs', 10, run=cli, clock=clock), log=lambda _: None,
                memory=memory)
            d._turn(Models(CFG.agent('agy')), 'review', 'p', 'review', tmp, 'x')
            self.assertEqual(memory.active('agy', ARGON)['until'],
                             unavailability.iso(T0 + 3600))


if __name__ == '__main__':
    unittest.main()
