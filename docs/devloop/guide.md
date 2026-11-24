# devloop: operator guide

devloop moves issues through implementation, independent review and fixes, so that a human only
decides what starts and, unless a [merge mode](#merge-modes) lets devloop merge, what gets
merged (see the [README](../../README.md#components)). The local SSDLC engine that it worked
next to left `main` with R1 of [ADR 0007](../adr/0007-da-ssdlc-a-devloop.md). Today devloop is
configured for this repository only, and it never touches the main checkout's working tree.

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
| CI | Checks pending | The review does not wait for them (#271); failing checks become a review round sent back as `agent:changes`, unless no failed test module is pertinent to the PR: then the failed jobs are re-run once, without a round (see [CI failures not pertinent to the PR](reference.md#ci-failures-not-pertinent-to-the-pr)) |
| Review | CI pending, passed or absent, not failed (absent CI with `local_tests` other than `full`: the full suite first runs locally, except within the [window of a new head](reference.md#ci-of-a-new-head), see [Local tests](reference.md#local-tests)) | The dispatcher computes the PR's [review level](reference.md#review-level-and-policy-receipt); at level 1 a **different** agent posts a structured verdict (see [Structured review](protocol.md#structured-review)), at level 2 two [independent reviewers](reference.md#level-2-two-independent-reviewers) do, one per action, and their verdicts are applied together |
| Fix | `agent:changes`, or a [`/devloop fix`](#fix-requested-by-the-operator-or-the-supervisor) command on a PR in `agent:review` or `agent:changes` | The implementer fixes or disputes every open finding of the round, and applies the requests of the command, then back to `agent:review` |
| Sync | A PR in `agent:review` or `agent:ready-to-merge` conflicts with `main` | The implementer resolves the conflicts of a merge of `origin/main`; the dispatcher pushes the merge commit, back to `agent:review` (see [Conflicts with `main`](protocol.md#conflicts-with-main)) |
| Approve | Verdict `approve` from every reviewer the level requires | `agent:ready-to-merge`, only if no [finding](protocol.md#findings-and-who-closes-them) is still open, the PR head is still the reviewed commit, CI passed and the policy receipt recomputed just before promotion matches the one of every approval (an earlier base is admitted only if `main` left the PR's files alone since, see [When `main` advances](reference.md#when-main-advances-during-a-round)), and the [receipt of the PR](protocol.md#pr-receipt) does not contradict the markers; the dispatcher then writes that receipt in the comment of the promotion, where a merge by devloop adds its record too |
| Level 0 | A trusted user's `reviews:0`, the whole diff in the operator's allowlist, every expected check `success` | `agent:ready-to-merge` without a review (see [Level 0](reference.md#level-0-no-review)); off by default |
| Merge | `agent:ready-to-merge` | **A human merges**, unless the [merge mode](#merge-modes) of the PR lets devloop merge |

The review starts while the CI of the head runs (#271), and the reviewer is told the CI is
`pending`. The merge still waits for that CI: an approval leads to `agent:ready-to-merge` only
once the CI of the **same head** passes, without a second review. If that CI fails, the approval
does not count for the merge: the failure joins the round of that approval as a `ci` finding,
without a round of its own (one head, one round, #359), the round's verdict becomes
`request_changes` and the PR goes to `agent:changes` for the fix (or its failed jobs are re-run
once, when no failed test module is pertinent to the PR). The same holds when the CI of the
approved head fails with the PR already in `agent:ready-to-merge`. A round where a reviewer asked
for the operator keeps its escalation. A review that asks for changes while the CI runs sends the PR to
`agent:changes`, but the fix waits for that CI: if it fails, the failure joins the round of that
review as a `ci` finding, without a round of its own (one head, one round, #352), and the same
fix answers the reviewer's findings and the CI failure together. A CI that already
failed before the review sends the PR to the fix without a review, as before. A PR with the [level 0](reference.md#level-0-no-review) label, when level 0
is enabled, has no review to start early: it still waits for its CI.

The review and fix prompts carry a summary of the CI of the head, read by the dispatcher: each
job with its outcome and duration and, for a job that runs `scripts/run-tests.py`, its final
`Ran N tests in S (…)` and `Slowest` lines (#300), when the project declares that format of its
CI logs (`[tests] ci_log_format = "run-tests"`, ADR 0006, R5). The agents, without network, can use those
measures instead of asking the supervisor for them; a job log that cannot be read only leaves
its summary out (see [Summary of the CI in the prompts](reference.md#summary-of-the-ci-in-the-prompts)).

The review prompt does not say who wrote the change (#389): no implementer key or model, no
attribution header or marker in the issue, the PR description and the comments it carries, and
an explicit statement that the reviewer is not the author (see
[What the reviewers read](protocol.md#attribution)). devloop still keeps the implementer out of
the reviewers, from the PR's marker.

A blocking finding carries its evidence (#481): the scenario that shows it, or the code and the
rule it violates with the quotation. One without evidence is recorded as non-blocking and does
not send the PR to the fix, unless it is a security finding, which stays blocking. A reviewer
left with only such findings gives the neutral verdict `unproven`, which is no approval: the PR
is not promoted without the explicit approvals of the quorum, and gets a new review. The fix
prompt asks the implementer to verify the evidence before fixing, and to dispute a finding whose
evidence does not hold; the reviewer who opened it then withdraws it or answers with new
evidence (see [Evidence of a blocking finding](reference.md#evidence-of-a-blocking-finding)).

### Changes to the trust policy

The trust policy decides who devloop believes and when a change is merged, and only the operator
changes it (decision of 2026-11-18, ADR 0005, section 4, #399). The review prompt
(`devloop/prompts/review.md`) and the advisory review prompt (`devloop/prompts/advisory.md`)
carry the same rule, at every review level: a change touches the trust policy when, and only
when, it does one of these five things (a closed list):

1. it changes the value, the default or the way devloop reads `trusted_users`,
   `operator_only_paths`, `protected_paths` (which keeps the branches out of `.github/`, the CI
   the merge depends on), `[merge] mode`, `[merge.levels]`, the levels and paths of
   `[review_policy]` (`sensitive_paths`, `sensitive_path_lists`, `zero`, `advisory_reviewer`),
   `[roles]` (`implementer`, `reviewers`, `supervisor`), `conclave` (which moves the decision on
   a block from the operator to three agents), `max_review_rounds`, `max_requested_fix_rounds`,
   `[pin] auto`, `[apps]`, or
   `write_args`, `review_args` and the other permissions of the agents;
2. it changes the constants that cap the delegations and the retries, such as `MAX_RETRIES`;
3. it changes who is believed: the filter of the trusted authors of the comments, the check of
   the identity of markers and records, the identity required for `/devloop` commands, the names
   by which devloop recognises trusted commands and records (#417): renaming, removing or
   changing the meaning of a `/devloop` command or of a kind of the `<!-- devloop:<kind>:v1 ... -->`
   markers;
4. it changes when a PR is merged or promoted, or who decides on it: the merge conditions, the
   quorum of the reviewers, the checks of `operator_only_paths` and `protected_paths`, the choice
   of the review level, the classes of escalation the supervisor decides in `auto` mode
   (`NON_CRITICAL` in `devloop/supervision.py`) and those that stay with the operator: moving
   `reviewer_decision` among the non-critical ones, for example, would take from the operator
   the very `needs_operator` of this rule. So do the names of the labels that govern merge, hold
   and escalation, defined in the `Labels` dataclass of `devloop/config.py` and not configurable
   (`agent:ready-to-merge`, `agent:merge-hold`, `agent:needs-operator`,
   `agent:needs-supervisor`, #417): renaming, removing or changing the meaning of one of them
   would bypass the hold or the escalations, because devloop would look for a name nobody puts
   any more;
5. it changes one of these prompts in `devloop/prompts/`: the review prompt, the advisory review
   prompt, the conclave prompts (`conclave_proposal.md`, `conclave_vote.md`, which define the
   perimeter reserved to the operator), the parts of `implement.md`, `fix.md` and `preflight.md`
   that instruct the agents on untrusted content and on trust rules, or the rule itself.

Nothing else is policy, unless it also does one of the five things: refactoring without a change
of behaviour, new functions, texts and comments, operational keys such as timeouts, capacities
and intervals. The prompts give examples of both. A change that is not policy is not reported.

**When the operator decides** (#447). The operator decides the policy, not every change that
carries it out ([ADR 0002](../adr/0002-ruoli-e-responsabilita.md): the operator takes the
reserved decisions, devloop and the agents carry them out). A change that touches the policy
needs the operator when, and only when, at least one of these three things holds:

1. it goes beyond or against a decision of the operator, or no decision covers it: a decision
   recorded in the section "Decise dall'operatore" of an ADR approved with the
   [cycle of the ADRs](../adr/README.md#ciclo-di-un-adr) and present on `main`, written by the
   operator in the body of the issue, or a `/devloop approve` of the operator on the issue (#472,
   #483, see below); the points an ADR leaves open are not decisions;
2. it weakens a check and no decision of the operator authorizes that weakening;
3. it changes the rule itself: the "Trust policy" section of the review or the advisory review
   prompt, or the conditions that decide when a change needs the operator, even when an ADR asks
   for it.

Since #472 point 3 is the rule alone. A change to the classes of escalation (`NON_CRITICAL` in
`devloop/supervision.py`), to a `/devloop` command or to a condition of the merge that a decision
of the operator **prescribes explicitly**, naming the class, the command or the condition, is a
faithful implementation and follows point 1: adding to `NON_CRITICAL` the class
`round_grantable` that ADR 0005 names (D2, row P5-1 of its plan) does not go to the operator. What
the decision does not name, such as another class added beside it, still does, under point 1.

Since #483 the prompts list explicitly what point 3 always keeps with the operator, even when a
decision of the operator, a `/devloop approve` on the issue included, prescribes it: the quorum
and the choice of the reviewers, the checks of `operator_only_paths` and `protected_paths`, the
names of the labels of hold, of escalation and of merge, `trusted_users`, and the "Trust policy"
section itself. A PR that lowers the quorum of a level, or narrows the `operator_only_paths`
check, goes to the operator for the merge whatever the issue approves; the prompts give it as an
example.

A **faithful implementation** does not need the operator: the issue cites the section of an ADR
on `main` (or the decision is in the body of the issue or in a `/devloop approve` of the operator
on it), and the change carries out the decision with no deviation. The reviewer checks the
conformity of the whole change to the decision, read on `main` and never in the diff of the PR,
and cites it in the review (file of the ADR, section and point, or the author and time of the
operator's command); its verdict is then the technical one, `approve` or `request_changes`, and devloop
merges the PR like any other of its level. Without the citation the PR needs the operator as
before; every deviation found needs the operator, with its reason and the decision it departs
from. The prompts give an example of each case: a conforming implementation of an ADR, a class
of `NON_CRITICAL` an ADR names, a deviation, a weakening without a decision, a change of the
rule. Unchanged: `operator_only_paths` (`docs/adr/`, `AGENTS.md`, `CLAUDE.md`) keeps the merge
with the operator whatever the reviewers say, the activations of ADR 0005 (D10) are not PRs and
stay with the operator, and a `needs_operator` for a `decision` works as below.

**The decision on the issue, not on the PR** (#472, decided by the operator on 2026-11-19,
[ADR 0005](../adr/0005-deleghe-e-ruoli-degli-agent.md#4-decisioni-riservate-alloperatore), D10).
The operator approves the policy once, on the issue, before the implementation, and not the code
of every PR:

- **Approval with `/devloop approve`** (#483). A decision of the operator on the issue is a
  command: a comment whose first line is `/devloop approve`, followed by the text of the decision
  (on the same line or below), for example:

  ```text
  /devloop approve
  Approvo la modifica della policy di fiducia descritta qui, punti 1 e 2.
  ```

  It counts for point 1 when a user of `trusted_users` wrote it in person (not starting with the
  attribution header), not from the supervisor's identity nor from any app, after the latest
  change of the title or the body of the issue and before devloop took it on (the first
  `labeled` event of `agent:working` after that change). Any other comment, an approval written
  without the command too, is context for the implementer and authorizes nothing. devloop lists
  the commands that count, with author, time and permalink of the comment (#506), under
  "Decisions of the operator on the issue" in the prompts of the readiness check, of the
  reviewers and of the advisory review; each covers
  only what its text approves, on the conditions it sets, and that is the agents' to judge. A
  change of the text after the command drops it; so does a change of the comment itself after
  the takeover (its `updated_at`), since the agents read it as it is now. The reviewers of a PR
  whose issue was not taken on after the latest change get none. A `/devloop approve` of the
  supervisor or of an app, one after the attribution header, one not on the first line and one
  without a text are refused, and devloop answers each once, with the reason: in the comment of
  the next readiness check, or, when none is due (a command of an app changes nothing that runs
  one), in a comment of the dispatcher. The command of a user outside `trusted_users` is ignored.
- **Complete history** (#483). The bounds above come from the history of the issue, which
  devloop reads in full, 100 events at a time. When the read stops before the last page, no
  command counts.
- **Readiness check.** With `preflight` configured, the check applies the same rule to the issue
  and declares in `trust_policy` whether the change the issue asks for touches the policy:
  `none`, `covered` (a decision of the operator covers it, cited in `trust_policy_reason`) or
  `uncovered`. An `uncovered` issue is not ready: devloop puts first among its gaps one of kind
  `operator_decision` with the question "Approvi la modifica della policy di fiducia descritta
  qui?" and the reason, and the issue goes to `agent:needs-operator` with class
  `operator_decision`, to the operator also in `auto` mode
  ([Readiness check before implementation](#readiness-check-before-implementation)). The
  operator answers with a `/devloop approve` on the issue: the hash of the request changes and
  the check runs again. The hash also carries the commands that record a decision, by author,
  time and text: a change of the text undone later leaves title and body as they were but drops
  those commands, so a check that relied on them is not reused and runs again.
- **The merge.** With that decision, a faithful PR with no open finding and green CI is merged by
  devloop like any other of its level, except for the paths of `operator_only_paths`. What no
  decision covers, what goes beyond or against one, an unauthorised weakening and a change of the
  rule itself stay with the operator as above.

- **Review of the quorum.** A reviewer for whom the change needs the operator gives the verdict
  `needs_operator`, even when the change is correct and the issue asks for it, and writes in
  `human_reason` the parts touched, each with its point of the list and its file, and which of
  the three points holds. The review prompt asks to assess the rule again at every round, on the
  whole diff of the PR against its base and not only on what the latest fix changed: a PR already
  flagged stays `needs_operator` after a fix, as long as its diff still needs the operator.
- **One hand-off to the operator** (#434). The reviewer says why it asks for the operator in
  `operator_reason`: `trust_policy` for this rule, `decision` for anything else (an ambiguous
  issue, a product or authority decision, a disagreement that cannot be settled technically), and
  lists its blocking findings in both cases. A `trust_policy` does not stop the loop: while
  blocking findings of that reviewer stay open the PR goes to `agent:changes` for the fix, as with
  `request_changes`, with no escalation and within `max_review_rounds` as usual. With no blocking
  finding of any reviewer of the quorum left open, and CI green on the same head, the round counts
  as approved: the PR is promoted to `agent:ready-to-merge` and gets `agent:needs-operator` with
  the [operator merge](#ready-prs-whose-merge-is-the-operators) notice, written once per head, that asks
  for the operator's review and merge and lists the parts of the policy each reviewer named.
  Once a reviewer of the PR has given `trust_policy`, devloop does not merge the PR itself,
  whatever `[merge]` says, with one exception decided by the operator on 2026-11-19 (#461,
  [ADR 0005](../adr/0005-deleghe-e-ruoli-degli-agent.md#4-decisioni-riservate-alloperatore),
  D10): the `trust_policy` of a reviewer no longer holds the merge when that same reviewer
  approves the head of the latest round, it closed its own blocking findings itself with its
  reviews (not the operator with `/devloop finding`, nor the CI), and no reviewer of that round
  gives `trust_policy`. It is the case of a PR that broke a rule in its first round and then
  carried out the decision faithfully: devloop merges it like any other of its level. Otherwise
  the PR goes to the operator as above, and the notice lists the parts each reviewer whose
  `trust_policy` still holds named in its latest one: on the approved head when a reviewer of
  that round gives it. A `decision`, or a
  `needs_operator` without `operator_reason` (a reviewer or a record from before #434), is an
  immediate escalation as before, of class `reviewer_decision`, which is critical and so stays
  with the operator in `auto` mode too
  ([Escalations to the supervisor](#escalations-to-the-supervisor)); only a `decision` suspends
  a round or opens a [conclave](#conclave-on-a-block). With a conclave configured, trust rules are
  in its reserved perimeter.
- **Advisory review.** It has no verdict and does not count for the quorum
  ([Advisory review for the operator](#advisory-review-for-the-operator)): when the change needs
  the operator, its opinion is `dubbi`, or `contrario` when the change weakens a check, and the
  first of its residual risks begins with "Policy di fiducia toccata:" and lists the parts
  touched; a faithful implementation gets an opinion on its merits, with the decision cited in
  the summary. Its schema does not change.

**Residual risks**, accepted by the operator (ADR 0005, section 4):

- the rule depends on the attention of the reviewers, at every round: at level 2 they are two,
  of different vendors, but they may both miss a change, and a reviewer of a later round who
  looks only at the latest fix may approve a PR an earlier round flagged; since #461 that
  approval, by the reviewer who flagged it, also gives the merge back to devloop. Since #447
  they also judge whether a change conforms to the decision it cites, and since #472 whether a
  decision names what the change adds and whether a comment of the operator approves it; the
  readiness check's `trust_policy` is as good as the attention of its agent. devloop checks
  nothing of it in code beyond who wrote a comment and when: no file is reserved to the operator
  for the policy, and `devloop/config.toml` is not in `operator_only_paths`;
- a change deliberately disguised as something else is not covered;
- the operator's local overlay (`config.local.toml`) can weaken the policy without any review;
- with a conclave configured, a change to the policy stays with the operator only if its members
  recognise it as a trust rule of the reserved perimeter;
- a PR that adds this rule, or changes it, is judged with the prompts of the running
  installation, which may not know the rule yet: such a PR is held for the operator by hand
  ([Holding a merge](#holding-a-merge)).

A head just pushed, without CI yet, is reviewed as if its CI were pending: with `local_tests`
other than `full` no full suite runs locally in place of the CI for `[ci] grace_minutes`, and as
long as `main` requires checks up to `[ci] required_timeout_minutes` (see
[CI of a new head](reference.md#ci-of-a-new-head)). A head in conflict with `main` gets no CI
from GitHub, so it is never waited for (#392): the fix of a PR in `agent:changes` starts at once
and aligns the branch with `main`. The ruleset of `main` still requires its checks for any merge
on GitHub.

Readiness belongs to the approved commit. Every tick re-checks ready PRs:

- if the head is no longer the approved commit, the approval is revoked and the PR goes back to
  `agent:review` for a new review;
- if CI is running again on the approved commit, readiness is suspended (`agent:review`) and the
  recorded approval is applied again, without a new review, once CI passes and if its policy
  receipt still matches;
- if CI fails on the approved commit, the failure joins the round that approved it as a `ci`
  finding, without a round of its own (one head, one round, #359): the approval decays and the
  PR goes to `agent:changes` for the fix. An approval already void, without a policy receipt or
  with a receipt found stale, still takes a CI round of its own; a
  [level 0](reference.md#level-0-no-review) promotion goes back to `agent:review` instead. When
  no failed test module is pertinent to the PR, the failed jobs are re-run once first
  ([CI failures not pertinent to the PR](reference.md#ci-failures-not-pertinent-to-the-pr)).

Any step can stop at `agent:needs-operator` with a comment explaining why: the
[readiness check](#readiness-check-before-implementation) found the issue not ready, the agent declared itself
blocked, tests still fail after the retry, the reviewer asked for a decision, the round limit
was reached, the reviewer pool has not enough independent reviewers for the review level, a
[level 0](reference.md#level-0-no-review) head misses an expected check, has an open finding or a standing
request for changes, an approval left open a finding of another owner, the
owner of a finding cannot be reconstructed from the markers, a sync with `main` failed, an agent, `gh` or `git` call failed or timed out, an agent
stalled with `agent_stall_action = "abort"` (see [Stalled agents](#stalled-agents)), the issue text was changed by an
untrusted user after `agent:ready`, or the PR body lost its marker. While `agent:needs-operator` is
set devloop ignores the PR or issue, so to resume: fix the cause, **remove `agent:needs-operator`**
and set the label that should run next (`agent:ready` on the issue, `agent:review` or
`agent:changes` on the PR). Every escalation comment repeats this. When the escalation records a
step that can run again, a trusted user may instead answer with `/devloop retry`, and the comment
says so ([Commands on escalations to the operator](#commands-on-escalations-to-the-operator)). The only exception is the
`agent:needs-operator` the dispatcher adds to a ready PR it does not merge (see
[Ready PRs whose merge is the operator's](#ready-prs-whose-merge-is-the-operators)). Every escalation has a
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
  from the supervisor's GitHub App (the app of that agent, or with `[roles.supervisor]` as a
  table the role app of [The supervisor app](reference.md#the-supervisor-app)), in any merge
  mode. A command from anyone else is ignored and
  logged, and so is text devloop publishes with a record.
- It applies to a PR in `agent:review` or `agent:changes`. On a PR in `agent:needs-operator`,
  `agent:needs-supervisor` or `agent:ready-to-merge` nothing runs.
- The next tick runs a fix of the **current head** with the request as a requirement, before any
  review. In `agent:changes` the fix also answers every finding still open. Every rule of a
  fix holds: [local tests](reference.md#local-tests), `protected_paths`, push with the implementer's
  identity, back to `agent:review`. The fix writes no review record, so it does not count
  against `max_review_rounds`; the review that follows is a new round as usual. That review
  does not count either, and starts also at the limit, when the command came from a user in
  `trusted_users` on a head the quorum had approved with no finding open, at most
  `max_requested_fix_rounds` times per PR (2, which the overlay may only lower); otherwise,
  and beyond that, it counts (see [Rounds](protocol.md#rounds), #536).
- **One fix per head.** Every command written before the fix runs is answered by that one fix.
  A command on a head where a requested fix already ran (for example one that changed nothing)
  is ignored and logged until a new commit reaches the PR, so that commands cannot loop.
- The fix record (`fix` marker) cites the comments that asked for it (their ids, authors and
  requests) and the head it ran on, and belongs to the latest round (round 0 before any
  review). The next review sees the request among the comments of the trusted users, as for
  any comment, and in the previous rounds of its prompt.
- A fix that stops at `agent:needs-operator` (the agent is blocked, tests still fail) records
  nothing, so the command is still to apply when the PR is resumed in `agent:review` or
  `agent:changes`. To drop it, delete the comment or remove its first line.

### Resuming after a `needs_operator` verdict without code changes

A verdict is bound to the reviewed commit. Back in `agent:review` with the same head, devloop
applies the recorded verdict again without a new review, so a `needs_operator` verdict sends the
PR straight back to `agent:needs-operator`. When the decision the reviewer asked for needs no code
change (for example a linked issue was opened, or the PR description was updated), ask for a
new review of the same commit:

1. A user in `trusted_users` comments on the PR with `/devloop rereview` as the **first line**.
   The lines below it can explain the decision.
2. Remove `agent:needs-operator` and set `agent:review`.

The next tick runs a new review. It is a new round: like any review it may start while the CI
of the head runs, and an approval leads to `agent:ready-to-merge` only once the CI of the same
head passes; it does not start once `max_review_rounds` is reached. The reviewer's prompt includes every
comment the trusted users wrote after the previous verdict. Only a request written after the
latest verdict counts, so it starts one review: once the new verdict is recorded, that verdict
is replayed again until another request. The request is ignored when it comes from anyone
outside `trusted_users`, when the command is not the first line, and in text devloop publishes:
that text starts with the attribution header, also when an actor without a GitHub App publishes
with the operator's login.

**The supervisor's rereview** (#491). The supervisor's identity may also write `/devloop
rereview <reason>`, but only as a command of a `tool_failure` escalation of a PR's review, and
only while a reviewer of the quorum of the latest round has no verdict because its review failed
(a tool failure, a timeout, no model available). A `/devloop retry` resumes that same round, with
the profiles it recorded when it opened, so it tries again models that may still be exhausted;
the rereview opens a new round instead, whose reviewers are chosen with the current
configuration, with a new receipt, and which counts against `max_review_rounds` as every round
does. When every reviewer of the quorum of the latest round has a verdict (`approve`,
`request_changes`, `needs_operator`), or one of them asked for the operator, the command is
refused with the reason, so that the supervisor cannot repeat a review to override a verdict;
so is a round the first reviewer closed without approving, with `second_reviewer` other than
`parallel` (#521): the second reviewer was not asked, and no verdict is missing. It
is refused as well when a new review was already asked after the latest review record and its
round wrote no record: such a round does not count against `max_review_rounds`, so a second
rereview would repeat it without limit; the supervisor uses `/devloop retry`, within its limit, or
hands the PR to the operator. The details are in [Escalations to the supervisor](#escalations-to-the-supervisor). The command does
not touch the merge: on a PR whose merge is the operator's (`operator_only_paths`) it stays the
operator's. For the users in `trusted_users` nothing changes: their `/devloop rereview` is the
request described above, and is never a command of an escalation.

A PR whose marker is missing stays in the flow
(it still counts as an open PR and its issue does not restart) and goes to
`agent:needs-operator`. A PR left without a state label, for example after a crash right after it
was opened, is put back into `agent:review`, and its issue loses `agent:working`. How devloop
recognises its PRs and their marker: [PR marker](protocol.md#pr-marker).

### Open PR limits

Open devloop PRs are capped by two limits (#199), so unattended work cannot outrun review
capacity:

- `max_open_prs` counts the PRs devloop is working on: `agent:review`, `agent:changes`, a sync,
  CI running, a PR without a state label or without its marker, and any other state not listed
  below.
- `max_parked_prs` (default 3) counts the **parked** PRs, those waiting for a person:
  `agent:needs-operator`, and `agent:ready-to-merge` when devloop does not merge it, because
  the [merge mode](#merge-modes) of the PR is `"off"`; such a PR also gets
  `agent:needs-operator` ([Ready PRs whose merge is the operator's](#ready-prs-whose-merge-is-the-operators)),
  and still counts once. A ready PR that devloop merges
  itself (`strict` or `auto` for its level) is not parked, nor is one whose merge mode cannot
  be computed in that tick.

A new issue starts only while both limits leave room: fewer PRs in progress than `max_open_prs`,
and fewer parked PRs than `max_parked_prs`, since many open PRs make conflicts more likely.
`devloop status` and every tick log show the two counters, as `open PRs: <n>/<max_open_prs> in
progress (max_open_prs), <n>/<max_parked_prs> parked (max_parked_prs)`, with the
[temporary capacity limits](#temporary-capacity-limits) in force when there are any. Issues with a
`blocked_labels` label are skipped.

**Exception for high-priority issues** (#274): once `max_parked_prs` is reached, a ready issue
with priority `high` (`priority:high`, or a label in `priority_labels` such as `prerequisito`,
see [Start order](#start-order-of-ready-issues)) still starts, because it may be the fix the
parked PRs wait for. It still needs a free slot of `max_open_prs`. Issues with priority `normal`
or `low` wait, as do supervisor retries of an issue step. The tick log and the `next:` lines of
`devloop status` give the reason: `implement #<n>: priority high: starts although
<n>/<max_parked_prs> PRs are parked (max_parked_prs)` (with the readiness check, the same reason
follows the one of the `preflight` action).

### Start order of ready issues

New issues start in this order (#146):

1. priority, highest first: `priority:high`, then `priority:normal` (the default, also without
   any priority label), then `priority:low`. A label listed in `priority_labels` (`prerequisito`
   in the distributed configuration) counts as `priority:high`;
2. with the same priority, the oldest `agent:ready`: the time of the **latest** `labeled` event
   of that label, the one behind the current admission. Removing the label and adding it again
   sends the issue to the back of its priority;
3. then the issue number.

Between 1 and 2 comes the work the queue waits for (#277): with the same priority, an issue that
blocks other ready issues starts before one that blocks none, whatever the time of their
`agent:ready` ([Dependencies between issues](#dependencies-between-issues)).

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
start on this tick (free PR slots, `blocked_labels`, `agent:needs-operator` and an untrusted
`agent:ready` still apply).

An issue with [dependencies](#dependencies-between-issues) (#277) shows them before its title:
`, blocked by #<n>` (the open issues that block it, so it does not start), `, blocks #<n>` (the
ready issues it blocks) and `, dependency cycle #<n>, #<m>`, for example
`ready 2. #124 priority=normal agent:ready=… labelled by brushtyler, blocked by #122: …`.

### Dependencies between issues

devloop reads GitHub's native issue dependencies ("blocked by" / "blocking", #277) and applies
them to the [start order](#start-order-of-ready-issues), so a prerequisite no longer has to be
written in the text and the issues that wait for it queued by hand once it is merged:

- **An issue blocked by an open issue does not start.** It is neither checked by the
  [readiness check](#readiness-check-before-implementation) nor implemented, and takes no slot
  of `max_open_prs`. When the last block closes it becomes eligible by itself, in the usual
  [start order](#start-order-of-ready-issues), without anyone queuing it again. A closed block
  blocks nothing. The same holds for a supervisor's `/devloop retry` of the readiness check or of
  the implementation of a blocked issue: the command waits, takes no slot and spends no attempt,
  and runs once the last block closes (the tick log says `the retry waits, blocked by #<n>`).
- **Only trusted blocks count.** A block counts only if the latest timeline event that added it
  to the blocked issue comes from a user in `trusted_users`, or from the supervisor's app on an
  issue it may queue ([Labels from the supervisor](#labels-from-the-supervisor)): the same rule
  as the priority labels. A block added by anyone else is ignored, and the tick log says so
  (`issue #<n>: ignored the block by #<m>, added by <login>`).
- **Prerequisites first.** With the same priority, an issue that blocks other ready issues
  starts before the issues that block none, whatever the time of their `agent:ready`.
- **Cycles.** devloop follows the open blocks of the repository from the queue, also through
  issues without `agent:ready` (up to 50 of them per tick); a blocking issue of another
  repository counts as open or closed, without reading its own blocks. An issue of the queue on
  a cycle (A blocked by B, B blocked by A) does not start: the tick log and `devloop status`
  show the cycle, and the dispatcher reports it once on the issue with a comment (a `cycle`
  record, see [Protocol](protocol.md#dependency-cycles)), which `devloop watch` shows to the
  supervisor. No label is added: once a block of the cycle is removed, the issues start as
  usual.

The dependencies are read once per tick, for each ready issue that may start, usually with one GraphQL
query (`Tracker.issue_dependencies`, see [Ports and adapters](../architecture.md#ports-and-adapters)).
The timeline events that tell who added each block are read 100 at a time, backwards, until
every open block has its latest addition: a long history of blocks added and removed does not
make a trusted block look as if it came from an unknown author.

### Labels from the supervisor

With `[roles].supervisor` set and a GitHub App for that agent in `[apps]`, or with
`[roles.supervisor]` as the table of the supervisor actor and its role app `[apps.supervisor]`
([The supervisor app](reference.md#the-supervisor-app), #408), the supervisor queues
issues and sets their priority with its own identity (#178): `agent:ready`, the `priority:*`
labels and those in `priority_labels` count also when the supervisor's app added them, but
**only on issues opened by a user in `trusted_users` or by one of devloop's apps** (the agents',
the dispatcher's, the supervisor's and, with `[roles.coordinator]`, the coordinator's). On an issue opened by anyone else only a trusted user's label counts, as
in [AGENTS.md](../../AGENTS.md) ("Contenuti di autori non fidati").

With `[roles.coordinator]` ([The coordinator](#the-coordinator)) the coordinator's `agent:ready`
and priority labels count on the same issues as the supervisor's (#595); its dependency and
implementer labels never do. The issues the coordinator opens are devloop's: the supervisor and
the coordinator may queue them. One exception holds them for the operator: when the
[readiness check](#readiness-check-before-implementation) of an issue the coordinator opened
declares the trust policy touched (`trust_policy` other than `none`, or not declared) and the
latest `agent:ready` is not a trusted user's, the check fails with a gap of kind
`operator_decision` and the issue goes to `agent:needs-operator` without starting. The operator
admits it by adding `agent:ready` again in person: a new check runs, and the gap is gone. The
rule holds for a passed check reused too: if the coordinator or the supervisor adds `agent:ready`
again after the operator's, the passed check reads as failed with that gap, and the issue goes
back to `agent:needs-operator` before the implementation. Without
a readiness check (`preflight` not set) nothing would say whether such an issue touches the trust
policy, so on the coordinator's issues only the trusted users' labels count.

As for the other label checks, the latest `labeled` event of the label decides: a trusted
user's `agent:ready` later replaced by the supervisor's on a stranger's issue no longer admits
it, nor approves its text any longer, exactly as an untrusted edit after the label would void
it. A supervisor's priority on an admitted issue changes the start order exactly as a trusted
user's would. The label approves the title and the body as they are; an edit by anyone outside
`trusted_users` afterwards, the supervisor included, voids it as usual.

Without `[roles].supervisor`, or without an app for it (the supervisor then acts with the
operator's `gh` login), only `trusted_users` count, as before.

### Choosing the implementer

Every issue goes to `roles.implementer` unless a label chooses another (#407, ADR 0005, D7): an
`impl:<kind>` label (`impl:codex`) gives it to the key `<kind>-implementer`, and prevails; an
area label of `[roles.implementers]` (empty by default) gives it to the implementer it maps to.
These labels count under the same rules as the supervisor's labels above: only while the issue
has them, and only when their latest `labeled` event comes from a trusted user, or from the
supervisor on an issue of a trusted user or of devloop's apps. The implementer is chosen at the
takeover and recorded in the `request` record and in the PR marker: changing the label later
does not change the implementer of an open PR. Two `impl:` labels, an `impl:` label of an
implementer that is not configured, area labels that lead to different implementers, or an
implementer that is the same agent as the readiness check, send the issue to the operator as
`configuration`. Every implementer signs with the implementer app once the keys are renamed by
role ([the keys by role](reference.md#the-implementer-app-and-the-keys-by-role)); off by default.

Once the keys are renamed, claude can join the reviewer pool as its third reviewer,
`claude-reviewer`, last and with another model than the implementer's (#409, ADR 0005, P5-5).
With `[review_policy] provider_quorum = true`, off by default, every agent declares its
`provider`; a reviewer must differ by tuple from everyone who wrote the PR, at least one
reviewer of the round is of another provider, a reviewer never falls back on a model of an
author's provider, and a new round skips a reviewer whose models are all remembered unavailable.
See
[Reviewers by provider and the third reviewer](reference.md#reviewers-by-provider-and-the-third-reviewer).
With `[review_policy] fallback_provider_ban = true` (#410, ADR 0005, P5-6), off by default and
independent of `provider_quorum`, a reviewer never falls back on a model of an author's provider
at either level, and a reviewer of the pool that falls back on the provider of an agent that may
implement stops devloop at startup: see
[No fallback on the authors' provider](reference.md#no-fallback-on-the-authors-provider).
With `[review_policy] findings_reviewer` (#416, ADR 0005, D8), off by default, a reviewer of the
same agent as the implementer, with another model and outside the pool, reviews each level 2
round of a PR signed with the implementer app for its findings only: they go to the fix and the
quorum closes them, while its approval never counts nor takes the place of a missing reviewer.
Since `claude-reviewer` is in the pool (#499), it takes a key and a GitHub App of its own, such
as `claude-findings`. See [Findings-only reviewer](reference.md#findings-only-reviewer).

### When the second reviewer starts

`[review_policy] second_reviewer` (#521), in the local overlay, decides when the second reviewer
of a level 2 round starts, to spare the quota of the scarcest agent:

- `parallel` (the default): both reviewers of the pair run together, as before.
- `sequential`: only the first of the pair runs (the first in the order of `roles.reviewers`).
  The second runs on the same head only once the first approved; a `needs_operator` for the trust
  policy without findings of its own counts as an approval, and starts it. A request for
  changes, or for a decision of the operator, closes the round with the first verdict alone.
- `parallel_cancel`: both run together, and a request for changes (or for a decision) of the
  first stops the second's turn, recorded as cancelled: no `tool_failure`, no escalation.

A round closed by the first reviewer goes to the fix, or to the operator, and counts against
`max_review_rounds` as any round; it lacks no verdict, so the supervisor's `/devloop rereview`
does not apply to it. Quorum, receipt and merge conditions do not change, and a value other than
the three stops devloop at startup. See
[When the second reviewer starts](reference.md#when-the-second-reviewer-starts).

### Readiness check before implementation

With `preflight = "<agent>"` in the configuration, an issue is checked before the implementer
starts on it (#114). The distributed `devloop/config.toml` leaves it **disabled**, with a
commented example: enabling it is the operator's decision. Without `preflight`, issues start as
described above.

The checker is a configured agent other than the implementer: a key without a role or, since
#499, a reviewer key such as `codex-reviewer`, never an implementer key. It runs in review mode (the
read-only arguments and sandbox of a reviewer, see [Configuration](reference.md#configuration)) in a
disposable detached worktree at `origin/main`, `issue-<n>-preflight`, removed afterwards, and
gets the implementer's context: title, body and the comments of `trusted_users`. Its answer
follows [`devloop/schemas/preflight.json`](../../devloop/schemas/preflight.json):

- `ready`, a boolean;
- `gaps`, each with a `kind` (`operator_decision`, `default_available` or `tool_unavailable`),
  a `description`, the `code_refs` that show it, a `proposed_default` (`""` if none) and
  `repository_dependent` (#270), `true` when the gap closes by itself once `main` gains some
  content (a prerequisite not merged yet, a file that does not exist yet), whatever its `kind`;
- `split`, how to divide an issue too large for one run, `""` if not needed;
- `trust_policy` and `trust_policy_reason` (#472): whether the change the issue asks for touches
  the [trust policy](#changes-to-the-trust-policy), `none`, `covered` or `uncovered`, with the
  parts touched and the decision of the operator that covers them, or why none does. The check
  gets the rule of the review prompt, whole, and the comments that may record a decision of the
  operator on the issue.

An issue is ready only if the agent says so **and** lists no gap and no split: a
`default_available` gap blocks too, until a trusted comment accepts the proposed default. A
default the repository or the issue already prescribes is not a gap. An `uncovered` change to the
trust policy is a gap of kind `operator_decision` that devloop writes itself, first, with the
question "Approvi la modifica della policy di fiducia descritta qui?": the issue goes to the
operator, never to the supervisor; a `covered` one is shown in the comment of the check, with
the decision it cites.

A decision that an ADR on `main` records as taken by the operator ("Decise dall'operatore") counts
as an operator decision for the issues that cite it (#367): `docs/adr/` is in
`operator_only_paths`, so it reaches `main` only through the operator's merge. A point the ADR
leaves open, or an ADR that is only proposed, settles nothing and stays an `operator_decision`
gap. The lifecycle of an ADR, from proposal to the recorded decisions, is in
[`docs/adr/README.md`](../adr/README.md). This rule is ssdlc's datum in the prompt, in
`[prompts.adr]` of its project file (ADR 0006, R4): the directory, which must be in
`operator_only_paths`, and the words of the ADRs. A project without it gets no such rule, and
a decision in its ADRs settles no gap
([Project data in the prompts](reference.md#project-data-in-the-prompts)).

The outcome is a comment on the issue, with a marker that records it and a hash of the request
(see [Readiness check records](protocol.md#readiness-check-records)). The outcome is reused
while the hash stays the same:

- **ready**: the issue starts on a later tick, in the usual order and within `max_open_prs` and `max_parked_prs`.
  The implementer starts only if the hash read just before still matches;
- **not ready**: the issue goes to `agent:needs-operator` (it keeps `agent:ready`), and the comment
  lists the gaps with the proposed defaults and the split. If `agent:needs-operator` is removed
  without changing the issue, the next tick blocks it again with the same outcome.

An issue blocked by the check is examined again on every tick, even in `agent:needs-operator`. A
new check runs when the hash changed (a trusted user decided a point, accepted a default, or
edited the issue) or when a trusted user wrote a comment whose **first line** is
`/devloop recheck`. Each request starts one check: it counts only if written after devloop's
latest record on the issue. The command is ignored, and logged, when anyone outside
`trusted_users` writes it. If the new check passes, the dispatcher removes `agent:needs-operator`
and the issue goes back in the queue.

A failed check is also repeated, without a change to the text and without `/devloop recheck`
(#270):

- **when `main` advances**, if at least one gap is `repository_dependent`: such an outcome holds
  only for the commit of `main` it ran on, recorded with it. The first tick that finds `main` at
  another commit checks the issue again, within the free PR slots of the tick like any check
  nobody asked for; if the prerequisite has arrived, the check passes. A failure whose gaps all
  concern the text holds as before, whatever `main` does;
- **when `agent:ready` is added again** after the failed check, by someone who may admit the
  issue: a trusted user or, where its labels count, the supervisor's app
  ([Labels from the supervisor](#labels-from-the-supervisor)). The supervisor only asks for the gaps to be
  evaluated again; it decides a default with `/devloop decide`. The latest `labeled` event of
  `agent:ready` must come after the check's comment (a label in the same second does not count);
  a label from anyone else is ignored, as it does not admit the issue at all.

Checks recorded before #270 carry neither the commit nor `repository_dependent`: they keep
holding for their text alone, devloop does not infer the dependency from their descriptions, and
`agent:ready` added again repeats them too. The next check records both.

A check nobody asked for (the text changed, `main` advanced) runs only for a block the check
owns: when the check owns `agent:needs-operator` is described in
[Readiness check records](protocol.md#readiness-check-records). A note of the supervisor, such as
its "Decisione per l'operatore" listing the options, does not take the block away from the
check: only a comment of the supervisor with an escalation or a readiness check record does
(#468).

A request of a person, `/devloop recheck` from a trusted user or `agent:ready` added again by
someone who may admit the issue, needs less (#468): the block need only be still the issue's
**latest**. The latest check set it (class `operator_decision` or `preflight_default`), no
escalation of another class came after it, and the label was not added again by anyone else.
Any other comment after the check, with the header or not, does not stop the request. When the
check runs, the block is the check's again: a pass removes it, a failure keeps it. A request
that runs no check, because the latest block is another one (an escalation of another class, for
example), leaves the block in place: devloop logs why and answers once on the issue with a
"Controllo di prontezza non ripetuto" comment that gives the reason. The answer spends a
`/devloop recheck`; an `agent:ready` added again is answered once for each time it is added.
An issue in `agent:needs-supervisor` (a recheck that failed and went to the supervisor as
`tool_failure`, for example) runs no check either: a request there gets the same answer, naming
the escalation the issue waits on, and every block stays.

Each check is an agent action: it takes a slot of `max_actions_per_tick`, and a check of an
issue that may start also takes one of the free PR slots of the tick. It runs under
`agent_timeout_seconds`. A check that fails or times out sends the issue to
`agent:needs-operator` with a comment, removing `agent:ready` like any failed step, and the
implementer does not start. The check runs on the checker's chain of
[fallback models](reference.md#fallback-models) like any other turn (#148): an unavailable model
(a quota exhausted, for example) leaves the check to the next one, and the issue goes to
`agent:needs-operator` only when no model is left. An agy check stopped on a denied permission
is first run once more, told what was denied and which commands it may run
([agy permissions](reference.md#agy-permissions), #180, #319). An agy check or review whose
model is denied twice moves to the next fallback model, and the denied model is skipped for
`denied_cooldown_hours`; without another model it escalates naming the model and the command.

### Conclave on a block

With `conclave = ["claude-reviewer", "codex-reviewer", "agy-reviewer"]` in the configuration
(#141, part 2 of #114), a block gets a conclave of three agents that votes on one written
resolution. Its members are reviewer keys or keys without a role, never an implementer key
(#499): a block of an implementer key, whose agent is not a member, convenes none. The distributed
`devloop/config.toml` leaves it **disabled**, with a commented example: enabling it is the
operator's decision. Without `conclave` the escalation is the one described above, and nothing
else happens. A `UNANIMOUS` decision restarts the work once (#142, [After a unanimous
decision](#after-a-unanimous-decision)), and a trusted user can annul it with `/devloop veto`
([Veto](#veto)). With any other outcome nothing is unblocked: the issue or PR stays in
`agent:needs-operator`, and the operator decides as before.

**When.** A block is an escalation to the operator of one of these kinds, when it is the latest
escalation of the issue or PR and nothing moved the item on after it (a review, fix or sync
record, a command applied, a passed readiness check, a policy record of no escalation):

- the implementer or a fixer that stopped as `blocked` (`Lavoro bloccato`): its notice, written
  with the agent's identity, records the agent, the step, the commit it started from and the
  reason. A sync that stops is not a block of the conclave;
- a review round whose verdict is `needs_operator` for a decision; one for the
  [trust policy](#changes-to-the-trust-policy) is no block (#434). With `conclave` configured the
  **first** such `needs_operator` suspends the round: its verdict applies at once, even while the policy receipt
  still waits for other reviewers, and none of them is asked. When several reviewers of the round
  asked for the operator, the first record of the round is the block and its author the
  proposer: the round opens one conclave.

An item in `agent:needs-supervisor` gets none.

**Members.** The three agents of `conclave`, distinct by GitHub identity and by `(kind, model,
effort)`; the configuration refuses fewer or more than three, an unknown agent or two equal
tuples. The agent that stopped is one of the three and makes the proposal; the other two vote,
in configuration order. When the agent that stopped is not one of the three, or two of them share
an identity (without GitHub Apps every agent has the operator's), there is no conclave: the
escalation stays as it is, and the log says why. The three stay distinct on [fallback
models](reference.md#fallback-models) too: a member may run only on a model whose tuple differs
from the profile each other member contributed with, as the record keeps it, and from the
configured one of each member still to be asked. A fallback that would coincide is not used; with
no model left the contribution fails. Every contribution records the profile it was given with.

**Protocol.** It is finite:

1. the agent that stopped gets its reason and the context, and writes **one** resolution
   ([`devloop/schemas/conclave_proposal.json`](../../devloop/schemas/conclave_proposal.json):
   `reserved`, `resolution`, `reserved_reason`, `operator_question`, `options`), or declares
   that the gap falls in the perimeter reserved to the operator;
2. the other two get the context, the reason and **exactly that text**, identified by its
   SHA-256, and vote `APPROVA` or `BLOCCA` with their reason
   ([`devloop/schemas/conclave_vote.json`](../../devloop/schemas/conclave_vote.json): `vote`,
   `reason`, and the same reserved fields). There are no negotiated revisions. Before each vote
   the dispatcher checks that the recorded text still has its hash, and every vote records the
   hash it was given on.

The resolution may be at most 4000 characters, every other text 1000, and the options at most 5
of 200 characters each: an answer beyond these limits is a failed contribution.

**Reserved perimeter.** The conclave never decides on rollout and activation on real runs, budget
and costs (extensions of budget and of rounds included), credentials and permissions, trust
rules, or merge (the revisions of the replay gate baseline left the list with the SSDLC engine,
ADR 0007, R1). A member that flags it writes the
question for the operator and the options. When the proposer flags it, the conclave ends at once:
no proposal and no votes.

**Outcome.**

- `RESERVED`, when any member flagged the reserved perimeter: it prevails over everything. The
  comment holds the question for the operator with the options;
- `UNANIMOUS`, with three consents: the proposal and two `APPROVA`;
- `BLOCKED`, with even one `BLOCCA`, a member that failed or timed out, which counts as `BLOCCA`
  and not as an abstention, or a context that changed. After a first `BLOCCA` or a failure the
  other vote is still collected, because it may flag the reserved perimeter; after a first
  voter's flag of the perimeter too, because it may write its own question for the operator.

Every missing contribution is recorded as `missing`, with its reason.

**Substitute vote of the coordinator.** With [`[roles.coordinator]`](roles/coordinator.md) (#605,
[ADR 0002](../adr/0002-ruoli-e-responsabilita.md), second precision of 2026-11-23, point 3), a
voter whose call fails (an exhausted quota, a CLI in error, no distinct fallback model left)
does not close the conclave: its vote **waits for the coordinator's substitute vote**, until
`conclave_substitute_timeout_seconds` (default 7200, two hours) from that first unavailability.
The record says which member is missing and why, and keeps the deadline in UTC, so that it
survives a restart; the other vote is still collected. The coordinator reads the resolution and
votes with a comment whose first line, after its attribution header, is

```text
/devloop vote <conclave> <sha256> APPROVE|BLOCK|RESERVED <reason>
```

where `<conclave>` is the id of the conclave's comment and `<sha256>` the hash of its current
proposal, both written in the comment while it waits. The reason may continue on the next lines,
up to 1000 characters. The dispatcher counts the first command of the coordinator's identity,
written after the wait started, that names this conclave and this hash, before the deadline:
it records it as a **substitute** vote (`APPROVE` and `BLOCK` are `APPROVA` and `BLOCCA`;
`RESERVED` flags the reserved perimeter, and the conclave ends `RESERVED`) and closes the
conclave with the rules above. A malformed command, one for another conclave or another hash,
or one written after the deadline is refused, and listed in the comment; the coordinator may
write a new one while the wait lasts. A `/devloop vote` of anyone else, trusted users included,
is ignored and only logged. Past the deadline without a vote the vote is missing, and the
conclave closes as without a coordinator.

- **The proposer is never substituted**: when the agent that stopped fails, the conclave ends
  `BLOCKED` as before.
- **One substitute vote per conclave**: when the second voter fails too, before or after the
  coordinator's vote, the conclave is `BLOCKED`, to the operator.
- **It is visible**: the vote is shown as the coordinator's in place of the member, with the
  coordinator's identity and the agent and model of `[roles.coordinator]`; the three distinct
  profiles rule does not apply to it, and the [daily summary](#daily-summary) lists it, so that
  the operator sees when the diversity of the voters was reduced (for example two Claude
  models).
- **The operator's [veto](#veto)** annuls a decision taken with a substitute vote as any other.

Without `[roles.coordinator]` a failed voter is missing at once, as before.

**Record.** The dispatcher publishes one comment with its own identity on the issue or PR of the
block, with the proposal, the votes with their reasons and the outcome, and a `conclave` marker
(see [Conclave records](protocol.md#conclave-records)), bound to the block's durable reference,
the hash of the proposal and, on a PR, its head. The comment is written before the first call and
edited as the conclave goes on.

**One conclave per block.** A block is identified by its durable reference: the comment of the
notice of the agent that stopped, or the round, the reviewer and the head of the first
`needs_operator` verdict. Restarts and replays of the same verdict (a round applied again
announces it again) never convene a second conclave, once one is concluded or recorded.

**Frozen context and staleness.** Every call gets the same context: the issue as the implementer
gets it (title, body and the trusted comments), read when the conclave opens, with its hash, and
the commit the block started from, in a disposable read-only checkout (`issue-<n>-conclave` or
`pr-<n>-conclave`) removed after each call. The changes the agent that stopped left uncommitted
in its worktree are attached to the prompts as a diff, read only: the tracked files as `git diff`
against the commit, and each new file git does not ignore with its content (a binary file, or one
above 256 KiB, by its size and SHA-256). If the PR's head, the hash of the issue or that diff
changes before the conclusion, the outcome is `BLOCKED` (`RESERVED` still prevails), and the same
block does not convene it again. The context is read again before each call and once more after
the last one, before the outcome is published: a change during the last call counts too. A PR whose head is no longer the commit of the block when the
conclave would open gets a `BLOCKED` conclave at once, without calls.

**Capacity and restarts.** Each call (the proposal and each vote) is one `conclave` action and
takes a slot of `max_actions_per_tick`; the calls left beyond it run on the next ticks, from the
recorded point. Each runs under `agent_timeout_seconds`, in review mode, on the member's chain of
[fallback models](reference.md#fallback-models). No limit or round grows. Before each call the
record marks it `started`: a call **not started** (no slot, a checkout that failed) runs on a
later tick; a call **started without a durable result** (a crash after the start) counts as a
failure, that is `BLOCCA`, and is not repeated. A failed `conclave` action, or one stopped by an
unexpected error of the dispatcher (with `max_parallel_actions` above 1 too), is only logged: the
item is already the operator's, and no other escalation is added, which would end the block.

#### After a unanimous decision

A `UNANIMOUS` decision restarts the step that stopped **once** (#142, part 3 of #114), with one
more `conclave` action on a later tick, while the item is still in `agent:needs-operator` (not in
`agent:needs-supervisor`). Before anything restarts, the dispatcher marks the record `resumed`:
a restart is never repeated, also after a crash.

- **Implementation or fix.** The step restarts from the **published commit**, as it always does:
  the implementation from the base branch, on its branch created again, a fix from the PR's
  head. The changes the agent that stopped left uncommitted in its worktree are not taken up as
  work: the dispatcher saves them as a diff in its logs (`<state_dir>/logs/conclave-<issue-n or
  pr-n>-<comment>.diff`), and the record cites the file, its size and its SHA-256 as
  **evidence**. The implementation runs at once, within the same action, and takes a free slot
  of `max_open_prs` as any implementation (without one it waits for a later tick); in the
  continuous mode the action keeps that slot while it runs, so the ticks planned meanwhile do not
  start another issue in its place. While the issue is blocked by an open issue
  ([Dependencies](#dependencies-between-issues)) the restart waits, takes no slot and is not
  spent: it runs on the tick after the block closes. It does not restart when the readiness check
  no longer covers the text, or the issue already has a devloop PR, open or merged. A fix goes
  back to `agent:changes`, and the next tick runs it.
- **Review with `needs_operator`.** The PR goes back to `agent:review`: the verdict of the block
  is **not applied again**, and the next tick reviews the same head in a new round, within the
  rounds `max_review_rounds` still allows; with none left the PR escalates for the round limit,
  as usual.

No limit, round or slot grows. The normal review of the PR and the human merge stay mandatory
after every restart: the decision only settles the gap the step stopped on. Once a record of the
issue or of its PR is `resumed`, devloop **never merges** that PR, whatever `[merge]` says
(`strict` or `auto`), also after the resolution lapses or is vetoed: the PR counts as one in
mode `off`, ready PRs get the [operator merge](#ready-prs-whose-merge-is-the-operators) notice with the comment of the decision,
and the final check before a merge refuses it as well.

**What reaches the prompts.** The trust in bots' comments does not widen, and `trusted_users`
stays as it is. The prompts of the implementation, of the fixes and of the reviews of that issue
and of its PR get **only the verified record** of the latest decision: a `conclave` record the
dispatcher signed, that holds together, with outcome `UNANIMOUS`, on the thread of its block (an
implementation on the issue, a fix or a review on the PR), with the resolution, the block it
settled and the comment that records it. A comment of any other author, a bot included, never
reaches a prompt as a resolution. The resolution **lapses**, and no prompt gets it, when:

- the content it was taken on changed: the hash of the issue's request (title, body and trusted
  comments, as for the [readiness check](#readiness-check-before-implementation));
- on a PR, the head it was bound to changed (a decision on a fix or a review); a decision on an
  implementation is bound to the issue's text only, and reaches the PR's fixes and reviews too;
- a trusted user vetoed it.

Every step that ran with a resolution records the comment of the decision (`conclave`) in its
marker: the PR's, the fix's, the review's, or the notice of an agent that stopped. A block raised
by such a step convenes **no other conclave**: the escalation stays with the operator, so that
the protocol stays finite.

#### Veto

A trusted user annuls the latest applicable decision with a comment whose **first line** is
`/devloop veto`, optionally followed by a reason, on the issue or on its PR. The same command
from anyone else, a bot or a devloop app included, is ignored and logged.

- The decision is annulled at once: from then on no prompt gets its resolution, and a decision
  not yet applied no longer restarts anything.
- From the next tick the steps after it are **suspended**: a PR devloop works on (`agent:review`,
  `agent:changes` or `agent:ready-to-merge`) goes to `agent:needs-operator` with an escalation of
  class `conclave_veto`, before any other action of the tick on it, a merge included. The
  escalation records the decision and the veto, which it spends.
- Before publishing anything a step produced with the resolution (the push of an implementation
  or of a fix, a review), the dispatcher reads the comments again: with a veto written meanwhile
  it publishes nothing, and suspends the item the same way. The implementation's commit stays in
  the local worktree.
- Commits already published are not reverted. A veto after the merge has no effect: the merge
  stays a human act.

### Tests red on the base too

PRs green on their own base can leave `main` red once merged together. An issue started from
that `main` then fails its local tests for a defect that is not its own (#459). So, when the
local tests of an implementation or of a fix fail, before counting the failure against the
implementer devloop reads the identities of the failed tests from the whole output and runs their
modules, one at a time, on the base commit of the branch, in a disposable worktree:

- **every failed test fails on the base too**: the step is `base_broken`. No other attempt of
  the agent and no escalation: the dispatcher writes a comment "Test rossi anche sulla base" with
  a `basebroken` record (the base commit and the failed tests) on the issue or on the PR, and
  nothing is pushed. The issue keeps `agent:working`, the PR its label. When `main` moves past
  that commit the step starts again by itself: the issue as a new implementation, in a free slot
  of `max_open_prs`; the fix at its next tick. Until then the issue is not `stale`. The blocks of
  the issue hold its restart as they hold a ready issue: `agent:needs-operator`,
  `agent:needs-supervisor` or a label of `blocked_labels`. With the readiness check (`preflight`)
  the restart goes through it: an issue whose request changed during the wait, or with a new
  `/devloop recheck`, is checked again before the implementation, and one whose check failed is
  blocked as a ready issue is. An escalation of that check keeps `agent:working` and the wait,
  so the issue is never `stale` meanwhile: gaps that all have a default go to the supervisor,
  and once `/devloop accept-defaults` or `/devloop decide` (or the operator, after a
  `/devloop escalate`) settles them the check runs again; a check that failed with its step
  recorded (a `tool_failure`, for one) resumes with `/devloop retry`, from the supervisor or,
  in `agent:needs-operator`, from a trusted user;
- **every failed test fails on the base too, and the CI of that base commit is green** (#497):
  the tests fail only where devloop runs them (its user, the permissions, the programs of the
  host), and a new commit of `main` would not change that. The step is `host_tests`, a critical
  escalation to the operator, never to the supervisor, with the failed tests, the base commit
  and the link to its green CI: no wait for `main`, and nothing is pushed. The issue leaves
  `agent:working`, the PR its label, as with `tests_failed`. The CI is green only when every
  check of that commit concluded with a success: a skipped or neutral check does not prove that
  its tests ran. A CI of the base commit that failed, is still running, has no check, has a
  check without a success or cannot be read leaves `base_broken`; while that wait holds, devloop
  reads the CI of the same commit again at each tick, and once it is green the step escalates
  `host_tests` as above, without waiting for `main` to move (the blocks of the issue hold that
  escalation as they hold its restart);
- **the same, but the failed tests pass once run again alone** (#598): the base runs right after
  the branch, on the same host and under the same load, so a test that fails only under load
  (the local tests of several actions run in parallel) fails there too. Before escalating
  `host_tests`, and before waiting as `base_broken`, devloop waits 5 seconds and runs once
  again, on the branch as it is, only the failed tests, one module at a time, each with its own
  `test_timeout_seconds` (a failed `setUpClass` or `tearDownClass` runs its whole class, a
  failed `setUpModule` or `tearDownModule` its whole module); the other actions go on.
  When they all pass the tests are unstable, not the host nor `main`: the step goes on as after
  passing tests, without another turn of the agent and without spending an attempt of
  correction. The failed verification stays in the [ledger](protocol.md#evidence-and-telemetry),
  and the retry is a `verification` of its own whose comment names the unstable tests, so that
  the supervisor can open an issue to fix them. When they fail again, the step escalates
  `host_tests` or waits as `base_broken`, as above: so the `host_tests` that comes from that
  wait, once the CI of the base commit is green, follows a failed retry too;
- **a test fails only on the branch**, or the failed tests cannot be identified (a timeout, an
  output without them, a module that does not start, whose id unittest makes up the same whatever
  the error, a project whose `[tests].selection` is not
  `python-unittest-modules`): the failure stays the implementer's, as before:
  `implement_test_retries` more attempts, then `tests_failed`.

### Red CI on `main`

With `[run].alert_issue`, at each tick (not in a dry run) devloop reads the checks of the current
commit of `origin/main`. When one ended with a failure, it publishes **one** alert "CI rossa su
`main`" on that issue, with the commit, the failed checks and the commits of `main` since the
latest green one, at most 10 back on its first-parent chain. A new alert comes only for a new red
commit; the commit alerted is kept in `state_dir` (`main-ci-alert.json`), so a restart does not
repeat it. Pending, missing or unreadable checks give no alert, and without `[run].alert_issue`
nothing is read. It is only a report: merges go on as before.

## Merge modes

This is the only description of the merge modes in this guide; the other sections link here.
Which modes exist, and that choosing one is the operator's decision, is set by
[ADR 0002](../adr/0002-ruoli-e-responsabilita.md) (see [Who does what](#who-does-what)).

`[merge].mode` decides who merges a PR in `agent:ready-to-merge` (#172):

| `mode` | Who merges |
|---|---|
| `"off"` (default, and this repository's) | A person, as before |
| `"strict"` | devloop, only when every [merge directive](protocol.md#merge-directives-and-record) holds |
| `"auto"` | As `"strict"`; in addition the supervisor can [hold a merge](#holding-a-merge) and [decide non-critical escalations](#escalations-to-the-supervisor) |

- **Per level.** `[merge.levels]` gives a mode to a review level, for example `"2" = "off"`.
  The most restrictive of that mode and `mode` applies: `off`, then `strict`, then `auto`. An
  override never relaxes `mode`.
- **Operator-only paths.** A PR whose diff touches `operator_only_paths` (default `docs/adr/`,
  `AGENTS.md` and `CLAUDE.md`) is merged by a person, whatever the mode.
- **The coordinator's exception.** With `[roles.coordinator]`, a PR whose merge is the
  operator's for any of these reasons (mode `"off"`, a level `"off"`, an operator-only path) may
  also be merged by devloop with the merger app on a `/devloop merge` of the coordinator, under
  the conditions and the daily cap of
  [The reserved merge of the coordinator](#the-reserved-merge-of-the-coordinator) (#529). The
  merge mode decides only what devloop merges by itself; without the coordinator, or without
  its command, the PR stays a person's.
- **The merge mode of a PR** is therefore `mode`, made more restrictive by the override of the
  PR's review level, and `"off"` when its diff touches an operator-only path. Elsewhere in this
  guide, "the merge mode of a PR" means this one.
- **Merge directives and record.** What devloop checks just before a merge, how it merges and
  what it records: [Merge directives and record](protocol.md#merge-directives-and-record).

### Closing the issue

With a merge made by the merger app, GitHub does not apply the PR's "Closes #N" (#228). After
every merge in `"strict"` or `"auto"` the dispatcher therefore closes the issue named by the
PR's marker itself, with its own identity (the merger app gets no new permission): a dispatcher
comment with a `closed` marker records the PR, the merged head, the merge commit and the mode,
then the issue is closed as completed and loses `agent:ready`. An issue that is already closed,
because GitHub closed it or a person merged, is left alone. A failure to close is written to the
dispatcher log and leaves the merge as it is.

**Safety net in the planner.** An open issue in `agent:ready` with a merged devloop PR (a PR from
its branch, `<branch_prefix><issue>`, or with `attempt_identity` the branch of one of its later
[attempts](protocol.md#attempt-identity), by a trusted user or a devloop app, merged by anyone) is
never checked nor implemented again. It becomes a `merged_issue` escalation, non-critical: in
`"auto"` it goes to `agent:needs-supervisor` and keeps `agent:ready`, otherwise to
`agent:needs-operator` without `agent:ready`. `/devloop retry` is refused for it: closing the issue,
or `/devloop escalate <reason>`, settles it. More work on the same subject needs a new issue.
The same check runs before a `/devloop retry` of an issue already escalated for another class
(a `tool_failure` of its readiness check or implementation, for example): with a merged devloop
PR the retry is refused, the step is not run, and the issue gets a `merged_issue` escalation
routed as above.

### Ready PRs whose merge is the operator's

In `"auto"` or `"strict"`, `agent:ready-to-merge` usually means that devloop merges the PR. When
it does not (#248), because the [merge mode](#merge-modes) of the PR is `"off"`, the dispatcher also adds
`agent:needs-operator` once the PR is ready, with a comment ("Il merge spetta all'operatore")
that says why: the reserved paths the diff touches, the mode or the level, the restart of its
work after a [conclave](#after-a-unanimous-decision) (#142), or a reviewer who said that it
touches the [trust policy](#changes-to-the-trust-policy) (#434) and whose `trust_policy` a
later approval did not clear (#461), with the parts each such reviewer named; this last reason is stated with `[merge] mode = "off"` too. The comment
carries a `notice` marker of event `operator_merge` with the head, how many `labeled` events of
the label came before, and the promotion it belongs to (how many promotions the PR's policy
records count). There is one comment per head: a head promoted again, after its CI ran again
for example, gets the label back and its comment edited with the new promotion and count, not a
second comment. A label the operator removes while the PR stays ready is not added again.
`agent:merge-hold` is not such a case: there the supervisor decides. With `[delegations] merge`
on, the supervisor may merge such a PR itself, under the conditions of
[Delegated merge](#delegated-merge) (#486).

That label does not stop devloop, which keeps working on the PR as on any ready PR: it
[syncs](protocol.md#conflicts-with-main) it when it conflicts with `main`, re-checks its CI, and with a new
commit revokes the readiness and sends the PR back to review. The review after a sync of the
approved head, also one whose conflicts the implementer resolved, starts even with no round left
and, if it approves, spends no round and makes the PR ready again without a `round_limit`
escalation ([Resync after approval](protocol.md#resync-after-approval), #458). Whenever the readiness decays
without an escalation (a revocation, a sync, a failed CI round, a stale receipt), the dispatcher
removes `agent:needs-operator` together with `agent:ready-to-merge`. It recognises its own label
by the notice of the current promotion: no other notice of the dispatcher and no escalation was
recorded after it (or after the promotion, for a comment edited), and the label's only
`labeled` event since is the dispatcher's. Any other
`agent:needs-operator`, such as one added by an escalation, an agent that stopped, or a person,
keeps today's behaviour: devloop leaves the PR alone. These PRs are already counted among the
[parked PRs](#open-pr-limits): the count does not change.

### Holding a merge

Only in `"auto"`. A PR comment whose first line, after the attribution header if any, is

```text
/devloop hold <reason>
/devloop release
```

holds or releases the merge. `hold` adds `agent:merge-hold`: the PR stays ready and is not
merged. `release` removes the label, and the PR is merged once the directives hold; removing the
label by hand does the same. A hold needs a reason. An `agent:merge-hold` added by hand counts up to
the last moment: devloop reads the labels of the PR again just before the merge call, and a label
added after the other checks stops the merge (#519,
[Merge directives and record](protocol.md#merge-directives-and-record)).

The commands count only from the identity of `[roles].supervisor` (the GitHub App of that agent,
or with `[roles.supervisor]` as a table the role app of
[The supervisor app](reference.md#the-supervisor-app)) and from `trusted_users`. The dispatcher applies a command with a dispatcher comment that carries
a `hold` marker (event, author, reason, head, and in `comment` the id of the comment it applied),
and that record spends the command it applied and every command written before it: a command
written after it, even one that arrived while the record was being written, still waits to be
applied. Among the commands that wait the latest wins. A command is applied before anything else
on its PR in that tick, so no merge of the same tick overtakes it, and a merge about to run reads
the comments again and stops if a command is waiting. A command is ignored, and written to the
dispatcher log, when the [merge mode](#merge-modes) of its PR is not `"auto"` (so a PR at a
level overridden to `"strict"` ignores it even with `mode = "auto"`), from anyone else, without
a reason, and in text devloop
publishes (a comment that ends with a devloop marker). As for
`/devloop finding`, devloop reads the comments of a PR only while it is in `agent:review`,
`agent:changes` or `agent:ready-to-merge`.

`"auto"`, also as the override of a level, without `[roles].supervisor` stops devloop at
startup. The supervisor names a configured agent without a role, or is the supervisor actor of
`[roles.supervisor]` as a table, with its role app ([The supervisor app](reference.md#the-supervisor-app),
off by default); the escalations it decides are described below. Since #499 `config.toml` names
its agents by role, and a key with a role is never the supervisor: with it, the supervisor is
the table.

```toml
[roles.supervisor]
name = "claude-supervisor"
kind = "claude"
provider = "anthropic"
model = "claude-opus-5-5"

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
`needs_operator` verdict for a decision gets an escalation comment of class `reviewer_decision`
next to the review; one for the trust policy gets none (#434, see
[Changes to the trust policy](#changes-to-the-trust-policy)).

| Class | When | Critical |
|---|---|---|
| `tool_failure` | The agent or its model produced no result: a crash, a timeout, a stall with `agent_stall_action = "abort"`, no model available, a denied permission, a block for [background work still running](reference.md#no-background-work-in-a-turn), an agent that [could not start](protocol.md#failed-agent-turns) even after its retry (also in a sync with conflicts on sensitive paths) | No |
| `preflight_default` | A failed [readiness check](#readiness-check-before-implementation) whose gaps are all `default_available`, with no split | No |
| `sync_failed` | A [sync](protocol.md#conflicts-with-main) that failed, with no file in conflict under a sensitive path (`sensitive_paths`) or `operator_only_paths`; one whose agent could not start is a `tool_failure` | No |
| `merged_issue` | An open issue in `agent:ready` that a merged devloop PR already resolved ([Closing the issue](#closing-the-issue)) | No |
| `round_grantable` | The limit of review rounds, with `[delegations] rounds` on and every condition of [granting a round](#granting-a-review-round) verified: the supervisor may grant one more | No |
| `round_limit` | The limit of review rounds, when the supervisor may not grant another one | Yes |
| `operator_decision` | A readiness check with an `operator_decision` or `tool_unavailable` gap, or a split | Yes |
| `untrusted` | The issue text changed after `agent:ready` by someone outside `trusted_users` | Yes |
| `reviewer_decision`, `open_findings` | A reviewer asks for a decision; an approval leaves open findings only the operator can close (`ci` findings with the head's CI still running, or passed during the review, wait for it instead) | Yes |
| `review_level`, `level_zero` | Not enough independent reviewers; a level-0 head that cannot be promoted | Yes |
| `sync_sensitive`, `protected_paths` | A sync conflict on a sensitive or operator-only path; a change to `protected_paths` | Yes |
| `tests_failed`, `blocked`, `interrupted`, `unattributable`, `lost_meta`, `inconsistent`, `configuration`, `failure` | Tests still failing (or no change), an agent that declared itself blocked, an interrupted implementation, findings without an owner, a lost PR marker, an inconsistent PR state, a configuration to decide (a turn refused for an entry of the directories of the CLIs that is not in the [list](#agent-user), #517, also in a sync), a `gh`, git or dispatcher error | Yes |
| `host_tests` | Local tests that fail on the base commit of the branch too, but pass in the CI of that commit: a [failure of the host](#tests-red-on-the-base-too), not of `main` | Yes |
| `receipt_conflict` | The [receipt of the PR](protocol.md#pr-receipt) contradicts the markers at its promotion | Yes |
| `retry_limit`, `supervisor_escalated`, `supervisor_refused` | The supervisor's retries are spent, it handed the escalation over, or it gave a command the escalation does not admit | Yes |
| `conclave_veto` | A trusted user annulled the latest decision of a [conclave](#conclave-on-a-block) with `/devloop veto` | Yes |
| `uncertain_turn` | With `block_uncertain_turns` on, a turn refused because its work has [uncertain turns](protocol.md#turn-journal) a trusted user has not reconciled with `/devloop reconcile` | Yes |
| `attempt` | With `attempt_identity` on, a PR whose [attempt](protocol.md#attempt-identity) the journal does not confirm, or concluded already (a PR reopened after it was merged or closed) | Yes |
| `budget_grantable` | With `[budget] enabled` on, a turn the [budget envelope](reference.md#budget-envelope) of its story refused, with `[delegations] budget` on and every condition of [extending the budget envelope](#extending-the-budget-envelope) verified: the supervisor may extend the envelope once | No |
| `budget` | With `[budget] enabled` on, a turn the [budget envelope](reference.md#budget-envelope) of its story does not cover, refused before it starts, when the supervisor may not extend it | Yes |

Any other class, or none (escalations written before #182), is critical.

**Routing.** With `[merge].mode = "auto"` a non-critical escalation adds
`agent:needs-supervisor` instead of `agent:needs-operator`, and an issue keeps `agent:ready`. It
stays with the operator, with a comment that says why, when:

- the [merge mode](#merge-modes) of the PR is not `"auto"`;
- the escalation is `budget_grantable` and the supervisor's agent implemented the PR: an
  extension of the budget is not among the escalations of process the operator delegated on
  those PRs. Every other non-critical escalation on a PR of the supervisor's agent goes to the
  supervisor like any other (#527, principle 4 of
  [ADR 0002](../adr/0002-ruoli-e-responsabilita.md#principi) as clarified on #524): `retry`,
  `rereview`, `grant-round` within the caps, `accept-defaults`, `decide`, hold and release and
  the delegated merge are decisions of process, which never override nor replace a reviewer's
  verdict. A verdict that asks for the operator stays a critical class, which never reaches the
  supervisor, and the supervisor's rereview is refused when every reviewer has a verdict, on
  these PRs as on any other. This replaces option (a) of ADR 0005, P5-2 (#406):
  `[delegations] retry_own_prs` has no effect any more, and is still accepted so that an overlay
  that sets it keeps loading. The PR is the supervisor's agent's when the `kind`, the `model` or
  the provider of its implementer, as the PR's marker records them, is the supervisor's: the
  supervisor actor's, with `[roles.supervisor]` as a table (#408), or the agent's that
  `roles.supervisor` names (#407, which also counts a PR whose implementer is that key)
  ([The supervisor app](reference.md#the-supervisor-app),
  [the keys by role](reference.md#the-implementer-app-and-the-keys-by-role));
- the issue was opened by someone outside `trusted_users` and devloop's apps, or its text is no
  longer approved by `agent:ready`;
- the issue (or the PR) has the label `phase:design`;
- any of this cannot be verified.

With `"off"` and `"strict"` every escalation goes to `agent:needs-operator`, as before. An item in
`agent:needs-supervisor` is not worked on, and a merge does not run, until the supervisor
decides. `devloop setup` creates the label.

**Commands.** A comment on the issue or PR whose first line, after the attribution header if
any, is one of:

```text
/devloop retry
/devloop accept-defaults
/devloop decide <decisions>
/devloop escalate <reason>
/devloop grant-round <reason>
/devloop grant-budget <reason>
/devloop rereview <reason>
```

They count only in `"auto"`, from the identity of `[roles].supervisor` (the GitHub App of that
agent, or with `[roles.supervisor]` as a table the role app of
[The supervisor app](reference.md#the-supervisor-app)) or from `trusted_users`, only on an item in `agent:needs-supervisor` without
`agent:needs-operator`, and only when written after the latest escalation. On an item in
`agent:needs-operator` the commands of trusted users follow
[Commands on escalations to the operator](#commands-on-escalations-to-the-operator). A command
from anyone else is ignored and written to the dispatcher log only; text devloop publishes is
never a command. A command from an allowed author that does not apply (outside `"auto"`, a
`decide` or an `escalate` without its text, or one refused below) always gets an answer: a
dispatcher comment with the reason and how to resume, besides the log line. As for
[holding a merge](#holding-a-merge), a dispatcher record, the answer included, spends the command
it handled and every command before it, and among the commands that wait the latest applicable
one wins; without one, the latest that does not apply is answered.

- `/devloop retry` runs the failed step again, for `tool_failure` and `sync_failed`. A PR goes
  back to the label of its step (`agent:review` for a review or a sync, `agent:changes` for a
  fix, `agent:review` for a [requested fix](#fix-requested-by-the-operator-or-the-supervisor)
  that started there, `agent:changes` for a sync that a fix in `agent:changes` started because
  its branch conflicted with `main`) and the next tick runs it; an issue's readiness check or implementation runs at once, and
  takes a free PR slot as it would; while the issue is blocked by an open issue it waits,
  unspent ([Dependencies between issues](#dependencies-between-issues)). A failed attempt writes no review record, so a retry
  **counts no round**. After the initial failure the supervisor may authorise **two** retries
  for the same key; the third command is not run and the item goes to `agent:needs-operator`
  (`retry_limit`). The automatic retries inside one run (a permission denied to agy,
  [fallback models](reference.md#fallback-models)) do not count. The key is durable, read from the
  escalation and the dispatcher's records on GitHub: the item, the step and, for a review, the
  reviewer; for a PR the head the step failed on, for an issue the commit of the base branch and
  the hash of the request (title, body and trusted comments). An issue's key is taken before the
  step runs, from the commit its checkout starts at and the request its prompt gets, so a base
  or a text that changes during the turn already makes the retry refer to a superseded input.
  The base does not bind the retry of an **implementation** (#475): with automatic merges `main`
  moves every few minutes, and the step starts again from the current base anyway, with the work
  its timed-out turn saved reapplied ([timeouts](protocol.md)); only its request binds it, for
  the refusal and for the count of retries. The base of a readiness check still does.
  The retries are counted per reviewer: when several reviewers fail together and later only
  some of them, each keeps the retries it already had. A retry whose input has changed
  since (a new head, base or request) is refused, and the escalation stays with the supervisor.
  Before a retry runs, what made the escalation non-critical is checked again on data read
  then (the conditions of **Routing** above: `phase:design`, author and text of the issue, merge
  mode and implementer of the PR): if one no longer holds, the retry is refused and the item
  goes to `agent:needs-operator` (`supervisor_refused`).
- `/devloop accept-defaults` accepts the defaults of a `preflight_default` escalation, only when
  **every** gap of the latest check is `default_available`, no split is needed, the issue has no
  `phase:design` label, was opened by a trusted user or a devloop app, and the check is the one of
  the current text. The dispatcher records the accepted defaults; that record counts as a
  trusted comment, both in the hash of the request and in the agents' prompts. The issue goes
  back in the queue: the readiness check runs again with the defaults and, if ready, the
  implementer starts. An issue with an `operator_decision` gap is refused and goes to
  `agent:needs-operator`. The readiness prompt tells the checker that a gap touching a decision ADR
  0001 reserves to the operator (rollout, budget and rounds, credentials and permissions, trust
  rules, the merge mode, the continuous loop and automatic pinning, ADR approval; the replay gate
  baseline left the list with the SSDLC engine, ADR 0007, R1), or a change to
  `operator_only_paths`, is always `operator_decision`.
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
- `/devloop escalate <reason>` hands the escalation to the operator: `agent:needs-operator`, with
  the reason (`supervisor_escalated`). It needs a reason.
- `/devloop grant-round <reason>` grants one more review round to a PR in a `round_grantable`
  escalation, as described in [Granting a review round](#granting-a-review-round). It needs a
  reason, which may go on in the lines below the command.
- `/devloop grant-budget <reason>` extends once the budget envelope of the story of an issue or
  PR in a `budget_grantable` escalation, as described in
  [Extending the budget envelope](#extending-the-budget-envelope). It needs a reason, which may
  go on in the lines below the command.
- `/devloop rereview <reason>` opens a **new review round** of a PR whose review failed (#491,
  a decision of the operator recorded in
  [ADR 0005](../adr/0005-deleghe-e-ruoli-degli-agent.md)). It counts only from the supervisor's
  identity: the `/devloop rereview` of a user in `trusted_users` stays the request described in
  [Resuming after a `needs_operator` verdict](#resuming-after-a-needs_operator-verdict-without-code-changes),
  and is never a command of an escalation; from anyone else it is ignored and logged. It applies
  only to a `tool_failure` escalation of a PR's **review**, on the head of its key, and only while
  a reviewer of the quorum of the latest round of that head has no verdict, because its review
  failed (a tool failure, a timeout, no model available). Unlike `/devloop retry`, which resumes
  that same round with the profiles it recorded when it opened, the new round chooses its
  reviewers with the current configuration, has a new receipt, and **counts against
  `max_review_rounds`** as every round does; the retries are not touched. The command is refused
  with the reason, and the escalation stays where it is, when every reviewer of the quorum of the
  latest round has a verdict (`approve`, `request_changes`, `needs_operator`) or one of them asked
  for the operator, so that a new review never overrides a verdict; when the first reviewer
  closed the latest round without approving, with
  [`second_reviewer`](#when-the-second-reviewer-starts) other than `parallel` (#521); when no round of the head
  waits for a reviewer (then a `retry` already opens a round with the current configuration);
  when a new review was already asked after the latest review record, by a trusted user or by a
  rereview of the supervisor applied, and its round wrote no record (a round without records does
  not count against `max_review_rounds`, so a second rereview would have no limit); on any other
  escalation or step. Its input and what made the escalation non-critical are checked
  again as for a retry, on a PR the supervisor's agent implemented too (#527). Applied, the
  dispatcher's
  `supervision` record voids the round as a trusted user's request does, and the PR goes back to
  `agent:review`. It needs a reason, which may go on in the lines below the command. The command
  does not touch the merge, so it also applies to a PR whose merge is the operator's (a level
  override or `operator_only_paths`), whose tool failure goes to `agent:needs-operator`: there
  the supervisor's rereview is accepted on the `tool_failure` escalation of the review when every
  other condition of **Routing** still holds, a command of a trusted user waiting on the same item
  wins over it, and after the new round the merge stays the operator's.
- `/devloop capacity` is not a command of an escalation: on one it is answered as not applicable
  and the escalation stays where it is. It applies only on `[report] issue`, as described in
  [Temporary capacity limits](#temporary-capacity-limits).

An escalation whose class is critical or unknown, or that was not routed to the supervisor,
accepts no command: the command is refused and the item goes to `agent:needs-operator`. A command
the class does not admit (`accept-defaults` or `decide` on a tool failure, `retry` on defaults,
`grant-round` on anything but `round_grantable`, `grant-budget` on anything but
`budget_grantable`, `rereview` on anything but the `tool_failure` of a PR's review) is refused
and the escalation stays with the supervisor. The answer lists the commands that apply to the
escalation (`retry` for `tool_failure` and `sync_failed`, `accept-defaults` and `decide` for
`preflight_default`, `grant-round` for `round_grantable`, `grant-budget` for `budget_grantable`,
none of them for an input changed since, `escalate` always) and the manual steps without the
supervisor.

#### Granting a review round

A delegation of the operator to the supervisor (#405, [ADR 0005](../adr/0005-deleghe-e-ruoli-degli-agent.md#round-di-review),
P5-1), **off by default**: `[delegations] rounds = false`. It replaces raising
`max_review_rounds` by hand, which applies to every PR: a round granted applies to one PR only,
and `max_review_rounds` does not change.

With `rounds = true`, in merge mode `"auto"`, a PR that reaches its round limit goes to
`agent:needs-supervisor` as `round_grantable` instead of `agent:needs-operator` as
`round_limit`, when every one of these holds, each verified by the dispatcher:

- the conditions of **Routing** above: the merge mode of the PR is `"auto"` (so it touches no
  `operator_only_paths`), the issue is trusted and still approved, no `phase:design`; since #527
  the PR may be one the supervisor's agent implemented;
- the latest round that counts against the limit is a CI failure, **or** the blocking findings
  left open are fewer than after the round before;
- no open blocking finding is a security finding: one whose `security` field (in the
  [review schema](protocol.md#structured-review)) is not `false`, a finding of a review written
  before the field existed included;
- the owner of every finding can be reconstructed;
- the caps are not reached: **2** rounds granted per PR (`max_rounds_per_pr`) and **3** grants
  per UTC day across every PR (`max_grants_per_day`, all delegations together, retries
  excluded). The caps are constants of the code (`MAX_GRANTED_ROUNDS` and `MAX_DAILY_GRANTS` in
  `devloop/supervision.py`); the configuration and the local overlay may only lower them, and a
  higher value is refused at startup. The grants of the day are read from the
  [turn journal](protocol.md#turn-journal): without a journal that can be read none applies.

Otherwise the escalation is `round_limit`, to the operator, and with the delegation on its
comment says which condition failed. The supervisor answers a `round_grantable` escalation with:

```text
/devloop grant-round <reason>
```

The reason is required and carries the analysis of the causes, or cites it in the diary issue:
the cause (findings new or decreasing, a sync, the CI, a PR too large, the interaction with other
PRs) and, when the cause can be corrected, the issue opened to correct it. devloop refuses the
command without a reason; the content of the analysis is checked by the coordinator, today the
operator. Before granting, the dispatcher checks every condition again on the PR read then: a
head moved since the escalation is refused and the escalation stays with the supervisor; a
delegation turned off, a cap reached or a condition that no longer holds hand the PR to the
operator as `round_limit`. The daily cap is checked once more when the grant is written in the
turn journal, in one step with the write: two grants applied together on different PRs
(parallel actions) never pass it, and the one that finds the last grant of the day taken hands
its PR to the operator as `round_limit`. Granted, the PR's limit becomes `max_review_rounds` plus the rounds
granted to it (plus the rounds that do not count, as before), and the PR goes back to
`agent:review` or `agent:changes`, where it was. The grant leaves a `grant` event in the turn
journal, a `supervision` record with outcome `granted` and a ledger `event`
`delegation_grant` with the caps, the value granted and what remains (see
[Supervision trace](protocol.md#supervision-trace)). A third round on the same PR goes back to the
operator as `round_limit`. The supervisor never approves: the judgement of the code stays with
the reviewers.

**Revocation.** `rounds = false` revokes the delegation. `devloop run` reads `[delegations]` again
before every tick, as it does `max_review_rounds`: from the next tick a round limit goes to the
operator, and a `grant-round` on an escalation already with the supervisor is refused and hands
the PR to the operator. On a `round_limit` escalation to the operator a trusted user grants one
more round to that PR with the same command, outside the caps of the delegation
([Granting a review round on an escalation to the operator](#granting-a-review-round-on-an-escalation-to-the-operator), #553);
raising `max_review_rounds` still grants more rounds to every PR.

#### Extending the budget envelope

A delegation of the operator to the supervisor (#411, [ADR 0005](../adr/0005-deleghe-e-ruoli-degli-agent.md#budget-dopo-365),
P5-7), **off by default**: `[delegations] budget = false`. It applies only with the
[budget](reference.md#budget-envelope) on, whose activation is a decision of the operator of its
own ([Prerequisites of activation](reference.md#prerequisites-of-activation)).

With `budget = true`, in merge mode `"auto"`, a turn of `implement`, `review`, `fix` or `sync`
that the envelope of its story refuses goes to `agent:needs-supervisor` as `budget_grantable`
instead of `agent:needs-operator` as `budget`, when every one of these holds, each verified by
the dispatcher:

- the conditions of **Routing** above;
- the story was **never extended**: the extension is once per story, ever;
- the envelope refuses the turn at a limit (`LIMIT_REACHED`), and the envelope with the
  extension would cover it now. The extension adds to every turn cap (in total, per role and per
  kind of phase) and to `wall_seconds` **25%** of the value frozen at the story's first booking,
  **rounded down** (`floor`), so that it never goes beyond 25%: 10 turns get 2 more, 3 turns
  none. It never adds dollars nor tokens. An extension that would not cover the turn (a nothing
  added to the exhausted cap, a dollar cap, an outcome still uncertain, a role not in the
  envelope) is not offered and does not spend the story's extension: the turn goes to the
  operator as today;
- the daily cap of the delegations is not reached (`max_grants_per_day`, shared with the rounds
  granted), read from the [turn journal](protocol.md#turn-journal).

The tolerance is a constant of the code (`BUDGET_TOLERANCE_PERCENT` in `devloop/supervision.py`,
ADR 0005, D3). Otherwise the escalation is `budget`, to the operator, and with the delegation on
its comment says which condition failed. The supervisor answers a `budget_grantable` escalation
with:

```text
/devloop grant-budget <reason>
```

The reason is required and carries the analysis of the causes, as for `grant-round`. As a retry,
the extension is bound to the input of the refused turn: on an issue a merged devloop PR already
resolved the command is refused and the issue becomes a `merged_issue` escalation, and when the
input changed after the escalation (the PR's head, the issue's base or request) the command is
refused and spent while the escalation stays with the supervisor; nothing is extended in either
case. Before granting, the dispatcher checks every condition again on data read then; a
delegation turned off, a story extended in the meantime, a cap reached or an extension that
would no longer cover the turn hand the item to the operator as `budget`. The extension is
written in the turn journal
first, in one step with the checks that the story has none and that the daily cap is not
reached: two commands applied together never extend a story twice. From then on it is part of
the envelope of the story, across restarts: the frozen limits stay those of the first booking,
and the journal's extension is added on top of them. A PR goes back to `agent:review` or
`agent:changes`, where it was; an issue leaves `agent:needs-supervisor` and its implementation
runs at once, as after a retry: as a retry it takes a free PR slot of `max_open_prs`, waiting for
a later tick without one, and while the issue is blocked by an open issue the command waits,
unspent, taking no slot. The grant leaves a `grant` event in the turn
journal with the story and the extension, a ledger `event` `delegation_grant` and a
`supervision` record with outcome `granted`, in this order (see
[Supervision trace](protocol.md#supervision-trace)): a dispatcher stopped before the record
completes the same command at its next tick, before any later command and even on an issue
resolved meanwhile, without extending the story again. When the stop was a GitHub error that
handed the item to the operator, or a later escalation came, the records are completed all the
same, and the item stays where it is, without resuming the step;
`devloop status` and the refusals show the extension in the state of the budget. A later turn
the extended envelope refuses goes to the operator as `budget`: a second extension, the dollars
and the values of the envelope stay the operator's (ADR 0004, D2).

**The grant books the extension; the turn spends it** (#466). A PR's turn runs at a later tick,
and may wait for capacity or a reviewer: meanwhile even the extended deadline (`wall_seconds`)
may pass. The extension is spent by the first turn of the story booked after the grant. If that
turn is refused all the same, before any turn used the extension, the extension lapses: the
journal records a `grant_lapsed` event, the envelope goes back to the frozen one, and the story
keeps its one extension. The refusal says so and goes through the delegation again, to the
supervisor as `budget_grantable` when an extension would cover the turn now, otherwise to the
operator as `budget` (for a deadline already past, an extension of 25% of `wall_seconds` usually
no longer covers it). The lapsed grant still counts against the daily cap of its day, and the
same command never applies again; a story never has more than one extension in force. Only the
refusal of a booking makes it lapse: the read-only check of the envelope before a conclave call
or an advisory review, and `devloop tick --dry-run`, refuse the turn and leave the extension
booked.

**The coordinator** (#595, [ADR 0002, precision of 2026-11-23](../adr/0002-ruoli-e-responsabilita.md#precisazione-del-2026-11-23-compiti-del-coordinatore-594)).
With `[roles.coordinator]` the coordinator's identity writes the same command, with the same
reason. On a `budget_grantable` escalation it counts as the supervisor's. On a `budget`
escalation to the operator, the coordinator grants the same one extension whatever
`[delegations] budget` and the merge mode say, when the latest record that put the item in
`agent:needs-operator` is that escalation and every condition holds, each verified then: the
input of the refused turn unchanged (the PR's head, the issue's base and request), an issue no
merged devloop PR resolved, the story never extended, an extension that covers the turn and the
daily cap of the grants (`max_grants_per_day`) not reached. The escalation records the story,
the kind and the agent of the refused turn for this; one written before #595 does not, and the
command is refused. Granted, the extension is recorded as above, with `role: coordinator` in its
`supervision` record, and the item resumes as after a retry, leaving `agent:needs-operator`; on
an issue, as above, the command takes a free PR slot (`max_open_prs`, and none while
`max_parked_prs` is reached) and waits, unspent, while the issue is blocked by an open issue. A
refusal is answered, spent, and leaves the item with the operator. A second extension of a story
stays the operator's. If the dispatcher stops after the journal recorded the coordinator's
extension, the next tick completes the records only, and the item stays in
`agent:needs-operator`: a `/devloop retry` resumes it, in the extended envelope.

**Revocation.** `budget = false` revokes the delegation from the next tick, as for `rounds`: a
refused turn goes to the operator, and a `grant-budget` on an escalation already with the
supervisor is refused and hands the item to the operator. A trusted user's `grant-budget` on an
escalation to the operator does not apply; the coordinator's applies as described above, until
`[roles.coordinator]` is removed.

**Trace.** Every command applied leaves a dispatcher record, described in
[Supervision trace](protocol.md#supervision-trace).

#### Temporary capacity limits

A delegation of the operator to the supervisor (#412, [ADR 0005](../adr/0005-deleghe-e-ruoli-degli-agent.md#capacità),
P5-8), **off by default**: `[delegations] capacity = false`. It lets the supervisor raise or lower
`max_open_prs` or `max_parked_prs` for a while, without touching the configuration or the
overlay, which stay the operator's. It needs `[report] issue`: the command is written there, on
the issue of the [daily summary](#daily-summary), never on an escalation. With `capacity = true`
and no `[report] issue` devloop does not start.

With `capacity = true`, in merge mode `"auto"`, the supervisor (or a trusted user) writes, as the
first line of a comment on that issue:

```text
/devloop capacity <key> <value> <duration> <reason>
```

- `<key>` is `max_open_prs` or `max_parked_prs`. `max_parallel_actions` is refused: it sizes the
  workers when `devloop run` starts, so only a restart applies it, and only the operator changes
  it. Any other key is refused.
- `<value>` is a whole number from 1 to the cap of the key: **6** for `max_open_prs` and **6** for
  `max_parked_prs`. A value lower than the configured one applies too.
- `<duration>` is `<N>h` or `<N>m`, at most **24 hours**, counted from when the command was
  written: a command the dispatcher reads late applies only for what is left, and one whose
  duration is already over is refused.
- `<reason>` is required, may go on in the lines below, and carries the analysis of the causes,
  or cites it in the diary issue, as for [granting a round](#granting-a-review-round).

The caps are constants of the code (`MAX_CAPACITY` and `MAX_CAPACITY_HOURS` in
`devloop/supervision.py`); `capacity_max_open_prs`, `capacity_max_parked_prs` and
`capacity_max_hours` in `[delegations]` may only lower them, and a higher value is refused at
startup. Every grant counts against the daily cap of the delegations (`max_grants_per_day`, 3,
with the rounds granted), checked and written in the [turn journal](protocol.md#turn-journal) in
one step: without a journal that can be read no grant applies.

At every tick, with the delegation on, the dispatcher reads the comments of the issue, answers the
commands written after the latest one it answered, and plans with the limits in force: for each
key the latest grant, until its end. The configuration itself does not change: when the limit
expires the next tick plans with the configured value again, and the log says
`capacity: <key> back to the configured <value>`. A later command on the same key replaces the
earlier one, even when it ends sooner. A grant whose value is above a cap the operator lowered
afterwards stops applying. When the issue cannot be read, the configured values apply. A command
from anyone but the supervisor's identity, a trusted user or (for `capacity` only) the
coordinator's identity is ignored and only logged;
`/devloop merge-budget` is answered as in [Delegated merge](#delegated-merge), any other
`/devloop` command on the issue as not applicable.

Each answer is a `supervision` record on the issue (see
[Supervision trace](protocol.md#supervision-trace)): `refused`, with why, changes nothing; a grant
(`granted`) records the key, the value, the configured value, the cap and the end, and the
same figures go in a ledger `event` `delegation_grant` and in a `grant` event of the turn journal.

With the delegation off, the dispatcher still reads the issue at every tick that is not a dry run
and answers each command with a `refused` record, "not applicable": the delegation is off. The
answer spends the command, so turning the delegation on later grants nothing written before: the
supervisor writes the command again.

A value or a duration in characters other than the ASCII digits `0`-`9` (such as `²`), or with
more than 9 digits, makes the command malformed: it is refused, and the tick goes on.

`devloop status` reads the issue like a dry run, without answering any command, and shows the
counters and the `next:` lines with the limits in force, the same a tick plans with.

**The coordinator** (#595, [ADR 0002, precision of 2026-11-23](../adr/0002-ruoli-e-responsabilita.md#precisazione-del-2026-11-23-compiti-del-coordinatore-594)).
With `[roles.coordinator]` the coordinator's identity writes the same command, on the same issue,
under the same rules: the caps of the code (`MAX_CAPACITY`, `MAX_CAPACITY_HOURS`) and the lower
ones of `[delegations]`, every grant counted against `max_grants_per_day`, merge mode `"auto"`.
For the coordinator the delegation is on with `[roles.coordinator]`, whatever `[delegations]
capacity` says: that key stays the switch of the supervisor's. Its records carry
`role: coordinator`, and its limits are in force only while `[roles.coordinator]` is on; the
supervisor's and the trusted users' only while `capacity = true`. The latest grant of a key
is the one checked: when its switch is off, the configured value applies, never an earlier grant
it replaced. Its `/devloop merge-budget`
stays the operator's: it is ignored and logged.

**Revocation.** `capacity = false` revokes the delegation from the next tick: the limits in force
stop applying, and the commands written from then on are refused as not applicable. Removing
`[roles.coordinator]` does the same for the coordinator's limits.

#### Delegated merge

A delegation of the operator to the supervisor (#486, [ADR 0005](../adr/0005-deleghe-e-ruoli-degli-agent.md#4-decisioni-riservate-alloperatore),
precision of 2026-11-21 to D2 and D10), **off by default**: `[delegations] merge = false`. It lets
the supervisor merge a ready PR whose merge is the operator's
([an operator merge](#ready-prs-whose-merge-is-the-operators)) when that merge does not really
need the operator. It needs `review_policy.advisory_reviewer`: with `merge = true` and no advisory
reviewer devloop does not start.

With `merge = true`, in merge mode `"auto"`, the supervisor's identity writes, as the first line
of a comment on such a PR, in `agent:ready-to-merge` with the dispatcher's own
`agent:needs-operator` and its comment "Il merge spetta all'operatore":

```text
/devloop merge <sha> <reason>
```

`<sha>` is the full id (40 characters) of the head to merge; `<reason>` is required and may go
on in the lines below. The dispatcher plans the command in the [merge lane](#the-merge-lane) and,
just before merging, verifies every condition on `<sha>`, mechanically, with no other attester.
When one does not hold, or cannot be verified (a record, a diff or a comment that cannot be
read), the command is refused, the answer says why, and the PR stays with the operator:

- `<sha>` is the PR's head and the head the latest round approved; the approvals of the quorum
  match the policy receipt recomputed now, no finding is open, the acceptance map holds, GitHub
  reports `MERGEABLE` and `CLEAN`, and the CI is `pass`;
- each reviewer of the quorum, on its latest review of `<sha>`, has `operator_reason` `none` or
  `decision`; one `trust_policy` (a change no decision of the operator covers), or a
  `trust_policy` of an earlier round that still holds the merge, refuses it;
- the [advisory review](#advisory-review-for-the-operator) of `<sha>` has the opinion `ok` or
  `dubbi`: `contrario`, a failed one, or none refuses it;
- the PR declares no [step on the host](#step-on-the-host) (`host_step`): a step on the host as
  root stays the operator's;
- the diff touches no file of the trust rule: `AGENTS.md`, `CLAUDE.md`,
  `devloop/prompts/review.md`, `devloop/prompts/advisory.md`, `devloop/prompts/preflight.md`,
  `devloop/policy.py`, `devloop/supervision.py` (which holds this list), `devloop/context.py`,
  `ops/host/ssdlc/project.toml` (`TRUST_RULE_PATHS`, a constant of the code that
  `[delegations] merge_trust_paths` may only lengthen), nor a file where the delegations, their
  caps, `operator_only_paths` and the merge conditions are set and applied: `devloop/config.py`,
  `devloop/config.toml`, `devloop/dispatch_merge.py`, `devloop/dispatch_supervision.py`
  (`DELEGATION_PATHS`). devloop cannot tell from the diff whether a change there touches them,
  so it refuses every one;
- the work of the PR did not restart after a [conclave](#after-a-unanimous-decision), and the
  [merge mode](#merge-modes) of the PR without its paths under `docs/adr/` is `"auto"`: any other
  operator-only path, or a level overridden to `"off"` or `"strict"`, stays the operator's;
- under `docs/adr/` the diff has only added lines, as plain text (no line removed or changed, no
  file removed, binary or with a new mode), and each block of consecutive added lines carries
  the link to a comment of this repository
  (`https://github.com/<repo>/issues/<N>#issuecomment-<id>`, or `/pull/<N>`) whose author, read
  now with the API, is in `trusted_users`: the record of a decision of the operator already
  written on GitHub, never a new one. A link under a former name of the repository
  (`repo_aliases`, [rename of the repository](reference.md#rename-of-the-repository)) counts
  the same way, only when that comment is found by id among the comments of issue `<N>` in
  `repo`: a comment that is not there (a 404) is refused;
- no `/devloop hold` of the supervisor or of a trusted user was written after the command: a
  trusted user cancels a command still waiting this way;
- the daily cap of the delegated merges is not reached.

With every condition true devloop merges the PR with the merger's app, as an automatic merge:
the same `merged` record, with the command, its author, its comment and its reason in
`delegation`, the same receipt, and the issue closed. Then a `supervision` record `merged` on the
PR spends the command. A refused command is answered with a `refused` record and spent: the
supervisor writes a new one once the cause is gone. A `/devloop merge` from a trusted user is
answered as not applicable (the operator merges by hand); from anyone else, an agent's app
included, it is ignored and only logged; on an escalation to the supervisor it is refused.

**Daily cap.** At most **5** delegated merges per UTC day (`MAX_DELEGATED_MERGES` in
`devloop/supervision.py`; `[delegations] max_merges_per_day` may only lower it). It is a cap of
its own: delegated merges do not take the common daily cap of the grants (`max_grants_per_day`,
3, for rounds, budget and capacity), a deliberate exception decided by the operator. The cap is
checked and the merge recorded in the [turn journal](protocol.md#turn-journal) in one step,
before the merge: without a journal that can be read no delegated merge runs. A trusted user, and
only a trusted user, extends the cap of the current UTC day with a comment on `[report] issue`
whose first line is

```text
/devloop merge-budget <N> <reason>
```

with `N` from 1 to 20 and a reason. The commands of the same day add up; the next day the cap is
the configured one again. The dispatcher answers each one at the next tick with a `supervision`
record on that issue, `granted` with `N`, the day and the cap of the day, or `refused`: from the
supervisor's identity, with the delegation off, malformed, or written on a day that is over. The
command of an app other than the supervisor's is ignored and only logged. When `[report] issue`
cannot be read, the cap of the configuration applies, never more. The
[daily summary](#daily-summary) lists every delegated merge with its reason, and every extension
with its author and reason.

**Revocation.** `merge = false` revokes the delegation from the next tick: a command waiting is
refused as not applicable.

### Commands on escalations to the operator

On an issue or PR in `agent:needs-operator` (#321) a user in `trusted_users` may use the same
commands, in every merge mode, written after the record that put the item there: the latest
dispatcher notice (an escalation, an agent that stopped, an [operator merge](#ready-prs-whose-merge-is-the-operators),
which counts from the promotion it was last written for), escalation of the review level
(`review_level`, `level_zero`) or failed readiness check. When the item moved on after that
record (a command applied, a passed readiness check, a review, a fix, a sync, a policy record
that is no escalation), the label is no longer that record's, for example one added by hand:
no command applies, and the answer gives the manual steps. The commands valid there are:

- `/devloop retry`, when that record is an escalation that records the step that failed and the
  key of its input. Every escalation of a failed agent, `gh` or git step (`tool_failure`,
  `failure`, `untrusted`) and of a sync with conflicts that failed (`sync_failed`,
  `sync_sensitive`, `tool_failure`) records it, also when it goes to the operator; so do `supervisor_escalated` and `supervisor_refused`
  of a `tool_failure`, `sync_failed` or `budget_grantable` escalation, which keep the step of
  the supervisor's escalation. Their comment says that `/devloop retry` applies. An issue whose readiness check
  failed this way keeps `agent:ready`, so that the check can run again. The
  class may be critical: a retry runs the step again and bypasses none of its checks. The
  conditions are the supervisor's: the input of the step must still be the one that failed (the
  same head, base and request; for an implementation the request only, #475), the issue must
  not be resolved by a merged devloop PR, a blocked
  issue waits, and at most **two** retries run for the same key, the supervisor's and the
  operator's together (so not after `retry_limit`). The readiness check runs again only on an
  issue that still has `agent:ready`, or `agent:working` while its implementation waits for the
  base branch ([Tests red on the base too](#tests-red-on-the-base-too)). devloop removes `agent:needs-operator` (and
  `agent:needs-supervisor`); a PR goes back to the label of its step and the next tick runs it,
  an issue's step runs at once.
- `/devloop accept-defaults` and `/devloop decide <decisions>`, when that record is a failed
  readiness check of class `preflight_default`, or the supervisor's escalation of one handed to
  the operator (`supervisor_escalated`, `supervisor_refused`), under the conditions of the
  supervisor's commands above. devloop records the defaults or the decisions and removes
  `agent:needs-operator`, and the issue goes back in the queue. On a failed check, an ordinary
  comment of a trusted user with the decisions works as well
  ([Readiness check](#readiness-check-before-implementation)): the dispatcher's answers to
  commands leave the block to the check.
- `/devloop grant-round <reason>`, when that record is a `round_limit` escalation of a PR: one
  more review round for that PR only, as described in
  [Granting a review round on an escalation to the operator](#granting-a-review-round-on-an-escalation-to-the-operator)
  (#553).

`/devloop escalate` never applies: the escalation is already the operator's. A command of the
supervisor's identity on an escalation to the operator does not apply either, except its
`/devloop rereview <reason>` on the `tool_failure` of the review of a PR whose merge is the
operator's (#491, see the supervisor's commands above), and its `/devloop merge` on an operator
merge, with `[delegations] merge` on ([Delegated merge](#delegated-merge)); a trusted user's
command waiting on the same item wins over the rereview, and a trusted user's
`/devloop rereview` is no command here. A trusted user's `/devloop merge` does not apply: the
operator merges by hand. Every command that
does not apply gets a dispatcher comment with the reason and how to resume (the manual steps
above, `/devloop recheck` for a readiness check, the manual merge for an operator merge), and the
item stays in `agent:needs-operator`. A command from outside `trusted_users` and the supervisor's
identity is only logged. The trace is the one of the supervisor's commands, under the title
"Comando su un'escalation all'operatore".

A turn left without an outcome, because devloop stopped while it ran, becomes `uncertain` in the
[turn journal](protocol.md#turn-journal) and is never run again automatically. With
`block_uncertain_turns = true` (off by default) it also refuses every new turn of the same issue
and its PRs, with an escalation of class `uncertain_turn` that lists the turns; so does a turn
whose outcome could not be written, from the next turn of the same step (a retry, a fallback)
onwards, and a legacy turn imported from `ledger-open-turns.json` on the issue or on one of its
PRs, open or closed (read on GitHub to find its issue; until it can be read, it blocks every
work). A conclave call refused this way ends that conclave with the escalation; an advisory
review refused this way escalates once for those turns and waits for their reconciliation.
Only a trusted
user reconciles them, with a comment whose first line is
`/devloop reconcile <turn id> <returned|failed|not_dispatched>` and whose next lines give the
reason, mandatory. The outcome is recorded once, also with the block off; then the step resumes
as from any escalation to the operator.

With `story_identity = true` (#363, off by default) the takeover of an issue also records its
[story](protocol.md#story-identity), the unit of the future budget: the journal assigns one to
the issue, or adds the hash of the new request to the one it has, and the comment of the
`request` record carries the dispatcher's `story` marker. An issue belongs to another's story
only through that marker of the dispatcher, never through what its text, an agent or a user
writes. With it off, and for issues taken over before, nothing changes.

With `attempt_identity = true` too (#384, off by default) the implementation of a story's root
runs in an [attempt](protocol.md#attempt-identity), opened in the journal before its first turn:
its PR marker and its turn records carry it. Retries before the PR resume the same attempt; a new
one starts only once the PR of the previous one is merged or closed, and is refused while that PR
is open. Each attempt has its own branch and PR: the first keeps `agent/issue-<n>`, the next ones
use `agent/issue-<n>-attempt-<k>`, so a new attempt never overwrites the branch of the previous
one. A PR reopened after its attempt concluded, or whose marker lost the attempt the journal ties
to it, is escalated (`attempt`). PRs opened before, without an attempt in their marker, are
handled as before.

The budget cannot be turned on yet: `[budget] enabled = true` stops devloop at startup until the
operator lifts the gate of its activation. A story at its limit can now be extended once, by the
supervisor ([Extending the budget envelope](#extending-the-budget-envelope), ADR 0005, off by
default); lifting the gate stays a decision of the operator. The
[prerequisites of activation](reference.md#prerequisites-of-activation) list what is settled
and what is still open (#396). What follows describes the envelope as it works once on.

With `[budget] enabled = true` (#365, off by default; it needs `story_identity = true`) each turn
of an agent is booked, before its CLI starts, in the [budget envelope](reference.md#budget-envelope)
of its story: turns in total, per role and per phase, time, and dollars where a CLI attests them.
A turn the envelope does not cover is refused before it starts, with an escalation of class
`budget` that says which limit and how much the story has used; so is a turn of an issue with no
story, such as one taken over before `story_identity` (excluded, and named by `devloop status`).
Consumption is cumulative across child issues, attempts, fallback models, retries and restarts,
and nothing refunds a booking. A story keeps the limits of its first booking, recorded in the
journal, and its time starts there: a later change of `[budget]` applies to new stories only, and
a role configured later escalates as `ROLE_NOT_IN_ENVELOPE`. Every agent declares a rule for
unknown usage, claude included; agy and codex accept only `unmetered_quota`, with the turn caps.
The readiness check stays outside the envelope. The
[state of the budget](reference.md#state-of-the-budget) of each story (#393), consumed, reserved
and available per dimension, with the dollars no CLI reports shown as `non attestato` and never
as 0, appears in `devloop status`, in a section of the PR receipt and in the escalation of a
refused turn. With it off, devloop books nothing, shows no budget and behaves as before.

With `publication_outbox = true` (#385, off by default), every comment, native review and pull
request devloop writes is saved in the same journal before it is sent, through the
[publication outbox](protocol.md#publication-outbox). A write whose outcome is uncertain (a
timeout, a 5xx answer) is looked for on GitHub before anything else is sent: found, it is not
sent again; not found once it can no longer take effect (30 seconds after the attempt, waited
for in the step), it is sent once more. After a restart, the first tick finishes what was left
half published, without duplicates: a write cut short is sent again only when it is still absent
30 seconds after the restart (counted from when the new dispatcher first read the journal, never
from when the write was recorded). The `gh` of each write holds the dispatcher lock while it
runs: if the dispatcher is killed while one of them runs, the next dispatcher does not start
(`another dispatcher is running (lock held)`) until that `gh` has ended, so that it never sends
again a write the `gh` may still publish. That `gh` runs under coreutils `timeout` (#397): it
ends at most about 5 minutes after it started, even with its dispatcher gone, and so does the
wait; a `gh` left stopped (SIGSTOP) is not counted by that clock, and keeps devloop from
starting until it is ended (`pgrep -a gh` lists it). With the outbox off, no `gh` holds the lock,
and a killed dispatcher restarts at once, as before #385.
A native review whose outcome stays uncertain is not retried by the review step: the outbox
settles it. A line `publication <id> ... not settled` in
the log is a write that could not be checked on GitHub yet, or not conclusively: the next tick
tries again, and nothing new is sent on that issue or PR until it is settled. If the journal
cannot take an outcome, nothing is sent until it does. A write the outbox dropped, never to be
published, is reported to the operator (#397): a `WARNING: publication <id> ... dropped` line in
the log at each start and, with `[run].alert_issue`, one alert on that issue, once per write; its
body stays in the journal.

The journal has a limit, `journal_max_mib` (64 MiB by default, #397). From 80% of it devloop
warns the operator with the current size, once per threshold on `[run].alert_issue`, or with a
`WARNING: the journal of the turns is <n> bytes` line in the log at each start without it; the
turns go on. Beyond the limit the journal is not loaded and takes no event: no turn starts, and
devloop removes nothing from it and has no compaction of its own: raise the limit in the local
overlay before it gets there.

#### Granting a review round on an escalation to the operator

A decision of the operator on #553: a PR at its round limit with a `round_limit` escalation in
`agent:needs-operator` gets one more round, for that PR only, when a user in `trusted_users`
writes, after that escalation:

```text
/devloop grant-round <reason>
```

`max_review_rounds` stays the default of every PR and does not change: the round raises the
limit of this PR only, as a round of the [delegation](#granting-a-review-round) does, and in
every merge mode, with the delegation on or off. It is **outside the caps of the delegation**,
which stay apart: it takes neither the rounds the supervisor may grant to the PR
(`max_rounds_per_pr`) nor the grants of the day (`max_grants_per_day`), and a later round limit
of the same PR may still go to the supervisor as `round_grantable` when the delegation applies.
The dispatcher checks, on the PR read then:

- the reason is there, and may go on in the lines below the command: a command without it is
  answered as malformed and the PR stays where it is;
- the record that put the PR in `agent:needs-operator` is a `round_limit` escalation: on an
  escalation of any other class (`supervisor_escalated` included) the command is refused;
- the head of the PR is the one the escalation recorded: a head moved since, or a `round_limit`
  escalation written before #553, which records none, refuses the command;
- the [turn journal](protocol.md#turn-journal) can take the grant: without one nothing is
  granted.

Each refusal is answered with why, spends the command, and leaves the PR in
`agent:needs-operator`. Granted, the round is recorded as those of the delegation, with its
author: a `grant` event in the turn journal (delegation `operator_rounds`, which counts against
no daily cap), a `supervision` record with outcome `granted`, `operator: true`, the head and the
rounds granted this way to the PR (`granted`), which raises its limit and spends the command, and
a ledger `event` `delegation_grant` with the same figures (see
[Supervision trace](protocol.md#supervision-trace)). The PR goes back to `agent:review` or
`agent:changes`, where it was, and the operator labels go. Another round needs another command,
at the next round limit.

The supervisor's identity cannot use the command there, as any command on an escalation to the
operator. The coordinator's identity may, as a trusted user, at most **one** round per PR
(`MAX_COORDINATOR_OPERATOR_ROUNDS` in `devloop/supervision.py`), beyond the caps of its
delegations: once the PR has a round granted on an escalation to the operator, by the coordinator
or by a trusted user, the coordinator's command there is refused and a further round is the
operator's. Its record carries `role: coordinator`, the cap and what remains.

### The coordinator

The coordinator is an agent, run periodically and on the alarms, that verifies the work of the
supervisor, writes the problems of the system as issues and prepares the operator's decisions
(the [four roles of ADR 0002](../adr/0002-ruoli-e-responsabilita.md#precisazione-del-2026-11-22-modello-a-quattro-ruoli-524-527),
#524). devloop does not run it: it knows only its identity, the role app `[apps.coordinator]`,
which counts with `[roles.coordinator]` in the local overlay, **off by default** (#528;
[The coordinator app](reference.md#the-coordinator-app)). Without that table nothing changes:
the coordinator's app is anyone else's.

With the table, the commands of the coordinator's identity have a **closed perimeter**, fixed in
the code. `capacity`, `grant-budget` and the priority labels joined it with #595, as the operator
decided in the [precision of 2026-11-23](../adr/0002-ruoli-e-responsabilita.md#precisazione-del-2026-11-23-compiti-del-coordinatore-594)
of ADR 0002:

| Command | From the coordinator |
|---|---|
| `retry`, `accept-defaults`, `decide` | Delegated: as a trusted user's, on the escalations to the supervisor (in merge mode `auto`) and on those to the operator ([Commands on escalations to the operator](#commands-on-escalations-to-the-operator)), under the same conditions. `decide` is refused, and the issue stays or goes to the operator, when the readiness check declares the trust policy touched (`trust_policy` other than `none`, or not declared); the gaps of credentials and budget are never `default_available`, so neither `decide` nor `accept-defaults` reaches them |
| `rereview` | Delegated: as a trusted user's, the request of a new review of the head after the latest verdict ([Resuming after a `needs_operator` verdict](#resuming-after-a-needs_operator-verdict-without-code-changes)), with or without a reason; its text never reaches the reviewers' prompt |
| `recheck` | Delegated: a new [readiness check](#readiness-check-before-implementation), as a trusted user's |
| `fix` | Delegated: as a trusted user's ([Fix requested by the operator or the supervisor](#fix-requested-by-the-operator-or-the-supervisor)) |
| `hold`, `release` | Delegated: as a trusted user's, in merge mode `auto` ([Holding a merge](#holding-a-merge)) |
| `grant-round` | Delegated: as a trusted user's, on a `round_grantable` escalation to the supervisor within the caps of the delegation ([Granting a review round](#granting-a-review-round)), and on a `round_limit` escalation to the operator at most **one** round per PR, beyond those caps; a further round on the same PR is the operator's ([Granting a review round on an escalation to the operator](#granting-a-review-round-on-an-escalation-to-the-operator), #553) |
| `capacity` | Delegated (#595): a temporary capacity limit on `[report] issue`, as the supervisor's, within the same caps and counted in `max_grants_per_day`, in merge mode `auto`; on with `[roles.coordinator]`, whatever `[delegations] capacity` says ([Temporary capacity limits](#temporary-capacity-limits)) |
| `grant-budget` | Delegated (#595): the one extension of a story's budget envelope, under the conditions and caps of the supervisor's, on a `budget_grantable` escalation to the supervisor and on a `budget` escalation to the operator; a second extension stays the operator's ([Extending the budget envelope](#extending-the-budget-envelope)) |
| labels | Delegated: its `agent:ready` added again and (#595) its priority labels count, as the supervisor's, only on issues opened by a trusted user or by a devloop app; its dependency and implementer labels never count ([Labels from the supervisor](#labels-from-the-supervisor)) |
| `approve`, `merge-budget`, `reconcile`, `trusted_users` and the keys of trust | Reserved to the operator in person: never the coordinator's. On an escalation they are answered as not applicable and spent; elsewhere they are ignored and logged, as anyone else's; `trusted_users` and the overlay are the operator's |
| `merge` | The coordinator's reserved merge (#529): [The reserved merge of the coordinator](#the-reserved-merge-of-the-coordinator) |
| `vote` | Delegated (#605): the substitute vote in place of an unavailable member, only on a conclave waiting for it, bound to its comment and to the SHA-256 of its proposal, before its deadline ([Conclave on a block](#conclave-on-a-block)); elsewhere ignored, and never a command of an escalation |
| any other (`escalate`, `finding`, `veto`, ...) | Refused: on an escalation answered as not applicable and spent, elsewhere ignored and logged |

The coordinator writes as an agent, with the attribution header of AGENTS.md: its commands are
read on the first line after the header. Like the supervisor's, its comments never reach a
prompt as the operator's: only the dispatcher's records of what it applied do, and the request of
a `/devloop fix`, as the supervisor's.

#### The reserved merge of the coordinator

The coordinator merges the ready PRs whose merge is the operator's that the
[delegated merge](#delegated-merge) of the supervisor does not cover, after the quorum and with
the operator's decision ([ADR 0002](../adr/0002-ruoli-e-responsabilita.md#precisazione-del-2026-11-22-modello-a-quattro-ruoli-524-527),
"Merge riservato del coordinatore", #529). With `[roles.coordinator]`, its identity writes, as
the first line after the attribution header of a comment on a PR in `agent:ready-to-merge` with
the dispatcher's own `agent:needs-operator` and its comment "Il merge spetta all'operatore"
([an operator merge](#ready-prs-whose-merge-is-the-operators)):

```text
/devloop merge <sha> <reason>
```

The dispatcher plans it in the [merge lane](#the-merge-lane), as the supervisor's, and verifies on
`<sha>`, just before merging, the conditions of the delegated merge with these differences:

- neither `[delegations] merge` nor the merge mode counts, of `[merge]` or of the PR: the PR waits
  for the operator's merge for whatever reason (`[merge] mode = "off"`, a level `"off"`, a path
  reserved to the operator, a `trust_policy` of the reviewers);
- the files of the trust rule and of the delegations, `docs/adr/` (with any change, not only
  added lines with a link) and a [step on the host](#step-on-the-host) refuse nothing: those are
  the coordinator's cases. A PR with a step on the host is merged, and the step holds the
  auto-pin until the agent of the host does it, as for every merge;
- when a reviewer of the PR declared the trust policy touched (`operator_reason`
  `trust_policy`, in any round), or the diff touches `docs/adr/`, the issue the PR closes must
  have a valid `/devloop approve` of the operator: written in person by a trusted user after the
  latest change of the issue's text and before devloop took it on, with the text of the decision
  ("Approval with `/devloop approve`" in
  [Changes to the trust policy](#changes-to-the-trust-policy)), read now
  with the API. A decision recorded only in an ADR does not count;
- each reviewer of the quorum, on its latest review of `<sha>`, has `operator_reason` `none` or
  `decision`, never `trust_policy`, as for the delegated merge.

What stays as for the delegated merge: `<sha>` the PR's head and the approved one, the quorum, no
open finding, the acceptance map, `MERGEABLE` and `CLEAN`, the CI `pass`, the advisory review of
`<sha>` with the opinion `ok` or `dubbi` (without `review_policy.advisory_reviewer` every
coordinator's merge is refused), no work restarted after a conclave, no `/devloop hold` written
after the command, and the labels read again just before the merge call. When a condition does
not hold, or cannot be verified, the command is refused with why, spent, and the PR stays with the
operator.

With every condition true devloop merges the PR with the merger's app: the `merged` record
carries the command, its author, its comment, its reason and `role: coordinator` in
`delegation`, and its text says that the coordinator asked for it; the `supervision` record
`merged`, with `role: coordinator`, names the `/devloop approve` that covered it (`approval`)
when one was needed. The reserved merge needs the [merger app](reference.md#the-merger-app)
(`[apps.merger]`) whatever the merge mode, `off` included: without it the command is refused
before the daily cap is touched, and devloop never runs it with the local `gh` login.

**Daily cap.** At most **10** reserved merges of the coordinator per UTC day
(`MAX_COORDINATOR_MERGES` in `devloop/supervision.py`), a cap of their own, apart from the
delegated merges of the supervisor and from the grants. `[roles.coordinator] max_merges_per_day`
may only lower it (0 turns the merge off); no command extends it. The cap is checked and the
merge recorded in the [turn journal](protocol.md#turn-journal) (`coordinator_merge`) in one step,
before the merge: without a journal that can be read no reserved merge runs. The
[daily summary](#daily-summary) lists them, merged or refused, under "Merge riservati del
coordinatore".

### Advisory review for the operator

With `[review_policy] advisory_reviewer` set (#369; empty, and so off, by default: the local
overlay turns it on, for example `advisory_reviewer = "claude-reviewer"`), a devloop PR in
`agent:needs-operator` gets one more review for the person who decides on it: after an
escalation to the operator, or when its merge is the operator's
([Ready PRs whose merge is the operator's](#ready-prs-whose-merge-is-the-operators): `[merge]`
off, a level `off`, an `operator_only_paths` path). A PR also in `agent:needs-supervisor` waits
for the supervisor. The agent may be the PR's implementer: each turn is a new CLI process, so it
does not have the implementer's context, and having the same model it could never count for the
quorum anyway. Like the review prompt, the advisory one is blind to the author (#389): it names
neither the implementer nor its model, tells the agent it is not the author, and passes on the
issue and the PR description without attribution headers and markers (see
[What the reviewers read](protocol.md#attribution)). A change to the trust policy that needs
the operator gets the opinion `dubbi` or `contrario` and a first residual risk that says so (see
[Changes to the trust policy](#changes-to-the-trust-policy)).

- **One per head.** An `advisory` action, which takes a slot of `max_actions_per_tick`, runs one
  read-only turn of that agent in a disposable checkout of the head (`pr-<n>-advisory`), with
  the issue, the PR, its changed files and the reviews of the quorum, and the schema
  [`devloop/schemas/advisory.json`](../../devloop/schemas/advisory.json): a summary of the PR,
  the residual risks, what the operator should look at, points the reviewers of the quorum may
  have missed, the findings of the quorum whose evidence does not hold and the fixes that were
  not needed ("Finding senza evidenza o corretti senza necessità", #481), and a non-binding opinion (`ok`, `dubbi`, `contrario`). A head gets one turn,
  even when it failed or its publication did (an action that failed before its result was kept
  gets one retry, see below); a new head gets its own while the PR is still in
  `agent:needs-operator`. The PR is read again before the turn and after it: when it is no
  longer open, its head moved, or it left `agent:needs-operator` or went to
  `agent:needs-supervisor` before the turn ended, nothing is published.
- **Publication.** The result of the turn is kept in `<state_dir>/advisory-results.json` before
  anything is published. The native review comes first, then the receipt, then the dispatcher's
  summary, last: a head is done once it has that summary. When a write fails, a later tick
  publishes what is missing from the kept result, or from the native review already published,
  without a new turn; a PR back to the operator on the same head gets the result kept for it.
- **Failed actions** (#381). An action that fails before the result of its turn is kept (an
  unexpected error of the dispatcher, a git or GitHub failure) is recorded for its head in
  `<state_dir>/advisory-failures.json`, so that the limit holds across a restart: the head is
  not planned again for 60 seconds, then gets **one** more turn, and after a second failure no
  turn again (`no new turn on this head` in the tick log). A new head starts over. When the
  failure cannot be written to that file, the limit could not hold across a restart: the head
  gets no new turn until devloop restarts (`no new turn on this head until a restart` in the
  tick log). The turn of
  a failed action is always closed in the ledger, with outcome `failure`, never left `open`;
  each attempt writes its own turn record and nothing else. A result already kept, or a native
  review already published, is still published by the next ticks, without a turn.
- **Where.** A native GitHub review of type `COMMENT` on the head, never `APPROVE` nor
  `REQUEST_CHANGES` (GitHub accepts a `COMMENT` from the PR's author too), with the agent's
  identity and the title "Review consultiva: non conta per il quorum". The dispatcher then adds
  the opinion to the current [receipt](protocol.md#pr-receipt) of the same head, if any, and
  writes its summary for the operator ("Review consultiva per l'operatore"), with the agent and
  the opinion only. A failed turn gets only the summary, which says so.
- **No effect.** It opens no round and does not count against `max_review_rounds`, creates no
  finding, changes no label and no promotion; devloop reads it to know whether a head already
  has one and to show its opinion, and for one decision only: with the
  [delegated merge](#delegated-merge) on (`[delegations] merge`), a `/devloop merge` of the
  supervisor on a head is refused unless the advisory review of that head has the opinion `ok`
  or `dubbi`, and so is the coordinator's
  ([The reserved merge of the coordinator](#the-reserved-merge-of-the-coordinator), #529); with
  `contrario`, or none, the merge stays the operator's. It still stays out of
  the quorum. The prompt and the texts devloop publishes say so. A failed action is never escalated:
  the PR is already the operator's. The exception is a turn refused by the
  block on [uncertain turns](protocol.md#turn-journal) (`block_uncertain_turns`): it
  escalates as `uncertain_turn`, with the turns to reconcile, once for those turns (taking
  `agent:ready-to-merge` away, as every escalation does), and the PR gets no other advisory
  action until they are reconciled. So does a turn the [budget envelope](reference.md#budget-envelope)
  refuses: it escalates as `budget` once for that refusal on that head, and the PR gets no other
  advisory action on that head while the story does not cover the turn; a new head is tried
  again, and escalated again if refused.
- **Cost.** One turn per head, recorded in the [ledger](protocol.md#evidence-and-telemetry) as a
  `turn` of kind `advisory`, and counted in the totals of the receipt.

## Who does what

Roles (operator, coordinator, supervisor, dispatcher, preflight, implementer, reviewer), what
each one does and does not do, the merge modes and the critical and non-critical decisions are
defined in [ADR 0002](../adr/0002-ruoli-e-responsabilita.md). The ADR is the only source for
them, and this guide does not repeat them: it describes how devloop applies them, in
[Merge modes](#merge-modes), [Labels from the supervisor](#labels-from-the-supervisor),
[Escalations to the supervisor](#escalations-to-the-supervisor) and
[The coordinator](#the-coordinator). Which agent holds each role is
in `[roles]` of the configuration ([reference](reference.md#configuration)).

The agents of the roles that devloop does not run have a runbook each, valid for any CLI, with
their inputs, privileges, tasks and prohibitions: the [supervisor](roles/supervisor.md), the
[coordinator](roles/coordinator.md) and the [agent of the host](roles/host.md).
`ops/host/start-role.sh`, with the role (`supervisor`, `coordinator` or `host`), starts its
session in tmux, as
the role's user, with the CLI and the model of `[roles.supervisor]` or `[roles.coordinator]`
(for `host`, those the operator chooses at launch) and the runbook as its first message
([ops/README.md](../../ops/README.md#i-quattro-ruoli)).

## Usage

```bash
# Run directly, devloop needs the project file and the overlay of the state_dir, before the
# command (the launcher, scripts/devloop-pinned, passes them itself):
devloop="python3 -m devloop --project-config .devloop/project.toml --local-config .devloop/config.local.toml"
$devloop setup            # create the agent:* labels, the priority:* labels and the review level labels (reviews:0 only when level 0 is on)
$devloop status           # running action, devloop PRs (with rounds counted on max_review_rounds, merge state and conflicts), ready issues in start order with their priority, planned actions; with [budget] on, the budget of the stories in progress; a pinned copy behind origin/main in the files of the installation
$devloop apps             # check the GitHub App of each actor, on the project's repo and with its key_dir
$devloop tick --dry-run   # print the ready issues in start order and the plan, change nothing (no lock: it runs beside a dispatcher)
$devloop tick             # run one round
$devloop run --idle-timeout 900  # tick continuously (see Continuous loop)
$devloop cleanup          # remove worktrees left by failed steps
$devloop gc               # clean up state_dir now: old pinned copies, worktrees of closed issues and PRs, old logs (see Cleanup of the state directory)
$devloop follow issue-52  # follow live the implementer of issue #52 (pr-<N>: reviewer or fixer)
$devloop watch --timeout 3600  # wait for the next change on GitHub, for the supervising agent
$devloop pin origin/main  # pin a commit of origin, after its smoke test (see Run from a pinned installation)
$devloop report --day 2026-11-16  # print the summary of a UTC day from devloop's records (default: today; --publish: on [report] issue, see Daily summary)
$devloop fallback reset agy-reviewer  # forget the models agy-reviewer remembers unavailable (see Memory of unavailable models)
$devloop token claude -- gh pr comment 70 --body-file reply.md  # run a command as an agent's app of the project (see GitHub identities; scripts/agent-token.py calls it)
```

Run it from any checkout of this repository. Worktrees, agent prompts and logs, and the lock that
prevents two dispatchers from running at once live under `state_dir` (default `.devloop`)
in the main checkout. `tick --dry-run` writes nothing and does not take that lock, so the
[smoke test of a pin](#pin) runs it while `run` is active. Like `status`, it reads the
[turn journal](protocol.md#turn-journal) without writing it: the running dispatcher alone does.
For the same reason they never use the [publication outbox](protocol.md#publication-outbox).
Not even its git commands write to the repository: it does not fetch, so it plans from the remote branches as last fetched (a
branch never fetched leaves its merge mode not computable), and the objects that `git
merge-tree` writes go to a temporary directory. In daily use run it from the pinned copy, through
`scripts/devloop-pinned` ([Run from a pinned installation](#run-from-a-pinned-installation)),
which also passes the [project file](reference.md#project-file) of the `state_dir`: since
ADR 0006, R3 `devloop/config.toml` has no `test_cmd`, so run directly devloop needs
`--project-config <state_dir>/project.toml`, before the command, as in the block above (from the
main checkout).
`--local-config <file>`, before the command, applies a local overlay to the configuration
([Local config as an overlay](#local-config-as-an-overlay)); the launcher passes
`<state_dir>/config.local.toml` when it exists, and so does the block above. `state_dir` does not grow without
limits: see [Cleanup of the state directory](#cleanup-of-the-state-directory).

### Cleanup of the state directory

Every pin adds a copy `pinned-<sha12>`, every turn adds its prompt and its log to `logs/`, and
the worktrees of finished work stay in `worktrees/`. devloop cleans `state_dir` up (#380) as
`[gc]` sets ([Configuration](reference.md#configuration)). The cleanup runs at a tick, before
planning, when no action is running (in the [continuous mode](#continuous-mode) only at a tick
with none running), at most every `interval_minutes` (default 60; 0: at every such tick; the
time of the last one is in `state_dir/gc-state.json`), and at once with `devloop gc`, which
holds the dispatcher lock like a tick. `tick --dry-run` never cleans up. Each removal is a line
of the log that starts with `gc:`; a cleanup that fails is logged and never stops the tick.

- **Pinned copies** (`pins`, on by default). After a successful pin, by hand or
  [after a merge](#pin-after-a-merge), and at each cleanup, `current` and the `keep_pins`
  (default 5) other copies pinned most recently stay; the other `pinned-<sha12>` are removed.
  The order is the time of the last successful pin of each copy, a reused copy (a rollback)
  included, recorded in `state_dir/pin-times.json`; a copy pinned before that record existed
  counts with the time its directory was last modified. A copy is never removed while a process
  uses it: the copy the dispatcher runs from, or one named by the `PYTHONPATH`, the command line
  or the working directory of a process of the same user (read in `/proc`). Where `/proc` cannot
  be read, or a live process of the same user cannot be inspected (one that is not dumpable, as
  `ssh-agent`, hides its environment and its working directory), no copy is removed: it cannot
  be told which copies are unused. Such a copy stays, and a later cleanup tries again. The rejected
  copies (`rejected-<sha12>`) are never touched. A pin that runs meanwhile leaves the copies to a
  later cleanup.
- **Worktrees** (`worktrees`, on by default). The worktrees of a closed issue (`issue-<n>` and
  its `issue-<n>-…` copies) and of a closed or merged PR (`pr-<n>`, `pr-<n>-review`, …) are
  removed, their state read fresh from GitHub. Never one of an issue or PR with a turn still
  open in the ledger (`state_dir/ledger-open-turns.json`), nor one whose state cannot be read;
  when the ledger cannot be read, no worktree is removed. A worktree with another name is left
  to `devloop cleanup`.
- **Logs** (`logs`, off by default: turn it on in the
  [local overlay](#local-config-as-an-overlay)). The files directly in `state_dir/logs` not
  modified for `log_days` days (default 14) are removed, except every file of an open issue or
  PR in `agent:needs-operator` or `agent:needs-supervisor`: its evidence (`timeout-…` and
  `conclave-…` files) and the logs of its turns, which the escalation cites. Nor every file of
  an issue or PR with a turn still open in the ledger, nor an evidence whose name has no issue
  or PR. The state and the labels of each issue or PR are read fresh, one at a time, not from a
  list of the escalations that a limit of GitHub could cut short. When an issue or PR cannot be
  read its files stay, and when the ledger cannot be read every file of an issue or PR stays.

```toml
# .devloop/config.local.toml
[gc]
logs = true
log_days = 30
keep_pins = 3
```

### From `agent:needs-human` to `agent:needs-operator`

Before #248 the label `agent:needs-operator` was called `agent:needs-human`, and the reviewers'
verdict `needs_operator` was `needs_human`. The configuration has no alias: `[labels]` is not
configurable, only the default name changed.

- **`devloop setup` renames the label** when the repository has `agent:needs-human` and not
  `agent:needs-operator`: it edits the label's name (with its new colour and description,
  "devloop: serve una decisione dell'operatore"), so the issues and PRs that have it keep it,
  and logs `labels: renamed …`. When **both** labels exist it renames nothing and touches
  neither: it logs a warning, and `devloop status` shows the same warning while both exist. Move
  the issues and PRs of the old label to the new one, then delete the old one.
- **The first tick of each process does the same**, before planning, in `devloop tick` and in
  `devloop run` (also in its continuous mode), so that the label the dispatcher adds exists
  even if nobody ran `setup` after the update: it renames the old label, or logs the warning
  when both exist. `devloop tick --dry-run` only logs `[dry-run] labels: would rename …`. A
  read or a rename that fails is logged and tried again at the next tick; meanwhile the old
  label keeps blocking as the new one.
- **Until the rename**, and while both exist, `agent:needs-human` blocks exactly as
  `agent:needs-operator` wherever that label blocks or authorizes: planning, merge, promotion,
  readiness check, the supervisor's commands. The tick does not stop. Who added the label counts
  under both names (the events recorded before the rename keep the old name), and the
  dispatcher removes only the label it added itself, under whichever name it has.
- **Records already published** are read as before: a review marker with the verdict
  `needs_human` counts as `needs_operator`.

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
While a restart from a new pinned copy waits for the running actions
([Pin after a merge](#pin-after-a-merge)), `state_dir/restart.json` records it, and
`devloop status` shows `restart to <sha12> pending since <time> (<N> running): no new implement
until the restart` as long as its dispatcher holds the lock.

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
| devloop review in the body of the native review of an agent with its own app (#325), from that app | `PR #N review by brushtyler-devloop-codex-agent[bot]: approve round 2 @abc1234` |
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
(`.devloop/STOP` of the main checkout), stop the loop: the tick in progress ends, no other
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
stop the loop first. The `gh` of each GitHub write holds the lock too while it runs (#385): one
that outlived a dispatcher killed meanwhile keeps the next one from starting until it ends. A [pin after a merge](#pin-after-a-merge) restarts the loop from the new
copy with the same arguments and the lock still held, unless a stop was requested meanwhile.
With `repo_lock_dir`, the loop and a tick hold the lock of the repository too, and a dispatcher
on the same repository with another `state_dir` does not start
([Several projects on one host](#several-projects-on-one-host)).

**Activation is the operator's.** The loop runs unsupervised and spends the agents' quotas, so
devloop installs no service. An example unit, `devloop.service`, for the
[installation as a dedicated user](#installation-as-a-dedicated-user), running the
[launcher](#launcher) of the pinned copy, which applies `.devloop/config.local.toml` when
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
WorkingDirectory=/home/devloop/devloop
# Where the agent CLIs, gh and git are found for the devloop user.
Environment=PATH=/home/devloop/.local/bin:/usr/local/bin:/usr/bin:/bin
# The launcher adds --local-config .devloop/config.local.toml when that file exists.
ExecStart=/usr/bin/python3 /home/devloop/devloop/scripts/devloop-pinned run --idle-timeout 900
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
sudo -u devloop touch /home/devloop/devloop/.devloop/STOP  # stop after the tick, and stay stopped
sudo -u devloop rm /home/devloop/devloop/.devloop/STOP     # then `systemctl start devloop`
```

After an alert (exit code 4) the unit stays stopped: remove the cause, then
`systemctl start devloop`.

The host that develops this repository runs, since #563, the instance `devloop@devloop` of the
template unit of [`ops/`](../../ops/README.md) (`PROJECT=devloop`), with the same users: there the
commands name `devloop@devloop` instead of `devloop`.

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
`max_open_prs`. A global action (a pin) starts only when nothing runs, alone: while one is
planned and actions are running, no other action starts, so they end and the global action runs
next; the actions planned with it start in the ticks after it. While a global action runs, no
other action starts, even when news on GitHub start a tick and slots are free. The
[pin after a merge](#pin-after-a-merge) does not stop the planning: the pin runs at once, and
only the restart from the new copy waits until no action runs. A merge is not a global action: it runs in [the merge lane](#the-merge-lane).
The turns of running actions are not marked incomplete by the ticks that run meanwhile; they are
checked once their action ends.

#### The merge lane

In `devloop run` a merge of a ready PR (#264) starts in the first tick that plans it with a free
slot, alongside the actions running on other issues and PRs: it does not wait for a 40-minute
implementation or local test suite to end, and while it is planned or running it holds back no
other action (a review, a readiness check, a fix). Merges have a lane of their own: **one merge
at a time**; a [delegated merge](#delegated-merge) (`delegated_merge`, #486) takes the same lane. A merge planned while another one runs waits for a later tick, and still keeps its
PR and its issue: no other action on them starts meanwhile. A merge takes a slot of
`max_parallel_actions` like any action. Of `max_actions_per_tick`, a tick spends a slot only on
the merge that can start: the merges waiting for the lane leave the slots to the other actions.
A merge that finds the limit already spent does not take the lane: it waits for a later tick,
and so do the merges after it, so none of them starts past the limit.

Why a merge may run alongside the other actions:

- the merge reads every merge directive again just before merging, recomputes the policy
  receipt on the current base, and GitHub merges into the current `main`
  (`--match-head-commit`);
- the actions on other numbers work in worktrees of their own and push only to their own
  branches; a `main` that moves under them is the case of any merge made by a person, and the
  existing mechanisms already handle it: the revocation when the base touches the PR's files
  ([When `main` advances](reference.md#when-main-advances-during-a-round)), the sync of a PR that conflicts
  with `main`, and the fix on an aligned base. No action depends on `main` in a way they do not
  cover, so no action is exclusive with a merge;
- actions on the same PR or on its issue never run with its merge, as for any action.

The [pin after a merge](#pin-after-a-merge) is not a global action either: the next tick pins
at once, while the other actions run, and keeps planning with the code it loaded, except new
implementations; the restart from the new copy waits for the first tick at which nothing runs,
or for `[pin] restart_max_wait_minutes`, after which no new action starts until the restart. A merge
that requests the pin while a tick reads GitHub does not stop that tick's actions: the next
tick pins. In
`devloop tick` a merge still runs alone, as a global action
([Parallel actions](reference.md#parallel-actions)).

The dispatcher lock stays one: the loop runs the actions in threads of its own process. A stop
(`SIGTERM`, `SIGINT`, the `STOP` file) starts no other tick and waits for the running actions to
end before the loop exits; so does the failed tick that stops the loop, before the alert
([Continuous loop](#continuous-loop)). Each action logs its
`outcome of <action> #<N>: …` line when it ends. With `max_parallel_actions = 1`, `devloop run`
runs ticks with the barrier, one action at a time, as before.

### Daily summary

`devloop report` (#256) prints the summary of a day, by default today, in UTC, generated from
devloop's own records: the ledger and the markers the dispatcher reads, the same as
`devloop status`, never a local log of a session ([records](protocol.md#daily-summary)).
`--day YYYY-MM-DD` chooses another day. It reads GitHub with the identity of the ticks' reads,
and writes nothing without `--publish`. The summary lists:

- the actions, one per line: each agent turn (implement, fix, sync, review, readiness check) with
  its UTC time, the issue or PR (`#N`, a link once published), kind, agent, outcome and duration,
  marked `(fallback)` when it ran with a fallback model, and each sync without an agent;
- the merges: devloop's with their mode and level, a person's as such;
- the escalations, with their class and whether they went to the operator or the supervisor;
- the CI blocks (a failed CI round) and the re-runs of failed jobs;
- the grants of the supervisor (#466, ADR 0005, section 3.1, rule 4), so that the coordinator
  verifies them without reading each thread: every `grant-round`, `grant-budget` and `capacity`
  the dispatcher answered that day, granted or refused, from its `supervision` records, with the
  UTC time, the issue or PR, the first line of the reason, the outcome and the caps left: the
  rounds still grantable to the PR, the story's one budget extension (booked until the turn
  starts) or the capacity limit in force with its expiry, and the grants of the day still
  available across every delegation;
- the delegated merges (#486, [Delegated merge](#delegated-merge)), in a table of their own:
  every `/devloop merge` the dispatcher answered that day, merged or refused, with its author and
  the first line of its reason (which starts with the sha) and, once merged, how many of the
  day's cap it took; and every `/devloop merge-budget`, with its author, its reason and, once
  granted, the delegated merges it adds and the cap of its day;
- the reserved merges of the coordinator (#529,
  [The reserved merge of the coordinator](#the-reserved-merge-of-the-coordinator)), in a table
  of their own with the same columns: every `/devloop merge` of the coordinator's identity the
  dispatcher answered that day, merged or refused, and, once merged, how many of the
  coordinator's daily cap it took;
- the substitute votes of the coordinator in a conclave (#605, [Conclave on a
  block](#conclave-on-a-block)), under "Voti sostitutivi del coordinatore nel conclave": from
  the dispatcher's `conclave` records, each vote recorded that day, with its UTC time, the issue
  or PR, the member it replaced, the coordinator's identity, its agent and model, and the vote,
  so that the operator sees when the diversity of the voters was reduced;
- the auto-pins that did not move `current` (#453): from the `pin` record of the alert on
  `[run].alert_issue`, one per commit, the UTC time, the commit of `origin/main` the running copy
  stays behind and the reason (the step of the smoke test, or the first line of the failed check;
  see [Pin after a merge](#pin-after-a-merge));
- the totals: PRs opened and merged that day, review rounds opened that day (each round once,
  on the day of its first review comment or of its [round register](protocol.md#round-register),
  counted once a review turn of the round succeeded or its CI was recorded: the native reviews
  of the agents with an app are not among the comments the summary reads), turns run with a
  fallback model, and the permissions headless agy denied, per model (#479): each turn stopped
  on a denied permission, whether agy reported the command or not, counted under the model it
  ran with, as in ``Permessi negati per modello: 3 (`<model>`: 2, …)``. Those turns are also
  among the actions, as failures.

A turn belongs to the day it started, with its whole duration; a merge of devloop to the day
GitHub reports for it, since its record goes in the comment of the promotion (#338); any other
record to the day it was written. A PR counts only with the marker of the implementer that
opened it.

**Publication.** With `[report] issue = <number>` in the
[local overlay](#local-config-as-an-overlay), an issue **dedicated** to the summary (not the
supervisor's diary), `--publish` publishes it as the dispatcher. The body of that issue is the
dispatcher's, and devloop rewrites it: on top the summary of today, below the index of the past
days, newest first, each a link to its archive. When the day changes, the next publication
generates the summary of the closed day again from the records and archives it in a comment of
its own, one per day; the days in between with activity are archived too (at most the last 31),
the empty ones are not. An archive already written is edited if the records changed, never
written again: more runs on the same day, or a run repeated after a failure, leave one comment
per day. A day archived while one of its turns was still open (a turn started just before
midnight, say) is generated again at every publication, and its archive edited when the records
change, until none of its turns is open: the outcome and the duration of that turn reach the
archive of the day it started. `--publish --day <past day>` writes or updates only the archive of that day and the
index, and keeps today's summary as it is; a future day is refused. Without `[report] issue`,
`--publish` stops with an error that says so, and nothing is read or written. Any error of a
`devloop report` exits with a non-zero code and its message.

**In `devloop run`.** With `[report] issue`, the [continuous loop](#continuous-loop) publishes
the summary at the end of a tick, at most every `[report] interval` minutes (default 15): the
first time at once, then no sooner than the interval since the last attempt, restarts included
(the time is kept in `state_dir/report-state.json`). A publication that fails is logged and
tried again at the next interval; it does not count as a failed tick. Without `[report] issue`
the loop publishes nothing. A manual `--publish` always publishes, even while the loop runs:
the two never publish at once (`state_dir/report.lock`).

### Stalled agents

An agent can wait without progress until a Bash command's maximum timeout or until
`agent_timeout_seconds`, for example on a command that never ends. While a turn runs, the
dispatcher checks two signals every `agent_stall_seconds / 10` seconds (between 1 and 30):

- the newest session transcript of the turn's worktree, the files `devloop follow` reads, in the
  home of the [agent user](#agent-user) when there is one: a new event counts as activity;
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
| `abort` | Also stop the turn (its whole process group) and escalate it like any agent failure: the issue or PR goes to `agent:needs-operator` with a comment giving the reason |

The CPU signal reads `/proc`, so the check works on Linux only; elsewhere it is off and the tick
log says so once per turn. Antigravity writes no transcript that devloop can read, so for it only
the CPU signal counts: a long silence of the model alone is reported as a stall.

### Run from a pinned installation

devloop works on this repository, including on its own code, so it runs from a pinned,
read-only copy of one commit of `origin`, never from the checkout it is changing (#174). This is
the same rule as the pinned controller this repository uses to develop itself. The configuration, schemas
and prompts come from the pinned copy; GitHub state and worktrees come from the clone.

#### Installation as a dedicated user

The installation in use since 2026-11-16 (#185). To recreate it on another machine, together
with the supervisor's user, its sudoers and the GitHub Apps, or to reuse it for another project,
see the [ops guide](../../ops/README.md) (#402): parametric scripts, the systemd unit, the app
manifests, a host check and the migration steps.

- an unprivileged system user `devloop`, with a clone of its own (`/home/devloop/devloop`);
- state, pinned copies and the local config in `.devloop` of that clone;
- the GitHub App keys in `~/.ghapps` of that user (each key mode `600`), and no `gh` login:
  devloop acts with its apps. A [merge mode](#merge-modes) other than `off` merges with the
  [merger app](reference.md#the-merger-app), whose key is there too; without that app it would
  need a local `gh` login. The [reserved merge of the coordinator](#the-reserved-merge-of-the-coordinator)
  needs the merger app in any mode, and never uses a local `gh` login;
- a second system user, `devloop-agent`, that runs the agent CLIs and the tests: devloop starts
  them as that user through `sudo`, so that they cannot read the keys, `state_dir` nor the local
  overlay ([Agent user](#agent-user), #415);
- the agent CLIs installed in the home of `devloop-agent` and logged in with logins of that user
  ([Agent CLIs](#agent-clis)).

From the beginning to the end, as root or with `sudo`:

1. **User and clone.** `python3`, `git`, `gh` and `openssl` (it signs the apps' tokens) come
   from the system.

   ```bash
   sudo useradd --create-home devloop
   sudo -u devloop -H git clone https://github.com/brushtyler/devloop.git /home/devloop/devloop
   ```

   Until #563 the clone kept the path of before the rename of the repository (ADR 0007), like
   the rest of the host; the host moved to these paths with
   [ops/host/migrate.md](../../ops/host/migrate.md#percorsi-devloop-e-istanza-devloopdevloop-563).

2. **Keys.** The apps are the repository's own set, created once by the operator from the
   manifests of [`ops/github/apps/`](../../ops/github/apps/) and never shared with another
   repository ([GitHub identities](reference.md#github-identities), and
   [The merger app](reference.md#the-merger-app) for the merger). Since ADR 0006, R6 they are
   declared in `[apps]` of the local overlay (step 4), not in `devloop/config.toml`: ssdlc's are
   in [`ops/host/ssdlc/overlay.toml`](../../ops/host/ssdlc/overlay.toml). Each private key goes to
   `~/.ghapps` of the `devloop` user, under the name GitHub gives it,
   `<slug>.<date>.private-key.pem`, mode `600`; a configured app whose key is missing stops
   devloop at startup.

   ```bash
   sudo -u devloop -H install -d -m 700 /home/devloop/.ghapps
   sudo install -o devloop -g devloop -m 600 <slug>.<date>.private-key.pem /home/devloop/.ghapps/
   ```

3. **Agent user and CLIs.** Prepare `devloop-agent` with `ops/host/setup-agent-user.sh`
   ([Agent user](#agent-user)), then, after `setup-agent-user.sh --unseal`, install Claude Code,
   codex and agy as that user and log in to each one with its logins, as described in
   [Agent CLIs](#agent-clis), and run `setup-agent-user.sh` again without `--unseal`.

4. **Overlay.** Write `/home/devloop/devloop/.devloop/config.local.toml`, owned by
   `devloop` ([Local config as an overlay](#local-config-as-an-overlay)). It is required: since
   ADR 0006, R6 it holds the project's `repo`, `trusted_users` and `[apps]`, with its `key_dir`
   (step 2), and for ssdlc the agy commands too, all from
   [`ops/host/ssdlc/overlay.toml`](../../ops/host/ssdlc/overlay.toml). Without the file, or
   without `repo` and `trusted_users` in it, devloop stops at startup. The other keys are the
   operator's overrides, each the operator's choice: the merge mode, `[pin] auto`
   ([Pin after a merge](#pin-after-a-merge)) and `[run] alert_issue`
   ([Continuous loop](#continuous-loop)). On this installation it also sets
   `[agent_user] name = "devloop-agent"` ([Agent user](#agent-user)).

5. **Pin.** The first pin runs the clone's devloop; every later command runs the pinned copy,
   through the [launcher](#launcher). The pin applies the overlay and runs the
   [smoke test](#pin), so a key the copy refuses stops here. Before it, the
   [project file](reference.md#project-file) is in the `state_dir` (copied from
   [`ops/host/ssdlc/project.toml`](../../ops/host/ssdlc/project.toml), with the keys of
   [`ops/host/ssdlc/overlay.toml`](../../ops/host/ssdlc/overlay.toml) in the overlay, ADR 0006,
   R3): the clone's devloop gets it and the overlay before the command, as the launcher would.

   ```bash
   sudo -u devloop -H sh -c 'cd ~/devloop && python3 -m devloop \
       --project-config .devloop/project.toml \
       --local-config .devloop/config.local.toml pin origin/main'
   ```

6. **Checks.** From the pinned copy, as the `devloop` user: every app, its installation and its
   permissions, then the state and the plan, without changing anything.

   ```bash
   sudo -u devloop -H python3 /home/devloop/devloop/scripts/devloop-pinned apps
   sudo -u devloop -H python3 /home/devloop/devloop/scripts/devloop-pinned status
   sudo -u devloop -H python3 /home/devloop/devloop/scripts/devloop-pinned tick --dry-run
   ```

7. **Start.** `devloop run` in the background, with an unbuffered log
   ([Starting and stopping the loop](#starting-and-stopping-the-loop)), or as a systemd unit
   ([Continuous loop](#continuous-loop)).

8. **Rollback.** A pin that turns out wrong goes back to an earlier copy
   ([Rollback](#rollback)); an update of an agent CLI goes back to the earlier version the same
   way it was installed ([Agent CLIs](#agent-clis)).

Turns must not depend on an update of an agent CLI in progress: a CLI that replaces its own
binary leaves, for a few seconds, nothing to start (#218; the start is retried once, see
[Failed agent turns](protocol.md#failed-agent-turns)). For the CLIs devloop uses, both:

- disable their automatic updates: for Claude Code, `DISABLE_AUTOUPDATER=1` in the launcher's
  environment. The [launcher](#launcher) exports it unless the environment already sets it, and
  the agent user's `~/.claude/settings.json` sets it too;
- and install them in the home of the agent user, Claude Code included, so that the updates
  of the operator's sessions do not touch them. They change only when the operator updates them
  by hand, with the loop stopped.

#### Agent user

The agent CLIs (claude, codex, agy) and the tests the dispatcher runs (`Workspace.run_tests`)
run as a system user of their own, `devloop-agent` on this installation (#415, decision of the
operator): a user that cannot read the private keys of the apps (`key_dir`), `state_dir`
(journal, logs, registers, pinned copies) nor the local overlay. Without it they would run as
`devloop`, and an agent could read the key of the merger or of the dispatcher with its own file
tools. `[agent_user] name` in the local overlay turns it on; empty, the default, they run as the
dispatcher's user, as before, and devloop says so at each start in the log and once on
`[run].alert_issue`, without stopping ([reference](reference.md#agent-user)). The activation on
a host already in service is in
[ops/host/migrate.md](../../ops/host/migrate.md#attivazione-dellutente-degli-agent-415).

- devloop starts each CLI, the test command and its own startup probe with
  `sudo -n -E -u devloop-agent`. The sudoers rule (`ops/host/sudoers-agent`) allows the `devloop`
  user only the three CLIs in `/home/devloop-agent/.local/bin`, `/usr/bin/bash` (the test command)
  and `/usr/bin/kill`, with the environment devloop builds (`SETENV`) and without a terminal
  (`!use_pty`), so that the command stays in the process group devloop started.
- A timeout or an `abort` of the [stall check](#stalled-agents) kills that process group as
  `devloop-agent`: the `devloop` user cannot signal another user's processes.
- `devloop-agent` writes only inside the worktrees (`state_dir/worktrees/<name>`) and in the
  directories devloop creates for each turn and test run in a shared directory without secrets
  (`state_dir/agent-shared`: the empty `gh` config, the schema and the final message of codex
  for each turn, the temporary directories of the tests), through default ACLs. It enters but
  cannot write `worktrees`, `agent-shared` and the directories in it that hold what devloop
  creates, so that it cannot replace them with a link; devloop also opens those paths without
  following links, and reads the final message of codex only from a regular file. Every other
  entry of `state_dir` has an ACL entry that gives `devloop-agent` no access, and `state_dir`
  itself only lets it through to those two.
- `devloop-agent` cannot write the clone's `.git`. devloop's own git commands in a worktree take
  the repository from `.git`, not from the worktree's `.git` file, and run no hook: an agent
  cannot make devloop run a hook or a configuration of its own.
- `devloop` reads the session transcripts in the home of `devloop-agent` (for the stall check and
  `devloop follow`) through read ACLs on `~/.claude/projects` and `~/.codex/sessions`.
- At the start of `tick` and `run` devloop checks, as `devloop-agent`, that it can neither read
  nor enter `key_dir` (or `$DEVLOOP_APP_KEY_DIR`) and the entries of `state_dir` other than the
  two above, that it cannot read the keys, `state_dir` and the overlay, that it can write
  nothing in `.git` nor the directories that hold the worktrees and its turns, that it can
  write the directories devloop makes for its turns, and that git, run as `devloop-agent` with
  the environment of the turns, works in a worktree of `devloop` (#500: each turn and test run
  trusts its own worktree with `safe.directory`, never `*`; devloop makes that worktree with
  its own git in a directory `devloop-agent` cannot reach, and opens it read-only afterwards,
  so no config of `devloop-agent` runs as `devloop`); otherwise it refuses to start and
  says what is wrong. `[agent_user] check = false` skips the check, in development only.
- Before it, also with `check = false`, devloop checks that the environment it builds reaches
  `devloop-agent` through `sudo -E` (#496), and otherwise refuses to start and says why.
  `sudo` must be sudo 1.9: sudo-rs, the default of Ubuntu, ignores `-E` also with `SETENV`, so
  the agents would run without the isolated environment. The fix, as root:
  `update-alternatives --set sudo /usr/bin/sudo.ws`; `check-host.sh` reports sudo-rs as an
  error.
- One user runs implementers and reviewers, so a turn must not change what later turns use
  (#438, decision of the operator): the instructions, the configuration and the binaries of the
  CLIs are root's, and `devloop-agent` writes only its credentials, sessions, caches and logs.
  Its home is root's and sticky (`1770`, its group): it creates the state and cache entries of
  the CLIs there (such as `~/.claude.json`, which stays its own) but cannot rename nor remove
  root's. `~/.claude`, `~/.codex`, `~/.gemini`, `~/.gemini/config` and
  `~/.gemini/antigravity-cli` are the same
  (#517, decision of the operator): root's, of its group, `1770`, so the CLIs save their logins
  with a temporary file and a `rename` (`.credentials.json` of claude, `auth.json` of codex with
  its `auth.lock`), create their locks, caches and temporary files, but `devloop-agent` neither
  renames nor removes what is root's there, nor the directories themselves. `~/.local` stays
  root's, `755`. What carries instructions, configuration, plugins or binaries is root's, of the
  group of `devloop-agent`, which reads it but cannot write it: `CLI_SEALED` in
  `devloop/agentuser.py` (`settings.json`, `CLAUDE.md`, `rules`, `agents`, `commands`, `skills`,
  `hooks`, `output-styles` and `plugins` of `~/.claude`; `config.toml`, `AGENTS.md`,
  `AGENTS.override.md`, `prompts`, `rules`, `skills`, `packages`, `plugins` and `memories` of
  `~/.codex`; `~/.gemini/settings.json`, `~/.gemini/GEMINI.md`; `config.json`, which enables
  the plugins of agy, `mcp_config.json`, which lists the MCP servers it loads, and `projects` of
  `~/.gemini/config`, where only `.migrated` stays `devloop-agent`'s;
  `~/.gemini/antigravity-cli/settings.json`; all of `~/.local`). Each one that does not exist is
  a placeholder of root, made by the setup script (an empty file when the name has a suffix,
  `{}` for a JSON file, an empty directory otherwise), so `devloop-agent` cannot create it. The
  startup check above also refuses to start when, as `devloop-agent`, it can write, own (an
  owner changes the mode), create, rename or remove any of them, or a CLI of
  `~/.local/bin`, the file it leads to and that file's directory; in a directory above them that
  it can write, only the sticky bit protects an entry, and only when neither the entry nor the
  directory is its own. A link along these paths, or inside them, is followed one step at a
  time, and what it leads to must be protected the same way; a link that cannot be followed (a
  loop) is refused. It also refuses a file of them that `devloop-agent` cannot read, or a
  directory it cannot enter, and a directory of the CLIs that is not sticky, or that it cannot
  write. The check runs only the system's tools (`PATH=/usr/bin:/bin`), never those of
  `~/.local/bin`, which comes first in the PATH of `devloop-agent`.
- The directories of the CLIs may hold only the entries of a list in the code, each with its
  type (#517, `CLI_ALLOWED` in `devloop/agentuser.py`, `AGENT_CLI_ALLOWED` in `ops/lib.sh`:
  logins and their temporary files and locks, sessions, caches, logs, databases of the CLIs),
  besides the sealed entries and the directories of the CLIs, which must be root's. At the
  start, in `ops/host/check-host.sh` and before every turn, devloop lists them as
  `devloop-agent`: an unknown entry, one of another type (a link where a file is expected), a
  link allowed for its name that leads elsewhere (#525: only `cli.log` of agy may be a link, to
  `log/<name>` next to it, a regular file and not a link again, as agy makes it at each start;
  `CLI_LINKS` in `devloop/agentuser.py`,
  `AGENT_CLI_LINKS` in `ops/lib.sh`), or a sealed one that `devloop-agent` made, refuses the turn before its CLI starts, with an
  escalation `configuration` to the operator that names the entry (also in a sync, and when the
  chain of models wraps it); a listing that fails refuses it too. `[agent_user] cli_denied` in
  the overlay may take entries away from the list ([reference](reference.md#agent-user)), never
  add one: a new entry goes through a pull request.
- What a CLI may read again as context in a later session (#517): the memories, knowledge and
  summaries of agy (`brain`, `implicit`, `builtin`, `conversation_summaries.db*` and
  `jetbox_summaries_proto.pb*` of `~/.gemini/antigravity-cli`), which no known setting of agy
  turns off, are removed, as `devloop-agent`, before each agy turn (`CLI_CLEARED`). agy starts
  them again empty. Since turns that run together share the home, the turns of agy run one at a
  time, also with [parallel actions](reference.md#parallel-actions): a turn waits until the
  agy turn that runs ends, without a timeout of its own (that turn has one), and a turn
  cancelled while it waits (`parallel_cancel`) never starts. The memories of codex
  (`~/.codex/memories_1.sqlite*`) are off instead: the sealed `~/.codex/config.toml` sets
  `[features] memories = false` in so many words, so that a change of the default of codex does
  not turn them on, and no profile of it may turn them on. The setup script writes the line,
  and the startup check, which reads the file as `devloop-agent`, and `check-host.sh` refuse a
  file without it or with another value. The turns of codex are neither cleared nor serialized:
  they run together, as those of claude and of different CLIs. `memories_1.sqlite*` stays in
  the list, `~/.codex/memories` stays sealed, and the auto memory of Claude Code is off
  (below).
- What a CLI creates on its first use inside the directories of the CLIs stays its own (#494): `CLI_WRITABLE` in `devloop/agentuser.py`,
  `~/.gemini/antigravity-cli/bin`, where agy writes the wrapper of its shell tool. The setup
  script creates it as `devloop-agent`'s, mode `700`, and the startup check refuses to start
  when `devloop-agent` cannot write it. Since a turn could leave there an executable for the
  turns after it, devloop reads it as `devloop-agent` before each agy turn and refuses the turn
  before it starts, with a `tool_failure`, when it holds anything but `agentapi`, or an
  `agentapi` that is not a regular file with the content agy writes (`#!/bin/sh` and
  `exec "<home>/.local/bin/agy" agentapi "$@"`, with or without the final newline:
  `agentuser.agy_wrapper`). An empty or missing `bin` is accepted.
- What the CLIs keep writable must not carry instructions into the next turn either. Every
  claude turn runs with `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, whatever the operator's
  environment says: the auto memory of `~/.claude/projects/<project>/memory`, shared by the
  worktrees of a repository, is neither loaded nor written. The environment alone would not
  bind: the `env` of the settings overrides it, and a turn writes `.claude/settings.json` and
  `.claude/settings.local.json` in its worktree, which the next turns of the pull request read.
  So the same value, with `autoMemoryEnabled: false`, also comes with `--settings`, whose
  settings come before those of the project and the local ones (after the managed ones only,
  which are root's); a `--settings` in the `write_args` or `review_args` of a claude agent, which
  would replace it, stops devloop at startup. It runs with `--strict-mcp-config`:
  the MCP servers of `~/.claude.json`, which `devloop-agent` writes, are ignored, and only those
  of a `--mcp-config` in `write_args` or `review_args` count (`agents.CLAUDE_TURN_ENV`,
  `agents.CLAUDE_TURN_SETTINGS`, `agents.build_command`).

`ops/host/setup-agent-user.sh` creates the user, its directories, the ACLs and the sudoers rule,
and makes the files of the CLIs root's; `ops/host/check-host.sh` verifies them
([ops guide](../../ops/README.md)). Run the setup script again after copying files into
`state_dir`: the ACL entries of the entries that already exist are its own. All agents share
this one user, so their CLI logins are not isolated from each other.

#### Agent CLIs

The agent CLIs devloop runs belong to the agent user (`devloop-agent`, [Agent user](#agent-user)):
installed in its home and logged in with its logins. sudo finds them on the `secure_path` of
`ops/host/sudoers-agent`, with `/home/devloop-agent/.local/bin` first. Without an agent user they
belong to the `devloop` user and devloop finds them on the `PATH` of the process that runs it
(the [unit](#continuous-loop) and the [start command](#starting-and-stopping-the-loop) set
`/home/devloop/.local/bin` first).

- **Claude Code** in `~/.local/bin`, installed with npm under the prefix `~/.local`, with its
  automatic updates off in `~/.claude/settings.json` of the agent user:

  ```bash
  sudo -u devloop-agent -H npm install -g --prefix /home/devloop-agent/.local @anthropic-ai/claude-code
  ```

  ```json
  {
    "autoUpdates": false,
    "env": {
      "DISABLE_AUTOUPDATER": "1"
    }
  }
  ```

- **codex** and **agy**, also in `~/.local/bin` of the agent user.

Installing them, logging in and updating them need the directories of the CLIs open to the agent
user: `sudo bash ops/host/setup-agent-user.sh --unseal`, with the loop stopped, gives them back
to it, and the same script without `--unseal` makes them root's again (#438, [Agent
user](#agent-user)); until then devloop refuses to start. Log in to each CLI once, in an
interactive session of the agent user (`sudo -iu devloop-agent`), and check that
`claude --version`, `codex --version` and `agy --version` answer there.

Each CLI renews and saves its own login in its directory, sticky since #517: the long-lived
token of claude of #507 (`[claude_token]`) was removed, and a configuration that still names
that table is refused at startup with what to do
([ops/host/migrate.md](../../ops/host/migrate.md#directory-delle-cli-con-lo-sticky-bit-517)).

**Updating them by hand, with the loop stopped** (decision of the operator, 2026-11-17). No turn
may start while a binary is being replaced, so:

1. stop the loop with the `STOP` file and wait until `devloop run` has exited
   ([Starting and stopping the loop](#starting-and-stopping-the-loop)): the tick in progress
   and its actions end first;
2. open the directories of the CLIs to the agent user with
   `sudo bash ops/host/setup-agent-user.sh --unseal`;
3. update the CLI as the agent user, for Claude Code with the same `npm install` as above
   (`@anthropic-ai/claude-code@<version>` for a given version), codex and agy each with its own
   installer;
4. check the new version with `--version`, as the agent user, and start it once, so that it
   creates the entries it needs;
5. make them root's again with `sudo bash ops/host/setup-agent-user.sh`;
6. remove `STOP` and start `devloop run` again.

To go back, install the earlier version the same way, again with the loop stopped.

#### Starting and stopping the loop

`devloop run` ([Continuous loop](#continuous-loop)) is a background process of the `devloop`
user, started through the [launcher](#launcher) from the clone:

```bash
sudo -u devloop -H sh -c 'cd ~/devloop && PATH=/home/devloop/.local/bin:/usr/local/bin:/usr/bin:/bin PYTHONUNBUFFERED=1 nohup python3 scripts/devloop-pinned run --idle-timeout 900 < /dev/null >> .devloop/run.log 2>&1 &'
sudo -u devloop -H tail -f /home/devloop/devloop/.devloop/run.log   # follow the log
```

- **`PATH`.** The command sets the same `PATH` as the [unit](#continuous-loop), with
  `/home/devloop/.local/bin` first. `sh -c` is not a login shell and does not read the profile
  of the `devloop` user: without it the loop would keep the `PATH` of `sudo`, which may leave
  out `~/.local/bin`, and every agent turn would fail to start, even though `--version` answers
  in an interactive session. The launcher passes this `PATH` to the agents.
- **Unbuffered log.** `PYTHONUNBUFFERED=1` makes every line reach the log as it is written, so
  `tail -f` shows the tick in progress and a crash leaves its last lines. The launcher passes its
  environment to the pinned copy it starts; a `-u` given to the launcher would not reach it.
- **Stop.** Create the `STOP` file. The loop logs `stop requested (…): no further tick`, ends
  the tick in progress and waits for its running actions, then exits with code 0; while `STOP`
  exists, a new `devloop run` exits at once, without a tick. The process has exited when
  `pgrep` no longer finds it. Remove `STOP` to start again.

  ```bash
  sudo -u devloop -H touch /home/devloop/devloop/.devloop/STOP   # stop after the tick
  pgrep -u devloop -af 'm devloop'                                    # nothing: it has exited
  sudo -u devloop -H rm /home/devloop/devloop/.devloop/STOP      # then start it again
  ```

- **`[pin] auto`.** With `auto = true` in the overlay, a merge, by devloop or by the operator, that touches
  `devloop/`, `core/` or a file of `sensitive_path_lists` pins `origin/main`, and the loop restarts by
  itself from the new copy, in the same process and log ([Pin after a merge](#pin-after-a-merge)). Otherwise, after a pin by hand, stop the loop and
  start it again: a running loop keeps the code it started with.
- **`[run] alert_issue`.** Three failed ticks in a row stop the loop with exit code 4. With
  `alert_issue` in the overlay the dispatcher also comments on that issue; without it the alert
  is only a line in the log. The background process does not start again by itself: remove the
  cause, then start it again.
- **`[report] issue`.** With it in the overlay the loop keeps the
  [daily summary](#daily-summary) on that issue, at most every `[report] interval` minutes;
  without it the loop publishes no summary.
- **Limits.** A new `max_open_prs`, `max_parked_prs`, `max_review_rounds` or `[delegations]` in
  the overlay takes effect at the next tick, without a stop; any other key needs a restart
  ([Changes while the loop runs](#changes-while-the-loop-runs)).

A systemd unit does the same with restarts on a crash: see [Continuous loop](#continuous-loop).

#### Pin

`python3 -m devloop --project-config <state_dir>/project.toml --local-config
<state_dir>/config.local.toml pin <ref>`, from the main checkout (or `scripts/devloop-pinned pin
<ref>`, which passes both itself), takes the commit from the [installation's source](#installation-source), which is this clone
unless `source` names another:

1. updates the branches and tags of `origin`, dropping those deleted on `origin` (the tags of
   `origin` go to `refs/devloop/origin-tags/`, apart from the local tags), and resolves `<ref>`:
   a name without prefix is a branch of `origin` (`main` is `origin/main`, never a local
   `main`), otherwise a tag of `origin`, and the commit must be reachable from a branch or a tag
   of `origin`. A commit only local, or reachable only from a local tag or from a branch deleted
   on `origin`, is refused. The commit of a copy already pinned (also abbreviated) is used
   without a fetch: that is the rollback;
2. reads `devloop/config.toml` of that commit with the [project file](reference.md#project-file)
   of the `state_dir` and the local overlay applied, and takes the files
   of `review_policy.sensitive_path_lists` (ssdlc's come from its project file since ADR 0006,
   R3): each must be a relative path inside the root and a
   regular file of the commit. The documents of the [documentation map](reference.md#documentation-map) are
   not taken: they belong to the repository, not to the installation;
3. extracts `devloop/`, `core/` when the commit has it (devloop imports it for the
   [budget envelope](reference.md#budget-envelope), #365), and those files into
   `.devloop/pinned-<sha12>`, with a `pin.json` naming the commit, and makes the copy
   read-only;
4. checks the copy: in a Python with only the copy on its module path, the package imports, and
   its configuration, with the overlay, loads (a `[docs] map` of the wrong shape fails here) and
   reads `sensitive_path_lists` from the copy;
5. runs the **smoke test** of a new copy: from the copy (only the copy on the module path), in
   the main checkout and with the same overlay, `python3 -m devloop tick --dry-run` (it plans,
   without agents and without writes, not even to the repository) and one round of `python3 -m devloop watch`, on a state
   file of its own, at most 30 seconds. Step 4 has already loaded the configuration with the
   overlay;
6. points `.devloop/current` at the copy, with an atomic rename of a symlink.

A failure at any step leaves `current` as it was, and leaves no new copy behind, with one
exception: a copy that fails the smoke test stays in `.devloop/rejected-<sha12>` (replacing
an earlier one of the same commit), for the analysis, and the pin fails with `smoke test of
<sha12> failed at <step>: <error>`. A rejected copy is never reused: a later pin of the same
commit extracts and tests it again. A step that fails with a transient error (a network error,
a GitHub 5xx, its time limit) runs once more before the copy is refused; any other error (an
import error, an invalid configuration, another GitHub error) refuses it at once. `pin --no-smoke
<ref>` skips the smoke test, for exceptional cases, and the log says `smoke test of <sha12>
skipped (--no-smoke)`. The smoke test needs the GitHub access of the dispatcher; a copy of a
commit from before the smoke test takes the dispatcher lock in `tick --dry-run`, so its smoke
test fails while `run` is active. A copy that already exists for the same commit is reused,
without a smoke test, but checked again against the current overlay: if a file it needs is
missing, the pin fails. Earlier copies stay for the rollback, up to `[gc] keep_pins` (default
5) besides `current`: after a successful pin the older ones that no process uses are removed
([Cleanup of the state directory](#cleanup-of-the-state-directory)). `pin` uses the
overlay given with `--local-config`, or `.devloop/config.local.toml` when it exists, and
the [project file](reference.md#project-file) of the `state_dir` when it exists: the lists it
adds are extracted, and a copy that cannot read it is refused. A
`run` already started keeps the code it started with: restart it after a pin by hand (a pin
after a merge restarts it by itself, see below). `devloop cleanup` removes only the agent
worktrees under `state_dir/worktrees`, never a pinned copy; `devloop gc` removes the pinned
copies beyond `[gc] keep_pins`.

#### Launcher

`scripts/devloop-pinned [--allow-root] <devloop arguments>` runs devloop from the copy that
`.devloop/current` points at:

```bash
python3 scripts/devloop-pinned status
python3 scripts/devloop-pinned run --idle-timeout 900
sudo -u devloop -H python3 /home/devloop/devloop/scripts/devloop-pinned tick --dry-run
```

It runs `python3 -P -m devloop` from the root of its clone, with `PYTHONPATH` on the copy only
and, when `.devloop/config.local.toml` exists, `--local-config` on it; when
`.devloop/project.toml` exists, `--project-config` on it (the
[project file](reference.md#project-file), ADR 0006, R2). It imports nothing
from the clone, and works both run directly by the `devloop` user and through `sudo -u devloop
-H`. Without a pin it stops and says so. It refuses to run as root, unless `--allow-root` is its
first argument. `-P` is required: without it `python3 -m` puts the current directory first on
the module path and imports the checkout's `devloop/` instead of the pinned one. Agents and test
commands do not inherit that `PYTHONPATH`: the tests of a worktree import the worktree's code.
It exports `DISABLE_AUTOUPDATER=1`, which the agents inherit, so that Claude Code does not update
itself while devloop runs it; a value already in the environment (the operator's) is kept.

With `--state-dir DIR` (ADR 0006, R1), an absolute path, the launcher takes the copy, the
overlay and the project file from `DIR/current`, `DIR/config.local.toml` and `DIR/project.toml`
instead of `.devloop` of its clone,
so it can live anywhere, for example installed on the host as
`/usr/local/lib/devloop/devloop-pinned` (see [Installation source](#installation-source)). It then
runs devloop in the current directory, which must be in the checkout of the managed repository
(the `WorkingDirectory` of the service; otherwise it stops and says so), and passes `DIR` to
devloop in `DEVLOOP_STATE_DIR`: devloop stops with `invalid config: state_dir: <path> is not the
directory given to the launcher, DIR` when the `state_dir` of its configuration is another
directory, so that pins and state never end up where the launcher does not look. The agents and
the test commands do not get that variable. Without `--state-dir` nothing changes.

#### Installation source

The **installation's source** is the clone `devloop pin` takes the code from; the **managed
repository** is the one of the worktrees, the issues and the PRs, whose main checkout is the
directory devloop runs in (ADR 0006, R1). For devloop's own repository (the project ssdlc) they
are the same clone, and nothing above changes. For a project that does not contain devloop, the
overlay names a clone of devloop (`brushtyler/devloop`) in
`source` (absolute, with `~`, or relative to the managed checkout; it must be the top of a git
checkout, else the configuration is refused), and usually a `state_dir` of the host:

```toml
source = "/home/devloop/devloop-source"
state_dir = "/var/lib/devloop/widget"
```

- `devloop pin <ref>` resolves and extracts `<ref>` from the source and its `origin`, with the
  same checks as above: a tag of devloop, so that an update is an explicit act. The smoke test runs
  in the managed repository, with the copy and the overlay. The managed repository is not fetched
  by a pin.
- The [pin after a merge](#pin-after-a-merge) is off: a merge in the managed repository never
  touches the installation. With `[pin] auto = true` the log says `auto-pin off: the installation
  comes from <source>, not from the managed repository`; `devloop status` does not compare the
  copy with `origin/<base_branch>`.
- The launcher is installed on the host from the source at the pinned tag, outside the pinned
  copy and the managed repository, and gets the `state_dir` with `--state-dir`; the
  [ops guide](../../ops/README.md) does it with `SOURCE_REPO` and `SOURCE_REF`.
- The first pin runs devloop of the source clone in the managed checkout, with the project file
  (below; since ADR 0006, R3 the installation has no `test_cmd`) and the overlay:

  ```bash
  cd /home/devloop/widget
  PYTHONPATH=/home/devloop/devloop-source python3 -P -m devloop \
      --project-config /var/lib/devloop/widget/project.toml \
      --local-config /var/lib/devloop/widget/config.local.toml pin v1.2.0
  ```

  later pins go through the launcher (`devloop-pinned --state-dir /var/lib/devloop/widget pin
  <tag>`).

The facts of the project (`test_cmd`, `[docs.map]`, …) and the restrictions it adds go in the
[project file](reference.md#project-file) `DIR/project.toml` (ADR 0006, R2), on the host; the
trust keys stay in the overlay. The whole installation of such a project, step by step, is in the
[quick start for a new project](quickstart.md). The prompts and the selection of the tests are still those of
ssdlc.

#### Several projects on one host

One host can run devloop for several projects, each with its own system user (and agent user),
clone, `state_dir`, `key_dir` with the keys of its set of apps, and service (ADR 0006, R8,
decision D6). The [ops guide](../../ops/README.md) does it with one parameters file per project
and `PROJECT`: the template unit `devloop@.service`, an instance `devloop@<project>` per project
with its parameters in a drop-in, a sudoers of the supervisor per unit, a clone and a `key_dir`
of the supervisor per project, and `check-host.sh` run once per project. The scripts refuse a
user that another installation of devloop on the host already has, in any role: only the
supervisor is the same for every project. A value written in `params.env` stands with `PROJECT`
too: the host of this repository became the instance `devloop@devloop` keeping its users, its
`key_dir`s and its supervisor's clone written there (#563,
[ops/host/migrate.md](../../ops/host/migrate.md#percorsi-devloop-e-istanza-devloopdevloop-563)).

**One repository, one host.** A repository is served by exactly one dispatcher, on one host.
The lock of `state_dir` (`state_dir/dispatcher.lock`) excludes only the dispatchers of the same
`state_dir`: two services configured on the same `repo` with two `state_dir`s would both act on
its issues and PRs, with double work and markers in conflict. So the host has a **repository
lock** too (decision of the operator, L1 of ADR 0006, section 3.8):

- with `repo_lock_dir` in the overlay ([reference](reference.md#configuration)), `tick` and
  `run` take, without waiting, `<repo_lock_dir>/<owner>--<name>.lock`, owner and name of `repo`
  in lower case, besides the lock of `state_dir`. While another dispatcher on the same repository
  holds it, whatever its `state_dir` or its user, devloop stops with `another dispatcher is running
  on <repo> (repository lock <path> held)`; dispatchers of different repositories run together;
- the kernel releases it when the process ends; a [restart from a new copy](#pin-after-a-merge)
  passes it on open, like the lock of `state_dir`. With the outbox of the publications, the `gh`
  of each GitHub write holds it too while it runs, as it holds the lock of `state_dir`: a
  dispatcher with another `state_dir` does not take over the repository while a publication of
  one that stopped may still be running. `tick --dry-run`, `status`, `cleanup` and `gc` do not
  take it;
- the directory is the host's, the same for every project: `/run/devloop/repos` with the ops
  scripts, created at every boot by `systemd-tmpfiles`, owned by root, mode `3770`, of a group
  (`devloop-repos`) that holds the devloop users of the host and nobody else. The group gives no
  access to the `state_dir`s; the agent users and the supervisor do not enter it. devloop does
  not create it: a `repo_lock_dir` that does not exist stops `tick` and `run`. It opens a lock
  read-only and never through a symlink. A lock that exists is opened as it is, without
  creating or replacing it and with its owner and mode left alone; only a missing one is
  created, readable by the other devloop users;
- devloop has no container for now: R7 of ADR 0006 is suspended, because codex's sandbox does
  not start in a container without `--privileged` (decision of the operator of 2026-11-21, on
  #426), and every project is installed on the host. If a container comes back, it has to mount
  that directory of the host, so that the lock spans the containers and the services of the host;
- it holds **on one host only**: the same repository from two hosts is not excluded. Serving a
  repository from several hosts would need a lease on the tracker (L2 of the ADR), to be weighed
  with the fencing of step 4 of ADR 0001.

Without `repo_lock_dir` devloop holds only the lock of `state_dir`, as before; `check-host.sh`
reports an overlay without it as an error.

#### Local config as an overlay

`.devloop/config.local.toml` holds the project's identities and trust and **the keys the
operator overrides**, for example:

```toml
repo = "brushtyler/devloop"
repo_aliases = ["brushtyler/ssdlc"]
trusted_users = ["brushtyler"]
preflight = "codex-reviewer"
max_open_prs = 3
max_review_rounds = 4

[merge]
mode = "strict"

[pin]
auto = true

[run]
alert_issue = 123

[apps]
key_dir = "~/.ghapps"

[apps.dispatcher]
app_id = 5747835
slug = "brushtyler-devloop-dispatcher"
```

devloop applies it over `devloop/config.toml` of the pinned copy: a table merges key by key
(`[merge]` above keeps `operator_only_paths` of the copy), any other value, a list too, replaces
the one of the copy. A new pin therefore needs no realignment of the config by hand. A key that
`config.toml` cannot have (`bogus = 1`, `[merge] method = "squash"`) stops devloop, and so does
any value the copy's config would refuse. An overlay cannot remove a key of the copy, only
replace it.

Beside it, `.devloop/project.toml` is the [project file](reference.md#project-file)
(ADR 0006, R2), with the same permissions: the facts of the managed repository (`test_cmd`,
`local_tests`, `[tests]`, `language`, `priority_labels`, `[docs.map]`) and the restrictions it adds
(`protected_paths`, `blocked_labels`, sensitive paths and lists, level-0 checks,
`operator_only_paths`). The order is installation < project file < overlay, but a restriction
of the project file only adds to those of the installation and the overlay. Any other key in it
stops devloop, naming the key; the trust keys stay in the overlay, and its `test_cmd` never
enters the default commands of agy: the overlay alone admits it. It must be a regular file of
its own: a symbolic link, or a file with other hard links, stops devloop. Its hash is part of
the policy version, and each start and reload records the configuration in the journal.

Since ADR 0006, R3 the installation's `config.toml` holds none of these keys: ssdlc's facts and
restrictions are in its project file, whose example is
[`ops/host/ssdlc/project.toml`](../../ops/host/ssdlc/project.toml), and the agy commands derived
from its tests in its overlay ([`ops/host/ssdlc/overlay.toml`](../../ops/host/ssdlc/overlay.toml)).
Without a project file, or a `test_cmd` in the overlay, devloop stops at startup (`missing
'test_cmd'`), and so does the pin of a copy, whose check loads the configuration. The migration
of a running host, and the order to follow so that the pin after the merge finds the project
file, are in [ops/host/migrate.md](../../ops/host/migrate.md#migrazione-di-r3-su-un-host-attivo).

Since ADR 0006, R6 the installation's `config.toml` has no `repo`, `trusted_users` nor `[apps]`
either: the managed repository, the logins devloop trusts and the project's set of GitHub Apps,
with its `key_dir`, are the operator's, in the overlay of the project's `state_dir`. By default
each repository has its own set of apps, installed on that repository only; whoever installs may
choose instead a set shared between repositories of the same owner served by the same host, with
the limits listed in [Identities of a project](reference.md#identities-of-a-project) (D3 and its
clarification of 2026-11-23, #570). Each project has its own `key_dir` either way, and
devloop's own repository keeps a set of its own; the merger app only where devloop merges (D3,
[GitHub identities](reference.md#github-identities)). ssdlc's are in
[`ops/host/ssdlc/overlay.toml`](../../ops/host/ssdlc/overlay.toml). Without `repo` and
`trusted_users` in the overlay devloop stops at startup, naming the overlay, and so does the pin
of a copy. The overlay of a host gains them before devloop takes a copy of R6
([ops/host/migrate.md](../../ops/host/migrate.md#identità-di-ssdlc-nelloverlay)).

Since #499 the installation's `config.toml` names its agents by role (ADR 0005, P5-3):
`claude-implementer`, which signs with the implementer app, `codex-reviewer`, `agy-reviewer` and,
last in the pool, `claude-reviewer` with Sonnet 5.5, the reserve of agy
([The implementer app and the keys by role](reference.md#the-implementer-app-and-the-keys-by-role),
[Reviewers by provider and the third reviewer](reference.md#reviewers-by-provider-and-the-third-reviewer)).
Its agents declare the apps `implementer`, `claude`, `codex` and `agy`: the overlay of every
project has their `[apps.*]` tables, or devloop stops at startup. An overlay written before,
whose tables or values name the keys `claude`, `codex` or `agy` (`[agents.agy]`, `preflight =
"codex"`, `roles.supervisor = "claude"`), stops devloop at startup with an error that names the
key by role to use and the step of [ops/host/migrate.md](../../ops/host/migrate.md#chiavi-per-ruolo-su-un-host-attivo),
which renames them with the queue empty. `preflight` and `conclave` take a reviewer key, such as
`codex-reviewer`: the same app and model as `codex` before. With
`review_policy.provider_quorum`, which ssdlc's overlay turns on, a new round of a level 2 PR whose
reviewer agy has every model remembered unavailable is reviewed by `codex-reviewer` and
`claude-reviewer`; with agy available the pair stays `codex-reviewer` and `agy-reviewer`, and
`claude-reviewer` never reviews a PR of claude without a reviewer of another provider beside it.

#### Changes while the loop runs

A running `devloop run` reads `devloop/config.toml` of its copy, the overlay and the
[project file](reference.md#project-file) again before
each tick (#278), and loads them at the first tick and then when their content changed (the hash
of each file), so a change made while the loop was starting is not missed. Each load whose
hashes differ from the last recorded ones leaves a `config` event in the
[turn journal](protocol.md#turn-journal), with the trust keys changed (ADR 0006, T1). Only these
keys take effect without a restart:

- `max_open_prs`, `max_parked_prs` and `max_review_rounds`: the new values apply from the next
  tick, with a line `config: <key> <old> -> <new> from this tick` in the log. The actions already
  running go on and are not interrupted: a higher `max_open_prs` lets the next tick start an
  issue alongside them.
- `[delegations]` (#405, #411, #412, #486): a delegation turned off, `rounds`, `budget`,
  `capacity` or `merge`, is revoked from the next tick (`retry_own_prs` has no effect since #527)
  ([Granting a review round](#granting-a-review-round),
  [Extending the budget envelope](#extending-the-budget-envelope),
  [Escalations to the supervisor](#escalations-to-the-supervisor),
  [Temporary capacity limits](#temporary-capacity-limits),
  [Delegated merge](#delegated-merge)). A temporary capacity limit of the
  supervisor is not a change of the configuration: it applies over the configured value of
  `max_open_prs` or `max_parked_prs` until it expires.

**Every other key needs a restart**, including the merge modes (`[merge]`), `[pin]`, the roles,
`trusted_users`, the agents, the identities (`[apps]`), `state_dir`, `max_parallel_actions` and
the other limits. When one of them changes, the log says
`config: <keys> changed: the change applies only after a restart, the running values stay in use`,
and the loop keeps the value it started with until it is
[stopped and started again](#starting-and-stopping-the-loop) (a
[pin after a merge](#pin-after-a-merge) restarts it too). No rule of trust or merge changes while
the loop runs.

A configuration that does not load (a TOML error, an unknown key, a value out of range, an
overlay removed or unreadable) does not stop the loop: the log says
`config: changed but not valid, the previous configuration stays in use: <error>`, the tick goes
on with the previous configuration, and the files are loaded again at their next change. The
overlay must exist when the loop starts: the [launcher](#launcher) passes `--local-config` only
for an overlay it finds, so an overlay created later needs a restart; the same holds for the
project file and `--project-config`. `devloop tick` reads the
configuration at each run, as before.

#### Rollback

Pin the earlier commit again: its copy is reused, checked against the current overlay, and
`current` points back at it, with no fetch needed. The pinned copies are the directories
`.devloop/pinned-<sha12>`: `current` and the `[gc] keep_pins` (default 5) pinned most
recently ([Cleanup of the state directory](#cleanup-of-the-state-directory)). A commit whose
copy was removed is pinned again from `origin`, with a new copy and its smoke test.

```bash
sudo -u devloop -H ls /home/devloop/devloop/.devloop/                  # the pinned-<sha12> copies
sudo -u devloop -H python3 /home/devloop/devloop/scripts/devloop-pinned pin 1a2b3c4d5e6f   # an earlier sha12
```

Any pin, by hand or after a merge, also drops a pin requested by a merge and not yet done, so a
rollback is not undone by the next tick. A running `devloop run` keeps the code it started with:
stop it and start it again ([Starting and stopping the loop](#starting-and-stopping-the-loop))
to run the earlier copy. With `[pin] auto = true` the next [merge that requests a
pin](#pin-after-a-merge) pins `origin/main` again: to stay on the earlier copy, set
`auto = false` in the overlay before starting the loop again.

#### Pin after a merge

With `[pin] auto = true` (default `false`: the operator's decision), after a merge done by
devloop ([merge modes](#merge-modes)) whose diff touches `devloop/`, `core/` (which devloop
imports since #365) or a file of
`sensitive_path_lists` (those of the running configuration), devloop records the request in
`.devloop/pin-pending.json` (only when the [installation's source](#installation-source) is
the managed repository: with `source` set the pin after a merge is off). The next tick, before
planning, runs `pin origin/main` (the
`base_branch` of `origin`). If the pin succeeds, `run` and `tick` start again from the new copy
with the same arguments: configuration, policy and dispatcher load again, and the lock stays
held across the restart, so no other dispatcher can start in between. If the pin fails before it
resolves the commit (the fetch of `origin`, another pin running), the tick
stops with `auto-pin of origin/main failed, the pinned copy is unchanged: <reason>`, `current`
stays as it was, and the request stays: the next tick tries again (`run` counts it as a
[failed tick](#continuous-loop), with its backoff; `tick` exits with it). A copy that fails its
checks once the commit is resolved is not a failed tick (see below). With `auto = false` a merge
requests nothing, and a request left from before is ignored.

A merge devloop did not make (#430), such as a PR of `operator_only_paths` or one the operator
merges by hand, requests the pin too. At each tick, before reading the request (not in
`tick --dry-run`), devloop fetches `origin/main` and compares its head with the head the last
tick saw, recorded in `.devloop/pin-base.json` (the first time, with the commit of
`current`). When it advanced with changes to the same paths, the log says `auto-pin:
origin/main advanced to <sha12> with <N> changed files of the installation (<files>): pin
requested`, and the pin follows with the same rules as above (`auto-pin of origin/main after the
advance of origin/main to <sha12>`). An advance that touches none of them requests nothing; nor
does one whose head `current` already holds, nor one seen while a request is pending (the
request of devloop's merge stays). The head is recorded as seen also when the copy is then
refused by the smoke test, so a refused commit is not tried again until `origin/main` advances.
A fetch that fails is logged (`auto-pin: the advance of origin/main not checked, next tick:
<reason>`) and the tick goes on. With `auto = false` nothing is fetched or recorded.

`devloop status` says when the pinned copy it runs from (the one `current` points at, through the
[launcher](#launcher)) differs from `origin/main`, as last fetched, in those paths: `pinned copy:
pinned-<sha12> (<sha12>) is behind origin/main (<sha12>, as last fetched) in <N> files of the
installation: <files>` (`is not an ancestor of` for a copy of a commit off the branch). It says
nothing when the copy is up to date in those paths, or when devloop runs from a checkout.

In the [continuous mode](#continuous-mode) of `devloop run` (#301) the pin does not wait for
the running actions. The next tick runs `pin origin/main` at once, smoke test included, and
moves `current`; the actions already running, and those the tick and the next ones start, run
with the code the process loaded, as they would without `[pin] auto`. While the restart is
pending no new implementation (`implement`, the long action) starts (#342): a ready issue waits
for the new copy, without taking a slot of `max_open_prs` or `max_actions_per_tick` from the
other actions, while the short actions (review, fix, sync, merge, readiness checks) still
start, so that devloop reaches on its own a moment without long actions and restarts soon. The
log says `restart from pinned-<sha12> deferred`, and each tick while actions run says
`restart to <sha12> pending since <time> (<N> running): no new implement until the restart`;
`devloop status` shows the same line.
The restart from the new copy happens at the first tick at which nothing runs, from the copy
`current` points at then (a pin by hand meanwhile counts; when that is the running copy, no
restart). A further merge that requests a pin while the restart waits pins again, and the
restart goes to the newer copy; the wait keeps counting from the first pin. After
`[pin] restart_max_wait_minutes` (default 30; 0 drains at once) of a pending restart, the last
resort, no new action starts, the log says `… (<N> running): over [pin] restart_max_wait_minutes
(<M>), no action starts until the restart`, and the restart follows the end of the running ones. The fetch of the pin waits for
the fetches of the running actions, and they for it. With one action at a time
(`max_parallel_actions = 1`) and in `devloop tick` nothing runs between ticks, so the pin
restarts at once, as above.

A copy refused by the [smoke test](#pin) is not a failed tick: the request is dropped, `current`
stays on the previous copy, the log says `auto-pin of origin/main refused, the pinned copy is
unchanged and the request is dropped: <reason>`, and the tick plans and runs its actions from
the running copy, so `run` goes on. With `[run].alert_issue` the dispatcher comments on that
issue once per refused commit (its commit and failed step; the error stays in the local log),
recorded in `.devloop/pin-alerted.json`; a comment that cannot be published is only
logged. The next merge that requests a pin, or the next advance of `origin/main` that touches
the installation, tries again, on the new `origin/main`.

The same holds for a copy that fails the checks before the smoke test (#453): a required file
missing, the import, or the configuration with the overlay and the project file (such as
`config: missing 'test_cmd'` when the project file is not on the host yet). The log says
`auto-pin of origin/main failed the checks, the pinned copy is unchanged and the request is
dropped: the copy of <sha12> failed the checks: <reason>`, the tick plans and runs its actions from
the running copy, and `run` counts no failed tick. The alert on `[run].alert_issue`, once per
commit (`pin-alerted.json`), gives the commit and the first line of the reason, with each
absolute path reduced to its last name. The commit is not tried again at each tick: only a new
commit of `origin/main` that touches the installation, or a pin by hand (`devloop pin`), after
fixing the cause, tries again. When `origin/main` advanced while the tick ran, between the check
of its advance and the fetch of the pin, the next tick requests the commit that failed: the
auto-pin finds it in `pin-failed.json` and drops the request without checking it again (the log
says `the auto-pin of <sha12> already failed (<reason>): not tried again …`), with no second alert.

Both alerts, of the smoke test and of the checks, carry a `pin` record: the commit, the branch,
the time and the reason the alert shows. The [daily summary](#daily-summary) lists them under
`Pin automatico non riuscito`, so the summary says on which day the running copy stayed behind
and why. Without `[run].alert_issue` there is no alert, and the summary does not show it.

A failure after `current` moved to the new copy (the removal of the request, the record of the
pin's time, the cleanup of old copies) is only logged: the pin stands and the process starts
again from the new copy. A request that could not be removed makes the next tick pin the same
commit again, reusing its copy.

The last auto-pin refused by the checks or by the smoke test is recorded in
`.devloop/pin-failed.json`, and `devloop status` shows it next to the line of the copy
behind `origin/main`: `auto-pin failed at <time>: origin/main at <sha12> is not pinned, the running
copy stays: <reason>; a new commit of origin/main or a pin by hand (devloop pin) tries again`.
Any successful pin, automatic or by hand, removes it.

#### Step on the host

A PR whose code needs a step on the host before it is pinned (#462), such as a new table of the
project file or of the overlay, declares it: implementer and fixer return `host_step` in their
structured output (a short text with a reference to the document of the step, such as a section
of `ops/host/migrate.md`; empty when there is none), the PR description shows it under
"Passo sull'host", and the reviewer checks it (#473): a declaration is needed when, and only
when, the pinned code cannot run as intended without a file in the `state_dir`, a key or table
of the project file or of the overlay that is required, or a command run as root or on the
service; a change only to the documents of `ops/host/`, or an optional key or table with a
default, needs none. Since #531 the reference names a section of `ops/host/migrate.md` or a
script of `ops/host/`, which the PR adds or updates when none describes the step yet, never free
commands: the reviewer verifies that it exists on the head of the PR and describes the step, and
a needed declaration whose reference does not counts as missing. After the merge the operator
launches the agent of the host (`ops/host/start-role.sh host`,
[runbook](roles/host.md)), which runs only that section or script, as `main` has it. A missing
declaration is a blocking finding, a superfluous one only a non-blocking finding. The latest
declaration holds: the PR's, or that of its latest fix.

devloop merges such a PR as any other: the queue does not stop. With `[pin] auto = true`,
though, the merge does not request the pin. Before merging, devloop records a hold of the
auto-pin in `.devloop/pin-held.json` (a hold that cannot be recorded stops the merge), and
after the merge it comments on `[run].alert_issue` with the step and adds `agent:needs-operator`
to the issue the PR closes; with `[pin] auto = false` only the alert and the label. A merge that
fails may still have happened (a timeout of `gh pr merge` loses GitHub's answer): its hold stays,
marked uncertain, and at the next tick devloop reads the PR. Merged, the hold stays and the alert
and the label follow then (`auto-pin: #<N> was merged despite the error: …`); not merged, the hold
goes (`auto-pin: #<N> was not merged: its hold of the auto-pin goes`). Each hold names its merge
attempt, and the tick settles only the hold of the attempt it read the PR for: when the merge
worker of `run` merges the same PR again meanwhile, the hold of that new attempt stays, even if
it too is uncertain, until a later tick reads the PR for it. The auto-pin reads the
hold again once it has resolved `origin/main`: a merge that the merge worker of `run` makes
while a pin is fetching writes its hold first, so a commit that contains it is never pinned.
While the hold stays, no auto-pin runs, of that commit nor of later ones:
a pending request (of a later merge, or of an advance of `origin/main`) waits, and the log says
`auto-pin of origin/main held: the step on the host of #<N> waits for the operator, who pins by
hand after it (devloop pin)`; `devloop status` shows `auto-pin held: …` and, for each hold, its
PR, its merge commit, whether its alert went out, and its step. The operator does the
step with the loop stopped, then pins by hand (`devloop pin`, also through the
[launcher](#launcher)): that pin releases, under the lock of the pins, the holds whose merge the
pinned commit contains (`auto-pin resumed: the hold after the step on the host of #<N> is
released`) and drops the pending request, and the auto-pin resumes with the next merge. A hold
whose merge the pinned commit does not contain, such as after a rollback to an older commit or
for a merge that landed during the pin, stays (`auto-pin held: the pinned commit does not
contain the merge of #<N>; …`), until a pin by hand of a commit that contains it. Only the merge
commit counts, never the head of the PR, which a commit without the merge may contain: devloop
reads it on GitHub (`mergeCommit`) at each tick for every hold that does not know it yet, alerted
or not, and until then the hold stays after any pin by hand (`auto-pin held: the merge commit of
#<N> is not known yet …`; pin by hand again once `devloop status` shows it). A hold that names no
PR (a `pin-held.json` that cannot be read) goes with any pin by hand.

A merge devloop did not make (#473), such as the operator's merge on GitHub of a PR in
`agent:needs-operator`, holds the auto-pin too. At each tick, after it has looked at
`origin/main` (`observe`), and again once an auto-pin has resolved its commit, devloop reads on
GitHub the PRs merged by each commit of the first-parent line of `origin/main` that neither the
pinned copy nor its last look contains (`.devloop/pin-scanned.json`; with neither, it
starts from the head it sees). A devloop PR, recognised by its branch and its marker, whose
latest declaration (of the marker or of a fix its implementer recorded) is not empty gets a hold
with the merge commit GitHub reports for the PR (for a rebase, its last commit, even when the
scan met the PR on an earlier one), and the alert says `La PR #<N> è stata mergiata fuori da devloop`. When
the PRs of the commit about to be pinned cannot be read, nothing is pinned and the log says
`auto-pin of origin/main waits for the next tick, the pinned copy is unchanged: …`.

Each hold records whether its alert went out (`alerted` in `pin-held.json`, and `commented` for
the comment alone). A comment or a label that failed, or a process stopped between the merge and
the alert, is tried again at each tick until it goes out, without repeating the part that did; a
hold whose PR turns out closed without merge goes. With no `[run].alert_issue` there is nothing to
publish, and the alert counts as gone out after the label.

At the start of `tick` and `run`, and at each reload of `run`, devloop checks the tables of the
project file that change its behaviour (`[prompts]`, `[tests]`): when the running copy reads one
of them and neither the project file nor the overlay sets it, the log says `config: this copy
reads [prompts], [tests] from the project file, and the configuration has none: devloop runs
without them, degraded; add them to the project file`, and the dispatcher comments once on
`[run].alert_issue` for each set of missing tables (recorded in
`.devloop/project-tables-alerted.json`, forgotten once none is missing). The copy still
starts.

devloop may import only the Python standard library, itself and the `core` package, and a test
enforces this (`tests/test_devloop_boundary.py`). [ADR 0001](../adr/0001-prodotto-unico-fasi-su-tracker.md) kept
devloop and the SSDLC engine in one repository and allowed reuse of the engine's code only through
`core`, pure functions that import no engine module ([Architecture](../architecture.md#future-core-package));
the engine left `main` with [ADR 0007](../adr/0007-da-ssdlc-a-devloop.md), and `core` stayed.

## Limits

- **Merge off by default.** A human merges every PR unless the operator chooses another
  [merge mode](#merge-modes), or turns on the coordinator (`[roles.coordinator]`), whose
  [reserved merge](#the-reserved-merge-of-the-coordinator) merges, on its command, also in mode
  `off` and on operator-only paths.
- **Identities need apps.** An agent without a GitHub App publishes through the operator's `gh`
  login, and then cannot submit a native review of a PR opened with that same login.
- **Trust comes from labels and identities.** Only issues whose `agent:ready` label was added by
  a user in `trusted_users` start (or by the supervisor's app, on issues of trusted users and of
  devloop's apps, see [Labels from the supervisor](#labels-from-the-supervisor)), only comments by those users reach the prompts, and markers are
  read only from the identity that may write them (see GitHub identities). Labelling approves the issue's title and body as they are: if someone outside
  `trusted_users` changes them later (or in the same second, since GitHub timestamps cannot
  order the two), implementation, review and fixes stop at
  `agent:needs-operator` until a trusted user adds `agent:ready` again. Issue text is still model
  input: do not label untrusted content.
- **Failures escalate immediately.** A timeout or a transient error of an agent, `gh` or `git`
  goes to `agent:needs-operator` rather than being retried. The only exception is an unavailable
  model of an agent with [fallback models](reference.md#fallback-models), which runs the turn again on the
  next one. The uncommitted work of an implementation or a fix that timed out is saved, never
  committed, and given back to the retry of the same step
  ([Failed agent turns](protocol.md#failed-agent-turns)).
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
