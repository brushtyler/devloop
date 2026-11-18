# Workflow snapshot

`ssdlc_workflow_status.snapshot(repo, ticket)` returns the `WorkflowSnapshot` of a run: the
selected generation, the task DAG, the feedback ledger, the metrics already persisted, the
[cost projections](#accounting-and-projections) and the [status](#liveness-and-next-action):
liveness observed in the kernel, its coverage, the activity, the native classification of a stop
and the next action, and the tail of the [last persisted log](#last-log). The
[`workflow-status` command](#the-workflow-status-command) prints it in four sections or as JSON
([#46](https://github.com/brushtyler/ssdlc/issues/46),
[#346](https://github.com/brushtyler/ssdlc/issues/346),
[#347](https://github.com/brushtyler/ssdlc/issues/347),
[#348](https://github.com/brushtyler/ssdlc/issues/348),
[#349](https://github.com/brushtyler/ssdlc/issues/349)).

## The `workflow-status` command

```bash
./ssdlc workflow-status --ticket <ticket>                 # four text sections
./ssdlc workflow-status --ticket <ticket> --json          # the snapshot, one JSON document
./ssdlc workflow-status --ticket <ticket> --watch         # refreshed every 2 seconds until Ctrl-C
./ssdlc workflow-status --ticket <ticket> --watch --json --interval 5   # JSON Lines
```

Like the snapshot, the command never writes under `.ssdlc/`. The text output has four sections:

1. **Run, generation and liveness**: native state and outcome, phase and step, generation and
   `dispatch_fenced`, liveness with its writers, the coverage components that are missing and why,
   the activity.
2. **Tasks and feedback**: task counts and each task of the plan (status, attempts, ready or what it
   waits on), tasks outside the plan, the journal position, the feedback ledger, its rounds and
   recorded interventions.
3. **Metrics, budget and projections**: every metric with its coverage and, when not complete, the
   reason; the projection of each measure, the frozen budget and the scenario, the warnings.
4. **Next action, prerequisites and last log**: the stop, the action, its mode, the command when
   there is one, the prerequisites, and the tail of the last log.

When the read is not `COHERENT`, each section says so and carries no data.

**JSON.** `--json` prints the [contract](#contract-schema_version-1) of the snapshot unchanged,
with the `log` field. With `--watch`, each snapshot is one line (JSON Lines): one write per line,
nothing else on stdout.

**Watch.** `--watch` takes a snapshot every `--interval` seconds: 2 by default, at least 1
(`--interval` without `--watch` is an invalid argument). It keeps sampling after a terminal,
`TORN`, `UNVERIFIABLE` or failed snapshot: only SIGINT (Ctrl-C) ends it, with exit code 130 and
no traceback. On a terminal, the text watch clears the screen before each snapshot.

**Sanitization.** Every string of the text output, from state fields to the log tail, is
sanitized: escape sequences (CSI, OSC and the others), control characters, carriage returns and
invisible format characters such as bidirectional overrides become U+FFFD; a tab or a no-break
space becomes a space. The log tail keeps its line feeds and tabs. JSON escapes every control
character.

### Exit codes

The first rule that applies gives the code:

| Code | When |
| --- | --- |
| `2` | Invalid arguments: missing or invalid ticket name, an `--interval` below 1 second or not a number, `--interval` without `--watch`, an unknown option. Nothing is read |
| `5` | `read.status` is `TORN` |
| `6` | `read.status` is `UNVERIFIABLE` (missing ticket, data that does not verify) or the read failed: a failed read is reported as an `UNVERIFIABLE` snapshot whose `reason` starts with `read error:` |
| `4` | `status.liveness` is `STALE` |
| `3` | `status.outcome` is `stopped`, or `generation.dispatch_fenced` is true |
| `0` | `status.outcome` is `completed` |
| `6` | `status.liveness` is `UNVERIFIED`, or `status.outcome` is `absent` or `unknown` |
| `0` | Every other operational case: a run in progress with a writer, paused, ready, awaiting sign-off |
| `130` | SIGINT, with or without `--watch` |

Metrics with only partial coverage never change the code. A stopped run whose liveness is not
verified is `3`, not `6`; a completed run is `0` even when its liveness is `UNVERIFIED`. Until the
controller declares its coverage ([#345](https://github.com/brushtyler/ssdlc/issues/345)), a run
in progress with no process holding its locks is `UNVERIFIED`, hence `6`. In `--watch` the code of
each snapshot does not end the command; JSON consumers compute it from each line with the rules
above.

## Evidence Pack

When the Evidence Pack is compiled, the controller takes a snapshot of the run and keeps it next
to the pack as `workflow-status.json`, a registered artifact with its SHA-256. The pack links it,
with the read status, liveness, outcome and exit code, on the line
`Workflow status at compilation`. It stays local, like the rest of the ticket: nothing is
published. The controller compiling the pack holds the run's locks, so liveness there is normally
`RUNNING`. A failed read is recorded as an `UNVERIFIABLE` snapshot with its reason; it never fails
the pack.

## Read without writing

The snapshot never writes under `.ssdlc/`:

- it takes no lock and creates no file or directory;
- it does not repair an interrupted journal tail: it reports its length
  (`journal.interrupted_tail_bytes`) and replays only the complete lines;
- it does not refresh `tasks-state.json`: the task journal is the authority;
- it follows no symlink: every path component is opened with `O_NOFOLLOW`, and a symlink makes
  the snapshot `UNVERIFIABLE`.

A pending selector or a feedback ledger the engine refuses is reported, not repaired.

## Coherent or `TORN`

Each file read is fingerprinted (SHA-256 of the content, plus device, inode, size and mtime of
the open file, so an atomic replace is seen even with the same content). After the collection,
every file read is opened again: selectors, and the files of the selected generation as the
engine's loader reads them (pointer, intent, manifest, projection and sealed artifacts). If one changed, the whole collection is
repeated, **at most three times**; then `read.status` is `TORN` and the snapshot carries no data.

| `read.status` | Meaning | Data |
| --- | --- | --- |
| `COHERENT` | Every file read was unchanged when checked again: the snapshot shows the files as they all were at one instant | yes |
| `TORN` | The files changed during each of the three collections | no |
| `UNVERIFIABLE` | The ticket is missing, or its data is stable on disk but does not read or verify (a selector that does not validate, `null` included, a broken journal chain or a malformed journal record, a symlink, invalid JSON) | no |

`read.attempts` is the number of collections. `sources` maps each file read, relative to the
ticket, to its SHA-256 (`null` for a file that does not exist): the files the snapshot is
coherent with. The mtime is only a change signal; it is never reported as a time.

## Contract (`schema_version` 1)

| Field | Content |
| --- | --- |
| `schema`, `schema_version` | `ssdlc.workflow-snapshot`, `1` |
| `ticket` | The ticket read |
| `read` | `status`, `attempts`, `max_attempts` (3), `observed_at` (the reader's clock, UTC), `reason` |
| `sources` | File read → SHA-256 or `null` |
| `run` | `status`, `current_phase`, `current_step` of `state.json`, as the engine wrote them |
| `generation` | `selectors` (`execution`, `continuation`: active and pending transition, present or not), `dispatch_fenced`, `kind` (`base`, `execution`, `continuation`), `transition_id`, `root` |
| `journal` | `sequence`, `sha256` of the last complete event, `interrupted_tail_bytes` |
| `tasks` | `plan_revision`, `nodes` (`task_id`, `title`, `depends_on`, native `status`, `recorded`, `attempts`, `waiting_on`, `ready`), `counts` by status (`UNRECORDED` for a task without a journal record), `outside_plan` |
| `feedback` | `status` (`absent`, `active`, `unavailable`), the ledger's public `projection`, `reason` |
| `metrics` | See below |
| `projection` | See [Accounting and projections](#accounting-and-projections) |
| `status` | See [Liveness and next action](#liveness-and-next-action) |
| `log` | See [Last log](#last-log) |

Only `COHERENT` fills `run` to `log`; otherwise they are `null`.

**Generation.** The snapshot reads the generation the published selectors name, with the same
precedence as the engine (`execution/active.json`, then `continuations/active.json`, else the
ticket itself). It checks the pointer, the hashes of the sealed intent and manifest, the projection
and the runtime binding (`generation.json`); for an execution generation, also that the dispatch
is authorized and that the ledger of `state.json` still starts with the projection's ledger, row
by row, as the engine's resolver checks it. It does not replay the chain of limit extensions:
dispatch still does. A selector file is present even when it holds `null`: a present selector
that names no generation is `UNVERIFIABLE`, never read as absent. A pending selector does not hide the active one:
`dispatch_fenced` says whether the engine would refuse to dispatch.

**DAG.** Tasks and dependencies come from the generation's `02-design/plan.json`, statuses and
attempts from its task journal. `ready` is `true` for a recorded task not completed whose
dependencies are all `COMPLETED`, `null` for a task without a journal record. A task journal
without its plan is `UNVERIFIABLE`.

## Metrics and coverage

Each metric is `{value, unit, coverage, source, reason}`. `coverage` is `complete`, `partial` or
`none`; a metric that is not complete always has a `reason`, and a metric with coverage `none` has
`value: null`. Only measures already persisted are aggregated.

| Metric | Source | Coverage |
| --- | --- | --- |
| `tasks_total`, `tasks_completed`, `tasks_remaining` | Plan and task journal | `complete`; `none` without a plan |
| `task_attempts` | Task journal | `partial` when some task has no journal record |
| `invocations` | Ledger telemetry in `state.json`, each invocation once | `partial` when legacy rows have no telemetry |
| `model_duration_sec` | `duration_sec` of the ledger rows | `partial` when some row has none; `none`, `value: null`, when no row has one |
| `spend_usd`, `estimate_usd`, `unclassified_usd` | Ledger, as the [separate caps](cost-telemetry.md#separate-caps-and-route-admission) read it | `partial` when completed calls reported no charge or no estimate |
| `tokens_total` | Ledger telemetry | `partial` or `none` when calls or legacy rows have no token total |
| `feedback_rounds` | Feedback ledger | `none` when the ledger is unavailable |
| `interventions` | Operator owner amendments and repair returns in the feedback ledger | Always `partial`: only interventions recorded there count |
| `task_elapsed_sec`, `phase_elapsed_sec`, `check_duration_sec`, `operator_wait_sec` | Not persisted | Always `none`, `value: null` |

Without a ledger in `state.json` the ledger metrics are `none`. Times are not observable today:
task journal events have no timestamp, ordinary checks record only their outcome (a duration is
kept only for timeouts), and the wait for the operator is not recorded. The snapshot never infers
them from file mtimes. Measuring them needs new instrumentation of the producers, a separate
issue.

## Accounting and projections

`projection` ([#348](https://github.com/brushtyler/ssdlc/issues/348)) estimates what the rest of
the run may cost, from the data the snapshot has already read: the ledger of `state.json`, the
task DAG, the phase the engine recorded and the frozen limits of the generation
(`limits/policy.json` and its journal, read without writing, like every other file of the
snapshot). `ssdlc_workflow_projection.project` is pure: it changes no budget, no limit and no
run, and its warnings never stop anything.

**Separate measures.** Each monetary measure has its own projection, next to the frozen cap of its
kind: `spend_usd` (charges in USD) against `metered_usd`, `estimate_usd` (API-equivalent
estimates) against `cli_estimate_usd`. They are never added together.

For each measure:

| Field | Content |
| --- | --- |
| `spent` | The measure so far, the same value as the metric |
| `development` | Average per completed task (the known measure of the `03-dev-<task>-a<n>` calls of each completed task, averaged over the completed tasks with at least one known call), times the remaining tasks. `per_task`, `tasks_completed`, `tasks_observed`, `tasks_remaining`. `0` when no task remains |
| `gates` | One call for each remaining mandatory global gate (`04-review`, `05-qa`, `05b-appsec`), at the average known measure of one gate call (`basis: gate_calls`) or, before any gate ran, of one call of the run (`basis: all_calls`). `per_call`, `calls_observed`, `calls_unknown`, `gates_remaining` |
| `central` | `spent` + `development` + `gates`: the sum of the known components |
| `budget` | With active frozen limits: `cap`, `accounted` (as `LimitsStore` charges it: observed usage plus frozen reserves), `remaining`, `projected` (`accounted` + development + gates), `over_cap` |
| `scenario` | With active frozen limits: `accounted` plus each remaining invocation (`invocations` cap minus those used) at the largest frozen reserve per invocation or observed charge of one invocation. `guaranteed_maximum: false`: it is a scenario, not a financial maximum |

The remaining gates come from `state.json`: those at or after `current_phase`, none once the
run is `AWAITING_SUPERVISOR_SIGNOFF` or `APPROVED_AND_MERGED`.

**Coverage.** Each component is `{value, unit, coverage, source, reason}`, like a metric. A
component without data is `value: null`, coverage `none`, with the reason: no task plan, no
completed task, no completed task with a known call of the measure, no call with a known measure
to price a gate, an unknown phase, no reserve or charge to price an invocation.

Only observations enter the averages; a zero reported by a call is an observation. The measure a
call's route does not report (the spend of a Claude CLI call, the estimate of an OpenRouter
charge) is a known zero. Everything else is unknown, never zero: a legacy row with or without a
`cost_usd` (an unclassified figure belongs to no measure), a call on a declared or unknown route
that reported nothing (Codex, Antigravity), the measure a route requires and the call did not
report (a Claude call without an estimate). Unknown calls, and completed tasks without a known
call (named in the reason), are left out of the average and make the component `partial`. `central` is
`partial` as soon as one of its components is not complete, and `projection.coverage` is
`complete`, `partial` or `none` over the development and gate components of both measures.

**Limits.** `projection.limits` is `absent` without a frozen profile, `active`, or `unavailable`
with the reason (a pending limit extension activation, a policy that does not validate, a journal
that does not match it or that records a malformed invocation, such as a null frozen reserve). The chain of limit extensions is not replayed: dispatch still does.
Without active limits, `budget` and `scenario` are `null`.

**Warnings.** `projection.warnings` lists, each with `effect: "none"`:

| `code` | When |
| --- | --- |
| `BUDGET_NEARLY_EXHAUSTED` | The remaining cap of a measure is zero or less, or at most the cost of one call (`one_call`): the average known measure of one call of the run (`one_call_basis: observed_calls`) or, without observations, the largest frozen reserve or charge of one invocation (`frozen_limits`). It does not depend on the gates left, so it holds in `06-ops` and `07-docs` too |
| `FORECAST_OVER_CAP` | `budget.projected` exceeds the cap |
| `INVOCATIONS_NEARLY_EXHAUSTED` | At most one invocation of the frozen `invocations` cap remains |

They are information for the operator: the snapshot writes nothing, and the engine's own
admission (`LimitsStore`) stays the only authority on dispatch.

## Liveness and next action

`status` ([#347](https://github.com/brushtyler/ssdlc/issues/347), `ssdlc_workflow_liveness`) keeps
three things apart: the canonical state of the run, its liveness and its activity. The classification
is pure; the observations it uses read without writing: the gate is located with `lstat` and never
opened, the [writer gate](writer-fencing.md) registry is read only if it exists, Docker is only listed.

| Field | Content |
| --- | --- |
| `state` | The native `state.json` status, as the engine wrote it (`IN_PROGRESS`, `PAUSED_AT_PHASE_BOUNDARY`, `FAILED`, …) |
| `outcome` | `in_progress`, `paused`, `ready` (`PO_READY`, `DESIGN_READY`, `PLAN_READY`, `CONTINUATION_IMPORTED`, `CONTINUATION_VERIFIED`), `awaiting_signoff`, `completed`, `stopped` (`FAILED`, `HALTED_BUDGET_EXCEEDED`, `ESCALATED_TO_SUPERVISOR`, `CONTINUATION_BLOCKED`, `CONTINUATION_CHECKS_FAILED`), `unknown` for any other status, `absent` without `state.json` |
| `liveness` | `RUNNING`, `QUIESCENT`, `STALE` or `UNVERIFIED` (below) |
| `writers` | The kernel holders seen: `lock` (`writer.gate` or `ticket.lock`), `mode`, `pid` |
| `coverage` | `complete` and, for each component (`writers`, `controller`, `host_trees`, `containers`), `covered` and the `reason` it is not |
| `activity` | `value` (`invocation`, `verification`, `idle` or `null`), the `invocations` and `verifications` recorded in progress, `reason` |
| `stop` | For a stopped run, or a `STALE` one: the native `class` and `reason_code`, `mode`, the recorded failure `category`, `detail`; otherwise `null` |
| `next_action` | `action`, `mode`, `command` (`argv` and `cwd`, or `null`), `prerequisites` |

**Liveness.** The writers are the processes holding a kernel `flock` on the ticket's
`writer.gate` or `ticket.lock`, read in `/proc/locks`. They are observed before each collection and
again after it; then the coverage is read, and only then are the files checked, so the files did not
move while the kernel was read.

| `liveness` | When |
| --- | --- |
| `RUNNING` | A writer was seen by either observation, whatever the coverage |
| `UNVERIFIED` | No writer seen, and the coverage is not complete |
| `QUIESCENT` | No writer seen, complete coverage, and the run at rest (`paused`, `ready`, `awaiting_signoff`, `completed`, `stopped`) |
| `STALE` | No writer seen, complete coverage, and work the engine left in progress (`in_progress` or `unknown`): the controller, a gate writer, is verified gone |

**Coverage.** Every writer of the run must be visible to the kernel or proven ended. Each
component has a reason when it is not:

| Component | Covered when |
| --- | --- |
| `writers` | Linux, a local filesystem, a `writer.gate` with its anchor, and `/proc/locks` readable |
| `controller` | `state.json` declares `writer_fencing_controller: host-tree-v1`: the controller holds a writer lease for its whole life and runs every host command through `run_host()`. The controller writes it from [#345](https://github.com/brushtyler/ssdlc/issues/345) on; until then no real run has it |
| `host_trees` | Every host command tree of the generation is proven ended, as the [writer gate](writer-fencing.md#host-writers) requires, and no registry entry is unreadable |
| `containers` | The run is admitted to automatic fencing (`writer_fencing: v1`, never refused) and Docker lists no container of the ticket; Docker is consulted only when every other component is covered |

Until #345, real runs are therefore `RUNNING` when a process holds one of the locks, and `UNVERIFIED`
otherwise: never `QUIESCENT` without complete coverage. The same rules give `QUIESCENT` and `STALE`
once the controller declares its coverage.

**Activity.** `invocation` when a task of the journal is `RUNNING` or the frozen limits journal holds
a `reserved` invocation, `verification` when a task is `VERIFYING`. Without writers and with complete
coverage the activity is `idle`, and recorded work is reported as interrupted. Otherwise, with nothing
recorded, it is `null`: other activities are not persisted, and no wait for a provider is deduced.

**Stops.** A stop keeps the classes of the supervisor's
[recovery matrix](supervision.md) (`RECOVERY_MATRIX`), with the precedence of its table. The recorded
`failure.category` comes first, whatever the stopped status: authentication, `INTEGRITY`, the recovery
stops such as `LIMIT_REACHED` or `OUTCOME_UNCERTAIN`, `ENVIRONMENT_RESOURCE`. The pipeline records
`ESCALATED_TO_SUPERVISOR` also for these categories, so an escalated run with an expired authentication
is `authentication`, not `operator_decision`. Then `HALTED_BUDGET_EXCEEDED` is `limits` and
`ESCALATED_TO_SUPERVISOR` is `operator_decision`. For `FAILED`, the categories the supervisor binds only
through its controller events (`CHECK_FAILURE`, `SCOPE_REQUIRED`, …) and a missing category stay
`uncertain_outcome`, `terminal_unbound`, as in its fallback row. A `STALE` run is `uncertain_outcome` too.

**Next action.** `action` and `mode` are those of the recovery matrix for a stop. A `command` is given
only when every argument is available: today only sign-off, `./ssdlc approve <ticket>` from the
repository, for `AWAITING_SUPERVISOR_SIGNOFF`. Every other action has `command: null` and lists what is
missing in `prerequisites`: an authorization, a limit value (none is proposed), or the parameters of a
resume, which are never reconstructed from files (a pause names its `--from-phase`, nothing else).
`RUNNING` asks to wait; `UNVERIFIED` first asks to confirm that no process of the run still runs, with
the coverage reasons.

## Last log

`log` ([#349](https://github.com/brushtyler/ssdlc/issues/349)) is the last log the run persisted, chosen
in the selected generation from the coherent data, never from a file's mtime:

1. the log of the last task journal event that introduces or updates a log reference (`log_ref`,
   `developer_log_ref`, `output_ref`, relative to `03-dev/tasks/`); within one event, the last in
   the order of the record;
2. without one, the `.log` registered in the `artifacts` of `state.json` with the latest
   `recorded_at`, the path breaking ties.

A reference that is not a plain relative path (`..`, an absolute path, other characters) is
skipped and counted in the reason, and so is a registered log without a valid `recorded_at`.

| Field | Content |
| --- | --- |
| `ref` | The log, relative to the ticket; `null` when no reference can be ordered |
| `source`, `sequence`, `event`, `recorded_at` | `task journal` with the event's `sequence` and name, or `artifacts` with its `recorded_at` |
| `size_bytes`, `tail_bytes`, `truncated` | The file's size, the bytes read, whether the tail starts after the beginning |
| `text` | The last 8192 bytes at most, decoded as UTF-8 (invalid bytes replaced) and sanitized like the text output, line feeds and tabs kept; `null` when not read |
| `coverage`, `reason` | `partial` when a log is chosen, `none` with `ref: null` |

Coverage is never complete: only logs the task journal references or the run registers as artifacts
are seen, and the stdout and stderr of the detached controller are not kept. The tail is read after
the coherence check, without following any symlink (`O_NOFOLLOW` on each path component) and with a
single read of at most 8192 bytes; the log is not one of the `sources`. A log that is missing, a
symlink or not a regular file keeps its `ref`, with `text: null` and the reason; it does not change
the read status or the exit code.
