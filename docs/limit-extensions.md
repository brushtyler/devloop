# Manual limit extensions

`operator-limit-extension-v1` is the common contract for extending the frozen
limits of a stopped bounded run, case by case, without a controller change for
each extension. It covers two operations: raising named global limits, and moving
the run's absolute deadline. Every extension needs an explicit operator
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
A run with a feedback recording ledger (`feedback/`) is refused: the ledger binds
the exact limits envelope, so its own admissions remain the way to change it.
I1 continuations are not eligible.

## Operations

| Operation | Changes | Never changes |
| --- | --- | --- |
| `extend_global_limit` | Exactly the named dimensions: `metered_usd`, `cli_estimate_usd`, `invocations`, an existing `role_invocations.<role>` or `phase_invocations.<phase>` cap, `development_attempts` | Every other profile field; `combined_reported_usd_cap`; the deadline |
| `extend_deadline` | The absolute deadline (`deadline_at`); `wall_seconds` grows by exactly the same integral number of seconds | `started_at`, every other profile field |

Each change records the old and the new value and must increase it. An unknown
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
   selector), the current consumption and reserves, the actor and the
   applicability conditions. It authorizes nothing.
3. Review the proposal, then authorize exactly that hash:
   `./ssdlc limits approve --ticket TICKET --proposal SHA256 --expires-at UNIX_SECONDS --actor NAME --source-fenced`.
   `--expires-at` is mandatory and must be in the future; `--source-fenced` is
   the operator's attestation that writers are stopped. The authorization
   (`schemas/limit-extension-authorization.schema.json`) is stored next to the
   proposal. The same decision can be repeated; a different one is refused.
4. `./ssdlc limits activate --ticket TICKET --proposal SHA256` checks the
   authorization against the exact proposal hash, the expiry, the Docker mounts
   of the run, and that the source still matches the proposal: same run,
   generation, inputs, journals and consumption. Any change is refused; write a
   new proposal. Then it applies the extension.
5. `./ssdlc limits status --ticket TICKET` lists the proposals (`PROPOSED`,
   `AUTHORIZED`, `EXPIRED`, `ACTIVATING`, `ACTIVE`) and the bound chain.

Resume the run as usual. A native run resumes with or without its original
`--limits-profile`; the extended envelope stays in force.

## Atomic activation and replay

Activation runs under the ticket, scheduler and limits locks. The expiry is
checked only at the first activation, before anything is written. Activation
then publishes an intent (`limits/extension-pending.json`) and applies, in
order: the new `limits/policy.json`, a `LIMITS_EXTENDED` event in the limits
journal, an `ATTEMPT_LIMIT_EXTENDED` event in the task journal when attempts
grow, the bounded-mode markers, and a receipt in `limit-extensions/<sha256>/`.
While the intent exists, every use of the limits is fenced.

After an interruption, repeat the same `limits activate`. The replay only
completes the published effects, even after the authorization has expired; each
step checks whether it already happened, so nothing is extended twice. A replay
of an active proposal only confirms it. Another proposal cannot activate while
an intent is pending, and a proposal bound to the parent of a completed
extension is refused.

## Integrity

The extended envelope keeps a bound chain in `binding.limit_extensions`: each
entry records the operation, the old and new values, the proposal and
authorization hashes and the hash of the parent envelope. Every load of the
limits undoes the chain of the current limits root step by step and refuses a
chain that does not restore its parent exactly. The selector of a reconciled run
still binds the envelope its generation was authorized with: resolution accepts
it only through this chain. A later generation inherits the chain as part of its
own authorized envelope.

Hashes bind host-owned files: they detect inconsistent edits, not a host user
who rewrites the proposal, the authorization and the journals together.
