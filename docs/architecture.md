# Architecture

This repository is **devloop**: a dispatcher that develops a repository from its GitHub issues
and PRs with agents, independent reviews and a merge under the operator's rules. This page
describes its components, the ports through which it reaches GitHub, the boundary that keeps its
code independent, and the `core` package of its pure validators.

The local SSDLC engine that was developed here beside devloop (its orchestrator and modules,
schemas, standards and documents) is frozen since 2026-11-22 and left `main` with R1 of
[ADR 0007](adr/0007-da-ssdlc-a-devloop.md): it stays in the Git history and in the tag
`ssdlc-engine-final`, the last commit of `main` that holds it. [ADR 0001](adr/0001-prodotto-unico-fasi-su-tracker.md)
is superseded for the engine's part.

## Components

| Component | Code | What it does |
| --- | --- | --- |
| **devloop** | `devloop/`, `scripts/devloop-pinned` | Dispatches the issues and PRs of this repository to an implementer agent and an independent reviewer, through fix rounds, up to a merge by a human or, when the operator enables a merge mode, by devloop. Its state lives on GitHub, in labels and in markers in comments. |
| **`core`** | `core/` | Pure validators and calculations: devloop uses them for the budget envelope (#365), see [Future `core` package](#future-core-package). |
| **Host operations** | `ops/`, `scripts/agent-token.py`, `scripts/supervisor-token.py` | The installation of devloop on a host, the GitHub apps and their tokens. |

The test suite is in `tests/` (run by `scripts/run-tests.py` and by the CI, see the
[development guide](development.md)), the documentation in `docs/`, with the decisions in
`docs/adr/`.

devloop's modules, by part:

| Part | Modules |
| --- | --- |
| Dispatcher | `devloop/dispatcher.py` (reads the GitHub state, plans the next steps of a tick, runs them), `devloop/runloop.py` (the continuous loop), `devloop/supervision.py` (escalation classes and supervisor commands), `devloop/localtests.py` (the tests run after a writing turn), `devloop/watch.py`, `devloop/activity.py`, `devloop/stall.py`, `devloop/follow.py` |
| GitHub | `devloop/ports.py`, `devloop/github.py`, `devloop/apps.py`: see [Ports and adapters](#ports-and-adapters) |
| Agents | `devloop/agents.py` (headless invocation of the agent CLIs), `devloop/schema.py` with `devloop/schemas/` (structured output), `devloop/prompts/`, `devloop/workspace.py` (Git worktrees), `devloop/unavailability.py` (models found unavailable) |
| Records and rules | `devloop/markdown.py` (attribution header and markers), `devloop/i18n.py` (the [language](devloop/reference.md#language-of-the-texts) of the dispatcher's texts), `devloop/ledger.py` (evidence and telemetry), `devloop/policy.py` (review level and policy receipt), `devloop/review.py` (review records and findings), `devloop/acceptance.py` (acceptance criteria), `devloop/hoststep.py` (the [step on the host](devloop/guide.md#step-on-the-host) a PR declares), `devloop/budget.py` (the [budget envelope](devloop/reference.md#budget-envelope) of each story, with `core.budget`) |
| Installation | `devloop/config.py` with `devloop/config.toml`, `devloop/pin.py` and `scripts/devloop-pinned` (the [pinned installation](devloop/guide.md#run-from-a-pinned-installation)) |

The operator's view of devloop is in the [guide](devloop/guide.md), its configuration in the
[reference](devloop/reference.md) and its records in the [protocol](devloop/protocol.md).

devloop runs from a read-only copy of one commit, made by `devloop pin` and started by
`scripts/devloop-pinned`, never from the checkout it changes, and keeps its state on GitHub, so a
tick can stop at any point and the next one resumes.

## Ports and adapters

The dispatcher reaches GitHub through three ports, declared in `devloop/ports.py` as step 1 of
[ADR 0001](adr/0001-prodotto-unico-fasi-su-tracker.md):

| Port | Methods |
|---|---|
| `Tracker` | issues and their history, comments of issues and PRs, labels: `issues_with_label`, `open_issue_numbers`, `issue`, `issue_history`, `issue_comments`, `pr_comments`, `comments_since`, `labelers`, `label_events`, `labeled_events`, `edit_labels`, `comment`, `close_issue`, `edit_comment`, `ensure_label`, `repo_labels`, `rename_label` |
| `CodeReview` | PRs and reviews: `open_prs`, `merged_prs`, `find_pr` (#362), `commit_prs` (#473), `branch_prs` (#384), `merge_state` (`clean`, `conflict` or `unknown`), `pr_head`, `pr_state`, `merge_pr`, `pr_reviews`, `create_pr`, `submit_review` |
| `CI` | checks of a commit: `checks` (summarized by `summarize_checks`), `check_details`, `check_runs`, `failed_log`, `job_log` (#300), `rerun_failed_jobs` (#200) |

`CI.failed_log` raises `LogUnreadable`, with the error of `gh`, when `gh` cannot read the log
(#545): the dispatcher writes the reason to the tick log. The `GitHub` adapter runs each `gh` with
the temporary directory devloop gives it (`tmp_dir`, `<state_dir>/gh-tmp`, mode `700`), not the
system's `/tmp`, where the cache of the logs (`gh-cli-cache`) may belong to another user.

`Tracker` also has `issue_dependencies` (#277): the native "blocked by" dependencies of an
issue, with the state of each blocking issue, the reason of its closure (`stateReason`, #671)
and who added each block, read with GraphQL (one query, more only when the events of an open
block, or of one closed for a reason other than completed, are older than the latest 100)
([Dependencies between issues](devloop/guide.md#dependencies-between-issues)).

`Tracker.open_issue_numbers` (#656) lists the numbers of every open issue, page by page with no
limit on their count, and `Tracker.comments_since` reads the
comments of every issue and PR written or edited since a time, each with whether it belongs to a
PR: with `[roles.coordinator]` the dispatcher reads the threads of the open issues at its first
snapshot, then the comments written since its previous snapshot, to find the coordinator's
`/devloop authorize` on an issue whatever its labels
([Authorization by the coordinator](devloop/guide.md#authorization-by-the-coordinator)).

`CodeReview.find_pr` (#362) reads a PR in any state, or None when the number is an issue: the
dispatcher uses it for a legacy turn imported from `ledger-open-turns.json` whose PR is not among
the open ones, to link the turn to the issue of its work
([Turn journal](devloop/protocol.md#turn-journal)).

`CodeReview.commit_prs` (#473) reads the numbers of the merged PRs that GitHub associates with a
commit: with `[pin] auto`, before an auto-pin, the dispatcher reads the PRs merged by each new
commit of the base branch, so that a merge it did not make that declares a step on the host
holds the pin ([Step on the host](devloop/guide.md#step-on-the-host)).

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
(no module of the SSDLC engine, `ssdlc_*` or `orchestrator`, no `devloop`, no third-party
package), may not call `open`, `exec`, `eval` or `__import__` and have no `global` statements,
and importing `core` in a fresh interpreter loads no engine or devloop module.

- **What it keeps.** devloop depends on no third-party package, neither directly nor through
  `core`, and its pinned copy needs nothing but `devloop/` and `core/`. It depended on no module
  of the SSDLC engine either, which let the engine leave `main` (ADR 0007, R1) without a change
  to the code that dispatches, reviews and merges.
- **What it does not check.** It reads `import` statements only: it does not see code loaded
  with `importlib` or `__import__`, subprocesses, or files read as data. The commands devloop
  runs in a worktree, such as `test_cmd`, run the repository's code outside the boundary.
- **Who may change it.** The boundary comes from #59. The operator revoked it in a limited way
  on 2026-11-16 (ADR 0001, constraint 6 and decision 1): the only allowed widening is the `core`
  package below. Any other change to the allowlist is a new operator decision.

## Future `core` package

ADR 0001 let devloop reuse code of the engine **only** through a declared `core` package, added
to the allowlist of `tests/test_devloop_boundary.py`. Its conditions:

- **Pure validators only.** No stateful loop.
- **Prompts and schemas reused as data**, copied into the pinned installation.
- **Never a direct import of the engine's modules.**

The package exists since step 2 of ADR 0001
([ADR 0004](adr/0004-passo-2-budget-stato-durevole.md#34-riuso-del-pacchetto-core-e-confine-di-devloop),
P2-1), with the budget validators. It was written **next to** the engine, not extracted from it,
on the engine's rules. devloop uses `core.budget` for the
[budget envelope](devloop/reference.md#budget-envelope) of each story (#365, P2-5a). Its modules:

| Module | What it holds |
|---|---|
| `core/schema.py` | A small JSON Schema validator for the schemas read as data. It knows only the keywords it lists and refuses a schema with any other one, instead of ignoring a constraint it cannot check |
| `core/budget.py` | Validation of a limits profile (its schema passed in as data) and of the rules for unknown usage per agent (`reserve_per_invocation` or `unmetered_quota`); the charge of each invocation, the dollar caps and the capacity for an imminent call on a given state, with the current time as an argument; reported tokens per agent |
| `core/telemetry.py` | The `ssdlc.cost-telemetry` record: its validation, its spend, estimate and tokens kept apart, the classification of ledger rows, the USD figures the caps read, route admission |

The rules, the engine's:

- an agent that reports no dollar cost, such as codex or agy, needs a declared rule; without one
  the call is refused before launch. No reserve in USD is ever derived for it, from tokens or
  anything else: only the declared rule applies;
- a value that was not reported is unknown (`None`), never 0;
- a reserve stays charged and an observed cost can only raise it; an invocation without a
  confirmed outcome blocks any new one;
- tokens are never priced, and the tokens of different agents are never added or compared.

`core` does not validate the feedback sections of a profile (`feedback_workflow`,
`feedback_delegation`), and holds nothing with state: devloop's own journal stays in devloop.

`tests/test_core.py` tests the package alone, with the engine's schema of the limits profile as a
fixture (`tests/fixtures/core/recovery-limits.schema.json`). Until R1 of ADR 0007 a second test
ran the same cases through `core` and through the engine and required the same answers; it left
with the engine.

Since devloop imports `core` (#365), its pinned copy includes it: `devloop pin` extracts
`devloop/`, `core/` when the commit has it, and the sensitive-path lists, and a merge by devloop
that touches `core/` triggers the [pin after a merge](devloop/guide.md#pin-after-a-merge) as one
that touches `devloop/` does.
