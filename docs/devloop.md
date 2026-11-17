# devloop: agent dispatcher for this repository

devloop moves issues through implementation, independent review and fixes, so that a human only
decides what starts and, unless a [merge mode](#merge-modes) lets devloop merge, what gets
merged. It is development tooling for this repository, not part
of the SSDLC orchestrator, and it never touches SSDLC runs or the main checkout's working tree.

## The loop

State lives on GitHub: `agent:*` labels and hidden markers in comments. A tick reads that state,
plans the next steps and runs them. A crashed tick is resumed by the next one.

| Step | Trigger | Result |
|---|---|---|
| Start | A trusted user adds `agent:ready` to an issue (or the [supervisor](#labels-from-the-supervisor), on an issue of a trusted user or of a devloop app) | `agent:working`; the implementer works in a fresh worktree |
| Implement | Changes made and [local tests](#local-tests) pass | Dispatcher commits, pushes `agent/issue-<n>`, opens a PR labelled `agent:review` |
| CI | Checks pending | Wait; failing checks become a review round sent back as `agent:changes`, unless no failed test module is pertinent to the PR: then the failed jobs are re-run once, without a round (see [CI failures not pertinent to the PR](#ci-failures-not-pertinent-to-the-pr)) |
| Review | CI passed or absent (absent CI with `local_tests` other than `full`: the full suite first runs locally, see [Local tests](#local-tests)) | The dispatcher computes the PR's [review level](#review-level-and-policy-receipt); at level 1 a **different** agent posts a structured verdict (see below), at level 2 two [independent reviewers](#level-2-two-independent-reviewers) do, one per action, and their verdicts are applied together |
| Fix | `agent:changes`, or a [`/devloop fix`](#fix-requested-by-the-operator-or-the-supervisor) command on a PR in `agent:review` or `agent:changes` | The implementer fixes or disputes every open finding of the round, and applies the requests of the command, then back to `agent:review` |
| Sync | A PR in `agent:review` or `agent:ready-to-merge` conflicts with `main` | The implementer resolves the conflicts of a merge of `origin/main`; the dispatcher pushes the merge commit, back to `agent:review` (see below) |
| Approve | Verdict `approve` from every reviewer the level requires | `agent:ready-to-merge`, only if no [finding](#findings-and-who-closes-them) is still open, the PR head is still the reviewed commit, CI passed and the policy receipt recomputed just before promotion matches the one of every approval (an earlier base is admitted only if `main` left the PR's files alone since, see [When `main` advances](#when-main-advances-during-a-round)) |
| Level 0 | A trusted user's `reviews:0`, the whole diff in the operator's allowlist, every expected check `success` | `agent:ready-to-merge` without a review (see [Level 0](#level-0-no-review)); off by default |
| Merge | `agent:ready-to-merge` | **A human merges** with `[merge].mode = "off"`, the default. With `"strict"` or `"auto"` devloop merges once every merge directive holds again (see [Merge modes](#merge-modes)) |

Readiness belongs to the approved commit. Every tick re-checks ready PRs:

- if the head is no longer the approved commit, the approval is revoked and the PR goes back to
  `agent:review` for a new review;
- if CI is running again on the approved commit, readiness is suspended (`agent:review`) and the
  recorded approval is applied again, without a new review, once CI passes and if its policy
  receipt still matches;
- if CI fails on the approved commit, the failure becomes a review round (`agent:changes`), or,
  when no failed test module is pertinent to the PR, a single re-run of the failed jobs
  ([CI failures not pertinent to the PR](#ci-failures-not-pertinent-to-the-pr)).

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
`agent:changes` on the PR). Every escalation comment repeats this. Every escalation has a
**class**, recorded in its marker; with `[merge].mode = "auto"` the non-critical ones go to
`agent:needs-supervisor` instead (see [Escalations to the supervisor](#escalations-to-the-supervisor)).

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
[Installation as a dedicated user](#installation-as-a-dedicated-user).

A round is one evaluation of a head: a CI failure, or the reviews of every reviewer the review
level requires (one at level 1, two at level 2), whatever the number of reviewers. The round
limit (`max_review_rounds`) is checked before every new round, whether a review or a CI failure,
so a PR whose head keeps moving cannot be reviewed indefinitely. Replaying a verdict that is
already recorded is not a new round, and neither is a sync with `main` nor the second review of
a round already open. A round that `main` advancing interrupted before it was complete does not
count either (see [When `main` advances](#when-main-advances-during-a-round)): the round that
replaces it counts once. Round numbers stay progressive, because they also name the findings
(`round/reviewer/ID`), so the round shown as the limit (`round 4 of at most 4`, `round 4/4`) is
`max_review_rounds` plus the rounds that did not count.

### Fix requested by the operator or the supervisor

A fix normally answers a review that asked for changes on the current head: a PR in
`agent:changes` whose head moved after that review (for example after a sync with `main`) goes
back to review instead. To ask for a correction that is already known, without waiting for a
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
  fix holds: [local tests](#local-tests), `protected_paths`, push with the implementer's
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

A devloop PR is recognised by its branch, `agent/issue-<n>`, opened by a trusted user. Its body
ends with a hidden marker that records the issue, implementer and reviewer: text may be edited
above it, but the marker must stay the last line. A PR whose marker is missing stays in the flow
(it still counts as an open PR and its issue does not restart) and goes to
`agent:needs-human`. A PR left without a state label, for example after a crash right after it
was opened, is put back into `agent:review`, and its issue loses `agent:working`.

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
in [AGENTS.md](../AGENTS.md) ("Contenuti di autori non fidati").

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
implementer does not start. An agy check stopped on a denied permission is first run once more
([agy permissions](#agy-permissions), #180).

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

The criterion for a sensitive path: **the code that decides permissions, fencing and
authorizations goes to level 2**, besides devloop itself and the agents' instructions. So the
repository's `config.toml` adds to the defaults above the ssdlc security modules (#171): writer
fencing and locks (`ssdlc_writer_gate.py`, `ssdlc_role_fencing.py`,
`ssdlc_container_holder.py`, `ssdlc_locks.py`), authorized transitions and gates
(`ssdlc_transitions.py`, `ssdlc_replay_gate.py`, `ssdlc_grader.py`), delegations and
admissions (`ssdlc_feedback_delegation.py`, `ssdlc_feedback_admission.py`,
`ssdlc_usage_admission.py`) and the app tokens (`scripts/agent-token.py`). A PR that changes
only other modules, such as `ssdlc_impact.py`, stays at level 1. An entry is only a path: a
listed file that does not exist yet is no error, and a PR that adds it gets level 2.

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
- **Both reviewers in one action** with [parallel actions](#parallel-actions)
  (`max_parallel_actions` above 1, #176). A review action runs at once every reviewer the round
  still waits for, each in its own copy of the worktree (`pr-<N>-review-<reviewer>`) and with
  the same prompt, so each still sees only the earlier rounds. Each verdict is recorded as
  before, one per reviewer, and a round whose reviewers both answer is applied in the same tick.
  If a reviewer fails, the other verdict stays recorded and the round waits only for the
  missing reviewer; the failure goes to `agent:needs-human` as any failure does, and once a
  human sends the PR back to `agent:review` the next action runs only that reviewer.
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
  opens a new round. A receipt that changed only in the commit of base is the exception below.

### When `main` advances during a round

An approval holds only for the base it was given on (#106). When `main` advances while a round
waits for its second reviewer, and the head, the policy version, the level, the reviewers and
their profiles are unchanged, the dispatcher checks whether the new commits of `main` interfere
with the PR (#160):

- it lists the paths changed by **every** commit of the first-parent chain of `main` after the
  round's base, up to the current one, each commit on its own: a change later reverted still
  counts, a rename counts with both paths, a merge is compared with its first parent, so it
  counts the net diff it brought to `main`. The commits of a merged branch are not walked
  (#179): one that merged `main` into the branch would bring back changes the old base already
  had. The net diff between the two bases is not used;
- `main` interferes when one of those paths is a file of the PR's diff (added, changed, deleted
  or renamed, both sides), a directory above one, or a path under one (a file replaced by a
  directory, or the reverse);
- when the commits cannot be listed (the old base is not an ancestor of the new one, because
  `main` was rewritten, or a commit is missing), non-interference is not proven;
- when `main` left the PR's files alone, the PR must also still merge into the new base without
  conflicts. Disjoint paths do not imply it: a directory renamed by `main`, where the PR adds a
  file, is a conflict on the file's location. The dispatcher merges the receipt's head into the
  new base with `git merge-tree --write-tree`, on these two exact commits, because the
  mergeability GitHub reported in the snapshot may predate the new base. A conflict turns the
  check's `outcome` into `conflict` (with `paths`, the files in conflict); a merge git cannot
  compute (a missing commit, a git older than 2.38) into `unverifiable`.

If `main` did not interfere, the round **continues**: only the missing reviewer reviews, with the
receipt of the new base, and the verdicts already given stay valid for the round. Its review
record also carries `bases`, the check from each earlier base of the round to the new one
(`from`, `to`, `outcome`). A PR in conflict with the new base is neither reviewed nor promoted:
the round waits for the sync, which runs as usual once GitHub reports the conflict and changes
the head.

Otherwise the round **restarts**, as before #160: every reviewer reviews again on the new base.
The first review of the new round records `restarts`, the round it replaces and the failed
check. That replaced round does not count against `max_review_rounds`; the new one does. The
exception covers only an incomplete round abandoned for the base alone: CI rounds, rounds
restarted for a new head, a change of policy, level or reviewers, or a `/devloop rereview` keep
counting. For example, with `max_review_rounds = 3`: round 1 is completed; round 2 is abandoned
for the base and does not count; round 3 replaces it and counts as the second; one more round,
number 4, is left.

Promotion recomputes the receipt on the current base, as for any approval. An approval given on
an earlier base, also when `main` advanced during the last review of a round already complete,
is admitted only if the same check passes from its base to the current one. The original review
records stay as they are, and the promotion's `policy` record carries `bases` with the checks
it relied on. If `main` interfered, the PR conflicts with the current base, or non-interference
is not proven, the approval is not promoted (a `stale` record lists the checks): a conflict is
synced first, as usual, and a new round follows, which counts.

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
The merge stays the operator's, unless a [merge mode](#merge-modes) other than `"off"` applies to
level 0, and then these same checks are repeated just before the merge. While the PR is ready, every tick
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
profiles (and the head) all match. A different commit of base is admitted only when the commits
of `main` in between left the PR's files alone (see
[When `main` advances](#when-main-advances-during-a-round)). When the base branch advanced
onto the PR's files, the policy or the
configuration of the pool or of a required reviewer (apps, models, efforts, `review_args`)
changed, or the level rose, the PR stays in
`agent:review`, a dispatcher comment says what changed, and the next tick runs a new review,
even of the same head. That review is a
round like any other and waits for CI. Approvals recorded before receipts existed have none:
they are not promoted and need a new review; their rounds and open findings stay as they are. A
PR already in `agent:ready-to-merge` keeps its label until something sends it back to review.

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
- **Operator-only paths.** A PR whose full diff (the paths the review level reads: deletions and
  both sides of a rename included) touches `operator_only_paths` behaves as `"off"`, whatever the
  mode. The default is `docs/adr/`, `AGENTS.md` and `CLAUDE.md`; an entry is a directory prefix
  or a file relative to the root, as for `sensitive_paths`.
- **Merge directives.** The same in `"strict"` and `"auto"`. A tick plans a merge only for a
  ready PR that meets them in its snapshot, and the merge reads them all again just before it
  runs, reviews, finding closures and policy records included (read again from the comments,
  with the same identity checks as the snapshot):
  - the PR is open and in `agent:ready-to-merge`, without `agent:needs-human`,
    `agent:needs-supervisor` or `agent:merge-hold`, and no hold or release command waits to be
    applied;
  - the head is the approved commit (at level 0, the promoted one), unchanged since the
    snapshot;
  - GitHub reports `mergeable` `MERGEABLE` and `mergeStateStatus` `CLEAN`;
  - CI is `pass`: a head without CI is not merged;
  - no blocking finding is open, and the [acceptance map](#acceptance-criteria-coverage) holds;
  - the conditions of the promotion: the receipt recomputed now matches the approvals of every
    reviewer the level requires (an earlier base is admitted as at promotion, see
    [When `main` advances](#when-main-advances-during-a-round)); at level 0, the checks of its
    promotion (level still 0, its labels, every expected check `success`, no standing
    `REQUEST_CHANGES`).

  When one is missing devloop does not merge, and the revocation already in place applies: a
  moved head, CI running again or failing, or a level 0 that no longer holds is revoked as on
  any tick of a ready PR; a receipt that no longer matches is recorded as `stale` and the PR goes
  back to `agent:review` for a new review; an open finding sends it to `agent:needs-human`. A
  PR that is `BLOCKED`, `BEHIND` or `UNSTABLE` stays ready and waits. **No mode, and no agent,
  merges with CI not green, with blocking findings open or without the approvals required.**
- **How.** `gh pr merge --merge --match-head-commit <approved head>`: a merge commit, only if
  the head is still the approved one. It runs with the [merger app](#the-merger-app) when
  `[apps.merger]` is configured, with a token minted at that moment and limited to
  `contents=write` and `pull_requests=write`; without it, with the local `gh` login devloop runs
  with. No other GitHub App, the dispatcher's included, gets `contents=write`. The branch is
  kept. A merge that GitHub refuses goes to `agent:needs-human` like any `gh` failure.
- **Slot.** A merge takes a slot of `max_actions_per_tick` and runs alone, after the actions
  planned before it and before those planned after it ([Parallel actions](#parallel-actions)).
- **Record.** A dispatcher comment with a `merged` marker records the head, the receipt, the
  level and the mode, and in `merged_by` the login of the merger app when it merged (none with
  the local login).

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
| `tool_failure` | The agent or its model produced no result: a crash, a timeout, a stall with `agent_stall_action = "abort"`, no model available, a denied permission, a block for [background work still running](#no-background-work-in-a-turn), an agent that [could not start](#the-loop) even after its retry (also in a sync with conflicts on sensitive paths) | No |
| `preflight_default` | A failed [readiness check](#readiness-check-before-implementation) whose gaps are all `default_available`, with no split | No |
| `sync_failed` | A [sync](#conflicts-with-main) that failed, with no file in conflict under a sensitive path (`sensitive_paths`) or `operator_only_paths`; one whose agent could not start is a `tool_failure` | No |
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
  that started there) and the next tick runs it; an issue's readiness check or implementation runs at once, and
  takes a free PR slot as it would. A failed attempt writes no review record, so a retry
  **counts no round**. After the initial failure the supervisor may authorise **two** retries
  for the same key; the third command is not run and the item goes to `agent:needs-human`
  (`retry_limit`). The automatic retries inside one run (a permission denied to agy,
  [fallback models](#fallback-models)) do not count. The key is durable, read from the
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
- `/devloop escalate <reason>` hands the escalation to the operator: `agent:needs-human`, with
  the reason (`supervisor_escalated`). It needs a reason.

An escalation whose class is critical or unknown, or that was not routed to the supervisor,
accepts no command: the command is refused and the item goes to `agent:needs-human`. A command
the class does not admit (`accept-defaults` on a tool failure, `retry` on defaults) is refused
and the escalation stays with the supervisor.

**Trace.** Every command applied leaves a dispatcher comment with a `supervision` marker: the
command, its author, its reason, the id of the comment that carried it, the class and the outcome
(`applied`, `accepted`, `escalated`, `refused` or `limit`), with the key and the attempt for a
retry and the defaults for an acceptance. The record is written before the labels change, so a
lost label change never runs a retry twice.

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
([`implement.json`](../devloop/schemas/implement.json), [`fix.json`](../devloop/schemas/fix.json)):
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
`max_review_rounds` like any other, and at the limit the PR goes to `agent:needs-human`. An issue
text changed by someone outside `trusted_users` cannot be checked: the approval does not hold, and
the new review escalates as for any untrusted change.

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

## Who does what

Roles are assigned to actors, not to agents: an actor is an agent with its own GitHub App, or the
operator. The assignment lives in the configuration (`[roles]`) or in
[ADR 0002](adr/0002-ruoli-e-responsabilita.md), not in the code, and every decision of a role
leaves a trace on GitHub signed by the identity that takes it. ADR 0002 is the source; its status
is *proposed* until the operator approves and merges it.

| Role | Actor today | Does | Does not |
|---|---|---|---|
| **Operator** | `brushtyler` | Takes the reserved decisions of ADR 0001; resolves escalations (`agent:needs-human`); approves ADRs; assigns roles; can always merge, block or resume any work | — |
| **Coordinator** (optional, not assigned today) | — | Backlog plan and priorities, draft ADRs, checks the supervisor's work. A revocable delegation of the operator | The reserved decisions |
| **Supervisor** | Claude Code (`claude-agent`) | Watches devloop; analyses runs; opens issues for problems and improvements; queues issues of trusted authors (`agent:ready`, priority) within the approved priorities; asks for a fix of a PR in review (`/devloop fix`, any mode); in `auto` mode holds or sequences merges and decides non-critical escalations; escalates to the operator | Implement, review, change configuration, policy, budget or credentials; merge: the dispatcher does |
| **Dispatcher** | devloop (`dispatcher`) | Runs the steps: preflight, implement, review, fix, sync, CI rounds, promotion, merge (`strict` or `auto` mode), pin (if on), continuous loop (if on); writes the markers; escalates | The reserved decisions; merge with merging off |
| **Preflight** | Codex (`codex-agent`) | Checks an issue's readiness, read-only | Write to GitHub: the dispatcher does |
| **Implementer** | Claude (`claude-agent`) | Implements and fixes in the worktree | Publish, push, merge |
| **Reviewer** | Codex (level 1); Codex + agy (level 2) | Structured review with a verdict | Fix |

The implementer never reviews its own PR, the supervisor neither implements nor reviews, and on a
PR it also implemented the supervisor decides no escalation or hold: the operator does.

### Merge modes

ADR 0002 sets three merge modes, configured with `[merge].mode` and an optional per-level
override in `[merge.levels]`; the most restrictive one wins. #172 implements the merge (see
[Merge modes](#merge-modes)) and #182 the
[escalations to the supervisor](#escalations-to-the-supervisor).

| Mode | Who decides | Behaviour |
|---|---|---|
| `off` (default) | Operator | devloop brings the PR to `agent:ready-to-merge` and stops. A person merges. Every escalation goes to the operator |
| `strict` | Merge directives | devloop merges **only** when the merge directives hold. Every escalation goes to the operator |
| `auto` | Directives + supervisor | As `strict`. The supervisor can also hold a merge (`/devloop hold`, `/devloop release`) and **decides non-critical escalations on its own** (`agent:needs-supervisor`). Critical ones go to the operator |

The merge directives are: an approval from every reviewer the level requires, no open finding,
the head equal to the approved commit, a matching policy receipt, CI passed, the PR mergeable
without conflicts, and neither `agent:needs-human` nor `agent:merge-hold`. **No mode, and no
agent, merges with CI not green, with open blocking findings or without the required approvals.**

**Critical: always to the operator, also in `auto`.**

- The reserved decisions of ADR 0001, including rounds and budget beyond the limits.
- PRs that touch `docs/adr/`, `AGENTS.md` or `CLAUDE.md` (`operator_only_paths`).
- The preflight's `operator_decision` decisions.
- Content from untrusted authors, or text changed after `agent:ready`.
- Any unclassified case.

**Non-critical: in `auto` the supervisor decides, with a trace on GitHub.**

- Retrying a step that failed for a tool problem (agent crashed, model unavailable, permission
  denied), without counting a round and with at most 2 attempts.
- Accepting the preflight's technical defaults (`default_available`) on issues of trusted authors.
- Holding or sequencing merges.
- Asking for a new review.
- Parking P2 work according to the operator's strategy.

## GitHub identities

Each agent and the dispatcher can act on GitHub with its own GitHub App (#65), configured under
`[apps]` in `config.toml`:

| Actor | Does |
|---|---|
| dispatcher | every read, labels, escalations, CI rounds, re-runs of failed CI jobs not pertinent to the PR (needs `actions: write`, see [CI failures not pertinent to the PR](#ci-failures-not-pertinent-to-the-pr)), finding closures, readiness |
| implementer | commits (author and committer are its bot), push, opens the PR, fix and blocked comments |
| reviewer | the review comment and a native GitHub review (`APPROVE`, `REQUEST_CHANGES` or `COMMENT`) on the reviewed commit |
| merger | only the merges of the `strict` and `auto` [merge modes](#merge-modes) (see [The merger app](#the-merger-app)) |

Apps are per agent, not per role: GitHub keeps one review state per identity, and it already
refuses an approval from the author of a PR. Least privilege per role comes from the tokens:
devloop mints an installation token for each action, limited to this repository and to the
permissions that action needs. A reviewer's token can read contents and write pull requests, but
cannot push; labels are always set by the dispatcher.

Records are bound to identities. A PR counts as a devloop PR with its marker only if it was opened
by the app of the implementer the marker names; a review counts only if its comment was written by
the app of the reviewer named in it, and never by the PR's implementer; a CI round and a finding
closure only from the dispatcher's app; a fix only from the implementer's app. `trusted_users` stays for people: it
decides who can start work with `agent:ready`, and it cannot contain bots. The only app whose
labels count is the supervisor's, on the issues described in
[Labels from the supervisor](#labels-from-the-supervisor).

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

### The merger app

The `strict` and `auto` [merge modes](#merge-modes) merge with an app of their own, `merger`
(#201), so that devloop needs neither a `gh` login nor the operator's broader permissions. It is
the only app with `contents=write`, and it does nothing else: comments, labels and pushes stay
with the dispatcher's and the agents' apps, its comments and labels count for no role (the
dispatcher accepts reviews only from configured agents, so a review naming `merger` as
reviewer is ignored even when its app wrote it), and
`scripts/agent-token.py` refuses it, as it refuses the dispatcher's app. Without `[apps.merger]`
the merges use the local `gh` login, as before.

Creating it (once, by the operator, in the account that owns the repository: *Settings →
Developer settings → GitHub Apps → New GitHub App*):

- **Name.** For example `brushtyler-devloop-merger`; its slug is the `slug` of the
  configuration, and its bot login `<slug>[bot]`.
- **Webhook.** Off: devloop reads GitHub, it receives no events.
- **Permissions.** Repository permissions `Contents: Read and write` and
  `Pull requests: Read and write`, plus `Metadata: Read-only`, which GitHub requires. Nothing
  else: no `Workflows`, `Issues` or administration permission, and no organisation or account
  permission.
- **Where it can be installed.** *Only on this account*.
- **Installation.** *Install App* → *Only select repositories* → this repository alone. devloop
  mints tokens for this repository only, but an installation limited to it keeps the key from
  reaching any other.
- **Private key.** *Generate a private key*, then put the downloaded file in `key_dir` of the
  user devloop runs as (or `$DEVLOOP_APP_KEY_DIR`) with its name as GitHub gives it,
  `<slug>.<date>.private-key.pem`, and `chmod 600` it. A configured app whose key is missing
  stops devloop at startup.
- **Branch protection.** If a rule of the base branch restricts who can push or merge, add the
  app to those allowed.

Configuring it, in `config.toml` (app id and slug are not secret):

```toml
[apps.merger]
app_id = 5792929
slug = "brushtyler-devloop-merger"
```

`merger`, like `dispatcher`, cannot be the name of an agent. `python3 -m devloop apps` then
lists `merger` with its installation and permissions; without the app it reports that merges use
the ambient `gh` login. Each merge asks for a new token, limited to `contents=write` and
`pull_requests=write`, when it runs; `--match-head-commit` binds it to the approved head as
before, and the `merged` record carries in `merged_by` the app's login.

## Ports and adapters

The dispatcher reaches GitHub through three ports, declared in `devloop/ports.py` as step 1 of
[ADR 0001](adr/0001-prodotto-unico-fasi-su-tracker.md):

| Port | Methods |
|---|---|
| `Tracker` | issues and their history, comments of issues and PRs, labels: `issues_with_label`, `issue`, `issue_history`, `issue_comments`, `pr_comments`, `labelers`, `label_events`, `labeled_events`, `edit_labels`, `comment`, `close_issue`, `edit_comment`, `ensure_label` |
| `CodeReview` | PRs and reviews: `open_prs`, `merged_prs`, `merge_state` (`clean`, `conflict` or `unknown`), `pr_head`, `pr_state`, `merge_pr`, `pr_reviews`, `create_pr`, `submit_review` |
| `CI` | checks of a commit: `checks` (summarized by `summarize_checks`), `check_details`, `check_runs`, `failed_log`, `rerun_failed_jobs` (#200) |

The ports hold only the methods the dispatcher calls, with their current names and signatures,
and return normalized data. `GitHub` (`devloop/github.py`) implements all three, and `Identities`
(`devloop/apps.py`) hands one out for each actor: they are the concrete adapters. The
dispatcher, its ledger and `setup` depend on the ports only. There is no other backend yet; the
module docstring records where a method that could fit more than one port went.

In the tests, `FakeGH` (`tests/helpers/devloop_fakes.py`) implements the ports in memory. It
proves neither persistence nor atomicity. `tests/test_devloop_ports.py` checks that the adapter
and every fake expose each port method with the same signature.

## Usage

```bash
python3 -m devloop setup            # create the agent:* labels, the priority:* labels and the review level labels (reviews:0 only when level 0 is on)
python3 -m devloop status           # running action, devloop PRs (with merge state and conflicts), ready issues in start order with their priority, planned actions
python3 -m devloop apps             # check the GitHub App of each actor
python3 -m devloop tick --dry-run   # print the ready issues in start order and the plan, change nothing
python3 -m devloop tick             # run one round
python3 -m devloop run --interval 600
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
on a [fallback model](#fallback-models), the start time and the dispatcher's process id. The
record is removed when the action ends, also when it fails. While several
[parallel actions](#parallel-actions) run, the file holds `{"actions": [...]}`, one record per
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
without `--ephemeral` (and with `--json`, for the [telemetry](#evidence-and-telemetry)): Codex saves the session of every turn in `~/.codex/sessions`, and these
transcripts stay there after the turn.

### Waking the supervising agent

devloop works in ticks that someone starts. The agent that supervises it (Claude Code, Codex or
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

The [ledger](#evidence-and-telemetry) records (`## Registro devloop`) are not reported: they are
too frequent. Trust follows [AGENTS.md](../AGENTS.md): an issue or PR counts if its author is in
`trusted_users` or is one of devloop's apps (the `[apps]` logins of the agents and the
dispatcher; not the [merger app](#the-merger-app), which only merges: its comments and the
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
| `turn` | Every invocation of an agent CLI, retries, fallback models and failures included: implement, fix, sync with an agent, review, readiness check | Kind, agent, GitHub identity (none: the operator's `gh` login), configured model and effort (for a fallback, the fallback model and the model it replaces), real model, start, end, duration, outcome (`success`, `failure`, `unavailable`), the error if any, telemetry, and the last command the turn ran or started when the CLI exposes it (`last_command`: only codex, from the `command_execution` items of `--json`; cleaned like the tail of a timeout, on one line, at most 200 characters; absent otherwise), also for a turn that timed out |
| `verification` | Every test command the dispatcher runs after a writing turn (none when the [local tests](#local-tests) select nothing), and the full suite it runs before a review of a head without CI | Command, outcome, duration, the tree hash before and after the test, and the commit (none before a review) |
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
  [merge mode](#merge-modes) other than `off` merges with the [merger app](#the-merger-app),
  whose key is there too; without that app it would need a local `gh` login.

```bash
sudo useradd --create-home devloop
sudo -u devloop -H git clone https://github.com/brushtyler/ssdlc.git /home/devloop/ssdlc
sudo -u devloop -H install -d -m 700 /home/devloop/.ghapps   # then the keys, mode 600
# The first pin runs the clone's devloop; every later command runs the pinned copy.
sudo -u devloop -H sh -c 'cd ~/ssdlc && python3 -m devloop pin origin/main'
sudo -u devloop -H python3 /home/devloop/ssdlc/scripts/devloop-pinned run --interval 600
```

Turns must not depend on an update of an agent CLI in progress: a CLI that replaces its own
binary leaves, for a few seconds, nothing to start (#218; the start is retried once, see
[The loop](#the-loop)). For the CLIs devloop uses, either:

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
   regular file of the commit. The documents of the [documentation map](#documentation-map) are
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
python3 scripts/devloop-pinned run --interval 600
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
stays as it was, and the request stays: the next tick tries again (`run` logs the failure and
goes on; `tick` exits with it). With `auto = false` a merge requests nothing, and a request left
from before is ignored.

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
[stall check](#stalled-agents); other values stop devloop at startup. `max_parallel_actions`
(default 1) sets how many actions of a tick run at once ([Parallel actions](#parallel-actions)).
`max_open_prs` (default 2) and `max_parked_prs` (default 3), integers of at least 1, cap the
open PRs in progress and those waiting for a person ([Open PR limits](#open-pr-limits)).
`ci_rerun_unrelated` (a boolean, default `true`) re-runs once a CI failure in test modules not
pertinent to the PR ([CI failures not pertinent to the PR](#ci-failures-not-pertinent-to-the-pr));
another value stops devloop at startup.
`[merge]` (`mode`, `levels`, `operator_only_paths`) and `roles.supervisor` set the
[merge modes](#merge-modes); an unknown mode, level or key stops devloop at startup.
`[pin]` (`auto`, default `false`) sets the [pin after a merge](#pin-after-a-merge); a value that is
not a boolean, or an unknown key, stops devloop at startup. `[docs] map` sets the
[documentation map](#documentation-map). Operator overrides go into the
[local overlay](#local-config-as-an-overlay), not into this file.
`[review_policy]` sets the
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

### No background work in a turn

A headless turn ends when the agent gives its final answer, without waiting for anything it
left running (#219). An implementer that split its work among sub-agents in the background
once answered before they ended, and stopped with nothing written. So:

- **No sub-agents.** Every claude turn, write and review, runs with
  `--disallowedTools Agent Task Workflow`, added by devloop before `write_args` or
  `review_args`, whatever they say: the tools that start sub-agents can run them in the
  background, so they are denied altogether. The agent still reads and analyses on its own,
  one step after the other.
- **Told in the prompt.** The implement and fix prompts ask to complete all the work within
  the turn, in the foreground, without sub-agents or other background activity, and to give
  the final answer only when the work is finished.
- **A block for unfinished background work is a `tool_failure`.** A write turn that stops with
  `blocked` and a reason that, in the same sentence, cites background or parallel work
  (sub-agents, an audit in parallel) and says it was still running or ended after the answer
  is not published as `Lavoro bloccato`: it fails as an agent error, escalated as a
  `tool_failure` that `/devloop retry` can run again. A "before" counts only with a verb of
  completion ("prima che l'audit parallelo si concludesse"), and a reason that asks for a
  decision of the operator stays a `blocked`, whatever else it says.

### Parallel actions

`max_parallel_actions` (an integer of at least 1, default 1) sets how many actions of one tick
may run at once (#176). With 1 a tick runs its actions one at a time, in plan order, as before.
This repository sets 3: implement, fix, sync and review of different issues and PRs each take
10–25 minutes, and each has its own worktree.

- **What runs together.** Actions on different issues and PRs, up to `max_parallel_actions`
  at once. Two actions on the same number, or on a PR and its issue, run one after the other in
  plan order. A global action (a write to `main` or to the whole repository, such as a merge or
  a pin) runs alone: after every action planned before it has ended, and before any action
  planned after it starts. `max_actions_per_tick` stays the limit on the total, and every agent
  turn and test run keeps its usual timeout.
- **Level-2 reviewers.** A review action runs together every reviewer its round still waits
  for (see [Level 2](#level-2-two-independent-reviewers)), so a round no longer takes two
  ticks.
- **Failures.** A failed action does not stop the others, nor the next action on the same
  number. A failure of an agent, `gh`, git or the issue's trust escalates as before; an
  unexpected exception escalates its issue or PR too, with `errore inatteso del dispatcher` and
  the exception, instead of stopping the tick. At the end the tick logs one
  `outcome of <action> #<N>: done|failed: …|unexpected error: …` line per action.
- **Tick log.** Each line of an action starts with `#<N>`, its issue or PR, and is written
  whole: the lines of parallel actions never mix.
- **Shared state.** Worktrees, agent logs, markers and GitHub writes are already per number.
  git commands that update state every worktree shares (`fetch`, `worktree`) run one at a
  time, GitHub App tokens are minted under a lock, the ledger's trace of open turns is updated
  under a lock, and the telemetry of a turn (`last_report`) belongs to the thread that ran it.
  Each run of `test_cmd` gets a temporary directory of its own (`TMPDIR`), removed afterwards,
  so parallel test runs do not share one.

### Local tests

After every writing turn (implement, fix, sync with conflicts) the dispatcher runs tests in the
worktree before committing (#169). `local_tests` says which:

| `local_tests` | Local tests |
|---|---|
| `"full"` (default) | `test_cmd`, the whole suite, as before #169 |
| `"changed"` (this repository) | Only the test modules pertinent to the diff, each with `python3 -m unittest discover -s tests -p '<module>'`, one after the other, stopping at the first that fails |
| `"none"` | None |

Any other value stops devloop at startup. With `"changed"` the diff is the whole one from the
merge base with `origin/main` to the current content of the worktree: the branch's commits, the
index, unstaged changes and new files git does not ignore, computed again after every attempt.
The pertinent modules, among the `tests/test_*.py` files of the worktree, are:

- every changed or added `tests/test_*.py`;
- for every other changed `X.py` (or `pkg/X.py`), the modules `tests/test_X*.py`; for
  `ssdlc_<name>.py` also `tests/test_<name>*.py` (`ssdlc_impact.py` selects `test_ssdlc_impact*.py`
  and `test_impact*.py`);
- for any file under `devloop/`, the modules `tests/test_devloop*.py`.

When no module is pertinent, for example when only documentation changed, and with `"none"`,
no test runs locally and the PR goes on to the CI. `implement_test_retries` applies to the tests
that ran: a failure goes back to the implementer with the output, and after the last attempt the
issue or PR goes to `agent:needs-human`, as with `"full"`.

**The CI stays the gate.** A defect the local modules miss becomes a CI round. When the full suite
did not run locally (`"changed"` or `"none"`), a review needs the CI passed: if the head has no
CI at all, the first reviewer's action of the round runs `test_cmd` locally, in a disposable copy
of the PR worktree, before the review, and a failure sends the PR to `agent:needs-human` without a
review. With `"full"` the gate is unchanged: CI passed or absent.

The mode and the modules run (`local_tests`: `{mode, modules}`, only `{mode}` with `"full"`) go to the tick log and, as
information only, to the markers of implement and fix: they are not part of the policy receipt
and no approval depends on them.

### CI failures not pertinent to the PR

A CI failure normally becomes a round (`ci_failed`). A failure that does not depend on the PR,
such as a flaky test already on `main`, would spend one for nothing. So at the **first** CI
failure of a head the dispatcher first reads, in the log of each failed job on its own
(`gh run view --job <id> --log-failed`, whole; the job is the one in the check's link), the test
modules that failed: `scripts/run-tests.py` prints `FAILED MODULE <name>` for each of them and a
final `FAILED (modules=N, worker problems=M)` (#200). Every failed job must say it: a run where
the suite fails only in modules not pertinent to the PR and another job fails without naming any
(such as a benchmark) is not determinable.

- **None pertinent.** If no failed module is pertinent to the PR's diff, by the same
  correspondence between sources and tests as `local_tests = "changed"`
  ([Local tests](#local-tests)), the dispatcher re-runs the failed jobs once
  (`POST /repos/<repo>/actions/runs/<id>/rerun-failed-jobs`) and opens no round. A dispatcher
  comment with a `rerun` marker records the head, the runs, the modules and the links of the
  failed checks; the ledger, when on, records a `ci_rerun` event.
- **Pertinent, or not determinable.** A failed module pertinent to the diff, a failed check that
  is not a GitHub Actions job (a commit status), a failed job whose log lacks the
  `FAILED MODULE` lines or the final summary, a count that does not match, a worker problem: the
  failure is a round at once, as before, and nothing is re-run.
- **While the re-run starts.** As long as GitHub still shows the very failed checks the re-run
  replaces (same links: a re-run job is a new check run), the dispatcher waits: no round, no
  review, no merge. A ready PR stays in `agent:ready-to-merge` meanwhile, and goes back to
  `agent:review` as for any CI running again on the approved commit.
- **Second failure.** One re-run per head: if the re-run fails too, the failure is a normal
  `ci_failed`, which counts as a round, and its comment cites the re-run (runs and modules). A new
  head starts again from its first failure.

The rerun needs **`actions: write`** on the dispatcher's app. Its token is minted with only that
permission. Without it (GitHub answers 403, or refuses to mint the token; also when the
dispatcher has no app and uses a `gh` login without the permission), or on any other GitHub error,
the dispatcher writes one line to the tick log (`CI rerun refused, …`) and the failure is a round,
as before. `ci_rerun_unrelated = false` turns the re-run off; the default is `true`. A marker of
the `rerun` kind counts only when the dispatcher's identity wrote it.

Only a `ci_failed` is re-run this way: a CI failure on a PR that has no round left goes to
`agent:needs-human` (`round_limit`), as before.

### Documentation map

`[docs] map` (#203) maps code paths to the documents that describe them, so that the documents
follow the code. It is the documentation phase of devloop, the `07-docs` of an SSDLC run:

```toml
[docs.map]
"devloop/" = ["docs/devloop.md"]
"orchestrator.py" = ["README.md", "docs/architecture.md"]
"schemas/" = ["docs/configuration.md"]
```

A key is a directory prefix ending in `/` (it covers every file under it) or a file, matched
exactly; both are relative to the root. A value is a non-empty list of documents. devloop stops at
startup on an absolute path, an empty key, list or document, a `.`, `..` or empty segment, a
backslash or a pattern character (`*`, `?`, `[`, `]`: `ssdlc_feedback*.py` is written as one key
per file), and a directory given as a document (a value ending in `/`). Loading the configuration
checks its shape only, not whether the documents exist (#210): they belong to the repository
devloop works on, not to the installation, and a [pin](#pin) does not extract them into the copy.
The [local overlay](#local-config-as-an-overlay) may add or replace keys.

The prompts list the documents mapped to the files of the diff, the same diff the
[local tests](#local-tests) use: from the merge base with `origin/main` to the current content of
the worktree, read again for each prompt. A mapped document that is not a regular file of that
worktree (absent, a directory or a symlink) is omitted from the list, with a line in the log
(`docs.map: <document> is not a file of the worktree; omitted from the prompt`); a key of the
whole map left without documents is omitted too. It is not an error.

| Prompt | Documents |
|---|---|
| First implement attempt | The whole map, since the diff is still empty: the implementer applies it to its own diff before finishing |
| Later implement attempts (after failed local tests), fix | The documents mapped to the current diff, each with the files it comes from; the implementer updates those whose described behaviour changes |
| Review | The documents mapped to the PR's diff. A documented behaviour that the change alters without updating its mapped document is a **blocking** finding; a mapped document that stays correct without changes is noted in `checks_performed` |

A file that no key covers asks for nothing, and a diff without mapped files adds no section to
the fix and review prompts. The map adds no turn and changes no budget or round. It grows with
later issues.

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
- **Told in the prompt.** The prompt of every agy turn, write and review, ends with a
  `## Shell commands` block (#155) generated from the same list as `--settings`: the exact
  prefixes allowed in that mode, the instruction to read and search files only with agy's own
  file tools, with paths relative to the working directory and without `grep`, `ls`, `cat`,
  `find`, `sed` or other shell commands, and the warning that a command outside the list ends
  the turn with no result. The same block (#180) asks to run commands so that their output stays
  short and complete (one test module at a time, with `-q`; a truncated output is rerun on a
  narrower scope) and never to read logs or files outside the working directory, the logs of
  agy's own background tasks included. The prompt file in the turn's log includes it. The other
  agents' prompts do not get it.
- **No output is an error.** An agy turn that exits with no output, or with
  `jetski: no output produced`, fails with `<agent> stopped without output: <message>`, which
  goes to the turn's log and to the `agent:needs-human` comment. When agy's JSON lists
  `denied_actions`, the message ends with `; denied_actions: [...]` (#155), so the escalation
  says which action was denied. It is never an empty result; with exit 0 it never counts as an
  unavailable model either, so it does not start the fallback. The message counts as the CLI's
  diagnostic on stderr, or on stdout only as a line of its own: a structured answer that quotes
  it is still a valid answer.
- **An empty run moves to the next model.** A turn that exits 0 with an empty stdout and
  `run ended with no output` on stderr (agy's `warning: run ended with no output and no recorded
  error`), or with a JSON envelope that has no `structured_output`, no error and no
  `denied_actions`, is an empty run (#222): the model was not usable for that turn. It fails
  with `agy returned no output: <warning>` or `agy returned no structured_output`, and the next
  model of the [fallback chain](#fallback-models) runs the turn, as for an unavailable model;
  the tick log says `agy: model <model> returned no output; trying <next model>`. On the last
  model of the chain, or on an agent without fallback models, the same model runs the turn once
  more, with `-retry` in its log name; a second empty run escalates (`tool_failure`) with
  `; no output again after one retry`. An empty run may be transient, so it is never recorded
  in the [memory of unavailable models](#memory-of-unavailable-models). A stop with
  `jetski: no output produced`, `denied_actions` or nothing at all on stderr is not an empty
  run and keeps the path described above.
- **A denied permission is retried once.** When that diagnostic names the permission (`a tool
  required the "read_file" permission …`) and the turn exited 0, the failure is the tool's,
  not the change's (#180): the same model runs the same turn once more, in the same directory
  and tick, with `-retry` in its log name. devloop's log reports the permission and the last
  action agy's JSON lists in `denied_actions`, or `target non riportato da agy` when there is
  none. A second denial on the same model escalates as before, and the comment ends with
  ``permission `<name>` denied again after one retry``. Any other failure (an unavailable
  model, a non-zero exit, a stop that names no permission, invalid output, a timeout) is not
  retried: it escalates or starts the fallback as before, except an empty run (see above). After a fallback, the next model gets
  its own single retry. The [readiness check](#readiness-check-before-implementation), which
  has no fallback, gets the same single retry, in the same checkout, with its own log and turn
  record (`issue-<n>-preflight-retry`).

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
  `agent:needs-human`. An agy [empty run](#agy-permissions) (exit 0, no result, no error) also
  moves to the next model, without being remembered.
- **Once.** Each model is tried at most once per action, but for the single retry of an agy
  denied permission or of an empty run on the last model: a rate limit the CLI reports as
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
  as such. When the reviewers of a round run together ([parallel actions](#parallel-actions)),
  each reserves the profile it runs with before any turn starts, and a fallback replaces the
  reservation atomically: a fallback must also be distinct from the profiles the other running
  reviewers reserved, otherwise it is skipped and the next model is tried. When no model is
  left, that reviewer fails and the round waits for it, without voiding the other verdict.

#### Memory of unavailable models

A model found unavailable stays unavailable for a while: a quota announces when it resets,
sometimes days later. So that each turn does not try it again first, devloop remembers it (#151):

- **What.** When a turn of an agent with `fallback_models` fails because its model is
  unavailable, devloop records the pair (agent key, model) in
  `<state_dir>/model-unavailability.json` of the main checkout, with the time, the reason and
  the time the model is expected back. The record is per agent: two agents with the same model
  have records of their own. It survives a restart of the dispatcher.
- **Until when.** If the text of the error contains `Resets in <duration>`, the duration being
  whole hours, minutes and seconds, in this order, each optional and at least one present
  (`152h4m35s`, `4m`, `30s`), the model is skipped until the time the error was detected plus
  that duration. Any other format counts as no indication: the model is skipped for
  `fallback_memory_seconds` of the agent (default 3600, at least 1). An agy empty run (#222)
  is never recorded: it may be transient.
- **During.** A turn of that agent skips a remembered model without running it and starts from
  the first model of the chain that is not remembered, configured model or fallback alike. The
  tick log says so (`<agent>: model <model> skipped: unavailable until <time> (<reason>)`), the
  `pr`, `review`, `fix` or `sync` marker of the turn lists it in `skipped_models` with the
  reason and the time (informative only: no receipt, independence check or approval reads
  it), the comment of a failure lists it among the models tried, and `devloop status` shows
  each remembered model with its time and reason. When every model of the chain is
  remembered, the turn runs no CLI: it fails as when no model is available, and the comment
  lists each model with its reason and the time it is expected back.
- **After.** Once that time has passed, the model is tried again. A new failure records it
  again, with a new time; a turn that succeeds on it forgets it.
- **Reset.** `devloop fallback reset <agent>` forgets every model remembered for that agent,
  and only those: its next turn starts from the configured model.
- **Without fallback models** nothing is recorded and nothing is skipped: the agent behaves as
  before.

```toml
[agents.agy]
fallback_memory_seconds = 3600  # when the error does not say when the model is back
```

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
  model of an agent with [fallback models](#fallback-models), which runs the turn again on the
  next one.
- **Single host.** One dispatcher runs at a time. With `max_parallel_actions = 1` (the
  default) a tick runs its actions one at a time; above 1, actions on different issues and PRs
  run in threads of that one process ([Parallel actions](#parallel-actions)).
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
