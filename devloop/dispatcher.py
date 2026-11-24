"""The dispatcher: reads GitHub state, plans the next steps, runs them.

State lives on GitHub only (labels plus hidden markers in comments), so a tick can crash at any
point and the next one resumes.

Identities: with GitHub Apps configured (``apps``), the dispatcher acts with its own app and each
agent with its own, and every record is bound to the identity that may write it: a PR to its
implementer's app, a review to its reviewer's app, a CI verdict to the dispatcher's app. Without
an app an actor uses the ambient ``gh`` login, and its records must come from ``trusted_users``.

Issue  agent:ready ─► agent:working ─► PR opened
PR     agent:review ─► (CI fail | request_changes) ─► agent:changes ─► agent:review ─► …
                   └─► approve ─► agent:ready-to-merge ─► merged (a human, or devloop, #172)
       any step can escalate to agent:needs-operator.

Readiness is bound to the approved commit: a PR is promoted only if its head is still the
reviewed commit and CI passed. A ready PR whose head moves, or whose CI is running again, goes
back to review (to agent:changes when CI fails on it).

A PR in review or ready that conflicts with the base branch is neither reviewed nor promoted: a
``sync`` merges the base into it, with the implementer resolving the conflicts, and sends it
back to review. A sync is not a review round.

Review level (``policy``): every agent review, CI round and promotion records a receipt of the
running dispatcher's review policy for the head, with the reviewers the level requires: one at
level 1, at level 2 the first pair of the pool that is independent (#111). A round is one
evaluation of a head: a CI failure, or one review by each required reviewer, run one per action
and applied together once all are in, so that a single fix receives every open finding. A PR
whose pool has not enough independent reviewers goes to agent:needs-operator. A round is promoted
only if every required reviewer approved the head with the receipt recomputed just before,
otherwise a new review is needed.

Base advancing (#160): a receipt that differs only in the commit of base still holds, both to
join an open round and to promote, if no commit of the base branch in between touched a file of
the PR's diff (``_base_check``). An open round abandoned only because the base touched the PR
is replaced by a new one and does not count against ``max_review_rounds`` (``counted_rounds``).

Resync after approval (#235): when the latest round approved a head and the PR's head differs
from it only by merges of the base branch that git concludes without conflicts
(``Workspace.clean_resync``), the review of the new head records ``resync`` and does not count
either. A sync whose conflicts an agent resolved changes the PR's content: its review records ``resync``
with the reason ``resync_conflicts`` (#458), runs also beyond ``max_review_rounds`` and counts
only if it does not approve. Nor does the new review of an approved head whose approval a dispatcher ``stale`` policy record
revoked because the base touched the PR (#252, ``base_revocation``): it records ``resync`` with
the reason ``base_changed``.

Level 0 (#107) needs no review: when a trusted user's label allows it and the whole diff is in
the operator's allowlist, the head goes to ready-to-merge once every expected check run
succeeded on it, no finding is open and no trusted user's request for changes stands.

Findings (#110): every blocking finding has an owner, ``round/reviewer/ID``. Only its owner
closes it in a later review; the dispatcher closes CI findings when CI passes on the current
head, and a trusted user can close any finding with ``/devloop finding <key> resolved|withdrawn
<reason>``. Both closures are dispatcher ``finding`` records. An approval is not promoted while
another owner's finding is open.

Readiness check (#114, with ``preflight`` configured): before ``implement`` a read-only agent,
other than the implementer, checks on a checkout of the base branch that the issue can be
implemented in one run. Its outcome is a ``preflight`` record on the issue, bound to the hash of
the title, the body and the trusted comments, and reused while that hash holds. A failed check
holds also only until someone who may admit the issue adds agent:ready again, and, when a gap
depends on the base branch, only for the commit it ran on (#270). An issue that is
not ready goes to agent:needs-operator; the check removes that label only if it added it itself.

Conclave (#141, with ``conclave`` configured): an issue or PR in agent:needs-operator whose latest
escalation is a block (an implementer or fixer that stopped as ``blocked``, a review round whose
verdict is ``needs_operator``) gets one conclave of three distinct agents: the one that stopped
proposes one resolution, the other two vote on exactly that text, bound by its hash. Each call is
a ``conclave`` action with a slot of its own; the outcome (``UNANIMOUS``, ``BLOCKED``,
``RESERVED``) is a dispatcher ``conclave`` record bound to the block, the content and the head.
The labels stay: the escalation is still the operator's (``dispatch_conclave``).

Advisory review (#369, with ``review_policy.advisory_reviewer``): a devloop PR in
agent:needs-operator gets, once per head, one ``advisory`` action: a read-only turn of that agent,
published as a native ``COMMENT`` review with its identity, and the dispatcher's summary with the
opinion, also added to the current receipt of the same head. No round, no finding, no label, no
promotion; a failure is never escalated, and a publication left halfway resumes on a later tick
without a new turn (``dispatch_advisory``).

Start order (#146): ready issues start by priority (``priority:high``, ``normal``, ``low``; a
``priority_labels`` label counts as high), then by the oldest current ``agent:ready``, then by
number. A priority label counts only if a trusted user added it.

Dependencies (#277): a ready issue with a native "blocked by" dependency on an open issue is
neither checked nor implemented, and takes no slot; it becomes eligible by itself once the last
block closes. A supervisor's ``/devloop retry`` of a blocked issue's step waits the same way,
unspent. A block counts only if the login that added it (the latest timeline event) may
admit the issue (``_admitters``); any other is ignored and logged. With the same priority, an
issue that blocks other ready issues starts first. The issues of the queue on a dependency cycle
do not start, and the dispatcher reports the cycle once on each of them (``cycle`` record),
without labels: once the cycle is gone, they start as usual.

Supervisor labels (#178): with ``roles.supervisor`` and its GitHub App (the agent's app, or
with ``[roles.supervisor]`` as a table the role app ``[apps.supervisor]``, #408),
``agent:ready`` and the priority labels count also when the supervisor's app added them, but
only on issues opened by a trusted user or by one of devloop's apps (``label_admitters``). On anyone else's issue only a
trusted user's label counts. The issue text stays trusted only while no one outside
``trusted_users`` edits it after the label. With ``[roles.coordinator]`` (#528, #595) the
coordinator's ``agent:ready`` and priority labels count on the same issues, and its app is one of
devloop's: an issue it opened that the readiness check declares touching the trust policy waits
for a trusted user's ``agent:ready`` (``_coordinator_gate``).

Authorization (#607, with ``[roles.coordinator]``): the coordinator authorizes the work with
``/devloop authorize [priority:<p>]``, which the ``authorize`` action records once, bound to the
SHA-256 of the title and the body, removing ``agent:needs-coordinator``; a command of any other
identity is refused and recorded. The supervisor's ``agent:ready`` admits an issue only while an
authorization holds for its current text (``_ready_admitters``); otherwise it is ignored and
logged. A readiness check that declares the trust policy touched on an issue no trusted user
opened sends it to agent:needs-operator, authorized or not (``_coordinator_gate``).

Parallel actions (#176, ``max_parallel_actions`` above 1): the actions of a tick on different
issues and PRs run at once, up to that many (``schedule``). Actions on the same number, or on a
PR and its issue, run one after the other in plan order; a global action (``GLOBAL_ACTIONS``)
or a merge runs alone. Each log line carries the number of its action (``TickLog``), a failed action does
not stop the others, and a review action at level 2 runs together every reviewer its round
still waits for.

Continuous mode (#227, ``devloop run`` with ``max_parallel_actions`` above 1, ``Workers``): no
barrier at the end of a tick. A tick plans and starts actions in the free slots, without waiting
for them; the loop runs the next tick as soon as one ends, or as news on GitHub arrive while a
slot is free. The issues and PRs of the running actions are left out of the plan (``plan``'s
``busy``), so no two actions on the same number overlap, and a global action starts only when
nothing runs, alone (``admit``). A merge runs in a lane of its own (#264, ``MERGE_LANE``): one
at a time, alongside the actions on other numbers. ``devloop tick`` keeps the barrier.

Merge (#172, ``[merge]``): with ``mode = "off"`` (the default) a person merges. With ``strict``
the dispatcher merges a ready PR, with the local ``gh`` login and ``--match-head-commit``, once
every merge directive holds again just before the merge: the conditions of the promotion, CI
``pass``, ``MERGEABLE`` and ``CLEAN``, neither ``agent:needs-operator`` nor ``agent:merge-hold``.
``auto`` adds ``/devloop hold <reason>`` and ``/devloop release`` from the supervisor's identity
or a trusted user. A level override or an operator-only path can only make the mode stricter.

Operator merge (#248): a ready PR that devloop does not merge (mode ``off``, a level ``off``, an
operator-only path) gets ``agent:needs-operator`` and an ``operator_merge`` notice, one comment
per head, edited when the head is promoted again, that says why (``_do_operator_merge``). That
label, and only while the notice of the current promotion is the latest of the dispatcher's
and the dispatcher added it after the notice, does not stop devloop
(``_operator_merge_owned``): the PR is synced, its CI checked, and when its readiness decays the
dispatcher removes the label with ``agent:ready-to-merge``. Any other ``agent:needs-operator``,
or the name it had before #248 (``config.LEGACY_NEEDS_OPERATOR``), hands the PR or the issue
to the operator as before.

Merged issues (#228): after a merge the dispatcher closes the PR's issue itself, with a
``closed`` record, and removes ``agent:ready``: the keywords "Closes #N" have no effect when the
merger's app merges. An issue already closed is left alone. A ready issue that a merged devloop
PR resolved is never checked nor implemented again: it becomes a ``merged_issue`` escalation,
non-critical (``_do_merged_issue``).

Escalation classes (#182, ``supervision``): every escalation records its class. In ``auto`` a
non-critical one (``tool_failure``, ``preflight_default``, ``sync_failed``) goes to
agent:needs-supervisor, unless something makes it critical (``_supervised``), and the
supervisor's identity or a trusted user decides it with ``/devloop retry``, ``/devloop
accept-defaults``, ``/devloop decide <text>`` or ``/devloop escalate <reason>``
(``_do_supervise``). Critical and unknown classes always go to agent:needs-operator. On an item
in agent:needs-operator a trusted user may use ``/devloop retry`` on an escalation that records
its step, and ``accept-defaults`` or ``decide`` on a ``preflight_default`` check (#321,
``_operator_command``); a command that does not apply is answered with the way to resume.

Requested fixes (#195): ``/devloop fix`` from a trusted user or the supervisor's identity, on a
PR in agent:review or agent:changes, runs a fix of the current head with the rest of the comment
as a requirement, without a review and without a round (``_do_requested_fix``). One such fix per
head: a later command on the same head counts only after a new commit (``_fix_requests``).

CI of a new head (#263, ``[ci]``): with ``local_tests`` other than ``full``, a PR in agent:review
whose head has no CI is treated for ``grace_minutes`` after its push as if its CI were pending
(``_ci_wait``): the push the dispatcher recorded, else the commit date. Then the full suite runs
locally, as before, unless the base branch requires checks: the PR waits for them, and after
``required_timeout_minutes`` it is escalated (``ci_missing``). A head that conflicts with the
base is never waited for nor escalated (#392): GitHub runs no ``pull_request`` workflow on it, so
the sync, or the fix that aligns the branch with the base, starts at once.

Review during the CI (#271): a review starts with the CI of the head pending, or within that
window, without the full suite locally; its record says CI ``pending``. The approval is promoted
only once the CI of the same head passes, replayed without a new review (``_approval_current``);
a CI that fails joins that same round as a ``ci`` finding (#359, ``ci_joins_round``), voids the
approval and makes the round's verdict ``request_changes``, without a round of its own. An
approval that saw no CI, on a head still without CI past the window, gets the full suite
locally before its promotion (``_do_apply_review``). A request for changes made before the CI
was known waits for that CI in agent:changes (``changes_before_ci``): a failure joins that same
round as a ``ci`` finding (#352, ``ci_joins_round``), without a round of its own, so that one
fix answers both.
"""

import functools
import hashlib
import json
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .agents import (TAIL_LINES, AgentError, ModelsUnavailable, ModelUnavailable, NoOutput,
                     PermissionDenied, TurnCancelled, background_unfinished, denied_retry_note)
from .apps import COORDINATOR, DISPATCHER, ISSUE_COMMENT, PR_COMMENT, SUPERVISOR, app_login
from .authorization import (AUTHORIZE, AUTHORIZE_COMMAND, authorize_arguments, changed_since,
                            text_hash, valid_authorization)
from .budget import BUDGET, OUTSIDE, BudgetRefused, extension_text
from .budget import of_issue as budget_of_issue
from .budget import reserve as budget_reserve
from .budget import status_lines as budget_status_lines
from .config import INSTALL_ROOT, LEGACY_NEEDS_OPERATOR, ConfigError, key_role
from .context import (_cli_state_refused, _describe_bases, _failure_text, label_styles,
                      _open_findings_reason,
                      _render_issue_comments, both_labels_warning, issue_decisions,
                      label_transition, refused_approvals, transition_labels)
from .dispatch_actions import ActionsMixin, Models, ready_approval, rerun_of
from .dispatch_advisory import AdvisoryFailures, AdvisoryMixin, AdvisoryResults
from .dispatch_base import BaseMixin, MainAlerts
from .dispatch_conclave import ConclaveMixin
from .dispatch_merge import MergeMixin
from .dispatch_preflight import PreflightMixin
from .dispatch_supervision import TEXTS as SUPERVISION_TEXTS
from .dispatch_supervision import SupervisionMixin, UntrustedChange, budget_fields
from .generated import changed_by_tests, generated_files
from .github import GitHubError, GitHubUncertain, author_login
from .i18n import ENGLISH, Catalog
from .journal import TEXTS as JOURNAL_TEXTS
from .journal import (CLOSED, MERGED, RECONCILE_COMMAND, JournalError, TurnBlocked, blocked_text,
                      reconcile_command)
from .ledger import attempt_branch, comment_ref, fix_register, open_turns, parse_branch
from .localtests import module_command, select_modules
from .markdown import (carries_record, dispatcher_header, publishable, read_marker,
                       render_authorize, render_cycle, render_escalation, render_operator_merge,
                       resume_text)
from .activity import iso, restart_line
from .outbox import SENT as PUBLISHED, describe as outbox_describe
from .pin import (PREFIX, CheckFailed, PinError, PinHeld, Repinned, ScanFailed, SmokeError,
                  held_prs, public_reason)
from .planning import (BASE_CHANGED, MERGE_LANE, MERGING, RESYNC_AFTER_APPROVAL,
                       RESYNC_CONFLICTS, PullRequest, Snapshot,
                       admit, changes_before_ci, ci_unknown, counted_rounds, issue_priority,
                       label_names, numbers_of, operator_blocked, operator_held,
                       pending_reviewers, plan, pr_counts, queue_lines, round_reasons,
                       runs_issue_step, schedule, waived_rounds, waiver, zero_labels_hold)
from .policy import (BINDING, MAX_LISTED_PATHS, agent_profile, differences, profile_of,
                     receipt_of)
from .ports import Backend, Tracker, summarize_checks
from .review import (CI, OPERATOR, UNPROVEN, current_round, finding_commands, finding_key,
                     flow_verdict, open_findings, owner_of, round_verdict, trust_policy_parts)
from .supervision import (ACCEPT_DEFAULTS, BUDGET_GRANTABLE, COORDINATOR_DELEGATED, DECIDE,
                          RETRY, ROUND_GRANTABLE, TOOL_FAILURE, UNCERTAIN_TURN,
                          operator_rounds_text, supervisor_command)
from .workspace import WorkspaceError

# The colours of the labels of the review level, of level 0 and of the priorities; their
# descriptions, in the project's ``language`` (#635), are ``label_*`` of ``TEXTS``.
LEVEL_LABEL_COLOR = '5319e7'
# How many prerequisites outside the queue a snapshot reads at most to follow the dependencies
# of the ready issues (#277): beyond them, cycles are not looked for.
MAX_DEPENDENCY_READS = 50
# The dropped publications one alert lists at most (#397): the others are in the local log.
MAX_DROPS_LISTED = 30
PRIORITY_LABEL_COLORS = {'priority:high': 'b60205', 'priority:normal': 'c2e0c6',
                         'priority:low': 'c5def5'}
