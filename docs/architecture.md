# Pipeline and architecture

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

Recovery transitions never edit an existing generation. They create a new generation that preserves the journal prefix, attempts, invocations and limits, and select it atomically after an explicit authorization. Readers for status, journal, completion and deadline resolve the selected runtime.

## Controller contract

Before new planning or the first task, the host captures and registers `00-recon/controller-contract.json`. It binds the path of the executing controller, its Git HEAD and status, and the hashes of its tracked and non-ignored files.

- The contract is checked before and after every developer invocation and verification check.
- A committed controller change is detected even when the checkout is clean.
- A missing or altered contract blocks execution.
- The fingerprint ignores global and system Git configuration, so it does not depend on `HOME`.

Keep the executing installation pinned to a reviewed version. When SSDLC develops itself, run that pinned controller against a separate target checkout. A run continues on its pinned controller; changing controller requires an explicit migration (see the [recovery documents](../README.md#recovery-and-continuation)).

## Integrity model and its limits

Every intermediate artifact is registered with a SHA-256 hash and checked on recovery. Changed upstream inputs block later phases instead of being silently reused. These checks have limits:

- **Unkeyed hashes are not authentication.** Anyone who can rewrite both the content and its hash defeats them.
- **Changes are detected only at checkpoints.** There is no continuous containment against external writers; containment is workstream B of the autonomy plan.
- **Coverage is partial.** Ignored Git files, unreferenced partial logs and artifacts outside the evidence inventory are not covered.
- **Strict parsing of model output** does not establish model honesty or eliminate prompt injection.
- **Locks** coordinate SSDLC processes. They do not stop arbitrary writers, and `source_fenced` in recovery authorizations remains an operator attestation (see [#45](https://github.com/brushtyler/ssdlc/issues/45)).

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

## Ports and adapters

The dispatcher reaches GitHub through three ports, declared in `devloop/ports.py` as step 1 of
[ADR 0001](adr/0001-prodotto-unico-fasi-su-tracker.md):

| Port | Methods |
|---|---|
| `Tracker` | issues and their history, comments of issues and PRs, labels: `issues_with_label`, `issue`, `issue_history`, `issue_comments`, `pr_comments`, `labelers`, `label_events`, `labeled_events`, `edit_labels`, `comment`, `close_issue`, `edit_comment`, `ensure_label` |
| `CodeReview` | PRs and reviews: `open_prs`, `merged_prs`, `merge_state` (`clean`, `conflict` or `unknown`), `pr_head`, `pr_state`, `merge_pr`, `pr_reviews`, `create_pr`, `submit_review` |
| `CI` | checks of a commit: `checks` (summarized by `summarize_checks`), `check_details`, `check_runs`, `failed_log`, `rerun_failed_jobs` (#200) |

The ports hold only the methods the dispatcher calls, with their current names and signatures,
and return normalized data. `GitHub` (`devloop/github.py`) implements all three, and `Identities`
(`devloop/apps.py`) hands one out for each actor: they are the concrete adapters. The
dispatcher, its ledger and `setup` depend on the ports only. There is no other backend yet; the
module docstring records where a method that could fit more than one port went.

In the tests, `FakeGH` (`tests/helpers/devloop_fakes.py`) implements the ports in memory. It
proves neither persistence nor atomicity. `tests/test_devloop_ports.py` checks that the adapter
and every fake expose each port method with the same signature.
