# Security policy

## Reporting a vulnerability

Report a vulnerability of devloop with GitHub's **private vulnerability reporting**: the
"Report a vulnerability" button of the repository's *Security* tab
(<https://github.com/brushtyler/devloop/security/advisories/new>). The report reaches the
maintainer only, as a draft security advisory.

Report it **never in an issue**, a pull request, a comment or a discussion: the repository is
public, and so is everything written there, also after an edit. This holds for an indirect
reference too ("there is a problem in the token script, details privately").

Write in the report what is affected, how to reproduce it and what an attacker gains. Do not
include real secrets (tokens, keys) nor the details of a real installation (paths, users,
configuration).

## What happens next

- The maintainer, the operator of the project, is the only one who creates and publishes the
  advisories. The coordinator of devloop may prepare the text of one on the host, never on
  GitHub ([decision N1 (a) of #593](https://github.com/brushtyler/devloop/issues/593#issuecomment-7147837673)).
- The fix is prepared in the temporary private fork of the advisory, outside devloop, which works
  on public pull requests. When the operator decides so, case by case, the fix goes instead
  through a public pull request that says the minimum, accepting that the fix itself may reveal
  the problem.
- Once the fix is released, the advisory is published.

The procedure of the operator and of the roles is in the [devloop guide](docs/devloop/guide.md#vulnerabilities-private-advisories).

## Supported versions

devloop runs from a pinned commit of `main`: a fix is released on `main`, and each installation
takes it with its next pin. Older commits get no separate fix.
