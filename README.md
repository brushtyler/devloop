# SSDLC

**devloop** develops software with AI agents under the explicit authority of an operator: it dispatches the GitHub issues and PRs of a repository to an implementer agent and independent reviewers, through fix rounds and recorded evidence, and approvals, limit extensions and merges are operator decisions. The agents are built on **Claude Code (`claude`)**, **OpenAI Codex (`codex`)**, **Antigravity (`agy`)** and **OpenRouter**.

The local SSDLC engine, developed in this repository beside devloop until 2026-11-22, is frozen and no longer on `main` ([ADR 0007](docs/adr/0007-da-ssdlc-a-devloop.md)): it stays in the Git history and in the tag `ssdlc-engine-final`.

## Components

| Component | Code | What it does |
| --- | --- | --- |
| **devloop** | `devloop/` | Dispatches GitHub issues and PRs: an implementer agent and an independent reviewer, fix rounds, and a merge done by a human or, when the operator enables a merge mode, by devloop under the merge directives |
| **`core`** | `core/` | Pure validators and calculations devloop uses for the budget of each story |

## Status

What exists today, stated explicitly so that this page does not promise more than there is:

- **devloop is how this repository is developed, and it is configured for it**: the facts and restrictions of this repository are in its project file on the host, of which `ops/host/ssdlc/project.toml` is the example. Its use on other repositories is planned in [ADR 0006](docs/adr/0006-devloop-su-altri-repository.md).
- **Merges are explicit.** devloop leaves the merge to a human unless the operator enables a [merge mode](docs/devloop/guide.md#merge-modes).
- **Cost accounting is partial.** Claude reports CLI estimates and OpenRouter reports metered usage. Subscription CLIs such as Codex do not report a cost; see [#43](https://github.com/brushtyler/ssdlc/issues/43) and [#14](https://github.com/brushtyler/ssdlc/issues/14).
- **devloop runs on a single Linux host.** It is not a distributed scheduler.

Open work is tracked in the [GitHub issues](https://github.com/brushtyler/ssdlc/issues).

## Direction

The plan of [ADR 0007](docs/adr/0007-da-ssdlc-a-devloop.md), in order:

1. **R1**, the engine and its constraints leave `main`, after the tag `ssdlc-engine-final`.
2. **R2**, the repository is renamed `brushtyler/devloop`.
3. **R3**, the host follows the rename.
4. **R4**, documentation and names follow it.

## Documentation

### Operating devloop

| Document | Content |
| --- | --- |
| [Guide](docs/devloop/guide.md) | For the operator and the supervisor: the loop, labels and `/devloop` commands, merge modes, escalations, watching, pinned installation, limits |
| [Reference](docs/devloop/reference.md) | Configuration, local tests, documentation map, agents, review levels and policy, GitHub identities and apps |
| [Protocol](docs/devloop/protocol.md) | Rounds, markers and records: receipts, merge directives, structured review and findings, evidence |
| [Architecture](docs/architecture.md) | Components, ports and adapters, the devloop boundary and the `core` package |

### Decisions

[Architecture decision records](docs/adr/) (in Italian): [ADR 0001](docs/adr/0001-prodotto-unico-fasi-su-tracker.md), one product with phases on the tracker, superseded for the engine's part; [ADR 0002](docs/adr/0002-ruoli-e-responsabilita.md), roles and responsibilities; [ADR 0003](docs/adr/0003-struttura-della-documentazione.md), documentation structure; [ADR 0007](docs/adr/0007-da-ssdlc-a-devloop.md), from ssdlc to devloop.

## Developing SSDLC

Read the [development guide](docs/development.md): the test suite (`scripts/run-tests.py`) and how to run a single module, the documentation tests, the CI, the rule that keeps documents in step with the code, and the pinned controller.

```bash
python3 scripts/run-tests.py                             # the whole suite, what CI runs
python3 -m unittest discover -s tests -p 'test_name.py'  # a single module
```

- Agents contributing to this repository follow [AGENTS.md](AGENTS.md).
- [devloop](docs/devloop.md) dispatches issues labelled `agent:ready` to an implementer agent and an independent reviewer agent, up to a PR that a human merges (devloop merges only when the operator enables a merge mode).

## License

Apache-2.0
