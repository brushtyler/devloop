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
  base and the old and new heads, and the fix comment says that the merge was done;
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

A `round_limit` escalation lists the rounds that count, each with its reason (the first review,
a CI failure, a review after a fix, a review after a sync whose conflicts the implementer
resolved, a new review of the same head or of a new one), then the rounds that do not count with
theirs, and recalls that the operator can grant more rounds by raising `max_review_rounds` (#289).
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
  merged on it. The failure becomes a CI round after the review's, counted against
  `max_review_rounds` as any CI round, and the PR goes to `agent:changes` with every finding
  still open (or its failed jobs are re-run once, see
  [CI failures not pertinent to the PR](reference.md#ci-failures-not-pertinent-to-the-pr)).
- **No CI after the window.** With `local_tests` other than `full` the full suite runs locally
  before the promotion; if it fails, the PR goes to `agent:needs-operator`.

A round that asks for changes while the CI is pending, or within the window, sends the PR to
`agent:changes`, and the fix waits for the CI of the same head. If that CI fails, the failure
becomes a CI round first, as above (or its failed jobs are re-run once), and the one fix that
follows answers the reviewer's findings and the CI failure together. If it passes, or the
window ends without CI, the fix starts with the reviewer's findings; checks the base branch
requires and that never appeared are not escalated then: the fix pushes a new head, which gets
its own CI.

A CI already failed before the review is a CI round, as before: no review runs on that head.

## PR marker

A devloop PR is recognised by its branch, `agent/issue-<n>`, opened by a trusted user. Its body
ends with a hidden marker that records the issue, implementer and reviewer: text may be edited
above it, but the marker must stay the last line.

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
| `diff_hash` | SHA-256 of the uncommitted changes of the agent that stopped, attached to the prompts: the tracked files as `git diff`, the new untracked files with their content |
| `proposal` | the proposer (`agent`), `state` (`pending`, `started`, `done`, `missing`), `missing` (the reason, when missing), the `resolution` and its `hash`, the reserved fields (`reserved`, `reserved_reason`, `operator_question`, `options`) and `profile`, the profile the contribution was given with (identity, kind, model, effort and arguments, a fallback model included; null until it is done) |
| `votes` | the two voters, each with `state`, `missing`, `vote` (`APPROVA`, `BLOCCA`), `reason`, the reserved fields, `profile` and `proposal_hash`, the hash of the text it voted on |
| `hash` | SHA-256 of the proposal's text: the content evaluated |
| `stale` | why the frozen context stopped holding, `""` otherwise |
| `outcome` | `UNANIMOUS`, `BLOCKED` or `RESERVED`; `""` while the conclave is open |

A record is read back only from the dispatcher's identity and with a valid structure, and it must
hold together: `hash` is the hash of the recorded text, every vote was given on that hash, every
contribution done records its `profile`, the profiles distinct two by two by identity and by
`(kind, model, effort)`, and the recorded outcome is the one its contributions give. A record that does not hold is logged and
left to the operator: the block gets no other conclave. Each call is also a ledger `turn` of kind
`conclave` ([Evidence and telemetry](#evidence-and-telemetry)).

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
- **Record.** A dispatcher comment with a `merged` marker records the head, the receipt, the
  level and the mode, and in `merged_by` the login of the merger app when it merged (none with
  the local login). Its text says that devloop decided the merge and names who ran it: the
  merger app (its login without `[bot]`) or the local `gh` login.

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
`pr` marker of a PR opened by the implementer it names. On `[report] issue` it writes two kinds of
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

Findings get stable ids (`R<round>-<n>`). The fix step answers each one (`fixed`, `disputed`,
`not_applicable`), and the next round sees those answers. The comment shows the review in
readable form and carries the full record in a hidden base64 marker for the next tick.

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
the PR goes to `agent:needs-operator` with a comment that lists the open findings. When a reviewer
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
text, and reads a marker only from the last line of a comment, of the expected kind and with a
valid structure. Text from an implementer or a reviewer cannot record a review, a fix or a PR
link.

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

## Records bound to identities

Records are bound to identities. A PR counts as a devloop PR with its marker only if it was opened
by the app of the implementer the marker names; a review counts only if its comment was written by
the app of the reviewer named in it, and never by the PR's implementer; a CI round and a finding
closure only from the dispatcher's app; a fix only from the implementer's app. A readiness check
(`preflight`) counts only if written by the app of the configured agent it names, the checker:
the dispatcher sets the labels it implies, never the record. A `notice` counts only from the
dispatcher's app: the escalations and the operator-merge notices are its own; the `notice` of an
agent that stopped (`Lavoro bloccato`) is published with that agent's app and stays out of the
hash of the [readiness check](#readiness-check-records) like any text devloop published. Only
the [conclave](#conclave-records) reads it, and only when written by the app of the agent it
names. A `conclave` record counts only from the dispatcher's app. `trusted_users`
stays for people: it
decides who can start work with `agent:ready`, and it cannot contain bots. The only app whose
labels count is the supervisor's, on the issues described in
[Labels from the supervisor](guide.md#labels-from-the-supervisor).

The native review mirrors the verdict for GitHub and branch protection; devloop itself reads only
the review comment, and a failed native review is logged without stopping the flow.

## Evidence and telemetry

The dispatcher records the evidence and the telemetry of every step for itself (#133), so that
they no longer have to be pieced together from markers and logs. Each record is a dispatcher
comment ending with a `ledger` marker, signed with the dispatcher's identity like its other
records (only that identity's records count), on the issue for the implementation and the
readiness check, on the PR for review, fix, sync and CI. The [PR receipt](#pr-receipt) (#135)
gathers them at the promotion.

| Record | When | Content |
|---|---|---|
| `request` | The issue goes from `agent:ready` to `agent:working` | SHA-256 of title and body, author and time of the trusted `labeled` event of `agent:ready`, ids of the trusted comments given to the implementer, time of the takeover. It is a snapshot **at the takeover**, not at the time of the label |
| `turn` | Every invocation of an agent CLI, retries, fallback models and failures included: implement, fix, sync with an agent, review, readiness check, conclave call | Kind, agent, GitHub identity (none: the operator's `gh` login), configured model and effort (for a fallback, the fallback model and the model it replaces), real model, start, end, duration, outcome (`success`, `failure`, `unavailable`), the error if any, telemetry, and the last command the turn ran or started when the CLI exposes it (`last_command`: only codex, from the `command_execution` items of `--json`; cleaned like the tail of a timeout, on one line, at most 200 characters; absent otherwise), also for a turn that timed out |
| `verification` | Every test command the dispatcher runs after a writing turn (none when the [local tests](reference.md#local-tests) select nothing), and the full suite it runs before a review of a head without CI, or before the promotion of an approval given while no CI was known | Command, outcome, duration, the tree hash before and after the test, and the commit (none before a review) |
| `event` | The CI of a head (`ci_round`): a failed CI round, the checks a review round saw whatever its verdict (`approve`, `request_changes` or `needs_operator`), or the passing checks read again just before the PR is promoted to `agent:ready-to-merge` (also at level 0); a sync without an agent, or the merge of `main` before a fix (`sync`) | For the CI, the head, the state (`fail`, `pass`, or `none` without checks), the review round (none at level 0) and each check with its outcome, the id of its GitHub Actions run (none for a commit status or a check outside Actions) and its duration in seconds (`duration_s`: none while it runs and for a commit status, #300). Every CI round also records `tests`, the [summary of the suite](reference.md#summary-of-the-ci-in-the-prompts) that the dispatcher reads for the review and fix prompts (read again from the job logs when a promotion records the round, for instance after a review that saw the CI pending): for each job whose log holds the final lines of `scripts/run-tests.py`, its name with `ran` and `slowest`, and for a job whose log could not be read, its name with `log: unavailable`; absent when no job has either. The same checks read again for the same head and round are not recorded twice, a new run is. For a sync, the merged base commit and the old and new heads |

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
for example an issue whose PR is already open or a PR escalated to `agent:needs-operator`, and then
forgets it. In case the note is lost, it also looks at open devloop PRs not in
`agent:needs-operator` (or in it only for an [operator merge](guide.md#ready-prs-whose-merge-is-the-operators)),
issues in `agent:working` and issues waiting for a readiness check.

**Tests and commits.** A passing test is bound to the commit created right after it only if the
tree is the same at three points: before the test, after the test and in the commit. The tree is
what the commit would contain (`git add -A`), computed on a copy of the index. If the test
changed files headed for the commit, or anything else differs, the commit stays `null`; the
attempts that failed, or that no commit followed, have commit `null` too.

**Earlier work.** PRs and issues started before these records have none of them. Missing data
is declared missing (`request` null, no turns), and is never reconstructed from the summaries
written by agents.

## PR receipt

When a PR goes to `agent:ready-to-merge`, at level 1 or 2 and at level 0, the dispatcher writes
its **receipt** (#135): a comment for the operator who merges, bound to the approved head and to
the promotion, that tells what was asked, who did what with which model and at which cost, what
was verified on which commit, how the findings were closed and how the criteria are covered. It
is written only with a [ledger](#evidence-and-telemetry), right after the `promoted` policy
record and before the label. The comment has two forms of the same receipt:

- the readable text, with the dispatcher's [header](#attribution);
- a `receipt` marker, read like the other dispatcher records: only from the dispatcher's
  identity, only as the last line, only with a valid structure. It records the identity of the
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

**Totals:** the number of rounds (and of those that count against `max_review_rounds`) and of
turns, the summed duration of the turns, the known tokens (input, output, cache read, cache
creation) and the known cost, the number of turns whose cost is not reported and of those with
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
edited: a warning "Ricevuta superata" with the time and the reason goes before its title, and
its marker gets `state: superseded`, `superseded_at` and `reason`. This happens when a ready PR
is [revoked](#merge-directives-and-record) (a new head, CI running again, the acceptance map or
level 0 that no longer hold), when the recomputed policy receipt is `stale`, when a CI round
fails on a ready PR, when a sync moves the head of a ready PR, and with every escalation that
takes `agent:ready-to-merge` away (the round limit reached by a CI failure, open findings, a
failed action, a lost marker, the level that requires the operator). A new promotion first marks
every receipt still current, whatever revoked it, then writes a new
one with the next promotion number: only the latest promotion's receipt is `current`. A failed
edit at a revocation is logged and marked again at the next promotion; at the promotion it stops
the promotion before its label, as any `gh` failure.
