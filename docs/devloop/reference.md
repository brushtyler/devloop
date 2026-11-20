# devloop: reference

Configuration, review levels and policy, GitHub identities and the agents of devloop. The
operator's [guide](guide.md) describes the loop and the commands, the [protocol](protocol.md)
markers and records; the [index](../devloop.md) lists the three pages.

## Configuration

[`devloop/config.toml`](../../devloop/config.toml) sets the repository, the test command, limits,
the `implementer` and the `reviewers` pool (none of them a key that implements, none listed twice;
`reviewer = "<agent>"`, the form before #111, is a pool of one, and setting both is an error),
the implementers of the area labels (`[roles.implementers]`, #407)
and, for each agent, the app it signs with (`app`,
[the keys by role](#the-implementer-app-and-the-keys-by-role)), the
model, reasoning effort and extra CLI arguments for writing and reviewing turns. Pass another file
with `--config`. `preflight` (absent by default) names the agent of the
[readiness check](guide.md#readiness-check-before-implementation); it must be a configured agent other
than every key that implements, and the implementer chosen for an issue must not be the same agent
(same `kind` or `model`), or the issue goes to the operator (#407). `conclave` (absent by default) lists the three agents of the
[conclave on a block](guide.md#conclave-on-a-block) (#141): exactly three configured agents,
different from each other and with different `(kind, model, effort)`; anything else stops devloop
at startup. Their fallback models are checked when each call runs: one whose tuple coincides with
another member's is not used. The prompts of `implement`, `fix` and `review`
(`devloop/prompts/`) end with `$conclave`: the verified resolution of the latest `UNANIMOUS`
decision on the issue or its PR, or nothing ([After a unanimous
decision](guide.md#after-a-unanimous-decision)). Among the limits, `agent_stall_seconds` (an integer of at least 1, default 900)
and `agent_stall_action` (`"warn"`, the default, or `"abort"`) control the
[stall check](guide.md#stalled-agents); other values stop devloop at startup. `max_parallel_actions`
(default 1) sets how many actions of a tick run at once ([Parallel actions](#parallel-actions)).
`max_open_prs` (default 2) and `max_parked_prs` (default 3), integers of at least 1, cap the
open PRs in progress and those waiting for a person ([Open PR limits](guide.md#open-pr-limits)).
`ci_rerun_unrelated` (a boolean, default `true`) re-runs once a CI failure in test modules not
pertinent to the PR ([CI failures not pertinent to the PR](#ci-failures-not-pertinent-to-the-pr));
another value stops devloop at startup. It needs the selection and the format of the CI logs of
the project's [`[tests]`](#local-tests): without either it is off, whatever its value. `block_uncertain_turns` (a boolean, default `false`)
turns on the block on uncertain turns of the [turn journal](protocol.md#turn-journal) (#362): a
turn left without outcome refuses every new turn of its work, with an escalation, until a trusted
user reconciles it with `/devloop reconcile`. Off, the journal records the turns all the same;
another value stops devloop at startup. `story_identity` (a boolean, default `false`) turns on the
[story identity](protocol.md#story-identity) (#363): at the takeover of an issue the journal
records its story and the `request` record carries the dispatcher's `story` marker. Off, devloop
records no story; another value stops devloop at startup. `attempt_identity` (a boolean, default
`false`) turns on the [attempt identity](protocol.md#attempt-identity) (#384): one active attempt
per phase, in the journal, in the PR marker and in the turn records, with one branch per attempt
(`<branch_prefix><issue>` for the first, `<branch_prefix><issue>-attempt-<k>` after). It needs
`story_identity = true`; without it, or with another value, devloop stops at startup. Off,
devloop records no attempt. `publication_outbox` (a boolean, default
`false`) turns on the [publication outbox](protocol.md#publication-outbox) (#385): every comment,
comment edit, native review and pull request of the dispatcher's and the agents' clients is saved
in the turn journal before it is sent, an uncertain outcome is reconciled on the exact comment of
the same author before any new POST and sent again only on an absence that is conclusive, and
each tick resumes what a restart left half published. Off, devloop publishes as
before (#397): no `gh` gets the dispatcher lock, and a review or pull request whose outcome is
unknown is a failed write; another value stops devloop at startup. It applies at the next start.
`journal_max_mib` (an integer of at least 1, default 64) is the
[limit of the turn journal](protocol.md#turn-journal) (#397): beyond it the journal is not loaded
and no event is written, so no turn starts, and nothing is ever removed from it; from 80% of it
devloop warns the operator, once per threshold on `[run].alert_issue`, or in the log at each
start without it. Another value stops devloop at startup; it applies at the next start.
`[budget]` (`enabled`, default `false`) sets the [budget envelope](#budget-envelope) of each
story (#365); on, it needs `story_identity = true` and every value of the envelope, and a missing
or invalid value, an unknown key or a token cap stops devloop at startup. Today `enabled = true`
stops devloop at startup in any case, until a story at its limit can be extended
([prerequisites of activation](#prerequisites-of-activation), #396).
`[merge]` (`mode`, `levels`, `operator_only_paths`) and `roles.supervisor` set the
[merge modes](guide.md#merge-modes); an unknown mode, level or key stops devloop at startup.
`roles.supervisor` is the key of a configured agent, which signs with that agent's app, or the
table of the supervisor actor (`name`, `kind`, `provider`, `model`), which signs with the role app
`[apps.supervisor]` ([The supervisor app](#the-supervisor-app), #408); a table with another key,
a missing or empty value, a `kind` that is not an agent kind, a `name` other than
`<kind>-supervisor` or equal to a key of `[agents]`, or no `[apps.supervisor]` stops devloop at
startup.
The keys of `[agents]` may name agent and role in full, `<kind>-implementer` or
`<kind>-reviewer`, with the `app` each signs with, and `[roles.implementers]` maps area labels
to implementers ([The implementer app and the keys by role](#the-implementer-app-and-the-keys-by-role),
#407); a key with a role used in another one, a key with a role whose kind is not the agent's,
an app that the rules of that section refuse, or a value of `[roles.implementers]` that is not an
implementer stops devloop at startup.
`[pin]` (`auto`, default `false`; `restart_max_wait_minutes`, default 30) sets the
[pin after a merge](guide.md#pin-after-a-merge), by devloop or by anyone else (#430), and how long, in the continuous mode, the restart
from the new copy may wait for the running actions while new ones, except implementations, still
start; the merge of a PR that declares a [step on the host](guide.md#step-on-the-host) (#462),
by devloop or by anyone else (#473), holds the auto-pin until a pin by hand of a commit that
contains it; an `auto` that is
not a boolean, a `restart_max_wait_minutes` that is not a whole number of at least 0, or an
unknown key, stops devloop at startup. `source` (unset by default, a key of the host's overlay)
names the clone of the [installation's source](guide.md#installation-source) (ADR 0006, R1),
where `devloop pin` takes the code from: absolute, with `~`, or relative to the managed
repository's main checkout. Unset, the source is the managed repository, as for ssdlc. A value
that is not a non-empty string stops devloop at startup, and one that is not the top of a git
checkout stops every command that needs it (the pin and the dispatcher's commands); with a
source other than the managed repository the pin after a merge is off. `state_dir` (default
`.ssdlc/devloop`) is relative to the managed repository's main checkout, or absolute: a directory
of the host. When the [launcher](guide.md#launcher) is given `--state-dir`, the configuration's
`state_dir` must be that directory, or devloop stops. Both apply at the next start.
`repo_lock_dir` (unset by default, a key of the host's overlay) is the host's directory of the
[repository locks](guide.md#several-projects-on-one-host) (ADR 0006, R8), common to every devloop
user of the host: `tick` and `run` hold `<repo_lock_dir>/<owner>--<name>.lock` of `repo` (owner
and name in lower case) besides `state_dir/dispatcher.lock`, and stop when another dispatcher
holds it, whatever its `state_dir`. A value that is not an absolute path (or one starting with
`~`), or a `repo` that is not `owner/name` with it, stops devloop at startup; a directory that
does not exist stops `tick` and `run`, and devloop does not create it. Unset, only the lock of
`state_dir`, as before. It applies at the next start. `[run]` (`alert_issue`, unset by
default) names the issue of the alert of the [continuous loop](guide.md#continuous-loop); a value that
is not an issue number (an integer of at least 1), or an unknown key, stops devloop at startup.
`[report]` (`issue`, unset by default; `interval`, default 15) sets the
[daily summary](guide.md#daily-summary): the dedicated issue it is published on and the minutes
between two automatic publications of `devloop run`; an `issue` that is not an issue number, an
`interval` that is not a whole number of at least 1, or an unknown key, stops devloop at startup.
`[gc]` sets the [cleanup of the state directory](guide.md#cleanup-of-the-state-directory)
(#380): `pins` (default `true`) with `keep_pins` (default 5, the pinned copies kept besides
`current`), `worktrees` (default `true`), `logs` (default `false`) with `log_days` (default 14),
and `interval_minutes` (default 60, the minimum time between two cleanups of the ticks; 0: at
every tick with nothing running); a `pins`, `worktrees` or `logs` that is not a boolean, a
`keep_pins` or `interval_minutes` that is not a whole number of at least 0, a `log_days` that is
not a whole number of at least 1, or an unknown key, stops devloop at startup.
`[agent_user]` sets the [agent user](#agent-user) (#415): `name` (default `""`, off), the system
user the agent CLIs and the test command run as, and `check` (default `true`), the startup check
that it cannot read `key_dir` nor `state_dir`; a `name` that is not a system user name, a `check`
that is not a boolean, or an unknown key, stops devloop at startup, and so does a `name` the
system does not know.
`[docs] map` sets the
[documentation map](#documentation-map). Operator overrides go into the
[local overlay](guide.md#local-config-as-an-overlay), not into this file. A running
`devloop run` takes a new `max_open_prs`, `max_parked_prs`, `max_review_rounds` or
`[delegations]` from the next tick; any other key needs a restart
([Changes while the loop runs](guide.md#changes-while-the-loop-runs)).
`[delegations]` (#405, ADR 0005) sets the delegations of the operator to the supervisor, each
with its revocation key and off by default: `rounds` (true or false, default false) lets the
supervisor [grant a review round](guide.md#granting-a-review-round) to a PR at the round limit;
`max_rounds_per_pr` (default 2) and `max_grants_per_day` (default 3) may only lower the caps of
the code, `MAX_GRANTED_ROUNDS` and `MAX_DAILY_GRANTS` in `devloop/supervision.py`;
`retry_own_prs` (true or false, default false, #406) lets a `tool_failure` on a PR implemented by
the supervisor's agent go to the supervisor, who may retry it
([Escalations to the supervisor](guide.md#escalations-to-the-supervisor)); `budget` (true or
false, default false, #411) lets the supervisor
[extend the budget envelope](guide.md#extending-the-budget-envelope) of a story once, in turns
and time, by `BUDGET_TOLERANCE_PERCENT` (25) of its values, within `max_grants_per_day`;
`capacity` (true or
false, default false, #412) lets the supervisor set a
[temporary capacity limit](guide.md#temporary-capacity-limits) of `max_open_prs` or
`max_parked_prs` with `/devloop capacity` on `[report] issue`, which it requires (off, with
`[report] issue` set, the command there is refused as not applicable and spent);
`capacity_max_open_prs` (default 6), `capacity_max_parked_prs` (default 6) and
`capacity_max_hours` (default 24) may only lower the caps of the code, `MAX_CAPACITY` and
`MAX_CAPACITY_HOURS` in `devloop/supervision.py`. `max_parallel_actions` is not delegated: it
applies only at a restart. A value that is not a whole number from 0 (from 1 for the `capacity_`
keys) to its cap, a `rounds`, `retry_own_prs`, `budget` or `capacity` that is not a boolean,
`capacity` on without `[report] issue`, or an unknown key, stops devloop at startup.
`[review_policy]` sets the
[review level](#review-level-and-policy-receipt): `default_level` (1 or 2, default 1), the level
`label` (default `reviews:2`), `sensitive_paths` and `sensitive_path_lists`; changing any of them,
or the reviewers of the pool (their order, app, kind, model, effort, `review_args` or
`fallback_models`), changes the policy version, so pending approvals need a new review.
`advisory_reviewer` (empty by default: off) names the agent of the
[advisory review](guide.md#advisory-review-for-the-operator) of a PR in
`agent:needs-operator` (#369); it may be the implementer, and an agent that is not configured,
or a value that is not a string, stops devloop at startup. It counts for no quorum, so it is
not part of the policy version: turning it on or off voids no approval. The local overlay
turns it on. The [project file](#project-file) (ADR 0006, R2) sits between the installation's
file and the overlay.

Each PR records its implementer and the first reviewer of the pool when it is opened, so
changing the roles does not affect the level-1 reviewer of open PRs. If the recorded reviewer is
no longer configured, the first of the pool takes over, but never the agent that implemented the
PR: without an independent reviewer, or if the PR's implementer is no longer configured, the PR
goes to `agent:needs-operator`. The level-2 pair always comes from the current pool.

Agents run headless: `claude -p`, `codex exec`, `agy -p`, with the schema in
[`devloop/schemas/`](../../devloop/schemas/). Implementers edit only their worktree; the
dispatcher process does every commit, push and GitHub write, with the identity of the actor
it acts for.

A reviewer does not run in the PR worktree but in a disposable copy of it: a detached worktree
at the same commit, `pr-<n>-review`, which shares the repository's refs (so
`git diff origin/main...HEAD` works) and is removed after the review. The tests the reviewer runs
can therefore write files and temporary directories. After the review the dispatcher checks
that the PR worktree is still at the reviewed commit with no change and no new file, not even
an ignored one; otherwise the verdict is discarded and the PR goes to `agent:needs-operator`.

| Reviewer | Sandbox |
|---|---|
| codex | `workspace-write` sandbox: writes only in the copy and in the temporary directory, no extra `writable_roots`, network disabled |
| claude | no edit tools; Bash limited to `git diff`, `git log`, `git show` and `python3 -m unittest tests.*` (`review_args`) |
| agy | `plan` mode; commands limited to `review_commands` (see [agy permissions](#agy-permissions)) |

Only codex has an OS-level sandbox. claude and agy are limited by their tools and modes, not by
the system: the tests they run can still reach the network and write outside the copy, as any
code running as the same user: the [agent user](#agent-user), when there is one, which cannot
read the keys nor `state_dir`. For them, the copy and the final check catch writes to the
reviewed tree, not access to the rest of the system. With the network disabled, codex cannot run tests
that open TCP sockets, even on loopback.

### Project file

The project file (ADR 0006, R2) holds what belongs to the managed repository: its facts and the
restrictions it adds. It is `<state_dir>/project.toml`, on the host, beside the
[local overlay](guide.md#local-config-as-an-overlay) and with the same permissions (readable only
by the dispatcher's user). The operator writes it; devloop never reads it from the managed
repository, a PR branch, the worktree of a turn or the pinned copy. The
[launcher](guide.md#launcher) passes it with `--project-config` when it exists, and devloop
refuses, at startup, a `--project-config` that is not `<state_dir>/project.toml`, that the
managed repository tracks, or that is a symbolic link or has other hard links; a `state_dir`
reached through a link is refused when the file it leads to is tracked by the git checkout that
holds it (a worktree, another clone). The file is read without following a link, at the start
and at each reload, and each reload checks it again before reading it. Without the file the
configuration is the installation's with the overlay, and the policy version has no hash of a
project file.

**ssdlc's project file** (ADR 0006, R3). The installation's `devloop/config.toml` holds no fact
nor restriction of a project: ssdlc's, as they were in it before R3, are in its project file, of
which [`ops/host/ssdlc/project.toml`](../../ops/host/ssdlc/project.toml) is the example the
operator copies to the host. The code has no default of ssdlc for them either: no
`sensitive_paths` and no `sensitive_path_lists` by default, and `[pin]` watches `devloop/`,
`core/` and the lists of the configuration. The overlay of ssdlc gains `write_commands` and
`review_commands` of agy ([`ops/host/ssdlc/overlay.toml`](../../ops/host/ssdlc/overlay.toml)):
the commands agy had by default from the `test_cmd` of `config.toml`, which the overlay alone
admits now. `repo`, `trusted_users` and `[apps]` stay in `config.toml` until R6, for
`scripts/agent-token.py` and `scripts/supervisor-token.py`, which read only them, with no
project file. Without a project file, or a `test_cmd` in the overlay, devloop stops at startup:
`test_cmd` is required. Since R5 the rules of ssdlc's test selection and the format of its CI
logs are its project file's `[tests]` too ([Local tests](#local-tests)).
`ops/host/ssdlc/check-equivalence.py` compares two installations on a
host (the one of before R3 with its overlay, the one of R3 with the project file and the
overlay; with `--before-project`, an installation of R3 or later with its project file, as for
R5): the values of the configuration key by key, the rendered prompts and the test modules
`local_tests` selects. An installation of before R5 had its selection and its format in the
code: its `tests` are those built-in values, and its own code selects the modules
([ops/host/migrate.md](../../ops/host/migrate.md#test-di-ssdlc-nel-file-di-progetto)). The hash of the project file is left out, since it is in the policy
version by construction: the version is compared without it. It exits with 0 when the two are
equivalent, 1 when they differ, 2 when a side does not load; it writes nothing, not even byte
code, so the operator runs it as root
([ops/host/migrate.md](../../ops/host/migrate.md#migrazione-di-r3-su-un-host-attivo)).

**Precedence:** installation (`devloop/config.toml` of the pinned copy) < project file <
overlay. The schema is closed, and a key outside it stops devloop at startup with a message that
names it (a key of the overlay is told so: `set it in the local overlay`):

| Kind | Keys | How it composes |
|---|---|---|
| Facts | `test_cmd`, `local_tests`, [`[tests]`](#local-tests), `language`, `priority_labels`, `[docs.map]`, `[prompts]` ([data in the prompts](#project-data-in-the-prompts)) | replace the installation's value (`[docs.map]`, `[tests]` and `[prompts]` merge key by key); the overlay replaces them in turn |
| Restrictions | `protected_paths`, `blocked_labels`, `[review_policy] sensitive_paths` and `sensitive_path_lists`, `[review_policy.zero] checks`, `[merge] operator_only_paths` | only add: the entries of the installation and of the overlay (or the code's default) stay, those of the project file that are not there follow. An empty list removes nothing. `[review_policy.zero] checks` only add to the checks of the installation and the overlay: the project file never turns level 0 on, and a check with none below stops devloop |

Every other key, the trust keys (`repo`, `trusted_users`, `[apps]`, `[merge] mode`, roles, review
levels, the level-0 allowlist, the agents and their permissions, `[budget]`, `preflight`,
`conclave`, `[delegations]`), the limits and `state_dir`, is the overlay's alone. The
`test_cmd` and the `[tests]` of the project file are the dispatcher's, never a permission of the
agents: the default commands of agy (`write_commands` and `review_commands`, see
[agy permissions](#agy-permissions)) still derive from the `test_cmd` of the overlay, or of the
installation without one there; the overlay alone admits the project's test command. The lists of
`sensitive_path_lists` the project file adds are read from the pinned copy, as those of the
installation: `devloop pin` extracts them, and checks the copy with the project file of the
`state_dir`. A copy whose code cannot read the project file is refused while there is one.

The SHA-256 of the file is part of the [policy version](#review-level-and-policy-receipt), and
so of every receipt: any change of the file, a comment too, voids the approvals of the open PRs,
as a change of the configuration does. `devloop run` reads it again before each tick, with the
other files ([Changes while the loop runs](guide.md#changes-while-the-loop-runs)): a new key of
it applies at the next start, a file that does not load leaves the previous configuration in
use. A file created while the loop runs is read at the next start.

A copy that reads `[prompts]` or `[tests]` from the project file still starts when neither the
project file nor the overlay sets them, with its prompts or its test selection degraded: at the
start of `tick` and `run`, and at each reload, devloop says so in the log and, once for each set
of missing tables, on `[run].alert_issue` (#462, [Step on the host](guide.md#step-on-the-host)).

**Facts of the repository.** The managed repository holds no file of devloop's configuration.
Its facts are the files the agents already read in their worktree (`AGENTS.md`, the README,
`pyproject.toml`, `package.json`, …), untrusted input: the dispatcher never reads them to decide
or to run anything. What the dispatcher uses (the test command and the selection of the tests,
`[docs.map]`, the restrictions) comes only from the host; a command derived from the repository
runs only inside an agent's turn. The commands the dispatcher runs, such as `test_cmd`, still
run the repository's code in its worktree, as before.

**Trace of the configuration.** At each start of `tick` and `run`, and at each reload of `run`
that finds the files changed, the dispatcher records in its [journal](protocol.md#turn-journal)
a `config` event: the SHA-256 of the overlay and of the project file, that of the effective
configuration, and the trust keys whose value changed since the previous one, with the value
before and after. The trust keys are `repo`, `base_branch`, `state_dir`, `source`, `test_cmd`,
`trusted_users`, `preflight`, `conclave`, `[roles]`, `[agents]`, `[apps]`, `[merge]`,
`[review_policy]`, `max_review_rounds`, `protected_paths`, `blocked_labels`, `[budget]`,
`[delegations]`, `[agent_user]` and `[pin]`. The start records the files the dispatcher was
built from, as they were read, never a second read of them. Its log says the same. A start whose
record cannot be written stops devloop.

### Project data in the prompts

The prompts of `devloop/prompts/` are templates of the installation (ADR 0006, R4). The text that
belongs to one project is data of its [project file](#project-file) (or of the overlay), in the
`[prompts]` table; the rules around it (permissions, verdicts, what an agent does not do, the
decisions reserved to the operator) stay in the templates. Each key is optional:

| Key | What the prompts do with it |
|---|---|
| `instructions` | What the agents read before an implement, preflight, review, advisory review or conclave turn: a list of Markdown fragments, joined as an English list (`` ['`AGENTS.md`', 'the README section "Developing SSDLC"'] `` gives ``Read `AGENTS.md` and the README section "Developing SSDLC" ``) |
| `fix_instructions` | The same, before a fix or a sync |
| `module_test_cmd` | The command that runs one test module, written with a placeholder name for the module (ssdlc's: `python3 -m unittest discover -s tests -p 'test_name.py'`): implement, fix and sync ask to run the pertinent modules one at a time with it, and never the full suite (`test_cmd`) |
| `module_test_helpers` | The directory of the helpers the test modules import, which that command still lets them import (needs `module_test_cmd`) |
| `[prompts.adr]` `dir`, `decided`, `open_value`, `open_choice` | The preflight's rule on the ADRs: a decision recorded under the heading `decided` of an ADR in `dir` on the base branch is the operator's and not a gap, while a point the ADR leaves open (`open_value`, `open_choice`: the words that mark a value and a choice still open) settles nothing. The four keys go together, and `dir` must be in `[merge] operator_only_paths`: the rule holds only because the operator alone merges that directory |

A key that is missing drops, whole, the sentence that names it: no instruction to read, no
command for one module (the agent is still told not to run the full suite), no rule on the ADRs
(the preflight is then stricter: a decision in an ADR does not settle a gap). Every value is one
line of text, not empty and without spaces around it; a value that is not, an unknown key, an
incomplete `[prompts.adr]` or a `dir` outside `operator_only_paths` stops devloop at startup. A
value is put in the prompt as it is, never read as a template. The installation's
`devloop/config.toml` has no `[prompts]`.

In a template a sentence of the project is an optional span, `{{?name:text}}`: `text` when the
value `name` is not empty, nothing when it is; a span alone on its line takes the line with it.
A span whose value the dispatcher did not give is an error, never a sentence dropped in silence.

**ssdlc's values** are in its project file ([`ops/host/ssdlc/project.toml`](../../ops/host/ssdlc/project.toml)):
with them the prompts are the same, byte for byte, as before R4, which
`tests/test_devloop_prompt_data.py` proves against the templates of before R4
(`tests/fixtures/ssdlc-pre-r4/prompts/`), with the changes of the templates made since then
listed one by one, each with its issue (`SINCE_R4` in `tests/helpers/ssdlc_project.py`, such as
`host_step`, #462, with the review criterion made precise by #473, or the trust policy, #472). The
text of the trust policy in the review and advisory review prompts is the installation's and does
not change; the readiness check gets the same text, read from the review prompt of the
installation (#472). On a host, the project file gains the
`[prompts]` table before the pin of R4
([ops/host/migrate.md](../../ops/host/migrate.md#dati-di-ssdlc-nei-prompt)).

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
- **Traceback.** The traceback of an unexpected exception of an action (#381) is written in the
  local logs, `<state_dir>/logs/<time>-<action>-<N>-traceback.txt`, never on GitHub, and the
  `unexpected error: …` line of the tick log ends with `(traceback in <file>)`, or with
  `(traceback not written: …)` when the file could not be written. With
  `max_parallel_actions = 1` the tick still stops on such an exception, after the same line.
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
| `"changed"` (ssdlc) | Only the test modules pertinent to the diff, chosen by the project's selection (`[tests] selection`), each with `python3 -m unittest discover -s tests -p '<module>'`, one after the other, stopping at the first that fails. Without a selection, `"full"` |
| `"none"` | None |

Any other value stops devloop at startup. With `"changed"` the diff is the whole one from the
merge base with `origin/main` to the current content of the worktree: the branch's commits, the
index, unstaged changes and new files git does not ignore, computed again after every attempt.

**`[tests]`** (ADR 0006, R5) declares how the dispatcher reads the project's tests. It is a fact
of the project: it belongs in the [project file](#project-file), the overlay may replace its
keys, and the installation sets none. Every key is optional:

| Key | Value | Without it |
|---|---|---|
| `selection` | The installation's strategy, by name. The only one is `"python-unittest-modules"` | `local_tests = "changed"` is `"full"`: the project loses no test, only time. `ci_rerun_unrelated` is off |
| `source_prefixes` | Parameter of the selection: name prefixes, letters, digits and `_` (ssdlc: `["ssdlc_"]`) | No such rule |
| `[tests.dir_modules]` | Parameter of the selection: `"<directory>/" = "test_<prefix>"`, a relative directory and the prefix of test module names (ssdlc: `"devloop/" = "test_devloop"`) | No such rule |
| `ci_log_format` | The format of the CI logs, by name. The only one is `"run-tests"`, what `scripts/run-tests.py` prints | `ci_rerun_unrelated` is off, and the [summary of the CI](#summary-of-the-ci-in-the-prompts) has no suite lines: no job log is read |

An unknown key or name, a parameter without `selection`, a prefix or a directory of another
form stop devloop at startup. `[tests]` never becomes a permission of the agents: the commands
they may run are the installation's and the overlay's (ADR 0006, D2). With
`"python-unittest-modules"` the pertinent modules, among the `tests/test_*.py` files of the
worktree, are:

- every changed or added `tests/test_*.py`;
- for every other changed `<module>.py` (or `<package>/<module>.py`), the modules
  `test_<module>*.py`; for `<prefix><name>.py`, with `<prefix>` one of `source_prefixes`, also
  `test_<name>*.py` (with ssdlc's, `ssdlc_impact.py` selects `test_ssdlc_impact*.py` and
  `test_impact*.py`);
- for any file under a directory of `[tests.dir_modules]`, the modules whose name starts with its
  prefix (with ssdlc's, any file under `devloop/` selects `tests/test_devloop*.py`).

When no module is pertinent, for example when only documentation changed, and with `"none"`,
no test runs locally and the PR goes on to the CI. `implement_test_retries` applies to the tests
that ran: a failure goes back to the implementer with the output, and after the last attempt the
issue or PR goes to `agent:needs-operator`, as with `"full"`.

**The CI stays the gate.** A defect the local modules miss becomes a CI round. The review may
start while the CI runs, but an approval leads to `agent:ready-to-merge` only once the CI of the
same head passes (#271). When the full suite did not run locally (`"changed"` or `"none"`) and
the head has no CI at all past the [window of a new head](#ci-of-a-new-head), the first
reviewer's action of the round runs `test_cmd` locally, in a disposable copy of the PR worktree,
before the review, and a failure sends the PR to `agent:needs-operator` without a review. An
approval given within that window, while no CI was known, gets the same full suite before its
promotion if the window ends without CI: a failure sends the PR to `agent:needs-operator`
without promoting it. With `"full"` an absent CI is enough, as before; a pending CI is not.

The mode and the modules run (`local_tests`: `{mode, modules}`, only `{mode}` with `"full"`) go to the tick log and, as
information only, to the markers of implement and fix: they are not part of the policy receipt
and no approval depends on them.

### CI of a new head

In continuous mode a review can be planned a few seconds after a push, before GitHub registers
the CI of the new head: with `local_tests` other than `"full"` the dispatcher would then run the
whole suite locally ([Local tests](#local-tests)) while the CI is about to start (#263). So a PR
in `agent:review` whose head has no CI at all is treated as if its CI were pending (#271): the
review starts, its prompt says the CI is `pending`, and no full suite runs locally. An approval
waits for the CI of that head, as with a CI pending: it is applied, without a new review, once
the CI passes. A request for changes waits for it too, in `agent:changes`, so that a CI failure
joins the same fix (see [Approval and CI](protocol.md#approval-and-ci)).

- **Grace window.** The wait lasts `[ci] grace_minutes` (default 5) from the push of the head:
  the push the running dispatcher made (implement, fix, sync), else the commit date of the head
  as GitHub reports it, read once. A date in the future, or one that cannot be read, counts from
  the first time the dispatcher looks at the head, so the wait stays bounded.
- **No CI after the window.** The [full suite runs locally](#local-tests) before the review, as
  before; for an approval already given within the window, before its promotion.
- **Required checks.** When the rulesets or the classic branch protection of `base_branch`
  require checks, the merge waits for them instead, up to `[ci] required_timeout_minutes`
  (default 30) from the push, and no full suite runs locally: the review still starts within
  that time. After that the PR goes to `agent:needs-operator` with an escalation
  of class `ci_missing`: it is not among the [classes](guide.md#escalations-to-the-supervisor),
  so it is critical and always goes to the operator. The timeout prevails over a longer grace
  window: with `grace_minutes = 60` and `required_timeout_minutes = 30` the PR is escalated 30
  minutes after the push. If the required checks cannot be read, the
  dispatcher writes a line to the tick log and counts them as none.
- **Head in conflict.** GitHub runs no `pull_request` workflow on a PR that conflicts with
  `base_branch`, so the CI of such a head never comes (#392). It is neither waited for nor
  escalated as `ci_missing`: in `agent:review` the PR is synced as usual, and in `agent:changes`
  the fix starts at once, aligns the branch with the base, and the CI runs on its new head. The
  fix still gets the open findings of the review; its prompt says that there is no CI summary of
  the head because of the conflict. The tick log says `PR #388: no CI expected on 9ac03f72ed71:
  it conflicts with main, and GitHub runs no pull_request workflow on it`.

The tick log says why the head waits for its CI, for example `PR #257: waiting for CI on
9ef3336dfbe6: pushed 12 s ago, within [ci] grace_minutes = 5`, and an approval not promoted yet
says `not promoted: no CI on 9ef3336dfbe6 yet, and the approval did not see it` (or `not
promoted: CI is pending`). With `local_tests = "full"` there is no window: a head without CI is
reviewed and promoted as before, the gate of [Local tests](#local-tests). `grace_minutes` is a whole number of at least
0 (0 turns the window off) and `required_timeout_minutes` one of at least 1; another value, or an
unknown key of `[ci]`, stops devloop at startup.

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
  `ci_failed`, which counts as a round, and its comment cites the re-run (runs and modules). On a
  head whose review round asked for changes (#352) or approved (#359) the failure joins that
  round instead, without a round of its own ([one head, one round](protocol.md#approval-and-ci)).
  A new head starts again from its first failure.

The rerun needs **`actions: write`** on the dispatcher's app. Its token is minted with only that
permission. Without it (GitHub answers 403, or refuses to mint the token; also when the
dispatcher has no app and uses a `gh` login without the permission), or on any other GitHub error,
the dispatcher writes one line to the tick log (`CI rerun refused, …`) and the failure is a round,
as before. `ci_rerun_unrelated = false` turns the re-run off; the default is `true`. The re-run
also needs the project's `[tests]` with a `selection` and `ci_log_format = "run-tests"`
([Local tests](#local-tests)): without either nothing tells the failed modules or their
pertinence, and every CI failure is a round. A marker of
the `rerun` kind counts only when the dispatcher's identity wrote it.

Only a `ci_failed` is re-run this way: a CI failure on a PR that has no round left goes to
`agent:needs-operator` (`round_limit`), as before, unless it joins the review round of its head,
which takes no round of the limit.

### Summary of the CI in the prompts

The agents have no network and cannot run the whole suite in their turn: they cannot measure the
suite after their change (its duration, the number of tests). So the review and fix prompts get
a **CI of the head** section that the dispatcher reads with its own identity (#300):

- **Each check** of the head, with its outcome and its duration (from `started_at` to
  `completed_at` of the check run; `still running` for one in progress, `duration not known`
  for a commit status).
- **The suite.** For each completed GitHub Actions job, the dispatcher reads the whole job log
  (`gh run view --job <id> --log`; the job is the one in the check's link) and, when the job
  runs `scripts/run-tests.py`, shows its final lines `Ran N tests in S (… modules, … split by
  class, … jobs)` and `Slowest: …`. A job without the first of these lines (unittest's own
  `Ran N tests in S` has no parentheses) shows only its outcome and duration. The dispatcher
  reads the logs only when the project declares `[tests] ci_log_format = "run-tests"`
  ([Local tests](#local-tests)); without it every job shows only its outcome and duration.
- **Log not readable.** A log `gh` cannot read (expired, deleted, an error from GitHub) shows
  `the job log could not be read: its test summary is not available`, and the action goes on;
  checks that cannot be read at all leave the section saying the summary is not available. The
  review and the fix never wait for, nor fail on, the summary.

The fix prompt shows the CI of the head the reviewer saw, the PR's head before the fix. A head
without checks that conflicts with the base has no CI to show (#392): the section says so. The
same values go to the `ci_round` event of the [ledger](protocol.md#evidence-and-telemetry).

### Documentation map

`[docs] map` (#203) maps code paths to the documents that describe them, so that the documents
follow the code. It is the documentation phase of devloop, the `07-docs` of an SSDLC run:

```toml
[docs.map]
"devloop/" = ["docs/devloop/guide.md"]
"orchestrator.py" = ["docs/getting-started.md", "docs/architecture.md"]
"schemas/quality-contract.schema.json" = ["docs/configuration.md"]
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

### Budget envelope

`[budget]` (#365, ADR 0004, section 3.3 and P2-5a) books each turn of an agent, before its CLI
starts, in the envelope of its [story](protocol.md#story-identity), with the calculations of the
`core` package (`core/budget.py`), and refuses a turn the envelope does not cover. It is **off by
default** (`enabled = false`, ADR 0004, D8): then devloop books nothing, the
[turn journal](protocol.md#turn-journal) has the same events as before, and the other keys of the
table, when present, are only checked for shape. Its values are the operator's decision (D2):
devloop proposes none, and the distributed `config.toml` shows the table without values.
**It cannot be turned on yet**: see the
[prerequisites of activation](#prerequisites-of-activation) below.

```toml
story_identity = true
[budget]
enabled = true
turns = <turns>                 # turns of the story, in total
wall_seconds = <seconds>        # from the first booking of the story
metered_usd = <dollars>         # measured spend
cli_estimate_usd = <dollars>    # estimates (claude's total_cost_usd)
[budget.role_turns]             # per role: the kind of the turn
implement = <turns>
fix = <turns>
review = <turns>
sync = <turns>
[budget.phase_turns]
implementation = <turns>
[budget.usage.codex]
model = "unmetered_quota"
reason = "<why>"
[budget.usage.agy]
model = "unmetered_quota"
reason = "<why>"
[budget.usage.claude]           # or reserve_per_invocation, with both dollar kinds
model = "unmetered_quota"
reason = "<why>"
```

With `enabled = true` every value is required, with no default, and devloop stops at startup
when one is missing or invalid:

| Key | Value |
|---|---|
| `turns`, `wall_seconds` | Whole numbers of at least 1: the turns of the story in total, and the seconds from its first booking with the budget on (not from its takeover) after which no turn starts |
| `metered_usd`, `cli_estimate_usd` | Dollar caps above 0, kept apart as in the engine: measured spend and estimates. A cap is exhausted at equality, so 0 would refuse every turn. No CLI of devloop reports a measured spend; claude reports an estimate |
| `role_turns` | A whole number of at least 0 for each role: `implement`, `fix`, `review`, `sync`, and `conclave` and `advisory` when the [conclave](guide.md#conclave-on-a-block) or the [advisory review](guide.md#advisory-review-for-the-operator) is configured. The role of a turn is its kind |
| `phase_turns` | A whole number of at least 0 for each kind of phase: `implementation`, the only one of the current flow |
| `usage.<agent>` | The rule for unknown usage of each agent, required for every one, with a `reason`. codex and agy, whose CLI reports no cost, accept only `model = "unmetered_quota"` (no reserve in USD: only the turn caps bound it, D2). claude reports its estimate only when a turn completes, so it needs one too (#396): `unmetered_quota`, or `"reserve_per_invocation"` with `metered_usd` and `cli_estimate_usd` above 0 (a reserve booked before each turn). Its estimate, when reported, is charged with either |

Also on: a story is the unit of the envelope, so `enabled = true` needs `story_identity = true`.
A token cap (any key with `token` in its name, in the table, in `role_turns` or in a rule) stops
devloop at startup, on or off, with `token caps are not supported yet`: the envelope holds turns,
time and attested dollars only, and the semantics of token caps are decided apart. An unknown
key, or a rule for an agent that is not configured, stops it too. The key needs a restart.

**The booking.** Before each turn, retries and fallback models included, the dispatcher checks
the envelope of the story of the turn's issue (the root, or a child the dispatcher associated
with it) and writes the booking in the turn's intent in the journal (`budget`), in the same step
and under the same lock: two parallel turns of a story never both take its last place. The rule
of an agent covers every model of its chain: a fallback runs on the same CLI. A turn is refused,
before its intent, its ledger record and its CLI, when:

- the story has used its turns in total, of the turn's role or of its phase, or its time
  (`LIMIT_REACHED`);
- a dollar cap is exhausted, or the reserve of the turn would pass it (`LIMIT_REACHED`);
- a turn of the story returned without a dollar figure and its agent has no rule: unknown is not
  counted as zero (`LIMIT_REACHED`). With every agent's rule required, claude's included, this
  applies only to a booking made without one;
- a turn of the story has no confirmed outcome: one without outcome that does not run in this
  process, or `uncertain` and not reconciled (`OUTCOME_UNCERTAIN`). A turn running in a parallel
  action of the same story counts, and does not refuse;
- the turn's role has no cap in the frozen envelope of the story, because it was configured
  after its first booking (`ROLE_NOT_IN_ENVELOPE`, see below);
- its issue belongs to no story, for example one taken over before `story_identity` was on
  (`SCOPE_REQUIRED`, see below).

**The envelope of a story is frozen** at its first booking: every booking records the limits of
its story (`envelope`: the turn caps, the dollar caps, `wall_seconds`, the start and the
deadline), and the later turns are checked against them, across restarts. The time of the story
starts at that first booking (`started_at`), not at its takeover: a story in progress since long
before the budget was on is not frozen with a deadline already past. A changed `[budget]` applies
to new stories only: it neither extends nor shrinks one already booked, and the refusal says so
when the configuration differs. Extending a story is an operator's decision, case by case, not
available yet. The rule of an agent for unknown usage is read from the configuration at each
booking, which records it.

**A role configured after the freeze** (the advisory review or the conclave, set up once a story
already has its envelope) has no cap in that envelope. devloop keeps the frozen envelope and adds
no default cap, which would be a value of the budget (D2): the turn is refused with a category of
its own, `ROLE_NOT_IN_ENVELOPE`, whose message names the role and the cap the current
configuration would give it to new stories. New stories get the role with its cap.

**Issues taken over before `story_identity`** belong to no story, and are excluded from the
budget with no migration: with the budget on their turns are refused (`SCOPE_REQUIRED`) with a
message that says so, and `devloop status` shows the same message, a `budget #<n>:` line for each
such issue being worked or with an open devloop PR.

The step then escalates to the operator (class `budget`, critical, with the `category` of the
refusal and, on a PR, its `head`), with the limit and the [state of the budget](#state-of-the-budget)
of the story at the refusal. A conclave call or an advisory review refused this way escalates as
for the block on uncertain turns: the check runs before the conclave records the call as started,
and before the checkout of the advisory review. An advisory review waits only on the escalations
of the current head: one whose refusal a `budget` escalation of the PR already names, with the
same category and the same head, is not planned again, and escalates no more, until the story
covers the turn again. Its escalation says that the review of that head is held; a new head is
tried again and, if refused, escalated again, never held in silence.

**The one extension** (#411, ADR 0005, P5-7). With `[delegations] budget = true` and merge mode
`"auto"`, a turn of `implement`, `review`, `fix` or `sync` refused at a limit (`LIMIT_REACHED`)
goes instead to the supervisor as `budget_grantable` when the story was never extended and the
extension would cover the turn now; the supervisor extends the envelope with
`/devloop grant-budget <reason>`
([Extending the budget envelope](guide.md#extending-the-budget-envelope)). The extension adds to
every turn cap (`turns`, each of `role_turns` and of `phase_turns`) and to `wall_seconds`
`BUDGET_TOLERANCE_PERCENT` (25, a constant of the code, D3) of the value frozen at the first
booking, rounded down, and never touches `metered_usd`, `cli_estimate_usd` or tokens. It is a
`grant` event of the delegation `budget` in the journal, with the `story` and the `extension`
(`percent`, `turns`, `role_turns`, `phase_turns`, `wall_seconds`) and the text of the command
(`reason`), for completing its trace after a stop: once per story, ever, and the authority on it. The bookings keep recording the frozen limits; the envelope of a booking,
of `devloop status` and of the receipt is the frozen one with that extension. An extension that
would add nothing to the exhausted limit (3 turns × 25% = 0), or that a dollar cap, an uncertain
outcome or a role outside the envelope would make useless, is not offered and is not spent.
The grant books the extension, and the first turn of the story booked after it spends it (#466):
if that turn is refused all the same (a PR's turn that waited past even the extended deadline),
the extension lapses with a `grant_lapsed` event in the journal, does not spend the story's one
extension, and the refusal goes through the delegation again; the lapsed grant still counts
against the daily cap.

**Consumption** is cumulative: every turn booked for a story counts, whatever its issue, attempt,
model, outcome or process, and nothing refunds a booking. A failed turn, one not dispatched and
an uncertain one stay counted; a reconciled turn counts with its reconciled outcome. The outcome
of a turn records the dollars its CLI reported (`usage`), which can only raise a charge: with a
reserve, the charge is the larger of the reserve and the observation (ADR 0004, D7).

**Outside the envelope:** the [readiness check](guide.md#readiness-check-before-implementation)
comes before the takeover. It is neither booked nor charged to the story, as before; its outcome
records its `usage` like the others. How to charge it is decided before the budget is turned on.

#### State of the budget

With the budget on (#393), the state of the budget of a story, read from the journal, shows per
dimension what is **consumed**, **reserved** and **available** against its cap: the turns in
total, per role and per kind of phase, the dollars of each kind and the time.

- **Turns**: consumed are those with a confirmed outcome (returned, failed, not dispatched, or
  reconciled), reserved those booked without one (running, uncertain, or left without outcome);
  both are charged, so available is the cap minus both.
- **Dollars**: consumed is the sum of what the CLIs reported; reserved the part of the reserves
  (`reserve_per_invocation`) no reported cost covers. A value no turn reports is shown as
  `non attestato`, never as 0: consumed is `non attestato` when no turn reports one, and `almeno`
  the known sum when some do, with the number of turns that report none; available is
  `non attestato` while one of those turns has no reserve either. A turn not dispatched spent
  nothing. With agy and codex (`unmetered_quota`) and with claude's `metered_usd`, which no CLI
  reports, the dollars are `non attestato`: the turn caps are what bounds them.
- **Time**: the seconds elapsed since the story's start and those left before its deadline.

It appears in three places, and nowhere with the budget off:

- `devloop status`: one line per story of the issues in progress and of the open PRs, each story
  once, as `budget <story> (radice #<root>, al <time>): turni 2 di 10 (consumati 1, prenotati 1,
  disponibili 8); …`; an issue that belongs to no story gets `budget #<n>: ` followed by the
  reason its turns are refused (`SCOPE_REQUIRED`, #396). `status` reads the journal without the
  lock: a turn running in the dispatcher counts as reserved;
- the [PR receipt](protocol.md#pr-receipt), in its section "Budget della storia", as a table;
- the escalation of a turn the envelope refuses, as the state at the refusal.

#### Prerequisites of activation

From the advisory review of #395 (#396). With `enabled = false`, the default, none of them has an
effect. For each point devloop applies the most conservative choice, which adds no value of the
budget (D2 stays with the operator) and can be revised before the activation:

1. **Every agent has a rule for unknown usage, claude included.** A claude turn that ends without
   its `total_cost_usd` (a model not available, a timeout, a crash, no output) would otherwise
   leave the story uncovered, refusing every later turn, the fallback model of the same step
   included. With the budget on, a configuration without `[budget.usage.claude]` stops devloop at
   startup. Nothing is invented: the operator declares `unmetered_quota` or a reserve.
2. **No activation without an extension.** The envelope of a story is frozen, and a story at its
   limit has no way out but `enabled = false` for every story: a new takeover keeps the same story
   and deadline. The extension exists since #411 (`grant-budget`, ADR 0005, P5-7), once per
   story and as a delegation off by default ([The one extension](#budget-envelope)); the gate
   (`BUDGET_EXTENSION` in `devloop/config.py`) stays closed until the operator lifts it, and
   until then `enabled = true` stops devloop at startup with a message that says so. This is the
   gate of every activation.
3. **The time starts at the first booking**, recorded in the envelope (`started_at`), not at the
   takeover. Issues taken over before `story_identity` are excluded with no migration
   (`SCOPE_REQUIRED`), and `devloop status` says so.
4. **A role added after the freeze** keeps the frozen envelope and escalates as
   `ROLE_NOT_IN_ENVELOPE`, not as a generic `LIMIT_REACHED`; no default cap.
5. **The advisory review waits only on the current head**: a `budget` escalation of an earlier
   head holds nothing, and each new head refused is escalated to the operator.

Still open before the activation: the values of the envelope and the reserves (D2), how to charge
the readiness check, and the period of recording only (D8).

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
  `git add` nor `git commit`, because the dispatcher commits. The `test_cmd` of these defaults is
  that of the overlay or of the installation, never that of the
  [project file](#project-file) (ADR 0006, D2). The installation has none since R3: ssdlc's
  overlay lists both explicitly, with `python3 scripts/run-tests.py` first, as agy had them.
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
  goes to the turn's log and to the `agent:needs-operator` comment. When agy's JSON lists
  `denied_actions`, the message ends with `; denied_actions: [...]` (#155), so the escalation
  says which action was denied. It is never an empty result; with exit 0 it never counts as an
  unavailable model either, so it does not start the fallback by itself: only a permission
  denied twice on the same model moves the turn to the next model of the chain, and escalates
  when the chain has none left (see below). The message counts as the CLI's
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
  none. The retry's prompt (#319) adds, before the `## Shell commands` block, a
  `## Denied in the previous attempt` block: what was denied (the command line of the last
  denied action, when agy's JSON reports it, cleaned as the [failure comment](protocol.md#failed-agent-turns)
  cleans commands; for a `command` permission without it, `comando non riportato da agy` with
  the action agy reports; for another permission, its target), not to run it again, and the
  command prefixes the turn may run, from the same list as `--settings`. agy 1.5 reports no
  command line (`{"action":"command","display_name":"Bash"}`).
- **A model denied twice leaves the turn to the next one.** A second denial on the same model,
  when the [fallback chain](#fallback-models) has another model after it, moves the turn to that
  model, as for an unavailable model (#319): the tick log says
  `agy: model <model> denied twice; trying <next model>`, and the denied model is remembered in
  the [memory of unavailable models](#memory-of-unavailable-models) for
  `denied_cooldown_hours` of the agent (default 6, at least 1), with the reason
  ``permission `<name>` denied twice (<what>)``. On the last model, or on an agent without
  fallback models, the second denial escalates as before (`tool_failure`), and the comment
  says ``permission `<name>` denied again after one retry; model `<model>`; <what>``, `<what>`
  being the command or `comando non riportato da agy` as in the retry: whoever decides knows
  whether to run the review again or replace the reviewer. That last model is not remembered.
  Any other failure (an unavailable
  model, a non-zero exit, a stop that names no permission, invalid output, a timeout) is not
  retried: it escalates or starts the fallback as before, except an empty run (see above). After a fallback, the next model gets
  its own single retry. The [readiness check](guide.md#readiness-check-before-implementation) runs
  on the same chain (#148) and gets the same single retry, with the same retry prompt, in the
  same checkout, with its own log and turn record (`issue-<n>-preflight-retry`); its second
  denial moves to the next model, and on the last one escalates naming the model and what was
  denied.

```toml
[agents.agy]
denied_cooldown_hours = 6  # how long a model denied twice is skipped
```

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
  the agent's, and `provider`, never the agent's
  ([Reviewers by provider](#reviewers-by-provider-and-the-third-reviewer), #409). A fallback reasons at `high`, `xhigh` or `max`: any other level, or for agy a
  model whose name ends with `-minimal`, `-low` or `-medium`, stops devloop at startup, as does a
  model listed twice or equal to the agent's own. The list is optional: an agent without it
  behaves as before.
- **When.** Only when the turn failed because the model is unavailable: the CLI exited with a
  non-zero code, or its JSON envelope is explicitly an error (claude `is_error`, agy `status`
  other than `SUCCESS`), and its stdout or stderr matches one of `unavailable_patterns`
  (regular expressions, case-insensitive; default `RESOURCE_EXHAUSTED`, `quota`, `429`,
  `model .* not available`), or the exit code is in `unavailable_exit_codes` (default none).
  A pattern matches only as a delimited word or code, never next to a letter or a digit: `429`
  matches `HTTP 429` and `(code 429)`, not the `…1c42429607c6` of an identifier (#443); a
  pattern that does not compile so, such as one opening with the global flag `(?i)`, stops
  devloop at startup. An
  output whose explicit error codes (`(code 400)`, agy's `"error_code":400`) are all `400`, a
  request the model rejected such as `INVALID_ARGUMENT (code 400)`, never counts, whatever else
  it matches and even with an exit code in `unavailable_exit_codes`. The reason recorded and
  logged names the pattern and quotes at most 200 characters of the line around the match,
  starting at most 80 before it, `…` where cut, even when the match itself is longer. A
  structured result, valid or not against the schema, and output that cannot be parsed (stdout
  that is not JSON or not a JSON object, a codex final message that is not JSON) take
  precedence even with a non-zero exit: only a missing or empty output, an envelope without
  `structured_output` or an explicit error envelope leave room for a match. A result, a parse
  error, a timeout and a stall never count, whatever the text, and follow the usual path to
  `agent:needs-operator`. An agy [empty run](#agy-permissions) (exit 0, no result, no error) also
  moves to the next model, without being remembered, and so does an agy permission
  [denied twice](#agy-permissions) on the same model (#319), which remembers that model for
  `denied_cooldown_hours`; on the last model of the chain either escalates.
- **Once.** Each model is tried at most once per action, but for the single retry of an agy
  denied permission or of an empty run on the last model: a rate limit the CLI reports as
  terminal counts as persistent, and a model found unavailable is not tried again by the later
  attempts of the same action (`implement_test_retries`). When the last model fails too, the
  issue or PR goes to `agent:needs-operator` with a comment listing the models tried and why. A
  second agy denial moves to the next model as an unavailable model does (#319), and on the last
  model escalates the same way. Any other failure after a fallback ends the action without
  trying further models, and its comment lists the models tried too, the failed one included.
- **Worktree.** The fallback is a new session with the same prompt and worktree. Partial changes
  of the previous attempt, and the merge in progress of a sync, stay as they are, without any
  rollback; the prompt tells the agent that the worktree may contain them. The
  [readiness check](guide.md#readiness-check-before-implementation) uses the chain of its checker
  too (#148), in the same checkout of the base branch, with a note of its own: files the previous
  attempt left there are not part of the base branch.
- **Traceability.** The model actually used goes into the attribution header of the text
  published for the turn (the outcome of a readiness check included), into the profile of the
  `pr`, `review`, `fix` or `sync` marker, into
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
  is never recorded: it may be transient. An agy model denied a permission twice in one turn,
  when the turn moves on to the next model (#319, [agy permissions](#agy-permissions)), is
  recorded for `denied_cooldown_hours` (default 6).
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
- **Keys by role** (#407). At the start of `tick` and `run`, the records of a key before the
  migration (`claude`, `codex`, `agy`) that is no longer configured move to the keys that name
  that agent with a role (`claude-implementer`, `claude-reviewer`), each with all of them; of two
  records of the same model the later time stays. The log says so once
  ([The implementer app and the keys by role](#the-implementer-app-and-the-keys-by-role)).
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

Sensitive paths are configurable under `[review_policy]`: `sensitive_paths` (an entry is a
directory or a file at the root) plus the files listed in the `files` object of each JSON file in
`sensitive_path_lists`. Neither has a default since ADR 0006, R3: they are restrictions of the
managed repository, which its [project file](#project-file) adds and the overlay may extend.
ssdlc's project file lists `devloop/`, `AGENTS.md`, `CLAUDE.md`, `.github/`, `schemas/` and the
modules below, and the list `schemas/recovery-grader-baseline.json`. The policy comes from the running dispatcher and its
configuration: the lists are read at startup from the root of the running installation (the
pinned checkout, see [Run from a pinned installation](guide.md#run-from-a-pinned-installation)), never
from a PR branch, so a PR that changes `devloop/config.toml`, the prompts or the lists cannot
apply weaker rules to itself. A list that cannot be read stops devloop at startup. Whatever the
level, a reviewer gives `needs_operator` to a change that touches the trust policy and needs the
operator for it: since #447 a faithful implementation of a decision of the operator recorded in
an ADR on `main` gets the technical verdict, unless it changes the rule itself (#399, see
[Changes to the trust policy](guide.md#changes-to-the-trust-policy)), with `operator_reason`
`trust_policy`: the fix goes on as for `request_changes`, and the merge is the operator's (#434)
unless the same reviewer then approves the latest round as the operator decided on 2026-11-19
(#461, see [Changes to the trust policy](guide.md#changes-to-the-trust-policy)). Since #472 the
rule itself is only the "Trust policy" section of the two prompts and the conditions that decide
when a change needs the operator: a change to `NON_CRITICAL`, to a `/devloop` command or to a
condition of the merge that a decision names explicitly is a faithful implementation, merged
like any other of its level. Since #483 a decision of the operator on the issue is only a
`/devloop approve` command, followed by the text of the decision, that a trusted user wrote in
person, not the supervisor's identity nor an app, after the latest change of the issue's text
and before the takeover (its latest edit too); an approval without the command decides nothing,
and the history of the issue is read in full, or no command counts. devloop lists those commands
under "Decisions of the operator on the issue" in the prompts of the readiness check, of the
reviewers and of the advisory review, and the readiness check answers the ones it refuses. The
quorum and the choice of the reviewers, the checks of `operator_only_paths` and
`protected_paths`, the names of the labels of hold, of escalation and of merge, `trusted_users`
and the "Trust policy" section stay with the operator even when a decision prescribes their
change (#483). The readiness check declares `trust_policy` and blocks, with the question to the
operator, an issue whose change no decision covers ([Readiness check before
implementation](guide.md#readiness-check-before-implementation)).

The criterion for a sensitive path: **the code that decides permissions, fencing and
authorizations goes to level 2**, besides devloop itself and the agents' instructions. So
ssdlc's project file adds the ssdlc security modules (#171): writer
fencing and locks (`ssdlc_writer_gate.py`, `ssdlc_role_fencing.py`,
`ssdlc_container_holder.py`, `ssdlc_locks.py`, `ssdlc_fencing_proof.py`), authorized transitions and gates
(`ssdlc_transitions.py`, `ssdlc_replay_gate.py`, `ssdlc_grader.py`), delegations and
admissions (`ssdlc_feedback_delegation.py`, `ssdlc_feedback_admission.py`,
`ssdlc_usage_admission.py`) and the app tokens (`scripts/agent-token.py`,
`scripts/supervisor-token.py`). A PR that changes
only other modules, such as `ssdlc_impact.py`, stays at level 1. An entry is only a path: a
listed file that does not exist yet is no error, and a PR that adds it gets level 2.

**Level 1** works as before: one reviewer, the PR's recorded reviewer or else the first of the
pool, never the PR's implementer. **Level 2** needs two independent reviewers (#111, below).
When the pool has not enough of them, the PR is neither reviewed nor promoted: it goes to
`agent:needs-operator` with a dispatcher comment that gives the level, its reason (the label and
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
now under the implementer's key, which runs fixes and syncs. Nor may either be the same agent in
effect as one of them, the same `kind` or the same `model` (#407, at level 1 too, against the
implementer: [the keys by role](#the-implementer-app-and-the-keys-by-role)); with
`review_policy.provider_quorum` the quorum rule on the provider replaces this last condition
([Reviewers by provider](#reviewers-by-provider-and-the-third-reviewer), #409). The dispatcher scans the pool in configuration order and takes the first such pair. The
choice is deterministic, so it stays the same for the whole round, and it is recorded in the
receipt (`reviewers`).

Since #111 the PR marker, every fix, every sync whose conflicts an agent resolved (`sync`
marker) and every review record the identity and tuple actually used (`profile`). At level 2,
when a PR or one of its fixes was recorded before, or a sync marker lacks the profile,
independence cannot be verified and the PR goes to `agent:needs-operator`. At level 1 older markers
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
  missing reviewer; the failure goes to `agent:needs-operator` as any failure does, and once a
  human sends the PR back to `agent:review` the next action runs only that reviewer.
- **Aggregation.** The verdicts are applied once both are in: `needs_operator` for a decision
  from either sends the PR to `agent:needs-operator`; otherwise a `request_changes` from either
  starts a single fix that receives every open finding of both; two approvals go to promotion. A
  `needs_operator` for the trust policy (#434) counts as `request_changes` while findings of its
  reviewer stay open, else as an approval whose merge is the operator's (until the same reviewer
  clears it by approving a later round, #461).
- **Quorum.** The PR is promoted only if every reviewer of the receipt recomputed just before
  approved the same head with that same receipt, and no blocking finding is open. A missing
  reviewer, one that timed out (the step goes to `agent:needs-operator`) or one that asked for a
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
  `neutral`, `cancelled` or failed sends the PR to `agent:needs-operator`. The test command is
  `test_cmd` of the running dispatcher and the workflows are under `.github/`, which a PR cannot
  change at level 0; still, do not list in the allowlist files that the CI runs as its own
  harness (for example `scripts/run-tests.py`).
- **No open finding.** A blocking finding still open from an earlier round sends the PR to
  `agent:needs-operator`; the operator can close it with a `/devloop finding` command.
- **No standing request for changes.** A native `REQUEST_CHANGES` review from a user in
  `trusted_users` blocks, also after new commits, until it is dismissed or the same user
  approves; a comment does not withdraw it. It sends the PR to `agent:needs-operator`.

The promotion is a dispatcher comment with a `policy` marker that records the receipt (level 0,
the label, who added it and when, the allowlist outcome) and the state of each expected check,
with, as for any promotion, the [receipt of the PR](protocol.md#pr-receipt) in the same comment.
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
| dispatcher | every read, labels, escalations, CI rounds, re-runs of failed CI jobs not pertinent to the PR (needs `actions: write`, see [CI failures not pertinent to the PR](#ci-failures-not-pertinent-to-the-pr)), finding closures, a PR's readiness for merge, the labels of the readiness check and its escalation to the supervisor, the record of a [conclave](guide.md#conclave-on-a-block), the restart after a `UNANIMOUS` decision and the suspension after a [veto](guide.md#veto), the summary of an [advisory review](guide.md#advisory-review-for-the-operator) |
| readiness checker (`preflight`) | the outcome of the [readiness check](guide.md#readiness-check-before-implementation) on the issue, with its `preflight` record, also when a fallback model ran it |
| implementer | commits (author and committer are its bot), push, opens the PR, fix and blocked comments |
| reviewer | one native GitHub review (`APPROVE` or `REQUEST_CHANGES`; for `needs_operator`, `REQUEST_CHANGES` while blocking findings of its own stay open, else `APPROVE`, #418) on the reviewed commit, whose body is the review and its record (#325, see [Where a review is published](protocol.md#where-a-review-is-published)); an agent without an app publishes the review comment instead |
| advisory reviewer (`advisory_reviewer`) | the [advisory review](guide.md#advisory-review-for-the-operator) of a head of a PR in `agent:needs-operator`: always a native review of type `COMMENT`, also from an agent without an app (with the ambient login) and also when it is the PR's implementer; the dispatcher writes its summary and edits the receipt |
| merger | only the merges of the `strict` and `auto` [merge modes](guide.md#merge-modes) (see [The merger app](#the-merger-app)) |
| supervisor | not devloop: the supervisor's commands, labels, issues, diary and fix or hold requests, with `[roles.supervisor]` as a table (see [The supervisor app](#the-supervisor-app)) |

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
before. With `publication_outbox` on (#385), `Identities.github` hands out each client with its
`comment`, `edit_comment`, `submit_review` and `create_pr` through the
[publication outbox](protocol.md#publication-outbox),
which reconciles an uncertain write on the app's login, or on `trusted_users` for the ambient
login; `Identities.direct` gives the client without it. Under the dispatcher lock, and only
with the outbox on (#397), `Identities.hold` gives its descriptor to every client
(`GitHub.lock_fd`), those created before it included, such as the dispatcher's and the merger's
that devloop builds before taking the lock: the `gh` of each
write keeps the lock open while it runs, so no next dispatcher starts while it may still publish.
That `gh` runs under coreutils `timeout`, a limit of its own that outlives the dispatcher: SIGTERM
once the call's timeout (300 seconds) is spent, SIGKILL 5 seconds later, so a `gh` stuck on the
network never holds the lock for ever; stopped so, the write is uncertain. With the outbox on,
and only then, the clients' `submit_review` and `create_pr` tell an uncertain outcome apart
(`GitHub.uncertain_publications`), as `comment` and `edit_comment` always do.
An app that is configured but whose key is missing (except the
[supervisor's](#the-supervisor-app), whose key devloop never reads) stops devloop at startup instead of
silently falling back to the operator's login: remove it from `[apps]` (or use `--config`) to run
that actor without it. `python3 -m devloop apps` checks each app, its installation and its
permissions.

The apps have no `Workflows` permission, and devloop never pushes changes under
`protected_paths` (default `.github/`): a workflow or a local action pushed to a branch of this
repository would run in CI before any review. Such a change goes to `agent:needs-operator` and is
applied by a person.

Agent CLIs and the test command run without GitHub credentials in reach: `GH_TOKEN` and
`GITHUB_TOKEN` are removed, `gh` gets an empty config directory, and git may only use local
repositories (`GIT_ALLOW_PROTOCOL=file`), so neither SSH keys nor HTTPS credentials apply whatever
the config files say; credential helpers, askpass programs, preconfigured authentication headers
and SSH are disabled as further layers. `DEVLOOP_APP_KEY_DIR` points to a directory without keys,
so the GitHub App helpers in the worktree (`devloop.apps`, `scripts/agent-token.py`,
`scripts/supervisor-token.py`) cannot mint a
token: the dispatcher does every GitHub write. For Claude Code, devloop sets `BASH_MAX_TIMEOUT_MS`
to `test_timeout_seconds`, so a command that asks for it can run the full suite, and
`BASH_DEFAULT_TIMEOUT_MS` to `agent_command_timeout_seconds` (default ten minutes, at most
`test_timeout_seconds`), so a command that sets no timeout, such as a wait loop that never ends,
stops on its own instead of holding the turn for an hour. Values the operator set in the
environment are kept. This prevents accidental use of the operator's credentials, not
deliberate access: code running as the same system user can still read files. The isolation
comes from the [agent user](#agent-user), which cannot read the keys at all.

### Agent user

With `[agent_user] name` set (#415), the dispatcher starts every agent CLI, its `--version`
included, and the test command as that system user, so that they cannot read the private keys
of the apps, `state_dir` nor the local overlay. Without it they run as the dispatcher's user, as
before. The operator chose the mechanism on #415: one dedicated user for every agent (their CLI
logins are not isolated from each other); a container would be an alternative. The installation
is in the [guide](guide.md#agent-user) and in the [ops guide](../../ops/README.md).

- **Start.** `sudo -n -E -u <name> -- <argv>`: `-n` fails at once without a sudo rule, `-E`
  keeps the isolated environment above, which needs the `SETENV` tag. The environment gets the
  agent user's `HOME`, `USER` and `LOGNAME`, where its CLIs keep their logins, and
  `safe.directory=*` for git, because the worktrees belong to the dispatcher's user. The test
  command runs as `/usr/bin/bash -c <test_cmd>` and the CLIs are found on the `secure_path` of
  the sudoers rule.
- **Stop.** A timeout or a stall `abort` kills the process group of the turn or of the tests as
  the agent user (`sudo -u <name> /usr/bin/kill -KILL -- -<pgid>`), then as the dispatcher's
  user, which may signal only its own processes. The sudoers rule sets `!use_pty`, so that the
  command stays in the process group devloop started and the stall check sees its processes.
- **Files outside the worktree.** What a turn reads or writes besides its worktree is in
  `<state_dir>/agent-shared`, which the agent user enters but cannot write: the empty `gh` config
  (`gh-config`), the schema of each turn (`schemas/`, which it cannot write either; codex and agy
  read it from a path), the final message of codex (`last.json` in a new directory of the turn
  in `turns/`), the private temporary directories of the tests (a new directory in `tmp/`, with
  [parallel actions](#parallel-actions)). The agent user writes only in those new directories,
  which inherit a default ACL; `turns/` and `tmp/` themselves are read-only for it, so it cannot
  replace what devloop creates there. devloop opens every component of these paths from
  `state_dir` without following a link (`O_NOFOLLOW`), creates the schema exclusively
  (`O_EXCL`), reads the final message back only from a regular file with a single link (a link,
  a hard link or a FIFO fails the turn), copies it to the turn's log and parses that copy, and
  removes the schema and the directory after the turn. Without an agent user nothing changes:
  the `gh` config stays in `<state_dir>/agent-gh-config`, the final message of codex in `logs/`,
  the schema in `devloop/schemas/` and the temporary directories in the system's.
- **Git metadata.** The git commands devloop runs in a worktree (status, add, commit, merge,
  push, …) get `GIT_DIR`, `GIT_COMMON_DIR` and `GIT_WORK_TREE` from the repository: the entry of
  `.git/worktrees` whose `gitdir` names the worktree, never the worktree's own `.git` file, which
  the agent can rewrite. A `commondir` of that entry leading elsewhere than the repository's
  `.git` stops the command (git would read the refs there). Every such command runs with
  `core.hooksPath=/dev/null` and `core.fsmonitor=false`: no hook nor fsmonitor program, not even
  the repository's. The agent user cannot write `.git`: `worktrees/` is read-only for it, like
  `turns/`, so a worktree cannot be replaced either.
- **Startup check.** At the start of `tick` and `run` (not `tick --dry-run` nor `status`), a
  probe run as the agent user must find that it can neither read nor enter `key_dir` (or
  `$DEVLOOP_APP_KEY_DIR`) and any entry of `state_dir` but `worktrees` and `agent-shared` (a
  directory it can enter without listing it fails too: a file in it would be in reach by name),
  that it cannot read the keys, the `--local-config` file and `state_dir` (list it), and that it
  can write nothing in the repository's `.git`. It must enter but not write `worktrees`,
  `agent-shared` and its `schemas`, `turns` and `tmp`; it must write a new directory devloop
  makes in `turns` and in `tmp`, and read a schema devloop writes. The agent user must also
  differ from the dispatcher's. Otherwise devloop exits, naming each path, before taking the
  lock. `[agent_user] check = false` skips the probe and logs that it is off: for development
  only.
- **Transcripts.** The stall check and `devloop follow` read the session transcripts in the agent
  user's home.

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
before, and the `merged` record carries in `merged_by` the app's login. The text of the
"Mergiata da devloop" section, which the dispatcher adds to the comment of the promotion
([record](protocol.md#merge-directives-and-record)), names the app that ran the merge; without
the app, the local `gh` login.

### The supervisor app

The supervisor has a role app of its own (ADR 0005, P5-4, #408): `brushtyler-devloop-supervisor`,
created by the operator with `Issues: Read and write`, `Pull requests: Read and write` and
`Metadata: Read-only`, and **no** `Contents`, because the supervisor neither implements nor
merges. It signs the supervisor's commands on escalations, its labels (`agent:ready`, priorities,
dependencies), its issues, the diary and its `/devloop fix` and `/devloop hold` requests, whatever
agent runs the supervisor. `config.toml` declares it:

```toml
[apps.supervisor]
app_id = 5824243
slug = "brushtyler-devloop-supervisor"
```

It counts only when the operator turns it on (D10, off by default), with `[roles.supervisor]` as
the table of the supervisor actor, in the local overlay:

```toml
[roles.supervisor]
name = "claude-supervisor"
kind = "claude"
provider = "anthropic"
model = "claude-opus-5-5"
```

- `claude-supervisor` is not a key of `[agents]`: devloop does not run the supervisor. Its `name`
  names agent and role in full (`<kind>-supervisor`), and no agent may have it nor be called
  `supervisor`.
- The supervisor's identity is then the login of `[apps.supervisor]` (`<slug>[bot]`), and no
  longer the app of an agent: wherever the commands, labels and requests of the supervisor
  count ([labels](guide.md#labels-from-the-supervisor),
  [holds](guide.md#merge-modes),
  [fixes](guide.md#fix-requested-by-the-operator-or-the-supervisor),
  [escalations](guide.md#escalations-to-the-supervisor)), those of the agent's app that was the
  supervisor stop counting. An issue the supervisor opens with its app counts as one of devloop's
  apps, as one opened with the agent's app did.
- A PR counts as implemented by the supervisor's agent (ADR 0002, principle 4, and
  `[delegations] retry_own_prs`) when the `kind`, the `model` or the provider of its
  implementer, as the profile of the PR's marker records them, is the actor's (the configured
  agent the marker names, for a marker without a profile); a PR whose agent cannot be read counts
  as the supervisor's. The actor's provider is the `provider` it declares; the implementer's is
  read from the prefix of its model
  ([the keys by role](#the-implementer-app-and-the-keys-by-role)). The keys are never compared,
  since `claude-supervisor` is never an implementer's key.
- devloop never reads the app's key and never writes with it: the key is in `key_dir` of the
  supervisor's system user, not in devloop's, and `python3 -m devloop apps` does not list it.
  `scripts/agent-token.py` refuses it, so no review or interactive session can write a command as
  the supervisor. The supervisor runs, from its own clone and as its own user:

  ```bash
  python3 scripts/supervisor-token.py -- gh issue comment <ISSUE> --body-file <FILE>
  python3 scripts/supervisor-token.py -- gh issue edit <ISSUE> --add-label agent:ready
  python3 scripts/supervisor-token.py --perm pull_requests=write -- gh pr comment <PR> --body-file <FILE>
  ```

  The script reads `[apps.supervisor]` from `devloop/config.toml` and only that key, from
  `$DEVLOOP_APP_KEY_DIR` or `key_dir`. Without `--perm` the token gets `issues=write` and
  `pull_requests=write`; only `issues` and `pull_requests` (read or write) and `metadata=read`
  may be asked for. The token reaches the command through `GH_TOKEN` only, and is never printed.
- Without the table (the key of an agent, or no `roles.supervisor`), nothing changes: the app is
  only declared, and its comments and labels count for no role.

Turning it on is best done with an empty queue: a command or label of the old identity written
before the switch, still unapplied, no longer counts afterwards (an issue admitted only by the
agent's `agent:ready` is no longer admitted, and needs the label again), which stops the flow
rather than widening it.

### The implementer app and the keys by role

The implementer has a role app of its own too (ADR 0005, P5-3, #407):
`brushtyler-devloop-implementer`, created by the operator with `Contents: Read and write`,
`Pull requests: Read and write`, `Issues: Read and write` and `Metadata: Read-only`, without
`Workflows`. `config.toml` declares it:

```toml
[apps.implementer]
app_id = 5824194
slug = "brushtyler-devloop-implementer"
```

It signs the commits, the pushes, the PRs and the records of the fixes and of the syncs of every
agent that declares it, whichever agent does the work; the agent and its model stay in the
attribution header, in the `implementer` key and the profile of the PR marker (whose `identity`
is the app's login), and in the commit trailer. Off by default (D10): `config.toml` keeps the
keys `claude`, `codex` and `agy`, nothing declares the app, devloop does not read its key and
`python3 -m devloop apps` does not list it. The operator turns it on with an **empty queue**, by
renaming the keys in `config.toml`. Not in the
local overlay: the overlay merges the tables key by key and cannot remove one, so the keys
`claude`, `codex` and `agy` of `config.toml` would stay beside the new ones, and two keys that
sign with the same app (`codex` and `codex-reviewer` with `[apps.codex]`) stop devloop at
startup (`apps.codex: each app belongs to one actor`):

```toml
[roles]
implementer = "claude-implementer"
reviewers = ["codex-reviewer", "agy-reviewer"]

[agents.claude-implementer]   # the table of [agents.claude]
app = "implementer"
# ...
[agents.codex-reviewer]       # the table of [agents.codex]
app = "codex"
# ...
[agents.agy-reviewer]         # the table of [agents.agy]
app = "agy"
# ...

[review_policy]
advisory_reviewer = "claude-reviewer"   # once claude-reviewer exists (P5-5, #409); "" until then
```

- **One key, one role.** A key may name agent and role in full, `<kind>-implementer` or
  `<kind>-reviewer`, where `<kind>` is the agent's `kind`. Such a key serves that role only:
  `roles.implementer`, the values of `[roles.implementers]` and the `impl:` labels take
  implementer keys; `roles.reviewers` and `review_policy.advisory_reviewer` reviewer keys;
  `preflight`, `conclave` and `roles.supervisor` as an agent key take no key with a role, until
  they get one. A key used in a role it does not name stops devloop at startup. A key without a
  role (`claude`, the keys before the migration) serves the roles it is given, as before; it may
  not be both a reviewer and a key that implements.
- **Declared app.** An agent signs with `[apps.<app>]` of its `app`, or without `app` with the
  table of its key, as before; without such a table, with the ambient `gh` login. Only a
  `<kind>-implementer` key may declare `app = "implementer"`, and several may; an implementer
  key signs with the implementer app or with the ambient login, never with an agent's app. No
  agent declares `dispatcher`, `merger` or `supervisor`. Every other app belongs to one agent
  only; an app no key declares is allowed only for devloop's own apps and for `claude`, `codex`
  and `agy`, whose tables stay to read the records they signed. A declared app without its table
  stops devloop at startup.
- **Records.** The fixes and syncs of a PR count when the app of its implementer's key wrote
  them; a review, a readiness check or a conclave's record never counts when written for a key
  that signs with a role app. `scripts/agent-token.py` refuses `implementer`, as it refuses
  `dispatcher` and `merger`. The records of before name `claude`, `codex` and `agy`: they are
  read as they are, with the app of that name, and never rewritten; the daily summary counts a
  review of `codex` when `codex-reviewer` is configured.
- **Conflicts by agent, never by key.** A reviewer is skipped, at level 1 and at level 2, when
  it is the same agent in effect as the PR's implementer (at level 2 as any author of the PR):
  the same `kind`, the same `model` or the same provider, as the profile of the marker records
  them, and the models of the agent configured under the implementer's key. The provider is the
  one the agent or the fallback declares (`provider`), else the one the prefix of the model's
  name gives (`claude-` Anthropic, `gpt-` OpenAI, `gemini-` Google), and a model with another
  prefix and no declared provider is compared only by `kind` and `model`. So neither
  `claude-reviewer` nor agy run with a Claude model reviews the code of `claude-implementer`.
  At level 2 this is stricter than the quorum rule (ii) of D8, which admits one reviewer of the
  authors' provider: that rule replaces this check with `review_policy.provider_quorum`
  ([Reviewers by provider and the third reviewer](#reviewers-by-provider-and-the-third-reviewer)),
  off by default. A PR whose
  implementer has no profile and no configured key has no reviewer at level 1, and goes to the
  operator. The pool is no longer refused in the configuration because of the implementer's key.
  The check on the PRs of the supervisor's agent compares the agent in the same way, with
  `roles.supervisor` as an agent key too ([The supervisor app](#the-supervisor-app)). The
  reviewer the PR marker records is the first of the pool that is not the implementer's agent.
- **Fallback models of a reviewer.** The same check applies, at both levels, to every model a
  reviewer's turn actually runs, fallbacks included: a model that is the same agent in effect
  as an author of the PR (at level 1 the implementer, as the selection reads it) is skipped
  without running, and when no model of the chain is left the turn fails without a review. At
  level 2 the model must also stay independent by tuple of the authors and distinct from the
  other reviewers of the round, as before.
- **Choice of the implementer** (D7). At the takeover of an issue, in order: an `impl:<kind>`
  label chooses `<kind>-implementer`; else the area labels of `[roles.implementers]`
  (`"<label>" = "<implementer key>"`, empty by default); else `roles.implementer`. A label counts
  only while the issue has it and its latest `labeled` event comes from a trusted user, or from
  the supervisor on an issue opened by a trusted user or by one of devloop's apps, as for the
  priority labels (#178); any other is ignored. Two `impl:` labels, an `impl:` label of an
  implementer not configured, area labels that lead to different implementers, and an
  implementer that is the same agent as `preflight` send the issue to the operator
  (`configuration`), without a takeover. The key chosen is in the `request` record of the ledger
  and in the PR marker, and a label changed later does not change the implementer of an open PR:
  its fixes and syncs run with the key of its marker.
- **Startup.** Before `tick` and `run` devloop moves the memory of the unavailable models to the
  keys by role ([Memory of unavailable models](#memory-of-unavailable-models)), then stops when
  an open devloop PR (of this repository, by a trusted user or one of devloop's apps, on a devloop
  branch) has a marker whose implementer or reviewer is not a configured key, or a key of the
  other role, and when the open PRs cannot be read. Nobody else's PR can stop devloop.

### Reviewers by provider and the third reviewer

ADR 0005, P5-5 (#409): the reviewers are excluded by the agent in effect and its provider, and
claude becomes the third reviewer of the pool, as a reserve. **Off by default** (D10):
`[review_policy] provider_quorum = false`, and the reviewers are chosen as in
[the keys by role](#the-implementer-app-and-the-keys-by-role).

- **Declared provider** (D8, option (c)). Every agent and every fallback model may declare who
  makes its model: `provider = "anthropic"` in `[agents.<key>]`, and `provider` in each entry of
  `fallback_models`, which never inherits the agent's (agy may fall back on a Claude model). It is
  read lowercase. A table of the known prefixes of the model names (`claude-` Anthropic, `gpt-`
  OpenAI, `gemini-` Google) is the check: a model with one of them declared with another
  provider, or an empty provider, stops devloop at startup, with the rules on or off. Without a
  declared provider devloop reads it from that prefix, as before. A declared provider is in the
  profile of the markers and the receipts (`provider`) and in the policy version: declaring one
  voids the approvals of the open PRs, as any change of the pool does. Without any declared
  provider the version is the one before.
- **Turning it on.** `provider_quorum = true` requires the provider of every agent and of every
  fallback model (`agents.<key>.provider: required with review_policy.provider_quorum` stops
  devloop otherwise), and enters the policy version. Both keys may be set in the local overlay.
  The rules then apply at both levels, to every PR, and replace the exclusion of the same agent:
  - **Independent by tuple** of every author of the PR, at level 1 too: the implementer, every
    fixer, every sync with conflicts and the models of the agent configured under the
    implementer's key, as at level 2. Another GitHub identity and another `(kind, model,
    effort)`. The kind alone no longer excludes: `claude-reviewer` with Sonnet may review the
    code of Opus.
  - **Quorum rule (ii)** of D8: at level 1 the only reviewer is of a provider other than every
    author's; at level 2 at least one of the two is, and the two are distinct as before. A
    provider that is not known, the reviewer's or an author's, is never another one. Level 1
    takes the PR's recorded reviewer, then the pool; level 2 the first valid pair of the pool, in
    configuration order. Without one, the PR goes to `agent:needs-operator` as before.
  - **Every model a turn runs**, fallbacks included, keeps the same rules: independent by tuple
    of the authors, distinct from the round's other reviewers, and the quorum rule on the round
    with that model in it (the reviewers not yet run counted with their configured model). A
    model that breaks them is skipped without running; when none of the chain is left the turn
    fails without a review (`tool_failure`). A fallback model is also never of an author's
    provider, whatever the quorum (D8, "Fallback dello stesso fornitore"): on a PR implemented
    by claude, agy does not fall back on a Claude model even when codex already gives the round
    another provider. Only a reviewer's primary model is subject to the quorum rule alone. The
    authors are those of the selection, with every model of the implementer's chain. The same
    ban without the quorum rule, at both levels, the check at startup and the providers recorded
    for the fallbacks the implementer and the fixers ran are `fallback_provider_ban` (P5-6,
    [No fallback on the authors' provider](#no-fallback-on-the-authors-provider)).
  - **The reviewer the marker records** is the first of the pool of another provider and
    another tuple than the implementer's models.
- **Unavailable reviewer.** When a **new round** chooses its reviewers, a reviewer whose whole
  chain is remembered unavailable ([Memory of unavailable models](#memory-of-unavailable-models):
  only an agent with fallback models) is skipped, and the next valid one of the pool takes its
  place. It is never skipped when it owns an open finding of the PR, which only it may close in
  a later review: the groups that keep the owners of the open findings come first. When every
  valid group has an unavailable reviewer, the first is kept and its turn fails as
  `tool_failure`, as before. A reviewer is never replaced in the middle of a round: until the
  round ends, the receipt keeps the reviewers it recorded while they still meet the rules, so a
  promotion or a merge recomputes the receipt the round was approved with, whatever the memory
  says by then. The promotion reads the round from the records it applies, not only from the
  snapshot: with parallel reviewers a round that chose another pair than the previous one ends
  in the same action, and is promoted with its own pair. The log names the skipped reviewer.
- **The third reviewer** (D8). `claude-reviewer`, kind `claude`, with a model other than the
  implementer's (Sonnet 5.5 against Opus 5.5), last in the pool, signing with `claude-agent`
  (`app = "claude"`). It needs the keys by role and the supervisor's role app first: before the
  migration `claude-agent` also signs the implementer's work, and two keys that sign with the
  same app stop devloop at startup (`apps.claude: each app belongs to one actor`). It reviews
  only when one of the first two is missing, unavailable or not independent, and at level 2 a
  PR of claude only beside a reviewer of another provider:

```toml
[review_policy]
provider_quorum = true

[roles]
implementer = "claude-implementer"
reviewers = ["codex-reviewer", "agy-reviewer", "claude-reviewer"]

[agents.claude-implementer]   # the table of [agents.claude]
app = "implementer"
provider = "anthropic"
# ...
[agents.codex-reviewer]       # the table of [agents.codex]
app = "codex"
provider = "openai"
# ...
[agents.agy-reviewer]         # the table of [agents.agy]
app = "agy"
provider = "google"
fallback_models = [
    { model = "gemini-4-hydrogen-high", model_label = "Gemini 4 Hydrogen", provider = "google" },
]
# ...
[agents.claude-reviewer]      # the table of [agents.claude], with Sonnet
app = "claude"
provider = "anthropic"
model = "claude-sonnet-5-5"
model_label = "Claude Sonnet 5.5"
# ...
```

Changing the pool, the providers or the rule changes the policy version and voids the approvals
of the open PRs: turn it on when no PR is close to its merge.

### No fallback on the authors' provider

ADR 0005, P5-6 (#410): a reviewer's fallback model never runs on a PR when its provider is that
of an author of the PR. It is a rule apart from the quorum rule of D8: it holds with
`provider_quorum` on or off. **Off by default** (D10): `[review_policy] fallback_provider_ban =
false`. Both keys may be set in the local overlay.

- **Every provider declared.** On, every agent and every fallback model declares its `provider`
  (`agents.<key>.provider: required with review_policy.fallback_provider_ban` stops devloop
  otherwise), checked against the prefixes of the model names as in
  [Reviewers by provider](#reviewers-by-provider-and-the-third-reviewer). So the profile a PR,
  fix or sync marker records carries the provider of the model the turn actually ran, a fallback
  included.
- **At runtime**, at level 1 and at level 2, a fallback model of a reviewer is skipped without
  running when its provider is not another one than every author's: the implementer, every
  fixer and every sync with conflicts as their markers record them, with the models they
  actually ran, and every model of the agent configured now under the implementer's key. A
  provider that is not known, the fallback's or an author's, is never another one; a PR whose
  markers do not record who wrote the code admits no fallback. Another reviewer of the round of
  another provider does not make the fallback admissible: on a PR implemented by claude,
  reviewed by codex and agy, agy does not fall back on a Claude model. When no model of the
  chain is left the turn fails without a review (`tool_failure`). The reviewer's primary model
  stays subject only to the other rules (the exclusion of the same agent, or the quorum rule with
  `provider_quorum`).
- **At startup**, as an early check, the configuration is refused when a reviewer of the pool
  has a fallback model of a provider other than its own model's and the same as the model of an
  agent that may implement: `roles.implementer`, the keys of `[roles.implementers]` and every
  `<kind>-implementer` an `impl:<kind>` label may choose (`agents.agy-reviewer.fallback_models[0]
  .provider: 'anthropic' is the provider of 'claude-implementer', which may implement, …`). It is
  the case of agy with a Claude fallback. The check at runtime still holds the rule: the authors
  of a PR are known only per PR.
- **Policy version.** On, the ban enters the policy version: turning it on voids the approvals
  of the open PRs, so that none given by a fallback of an author's provider survives. Off, the
  version is the one before.

```toml
[review_policy]
fallback_provider_ban = true
```
