# devloop: agent dispatcher for this repository

devloop moves issues through implementation, independent review and fixes. Its documentation is
split into three pages:

- [Guide](devloop/guide.md), for the operator and the supervisor: the loop, labels and
  `/devloop` commands, merge modes and escalations, watching a tick and the continuous loop, the
  pinned installation, limits.
- [Reference](devloop/reference.md): configuration, local tests, documentation map, agents and
  fallback models, review levels and policy, GitHub identities and apps.
- [Protocol](devloop/protocol.md): sync mechanics, rounds, markers and records (receipts, merge
  directives, structured review and findings, attribution, evidence and telemetry).

Ports and adapters are in [Pipeline and architecture](architecture.md#ports-and-adapters); roles
and merge modes in [ADR 0002](adr/0002-ruoli-e-responsabilita.md).
