# Manual limit extensions

`operator-limit-extension-v1` is the common contract for extending the frozen
limits of a stopped bounded run, case by case, without a controller change for
each extension. It covers raising named global limits, moving the run's
absolute deadline,
[verifying the same candidate again after a verifier timeout](#reverification-after-a-verifier-timeout),
and the [per-task budget](#per-task-budget-and-reallocation): the allocation of
an existing run and the transfer of unused cap from one task to another,
and the [substitution of a USD obligation by a cap in credits](#caps-in-credits).
Every extension needs an explicit operator
authorization; there is no automatic or delegated path (decision A5 of
[ADR 0001](adr/0001-prodotto-unico-fasi-su-tracker.md)). No command calls a model.

The specialized contracts stay valid and usable, with their historical receipts:
[verification repair](scope-amendments.md#operator-repair-after-a-verification-timeout)
and its [deadline extension](scope-amendments.md#separately-authorized-deadline-extension),
and the [operator owner recovery](developer-feedback.md#completed-owner-scope-recovery-38).
Scope authorizations stay separate. A pending proposal of another contract is
never converted: write a new proposal.

## Eligible runs

- a native ticket started with `--limits-profile` (its limits live in the ticket);
- the selected I2 generation of a reconciled run (`execution/active.json`).

The run must be stopped (`FAILED`, `ESCALATED_TO_SUPERVISOR` or
`HALTED_BUDGET_EXCEEDED`) and quiescent: no invocation in `reserved` or
`uncertain`, no task in progress, no interrupted journal tail, no pending
preparation or other extension. Reconcile writers and uncertain outcomes first.
I1 continuations are not eligible.

### Runs with a feedback ledger

A run with a feedback recording ledger (`feedback/`) is eligible when the ledger
is complete and quiescent:

- the recording authority is `ACTIVE` and its history has no interrupted tail;
- no role call has an unknown outcome (`INTENT`);
- no request still waits for a step bound to the current counters and journals
  (dispositions `WAITING`, `NATIVE_AMENDMENT_REQUIRED`,
  `NATIVE_REPAIR_RETURN_REQUIRED`, `OWNER_REVALIDATION_REQUIRED`). Complete
  those steps first.

The proposal binds the ledger's authority and journal head, and lists the other
unresolved requests (for example `OPERATOR_DECISION`). Those requests are not
converted: they stay bound to the parent counters, so after the extension a
new feedback request is needed to continue them.

Activation records the extension in the ledger
(`FEEDBACK_LIMITS_EXTENDED`, in `limit_extensions`) with the parent and new
envelope and profile hashes. The recording authority frozen on the parent
envelope, and a generation admitted with it, keep exactly that authority through
the recorded extension. A ledger row counts only if the envelope's own bound
chain contains the same proposal and authorization. Recording allowances,
feedback paths and scope authorizations stay separate. Raising a
`role_invocations.feedback-*` cap raises only the native cap, not the recording
allowance of the ledger.

## Operations

| Operation | Changes | Never changes |
| --- | --- | --- |
| `extend_global_limit` | Exactly the named dimensions: `metered_usd`, `cli_estimate_usd`, `invocations`, an existing `role_invocations.<role>` or `phase_invocations.<phase>` cap, `development_attempts` | Every other profile field; `combined_reported_usd_cap`; the deadline |
| `extend_deadline` | The absolute deadline (`deadline_at`); `wall_seconds` grows by exactly the same integral number of seconds | `started_at`, every other profile field |
| `reverify_after_timeout` | One more verification of the same captured candidate; the timeout of each named timed-out check (`check_timeout.<check_id>`), for that verification only | The whole profile, the limits journal and the chain; check commands, criteria and the other checks; the plan and quality contract |
| `allocate_task_budget` | Adds the per-task caps (`task_budget`) to a profile without them, from the residual of the run | Every run cap, counters, consumption; nothing consumed before is charged to a task |
| `reallocate_task_budget` | Moves available cap of one dimension from one allocated task to another | Every run cap and the sum of the task caps; counters, consumption, other tasks' caps |
| `substitute_credit_cap` | Lets a credit cap the profile already configures replace the USD obligation of its engine's subscription route (`credit_caps.<engine>.replaces_usd`, [caps in credits](#caps-in-credits)) | Every cap, the credit cap itself, counters, consumption, the original authorization |

For the extensions and the reverification, each change records the old and the
new value and must increase it. An unknown
operation, a dimension outside this list, a decrease, a duplicate or a value that
breaks `schemas/recovery-limits.schema.json` is refused. There is no implicit
increase: the proposal carries the exact resulting profile, and any difference
from the parent profile outside the named dimensions is refused, both at
proposal and at activation. Raising `development_attempts` also raises the
persistent attempt limit of the task journal by the same step; if that limit
differs from the profile, the extension is refused.

An already expired deadline can be extended. The new deadline must still be in
the future. The elapsed time is kept (`started_at` does not change), the limits
journal keeps the original deadline in its earlier events, and the envelope
records the old and new deadline and wall allowance. On reconciled runs the same
entry joins the bound deadline chain (`operator-limit-extension-deadline-v1`)
that the historical accounting anchor follows.

Counters, observed costs, unknown usage (which stays `null`, never 0), reserves,
transport retries, diagnoses and task attempts stay unchanged.

## Reverification after a verifier timeout

`reverify_after_timeout` verifies **the same candidate** again after a verifier
timeout, without a model call and without a new developer attempt. It applies to
a failed task whose last attempt has a quiescent captured candidate and ended in
`ENVIRONMENT_DIAGNOSIS` because of a timeout: at least one check exited 124 and
the others exited 0 or 124 (the same condition as
[verification repair](scope-amendments.md#operator-repair-after-a-verification-timeout)).

- **Only timed-out checks.** Each change names a check of the failed
  verification that exited 124, as `check_timeout.<check_id>`, with a new
  integral timeout longer than the one the check timed out with and at most the
  profile's `invocation_timeout_seconds`. The change records as `old` the
  configured timeout, which stays unchanged. Any other check, a timeout not
  longer than the one that timed out, a value beyond the bound and any limit
  dimension are refused.
- **Exit 124 alone justifies nothing.** The proposal cites, for every timed-out
  check, its configured timeout (`timeout_sec`), the timeout the failed
  verification actually ran it with (`applied_timeout_sec`: after a
  reverification that timed out again, the longer one it was granted), the
  duration the controller measured (`elapsed_seconds`) and its log with the
  SHA-256. The controller records the applied timeout and the duration with each
  exit-124 result. A failure recorded without them cannot be reverified: use
  verification repair or a new attempt.
- **Temporary.** The longer timeouts apply only to this reverification. The
  plan, the quality contract, the commands, the criteria, the other checks, the
  checkpoint preflight and every later verification keep their timeouts.
- **Three verifications per attempt.** The reverification counts among the
  three verifications of the captured candidate of an attempt
  (`capture_verifications`), with the one that timed out. When three are used,
  the proposal is refused, and the scheduler refuses a fourth as well.

Write a request with the task:

```json
{
  "operation": "reverify_after_timeout",
  "task_id": "T03",
  "changes": [{"dimension": "check_timeout.CHECK-GLOBAL", "new": 900}],
  "cause": "The suite outgrew its 600 s timeout on the captured candidate.",
  "evidence": [{"description": "timed-out check log", "sha256": "<sha256 of the log>"}],
  "actor": "<operator>"
}
```

The proposal adds a `reverification` object: the task and attempt, the hashes of
the failure and of the capture, the candidate tree, the verifications already
used and the limit, `invocation_timeout_seconds`, and the timed-out checks. Its
`new_profile` equals the current profile. Approve and activate it as usual.

Activation changes no limit: the envelope, the limits journal and the chain stay
as they are. It appends one `TIMEOUT_REVERIFICATION_AUTHORIZED` event to the
task journal, under the scheduler lock, with a grant bound to the proposal,
authorization, failure and capture hashes and to the exact old and new
timeouts. Then resume the run with `run --from-phase 03-dev`. After the
checkpoint preflight, the scheduler:

1. checks the grant against the last failure, the capture, the current check
   timeouts and the bound, then against sources the task journal does not hold:
   it must equal the grant that the stored proposal, the operator authorization
   and the activation receipt produce, and its timeouts must stay within the
   `invocation_timeout_seconds` of the frozen policy. Any difference, a grant
   edited in the task journal included, is refused;
2. records the verification (`TIMEOUT_REVERIFICATION_STARTED`), restores the
   archived candidate tree and loads its capture receipt;
3. runs the task and quality checks, with the longer timeout only for the
   granted checks, and the owner revalidation when one applies.

If every check passes, the task completes with the same candidate and the same
attempt number. Otherwise a new failed record (`origin:
timeout_reverification`) keeps the evidence, the candidate is rolled back and
the run stops; the grant is spent. If the reverification is interrupted, the
next resume marks it `INTERRUPTED` and rolls the candidate back without running
it again: the verification stays counted, and continuing needs a new proposal.

## Per-task budget and reallocation

Issue #163, with the operator's decision D4 of
[ADR 0004](adr/0004-passo-2-budget-stato-durevole.md#35-budget-per-task-e-trasferimenti-domande-di-163).

**Attribution.** Every reservation in the limits journal records its account,
and keeps it when the outcome is reconciled: `account: task` with the
`task_id` for a developer call made for a task, `account: run` for a call no
task owns (design, plan, diagnosis, gates, feedback). The shared run account is
always counted in the run caps and is never transferred to a task. Rows recorded
before this attribution have neither field and are listed as `unattributed`.

**Allocation.** The optional `task_budget` section of the profile
(`schemas/recovery-limits.schema.json`, `capability: task-budget-v1`) gives
some tasks a cap on their own cumulative consumption in the transferable
dimensions only: `invocations`, `role_invocations.<role>`,
`phase_invocations.<phase>`, `metered_usd` and `cli_estimate_usd`. Each
dimension stays separate. `development_attempts` stays a per-task cap of the
scheduler, and the deadline moves only with `extend_deadline`.

- The run caps stay the **defaults** and the overall limit. A task cap is an
  **override** on top of them: a task without one is bound by the run caps only,
  and an override never changes a run cap. A task cap needs the run cap of the
  same dimension, and the task caps of a dimension never add up to more than the
  run cap.
- A new profile declares its allocation, so it is part of the profile hash.
- An existing run gets one only through `allocate_task_budget`, an explicit
  transition on the model of `live-usage-profile-admission-v1`: it adds only
  the `task_budget` section, the quotas of a dimension must fit the residual of
  the run (the run cap less everything consumed or reserved), and the proposal
  lists the residual, the calls already made per task and on the shared and
  unattributed accounts. Nothing is reattributed: the invocations recorded
  before the allocation (`prior_invocations`) never count against a task cap.
  It names tasks of `02-design/plan.json`.

The pre-flight check before a task attempt and the atomic check at the
reservation, under the limits lock, both apply the task's caps. A task that
exhausts one stops the run with `LIMIT_REACHED`, as a run cap does.

**Transfer.** `reallocate_task_budget` moves cap from a donor task to a
recipient task. Both must have a cap in each transferred dimension. Only the
part of the donor's cap that is neither consumed nor committed moves: an open
or uncertain reservation is committed, and so is a dollar reserve
(`reserve_per_invocation`), which stays charged after the call. A transfer
creates no budget and refunds nothing: the run caps and the sum of the task caps
stay the same. Dollar task caps and transfer amounts are decimals with at most
six places (more is refused), so the donor loses exactly what the recipient
gains. Run caps and recorded charges are read as the decimal their JSON number
writes (`0.3` is three tenths, not the nearest binary float), so a charge equal
to a cap fills it exactly. They are compared at that exact value, never rounded: the
views show consumption rounded up and availability rounded down, and a
transfer is checked against the exact available amount. Without an allocation, a transfer is refused: a run created
before this feature first needs `allocate_task_budget`. Raising the overall cap
of the run stays an `extend_global_limit`.

```json
{
  "operation": "reallocate_task_budget",
  "transfer": {"from_task": "T01", "to_task": "T02",
               "amounts": {"invocations": 2, "cli_estimate_usd": 1.5}},
  "cause": "T01 completed with part of its cap unused; T02 needs one more attempt.",
  "evidence": [{"description": "limits inspect output", "sha256": "<sha256 of the saved output>"}],
  "actor": "<operator>"
}
```

An allocation request names `"operation": "allocate_task_budget"` and
`"allocation": {"T01": {"invocations": 3, "cli_estimate_usd": 4}, ...}`. The
host computes the `changes`, one per task and dimension
(`task_budget.<task>.<dimension>`, with `old: null` for an allocation), and the
resulting profile; a request that names `changes` is refused.

**Effects.** The proposal carries a `task_budget` object with the request, the
run defaults, both accounts and the `effects` on every task of the allocation,
the scheduler and the plan: its status, its override and available amounts
before and after, whether the operation changes it, and whether it depends on a
changed task (`depends_on_changed`). The operator authorizes these effects with
the proposal hash. `limits status` repeats them for each budget proposal
(`task_budget_effects`), and `limits inspect` and `limits status` show the
current `task_budget` view: defaults, per-task overrides and usage, accounts,
and whether transfers are enabled.

Activation is the one of the other operations: the new `limits/policy.json`, a
`TASK_BUDGET_ALLOCATED` or `TASK_BUDGET_REALLOCATED` event in the limits
journal with unchanged counters, the bound chain entry and the receipt. The
pending intent fences every reservation until the activation completes. Two
proposals built on the same parent cannot both apply: the ticket and limits
locks serialize activations, and the second one finds that its parent changed.
A replay completes the published effects once.

## Caps in credits

Issue #214, with the operator's decision D6 of
[ADR 0004](adr/0004-passo-2-budget-stato-durevole.md#5-decisioni-riservate-alloperatore).
A credit cap of the profile (`credit_caps`, see
[Cost telemetry](cost-telemetry.md#caps-in-credits-and-existing-profiles)) adds
to the USD caps and does not replace their obligation: a `codex` or `agy` call
without a `live_unknown_usage` rule is refused before launch even with a credit
cap. `substitute_credit_cap` is the explicit transition of that profile that lets
the credit cap replace the USD obligation of the engine's subscription route.

- It applies only to a credit cap the frozen profile already configures, for an
  engine without a `live_unknown_usage` rule. A profile in USD is never
  migrated: there is nothing to substitute.
- Each change is `{"dimension": "credit_caps.<engine>.replaces_usd", "new": true}`
  (recorded with `old: false`). Any other dimension or value is refused.
- The profile changes only by that flag. A new profile that declares it is
  refused: the substitution exists only through this authorized transition, and
  every load refuses a flag without its entry in the bound chain.

```json
{
  "operation": "substitute_credit_cap",
  "changes": [{"dimension": "credit_caps.agy.replaces_usd", "new": true}],
  "cause": "The agy plan charges credits; the operator accepts its credit cap for this run.",
  "evidence": [{"description": "plan terms", "sha256": "<sha256 of the saved document>"}],
  "actor": "<operator>"
}
```

Activation is the one of the other operations: the new `limits/policy.json`, a
`CREDIT_CAP_SUBSTITUTED` event in the limits journal with unchanged counters,
the bound chain entry and the receipt. The journal keeps its history and the
original `LIMITS_AUTHORIZED` event; undoing the chain entry restores the
authorized envelope exactly. Resuming with the original `--limits-profile` keeps
the transition in force.

## Workflow

Run the commands with a pinned controller, after stopping and fencing the
controller, the supervisor and every other writer.

1. `./ssdlc limits inspect --ticket TICKET` prints the source (`native` or
   `reconciled` and its generation), the current profile, the consumption and
   whether the run is eligible. It writes nothing.
2. Write a request and run `./ssdlc limits propose --ticket TICKET --request REQUEST`:

   ```json
   {
     "operation": "extend_global_limit",
     "changes": [{"dimension": "metered_usd", "new": 20},
                 {"dimension": "role_invocations.03-dev", "new": 6}],
     "cause": "The reviewed task exhausted the dollar cap on its last attempt.",
     "evidence": [{"description": "limits inspect output", "sha256": "<sha256 of the saved output>"}],
     "actor": "<operator>",
     "task_id": "T03"
   }
   ```

   For a deadline, use `"operation": "extend_deadline"` and the single change
   `{"dimension": "deadline_at", "new": UNIX_SECONDS}`. `task_id` is optional.
   A request may also carry the full expected `profile`; it must equal the
   computed one. The controller stores the proposal
   (`schemas/limit-extension-proposal.schema.json`) under
   `limit-extensions/<sha256>/proposal.json` and prints its SHA-256. The proposal
   records the cause and evidence, the run identity, the parent generation (the
   envelope, profile and counter hashes, the limits and task journal heads, the
   selector, the hashes of the run inputs `state.json`, `02-design/plan.json`,
   `00-recon/quality-contract.json` and the context documents, with `null` for
   an absent one, and the feedback ledger binding), the current consumption and
   reserves, the actor and the applicability conditions. It authorizes nothing.
3. Review the proposal, then authorize exactly that hash:
   `./ssdlc limits approve --ticket TICKET --proposal SHA256 --expires-at UNIX_SECONDS --actor NAME --source-fenced`.
   `--expires-at` is mandatory and must be in the future; `--source-fenced` is
   the operator's attestation that writers are stopped. The authorization
   (`schemas/limit-extension-authorization.schema.json`) is stored next to the
   proposal. The same decision can be repeated; a different one is refused.
4. `./ssdlc limits activate --ticket TICKET --proposal SHA256` checks the
   authorization against the exact proposal hash, the expiry, the Docker mounts
   of the run, and that the source still matches the proposal: same run,
   generation, input files, journals, feedback ledger and consumption. Any
   change is refused; write a new proposal. Then it applies the extension.
5. `./ssdlc limits status --ticket TICKET` lists the proposals (`PROPOSED`,
   `AUTHORIZED`, `EXPIRED`, `ACTIVATING`, `ACTIVE`) and the bound chain.

`limits inspect` and `limits status` also show the run deadline
(`run_deadline`: `deadline_at` and `ACTIVE` or `EXPIRED`) and the dependent
authorizations that an extension does not renew (`dependent_authorizations`).
Today this is the feedback recording of a run with a feedback ledger: its
status, its own `expires_at` and reason, and `renewed_by_run_extension: false`.
Extending the deadline of a run whose recording has expired leaves the
recording `EXPIRED`: the operations that depend on it stay blocked (see
[Expired recording](developer-feedback.md#expired-recording)), the others
proceed.

Resume the run as usual. A native run resumes with or without its original
`--limits-profile`; the extended envelope stays in force.

## Atomic activation and replay

Activation runs under the ticket, scheduler and limits locks, and under the
feedback consultation and ledger locks when the run has a feedback ledger. The
expiry is checked only at the first activation: before the source checks and
again immediately before the intent is published, so an authorization (or a
proposed deadline) that expires during the checks is refused and nothing is
written. Activation then publishes an intent (`limits/extension-pending.json`)
and applies, in order: the new `limits/policy.json`, a `LIMITS_EXTENDED` event in
the limits journal, an `ATTEMPT_LIMIT_EXTENDED` event in the task journal when
attempts grow, the `FEEDBACK_LIMITS_EXTENDED` event in the feedback ledger when
there is one, the bounded-mode markers, and a receipt in
`limit-extensions/<sha256>/`. A `reverify_after_timeout` activation applies
only the task-journal grant and the receipt.
While the intent exists, every use of the limits is fenced.

After an interruption, repeat the same `limits activate`. The replay only
completes the published effects, even after the authorization has expired; each
step checks whether it already happened, so nothing is extended twice. A torn
append of the feedback event itself is trimmed (and kept as
`interrupted-tail-*.bin`) and written again; any other interrupted feedback
history is refused. A replay
of an active proposal only confirms it. Another proposal cannot activate while
an intent is pending, and a proposal bound to the parent of a completed
extension is refused.

## Integrity

The extended envelope keeps a bound chain in `binding.limit_extensions`: each
entry records the operation, the old and new values, the proposal and
authorization hashes and the hash of the parent envelope. An
`allocate_task_budget` entry also records the hash of the whole allocated
`task_budget` section (`task_budget_sha256`, `prior_invocations` included):
undoing it checks that hash and the recorded changes before removing the
section, so an altered, added or dropped task cap is refused. Every load of the
limits undoes the chain of the current limits root step by step and refuses a
chain that does not restore its parent exactly. The selector of a reconciled run
still binds the envelope its generation was authorized with: resolution accepts
it only through this chain. A later generation inherits the chain as part of its
own authorized envelope. A reverification changes no envelope, so it has no
chain entry: its grant lives in the task journal and its receipt next to the
proposal.

Hashes bind host-owned files: they detect inconsistent edits, not a host user
who rewrites the proposal, the authorization and the journals together.
