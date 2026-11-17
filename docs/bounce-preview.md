# Targeted bounce preview

See the [native gate convergence guide](native-gate-convergence.md) for
execution, bootstrap and corrective recovery examples.

Unified task execution, explicit greenfield bootstrap and targeted corrective bounce
(Step 3b.3) are implemented. Every new run uses the scheduler, including a
single-node plan for Tier S. Historical sealed task runs retain their contracts,
attempts and budgets. Legacy runs remain readable; execution, resume and approval
are refused before record writes. Start a new ticket instead.

A trusted substantive Review, QA or AppSec rejection enters the corrective
executor. It verifies evidence, durably records a separate transaction, invalidates
exact roots, repairs them and revalidates dependents in topological order. All three
global gates run again on the final candidate before signoff readiness. Free text,
scanner failures and diagnostic reports grant no repair authority. The preview
remains immutable with `execution_authorized=false`.

Recovery reconciles both journals without duplicating invalidation or committed
repair. Unfinished transactions may append missing acknowledgments and required
work; only COMPLETED/APPROVED transaction replay is a zero-write no-op. An uncertain
developer dispatch stops for operator reconciliation without redispatch. Attempts,
retained evidence and frozen budget limits are preserved; raising or disabling an
environment limit cannot lift a transaction's frozen limit.

Advanced-target merge reconciliation, automatic scope migration, provider accounting
(#14), new billing units/quota polling, parallel scheduling, advisory acceptance and
subgoals remain later milestones. GitHub delivery (#4) is not one of them: it is
absorbed by [ADR 0001](adr/0001-prodotto-unico-fasi-su-tracker.md), which moves phase
outputs to the tracker, and its activation is discussed after parity. No
controller/runtime editing or new dependencies are introduced by this milestone.

Hashes are unkeyed integrity checks, not authentication. Verification provides
point-in-time integrity checks; no continuous containment is claimed. A
coordinated external rewrite of the integrity chain is outside this protection.

```bash
python3 orchestrator.py bounce-preview --repo . --ticket TKT-001 \
  --phase 04-review --attempt 1
```

The host API is `SSDLCOrchestrator.preview_bounce(phase, attempt)`. Select an exact
recorded task-mode gate (`04-review`, `05-qa` or `05b-appsec`); legacy tickets
without host provenance are rejected. The existing separately pinned controller
contract is required and is never recaptured by preview. Preview invokes no
model, developer, scanner or test runner and preserves raw evidence, checkpoint,
scheduler state, attempts, ledger and cost totals.

Canonical JSON and a readable summary, with ticket-relative source references,
SHA-256 hashes and a content-derived revision, live under
`bounce-preview/<phase>/attempt-<n>/<revision>/`. Publication is locked,
create-or-verify and registered through artifact references. Identical verified
reruns, including restarts, perform no artifact or registration writes. Matching
partial files are verified and completed; conflicting or registered-but-missing
artifacts block without overwriting old evidence. Changed verified inputs use a
new revision. Failed post-publication checks never produce a successful preview;
restart recovery always verifies again.

CLI exits: **0** means candidates with no blockers, eligible for the engine-owned corrective executor;
**10** means a produced non-executable or diagnostic preview (including an empty
plan); **20** means blocked or rejected; **2** means invalid CLI usage. Exit 0
grants no approval or execution authority. Every result has
`execution_authorized=false`. Local `preview_counters` track created, reused,
recovered and blocked-by-reason runs without touching provider accounting.
Logs contain selectors, revision, status, reason codes and counts, never finding
text or provider payloads.

## Corrective accounting in bounded continuations

The corrective executor uses the active cumulative I2 profile and its accounting
view, including explicit reserves for historical unknown usage. It preserves the
frozen additive or exclusive reserve model. Unknown observed costs remain null;
a reserve is an allocation, not an observed charge. Transaction-local and
environment limits can tighten that authority, never increase it. Existing narrower
transaction ceilings remain binding. Provider accounting and live-unknown cost
coverage remain separate work; this change does not add billing units.

Corrective publication can resume after cancel/budget interruption between the
final preflight acknowledgment and publication intent, including interrupted
archive writes. The recorded `REVALIDATED` authority is verified against the
unchanged journal and candidate before publishing; no repair or final preflight
is repeated. Lowering the gate ceiling below a recovered next attempt stops
explicitly as `gate_attempts_exhausted`. Budget stops retain `budget_interrupted`
instead of being reported as mutated evidence.

Ordinary task repairs retain cumulative head-and-tail excerpts of failed checks
(up to 6,000 characters per log and 24,000 overall, newest failures first). The
12,000-character corrective-context limit applies to corrective transactions and
is checked before reserving a developer attempt. Approved task instructions and
paths are preserved; pattern-based credential redaction applies to diagnostics,
while exact environment secrets and private-key material are scrubbed throughout.
