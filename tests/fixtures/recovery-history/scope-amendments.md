### Post-checkpoint scope recovery (A6a)

This implements the post-checkpoint scope-recovery case needed by #22 under A6a
of `PLAN_AUTONOMY_WORKSTREAMS.md`.

Offline validation and its limits are recorded in
[the sanitized validation summary](evidence/post-checkpoint-scope-validation.json).

Issue #36 separately tracks native phase-boundary stops and
retaining failed-baseline diagnostics during I1/I2 preparation; scope-amendment
refusals keep their own [refusal records](#refusal-records).
