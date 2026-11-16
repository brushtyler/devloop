# Cost telemetry

Every engine call records its cost telemetry in a shared, versioned schema
(`ssdlc.cost-telemetry`, version 1, module `ssdlc_telemetry.py`;
[issue #164](https://github.com/brushtyler/ssdlc/issues/164), part A of
[#14](https://github.com/brushtyler/ssdlc/issues/14)). It replaces the single
`cost_usd` figure, which mixed charges with estimates.

## Four separate measures

| Measure | What it is | Availability |
| --- | --- | --- |
| `spend` | A charge the provider measured or reported, in its own unit (currency or credits, never converted). Today only OpenRouter's `usage.cost`. | `reported` or `unknown` |
| `estimate` | An API-equivalent value in USD estimated by the CLI. Claude CLI's `total_cost_usd` is an estimate: on a subscription it is not a charge. | `estimated` or `unknown` |
| `tokens` | Raw counts: input, cached input, cache creation, output and reasoning, with the fields that overlap declared and whether the source reports them per turn or cumulatively. | `reported` or `unknown` |
| `quota_snapshots` | A versioned container, always empty and `unknown` here. Part C ([#166](https://github.com/brushtyler/ssdlc/issues/166)) defines and collects the snapshots. | `unknown` |

Each measure has a unit, a timestamp (`observed_at`) and a source. A value that
was not reported is `unknown` (`null`), never 0. A zero the source reported
explicitly is a value. Charges, estimates and tokens are never added together.

Each record also carries the provider, the adapter, the outcome (`completed` or
`incomplete`) and the provenance of the **route** and the **billing mode**. Both
come only from a verified signal (the OpenRouter HTTP adapter itself sent the
request) or from an explicit configuration of the invocation (for example
`SSDLC_DEV_USE_OPENROUTER=true`). The engine label or an API key in the
environment is never enough: otherwise they are `unknown`. The configuration
counts only if the CLI process really receives it: in a role container, Claude
sees only the variables the Docker command passes, so the OpenRouter route is
`unknown` unless that command carries the OpenRouter `ANTHROPIC_BASE_URL`.
Billing mode is `unknown` for every route in this version.

A crash, a non-zero exit or a failed agy envelope is an `incomplete` call with
every measure `unknown`.

## Adapters

| Engine | Source | Tokens | Overlaps |
| --- | --- | --- | --- |
| `claude` | `--output-format json` | `input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens`; cumulative | input excludes cache reads and creation; thinking is inside output, not reported apart |
| `codex` | `codex exec --json`, one `turn.completed.usage` per turn, summed | `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`; per turn | cached input inside input; reasoning inside output |
| `agy` | `--output-format json`, the envelope's `usage` (contract observed on agy 1.5.9) | `input_tokens`, `cache_read_tokens`, `output_tokens`, `thinking_tokens`; cumulative over the invocation | none: cache reads and thinking are separate from input and from `total_tokens` |
| OpenRouter | the response `usage` | `prompt_tokens`, `completion_tokens` and their cached/reasoning details | details inside prompt and completion |

The answer and the error handling stay as before. Codex still returns the final
agent message, or its error messages on failure. agy returns the envelope's
`response`; output that is not a successful envelope is returned as the CLI
printed it. The envelope's `conversation_id` and any account identifier are
never kept.

The token total of a call adds only the fields the source defines that are not
contained in another one. If one of them was not reported, the total is
`unknown`. Codex sums a field over the turns only when every turn reports it: a
turn without that field, or without usage, leaves the field `unknown` for the
whole call, never a partial sum.

## Ledger, state and Evidence Pack

New ledger rows carry `telemetry_schema_version: 1` and, when the row records an
engine call, its `telemetry` record. `cost_usd`, `tokens_in` and `tokens_out`
stay for compatibility; budgets and frozen limits are unchanged in this part.

- The Evidence Pack shows the measure of each row (`charge`, `estimate (API-equivalent)`,
  `unknown`) and a table **Cost Telemetry by Provider and Measure Type**: calls
  completed and incomplete, charges by unit, estimates, **estimate coverage**,
  tokens by category and their total without double counting.
- Estimate coverage, per provider: completed calls with an estimate available
  (an explicit zero counts), over all completed calls. Incomplete calls appear
  apart as `unknown`.
- `state.json` `phase_boundary.usage.cost_telemetry` holds the same breakdown.

Each invocation has an `invocation_id`, and the breakdown counts it once. A gate
evaluation persists the record in its `invocation-N.json`, so a resumed or
replayed evaluation records the same invocation and is not counted twice. If a
crash falls between the response and that file, the resumed evaluation records
the reserved call as `incomplete` with every measure `unknown`, once, and the
evidence keeps reporting the telemetry as missing.

A diagnosis call stores its record in `diagnosis-telemetry/<request>.json`
(`ssdlc.diagnosis-telemetry`, version 1), written and registered before the
result, whose closed schema is unchanged. The ledger and every replay read it
from there. A result written before this artifact existed is a completed call
with every measure `unknown`.

## Historical rows

Rows written before this schema are never rewritten or reinterpreted. When read,
they get a conservative annotation (`legacy_unversioned`): their `cost_usd` is an
unclassified legacy figure, neither a charge nor an estimate, and every measure
is `unknown`. Frozen limits profiles, journals and authorizations are unchanged.
