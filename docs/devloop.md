# devloop: agent dispatcher for this repository

devloop moves issues through implementation, independent review and fixes, so that a human only
decides what starts and what gets merged. It is development tooling for this repository, not part
of the SSDLC orchestrator, and it never touches SSDLC runs or the main checkout's working tree.

## The loop

State lives on GitHub: `agent:*` labels and hidden markers in comments. A tick reads that state,
plans the next steps and runs them. A crashed tick is resumed by the next one.

| Step | Trigger | Result |
|---|---|---|
| Start | A trusted user adds `agent:ready` to an issue | `agent:working`; the implementer works in a fresh worktree |
| Implement | Changes made and local tests pass | Dispatcher commits, pushes `agent/issue-<n>`, opens a PR labelled `agent:review` |
| CI | Checks pending | Wait; failing checks become a review round sent back as `agent:changes` |
| Review | CI passed or absent | The dispatcher computes the PR's [review level](#review-level-and-policy-receipt); at level 1 a **different** agent posts a structured verdict (see below), at level 2 two [independent reviewers](#level-2-two-independent-reviewers) do, one per action, and their verdicts are applied together |
| Fix | `agent:changes` | The implementer fixes or disputes every open finding of the round, then back to `agent:review` |
| Sync | A PR in `agent:review` or `agent:ready-to-merge` conflicts with `main` | The implementer resolves the conflicts of a merge of `origin/main`; the dispatcher pushes the merge commit, back to `agent:review` (see below) |
| Approve | Verdict `approve` from every reviewer the level requires | `agent:ready-to-merge`, only if no [finding](#findings-and-who-closes-them) is still open, the PR head is still the reviewed commit, CI passed and the policy receipt recomputed just before promotion matches the one of every approval |
| Level 0 | A trusted user's `reviews:0`, the whole diff in the operator's allowlist, every expected check `success` | `agent:ready-to-merge` without a review (see [Level 0](#level-0-no-review)); off by default |
| Merge | `agent:ready-to-merge` | **A human merges.** devloop never merges |

Readiness belongs to the approved commit. Every tick re-checks ready PRs:

- if the head is no longer the approved commit, the approval is revoked and the PR goes back to
  `agent:review` for a new review;
- if CI is running again on the approved commit, readiness is suspended (`agent:review`) and the
  recorded approval is applied again, without a new review, once CI passes and if its policy
  receipt still matches;
- if CI fails on the approved commit, the failure becomes a review round (`agent:changes`).

### Conflicts with `main`

With more than one PR open, merging one can put another in conflict with `main`. Before planning
a review or a promotion, the dispatcher reads each PR's merge state from GitHub (`mergeable` and
`mergeStateStatus`, in the same `gh pr list` call as the rest of the snapshot):

- **conflict** (`mergeable` CONFLICTING, or `DIRTY`): the PR is neither reviewed nor kept ready;
  the tick plans a `sync`. A verdict that asks for changes and is already recorded is applied
  first, and a PR in `agent:changes` is fixed first: the sync happens when it is back in review;
- **unknown** (`UNKNOWN`, GitHub is still computing): nothing is planned for the PR until a later
  tick knows the state. A PR already in `agent:ready-to-merge` keeps its label meanwhile;
- any other state (`CLEAN`, `BLOCKED`, `BEHIND`, `UNSTABLE`, …) is not a conflict.

A sync runs in the PR worktree. The dispatcher starts `git merge --no-commit origin/main`; if
files are left in conflict, the PR's implementer gets a prompt that lists them, each with the
commits of `main` that changed it, and resolves them keeping the behaviour of both sides. The
agent edits the files only: the dispatcher then checks that the merge is still the one it started
(head unchanged, same `MERGE_HEAD`) and that no conflict marker (`<<<<<<<`, `>>>>>>>`) is left,
runs the test command (with the usual retry), commits the merge with the implementer's identity
and pushes it. A merge without local conflicts (GitHub's state was older than `main`) is
committed and pushed without an agent; a branch that already contains `origin/main` is left
alone until GitHub recomputes. As for any push, the merge must not differ from `main` under
`protected_paths`.

A sync writes no review record, so it does not consume a review round, and it runs even when no
round is left. The PR goes back to `agent:review`, also from `agent:ready-to-merge`, and the next
round reviews the new head after CI as usual; that round counts against `max_review_rounds`. A
sync that fails (the implementer is blocked, tests still fail, markers are left, the merge was
concluded or aborted by the agent, or the agent run failed) goes to `agent:needs-human` with a
comment that lists the files in conflict and their commits.

Any step can stop at `agent:needs-human` with a comment explaining why: the
[readiness check](#readiness-check-before-implementation) found the issue not ready, the agent declared itself
blocked, tests still fail after the retry, the reviewer asked for a decision, the round limit
was reached, the reviewer pool has not enough independent reviewers for the review level, a
[level 0](#level-0-no-review) head misses an expected check, has an open finding or a standing
request for changes, an approval left open a finding of another owner, the
owner of a finding cannot be reconstructed from the markers, a sync with `main` failed, an agent, `gh` or `git` call failed or timed out, an agent
stalled with `agent_stall_action = "abort"` (see [Stalled agents](#stalled-agents)), the issue text was changed by an
untrusted user after `agent:ready`, or the PR body lost its marker. While `agent:needs-human` is
set devloop ignores the PR or issue, so to resume: fix the cause, **remove `agent:needs-human`**
and set the label that should run next (`agent:ready` on the issue, `agent:review` or
`agent:changes` on the PR). Every escalation comment repeats this.

A round is one evaluation of a head: a CI failure, or the reviews of every reviewer the review
level requires (one at level 1, two at level 2), whatever the number of reviewers. The round
limit (`max_review_rounds`) is checked before every new round, whether a review or a CI failure,
so a PR whose head keeps moving cannot be reviewed indefinitely. Replaying a verdict that is
already recorded is not a new round, and neither is a sync with `main` nor the second review of
a round already open.

### Resuming after a `needs_human` verdict without code changes

A verdict is bound to the reviewed commit. Back in `agent:review` with the same head, devloop
applies the recorded verdict again without a new review, so a `needs_human` verdict sends the
PR straight back to `agent:needs-human`. When the decision the reviewer asked for needs no code
change (for example a linked issue was opened, or the PR description was updated), ask for a
new review of the same commit:

1. A user in `trusted_users` comments on the PR with `/devloop rereview` as the **first line**.
   The lines below it can explain the decision.
2. Remove `agent:needs-human` and set `agent:review`.

The next tick runs a new review. It is a new round: it waits for CI like any review, and it
does not start once `max_review_rounds` is reached. The reviewer's prompt includes every
comment the trusted users wrote after the previous verdict. Only a request written after the
latest verdict counts, so it starts one review: once the new verdict is recorded, that verdict
is replayed again until another request. The request is ignored when it comes from anyone
outside `trusted_users`, when the command is not the first line, and in text devloop publishes:
that text starts with the attribution header, also when an actor without a GitHub App publishes
with the operator's login.

A devloop PR is recognised by its branch, `agent/issue-<n>`, opened by a trusted user. Its body
ends with a hidden marker that records the issue, implementer and reviewer: text may be edited
above it, but the marker must stay the last line. A PR whose marker is missing stays in the flow
(it still counts against `max_open_prs` and its issue does not restart) and goes to
`agent:needs-human`. A PR left without a state label, for example after a crash right after it
was opened, is put back into `agent:review`, and its issue loses `agent:working`.

Open devloop PRs are capped by `max_open_prs`, including PRs waiting for merge or for a human,
so unattended work cannot outrun review capacity. New issues start in order: `priority_labels`
first, then by number. Issues with a `blocked_labels` label are skipped.

### Readiness check before implementation

With `preflight = "<agent>"` in the configuration, an issue is checked before the implementer
starts on it (#114). The distributed `devloop/config.toml` leaves it **disabled**, with a
commented example: enabling it is the operator's decision. Without `preflight`, issues start as
described above.

The checker is a configured agent other than the implementer. It runs in review mode (the
read-only arguments and sandbox of a reviewer, see [Configuration](#configuration)) in a
disposable detached worktree at `origin/main`, `issue-<n>-preflight`, removed afterwards, and
gets the implementer's context: title, body and the comments of `trusted_users`. Its answer
follows [`devloop/schemas/preflight.json`](../devloop/schemas/preflight.json):

- `ready`, a boolean;
- `gaps`, each with a `kind` (`operator_decision`, `default_available` or `tool_unavailable`),
  a `description`, the `code_refs` that show it and a `proposed_default` (`""` if none);
- `split`, how to divide an issue too large for one run, `""` if not needed.

An issue is ready only if the agent says so **and** lists no gap and no split: a
`default_available` gap blocks too, until a trusted comment accepts the proposed default. A
default the repository or the issue already prescribes is not a gap.

The outcome is a comment on the issue, written with the checker's identity and header, whose
marker records the outcome and a hash of the title, the body and the trusted users' comments.
The hash leaves out devloop's own records and comments whose first line is a `/devloop`
command. A record is recognised by its provenance and its metadata together: it starts with the
attribution header, it is written by an identity allowed to write devloop records (an agent's or
the dispatcher's app, or a trusted user for an actor without an app), and it ends with a valid
devloop marker. Escalations, agents that stop (`Lavoro bloccato`) and the checks themselves
carry one. Any other trusted comment counts, even one that starts with the header: without
GitHub Apps devloop and the operator share a login, and the header alone would hide a
requirement the check never saw. The outcome is reused while the hash stays the same:

- **ready**: the issue starts on a later tick, in the usual order and within `max_open_prs`.
  The implementer starts only if the hash read just before still matches;
- **not ready**: the issue goes to `agent:needs-human` (it keeps `agent:ready`), and the comment
  lists the gaps with the proposed defaults and the split. If `agent:needs-human` is removed
  without changing the issue, the next tick blocks it again with the same outcome.

An issue blocked by the check is examined again on every tick, even in `agent:needs-human`. A
new check runs when the hash changed (a trusted user decided a point, accepted a default, or
edited the issue) or when a trusted user wrote a comment whose **first line** is
`/devloop recheck`. Each request starts one check: it counts only if written after devloop's
latest record on the issue. The command is ignored, and logged, when anyone outside
`trusted_users` writes it. If the new check passes, the dispatcher removes `agent:needs-human`
and the issue goes back in the queue.

The dispatcher removes only a block that belongs to the check. The check owns
`agent:needs-human` only if it added the label when the label was absent: its record keeps the
number of earlier `labeled` events of the label, and the dispatcher must have written the only
one since. The check loses the block, which then stays until a person removes it, if the label
was removed and applied again, if a comment starting with the attribution header, written by an
identity allowed to write devloop records, came after the check (another escalation, or
anything that may be one, marker or not; the dispatcher's [ledger records](#evidence-and-telemetry),
such as the turn of a recheck, do not count), or if the attribution is uncertain. A comment from
anyone else, header or not, never touches the attribution. A label already present when the
check failed is never the check's. Rechecks run only for blocks the check owns.

Each check is an agent action: it takes a slot of `max_actions_per_tick`, and a check of an
issue that may start also takes one of the free PR slots of the tick. It runs under
`agent_timeout_seconds`. A check that fails or times out sends the issue to
`agent:needs-human` with a comment, removing `agent:ready` like any failed step, and the
implementer does not start.

## Review level and policy receipt

The number of reviews a PR needs is its **review level**. The dispatcher computes it from its own
review policy, never from the PR (#106, first step of #66). The effective level of a head is the
highest of:

- `default_level` of the configuration (1);
- 2, if a user in `trusted_users` added the `reviews:2` label (`label`) **to the PR**. Every
  `labeled` event in the PR's history counts, so the level stays raised if the label is removed
  later, even before the next tick, or added again by someone untrusted. The label added by
  anyone else, or to the issue, is ignored;
- 2, if the PR's full diff against the base (from their merge base, as GitHub shows it) touches
  a sensitive path. Deletions count, both the old and the new path of a rename count, and a
  symlink counts also for its target (a target outside the repository always counts);
- the highest level already recorded on the PR by an earlier receipt. A fix that widens the
  diff to the core raises the level, and a later head that narrows it again does not lower it.

Nothing lowers a level, and nothing takes it below 1, except the operator's `reviews:0` label
(see [Level 0](#level-0-no-review)). The level is computed again on every new head, before each
review, CI round and promotion.

Sensitive paths are configurable under `[review_policy]`: `sensitive_paths` (default `devloop/`,
`AGENTS.md`, `CLAUDE.md`, `.github/`, `schemas/`; an entry is a directory or a file at the root)
plus the files listed in the `files` object of each JSON file in `sensitive_path_lists` (default
`schemas/recovery-grader-baseline.json`). The policy comes from the running dispatcher and its
configuration: the lists are read at startup from the root of the running installation (the
pinned checkout, see [Run from a pinned installation](#run-from-a-pinned-installation)), never
from a PR branch, so a PR that changes `devloop/config.toml`, the prompts or the lists cannot
apply weaker rules to itself. A list that cannot be read stops devloop at startup.

**Level 1** works as before: one reviewer, the PR's recorded reviewer or else the first of the
pool, never the PR's implementer. **Level 2** needs two independent reviewers (#111, below).
When the pool has not enough of them, the PR is neither reviewed nor promoted: it goes to
`agent:needs-human` with a dispatcher comment that gives the level, its reason (the label and
who added it, or the sensitive paths) and why the pool falls short. Since the level does not go
down, putting the PR back into `agent:review` escalates it again until the configuration
changes: until then a person reviews the PR and decides on the merge.

### Level 2: two independent reviewers

The reviewers come from the pool `roles.reviewers` in `config.toml`. Two reviewers count as
distinct only if they differ both in GitHub identity (their app; agents without an app share the
operator's login) and in the tuple `(kind, model, effort, review_args)`; keys and descriptions
do not count. Neither may share the identity or the `(kind, model, effort)` of whoever wrote
code on the PR: the implementer recorded in the PR marker, the author of every fix, also of
earlier rounds, the agent that resolved the conflicts of every sync, and the agent configured
now under the implementer's key, which runs fixes and syncs. The dispatcher scans the pool in configuration order and takes the first such pair. The
choice is deterministic, so it stays the same for the whole round, and it is recorded in the
receipt (`reviewers`).

Since #111 the PR marker, every fix, every sync whose conflicts an agent resolved (`sync`
marker) and every review record the identity and tuple actually used (`profile`). At level 2,
when a PR or one of its fixes was recorded before, or a sync marker lacks the profile,
independence cannot be verified and the PR goes to `agent:needs-human`. At level 1 older markers
stay valid as before.

A round at level 2 works as follows:

- **One review per action.** Each review call is an action of its own and counts against
  `max_actions_per_tick`. The first reviewer opens the round; the PR stays in `agent:review`,
  and a later action runs the second reviewer, which joins the same round as long as the head
  and the recomputed receipt are unchanged. Each reviewer judges the head on its own: its prompt
  has the earlier rounds, not the other reviewer's verdict.
- **Aggregation.** The verdicts are applied once both are in: `needs_human` from either sends
  the PR to `agent:needs-human`; otherwise a `request_changes` from either starts a single fix
  that receives every open finding of both; two approvals go to promotion.
- **Quorum.** The PR is promoted only if every reviewer of the receipt recomputed just before
  approved the same head with that same receipt, and no blocking finding is open. A missing
  reviewer, one that timed out (the step goes to `agent:needs-human`) or one that asked for a
  human is not an approval, and the quorum never goes down. A second review of the same reviewer
  in the same round is ignored, so two approvals of one reviewer are not enough.
- **Another owner's findings.** An approval stays an approval even if another reviewer's
  finding is still open: only its owner, CI or the operator closes it (see
  [Findings](#findings-and-who-closes-them)). The dispatcher checks every finding of the round
  before promoting or starting the fix.
- **Restarts and races.** A review already published for the round is never run again: after a
  crash or a failure following the first review, the next action runs only the missing one, and
  a round with both reviews published is applied without any inference. If the head moves while
  a reviewer works, its verdict is discarded without being published, and the new head is
  reviewed in a new round by both reviewers. A round whose head moved, whose receipt changed,
  that a fix answered or that a `/devloop rereview` request voided is closed: the next review
  opens a new round.

### Level 0: no review

Level 0 (#107, second step of #66) promotes a PR to `agent:ready-to-merge` without any review.
Only a trusted user lowers a level: no heuristic, plan or agent can. It is **off by default**,
and stays off unless `[review_policy.zero]` in `config.toml` has both a non-empty `allowlist`
and a non-empty `checks`. While it is off, everything works as described above, and the policy
version does not change.

A head gets level 0 only when all of these hold:

- **The label.** The latest `labeled` or `unlabeled` event of `reviews:0` (`zero.label`) on the
  PR added it, and a user in `trusted_users` wrote it. A label added by anyone else is ignored;
  removing the label, or someone else adding it again, takes level 0 away.
- **Only level 1.** The label lowers level 1 only. The `reviews:2` label from a trusted user
  (any event in the PR's history), a level 2 already recorded on the PR, a sensitive path and
  `default_level = 2` always prevail.
- **The allowlist.** Every path of the PR's full diff against the base (as for the level: both
  sides of a rename, deletions) is in `allowlist`: under a listed directory (`docs/`) or a
  listed file. A Markdown file (`.md`, `.markdown`, `.mdx`) counts only when listed by its exact
  path, so `docs/` does not cover `docs/guide.md`. A symlink never counts. These are excluded
  even when listed: `AGENTS.md` and `CLAUDE.md` at any depth (for example `docs/AGENTS.md`, any
  case), `devloop/prompts/`, `standards/`, `.github/`, `schemas/` (with the replay gate
  baseline) and every sensitive path. Paths are compared exactly as Git names them, spaces
  included: only the configured entries are trimmed. One path outside the allowlist keeps the
  level computed above.
- **Not refused before.** The level is computed again on every head. When a receipt recorded on
  the PR refused the label (a fix widened the diff out of the allowlist, or a level 2 applied),
  that label event no longer counts, even if a later head is back inside the allowlist: the PR
  stays at least at level 1 until a trusted user adds the label again (remove it, add it back).

At level 0 the dispatcher runs no reviewer. Before promoting it re-reads the head and checks:

- **Expected CI.** Each check in `checks` is a check run identified by its exact `name` and the
  `app_id` of the GitHub App that produces it; commit statuses do not count. For each, the most
  recent run on the current head decides, and only `success` is a success. While one is still
  running the PR waits; a check that is missing (also a filtered workflow), `skipped`,
  `neutral`, `cancelled` or failed sends the PR to `agent:needs-human`. The test command is
  `test_cmd` of the running dispatcher and the workflows are under `.github/`, which a PR cannot
  change at level 0; still, do not list in the allowlist files that the CI runs as its own
  harness (for example `scripts/run-tests.py`).
- **No open finding.** A blocking finding still open from an earlier round sends the PR to
  `agent:needs-human`; the operator can close it with a `/devloop finding` command.
- **No standing request for changes.** A native `REQUEST_CHANGES` review from a user in
  `trusted_users` blocks, also after new commits, until it is dismissed or the same user
  approves; a comment does not withdraw it. It sends the PR to `agent:needs-human`.

The promotion is a dispatcher comment with a `policy` marker that records the receipt (level 0,
the label, who added it and when, the allowlist outcome) and the state of each expected check.
**devloop never merges**: the merge stays the operator's. While the PR is ready, every tick
checks that the head is still the promoted one, that `reviews:0` is still on the PR and
`reviews:2` is not, and that CI is not running again or failing. It also recomputes the receipt
from the running policy (the label history, every `reviews:2` event, the current allowlist),
reads the expected check runs again with the strict rules above and the native reviews: if the
level is no longer 0, if the most recent run of an expected check is not `success`, or if a
trusted user's `REQUEST_CHANGES` now stands, or if any of this cannot be read, readiness is
revoked (or CI becomes a review round) and the level is computed again. A CI failure at level 0 is a
round like any other, and its fix goes back to level 0 only if the diff stays in the allowlist.

```toml
[review_policy.zero]
label = "reviews:0"
allowlist = ["docs/", "docs/guide.md", "tests/fixtures/"]
checks = [{ name = "unittest", app_id = 15368 }]  # 15368: GitHub Actions
```

### Receipts

Every agent review, CI round and promotion records a **receipt** in its marker:

| Field | Content |
|---|---|
| `pr`, `base`, `head` | The PR, the base branch commit and the head the level was computed for |
| `policy` | The policy version: the SHA-256 of the loaded policy configuration, including key, app, kind, model, effort and `review_args` of every reviewer of the pool |
| `level`, `reasons` | The effective level and why: default, label with its author, sensitive paths, earlier level, and with level 0 enabled the `reviews:0` label with its author, time, whether it applied and the paths outside the allowlist |
| `reviewers` | The reviewers the level requires, in order (none at level 0, or when the pool has not enough independent ones) |
| `profiles` | The identity and tuple `(kind, model, effort, review_args)` each required reviewer reviews with now. At level 1 the PR's recorded reviewer stays required even when it is no longer in the pool, so the policy version does not cover it: its profile does |

Promotions and escalations for a pool without enough independent reviewers are dispatcher
comments with a `policy` marker, read only from the dispatcher's identity. Before promoting a
round the dispatcher recomputes the receipt. The approvals count only if each required reviewer
approved and the commit of base, the policy version, the level, the required reviewers and their
profiles (and the head) all match: when the base branch advanced, the policy or the
configuration of the pool or of a required reviewer (apps, models, efforts, `review_args`)
changed, or the level rose, the PR stays in
`agent:review`, a dispatcher comment says what changed, and the next tick runs a new review,
even of the same head. That review is a
round like any other and waits for CI. Approvals recorded before receipts existed have none:
they are not promoted and need a new review; their rounds and open findings stay as they are. A
PR already in `agent:ready-to-merge` keeps its label until something sends it back to review.

## Structured review

The reviewer returns JSON validated against [`devloop/schemas/review.json`](../devloop/schemas/review.json):
`verdict` (`approve`, `request_changes`, `needs_human`), blocking and non-blocking findings with
file, line and a suggested fix, and the status of every earlier blocking finding (`resolved`,
`unresolved`, `withdrawn`). devloop then enforces consistency:

- `approve` with a blocking finding, or with one of the reviewer's own earlier findings
  unresolved, becomes `request_changes` (another owner's open finding does not change it: the
  dispatcher checks it before promoting);
- `request_changes` with nothing to fix becomes `needs_human`;
- an earlier finding the reviewer does not assess stays open.

Findings get stable ids (`R<round>-<n>`). The fix step answers each one (`fixed`, `disputed`,
`not_applicable`), and the next round sees those answers. The comment shows the review in
readable form and carries the full record in a hidden base64 marker for the next tick.

### Findings and who closes them

Every blocking finding has an owner, the reviewer that opened it, and its identity is the triple
`round/reviewer/ID`, for example `2/codex/R2-1`. The key is recorded in the review marker; a
review whose keys name another round or another owner is not accepted, and the PR goes to
`agent:needs-human` (#110). A CI round is
a review of the reviewer `ci`. A finding stays open until one of these closes it:

| Who | How |
|---|---|
| The reviewer that opened it | `resolved` or `withdrawn` in one of its later reviews |
| The dispatcher, for a `ci` finding | Automatically, when CI passes on the PR's current head (the same commit that failed after a re-run, or a later one), in `agent:review` or still in `agent:changes` before the implementer starts |
| The operator | The `/devloop finding` command below |

Another reviewer's assessment does not close a finding: it is recorded in its review, with a
note, and the finding stays open. When every reviewer of a round approves but a finding of an
owner outside the round (CI, or an earlier reviewer) is still open, the round is not promoted:
the PR goes to `agent:needs-human` with a comment that lists the open findings. When a reviewer
of the round asks for changes, the fix receives every open finding, whoever owns it.
Once the operator has closed them, putting the PR back into `agent:review` applies the recorded
approval again, without a new review. The round count does not change.

The closures made by CI and by the operator are dispatcher comments with a `finding` marker,
read only from the dispatcher's identity: each entry records the key, `resolved` or
`withdrawn`, who closed it (`ci` or `operator`, with the author), the reason and the head. A
closed finding stays closed. When they close every finding of a request for changes, the PR
goes back to `agent:review` and the head gets a new review. The tick that records closures
does nothing else on that PR; the next tick continues with them.

#### Closing a finding as the operator

A user in `trusted_users` comments on the PR with a line:

```text
/devloop finding 2/codex/R2-1 resolved|withdrawn <reason>
```

The line can be anywhere in the comment, and a comment can hold more than one. The dispatcher
recognises it, as it recognises `/devloop rereview`, and records the closure with the author
and the reason; from then on the finding is closed. The reason is required. A command from
anyone outside `trusted_users`, malformed, or naming a finding that does not exist is ignored
and written to the dispatcher log; one naming a finding already closed has no effect. Text
devloop publishes is never a command. devloop reads the comments of a PR only while it is in
`agent:review`, `agent:changes` or `agent:ready-to-merge`: a command written while the PR is in
`agent:needs-human` takes effect once the label is back.

#### Markers written before #110

Review markers written before finding keys existed are read with the rules of their time: the
rounds and the findings still open are kept, and each finding is attributed to the reviewer
recorded in the marker of the round that opened it (`ci` for CI rounds). From the first keyed
review on, only the owner, CI or the operator closes them. These rules cover only the markers
that precede the first keyed one: a later review marker without keys, or with keys on only some
findings, closes nothing, and its own findings stay open, attributed to its reviewer. When the owner cannot be
reconstructed (a review marker without a reviewer, or one that leaves open a finding whose
round is unknown), the PR goes to `agent:needs-human` with a comment that lists the problem:
the review and the decision on the merge are up to a person.

Markers are control records, while the rest of each comment is text written by a model, and both
are published by the same account. So the dispatcher neutralizes HTML comments in all published
text, and reads a marker only from the last line of a comment, of the expected kind and with a
valid structure. Text from an implementer or a reviewer cannot record a review, a fix or a PR
link.

## Attribution

Every text devloop publishes starts with the header that [AGENTS.md](../AGENTS.md) requires. The
values come from the configured agent: CLI version from its `--version`, model and reasoning level
as passed on the command line. A known version is cached for the life of the process; a failed
`--version` is retried at the next publication, and the header says `CLI non noto` only for texts
published while it keeps failing. When a turn ran on a [fallback model](#fallback-models), the
header names that model and its reasoning level, not the configured ones. Text the dispatcher
writes on its own, such as escalations and CI rounds, names `devloop dispatcher` as the agent and
no model.

## GitHub identities

Each agent and the dispatcher can act on GitHub with its own GitHub App (#65), configured under
`[apps]` in `config.toml`:

| Actor | Does |
|---|---|
| dispatcher | every read, labels, escalations, CI rounds, finding closures, readiness |
| implementer | commits (author and committer are its bot), push, opens the PR, fix and blocked comments |
| reviewer | the review comment and a native GitHub review (`APPROVE`, `REQUEST_CHANGES` or `COMMENT`) on the reviewed commit |

Apps are per agent, not per role: GitHub keeps one review state per identity, and it already
refuses an approval from the author of a PR. Least privilege per role comes from the tokens:
devloop mints an installation token for each action, limited to this repository and to the
permissions that action needs. A reviewer's token can read contents and write pull requests, but
cannot push; labels are always set by the dispatcher.

Records are bound to identities. A PR counts as a devloop PR with its marker only if it was opened
by the app of the implementer the marker names; a review counts only if its comment was written by
the app of the reviewer named in it, and never by the PR's implementer; a CI round and a finding
closure only from the dispatcher's app; a fix only from the implementer's app. `trusted_users` stays for people: it
decides who can start work with `agent:ready`, and it cannot contain bots.

The native review mirrors the verdict for GitHub and branch protection; devloop itself reads only
the review comment, and a failed native review is logged without stopping the flow.

Private keys are read from `key_dir` (or `$DEVLOOP_APP_KEY_DIR`) as
`<slug>.<date>.private-key.pem`, the newest one per app, and must have mode `600`. The JWT is
signed with the `openssl` binary. Tokens stay in the dispatcher: they reach `gh` through
`GH_TOKEN` and `git push` through an HTTP header in the environment of that single command, never
through a file or the command line. No other credential can join the push: it refuses a URL that
an `insteadOf` or `pushInsteadOf` rule would rewrite, allows only the HTTPS transport, and
disables credential helpers, askpass programs and preconfigured authentication headers. The
header is keyed on the exact push URL, so that no `http.<url>.extraHeader` in a config file is
more specific, and `git -c` settings inherited through `GIT_CONFIG_PARAMETERS` are dropped. An actor
without an app uses the ambient `gh` login and its records must come from `trusted_users`, as
before. An app that is configured but whose key is missing stops devloop at startup instead of
silently falling back to the operator's login: remove it from `[apps]` (or use `--config`) to run
that actor without it. `python3 -m devloop apps` checks each app, its installation and its
permissions.

The apps have no `Workflows` permission, and devloop never pushes changes under
`protected_paths` (default `.github/`): a workflow or a local action pushed to a branch of this
repository would run in CI before any review. Such a change goes to `agent:needs-human` and is
applied by a person.

Agent CLIs and the test command run without GitHub credentials in reach: `GH_TOKEN` and
`GITHUB_TOKEN` are removed, `gh` gets an empty config directory, and git may only use local
repositories (`GIT_ALLOW_PROTOCOL=file`), so neither SSH keys nor HTTPS credentials apply whatever
the config files say; credential helpers, askpass programs, preconfigured authentication headers
and SSH are disabled as further layers. `DEVLOOP_APP_KEY_DIR` points to a directory without keys,
so the GitHub App helpers in the worktree (`devloop.apps`, `scripts/agent-token.py`) cannot mint a
token: the dispatcher does every GitHub write. For Claude Code, devloop sets `BASH_MAX_TIMEOUT_MS`
to `test_timeout_seconds`, so a command that asks for it can run the full suite, and
`BASH_DEFAULT_TIMEOUT_MS` to `agent_command_timeout_seconds` (default ten minutes, at most
`test_timeout_seconds`), so a command that sets no timeout, such as a wait loop that never ends,
stops on its own instead of holding the turn for an hour. Values the operator set in the
environment are kept. This prevents accidental use of the operator's credentials, not
deliberate access: code running as the same system user can still read files. Real isolation
needs a dedicated system user or a container.

## Usage

```bash
python3 -m devloop setup            # create the agent:* labels and the review level labels (reviews:0 only when level 0 is on)
python3 -m devloop status           # running action, devloop PRs (with merge state and conflicts), ready issues, planned actions
python3 -m devloop apps             # check the GitHub App of each actor
python3 -m devloop tick --dry-run   # print the plan, change nothing
python3 -m devloop tick             # run one round
python3 -m devloop run --interval 600
python3 -m devloop cleanup          # remove worktrees left by failed steps
python3 -m devloop follow issue-52  # follow live the implementer of issue #52 (pr-<N>: reviewer or fixer)
```

Run it from any checkout of this repository. Worktrees, agent prompts and logs, and the lock that
prevents two dispatchers from running at once live under `state_dir` (default `.ssdlc/devloop`)
in the main checkout.

### Watching a tick

While a tick runs an action it records it in `state_dir/activity.json`: the action, the issue or
PR, the agent and the path of its prompt once an agent turn starts, the model when the turn runs
on a [fallback model](#fallback-models), the start time and the dispatcher's process id. The
record is removed when the action ends, also when it fails. `devloop status` shows it first, with
how long it has been running and, for a fallback, `on fallback model <model>`. If no dispatcher holds the
lock, or the recorded process is gone, the action is reported as `interrupted`: the tick was
killed before it could clean up, and the next tick overwrites the record when it starts an action.

devloop writes an agent's log only when its turn ends. To follow the agent while it works,
`devloop follow issue-<N>` (implementer) or `devloop follow pr-<N>` (reviewer or fixer) prints
the messages and tool calls of the newest Claude (`~/.claude/projects`) or Codex
(`~/.codex/sessions`) session transcript whose working directory is that worktree, then keeps
following it, and switches to a newer transcript of the same worktree when one appears, for
example the next attempt of a turn. The reviewer works in a disposable copy of the PR worktree,
`pr-<N>-review`, so `devloop follow pr-<N>` looks at both and follows the newest session: the
reviewer's while a review runs, the fixer's while a fix runs. It reads local files only and needs no GitHub access. Ctrl-C
stops it. Antigravity sessions are not supported. For this reason devloop runs `codex exec`
without `--ephemeral` (and with `--json`, for the [telemetry](#evidence-and-telemetry)): Codex saves the session of every turn in `~/.codex/sessions`, and these
transcripts stay there after the turn.

### Evidence and telemetry

The dispatcher records the evidence and the telemetry of every step for itself (#133), so that
they no longer have to be pieced together from markers and logs. Each record is a dispatcher
comment ending with a `ledger` marker, signed with the dispatcher's identity like its other
records (only that identity's records count), on the issue for the implementation and the
readiness check, on the PR for review, fix, sync and CI. They publish no receipt yet: that is
#135.

| Record | When | Content |
|---|---|---|
| `request` | The issue goes from `agent:ready` to `agent:working` | SHA-256 of title and body, author and time of the trusted `labeled` event of `agent:ready`, ids of the trusted comments given to the implementer, time of the takeover. It is a snapshot **at the takeover**, not at the time of the label |
| `turn` | Every invocation of an agent CLI, retries, fallback models and failures included: implement, fix, sync with an agent, review, readiness check | Kind, agent, GitHub identity (none: the operator's `gh` login), configured model and effort (for a fallback, the fallback model and the model it replaces), real model, start, end, duration, outcome (`success`, `failure`, `unavailable`), the error if any, telemetry |
| `verification` | Every test command the dispatcher runs after a writing turn | Command, outcome, duration, the tree hash before and after the test, and the commit |
| `event` | The CI of a head (`ci_round`): a failed CI round, the checks a review round saw whatever its verdict (`approve`, `request_changes` or `needs_human`), or the passing checks read again just before the PR is promoted to `agent:ready-to-merge` (also at level 0); a sync without an agent (`sync`) | For the CI, the head, the state (`fail`, `pass`, or `none` without checks), the review round (none at level 0) and each check with its outcome and the id of its GitHub Actions run (none for a commit status or a check outside Actions); the same checks read again for the same head and round are not recorded twice, a new run is. For a sync, the merged base commit and the old and new heads |

**Telemetry** is what the CLI attests, not authenticated:

| CLI | Source | Fields |
|---|---|---|
| claude | `--output-format json` | `total_cost_usd`, `duration_ms`, `num_turns`, `usage` (input, output, cache read and cache creation tokens); the real model is the list of the keys of `modelUsage`, with no main model chosen |
| codex | `codex exec --json`, which devloop now passes | input, output and cached input tokens, summed over the `turn.completed` events (input already includes the cached input). No cost, duration or model |
| agy | none | every field null |

A value that is not attested is `null`, never 0, and the comment shows `non riportato`; the real
model stays `null` next to the configured one when the CLI attests none.

**Turn persistence.** A turn's record is written before the CLI starts, as `open`; if that write
fails the turn does not start and the step escalates like any `gh` failure. When the CLI ends the
record is edited with end, duration, outcome and telemetry. A turn left without that closing
write, because the dispatcher crashed or the edit failed, stays without end, outcome and
telemetry: the next tick logs `incomplete turn on #<n>` and marks the record `incomplete` (only
logs it with `--dry-run`). Where to look does not depend on labels, which change once a step
ends: before writing a turn's record the dispatcher notes the issue or PR in
`<state_dir>/ledger-open-turns.json`, and removes it when the turn is closed. If that note cannot
be written, the turn does not start either. The next tick checks every issue or PR still noted,
for example an issue whose PR is already open or a PR escalated to `agent:needs-human`, and then
forgets it. In case the note is lost, it also looks at open devloop PRs not in
`agent:needs-human`, issues in `agent:working` and issues waiting for a readiness check.

**Tests and commits.** A passing test is bound to the commit created right after it only if the
tree is the same at three points: before the test, after the test and in the commit. The tree is
what the commit would contain (`git add -A`), computed on a copy of the index. If the test
changed files headed for the commit, or anything else differs, the commit stays `null`; the
attempts that failed, or that no commit followed, have commit `null` too.

**Earlier work.** PRs and issues started before these records have none of them. Missing data
is declared missing (`request` null, no turns), and is never reconstructed from the summaries
written by agents.

### Stalled agents

An agent can wait without progress until a Bash command's maximum timeout or until
`agent_timeout_seconds`, for example on a command that never ends. While a turn runs, the
dispatcher checks two signals every `agent_stall_seconds / 10` seconds (between 1 and 30):

- the newest session transcript of the turn's worktree, the files `devloop follow` reads: a new
  event counts as activity;
- the processes the agent CLI started (its descendants and the other members of its session, the
  CLI itself excluded): CPU use of at least 1% of the time since the previous check counts as
  activity. A test suite running in the foreground is active; `sleep`, or a loop that waits for
  a line in a file, is not.

When neither shows activity for `agent_stall_seconds` (default 900), the tick log gets a
`stalled` line, and the activity record a `stalled` entry with the time of the last activity and,
if the transcript shows one, the tool call still in progress. `devloop status` then reports
`stalled: <action> #<n> with <agent>, no agent activity for <time> (since <time>, command: <call>)`.
If the agent becomes active again, the log says so and the mark is removed.

`agent_stall_action` decides what follows:

| Value | Effect |
|---|---|
| `warn` (default) | Report only. The turn goes on; stopping it is the operator's decision |
| `abort` | Also stop the turn (its whole process group) and escalate it like any agent failure: the issue or PR goes to `agent:needs-human` with a comment giving the reason |

The CPU signal reads `/proc`, so the check works on Linux only; elsewhere it is off and the tick
log says so once per turn. Antigravity writes no transcript that devloop can read, so for it only
the CPU signal counts: a long silence of the model alone is reported as a stall.

### Run from a pinned installation

devloop works on this repository, including on its own code, so run a pinned copy rather than
the checkout it is changing. This is the same rule as the pinned controller SSDLC uses to develop
itself:

```bash
cd /path/to/ssdlc                                        # the repository devloop works on
git worktree add --detach .ssdlc/devloop/pinned <commit> # once per devloop version
PYTHONPATH=.ssdlc/devloop/pinned python3 -P -m devloop tick
```

`-P` is required: without it `python3 -m` puts the current directory first on the module path
and imports the checkout's `devloop/` instead of the pinned one. The configuration, schemas and prompts come from the pinned copy; GitHub state and worktrees come
from the current repository. Moving the pin to a newer commit is an explicit operator action.
`devloop cleanup` removes only the agent worktrees under `state_dir/worktrees`, never the pin.

devloop imports only the Python standard library and itself. A test enforces this, so the tool
can later move to its own repository without changes.

## Configuration

[`devloop/config.toml`](../devloop/config.toml) sets the repository, the test command, limits,
the `implementer` and the `reviewers` pool (none of them the implementer, none listed twice;
`reviewer = "<agent>"`, the form before #111, is a pool of one, and setting both is an error)
and, for each agent, the
model, reasoning effort and extra CLI arguments for writing and reviewing turns. Pass another file
with `--config`. `preflight` (absent by default) names the agent of the
[readiness check](#readiness-check-before-implementation); it must be a configured agent other
than the implementer. Among the limits, `agent_stall_seconds` (an integer of at least 1, default 900)
and `agent_stall_action` (`"warn"`, the default, or `"abort"`) control the
[stall check](#stalled-agents); other values stop devloop at startup. `[review_policy]` sets the
[review level](#review-level-and-policy-receipt): `default_level` (1 or 2, default 1), the level
`label` (default `reviews:2`), `sensitive_paths` and `sensitive_path_lists`; changing any of them,
or the reviewers of the pool (their order, app, kind, model, effort, `review_args` or
`fallback_models`), changes the policy version, so pending approvals need a new review.

Each PR records its implementer and the first reviewer of the pool when it is opened, so
changing the roles does not affect the level-1 reviewer of open PRs. If the recorded reviewer is
no longer configured, the first of the pool takes over, but never the agent that implemented the
PR: without an independent reviewer, or if the PR's implementer is no longer configured, the PR
goes to `agent:needs-human`. The level-2 pair always comes from the current pool.

Agents run headless: `claude -p`, `codex exec`, `agy -p`, with the schema in
[`devloop/schemas/`](../devloop/schemas/). Implementers edit only their worktree; the
dispatcher process does every commit, push and GitHub write, with the identity of the actor
it acts for.

A reviewer does not run in the PR worktree but in a disposable copy of it: a detached worktree
at the same commit, `pr-<n>-review`, which shares the repository's refs (so
`git diff origin/main...HEAD` works) and is removed after the review. The tests the reviewer runs
can therefore write files and temporary directories. After the review the dispatcher checks
that the PR worktree is still at the reviewed commit with no change and no new file, not even
an ignored one; otherwise the verdict is discarded and the PR goes to `agent:needs-human`.

| Reviewer | Sandbox |
|---|---|
| codex | `workspace-write` sandbox: writes only in the copy and in the temporary directory, no extra `writable_roots`, network disabled |
| claude | no edit tools; Bash limited to `git diff`, `git log`, `git show` and `python3 -m unittest tests.*` (`review_args`) |
| agy | `plan` mode; commands limited to `review_commands` (see [agy permissions](#agy-permissions)) |

Only codex has an OS-level sandbox. claude and agy are limited by their tools and modes, not by
the system: the tests they run can still reach the network and write outside the copy, as any
code running as the same user. For them, the copy and the final check catch writes to the
reviewed tree, not access to the rest of the system. With the network disabled, codex cannot run tests
that open TCP sockets, even on loopback.

### agy permissions

In headless mode (`agy -p`) agy cannot ask for a permission: a denied tool ends the session with
exit 0, no output and `jetski: no output produced …` on stderr (#122). devloop therefore gives
each agy turn its minimal permissions, and treats a stop without output as a failure.

- **Per turn, not in the profile.** Every turn gets `--settings '{"permissions": {"allow": [...]}}'`,
  which agy applies to that session only. The operator's `~/.gemini/antigravity-cli/settings.json`,
  the authentication and `HOME` are not touched.
- **Commands.** The rules are `command(<prefix>)`, which agy matches by token prefix:
  `command(python3 -m unittest)` allows `python3 -m unittest …` and not `python3 -c …`. The
  prefixes come from `review_commands` (review and readiness check turns) and `write_commands`
  (implement, fix and sync turns) of the agy agent. Both default to the tokens of `test_cmd`,
  `python3 -m unittest`, `git status`, `git diff`, `git log` and `git show`; write turns get no
  `git add` nor `git commit`, because the dispatcher commits.
- **Never.** devloop refuses at startup a prefix that is a bare interpreter, shell or `git`
  (`python3`, `bash`, `sh`, …), also when given by path (`/usr/bin/python3`, `/bin/sh`,
  `venv/bin/python`), one with wildcards, quotes or shell syntax (`*`, `;`, `&&`, `|`,
  `$`, …), and `--dangerously-skip-permissions` or `--settings` in `write_args` or `review_args`.
  A `test_cmd` that cannot be a prefix, such as `make test && lint`, needs explicit lists.
  The two lists are accepted only for agy agents.
- **Bound to approvals.** The commands are part of the agent's profile that markers and
  receipts record (`commands`, agy only), and the `review_commands` of the pool's reviewers
  enter the policy version: changing them voids the approvals given with the old ones, also for
  a recorded reviewer no longer in the pool.
- **Files.** Reads and edits stay inside the turn's working directory, which agy enforces
  without rules; `read_file(...)` rules are not used, since they did not restrict reads in the
  tests on agy 1.5.9.
- **No output is an error.** An agy turn that exits with no output, or with
  `jetski: no output produced`, fails with `<agent> stopped without output: <message>`, which
  goes to the turn's log and to the `agent:needs-human` comment. It is never an empty result;
  with exit 0 it never counts as an unavailable model either. The message counts as the CLI's
  diagnostic on stderr, or on stdout only as a line of its own: a structured answer that quotes
  it is still a valid answer.

```toml
[agents.agy]
review_commands = ["python3 scripts/run-tests.py", "python3 -m unittest", "git diff", "git log"]
```

### Fallback models

An agent may list `fallback_models`, in order, for when its model is unavailable (#121). agy runs
on `gemini-4-argon-high`, which has a budget of its own, and falls back to
`gemini-4-hydrogen-high`:

```toml
[agents.agy]
model = "gemini-4-argon-high"
model_label = "Gemini 4 Argon"
effort = "high"
fallback_models = [
    { model = "gemini-4-hydrogen-high", model_label = "Gemini 4 Hydrogen" },
]
```

- **Schema.** Each entry has `model`, `model_label` and optionally `effort`, which otherwise is
  the agent's. A fallback reasons at `high`, `xhigh` or `max`: any other level, or for agy a
  model whose name ends with `-minimal`, `-low` or `-medium`, stops devloop at startup, as does a
  model listed twice or equal to the agent's own. The list is optional: an agent without it
  behaves as before.
- **When.** Only when the turn failed because the model is unavailable: the CLI exited with a
  non-zero code, or its JSON envelope is explicitly an error (claude `is_error`, agy `status`
  other than `SUCCESS`), and its stdout or stderr matches one of `unavailable_patterns`
  (regular expressions, case-insensitive; default `RESOURCE_EXHAUSTED`, `quota`, `429`,
  `model .* not available`), or the exit code is in `unavailable_exit_codes` (default none). A
  structured result, valid or not against the schema, and output that cannot be parsed (stdout
  that is not JSON or not a JSON object, a codex final message that is not JSON) take
  precedence even with a non-zero exit: only a missing or empty output, an envelope without
  `structured_output` or an explicit error envelope leave room for a match. A result, a parse
  error, a timeout and a stall never count, whatever the text, and follow the usual path to
  `agent:needs-human`.
- **Once.** Each model is tried at most once per action: a rate limit the CLI reports as
  terminal counts as persistent, and a model found unavailable is not tried again by the later
  attempts of the same action (`implement_test_retries`). When the last model fails too, the
  issue or PR goes to `agent:needs-human` with a comment listing the models tried and why. Any
  other failure after a fallback ends the action without trying further models, and its
  comment lists the models tried too, the failed one included.
- **Worktree.** The fallback is a new session with the same prompt and worktree. Partial changes
  of the previous attempt, and the merge in progress of a sync, stay as they are, without any
  rollback; the prompt tells the agent that the worktree may contain them.
- **Traceability.** The model actually used goes into the attribution header of the text
  published for the turn, into the profile of the `pr`, `review`, `fix` or `sync` marker, into
  the tick log (`<agent> ran on fallback model <model>`) and, while the turn runs, into
  `devloop status`. The policy receipt keeps binding the configured reviewers.
- **Independence.** At level 2 a reviewer's fallback model runs only if it is still independent
  of who wrote the code (including the implementer's own fallback models) and distinct from the
  other reviewers of the round, as their records show them; otherwise it is skipped and listed
  as such.

## Limits

- **No automatic merge.** A human merges every PR.
- **Identities need apps.** An agent without a GitHub App publishes through the operator's `gh`
  login, and then cannot submit a native review of a PR opened with that same login.
- **Trust comes from labels and identities.** Only issues whose `agent:ready` label was added by
  a user in `trusted_users` start, only comments by those users reach the prompts, and markers are
  read only from the identity that may write them (see GitHub identities). Labelling approves the issue's title and body as they are: if someone outside
  `trusted_users` changes them later (or in the same second, since GitHub timestamps cannot
  order the two), implementation, review and fixes stop at
  `agent:needs-human` until a trusted user adds `agent:ready` again. Issue text is still model
  input: do not label untrusted content.
- **Failures escalate immediately.** A timeout or a transient error of an agent, `gh` or `git`
  goes to `agent:needs-human` rather than being retried. The only exception is an unavailable
  model of an agent with [fallback models](#fallback-models), which runs the turn again on the
  next one.
- **Single host, sequential.** One tick runs actions one at a time.
- **`gh` compatibility.** Checks, labels, comments and PR creation go through the REST API
  (`gh api`), so devloop does not depend on newer porcelain flags such as `gh pr checks --json`.
  For commit statuses the aggregate state counts, including contexts beyond the first page.
