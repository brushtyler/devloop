"""Issue #164 (part A of #14): shared cost telemetry schema, adapters, ledger and report."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ssdlc_telemetry as t
from ssdlc_recovery import observed_tokens

# Real agy 1.5.9 envelopes from the devloop logs of 2026-11-16 (issue #164), conversation_id omitted.
AGY_ONE = {"status": "SUCCESS", "num_turns": 1, "usage": {"input_tokens": 68991, "output_tokens": 1147,
           "thinking_tokens": 536, "cache_read_tokens": 88505, "total_tokens": 70138}}
AGY_TWO = {"status": "SUCCESS", "usage": {"input_tokens": 131639, "output_tokens": 14379, "thinking_tokens": 11333,
           "cache_read_tokens": 974669, "total_tokens": 146018}}
CLAUDE = {"type": "result", "subtype": "success", "is_error": False, "result": "claude answer", "num_turns": 3,
          "total_cost_usd": 0.4213, "usage": {"input_tokens": 12, "cache_creation_input_tokens": 3000,
                                              "cache_read_input_tokens": 45000, "output_tokens": 800}}
CODEX_EVENTS = [{'type': 'thread.started', 'thread_id': 't'}, {'type': 'turn.started'},
                {'type': 'item.completed', 'item': {'id': 'i1', 'type': 'agent_message', 'text': 'final answer'}},
                {'type': 'turn.completed', 'usage': {'input_tokens': 1000, 'cached_input_tokens': 600,
                                                     'output_tokens': 300, 'reasoning_output_tokens': 120}},
                {'type': 'turn.completed', 'usage': {'input_tokens': 500, 'cached_input_tokens': 400,
                                                     'output_tokens': 100}}]


def cli(stdout, code=0, stderr=''):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def row(engine, telemetry=None, **extra):
    """A ledger row as CostAndTelemetryEntry writes it."""
    from orchestrator import CostAndTelemetryEntry
    return CostAndTelemetryEntry('03-dev', 'Role', engine, 'm', 'GENERATED', 1.0, telemetry=telemetry, **extra).to_dict()


class SchemaTests(unittest.TestCase):
    def test_four_measures_with_unit_source_availability_and_provenance(self):
        record = t.from_claude(CLAUDE)
        self.assertEqual((record['schema'], record['schema_version']), ('ssdlc.cost-telemetry', 1))
        self.assertEqual(set(record) >= {'spend', 'estimate', 'tokens', 'quota_snapshots', 'route', 'billing'}, True)
        for key in ('spend', 'estimate', 'tokens'):
            self.assertIn(record[key]['availability'], t.AVAILABILITY)
            self.assertIn('unit', record[key])
            self.assertIn('source', record[key])
            self.assertTrue(record[key]['observed_at'])
        self.assertEqual(record['estimate'], dict(record['estimate'], availability='estimated', amount=0.4213,
                         unit='USD', basis='api_equivalent', source='claude_cli.total_cost_usd'))
        self.assertEqual(record['spend']['availability'], 'unknown')
        self.assertEqual(record['tokens']['unit'], 'tokens')
        # Quota snapshots: a versioned container, empty and unavailable until part C.
        self.assertEqual(record['quota_snapshots'], {'availability': 'unknown', 'schema_version': 1,
                                                     'status': 'not_collected', 'snapshots': []})
        # Route and billing: never from the engine label or an API key in the environment.
        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'secret-key', 'OPENROUTER_API_KEY': 'other-secret'}):
            record = t.from_claude(CLAUDE)
        self.assertEqual(record['route'], {'value': None, 'provenance': 'unknown'})
        self.assertEqual(record['billing'], {'value': None, 'provenance': 'unknown'})
        self.assertNotIn('secret', json.dumps(record))
        self.assertEqual(t.from_openrouter({'prompt_tokens': 1})['route'],
                         {'value': 'openrouter_api', 'provenance': 'verified_signal'})

    def test_unknown_is_not_zero(self):
        # Absent cost: unknown, not 0. An explicit zero is a reported value.
        absent = t.from_claude(dict(CLAUDE, total_cost_usd=None))
        self.assertEqual((absent['estimate']['availability'], absent['estimate']['amount']), ('unknown', None))
        missing = t.from_claude({k: v for k, v in CLAUDE.items() if k != 'total_cost_usd'})
        self.assertEqual(missing['estimate']['amount'], None)
        zero = t.from_claude(dict(CLAUDE, total_cost_usd=0))
        self.assertEqual((zero['estimate']['availability'], zero['estimate']['amount']), ('estimated', 0))
        for bad in (-1, float('nan'), float('inf'), True, '0.1'):
            with self.subTest(bad=bad):
                self.assertEqual(t.from_claude(dict(CLAUDE, total_cost_usd=bad))['estimate']['availability'], 'unknown')
        # Tokens: an absent field is None, an explicit 0 is 0.
        tokens = t.from_agy({'usage': {'input_tokens': 0, 'output_tokens': 5}})['tokens']
        self.assertEqual(tokens['counts'], {'input': 0, 'cached_input': None, 'cache_creation': None,
                                            'output': 5, 'reasoning': None})
        none = t.from_agy({'status': 'SUCCESS'})['tokens']
        self.assertEqual((none['availability'], set(none['counts'].values())), ('unknown', {None}))
        # Spend: OpenRouter's explicit zero charge is reported; an absent one is unknown.
        self.assertEqual(t.from_openrouter({'cost': 0})['spend']['availability'], 'reported')
        absent_charge = t.from_openrouter({})['spend']
        self.assertEqual((absent_charge['availability'], absent_charge['amount']), ('unknown', None))
        # The validator refuses a zero passed off as unknown, and an unknown passed off as zero.
        record = t.record('codex')
        for key, measure in (('spend', dict(record['spend'], amount=0)),
                             ('estimate', dict(record['estimate'], availability='estimated', amount=None)),
                             ('tokens', dict(record['tokens'], availability='reported'))):
            with self.subTest(key=key), self.assertRaises(ValueError):
                t.validate(dict(record, **{key: measure}))
        # Crashes and incomplete calls are unknown in every measure.
        crashed = t.incomplete('claude')
        self.assertEqual(crashed['outcome'], 'incomplete')
        self.assertTrue(all(crashed[k]['availability'] == 'unknown' for k in ('spend', 'estimate', 'tokens')))
        with self.assertRaises(ValueError):
            t.validate(dict(crashed, estimate=t.estimate(0.1, 'x')))


class OverlapTests(unittest.TestCase):
    def test_agy_cache_and_thinking_are_separate_categories(self):
        tokens = t.from_agy(AGY_ONE)['tokens']
        self.assertEqual(tokens['counts'], {'input': 68991, 'cached_input': 88505, 'cache_creation': None,
                                            'output': 1147, 'reasoning': 536})
        self.assertEqual((tokens['scope'], tokens['turns'], tokens['overlaps']), ('cumulative', 1, []))
        # total_tokens (input + output) is not stored: the four categories add without overlap.
        self.assertEqual(t.token_total(tokens), 68991 + 88505 + 1147 + 536)
        self.assertEqual(t.token_total(t.from_agy(AGY_TWO)['tokens']), 131639 + 974669 + 14379 + 11333)

    def test_codex_cached_input_and_reasoning_are_not_counted_twice(self):
        record = t.from_codex([e['usage'] for e in CODEX_EVENTS if e['type'] == 'turn.completed'])
        tokens = record['tokens']
        # The second turn does not report reasoning: its share is unknown, so is the field.
        self.assertEqual(tokens['counts'], {'input': 1500, 'cached_input': 1000, 'cache_creation': None,
                                            'output': 400, 'reasoning': None})
        self.assertEqual((tokens['scope'], tokens['turns']), ('per_turn', 2))
        self.assertEqual(tokens['overlaps'], [['cached_input', 'input'], ['reasoning', 'output']])
        self.assertEqual(t.token_total(tokens), 1500 + 400)
        complete = t.from_codex([{'input_tokens': 10, 'output_tokens': 5, 'reasoning_output_tokens': 2},
                                 {'input_tokens': 20, 'output_tokens': 6, 'reasoning_output_tokens': 0}])
        self.assertEqual(complete['tokens']['counts']['reasoning'], 2)

    def test_codex_field_missing_in_one_turn_is_unknown_not_a_partial_sum(self):
        tokens = t.from_codex([{'input_tokens': 100, 'output_tokens': 10}, {'output_tokens': 20}])['tokens']
        self.assertEqual(tokens['counts'], {'input': None, 'cached_input': None, 'cache_creation': None,
                                            'output': 30, 'reasoning': None})
        self.assertEqual(tokens['availability'], 'reported')
        self.assertIsNone(t.token_total(tokens))
        # A turn without any usage leaves every field unknown for the whole call.
        none = t.from_codex([{'input_tokens': 100, 'output_tokens': 10}, {}])['tokens']
        self.assertEqual((none['availability'], set(none['counts'].values())), ('unknown', {None}))
        self.assertEqual(t.from_codex([])['tokens']['availability'], 'unknown')

    def test_claude_cache_reads_and_creation_are_separate_and_thinking_is_in_output(self):
        tokens = t.from_claude(CLAUDE)['tokens']
        self.assertEqual(tokens['counts'], {'input': 12, 'cached_input': 45000, 'cache_creation': 3000,
                                            'output': 800, 'reasoning': None})
        self.assertEqual((tokens['scope'], tokens['turns']), ('cumulative', 3))
        self.assertEqual(t.token_total(tokens), 12 + 45000 + 3000 + 800)
        # A field the source defines but did not report leaves the total unknown, not partial.
        partial = copy.deepcopy(CLAUDE); del partial['usage']['cache_creation_input_tokens']
        self.assertIsNone(t.token_total(t.from_claude(partial)['tokens']))

    def test_openrouter_details_are_inside_prompt_and_completion(self):
        tokens = t.from_openrouter({'prompt_tokens': 900, 'completion_tokens': 100, 'cost': 0.01,
                                    'prompt_tokens_details': {'cached_tokens': 800},
                                    'completion_tokens_details': {'reasoning_tokens': 60}})['tokens']
        self.assertEqual(t.token_total(tokens), 1000)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        from orchestrator import EngineRunner
        self.runner = EngineRunner
        self.container = patch.object(EngineRunner, '_should_spawn_role_container', return_value=False)
        self.container.start(); self.addCleanup(self.container.stop)

    def run_with(self, fn, completed, *args, **kwargs):
        calls = []
        def fake(cmd, **_):
            calls.append(cmd)
            return completed
        with patch.object(self.runner, '_run_role_command', side_effect=fake):
            return fn(*args, **kwargs), calls

    def test_codex_json_collects_tokens_without_changing_answer_or_errors(self):
        stdout = '\n'.join(json.dumps(e) for e in CODEX_EVENTS) + '\n'
        result, calls = self.run_with(self.runner.run_codex, cli(stdout, 0, 'tool transcript'), 'prompt', self.root)
        self.assertIn('--json', calls[0])
        self.assertEqual(tuple(result), ('final answer', 0))
        self.assertEqual(result.telemetry['provider'], 'codex')
        self.assertEqual(result.telemetry['tokens']['counts']['input'], 1500)
        self.assertEqual(result.telemetry['estimate']['availability'], 'unknown')
        self.assertEqual(result.reported_tokens, {'input_tokens': 1500, 'output_tokens': 400})
        failed = json.dumps({'type': 'turn.failed', 'error': {'message': 'usage limit reached'}}) + '\n'
        result, _ = self.run_with(self.runner.run_codex, cli(failed, 1, 'stderr'), 'prompt', self.root)
        self.assertEqual(tuple(result), ('usage limit reached\nstderr', 1))
        self.assertEqual(result.telemetry['outcome'], 'incomplete')
        result, _ = self.run_with(self.runner.run_codex, cli('plain text\n'), 'prompt', self.root)
        self.assertEqual(tuple(result), ('plain text', 0))
        self.assertEqual(result.telemetry['tokens']['availability'], 'unknown')
        # A turn.completed without usage is a turn whose tokens are unknown, not a missing turn.
        bare = stdout + json.dumps({'type': 'turn.completed'}) + '\n'
        result, _ = self.run_with(self.runner.run_codex, cli(bare), 'prompt', self.root)
        self.assertEqual(tuple(result), ('final answer', 0))
        self.assertEqual(result.telemetry['tokens']['availability'], 'unknown')
        # invoke_role keeps its five values and carries the telemetry.
        result, _ = self.run_with(self.runner.invoke_role, cli(stdout), 'codex', 'm', 's', 'u', self.root, 'P', 'T', '03-dev')
        self.assertEqual(tuple(result), ('final answer', 0.0, 0, 0, 'P:T:03-dev'))
        self.assertEqual(result.telemetry['tokens']['counts']['output'], 400)

    def test_agy_json_collects_tokens_without_changing_answer_or_errors(self):
        envelope = dict(AGY_ONE, response='agy answer', conversation_id='conv-secret-id')
        result, calls = self.run_with(self.runner.run_agy, cli(json.dumps(envelope), 0, 'note'), 'prompt', self.root)
        self.assertEqual(calls[0][calls[0].index('--output-format') + 1], 'json')
        # The answer is the envelope's response, followed by stderr as before.
        self.assertEqual(tuple(result), ('agy answer\nnote', 0))
        self.assertEqual(result.telemetry['tokens']['counts']['reasoning'], 536)
        self.assertEqual(result.telemetry['estimate']['availability'], 'unknown')
        self.assertEqual(result.telemetry['spend']['availability'], 'unknown')
        self.assertNotIn('conv-secret-id', json.dumps(result.telemetry))
        self.assertEqual(observed_tokens('agy', 'run_agy', result), {'input_tokens': 68991, 'output_tokens': 1147})
        # Output that is not an envelope, and a failed status, are returned as the CLI printed them.
        result, _ = self.run_with(self.runner.run_agy, cli('plain answer\n', 0, ''), 'prompt', self.root)
        self.assertEqual(tuple(result), ('plain answer', 0))
        self.assertEqual((result.telemetry['outcome'], result.telemetry['tokens']['availability']), ('completed', 'unknown'))
        error = json.dumps({'status': 'ERROR', 'response': 'quota exhausted', 'usage': AGY_ONE['usage']})
        result, _ = self.run_with(self.runner.run_agy, cli(error, 0, ''), 'prompt', self.root)
        self.assertEqual(tuple(result), (error, 0))
        self.assertEqual(result.telemetry['outcome'], 'incomplete')
        self.assertEqual(observed_tokens('agy', 'run_agy', result), {'input_tokens': None, 'output_tokens': None})
        result, _ = self.run_with(self.runner.run_agy, cli('', 2, 'crash'), 'prompt', self.root)
        self.assertEqual(tuple(result), ('crash', 2))
        self.assertEqual(result.telemetry['outcome'], 'incomplete')
        with self.assertRaisesRegex(RuntimeError, 'agy exited with code 2:\ncrash'):
            self.run_with(self.runner.invoke_role, cli('', 2, 'crash'), 'agy', 'm', 's', 'u', self.root, 'P', 'T', '03-dev')
        result, _ = self.run_with(self.runner.invoke_role, cli(json.dumps(envelope)), 'agy', 'm', 's', 'u',
                                  self.root, 'P', 'T', '03-dev')
        self.assertEqual(tuple(result), ('agy answer', 0.0, 0, 0, 'P:T:03-dev'))
        self.assertEqual(result.telemetry['tokens']['counts']['input'], 68991)

    def test_claude_labels_total_cost_as_an_estimate(self):
        result, _ = self.run_with(self.runner.run_claude, cli(json.dumps(CLAUDE)), 'prompt', self.root)
        # The tuple callers unpack is unchanged.
        self.assertEqual(tuple(result), ('claude answer', 0, 0.4213, 45012, 800))
        self.assertEqual(result.telemetry['estimate']['availability'], 'estimated')
        self.assertEqual(result.telemetry['estimate']['basis'], 'api_equivalent')
        self.assertEqual(result.telemetry['spend']['availability'], 'unknown')
        self.assertEqual(observed_tokens('claude', 'run_claude', result), {'input_tokens': 45012, 'output_tokens': 800})
        result, _ = self.run_with(self.runner.invoke_role, cli(json.dumps(CLAUDE)), 'claude', 'm', 's', 'u',
                                  self.root, 'P', 'T', '03-dev')
        self.assertEqual(tuple(result), ('claude answer', 0.4213, 45012, 800, 'P:T:03-dev'))
        self.assertEqual(result.telemetry['estimate']['amount'], 0.4213)
        self.assertEqual(observed_tokens('claude', 'invoke_role', result), {'input_tokens': 45012, 'output_tokens': 800})
        # An explicit OpenRouter route of the invocation is explicit configuration.
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'k'}):
            result, _ = self.run_with(self.runner.run_claude, cli(json.dumps(CLAUDE)), 'prompt', self.root,
                                      use_openrouter=True)
        self.assertEqual(result.telemetry['route'], {'value': 'openrouter_api', 'provenance': 'explicit_config'})
        result, _ = self.run_with(self.runner.run_claude, cli(json.dumps(CLAUDE), 1), 'prompt', self.root)
        self.assertEqual(result.telemetry['outcome'], 'incomplete')
        self.assertEqual(result.telemetry['estimate']['availability'], 'unknown')

    def test_claude_openrouter_route_follows_the_container_command(self):
        (self.root / '.ssdlc').mkdir()
        self.container.stop()
        container = patch.object(self.runner, '_should_spawn_role_container', return_value=True)
        container.start(); self.addCleanup(container.stop)
        ambient = {k: v for k, v in os.environ.items()
                   if k not in ('ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY')}
        # The container reads its variables from the Docker command, not from the local env
        # the invocation configured: it does not receive the OpenRouter route.
        with patch.dict('os.environ', dict(ambient, OPENROUTER_API_KEY='k'), clear=True):
            result, calls = self.run_with(self.runner.run_claude, cli(json.dumps(CLAUDE)), 'prompt', self.root,
                                          use_openrouter=True)
        docker = calls[0]
        self.assertEqual(docker[:2], ['docker', 'run'])
        self.assertNotIn('ANTHROPIC_BASE_URL', self.runner._container_env(docker))
        self.assertEqual(result.telemetry['route'], {'value': None, 'provenance': 'unknown'})
        # When the Docker command does pass the OpenRouter base URL, the route is attested.
        with patch.dict('os.environ', dict(ambient, OPENROUTER_API_KEY='k',
                                           ANTHROPIC_BASE_URL='https://openrouter.ai/api'), clear=True):
            result, calls = self.run_with(self.runner.run_claude, cli(json.dumps(CLAUDE)), 'prompt', self.root,
                                          use_openrouter=True)
        self.assertEqual(self.runner._container_env(calls[0])['ANTHROPIC_BASE_URL'], 'https://openrouter.ai/api')
        self.assertEqual(result.telemetry['route'], {'value': 'openrouter_api', 'provenance': 'explicit_config'})
        # Without the explicit request, an ambient base URL is not a route of this invocation.
        with patch.dict('os.environ', dict(ambient, OPENROUTER_API_KEY='k',
                                           ANTHROPIC_BASE_URL='https://openrouter.ai/api'), clear=True):
            result, _ = self.run_with(self.runner.run_claude, cli(json.dumps(CLAUDE)), 'prompt', self.root)
        self.assertEqual(result.telemetry['route']['provenance'], 'unknown')


class LedgerTests(unittest.TestCase):
    def test_resume_and_replay_of_the_same_invocation_count_once(self):
        record = t.from_agy(AGY_ONE)
        replayed = json.loads(json.dumps(record))  # as read back from a persisted invocation file
        ledger = [row('agy', record), row('agy', replayed), row('agy', t.from_agy(AGY_TWO))]
        report = t.breakdown(ledger)
        agy = report['providers']['agy']
        self.assertEqual(report['duplicate_records_ignored'], 1)
        self.assertEqual(agy['invocations'], {'completed': 2, 'incomplete': 0})
        self.assertEqual(agy['tokens']['counts']['input'], 68991 + 131639)
        self.assertEqual(agy['tokens']['total'], t.token_total(record['tokens']) + t.token_total(t.from_agy(AGY_TWO)['tokens']))
        self.assertEqual(t.breakdown(ledger + [row('agy', replayed)]), dict(report, duplicate_records_ignored=2))

    def test_gate_evaluation_resume_reuses_the_persisted_telemetry(self):
        from ssdlc_recovery import ReportedOutput
        from orchestrator import CostAndTelemetryEntry, _with_incomplete_telemetry
        record = t.from_claude(CLAUDE)
        result = ReportedOutput(('text', 0.4, 1, 2, 'trace'), None, record)
        invocation = dict(cost_usd=0.4, tokens_in=1, tokens_out=2, trace_user='trace', duration_sec=1,
                          telemetry=result.telemetry)
        persisted = json.loads(json.dumps(invocation))
        rows = [CostAndTelemetryEntry('04-review-a1', 'Code Reviewer', 'claude', 'm', 'APPROVED', 1,
                                      **{k: v for k, v in tel.items() if k != 'duration_sec'}).to_dict()
                for tel in (invocation, persisted)]
        self.assertEqual(t.breakdown(rows)['providers']['claude']['invocations']['completed'], 1)
        # An interrupted call gets unknown, incomplete telemetry.
        interrupted = _with_incomplete_telemetry({'outcome_class': 'interrupted', 'engine': 'codex'}, {})
        self.assertEqual(interrupted['telemetry']['outcome'], 'incomplete')
        self.assertEqual(_with_incomplete_telemetry({'outcome_class': 'approved'}, {}), {})

    def test_estimate_coverage_per_provider(self):
        ledger = [row('claude', t.from_claude(CLAUDE)), row('claude', t.from_claude(dict(CLAUDE, total_cost_usd=0))),
                  row('claude', t.from_claude(dict(CLAUDE, total_cost_usd=None))), row('claude', t.incomplete('claude')),
                  row('codex', t.from_codex([{'input_tokens': 5, 'output_tokens': 1}]))]
        report = t.breakdown(ledger)
        claude = report['providers']['claude']
        # Completed calls with an estimate (an explicit zero counts) over completed calls; crashes apart.
        self.assertEqual(claude['estimate_coverage'], {'available': 2, 'completed': 3, 'ratio': 2 / 3,
                                                       'incomplete_unknown': 1})
        self.assertEqual(claude['estimate']['usd'], 0.4213)
        self.assertEqual(claude['spend']['reported'], 0)
        self.assertEqual(report['providers']['codex']['estimate_coverage']['ratio'], 0)
        self.assertEqual(report['providers']['codex']['estimate']['usd'], None)
        self.assertFalse(report['measures_summed_together'])

    def test_historical_rows_are_annotated_never_rewritten(self):
        legacy = [{'timestamp': 'x', 'phase': '03-dev', 'engine': 'claude', 'model': 'm', 'status': 'GENERATED',
                   'duration_sec': 1, 'cost_usd': 1.5, 'tokens_in': 10, 'tokens_out': 2, 'trace_user': ''},
                  {'cost_usd': None}, {'engine': 'codex', 'cost_usd': 0.0}]
        before = copy.deepcopy(legacy)
        report = t.breakdown(legacy)
        self.assertEqual(legacy, before)
        claude = report['providers']['claude']
        self.assertEqual(claude['legacy'], {'rows': 1, 'unclassified_usd': 1.5, 'unknown_cost_rows': 0})
        # A legacy dollar figure is neither a charge nor an estimate.
        self.assertEqual((claude['estimate']['usd'], claude['spend']['by_unit']), (None, {}))
        self.assertEqual(claude['invocations'], {'completed': 0, 'incomplete': 0})
        self.assertEqual(report['providers']['unknown']['legacy']['unknown_cost_rows'], 1)
        annotation = t.annotate_legacy(legacy[0])
        self.assertEqual((annotation['annotation'], annotation['rewritten'], annotation['estimate']),
                         ('legacy_unversioned', False, 'unknown'))
        self.assertEqual(t.row_cost_text(legacy[0]), '$1.5000 (legacy, unclassified)')
        self.assertEqual(t.row_cost_text(legacy[1]), 'unknown (legacy)')
        # New rows carry the schema version; a malformed telemetry record is not kept.
        new = row('claude', dict(t.from_claude(CLAUDE), schema_version=99))
        self.assertEqual((new['telemetry_schema_version'], 'telemetry' in new), (1, False))
        self.assertEqual(t.classify(new), ('no_invocation', None))

    def test_replay_gate_baseline_is_unchanged(self):
        from ssdlc_grader import verify_baseline
        self.assertTrue(verify_baseline())


from test_gate_reports import GateFixture, Crash  # noqa: E402
from test_full_checkpoint_review import framed  # noqa: E402


class GateCrashResumeTests(GateFixture):
    def test_crash_between_response_and_telemetry_is_an_incomplete_call_on_resume(self):
        import ssdlc_gate_reports as gates
        from ssdlc_recovery import ReportedOutput
        original = gates.GateEvaluator._write
        def write(evaluator, name, value):
            if name == 'invocation-1.json':
                raise Crash()
            return original(evaluator, name, value)
        reply = lambda *a, **k: ReportedOutput((framed('review', self.orch), 0.42, 1, 2, 'fake'), None,
                                               t.from_claude(CLAUDE))
        with patch.object(gates.GateEvaluator, '_write', write), self.assertRaises(Crash):
            self.run_review(reply)
        slot = self.slots()[0]
        self.assertTrue((slot / 'response-1.md').exists())
        self.assertFalse((slot / 'invocation-1.json').exists())
        # A real resume from the persisted state: no new provider call, one incomplete row.
        self.orch.state = self.orch._load_or_init_state()
        result, provider = self.run_review()
        provider.assert_not_called()
        self.assertTrue(result[0])
        rows = [r for r in self.orch.state['ledger'] if r.get('phase') == '04-review-a1']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['telemetry']['outcome'], 'incomplete')
        self.assertFalse((slot / 'invocation-1.json').exists())  # the evidence still says missing
        report = t.breakdown(self.orch.state['ledger'])
        entry = report['providers'][t.provider_for(rows[0]['engine'])]
        self.assertEqual(entry['invocations'], {'completed': 0, 'incomplete': 1})
        self.assertEqual(entry['estimate_coverage'], {'available': 0, 'completed': 0, 'ratio': None,
                                                      'incomplete_unknown': 1})
        # A further resume records nothing new.
        self.orch.state = self.orch._load_or_init_state()
        _, provider = self.run_review()
        provider.assert_not_called()
        self.assertEqual(t.breakdown(self.orch.state['ledger']), report)


class DiagnosisTelemetryTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        import ssdlc_gate_reports as gates
        from orchestrator import SSDLCOrchestrator
        from ssdlc_findings import TrustedContext
        from unittest.mock import Mock
        self.gates = gates
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        orch = self.orch = SSDLCOrchestrator.__new__(SSDLCOrchestrator)
        orch.ssdlc_root = self.root
        orch.state = dict(artifacts={}, ledger=[])
        def register(ref, path):
            digest = gates._digest(path.read_bytes())
            if ref in orch.state['artifacts']:
                self.assertEqual(digest, orch.state['artifacts'][ref]['sha256'])
            orch.state['artifacts'][ref] = dict(sha256=digest)
        orch._register_new = register
        orch.project_id, orch.ticket_id = 'project', 'ticket'
        orch.check_budget_circuit_breaker = Mock()
        orch.record_gate = Mock()
        orch.record_ledger = lambda entry: orch.state['ledger'].append(entry.to_dict())
        self.request = gates.EvaluationRequest('review', 'claude', 'model', 1, 1, 'a' * 40, 'system', 'user',
                                               TrustedContext('a' * 40, ['T1'], [], []), [], 'fingerprint', None, 'high')
        finding = SimpleNamespace(id='F1', to_dict=lambda: dict(id='F1', task_attribution=['T1']))
        self.result = gates.EvaluationResult('rejected_substantive', False,
                                             assessment=SimpleNamespace(findings=[finding], diagnosis_required=['F1']))
        self.candidate = gates.CandidateEvidence(quality=dict(checks=[dict(required=True, candidate_proof=True)]),
                                                 block='catalogue')

    def diagnose(self, reply):
        from orchestrator import EngineRunner
        with patch('orchestrator.git', return_value='tree'), \
             patch.object(EngineRunner, 'invoke_role', return_value=reply) as invoke:
            self.orch._diagnose_gate([self.request], [self.result], self.candidate, self.root, '04-review')
        return invoke

    def diagnosis_rows(self):
        return [r for r in self.orch.state['ledger'] if r['phase'] == '04-review-diag' and r['status'] == 'result']

    def test_diagnosis_keeps_the_reported_telemetry_and_replays_it_once(self):
        from ssdlc_recovery import ReportedOutput
        recorded = t.from_claude(CLAUDE)
        invoke = self.diagnose(ReportedOutput(('advisory', 0.4213, 45012, 800, ''), None, recorded))
        self.assertEqual(invoke.call_count, 1)
        rows = self.diagnosis_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['telemetry'], json.loads(json.dumps(recorded)))
        self.assertEqual(rows[0]['telemetry']['estimate']['amount'], 0.4213)
        self.assertEqual(rows[0]['telemetry']['tokens']['counts']['cached_input'], 45000)
        self.assertNotIn('telemetry', rows[0]['details'])
        # A separate versioned artifact, registered; the closed result record is unchanged.
        artifacts = sorted((self.root / 'diagnosis-telemetry').glob('*.json'))
        self.assertEqual(len(artifacts), 1)
        stored = json.loads(artifacts[0].read_text())
        self.assertEqual((stored['schema'], stored['schema_version']), ('ssdlc.diagnosis-telemetry', 1))
        self.assertIn(artifacts[0].relative_to(self.root).as_posix(), self.orch.state['artifacts'])
        result = json.loads(next((self.root / 'diagnosis/requests').glob('*/result.json')).read_text())
        self.assertNotIn('telemetry', result)
        report = t.breakdown(self.orch.state['ledger'])
        self.assertEqual(report['providers']['claude']['estimate_coverage']['available'], 1)
        # Replay: no new call and no second row.
        invoke = self.diagnose(ReportedOutput(('other', 0.0, 0, 0, ''), None, t.from_claude(dict(CLAUDE, total_cost_usd=9))))
        invoke.assert_not_called()
        self.assertEqual(t.breakdown(self.orch.state['ledger']), report)
        # A ledger lost before the row was saved is rebuilt from the same persisted record.
        self.orch.state['ledger'] = []
        self.diagnose(('unused', 0.0, 0, 0, ''))
        self.assertEqual(self.diagnosis_rows()[0]['telemetry'], rows[0]['telemetry'])
        # A changed telemetry artifact is refused, not silently replaced.
        artifacts[0].write_bytes(artifacts[0].read_bytes().replace(b'0.4213', b'0.0001'))
        self.orch.state['ledger'] = []
        from ssdlc_tasks import TaskError
        with self.assertRaisesRegex(TaskError, 'Diagnosis telemetry artifact missing or changed'):
            self.diagnose(('unused', 0.0, 0, 0, ''))

    def test_historical_diagnosis_without_telemetry_is_a_completed_call_with_unknown_measures(self):
        self.diagnose(('advisory', 0.0, 0, 0, ''))
        self.assertFalse((self.root / 'diagnosis-telemetry').exists())
        row = self.diagnosis_rows()[0]
        self.assertEqual(row['telemetry']['outcome'], 'completed')
        self.assertTrue(all(row['telemetry'][k]['availability'] == 'unknown' for k in ('spend', 'estimate', 'tokens')))


class EvidencePackTests(unittest.TestCase):
    def test_evidence_pack_shows_breakdown_by_provider_and_measure_with_estimate_coverage(self):
        import test_unified_integration as journeys
        case = journeys.StartupJourneys()
        self.addCleanup(case.doCleanups)
        case.setUp()
        case.new_run()
        owner = case.active_orch
        owner.state['ledger'] = [
            {'phase': '00-recon', 'engine': 'claude', 'model': 'm', 'status': 'OK', 'cost_usd': 2.0},
            row('claude', t.from_claude(CLAUDE)),
            row('claude', t.incomplete('claude')),
            row('codex', t.from_codex([{'input_tokens': 100, 'cached_input_tokens': 40, 'output_tokens': 10}])),
            row('agy', t.from_agy(AGY_ONE)),
            row('openrouter', t.from_openrouter({'prompt_tokens': 5, 'completion_tokens': 1, 'cost': 0.002})),
        ]
        owner._save_state()
        text = owner.compile_evidence_pack(True).read_text()
        self.assertNotIn('Tracked OpenRouter Cost', text)
        self.assertNotIn('Reported Ledger Cost', text)
        self.assertIn('### Cost Telemetry by Provider and Measure Type', text)
        self.assertIn('estimate $0.4213 (API-equivalent)', text)
        self.assertIn('charge 0.0020 USD', text)
        self.assertIn('$2.0000 (legacy, unclassified)', text)
        claude = next(line for line in text.splitlines() if line.startswith('| `claude` | 1 / 1 |'))
        self.assertIn('$0.4213 (1 estimated, 1 unknown)', claude)
        self.assertIn('1/1 (100%); incomplete (unknown): 1', claude)
        self.assertIn('1 rows, $2.0000 unclassified', claude)
        agy = next(line for line in text.splitlines() if line.startswith('| `agy` |'))
        self.assertIn('68,991 / 88,505 / unknown / 1,147 / 536', agy)
        self.assertIn('0/1 (0%)', agy)
        self.assertIn(f"{68991 + 88505 + 1147 + 536:,} (1 calls; 0 unknown)", agy)
        codex = next(line for line in text.splitlines() if line.startswith('| `codex` |'))
        self.assertIn('110 (1 calls', codex)
        openrouter = next(line for line in text.splitlines() if line.startswith('| `openrouter` |'))
        self.assertIn('0.0020 USD (1 reported, 0 unknown)', openrouter)


if __name__ == '__main__':
    unittest.main()
