# devloop: reference

Configuration, review levels and policy, GitHub identities and the agents of devloop. The
operator's [guide](guide.md) describes the loop and the commands, the [protocol](protocol.md)
markers and records; the [index](../devloop.md) lists the three pages.

## Configuration

[`devloop/config.toml`](../../devloop/config.toml) sets the repository, the test command, limits,
the `implementer` and the `reviewers` pool (none of them the implementer, none listed twice;
`reviewer = "<agent>"`, the form before #111, is a pool of one, and setting both is an error)
and, for each agent, the
model, reasoning effort and extra CLI arguments for writing and reviewing turns. Pass another file
with `--config`. `preflight` (absent by default) names the agent of the
[readiness check](guide.md#readiness-check-before-implementation); it must be a configured agent other
than the implementer. Among the limits, `agent_stall_seconds` (an integer of at least 1, default 900)
and `agent_stall_action` (`"warn"`, the default, or `"abort"`) control the
[stall check](guide.md#stalled-agents); other values stop devloop at startup. `max_parallel_actions`
(default 1) sets how many actions of a tick run at once ([Parallel actions](#parallel-actions)).
`max_open_prs` (default 2) and `max_parked_prs` (default 3), integers of at least 1, cap the
open PRs in progress and those waiting for a person ([Open PR limits](guide.md#open-pr-limits)).
`ci_rerun_unrelated` (a boolean, default `true`) re-runs once a CI failure in test modules not
pertinent to the PR ([CI failures not pertinent to the PR](#ci-failures-not-pertinent-to-the-pr));
another value stops devloop at startup.
`[merge]` (`mode`, `levels`, `operator_only_paths`) and `roles.supervisor` set the
[merge modes](guide.md#merge-modes); an unknown mode, level or key stops devloop at startup.
`[pin]` (`auto`, default `false`) sets the [pin after a merge](guide.md#pin-after-a-merge); a value that is
not a boolean, or an unknown key, stops devloop at startup. `[run]` (`alert_issue`, unset by
default) names the issue of the alert of the [continuous loop](guide.md#continuous-loop); a value that
is not an issue number (an integer of at least 1), or an unknown key, stops devloop at startup.
`[docs] map` sets the
[documentation map](#documentation-map). Operator overrides go into the
[local overlay](guide.md#local-config-as-an-overlay), not into this file.
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
[`devloop/schemas/`](../../devloop/schemas/). Implementers edit only their worktree; the
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
  planned after it starts. This is the rule of `devloop tick`; in `devloop run` a merge is not
  global, see [The merge lane](guide.md#the-merge-lane). `max_actions_per_tick` stays the limit on the total, and every agent
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
- for every other changed `<module>.py` (or `<package>/<module>.py`), the modules
  `test_<module>*.py`; for `ssdlc_<name>.py` also `test_<name>*.py` (`ssdlc_impact.py` selects
  `test_ssdlc_impact*.py` and `test_impact*.py`);
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
"devloop/" = ["docs/devloop/guide.md"]
"orchestrator.py" = ["README.md", "docs/architecture.md"]
"schemas/" = ["docs/configuration.md"]
```

A key is a directory prefix ending in `/` (it covers every file under it) or a file, matched
exactly; both are relative to the root. A value is a non-empty list of documents. devloop stops at
startup on an absolute path, an empty key, list or document, a `.`, `..` or empty segment, a
backslash or a pattern character (`*`, `?`, `[`, `]`: `ssdlc_feedback*.py` is written as one key
per file), and a directory given as a document (a value ending in `/`). Loading the configuration
checks its shape only, not whether the documents exist (#210): they belong to the repository
devloop works on, not to the installation, and a [pin](guide.md#pin) does not extract them into the copy.
The [local overlay](guide.md#local-config-as-an-overlay) may add or replace keys.

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
  its own single retry. The [readiness check](guide.md#readiness-check-before-implementation), which
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
pinned checkout, see [Run from a pinned installation](guide.md#run-from-a-pinned-installation)), never
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
  [Findings](protocol.md#findings-and-who-closes-them)). The dispatcher checks every finding of the round
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
synced first, as usual, and a new round follows. That round does not count when it reviews the
same head again (#252), or a head only a clean sync moved (#235); otherwise it counts (see
[Resync after approval](protocol.md#resync-after-approval)).

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
  path, so `docs/` covers none of the Markdown files under it. A symlink never counts. These are
  excluded even when listed: `AGENTS.md` and `CLAUDE.md` at any depth and in any case, `devloop/prompts/`, `standards/`, `.github/`, `schemas/` (with the replay gate
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
The merge stays the operator's, unless a [merge mode](guide.md#merge-modes) other than `"off"` applies to
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

## GitHub identities

Each agent and the dispatcher can act on GitHub with its own GitHub App (#65), configured under
`[apps]` in `config.toml`:

| Actor | Does |
|---|---|
| dispatcher | every read, labels, escalations, CI rounds, re-runs of failed CI jobs not pertinent to the PR (needs `actions: write`, see [CI failures not pertinent to the PR](#ci-failures-not-pertinent-to-the-pr)), finding closures, readiness |
| implementer | commits (author and committer are its bot), push, opens the PR, fix and blocked comments |
| reviewer | the review comment and a native GitHub review (`APPROVE`, `REQUEST_CHANGES` or `COMMENT`) on the reviewed commit |
| merger | only the merges of the `strict` and `auto` [merge modes](guide.md#merge-modes) (see [The merger app](#the-merger-app)) |

Apps are per agent, not per role: GitHub keeps one review state per identity, and it already
refuses an approval from the author of a PR. Least privilege per role comes from the tokens:
devloop mints an installation token for each action, limited to this repository and to the
permissions that action needs. A reviewer's token can read contents and write pull requests, but
cannot push; labels are always set by the dispatcher.

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

The `strict` and `auto` [merge modes](guide.md#merge-modes) merge with an app of their own, `merger`
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
