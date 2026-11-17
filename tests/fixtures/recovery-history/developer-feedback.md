### Increments of issue #39

Issue #39 extends A6a. Its implementation is split into three increments.

### Implementation status

| Increment | Contract | Status |
| --- | --- | --- |
| F1 | Typed requests, conservative diagnosis, persistent routing/role receipts, ownership and independence metadata | Implemented offline; native inspection commands in [Native offline diagnosis](#native-offline-diagnosis) |
| F2 | Funded role adapter, delegated native activation, technical fencing, lineage and cumulative amendment limits | Implemented as an opt-in host API, validated offline; F3 wires the bounded production adapter |
| F3 | Supervisor recovery integration, sanitized idempotent GitHub projections, prerequisite proposals/return, consultative PO routing | Implemented opt-in, validated offline; prerequisite execution/return remains an explicit unsupported native transition |

Live rollout/measurement remains separate from offline acceptance. Native #38
owner recovery is described in
[Completed-owner scope recovery](#completed-owner-scope-recovery-38). This does
not change a running controller or
authorize migration of the historical #22 run. The current #22 T08 case is a
baseline test change and deliberately requires operator coverage review under
the first proposed policy, even after automatic diagnosis is implemented.

### Decisions from the two issue reviews

The [implementation decisions](https://github.com/brushtyler/ssdlc/issues/39#issuecomment-6807639765)
resolve the Claude/Gemini review comments as follows:

- A failing test outside the manifest is an impact-analysis hint, not proof
  that the test must change. Failed code remains a possible cause.
- Deduplicate by ticket, task, parent plan and normalized requested-file and
  failed-check/test sets. Attempt/capture bindings are occurrence provenance,
  never the sole cause key. Changed evidence cannot reuse an old approval.
- Record role identity, engine, model and context separately. Equal role or
  context identities are refused. Equal engine/model pairs or unknown model
  metadata are disclosed as `reduced_independence`; the proposed initial
  delegation policy escalates them to the operator.
- Protected paths are never delegated. Baseline test changes require coverage
  analysis and the operator. Completed ownership requires native owner
  revalidation; delegation additionally requires the explicit opt-in described in
  [Delegation and existing recording authorities](#delegation-and-existing-recording-authorities).
  Historical proof is never treated as fresh proof of an amended candidate.
- A rejected/inconclusive request does not redispatch the identical developer
  attempt. An alternative implementation needs a separately reviewed decision,
  bounded negative context and a normal charged development attempt.
- A local prerequisite is still a DAG change. Until a native transition supports
  it, record a blocking proposal without adding nodes or launching child runs.
- Diagnosis/planning/review are named roles, not new cost kinds. Their calls
  must share the frozen cumulative limits and cannot borrow `03-dev` attempts.
- Check useful remaining attempts and role capacity before consultation. Keep
  ticket/task amendment counts, cumulative file expansion, rounds and absence
  of progress persistent. No attempt or accounting refunds.
- Attribute predictable omissions to the registered historical impact report.
  Missing evidence means `UNKNOWN`; an unmatched report entry is not proof that
  the dependency was unpredictable.

### F2 offline acceptance

Offline acceptance covers funded activation and repair; six publication/quota
crash boundaries; lost prepare response; two native descendant generations;
missing execution grant or funded proof; controller/environment and writer/lock
guards; prepared candidate and historical-cost/response mutation. External
controller/Docker observations and model responses are simulated in contract
tests; journals, Git worktrees, locks, selectors and native scheduler are real.
The genuine-production-defect fixture separately proves rejection without
weakening the independent test or redispatching an identical developer attempt.

### Existing I2 profile admission and historical #22

Historical #22 is untouched. Adoption requires the latest merged controller,
inspection of its actual source and remaining caps, native discard/archive of
any older unselected preparation, and a concrete profile proposal approved by
the operator. Existing authorizations never acquire this grant implicitly.
Baseline T08 still requires the operator under the first coverage policy.

### Evidence and rollout

Offline acceptance uses real journals, Git worktrees, kernel locks and native
scheduler/selector transitions, with simulated providers and external host
observations. No paid calls or automatic #22 migration are part of this work.
Live rollout is opt-in after review: compare intervention/wait counts and latency
against the three historical manual amendments, while checking coverage and
end-to-end completion. Offline semantic fakes do not prove model quality or
live authentication. Production use retains the applicable B/I6 containment and
delivery requirements. The #38 owner-revalidation extension is described in
[Completed-owner scope recovery](#completed-owner-scope-recovery-38).
