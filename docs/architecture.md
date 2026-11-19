# Pipeline and architecture

SSDLC is one product, in one repository, with two components that today work side by side and
will converge ([ADR 0001](adr/0001-prodotto-unico-fasi-su-tracker.md)). The first sections of
this page describe the product: its components, how the local engine and the devloop dispatcher
relate, the ports through which devloop reaches GitHub, the boundary that keeps devloop
independent of the engine, and the `core` package for the code they share. The
sections from [Phases](#phases) on describe the local engine: phases, risk tiers, isolation, run
layout, controller contract and integrity model.

## Components

| Component | Code | What it does |
| --- | --- | --- |
| **Local engine** | `orchestrator.py`, `ssdlc_*.py`, `setup-ssdlc.sh`, `schemas/`, `standards/` | Runs a ticket for a target project on one host, through the [phases](#phases) below, with state, journal and evidence under `<project>/.ssdlc/`. `setup-ssdlc.sh` bootstraps the toolkit and initializes projects; `schemas/` holds the JSON schemas of the engine's records (plans, manifests, gate findings, recovery and reconciliation records); `standards/` holds the templates of a project's standards registry. |
| **devloop** | `devloop/`, `scripts/devloop-pinned` | Dispatches the issues and PRs of this repository to an implementer agent and an independent reviewer, through fix rounds, up to a merge by a human or, when the operator enables a merge mode, by devloop. Its state lives on GitHub, in labels and in markers in comments. |

Both components share the test suite in `tests/` (run by `scripts/run-tests.py` and by the CI,
see the [development guide](development.md)) and the documentation in `docs/`, with the
decisions in `docs/adr/`. The `core` package (`core/`) holds pure validators and calculations
written for both; devloop uses it for the budget envelope (#365), the engine not yet (see
[Future `core` package](#future-core-package)).

devloop's modules, by part:

| Part | Modules |
| --- | --- |
| Dispatcher | `devloop/dispatcher.py` (reads the GitHub state, plans the next steps of a tick, runs them), `devloop/runloop.py` (the continuous loop), `devloop/supervision.py` (escalation classes and supervisor commands), `devloop/localtests.py` (the tests run after a writing turn), `devloop/watch.py`, `devloop/activity.py`, `devloop/stall.py`, `devloop/follow.py` |
| GitHub | `devloop/ports.py`, `devloop/github.py`, `devloop/apps.py`: see [Ports and adapters](#ports-and-adapters) |
| Agents | `devloop/agents.py` (headless invocation of the agent CLIs), `devloop/schema.py` with `devloop/schemas/` (structured output), `devloop/prompts/`, `devloop/workspace.py` (Git worktrees), `devloop/unavailability.py` (models found unavailable) |
| Records and rules | `devloop/markdown.py` (attribution header and markers), `devloop/ledger.py` (evidence and telemetry), `devloop/policy.py` (review level and policy receipt), `devloop/review.py` (review records and findings), `devloop/acceptance.py` (acceptance criteria), `devloop/hoststep.py` (the [step on the host](devloop/guide.md#step-on-the-host) a PR declares), `devloop/budget.py` (the [budget envelope](devloop/reference.md#budget-envelope) of each story, with `core.budget`) |
| Installation | `devloop/config.py` with `devloop/config.toml`, `devloop/pin.py` and `scripts/devloop-pinned` (the [pinned installation](devloop/guide.md#run-from-a-pinned-installation)) |

The operator's view of devloop is in the [guide](devloop/guide.md), its configuration in the
[reference](devloop/reference.md) and its records in the [protocol](devloop/protocol.md).

## Engine and dispatcher

Today the two components do not call each other:

- **No run-time link.** devloop does not run the engine's pipeline, and the engine does not run
  devloop. devloop may import only the standard library, itself and `core` (see
  [The devloop boundary](#the-devloop-boundary)); the engine does not import `devloop`.
- **Each has its own pinned installation.** The engine's controller is bound to each run by the
  [controller contract](#controller-contract), and when SSDLC develops itself a pinned
  controller runs against a separate target checkout. devloop runs from a read-only copy of one
  commit, made by `devloop pin` and started by `scripts/devloop-pinned`, never from the checkout
  it changes.
- **Separate state.** The engine keeps the state of a run on the host: `state.json`, the
  journals and the execution generations under `.ssdlc/<ticket>/` (see
  [Run layout](#run-layout)). devloop keeps its state on GitHub, so a tick can stop at any
  point and the next one resumes.
- **devloop develops this repository, engine included.** The engine's code is what devloop's
  agents change, test and review: `test_cmd` runs the whole suite, engine tests included; with
  `local_tests = "changed"` a change to `ssdlc_<name>.py` selects `test_<name>*.py`
  ([Local tests](devloop/reference.md#local-tests)); the engine modules that decide fencing,
  transitions and authorizations are sensitive paths that need two reviewers
  ([Review level and policy receipt](devloop/reference.md#review-level-and-policy-receipt)); the
  [documentation map](devloop/reference.md#documentation-map) binds engine modules to their
  documents.
- **The engine has no dispatcher of its own.** Specification, design, plan and evidence are
  files on the host of the run; `08-supervisor` opens a draft PR and waits for human sign-off.

Issue #59 had planned the engine as the executor of one unit of work inside devloop; that was
never built. ADR 0001 replaced it with convergence, in steps: the tracker ports (step 1, done), budget
and durable state (step 2), the phases as issue and PR types next to the local engine (step 3),
containment and fencing of the executors (step 4), a filesystem backend (step 5), and the
convergence of the local engine only after demonstrated parity and an explicit migration
authorized by the operator (step 6). Until then the engine's guarantees are not removed or
replaced (ADR 0001, constraint 1).

## Ports and adapters

The dispatcher reaches GitHub through three ports, declared in `devloop/ports.py` as step 1 of
[ADR 0001](adr/0001-prodotto-unico-fasi-su-tracker.md):

| Port | Methods |
|---|---|
| `Tracker` | issues and their history, comments of issues and PRs, labels: `issues_with_label`, `issue`, `issue_history`, `issue_comments`, `pr_comments`, `labelers`, `label_events`, `labeled_events`, `edit_labels`, `comment`, `close_issue`, `edit_comment`, `ensure_label`, `repo_labels`, `rename_label` |
| `CodeReview` | PRs and reviews: `open_prs`, `merged_prs`, `find_pr` (#362), `branch_prs` (#384), `merge_state` (`clean`, `conflict` or `unknown`), `pr_head`, `pr_state`, `merge_pr`, `pr_reviews`, `create_pr`, `submit_review` |
| `CI` | checks of a commit: `checks` (summarized by `summarize_checks`), `check_details`, `check_runs`, `failed_log`, `job_log` (#300), `rerun_failed_jobs` (#200) |

`Tracker` also has `issue_dependencies` (#277): the native "blocked by" dependencies of an
issue, with the state of each blocking issue and who added each block, read with GraphQL (one
query, more only when the events of an open block are older than the latest 100)
([Dependencies between issues](devloop/guide.md#dependencies-between-issues)).

`CodeReview.find_pr` (#362) reads a PR in any state, or None when the number is an issue: the
dispatcher uses it for a legacy turn imported from `ledger-open-turns.json` whose PR is not among
the open ones, to link the turn to the issue of its work
([Turn journal](devloop/protocol.md#turn-journal)).

`CodeReview.branch_prs` (#384) reads the PRs of a head branch in any state, newest first, with
`state` and `createdAt`: with `attempt_identity` on, before resuming an attempt that has no PR
recorded, the dispatcher looks on the attempt's branch for a PR already published for it, so as
not to open a second PR of the same attempt
([Attempt identity](devloop/protocol.md#attempt-identity)).

`CI` also has `commit_date` and `required_checks` (#263): the commit date of a head and the
checks the base branch requires, for the wait for the
[CI of a new head](devloop/reference.md#ci-of-a-new-head).

`Tracker.comment` and `Tracker.edit_comment` tell three outcomes apart (#364), and so do
`CodeReview.submit_review` and `CodeReview.create_pr` with the publication outbox on (#385;
`GitHub.uncertain_publications`, #397: off, their failures are all certain): success, a
failure (`GitHubError`: nothing was written) and an **uncertain** outcome (`GitHubUncertain`):
a timeout, a connection lost after sending, a 5xx answer, so the write may have taken effect.
The adapter treats a 4xx answer and a failure before the connection opened (`dial tcp`, `error
connecting to`, a failed TLS handshake) as certain, and any other failure of these four writes
as uncertain. `GitHubUncertain` is a `GitHubError`: the dispatcher still handles it as a failed
write. `ports.reconcile_comment` tells whether an uncertain write took effect: it reads the
comments of the issue or PR back and returns the one written by the same login whose body is
exactly the one written, or that contains a given marker (for an edit, only the edited
comment), or None when the write was not published. The login may also be a set of logins (the
ambient `gh` login is known only as one of `trusted_users`), and `exclude` skips comments already
known as the publication of another write. The
[publication outbox](devloop/protocol.md#publication-outbox) of
[ADR 0004](adr/0004-passo-2-budget-stato-durevole.md) (#385, `devloop/outbox.py`) calls it
before any new POST when `publication_outbox` is on (for a review or a pull request it reads
the PR's native reviews or the open pull requests), and sends a write again only on an absence
read when the uncertain attempt could no longer take effect, counted from a time the attempt is
known to have ended, never from the time recorded before the send (after a stop, from the start
of the next dispatcher, only for an attempt whose `gh` held the dispatcher lock:
`GitHub.lock_fd`, passed to the `gh` of each write only with the outbox on, which then runs
under coreutils `timeout`, #397); it wraps the `comment`,
`edit_comment`, `submit_review` and `create_pr` of the clients of `Identities.github` and is
itself a `Backend`. Without it the dispatcher still handles an
uncertain outcome as a failed write. The other writes and the reads report every failure as a
`GitHubError`, as before.

The ports hold only the methods the dispatcher calls, with their current names and signatures,
and return normalized data. `GitHub` (`devloop/github.py`) implements all three, and `Identities`
(`devloop/apps.py`) hands one out for each actor: they are the concrete adapters. The
dispatcher, its ledger and `setup` depend on the ports only. There is no other backend yet; the
module docstring records where a method that could fit more than one port went.

In the tests, `FakeGH` (`tests/helpers/devloop_fakes.py`) implements the ports in memory. It
proves neither persistence nor atomicity. `tests/test_devloop_ports.py` checks that the adapter
and every fake expose each port method with the same signature. `FakeGH` can also simulate an
uncertain `comment` or `edit_comment`, applied or lost (`uncertain_writes`), and give its
comments ids (`with_ids`); the same test runs an uncertain write and its reconciliation on the
adapter, with a scripted `gh`, and on the fake.

## The devloop boundary

`tests/test_devloop_boundary.py` reads every `.py` file under `devloop/` and fails if any of
them imports a module other than the standard library (`sys.stdlib_module_names`), `devloop`
itself or the [`core` package](#future-core-package). Relative imports inside the package are
allowed. The same test checks `core`: its files may import only `math`, `re` and `core` itself
(no `ssdlc_*`, no `orchestrator`, no `devloop`, no third-party package), may not call `open`,
`exec`, `eval` or `__import__` and have no `global` statements, and importing `core` in a fresh
interpreter loads no engine or devloop module.

- **What it keeps.** devloop depends on no `ssdlc_*` module, on `orchestrator.py` and on no
  third-party package, neither directly nor through `core`. Its pinned copy needs no engine code,
  and a change to an engine module does not change the code that dispatches, reviews and merges.
  The engine is the candidate under test of the replay gate; the boundary keeps it out of the
  code that develops it.
- **What it does not check.** It reads `import` statements only: it does not see code loaded
  with `importlib` or `__import__`, subprocesses, or files read as data. The commands devloop
  runs in a worktree, such as `test_cmd`, run the repository's code, engine included, outside
  the boundary.
- **Who may change it.** The boundary comes from #59. The operator revoked it in a limited way
  on 2026-11-16 (ADR 0001, constraint 6 and decision 1): the only allowed widening is the `core`
  package below. Any other change to the allowlist is a new operator decision.

## Future `core` package

ADR 0001 lets devloop reuse code of the engine **only** through a declared `core` package,
added to the allowlist of `tests/test_devloop_boundary.py`. Its conditions:

- **Pure validators only**, such as the requirements catalogue and plan validation. No stateful
  loop: not `DesignSession`, not `orchestrator.py`.
- **Prompts and schemas reused as data**, copied into the pinned installation.
- **Never a direct import of `ssdlc_*` modules.**

The package exists since step 2 of ADR 0001
([ADR 0004](adr/0004-passo-2-budget-stato-durevole.md#34-riuso-del-pacchetto-core-e-confine-di-devloop),
P2-1), with the budget validators. It is written **next to** the engine, not extracted from it.
devloop uses `core.budget` for the [budget envelope](devloop/reference.md#budget-envelope) of
each story (#365, P2-5a); **the engine does not use it yet**, and will only when it converges
(step 6). Its modules:

| Module | What it holds | Engine counterpart |
|---|---|---|
| `core/schema.py` | A small JSON Schema validator for the schemas read as data. It knows only the keywords it lists and refuses a schema with any other one, instead of ignoring a constraint it cannot check | `validate_document` in `ssdlc_tasks.py`, with the `jsonschema` package |
| `core/budget.py` | Validation of a limits profile (`schemas/recovery-limits.schema.json`, passed in as data) and of the rules for unknown usage per agent (`reserve_per_invocation` or `unmetered_quota`); the charge of each invocation, the dollar caps and the capacity for an imminent call on a given state, with the current time as an argument; reported tokens per agent | `LimitsStore` and `validate_live_usage` in `ssdlc_recovery.py` |
| `core/telemetry.py` | The `ssdlc.cost-telemetry` record: its validation, its spend, estimate and tokens kept apart, the classification of ledger rows, the USD figures the caps read, route admission | `ssdlc_telemetry.py` |

The rules are the engine's:

- an agent that reports no dollar cost, such as codex or agy, needs a declared rule; without one
  the call is refused before launch. No reserve in USD is ever derived for it, from tokens or
  anything else: only the declared rule applies;
- a value that was not reported is unknown (`None`), never 0;
- a reserve stays charged and an observed cost can only raise it; an invocation without a
  confirmed outcome blocks any new one;
- tokens are never priced, and the tokens of different agents are never added or compared.

`core` does not validate the feedback sections of a profile (`feedback_workflow`,
`feedback_delegation`), and holds nothing with state: the journal, the lock and the deadline
and limit extensions stay in the engine, and devloop's own journal will stay in devloop.

`tests/test_core.py` tests the package alone. `tests/test_core_equivalence.py` runs the same
profiles, states, telemetry records, ledgers and routes through `core` and through the engine,
including every variant of reference profiles and records with one value deleted or replaced,
and requires the same answers: acceptance or refusal, refusal category and message, booked rows
and figures. The engine and its tests are unchanged.

Since devloop imports `core` (#365), its pinned copy includes it: `devloop pin` extracts
`devloop/`, `core/` when the commit has it, and the sensitive-path lists, and a merge by devloop
that touches `core/` triggers the [pin after a merge](devloop/guide.md#pin-after-a-merge) as one
that touches `devloop/` does.

## Phases

| Phase | Role | Focus and artifacts |
| :--- | :--- | :--- |
| **`00-recon`** | Codebase archaeologist | Maps blast radius, architectural impact and dependencies. Assigns the risk tier (`S`, `M`, `L`) and development profile (`FULLSTACK`, `ALGORITHMIC`, `REFACTOR`, `FRONTEND_UI`, …). |
| **`01-po`** | Product owner | Testable user stories, Given/When/Then acceptance criteria (`AC-*`) and security abuse cases (`SEC-*`). |
| **`02-design`** | Architect and security reviewer | Pair design session producing `adr.md` and `threat-model.md` (STRIDE). In task mode it also produces the reviewed task plan. |
| **`03-dev`** | Developer | Code and tests in an isolated worktree branch (`ssdlc/<ticket-id>`). In task mode, one task at a time from the plan DAG. |
| **`04-review`** | Multi-model review panel | Independent review by distinct models. Blocks on security flaws, regressions or spec violations. |
| **`05-qa`** | QA engineer | Deterministic test suites (`--test-cmd`) and acceptance-criteria validation. |
| **`05b-appsec`** | AppSec red-teamer | Dependency vetting, secret scanning, OWASP top-10 and supply-chain checks. |
| **`06-ops`** | DevSecOps / SRE | Deployment checklist, monitoring, metrics and rollback procedures. |
| **`07-docs`** | Technical writer | User documentation, migration guides and release notes. |
| **`08-supervisor`** | Evidence coordinator | Compiles `EVIDENCE_PACK.md`, synchronizes the remote, opens a draft PR and waits for human sign-off. |

## Risk tiers

With `--tier auto`, `00-recon` assigns the tier:

- **Tier S** (low risk, small fixes): `00-recon` → `01-po` → `03-dev` → `04-review` (single reviewer) → `05-qa` → `08-supervisor`. There is no pair design and no DevSecOps work.
- **Tier M** (standard feature): adds pair design, multi-model review and documentation.
- **Tier L** (high risk, architectural or fullstack): the full pipeline, including STRIDE threat modelling, consensus review, AppSec red-teaming and DevSecOps runbooks.

The tier also selects the default reasoning effort, the developer model and the budget caps (see [Configuration](configuration.md)).

## Isolation

```text
Host
├── /usr/local/bin, ~/.codex, ~/.claude      untouched
├── <ssdlc-toolkit>/
│   ├── orchestrator.py, ssdlc_*.py          controller
│   ├── setup-ssdlc.sh                       bootstrap and project initializer
│   └── .tools/                              shared CLIs (mounted read-only)
└── <project>/
    ├── ssdlc                                project-local CLI wrapper
    └── .ssdlc/
        ├── project.env                      project keys and routing
        ├── docker-compose.yml               container definition
        ├── .runtime/                        isolated CODEX_HOME, CLAUDE_CONFIG_DIR, agy_auth, resources
        ├── .supervisor/                     supervisor metadata, locks, audit
        └── <ticket>/                        see "Run layout"
```

- Several projects can run on the same host without credential collisions or SQLite lock conflicts.
- In Docker mode:
  - verification containers mount the source read-only and receive no runtime credentials;
  - role containers carry the `ssdlc.run_id` and `ssdlc.project` labels, used for scoped cancellation.
- When the toolkit or the target is a linked Git checkout, its shared Git metadata is mounted read-only. Git stays visible, but repository configuration and other worktrees cannot be written.

## Run layout

```text
.ssdlc/<ticket>/
├── state.json                     canonical run state (owned by the orchestrator)
├── 00-recon/ … 07-docs/           phase artifacts, evaluations, evidence
│   └── 00-recon/controller-contract.json
├── 03-dev/tasks/                  task mode: manifests, logs, diffs, task-journal.jsonl, tasks-state.json
├── worktree/                      isolated Git worktree
├── EVIDENCE_PACK.md
├── execution/active.json          selected execution generation (recovered runs)
├── execution-runtimes/<id>/       one directory per execution generation
└── feedback/                      feedback ledger (when the feedback workflow is enabled)
```

The journals are authoritative: `task-journal.jsonl` for tasks and `limits/task-journal.jsonl` for frozen limits. JSON state files are projections of them.

Recovery transitions never edit an existing generation. They create a new generation that preserves the journal prefix, attempts, invocations and limits, and select it atomically after an explicit authorization. Readers for status, journal, completion and deadline resolve the selected runtime. The one in-place change is a [manual limit extension](limit-extensions.md) (`limits activate`): it raises named limits or the deadline of the stopped run's frozen profile, appends to the journals instead of rewriting them, and records a bound chain that restores the authorized envelope. Its `reverify_after_timeout` operation changes no limit: it appends a grant to the task journal, and the resumed scheduler verifies the same captured candidate once more, with longer timeouts for the timed-out checks only. Its `allocate_task_budget` and `reallocate_task_budget` operations change only the per-task caps (`task_budget`) of the profile, never a run cap; every reservation records the task it belongs to or the shared run account (`ssdlc_task_budget.py`). Its `substitute_credit_cap` operation is the explicit transition that lets a cap in credits, already configured in the profile, replace the USD obligation of a subscription route; without it a credit cap only adds to the dollar caps ([Cost telemetry](cost-telemetry.md#caps-in-credits-and-existing-profiles)).

## Controller contract

Before new planning or the first task, the host captures and registers `00-recon/controller-contract.json`. It binds the path of the executing controller, its Git HEAD and status, and the hashes of its tracked and non-ignored files.

- The contract is checked before and after every developer invocation and verification check.
- A committed controller change is detected even when the checkout is clean.
- A missing or altered contract blocks execution.
- The fingerprint ignores global and system Git configuration, so it does not depend on `HOME`.

Keep the executing installation pinned to a reviewed version. When SSDLC develops itself, run that pinned controller against a separate target checkout. A run continues on its pinned controller; changing controller requires an explicit migration (see the [recovery index](recovery.md)).

## Integrity model and its limits

Every intermediate artifact is registered with a SHA-256 hash and checked on recovery. Changed upstream inputs block later phases instead of being silently reused. These checks have limits:

- **Unkeyed hashes are not authentication.** Anyone who can rewrite both the content and its hash defeats them.
- **Changes are detected only at checkpoints.** There is no continuous containment against external writers; containment is workstream B of the autonomy plan.
- **Coverage is partial.** Ignored Git files, unreferenced partial logs and artifacts outside the evidence inventory are not covered.
- **Strict parsing of model output** does not establish model honesty or eliminate prompt injection.
- **Locks** coordinate SSDLC processes. They do not stop arbitrary writers, and `source_fenced` in recovery authorizations remains an operator attestation (see [#45](https://github.com/brushtyler/ssdlc/issues/45)). Only for a run admitted to automatic fencing can a transition replace it with the host's proof under the writer gate ([Writer fencing](writer-fencing.md#native-transitions-the-automatic-proof)).

### Task journal: guarantees and threat model

`task-journal.jsonl` is a hash chain. Each event stores its `sequence`, the SHA-256 of the previous event (`previous`) and its own SHA-256 (`sha256`), computed over the canonical JSON of the event without a key. Loading the journal replays the whole chain and refuses recovery if any link is wrong. The chain provides **integrity checks, not authenticated provenance**: it shows that the journal is internally consistent, not who wrote it. The SHA-256 values are not signatures.

**Trust boundary.** The chain is meaningful only while untrusted code cannot write the ticket directory. In Docker mode, role containers mount the project read-only; the read-write mounts are `.ssdlc/.runtime`, the ticket worktree and its own Git metadata. Verification containers hide `.ssdlc` behind an empty tmpfs. The journal, plan, contracts and other control artifacts under `.ssdlc/<ticket>/` are therefore outside the reach of developer agents and checks. The boundary does not hold in `local` execution mode, if a run makes the repository root itself writable, or against anyone with write access to the ticket directory on the host: the operator account, the controller process and other host processes running as that user.

What the chain detects, as long as the attacker does not recompute it:

| Change to the journal | Result on load |
| --- | --- |
| Editing an event without updating its `sha256` | refused |
| Editing an event and its `sha256`, but not the next event's `previous` | refused |
| Deleting, reordering or duplicating an event in the middle | refused (`sequence`/`previous` mismatch) |
| Inserting an event copied from another journal | refused |
| An incomplete last line (crash during append) | truncated to the last complete event; the tail is archived as `interrupted-tail-<sha256>.bin` |
| Editing only `tasks-state.json` | ignored: the snapshot is rebuilt from the journal |
| Deleting the journal while the snapshot remains | refused |

What the chain alone does **not** detect:

- **Whole-log rewrite.** Anyone who can write the file can produce a different history with a recomputed, valid chain. No secret is involved.
- **Truncation.** Removing complete events from the end leaves a valid shorter chain.
- **Rollback and replay.** Restoring an older copy of the journal, or the journal of another run, yields a valid chain.

These need an **independent trust anchor**: a journal head (`sequence`, `sha256`) recorded before the change, stored where the attacker cannot rewrite it, and compared later. Some host records already store a head: bounce previews and corrective transactions refuse to continue if the journal moved or no longer contains the recorded head. These records live in the same ticket directory, though. They detect inconsistent edits and SSDLC's own concurrent changes, not a coordinated rewrite of the journal and its anchors by the same host user. Whatever an evidence pack or sign-off reports about the journal has the same limits.

#### Stronger provenance: evaluation

SSDLC does not implement authenticated provenance today. If a deployment needs it, the options are, from simplest:

1. **External checkpoint.** At phase boundaries and at sign-off, publish the journal head to a store that the host user cannot rewrite: an append-only or write-once store, a transparency log, or a comment posted by a separate identity. Later verification compares the local chain with the published heads. This detects rewrite, truncation and rollback before the last published checkpoint, but not changes after it.
2. **Keyed MAC (for example HMAC-SHA-256).** It helps only if the key is held by a service the controller cannot read. A key the controller can read can be used by anyone who controls the controller.
3. **Digital signature over checkpoints.** A signing service or hardware token signs the head; anyone with the public key can verify. It adds identity to option 1, but it is only as strong as the custody of the private key.

For options 2 and 3, the key must stay outside every developer and test sandbox. It must never be placed in the project, the ticket directory, `.ssdlc/.runtime` (mounted read-write in role containers), environment variables passed to containers, or the toolkit directory. Even then, a signature shows that the signer approved a head; it does not prove that the events are true. Signed checkpoints must still be combined with the controls above. Adopting one of these options is a product decision and is not part of the current task engine.
