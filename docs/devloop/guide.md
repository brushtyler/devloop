# devloop: operator guide

devloop moves issues through implementation, independent review and fixes, so that a human only
decides what starts and, unless a [merge mode](#merge-modes) lets devloop merge, what gets
merged. It is development tooling for this repository, not part
of the SSDLC orchestrator, and it never touches SSDLC runs or the main checkout's working tree.

This page is for the operator and the supervisor. Configuration keys, review levels and
identities are in the [reference](reference.md), markers and records in the
[protocol](protocol.md); the [index](../devloop.md) lists the three pages.

## The loop

State lives on GitHub: `agent:*` labels and hidden markers in comments. A tick reads that state,
plans the next steps and runs them. A crashed tick is resumed by the next one.

| Step | Trigger | Result |
|---|---|---|
| Start | A trusted user adds `agent:ready` to an issue (or the [supervisor](#labels-from-the-supervisor), on an issue of a trusted user or of a devloop app) | `agent:working`; the implementer works in a fresh worktree |
| Implement | Changes made and [local tests](reference.md#local-tests) pass | Dispatcher commits, pushes `agent/issue-<n>`, opens a PR labelled `agent:review` |
| CI | Checks pending | Wait; failing checks become a review round sent back as `agent:changes`, unless no failed test module is pertinent to the PR: then the failed jobs are re-run once, without a round (see [CI failures not pertinent to the PR](reference.md#ci-failures-not-pertinent-to-the-pr)) |
| Review | CI passed or absent (absent CI with `local_tests` other than `full`: the full suite first runs locally, see [Local tests](reference.md#local-tests)) | The dispatcher computes the PR's [review level](reference.md#review-level-and-policy-receipt); at level 1 a **different** agent posts a structured verdict (see [Structured review](protocol.md#structured-review)), at level 2 two [independent reviewers](reference.md#level-2-two-independent-reviewers) do, one per action, and their verdicts are applied together |
| Fix | `agent:changes`, or a [`/devloop fix`](#fix-requested-by-the-operator-or-the-supervisor) command on a PR in `agent:review` or `agent:changes` | The implementer fixes or disputes every open finding of the round, and applies the requests of the command, then back to `agent:review` |
| Sync | A PR in `agent:review` or `agent:ready-to-merge` conflicts with `main` | The implementer resolves the conflicts of a merge of `origin/main`; the dispatcher pushes the merge commit, back to `agent:review` (see [Conflicts with `main`](protocol.md#conflicts-with-main)) |
| Approve | Verdict `approve` from every reviewer the level requires | `agent:ready-to-merge`, only if no [finding](protocol.md#findings-and-who-closes-them) is still open, the PR head is still the reviewed commit, CI passed and the policy receipt recomputed just before promotion matches the one of every approval (an earlier base is admitted only if `main` left the PR's files alone since, see [When `main` advances](reference.md#when-main-advances-during-a-round)) |
| Level 0 | A trusted user's `reviews:0`, the whole diff in the operator's allowlist, every expected check `success` | `agent:ready-to-merge` without a review (see [Level 0](reference.md#level-0-no-review)); off by default |
| Merge | `agent:ready-to-merge` | **A human merges** with `[merge].mode = "off"`, the default. With `"strict"` or `"auto"` devloop merges once every merge directive holds again (see [Merge modes](#merge-modes)) |

A head just pushed, without CI yet, is not reviewed at once: with `local_tests` other than
`full` the review waits for its CI for `[ci] grace_minutes`, and as long as `main` requires
checks up to `[ci] required_timeout_minutes` (see [CI of a new head](reference.md#ci-of-a-new-head)).

Readiness belongs to the approved commit. Every tick re-checks ready PRs:

- if the head is no longer the approved commit, the approval is revoked and the PR goes back to
  `agent:review` for a new review;
- if CI is running again on the approved commit, readiness is suspended (`agent:review`) and the
  recorded approval is applied again, without a new review, once CI passes and if its policy
  receipt still matches;
- if CI fails on the approved commit, the failure becomes a review round (`agent:changes`), or,
  when no failed test module is pertinent to the PR, a single re-run of the failed jobs
  ([CI failures not pertinent to the PR](reference.md#ci-failures-not-pertinent-to-the-pr)).

Any step can stop at `agent:needs-human` with a comment explaining why: the
[readiness check](#readiness-check-before-implementation) found the issue not ready, the agent declared itself
blocked, tests still fail after the retry, the reviewer asked for a decision, the round limit
was reached, the reviewer pool has not enough independent reviewers for the review level, a
[level 0](reference.md#level-0-no-review) head misses an expected check, has an open finding or a standing
request for changes, an approval left open a finding of another owner, the
owner of a finding cannot be reconstructed from the markers, a sync with `main` failed, an agent, `gh` or `git` call failed or timed out, an agent
stalled with `agent_stall_action = "abort"` (see [Stalled agents](#stalled-agents)), the issue text was changed by an
untrusted user after `agent:ready`, or the PR body lost its marker. While `agent:needs-human` is
set devloop ignores the PR or issue, so to resume: fix the cause, **remove `agent:needs-human`**
and set the label that should run next (`agent:ready` on the issue, `agent:review` or
`agent:changes` on the PR). Every escalation comment repeats this. Every escalation has a
**class**, recorded in its marker; with `[merge].mode = "auto"` the non-critical ones go to
`agent:needs-supervisor` instead (see [Escalations to the supervisor](#escalations-to-the-supervisor)).

### Fix requested by the operator or the supervisor

A fix normally answers a review that asked for changes on the current head: a PR in
`agent:changes` whose head moved after that review (for example after a sync with `main`) goes
back to review instead, unless the head moved only by the syncs that the fix itself started (see
[Fixes start from the latest `main`](protocol.md#fixes-start-from-the-latest-main)). To ask for a correction that is already known, without waiting for a
review and without spending a round, comment on the PR:

```text
/devloop fix
Add the fakes that came with the merge to the conformance test.
```

- `/devloop fix` must be the **first line** of the comment (after the attribution header, for
  the supervisor). The rest of that line and the lines below it are the request; a command
  without a request is ignored.
- It counts from a user in `trusted_users` and, only when `[roles].supervisor` is configured,
  from the supervisor's GitHub App, in any merge mode. A command from anyone else is ignored and
  logged, and so is text devloop publishes with a record.
- It applies to a PR in `agent:review` or `agent:changes`. On a PR in `agent:needs-human`,
  `agent:needs-supervisor` or `agent:ready-to-merge` nothing runs.
- The next tick runs a fix of the **current head** with the request as a requirement, before any
  review. In `agent:changes` the fix also answers every finding still open. Every rule of a
  fix holds: [local tests](reference.md#local-tests), `protected_paths`, push with the implementer's
  identity, back to `agent:review`. The fix writes no review record, so it does not count
  against `max_review_rounds`; the review that follows is a new round as usual.
- **One fix per head.** Every command written before the fix runs is answered by that one fix.
  A command on a head where a requested fix already ran (for example one that changed nothing)
  is ignored and logged until a new commit reaches the PR, so that commands cannot loop.
- The fix record (`fix` marker) cites the comments that asked for it (their ids, authors and
  requests) and the head it ran on, and belongs to the latest round (round 0 before any
  review). The next review sees the request among the comments of the trusted users, as for
  any comment, and in the previous rounds of its prompt.
- A fix that stops at `agent:needs-human` (the agent is blocked, tests still fail) records
  nothing, so the command is still to apply when the PR is resumed in `agent:review` or
  `agent:changes`. To drop it, delete the comment or remove its first line.

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

A PR whose marker is missing stays in the flow
(it still counts as an open PR and its issue does not restart) and goes to
`agent:needs-human`. A PR left without a state label, for example after a crash right after it
was opened, is put back into `agent:review`, and its issue loses `agent:working`. How devloop
recognises its PRs and their marker: [PR marker](protocol.md#pr-marker).

### Open PR limits

Open devloop PRs are capped by two limits (#199), so unattended work cannot outrun review
capacity:

- `max_open_prs` counts the PRs devloop is working on: `agent:review`, `agent:changes`, a sync,
  CI running, a PR without a state label or without its marker, and any other state not listed
  below.
- `max_parked_prs` (default 3) counts the **parked** PRs, those waiting for a person:
  `agent:needs-human`, and `agent:ready-to-merge` when devloop does not merge it: `[merge]`
  `mode = "off"`, a `[merge.levels]` override `"off"` for its review level, or a diff that
  touches `operator_only_paths` ([merge modes](#merge-modes)). A ready PR that devloop merges
  itself (`strict` or `auto` for its level) is not parked, nor is one whose merge mode cannot
  be computed in that tick.

A new issue starts only while both limits leave room: fewer PRs in progress than `max_open_prs`,
and fewer parked PRs than `max_parked_prs`, since many open PRs make conflicts more likely.
`devloop status` and every tick log show the two counters, as `open PRs: <n>/<max_open_prs> in
progress (max_open_prs), <n>/<max_parked_prs> parked (max_parked_prs)`. Issues with a
`blocked_labels` label are skipped.

### Start order of ready issues

New issues start in this order (#146):

1. priority, highest first: `priority:high`, then `priority:normal` (the default, also without
   any priority label), then `priority:low`. A label listed in `priority_labels` (`prerequisito`
   in the distributed configuration) counts as `priority:high`;
2. with the same priority, the oldest `agent:ready`: the time of the **latest** `labeled` event
   of that label, the one behind the current admission. Removing the label and adding it again
   sends the issue to the back of its priority;
3. then the issue number.

A priority label counts only if the latest `labeled` event of that label on the issue comes from
a user in `trusted_users`, as for `agent:ready` (or from the supervisor, see
[Labels from the supervisor](#labels-from-the-supervisor)). Only the labels the issue has now count: a
priority label removed since is ignored, as is one added by anyone else, and the issue keeps the
highest priority among the valid ones (`normal` when none is left). The same order applies with
the [readiness check](#readiness-check-before-implementation). `devloop setup` creates the three
priority labels.

`devloop status` and `devloop tick --dry-run` list the ready issues in this order, each with its
position, priority, time of `agent:ready` and who added it, for example
`ready 1. #122 priority=high agent:ready=2026-11-16T09:12:00Z labelled by brushtyler: …`. The
list covers every issue labelled `agent:ready`; the planned actions that follow it say which ones
start on this tick (free PR slots, `blocked_labels`, `agent:needs-human` and an untrusted
`agent:ready` still apply).

### Labels from the supervisor

With `[roles].supervisor` set and a GitHub App for that agent in `[apps]`, the supervisor queues
issues and sets their priority with its own identity (#178): `agent:ready`, the `priority:*`
labels and those in `priority_labels` count also when the supervisor's app added them, but
**only on issues opened by a user in `trusted_users` or by one of devloop's apps** (the agents'
and the dispatcher's). On an issue opened by anyone else only a trusted user's label counts, as
in [AGENTS.md](../../AGENTS.md) ("Contenuti di autori non fidati").

As for the other label checks, the latest `labeled` event of the label decides: a trusted
user's `agent:ready` later replaced by the supervisor's on a stranger's issue no longer admits
it, nor approves its text any longer, exactly as an untrusted edit after the label would void
it. A supervisor's priority on an admitted issue changes the start order exactly as a trusted
user's would. The label approves the title and the body as they are; an edit by anyone outside
`trusted_users` afterwards, the supervisor included, voids it as usual.

Without `[roles].supervisor`, or without an app for it (the supervisor then acts with the
operator's `gh` login), only `trusted_users` count, as before.

### Readiness check before implementation

With `preflight = "<agent>"` in the configuration, an issue is checked before the implementer
starts on it (#114). The distributed `devloop/config.toml` leaves it **disabled**, with a
commented example: enabling it is the operator's decision. Without `preflight`, issues start as
described above.

The checker is a configured agent other than the implementer. It runs in review mode (the
read-only arguments and sandbox of a reviewer, see [Configuration](reference.md#configuration)) in a
disposable detached worktree at `origin/main`, `issue-<n>-preflight`, removed afterwards, and
gets the implementer's context: title, body and the comments of `trusted_users`. Its answer
follows [`devloop/schemas/preflight.json`](../../devloop/schemas/preflight.json):

- `ready`, a boolean;
- `gaps`, each with a `kind` (`operator_decision`, `default_available` or `tool_unavailable`),
  a `description`, the `code_refs` that show it and a `proposed_default` (`""` if none);
- `split`, how to divide an issue too large for one run, `""` if not needed.

An issue is ready only if the agent says so **and** lists no gap and no split: a
`default_available` gap blocks too, until a trusted comment accepts the proposed default. A
default the repository or the issue already prescribes is not a gap.

The outcome is a comment on the issue, with a marker that records it and a hash of the request
(see [Readiness check records](protocol.md#readiness-check-records)). The outcome is reused
while the hash stays the same:

- **ready**: the issue starts on a later tick, in the usual order and within `max_open_prs` and `max_parked_prs`.
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

Rechecks run only for blocks the check owns: when the check owns `agent:needs-human` is
described in [Readiness check records](protocol.md#readiness-check-records).

Each check is an agent action: it takes a slot of `max_actions_per_tick`, and a check of an
issue that may start also takes one of the free PR slots of the tick. It runs under
`agent_timeout_seconds`. A check that fails or times out sends the issue to
`agent:needs-human` with a comment, removing `agent:ready` like any failed step, and the
implementer does not start. An agy check stopped on a denied permission is first run once more
([agy permissions](reference.md#agy-permissions), #180).

## Merge modes

`[merge].mode` decides who merges a PR in `agent:ready-to-merge` (#172):

| `mode` | Who merges |
|---|---|
| `"off"` (default, and this repository's) | A person, as before |
| `"strict"` | devloop, only when every merge directive below holds |
| `"auto"` | As `"strict"`; in addition the supervisor can [hold a merge](#holding-a-merge) and [decide non-critical escalations](#escalations-to-the-supervisor) |

- **Per level.** `[merge.levels]` gives a mode to a review level, for example `"2" = "off"`.
  The most restrictive of that mode and `mode` applies: `off`, then `strict`, then `auto`. An
  override never relaxes `mode`.
- **Operator-only paths, merge directives and record.** See
  [Merge directives and record](protocol.md#merge-directives-and-record).

### Closing the issue

With a merge made by the merger app, GitHub does not apply the PR's "Closes #N" (#228). After
every merge in `"strict"` or `"auto"` the dispatcher therefore closes the issue named by the
PR's marker itself, with its own identity (the merger app gets no new permission): a dispatcher
comment with a `closed` marker records the PR, the merged head, the merge commit and the mode,
then the issue is closed as completed and loses `agent:ready`. An issue that is already closed,
because GitHub closed it or a person merged, is left alone. A failure to close is written to the
dispatcher log and leaves the merge as it is.

**Safety net in the planner.** An open issue in `agent:ready` with a merged devloop PR (a PR from
its branch, `<branch_prefix><issue>`, by a trusted user or a devloop app, merged by anyone) is
never checked nor implemented again. It becomes a `merged_issue` escalation, non-critical: in
`"auto"` it goes to `agent:needs-supervisor` and keeps `agent:ready`, otherwise to
`agent:needs-human` without `agent:ready`. `/devloop retry` is refused for it: closing the issue,
or `/devloop escalate <reason>`, settles it. More work on the same subject needs a new issue.
The same check runs before a `/devloop retry` of an issue already escalated for another class
(a `tool_failure` of its readiness check or implementation, for example): with a merged devloop
PR the retry is refused, the step is not run, and the issue gets a `merged_issue` escalation
routed as above.

### Holding a merge

Only in `"auto"`. A PR comment whose first line, after the attribution header if any, is

```text
/devloop hold <reason>
/devloop release
```

holds or releases the merge. `hold` adds `agent:merge-hold`: the PR stays ready and is not
merged. `release` removes the label, and the PR is merged once the directives hold; removing the
label by hand does the same. A hold needs a reason.

The commands count only from the identity of `[roles].supervisor` (the GitHub App of that agent)
and from `trusted_users`. The dispatcher applies a command with a dispatcher comment that carries
a `hold` marker (event, author, reason, head, and in `comment` the id of the comment it applied),
and that record spends the command it applied and every command written before it: a command
written after it, even one that arrived while the record was being written, still waits to be
applied. Among the commands that wait the latest wins. A command is applied before anything else
on its PR in that tick, so no merge of the same tick overtakes it, and a merge about to run reads
the comments again and stops if a command is waiting. A command is ignored, and written to the
dispatcher log, when the merge mode of its PR is `"strict"` or `"off"` (the mode with the
override of its level and its operator-only paths, so a PR at a level overridden to `"strict"`
ignores it even with `mode = "auto"`), from anyone else, without a reason, and in text devloop
publishes (a comment that ends with a devloop marker). As for
`/devloop finding`, devloop reads the comments of a PR only while it is in `agent:review`,
`agent:changes` or `agent:ready-to-merge`.

`"auto"`, also as the override of a level, without `[roles].supervisor` stops devloop at
startup. The supervisor names a configured agent; the escalations it decides are described
below.

```toml
[roles]
supervisor = "claude"

[merge]
mode = "auto"
operator_only_paths = ["docs/adr/", "AGENTS.md", "CLAUDE.md"]

[merge.levels]
"2" = "off"
```

### Escalations to the supervisor

Every escalation of the dispatcher has a **class** (#182), recorded in its marker: the `notice`
marker of an escalation comment (`class`, `critical`, and `to`, `operator` or `supervisor`), the
`notice` of an agent that stopped (`blocked`), the `preflight` marker of a failed readiness
check, and the `policy` marker of a level that cannot be reviewed or promoted. A reviewer's
`needs_human` verdict gets an escalation comment of class `reviewer_decision` next to the review.

| Class | When | Critical |
|---|---|---|
| `tool_failure` | The agent or its model produced no result: a crash, a timeout, a stall with `agent_stall_action = "abort"`, no model available, a denied permission, a block for [background work still running](reference.md#no-background-work-in-a-turn), an agent that [could not start](protocol.md#failed-agent-turns) even after its retry (also in a sync with conflicts on sensitive paths) | No |
| `preflight_default` | A failed [readiness check](#readiness-check-before-implementation) whose gaps are all `default_available`, with no split | No |
| `sync_failed` | A [sync](protocol.md#conflicts-with-main) that failed, with no file in conflict under a sensitive path (`sensitive_paths`) or `operator_only_paths`; one whose agent could not start is a `tool_failure` | No |
| `merged_issue` | An open issue in `agent:ready` that a merged devloop PR already resolved ([Closing the issue](#closing-the-issue)) | No |
| `round_limit` | The limit of review rounds | Yes |
| `operator_decision` | A readiness check with an `operator_decision` or `tool_unavailable` gap, or a split | Yes |
| `untrusted` | The issue text changed after `agent:ready` by someone outside `trusted_users` | Yes |
| `reviewer_decision`, `open_findings` | A reviewer asks for a decision; an approval leaves open findings only the operator can close | Yes |
| `review_level`, `level_zero` | Not enough independent reviewers; a level-0 head that cannot be promoted | Yes |
| `sync_sensitive`, `protected_paths` | A sync conflict on a sensitive or operator-only path; a change to `protected_paths` | Yes |
| `tests_failed`, `blocked`, `interrupted`, `unattributable`, `lost_meta`, `inconsistent`, `configuration`, `failure` | Tests still failing (or no change), an agent that declared itself blocked, an interrupted implementation, findings without an owner, a lost PR marker, an inconsistent PR state, a configuration to decide, a `gh`, git or dispatcher error | Yes |
| `retry_limit`, `supervisor_escalated`, `supervisor_refused` | The supervisor's retries are spent, it handed the escalation over, or it gave a command the escalation does not admit | Yes |

Any other class, or none (escalations written before #182), is critical.

**Routing.** With `[merge].mode = "auto"` a non-critical escalation adds
`agent:needs-supervisor` instead of `agent:needs-human`, and an issue keeps `agent:ready`. It
stays with the operator, with a comment that says why, when:

- the merge mode of the PR is not `"auto"`: the override of its level, or a path of
  `operator_only_paths` in its diff;
- the supervisor's agent implemented the PR;
- the issue was opened by someone outside `trusted_users` and devloop's apps, or its text is no
  longer approved by `agent:ready`;
- the issue (or the PR) has the label `phase:design`;
- any of this cannot be verified.

With `"off"` and `"strict"` every escalation goes to `agent:needs-human`, as before. An item in
`agent:needs-supervisor` is not worked on, and a merge does not run, until the supervisor
decides. `devloop setup` creates the label.

**Commands.** A comment on the issue or PR whose first line, after the attribution header if
any, is one of:

```text
/devloop retry
/devloop accept-defaults
/devloop decide <decisions>
/devloop escalate <reason>
```

They count only in `"auto"`, from the identity of `[roles].supervisor` (the GitHub App of that
agent) or from `trusted_users`, only on an item in `agent:needs-supervisor` without
`agent:needs-human`, and only when written after the latest escalation. Any other command is
ignored and written to the dispatcher log; text devloop publishes is never a command. As for
[holding a merge](#holding-a-merge), a dispatcher record spends the command it applied and every
command before it, and among the commands that wait the latest wins.

- `/devloop retry` runs the failed step again, for `tool_failure` and `sync_failed`. A PR goes
  back to the label of its step (`agent:review` for a review or a sync, `agent:changes` for a
  fix, `agent:review` for a [requested fix](#fix-requested-by-the-operator-or-the-supervisor)
  that started there, `agent:changes` for a sync that a fix in `agent:changes` started because
  its branch conflicted with `main`) and the next tick runs it; an issue's readiness check or implementation runs at once, and
  takes a free PR slot as it would. A failed attempt writes no review record, so a retry
  **counts no round**. After the initial failure the supervisor may authorise **two** retries
  for the same key; the third command is not run and the item goes to `agent:needs-human`
  (`retry_limit`). The automatic retries inside one run (a permission denied to agy,
  [fallback models](reference.md#fallback-models)) do not count. The key is durable, read from the
  escalation and the dispatcher's records on GitHub: the item, the step and, for a review, the
  reviewer; for a PR the head the step failed on, for an issue the commit of the base branch and
  the hash of the request (title, body and trusted comments). An issue's key is taken before the
  step runs, from the commit its checkout starts at and the request its prompt gets, so a base
  or a text that changes during the turn already makes the retry refer to a superseded input.
  The retries are counted per reviewer: when several reviewers fail together and later only
  some of them, each keeps the retries it already had. A retry whose input has changed
  since (a new head, base or request) is refused, and the escalation stays with the supervisor.
  Before a retry runs, what made the escalation non-critical is checked again on data read
  then (the conditions of **Routing** above: `phase:design`, author and text of the issue, merge
  mode and implementer of the PR): if one no longer holds, the retry is refused and the item
  goes to `agent:needs-human` (`supervisor_refused`).
- `/devloop accept-defaults` accepts the defaults of a `preflight_default` escalation, only when
  **every** gap of the latest check is `default_available`, no split is needed, the issue has no
  `phase:design` label, was opened by a trusted user or a devloop app, and the check is the one of
  the current text. The dispatcher records the accepted defaults; that record counts as a
  trusted comment, both in the hash of the request and in the agents' prompts. The issue goes
  back in the queue: the readiness check runs again with the defaults and, if ready, the
  implementer starts. An issue with an `operator_decision` gap is refused and goes to
  `agent:needs-human`. The readiness prompt tells the checker that a gap touching a decision ADR
  0001 reserves to the operator (rollout, budget and rounds, credentials and permissions, trust
  rules, the replay gate baseline, the merge mode, the continuous loop and automatic pinning, ADR
  approval), or a change to `operator_only_paths`, is always `operator_decision`.
- `/devloop decide <decisions>` decides the gaps of a `preflight_default` escalation otherwise
  than `accept-defaults` does: the supervisor may accept some defaults and replace others. The
  decisions are the rest of the comment, from the text after the command on its first line to
  the end; a command without them is ignored. It admits the same escalations as
  `accept-defaults`, under the same conditions, and is refused in the same cases. The dispatcher
  records the decisions with the gaps they answer and removes `agent:needs-supervisor`; the
  issue goes back in the queue and the readiness check runs again with the decisions as a
  trusted comment, as it does with accepted defaults (they count in the hash of the request and
  reach the prompts of the check and of the implementer). The decisions reach the prompts **only
  through that record**: the supervisor's own comment, as any comment of an author outside
  `trusted_users`, never does. Gaps reserved to the operator (`operator_decision`) are not the
  supervisor's: the operator decides them with a comment of their own on GitHub.
- `/devloop escalate <reason>` hands the escalation to the operator: `agent:needs-human`, with
  the reason (`supervisor_escalated`). It needs a reason.

An escalation whose class is critical or unknown, or that was not routed to the supervisor,
accepts no command: the command is refused and the item goes to `agent:needs-human`. A command
the class does not admit (`accept-defaults` or `decide` on a tool failure, `retry` on defaults) is refused
and the escalation stays with the supervisor.

**Trace.** Every command applied leaves a dispatcher record, described in
[Supervision trace](protocol.md#supervision-trace).

## Who does what

Roles (operator, coordinator, supervisor, dispatcher, preflight, implementer, reviewer), what
each one does and does not do, the merge modes and the critical and non-critical decisions are
defined in [ADR 0002](../adr/0002-ruoli-e-responsabilita.md), the source for them. The
[merge modes](#merge-modes) and the [escalations to the supervisor](#escalations-to-the-supervisor)
above describe how devloop applies them.

## Usage

```bash
python3 -m devloop setup            # create the agent:* labels, the priority:* labels and the review level labels (reviews:0 only when level 0 is on)
python3 -m devloop status           # running action, devloop PRs (with merge state and conflicts), ready issues in start order with their priority, planned actions
python3 -m devloop apps             # check the GitHub App of each actor
python3 -m devloop tick --dry-run   # print the ready issues in start order and the plan, change nothing
python3 -m devloop tick             # run one round
python3 -m devloop run --idle-timeout 900  # tick continuously (see Continuous loop)
python3 -m devloop cleanup          # remove worktrees left by failed steps
python3 -m devloop follow issue-52  # follow live the implementer of issue #52 (pr-<N>: reviewer or fixer)
python3 -m devloop watch --timeout 3600  # wait for the next change on GitHub, for the supervising agent
python3 -m devloop pin origin/main  # pin a commit of origin (see Run from a pinned installation)
python3 -m devloop fallback reset agy  # forget the models agy remembers unavailable (see Memory of unavailable models)
```

Run it from any checkout of this repository. Worktrees, agent prompts and logs, and the lock that
prevents two dispatchers from running at once live under `state_dir` (default `.ssdlc/devloop`)
in the main checkout. In daily use run it from the pinned copy, through
`scripts/devloop-pinned` ([Run from a pinned installation](#run-from-a-pinned-installation)).
`--local-config <file>`, before the command, applies a local overlay to the configuration
([Local config as an overlay](#local-config-as-an-overlay)).

### Watching a tick

While a tick runs an action it records it in `state_dir/activity.json`: the action, the issue or
PR, the agent and the path of its prompt once an agent turn starts, the model when the turn runs
on a [fallback model](reference.md#fallback-models), the start time and the dispatcher's process id. The
record is removed when the action ends, also when it fails. While several
[parallel actions](reference.md#parallel-actions) run, the file holds `{"actions": [...]}`, one record per
action, and `devloop status` shows one line for each; a reviewer running alongside another one
in the same action leaves the action's record as it is. `devloop status` shows it first, with
how long it has been running and, for a fallback, `on fallback model <model>`. If no dispatcher holds the
lock, or the recorded process is gone, the action is reported as `interrupted`: the tick was
killed before it could clean up, and the next tick overwrites the record when it starts an action.

devloop writes an agent's log only when its turn ends. To follow the agent while it works,
`devloop follow issue-<N>` (implementer) or `devloop follow pr-<N>` (reviewer or fixer) prints
the messages and tool calls of the newest Claude (`~/.claude/projects`) or Codex
(`~/.codex/sessions`) session transcript whose working directory is that worktree, then keeps
following it, and switches to a newer transcript of the same worktree when one appears, for
example the next attempt of a turn. The reviewer works in a disposable copy of the PR worktree,
`pr-<N>-review` (`pr-<N>-review-<reviewer>` for each reviewer of a level-2 round running in
parallel), so `devloop follow pr-<N>` looks at all of them and follows the newest session: the
reviewer's while a review runs, the fixer's while a fix runs. It reads local files only and needs no GitHub access. Ctrl-C
stops it. Antigravity sessions are not supported. For this reason devloop runs `codex exec`
without `--ephemeral` (and with `--json`, for the [telemetry](protocol.md#evidence-and-telemetry)): Codex saves the session of every turn in `~/.codex/sessions`, and these
transcripts stay there after the turn.

### Waking the supervising agent

devloop works in ticks that someone starts, by hand or with the
[continuous loop](#continuous-loop). The agent that supervises it (Claude Code, Codex or
Antigravity) needs to know when something changed on GitHub. `devloop watch` (#173) reads the
repository every `--interval` seconds (default 60) and prints one line per change:

```text
HH:MM <type> #N <detail>
```

The time is UTC. A line carries no secret and no local path, and is a single line: titles and
comment text are cut short and control characters removed. What it reports:

| Change | Line |
|---|---|
| Issue or PR opened | `new PR #N [labels] title`, `new issue #N [labels] title` |
| Issue or PR closed or merged | `PR #N merged: title`, `issue #N closed (completed): title` |
| Labels added or removed | `PR #N labels +agent:changes -agent:review` |
| New head of a PR | `PR #N new head abc1234` |
| CI turning `pass` or `fail` on the head (the checks the dispatcher reads) | `PR #N CI pass @abc1234` |
| devloop review: the valid `review` record in the conversation, from the identity that may write it (its native review is not reported again) | `PR #N review codex approve round 2 @abc1234` |
| Native `APPROVED` or `CHANGES_REQUESTED` review of a trusted user | `PR #N review by brushtyler: APPROVED @abc1234` |
| Comment of a trusted user or of a devloop app; one a trusted user wrote in person (no attribution header) is an `operator comment` | `issue #N operator comment by brushtyler: first line` |

The [ledger](protocol.md#evidence-and-telemetry) records (`## Registro devloop`) are not reported: they are
too frequent. Trust follows [AGENTS.md](../../AGENTS.md): an issue or PR counts if its author is in
`trusted_users` or is one of devloop's apps (the `[apps]` logins of the agents and the
dispatcher; not the [merger app](reference.md#the-merger-app), which only merges: its comments and the
`review` records naming it are ignored). Anyone else's issue or PR
produces only `new issue #N by an untrusted author (ignored)` (or `new PR`), with no title nor
content, and nothing after it. The exception is an issue whose latest `agent:ready` was added by
a user in `trusted_users`, whether the label is still there or the dispatcher already replaced it
with `agent:working`: while so it is reported (`issue #N admitted with agent:ready by <login>`),
its labels, closure and trusted comments, never its title. A later `agent:ready` added by anyone
else ends the admission. The labeled events of an external issue are read when it is first seen
and whenever its labels change. Labels, heads, CI and closures of a trusted issue or PR are
reported whoever changed them; comments and reviews only when their author is trusted or a
devloop app. A trusted comment on an issue or PR outside the open listings (closed before the
baseline, say) is reported too: the watcher reads its author once and remembers it. On such an
external issue the admission can change while it stays closed, so the watcher reads its latest
`agent:ready` again in every round with a new trusted comment on it.

Two modes:

- **`--until-change`** (default): the command ends at the first round with news, exit code 0,
  with its lines on stdout. With `--timeout <seconds>` it exits with code 3 once that many
  seconds passed since it started without news, failed reads included (monotonic clock): each
  `gh` call gets at most the time left, and so does each step (signature, HTTP request) of
  the mint of the dispatcher app's token; a round that ends after the timeout is dropped
  without saving, so what it read is reported by the next run. Without `--timeout` it waits
  forever. It suits any agent that is woken when a command ends.
  For Claude Code, run it as a background Bash command: Claude Code is woken when it exits, reads
  the lines, acts and starts it again.

  ```bash
  python3 -m devloop watch --until-change --timeout 3600   # Bash tool, run_in_background: true
  ```

- **`--exec "<command>"`**: the watcher keeps running and, after each round with news, runs the
  command with the lines on its stdin, and waits for it to end: never two at a time. The
  command is split as a shell would (`shlex.split`) but runs without a shell. It suits agents
  without a notification when a command ends, such as Codex or Antigravity resuming a session:

  ```bash
  python3 -m devloop watch --exec "codex exec resume --last -"   # "-": Codex reads the prompt from stdin
  ```

  A command that does not start or exits with an error is reported on stderr, and the watcher
  goes on without delivering those lines again. `--timeout` is refused with `--exec`.

The first run records the starting state and prints `watch baseline: N open PRs, M open issues`,
which is not news. The state is saved in `state_dir/watch-state.json` of the main checkout
(`--state <path>` for another file), atomically, after every successful round and **before** its
lines are printed or delivered: a restarted watcher does not repeat lines, and the next run of
`--until-change` reports at once what changed meanwhile. A state file that cannot be read gets a
new baseline. Run one watcher per state file.

A failed read of GitHub (a transient `gh` error) does not end the watcher: the round is retried
after the interval and the state of the latest successful round is kept. Each failed round
writes `devloop watch: round failed (<class>)` on stderr, with the class of the error and never
`gh`'s output, tokens or paths: `authentication` (no `gh` login, bad credentials, an app token
that could not be minted), `permission`, `not found`, `rate limit`, `server error`, `network`,
`gh unavailable`, `cli_version` (the installed `gh` does not know a `--json` field: `Unknown
JSON field`), or the name of the error's type when none applies. After three consecutive
failed rounds it emits one line, `watch error: 3 consecutive rounds could not read GitHub, the
last valid state is kept`, without `gh`'s output; it counts as news (`--until-change` exits with
0, `--exec` delivers it). A successful round resets the count. This includes the read of an issue
or PR that left the open listings: only GitHub's answer that it does not exist (deleted or
transferred) gives `no longer open`.

The watcher reads with the same identity as the ticks' reads (#197): the dispatcher's app,
when `[apps]` configures one, with its installation token in `GH_TOKEN`, so it works without
any `gh` login (as the `devloop` user does); the keys of the `[apps]` are needed, as for `tick`. Without a
dispatcher app it reads with the local `gh` login. Each round reads the open PRs
and issues (`gh pr list`, up to 200, and `gh issue list`, up to 300), the comments since the
previous round, for each trusted open PR its checks and native reviews, and the labeled events
of an external issue whose labels changed or, outside the open listings, with a new trusted
comment.

### Continuous loop

`devloop run` (#177) moves devloop forward without anyone starting the ticks:

1. it runs a tick;
2. if the tick ran at least one action, the next tick starts at once;
3. otherwise it waits for news on GitHub, as `devloop watch --until-change` does
   ([Waking the supervising agent](#waking-the-supervising-agent)), for at most
   `--idle-timeout` seconds (default 900, 15 minutes); at the first news, or at the timeout, it
   runs the next tick.

With `max_parallel_actions` above 1 the loop runs in [continuous mode](#continuous-mode): a tick
starts its actions without waiting for them, and while some run the loop waits for one to end
or, with a slot free, for news on GitHub; either way the next tick plans at once.

The wait reads GitHub every 60 seconds (less when `--idle-timeout` is shorter), with the same
identity as the ticks' reads, and keeps its state in `state_dir/run-watch-state.json`, apart from
the state of a `devloop watch` of the supervising agent. The first wait records the starting
state; a later one compares with the state of the previous wait, so changes made by ticks with
actions in between count as news and cost at most one more tick. A wait that fails waits the
rest of the timeout. `--interval <seconds>`, the option of the loop before #177, is a deprecated
alias of `--idle-timeout`: it still works, with a warning in the log, and the two together are
refused.

**Clean stop.** `SIGTERM` or `SIGINT` (Ctrl-C), or the file `state_dir/STOP`
(`.ssdlc/devloop/STOP` of the main checkout), stop the loop: the tick in progress ends, no other
starts, and a wait in progress ends at once (after the read of GitHub in progress, if any: no
other read starts). A stop requested during a tick that ends with a new
[pin after a merge](#pin-after-a-merge) ends the loop too, without the restart from the new copy;
the next start runs the new copy. The exit code is 0. Agent turns and tests
run in process groups of their own, so the signal reaches only devloop and the tick finishes its
actions. devloop never removes `STOP`: while it exists `devloop run` says so in the log and exits
with 0 at once, without a tick, so not even a restart by systemd starts the loop again. The
operator removes it to start again.

**Failed ticks.** A tick fails when it ends with an error (`gh` or `git` unreachable, a failed
[pin after a merge](#pin-after-a-merge), any unexpected error), not when one of its actions
fails: the tick escalates that action as usual and counts as a success. After a failed tick the
loop waits 1 minute, after the second failure in a row 5 minutes, and the third failure in a row
stops the loop at once (the 30 minutes that would follow it are never waited), with an alert:

- with `[run].alert_issue` (an issue number, unset by default: the operator chooses it in the
  [local overlay](#local-config-as-an-overlay)), a comment of the dispatcher on that issue, with
  the attribution header and the class of each error (`network`, `authentication`, … as in
  `devloop watch`, or the name of the error's type), never its message, paths or tokens. One
  attempt only: if it fails, the log says so, with the class of that error;
- without it, a line `ALERT: 3 consecutive ticks failed (…): the loop stops` in the local log.

Either way the loop exits with code 4 and runs no other tick. A successful tick resets the count.
The log keeps the full message of each failed tick.

**Lock.** The loop holds the dispatcher lock (`state_dir/dispatcher.lock`) for as long as it
runs, waits included: a manual `devloop tick` (or `cleanup`) exits with `another dispatcher is
running (lock held)`, and a loop does not start while a manual tick runs. To run a tick by hand,
stop the loop first. A [pin after a merge](#pin-after-a-merge) restarts the loop from the new
copy with the same arguments and the lock still held, unless a stop was requested meanwhile.

**Activation is the operator's.** The loop runs unsupervised and spends the agents' quotas, so
devloop installs no service. An example unit, `devloop.service`, for the
[installation as a dedicated user](#installation-as-a-dedicated-user), running the
[launcher](#launcher) of the pinned copy, which applies `.ssdlc/devloop/config.local.toml` when
it exists ([local overlay](#local-config-as-an-overlay)):

```ini
[Unit]
Description=devloop continuous loop
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=devloop
Group=devloop
WorkingDirectory=/home/devloop/ssdlc
# Where the agent CLIs, gh and git are found for the devloop user.
Environment=PATH=/home/devloop/.local/bin:/usr/local/bin:/usr/bin:/bin
# The launcher adds --local-config .ssdlc/devloop/config.local.toml when that file exists.
ExecStart=/usr/bin/python3 /home/devloop/ssdlc/scripts/devloop-pinned run --idle-timeout 900
# SIGTERM to devloop only: the tick in progress ends, its agent turns and tests are not killed.
# The default KillMode=control-group would send SIGTERM to them too.
KillMode=mixed
# Longer than the longest tick; after it systemd kills the tick (its action shows as
# interrupted in `devloop status`).
TimeoutStopSec=4h
# A crash restarts the loop; a clean stop (0, STOP file included) and exit code 4 (three failed
# ticks in a row, alert sent) do not.
Restart=on-failure
RestartSec=5min
RestartPreventExitStatus=4

[Install]
WantedBy=multi-user.target
```

```bash
sudo install -m 644 devloop.service /etc/systemd/system/devloop.service
sudo systemctl daemon-reload
sudo systemctl start devloop      # start the loop now
sudo systemctl enable devloop     # and at every boot (optional)
journalctl -u devloop -f          # follow its log
sudo systemctl stop devloop       # SIGTERM: the tick in progress ends, then the loop exits
sudo -u devloop touch /home/devloop/ssdlc/.ssdlc/devloop/STOP  # stop after the tick, and stay stopped
sudo -u devloop rm /home/devloop/ssdlc/.ssdlc/devloop/STOP     # then `systemctl start devloop`
```

After an alert (exit code 4) the unit stays stopped: remove the cause, then
`systemctl start devloop`.

### Continuous mode

With `max_parallel_actions` above 1, actions run in one of two modes (#227):

- **`devloop tick`: one round with a barrier.** The tick plans, runs its actions as
  [Parallel actions](reference.md#parallel-actions) describes, and ends when every one has ended. This is
  the mode of manual use and of the tests, unchanged.
- **`devloop run`: no barrier at the end of a tick.** A tick plans and starts in the free slots
  the actions that fit, up to `max_parallel_actions` running at once, and returns without
  waiting for them. As soon as one ends, the next tick reads GitHub again and plans the free
  slots; while a slot is free, news on GitHub (a CI that passes, a new ready issue) starts a
  tick too, within the usual `--idle-timeout`. A long action (a 45-minute implementation) no
  longer holds the reviews and fixes that become ready meanwhile. An action that ends while a
  tick reads GitHub starts the next tick at once, even when it was the last one running.

In `devloop run` a plan leaves out the issues and PRs of the running actions: no second action
starts on the same number, nor on a PR and its issue, until the first has ended; of two actions
planned on the same number only the first starts, and the next tick, which sees its result,
decides the other. An implementation or readiness check that is running keeps its slot of
`max_open_prs`. A global action (a merge) starts only when nothing runs, alone: while one is
planned and actions are running, no other action starts, so they end and the global action runs
next; the actions planned with it start in the ticks after it. While a global action runs, no
other action starts, even when news on GitHub start a tick and slots are free. The
[pin after a merge](#pin-after-a-merge) runs only when no action runs, and while one is pending
no action starts. The turns of running actions are not marked incomplete by the ticks that run
meanwhile; they are checked once their action ends.

The dispatcher lock stays one: the loop runs the actions in threads of its own process. A stop
(`SIGTERM`, `SIGINT`, the `STOP` file) starts no other tick and waits for the running actions to
end before the loop exits; so does the failed tick that stops the loop, before the alert
([Continuous loop](#continuous-loop)). Each action logs its
`outcome of <action> #<N>: …` line when it ends. With `max_parallel_actions = 1`, `devloop run`
runs ticks with the barrier, one action at a time, as before.

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

devloop works on this repository, including on its own code, so it runs from a pinned,
read-only copy of one commit of `origin`, never from the checkout it is changing (#174). This is
the same rule as the pinned controller SSDLC uses to develop itself. The configuration, schemas
and prompts come from the pinned copy; GitHub state and worktrees come from the clone.

#### Installation as a dedicated user

The installation in use since 2026-11-16 (#185):

- an unprivileged system user `devloop`, with a clone of its own (`/home/devloop/ssdlc`);
- state, pinned copies and the local config in `.ssdlc/devloop` of that clone;
- the GitHub App keys in `~/.ghapps` of that user (each key mode `600`), the agent CLIs logged
  in with logins of that user, and no `gh` login: devloop acts with its apps. A
  [merge mode](#merge-modes) other than `off` merges with the [merger app](reference.md#the-merger-app),
  whose key is there too; without that app it would need a local `gh` login.

```bash
sudo useradd --create-home devloop
sudo -u devloop -H git clone https://github.com/brushtyler/ssdlc.git /home/devloop/ssdlc
sudo -u devloop -H install -d -m 700 /home/devloop/.ghapps   # then the keys, mode 600
# The first pin runs the clone's devloop; every later command runs the pinned copy.
sudo -u devloop -H sh -c 'cd ~/ssdlc && python3 -m devloop pin origin/main'
sudo -u devloop -H python3 /home/devloop/ssdlc/scripts/devloop-pinned run --idle-timeout 900
```

Turns must not depend on an update of an agent CLI in progress: a CLI that replaces its own
binary leaves, for a few seconds, nothing to start (#218; the start is retried once, see
[Failed agent turns](protocol.md#failed-agent-turns)). For the CLIs devloop uses, either:

- disable their automatic updates: for Claude Code, `DISABLE_AUTOUPDATER=1` in the launcher's
  environment. The [launcher](#launcher) exports it unless the environment already sets it;
- or install them in the home of the `devloop` user, as `codex` and `agy` already are, so that
  the updates of the operator's sessions do not touch them.

#### Pin

`python3 -m devloop pin <ref>`, from the main checkout (or `scripts/devloop-pinned pin <ref>`):

1. updates the branches and tags of `origin`, dropping those deleted on `origin` (the tags of
   `origin` go to `refs/devloop/origin-tags/`, apart from the local tags), and resolves `<ref>`:
   a name without prefix is a branch of `origin` (`main` is `origin/main`, never a local
   `main`), otherwise a tag of `origin`, and the commit must be reachable from a branch or a tag
   of `origin`. A commit only local, or reachable only from a local tag or from a branch deleted
   on `origin`, is refused. The commit of a copy already pinned (also abbreviated) is used
   without a fetch: that is the rollback;
2. reads `devloop/config.toml` of that commit with the local overlay applied, and takes the files
   of `review_policy.sensitive_path_lists`: each must be a relative path inside the root and a
   regular file of the commit. The documents of the [documentation map](reference.md#documentation-map) are
   not taken: they belong to the repository, not to the installation;
3. extracts `devloop/` and those files into `.ssdlc/devloop/pinned-<sha12>`, with a `pin.json`
   naming the commit, and makes the copy read-only;
4. checks the copy: in a Python with only the copy on its module path, the package imports, and
   its configuration, with the overlay, loads (a `[docs] map` of the wrong shape fails here) and
   reads `sensitive_path_lists` from the copy;
5. points `.ssdlc/devloop/current` at the copy, with an atomic rename of a symlink.

A failure at any step leaves `current` as it was, and leaves no new copy behind. A copy that
already exists for the same commit is reused, but checked again against the current overlay: if
a file it needs is missing, the pin fails. Earlier copies stay, for the rollback. `pin` uses the
overlay given with `--local-config`, or `.ssdlc/devloop/config.local.toml` when it exists. A
`run` already started keeps the code it started with: restart it after a pin by hand (a pin
after a merge restarts it by itself, see below). `devloop cleanup` removes only the agent
worktrees under `state_dir/worktrees`, never a pinned copy.

#### Launcher

`scripts/devloop-pinned [--allow-root] <devloop arguments>` runs devloop from the copy that
`.ssdlc/devloop/current` points at:

```bash
python3 scripts/devloop-pinned status
python3 scripts/devloop-pinned run --idle-timeout 900
sudo -u devloop -H python3 /home/devloop/ssdlc/scripts/devloop-pinned tick --dry-run
```

It runs `python3 -P -m devloop` from the root of its clone, with `PYTHONPATH` on the copy only
and, when `.ssdlc/devloop/config.local.toml` exists, `--local-config` on it. It imports nothing
from the clone, and works both run directly by the `devloop` user and through `sudo -u devloop
-H`. Without a pin it stops and says so. It refuses to run as root, unless `--allow-root` is its
first argument. `-P` is required: without it `python3 -m` puts the current directory first on
the module path and imports the checkout's `devloop/` instead of the pinned one. Agents and test
commands do not inherit that `PYTHONPATH`: the tests of a worktree import the worktree's code.
It exports `DISABLE_AUTOUPDATER=1`, which the agents inherit, so that Claude Code does not update
itself while devloop runs it; a value already in the environment (the operator's) is kept.

#### Local config as an overlay

`.ssdlc/devloop/config.local.toml` holds **only the keys the operator overrides**, for example:

```toml
preflight = "codex"
max_open_prs = 3
max_review_rounds = 4

[merge]
mode = "strict"

[pin]
auto = true
```

devloop applies it over `devloop/config.toml` of the pinned copy: a table merges key by key
(`[merge]` above keeps `operator_only_paths` of the copy), any other value, a list too, replaces
the one of the copy. A new pin therefore needs no realignment of the config by hand. A key that
`config.toml` cannot have (`bogus = 1`, `[merge] method = "squash"`) stops devloop, and so does
any value the copy's config would refuse. An overlay cannot remove a key of the copy, only
replace it.

#### Rollback

Pin the earlier commit again: its copy is reused, checked against the current overlay, and
`current` points back at it, with no fetch needed.

```bash
python3 scripts/devloop-pinned pin 1a2b3c4d5e6f   # the sha12 of an earlier pinned-<sha12>
```

Any pin, by hand or after a merge, also drops a pin requested by a merge and not yet done, so a
rollback is not undone by the next tick.

#### Pin after a merge

With `[pin] auto = true` (default `false`: the operator's decision), after a merge done by
devloop ([merge modes](#merge-modes)) whose diff touches `devloop/` or a file of
`sensitive_path_lists` (those of the running configuration), devloop records the request in
`.ssdlc/devloop/pin-pending.json`. The next tick, before planning, runs `pin origin/main` (the
`base_branch` of `origin`). If the pin succeeds, `run` and `tick` start again from the new copy
with the same arguments: configuration, policy and dispatcher load again, and the lock stays
held across the restart, so no other dispatcher can start in between. If the pin fails, the tick
stops with `auto-pin of origin/main failed, the pinned copy is unchanged: <reason>`, `current`
stays as it was, and the request stays: the next tick tries again (`run` counts it as a
[failed tick](#continuous-loop), with its backoff; `tick` exits with it). With `auto = false` a merge requests nothing, and a request left
from before is ignored.

devloop imports only the Python standard library and itself, and a test enforces this
(`tests/test_devloop_boundary.py`). [ADR 0001](../adr/0001-prodotto-unico-fasi-su-tracker.md) keeps
devloop and ssdlc in one repository and allows reuse of ssdlc code only through a declared `core`
package, added to that test's allowlist; there is no such package yet.

## Limits

- **Merge off by default.** With `[merge].mode = "off"` a human merges every PR. The other
  modes merge only under the [merge directives](#merge-modes), never a PR touching an
  operator-only path or at a level whose override is `"off"`.
- **Identities need apps.** An agent without a GitHub App publishes through the operator's `gh`
  login, and then cannot submit a native review of a PR opened with that same login.
- **Trust comes from labels and identities.** Only issues whose `agent:ready` label was added by
  a user in `trusted_users` start (or by the supervisor's app, on issues of trusted users and of
  devloop's apps, see [Labels from the supervisor](#labels-from-the-supervisor)), only comments by those users reach the prompts, and markers are
  read only from the identity that may write them (see GitHub identities). Labelling approves the issue's title and body as they are: if someone outside
  `trusted_users` changes them later (or in the same second, since GitHub timestamps cannot
  order the two), implementation, review and fixes stop at
  `agent:needs-human` until a trusted user adds `agent:ready` again. Issue text is still model
  input: do not label untrusted content.
- **Failures escalate immediately.** A timeout or a transient error of an agent, `gh` or `git`
  goes to `agent:needs-human` rather than being retried. The only exception is an unavailable
  model of an agent with [fallback models](reference.md#fallback-models), which runs the turn again on the
  next one.
- **Single host.** One dispatcher runs at a time. With `max_parallel_actions = 1` (the
  default) a tick runs its actions one at a time; above 1, actions on different issues and PRs
  run in threads of that one process ([Parallel actions](reference.md#parallel-actions)), and in
  `devloop run` without waiting for the end of the tick ([Continuous mode](#continuous-mode)).
- **`devloop watch` compares states.** Each round compares what it reads with the latest
  successful round, so a change undone between two reads (a label added and removed, an issue
  opened and closed, a CI that fails and passes again on the same head, an `agent:ready` removed
  and added again by someone else on an external issue) is not reported.
  Comments and reviews have persistent ids: each one still there at the next read is reported.
  The state is saved before the lines are delivered, so an interruption in between (the watcher
  killed, an `--exec` command that fails) loses those lines rather than repeating them.
- **`gh` compatibility.** Checks, labels, comments and PR creation go through the REST API
  (`gh api`), so devloop does not depend on newer porcelain flags such as `gh pr checks --json`.
  The porcelain reads (`gh issue list/view`, `gh pr list/view`) ask only for long-standing
  `--json` fields, all known to `gh` 2.46; an issue's state and close reason are read with REST,
  since `gh issue view --json stateReason` is newer than that (#211).
  For commit statuses the aggregate state counts, including contexts beyond the first page.
  Of the check runs with the same name and app (a re-run of the workflows starts a new suite,
  and the old run stays on the commit) only the most recent counts: the greatest `started_at`
  and, on a tie, the greatest id, as GitHub does in a PR's rollup. A superseded run still
  `in_progress` does not keep the CI pending, an old failure does not fail it, and the failed
  run ids (the logs sent back to the implementer) come only from the latest runs (#221).
