# devloop: agent dispatcher for this repository

devloop moves issues through implementation, independent review and fixes. Its documentation is
split into three pages:

- [Guide](devloop/guide.md), for the operator and the supervisor: the loop, labels and
  `/devloop` commands, merge modes and escalations, watching a tick and the continuous loop, the
  installation from the dedicated user to the rollback (keys, agent CLIs, pin, overlay, start
  and stop), limits.
- [Reference](devloop/reference.md): configuration, local tests, documentation map, agents and
  fallback models, review levels and policy, GitHub identities and apps.
- [Protocol](devloop/protocol.md): sync mechanics, rounds, markers and records (receipts, merge
  directives, structured review and findings, attribution, evidence and telemetry, and the
  receipt of a PR promoted to `agent:ready-to-merge`).

Ports and adapters are in [Pipeline and architecture](architecture.md#ports-and-adapters); roles
and merge modes in [ADR 0002](adr/0002-ruoli-e-responsabilita.md).

The split moved the content of the former single page without rewriting it. CI and the level-2
reviews verified it on the merge of #262, commit `3c34f1cde8065bfeabee0d8c76294eafd6032a8e`. The
ledger of the split and the test that checked it were removed afterwards (#273): they stay in the
repository history at that commit.
