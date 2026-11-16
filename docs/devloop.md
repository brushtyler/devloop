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

Any step can stop at `agent:needs-human` with a comment explaining why: the agent declared itself
blocked, tests still fail after the retry, the reviewer asked for a decision, the round limit
was reached, the reviewer pool has not enough independent reviewers for the review level, an approval left open a finding of another owner, the
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

Nothing lowers a level, and nothing takes it below 1. The level is computed again on every new
head, before each review, CI round and promotion.

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

### Receipts

Every agent review, CI round and promotion records a **receipt** in its marker:

| Field | Content |
|---|---|
| `pr`, `base`, `head` | The PR, the base branch commit and the head the level was computed for |
| `policy` | The policy version: the SHA-256 of the loaded policy configuration, including key, app, kind, model, effort and `review_args` of every reviewer of the pool |
| `level`, `reasons` | The effective level and why: default, label with its author, sensitive paths, earlier level |
| `reviewers` | The reviewers the level requires, in order (none when the pool has not enough independent ones) |
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
published while it keeps failing. Text the dispatcher writes on its own, such as escalations and CI
rounds, names `devloop dispatcher` as the agent and no model.

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
python3 -m devloop setup            # create the agent:* labels and the review level label
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
PR, the agent and the path of its prompt once an agent turn starts, the start time and the
dispatcher's process id. The record is removed when the action ends, also when it fails.
`devloop status` shows it first, with how long it has been running. If no dispatcher holds the
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
without `--ephemeral`: Codex saves the session of every turn in `~/.codex/sessions`, and these
transcripts stay there after the turn.

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
with `--config`. Among the limits, `agent_stall_seconds` (an integer of at least 1, default 900)
and `agent_stall_action` (`"warn"`, the default, or `"abort"`) control the
[stall check](#stalled-agents); other values stop devloop at startup. `[review_policy]` sets the
[review level](#review-level-and-policy-receipt): `default_level` (1 or 2, default 1), the level
`label` (default `reviews:2`), `sensitive_paths` and `sensitive_path_lists`; changing any of them,
or the reviewers of the pool (their order, app, kind, model, effort or `review_args`), changes
the policy version, so pending approvals need a new review.

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
| agy | `plan` mode |

Only codex has an OS-level sandbox. claude and agy are limited by their tools and modes, not by
the system: the tests they run can still reach the network and write outside the copy, as any
code running as the same user. For them, the copy and the final check catch writes to the
reviewed tree, not access to the rest of the system. With the network disabled, codex cannot run tests
that open TCP sockets, even on loopback.

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
  goes to `agent:needs-human` rather than being retried.
- **Single host, sequential.** One tick runs actions one at a time.
- **`gh` compatibility.** Checks, labels, comments and PR creation go through the REST API
  (`gh api`), so devloop does not depend on newer porcelain flags such as `gh pr checks --json`.
  For commit statuses the aggregate state counts, including contexts beyond the first page.
