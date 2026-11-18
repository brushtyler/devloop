# Recovery

A run of the local engine that stopped continues in one of two ways: it resumes, or it goes through an explicit **transition** that the operator reviews and authorizes. This page is the index: from the stop, to the transition, to the document that describes it.

Never edit journals, attempts, limits or selectors by hand to continue a run: the controller detects it and refuses to continue. Copying a newer controller into a stopped run is not a migration.

## From stop to transition

| Stop or situation | Transition | Document |
| --- | --- | --- |
| Run interrupted (process, host or terminal) with no stop that needs a decision | Resume: `resume` for supervised runs, `run --from-phase` for foreground runs | [Supervision](supervision.md#resume-and-fingerprints), [Troubleshooting](troubleshooting.md#resuming-an-interrupted-run) |
| Interrupted after the developer returned: capture, adoption, verification or `COMMIT_INTENT` | Native resume revalidates the capture receipt and runs fresh checks, without another developer call | [Candidate capture](candidate-capture.md#trust-and-state-contract) |
| `OUTCOME_UNCERTAIN` | Reconcile the invocation; no automatic redispatch or refund | [Bounded recovery](bounded-recovery.md#timeout-transport-and-lock-contracts) |
| `LIMIT_REACHED`: a frozen limit or the deadline is exhausted | Only an explicit, reviewed extension continues the run; limits are never raised automatically. `limits inspect`, `propose`, `approve`, `activate` (`operator-limit-extension-v1`) raise named global limits or the deadline | [Manual limit extensions](limit-extensions.md), [Bounded recovery](bounded-recovery.md#activation-and-limits) |
| `LIMIT_REACHED` because a live call has no `live_unknown_usage` rule: a `codex` or `agy` call refused before launch, or a call that reported no cost | `usage-profile-prepare` and `usage-profile-activate` (`live-usage-profile-admission-v1`) | [Bounded recovery](bounded-recovery.md#live-calls-without-a-reported-cost-issue-43), [Cost telemetry](cost-telemetry.md#separate-caps-and-route-admission) |
| `DESIGN_STALLED` | One named diagnosis at most, then an operator decision | [Bounded recovery](bounded-recovery.md#design-convergence) |
| `GIT_LOCK_UNOWNED` | Operator decision; no automatic cleanup without ownership proof | [Bounded recovery](bounded-recovery.md#stop-inventory-and-ci-authority) |
| `ENVIRONMENT_DIAGNOSIS` | Evidence is retained and no model repair loop runs; inspect the environment. A verifier timeout (exit 124) on a failed task can use `operator-verification-repair-v1` | [Bounded recovery](bounded-recovery.md#stop-inventory-and-ci-authority), [Scope amendments](scope-amendments.md#operator-repair-after-a-verification-timeout) |
| Stopped task run with no completed checkpoint, under an older controller | `continuation-inspect`, `-preview`, `-prepare`, `-activate`, `-verify` (`pre-checkpoint-import-v1`, I1) | [Continuations](continuations.md) |
| Stopped I1 generation that needs native development | `continuation-limits-inspect`, `-prepare`, `-activate` (`i1-cumulative-limits-v1`) | [Historical reconciliation](historical-reconciliation.md) |
| `SCOPE_REQUIRED` after a checkpoint, or a `CHECK_FAILURE` localized entirely outside the task manifest | `continuation-scope-inspect`, `-preview`, `-prepare`, `-activate` (reviewed scope amendment) | [Scope amendments](scope-amendments.md) |
| `SCOPE_REQUIRED` or an out-of-manifest `CHECK_FAILURE` on a run with feedback enabled | `feedback-diagnose`, then the delegated amendment or the automatic feedback workflow | [Developer feedback](developer-feedback.md#native-offline-diagnosis) |
| An added target was changed by a completed task | Owner revalidation; for an operator amendment, `--feedback-request`, `--operator-owner-limit` or `--operator-recovery-profile` | [Developer feedback](developer-feedback.md#completed-owner-scope-recovery-38) |
| Rejected feedback decision, and the existing implementation should be repaired | `feedback-repair-prepare` and `feedback-repair-activate` (`feedback-operator-repair-return-v1`) | [Developer feedback](developer-feedback.md#explicit-recovery-of-an-already-rejected-decision) |
| Existing I2 profile without a feedback grant | `feedback-profile-prepare` and `feedback-profile-activate` (`feedback-profile-admission-v1`) | [Developer feedback](developer-feedback.md#existing-i2-profile-admission) |
| A Review, QA or AppSec rejection that a targeted bounce could repair | `bounce-preview` (read-only); the corrective executor repairs and reruns the gates | [Bounce preview](bounce-preview.md), [Native gate convergence](native-gate-convergence.md) |
| Any transition while an old controller, supervisor or other writer may still run | Stop and fence every writer before preparing; `source_fenced: true` is the operator's attestation. Automatic fencing through the writer gate, and how far it is in use, is described in its own document | [Continuations](continuations.md), [Writer fencing](writer-fencing.md) |

The other stops of the local engine, and what to do about them, are listed in [Troubleshooting](troubleshooting.md#common-stops). The supervisor classes and the action each allows are in the [recovery matrix](supervision.md#recovery-matrix).

## How a transition works

The transitions above follow the same steps. Which steps a transition has, and what each one binds, is in its document.

1. **Inspect** the stopped run, where the transition has an inspection command. Inspection is read-only and never repairs a journal.
2. **Preview and review**, for continuations and scope amendments: the controller prints the subject without running repository code, and an independent review is bound to its hash.
3. **Prepare** a new generation. Preparation calls no model.
4. **Authorize** the exact proposal in a separate file that binds its hash and, where the transition requires it, the operator's own `source_fenced: true` attestation and every extension being approved.
5. **Activate**: the controller checks everything again and selects the new generation atomically. After an interruption, retry the exact same command.
6. **Resume** the run on the new generation.

A transition never refunds attempts or charged calls, and never raises a limit unless the authorization explicitly approves that extension. Hashes bind host-owned files: they are not authentication against a host user who can replace both the payload and the authorization.

## Recovery documents

| Document | Content |
| --- | --- |
| [Candidate capture](candidate-capture.md) | How developer output is captured and verified in disposable workspaces |
| [Bounded recovery](bounded-recovery.md) | Frozen limits, timeout capture, semantic design progress, stop inventory, A5 replay gate |
| [Manual limit extensions](limit-extensions.md) | Operator-authorized extension of named global limits or of the deadline of a stopped run |
| [Continuations](continuations.md) | Importing a stopped pre-checkpoint run for fresh verification |
| [Historical reconciliation](historical-reconciliation.md) | Admitting a stopped I1 generation to native development with cumulative limits |
| [Scope amendments](scope-amendments.md) | Recovering a task stopped on `SCOPE_REQUIRED` after a checkpoint; verification-timeout repair |
| [Developer feedback](developer-feedback.md) | Scope diagnosis, reviewed feedback roles, delegated amendments, owner revalidation, GitHub projections |
| [Native gate convergence](native-gate-convergence.md) | Candidate checks collected before the reviews that need them; corrective recovery |
| [Bounce preview](bounce-preview.md) | Read-only preview of a targeted bounce from a gate |
| [Writer fencing](writer-fencing.md) | Writer gate protocol and in-container PID 1 holder for automatic fencing |

Each recovery document keeps its reference first. How it was built, increment by increment (I0, I1, I2, F1–F3, issue #22), is in the **History** section at the end of the document.