ZERO_LABEL_COLOR = 'c5def5'
# The texts the dispatcher composes itself, in the project's ``language`` (#635): the warnings
# on ``[run].alert_issue``, the escalations and records of this module, the descriptions of the
# labels. English for a language without translation; markers, commands and labels are the same
# in every language. ``field_*``: the fields of a policy receipt whose differences void an
# approval.
TEXTS = Catalog(
    en={
        'label_level': 'devloop: the PR requires a review of level 2',
        'label_zero': 'devloop: level 0, no review if the diff is in the allowlist',
        'label_priority_high': 'devloop: high priority, starts before the other ready issues',
        'label_priority_normal': 'devloop: normal priority (implicit)',
        'label_priority_low': 'devloop: low priority, starts after the other ready issues',
        'field_pr': 'PR',
        'field_base': 'base commit',
        'field_head': 'head',
        'field_policy': 'policy version',
        'field_level': 'level',
        'field_reviewers': 'required reviewers',
        'field_profiles': 'identity, model, effort or arguments of the reviewers',
        'kept_binary': 'binary',
        'kept_more': '- … and {more} more files',
        'kept_text': ('\n\nThe timed-out turn left uncommitted changes: {files} files, +{added} '
                      '−{removed} lines. devloop did not commit them: it saved them as evidence '
                      'in `{file}` in `{logs}`. A retry of the same step reapplies them to the '
                      'new worktree before starting the agent; if they do not apply, it passes '
                      'them to the agent in the prompt.\n\n'),
        'rounds_more': ' plus {granted} granted',
        'rounds_counted': 'Rounds counted against the limit ({count} of {limit}, '
                          '`max_review_rounds`{more}):',
        'rounds_none': '- none',
        'rounds_free': 'Not counted:',
        'rounds_delegation': ('The conditions of the delegation of the rounds are verified: the '
                              'supervisor may grant another round to this PR with `/devloop '
                              'grant-round <reason>`, where the reason reports or cites the '
                              'analysis of the causes.'),
        'untrusted_no_label': 'no label `{label}` added by a trusted user',
        'untrusted_text': 'the text was changed by `{who}` after the label `{label}`',
        'untrusted_title': 'the title was changed by `{who}` after the label `{label}`',
        'review_no_reviewer': 'the review of round {round} does not record the reviewer',
        'review_other_owners': ('the review of round {round} of `{owner}` attributes findings to '
                                'others: {keys}'),
        'authorize_not_coordinator': ('only the identity of the coordinator '
                                      '(`[roles.coordinator]`) authorizes the work'),
        'authorize_untrusted': ('the issue was opened neither by a trusted user nor by an app of '
                                'devloop: only a trusted user queues it'),
        'authorize_changed': ('the title or the body of the issue changed after the command (or '
                              'in the same second): the text read is the one authorized, a new '
                              '`{command}` is needed'),
        'pin_check_title': 'Failed auto-pin',
        'pin_check_text': ('The copy of `{ref}` at commit `{sha}` did not pass the check before '
                           'the smoke test. `current` stays on the previous copy, the pin '
                           'request is cancelled and the loop goes on with the running copy.\n\n'
                           'Reason: `{reason}`\n\n'
                           'The pin starts again with a new commit of `{ref}` that touches the '
                           'installation, or by hand (`devloop pin`) after fixing the cause. '
                           '`devloop status` shows the reason until a pin succeeds.'),
        'pin_smoke_title': 'Auto-pin refused by the smoke test',
        'pin_smoke_text': ('The copy of `{ref}` at commit `{sha}` did not pass the smoke test at '
                           'step `{step}`. `current` stays on the previous copy, the pin request '
                           'is cancelled and the loop goes on.\n\n'
                           'The detail is in the local log; the refused copy stays in '
                           '`rejected-{sha}` in the state directory, for the analysis.'),
        'unexpected_error': 'unexpected error of the dispatcher ({error})',
        'log_hint': ' Local log: `{name}` in `{logs}`.',
        'tail_hint': (' The last lines of the output (at most {lines}, without secrets or '
                      'absolute paths) are at the bottom of the comment.'),
        'step_failed': 'The step `{step}` failed: {failure}{hint}',
        'advisory_held': ('\n\nThe advisory review of head `{head}` stays suspended until the '
                          'story covers the turn. A new head does not stay suspended in silence: '
                          'its advisory review is tried again and, if refused, reported with '
                          'another escalation.'),
        'cli_state': ('\n\nThe directories of the CLIs of the agent user hold an entry devloop '
                      'does not admit (#517). With the loop stopped, the operator removes it as '
                      'the agent user and looks for the turn or the tests that wrote it, or adds '
                      'it to the list with a PR, as in `ops/host/migrate.md`, section "Directory '
                      'delle CLI con lo sticky bit (#517)".'),
        'drop_line': '\n- `{pub}`: {what}, by `{actor}`: {reason}',
        'drops_more': '\n- … and {more} more, listed in the local log',
        'drops_title': 'Dropped publications',
        'drops_text': ('The outbox of the publications dropped these writes: they were not '
                       'published on GitHub and will not be.{listed}\n\n'
                       'The body of each stays in the journal of the turns (`turn-journal.jsonl` '
                       'in the state directory), with its id: it must be published by hand, if '
                       'still needed.'),
        'journal_title': 'Journal of the turns near its limit',
        'journal_text': ('The journal of the turns (`turn-journal.jsonl` in the state directory) '
                         'is {size} bytes, {percent}% of the limit of {limit} bytes '
                         '(`journal_max_mib`). Beyond the limit the journal does not load and '
                         'accepts no event: no turn starts until the operator raises the limit. '
                         'devloop removes nothing from it and does not compact it. This warning '
                         'is published once per threshold.'),
        'attempt_unreadable': 'the journal of the turns is not readable: {error}',
        'attempt_bound': 'ties it to the attempt `{attempt}`',
        'attempt_unbound': ('has the active attempt `{attempt}` of issue #{issue} without a '
                            'recorded PR, which may be this one'),
        'attempt_no_mark': ('the marker of the PR has no attempt, but the journal of the turns '
                            '{which}: the field `attempt` was removed, or its absence cannot be '
                            'told from a removal'),
        'attempt_unconfirmed': ('the attempt `{attempt}` of the marker of the PR is not '
                                'confirmed by the journal of the turns, which is its authority'),
        'attempt_other_pr': ('the attempt `{attempt}` of the marker is the one of PR #{pr}, not '
                             'of this one'),
        'attempt_concluded': ('the attempt `{attempt}` of this PR is already concluded '
                              '(`{outcome}`): a PR reopened after the conclusion of its attempt '
                              'does not resume the work'),
        'attempt_escalation': ('The attempt recorded in the marker of this PR does not hold: '
                               '{problem}. devloop does not go on with the work on this PR: the '
                               'operator decides.'),
        'tests_failed': 'The local tests fail even after the attempts of correction.',
        'blocked_title': 'Work blocked',
        'protected': ('The change touches protected paths (`protected_paths`), which devloop '
                      'does not publish: a workflow or a local action on a branch of this '
                      'repository would be run by the CI before any review. The commit stays in '
                      'the local worktree; the change must be applied by hand.'),
        'stale_no_receipt': 'the approval has no policy receipt',
        'stale_changed': 'changed: {fields}',
        'stale_missing': 'the approval of {keys} is missing',
        'stale_bases': ('\n\nCommits of `{branch}` between the base of the approval and the '
                        'current one:\n\n'),
        'stale_round_free': ('The new review of the same commit opens a new round, which does '
                             'not count against the limit of the rounds; the open findings stay '
                             'unchanged.'),
        'stale_round': 'The new review opens a new round; the open findings stay unchanged.',
        'stale_title_promotion': 'Approval not valid for the promotion',
        'stale_title_merge': 'Approval not valid for the merge',
        'stale_text_promotion': ('The policy receipt computed again before the promotion does not '
                                 'match the one of the approval ({what}). The PR stays in review: '
                                 'a new review is needed, of the same commit too. '),
        'stale_text_merge': ('The policy receipt computed again before the merge does not match '
                             'the one of the approval ({what}). The PR stays in review: a new '
                             'review is needed, of the same commit too. '),
        'reviewer_decision': ('A reviewer asks for a decision of the operator: the reason is in '
                              'its review.'),
        'round_limit_open': ('Reached the limit of {limit} review rounds with blocking findings '
                             'still open.'),
        'merge_off': '- `[merge] mode = "off"`: devloop does not merge the PRs.',
        'reserved_more': ' and {more} more',
        'reserved_paths': ('- the PR touches paths reserved to the operator '
                           '(`[merge] operator_only_paths`): {shown}{more}.'),
        'level_off': '- `[merge.levels]` sets `off` for the review level {level} of this PR.',
        'conclave_unblocked': ('- the work of this PR started again after the decision of the '
                               'conclave recorded in comment {comment}: after each unblock '
                               'devloop does not merge it alone. The operator merges it, or the '
                               'coordinator with its reserved merge if the decision was unanimous '
                               'and still holds and a complete review round after the restart '
                               'approved the head.'),
        'interrupted': ('Implementation interrupted: the issue was being worked on but there is '
                        'no open devloop PR.'),
        'ci_green': 'CI green on commit {head}',
        'closed_resolved': 'resolved',
        'closed_withdrawn': 'withdrawn',
        'closed_by_ci': 'the CI',
        'closed_by_operator': 'the operator `{author}`',
        'closed_line': '- `{finding}`: {status} by {who}: {reason}',
        'findings_title': 'Findings closed',
        'findings_text': 'From now on these findings are closed: no later review reopens them.',
        'unattributable': ('The owner of some findings cannot be rebuilt from the markers of this '
                           'PR, so devloop does not know who may close them and does not go on: '
                           'the review and the decision on the merge are a person\'s.\n\n'),
        'lost_meta': ('The description of the PR no longer ends with the devloop marker of issue '
                      '#{issue}, so implementer and reviewers are not known. Restore the line of '
                      'the marker (it is in the history of the edits of the description) as the '
                      'last line.'),
        'ci_missing': ('On head `{head}` there is no CI {minutes} minutes after the push, but '
                       '`{branch}` requires the checks {names}: the review does not start '
                       'without the CI. Check that the CI started on the head, or run it '
                       'again.'),
        'round_limit_both': 'of the supervisor or of the operator',
        'round_limit_operator': 'of the operator',
        'round_limit': ('Reached the limit of {limit} review rounds: a new round (review or '
                        'failed CI) does not start without a decision {who}. A failed CI on the '
                        'head of a review round that approved or requested changes enters that '
                        'round and does not open a new one.'),
        'trust_intro': ('- the PR touches the trust policy: the review and the merge of the '
                        'operator are needed. Parts touched declared by the reviewers:'),
        'trust_no_parts': 'the reviewer did not name them.',
    },
    it={
        'label_level': 'devloop: la PR richiede una review di livello 2',
        'label_zero': 'devloop: livello 0, nessuna review se il diff è nell\'allowlist',
        'label_priority_high': 'devloop: priorità alta, parte prima delle altre issue pronte',
        'label_priority_normal': 'devloop: priorità normale (implicita)',
        'label_priority_low': 'devloop: priorità bassa, parte dopo le altre issue pronte',
        'field_pr': 'PR',
        'field_base': 'commit di base',
        'field_head': 'head',
        'field_policy': 'versione della policy',
        'field_level': 'livello',
        'field_reviewers': 'reviewer richiesti',
        'field_profiles': 'identità, modello, effort o argomenti dei reviewer',
        'kept_binary': 'binario',
        'kept_more': '- … e altri {more} file',
        'kept_text': ('\n\nIl turno in timeout ha lasciato modifiche non committate: {files} file, '
                      '+{added} −{removed} righe. devloop non le ha committate: le ha salvate come '
                      'evidenza in `{file}` in `{logs}`. Un retry dello stesso passo le riapplica '
                      'al worktree nuovo prima di avviare l\'agent; se non si applicano, le passa '
                      'all\'agent nel prompt.\n\n'),
        'rounds_more': ' più {granted} concessi',
        'rounds_counted': 'Round contati sul limite ({count} di {limit}, '
                          '`max_review_rounds`{more}):',
        'rounds_none': '- nessuno',
        'rounds_free': 'Non contano:',
        'rounds_delegation': ('Le condizioni della delega dei round sono verificate: il '
                              'supervisore può concedere un altro round a questa PR con '
                              '`/devloop grant-round <motivo>`, dove il motivo riporta o cita '
                              'l\'analisi delle cause.'),
        'untrusted_no_label': 'nessuna label `{label}` messa da un utente fidato',
        'untrusted_text': 'il testo è stato modificato da `{who}` dopo la label `{label}`',
        'untrusted_title': 'il titolo è stato modificato da `{who}` dopo la label `{label}`',
        'review_no_reviewer': 'la review del round {round} non registra il reviewer',
        'review_other_owners': ('la review del round {round} di `{owner}` attribuisce finding ad '
                                'altri: {keys}'),
        'authorize_not_coordinator': ('solo l\'identità del coordinatore '
                                      '(`[roles.coordinator]`) autorizza il lavoro'),
        'authorize_untrusted': ('l\'issue non è stata aperta da un utente fidato né da un\'app di '
                                'devloop: la mette in coda solo un utente fidato'),
        'authorize_changed': ('il titolo o il corpo dell\'issue sono cambiati dopo il comando (o '
                              'nello stesso secondo): si autorizza il testo che si è letto, serve '
                              'un nuovo `{command}`'),
        'pin_check_title': 'Pin automatico non riuscito',
        'pin_check_text': ('La copia di `{ref}` al commit `{sha}` non ha superato il controllo '
                           'prima dello smoke test. `current` resta sulla copia precedente, la '
                           'richiesta di pin è annullata e il ciclo continua con la copia in '
                           'esecuzione.\n\n'
                           'Motivo: `{reason}`\n\n'
                           'Il pin riparte con un nuovo commit di `{ref}` che tocca '
                           'l\'installazione, oppure a mano (`devloop pin`) dopo aver corretto la '
                           'causa. `devloop status` mostra il motivo finché un pin non riesce.'),
        'pin_smoke_title': 'Pin automatico rifiutato dallo smoke test',
        'pin_smoke_text': ('La copia di `{ref}` al commit `{sha}` non ha superato lo smoke test '
                           'al passo `{step}`. `current` resta sulla copia precedente, la '
                           'richiesta di pin è annullata e il ciclo continua.\n\n'
                           'Il dettaglio è nel log locale; la copia rifiutata resta in '
                           '`rejected-{sha}` nella directory di stato, per l\'analisi.'),
        'unexpected_error': 'errore inatteso del dispatcher ({error})',
        'log_hint': ' Log locale: `{name}` in `{logs}`.',
        'tail_hint': (' Le ultime righe dell\'output (al massimo {lines}, senza segreti né '
                      'percorsi assoluti) sono in fondo al commento.'),
        'step_failed': 'Il passo `{step}` è fallito: {failure}{hint}',
        'advisory_held': ('\n\nLa review consultiva della head `{head}` resta sospesa finché la '
                          'storia non copre il turno. Una nuova head non resta sospesa in '
                          'silenzio: la sua review consultiva viene ritentata e, se rifiutata, '
                          'segnalata con un\'altra escalation.'),
        'cli_state': ('\n\nLe directory delle CLI dell\'utente degli agent contengono una voce '
                      'che devloop non ammette (#517). A loop fermo, l\'operatore la rimuove come '
                      'utente degli agent e cerca il turno o i test che l\'hanno scritta, oppure '
                      'la aggiunge alla lista con una PR, come in `ops/host/migrate.md`, sezione '
                      '"Directory delle CLI con lo sticky bit (#517)".'),
        'drop_line': '\n- `{pub}`: {what}, di `{actor}`: {reason}',
        'drops_more': '\n- … e altre {more}, elencate nel log locale',
        'drops_title': 'Pubblicazioni abbandonate',
        'drops_text': ('L\'outbox delle pubblicazioni ha abbandonato queste scritture: non sono '
                       'state pubblicate su GitHub e non lo saranno.{listed}\n\n'
                       'Il corpo di ognuna resta nel journal dei turni (`turn-journal.jsonl` '
                       'nella directory di stato), con il suo id: va pubblicato a mano, se serve '
                       'ancora.'),
        'journal_title': 'Journal dei turni vicino al limite',
        'journal_text': ('Il journal dei turni (`turn-journal.jsonl` nella directory di stato) è '
                         'di {size} byte, il {percent}% del limite di {limit} byte '
                         '(`journal_max_mib`). Oltre il limite il journal non si carica e non '
                         'accetta eventi: nessun turno parte finché l\'operatore non alza il '
                         'limite. devloop non ne rimuove nulla e non lo compatta. Questo avviso '
                         'si pubblica una volta per soglia.'),
        'attempt_unreadable': 'il journal dei turni non è leggibile: {error}',
        'attempt_bound': 'le associa il tentativo `{attempt}`',
        'attempt_unbound': ('ha il tentativo attivo `{attempt}` dell\'issue #{issue} senza PR '
                            'registrata, che potrebbe essere questa'),
        'attempt_no_mark': ('il marker della PR non ha il tentativo, ma il journal dei turni '
                            '{which}: il campo `attempt` è stato rimosso, o la sua assenza non si '
                            'distingue da una rimozione'),
        'attempt_unconfirmed': ('il tentativo `{attempt}` del marker della PR non è confermato '
                                'dal journal dei turni, che ne è l\'autorità'),
        'attempt_other_pr': ('il tentativo `{attempt}` del marker è quello della PR #{pr}, non di '
                             'questa'),
        'attempt_concluded': ('il tentativo `{attempt}` di questa PR è già concluso '
                              '(`{outcome}`): una PR riaperta dopo la conclusione del suo '
                              'tentativo non riprende il lavoro'),
        'attempt_escalation': ('Il tentativo registrato nel marker di questa PR non vale: '
                               '{problem}. devloop non prosegue il lavoro su questa PR: decide '
                               'l\'operatore.'),
        'tests_failed': 'I test locali falliscono anche dopo i tentativi di correzione.',
        'blocked_title': 'Lavoro bloccato',
        'protected': ('La modifica tocca percorsi protetti (`protected_paths`), che devloop non '
                      'pubblica: un workflow o una action locale su un branch di questo '
                      'repository verrebbe eseguito dalla CI prima di qualsiasi review. Il '
                      'commit resta nel worktree locale; la modifica va applicata a mano.'),
        'stale_no_receipt': 'l\'approvazione non ha una ricevuta di policy',
        'stale_changed': 'cambiati: {fields}',
        'stale_missing': 'manca l\'approvazione di {keys}',
        'stale_bases': ('\n\nCommit di `{branch}` fra la base dell\'approvazione e quella '
                        'corrente:\n\n'),
        'stale_round_free': ('La nuova review dello stesso commit apre un nuovo round, che non '
                             'conta per il limite dei round; i finding aperti restano '
                             'invariati.'),
        'stale_round': 'La nuova review apre un nuovo round; i finding aperti restano invariati.',
        'stale_title_promotion': 'Approvazione non valida per la promozione',
        'stale_title_merge': 'Approvazione non valida per il merge',
        'stale_text_promotion': ('La ricevuta di policy ricalcolata prima della promozione non '
                                 'coincide con quella dell\'approvazione ({what}). La PR resta in '
                                 'review: serve una nuova review, anche dello stesso commit. '),
        'stale_text_merge': ('La ricevuta di policy ricalcolata prima del merge non coincide con '
                             'quella dell\'approvazione ({what}). La PR resta in review: serve '
                             'una nuova review, anche dello stesso commit. '),
        'reviewer_decision': ('Un reviewer chiede una decisione dell\'operatore: il motivo è '
                              'nella sua review.'),
        'round_limit_open': ('Raggiunto il limite di {limit} round di review con finding '
                             'bloccanti ancora aperti.'),
        'merge_off': '- `[merge] mode = "off"`: devloop non mergia le PR.',
        'reserved_more': ' e altri {more}',
        'reserved_paths': ('- la PR tocca percorsi riservati all\'operatore '
                           '(`[merge] operator_only_paths`): {shown}{more}.'),
        'level_off': ('- `[merge.levels]` imposta `off` per il livello di review {level} di '
                      'questa PR.'),
        'conclave_unblocked': ('- il lavoro di questa PR è ripartito dopo la decisione del '
                               'conclave registrata nel commento {comment}: dopo ogni sblocco '
                               'devloop non la mergia da solo. La mergia l\'operatore, o il '
                               'coordinatore con il suo merge riservato se la decisione era '
                               'unanime e resta valida e un round di review completo dopo la '
                               'ripartenza ha approvato l\'head.'),
        'interrupted': ('Implementazione interrotta: la issue era in lavorazione ma non esiste '
                        'una PR devloop aperta.'),
        'ci_green': 'CI verde sul commit {head}',
        'closed_resolved': 'risolto',
        'closed_withdrawn': 'ritirato',
        'closed_by_ci': 'la CI',
        'closed_by_operator': 'l\'operatore `{author}`',
        'closed_line': '- `{finding}`: {status} da {who}: {reason}',
        'findings_title': 'Finding chiusi',
        'findings_text': 'Da ora questi finding sono chiusi: nessuna review successiva li riapre.',
        'unattributable': ('Non è possibile ricostruire il proprietario di alcuni finding dai '
                           'marker di questa PR, quindi devloop non sa chi può chiuderli e non '
                           'prosegue: la review e la decisione sul merge spettano a una '
                           'persona.\n\n'),
        'lost_meta': ('La descrizione della PR non termina più con il marker devloop della issue '
                      '#{issue}, quindi implementer e reviewer non sono noti. Ripristinare la riga '
                      'del marker (si trova nella cronologia delle modifiche della descrizione) '
                      'come ultima riga.'),
        'ci_missing': ('Sul head `{head}` non risulta alcuna CI {minutes} minuti dopo il push, ma '
                       '`{branch}` richiede i check {names}: la review non parte senza la CI. '
                       'Verificare che la CI sia partita sul head, o rilanciarla.'),
        'round_limit_both': 'del supervisore o dell\'operatore',
        'round_limit_operator': 'dell\'operatore',
        'round_limit': ('Raggiunto il limite di {limit} round di review: un nuovo round (review o '
                        'CI fallita) non parte senza una decisione {who}. Una CI fallita sul head '
                        'di un round di review che ha approvato o chiesto modifiche entra in quel '
                        'round e non ne apre uno nuovo.'),
        'trust_intro': ('- la PR tocca la policy di fiducia: servono la review e il merge '
                        'dell\'operatore. Parti toccate dichiarate dai reviewer:'),
        'trust_no_parts': 'il reviewer non le ha indicate.',
    })
# Appended to the prompt of a turn that runs on a fallback model after another model of the
# same turn was unavailable (#121): its session may have changed the working directory.
FALLBACK_NOTE = {
    'write': ('\n\n## Previous attempt on another model\n\nAn earlier attempt at this turn, on '
              'another model, stopped because that model was unavailable. The worktree may '
              'contain its partial work, and a merge in progress stays in progress: check '
              '`git status` and `git diff` first, keep what is correct and finish the task.\n'),
    'review': ('\n\n## Previous attempt on another model\n\nAn earlier attempt at this review, on '
               'another model, stopped because that model was unavailable. The working directory '
               'may contain files that attempt left: they are not part of the pull request, '
               'which is what `git diff` against the base shows.\n'),
    # The readiness check (#148): a review-mode turn on a checkout of the base branch, no PR.
    'preflight': ('\n\n## Previous attempt on another model\n\nAn earlier attempt at this '
                  'readiness check, on another model, stopped because that model was '
                  'unavailable. The checkout may contain files that attempt left: they are not '
                  'part of the base branch, which is what `git status` and `git diff` show '
                  'against it.\n'),
    # The conclave (#141): a review-mode turn on a checkout of the block's commit.
    'conclave': ('\n\n## Previous attempt on another model\n\nAn earlier attempt at this '
                 'conclave call, on another model, stopped because that model was unavailable. '
                 'The checkout may contain files that attempt left: they are not part of the '
                 'commit of reference, which is what `git status` and `git diff` show against '
                 'it.\n'),
}
# The writing steps whose uncommitted work a timeout keeps for the retry of the step (#374). A
# sync is not one: its worktree holds a merge in progress, which a new worktree starts again.
KEEPS_WORK = ('implement', 'fix')
# At most this many characters of a kept patch that does not apply go to the prompt, and this
# many of its files to the escalation (#374).
MAX_KEPT_PROMPT = 100_000
MAX_KEPT_LISTED = 30


def kept_work_file(label):
    """The file of ``<state_dir>/logs`` that keeps the uncommitted work of a timed-out writing
    turn of step ``label`` (``issue-7-implement``, ``pr-9-fix-r2``) until its retry (#374)."""
    return f'timeout-{label}.diff'


def _kept_line(t, name, added, removed):
    return f'- `{name}`: ' + (t('kept_binary') if added is None else f'+{added} −{removed}')


def kept_work_text(kept, state_dir, language=ENGLISH):
    """The paragraph an escalation gets for the uncommitted work a timed-out turn left (#374),
    in ``language`` (#635): the evidence file, its files and lines, and what a retry does with
    it."""
    t = TEXTS.texts(language)
    stats = kept['stats']
    added = sum(a for _, a, _ in stats if a is not None)
    removed = sum(r for _, _, r in stats if r is not None)
    listed = [_kept_line(t, *entry) for entry in stats[:MAX_KEPT_LISTED]]
    if len(stats) > MAX_KEPT_LISTED:
        listed.append(t('kept_more', more=len(stats) - MAX_KEPT_LISTED))
    return (t('kept_text', files=len(stats), added=added, removed=removed, file=kept['file'],
              logs=f'{state_dir}/logs') + '\n'.join(listed))


def kept_work_note(timeout, stats, applied, error, patch):
    """The note appended to the prompt of the retry of a step whose previous turn timed out
    leaving uncommitted work (#374): reapplied to the new worktree, or, when ``git apply``
    refused it, the patch itself, cut to ``MAX_KEPT_PROMPT`` characters. ``patch``: its bytes,
    decoded here for the prompt only (bytes that are not UTF-8 shown as replacement
    characters); a string is taken as it is."""
    if isinstance(patch, bytes):
        patch = patch.decode('utf-8', 'replace')
    files = '\n'.join(f'- `{name}`' for name, _, _ in stats) or '- (none listed)'
    note = ('\n\n## The previous turn timed out\n\nThe previous turn of this same step was '
            f'stopped after {timeout} seconds (`agent_timeout_seconds`) before it finished, and '
            'left uncommitted changes. devloop saved them; this worktree is a new one. ')
    if applied:
        note += ('They have been reapplied to it, uncommitted, before this turn started. '
                 f'Files:\n\n{files}\n\nExamine them first (`git status`, `git diff`): keep what '
                 'is correct, fix what is not, then finish the task.\n')
    else:
        text = patch if len(patch) <= MAX_KEPT_PROMPT else (
            patch[:MAX_KEPT_PROMPT] + f'\n[… cut: the patch has {len(patch)} characters]')
        fence = '`' * max(3, 1 + max((len(run) for run in re.findall('`+', text)), default=0))
        note += ('They could not be reapplied to it (`git apply`: '
                 f'{error or "refused"}), so this worktree does not contain them. Files:\n\n'
                 f'{files}\n\nThis is their diff, against the commit the previous turn started '
                 'from: examine it first, redo what is still correct, then finish the task.\n\n'
                 f'{fence}diff\n{text.rstrip()}\n{fence}\n')
    return note + ('\nTo end within the time limit, run the targeted tests of what you change '
                   'first, and any long suite only after them.\n')


def _timestamp(value):
    """Seconds since the epoch of an ISO 8601 date with its zone, as GitHub writes them, or
    None when ``value`` is not one."""
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment.timestamp() if moment.tzinfo is not None else None


def with_native_reviews(comments, reviews):
    """``comments`` with, among them, each native review of ``reviews`` (``pr_reviews``) that
    carries a ``review`` record (#325), as a comment would be: ``{id: None, review_id, native:
    True, author, body, created_at}``. A review goes before the first comment written at the
    same time or later, so that the records written after it follow it; a pending review, not
    published, is left out. Every other native review stays out of the conversation, as before.
    ``comments`` without a time (``created_at``) never move a review."""
    native = []
    for review in reviews:
        at = _timestamp(review.get('submitted_at'))
        body = review.get('body') or ''
        if (at is None or review.get('state') == 'PENDING'
                or read_marker(body, 'review') is None):
            continue
        native.append((at, {'id': None, 'review_id': review.get('id'), 'native': True,
                            'author': {'login': review.get('login')}, 'body': body,
                            'created_at': review['submitted_at']}))
    if not native:
        return list(comments)
    native.sort(key=lambda item: item[0])
    merged = []
    for comment in comments:
        at = _timestamp(comment.get('created_at'))
        while native and at is not None and native[0][0] <= at:
            merged.append(native.pop(0)[1])
        merged.append(comment)
    return merged + [review for _, review in native]


def rerun_waiting(record, details):
    """Whether the checks ``details`` of a head still fail with exactly the checks its re-run
    ``record`` replaces (#200): a re-run job is a new check run, with a new link."""
    failed = {c.get('link') for c in details if c.get('bucket') == 'fail'}
    return bool(failed) and failed <= set(record['checks'])


class Workers:
    """The actions running in continuous mode (#227): up to ``slots`` at once, each in a thread
    of its own. ``ended()`` tells whether an action ended since the latest ``clear()``. Safe
    across threads."""

    def __init__(self, slots):
        self.slots = slots
        self._pool = ThreadPoolExecutor(max_workers=slots, thread_name_prefix='devloop-action')
        self._lock = threading.Lock()
        self._running = {}  # key: (action, numbers it touches)
        self._ended = threading.Event()

    def running(self):
        """``(action, numbers)`` of the actions running now, in the order they started."""
        with self._lock:
            return list(self._running.values())

    def free(self):
        return self.slots - len(self.running())

    def full(self):
        return self.free() <= 0

    def ended(self):
        return self._ended.is_set()

    def clear(self):
        self._ended.clear()

    def start(self, action, numbers, job):
        """Run ``job()`` in a free slot; ``action`` and ``numbers`` count as running from now
        until it returns."""
        key = object()
        with self._lock:
            self._running[key] = (action, frozenset(numbers))

        def run():
            try:
                job()
            finally:
                # Under the lock: whoever sees the action gone from running() sees ended() too.
                with self._lock:
                    del self._running[key]
                    self._ended.set()
        self._pool.submit(run)

    def close(self):
        """Wait for the running actions and release the threads."""
        self._pool.shutdown(wait=True)


class TickLog:
    """The tick log, safe across threads: each message reaches ``sink`` whole, one at a time.
    While a thread runs inside ``prefixed(prefix)``, each line of its messages is written on its
    own, starting with ``prefix``, so that the lines of parallel actions never mix (#176).
    Outside it a message reaches ``sink`` unchanged."""

    def __init__(self, sink):
        self.sink = sink
        self._lock = threading.Lock()
        self._local = threading.local()

    @property
    def prefix(self):
        return getattr(self._local, 'prefix', '')

    @contextmanager
    def prefixed(self, prefix):
        previous = self.prefix
        self._local.prefix = prefix
        try:
            yield
        finally:
            self._local.prefix = previous

    def __call__(self, message):
        prefix = self.prefix
        with self._lock:
            if not prefix:
                self.sink(message)
                return
            for line in str(message).split('\n'):
                self.sink(f'{prefix} {line}')


def label_admitters(author, trusted_users, supervisor=None, devloop_logins=()):
    """The logins whose ``agent:ready`` and priority labels count on an issue opened by
    ``author`` (#178): the trusted users, and ``supervisor`` (the login of the supervisor's
    app, None without one) only when the author is a trusted user or one of
    ``devloop_logins``, the logins of devloop's apps."""
    admitters = set(trusted_users)
    if supervisor and author and (author in admitters or author in devloop_logins):
        admitters.add(supervisor)
    return admitters


def issue_ref(key):
    """``#N`` for an issue of the repository, ``owner/name#N`` for one of another (#277):
    ``key`` is ``(repo, number)``, ``repo`` empty for the repository's own issues."""
    repo, number = key
    return f'{repo}#{number}' if repo else f'#{number}'


def dependency_cycles(edges):
    """The issues on a dependency cycle (#277), as ``{issue: members}``: ``edges`` maps each
    issue to the open issues that block it, ``members`` are the issues of its cycle (those it
    reaches and that reach it), sorted. An issue that blocks itself is a cycle of one."""
    def reach(start):
        seen, todo = set(), list(edges.get(start, ()))
        while todo:
            node = todo.pop()
            if node not in seen:
                seen.add(node)
                todo.extend(edges.get(node, ()))
        return seen

    reached = {node: reach(node) for node in edges}
    return {node: sorted(m for m in reached[node] if node in reached.get(m, ()))
            for node in edges if node in reached[node]}


def pr_counts_line(snapshot, cfg):
    """The two counters, as the tick log and ``status`` show them."""
    working, held = pr_counts(snapshot, cfg)
    return (f'open PRs: {working}/{cfg.max_open_prs} in progress (max_open_prs), '
            f'{held}/{cfg.max_parked_prs} parked (max_parked_prs)')


def pr_status_line(pr, cfg, reviews=None):
    """A PR as ``status`` shows it (#289): its labels, the rounds counted against
    ``max_review_rounds`` (``rounds=3/3``, with the rounds that did not count, if any), CI and
    the merge state. ``reviews`` are the PR's review records when the snapshot did not read
    them (``Dispatcher.status_reviews``); by default ``pr.reviews``."""
    reviews = pr.reviews if reviews is None else reviews
    labels = ', '.join(sorted(l for l in pr.labels if l.startswith('agent:')))
    waive = waiver(cfg, pr.fixes, pr.closures)  # with the rounds after a /devloop fix (#536)
    waived = len(waived_rounds(reviews, **waive))
    granted = pr.granted_rounds
    rounds = (f'rounds={counted_rounds(reviews, **waive)}/{cfg.max_review_rounds + granted}'
              + (f' (+{granted} granted)' if granted else '')
              + (f' (+{waived} not counted)' if waived else ''))
    conflict = ' CONFLICT with ' + cfg.base_branch if pr.merge_state == 'conflict' else ''
    return (f'PR #{pr.number} (issue #{pr.issue}) [{labels}] {rounds} ci={pr.checks} '
            f'merge={pr.merge_state}{conflict}')


def round_limit_reason(lead, reviews, fixes, syncs, limit, granted=0, grantable=False,
                       trusted=(), cap=0, closures=(), language=ENGLISH):
    """The reason of a ``round_limit`` escalation (#289): ``lead``, then the rounds of
    ``reviews`` that count against ``limit`` (``max_review_rounds`` plus the ``granted`` rounds,
    #405), each with its reason, and those that do not, so that the operator sees where the
    limit comes from: with the ``trusted`` users, the ``cap`` and the finding ``closures`` of
    ``waiver``, also the reviews after a ``/devloop fix`` of a trusted user on an approved
    head, with who asked it (#536). ``grantable``: a ``round_grantable`` escalation, whose next
    round the supervisor may grant. ``language``: the project's, of the text (#635)."""
    t = TEXTS.texts(language)
    rounds = round_reasons(reviews, fixes, syncs, trusted, cap, closures, language)
    counted = [(n, why) for n, why, counts in rounds if counts]
    more = t('rounds_more', granted=granted) if granted else ''
    text = (lead.rstrip() + '\n\n'
            + t('rounds_counted', count=len(counted), limit=limit, more=more) + '\n\n'
            + ('\n'.join(f'- round {n}: {why}' for n, why in counted) or t('rounds_none')))
    free = [(n, why) for n, why, counts in rounds if not counts]
    if free:
        text += ('\n\n' + t('rounds_free') + '\n\n'
                 + '\n'.join(f'- round {n}: {why}' for n, why in free))
    if grantable:
        return text + '\n\n' + t('rounds_delegation')
    return text + '\n\n' + operator_rounds_text(language)


