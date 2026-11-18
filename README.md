# SSDLC

**SSDLC** is a system for developing software with AI agents under the explicit authority of an operator: work goes through phases, independent reviews and recorded evidence, and approvals, limit extensions and merges are operator decisions. The agents are built on **Claude Code (`claude`)**, **OpenAI Codex (`codex`)**, **Antigravity (`agy`)** and **OpenRouter**.

SSDLC is one product, in this repository, with two components that today work side by side and will converge ([ADR 0001](docs/adr/0001-prodotto-unico-fasi-su-tracker.md)).

## Components

| Component | Code | What it does |
| --- | --- | --- |
| **Local engine** | `orchestrator.py`, `ssdlc_*.py` | Runs a ticket for a target project on one host: risk-tiered phases from recon and requirements through design, threat model, task-mode development in an isolated worktree, multi-model review, QA and AppSec, up to an evidence pack for human sign-off |
| **devloop** | `devloop/` | Dispatches GitHub issues and PRs: an implementer agent and an independent reviewer, fix rounds, and a merge done by a human or, when the operator enables a merge mode, by devloop under the merge directives |

## Status

What exists today, stated explicitly so that this page does not promise more than there is:

- **devloop is how this repository is developed, and it is configured for it**: repository, trusted users and GitHub apps are set in `devloop/config.toml`. It is not packaged for use on other repositories.
- **The local engine is what you install for your own projects** ([Getting started](docs/getting-started.md)). Its guarantees stay unchanged until parity with the new path is demonstrated (ADR 0001, constraint 1).
- **SSDLC phases on the tracker do not exist yet** (ADR 0001, step 3): specification, design, plan and evidence of the local engine are files on the host of the run, not issues and PRs.
- **Task mode is the default for all new local-engine runs**, including Tier S. Recovery transitions, scope amendments and the feedback workflow apply to task-mode runs.
- **Merges are explicit.** The local engine never merges on its own: approval and every limit extension are explicit operator actions. devloop leaves the merge to a human unless the operator enables a [merge mode](docs/devloop/guide.md#merge-modes).
- **Integrity checks are not authentication.** SHA-256 hashes detect changes at checkpoints. They do not protect against a host user who rewrites both evidence and hashes, and there is no continuous containment (workstream B). The task journal's hash chain does not detect a rewrite of the whole journal, truncation or rollback without an independent trust anchor; see the [task journal threat model](docs/architecture.md#task-journal-guarantees-and-threat-model).
- **Cost accounting is partial.** Claude reports CLI estimates and OpenRouter reports metered usage. Subscription CLIs such as Codex do not report a cost; see [#43](https://github.com/brushtyler/ssdlc/issues/43) and [#14](https://github.com/brushtyler/ssdlc/issues/14).
- **Supervision is single-host Linux.** It is not a distributed scheduler.

Open work is tracked in the [GitHub issues](https://github.com/brushtyler/ssdlc/issues).

## Direction

The steps of [ADR 0001](docs/adr/0001-prodotto-unico-fasi-su-tracker.md), in order:

1. **Tracker ports**, done: `Tracker`, `CodeReview` and `CI` in `devloop/ports.py` ([Ports and adapters](docs/architecture.md#ports-and-adapters)).
2. Budget, reservation and durable state, with the identity of story, phase and attempt.
3. Phases as issue and PR types, as an additional path next to the local engine.
4. Containment and fencing of the executors.
5. A filesystem backend, with signed identities.
6. Convergence of the local engine, only after demonstrated parity and an explicit migration authorized by the operator.

## Documentation

### Using the local engine

| Document | Content |
| --- | --- |
| [Getting started](docs/getting-started.md) | Requirements, installation, first project, basic configuration, everyday commands, where things are stored |
| [Configuration](docs/configuration.md) | Authentication, inheritance, model routing, reasoning effort, budgets, containers, remote sync, maintenance |
| [Architecture](docs/architecture.md) | Components, engine and dispatcher, ports and adapters, the devloop boundary and the future `core` package; phases, tiers, isolation, run layout, controller contract, integrity model |
| [Task mode](docs/task-mode.md) | PO validation, design sessions, task planning, scheduler, repair, gate reports |
| [Supervision](docs/supervision.md) | Detached runs, resume, cancel, recovery matrix |
| [Cost telemetry](docs/cost-telemetry.md) | Charges, API-equivalent estimates and tokens per provider; Evidence Pack breakdown |
| [Troubleshooting](docs/troubleshooting.md) | Memory/OOM, resuming, deploy keys, common stops |

### Recovery and continuation

Start from the [recovery index](docs/recovery.md): from the stop, to the transition, to the document.

| Document | When to use it |
| --- | --- |
| [Candidate capture](docs/candidate-capture.md) | How developer output is captured and verified in disposable workspaces (I0) |
| [Bounded recovery](docs/bounded-recovery.md) | Frozen limits, timeout capture, semantic design progress, A5 replay gate (I2) |
| [Manual limit extensions](docs/limit-extensions.md) | Extending named limits or the deadline of a stopped run, or reverifying the same candidate after a verifier timeout, with an exact operator authorization (#162, #167) |
| [Continuations](docs/continuations.md) | Importing a stopped pre-checkpoint run for fresh verification (I1) |
| [Historical reconciliation](docs/historical-reconciliation.md) | Admitting a stopped I1 generation to native development with cumulative limits |
| [Scope amendments](docs/scope-amendments.md) | Recovering a task stopped on `SCOPE_REQUIRED` after a checkpoint |
| [Developer feedback](docs/developer-feedback.md) | Scope diagnosis, reviewed feedback roles, delegated amendments, GitHub projections (#39) |
| [Native gate convergence](docs/native-gate-convergence.md) | Candidate checks collected before the reviews that need them |
| [Bounce preview](docs/bounce-preview.md) | Read-only preview of a targeted bounce from a gate |
| [Writer fencing](docs/writer-fencing.md) | Writer gate protocol and in-container PID 1 holder for automatic fencing (#45); every new run records `writer_fencing: v1`, and the role containers of admitted runs start under the holder, their host Git commands under `run_host()` |

### Operating devloop

| Document | Content |
| --- | --- |
| [Guide](docs/devloop/guide.md) | For the operator and the supervisor: the loop, labels and `/devloop` commands, merge modes, escalations, watching, pinned installation, limits |
| [Reference](docs/devloop/reference.md) | Configuration, local tests, documentation map, agents, review levels and policy, GitHub identities and apps |
| [Protocol](docs/devloop/protocol.md) | Rounds, markers and records: receipts, merge directives, structured review and findings, evidence |

### Decisions and plans

[Architecture decision records](docs/adr/) (in Italian): [ADR 0001](docs/adr/0001-prodotto-unico-fasi-su-tracker.md), one product with phases on the tracker; [ADR 0002](docs/adr/0002-ruoli-e-responsabilita.md), roles and responsibilities; [ADR 0003](docs/adr/0003-struttura-della-documentazione.md), documentation structure. [PLAN_AUTONOMY_WORKSTREAMS.md](PLAN_AUTONOMY_WORKSTREAMS.md) describes the autonomy roadmap (workstreams for recovery, containment and oracle, increments I0–I6); [PLAN_TASK_ROUTING_AND_SUBGOALS.md](PLAN_TASK_ROUTING_AND_SUBGOALS.md) covers task routing and subgoals.

## Developing SSDLC

Read the [development guide](docs/development.md): the test suite (`scripts/run-tests.py`) and how to run a single module, the documentation tests, the CI, the rule that keeps documents in step with the code, and the pinned controller.

```bash
.tools/python/bin/python3 scripts/run-tests.py           # the whole suite, what CI runs
python3 -m unittest discover -s tests -p 'test_name.py'  # a single module
```

- When SSDLC develops itself, run a pinned controller checkout against a **separate** target checkout.
- Agents contributing to this repository follow [AGENTS.md](AGENTS.md).
- [devloop](docs/devloop.md) dispatches issues labelled `agent:ready` to an implementer agent and an independent reviewer agent, up to a PR that a human merges (devloop merges only when the operator enables a merge mode).

## License

Apache-2.0
