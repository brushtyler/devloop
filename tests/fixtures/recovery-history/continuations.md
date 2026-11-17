### Increment I1

This implements the first compatibility rule in `PLAN_AUTONOMY_WORKSTREAMS.md`:
`pre-checkpoint-import-v1`. It is direct bootstrap work with offline semantic
engines and a credential-free Docker smoke, not autonomous delivery. Validation
and scope are recorded in `docs/evidence/i1-offline-validation.json`.

Issue #22's real run has only been inventoried read-only while implementing I1.
Its preserved T01 source must still be reconciled with the latest merged base,
independently reviewed and explicitly authorized using this path. No historical
self-reported tests are accepted as fresh results. The issue #20 regression
principle is covered by retaining historical gates separately from new baseline
and task proof; I1 does not reopen or duplicate its gate-evaluator implementation.
