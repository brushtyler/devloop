### Increment I2

I2 implements A4.4–A4.7, A6b, the new A3 stop inventory and an initial A5 CI
replay gate from `PLAN_AUTONOMY_WORKSTREAMS.md`. This is direct bootstrap work
with semantic engines, real Git and credential-free Docker verification. It is
not an unattended delivery demonstration or the final autonomous profile.

### First increment for new runs (PR #26)

Existing tickets cannot acquire a fresh profile over historical accounting.
I1 imported generations remain verification-only until a separately reviewed
cumulative-limit reconciliation/authorization is supported through
[issue #27](https://github.com/brushtyler/ssdlc/issues/27), which completes I2.
PR #26 is its independently reviewable first increment for new runs. In particular this
increment neither resumes issue #22 nor treats its existing ledger/unknown
legacy caps as zero. This conservative compatibility refusal is deliberate;
the original source, counters and limits remain preserved. Do not copy this
controller over a frozen run or manually rewrite its state.

Other new-run I2 hardening remains in issue #29.

### Protected baseline and issue #29 validation

The v2 baseline adds oscillating findings, diagnosis-as-evidence and empty ADR
after diagnosis. `escaped_seeded_defects` is derived from the seeded repair's
observed source/checkpoint, not a constant success claim. Other metrics remain
out of scope until measured. The standalone replay is still
`python3 -I ssdlc_replay_gate.py --output FILE`; existing CI unit
discovery executes it and rejects pin drift or wrong outcomes/counters.

`tests/test_i2_hardening.py` additionally covers deleted/tampered mode bindings,
each bounded entrypoint, capture integrity refusals and active writers, isolated
protected violations, real role-adapter timeout fencing, frozen outer limits,
legacy HTTP retry and completion disclosures. Existing `test_not_dispatched`
regressions from #27 remain authority for trusted prelaunch refusal. Missing
role executables, unavailable native developer isolation, missing AGY credentials
and refused supervisor dispatch intent also carry trusted non-dispatch proof; arbitrary
post-launch exception text/category does not authorize replay. Timeout/import
completion uncertainty appears in the evidence pack and the explicit sign-off
output; passing task checks still grants no global readiness by itself.
Deterministic evidence compilation remains an authorized service operation after
historical admission, so a limit/stop report does not require another model call.

### Issue #22 fixture-boundary revision (v4)

The issue #22 v4 fixture-boundary revision extracts `RepositoryTests`, `finding`
and `review` unchanged into pinned `tests/replay/repository_fixtures.py`. Replay
cases import that module directly. Ordinary execution-mode tests still run in
the full unit suite and retain their assertions, but their complete modules are
no longer dependencies of the protected replay and are no longer pinned merely
because they previously hosted shared helpers. Existing imports of those helpers
from the ordinary test modules remain compatible re-exports. The grader, helper,
case implementations, expectations, isolated launcher and CI gate stay protected.
This permits separately reviewed task-only fixture adaptations without granting
candidate tasks authority to change the recovery oracle. It is not activation,
verification or a new budget authorization for the historical #22 run.
