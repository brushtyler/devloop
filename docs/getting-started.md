# Getting started with the local engine

This guide is for users of the SSDLC **local engine** (`orchestrator.py`, `ssdlc_*.py`), which runs a ticket for a target project on one host. It covers requirements, installation, a first project, basic configuration, everyday commands and where files are stored. The [README](../README.md) describes the product, its components and their status; devloop, the GitHub dispatcher, has its own [operator guide](devloop/guide.md).

---

## Key principles

- **No global configuration pollution.** `~/.codex`, `~/.claude`, global `npm` and Python stay untouched. Each project has its own runtime (`<project>/.ssdlc/.runtime/`), and CLI binaries live in the toolkit's `.tools/` directory.
- **Cross-model verification.** A model never approves its own work. Design, review, QA and AppSec are assigned to different roles and, where available, to different engines.
- **Isolated execution.** Development happens in a project-local Git worktree. In Docker mode, role and verification containers have resource limits and no host credentials; verification mounts the source read-only.
- **Evidence by default.** Inputs, outputs, decisions and check logs are archived with SHA-256 hashes and compiled into `EVIDENCE_PACK.md`. The hashes are unkeyed integrity checks, not authentication (see [Architecture](architecture.md#integrity-model-and-its-limits)).
- **Explicit authority.** Approvals, limit extensions, recovery transitions and merges need an explicit operator decision. Nothing is approved because a limit ran out.

---

## How a run works

```mermaid
flowchart TD
    P0["00-recon<br/>(blast radius & risk tier)"] --> P1["01-po<br/>(stories, AC-*, SEC-*)"]
    P1 --> P2["02-design<br/>(ADR & STRIDE review)"]
    P2 --> P3["03-dev<br/>(task DAG in isolated worktree)"]
    P3 --> P4["04-review<br/>(multi-model review)"]
    P4 -->|changes requested| P3
    P4 -->|approved| P5["05-qa"]
    P5 -->|failed| P3
    P5 --> P5b["05b-appsec"]
    P5b --> P6["06-ops"]
    P6 --> P7["07-docs"]
    P7 --> P8["08-supervisor<br/>(evidence pack, draft PR, human sign-off)"]
```

`00-recon` assigns a **risk tier** that decides how much of the pipeline runs:

| Tier | Typical change | Pipeline |
| --- | --- | --- |
| **S** | small fix | recon → PO → dev → single review → QA → supervisor |
| **M** | standard feature | adds pair design, multi-model review and docs |
| **L** | high risk or architectural | full pipeline, including STRIDE threat model, AppSec and ops runbooks |

**Every new run uses task mode.** Design produces a reviewed **task plan**: a DAG of tasks with file manifests, owned requirements and automated checks. Development then runs one task at a time:

1. The host checks the baseline at a clean checkpoint, then dispatches the developer into a disposable workspace.
2. The candidate is **captured** and verified by the host. Only passing checks create a Git checkpoint.
3. Failed attempts are archived and repaired in place, within persistent attempt limits. Identical failing repairs stop as `REPAIR_STALLED`.
4. A developer that needs files outside its manifest stops with `SCOPE_REQUIRED`. The run then goes through a reviewed scope amendment, never through silent expansion of the manifest.
5. Global review, QA and AppSec run on the verified result. They use structured, nonce-bound gate reports.

All new runs use the task scheduler, including Tier S. See [unified execution and bootstrap](task-mode.md#unified-execution-and-greenfield-bootstrap), [corrective bounce](bounce-preview.md) and [native gate convergence](native-gate-convergence.md).

Runs can also be **supervised** in the background, **resumed** from a phase, and **recovered** through explicit, hash-bound transitions under frozen cumulative limits. See the [recovery index](recovery.md).

Details: [Pipeline and architecture](architecture.md) · [Task mode](task-mode.md).

---

## Requirements

- Linux (supervision, flock-based locking and `/proc` checks are Linux-only). macOS can run foreground pipelines.
- Docker, recommended for isolation (`SSDLC_EXECUTION_MODE=docker`).
- Git 2.30 or later and Python 3.12.
- At least one AI provider: a Claude or ChatGPT subscription logged in on the host, an Antigravity login, or an `OPENROUTER_API_KEY`.

---

## Installation

```bash
git clone git@github.com:brushtyler/ssdlc.git
cd ssdlc
./setup-ssdlc.sh --bootstrap
```

`--bootstrap` installs the CLIs and Python requirements into `.tools/` and builds the shared runner image `ssdlc-runner:latest`. Later, `./setup-ssdlc.sh --update-tools` updates the CLIs for every project at once (see [Configuration › Maintenance](configuration.md#maintenance)).

Log in to your subscriptions **once on the host**. Projects inherit the credentials automatically:

```bash
codex auth login        # ChatGPT Plus/Pro/Team → ~/.codex/auth.json
claude auth login       # Claude Pro/Max/Team   → ~/.claude/.credentials.json
```

For headless hosts and other options, see [Configuration › Authentication](configuration.md#authentication).

---

## Your first project

**1. Initialize SSDLC in the target repository** (run from the toolkit directory):

```bash
./setup-ssdlc.sh --init-project /path/to/my-project --id my-project --mode docker --sync-remote auto
```

This creates, inside the project, `.ssdlc/project.env` (configuration, mode `600`), `.ssdlc/docker-compose.yml`, the isolated `.ssdlc/.runtime/`, an Antigravity skill in `.agents/skills/ssdlc/`, and the `./ssdlc` wrapper.

**2. Check the environment:**

```bash
cd /path/to/my-project
./ssdlc doctor
```

`doctor` verifies the CLIs, isolation, credential fingerprints and GitHub token visibility. It also reports obsolete, unknown or duplicated `SSDLC_*` settings in `project.env` and `global.env`.

**3. Run a ticket** from a clean, committed baseline, with a narrowly scoped goal:

```bash
./ssdlc run --ticket TKT-001-auth \
  --goal "Implement OAuth2 PKCE authentication flow with unit tests" \
  --tier auto
```

A filename in the goal does not pass that file's content to API-based roles; describe what you need.

**4. Review the evidence and decide:**

```bash
cat .ssdlc/TKT-001-auth/EVIDENCE_PACK.md
./ssdlc approve TKT-001-auth                       # merge the verified result
./ssdlc reject  TKT-001-auth --reason "Persist refresh tokens in secure storage"
```

---

## Basic configuration

SSDLC reads, in order of precedence:

1. the **process environment**;
2. **`<project>/.ssdlc/project.env`**, per-project settings;
3. **`~/.ssdlc/global.env`**, host-wide defaults inherited by every project.

A minimal `~/.ssdlc/global.env`:

```bash
export SSDLC_INHERIT_HOST_CODEX_AUTH="true"    # copy ~/.codex/auth.json into each project runtime
export SSDLC_INHERIT_HOST_CLAUDE_AUTH="true"   # copy Claude credentials into each project runtime
export SSDLC_MAX_REASONING_EFFORT="medium"     # upper bound for every role
# export OPENROUTER_API_KEY="sk-or-v1-..."     # optional fallback provider
```

The settings used most often:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SSDLC_EXECUTION_MODE` | `docker` | `docker` (isolated) or `local` (trusted code only) |
| `SSDLC_ENABLE_TASK_BREAKDOWN` | Ignored | Obsolete; all new runs use the task plan, scheduler and structured gates |
| `SSDLC_DEFAULT_TIER` | `auto` | Risk tier when `--tier` is not given |
| `SSDLC_<ROLE>_ENGINE` / `_MODEL` | `auto` | Per-role routing override |
| `SSDLC_BUDGET_CAP_USD` | S $3, M $6, L $20 | Measured-spend circuit breaker: charges only, never CLI estimates |
| `SSDLC_ESTIMATE_CAP_USD` | S $3, M $6, L $20 | Separate circuit breaker on API-equivalent estimates (Claude CLI) |
| `SSDLC_UNMETERED_QUOTA` | none | Subscription CLIs (`codex,agy`) declared outside the dollar caps without a limits profile; undeclared ones are refused before launch while a cap is in force |
| `SSDLC_QUOTA_CONTROLS` | none | Explicit subscription quota controls per route and bucket, with a mandatory minimum reserve and snapshot age; a reserve reached or an unverifiable snapshot refuses the call |
| `SSDLC_SYNC_REMOTE` | `auto` | GitHub/GitLab synchronization of branches, issues and draft PRs |
| `GITHUB_TOKEN` / `GH_TOKEN` | — | Needed to create issues and PRs (`export GITHUB_TOKEN="$(gh auth token)"`) |

The full reference covers routing tables, reasoning effort, budgets, container limits, standards profiles, remote sync and maintenance: [docs/configuration.md](configuration.md).

---

## Everyday commands

| Command | Purpose |
| --- | --- |
| `./ssdlc doctor` | Check tools, isolation and credentials |
| `./ssdlc run --ticket T --goal "…" [--tier auto]` | Run the pipeline in the foreground |
| `./ssdlc run … --from-phase 03-dev` | Resume from a phase, preserving approved upstream work |
| `./ssdlc run … --until-phase 03-dev` | Stop as `PAUSED_AT_PHASE_BOUNDARY` before the global gates; continue with `--from-phase 04-review` ([docs](task-mode.md#stopping-at-a-phase-boundary)) |
| `./ssdlc po` / `design` / `plan --ticket T` | Revalidate PO output, resume design, retry task planning (task mode) |
| `./ssdlc approve T` / `reject T --reason "…"` | Human sign-off |
| `python3 orchestrator.py start` / `status` / `cancel` / `resume` / `monitor` | Detached supervision ([docs](supervision.md)) |
| `python3 orchestrator.py bounce-preview …` | Read-only targeted-bounce preview ([docs](bounce-preview.md)) |
| `python3 orchestrator.py continuation-*` | Explicit recovery transitions for stopped runs ([docs](recovery.md)) |
| `python3 orchestrator.py usage-profile-*` | Add engine usage rules to the limits profile of a stopped generation ([docs](bounded-recovery.md#live-calls-without-a-reported-cost-issue-43)) |
| `python3 orchestrator.py feedback-*` | Developer-feedback diagnosis and delegation ([docs](developer-feedback.md)) |

---

## Where things are stored

```text
<ssdlc-toolkit>/
├── orchestrator.py, ssdlc_*.py      controller and modules
├── setup-ssdlc.sh                   bootstrap and project initializer
├── schemas/                         versioned JSON schemas
├── standards/                       default project standards
└── .tools/                          shared CLIs and Python environment

<project>/
├── ssdlc                            project-local CLI wrapper
└── .ssdlc/
    ├── project.env                  project configuration and keys
    ├── .runtime/                    isolated CODEX_HOME, CLAUDE_CONFIG_DIR, resource records
    ├── .supervisor/                 supervisor metadata, locks and audit (detached runs)
    └── <ticket>/
        ├── state.json               canonical run state
        ├── 00-recon/ … 07-docs/     phase artifacts and evidence
        ├── worktree/                isolated Git worktree
        └── EVIDENCE_PACK.md         evidence summary for sign-off
```

Recovered runs keep each generation under `<ticket>/execution-runtimes/<id>/`, selected by `<ticket>/execution/active.json`. See [Architecture](architecture.md#run-layout).

---

## Next steps

The [documentation index](../README.md#documentation) lists every guide by audience: configuration, architecture, task mode, supervision, cost telemetry, troubleshooting and the recovery documents.
