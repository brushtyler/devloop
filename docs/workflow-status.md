# Workflow snapshot

`ssdlc_workflow_status.snapshot(repo, ticket)` returns the `WorkflowSnapshot` of a run: the
selected generation, the task DAG, the feedback ledger and the metrics already persisted. It is
part 1 of the `workflow-status` CLI ([#46](https://github.com/brushtyler/ssdlc/issues/46),
[#346](https://github.com/brushtyler/ssdlc/issues/346)). The command itself, the run states
(`RUNNING`, `QUIESCENT`, `STALE`, …), projections, suggested commands, exit codes and `--watch`
come in the next parts. Today the snapshot is a Python API, with no command of its own.

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

Only `COHERENT` fills `run` to `metrics`; otherwise they are `null`.

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
