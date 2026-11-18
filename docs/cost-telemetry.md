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
| `quota_snapshots` | A versioned container: empty and `unknown` unless a quota control is enabled for the route of the call; then the public snapshots before and after the call and their deltas ([Subscription quota snapshots and controls](#subscription-quota-snapshots-and-controls)) | `reported` or `unknown` |

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
stay for compatibility. The caps read the classified measures (see
[Separate caps and route admission](#separate-caps-and-route-admission)); frozen
limits profiles are unchanged.

- The Evidence Pack shows the measure of each row (`charge`, `estimate (API-equivalent)`,
  `unknown`) and a table **Cost Telemetry by Provider and Measure Type**: calls
  completed and incomplete, charges by unit, estimates, **estimate coverage**,
  tokens by category and their total without double counting.
- Estimate coverage, per provider: completed calls with an estimate available
  (an explicit zero counts), over all completed calls. Incomplete calls appear
  apart as `unknown`.
- `state.json` `phase_boundary.usage.cost_telemetry` holds the same breakdown.
- The breakdown's `quota_snapshots` counts the calls with quota snapshots and their
  bucket deltas, observed or `unknown`; the Evidence Pack states them under the table.

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

## Separate caps and route admission

[Issue #165](https://github.com/brushtyler/ssdlc/issues/165), part B1 of
[#14](https://github.com/brushtyler/ssdlc/issues/14), with the defaults the
operator approved on 2026-11-16. Spend and estimates have separate, named caps,
and an estimate never consumes the spend cap.

| Variable | Default | Bounds |
| --- | --- | --- |
| `SSDLC_BUDGET_CAP_USD` | S $3, M $6, L $20 | Measured spend: charges reported in USD by a measured route. Gate `BUDGET_CAP_GATE` |
| `SSDLC_ESTIMATE_CAP_USD` | S $3, M $6, L $20 | API-equivalent estimates (Claude CLI). Gate `ESTIMATE_CAP_GATE` |
| `SSDLC_DEV_BUDGET_USD` | S $1, M $2.5, L $8 | Each Claude developer call, as `--max-budget-usd` |
| `SSDLC_UNMETERED_QUOTA` | none | Not a cap: the subscription CLIs (`codex`, `agy`) declared outside the dollar caps without a frozen profile (see [Measures required per route](#measures-required-per-route)) |

The three caps are independent and all stay active with their tier defaults; only an
explicit `0` disables one of the two ticket caps. The limits on attempts, calls,
rounds and elapsed time are unchanged, so a monetary estimate is never the only
bound of a loop. No token limit is introduced: token telemetry is kept and
reported as before.

How the ledger feeds the caps (each invocation once):

- a charge in USD counts as spend;
- an estimate counts as an estimate;
- the `cost_usd` of a legacy row, of a row without telemetry, or of a call with
  neither a charge nor an estimate (an incomplete call, for example) cannot be
  classified. It keeps counting as spend, as it always did, and is never
  reinterpreted as an estimate;
- a completed call on a measured route that reported no charge in USD makes the
  spend unknown: with the spend cap in force the run stops with
  `BUDGET_CAP_GATE` instead of counting it as zero;
- a completed Claude CLI call that reported no estimate (no `total_cost_usd`)
  makes the estimates unknown: with the estimate cap in force the run stops with
  `ESTIMATE_CAP_GATE` instead of counting it as zero. An incomplete call reports
  nothing by definition and stays an unclassified figure.

### Measures required per route

Before the call is reserved or launched, the effective route of the call is
derived from its own configuration and admitted only if the caps in force can
bound it. An OpenRouter call without `OPENROUTER_API_KEY` and without a frozen
profile falls back to a local CLI (`call_cli_fallback`): the route of that CLI is
the one admitted, before the supervisor intent.

| Route | When | Measure required |
| --- | --- | --- |
| `openrouter_api` | OpenRouter HTTP adapter; Claude or Codex with `use_openrouter` and `OPENROUTER_API_KEY` | spend. Only the HTTP adapter reports it: Claude or Codex on this route are refused while the spend cap is in force, because an estimate is not a charge |
| `claude_cli` | Claude CLI with no base URL of its own | estimate (`total_cost_usd`) |
| `codex_cli`, `agy_cli` | the CLI subscription | an explicit `unmetered_quota` declaration for the engine: the `live_unknown_usage` of the frozen profile (`unmetered_quota` or `reserve_per_invocation`, [#43](https://github.com/brushtyler/ssdlc/issues/43)) or, without a profile, `SSDLC_UNMETERED_QUOTA`; or a credit cap of the frozen profile that replaces the USD obligation after the operator's transition ([Caps in credits](#caps-in-credits-and-existing-profiles)). Without one it is refused while either cap is in force |
| `unknown` | OpenRouter requested for Claude but the role container would not receive its base URL, or the Claude CLI would receive another base URL | none can be determined: refused while either cap is in force |

A refusal is `LIMIT_REACHED` with "refused before launch": no reservation, no
supervisor intent, no process. With a frozen limits profile both of its dollar
caps are mandatory, so the environment cannot disable this admission; the profile
declaration applies to the CLI subscription route only, never to the OpenRouter
route of the same engine. The capacity check of
[#128](https://github.com/brushtyler/ssdlc/issues/128) and the atomic check at
reservation follow unchanged.

A feedback consultation is admitted on its route the same way, before its intent
and its reservation. If a prepaid feedback call is refused later, at the
dispatch, its reservation is closed as `not_dispatched`, never left uncertain.

Without a frozen profile the operator declares the subscription routes that stay
outside the dollar caps with `SSDLC_UNMETERED_QUOTA`, a comma-separated list of
`codex` and `agy` (for example `SSDLC_UNMETERED_QUOTA=codex,agy`); any other name
refuses the call. A declared route is bounded by attempts, calls and time. The
declaration never covers the OpenRouter route of the same engine. A frozen profile
keeps its own separate `metered_usd` and `cli_estimate_usd` caps and the frozen
combined authority of its corrective transactions.

### Caps in credits and existing profiles

[Issue #214](https://github.com/brushtyler/ssdlc/issues/214), part B2 of
[#14](https://github.com/brushtyler/ssdlc/issues/14), with the operator's
decision D6 of [ADR 0004](adr/0004-passo-2-budget-stato-durevole.md#5-decisioni-riservate-alloperatore).

**Existing profiles.** A frozen profile is never reinterpreted. Profiles in USD
stay as they are: there is no global migration, and their caps keep the meaning
described above. Only a profile with caps in credits can need a transition, and
only to let those credits replace the USD obligation of a route.

**Caps in credits.** A frozen limits profile may configure a cap in credits for
the subscription route of `codex` or `agy`, the engines that report no dollar
measure (`credit_caps` in `schemas/recovery-limits.schema.json`):

```json
"credit_caps": {"agy": {"unit": "agy_credits", "cap": 300, "per_invocation": 10,
                        "reason": "The agy plan charges AI credits, not USD"}}
```

- `unit` names the credits; it is never USD, and credits are never converted to
  or from dollars.
- Before each call of the engine, `per_invocation` credits are booked against
  `cap` and stay charged. A booking that would exceed the cap is refused before
  launch, and a cap already reached stops the run with `LIMIT_REACHED`. Bookings
  and cap are summed and compared as the exact decimal their JSON numbers write,
  never as binary floats: three bookings of `0.1` fill a cap of `0.3` exactly. No
  adapter reports a charge in credits per call today (Codex and agy expose only
  account-wide balances), so the booking is the measure.
- Without a frozen profile there are no credit caps.

**Credits add, they do not replace.** A credit cap is not a measure comparable
with the USD caps. While a USD cap is in force, a route without a USD measure or
rule of its own (no `live_unknown_usage` declaration for its engine) stays
refused before launch, even with a credit cap: the refusal names
`substitute_credit_cap`. With a USD rule, the credit cap is an extra bound on
top of it. A new profile cannot declare the substitution itself.

**Explicit transition.** Only the operator can let a configured credit cap
replace the USD obligation of the route, for that profile, with the
[`substitute_credit_cap`](limit-extensions.md#caps-in-credits) operation of
`operator-limit-extension-v1`. It sets `credit_caps.<engine>.replaces_usd` and
changes nothing else: the limits journal, its counters and the original
authorization are kept, and the bound chain, the `CREDIT_CAP_SUBSTITUTED` event
and the receipt are its trace. After it, the calls of that engine are admitted
with `usage_model: credit_cap`, bounded by the credit cap and by the invocation,
attempt and time limits. They are outside the dollar caps, as the coverage of the
Evidence Pack shows. The substitution never covers the OpenRouter route of the
engine.

## Subscription quota snapshots and controls

[Issue #166](https://github.com/brushtyler/ssdlc/issues/166), part C of
[#14](https://github.com/brushtyler/ssdlc/issues/14), with the defaults the
operator approved on 2026-11-16 (module `ssdlc_quota.py`). The fixtures validate
it offline; the live validation in the isolated runtime is the operator's.

### Reading a quota

No quota read runs a model. Each read has a time limit (30 seconds) and runs as
the engine would run, in its role container with the same authentication and no
other: agy gets only its OAuth copy, Codex only its `auth.json`, `version.json`
and isolated `CODEX_HOME`, without API keys or the stores of the other engines.

| Engine | Read | Buckets |
| --- | --- | --- |
| `agy` | `agy -p /usage --output-format json`, accepted only with `num_turns = 0` (contract observed on agy 1.5.9); any other value is refused as `inference_detected` | `command.data.groups[].buckets[]`: `id`, group, `window`, `remaining_fraction` and `reset_time` |
| `codex` | `codex app-server`, messages `initialize`, `initialized` and `account/rateLimits/read` only (no thread, no turn) | one per window (`<limitId>.primary`, `<limitId>.secondary`) of each limit in `rateLimitsByLimitId`: `usedPercent`, `windowDurationMins` and `resetsAt`. `rateLimits` is the alias of one of those limits and is never counted twice: it is read only when the map is absent or empty |
| `claude` | none in headless mode | the quota is `unknown` |

The Codex fixture is synthetic and declared as such: it is built from the
documented shape of the method. A read that fails, times out or returns something
else is an `unavailable` snapshot, never a guess. A value missing or out of range
is `null`, never 0; an explicit 0% is a value. Credits and plans are not read.

A snapshot is cached per engine and account and reused only while it is younger
than the maximum age the control accepts and none of its resets has passed.
Without a verified account identity nothing is cached. The snapshot after the
call is always read again.

### Deltas

When a control is enabled for the route of a call, a snapshot is read before the
launch and another after the call. The delta is in **percentage points** of the
bucket used: from 20% to 23% used is +3 pp. Two snapshots are compared only for
the same account identity, bucket, window and reset, and only when both are valid
and at most the control's maximum age away from the call.

A positive delta is `observed_account_delta`: a change seen on the whole account,
where other clients may be working at the same time. It is never the consumption
of the call (`call_consumption` is always `null`), and it says
`concurrent_use` when this process ran another call on the same engine during it
(otherwise `concurrent_use_not_excluded`).
Every other case is `unknown`, with its reason: `reset`, `refill` (a negative
delta), `no_change_observed` (zero), `bucket_missing`, `bucket_disabled`,
`window_changed`, `value_null`, `stale_snapshot`, `account_not_verified`,
`snapshot_not_collected`. A negative or zero consumption is never recorded.

The call's telemetry carries both snapshots, the deltas and the result of each
control in `quota_snapshots` (`status: collected`). A failed read after the call
leaves an `unavailable` snapshot and `unknown` deltas, and never changes the
result of the call.

### Controls

Controls are enabled only explicitly, per route and per bucket, with
`SSDLC_QUOTA_CONTROLS`: a JSON list where every field is mandatory.

```bash
export SSDLC_QUOTA_CONTROLS='[
  {"route": "agy_cli", "bucket": "gemini-weekly", "min_reserve_percent": 10, "max_age_seconds": 300},
  {"route": "codex_cli", "bucket": "codex.primary", "min_reserve_percent": 15, "max_age_seconds": 300}
]'
```

| Field | Meaning |
| --- | --- |
| `route` | `agy_cli`, `codex_cli` or `claude_cli` |
| `bucket` | the bucket id of the snapshot (agy `id`, Codex `<limitId>.primary` or `.secondary`) |
| `min_reserve_percent` | minimum remaining share, in (0, 100]: at or below it the call is blocked |
| `max_age_seconds` | maximum age of the snapshot, > 0 |

Before the reservation and the launch, the call is refused with `LIMIT_REACHED`
("refused before launch") when:

- a control's snapshot cannot be verified: the read failed, timed out or ran a
  model, the snapshot is too old or from the future, its bucket is missing,
  disabled or `null`, its window is missing, empty, only blanks or malformed,
  its reset is missing or malformed, or its reset
  has already passed. A `claude_cli` control
  always blocks, because the Claude quota is `unknown`;
- a control's reserve is reached;
- the variable is malformed (missing field, unknown route, duplicate control):
  then every call is refused.

There is never an automatic switch to overage, extra credits or a paid route: a
Codex account with credits left, or an agy bucket that offers AI credits, stays
blocked. Without a control nothing is read and nothing is blocked.

### Privacy

The account identity is derived from the credentials the engine itself uses
(the subject of their `id_token`, else their refresh token, else their whole
content), read again for every snapshot: credentials changed in the same file are
another account, so the cache is not reused and the deltas stay
`account_not_verified`, as they do when no credential can be read. It is kept
only as a digest keyed with a secret of the process that is never written. The snapshots
in telemetry, ledgers and the Evidence Pack hold no account identifier, email,
plan, conversation identifier or authentication payload, and a container that
carries one is refused.
