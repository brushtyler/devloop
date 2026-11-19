# devloop: protocol

Markers and records devloop writes and reads, and the mechanics behind the steps of the
operator's [guide](guide.md). Configuration keys are in the [reference](reference.md); the
[index](../devloop.md) lists the three pages.

## Conflicts with `main`

With more than one PR open, merging one can put another in conflict with `main`. Before planning
a review or a promotion, the dispatcher reads each PR's merge state from GitHub (`mergeable` and
`mergeStateStatus`, in the same `gh pr list` call as the rest of the snapshot):

- **conflict** (`mergeable` CONFLICTING, or `DIRTY`): the PR is neither reviewed nor kept ready;
  the tick plans a `sync`. A verdict that asks for changes and is already recorded is applied
  first, and a PR in `agent:changes` goes to its fix, which finds the conflicts and runs the sync
  before it (see [Fixes start from the latest `main`](#fixes-start-from-the-latest-main));
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
round reviews the new head as usual, also while its CI runs; that round counts against `max_review_rounds`,
except after an approval (see [Resync after approval](#resync-after-approval)). The sync
comment says so (#289): after conflicts the implementer resolved, it says that the next review
(or the review after the fix the sync started) **counts**, which counted round it will be out of
`max_review_rounds` and how many remain after it, or, with none left, that the next review
needs an operator's decision. After a merge without conflicts of an approved head it says that
the review of the new head does not count; otherwise the review counts as it would have without
the sync, and the comment shows the round in the same way. A
sync that fails (the implementer is blocked, tests still fail, markers are left, the merge was
concluded or aborted by the agent, or the agent run failed) goes to `agent:needs-operator` with a
comment that lists the files in conflict and their commits.

### Fixes start from the latest `main`

CI runs on the PR's merge commit with `main` (`refs/pull/N/merge`), not on the branch: when
`main` advanced without conflicts, CI can fail for code that exists only on `main`, which a fix
on the branch would not see (#250). So before every fix (the answer to a review round or to a
CI round, and a [`/devloop fix`](guide.md#fix-requested-by-the-operator-or-the-supervisor)) the
dispatcher checks, in the fix worktree, whether the branch contains `origin/main`:

- **it does**: the fix runs as before;
- **it does not, and the merge has no conflicts**: the dispatcher commits the merge with the
  implementer's identity, as for a sync, checks that it does not differ from `main` under
  `protected_paths`, and starts the agent on top of it. The push carries the merge commit and the
  fix, also when the agent changed nothing. The ledger records the `sync` event with the merged
  base and the old and new heads, in the [fix register](#fix-register) (#355), and the fix
  comment says that the merge was done;
- **it does not, and the merge has conflicts**: the fix does not start. The usual sync runs
  instead (the implementer resolves the conflicts, tests, push), but the PR keeps its label
  (`agent:changes`, or `agent:review` for a `/devloop fix`), and the fix runs at the next tick on
  the synced head. The `sync` marker records the head it synced (`from`), so that the fix
  recognizes the synced head as its own; a sync that fails escalates as usual, and a
  supervisor's `/devloop retry` resumes the PR from the fix's label, so that the fix still runs.

The fix prompt tells the agent that the branch already contains the latest `origin/main`, so
that it does not try to fetch, merge or rebase it. The merge of the base writes no review
record, so, like a sync, it does not open nor consume a round: the review that follows the fix is
the next round, as for any fix.

### Resync after approval

A PR approved and then synced, for example because GitHub reported the mergeability as unknown
and another PR was merged before devloop could merge it, needs a new review of the new head
(#235). That review does **not** count against `max_review_rounds` when all of these hold:

- the latest round is complete and approved a head, the same for every reviewer;
- the new head differs from it only by merges of `origin/main`: walking back from the new head,
  each commit up to the approved one (at most 10) has two parents, the second a commit of
  `main`, and exactly the tree `git merge-tree --write-tree` writes, without conflicts, for those
  two parents. A merge whose conflicts the implementer resolved, a fix, or any other commit
  changes the PR's content, and the review counts as before;
- the review is not a `/devloop rereview`, and CI did not fail on the new head (a CI round
  counts).

The dispatcher checks this when it reads the PR; when git cannot tell (a missing commit), the
review counts. The review records `resync`, with the reason `resync_after_approval`, the
approved round and its head (`from`), and its comment says it does not count. Every later sync
after a new approval is free in the same way. The limit stays the same for the implementer's
changes: a request for changes in that round needs a fix, whose review is a round that counts,
so with no round left it goes to `agent:needs-operator` at once.

The same holds when the head did not move at all (#252). Before promoting or merging, the
dispatcher recomputes the receipt; when `main` advanced touching a file of the PR (or the
check could not be verified, or the head no longer merges into it cleanly), it revokes the
approval with an "Approvazione non valida" comment, a `stale` event of the `devloop:policy:v1`
marker that lists the base checks (`bases`), and the PR goes back to review. The new review of
that **same head** does not count against `max_review_rounds` when:

- the latest round is complete and approved that head, the same for every reviewer;
- the latest policy record after it is a `stale` event on that head written by the dispatcher's
  identity (a marker anyone else writes is ignored), with at least one base check that is not
  `disjoint`. A revocation for anything else (level, reviewers, policy version, a missing
  approval) opens a round that counts;
- the review is not a `/devloop rereview`, and CI did not fail on the head.

The review records `resync` with the reason `base_changed`, the approved round and its head
(`from`), and its comment says it does not count; the revocation comment says so too. A new
commit of the implementer in the meantime changes the head, and its review counts as before.

## Failed agent turns

The escalation of a failed agent turn names its local log, `<time>-<label>-<agent>.log` in
`<state_dir>/logs`. The log exists for every turn, including one that timed out, was stopped as
stalled or could not start: it holds what the agent wrote on stdout and stderr until then. A
descendant that left the agent's process group and still holds its output open does not lose
what was read before it. For a timeout the comment also quotes the last lines of that output, at
most 10, each cut to 300 characters. Terminal escape sequences (colours, titles), raw or written
as `\u001b` in JSON events, are removed first. Private keys, tokens, the value assigned to a name
that looks secret (`DB_PASSWORD=…`, `"token": "…"`, whatever its length; a quoted value up to
its closing quote, spaces and escaped quotes included, also inside the JSON events of a CLI) and
the values of the agent's variables whose name looks secret (`KEY`, `TOKEN`, `SECRET`,
`PASSWORD`, `CREDENTIAL`) become `[REDACTED]`: such a value of 8 characters or more wherever it
appears, a shorter one, however short, as a whole word, even when that blanks a common word
(`1` in `ran 1 test`). Paths in the worktree become relative and any other absolute path
keeps only its last segment (`…/name`). The full output stays in the local log. A CLI that writes
its output only at the end, such as `claude --output-format json`, may leave nothing to quote. The
turn's [ledger record](#evidence-and-telemetry) keeps its duration and, when the CLI exposes it,
the last command it ran or started.

An implementation or a fix that times out (`agent_timeout_seconds`) may leave uncommitted work in
its worktree (#374). devloop never commits it. It saves it as evidence,
`timeout-<label>.diff` in `<state_dir>/logs` (`<label>` is the step's, such as
`issue-7-implement` or `pr-9-fix-r2`): a patch against the commit the turn started from, with
new files that git does not ignore, deleted files and binary files. The patch is handled as bytes
from the save to `git apply`, so line ends (CRLF) and text that is not UTF-8 come back unchanged;
only the copy in a prompt is decoded, with replacement characters. The escalation names that
file and lists its files with the lines added and removed. The retry of the same step (same
label) recreates the worktree, then, before the agent starts, the dispatcher applies the patch
with `git apply`, all or nothing, without staging it. If it does not apply, for example because
the base changed the same lines, the worktree stays as created, and the prompt carries the patch
as text (at most 100,000 characters) with its files. Either way the prompt says that the
previous turn timed out, that its changes must be examined before going on, and to run the
targeted tests before any long suite. The patch is taken once: the file is renamed
`timeout-<label>.diff.used-<time>` and stays as evidence. The retention of the logs (`[gc] logs`,
off by default) never removes it while its issue or PR is in `agent:needs-operator` or
`agent:needs-supervisor` ([Cleanup of the state directory](guide.md#cleanup-of-the-state-directory)).
A turn that fails otherwise, that was
stopped as stalled, or a sync (whose worktree holds a merge in progress) keeps nothing, and a
timeout without uncommitted work escalates as before.

An agent CLI that could not start because its executable is missing or not executable
(`FileNotFoundError`, `PermissionError`), as for the few seconds in which a CLI replaces itself
while it updates, is started once more after 30 seconds, in the same turn (#218). Only if it
fails again is the turn a failure: `<agent> could not start (again, after one retry)`, an
escalation of class `tool_failure` whatever the step, a [sync](#conflicts-with-main) with files
in conflict on sensitive paths included. Any other start error is not retried, and is a
`tool_failure` too. To keep turns
from depending on an update in progress, see the recommendation under
[Installation as a dedicated user](guide.md#installation-as-a-dedicated-user).

## Rounds

A round is one evaluation of a head: a CI failure, or the reviews of every reviewer the review
level requires (one at level 1, two at level 2), whatever the number of reviewers. The round
limit (`max_review_rounds`) is checked before every new round, whether a review or a CI failure,
so a PR whose head keeps moving cannot be reviewed indefinitely. Replaying a verdict that is
already recorded is not a new round, and neither is a sync with `main` nor the second review of
a round already open. A round that `main` advancing interrupted before it was complete does not
count either (see [When `main` advances](reference.md#when-main-advances-during-a-round)): the round that
replaces it counts once. Nor does the review of an approved head that only a sync without
conflicts moved, nor the new review of an approved head whose approval the base revoked (see
[Resync after approval](#resync-after-approval)). Round numbers stay progressive, because they also name the findings
(`round/reviewer/ID`), so the round shown as the limit (`round 4 of at most 4`, `round 4/4`) is
`max_review_rounds` plus the rounds that did not count.

**One head, one round** (#352, #359): a CI failure on a head that already has a review round,
not yet answered by a fix, does not open a round of its own when that round asked for changes or
approved; it joins that round as a `ci` finding (see [Approval and CI](#approval-and-ci)). A
round where a reviewer asked for the operator keeps its escalation. A CI failure before any
review of the head is a round of its own.

A `round_limit` escalation lists the rounds that count, each with its reason (the first review,
a CI failure, a review after a fix, a review after a sync whose conflicts the implementer
resolved, a new review of the same head or of a new one; a round the CI failure of its head
joined adds `con la CI fallita dello stesso head`, and `che ne ha fatto decadere l'approvazione`
when the round had approved), then the rounds that do not count with theirs, and recalls that the operator can grant more rounds by raising `max_review_rounds` (#289).
`devloop status` shows the counted rounds of each PR on the limit, as `rounds=<counted>/<max>`,
followed by `(+<n> not counted)` when some rounds did not count. It reads them from the thread
also for a PR stopped in `agent:needs-operator` (as after `round_limit`) or
`agent:needs-supervisor`, whose thread the tick does not read.

### Approval and CI

A review round starts while the CI of its head is pending (#271), or while a head just pushed has
no CI yet within the [window of a new head](reference.md#ci-of-a-new-head): the review record
then carries `ci: pending`, and no full suite runs locally in place of the CI. The CI of the
same head decides what the approval is worth:

- **CI passed.** The recorded approval is applied without a new review, as a replay: no new
  round, and the PR goes to `agent:ready-to-merge` if the other conditions of the promotion hold.
- **CI failed.** The approval does not count for the merge: the PR is never promoted and never
  merged on it (or its failed jobs are re-run once first, see
  [CI failures not pertinent to the PR](reference.md#ci-failures-not-pertinent-to-the-pr)).
  **One head, one round** (#359): the failure does not open a round of its own. The dispatcher
  adds a `ci` record to the round that approved, with the failed checks as its `ci` finding;
  the round's verdict becomes `request_changes`, the approval decays, and the PR goes to
  `agent:changes` for the fix, without a new round. The round still counts once against
  `max_review_rounds`. The trace stays: the `ci` record's note says that the CI failure voided
  the approval of its round, the `ci_round` event in the round's register carries
  `voided_approval: true`, and the round's reason in a `round_limit` escalation says so. The
  same holds when the CI of an approved head fails later, with the PR already in
  `agent:ready-to-merge`: the promotion's receipt is superseded, as for any round that does not
  approve.
- **No CI after the window.** With `local_tests` other than `full` the full suite runs locally
  before the promotion; if it fails, the PR goes to `agent:needs-operator`.

A request for changes waits for the CI of its head in `agent:changes`, unless the head conflicts
with the base (#392): GitHub runs no CI on it, so the fix starts at once, with the review's
findings and no CI summary of that head, and the CI runs on the head the fix produces. Such a
head is never escalated as `ci_missing`.

A round that asks for changes while the CI is pending, or within the window, sends the PR to
`agent:changes`, and the fix waits for the CI of the same head. If that CI fails (after the
re-run of its failed jobs, when there is one), **one head, one round** (#352): the failure does
not open a round of its own. The dispatcher adds a `ci` record to the round that asked for
changes, with the failed checks as its `ci` finding; the round's register gets the `ci_round`
event, the round still counts once against `max_review_rounds`, and the one fix that follows
answers the reviewer's findings and the CI failure together. If that CI passes, or the window
ends without CI, the fix starts with the reviewer's findings; checks the base branch requires
and that never appeared are not escalated then: the fix pushes a new head, which gets its own
CI.

The same holds for a round still waiting for other reviewers when one of them already asked for
changes, or when every reviewer so far approved (#359): the failure joins it, and the round
waits for the others as before. A round where a reviewer asked for the operator, and none for
changes, does not take the failure: its `needs_operator` verdict keeps precedence and the PR
waits for the operator. Nor does an approval already void, without a receipt or with a receipt
found stale after it (see [Resync after approval](#resync-after-approval)): the failure is a CI
round of its own, as before.

A CI already failed before the review is a CI round, as before: no review runs on that head.

## PR marker

A devloop PR is recognised by its branch, `agent/issue-<n>` (or, with `attempt_identity`, the
branch of a later [attempt](#attempt-identity), `agent/issue-<n>-attempt-<k>`), opened by a
trusted user. Its body
ends with a hidden marker that records the issue, implementer and reviewer: text may be edited
above it, but the marker must stay the last line. With `attempt_identity` on (#384) it also
records the [attempt](#attempt-identity) of the PR, which the journal must confirm.

## Readiness check records

The outcome is a comment on the issue, written with the checker's identity and header, whose
marker records the outcome and a hash of the title, the body and the trusted users' comments.
The hash leaves out devloop's own records and comments whose first line is a `/devloop`
command. A record is recognised by its provenance and its metadata together: it starts with the
attribution header, it is written by an identity allowed to write devloop records (an agent's or
the dispatcher's app, or a trusted user for an actor without an app), and it ends with a valid
devloop marker. Escalations, agents that stop (`Lavoro bloccato`) and the checks themselves
carry one. Any other trusted comment counts, even one that starts with the header: without
GitHub Apps devloop and the operator share a login, and the header alone would hide a
requirement the check never saw.

Since #270 the marker also records `base`, the commit of the base branch the check ran on, and
each gap its `repository_dependent` flag. A failed outcome with at least one such gap holds only
while the base branch stays at `base`; one whose gaps all concern the text holds for the hash
alone. A failed outcome also stops holding when the latest `labeled` event of `agent:ready`
comes after the check's comment (by GitHub's `created_at`, a later second) and one of the
issue's admitters wrote it. Records written before #270 lack both fields and are read as
before: their failure holds for the hash, the dependency is never inferred from the
descriptions, and the label added again repeats them too. A failure reused to block the issue
again keeps the `base` of its record.

The dispatcher removes only a block that belongs to the check. The check owns
`agent:needs-operator` only if it added the label when the label was absent: its record keeps the
number of earlier `labeled` events of the label, and the dispatcher must have written the only
one since. The check loses the block, which then stays until a person removes it, if the label
was removed and applied again, if a comment starting with the attribution header, written by an
identity allowed to write devloop records, came after the check (another escalation, or
anything that may be one, marker or not; the dispatcher's [ledger records](#evidence-and-telemetry),
such as the turn of a recheck, and its [supervision traces](#supervision-trace), such as the
answer to a command that does not apply, do not count), or if the attribution is uncertain. A comment from
anyone else, header or not, never touches the attribution. A label already present when the
check failed is never the check's.

## Conclave records

The [conclave on a block](guide.md#conclave-on-a-block) (#141) works on two records.

**The block.** The `notice` of an agent that stopped (`event: blocked`) records, since #141, the
agent's key (`agent`), its reason (`reason`), the step (`step`: `implement`, `fix` or `sync`)
and the commit the step started from (`head`: the base commit of the branch for an
implementation, the PR's head for a fix or a sync). A notice without them, written before #141,
convenes no conclave. The durable reference of a block is `{kind: "blocked", comment, agent,
step}`, `comment` being the id of the notice's comment, or, for a review round whose verdict is
`needs_operator`, `{kind: "review", round, reviewer, head}` of the first `needs_operator` record
of the round. With `conclave` configured that record suspends the round: it is the block even
while the receipt of the round still waits for other reviewers.

**The conclave.** One dispatcher comment per block, edited as the conclave goes on, ending with a
`conclave` marker:

| Field | Content |
|---|---|
| `block` | the durable reference of the block, as above |
| `step` | `implement`, `fix` or `review` |
| `issue`, `issue_hash` | the issue and the hash of its request (title, body and trusted comments, as for the [readiness check](#readiness-check-records)) when the conclave opened |
| `commit`, `head` | the commit of the block, which every call checks out; on a PR its head when the conclave opened (null on an issue) |
| `diff_hash` | SHA-256 of the uncommitted changes of the agent that stopped, attached to the prompts: the tracked files as `git diff`, the new untracked files with their content (split at LF only, every other separator kept as it is) and the SHA-256 of their bytes |
| `proposal` | the proposer (`agent`), `state` (`pending`, `started`, `done`, `missing`), `missing` (the reason, when missing), the `resolution` and its `hash`, the reserved fields (`reserved`, `reserved_reason`, `operator_question`, `options`) and `profile`, the profile the contribution was given with (identity, kind, model, effort and arguments, a fallback model included; null until it is done) |
| `votes` | the two voters, each with `state`, `missing`, `vote` (`APPROVA`, `BLOCCA`), `reason`, the reserved fields, `profile` and `proposal_hash`, the hash of the text it voted on |
| `hash` | SHA-256 of the proposal's text: the content evaluated |
| `stale` | why the frozen context stopped holding, `""` otherwise |
| `outcome` | `UNANIMOUS`, `BLOCKED` or `RESERVED`; `""` while the conclave is open |
| `resumed` | since #142, absent or null until a `UNANIMOUS` decision restarts the work, then `{evidence}`: the file of the dispatcher's logs with the uncommitted changes of the agent that stopped (`{file, bytes, sha256}`), or null when there were none |

A record is read back only from the dispatcher's identity and with a valid structure, and it must
hold together: `hash` is the hash of the recorded text, every vote was given on that hash, every
contribution done records its `profile`, the profiles distinct two by two by identity and by
`(kind, model, effort)`, the recorded outcome is the one its contributions give, and only a
`UNANIMOUS` one is `resumed`. A record that does not hold is logged and
left to the operator: the block gets no other conclave. Each call is also a ledger `turn` of kind
`conclave` ([Evidence and telemetry](#evidence-and-telemetry)).

**After the decision (#142).** The latest decision of an issue and its PR is the `UNANIMOUS`
record, among those that hold, with the greatest comment id (GitHub's comment ids grow with
time) on the thread of its block: `step` `implement` on the issue, `fix` or `review` on the PR,
with `issue` the issue's number. Its resolution reaches a prompt only while `issue_hash` is the
hash of the issue's request read now, `head`, when not null, is the head the step works on, and
no trusted user wrote `/devloop veto` after it on either thread. A step that ran with it records
the comment id of the record in `conclave`: in the `pr` marker of the implementation, in the
`fix` and `review` markers, and in the `notice` of an agent that stopped, which then convenes no
other conclave. The escalation of a veto (`class: conclave_veto`) records in its `notice` the
comment of the decision (`veto_of`) and of the veto (`veto`): it spends that veto, and a PR is
suspended once per decision. A `review` block whose record is `resumed`, on the PR's head and on
its latest round, is not replayed: the next review opens a new round, as after `/devloop
rereview`. Any `resumed` record of the dispatcher on the issue or on its PR, whether its
resolution still holds or not, makes the merge of that PR human: the snapshot plans it in mode
`off`, the `operator_merge` notice says why, and `_do_merge` checks it again before merging.

## Receipts

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
profiles (and the head) all match. A different commit of base is admitted only when the commits
of `main` in between left the PR's files alone (see
[When `main` advances](reference.md#when-main-advances-during-a-round)). When the base branch advanced
onto the PR's files, the policy or the
configuration of the pool or of a required reviewer (apps, models, efforts, `review_args`)
changed, or the level rose, the PR stays in
`agent:review`, a dispatcher comment says what changed, and the next tick runs a new review,
even of the same head. That review is a
round like any other: it may start while the CI runs, and its approval waits for the CI of the
same head (see [Approval and CI](#approval-and-ci)). Approvals recorded before receipts existed have none:
they are not promoted and need a new review; their rounds and open findings stay as they are. A
PR already in `agent:ready-to-merge` keeps its label until something sends it back to review.

## Merge directives and record

How devloop merges in the `"strict"` and `"auto"` [merge modes](guide.md#merge-modes):

- **Operator-only paths.** A PR whose full diff (the paths the review level reads: deletions and
  both sides of a rename included) touches `operator_only_paths` behaves as `"off"`, whatever the
  mode. The default is `docs/adr/`, `AGENTS.md` and `CLAUDE.md`; an entry is a directory prefix
  or a file relative to the root, as for `sensitive_paths`.
- **Merge directives.** The same in `"strict"` and `"auto"`. A tick plans a merge only for a
  ready PR that meets them in its snapshot, and the merge reads them all again just before it
  runs, reviews, finding closures and policy records included (read again from the comments,
  with the same identity checks as the snapshot):
  - the PR is open and in `agent:ready-to-merge`, without `agent:needs-operator`,
    `agent:needs-supervisor` or `agent:merge-hold`, and no hold or release command waits to be
    applied;
  - the head is the approved commit (at level 0, the promoted one), unchanged since the
    snapshot;
  - GitHub reports `mergeable` `MERGEABLE` and `mergeStateStatus` `CLEAN`;
  - CI is `pass`: a head without CI is not merged;
  - no blocking finding is open, and the [acceptance map](#acceptance-criteria-coverage) holds;
  - the conditions of the promotion: the receipt recomputed now matches the approvals of every
    reviewer the level requires (an earlier base is admitted as at promotion, see
    [When `main` advances](reference.md#when-main-advances-during-a-round)); at level 0, the checks of its
    promotion (level still 0, its labels, every expected check `success`, no standing
    `REQUEST_CHANGES`).

  When one is missing devloop does not merge, and the revocation already in place applies: a
  moved head, CI running again or failing, or a level 0 that no longer holds is revoked as on
  any tick of a ready PR; a receipt that no longer matches is recorded as `stale` and the PR goes
  back to `agent:review` for a new review; an open finding sends it to `agent:needs-operator`. A
  PR that is `BLOCKED`, `BEHIND` or `UNSTABLE` stays ready and waits. **No mode, and no agent,
  merges with CI not green, with blocking findings open or without the approvals required.**
- **How.** `gh pr merge --merge --match-head-commit <approved head>`: a merge commit, only if
  the head is still the approved one. It runs with the [merger app](reference.md#the-merger-app) when
  `[apps.merger]` is configured, with a token minted at that moment and limited to
  `contents=write` and `pull_requests=write`; without it, with the local `gh` login devloop runs
  with. No other GitHub App, the dispatcher's included, gets `contents=write`. The branch is
  kept. A merge that GitHub refuses goes to `agent:needs-operator` like any `gh` failure.
- **Slot.** A merge takes a slot of `max_actions_per_tick`. In `devloop tick` it runs alone,
  after the actions planned before it and before those planned after it
  ([Parallel actions](reference.md#parallel-actions)); in `devloop run` it runs in the
  [merge lane](guide.md#the-merge-lane), one merge at a time, alongside the actions on other numbers.
- **Record.** A `merged` marker of the dispatcher records the head, the receipt, the
  level and the mode, and in `merged_by` the login of the merger app when it merged (none with
  the local login). Its text, the section "Mergiata da devloop", says that devloop decided the
  merge and names who ran it: the merger app (its login without `[bot]`) or the local `gh`
  login. Since #338 they go in the comment of the promotion the merge concludes, which the
  dispatcher edits: the latest `promoted` record it signed, read just before the merge, for the
  merged head and with its [receipt](#pr-receipt), if any, still `current`. The section follows
  the text of the promotion and its receipt, with the policy receipt recomputed for the merge
  only when it reads differently from the promotion's, and the `merged` marker follows the
  `policy` and `receipt` ones. A promotion that devloop does not merge (the merge is the
  operator's, or a hold stops it) keeps its comment of promotion and receipt. Without such a
  comment, when the edited one would exceed the limit of GitHub or when the edit fails, the
  record is a comment of its own, as before #338. The [daily summary](#daily-summary) counts a
  `merged` record on the day GitHub reports for the merge of its PR, when known, not on the day
  of its comment.

## Supervision trace

The trace of the commands of the [escalations to the supervisor](guide.md#escalations-to-the-supervisor)
and of the [escalations to the operator](guide.md#commands-on-escalations-to-the-operator):

**Trace.** Every command applied leaves a dispatcher comment with a `supervision` marker: the
command, its author, its reason, the id of the comment that carried it, the class and the outcome
(`applied`, `accepted`, `decided`, `escalated`, `refused` or `limit`), with the key and the
attempt for a retry, the defaults for an acceptance and, for a decision, its text (in `reason`)
and the gaps it answers. The record is written before the labels change, so a
lost label change never runs a retry twice. A command that does not apply (#321) gets the same
record, `refused`, with the reason and how to resume in the comment: it spends the command, which
is answered once. The applied retries counted against the limit are those of both records, the
supervisor's and the operator's.

**Escalation.** The `notice` marker of an escalation to the operator records, as one to the
supervisor does, the step that failed, the key of its input and, for a PR, the label a retry
resumes from, when the step is one a retry runs again (#321). A supervisor's escalation handed to
the operator (`supervisor_escalated`, `supervisor_refused`) keeps them only for `tool_failure` and
`sync_failed`: the key of a `preflight_default` or `merged_issue` escalation only routes it.

## Dependency cycles

When ready issues are on a cycle of [native dependencies](guide.md#dependencies-between-issues)
(#277), the `dependency_cycle` action reports it on each issue of the queue on the cycle: a
dispatcher comment ("Ciclo di dipendenze") ending with a `cycle` marker that lists the issues of
the cycle (`#N`, sorted). The action reads the dependencies again first and writes nothing if
the cycle is gone. A `cycle` record counts only from the dispatcher's identity, and the same
cycle is reported once on each issue; another cycle on the same issue is reported again. The
record adds no label and blocks nothing by itself: the open blocks do.

## Daily summary

The [daily summary](guide.md#daily-summary) (#256) reads only records bound to identities: the
`ledger`, `merged` and `notice` (escalation) records the dispatcher signed, the `review` records
of a configured reviewer written with its identity (a CI round with the dispatcher's), and the
`pr` marker of a PR opened by the implementer it names. It reads the comments of the repository,
not the native reviews: a round whose reviews are native reviews is counted from its
[round register](#round-register), once a review turn of it succeeded or its CI was recorded. The
records of a [fix register](#fix-register) are read one by one, and open no round. On `[report] issue` it writes two kinds of
`report` record, both with the day (`YYYY-MM-DD`, UTC):

- `today`, at the end of the issue body: the day the body shows. It counts only when the
  dispatcher wrote the body last (its last editor, or its author if never edited): a body
  edited by anyone else shows no day, and no closed day is archived from it;
- `archive`, at the end of a dispatcher comment: the summary of a closed day. Only the first
  archive of each day that the dispatcher wrote counts; another identity's comment with the same
  marker is ignored. A publication edits the archive of a day instead of writing another, so a
  second run, or a run repeated after a failure, leaves one comment per day. `open` is `true`
  when a turn of the day was still open at the time: every publication then reconciles that
  archive with the records, until it is written with `open` `false`.

## Structured review

The reviewer returns JSON validated against [`devloop/schemas/review.json`](../../devloop/schemas/review.json):
`verdict` (`approve`, `request_changes`, `needs_operator`), blocking and non-blocking findings with
file, line and a suggested fix, and the status of every earlier blocking finding (`resolved`,
`unresolved`, `withdrawn`). devloop then enforces consistency:

- `approve` with a blocking finding, or with one of the reviewer's own earlier findings
  unresolved, becomes `request_changes` (another owner's open finding does not change it: the
  dispatcher checks it before promoting);
- `request_changes` with nothing to fix becomes `needs_operator`;
- an earlier finding the reviewer does not assess stays open.

**Trust policy** (#399). The review prompt asks for `needs_operator` when the change touches the
trust policy, at every review level, with the parts touched in `human_reason`, and to assess it
again at every round on the whole diff of the PR against its base, not only on the latest fix;
the advisory review prompt asks instead for the opinion `dubbi` (`contrario` when a check is
weakened) and a first residual risk beginning with "Policy di fiducia toccata:". Both carry the
same closed list of five points and the same examples, described in
[Changes to the trust policy](guide.md#changes-to-the-trust-policy). The rule lives only in the
prompts: the schemas `review.json` and `advisory.json` do not change, and devloop does not check
in code whether a verdict should have been `needs_operator`. Its residual risks, accepted by the
operator, are listed in the guide: the attention of the reviewers at every round, a disguised
change, the local overlay, with a conclave the reserved perimeter its members recognise, and a
PR that changes the rule, judged with the prompts of the running installation.

Findings get stable ids (`R<round>-<n>`). The fix step answers each one (`fixed`, `disputed`,
`not_applicable`), and the next round sees those answers. The review is published in readable
form, with the full record in a hidden base64 marker for the next tick: as the body of a native
GitHub review for an agent with an app of its own, as a comment for any other agent (see
[Where a review is published](#where-a-review-is-published)).

### Where a review is published

Since #325 each review is **one element** of the PR's timeline:

- **An agent with its own GitHub App** submits one native review on the reviewed commit, with
  its app: `APPROVE`, `REQUEST_CHANGES` or `COMMENT` (for `needs_operator`) mirrors the verdict
  for GitHub and branch protection, and its body is the whole review, header, readable text and
  `review` marker. That native review is the record devloop reads, and no review comment is
  written. Its submission is required: a failure is tried again, up to three attempts in all,
  after a pause of 5 seconds and then 10. Before each new attempt the dispatcher reads the PR's
  reviews again, and a review with the same body already published by that app (an answer lost
  after GitHub accepted it) ends the retries. After the third failure the step escalates like
  any failed write (`failure`); the verdicts of other reviewers already published stay recorded.
  With the [publication outbox](#publication-outbox) an uncertain outcome the outbox could not
  settle ends the retries at once, and the step escalates the same way: only the outbox may send
  that review again (#385).
- **An agent without an app** publishes with the operator's `gh` login, which may also be the
  PR's author: it keeps the review comment, as before, and submits no native review.

devloop reads the review records from the PR's conversation and, among its comments in the order
of their times, from the native reviews whose body ends with a valid `review` marker (a pending
review, not published, is left out; a native review dismissed later stays a record). A native
review placed at the same second as a comment comes before it. A native review counts only as the
record of an agent with an app of its own, written with that app, and never by the PR's
implementer (see [Records bound to identities](#records-bound-to-identities)). Any other native
review stays out of the conversation, as before.

**Advisory review** (#369). The [advisory review](guide.md#advisory-review-for-the-operator) of a
PR in `agent:needs-operator` is always a native review of type `COMMENT`, with the identity of
the agent of `advisory_reviewer` (its app, or the ambient login without one), titled "Review
consultiva: non conta per il quorum". The agent answers with JSON validated against
[`devloop/schemas/advisory.json`](../../devloop/schemas/advisory.json) (prompt
`devloop/prompts/advisory.md`). Its body ends with an `advisory` marker, never a `review`
one: `head`, `reviewer`, `outcome` (`done`) and `opinion` (`ok`, `dubbi`, `contrario`). It is
never part of the PR's conversation as the review records see it, so it is no round, no verdict
and no finding. The dispatcher's summary of it ("Review consultiva per l'operatore") ends with
an `advisory` marker too, with the same fields, or `outcome: failed` and `opinion: null` when
the turn failed: only the dispatcher's identity writes it, and it carries no text of the agent.
A head has its advisory review once it has that summary, written last. A published native
review with an `advisory` marker of that head, written by the identity of the configured agent,
stands for its turn: a head that has it without the summary gets the summary, and the receipt,
without a new turn.

**Compatibility.** The review comments published before #325 stay records, read with the same
rules as before: a PR opened during the change, with rounds of both forms, keeps every round and
every finding, and counts them as if all its rounds were comments.

**Length.** GitHub accepts at most 65 536 characters in the body of a comment or a review. A
longer review keeps its header and its marker whole, with every finding, and only its readable
text is cut, followed by a notice (`Testo troncato`). When header, notice and marker alone exceed
the limit, nothing is published and the step escalates (`failure`): no finding is dropped and no
other comment is written in its place. The same rule applies to the review comment of an agent
without an app.

### Acceptance criteria coverage

The acceptance criteria of an issue are the items of a checkbox list (`- [ ]`, `- [x]`) or of a
numbered list (`1.`, `1)`) in the first section whose Markdown heading is "Criteri di
accettazione" or "Acceptance criteria" (any level, case-insensitive, an ending colon allowed).
The section ends at the next heading of the same or a higher level. Indented lines continue the
item above them; other bullets and text are not criteria. Nothing inside a fenced code block
counts, neither items nor headings: as in CommonMark, a block opened by a fence of backticks or
tildes ends only at a line with the same character repeated at least as many times and nothing
else, so a `` ``` `` example inside a ` ```` ` block stays in the block. The dispatcher reads
the issue text when it starts each turn, and gives the criteria it found to the implementer, the
fixer and the reviewer.

Implementer and fixer return `acceptance_map` in their structured output
([`implement.json`](../../devloop/schemas/implement.json), [`fix.json`](../../devloop/schemas/fix.json)):
one entry per criterion, in the order of the issue, with `criterion`, `tests` (`file` and `name`
of each test that verifies it), `status` (`covered` or `not_covered`) and `reason`. The map of the
implementer is recorded in the PR marker and shown in the PR description; each fix records the
full map again in its `fix` marker and comment. The latest map holds: that of the latest fix
that carries one, otherwise that of the PR. A sync carries no map, so the previous one still
holds after it.

The reviewer receives the criteria and the latest map, and must verify them: a criterion without
real coverage, or with tests that do not actually verify it, is a blocking finding. A
`not_covered` criterion with a reason is left to the reviewer's judgement. Independently of the
verdict the reviewer proposes, `normalize_review()` adds a blocking finding, owned by that
reviewer, for:

- each `not_covered` criterion with an empty `reason`;
- a missing map when the issue has recognisable criteria (a PR opened before #134, for example):
  the next fix provides it;
- a map whose number of entries differs from the number of criteria.

So an `approve` with one of them becomes `request_changes`. An issue without recognisable
criteria expects an empty map (`[]`), declared as such in the PR description, and blocks
nothing; for it a missing map counts as an empty one.

The same check guards an approval already recorded, which a review recorded before #134 gave
without seeing a map. Before replaying an approval of the head from `agent:review`, and on every
tick of a PR in `agent:ready-to-merge` (not at level 0, which has no review), the snapshot reads
the issue text again and applies the rules above to the latest map. If one of them would add a
finding, the approval is neither replayed nor kept: a ready PR goes back to `agent:review` with an
"Approvazione sospesa" comment that lists why, and the PR gets a new review, whose
`normalize_review()` adds the finding. That review is a new round: it counts against
`max_review_rounds` like any other, and at the limit the PR goes to `agent:needs-operator`. An issue
text changed by someone outside `trusted_users` cannot be checked: the approval does not hold, and
the new review escalates as for any untrusted change.

### Findings and who closes them

Every blocking finding has an owner, the reviewer that opened it, and its identity is the triple
`round/reviewer/ID`, for example `2/codex/R2-1`. The key is recorded in the review marker; a
review whose keys name another round or another owner is not accepted, and the PR goes to
`agent:needs-operator` (#110). A CI round is
a review of the reviewer `ci`. A finding stays open until one of these closes it:

| Who | How |
|---|---|
| The reviewer that opened it | `resolved` or `withdrawn` in one of its later reviews |
| The dispatcher, for a `ci` finding | Automatically, when CI passes on the PR's current head (the same commit that failed after a re-run, or a later one), in `agent:review` or still in `agent:changes` before the implementer starts |
| The operator | The `/devloop finding` command below |

Another reviewer's assessment does not close a finding: it is recorded in its review, with a
note, and the finding stays open. When every reviewer of a round approves but a finding of an
owner outside the round (CI, or an earlier reviewer) is still open, the round is not promoted:
the PR goes to `agent:needs-operator` with a comment that lists the open findings (`open_findings`).
The exception is a PR whose open findings are all `ci` findings while the CI of the approved head
is still running, has not appeared yet within the wait of a just pushed head, or has passed while
the review ran (#334): the CI can still close them, so the approval stays recorded and the PR
waits in `agent:review`. When the CI passes, the next tick closes its findings and the approval is applied without a new review; when it
fails, the failure joins the round that approved as a `ci` finding, without a round of its own,
and voids its approval (one head, one round, #359, see [Approval and CI](#approval-and-ci)). A `ci` finding whose head CI has already concluded without
passing goes to the operator. When a reviewer
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
`agent:needs-operator` takes effect once the label is back.

#### Markers written before #110

Review markers written before finding keys existed are read with the rules of their time: the
rounds and the findings still open are kept, and each finding is attributed to the reviewer
recorded in the marker of the round that opened it (`ci` for CI rounds). From the first keyed
review on, only the owner, CI or the operator closes them. These rules cover only the markers
that precede the first keyed one: a later review marker without keys, or with keys on only some
findings, closes nothing, and its own findings stay open, attributed to its reviewer. When the owner cannot be
reconstructed (a review marker without a reviewer, or one that leaves open a finding whose
round is unknown), the PR goes to `agent:needs-operator` with a comment that lists the problem:
the review and the decision on the merge are up to a person.

Markers are control records, while the rest of each comment is text written by a model, and both
are published by the same account. So the dispatcher neutralizes HTML comments in all published
text, and reads a marker only from the block of marker lines that ends a comment, of the expected
kind and with a valid structure. That block is usually the last line alone; the comment of a
promotion carries its records there, one per line (see [PR receipt](#pr-receipt)). A marker
anywhere else is text, and a block with two markers of the same kind holds no record of that
kind. Text from an implementer or a reviewer cannot record a review, a fix or a PR link.

## Attribution

Every text devloop publishes starts with an attribution header. This section is the reference for
its format: [AGENTS.md](../../AGENTS.md) only tells the agents to add no header in devloop turns, and
keeps the Agent, Model and Reasoning Level header for texts an agent publishes outside devloop
with its own identity. The dispatcher composes the header in one place (`markdown.attribution`)
as a single `> [!NOTE]` block (#232). It opens with `Bot`, the name of the identity that
publishes, and `Version`, the version of devloop:

- text the dispatcher writes on its own, such as markers, escalations, CI rounds, merges, ledger
  records and closed findings, comes from a bot without AI: the block holds `Bot` and `Version`
  only;
- text of an AI agent (review, fix, implementation, readiness check, `Lavoro bloccato`) adds, in
  the same block, the agent with its CLI version, the model and the reasoning level.

```markdown
> [!NOTE]
> **Bot:** devloop dispatcher
> **Version:** 0123456789ab
```

```markdown
> [!NOTE]
> **Bot:** devloop codex-agent
> **Version:** 0123456789ab
> **Agent:** Codex (CLI v0.5.1)
> **Model:** GPT-5.5
> **Reasoning Level:** High
```

`Bot` is `devloop dispatcher` for the dispatcher and `devloop <key>-agent` for the agent
configured as `[agents.<key>]` (`devloop claude-agent`, `devloop codex-agent`, `devloop
agy-agent`); the merger, if it ever publishes texts, will be `devloop merger`. `Version` is the first 12 characters of the commit in the `pin.json` of the
[pinned copy](guide.md#run-from-a-pinned-installation) in use. Without a pinned copy, in development or
in the tests, it is the commit of the checkout devloop runs from, if that directory is the top
level of a git checkout, otherwise `non nota`. It is read once per process: a new pin starts a
new process.

The agent's values come from the configured agent: CLI version from its `--version`, model and
reasoning level as passed on the command line. A known version is cached for the life of the
process; a failed `--version` is retried at the next publication, and the header says `CLI non
noto` only for texts published while it keeps failing. When a turn ran on a
[fallback model](reference.md#fallback-models), the header names that model and its reasoning level, not the
configured ones. The agents add no header of their own to the text they return (summary,
findings, notes, PR body): the dispatcher would duplicate it.

Texts published before #232 carry the agent header alone, without `Bot` and `Version`; the
dispatcher's names `devloop dispatcher` as the agent and no model:

```markdown
> [!NOTE]
> **Agent:** devloop dispatcher
> **Model:** nessuno (testo generato dal dispatcher)
> **Reasoning Level:** n/a
```

All forms stay valid: devloop
recognizes its texts by the `> [!NOTE]` that opens every header, by the identity that wrote them
and, for records, by a valid marker, never by the header alone. A text with the `Bot` header from
an identity that may not write devloop records is no record. `operator_comments` leaves out texts
with any of these headers, and the `/devloop` commands are read on the first line after any of
them.

**What the reviewers read** (#389). The review prompt and the
[advisory](guide.md#advisory-review-for-the-operator) one are blind to the author: they name
neither the implementer's key nor its model, and tell the reviewer that it is not the author of
the change, which a separate session wrote, and that a `git log` or a commit trailer naming an
agent or a model does not change that. The texts they pass on, the issue body, the PR
description, the trusted users' comments and the implementer's responses to the findings, lose
every attribution block (a `> [!NOTE]` followed only by `Bot`, `Version`, `Agent`, `Model` and
`Reasoning Level` lines, outside fenced code) and the markers that end them
(`markdown.without_attribution`). Everything else stays, technical text that names an agent or a
model included. The originals on GitHub, the PR's marker, the ledger and the commit trailers do
not change: devloop still reads the implementer from the marker to keep it out of the reviewers.

## Records bound to identities

Records are bound to identities. A PR counts as a devloop PR with its marker only if it was opened
by the app of the implementer the marker names; a review counts only if its native review or its
comment was written by the app of the reviewer named in it, and never by the PR's implementer; a CI round and a finding
closure only from the dispatcher's app; a fix only from the implementer's app. A readiness check
(`preflight`) counts only if written by the app of the configured agent it names, the checker:
the dispatcher sets the labels it implies, never the record. A `notice` counts only from the
dispatcher's app: the escalations and the operator-merge notices are its own; the `notice` of an
agent that stopped (`Lavoro bloccato`) is published with that agent's app and stays out of the
hash of the [readiness check](#readiness-check-records) like any text devloop published. Only
the [conclave](#conclave-records) reads it, and only when written by the app of the agent it
names. A `conclave` record counts only from the dispatcher's app. An `advisory` record (#369)
counts in a native review only from the identity of the agent of `advisory_reviewer`, and in a
comment only from the dispatcher's app. A `story` record (#363) counts only from the dispatcher's
app, and only when the [turn journal](#story-identity) confirms it. `trusted_users`
stays for people: it
decides who can start work with `agent:ready`, and it cannot contain bots. The only app whose
labels count is the supervisor's, on the issues described in
[Labels from the supervisor](guide.md#labels-from-the-supervisor).

For an agent with an app the review record is the body of its native review, which also mirrors
the verdict for GitHub and branch protection; for an agent without an app it is the review
comment (see [Where a review is published](#where-a-review-is-published)). The identity rule is
the same for both: the app of the reviewer named in the record, never the PR's implementer.

## Evidence and telemetry

The dispatcher records the evidence and the telemetry of every step for itself (#133), so that
they no longer have to be pieced together from markers and logs. Each record is a dispatcher
comment ending with a `ledger` marker, signed with the dispatcher's identity like its other
records (only that identity's records count), on the issue for the implementation and the
readiness check, on the PR for review, fix, sync and CI. The turns of a review round and its
`ci_round` event share one comment, the [round register](#round-register); the turns, the
`sync` and the local tests of a fix share another, the [fix register](#fix-register). The
[PR receipt](#pr-receipt) (#135) gathers them at the promotion.

| Record | When | Content |
|---|---|---|
| `request` | The issue goes from `agent:ready` to `agent:working` | SHA-256 of title and body, author and time of the trusted `labeled` event of `agent:ready`, ids of the trusted comments given to the implementer, time of the takeover. It is a snapshot **at the takeover**, not at the time of the label |
| `turn` | Every invocation of an agent CLI, retries, fallback models and failures included: implement, fix, sync with an agent, review, readiness check, conclave call, [advisory review](guide.md#advisory-review-for-the-operator) (`advisory`, on the PR, in a comment of its own) | Kind, agent, GitHub identity (none: the operator's `gh` login), configured model and effort (for a fallback, the fallback model and the model it replaces), real model, start, end, duration, outcome (`success`, `failure`, `unavailable`), the error if any, telemetry, and the last command the turn ran or started when the CLI exposes it (`last_command`: only codex, from the `command_execution` items of `--json`; cleaned like the tail of a timeout, on one line, at most 200 characters; absent otherwise), also for a turn that timed out. With `attempt_identity` on, the id of the [attempt](#attempt-identity) the turn belongs to (`attempt`; absent for a turn of no attempt) |
| `verification` | Every test command the dispatcher runs after a writing turn (none when the [local tests](reference.md#local-tests) select nothing), and the full suite it runs before a review of a head without CI, or before the promotion of an approval given while no CI was known | Command, outcome, duration, the tree hash before and after the test, and the commit (none before a review) |
| `event` | The CI of a head (`ci_round`): a failed CI round, the checks a review round saw whatever its verdict (`approve`, `request_changes` or `needs_operator`), or the passing checks read again just before the PR is promoted to `agent:ready-to-merge` (also at level 0); a sync without an agent, or the merge of `main` before a fix (`sync`) | For the CI, the head, the state (`fail`, `pass`, or `none` without checks), the review round (none at level 0) and each check with its outcome, the id of its GitHub Actions run (none for a commit status or a check outside Actions) and its duration in seconds (`duration_s`: none while it runs and for a commit status, #300). Every CI round also records `tests`, the [summary of the suite](reference.md#summary-of-the-ci-in-the-prompts) that the dispatcher reads for the review and fix prompts (read again from the job logs when a promotion records the round, for instance after a review that saw the CI pending): for each job whose log holds the final lines of `scripts/run-tests.py`, its name with `ran` and `slowest`, and for a job whose log could not be read, its name with `log: unavailable`; absent when no job has either. The same checks read again for the same head and round are not recorded twice, a new run is. A failure that joined a round that had approved its head carries `voided_approval: true` (#359). For a sync, the merged base commit and the old and new heads |

**Telemetry** is what the CLI attests, not authenticated:

| CLI | Source | Fields |
|---|---|---|
| claude | `--output-format json` | `total_cost_usd`, `duration_ms`, `num_turns`, `usage` (input, output, cache read and cache creation tokens); the real model is the list of the keys of `modelUsage`, with no main model chosen |
| codex | `codex exec --json`, which devloop now passes | input, output and cached input tokens, summed over the `turn.completed` events (input already includes the cached input). No cost, duration or model |
| agy | `agy -p … --output-format json`, the top-level keys of the envelope (never those of the structured output `--json-schema` adds) | `duration_seconds` (as `duration_ms`), `num_turns`, and from `usage` the input, output, cache read and thinking tokens (`cache_read_tokens` as `cache_read_input_tokens`, `thinking_tokens` in its own field). Input **excludes** the cached input, which agy counts apart. `total_tokens` is not kept: in the envelopes observed it is input plus output. No cost or model |

agy's `usage` covers the **whole conversation**, not a single request: it is the turn's because
devloop starts a new conversation on every turn (`agy -p`, never `-c`). A turn that continued a
conversation would have to subtract the previous envelope of the same conversation.

`thinking_tokens` is a field of its own, filled only by agy: claude counts its thinking inside
the output tokens, and codex's reasoning tokens are not read. Whether agy's `output_tokens`
already include the thinking is not established: in the envelopes observed `total_tokens` is
input plus output and the output is always larger than the thinking, which fits both readings.
So the thinking tokens are kept as reported, never added to the output nor subtracted from it.

The token counts of different CLIs do not mean the same thing: codex's input includes the cached
input, claude's and agy's do not. Compare them only within the same agent; the totals of the
[PR receipt](#pr-receipt) add up what each turn reports, whatever its agent.

A value that is not attested is `null`, never 0, and the comment shows `non riportato`; the real
model stays `null` next to the configured one when the CLI attests none. An agy envelope that is
missing, not JSON or without `usage` leaves every field `null`, duration and turns included; a
duration that does not fit a finite number of milliseconds is `null`. Telemetry never makes a
turn fail.

**Turn persistence.** A turn's record is written before the CLI starts, as `open`; if that write
fails the turn does not start and the step escalates like any `gh` failure. When the CLI ends the
record is edited with end, duration, outcome and telemetry. An unexpected error of the
dispatcher while the turn runs (not an agent's failure, #381) closes it too, with outcome
`failure` and the exception as its error, before the error goes on. A turn left without that
closing write, because the dispatcher crashed or the edit failed, stays without end, outcome and
telemetry: the next tick logs `incomplete turn on #<n>` and marks the record `incomplete` (only
logs it with `--dry-run`). Where to look does not depend on labels, which change once a step
ends: before writing a turn's record the dispatcher records its intent in the
[turn journal](#turn-journal) of `state_dir`, which keeps the issue or PR until the turn has an
outcome and its record is closed. If the intent cannot be written, the turn does not start
either. The next tick checks every issue or PR the journal still holds, for example an issue
whose PR is already open or a PR escalated to `agent:needs-operator`, and then forgets it. In
case that trace is lost, it also looks at open devloop PRs not in
`agent:needs-operator` (or in it only for an [operator merge](guide.md#ready-prs-whose-merge-is-the-operators)),
issues in `agent:working` and issues waiting for a readiness check. While an issue or PR is
noted there, the [cleanup of the state directory](guide.md#cleanup-of-the-state-directory)
removes neither its worktrees nor its files in the logs.

**Tests and commits.** A passing test is bound to the commit created right after it only if the
tree is the same at three points: before the test, after the test and in the commit. The tree is
what the commit would contain (`git add -A`), computed on a copy of the index. If the test
changed files headed for the commit, or anything else differs, the commit stays `null`; the
attempts that failed, or that no commit followed, have commit `null` too.

**Earlier work.** PRs and issues started before these records have none of them. Missing data
is declared missing (`request` null, no turns), and is never reconstructed from the summaries
written by agents.

### Round register

Since #325 the review turns of a round and its `ci_round` event are not comments of their own:
the dispatcher writes **one comment per round** on the PR, `## Registro devloop: round <n>`,
ending with a `ledger` marker of type `round`. Its `records` hold every review turn of the round,
retries and fallback models included, of every reviewer, and the `ci_round` event of the round
(also of a CI failure round, and the one a promotion records later for that round), each in a
section of its own with its number (`section`) and the same content as a record of its own.
Together with the native reviews, a level-2 round leaves three elements in the PR's timeline:
two reviews and one register.

- **Persistence stays.** The register is created, or the turn's section added to it, before the
  CLI starts, with the turn `open`; if that write fails the turn does not start. When the CLI
  ends, the section is completed. A turn interrupted without that write stays `open` in the
  register, and the next tick marks only its section `incomplete`, as described above.
- **Writes are serialized.** Reviewers running in parallel (`max_parallel_actions` above 1) write
  the same comment: every write of a register (a new section, a closed turn, a turn marked
  incomplete) takes a lock of the dispatcher and reads the register again from GitHub right
  before editing it, so no update of the other reviewer is lost. The dispatcher edits only a
  register it signed; one written by any other identity is ignored, and a new one is created.
- **Reading.** A register is read as its records, one by one: the receipt, the CI already
  recorded, the open turns and the [daily summary](#daily-summary) see each turn and event as
  before. A reviewer of the same round in a later action joins the round's register.

A fix has a register of its own, the [fix register](#fix-register). The other records
(`request`, the `verification` of an implementation, of a sync and of the full suite before a
review, the turns of implement, sync, readiness check and conclave, the `sync` event of a sync
without an agent, the `ci_rerun` events, and the `ci_round` of a level-0 promotion, which has no
round) keep a comment each. The records written before #325, one comment per turn and per CI
round, stay valid and are read as before.

### Fix register

Since #355 a fix (the answer to a review round, or one asked with `/devloop fix`) writes **one
dispatcher comment** on the PR, `## Registro devloop: correzione del round <n>` (`correzione
prima di ogni review` for a fix asked before any review, round 0), ending with a `ledger` marker
of type `fix`. Its `records` hold, each in a section of its own with its number (`section`) and
the same content as a record of its own:

- every `turn` of the fix, retries after failed local tests and fallback models included;
- every `verification` of its local tests, the passing one bound to the commit of the fix with
  the same rule as before;
- the `sync` event of the merge of the base the dispatcher made before the fix (#250), when it
  made one, written once the merge is pushed with the fix.

With the implementer's "Correzioni per il round N" comment, which has another author and stays
apart, a fix leaves two elements in the PR's timeline instead of four. The rules of the
[round register](#round-register) apply: the register is created with the first turn, `open`,
before the CLI starts (if that write fails the turn does not start); every write re-reads it from
GitHub under the same lock; only a register the dispatcher signed is edited; a fix interrupted
before the closing write leaves its turn `open` in the register, and the next tick marks only
that section `incomplete`. Each fix has its own register: it never joins the register of the
round it answers, nor that of an earlier fix. Reads expand it into its records like a round's
register. The records of a fix written before #355, one comment per turn, verification and
`sync` event, stay valid and are read as before.

### Turn journal

Since #362 (ADR 0004, P2-2) the authority on the turns is a local journal,
`<state_dir>/turn-journal.jsonl`; the `turn` records on GitHub are its published projection, and
their format does not change. The journal is append-only, one JSON event per line, each with its
`sequence`, the hash of the event before it (`previous`) and its own (`sha256`):

- `intent`: a turn about to start, with its id (`turn-<n>-<8 hex digits>`), the issue or PR, the
  issue of its work, the repository, kind, agent, model and label. It is written and made durable
  (`fsync`) before the turn's record on GitHub, so before the CLI starts. If it cannot be written,
  the turn does not start and the step escalates like any `gh` failure.
- `outcome`, one per turn: `returned` (the CLI returned), `failed` (it failed, or its model was
  unavailable), `not_dispatched` (its record on GitHub could not be written: the CLI never
  started) or `uncertain`. The outcome is recorded before the record on GitHub is closed; if that
  edit fails, the journal keeps the issue or PR for the next tick.
- `uncertain`: a turn left without an outcome, because the dispatcher stopped while it ran or the
  outcome could not be written. The next tick, with no action running on that issue or PR, records
  it so and logs `uncertain turn <id> on #<n>` (only logs it with `--dry-run`). The issue or PR
  stays in the journal's trace until a tick has checked its records on GitHub: a read that fails
  is tried again on the next tick. An uncertain turn is **never** run again automatically, and
  stays counted: nothing refunds it.
- `reconciled` and `refused`: see below. `unsettled` and `settled`: a record on GitHub left open
  after a failed edit, and the tick that checked it. `linked`: the issue of the work of a legacy
  turn (see the migration). `story`, `request` and `child`: the stories, with `story_identity`
  on (see [Story identity](#story-identity)). `attempt`, `attempt_pr` and `concluded`: the
  attempts, with `attempt_identity` on (see [Attempt identity](#attempt-identity)), and an
  intent of a turn of an attempt carries its id. `size_warning`: the operator was warned that
  the journal reached 80% of its limit (see below). The events of the
  [publication outbox](#publication-outbox) start with `publication`.
- With the [budget envelope](reference.md#budget-envelope) on (`[budget] enabled`, #365), the
  intent of a turn charged to a story carries its booking, `budget`: `{story, role, phase,
  engine}` (the role is the kind of the turn, the engine the kind of the agent's CLI) and, when
  the agent has a declared rule for unknown usage, `usage_model` and, for
  `reserve_per_invocation`, `conservative_usage` (the reserve in `metered_usd` and
  `cli_estimate_usd`), and `envelope`, the limits of the story (`turns`, `role_turns`,
  `phase_turns`, `wall_seconds`, `started_at`, `deadline_at`, `metered_usd`,
  `cli_estimate_usd`): those of its first booking, which the later ones keep, across restarts and
  configuration changes. `started_at` is the time of that first booking, from which
  `wall_seconds` runs, not the takeover of the story (#396); a booking without it, from before,
  starts `wall_seconds` before its `deadline_at`. The
  check and the booking are one step under the journal's lock; a turn the
  envelope does not cover writes no intent. The `outcome` of every turn, a readiness check
  included, then carries `usage`: `{metered_usd, cli_estimate_usd}`, what its CLI reported, `null`
  where it reported nothing (today only claude's estimate). With the budget off neither field is
  written.

**Integrity.** Every read verifies the whole chain. An incomplete last line (a write cut short),
or a last line that is not JSON, is set aside in `turn-journal-tail-<sha256>.bin` and cut off: the
valid records before it stay. A hash that does not match, or an unreadable line before the last,
is an explicit error, never ignored: the journal is not used and no turn starts. Next to it,
`turn-journal-state.json` keeps the sequence and hash of the latest event; a snapshot without its
journal, or a journal shorter than it or off it (cut, replaced, rolled back) is refused (ADR 0001,
constraint 3). The chain is not keyed: it gives integrity, not provenance.

**Limit.** Since #397 the journal's file has a limit, `journal_max_mib` (64 MiB by default),
which bounds the bodies the [publication outbox](#publication-outbox) keeps in it too. Its size
is checked before the journal is loaded and before each event is appended. A journal beyond the
limit is not loaded, by the dispatcher nor by `devloop status` or `tick --dry-run`, and an event
that would take it beyond is refused whole, before a byte is written: an explicit `JournalError`,
handled like any journal that cannot be read or written, so no turn starts.
Nothing is removed, cut or replaced to stay under the limit: the events, the bodies, the snapshot
and the checks of the chain stay as they are. A journal that is already beyond the limit when
devloop starts (an older one, or a lowered limit) is kept as it is and refused the same way, until
the operator raises the limit: devloop has no compaction of its own. Before that, once the journal reaches 80% of the
limit, where nothing is refused yet, each tick of the dispatcher warns the operator, with the
current size: with `[run].alert_issue`, one alert per threshold, which the journal records
(`size_warning`) so that it is not repeated after a restart (a new limit is a new threshold); a
failed alert is tried again at the next tick. With `publication_outbox`, an alert with an
uncertain outcome is only reconciled, never sent again (see the outbox below). Without `[run].alert_issue` the warning is a line
of the log, `WARNING: the journal of the turns is <n> bytes, ...`, once per start of devloop while
the size stays beyond the threshold. Either way the turns go on as usual until the limit itself.

**Who writes it.** Only the dispatcher that holds `dispatcher.lock` writes the journal: it keeps
the chain in memory and appends to it. `devloop status` and `devloop tick --dry-run`, which run
beside it without the lock, read it without any effect on the files: they neither set aside a
tail nor import or remove `ledger-open-turns.json`, and what their planning would record (a
legacy turn linked to its work, a turn made uncertain) stays in their memory. Their ids of the
legacy turns not imported yet are their own. A record they appended would break the chain of the
running dispatcher at its next write, and with it the journal.

**Migration.** The journal replaces `<state_dir>/ledger-open-turns.json`. An existing file is
imported once, at the first read, as one legacy turn per issue or PR (`legacy-<n>-<8 hex
digits>`), explicitly `uncertain`, with its number and count and no outcome made up; then the
file is removed, and one that comes back is not imported again. The next tick checks those issues
and PRs on GitHub as before. Each legacy turn is then linked to the issue of its work (a `linked`
event), so that it stays part of that work after a restart: a number among the open PRs through
the dispatcher's map; with the block on, any other number by reading it on GitHub in any state
(`find_pr`), so also a PR closed while devloop was stopped. A devloop PR, recognised by its branch
and trusted author as in the snapshot, belongs to the work of its issue; an issue, or a PR devloop
does not recognise, to its own number only.

**Block on uncertain turns.** With `block_uncertain_turns = true` (off by default: ADR 0004, D8),
a turn whose work has uncertain turns not reconciled is refused before its intent, with an
escalation of class `uncertain_turn` that lists them and how to reconcile them. The work is the
repository and the issue: the issue and its devloop PRs, which the dispatcher recognises by
branch and marker. Every new turn of it is refused, whatever its kind, agent, model, reviewer or
round. A legacy turn blocks the issue or PR of its number and, when that number is a PR of a
work, its issue and the other PRs of the same issue, open or closed. Before a turn is admitted the
legacy turns not linked yet are linked; one whose number cannot be read on GitHub may be a PR of
any work, so it blocks every new turn, and the escalation says so, until a later read links it. A
turn that ended without a durable outcome (its write
failed), or that a tick could not record as `uncertain`, blocks the work in the same way: a retry
or a fallback of the same step is refused, and the turn becomes `uncertain`, to reconcile, at the
next tick. Turns still running in parallel actions never block. A conclave call refused this way
is not recorded as started: the conclave of that block ends with the `uncertain_turn` escalation
and the item stays with the operator. An [advisory review](guide.md#advisory-review-for-the-operator)
refused this way, before its checkout, escalates too, although its other failures never do: the
escalation's marker records the ids of the turns it lists (`turns`), and while every turn that
blocks the work is among those already escalated on the PR, the PR gets no advisory action; a
new blocking turn gets its own escalation, and after the reconciliation the review runs. With
the block off devloop behaves as before: the journal records the turns all the same.

**Reconciliation.** Only the operator decides the outcome of an uncertain turn. A user in
`trusted_users`, never the supervisor nor an agent, writes on the issue or PR of the turn, on the
issue of its work or on one of its open PRs, a comment whose first line, after the attribution
header, is the command alone:

```text
/devloop reconcile <turn id> <returned|failed|not_dispatched>
<the reason, mandatory, on the lines below>
```

The next tick records the outcome in the journal once, with author, comment, note and time,
whether or not the block is on. A second command for the same turn with the same outcome has no
effect; with a different outcome it is refused, like `INVOCATION_RECONCILED` in the local engine.
A command without the note, with another outcome, for an unknown turn or for one that is not
uncertain is refused too: the journal records the refusal and the dispatcher answers it once on
that thread. A command from outside `trusted_users` is only logged. Commands are read while their
turn waits for reconciliation, on each tick. No automatic evidence is accepted in its place: the
proof of termination comes with step 4 of ADR 0001. After the reconciliation the step resumes as
any escalation to the operator does.

### Story identity

Since #363 (ADR 0004, P2-3a, recommendation (b) of section 3.2) the journal can hold the
**stories**, the unit of the budget of step 2: an issue and, from step 3, its child issues. It is
off by default (`story_identity = false`, ADR 0004, D8): with it off devloop records no story and
behaves as before.

- **Identity.** At the takeover of an issue (`agent:ready` to `agent:working`) the journal
  records a `story` event: the id the dispatcher assigns (`story-<n>-<8 hex digits>`), the
  repository and number of the issue, its root, and the `hash` of the [`request`](#evidence-and-telemetry)
  record of that takeover, the snapshot of the text the readiness check judged ready and
  devloop runs. It is written before the record on GitHub: if it cannot be, no record is
  written and the step escalates like any `gh` failure.
- **Later requests.** A new takeover of the root writes a new `request` record, as before. When
  its hash differs from the story's last one (a trusted user changed the issue), the journal
  adds it with a `request` event: the story keeps its id, repository and root, and its earlier
  hashes. The same text adds nothing. A change by an untrusted author still stops devloop.
- **Root marker.** The comment of the `request` record of the root ends with the `ledger` marker
  followed by a `story` marker, `{story, repo, root, issue, hashes}`, with every hash recorded so
  far, oldest first, and a "Storia" section in its text. A marker missing because a write failed
  is written again at the next takeover, from the journal. The journal must confirm the root's
  latest marker too: its `hashes` must be a prefix of the story's. A marker behind the journal
  (a write that failed after the journal recorded the hash) is valid; one that diverges from it,
  or goes beyond it (a journal restored from before), contradicts it: an integrity error, and the
  step escalates without recording the new hash.
- **Children.** A child issue inherits the story **only** from a `story` marker the dispatcher
  wrote on it, `{story, repo, root, issue}` without hashes (D7). A story declared in the text of
  the issue, by an agent or by a user, the operator included, or in a marker written by any
  other identity, is not inherited: at its takeover that issue becomes the root of a story of its
  own. The marker is read only from the dispatcher's identity, and the journal must confirm it: a
  dispatcher's marker for another issue, of a story the journal does not hold, or of an issue
  that is neither the root nor a child of that story is an integrity error, and the step
  escalates. A child is taken over without a story of its own and is never made a root.
- **Writing the marker of a child.** The dispatcher has the function (`Ledger.mark_child`): it
  records a `child` event in the journal, then writes the marker on the issue. **No flow calls
  it yet**: which act authorizes the association of a child is decided with step 3 of ADR 0001,
  and until then no issue gets the marker of a child.

Issues taken over before, or with the identity off, have no marker and are handled as before.

### Attempt identity

Since #384 (ADR 0004, P2-3b, recommendation (i) of section 3.2) the journal can hold the
**attempts** of each phase, with **one active attempt per phase**. It is off by default
(`attempt_identity = false`, ADR 0004, D8) and needs `story_identity = true`: with it off devloop
records no attempt and behaves as before.

- **Phase.** The triple (story, kind, approved upstream revision). The current flow has only the
  `implementation` phase of the root of a story, with a null revision: the phase stays the same
  across later requests and advances of the base branch. A child issue has no attempt of its own
  until step 3.
- **Identity.** An `attempt` event: the id the dispatcher assigns (`attempt-<n>-<8 hex digits>`),
  the phase, its number in the phase (`n`, from 1), the repository and issue it works on, and the
  key `(design, task)` of a unit generated by a plan (sha of the approved design, task id),
  foreseen in the format and null until the phases of step 3.
- **Opening.** The implementation opens its attempt after the `request` record and before its
  first turn. Retries, fallbacks, fixes, syncs and reviews are turns of the active attempt, not
  new attempts. An attempt whose PR was not opened yet (a failure before the PR) stays active, and
  the retry of the implementation resumes it.
- **One active attempt.** A new attempt of a phase is opened only once the previous one is
  concluded with a recorded outcome, and it consumes the same story budget. While the PR of the
  active attempt is open, a new one is refused (`AttemptActive`) before any turn, label or PR, and
  the step escalates. A merged issue is still never implemented again (#228), whichever attempt's
  branch the merged PR came from.
- **A PR not recorded.** An active attempt without a recorded PR is no proof that it has none:
  the write that records the PR may fail after the PR was opened. Before resuming it, the
  takeover looks on the attempt's branch, in any state, for the devloop PR whose marker names it:
  found, it is recorded, and concluded if no longer open, before a new attempt is opened. A PR of
  that branch opened since the attempt whose marker does not name it may be its PR with the field
  removed: the attempt is not resumed and the step escalates, so that no attempt gets two PRs.
- **Outcome.** A `concluded` event, once per attempt: `merged` or `closed` (its PR merged, or
  closed without merge), recorded by the tick that finds the PR no longer open (`find_pr`), or by
  the takeover that needs a new attempt; `abandoned`, with its reason, from an internal primitive
  (`TurnJournal.abandon`) that **no command and no flow calls yet**.
- **PR.** The PR of the attempt is recorded with an `attempt_pr` event, one per attempt, and its
  [marker](#pr-marker) carries `attempt`: `{attempt, story, phase, revision, n, design, task}`.
  Every field is required, the null ones included: a marker without one does not hold, and the
  PR is one whose marker was lost. The journal is the authority (D7): on each snapshot an attempt
  of a PR marker that the journal does not hold, of another issue, phase, number or key, of
  another PR, or already concluded (a PR reopened after it was merged or closed) does not hold.
  Nor does a marker without `attempt` on a PR the journal ties to an attempt: the attempt that
  has it as its PR, or an active attempt of its issue without a recorded PR (the field may have
  been removed). Such a PR gets no work, merge, conclave or advisory review, and is escalated
  (`attempt`); a turn on it is refused too.
- **Turns.** The intent of each turn of an attempt in the journal and its `turn` record on GitHub
  carry `attempt`, its id, with a "Tentativo" row in the text. A readiness check belongs to no
  attempt.

**One branch and one PR per attempt**, with the identity of the attempt in the branch name: the
first attempt keeps the branch of today (`branch_prefix` and the issue number, `agent/issue-<n>`),
attempt number `k` from the second on has `agent/issue-<n>-attempt-<k>`. A new attempt never
overwrites the branch of the previous one, and a closed PR is never reused. A PR of an attempt
branch is a devloop PR of issue `<n>` only with the marker of attempt `k`; the branch of today
holds the first attempt or a PR of none. Otherwise its marker does not hold, as when it is lost.
PRs opened before, or with the identity off, have no attempt in their marker: they are handled as
before, and their turns belong to no attempt.

### Publication outbox

Since #385 (ADR 0004, P2-4), with `publication_outbox = true` (off by default: ADR 0004, D8),
every comment, comment edit, native review and pull request devloop writes on GitHub passes
through an outbox (`devloop/outbox.py`): ledger records, agents' outputs (the implementer's pull
request, the native review of a reviewer with an app, the review comment of one without),
receipts and every other comment written with the dispatcher's or an agent's client. Without it
devloop publishes as before. The outbox keeps its events in the [turn journal](#turn-journal), in
the same hash chain; the turns ignore them:

- `publication`: a write about to be sent, its first attempt, with its id (`pub-<16 hex
  digits>`), `op` (`comment`, `edit`, `review` or `pr`), its arguments (the issue or PR, the
  comment of an edit, the commit and event of a review, the base, head and title of a pull
  request), the actor and permissions of its client, the **whole body**, the time and `held`
  (whether its `gh` holds the dispatcher lock, see below). It is
  durable before anything is sent, and nothing else is written before the send: if it cannot be
  written, nothing is sent, nothing is left for a restart to send, and the write fails like any
  `gh` failure.
- `publication_sending`: a later attempt about to be sent, with its time and `held`. From
  `publication` or
  here until an answer is recorded, the outcome is unknown.
- `publication_sent`, with the id of what was published (the comment's, the review's when known,
  the pull request's number) and how it was known (`response` or `reconciliation`);
  `publication_failed`, a failure reported to the caller, with its time, certain (final: GitHub
  wrote nothing) or `uncertain`; `publication_dropped`, never to be published, with why;
  `publication_signalled`, the operator was told of that drop (see below).

**No POST repeated on an uncertain outcome.** A write whose outcome is uncertain (a timeout, a
connection lost after sending, a 5xx answer: `GitHubUncertain`, #364; for a native review and a
pull request too, since #385) is reconciled: a comment
with `ports.reconcile_comment`, the comment of the same author (the app's login; for the ambient
`gh` login, one of `trusted_users`) with exactly that body, skipping the comments the outbox
already knows as another write's publication; an edit on the edited comment only; a review among
the PR's native reviews of the same author; a pull request among the open ones, from its head
branch, title and body. Found, the write is published. **Absence alone is no proof** while the
attempt may still take effect: a read counts as conclusive only when it starts 30 seconds after
a time the attempt is known to have ended (GitHub ends the requests it takes more than 10
seconds to serve). The time recorded before the send never counts: a pause of the process may
come between it and the send. In the process that sent it, that time is the end of the call,
measured once the call returned (its `gh` ended, killed at its timeout). For an attempt whose
end is unknown because the process stopped, it is when the restarted dispatcher first read the
journal, under the dispatcher lock, provided the attempt is `held`: devloop passes the lock to
the `gh` of each write, which keeps it open while it runs, so the lock is free only once that
`gh` has ended, also one that outlived the dispatcher that started it. The timeout of a `gh`
call that the dispatcher applies proves nothing after a stop: the parent that applied it is
gone. A `gh` still running after its dispatcher stopped therefore keeps the next one from
starting (`another dispatcher is running (lock held)`) until it ends; so that this never lasts
for ever, that `gh` runs under coreutils `timeout` (#397), a limit of its own that survives the
dispatcher: SIGTERM once the call's timeout (300 seconds) is spent, SIGKILL 5 seconds later. A
write stopped so is uncertain. Only with the outbox on does devloop pass the lock to `gh` (#397,
ADR 0004, D8): with it off, a dispatcher killed while a `gh` of its runs leaves the lock free,
and the next one starts at once, as before #385. An attempt not `held` has no proof of its end: it is never sent again,
only reconciled, and dropped if it is still absent 300 seconds (the timeout of a `gh` call)
plus 30 after the restart. The write that got the
uncertain answer waits for that moment and reads again; only a conclusive absence sends it once
more, at most two sends in all. Otherwise, or when GitHub cannot be read, the caller gets the
uncertain failure, as before the outbox; so does an error of the client that is not a `gh`
failure (an answer it cannot read), and the write is then only reconciled. A native review
whose outcome stays uncertain is not submitted again by the review's own retries: the outbox
settles it before any later write on the PR.

**Before every new write** on an issue or PR (on a comment for an edit, on a head branch for a
pull request), the writes about the same one still pending, and not running in another action,
are settled first; if one cannot be (GitHub not readable, or its absence not conclusive yet), the
new write is not sent and fails like any `gh` failure.

**What the caller was told holds.** A write whose caller was told it failed is never sent again:
at most a reconciliation finds it, published by an attempt already made. Its failure, like the
`publication_sent` of a write published, is an outcome devloop already acted on: if the journal
cannot take it, it is kept in memory and written before any later event of the journal, of the
outbox or of the turns (a turn's `not_dispatched` included), and no write is sent before it is.
A published comment whose confirmation is not durable yet keeps its id and issue in memory, so
that its edit waits for it too. If the process stops before the journal takes it, nothing
written after it is durable either, and the restart handles the write as cut short.

**Restart.** Each tick starts by writing what the journal could not take, then by settling every
pending write, before it reads GitHub. A write cut short by the stop, its caller gone with the
process, is reconciled: found, it is recorded as published; conclusively absent (30 seconds
after the restart read the journal, whatever time the write was recorded at), it is sent once
more if its `gh` held the dispatcher lock, dropped otherwise; until then it blocks the new
writes on the same issue, PR, comment or branch.
A write whose caller was already told it failed with an uncertain outcome is only
reconciled: found, it is recorded as published; conclusively absent, it is dropped, because its
step already handled it as not published. A pending edit followed by a later write on the same
comment is dropped: the later one was built from GitHub as it was. What cannot be settled is
logged (`publication <id> (... on #<n>) not settled`) and waits for the next tick.

**Dropped writes reach the operator** (#397). After settling, each tick reports the writes the
outbox dropped, here or in an action since: each is logged once per start of devloop
(`WARNING: publication <id> (... on #<n>) dropped, never published: <why>`), and with
`[run].alert_issue` the dispatcher publishes one alert there for those not reported yet, with
their id, operation, issue or PR, actor and reason, never their body, which stays in the journal.
The journal then records each as reported (`publication_signalled`): it is never reported
again, not even after a restart. A failed alert is tried again at the next tick. Without
`[run].alert_issue` the log alone reports them, at each start.

The alert is itself a write of the outbox, and its `publication` names what it reports, in
`covers`: the ids of the drops, or `journal_size:<threshold>` for the alert on the size of the
journal. So an alert whose outcome is uncertain is never followed by a second one about the same
drops or threshold: while it is pending they count as reported, also after a restart, and the
outbox only reconciles it. Once it is found published, the dispatcher writes the
`publication_signalled` of its drops, or the `size_warning` of its threshold, without sending
anything. Dropped in its turn, absent from GitHub, it covers nothing: the next alert reports its
drops again, and the dropped alert with them.

A reconciliation is as precise as the body: devloop's comments, reviews and pull requests carry
times, heads and digests, so two writes with the same body by the same author are not expected. The key applies at the next
start of devloop, like the other keys outside the reloaded ones; `devloop status` and `tick
--dry-run` never use the outbox.

## PR receipt

When a PR goes to `agent:ready-to-merge`, at level 1 or 2 and at level 0, the dispatcher writes
its **receipt** (#135): a comment for the operator who merges, bound to the approved head and to
the promotion, that tells what was asked, who did what with which model and at which cost, what
was verified on which commit, how the findings were closed and how the criteria are covered. It
is written only with a [ledger](#evidence-and-telemetry), before the label, in the comment of the
promotion itself (#338): one dispatcher comment holds the promotion ("Pronta per il merge") and
its receipt ("Ricevuta della PR #N"), and ends with the `policy` record of the promotion
followed by the `receipt` record, one marker per line. The receipt has two forms in it:

- the readable text, under the promotion's and with the dispatcher's [header](#attribution);
- a `receipt` marker, read like the other dispatcher records: only from the dispatcher's
  identity, only in the block of markers that ends the comment, only with a valid structure.
  It records the identity of the
  receipt (`pr`, `issue`, the approved `head`, `base`, the `policy` version, `level`, the number
  of the `promotion` on the PR and the SHA-256 `digest` of its content), the `totals`, the data
  declared `missing`, and `state` (`current` or `superseded`).

**Content.** It comes only from the dispatcher's ledger records and from the markers, never from
text written by agents: no summary, no finding title, no error text of a turn. The one part an
agent declared is the final acceptance map, which the dispatcher recorded in its markers: its
test references and reasons are kept as they are, in the content and in the digest, and shown
only as inert code spans (one line, `|` escaped), so no Markdown, link, mention or HTML of them
renders.

| Section | Source |
|---|---|
| Request | The latest `request` snapshot of the issue |
| Totals | Computed from the sections below (see after the table) |
| Turns | Every `turn` record of the issue (readiness checks, implementation) and of the PR (review, fix, sync): kind, agent, configured and real model, start, outcome, duration, input and output tokens, cost |
| CI and sync events | The `event` records of the PR (`ci_round`, `sync`) |
| Verifications on the approved head | The local `verification` records bound to that commit, and the latest `ci_round` of that head and of the approved round (none at level 0), with each check, its outcome and its duration |
| Rounds and verdicts | The `review` markers: each round with its head and the verdict of each reviewer (`ci` for a CI round) |
| Findings | Every blocking finding ever opened (`round/reviewer/ID`) and who closed it: its owner in a later round, `ci` or `operator` with the author (`finding` records). One closed under the rules [before #110](#markers-written-before-110) has no recorded closer |
| Policy | The policy receipt recomputed for the promotion: level, version, base, head, reasons, required reviewers |
| Acceptance criteria | The latest map: for each entry, the criterion as the issue text states it now (not quoted when someone outside `trusted_users` changed the text), the status, the tests it declares (`file` and `name`) and its reason |
| Story budget | Only with the [budget envelope](reference.md#budget-envelope) on (#393): the state of the budget of the issue's story as the dispatcher's journal records it when the receipt is built, per dimension (see after the table) |

**Story budget** ("Budget della storia"): with `[budget] enabled` the content has a `budget` key
and the receipt a section of its own, a table with what is consumed, reserved and available
against the cap of each dimension: the turns in total, per role and per kind of phase, the
dollars of each kind (`metered_usd`, `cli_estimate_usd`) and the time (seconds from the story's
start, with its deadline). Consumed: the turns with a confirmed outcome and the dollars the CLIs
reported; reserved: the turns without a confirmed outcome (running, uncertain or without outcome)
and the part of the reserves no reported cost covers. A dollar value no turn reports is
`non attestato`, never 0: consumed is `non attestato` when no turn reports one, `almeno` the
known sum when some do, with the number of turns that report none; available is `non attestato`
while one of them has no reserve either. An issue that belongs to no story says so. With the
budget off the content has no `budget` key and no section: content and digest are the ones
before #393.

**Totals:** the number of rounds (and of those that count against `max_review_rounds`) and of
turns, the summed duration of the turns, the known tokens (input, output, cache read, cache
creation, and the thinking tokens agy reports apart) and the known cost, the number of turns whose cost is not reported and of those with
no token reported. The duration of the verifications is apart: the local tests (ms) and the
checks of the CI on the approved head (s). A value no record attests is `null` in the marker and
`non riportato` in the text, never 0: a sum of values none of which is known is `null`.

**Coherence.** Before the promotion the dispatcher builds the receipt and checks it against the
markers. A **contradiction** blocks the promotion: the PR goes to `agent:needs-operator` with an
escalation of class `receipt_conflict` (critical) that lists it, and neither the `promoted`
record nor the receipt is written. These are contradictions:

- the head of the policy receipt is not the approved head (the latest round's, or at level 0
  the promoted one);
- the latest `ci_round` the ledger records for the approved head and round has a state other
  than `pass` or `none`, while the promotion relies on a CI passed or absent;
- the request snapshot names another issue than the PR marker;
- a `fix` or `sync` turn on the PR was run by another agent than the implementer the PR marker
  names, or a `review` turn by that implementer.

**Missing data** does not block: a PR or issue started before #133 or #134 has no snapshot, no
turns, no turn of the implementation, no `ci_round` for the approved head, or no map, and the
criteria cannot be quoted from a text no longer trusted. The receipt lists what is missing
under "Dati storici mancanti" (`missing`: `request`, `turns`, `implement`, `ci`,
`acceptance_map`, `criteria`) and reconstructs none of it.

**Invalidation.** When the approval is revoked the receipt no longer holds, and its comment is
edited: a warning "Ricevuta superata" with the time and the reason goes before its first title,
and its marker gets `state: superseded`, `superseded_at` and `reason`; the `policy` record of
the promotion in the same comment stays as it is. This happens when a ready PR
is [revoked](#merge-directives-and-record) (a new head, CI running again, the acceptance map or
level 0 that no longer hold), when the recomputed policy receipt is `stale`, when a CI round
fails on a ready PR, when a sync moves the head of a ready PR, and with every escalation that
takes `agent:ready-to-merge` away (the round limit reached by a CI failure, open findings, a
failed action, a lost marker, the level that requires the operator). A new promotion first marks
every receipt still current, whatever revoked it, then writes a new comment, with the
promotion and a receipt with the next promotion number: only the latest promotion's receipt is
`current`. A failed edit at a revocation is logged and marked again at the next promotion; at
the promotion it stops the promotion before its comment and its label, as any `gh` failure.

**One comment per promotion** (#338). When promotion and receipt together exceed the 65 536
characters of a GitHub comment, they are written as two comments, the promotion and then the
receipt, as before #338. The comments written before #338 keep that form and are read as they
are: a promotion with its `policy` record, then a receipt with its `receipt` record, then, after
a merge, a comment of its own with the `merged` record. When devloop merges the PR, the
[`merged` record](#merge-directives-and-record) goes in the comment of the promotion.

**Advisory review** (#369). The opinion of the
[advisory review](guide.md#advisory-review-for-the-operator) of the approved head is a section
of its own, "Review consultiva": the agent, the head and the opinion (or that it failed), from
the dispatcher's `advisory` record, never the agent's text. It is in the content, and so in the
digest, only when the head has one: a receipt without it is the one before #369. After the
advisory review the dispatcher edits the current receipt of the same head in place: section,
digest and totals (with the `advisory` turn) recomputed from the ledger, while promotion,
policy record and state stay, and so does the text before the receipt's title. A superseded
receipt, or one of another head, is left as it is. Without a current receipt the opinion stays in
the dispatcher's summary, and a later receipt of the same head carries it; no promotion is
written for it. The receipt is edited before the dispatcher's summary: an edit that fails leaves
the summary unwritten, and a later tick edits the receipt again, then writes the summary,
without a new turn. A receipt that cannot hold the section is logged and left as it is: the
summary tells the operator.