def control_command(body):
    """Whether a comment is a devloop command: its first line starts with ``/devloop``."""
    lines = (body or '').strip().splitlines()
    return bool(lines) and lines[0].split()[:1] == ['/devloop']


def preflight_hash(title, body, comments, trusted_users, automatic, accepted=None,
                   decisions=()):
    """The hash that binds a readiness check to the issue text: title, body and the comments of
    trusted users, without devloop's own records (``automatic(comment)``) and without
    ``/devloop`` commands (a supervisor command after the attribution header too, #182). Any
    other trusted comment counts, header or not, and so does a dispatcher record of defaults
    the supervisor accepted (``accepted(comment)``, #182), as a trusted comment would.

    ``decisions``: the comments that record a decision of the operator (``issue_decisions``,
    #472), each by its author, its time and its text: a ``/devloop approve`` (#483) is a command,
    so its text is not among the others. Their validity depends on the history of the text,
    not only on the text: a change of the title or body, undone later, leaves the text as it was
    but drops them, and with them the hash, so the check that relied on them runs again. Without
    such comments the hash is the one of before #472."""
    accepted = accepted or (lambda comment: False)
    kept = [[(c.get('author') or {}).get('login'), c.get('body') or ''] for c in comments
            if accepted(c) or ((c.get('author') or {}).get('login') in trusted_users
                               and not automatic(c) and not control_command(c.get('body'))
                               and supervisor_command(c.get('body')) is None)]
    request = [title or '', body or '', kept]
    if decisions:
        request.append([[c['author']['login'], c['created_at'], c.get('body') or '']
                        for c in decisions])
    data = json.dumps(request, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def base_revocation(record, head):
    """Whether the policy record ``record`` revoked the approval of ``head`` because the base
    branch advanced (#252): a ``stale`` event on that head whose base checks (``bases``, #160)
    show a base that touched the PR, conflicted with it, or could not be verified. A revocation
    for anything else (level, reviewers, policy, a missing approval) writes no such check, or
    only ``disjoint`` ones."""
    if not isinstance(record, dict) or record.get('event') != 'stale':
        return False
    receipt, bases = record.get('receipt'), record.get('bases')
    return (isinstance(receipt, dict) and receipt.get('head') == head
            and isinstance(bases, list) and bool(bases)
            and all(isinstance(c, dict) for c in bases)
            and any(c.get('outcome') != 'disjoint' for c in bases))


def untrusted_change(history, ready_label, trusted_users, admitters=None, language=ENGLISH):
    """Why the issue's title or body can no longer be trusted, in ``language`` (#635), or
    None.

    Trust comes from one of ``admitters`` (``label_admitters``, the trusted users by default)
    adding ``ready_label`` in its latest ``labeled`` event (``ready_approval``); a later change
    to the title or the body by anyone outside ``trusted_users`` voids it until the label is
    added again. GitHub timestamps have one-second granularity, so an untrusted change in the
    same second as the label cannot be proven to precede it and voids trust too.
    """
    t = TEXTS.texts(language)
    admitters = trusted_users if admitters is None else admitters
    approved = ready_approval(history['labeled'], ready_label, admitters)
    if not approved:
        return t('untrusted_no_label', label=ready_label)
    since = approved[1]
    edited = history['last_edited_at']
    if edited and edited >= since and history['editor'] not in trusted_users:
        return t('untrusted_text', who=history['editor'], label=ready_label)
    for who, at in history['renamed']:
        if at >= since and who not in trusted_users:
            return t('untrusted_title', who=who, label=ready_label)
    return None


class Dispatcher(SupervisionMixin, PreflightMixin, MergeMixin, ActionsMixin, ConclaveMixin,
                 AdvisoryMixin, BaseMixin):
    """``gh`` acts as the dispatcher: it implements the ports of ``ports.Backend`` (tracker,
    code review and CI), and the dispatcher uses nothing else of it. ``identities``
    (``apps.Identities``) gives each agent its own GitHub identity; without it every actor uses
    ``gh``. ``activity`` (``activity.Activity``) records the running action for ``devloop
    status``. ``ledger`` (``ledger.Ledger``) records evidence and telemetry of each step (#133)."""

    def __init__(self, cfg, gh: Backend, ws, runner, log=print, identities=None, activity=None,
                 ledger=None, merger=None, pinner=None, memory=None, clock=time.time,
                 advisory_results=None, advisory_failures=None, housekeeping=None,
                 main_alerts=None):
        self.cfg = cfg
        # The commit of the base branch whose red CI was last alerted (#459, ``MainAlerts``):
        # in ``state_dir`` from ``devloop``, in memory without it; and the one this process
        # last logged.
        self.main_alerts = main_alerts if main_alerts is not None else MainAlerts()
        self._main_logged = None
        # The cleanup of state_dir (#380, ``housekeeping.Housekeeping``), or None: run at a
        # tick with nothing running (``_housekeeping``).
        self.housekeeping = housekeeping
        # Wall-clock seconds, and since when each head waits for its CI (#263): the time of
        # the dispatcher's own push, else its commit date, read once.
        self.clock = clock
        self._ci_since = {}
        # The pin after a merge (#174, ``pin.AutoPin``), used only with ``[pin] auto = true``.
        self.pinner = pinner
        self.gh = gh
        # Merges run with the merger's app when there is one (#201, ``Identities.merger``),
        # otherwise with the local ``gh`` login devloop runs with (#172): no other app, the
        # dispatcher's included, gets ``contents=write``.
        self.merger = merger if merger is not None else gh
        self.ws = ws
        self.runner = runner
        # Shared by the threads of parallel actions (#176): one line at a time.
        self.log = log if isinstance(log, TickLog) else TickLog(log)
        self.identities = identities
        self.activity = activity
        self.ledger = ledger  # bound to the dispatcher's signature (``ledger`` below)
        # The issue of each devloop PR seen (#362, ``_work_issue``): its branch, checked against
        # the dispatcher's marker (``_devloop_pr``).
        self._issue_of = {}
        # The ``attempt`` of the marker of each devloop PR seen (#384, ``_turn_attempt``), None
        # for a PR whose marker has none.
        self._pr_attempts = {}
        # The models found unavailable (``unavailability.Memory``, #151), or None: every turn
        # starts from the configured model.
        self.memory = memory
        # The advisory turns run and not yet published (#369, ``AdvisoryResults``): in
        # ``state_dir`` from ``devloop``, in memory without it.
        self.advisory_results = (advisory_results if advisory_results is not None
                                 else AdvisoryResults())
        # The failed advisory actions of each head (#381, ``AdvisoryFailures``): in
        # ``state_dir`` from ``devloop``, in memory without it.
        self.advisory_failures = (advisory_failures if advisory_failures is not None
                                  else AdvisoryFailures())
        # Continuous mode (#227): set by ``devloop run`` with parallel actions, a tick starts
        # its actions in these ``Workers`` and does not wait for them.
        self.workers = None
        # Continuous mode (#301): ``(sha12, since)`` of the new pinned copy whose restart waits
        # for the running actions, or None.
        self._restart = None
        # Whether a tick of this process already checked the rename of agent:needs-operator
        # (#248, ``_transition_labels``).
        self.labels_checked = False
        # Whether this process already warned of the size of the journal (#397,
        # ``_watch_journal``), and the dropped publications it already logged
        # (``_signal_dropped``).
        self._journal_warned = False
        self._drops_logged = set()

    @property
    def ledger(self):
        return self._ledger

    @ledger.setter
    def ledger(self, ledger):
        """The ledger edits only the round registers the dispatcher signed (#325), and writes
        in the project's ``language`` (#631)."""
        if ledger is not None:
            ledger.signed = self._signed
            ledger.language = self.cfg.language
        self._ledger = ledger

    # Identities ------------------------------------------------------------
    # An agent key acts with the app it declares (``Config.app_of``, ADR 0005, P5-3, #407): an
    # implementer with the role app ``implementer``, a reviewer with its agent's app. The key of
    # a historical record no longer configured, such as ``claude``, is looked up as an app.
    def _as(self, actor, permissions) -> Backend:
        """The GitHub client ``actor`` acts with, holding only ``permissions``."""
        return (self.identities.github(self.cfg.app_of(actor), permissions) if self.identities
                else self.gh)

    def _git_auth(self, actor, permissions):
        return (self.identities.git_auth(self.cfg.app_of(actor), permissions) if self.identities
                else None)

    def _app_login(self, actor):
        return self.identities.login(self.cfg.app_of(actor)) if self.identities else None

    def _wrote_as_agent(self, login, key):
        """Whether ``login`` may write the record of the configured agent ``key`` as an agent
        of its own (a review, a readiness check, a conclave's): never for a key that signs with
        a role app (#407), whose app is shared by every implementer and signs only their
        work."""
        return (key in self.cfg.agents and not self.cfg.signs_for_role(key)
                and self._wrote(login, key))

    def _wrote(self, login, actor):
        """Whether ``login`` may write records for ``actor``: the actor's app if it has one,
        otherwise a trusted user."""
        expected = self._app_login(actor)
        return login == expected if expected else login in self.cfg.trusted_users

    # Snapshot ------------------------------------------------------------
    def _markers(self, pr, comments, kind):
        """Records of ``kind`` from the identities allowed to write them, at most one per
        comment: fixes from the PR's implementer, CI verdicts and policy records from the
        dispatcher, reviews from the configured agent named in the record, never the
        implementer nor the merger."""
        return [record for _, record in self._marker_entries(pr, comments, kind)]

    def _marker_entries(self, pr, comments, kind, problems=None):
        """``(position in comments, record)`` for each record ``_markers`` accepts.

        ``comments`` may hold native reviews (``_pr_thread``): one counts only as the
        ``review`` record of an agent with an app of its own, written with that app (#325).
        A review counts only if each finding key it carries is ``round/owner/ID`` of the review
        itself. ``problems`` collects reviews that an identity allowed to write records wrote
        but whose findings have no reconstructible owner: a review without a reviewer, or with
        keys of another round or owner."""
        implementer = pr.meta.get('implementer')
        found = []
        for position, comment in enumerate(comments):
            record = read_marker(comment.get('body', ''), kind)
            if record is None:
                continue
            login = (comment.get('author') or {}).get('login')
            if kind in ('fix', 'sync'):
                actor = implementer
            elif kind in ('policy', 'finding', 'rerun'):
                actor = DISPATCHER
            elif record['source'] == 'ci':
                actor = DISPATCHER
            elif not isinstance(record.get('reviewer'), str) or not record['reviewer']:
                if problems is not None and self._writes_records(login):
                    problems.append(TEXTS.texts(self.cfg.language)(
                        'review_no_reviewer', round=record['round']))
                continue
            elif (record['reviewer'] in (implementer, CI) or record['reviewer'] not in self.cfg.agents
                  or self.cfg.signs_for_role(record['reviewer'])):
                # Only a configured agent reviews: never the implementer, nor one of devloop's
                # own apps, such as the merger, whose app only merges (#201), nor an agent that
                # signs with the implementer's role app (#407).
                continue
            else:
                actor = record['reviewer']
            if not (actor and self._wrote(login, actor)):
                continue
            if comment.get('native') and not (kind == 'review' and record['source'] == 'agent'
                                              and self._app_login(actor)):
                # A native review is a record only for an agent with an app of its own, written
                # with that app (#325); the other agents keep the review comment.
                continue
            if kind == 'review':
                owner = owner_of(record)
                wrong = [f['key'] for f in record['blocking_findings']
                         if 'key' in f and f['key'] != finding_key(record['round'], owner, f['id'])]
                if wrong:
                    if problems is not None:
                        problems.append(TEXTS.texts(self.cfg.language)(
                            'review_other_owners', round=record['round'], owner=owner,
                            keys=', '.join(f'`{k}`' for k in wrong)))
                    continue
            found.append((position, record))
        return found

    def _pr_thread(self, number):
        """The conversation of PR ``number`` as the review records need it (#325): its comments
        and, among them in the order of their times, the native reviews that carry a ``review``
        record (``with_native_reviews``). Who may have written each record is checked by
        ``_marker_entries``, as for a comment."""
        return with_native_reviews(self.gh.pr_comments(number), self.gh.pr_reviews(number))

    def _writes_records(self, login):
        """Whether ``login`` may write some devloop record: an agent's, or the dispatcher's."""
        return any(self._wrote(login, actor) for actor in (*self.cfg.agents, DISPATCHER))

    def _finding_commands(self, pr, comments, state):
        """Closures asked with ``FINDING_COMMAND`` lines and not recorded yet, one per open
        finding, from comments trusted users wrote in person. A command from anyone else,
        malformed, or naming a finding that never existed is ignored and logged; one naming a
        finding already closed has nothing left to do. The coordinator's lines are none of this
        reader's (#606): its closures are commands of an ``open_findings`` escalation."""
        open_keys = {f['key'] for f in state.open}
        pending = {}
        for comment in comments:
            body = comment.get('body') or ''
            if body.lstrip().startswith('> [!NOTE]'):
                continue  # text devloop or its agents published: never a command
            login = (comment.get('author') or {}).get('login')
            if self._is_coordinator(login):
                # The coordinator's closures are commands of an ``open_findings`` escalation
                # (#606, ``_coordinator_findings``), recorded there after their checks.
                continue
            for line, parsed in finding_commands(body):
                why = ('the author is not a trusted user' if login not in self.cfg.trusted_users
                       else 'malformed, expected `/devloop finding <round/reviewer/ID> '
                            'resolved|withdrawn <reason>`' if parsed is None
                       else 'no such finding' if parsed[0] not in state.known else None)
                if why:
                    self.log(f'  PR #{pr.number}: ignored `{line[:120]}` by {login}: {why}')
                    continue
                key, status, reason = parsed
                if key in open_keys and key not in pending:
                    pending[key] = {'key': key, 'status': status, 'by': OPERATOR,
                                    'author': login, 'reason': reason, 'head_sha': pr.head_sha}
        return list(pending.values())

    def _supervisor_login(self):
        """The login of the supervisor's identity, None without one. With
        ``[roles.supervisor]`` as a table (ADR 0005, P5-4, #408), the role app of
        ``[apps.supervisor]``, known by its slug: devloop reads no key of it. With the key of an
        agent, that agent's app, if it has one."""
        cfg = self.cfg
        if cfg.supervisor_actor is not None:
            return app_login(cfg.apps[SUPERVISOR].slug)
        return self._app_login(cfg.supervisor) if cfg.supervisor else None

    def _devloop_logins(self):
        """The logins of devloop's apps whose issues count as devloop's (#178, #182): the
        agents', the dispatcher's, the supervisor's identity and, with ``[roles.coordinator]``,
        the coordinator's (#595). A coordinator's issue that the readiness check declares
        touching the trust policy still needs a trusted user's ``agent:ready``
        (``_coordinator_gate``)."""
        return ({self._app_login(actor) for actor in (*self.cfg.agents, DISPATCHER)}
                | {self._supervisor_login(), self._coordinator_login()}) - {None}

    def _may_hold(self, login):
        """Whether ``login`` may hold or release a merge (#172), or ask for a fix (#195): a
        trusted user, or the supervisor's identity (``_supervisor_login``)."""
        supervisor = self._supervisor_login()
        return login in self.cfg.trusted_users or bool(supervisor and login == supervisor)

    def _coordinator_login(self):
        """The login of the coordinator's identity (#528), the role app of
        ``[apps.coordinator]`` known by its slug, or None without ``[roles.coordinator]`` (off
        by default): devloop reads no key of it."""
        cfg = self.cfg
        if cfg.coordinator is None:
            return None
        return app_login(cfg.apps[COORDINATOR].slug)

    def _is_coordinator(self, login):
        """Whether ``login`` is the coordinator's identity (``_coordinator_login``)."""
        coordinator = self._coordinator_login()
        return bool(coordinator) and login == coordinator

    def _coordinator_may(self, login, command):
        """Whether ``login`` is the coordinator's identity and ``command`` one of the commands
        delegated to it (``COORDINATOR_DELEGATED``, #528), which then counts as a trusted
        user's."""
        return self._is_coordinator(login) and command in COORDINATOR_DELEGATED

    def _admitters(self, author, coordinator=False):
        """The logins whose labels count on an issue opened by ``author`` (#178,
        ``label_admitters``): the supervisor's identity counts only when it has one, and only
        on issues of trusted users or of devloop's apps. ``coordinator``: the logins whose
        ``agent:ready`` and priority labels count, also the coordinator's identity on the same
        issues (#528: the resume with the labels; #595: its priorities); its dependency and
        implementer labels never count.

        On an issue the coordinator opened (#595) the labels of the supervisor and of the
        coordinator count only with a readiness check, which says whether it touches the trust
        policy (``_coordinator_gate``): without one only the trusted users' do (it fails
        closed)."""
        coordinator_login = self._coordinator_login()
        if self._is_coordinator(author) and not self.cfg.preflight:
            return set(self.cfg.trusted_users)
        supervisor = self._supervisor_login()
        admitters = (label_admitters(author, self.cfg.trusted_users, supervisor,
                                     self._devloop_logins())
                     if supervisor else set(self.cfg.trusted_users))
        if coordinator and coordinator_login:
            admitters |= label_admitters(author, self.cfg.trusted_users, coordinator_login,
                                         self._devloop_logins())
        return admitters

    # Authorization by the coordinator (#607) ----------------------------------
    def _needs_authorization(self, login):
        """Whether an ``agent:ready`` added by ``login`` admits an issue only with a valid
        authorization of the coordinator (#607): the supervisor's identity, with
        ``[roles.coordinator]``. A trusted user's and the coordinator's never need one."""
        supervisor = self._supervisor_login()
        return (bool(supervisor) and login == supervisor and self._coordinator_login() is not None
                and login not in self.cfg.trusted_users)

    def _authorize_records(self, comments):
        """The dispatcher's ``authorize`` records of an issue's ``comments``, oldest first."""
        return [record for _, record in self._dispatcher_records(comments, 'authorize')]

    def _pending_authorizations(self, comments):
        """The comments of ``comments`` with a ``/devloop authorize`` (#607) that no record of
        the dispatcher answers yet, oldest first, from any author: each gets one record,
        ``authorized`` or ``refused``. devloop's own records are never commands."""
        recorded = {record['comment'] for record in self._authorize_records(comments)}
        return [c for c in comments
                if isinstance(c.get('id'), int) and not isinstance(c.get('id'), bool)
                and c['id'] not in recorded and not carries_record(c.get('body') or '')
                and authorize_arguments(c.get('body')) is not None]

    def _authorization(self, history, comments):
        """The coordinator's authorization that holds for the issue's current title and body
        (``authorization.valid_authorization``), or None."""
        return valid_authorization(self._authorize_records(comments), history)

    def _ready_admitters(self, history, comments):
        """The logins whose ``agent:ready`` admits the issue of ``history`` now: those of
        ``_admitters`` with ``coordinator``, without the supervisor's identity when its label
        needs an authorization of the coordinator (``_needs_authorization``) and none holds for
        the current text (#607)."""
        admitters = self._admitters(history['author'], coordinator=True)
        supervisor = self._supervisor_login()
        if (supervisor in admitters and self._needs_authorization(supervisor)
                and self._authorization(history, comments) is None):
            admitters.discard(supervisor)
        return admitters

    def _do_authorize(self, number):
        """Record, once each and oldest first, the ``/devloop authorize`` of issue ``number``
        that no record answers yet (#607), then remove ``agent:needs-coordinator`` when an
        authorization holds for the current text.

        A command authorizes only from the coordinator's identity (``COORDINATOR_DELEGATED``),
        with well-formed arguments, on an issue of a trusted user or of one of devloop's apps,
        and only when neither the title nor the body changed at or after it
        (``changed_since``): the record binds the SHA-256 of the text the coordinator read
        (``text_hash``). Any other is refused, with why. Comments and history are read again
        here: what was recorded since the snapshot is left alone."""
        lb = self.cfg.labels
        comments = self.gh.issue_comments(number)
        pending = self._pending_authorizations(comments)
        if not pending:
            self.log(f'  the {AUTHORIZE_COMMAND} were recorded since the snapshot')
            return
        history = self.gh.issue_history(number)
        written = []
        for comment in pending:
            login = (comment.get('author') or {}).get('login') or ''
            at = comment.get('created_at') if isinstance(comment.get('created_at'), str) else ''
            priority, problem = authorize_arguments(comment.get('body'), self.cfg.language)
            t = TEXTS.texts(self.cfg.language)
            reason = (
                t('authorize_not_coordinator') if not self._coordinator_may(login, AUTHORIZE)
                else problem if problem
                else t('authorize_untrusted') if not self._devloop_author(history.get('author'))
                else t('authorize_changed', command=AUTHORIZE_COMMAND)
                if changed_since(history, at)
                else '')
            record = {'comment': comment['id'], 'by': login, 'at': at, 'priority': priority}
            if reason:
                record.update(outcome='refused', reason=reason)
                self.log(f'  issue #{number}: refused {AUTHORIZE_COMMAND} by {login}: {reason}')
            else:
                record.update(outcome='authorized',
                              hash=text_hash(history['title'], history['body']))
                self.log(f'  issue #{number}: authorized by {login}'
                         + (f', priority {priority}' if priority else ''))
            self.gh.comment(number, render_authorize(record, lb.ready, self.cfg.language))
            written.append(record)
        labels = label_names(self.gh.issue(number).get('labels') or [])
        if (lb.needs_coordinator in labels
                and valid_authorization(self._authorize_records(comments) + written,
                                        history) is not None):
            self.gh.edit_labels(number, remove=[lb.needs_coordinator])
            self.log(f'  issue #{number}: {lb.needs_coordinator} removed')

    def _unauthorized_ready(self, number):
        """Why the latest ``agent:ready`` of issue ``number``, read now, does not admit it for
        lack of an authorization of the coordinator (#607), or ''. The latest ``labeled`` event
        decides even when devloop removed the label since, at the takeover: an implementation
        that waited for the base branch (#459) starts again only while one holds too."""
        if self._coordinator_login() is None:
            return ''
        history = self.gh.issue_history(number)
        approved = ready_approval(history['labeled'], self.cfg.labels.ready,
                                  self._admitters(history['author'], coordinator=True))
        if approved is None or not self._needs_authorization(approved[0]):
            return ''
        if self._authorization(history, self.gh.issue_comments(number)) is not None:
            return ''
        return (f'{self.cfg.labels.ready} by {approved[0]} without an authorization of the '
                'coordinator for the current text')

    def _stop_unauthorized(self, number, why):
        """Stop the start of issue ``number`` that no authorization of the coordinator admits
        (``why``, from ``_unauthorized_ready``, #607). A ready issue only does not start: the
        snapshot no longer admits it. One that holds ``agent:working``, an implementation that
        waited for the base branch (#459), leaves it for ``agent:needs-coordinator``: the
        coordinator authorizes its current text again, then the supervisor queues it again."""
        lb = self.cfg.labels
        if lb.working not in label_names(self.gh.issue(number).get('labels') or []):
            self.log(f'  {why}; it does not start')
            return
        self.gh.edit_labels(number, add=[lb.needs_coordinator], remove=[lb.working])
        self.log(f'  issue #{number}: {why}; it does not start again: {lb.working} replaced '
                 f'by {lb.needs_coordinator}')

    def _do_unauthorized(self, number):
        """The restart of issue ``number``, whose implementation waited for the base branch
        (#459), that no authorization of the coordinator admits (``planning.base_restart``,
        #607): ``_stop_unauthorized``, unless one holds again now."""
        why = self._unauthorized_ready(number)
        if not why:
            self.log('  an authorization of the coordinator holds now; next tick')
            return
        self._stop_unauthorized(number, why)

    def _issue_decisions(self, comments, history, taken=False):
        """``issue_decisions`` of an issue (#472): neither the supervisor's identity nor any
        other app decides for the operator."""
        return issue_decisions(comments, history, self.cfg.trusted_users, self._devloop_logins(),
                               self.cfg.labels.working, taken=taken)

    def _refused_approvals(self, comments):
        """``refused_approvals`` of an issue (#483), with the apps of devloop and the
        supervisor's identity, its reasons in the project's language (#632)."""
        return refused_approvals(comments, self.cfg.trusted_users, self._devloop_logins(),
                                 language=self.cfg.language)

    def _untrusted_change(self, history):
        """``untrusted_change`` of an issue, with the admitters of its author's
        ``agent:ready``."""
        return untrusted_change(history, self.cfg.labels.ready, self.cfg.trusted_users,
                                self._admitters(history['author'], coordinator=True),
                                self.cfg.language)

    # Supervision (#182) --------------------------------------------------------
    def _accepted(self, comment):
        """Whether ``comment`` is the dispatcher's record of defaults the supervisor accepted,
        or of its decision on the gaps of a readiness check (#258): it counts as a trusted
        comment of the issue. The supervisor's own comment never does."""
        record = read_marker(comment.get('body', ''), 'supervision')
        return (record is not None
                and (record['command'], record['outcome']) in ((ACCEPT_DEFAULTS, 'accepted'),
                                                              (DECIDE, 'decided'))
                and self._signed((comment.get('author') or {}).get('login')))

    def _request_hash(self, history, comments):
        """The hash of an issue's request (``preflight_hash``): what binds a readiness check,
        and the key of an issue step's retries. With the comments that may record a decision of
        the operator as the readiness check reads them (#472): a check whose decision no longer
        holds is not reused."""
        return preflight_hash(history['title'], history['body'], comments,
                              self.cfg.trusted_users, self._automatic, self._accepted,
                              decisions=self._issue_decisions(comments, history))

    def _devloop_author(self, login):
        """Whether an issue opened by ``login`` may have its escalations decided by the
        supervisor: a trusted user or one of devloop's apps."""
        return bool(login) and (login in self.cfg.trusted_users or login in self._devloop_logins())

    def _dispatcher_records(self, comments, kind, event=None):
        """``(position, record)`` of the records of ``kind`` the dispatcher signed."""
        found = []
        for position, comment in enumerate(comments):
            record = read_marker(comment.get('body', ''), kind)
            if (record is not None and (event is None or record.get('event') == event)
                    and self._signed((comment.get('author') or {}).get('login'))):
                found.append((position, record))
        return found

    def _operator_labelers(self, number):
        """Logins of every ``labeled`` event of ``agent:needs-operator`` on issue or PR
        ``number``, oldest first, under both its names (#248): the events written before the
        rename keep the old name, and come before those of the new one."""
        lb = self.cfg.labels
        return (self.gh.labelers(number, LEGACY_NEEDS_OPERATOR)
                + self.gh.labelers(number, lb.needs_operator))

    def _operator_merge_owned(self, pr, current, notices, policy, promoted):
        """Whether ``agent:needs-operator`` on the ready ``pr`` is the dispatcher's own label of
        an operator merge (#248). ``current``: ``(position, record)`` of the latest
        ``operator_merge`` notice of the current promotion, which names the head made ready
        (the PR's head may have moved since); it counts from its own position or from the
        latest promotion (``promoted``, a position), whichever is later, since a head promoted
        again edits its notice instead of writing a new one. From there no other notice of the
        dispatcher (``notices``) and no escalation recorded in a policy record (``policy``) may
        follow, the label has no ``labeled`` event but those the notice counted and one more,
        and the dispatcher wrote that one. The label under its name before #248 is never the
        dispatcher's. Anything else, or anything uncertain, leaves the PR to the operator."""
        lb = self.cfg.labels
        if (lb.needs_operator not in pr.labels or LEGACY_NEEDS_OPERATOR in pr.labels
                or lb.ready_to_merge not in pr.labels or current is None):
            return False
        record = current[1]
        position = max(current[0], promoted)
        if any(later > position for later, _ in notices):
            return False
        # The escalations of the review level and of level 0 are policy records (#182).
        if any(later > position and entry.get('class') for later, entry in policy):
            return False
        labelers = self._operator_labelers(pr.number)
        return (len(labelers) == record['labeled'] + 1
                and self._wrote(labelers[-1], DISPATCHER))

    def _released(self, pr):
        """The dispatcher's own label of an operator merge on ``pr`` (#248), which goes with
        ``agent:ready-to-merge`` when the readiness decays without an escalation; none
        otherwise."""
        return [self.cfg.labels.needs_operator] if pr.operator_owned else []

    def _resync(self, pr):
        """``PullRequest.resync`` (#235): when the latest round is a complete agent round that
        approved an earlier head, and the PR's head differs from it only by merges of the base
        branch that git concludes without conflicts, ``{reason, round, from}``; otherwise None.
        When some of those merges are syncs whose conflicts the implementer resolved, as their
        ``sync`` markers record (#458), the reason is ``RESYNC_CONFLICTS``: the review runs
        also beyond the limit and counts only if it does not approve. A merge someone else
        resolved, a fix or any other commit gives None.

        When that round approved the PR's head itself (#252), the reason is ``BASE_CHANGED`` if
        the latest policy record after it, which only the dispatcher writes, revoked the
        approval of this head because the base branch touched the PR (``base_revocation``)."""
        latest = current_round(pr.reviews)
        if (not latest or latest[0]['source'] != 'agent' or pending_reviewers(latest)
                or round_verdict(latest) != 'approve'):
            return None
        approved = latest[0]['head_sha']
        if any(r['head_sha'] != approved for r in latest):
            return None
        if approved == pr.head_sha:
            if not base_revocation(pr.revoked, approved):
                return None
            return {'reason': BASE_CHANGED, 'round': latest[0]['round'], 'from': approved}
        try:
            merges = self.ws.resync_merges(pr.head_ref, pr.head_sha, approved)
        except WorkspaceError as exc:
            self.log(f'  resync of #{pr.number} not verifiable: {exc}')
            return None
        if merges is None:
            return None
        if not merges:
            return {'reason': RESYNC_AFTER_APPROVAL, 'round': latest[0]['round'],
                    'from': approved}
        # Merges whose conflicts someone resolved (#458): each must be the head of a sync the
        # implementer recorded, or the content may have changed in other ways.
        synced = {s.get('head_sha') for s in pr.syncs}
        if not all(commit in synced for commit in merges):
            return None
        return {'reason': RESYNC_CONFLICTS, 'round': latest[0]['round'], 'from': approved}

    def _merge_mode(self, pr):
        """The merge mode of ``pr`` now (#172): ``[merge]`` with the override of its review
        level, ``off`` when its diff touches an operator-only path. None when it cannot be
        computed."""
        try:
            receipt, paths = self._receipt_and_paths(pr)
        except (GitHubError, WorkspaceError) as exc:
            self.log(f'  merge mode of #{pr.number} not computable: {exc}')
            return None
        if receipt['head'] != pr.head_sha:
            return None
        return self.cfg.merge.mode_for(receipt['level'], paths)

    def _merged_prs(self, number):
        """The merged devloop PRs of issue ``number`` (#228), newest first, as ``{number,
        merge_commit}``: those from its branch that ``_devloop_pr`` recognises, whether or not
        their body still ends with the marker. Who merged them does not matter. With
        ``attempt_identity`` (#384) also those of the branch of each later attempt of the issue
        that the journal holds (``attempt_branch``)."""
        found = []
        for branch in self._issue_branches(number):
            for raw in self.gh.merged_prs(branch):
                pr = self._devloop_pr(dict(raw, headRefOid=raw.get('headRefOid') or ''))
                if pr is not None and pr.issue == number:
                    found.append({'number': pr.number,
                                  'merge_commit': (raw.get('mergeCommit') or {}).get('oid')})
        return sorted(found, key=lambda p: -p['number'])

    def _issue_branches(self, number):
        """The devloop branches of issue ``number``: that of today and, with
        ``attempt_identity`` (#384), that of each later attempt the journal holds."""
        prefix = self.cfg.branch_prefix
        branches = [attempt_branch(prefix, number, 1)]
        if self.cfg.attempt_identity and self.ledger is not None:
            branches += [attempt_branch(prefix, number, n) for n in sorted(
                {a['n'] for a in self.ledger.journal.attempts().values()
                 if a['repo'] == self.cfg.repo and a['issue'] == number and a['n'] > 1})]
        return branches

    def key_problems(self):
        """Why devloop may not start with the agent keys it has (ADR 0005, P5-3, #407), one line
        per problem, or none: an open devloop PR whose marker names as its implementer or its
        reviewer a key that is not configured, or one that names another role
        (``config.key_role``). Its fixes and syncs run with the agent its marker names, so the
        keys renamed by role change only with an empty queue (D10). Only the PRs that may be
        devloop's count (``_devloop_pr``: of this repository, by a trusted user or one of
        devloop's apps, on a devloop branch): nobody else's PR can stop devloop."""
        cfg, problems = self.cfg, []
        agents = self.identities.agent_logins() if self.identities else set()
        for raw in self.gh.open_prs():
            author = (raw.get('author') or {}).get('login')
            if raw.get('isCrossRepository') or (author not in cfg.trusted_users
                                                and author not in agents):
                continue
            if parse_branch(cfg.branch_prefix, raw.get('headRefName') or '') is None:
                continue
            meta = read_marker(raw.get('body') or '', 'pr')
            if meta is None:
                continue
            for field, role in (('implementer', 'implementer'), ('reviewer', 'reviewer')):
                if field not in meta:
                    continue
                key = meta[field]
                if not isinstance(key, str) or key not in cfg.agents:
                    problems.append(f'PR #{raw["number"]}: its marker names the {field} {key!r}, '
                                    'which is not configured')
                elif key_role(key) not in (None, role):
                    problems.append(f'PR #{raw["number"]}: its marker names the {field} {key!r}, '
                                    f'a {key_role(key)} key')
        return problems

    def _devloop_pr(self, raw):
        """A devloop PR is recognised by its branch, ``<branch_prefix><issue>``, so that it stays
        in the flow (and its branch is never overwritten) even if its marker is lost. ``meta`` is
        empty when the body no longer ends with a marker for that issue, or when the PR was not
        opened by the implementer the marker names."""
        author = (raw.get('author') or {}).get('login')
        agents = self.identities.agent_logins() if self.identities else set()
        if raw.get('isCrossRepository') or (author not in self.cfg.trusted_users
                                            and author not in agents):
            return None
        ref = raw['headRefName']
        parsed = parse_branch(self.cfg.branch_prefix, ref)
        if parsed is None:
            return None
        issue, n = parsed
        meta = read_marker(raw.get('body', ''), 'pr')
        if meta is not None and (meta['issue'] != issue
                                 or not self._wrote(author, meta['implementer'])):
            meta = None
        if meta is not None and ('attempt' in meta or n is not None) and (
                meta.get('attempt') or {}).get('n') != (n or 1):
            # One branch per attempt (#384, ``attempt_branch``): the branch of today holds the
            # first, that of a later attempt only the PR of that attempt. A marker of another
            # number, or without the attempt on a later attempt's branch, does not hold.
            meta = None
        return PullRequest(number=raw['number'], issue=issue,
                           head_ref=ref, head_sha=raw['headRefOid'],
                           title=raw['title'], body=raw.get('body', ''),
                           labels=label_names(raw['labels']), meta=meta or {},
                           merge_state=self.gh.merge_state(raw),
                           mergeable=raw.get('mergeable'), merge_status=raw.get('mergeStateStatus'))

    def status_reviews(self, pr):
        """The review records of ``pr`` for ``status`` (#289). The snapshot reads the thread
        only of the PRs devloop works on; a PR escalated to agent:needs-operator (as after
        ``round_limit``) or agent:needs-supervisor keeps its rounds in the thread, read here
        without changing what the snapshot plans on, with its fixes (``pr.fixes``)."""
        lb = self.cfg.labels
        if not pr.meta or pr.labels & {lb.review, lb.changes, lb.ready_to_merge}:
            return pr.reviews
        comments = self._pr_thread(pr.number)
        pr.granted_rounds = self._granted_rounds(comments)  # its limit, for the line (#405)
        pr.operator_rounds = self._granted_rounds(comments, operator=True)
        # Its fixes, with the reviews before each request, and finding closures, for the
        # rounds after a /devloop fix that do not count (#536).
        entries = self._marker_entries(pr, comments, 'review')
        pr.fixes = self._fixes(pr, comments, entries)
        pr.closures = self._closures(pr, comments)
        return [record for _, record in entries]

    def snapshot(self):
        lb = self.cfg.labels
        prs = []
        # The base branch, fetched at most once per snapshot, for the failed checks that depend
        # on it (#270) and the steps that wait for it to advance (#459).
        base_head = functools.cache(lambda: self.ws.base_head())
        # The CI of the base commit of a wait that holds, read at most once per commit: once
        # green, the step escalates host_tests instead of waiting further (#497).
        base_ci = functools.cache(self._base_ci_green)
        for raw in self.gh.open_prs():
            pr = self._devloop_pr(raw)
            if pr is None:
                continue
            if pr.meta and pr.labels & {lb.review, lb.changes, lb.ready_to_merge}:
                thread = self._pr_thread(pr.number)
                self._read_thread(pr, thread)
                # A fix whose failed tests fail on the base too waits for it to advance (#459).
                wait = self._base_wait(thread, 'fix', pr.head_sha)
                if wait is not None and self._base_holds(wait, base_head):
                    pr.base_broken = dict(wait, ci=base_ci(wait['base']))
                    self.log(f'  PR #{pr.number}: the fix waits for {self.cfg.base_branch} to '
                             f'move past {wait["base"][:12]}, where its failed tests fail too'
                             + (', but its CI passed: host_tests' if pr.base_broken['ci']
                                else ''))
                # A veto to apply, or a review block restarted by a conclave (#142).
                self._conclave_pr_state(pr, thread)
            # In agent:changes CI matters to close an open CI finding, and after a review that
            # asked for changes before the CI of the head was known: the fix waits for it, so
            # that a failure joins the same fix (#271, ``plan``).
            ci_open = lb.changes in pr.labels and any(
                f['owner'] == CI for f in open_findings(pr.reviews, pr.closures))
            blind = lb.changes in pr.labels and changes_before_ci(pr)
            if pr.labels & {lb.review, lb.ready_to_merge} or ci_open or blind:
                if self.ledger is not None:
                    pr.check_details = self.gh.check_details(pr.head_sha)
                    pr.checks, pr.failed_checks, pr.failed_runs = summarize_checks(
                        pr.check_details)
                else:
                    pr.checks, pr.failed_checks, pr.failed_runs = self.gh.checks(pr.head_sha)
                record = rerun_of(pr.reruns, pr.head_sha)
                if pr.checks == 'fail' and record is not None:
                    details = (pr.check_details if pr.check_details is not None
                               else self.gh.check_details(pr.head_sha))
                    pr.rerun_waiting = rerun_waiting(record, details)
                if (pr.merge_state == 'conflict' and pr.checks == 'none'
                        and self.cfg.local_tests != 'full' and (pr.meta and lb.review in pr.labels
                                                                 or blind)):
                    # GitHub runs no pull_request workflow on a head that conflicts with the
                    # base (#392): its CI never comes. Not waited for, nor escalated as
                    # ci_missing: the sync, or the fix that aligns the branch with the base,
                    # gives the PR a new head with its own CI.
                    self.log(f'  PR #{pr.number}: no CI expected on {pr.head_sha[:12]}: it '
                             f'conflicts with {self.cfg.base_branch}, and GitHub runs no '
                             'pull_request workflow on it')
                elif (pr.meta and lb.review in pr.labels and pr.checks == 'none'
                        and self.cfg.local_tests != 'full'):
                    pr.ci_wait, pr.ci_missing = self._ci_wait(pr)
                elif blind and pr.checks == 'none' and self.cfg.local_tests != 'full':
                    # While #263 waits for the CI of a just pushed head, so does the fix. Past
                    # that wait the fix starts: checks still missing are not escalated, and the
                    # new head of the fix gets its own CI.
                    pr.ci_wait = self._ci_wait(pr)[0]
            if (lb.ready_to_merge in pr.labels and pr.zero_head is not None
                    and zero_labels_hold(pr, self.cfg.review_policy)):
                pr.zero_problem = self._zero_problem(pr)
            if pr.meta and pr.labels & {lb.review, lb.ready_to_merge} and pr.zero_head is None:
                pr.acceptance = self._acceptance_problems(pr)
            if pr.meta and lb.review in pr.labels:
                pr.resync = self._resync(pr)
            # Also for a ready PR that may not be merged now: its mode tells whether it waits
            # for a person, outside max_open_prs (#199). ``plan`` merges only with merge_ready.
            if (self.cfg.merge.enabled() and pr.meta and lb.ready_to_merge in pr.labels
                    and not operator_held(pr, lb)):
                pr.merge_mode = self._merge_mode(pr)
                if pr.merge_mode in MERGING and (trust_policy_parts(pr.reviews)
                                                 or self._human_merge(pr) is not None):
                    # Work of this PR restarted after a conclave (#142), or a reviewer said it
                    # touches the trust policy (#434): the merge is human.
                    pr.merge_mode = 'off'
            if pr.meta:
                pr.attempt_problem = self._attempt_problem(pr.number, pr.issue,
                                                           pr.meta.get('attempt'))
            prs.append(pr)
        self._issue_of.update((pr.number, pr.issue) for pr in prs)
        self._pr_attempts.update((pr.number, pr.meta.get('attempt')) for pr in prs)
        ready = []
        pr_issues = {pr.issue for pr in prs}
        skip = set(self.cfg.blocked_labels) | {lb.working, lb.needs_supervisor}
        # The issues with a /devloop authorize to record (#607): those in
        # agent:needs-coordinator, and the ready ones whose agent:ready needs an authorization.
        authorizing = []
        if self._coordinator_login() is not None:
            for raw in self.gh.issues_with_label(lb.needs_coordinator):
                if self._pending_authorizations(self.gh.issue_comments(raw['number'])):
                    authorizing.append(raw['number'])
        for raw in self.gh.issues_with_label(lb.ready):
            labels = label_names(raw['labels'])
            events = self.gh.labeled_events(raw['number'])
            # The latest agent:ready: its author admits the issue and its time is the issue's
            # seniority in the queue (#146).
            admitted = ([e for e in events if e['label'] == lb.ready] or [{}])[-1]
            # Whose labels count depends on who opened the issue (#178).
            author = author_login(raw.get('author'))
            # The coordinator's agent:ready (#528) and priority labels (#595) count too.
            admitters = self._admitters(author, coordinator=True)
            if admitted.get('by') in admitters and self._needs_authorization(admitted.get('by')):
                # The supervisor's agent:ready admits only work the coordinator authorized for
                # its current text (#607); a /devloop authorize not recorded yet is, first.
                comments = self.gh.issue_comments(raw['number'])
                if self._pending_authorizations(comments):
                    authorizing.append(raw['number'])
                admitters = self._ready_admitters(self.gh.issue_history(raw['number']),
                                                  comments)
                if admitted.get('by') not in admitters:
                    self.log(f'  issue #{raw["number"]}: ignored {lb.ready} by '
                             f'{admitted.get("by")}: no authorization of the coordinator '
                             f'({AUTHORIZE_COMMAND}) holds for its current text')
            issue = {'number': raw['number'], 'title': raw['title'], 'labels': labels,
                     'labeler': admitted.get('by'), 'ready_at': admitted.get('at'),
                     'admitted': admitted.get('by') in admitters,
                     'priority': issue_priority(labels, events, admitters,
                                                self.cfg.priority_labels)}
            if issue['admitted'] and not issue['labels'] & skip and issue['number'] not in pr_issues:
                # A merged PR that resolved it (#228): ``plan`` never starts it again.
                issue['merged'] = [p['number'] for p in self._merged_prs(issue['number'])]
            ready.append(issue)
        # Native dependencies (#277) of the issues that may start, then the readiness check of
        # those without an open block (#114), that may start or are blocked by the check.
        candidates = [i for i in ready if 'merged' in i and not i['merged']]
        views = self._dependency_view([i['number'] for i in candidates]) if candidates else {}
        for issue in candidates:
            issue.update(views[issue['number']])
            if issue['cycle']:
                issue['cycle_reported'] = self._cycle_reported(issue['number'], issue['cycle'])
                self.log(f'  issue #{issue["number"]}: dependency cycle '
                         + ', '.join(issue['cycle']) + ': it does not start')
            if self.cfg.preflight and not issue['blocked_by']:
                issue['preflight'] = self._preflight_view(issue['number'], issue['labels'],
                                                          base_head)
        if self.cfg.preflight:
            # An issue that waits for the supervisor runs no check, but a request of a person
            # for one is answered all the same (#468): its view says whether one is pending.
            waiting = (set(self.cfg.blocked_labels) | {lb.working}) - {lb.needs_supervisor}
            for issue in ready:
                if (lb.needs_supervisor in issue['labels'] and issue['admitted']
                        and not issue['labels'] & waiting and issue['number'] not in pr_issues):
                    issue['preflight'] = self._preflight_view(issue['number'], issue['labels'],
                                                              base_head)
        working_raw = self.gh.issues_with_label(lb.working)
        working = [raw['number'] for raw in working_raw]
        # An implementation whose failed tests fail on the base too (#459): whether it still
        # waits for the base branch to advance, the issue's labels, which may hold its restart,
        # and, with ``preflight``, the view of its readiness check once it may start again.
        waits = {}
        for raw in working_raw:
            number = raw['number']
            if number in pr_issues:
                continue
            wait = self._base_wait(self.gh.issue_comments(number), 'implement')
            if wait is None:
                continue
            labels = label_names(raw.get('labels') or [])
            holds = self._base_holds(wait, base_head)
            ci = base_ci(wait['base']) if holds else ''
            waits[number] = {'holds': holds, 'labels': labels, 'preflight': None, 'ci': ci}
            # It starts again only while an authorization of the coordinator holds (#607): a
            # text changed during the wait voids it, with no check run first.
            unauthorized = '' if holds else self._unauthorized_ready(number)
            if unauthorized:
                waits[number]['unauthorized'] = unauthorized
            elif not holds and self.cfg.preflight:
                waits[number]['preflight'] = self._preflight_view(number, labels, base_head)
            self.log(f'  issue #{number}: the implementation '
                     + (f'waits for {self.cfg.base_branch} to move past {wait["base"][:12]}'
                        + (', but its CI passed: host_tests' if ci else '')
                        if holds else f'may start again: {self.cfg.base_branch} moved '
                        f'past {wait["base"][:12]}'))
        return Snapshot(prs=prs, ready_issues=ready, working_issues=working, base_waits=waits,
                        authorizations=list(dict.fromkeys(authorizing)),
                        supervised=self._supervised_items(prs),
                        conclaves=self._conclave_items(prs),
                        advisories=self._advisory_items(prs))

    def _pushed(self, path):
        """Record that the head of worktree ``path`` was pushed now (#263): the wait for its CI
        counts from here."""
        self._ci_since[self.ws.head(path)] = self.clock()

    def _ci_wait(self, pr):
        """``(why, missing)`` for a PR in agent:review whose head has no CI, with ``local_tests``
        other than ``full`` (#263). Within ``[ci] grace_minutes`` of the head's push the review
        waits, as if its CI were pending (``why`` says it, and the log too). After that, when
        the base branch requires checks, it waits for them up to ``required_timeout_minutes``,
        and then ``missing`` lists them: the PR is escalated, even within a grace window longer
        than that timeout. Otherwise ``('', [])``: the full suite runs locally before the
        review, as before.

        The push counts from when this dispatcher pushed the head, else from the head's commit
        date, read once; a date in the future, or one that cannot be read, counts from now, so
        that the wait stays bounded."""
        ci, sha = self.cfg.ci, pr.head_sha
        now = self.clock()
        since = self._ci_since.get(sha)
        if since is None:
            try:
                committed = _timestamp(self.gh.commit_date(sha))
            except GitHubError as exc:
                self.log(f'  PR #{pr.number}: commit date of {sha[:12]} not read: {exc}')
                committed = None
            since = now if committed is None else min(committed, now)
            self._ci_since[sha] = since
        age = now - since
        grace = age < ci.grace_minutes * 60
        timed_out = age >= ci.required_timeout_minutes * 60

        def in_grace():
            why = (f'waiting for CI on {sha[:12]}: pushed {int(age)} s ago, within '
                   f'[ci] grace_minutes = {ci.grace_minutes}')
            self.log(f'  PR #{pr.number}: {why}')
            return why, []

        # A grace longer than the timeout of the required checks does not delay their
        # escalation: past the timeout the required checks are read even within the grace.
        if grace and not timed_out:
            return in_grace()
        base = self.cfg.base_branch
        try:
            required = self.gh.required_checks(base)
        except GitHubError as exc:
            self.log(f'  PR #{pr.number}: the checks {base} requires not read, counted as none: '
                     f'{exc}')
            required = []
        if not required:
            return in_grace() if grace else ('', [])
        if not timed_out:
            why = (f'waiting for CI on {sha[:12]}: {base} requires {", ".join(required)}, not '
                   f'reported {int(age)} s after the push ([ci] required_timeout_minutes = '
                   f'{ci.required_timeout_minutes})')
            self.log(f'  PR #{pr.number}: {why}')
            return why, []
        self.log(f'  PR #{pr.number}: no CI on {sha[:12]} {int(age)} s after the push, but {base} '
                 f'requires {", ".join(required)}')
        return '', list(required)

    def _supervised_items(self, prs):
        """The issues and PRs in ``agent:needs-supervisor``, not in ``agent:needs-operator``,
        with a supervisor command to apply (#182), and those in ``agent:needs-operator`` with a
        command of a trusted user (#321); either may be a command only to answer
        (``_supervisor_view``). An extension of the budget envelope recorded in the journal and
        not yet traced (``_pending_extension``, #411) is the command of its item."""
        lb = self.cfg.labels
        items = [(pr.number, True, self.gh.pr_comments, operator_blocked(pr.labels, lb))
                 for pr in prs if lb.needs_supervisor in pr.labels
                 or operator_blocked(pr.labels, lb)]
        seen = set()
        for label in (lb.needs_supervisor, *sorted(lb.operator())):
            for raw in self.gh.issues_with_label(label):
                if raw['number'] not in seen:
                    seen.add(raw['number'])
                    items.append((raw['number'], False, self.gh.issue_comments,
                                  operator_blocked(label_names(raw['labels']), lb)))
        found = []
        for number, is_pr, read, operator in items:
            comments = read(number)
            # An extension of the budget envelope whose records a stopped or failed run did not
            # write (#411) is the command to complete, whatever command is the latest now and
            # wherever the item is; ``trace_only`` when its step does not resume from it.
            pending = self._pending_extension(number, comments, is_pr, operator)
            view = (dict(pending['command'], step=pending['escalation'].get('step'),
                         anchor={'kind': 'notice', 'record': pending['escalation']},
                         trace_only=not pending['resume'])
                    if pending else self._supervisor_view(number, comments, operator))
            if view is not None:
                # ``coordinator``: the command is the coordinator's, whose grant-budget on a
                # budget escalation to the operator runs the step (#595, ``runs_issue_step``).
                found.append(dict(view, number=number, pr=is_pr,
                                  coordinator=self._is_coordinator(view.get('by'))))
        # A retry of an issue's step, or an extension of the budget envelope that runs it
        # (#411, #595), waits while the issue is blocked (#277), as a ready issue does:
        # ``blocked_by`` holds its open blocks.
        retries = [item for item in found if runs_issue_step(item)]
        views = self._dependency_view([item['number'] for item in retries]) if retries else {}
        for item in retries:
            item['blocked_by'] = views[item['number']]['blocked_by']
            if item['blocked_by']:
                what = 'the retry' if item['command'] == RETRY else 'the extension'
                self.log(f'  issue #{item["number"]}: {what} waits, blocked by '
                         + ', '.join(item['blocked_by']))
        return found

    # Dependencies (#277) -------------------------------------------------------
    def _dependency_edges(self, numbers):
        """The trusted "blocked by" dependencies that are open, from the issues ``numbers`` of
        the repository: ``{(repo, number): {(repo, number), ...}}``, ``repo`` empty for the
        repository's own issues (``issue_ref``).

        A block counts only while its issue is open and only if the latest event that added it
        comes from a login that may admit the blocked issue (``_admitters``: a trusted user, or
        the supervisor's app on an issue of a trusted user or of devloop): any other is ignored
        and logged. The open blocks of the repository are explored in turn, ``agent:ready`` or
        not, up to ``MAX_DEPENDENCY_READS`` issues beyond ``numbers``; an issue of another
        repository blocks as its state says, without reading its own blocks."""
        # The repository under its name or a former one (``repo_aliases``, #540).
        home = {name.lower() for name in (self.cfg.repo, *self.cfg.repo_aliases)}

        def key(item):
            repo = item.get('repo') or ''
            return ('' if repo.lower() in home else repo, item['number'])

        todo = [('', number) for number in dict.fromkeys(numbers)]
        seen, edges = set(todo), {}
        budget = len(todo) + MAX_DEPENDENCY_READS
        while todo:
            node = todo.pop(0)
            if budget <= 0:
                self.log(f'  dependencies: more than {MAX_DEPENDENCY_READS} prerequisites to '
                         f'read, {issue_ref(node)} and the others are not explored')
                break
            budget -= 1
            read = self.gh.issue_dependencies(node[1])
            admitters = self._admitters(read.get('author'))
            added_by = {key(event): event.get('by') for event in read.get('added') or []}
            blockers = set()
            for item in read.get('blocked_by') or []:
                if item.get('state') != 'OPEN':
                    continue  # a closed block blocks nothing
                blocker = key(item)
                by = added_by.get(blocker)
                if by not in admitters:
                    self.log(f'  issue {issue_ref(node)}: ignored the block by '
                             f'{issue_ref(blocker)}, added by {by or "an unknown author"}: '
                             'neither a trusted user nor the supervisor')
                    continue
                blockers.add(blocker)
                if not blocker[0] and blocker not in seen:
                    seen.add(blocker)
                    todo.append(blocker)
            edges[node] = blockers
        return edges

    def _dependency_view(self, numbers):
        """What planning needs about the dependencies of the ready issues ``numbers``, read now
        from GitHub (#277): for each, ``{blocked_by, blocks, cycle}`` as ``Snapshot`` describes
        them."""
        edges = self._dependency_edges(numbers)
        cycles = dependency_cycles(edges)
        ready = set(numbers)
        views = {}
        for number in numbers:
            blockers = sorted(edges.get(('', number), ()))
            views[number] = {
                'blocked_by': [issue_ref(b) for b in blockers],
                'blocks': sorted(n for n in ready
                                 if ('', number) in edges.get(('', n), ()) and n != number),
                'cycle': [issue_ref(m) for m in cycles.get(('', number), ())]}
        return views

    def _cycle_reported(self, number, cycle):
        """Whether the dispatcher already reported ``cycle`` on issue ``number``: a ``cycle``
        record it signed, with the same issues."""
        return any(record['cycle'] == cycle for _, record in
                   self._dispatcher_records(self.gh.issue_comments(number), 'cycle'))

    def _do_dependency_cycle(self, number):
        """Report on a ready issue the dependency cycle it is on (#277), once per cycle. The
        cycle is read again first: one removed since, or already reported, is not."""
        cycle = self._dependency_view([number])[number]['cycle']
        if not cycle:
            self.log('  no longer on a dependency cycle')
            return
        if self._cycle_reported(number, cycle):
            self.log('  cycle already reported')
            return
        self.gh.comment(number, render_cycle({'cycle': cycle}, self.cfg.language))
        self.log('  dependency cycle reported: ' + ', '.join(cycle))

    # Tick ----------------------------------------------------------------
    def tick(self, dry_run=False):
        if self.workers is not None and not dry_run:
            return self._tick_continuous()
        self._auto_pin(dry_run)
        if not dry_run:
            self._housekeeping()
            self._watch_journal()
            self._resume_publications()
            self._signal_dropped()
            self._watch_main()
        self._transition_labels(dry_run)
        snap = self.snapshot()
        self._flag_incomplete(snap, dry_run)
        self._reconcile_turns(snap, dry_run)
        self._settle_attempts(snap, dry_run)
        # The supervisor's temporary capacity limits in force (#412), for this plan only.
        cfg = self._capacity(dry_run)
        actions = plan(snap, cfg)
        prs = {pr.number: pr for pr in snap.prs}
        self.log(f'{"[dry-run] " if dry_run else ""}{pr_counts_line(snap, cfg)}')
        if dry_run:
            for line in queue_lines(snap):
                self.log(f'[dry-run] {line}')
        if not dry_run and self.cfg.max_parallel_actions > 1:
            self._run_parallel(actions, prs)
            return actions
        for action in actions:
            self.log(f'{"[dry-run] " if dry_run else ""}{action.kind} #{action.number}'
                     + (f': {action.reason}' if action.reason else ''))
            if dry_run:
                continue
            try:
                self._run_action(action, prs)
            except Exception as exc:  # the tick fails as before, with its traceback kept
                self.log(f'  unexpected error: {type(exc).__name__}: {exc}'
                         + self._keep_traceback(action, exc))
                raise
        return actions

    def _tick_continuous(self):
        """A tick of the continuous mode (#227): plan around the actions still running and
        start in ``self.workers`` those that fit the free slots (``admit``), without waiting for
        them. Returns the actions started.

        The pin after a merge runs at once, also while actions run (#301): a new copy only
        moves ``current``, and the restart from it waits for the first tick at which nothing
        runs, while this one plans and starts actions with the code it loaded, as without
        ``[pin] auto``. While the restart is pending no new implementation starts (#342), the
        long action, so that the running ones end soon; the short ones still start. After
        ``[pin] restart_max_wait_minutes`` of a pending restart nothing starts until the running
        actions end and the restart happens. A merge that requests a pin while this tick reads
        GitHub (#264) is pinned by the next tick."""
        workers = self.workers
        # An action that ends from here on wakes the loop again: its result is not in this plan.
        workers.clear()
        running = workers.running()
        self._auto_pin(False, defer=bool(running))
        if not running:
            self._restart_if_pending()
            self._housekeeping()
        elif self._restart is not None:
            if self._restart_wait_over(running):
                return []
            self.log(restart_line(self._restart[0], iso(self._restart[1]), len(running)))
        self._watch_journal()
        self._resume_publications()
        self._signal_dropped()
        self._watch_main()
        self._transition_labels(False)
        snap = self.snapshot()
        prs = {pr.number: pr for pr in snap.prs}
        issue_of = {number: pr.issue for number, pr in prs.items()}
        busy = set()
        for action, numbers in running:
            busy |= numbers | numbers_of(action.number, issue_of)
        # A PR an implementation opened while it still runs belongs to it too.
        busy |= {number for number, issue in issue_of.items() if issue in busy}
        self._flag_incomplete(snap, False, busy)
        self._reconcile_turns(snap, False)
        self._settle_attempts(snap, False)
        # A conclave that restarts an implementation (#142, ``Action.opens``) may open a PR too.
        reserved = sum(1 for action, _ in running
                       if action.kind in ('implement', 'preflight')
                       or action.kind == 'supervise' and action.number not in prs
                       or action.kind == 'conclave' and action.opens)
        merging = any(action.kind in MERGE_LANE for action, _ in running)
        # #342: no new implementation, the long action, while the restart waits; the plan
        # leaves its slots to the other actions.
        pending = bool(running) and self._restart is not None
        # The supervisor's temporary capacity limits in force (#412), for this plan only.
        cfg = self._capacity()
        actions = plan(snap, cfg, busy, reserved, merging, implement=not pending)
        # The limit may pass while the snapshot reads GitHub: checked again before any start.
        if pending and self._restart_wait_over(running):
            return []
        started = admit(actions, issue_of, running, workers.free())
        self.log(f'{pr_counts_line(snap, cfg)}; running: '
                 + (', '.join(f'{a.kind} #{a.number}' for a, _ in running) or 'none'))
        for action in started:
            workers.start(action, numbers_of(action.number, issue_of),
                          lambda action=action: self._run_started(action, prs))
        return started

    def _restart_wait_over(self, running):
        """Whether the pending restart waited ``[pin] restart_max_wait_minutes`` or more for the
        ``running`` actions (#301); if so, log that no action starts until the restart."""
        sha, since = self._restart
        limit = self.cfg.pin.restart_max_wait_minutes
        if self.clock() - since < limit * 60:
            return False
        self.log(restart_line(sha, iso(since), len(running),
                              f'over [pin] restart_max_wait_minutes ({limit}), no action starts '
                              'until the restart'))
        return True

    def _set_restart(self, copy):
        """Record that a restart from the new pinned ``copy`` waits for the running actions
        (#301); None when none waits. The first moment of the wait stays while later pins
        replace the copy, so that a merge after another does not reset the limit."""
        if copy is None:
            self._restart = None
        else:
            sha = Path(copy).name.removeprefix(PREFIX)
            since = self._restart[1] if self._restart is not None else self.clock()
            self._restart = (sha, since)
        if self.activity:
            self.activity.restart(*(self._restart or (None, None)))

    def _restart_if_pending(self):
        """With nothing running and a restart pending (#301), start again from the copy
        ``current`` points at now (``Repinned``): the one of the pin, unless the operator
        pinned another by hand meanwhile. None runs when that is the running copy."""
        if self._restart is None:
            return
        copy = self.pinner.current()
        self._set_restart(None)
        if copy is None or Path(copy).resolve() == INSTALL_ROOT.resolve():
            self.log('no restart: current no longer points at a new copy')
            return
        self.log(f'nothing runs: restart from {Path(copy).name}')
        raise Repinned(copy)

    def _housekeeping(self):
        """The cleanup of ``state_dir`` (#380), at a tick with nothing running, before planning:
        a failure is logged and never fails the tick."""
        if self.housekeeping is None:
            return
        try:
            self.housekeeping.tick(self.cfg)
        except Exception as exc:  # the cleanup never stops the tick
            self.log(f'gc: the cleanup failed: {type(exc).__name__}: {exc}')

    def _run_started(self, action, prs):
        """Run an action of the continuous mode in its worker thread, as a lane of one, and log
        its outcome."""
        outcomes = {}
        self._run_lane([(0, action)], prs, outcomes)
        self.log(f'outcome of {action.kind} #{action.number}: ' + outcomes.get(0, 'not run'))

    def _transition_labels(self, dry_run):
        """At the first tick of the process (#248), as ``devloop setup`` does: rename
        ``agent:needs-operator`` from its old name when only that exists, so that the label the
        dispatcher adds exists and the issues and PRs that had the old one keep it; with both
        names, touch neither and say so. A dry run only logs what it would do. A failed read
        or rename is logged and tried again at the next tick: until then the old label blocks
        as the new one."""
        if self.labels_checked:
            return
        lb = self.cfg.labels
        try:
            if dry_run:
                transition = label_transition(self.gh, lb)
                if transition == 'rename':
                    self.log(f'[dry-run] labels: would rename {LEGACY_NEEDS_OPERATOR} to '
                             f'{lb.needs_operator}')
                elif transition == 'both':
                    self.log(f'[dry-run] labels: {both_labels_warning(lb)}')
                return
            note = transition_labels(self.gh, lb, self.cfg.language)[1]
        except GitHubError as exc:
            self.log(f'labels: the rename of {LEGACY_NEEDS_OPERATOR} could not be checked, '
                     f'next tick: {exc}')
            return
        if note:
            self.log(f'labels: {note}')
        self.labels_checked = True

    def _auto_pin(self, dry_run, defer=False):
        """With ``[pin] auto = true`` and a pin requested by a merge (#174), pin
        ``origin/<base_branch>`` before planning. A pin that fails before it resolves the
        commit (the fetch, the lock of the pins) stops the tick with ``PinError`` and leaves the
        pointer and the request as they were; a new copy raises ``Repinned``, so that the
        process starts again from it. With ``defer`` (actions run, #301) a new copy only records
        the restart, which waits for them (``_set_restart``).

        A copy refused by the smoke test (``SmokeError``, #260), or one that fails the checks
        before it (``CheckFailed``, #453: a missing file, the import, the configuration), is not
        a failed tick: the request is dropped, ``current`` stays, planning goes on from the
        running copy, and an alert goes to ``[run].alert_issue`` once per refused commit, with
        the ``pin`` record the daily summary reads. The commit is tried again only when
        ``origin/<base_branch>`` advances to another commit, or by a pin by hand: a request that
        resolves the commit that failed last is dropped without trying it (``again``), and its
        alert is not published twice.

        A merge devloop did not make (#430), such as one of the operator, requests the pin
        too: before reading the request, a tick that is not a dry run asks the pinner whether
        ``origin/<base_branch>`` advanced with changes to ``[pin] paths`` (``observe``). A
        failure there is logged, and the next tick tries again.

        While a merged PR's step on the host holds the auto-pin (#462, ``AutoPin.held``) a
        request is only logged: it stays until the operator pins by hand, after the step. The
        hold is read again once the pin resolved the commit (``PinHeld``): a merge that ran
        meanwhile wrote its hold before merging. A hold whose merge had an unknown outcome is
        settled first (``_settle_host_steps``).

        A merge devloop did not make (#473), such as one of the operator, holds the auto-pin as
        well: after ``observe``, and again once the pin resolved its commit, the pinner reads
        the PRs merged by the new commits of ``origin/<base_branch>`` (``scan``, with
        ``_merged_host_steps``) and holds those that declare a step. Then the alarm of each hold
        that has not gone out yet is tried again (``_alert_held_steps``). The PRs of the
        resolved commit that cannot be read leave the pin to the next tick (``ScanFailed``)."""
        if self.pinner is None or not self.cfg.pin.auto:
            return
        ref = f'origin/{self.cfg.base_branch}'
        if not dry_run:
            try:
                self.pinner.observe(self.cfg.base_branch, self.cfg.pin.touches)
            except (PinError, OSError) as exc:
                self.log(f'auto-pin: the advance of {ref} not checked, next tick: {exc}')
            self._settle_host_steps()
            try:
                self.pinner.scan(self.cfg.base_branch, self._merged_host_steps)
            except (PinError, OSError) as exc:
                self.log(f'auto-pin: the merges of {ref} not checked for a step on the host, '
                         f'next tick: {exc}')
            self._alert_held_steps()
        request = self.pinner.pending()
        if not request:
            return
        steps = self.pinner.held()
        if steps:
            self._log_held_pin(ref, steps, dry_run)
            return
        if request.get('pr'):
            why = f'the merge of #{request["pr"]}'
        elif isinstance(request.get('head'), str) and request['head']:
            why = f'the advance of {ref} to {request["head"][:12]}'
        else:
            why = 'a merge'
        if dry_run:
            self.log(f'[dry-run] auto-pin of {ref} pending after {why}')
            return
        self.log(f'auto-pin of {ref} after {why}')
        try:
            copy = self.pinner.pin(ref, lookup=self._merged_host_steps)
        except PinHeld as exc:
            # A merge with a step on the host ran while the pin resolved the commit (#462), or
            # the commit holds one devloop did not make (#473): its alarm goes out now.
            self._alert_held_steps()
            self._log_held_pin(ref, exc.steps, dry_run)
            return
        except ScanFailed as exc:
            self.log(f'auto-pin of {ref} waits for the next tick, the pinned copy is unchanged: '
                     f'{exc}')
            return
        except SmokeError as exc:
            self.pinner.cancel()
            self.log(f'auto-pin of {ref} refused, the pinned copy is unchanged and the request '
                     f'is dropped: {exc}')
            self._alert_refused_pin(ref, exc)
            return
        except CheckFailed as exc:
            self.pinner.cancel()
            self.log(f'auto-pin of {ref} failed the checks, the pinned copy is unchanged and the '
                     f'request is dropped: {exc}')
            self._alert_refused_pin(ref, exc)
            return
        except (PinError, ConfigError, OSError) as exc:
            raise PinError(f'auto-pin of {ref} failed, the pinned copy is unchanged: {exc}') \
                from exc
        if Path(copy).resolve() == INSTALL_ROOT.resolve():
            if self._restart is not None:
                self._set_restart(None)  # the new pin is the running copy: nothing to restart
        elif defer:
            self._set_restart(copy)
            self.log(f'restart from {Path(copy).name} deferred: the running actions go on with '
                     'the code loaded, and the restart happens when none runs')
        else:
            if self._restart is not None:
                self._set_restart(None)  # this restart is the one that waited
            raise Repinned(copy)

    def _log_held_pin(self, ref, steps, dry_run):
        """A merged PR declared a step on the host (#462): only a pin by hand, after it,
        releases the auto-pin. The request stays, and that pin clears it."""
        self.log(f'{"[dry-run] " if dry_run else ""}auto-pin of {ref} held: the step on the '
                 'host of ' + held_prs(steps) + ' waits for the operator, who pins by hand '
                 'after it (devloop pin)')

    def _alert_refused_pin(self, ref, exc):
        """The alert of a copy refused by the smoke test of an auto-pin (#260, ``SmokeError``)
        or by the checks before it (#453, ``CheckFailed``), on ``[run].alert_issue``, once per
        commit. For the smoke test: its commit and failed step, never the error's text (it stays
        in the local log). For the checks: its commit and the reason (``public_reason``: first
        line, absolute paths reduced to their last name). Both carry a ``pin`` record (#453):
        the commit, ``ref``, the time and that reason, which the daily summary lists. One
        attempt; a failure is logged."""
        issue = self.cfg.run.alert_issue
        if not issue:
            self.log('  no [run].alert_issue is configured: no alert for the refused pin')
            return
        if self.pinner.alerted(exc.sha):
            self.log(f'  the refusal of {exc.sha[:12]} was already reported on #{issue}')
            return
        t = TEXTS.texts(self.cfg.language)
        if isinstance(exc, CheckFailed):
            reason = public_reason(exc.reason).replace('`', "'")
            text = (dispatcher_header() + f'\n## {t("pin_check_title")}\n\n'
                    + t('pin_check_text', ref=ref, sha=exc.sha[:12], reason=reason))
        else:
            reason = f'smoke test: {exc.step}'
            text = (dispatcher_header() + f'\n## {t("pin_smoke_title")}\n\n'
                    + t('pin_smoke_text', ref=ref, sha=exc.sha[:12], step=exc.step))
        # The record of the daily summary (#453): the commit not pinned, when and why, as above.
        body = publishable(text, 'pin', {'sha': exc.sha, 'ref': ref, 'reason': reason,
                                         'at': iso(self.clock())})
        try:
            self.gh.comment(issue, body)
        except Exception as exc_:  # one attempt only: its class, never its output
            self.log(f'  the alert of the refused pin on #{issue} could not be published '
                     f'({type(exc_).__name__})')
            return
        self.pinner.mark_alerted(exc.sha)
        self.log(f'  alert of the refused pin published on #{issue}')

    def _run_action(self, action, prs):
        """Run one action; a failure of an agent, ``gh``, git or trust escalates its issue or
        PR and is returned. Any other exception propagates."""
        handler = getattr(self, f'_do_{action.kind}')
        # Recorded for `devloop status` while it runs, removed afterwards even on error.
        running = (self.activity.action(action.kind, action.number) if self.activity
                   else nullcontext())
        with running:
            try:
                handler(prs.get(action.number) or action.number)
            except (AgentError, GitHubError, WorkspaceError, UntrustedChange) as exc:
                self.log(f'  failed: {exc}')
                self._escalate_failure(action, prs.get(action.number), exc)
                return exc
        return None

    def _run_parallel(self, actions, prs):
        """Run the actions of a tick as ``schedule`` arranges them, up to
        ``max_parallel_actions`` at once (#176). A failed action does not stop the others: each
        lane goes on with its next action, and the tick reports the outcome of every one."""
        outcomes = {}
        issue_of = {number: pr.issue for number, pr in prs.items()}
        with ThreadPoolExecutor(max_workers=self.cfg.max_parallel_actions,
                                thread_name_prefix='devloop-action') as pool:
            for phase in schedule(actions, issue_of):
                wait([pool.submit(self._run_lane, lane, prs, outcomes) for lane in phase])
        for position, action in enumerate(actions):
            self.log(f'outcome of {action.kind} #{action.number}: '
                     + outcomes.get(position, 'not run'))

    def _run_lane(self, lane, prs, outcomes):
        """Run the actions of one lane in order, in a worker thread. An unexpected exception
        of an action escalates its issue or PR, as a failure would, instead of stopping the
        tick."""
        for position, action in lane:
            with self.log.prefixed(f'#{action.number}'):
                self.log(f'{action.kind} #{action.number}'
                         + (f': {action.reason}' if action.reason else ''))
                try:
                    failed = self._run_action(action, prs)
                except Exception as exc:  # reported and escalated, never lost
                    outcomes[position] = f'unexpected error: {type(exc).__name__}: {exc}'
                    self.log(f'  unexpected error: {type(exc).__name__}: {exc}'
                             + self._keep_traceback(action, exc))
                    try:
                        self._escalate_failure(action, prs.get(action.number), RuntimeError(
                            TEXTS.texts(self.cfg.language)(
                                'unexpected_error', error=f'{type(exc).__name__}: {exc}')))
                    except Exception as again:
                        self.log(f'  could not escalate: {again}')
                    continue
                first = (str(failed).strip().splitlines() or [type(failed).__name__])[0]
                outcomes[position] = f'failed: {first}' if failed else 'done'

    def _keep_traceback(self, action, exc):
        """Write the traceback of ``exc``, an unexpected error of ``action``, in the local logs
        (``<state_dir>/logs``, #381), never on GitHub; return what the run log line adds: the
        file, or why it could not be written."""
        name = f'{time.strftime("%Y%m%dT%H%M%S")}-{action.kind}-{action.number}-traceback.txt'
        text = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        try:
            path = self.ws.save_evidence(name, text)
        except Exception as again:  # the error itself is still reported
            return f' (traceback not written: {type(again).__name__})'
        return f' (traceback in {path})'

    # Helpers -------------------------------------------------------------
    def _escalate(self, number, reason, detail='', remove=(), cls='failure', supervised=None,
                  why='', retry=None, extra=None):
        """Escalate issue or PR ``number`` with an escalation of class ``cls`` (#182). With
        ``supervised`` (``_supervised``) it goes to ``agent:needs-supervisor`` and an issue
        keeps ``agent:ready``; otherwise to ``agent:needs-operator``, with ``why`` a non-critical
        class in ``auto`` stays with the operator, and with ``retry`` (``_retry_payload``) the
        step a trusted user may run again with ``/devloop retry`` (#321)."""
        lb = self.cfg.labels
        language = self.cfg.language
        if supervised:
            self.gh.comment(number, render_escalation(reason, detail, cls, supervised,
                                                      language=language))
            self.gh.edit_labels(number, add=[lb.needs_supervisor],
                                remove=[label for label in remove if label != lb.ready])
        else:
            if why:
                reason = (reason.rstrip() + '\n\n'
                          + SUPERVISION_TEXTS.texts(language)('why_operator', why=why))
            self.gh.comment(number, render_escalation(reason, detail, cls, retry=retry,
                                                      extra=extra, language=language))
            self.gh.edit_labels(number, add=[lb.needs_operator], remove=remove)
        if lb.ready_to_merge in remove:
            # An escalation that takes the readiness away voids the receipt of the promotion
            # (#135), whatever its path: round limit, failure, open findings, lost marker.
            label = lb.needs_supervisor if supervised else lb.needs_operator
            self._supersede_receipts(number, self._merge_texts()(
                'supersede_escalation', cls=cls, label=label))

    def _log_hint(self, exc):
        t = TEXTS.texts(self.cfg.language)
        log = getattr(exc, 'log', None)
        hint = (t('log_hint', name=Path(log).name, logs=f'{self.cfg.state_dir}/logs') if log
                else '')
        if getattr(exc, 'tail', ''):
            hint += t('tail_hint', lines=TAIL_LINES)
        return hint

    def _escalate_failure(self, action, pr, exc):
        """Escalate a failed action. Its class (#182): ``tool_failure`` when the agent or its
        model produced no result (``AgentError``: a crash, a timeout, a stall, an unavailable
        model, a denied permission), ``untrusted`` for an issue text no longer trusted,
        ``failure`` for anything else (``gh``, git, the dispatcher itself).

        A conclave (#141) is never escalated, whatever failed and however it ran
        (``_run_action``, or ``_run_lane`` for an unexpected error): it runs on an item already
        with the operator, and another escalation would end its block, leaving a call started
        without a result unsettled. It resumes from its record on a later tick. Except a call
        refused by the block on uncertain turns (``TurnBlocked``, #362): it escalates as
        ``uncertain_turn``, and the conclave of that block ends there, the item staying with the
        operator. The call was refused before being recorded as started (``_conclave_call``).

        Nor is an advisory review (#369): the PR is already the operator's, and the advisory
        review changes no label. A head without its summary gets it on a later tick. Except,
        again, a turn refused by ``TurnBlocked``: it escalates as ``uncertain_turn``, and while
        the turns it names still block the work the PR gets no other advisory action
        (``_advisory_held``).

        The escalation of a ``TurnBlocked`` records the ids of the turns it names (``turns``).
        A turn the budget envelope does not cover (``BudgetRefused``, #365, a ``TurnBlocked``
        with no turns) escalates the same way, with class ``budget``, its ``category`` and, on
        a PR, its ``head`` (#396); or, when the supervisor may extend the envelope of its story
        (``_budget_grant``, #411), to the supervisor as ``budget_grantable``, with the story
        and the extension in its record."""
        if action.kind == 'conclave' and not isinstance(exc, TurnBlocked):
            self.log('  conclave: no escalation, it resumes from its record')
            return
        if action.kind == 'advisory' and not isinstance(exc, TurnBlocked):
            self.log('  advisory review: no escalation, the PR is already the operator\'s')
            return
        lb, t = self.cfg.labels, TEXTS.texts(self.cfg.language)
        reason = t('step_failed', step=action.kind, failure=_failure_text(exc),
                   hint=self._log_hint(exc))
        if getattr(exc, 'kept_work', None):  # what a timed-out writing turn left (#374)
            reason += kept_work_text(exc.kept_work, self.cfg.state_dir, self.cfg.language)
        if action.kind == 'advisory' and isinstance(exc, BudgetRefused) and pr is not None:
            reason += t('advisory_held', head=pr.head_sha[:12])
        if _cli_state_refused(exc):
            reason += t('cli_state')
        cls = ('configuration' if _cli_state_refused(exc)  # the CLIs' entries (#517)
               else TOOL_FAILURE if isinstance(exc, AgentError)
               else 'untrusted' if isinstance(exc, UntrustedChange)
               else BUDGET if isinstance(exc, BudgetRefused)
               else UNCERTAIN_TURN if isinstance(exc, TurnBlocked) else 'failure')
        number = pr.number if pr is not None else action.number
        remove = ([lb.review, lb.changes, lb.ready_to_merge] if pr is not None
                  else [lb.working, lb.ready])
        try:
            reviewer, key = getattr(exc, 'reviewer', None), getattr(exc, 'key', None)
            supervised, why = self._supervised(number, cls, action.kind, pr, reviewer, key=key)
            if isinstance(exc, BudgetRefused):
                # The one extension of the envelope, when the supervisor may grant it (#411).
                supervised, why = self._budget_grant(number, action.kind, pr, reviewer, key,
                                                     exc)
                if supervised:
                    cls = BUDGET_GRANTABLE
                    language = self.cfg.language
                    reason += '\n\n' + SUPERVISION_TEXTS.texts(language)(
                        'budget_grantable',
                        extension=extension_text(supervised['extension'], language))
            # To the operator the step stays recorded too, for a trusted user's retry (#321).
            retry = None if supervised else self._retry_payload(action.kind, pr, reviewer, key)
            if retry and retry['step'] == 'preflight':
                # A readiness check runs again only on an issue with agent:ready: one the
                # operator may retry keeps it, as a failed check does.
                remove = [label for label in remove if label != lb.ready]
            if pr is None and (supervised or retry or {}).get('step') == 'preflight':
                # The check that restarts an implementation waiting for its base keeps
                # agent:working (#459): its escalation leaves the wait (``_base_wait``), and a
                # retry resumes it.
                remove = [label for label in remove if label != lb.working]
            # A budget refusal on a PR records its head (#396): an advisory review waits only
            # on the refusal of the head it would review (``_advisory_held``). To the operator
            # it records the story, the kind and the agent of the refused turn too, for the
            # coordinator's extension of its envelope (#595, ``_operator_grant_budget``).
            extra = ({'category': exc.category,
                      **({'head': pr.head_sha} if pr is not None else {}),
                      **({} if supervised else budget_fields(exc))}
                     if isinstance(exc, BudgetRefused)
                     else {'turns': [t['turn'] for t in exc.turns]}
                     if isinstance(exc, TurnBlocked) else None)
            self._escalate(number, reason, getattr(exc, 'tail', ''), remove=remove, cls=cls,
                           supervised=supervised, why=why, retry=retry, extra=extra)
        except GitHubError as gh_exc:
            self.log(f'  could not escalate: {gh_exc}')

    def _issue_context(self, number):
        """The issue as model input. Title and body come from the same read as their edit
        history, and are refused if someone outside ``trusted_users`` changed them after the
        ready label."""
        history = self.gh.issue_history(number)
        reason = self._untrusted_change(history)
        if reason:
            raise UntrustedChange(f'issue #{number}: {reason}')
        # Defaults the supervisor accepted count as trusted comments (#182): read with REST, as
        # every other record of the dispatcher, so that its app's login is the one it signs with.
        listed = self.gh.issue_comments(number)
        accepted = [c for c in listed if self._accepted(c)]
        # ``request``: the hash of the request as read here (#182), part of the key of an
        # implement turn's retries. ``thread``: the comments as read here, where the conclave
        # records and the vetoes of the issue are (#142). ``decisions``: the comments that may
        # record a decision of the operator for the reviewers (#472), written before the takeover.
        issue = dict(self.gh.issue(number), title=history['title'], body=history['body'],
                     labeled=history['labeled'], admitters=self._admitters(history['author']),
                     ready_admitters=self._admitters(history['author'], coordinator=True),
                     accepted=accepted, request=self._request_hash(history, listed),
                     thread=listed, decisions=self._issue_decisions(listed, history, taken=True))
        return issue, _render_issue_comments(issue.get('comments') or [], self.cfg.trusted_users,
                                             [read_marker(c['body'], 'supervision')
                                              for c in accepted])

    # Ledger (#133) -----------------------------------------------------------
    def _resume_publications(self):
        """With ``publication_outbox`` (#385), settle the writes left pending in the outbox
        (``outbox.Outbox.resume``), before the tick reads GitHub: a restart resumes what was
        left half published, without duplicates. What cannot be settled is logged and waits;
        a new write on the same issue or PR settles it first, or is not sent."""
        outbox = getattr(self.identities, 'outbox', None)
        if outbox is None:
            return
        try:
            lines = outbox.resume()
        except GitHubError as exc:
            lines = [f'  outbox of the publications not readable: {exc}']
        for line in lines:
            self.log(line)

    def _signal_dropped(self):
        """With ``publication_outbox`` (#385), tell the operator of each write the outbox
        dropped (#397): never published, and never to be. Each is logged once per process; with
        ``[run].alert_issue`` the dispatcher also publishes one alert for those not reported
        yet, and the journal records them reported (``publication_signalled``): each is
        reported once. A failed alert is tried again at the next tick.

        The alert names in its publication the drops it reports (``OutboxClient.alert``): while
        it is pending, its outcome uncertain, they are not reported again, also after a
        restart; once the outbox finds it published, they are recorded reported
        (``Outbox.record_alerted``) without a second alert. Dropped itself, it reports nothing:
        its drops are reported again, with it."""
        outbox = getattr(self.identities, 'outbox', None)
        if outbox is None:
            return
        try:
            outbox.record_alerted()
        except GitHubError as exc:
            self.log(f'  the alert of the dropped publications is not recorded in the journal: '
                     f'{exc}')
        try:
            dropped = outbox.unsignalled()
        except GitHubError as exc:
            self.log(f'  dropped publications not readable: {exc}')
            return
        issue = self.cfg.run.alert_issue
        for entry in dropped:
            if entry['pub'] in self._drops_logged:
                continue
            self._drops_logged.add(entry['pub'])
            self.log(f'  WARNING: publication {entry["pub"]} ({outbox_describe(entry)}) dropped, '
                     f'never published: {entry.get("reason")}'
                     + ('' if issue else '; no [run].alert_issue is configured: only this log '
                                         'reports it'))
        if not issue or not dropped:
            return
        t = TEXTS.texts(self.cfg.language)
        listed = ''.join(t('drop_line', pub=e['pub'], what=outbox_describe(e), actor=e['actor'],
                           reason=e.get('reason')) for e in dropped[:MAX_DROPS_LISTED])
        more = len(dropped) - MAX_DROPS_LISTED
        if more > 0:
            listed += t('drops_more', more=more)
        body = publishable(dispatcher_header() + f'\n## {t("drops_title")}\n\n'
                           + t('drops_text', listed=listed))
        covers = [e['pub'] for e in dropped]
        try:
            self._alert(issue, body, covers)
        except Exception as exc:  # one attempt per tick: its class, never its output
            self._alert_failed('the alert of the dropped publications', issue, exc, covers)
            return
        try:
            outbox.signalled([e['pub'] for e in dropped])
        except GitHubError as exc:
            self.log(f'  the alert of the dropped publications is not recorded in the journal: '
                     f'{exc}')
        self.log(f'  alert of {len(dropped)} dropped publications published on #{issue}')

    def _watch_journal(self):
        """The size of the journal of the turns (#397): from ``WARN_RATIO`` of its limit
        (``journal_max_mib``), where nothing is refused yet, the operator is warned before the
        limit stops the turns. With ``[run].alert_issue``, one alert per threshold, recorded in
        the journal (``size_warning``); without it, a warning in the log at each start of
        devloop while the size stays beyond. The turns go on as usual; only beyond the limit
        is the journal refused (``journal.TurnJournal``)."""
        journal = getattr(self.ledger, 'journal', None)
        if journal is None or journal.read_only or self._journal_warned:
            return
        threshold = journal.warn_bytes()
        try:
            size = journal.size()
        except JournalError as exc:
            self.log(f'  {exc}')
            return
        if threshold is None or size < threshold:
            return
        issue = self.cfg.run.alert_issue
        percent = size * 100 // journal.max_bytes
        self.log(f'WARNING: the journal of the turns is {size} bytes, {percent}% of its limit of '
                 f'{journal.max_bytes} bytes (journal_max_mib): beyond it no turn starts'
                 + ('' if issue else '; no [run].alert_issue is configured: this warning is '
                                     'only in the log, at each start'))
        if not issue:
            self._journal_warned = True
            return
        key = f'journal_size:{threshold}'
        outbox = getattr(self.identities, 'outbox', None)
        try:
            if journal.size_warned(threshold):
                self._journal_warned = True
                return
            # With the outbox, an alert of this threshold names it in its publication: once
            # published it is recorded, never sent twice; while its outcome is uncertain it is
            # only reconciled, and a new one follows only if it turns out dropped.
            state = outbox.alerts().get(key) if outbox is not None else None
            if state == PUBLISHED:
                self._journal_warned = True
                journal.size_warning(size, threshold)
                self.log(f'  alert of the size of the journal found published on #{issue}: '
                         'recorded in the journal')
                return
            if state is not None:
                self.log(f'  the alert of the size of the journal on #{issue} has an uncertain '
                         'outcome: the outbox reconciles it, and no other alert is published '
                         'meanwhile')
                return
        except JournalError as exc:
            self.log(f'  the alert of the size of the journal cannot be checked: {exc}')
            self._journal_warned = True
            return
        t = TEXTS.texts(self.cfg.language)
        body = publishable(dispatcher_header() + f'\n## {t("journal_title")}\n\n'
                           + t('journal_text', size=size, percent=percent,
                               limit=journal.max_bytes))
        try:
            self._alert(issue, body, [key])
        except Exception as exc:  # one attempt per tick: its class, never its output
            self._alert_failed('the alert of the size of the journal', issue, exc, [key])
            return
        self._journal_warned = True
        try:
            journal.size_warning(size, threshold)
        except JournalError as exc:
            self.log(f'  the alert of the size of the journal is not recorded in it: {exc}')
        self.log(f'  alert of the size of the journal published on #{issue}')

    def _alert(self, issue, body, covers):
        """Publish an alert on ``issue`` (#397). With the outbox, its publication names what it
        reports, ``covers`` (``OutboxClient.alert``): an uncertain outcome is never followed by
        a second alert about the same drops or threshold. Without it, a comment as before."""
        alert = getattr(self.gh, 'alert', None)
        if getattr(self.identities, 'outbox', None) is None or not callable(alert):
            return self.gh.comment(issue, body)
        return alert(issue, body, covers=covers)

    def _alert_failed(self, what, issue, exc, covers):
        """Log an alert that raised ``exc``: tried again at the next tick when it certainly was
        not published; only reconciled by the outbox when its outcome is uncertain (its
        publication still pending covers ``covers``)."""
        outbox = getattr(self.identities, 'outbox', None)
        try:
            pending = outbox is not None and any(k in outbox.alerts() for k in covers)
        except GitHubError:
            pending = isinstance(exc, GitHubUncertain)
        if pending:
            self.log(f'  {what} on #{issue} has an uncertain outcome ({type(exc).__name__}): '
                     'the outbox reconciles it, and no other alert is published meanwhile')
        else:
            self.log(f'  {what} on #{issue} could not be published ({type(exc).__name__}): '
                     'tried again at the next tick')

    def _signed(self, login):
        """Whether ``login`` signs as the dispatcher, as its other records do."""
        return self._wrote(login, DISPATCHER)

    def _open_turn(self, where, candidate, mode, label, role=None):
        """The ledger record of a turn about to start, or None without a ledger. A failed write
        raises ``GitHubError``: the turn does not start. ``where``: ``(number, kind)``, with the
        round of a review turn (#325) or the ``FixRegister`` of a fix turn (#355) after them.
        ``role``: ``FINDINGS_ONLY`` for the turn of a findings-only review (#416), recorded.

        With ``block_uncertain_turns`` (#362) a turn of a work, the issue and its PRs, with
        turns that block it (``_check_turns``) is refused before its intent (``TurnBlocked``):
        the step escalates to the operator.

        With the budget on (#365, ``[budget] enabled``) the turn is booked in the envelope of
        its story in its intent, every fallback model and retry with a booking of its own, and
        a turn the envelope does not cover is refused before its intent (``BudgetRefused``):
        the step escalates (class ``budget``). A readiness check is outside the envelope."""
        if self.ledger is None or where is None:
            return None
        number, kind, *place = where
        fix = fix_register(where)
        issue = self._work_issue(number)
        self._check_turns(number)
        budget = self.cfg.budget
        reserve = None
        if budget.enabled and kind not in OUTSIDE:
            def reserve(journal):
                booking = budget_reserve(budget, journal, repo=self.cfg.repo, issue=issue,
                                         kind=kind, agent=candidate, language=self.cfg.language)
                self.log(f'  turn {kind} of {candidate.key} on #{number} booked in story '
                         f'{booking["story"]}')
                return booking
        try:
            return self.ledger.open_turn(
                number, kind=kind, agent=candidate.key, identity=self._app_login(candidate.key),
                mode=mode, model=candidate.model, effort=candidate.effort,
                fallback_for=candidate.fallback_for, label=label,
                round_no=place[0] if place and fix is None else None, register=fix,
                issue=issue, repo=self.cfg.repo, attempt=self._turn_attempt(number, issue, kind),
                reserve=reserve, measure=budget.enabled, **({'role': role} if role else {}))
        except BudgetRefused as exc:
            self.log(f'  turn refused on #{number}: not covered by the budget envelope '
                     f'({exc.category})')
            raise

    def _check_budget(self, number, kind, agent):
        """With the budget on (#365), raise ``BudgetRefused`` when the envelope of the story of
        the work of ``number`` would not cover a turn of ``kind`` of ``agent`` (``AgentConfig``)
        now: the check of the booking, read only, for a step that records something before its
        turn (a conclave call recorded as started) or that must not plan a turn already refused
        and escalated (the advisory review). The booking itself stays in ``_open_turn``, under
        the journal's lock: a check never makes an extension no turn used lapse (#466), only
        the refusal of a booking does. Without a ledger, or with the budget off, nothing."""
        budget = self.cfg.budget
        if self.ledger is None or not budget.enabled or kind in OUTSIDE:
            return
        try:
            budget_reserve(budget, self.ledger.journal, repo=self.cfg.repo,
                           issue=self._work_issue(number), kind=kind, agent=agent,
                           check_only=True, language=self.cfg.language)
        except BudgetRefused as exc:
            self.log(f'  turn {kind} refused on #{number}: not covered by the budget envelope '
                     f'({exc.category})')
            raise

    def budget_status(self, issues):
        """``devloop status`` (#393): the lines of the state of the budget of the stories of
        ``issues``, from the journal, with the reason the turns of an issue of no story are
        refused (``budget.no_story``, #396). Without a ledger, or with the budget off, none."""
        if self.ledger is None:
            return []
        try:
            return budget_status_lines(self.cfg.budget, self.ledger.journal, self.cfg.repo,
                                       issues)
        except JournalError as exc:
            return [f'budget: the journal cannot be read: {exc}']

    def _budget_view(self, issue):
        """With the budget on (#393), the state of the budget of the story of issue ``issue``
        for the PR receipt (``budget.view``), ``{'story': None}`` when the issue belongs to no
        story; None with the budget off or without a ledger."""
        if self.ledger is None or not self.cfg.budget.enabled:
            return None
        return (budget_of_issue(self.cfg.budget, self.ledger.journal, self.cfg.repo, issue)
                or {'story': None})

    # Attempts (#384)---------------------------------------------------------
    def _turn_attempt(self, number, issue, kind):
        """With ``attempt_identity`` (#384), the id of the attempt a turn of ``kind`` on
        ``number`` belongs to, of the work of issue ``issue``; None without one, as before.

        A turn on a PR belongs to the attempt of its marker, which the journal must confirm
        (``_attempt_problem``): an attempt it does not hold, of another issue or PR, or already
        concluded (a PR reopened after its outcome) raises ``JournalError``, and the step
        escalates. A PR opened before, without the attempt in its marker, has none, unless the
        journal ties it to one: then the field was removed, and the turn is refused as well. A
        turn on the issue (implementation, retries, fallbacks) belongs to its active attempt,
        opened by ``_take_attempt``. A readiness check belongs to none: it comes before the
        takeover."""
        if not self.cfg.attempt_identity or kind == 'preflight':
            return None
        journal = self.ledger.journal
        if number == issue:
            active = journal.active_attempt_of(self.cfg.repo, issue)
            return active['attempt'] if active is not None else None
        mark = self._pr_attempts.get(number)
        self._check_attempt(number, issue, mark)
        if mark is None:
            return None
        # Its PR not recorded yet (a write that failed after the PR was opened): recorded now.
        journal.bind_pr(mark['attempt'], number)
        return mark['attempt']

    def _check_attempt(self, number, issue, mark):
        """Raise ``JournalError`` when the attempt ``mark`` of PR ``number`` does not hold
        (#384, ``_attempt_problem``): before a turn, or a step that records something, on it."""
        problem = self._attempt_problem(number, issue, mark)
        if problem:
            self.log(f'  PR #{number}: {problem}')
            raise JournalError(problem)

    def _attempt_problem(self, number, issue, mark):
        """Why the attempt ``mark`` of the marker of PR ``number``, of issue ``issue``, does not
        hold (#384), in the project's language (#635); ``''`` when it holds and without ``attempt_identity``. The
        journal is the authority (ADR 0004, D7): the attempt must be one it holds, of the same
        story, phase, number, key and issue, active, and of this PR or of none yet.

        A PR whose marker has no attempt (opened before) is handled as today, unless the journal
        ties it to an attempt: the attempt that has it as its PR, or an active attempt of its
        issue without a recorded PR, whose PR this may be. Then the field was removed from the
        marker, or its absence cannot be told from that, and the PR does not hold either."""
        if not self.cfg.attempt_identity or self.ledger is None:
            return ''
        t = TEXTS.texts(self.cfg.language)
        try:
            attempts = self.ledger.journal.attempts()
        except JournalError as exc:
            return t('attempt_unreadable', error=exc)
        if mark is None:
            mine = [a for a in attempts.values() if a['repo'] == self.cfg.repo]
            bound = next((a for a in mine if a['pr'] == number), None)
            unbound = next((a for a in mine if a['issue'] == issue and a['pr'] is None
                            and a['outcome'] is None), None)
            if bound is None and unbound is None:
                return ''
            which = (t('attempt_bound', attempt=bound['attempt']) if bound is not None else
                     t('attempt_unbound', attempt=unbound['attempt'], issue=issue))
            return t('attempt_no_mark', which=which)
        current = attempts.get(mark['attempt'])
        if (current is None or current['repo'] != self.cfg.repo or current['issue'] != issue
                or any(current[k] != mark[k]
                       for k in ('story', 'phase', 'revision', 'n', 'design', 'task'))):
            return t('attempt_unconfirmed', attempt=mark['attempt'])
        if current['pr'] is not None and current['pr'] != number:
            return t('attempt_other_pr', attempt=mark['attempt'], pr=current['pr'])
        if current['outcome'] is not None:
            return t('attempt_concluded', attempt=mark['attempt'], outcome=current['outcome'])
        return ''

    def _conclude_attempt(self, attempt):
        """Record the outcome of ``attempt`` when its PR is merged (``merged``) or closed without
        merge (``closed``), as GitHub has it now (#384); whether it is concluded."""
        raw = self.gh.find_pr(attempt['pr'])
        state = (raw or {}).get('state')
        outcome = {'MERGED': MERGED, 'CLOSED': CLOSED}.get(state)
        if outcome is None:
            return False
        self.ledger.journal.conclude(attempt['attempt'], outcome, f'PR #{attempt["pr"]} {state}')
        self.log(f'  attempt {attempt["attempt"]} of #{attempt["issue"]} concluded: {outcome} '
                 f'(PR #{attempt["pr"]})')
        return True

    def _settle_attempts(self, snap, dry_run):
        """With ``attempt_identity`` (#384), record the outcome of each active attempt whose PR
        is no longer open: merged, or closed without merge. An attempt without a PR yet stays
        active, for the retries of its implementation."""
        if not self.cfg.attempt_identity or self.ledger is None or dry_run:
            return
        open_prs = {pr.number for pr in snap.prs}
        try:
            attempts = self.ledger.journal.attempts().values()
        except GitHubError as exc:
            self.log(f'  attempts not checked: {exc}')
            return
        for attempt in attempts:
            if (attempt['outcome'] is not None or attempt['pr'] is None
                    or attempt['pr'] in open_prs or attempt['repo'] != self.cfg.repo):
                continue
            try:
                self._conclude_attempt(attempt)
            except GitHubError as exc:
                self.log(f'  attempt {attempt["attempt"]} not checked: {exc}')

    def _take_attempt(self, number):
        """With ``attempt_identity`` (#384), the attempt of the implementation of issue
        ``number``, before its first turn: its active attempt when it has no PR yet (a retry
        after a failure before the PR), otherwise a new one, once the PR of the previous one is
        merged or closed and its outcome recorded. While that PR is open, ``AttemptActive``: a
        second active attempt in the same phase is refused, and the step escalates. None when
        the identity is off and for an issue that is not the root of a story (a child has no
        attempt of its own until step 3).

        An active attempt without a recorded PR is not proof that no PR exists: the write that
        records it may have failed after the PR was opened. Its PR is looked for first on its
        branch (``_attempt_pr``), recorded, and concluded if no longer open; when it cannot be
        told, ``JournalError``, and the step escalates instead of resuming."""
        if not self.cfg.attempt_identity or self.ledger is None:
            return None
        journal = self.ledger.journal
        found = journal.story_of(self.cfg.repo, number)
        if found is None or found[1] != 'root':
            return None
        story = found[0]['story']
        active = journal.active_attempt(story)
        if active is not None and active['pr'] is None:
            published = self._attempt_pr(active)
            if published is not None:
                self.log(f'  PR #{published} of attempt {active["attempt"]} recorded now')
                active = journal.bind_pr(active['attempt'], published)
        if active is not None and active['pr'] is not None and self._conclude_attempt(active):
            active = None
        if active is not None and active['pr'] is None:
            self.log(f'  attempt {active["attempt"]} (n. {active["n"]}) of #{number} resumed')
            return active
        attempt = journal.open_attempt(story, number)
        self.log(f'  attempt {attempt["attempt"]} (n. {attempt["n"]}) of #{number} opened')
        return attempt

    def _attempt_branch(self, attempt):
        """The branch of ``attempt`` (#384, ``attempt_branch``)."""
        return attempt_branch(self.cfg.branch_prefix, attempt['issue'], attempt['n'])

    def _attempt_pr(self, attempt):
        """The PR already published for ``attempt``, active and without a recorded PR (#384),
        in any state, or None when there is none: the devloop PR of its branch whose marker
        names it. A devloop PR of that branch opened since the attempt (its ``at``) whose
        marker does not name it may be its PR with the field removed: ``JournalError``, since
        resuming could publish a second PR of the same attempt. So does more than one PR."""
        named, unsure = [], []
        for raw in self.gh.branch_prs(self._attempt_branch(attempt)):
            pr = self._devloop_pr(dict(raw, headRefOid=raw.get('headRefOid') or ''))
            if pr is None or pr.issue != attempt['issue']:
                continue
            if (pr.meta.get('attempt') or {}).get('attempt') == attempt['attempt']:
                named.append(pr.number)
            elif (raw.get('createdAt') or '') >= (attempt.get('at') or ''):
                unsure.append(pr.number)
        if len(named) > 1 or unsure:
            prs = ', '.join(f'#{n}' for n in sorted(named + unsure))
            raise JournalError(
                f'attempt {attempt["attempt"]} has no PR recorded, and its branch has PRs that '
                f'may be its own ({prs}): it is not resumed until the operator decides')
        return named[0] if named else None

    def _do_attempt(self, pr):
        """A PR whose attempt does not hold (#384, ``_attempt_problem``): to the operator."""
        lb = self.cfg.labels
        self._escalate(pr.number, TEXTS.texts(self.cfg.language)(
                           'attempt_escalation', problem=pr.attempt_problem),
                       remove=[lb.review, lb.changes, lb.ready_to_merge], cls='attempt')

    def _work_issue(self, number):
        """The issue of the work of ``number`` (#362): the issue of a devloop PR, else
        ``number`` itself."""
        return self._issue_of.get(number, number)

    def _check_turns(self, number):
        """With ``block_uncertain_turns`` (#362), raise ``TurnBlocked`` when the work of
        ``number`` has turns that block it (``TurnJournal.blocking``): uncertain turns not
        reconciled, turns that ended without a durable outcome or that a tick could not make
        uncertain, and legacy turns of the issue or of its PRs, open or closed: those not linked
        to their work yet are linked first (``_link_legacy``), and one whose work cannot be read
        blocks every turn. Turns still running in this process, in parallel actions, never
        block. Without a ledger or the block, nothing."""
        if self.ledger is None or not self.cfg.block_uncertain_turns:
            return
        issue = self._work_issue(number)
        if self.ledger.journal.unlinked():
            self._link_legacy()
        held = self.ledger.journal.blocking(self.cfg.repo, issue, number, dict(self._issue_of))
        if held:
            self.log(f'  turn refused on #{number}: turns of issue #{issue} without a reconciled '
                     'outcome: ' + ', '.join(t['turn'] for t in held))
            raise TurnBlocked(blocked_text(issue, held, self.cfg.language), held)

    def _link_legacy(self):
        """Link each legacy turn imported from ``ledger-open-turns.json`` (#362) to the issue of
        its work, durably (``TurnJournal.link``): through the dispatcher's map of the open PRs
        and, with ``block_uncertain_turns``, by reading the others on GitHub in any state. A
        devloop PR, recognised by its branch and author as in the snapshot (``_devloop_pr``),
        belongs to the work of its issue; an issue, or a PR devloop does not recognise, only to
        its own number. A number that cannot be read stays unlinked, and blocks every new turn
        until a later read links it."""
        journal = self.ledger.journal
        found = {}
        for number in journal.unlinked():
            if number in self._issue_of:
                found[number] = self._issue_of[number]
                continue
            if not self.cfg.block_uncertain_turns:
                continue
            try:
                raw = self.gh.find_pr(number)
                pr = self._devloop_pr(dict(raw, headRefOid=raw.get('headRefOid') or '')) \
                    if raw is not None else None
            except GitHubError as exc:
                self.log(f'  legacy turn on #{number} not linked to its work: {exc}')
                continue
            found[number] = pr.issue if pr is not None else number
        if found:
            for turn in journal.link(found, self.cfg.repo):
                self.log(f'  legacy turn {turn} linked to the work of its issue')

    def _close_turn(self, entry, outcome, error=None):
        """Close a turn's record with what the CLI attested and the last command its output
        exposes (#215); for an agy turn stopped on a denied permission, the permission and the
        command agy reports (#479). A failed write leaves the turn open, and the next tick marks it
        incomplete. If its outcome cannot be written in the journal (#362), with
        ``block_uncertain_turns`` no other turn of its work starts (``_check_turns``): the next
        one of this step, a retry or a fallback, is refused and the step escalates."""
        if entry is None:
            return
        denied = error if isinstance(error, PermissionDenied) else None
        try:
            self.ledger.close_turn(entry, outcome, getattr(self.runner, 'last_report', None),
                                   error, getattr(self.runner, 'last_command', None),
                                   denied.command if denied else None,
                                   (denied.permission or 'non riportato') if denied else None)
        except GitHubError as exc:
            self.log(f'  turn record on #{entry.number} not closed: {exc}')

    def _local_tests(self, path):
        """``(command, record)`` of the local tests after a writing turn in ``path`` (#169):
        the command, None when nothing is to run, and the mode and modules, informative
        metadata for the log and the implement and fix markers. With ``changed`` the modules are
        computed again on every call, from the worktree as it is now."""
        mode = self.cfg.local_tests
        if mode == 'full':
            return self.cfg.test_cmd, {'mode': mode}
        if mode == 'none':
            return None, {'mode': mode, 'modules': []}
        modules = select_modules(self.ws.worktree_changes(path), self.ws.test_modules(path),
                                 self.cfg.tests)
        return (module_command(modules) if modules else None), {'mode': mode, 'modules': modules}

    def _run_tests(self, where, path, attempt, command, retried=None):
        """Run ``command``, recording it in the ledger with the tree tested; returns
        ``(passed, tail, entry)``. The test of a fix is a section of its register (#355).
        ``command`` may be a list of commands (#598): each runs with its own
        ``test_timeout_seconds``, stopping at the first that fails, whose tail is returned; the
        ledger records them joined by ``&&``. ``retried``: the failed tests that ``command`` runs
        again alone (#598), recorded."""
        commands = command if isinstance(command, list) else [command]
        if self.ledger is None or where is None:
            return (*self._run_each(path, commands), None)
        before = self.ws.tree(path)
        started = time.monotonic()
        passed, tail = self._run_each(path, commands)
        duration = int((time.monotonic() - started) * 1000)
        after = self.ws.tree(path)
        # The files the test changed (#627): a tracked bytecode rewritten, a cache created.
        # The commit that follows holds them, and is not the tree tested.
        changed = self.ws.tree_changes(path, before, after) if before != after else []
        if changed:
            self.log('  the tests changed ' + ', '.join(changed[:10])
                     + (f' and {len(changed) - 10} more' if len(changed) > 10 else ''))
        entry = self.ledger.verification(where[0], kind=where[1], attempt=attempt,
                                         command=' && '.join(commands), passed=passed,
                                         duration_ms=duration, tree_before=before,
                                         tree_after=after, register=fix_register(where),
                                         retried=retried, changed_by_tests=changed)
        return passed, tail, entry

    def _run_each(self, path, commands):
        """``(passed, tail)`` of ``commands`` run one after the other in ``path``, each with its
        own ``test_timeout_seconds``, stopping at the first that fails."""
        for command in commands:
            passed, tail = self.ws.run_tests(path, command, self.cfg.test_timeout_seconds)
            if not passed:
                break
        return passed, tail

    def _verify(self, where, path, attempt):
        """Run the local tests (``_local_tests``); returns ``(passed, tail, entry, record)``.
        Without tests to run it passes with no ledger record: the CI decides."""
        command, record = self._local_tests(path)
        if command is None:
            self.log(f'  local tests ({record["mode"]}): none to run, the CI decides')
            return True, '', None, record
        if record['mode'] != 'full':
            self.log(f'  local tests ({record["mode"]}): {", ".join(record["modules"])}')
        return (*self._run_tests(where, path, attempt, command), record)

    def _bind_tested(self, tested, path, commit):
        """Bind the passing test to ``commit`` when the committed tree is the one tested."""
        if tested is None:
            return
        try:
            if not self.ledger.bind_commit(tested, commit, self.ws.commit_tree(path, commit)):
                changed = changed_by_tests(tested)
                self.log(f'  the tree of {commit[:12]} is not the one tested: commit not bound'
                         + (f' (the tests changed {len(changed)} files)' if changed else ''))
        except GitHubError as exc:
            self.log(f'  verification record on #{tested.number} not updated: {exc}')

    def _generated(self, path, since, tested):
        """The files of the commit just made in ``path`` after commit ``since`` to point out
        (#627, ``generated_files``): those that look generated, the binary ones, and those the
        passing test ``tested`` changed. They stay in the commit; the log names how many."""
        files = generated_files(self.ws.changed_files(path, since=since),
                                changed_by_tests(tested), self.ws.binary_files(path, since))
        if files:
            self.log(f'  {len(files)} generated files or files changed by the tests in the '
                     'commit, pointed out')
        return files

    def _ledger_event(self, number, event, register=None, **fields):
        """An event of its own, or with ``register`` a section of that fix register (#355)."""
        if self.ledger is not None:
            self.ledger.event(number, event, register=register, **fields)

    def _fix_where(self, number, round_no):
        """The ``where`` of the turns and tests of a fix of PR ``number`` answering round
        ``round_no``: with a ledger they share one register (#355), its ``FixRegister`` last."""
        if self.ledger is None:
            return (number, 'fix')
        return (number, 'fix', self.ledger.fix_register(number, round_no))

    def _read_ci(self, head):
        """``(state, checks)`` of ``head``'s CI: with a ledger, from the same read that gives
        every check to record (``check_details``); without one, ``checks`` is None."""
        if self.ledger is None:
            return self.gh.checks(head)[0], None
        details = self.gh.check_details(head)
        return summarize_checks(details)[0], details

    def _record_ci(self, pr, head, state, details, round_no, jobs=None, voided_approval=False):
        """Record the CI of ``head`` as a ``ci_round`` event (#133), failed or passed: each
        check with its outcome, its duration in seconds (#300; None while it runs, and for a
        commit status) and, for a GitHub Actions run, its id (a commit status has none).
        ``jobs``, the summary the prompts got (``_ci_jobs``), adds ``tests``: for each job, the
        final lines of ``scripts/run-tests.py`` read from its log, or ``log: unavailable``
        when the log could not be read. Without ``jobs`` (a promotion after a review that saw
        the CI pending, a level-0 promotion) they are read now from ``details``, so that the
        event has them whatever path records it. The same checks read again for the same head
        and round are not recorded twice. The event of a review round is a section of that
        round's register (#325). ``voided_approval``: the failure joined a round that had
        approved the head, and voided that approval (#359); the event says so."""
        if self.ledger is None or details is None:
            return
        checks = [{'name': c['name'], 'outcome': c.get('conclusion') or c['bucket'],
                   'run_id': c.get('run_id'), 'duration_s': c.get('duration_s')}
                  for c in details]
        key = (head, state, round_no, checks)
        if key in pr.ci_recorded:
            return
        # A level-0 promotion has no review round.
        extra = {'round': round_no} if round_no is not None else {}
        if jobs is None:
            jobs = self._ci_jobs(details)
        tests = [dict({'name': j['name']}, **(j['tests'] if j.get('tests') else {'log': j['log']}))
                 for j in jobs or () if j.get('tests') or j.get('log')]
        if tests:
            extra['tests'] = tests
        if voided_approval:
            extra['voided_approval'] = True
        self.ledger.event(pr.number, 'ci_round', round_no=round_no, head=head, state=state,
                          **extra, checks=checks)
        pr.ci_recorded.append(key)

    def _flag_incomplete(self, snap, dry_run, busy=()):
        """Report the turns a previous tick opened and never closed, and mark them incomplete:
        no turn runs while a tick starts, since one dispatcher runs at a time, except on the
        numbers in ``busy``, those of the actions still running in continuous mode (#227):
        their turns are left alone, and checked once they end.

        It looks first where the ledger's trace (``Ledger.pending``) says a turn was opened and
        not closed, whatever labels the issue or PR has now: an issue whose PR is open or that
        was escalated, a PR in ``agent:needs-operator``. Then, in case that trace was lost, open
        devloop PRs that devloop still works on (``operator_held``), issues in
        ``agent:working`` and issues waiting for a readiness check. A number leaves the trace
        once checked.

        In the journal (#362), before reading GitHub, every turn left without an outcome on a
        traced number becomes ``uncertain``, logged with its id: it is never run again
        automatically, and with ``block_uncertain_turns`` it blocks the work until a trusted
        user reconciles it (``_reconcile_turns``). The number stays traced until its records
        on GitHub are checked. The legacy turns are linked to the issue of their work
        (``_link_legacy``)."""
        if self.ledger is None:
            return
        lb = self.cfg.labels
        try:
            traced = self.ledger.pending()
        except GitHubError as exc:
            self.log(f'  trace of the open turns not readable: {exc}')
            traced = []
        journal = self.ledger.journal
        if not dry_run:
            try:
                self._link_legacy()
            except GitHubError as exc:
                self.log(f'  legacy turns not linked to their issue: {exc}')
        for number in traced:
            if number in busy:
                continue
            try:
                left = [t for t in journal.turns().values()
                        if t['number'] == number and t['outcome'] is None]
                for turn in left:
                    self.log(f'uncertain turn {turn["turn"]} on #{number}: {turn["kind"]} by '
                             f'{turn["agent"]}, started {turn["at"]}, no outcome recorded')
                if left and not dry_run:
                    journal.uncertain(number)
            except GitHubError as exc:
                self.log(f'  turns of #{number} not marked uncertain: {exc}')
        numbers = list(traced)
        numbers += [pr.number for pr in snap.prs if not operator_held(pr, lb)]
        numbers += list(snap.working_issues)
        numbers += [i['number'] for i in snap.ready_issues if 'preflight' in i]
        for number in dict.fromkeys(numbers):
            if number in busy:
                continue
            try:
                for comment_id, record in open_turns(self.gh.issue_comments(number),
                                                     self._signed):
                    self.log(f'incomplete turn on #{number}: {record["kind"]} by '
                             f'{record["agent"]}, started {record["started_at"]}, never closed')
                    if not dry_run:
                        self.ledger.flag_incomplete(number, comment_id, record)
                if number in traced and not dry_run:
                    self.ledger.settle(number)
            except GitHubError as exc:
                self.log(f'  turn records of #{number} not checked: {exc}')

    def _reconcile_turns(self, snap, dry_run):
        """Apply the ``/devloop reconcile`` commands of trusted users (#362) to the uncertain
        turns not reconciled yet, whether or not ``block_uncertain_turns`` is on. They are read
        on the issue or PR of each such turn, on the issue of its work and on that issue's open
        PRs. A command of anyone else is ignored and logged; one that does not apply is
        refused and answered once."""
        if self.ledger is None:
            return
        journal = self.ledger.journal
        try:
            waiting = journal.waiting()
        except GitHubError as exc:
            self.log(f'  journal of the turns not readable: {exc}')
            return
        numbers = []
        for turn in waiting:
            issue = turn['issue'] if turn.get('issue') is not None else self._work_issue(
                turn['number'])
            numbers += [turn['number'], issue] + [pr.number for pr in snap.prs
                                                  if pr.issue == issue]
        for number in dict.fromkeys(numbers):
            try:
                for comment in self.gh.issue_comments(number):
                    self._reconcile_comment(number, comment, dry_run)
            except GitHubError as exc:
                self.log(f'  reconciliations on #{number} not checked: {exc}')

    def _reconcile_comment(self, number, comment, dry_run):
        """Apply the ``/devloop reconcile`` command of ``comment`` on ``number``, if any."""
        body = comment.get('body') or ''
        if comment.get('native') or carries_record(body):
            return
        command = reconcile_command(body)
        ident = comment_ref(comment)
        if command is None or not isinstance(ident, int) or isinstance(ident, bool):
            return
        journal = self.ledger.journal
        if journal.handled(ident):
            return
        login = (comment.get('author') or {}).get('login')
        if login not in self.cfg.trusted_users:
            self.log(f'  #{number}: ignored `{RECONCILE_COMMAND}` by {login}: the author is not '
                     'a trusted user')
            return
        turn, outcome, note = command
        # The answer and its reasons in the project's language (#633).
        language = self.cfg.language
        t = JOURNAL_TEXTS.texts(language)
        try:
            if turn is None:
                raise ValueError(t('reconcile_first_line'))
            if outcome is None:
                raise ValueError(t('reconcile_outcome'))
            if dry_run:
                self.log(f'[dry-run] #{number}: {login} reconciles {turn} as {outcome}')
                return
            applied = journal.reconcile(turn, outcome, by=login, comment=ident, note=note,
                                        language=language)
        except ValueError as exc:
            self.log(f'  #{number}: `{RECONCILE_COMMAND}` of comment {ident} by {login} refused: '
                     f'{exc}')
            if not dry_run:
                journal.refuse(ident, login, turn, str(exc))
                self.gh.comment(number, publishable(
                    dispatcher_header() + f'\n## {t("reconcile_title")}\n\n'
                    + t('reconcile_refused', command=RECONCILE_COMMAND, comment=ident, by=login,
                        why=exc)))
            return
        self.log(f'  #{number}: {turn} reconciled as {outcome} by {login} (comment {ident})'
                 if applied else f'  #{number}: {turn} already reconciled as {outcome}')

    def _permission_denied(self, candidate, exc, retry):
        """Log an agy turn stopped on a denied permission (``PermissionDenied``, #180) with the
        permission and its target, preceded by the command when agy reports it (#479). Returns
        None when ``retry``, the turn being run once more; otherwise the error to escalate,
        which names the permission denied again, the model and what was denied: the command, or
        ``command not reported by agy`` (#319)."""
        target = exc.target or 'target not reported by agy'
        if exc.command:
            target = f'command `{exc.command}`; {target}'
        if retry:
            self.log(f'  {candidate.key}: model {candidate.model} denied the '
                     f'"{exc.permission}" permission ({target}); retrying once')
            return None
        self.log(f'  {candidate.key}: model {candidate.model} denied the '
                 f'"{exc.permission}" permission again ({target})')
        return PermissionDenied(f'{exc}; permission `{exc.permission}` denied again after one '
                                f'retry; model `{candidate.model}`; {exc.denied()}', exc.log,
                                exc.permission, exc.target, exc.command)

    def _remember_denied(self, agent, candidate, exc):
        """Record ``candidate``, denied a permission twice, as unreliable for the headless turns
        of ``agent`` for ``denied_cooldown_hours`` (#319), in the memory of unavailable models.
        Returns the text the log line adds; an agent without fallback models records nothing."""
        if self.memory is None or not agent.fallback_models:
            return ''
        until = self.memory.now() + agent.denied_cooldown_hours * 3600
        reason = f'permission `{exc.permission}` denied twice ({exc.denied()})'
        try:
            entry = self.memory.record(candidate.key, candidate.model, reason, until)
        except OSError as error:
            return f'; not remembered: {error}'
        return f'; skipped until {entry["until"]}'

    # Memory of unavailable models (#151) -----------------------------------
    def _remembered(self, models, candidate):
        """The memory record that makes ``candidate`` skipped until it expires, or None: only
        for an agent with fallback models. Every remembered model is skipped, the last of the
        chain too: a chain remembered whole runs no CLI."""
        if self.memory is None or not models.agent.fallback_models:
            return None
        return self.memory.active(candidate.key, candidate.model)

    def _remember(self, agent, candidate, exc):
        """Record ``candidate`` unavailable for ``agent``, until the reset the error announces
        (``exc.resets_at``) or ``fallback_memory_seconds`` from now. Returns the text the log
        line adds; an agent without fallback models records nothing."""
        if self.memory is None or not agent.fallback_models:
            return ''
        until = exc.resets_at
        if until is None:
            until = self.memory.now() + agent.fallback_memory_seconds
        try:
            entry = self.memory.record(candidate.key, candidate.model, exc.reason, until)
        except OSError as error:
            return f'; not remembered: {error}'
        return f'; skipped until {entry["until"]}'

    def _forget(self, agent, candidate):
        """Forget ``candidate`` once it answered: it is available again."""
        if self.memory is None or not agent.fallback_models:
            return
        try:
            self.memory.forget(candidate.key, candidate.model)
        except OSError as error:
            self.log(f'  {candidate.key}: model {candidate.model} not forgotten: {error}')

    def _turn(self, models, mode, prompt, schema, path, label, allowed=None, where=None,
              note=None, role=None):
        """Run one agent turn on the first of ``models`` (``Models``) that is available, and
        return its output and the agent configuration it ran with.

        A model whose turn fails as unavailable (``ModelUnavailable``) leaves ``models``, so the
        action never tries it again, and the next one runs with the same prompt and worktree,
        plus a note about the partial work: ``FALLBACK_NOTE[note]``, ``note`` being ``mode`` by
        default (``preflight`` for the readiness check, #148). Every
        other failure ends the turn as before, listing
        the models tried or left out if any (``ModelsUnavailable`` with ``last_failed``). A model
        that ``allowed`` refuses leaves ``models`` without running. When none is left, an agent
        without fallback models fails as before #121; one with them fails listing the models
        tried and why (``ModelsUnavailable``).

        An agy turn stopped on a permission headless mode cannot ask (``PermissionDenied``) is a
        failure of the tool (#180): the same model runs the same turn once more, in the same
        directory, with ``-retry`` in its label, and a prompt that says what was denied and which
        commands it may run (#319). After a second denial on the same model the next model of
        the chain runs, as after an unavailable one, and the denied model is remembered for
        ``denied_cooldown_hours`` (#319); on the last model it fails as any other error, naming
        the permission, the model and what was denied.

        An agy turn that ended with no result and no error (``NoOutput``, #222) leaves
        ``models`` as an unavailable model does, without being remembered: the next model runs.
        On the last model of the chain the same model runs once more, with ``-retry`` in its
        label; a second empty run fails as any other error.

        ``where``, ``(number, kind)``, is the issue or PR whose ledger records each CLI
        invocation as a turn (#133): written before the CLI starts, closed when it ends; with
        ``role`` (``FINDINGS_ONLY``, #416) the record says the turn is a findings-only review. An
        exception that is not an ``AgentError`` closes it too, ``failure``, and propagates
        (#381): a turn is never left ``open`` by an error of the dispatcher. A turn stopped
        because its result is no longer needed (``TurnCancelled``, #521) is closed
        ``cancelled`` and propagates at once: no other model of the chain runs.

        With a memory (#151), an agent with fallback models records each model found
        unavailable, and skips a remembered one until its record expires, without running it:
        it leaves ``models`` and is listed in ``models.skipped`` and on the configuration
        returned (``skipped``). When every model of the chain is remembered, no CLI runs and
        the turn fails listing each one with its reason and time (``ModelsUnavailable``). A
        model that answers is forgotten."""
        failed = None
        retried = {}  # the models already denied a permission in this call: their denial
        emptied = set()  # the last models of the chain already rerun after no output (#222)
        while models.left:
            candidate = models.left.pop(0)
            remembered = self._remembered(models, candidate)
            if remembered is not None:
                models.skipped.append((candidate.model, remembered['reason'],
                                       remembered['until']))
                models.tried.append((candidate.model, 'skipped: unavailable until '
                                                      f'{remembered["until"]} '
                                                      f'({remembered["reason"]})'))
                self.log(f'  {candidate.key}: model {candidate.model} skipped: unavailable until '
                         f'{remembered["until"]} ({remembered["reason"]})')
                continue
            if allowed is not None and not allowed(candidate):
                models.tried.append((candidate.model, 'not independent of the authors of the PR '
                                                      'or of the other reviewers of the round'))
                self.log(f'  {candidate.key}: model {candidate.model} skipped: not independent')
                continue
            text = prompt + FALLBACK_NOTE[note or mode] if failed else prompt
            if candidate.model in retried:  # what was denied, and what is allowed (#319)
                text += denied_retry_note(candidate, mode, retried[candidate.model])
            suffix = f'-{candidate.model}' if candidate.fallback_for else ''
            # Its own log: the first attempt's stays as it was.
            suffix += '-retry' * ((candidate.model in retried) + (candidate.model in emptied))
            entry = self._open_turn(where, candidate, mode, label + suffix, role)
            try:
                output = self.runner.run(candidate, mode, text, schema, path, label + suffix)
            except TurnCancelled as exc:
                # No longer needed (#521): no other model runs, and the turn is not a failure.
                self._close_turn(entry, 'cancelled', exc)
                raise
            except ModelUnavailable as exc:
                self._close_turn(entry, 'unavailable', exc)
                models.tried.append((candidate.model, f'{exc} ({exc.reason})'))
                self.log(f'  {candidate.key}: model {candidate.model} unavailable: {exc.reason}'
                         + self._remember(models.agent, candidate, exc))
                failed = exc
                continue
            except NoOutput as exc:
                self._close_turn(entry, 'failure', exc)
                if models.left:
                    # Not usable for this turn, maybe only for a moment: the next model runs,
                    # and nothing is remembered (#222).
                    models.tried.append((candidate.model, str(exc)))
                    self.log(f'  {candidate.key}: model {candidate.model} returned no output; '
                             f'trying {models.left[0].model}')
                    failed = exc
                    continue
                if candidate.model not in emptied:  # the last model of the chain: once more
                    emptied.add(candidate.model)
                    models.left.insert(0, candidate)
                    self.log(f'  {candidate.key}: model {candidate.model} returned no output; '
                             f'trying {candidate.model} again')
                    continue
                self.log(f'  {candidate.key}: model {candidate.model} returned no output again')
                error = AgentError(f'{exc}; no output again after one retry', exc.log)
                if not models.tried:
                    raise error from exc
                models.tried.append((candidate.model, str(error)))
                raise ModelsUnavailable(models.agent.key, models.tried, exc.log,
                                        last_failed=True) from exc
            except PermissionDenied as exc:
                self._close_turn(entry, 'failure', exc)
                error = self._permission_denied(candidate, exc,
                                                retry=candidate.model not in retried)
                if error is None:
                    retried[candidate.model] = exc
                    models.left.insert(0, candidate)
                    continue
                if models.left:
                    # Denied twice: the model ignores the commands it may run. The next model
                    # runs, as after an unavailable one, and this one is remembered (#319).
                    models.tried.append((candidate.model, str(error)))
                    self.log(f'  {candidate.key}: model {candidate.model} denied twice; trying '
                             f'{models.left[0].model}'
                             + self._remember_denied(models.agent, candidate, exc))
                    failed = error
                    continue
                if not models.tried:
                    raise error from exc
                models.tried.append((candidate.model, str(error)))
                raise ModelsUnavailable(models.agent.key, models.tried, exc.log,
                                        last_failed=True) from exc
            except AgentError as exc:
                self._close_turn(entry, 'failure', exc)
                if not models.tried:
                    raise
                # Any other failure ends the turn as before, but after models were tried or left
                # out the escalation lists them all, this one included.
                models.tried.append((candidate.model, str(exc)))
                error = ModelsUnavailable(models.agent.key, models.tried, exc.log,
                                          last_failed=True)
                error.tail, error.timed_out = exc.tail, exc.timed_out
                raise error from exc
            except Exception as exc:  # not the agent's failure: the turn is closed all the same
                self._close_turn(entry, 'failure', f'{type(exc).__name__}: {exc}')
                raise
            self._close_turn(entry, 'success')
            models.left.insert(0, candidate)  # available: later attempts of the action use it
            self._forget(models.agent, candidate)
            if candidate.fallback_for:
                self.log(f'  {candidate.key} ran on fallback model {candidate.model} '
                         f'({candidate.model_label}) instead of {candidate.fallback_for}')
            if models.skipped:
                candidate = replace(candidate, skipped=tuple(models.skipped))
            return output, candidate
        if failed is not None and not models.agent.fallback_models:
            raise failed  # an agent without fallback models: as before #121
        raise ModelsUnavailable(models.agent.key, models.tried, getattr(failed, 'log', None))

    def _mapped_docs(self, path):
        """``{document: [paths]}`` of ``[docs] map`` for the diff of the worktree ``path``
        (#203): the same diff as the local tests (``worktree_changes``, #169), read now. A
        document missing from that worktree is left out (``_missing_docs``, #210)."""
        if not self.cfg.docs.entries:
            return {}
        found = self.cfg.docs.documents(self.ws.worktree_changes(path))
        missing = self._missing_docs(path, found)
        return {d: paths for d, paths in found.items() if d not in missing}

    def _mapped_entries(self, path):
        """The entries of ``[docs] map`` for the first implement prompt, without the documents
        missing from the worktree ``path`` (#210), and without the keys left with none."""
        entries = self.cfg.docs.entries
        missing = self._missing_docs(path, [d for _, documents in entries for d in documents])
        kept = ((key, tuple(d for d in documents if d not in missing))
                for key, documents in entries)
        return tuple((key, documents) for key, documents in kept if documents)

    def _missing_docs(self, path, documents):
        """The ``documents`` of ``[docs] map`` that are not a regular file of the worktree
        ``path``, one log line each (#210): the configuration only checks the map's shape, so a
        prompt omits them instead of failing."""
        missing = set(self.ws.missing_files(path, sorted(set(documents)))) if documents else set()
        for document in sorted(missing):
            self.log(f'  docs.map: {document} is not a file of the worktree; omitted from the '
                     'prompt')
        return missing

    def _write_turn(self, agent, prompt, schema, path, label, where=None, base_check=False):
        """Run a writing agent, then the local tests (``_verify``); retry with the failures.
        With ``base_check`` (an implementation or a fix, #459) a failure whose failed tests all
        fail on the base commit of the branch too is not retried: the reason to escalate is then
        a ``BaseBroken`` (``dispatch_base``), and the step waits for the base branch to advance;
        a ``HostTests`` when they pass in the CI of that commit (#497), escalated to the operator.
        Neither holds when the failed tests pass once run again alone on the branch (#598,
        ``_retry_alone``): then the turn succeeds, with the record of that retry as the passing
        test.
        ``prompt`` is the text, or a function of the attempt (0 first) that renders it, so that
        each attempt gets what its worktree holds then (#203).
        Returns the output, ``None`` on success or a reason to escalate, the agent configuration
        of the last turn (``_turn``), the ledger record of the passing test, to bind to the
        commit that follows (None without a ledger or without a passing test), and the mode and
        modules of the local tests of the last attempt (None when no test was due: a blocked
        turn, or an implementation without changes). A model found
        unavailable is not tried again in later attempts.

        An implementation or a fix (``KEEPS_WORK``) whose turn times out with uncommitted work
        saves it as evidence (``_keep_timed_out``), never committed, for the escalation and the
        retry of the same step (#374): that retry, before its first turn, applies it to its new
        worktree, or passes it in the prompt (``_resume_kept``)."""
        models = Models(agent)
        failures = ''
        keep = schema in KEEPS_WORK
        start = self.ws.head(path) if keep else None
        resumed = self._resume_kept(path, label) if keep else ''
        for attempt in range(1 + self.cfg.implement_test_retries):
            text = (prompt(attempt) if callable(prompt) else prompt) + resumed + failures
            try:
                result, used = self._turn(models, 'write', text, schema, path,
                                          f'{label}-{attempt + 1}', where=where)
            except AgentError as exc:
                if keep and exc.timed_out:
                    self._keep_timed_out(exc, path, start, label)
                raise
            if result['status'] == 'blocked':
                if background_unfinished(result.get('blocked_reason')):
                    # The turn ended before its own background work (#219): a failure of the
                    # tool, retryable, not a block of the change.
                    raise AgentError(f'{used.key} stopped while background work was still '
                                     f'running: {result["blocked_reason"].strip()}')
                return result, None, used, None, None
            if not self.ws.has_changes(path) and schema == 'implement':
                return result, ('Implementazione senza modifiche ai file.', ''), used, None, None
            passed, tail, tested, local = self._verify(where, path, attempt + 1)
            if passed:
                return result, None, used, tested, local
            broken = self._base_broken(path, tail) if base_check else None
            if broken is not None:
                # Before the failure of the host or of the base, its tests once again alone
                # (#598): when they pass they are unstable under load, and the step goes on as
                # after passing tests. A base_broken retried here is a host_tests that its wait
                # may escalate later, when the CI of the base turns green (``_do_host_tests``).
                passed, tested = self._retry_alone(where, path, attempt + 1, broken)
                if passed:
                    return result, None, used, tested, local
            if broken is not None:
                return result, broken, used, None, local
            failures += ('\n\n## Test failures after your previous attempt\n\nThe test command '
                         f'failed. Fix the cause, then finish again.\n\n```text\n{tail}\n```\n')
        return (result, (TEXTS.texts(self.cfg.language)('tests_failed'), tail),
                used, None, local)

    def _keep_timed_out(self, exc, path, start, label):
        """Save what the timed-out turn ``exc`` of step ``label`` left uncommitted in ``path``
        beyond commit ``start`` (#374), as ``kept_work_file(label)`` in the logs, and record it
        on ``exc`` (``kept_work``: file and stats) for the escalation. Nothing is committed. A
        failure to save is logged and leaves the escalation as it was."""
        file = kept_work_file(label)
        try:
            kept = self.ws.keep_work(path, start, file)
        except (WorkspaceError, OSError, UnicodeError) as error:
            self.log(f'  uncommitted work of the timed-out turn not saved: {error}')
            return
        if kept is None:
            return
        exc.kept_work = {'file': file, 'stats': kept[1]}
        self.log(f'  uncommitted work of the timed-out turn saved as {file}, not committed')

    def _resume_kept(self, path, label):
        """The prompt note of the retry of step ``label`` (#374), '' when its previous turn left
        no saved work. The saved patch is taken once (``take_work``), as bytes, and applied as
        it is to the new worktree ``path``; when it does not apply, the note carries it as
        text."""
        try:
            patch = self.ws.take_work(kept_work_file(label))
        except (OSError, UnicodeError) as error:
            self.log(f'  work saved by the timed-out turn not read: {error}')
            return ''
        if not patch:
            return ''
        try:
            applied, error = self.ws.apply_work(path, patch)
        except WorkspaceError as exc:
            applied, error = False, str(exc)
        try:
            stats = self.ws.patch_stats(path, patch)
        except WorkspaceError:
            stats = []
        self.log(f'  work saved by the timed-out turn {"reapplied" if applied else "not applied"}'
                 f' ({len(stats)} files)' + ('' if applied else f': {error}'))
        return kept_work_note(self.cfg.agent_timeout_seconds, stats, applied, error, patch)

    def _commit_message(self, agent, message, issue):
        message = message.strip() + f'\n\nRefs #{issue}\n'
        if agent.commit_trailer:
            message += f'\n{agent.commit_trailer}\n'
        return message

    def _blocked(self, number, agent, reason, remove, on_pr, step=None, head=None,
                 conclave=None):
        """An agent that stopped as ``blocked`` in ``step``, which started from commit
        ``head``: its notice, written with its identity, then ``agent:needs-operator``. The
        marker records the agent, the step, the commit and the reason, the block a conclave
        (#141) works on, and in ``conclave`` the comment of the conclave decision whose
        resolution the step had (#142): such a block convenes no other conclave."""
        author = self._as(agent.key, PR_COMMENT if on_pr else ISSUE_COMMENT)
        payload = {'event': 'blocked', 'class': 'blocked', 'critical': True, 'to': 'operator',
                   'agent': agent.key, 'reason': reason.strip()}
        if step:
            payload.update(step=step, head=head)
        if conclave is not None:
            payload['conclave'] = conclave
        language = self.cfg.language
        author.comment(number, publishable(
            self.runner.header(agent) + f'\n## {TEXTS.texts(language)("blocked_title")}\n\n'
            + reason.strip() + '\n\n' + resume_text(language), 'notice', payload))
        self.gh.edit_labels(number, add=[self.cfg.labels.needs_operator], remove=remove)

    def _protected_changes(self, path, since=None):
        """Changed files under ``protected_paths``, which devloop never pushes."""
        return [f for f in self.ws.changed_files(path, since)
                if any(f.startswith(prefix) for prefix in self.cfg.protected_paths)]

    def _escalate_protected(self, number, files, remove):
        listed = '\n'.join(f'- `{f}`' for f in files[:20])
        self._escalate(number, TEXTS.texts(self.cfg.language)('protected') + '\n\n' + listed,
                       remove=remove, cls='protected_paths')

    # Review policy -----------------------------------------------------------
    def _profile(self, key, mode, used=None):
        """The identity and tuple agent ``key`` acts with now in ``mode`` (``write`` or
        ``review``), as markers record them. ``used``: the configuration a turn actually ran
        with, on a fallback model (#121), instead of the configured one."""
        return agent_profile(used or self.cfg.agent(key), self._app_login(key), mode)

    def _authors(self, pr):
        """The profiles of who wrote code on the PR, or may write it next, or None when a
        marker does not record who wrote the code: the implementer, every fixer and every
        agent that resolved conflicts in a sync, as their markers record them, and the agent
        configured now under the implementer's key, which runs fixes and syncs, with each of
        its models (#121)."""
        authors = [profile_of(pr.meta), *map(profile_of, pr.fixes + pr.syncs)]
        if None in authors:
            return None
        implementer = pr.meta['implementer']
        if implementer in self.cfg.agents:
            authors += [self._profile(implementer, 'write', used)
                        for used in self.cfg.agent(implementer).chain()]
        return authors

    def _approvals_match(self, pr, records, current, paths, step='promotion'):
        """The base checks the approvals ``records`` rely on (``[]`` when none was needed) when
        every reviewer ``current`` requires approved with a receipt that matches it, an earlier
        base admitted only if the base branch left the PR's files alone since (#160).
        Otherwise None, after recording why (``stale``) and sending the PR back to review for
        a new review of the same head. ``step``: the promotion, or the merge (#172)."""
        lb = self.cfg.labels
        changed = [k for k in BINDING
                   if any(k in differences(receipt_of(r), current) for r in records)]
        # An approval given on an earlier base holds only if no commit of the base branch since
        # then reached the PR's files (#160).
        bases = self._base_checks(records, current, paths) if changed == ['base'] else []
        if bases and all(c['outcome'] == 'disjoint' for c in bases):
            changed = []
        # A needs_operator for the trust policy without findings of its own approves the head
        # (#434): the merge that follows is the operator's (``trust_policy_parts``).
        approved = {owner_of(r) for r in records if flow_verdict(r) == 'approve'}
        missing = [key for key in current['reviewers'] if key not in approved]
        branch, language = self.cfg.base_branch, self.cfg.language
        t = TEXTS.texts(language)
        if changed or missing:
            what = (t('stale_no_receipt') if any(receipt_of(r) is None for r in records)
                    else t('stale_changed', fields=', '.join(t(f'field_{k}') for k in changed))
                    if changed
                    else t('stale_missing', keys=', '.join(f'`{k}`' for k in missing)))
            detail = (t('stale_bases', branch=branch)
                      + _describe_bases(bases, branch, language)) if bases else ''
            payload = {'event': 'stale', 'receipt': current, 'bases': bases or []}
            # The new review of this same head does not count against the limit when only
            # the base revoked the approval (#252); any other revocation opens a round as usual.
            rounds = t('stale_round_free'
                       if records and base_revocation(payload, records[0]['head_sha'])
                       else 'stale_round')
            step = 'merge' if step == 'merge' else 'promotion'
            self._policy_comment(
                pr, t(f'stale_title_{step}'), t(f'stale_text_{step}', what=what) + rounds
                + detail, current, 'stale', bases)
            self.gh.edit_labels(pr.number, add=[lb.review],
                                remove=[lb.changes, lb.ready_to_merge, *self._released(pr)])
            # A receipt of an earlier promotion no longer holds (#135).
            self._supersede_receipts(pr.number, self._merge_texts()('supersede_stale'))
            return None
        return bases

    def _ci_closes(self, pr, head, findings):
        """Whether the CI of ``head`` may still close ``findings`` (#334): all of them are CI
        findings and that CI, read now, is still running, has not appeared yet within the wait
        of a just pushed head (#263), or has passed since the snapshot, while the review ran:
        the next tick records its closures. A CI already concluded without passing closes
        nothing, and neither does any other owner's finding."""
        if not findings or any(f['owner'] != CI for f in findings):
            return False
        state = self._read_ci(head)[0]
        return state in ('pending', 'pass') or (state == 'none' and bool(pr.ci_wait))

    def _apply_round(self, pr, records, announce, reviews, tested=False):
        """Apply a complete round, ``records`` (one per reviewer). ``reviews``: every review of
        the PR up to the round included. Every finding still open counts here, whoever owns
        it: an approval does not close another owner's finding. Only CI findings, with the CI
        of the approved head still running or passed since the snapshot, wait for that CI
        instead (#334). ``tested``: the
        full suite passed locally on the head after a review that did not know its CI
        (#271)."""
        lb = self.cfg.labels
        transient = [lb.review, lb.changes, lb.ready_to_merge]
        verdict = round_verdict(records)
        if verdict != 'approve' and lb.ready_to_merge in pr.labels:
            # A CI round on a ready PR (#248) voids the receipt of its promotion (#135).
            self._supersede_receipts(pr.number, self._merge_texts()(
                'supersede_round', round=records[0]['round'], head=records[0]['head_sha'][:12]))
        if verdict == 'approve':
            left = open_findings(reviews, pr.closures)
            if left and self._ci_closes(pr, records[0]['head_sha'], left):
                # Only CI findings, and the CI of the approved head still running or passed
                # during the review (#334): the approval stays recorded and waits in review.
                # Passed, the CI closes them and the approval is applied without a new review;
                # failed, a new CI round opens.
                self.log(f'  not promoted: CI findings wait for the CI of '
                         f'{records[0]["head_sha"][:12]}')
            elif left:
                # Owners outside the round (CI, an earlier reviewer): no agent closes them.
                self._escalate(pr.number, _open_findings_reason(left, self.cfg.language),
                               remove=transient, cls='open_findings')
            elif self._approval_current(pr, records[0],
                                        blind=not tested and ci_unknown(records)):
                self._promote(pr, records, reviews)
        elif verdict == 'needs_operator':
            if announce:
                # The reviewer's record asks for the decision; this one classifies it (#182).
                self.gh.comment(pr.number, render_escalation(
                    TEXTS.texts(self.cfg.language)('reviewer_decision'),
                    cls='reviewer_decision', language=self.cfg.language))
            self.gh.edit_labels(pr.number, add=[lb.needs_operator], remove=transient)
        elif verdict == UNPROVEN:
            # #481: a reviewer's request for changes had no evidence. Nothing to fix, and no
            # explicit approval for the quorum: the PR stays in review, and the next tick plans
            # a new review of the head, or the round limit.
            self.log(f'  not promoted: round {records[0]["round"]} has no explicit approval of '
                     'every reviewer (unproven)')
            self.gh.edit_labels(pr.number, add=[lb.review],
                                remove=[lb.changes, lb.ready_to_merge, *self._released(pr)])
        elif (counted_rounds(reviews, **waiver(self.cfg, pr.fixes, pr.closures))
              >= self.cfg.max_review_rounds + pr.granted_rounds):
            limit = self.cfg.max_review_rounds + pr.granted_rounds
            # The next round may be the supervisor's to grant (#405); else the operator's.
            route, why = self._round_grant(pr, reviews) if announce else (None, '')
            reason = round_limit_reason(
                TEXTS.texts(self.cfg.language)('round_limit_open', limit=limit), reviews,
                pr.fixes, pr.syncs, limit, pr.granted_rounds, grantable=route is not None,
                trusted=self.cfg.trusted_users, cap=self.cfg.max_requested_fix_rounds,
                closures=pr.closures, language=self.cfg.language)
            if route is not None:
                self._escalate(pr.number, reason, remove=transient, cls=ROUND_GRANTABLE,
                               supervised=route)
                return
            if announce:
                if why:
                    reason += '\n\n' + SUPERVISION_TEXTS.texts(self.cfg.language)('why_round',
                                                                                  why=why)
                # The head and the label to resume, for a trusted user's grant-round (#553).
                self.gh.comment(pr.number, render_escalation(
                    reason, cls='round_limit',
                    extra={'head': pr.head_sha, 'resume': self._round_resume(pr)},
                    language=self.cfg.language))
            self.gh.edit_labels(pr.number, add=[lb.needs_operator], remove=transient)
        else:
            # A CI failure on a ready PR (#248): the dispatcher's own operator label goes too.
            self.gh.edit_labels(pr.number, add=[lb.changes],
                                remove=[lb.review, lb.ready_to_merge, *self._released(pr)])

    # Actions -------------------------------------------------------------
    def _do_operator_merge(self, pr):
        """Tell that the merge of a ready PR is the operator's (#248), with one comment per
        head: an ``operator_merge`` notice that says why, then ``agent:needs-operator``. A head
        promoted again, after its readiness decayed (its CI ran again, for example), edits the
        notice it already has, with the current promotion and count of ``labeled`` events,
        instead of writing a second one. The PR is read again first: a head, a state or labels
        changed since the snapshot leave it to the next tick, and so does a merge mode that now
        lets devloop merge it. Notice first: a label that cannot be added fails the step, which
        escalates the PR."""
        lb = self.cfg.labels
        fresh = self.gh.pr_state(pr.number)
        labels = label_names(fresh.get('labels') or [])
        if fresh.get('state') != 'OPEN' or (fresh.get('headRefOid') or '') != pr.head_sha:
            self.log('  head or state changed since the snapshot; next tick')
            return
        if lb.ready_to_merge not in labels or operator_blocked(labels, lb):
            self.log('  labels changed: ' + ', '.join(sorted(labels)))
            return
        reasons = self._operator_merge_reasons(pr)
        if not reasons:
            return
        labeled = len(self._operator_labelers(pr.number))
        body = render_operator_merge(pr.head_sha, labeled, pr.promotions, reasons,
                                     lb.needs_operator, self.cfg.language)
        if pr.operator_notice is None:
            self.gh.comment(pr.number, body)
        else:
            self.gh.edit_comment(pr.operator_notice, body)
            self.log(f'  promoted again: updated the notice of {pr.head_sha[:12]}')
        self.gh.edit_labels(pr.number, add=[lb.needs_operator])
        self.log(f'  the merge of {pr.head_sha[:12]} is the operator\'s')

    def _operator_merge_reasons(self, pr):
        """Why devloop does not merge the ready ``pr`` (#248), as Markdown lines: ``[merge]``
        off, a level ``off`` in ``[merge.levels]``, the operator-only paths its diff touches,
        the trust policy its reviewers say it touches (#434), with the parts they name.
        ``''`` (logged) when the head moved or devloop may merge it."""
        merge, t = self.cfg.merge, TEXTS.texts(self.cfg.language)
        trust = _trust_policy_lines(t, trust_policy_parts(pr.reviews))
        if not merge.enabled():
            return '\n'.join([*trust, t('merge_off')])
        receipt, paths = self._current_receipt(pr)
        if receipt is None:
            return ''
        lines = list(trust)
        reserved = sorted(p for p in paths if merge.operator_only(p))
        if reserved:
            shown = ', '.join(f'`{p}`' for p in reserved[:MAX_LISTED_PATHS])
            more = (t('reserved_more', more=len(reserved) - MAX_LISTED_PATHS)
                    if len(reserved) > MAX_LISTED_PATHS else '')
            lines.append(t('reserved_paths', shown=shown, more=more))
        level = receipt['level']
        if any(lvl == level and mode == 'off' for lvl, mode in merge.levels):
            lines.append(t('level_off', level=level))
        unblocked = self._human_merge(pr)
        if unblocked is not None:
            lines.append(t('conclave_unblocked', comment=unblocked))
        if not lines:
            self.log(f'  merge mode {merge.mode_for(level, paths)}: devloop may merge this PR')
        return '\n'.join(lines)

    def _do_stale(self, number):
        lb = self.cfg.labels
        self._escalate(number, TEXTS.texts(self.cfg.language)('interrupted'),
                       remove=[lb.working], cls='interrupted')

    def _do_settle(self, number):
        self.gh.edit_labels(number, remove=[self.cfg.labels.working])

    def _do_reconcile(self, pr):
        self.gh.edit_labels(pr.number, add=[self.cfg.labels.review],
                            remove=[self.cfg.labels.changes])

    def _do_findings(self, pr):
        """Record the closures of open findings: those trusted users asked for, and the CI
        findings once CI passes on the PR's current head."""
        t = TEXTS.texts(self.cfg.language)
        closed = list(pr.commands)
        ci = [f for f in open_findings(pr.reviews, pr.closures) if f['owner'] == CI]
        if ci and pr.checks == 'pass':
            if self.gh.pr_head(pr.number) != pr.head_sha:
                self.log('  head moved since the snapshot; CI findings stay open')
            else:
                closed += [{'key': f['key'], 'status': 'resolved', 'by': CI,
                            'author': self._app_login(DISPATCHER) or 'devloop dispatcher',
                            'reason': t('ci_green', head=pr.head_sha[:12]),
                            'head_sha': pr.head_sha} for f in ci]
        if not closed:
            return
        lines = []
        for c in closed:
            who = (t('closed_by_ci') if c['by'] == CI
                   else t('closed_by_operator', author=c['author']))
            lines.append(t('closed_line', finding=c['key'], status=t(f'closed_{c["status"]}'),
                           who=who, reason=' '.join(c['reason'].split())))
        self.gh.comment(pr.number, publishable(
            dispatcher_header() + f'\n## {t("findings_title")}\n\n' + '\n'.join(lines)
            + '\n\n' + t('findings_text'), 'finding', {'closed': closed}))
        pr.closures = pr.closures + closed
        pr.commands = []

    def _do_unattributable(self, pr):
        lb = self.cfg.labels
        self._escalate(pr.number, TEXTS.texts(self.cfg.language)('unattributable')
                       + '\n'.join(f'- {p}' for p in pr.finding_problems),
                       remove=[lb.review, lb.changes, lb.ready_to_merge], cls='unattributable')

    def _do_lost_meta(self, pr):
        lb = self.cfg.labels
        self._escalate(pr.number, TEXTS.texts(self.cfg.language)('lost_meta', issue=pr.issue),
                       remove=[lb.review, lb.changes, lb.ready_to_merge], cls='lost_meta')

    def _do_ci_missing(self, pr):
        """A head without CI, past ``[ci] required_timeout_minutes``, on a base branch that
        requires checks (#263): to the operator, unless the head moved or some CI appeared."""
        lb = self.cfg.labels
        if self.gh.pr_head(pr.number) != pr.head_sha:
            self.log('  head moved since the snapshot; retrying next tick')
            return
        if self.gh.checks(pr.head_sha)[0] != 'none':
            self.log(f'  CI appeared on {pr.head_sha[:12]}; retrying next tick')
            return
        names = ', '.join(f'`{name}`' for name in pr.ci_missing)
        self._escalate(pr.number, TEXTS.texts(self.cfg.language)(
                           'ci_missing', head=pr.head_sha[:12],
                           minutes=self.cfg.ci.required_timeout_minutes,
                           branch=self.cfg.base_branch, names=names),
                       remove=[lb.review], cls='ci_missing')

    def _do_round_limit(self, pr):
        """Escalate ``pr`` at its round limit: ``max_review_rounds`` plus the rounds the
        supervisor granted. To the supervisor as ``round_grantable`` when it may grant the next
        round (#405, ``_round_grant``), otherwise to the operator as ``round_limit``, with why
        the delegation does not apply when it is on."""
        lb = self.cfg.labels
        limit = self.cfg.max_review_rounds + pr.granted_rounds
        route, why = self._round_grant(pr, pr.reviews)
        t = TEXTS.texts(self.cfg.language)
        who = t('round_limit_both' if route else 'round_limit_operator')
        self._escalate(pr.number, round_limit_reason(
            t('round_limit', limit=limit, who=who), pr.reviews, pr.fixes,
            pr.syncs, limit, pr.granted_rounds, grantable=route is not None,
            trusted=self.cfg.trusted_users, cap=self.cfg.max_requested_fix_rounds,
            closures=pr.closures, language=self.cfg.language),
            remove=[lb.review, lb.changes, lb.ready_to_merge],
            cls=ROUND_GRANTABLE if route else 'round_limit', supervised=route, why=why,
            # The head and the label to resume, for a trusted user's grant-round (#553).
            extra=None if route else {'head': pr.head_sha, 'resume': self._round_resume(pr)})

    def _do_apply_review(self, pr):
        """Apply the round already recorded. An approval given while the CI was not known
        (#271), on a head that got no CI past the window of #263 and with ``local_tests`` other
        than ``full``, gets the full suite locally first, as a review would have before it."""
        records = current_round(pr.reviews)
        tested = False
        if (round_verdict(records) == 'approve' and pr.checks == 'none'
                and self.cfg.local_tests != 'full' and ci_unknown(records)):
            name = f'pr-{pr.number}'
            path = self.ws.existing_branch(name, pr.head_ref)
            if self.ws.head(path) != pr.head_sha:
                self.log('  head moved since the snapshot; retrying next tick')
                return
            if not self._full_suite_without_ci(pr, path, name, approved=True):
                return
            self.ws.remove(name)
            tested = True
        self._apply_round(pr, records, announce=False, reviews=pr.reviews, tested=tested)


MAX_TRUST_REASON = 2000


def _trust_policy_lines(t, parts):
    """The reason of an operator merge for the trust policy (#434), as Markdown lines in the
    language of ``t`` (#635), from ``trust_policy_parts``: empty without parts."""
    if not parts:
        return []
    lines = [t('trust_intro')]
    for reviewer, round_no, reason in parts:
        text = reason or t('trust_no_parts')
        if len(text) > MAX_TRUST_REASON:
            # The whole text is in the reviewer's review; the notice stays within GitHub's limit.
            text = text[:MAX_TRUST_REASON].rstrip() + ' […]'
        lines.append(f'  - `{reviewer}`, round {round_no}: '
                     + text.replace('\n', '\n    '))
    return lines


def ensure_labels(gh: Tracker, labels, review_policy=None, language=ENGLISH):
    """Create or update devloop's labels, their descriptions in ``language`` (#635), and return
    what the transition of #248 did, for the log ('' when nothing): see
    ``transition_labels``."""
    t = TEXTS.texts(language)
    transition, note = transition_labels(gh, labels, language)
    for attr, (color, description) in label_styles(language).items():
        if attr == 'needs_operator' and transition == 'both':
            continue
        gh.ensure_label(getattr(labels, attr), color, description)
    if review_policy is not None:
        gh.ensure_label(review_policy.label, LEVEL_LABEL_COLOR, t('label_level'))
        if review_policy.zero_enabled():
            gh.ensure_label(review_policy.zero_label, ZERO_LABEL_COLOR, t('label_zero'))
    for name, color in PRIORITY_LABEL_COLORS.items():
        gh.ensure_label(name, color, t('label_' + name.replace(':', '_')))
    return note
