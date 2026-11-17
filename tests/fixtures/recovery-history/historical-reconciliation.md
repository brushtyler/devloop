Hardening of other new-run I2 paths is tracked separately in #29.

### Verification and next rollout

`tests/test_reconciliation.py`, `tests/test_historical_accounting.py`,
`tests/test_not_dispatched.py` and `tests/test_continuation_hardening.py` run in the existing
CI unit discovery gate. They exercise real Git/journals, native constructor and
provider wrapper accounting with semantic/fake engines; there are no paid calls.
The sanitized #22-shaped accounting replay includes 25 ledger entries, six
design rounds, four planning attempts, one T01 attempt, preserved limits and
unknown usage. It is not a replay of private model transcripts or a live resume.

After merge, reconcile the actual #22 candidate against latest main, obtain the
independent amendment review, prepare the concrete cumulative proposal and obtain
its authorization before resuming through SSDLC. Record before/after logical and
live calls, attempts, duplicated work, model/check/elapsed/operator-wait time and
required/avoidable interventions. Preserve the historical baseline and report
fresh global-gate outcomes; completing this offline PR is not that evaluation.
