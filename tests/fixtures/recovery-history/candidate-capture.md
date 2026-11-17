### Increment I0

Scope: [issue #23](https://github.com/brushtyler/ssdlc/issues/23), increment I0 of
`PLAN_AUTONOMY_WORKSTREAMS.md`. This is a direct bootstrap implementation, not an
autonomous SSDLC delivery demonstration. Offline validation is recorded in
`docs/evidence/i0-offline-validation.json`; final full-suite CI binds to the PR head.

The issue #22 preserved T01 has not been imported, verified or resumed by
implementing I0.

### Offline acceptance

`python3 -m unittest discover -s tests -p test_candidate_capture.py -v` discovers
the protected fixture and semantic adapters under `tests/replay/`. It uses real
Git operations and Python subprocesses with semantic role adapters. It exercises
the issue #22 cache mechanism, guard/capture/verification/commit interruptions,
tampering, unsafe changes, additions/deletions, persistent attempts and refusal
to silently change historical execution mode. No provider, subscription login
or real development run is needed for these cases.

### I2 integrity rejection hardening (#29)

Issue #29, part of the I2 hardening, added the behavior described in
[Integrity rejections](#integrity-rejections).
