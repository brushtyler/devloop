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
either. A sync whose conflicts an agent resolved changes the PR's content: its review counts.
Nor does the new review of an approved head whose approval a dispatcher ``stale`` policy record
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
the title, the body and the trusted comments, and reused while that hash holds. An issue that is
not ready goes to agent:needs-operator; the check removes that label only if it added it itself.

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

Supervisor labels (#178): with ``roles.supervisor`` and its GitHub App, ``agent:ready`` and the
priority labels count also when the supervisor's app added them, but only on issues opened by a
trusted user or by one of devloop's apps (``label_admitters``). On anyone else's issue only a
trusted user's label counts. The issue text stays trusted only while no one outside
``trusted_users`` edits it after the label.

Parallel actions (#176, ``max_parallel_actions`` above 1): the actions of a tick on different
issues and PRs run at once, up to that many (``schedule``). Actions on the same number, or on a
PR and its issue, run one after the other in plan order; a global action (``GLOBAL_ACTIONS``)
runs alone. Each log line carries the number of its action (``TickLog``), a failed action does
not stop the others, and a review action at level 2 runs together every reviewer its round
still waits for.

Continuous mode (#227, ``devloop run`` with ``max_parallel_actions`` above 1, ``Workers``): no
barrier at the end of a tick. A tick plans and starts actions in the free slots, without waiting
for them; the loop runs the next tick as soon as one ends, or as news on GitHub arrive while a
slot is free. The issues and PRs of the running actions are left out of the plan (``plan``'s
``busy``), so no two actions on the same number overlap, and a global action starts only when
nothing runs, alone (``admit``). ``devloop tick`` keeps the barrier.

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
(``_do_supervise``). Critical and unknown classes always go to agent:needs-operator.

Requested fixes (#195): ``/devloop fix`` from a trusted user or the supervisor's identity, on a
PR in agent:review or agent:changes, runs a fix of the current head with the rest of the comment
as a requirement, without a review and without a round (``_do_requested_fix``). One such fix per
head: a later command on the same head counts only after a new commit (``_fix_requests``).

CI of a new head (#263, ``[ci]``): with ``local_tests`` other than ``full``, a PR in agent:review
whose head has no CI is not reviewed for ``grace_minutes`` after its push, as if its CI were
pending (``_ci_wait``): the push the dispatcher recorded, else the commit date. Then the full
suite runs locally, as before, unless the base branch requires checks: the review waits for
them, and after ``required_timeout_minutes`` the PR is escalated (``ci_missing``).
"""

import hashlib
import itertools
import json
import string
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from .acceptance import (acceptance_criteria, acceptance_findings, format_criteria, format_map,
                         latest_map, render_map)
from .agents import (TAIL_LINES, AgentError, ModelsUnavailable, ModelUnavailable, NoOutput,
                     PermissionDenied, StartFailure, background_unfinished)
from .apps import DISPATCHER, IMPLEMENT, ISSUE_COMMENT, MERGER, PR_COMMENT, RERUN, REVIEW
from .config import INSTALL_ROOT, LEGACY_NEEDS_OPERATOR, ConfigError
from .github import GitHubError, author_login, job_id
from .ledger import entries as ledger_entries
from .ledger import open_turns
from .localtests import failed_modules, module_command, select_modules
from .markdown import (RESUME, carries_record, dispatcher_header, publishable, read_marker,
                       render_cycle, render_escalation, render_fix, render_native_review,
                       render_operator_merge,
                       render_preflight, render_review, render_supervision)
from .pin import PinError, Repinned, SmokeError
from .policy import describe as describe_receipt
from .policy import (BINDING, MAX_LISTED_PATHS, ZERO_LEVEL, agent_profile, changes_requested,
                     check_outcome, check_states, differences, distinct, evaluate, independent,
                     profile_of, receipt_of, recorded_level, zero_refused)
from .ports import Backend, Tracker, summarize_checks
from .review import (CI, FINDING_COMMAND, OPERATOR, ci_failure_review, current_round,
                     finding_commands, finding_key, finding_state, last_round, normalize_review,
                     open_findings, owner_of, round_verdict)
from .supervision import (ACCEPT_DEFAULTS, DECIDE, DESIGN_LABEL, ESCALATE, ISSUE_STEPS,
                          MAX_RETRIES, MERGED_ISSUE, PREFLIGHT_DEFAULT, RETRY, RETRY_STEPS,
                          SYNC_FAILED, TOOL_FAILURE, critical, preflight_class, retries_done,
                          supervisor_command)
from .workspace import WorkspaceError

PROMPT_DIR = Path(__file__).resolve().parent / 'prompts'
REREVIEW_COMMAND = '/devloop rereview'
RECHECK_COMMAND = '/devloop recheck'
# Hold and release a merge in ``auto`` mode (#172): the first line of a comment, after the
# attribution header, from the supervisor's identity or a trusted user.
HOLD_COMMAND = '/devloop hold'
RELEASE_COMMAND = '/devloop release'
# A fix asked by a trusted user or the supervisor (#195): the first line of a comment, after the
# attribution header; the rest of the comment is the request.
FIX_COMMAND = '/devloop fix'
# The modes in which devloop merges (#172).
MERGING = ('strict', 'auto')
# Why a review round does not count (#235): the round before approved a head that the new one
# differs from only by clean merges of the base branch.
RESYNC_AFTER_APPROVAL = 'resync_after_approval'
# And (#252): the round before approved the very head the new one reviews again, after the
# dispatcher revoked that approval because the base branch touched the PR's files.
BASE_CHANGED = 'base_changed'
# Actions that take a slot of ``max_actions_per_tick``.
EXPENSIVE = ('review', 'fix', 'implement', 'sync', 'preflight', 'merge', 'supervise')
# Actions that write to the base branch or to the whole repository (the merge of #172, the pin
# of #174): with ``max_parallel_actions`` above 1 each runs alone, after the actions planned
# before it and before those planned after it (#176).
GLOBAL_ACTIONS = frozenset({'merge', 'pin'})
LABEL_STYLE = {
    'ready': ('0e8a16', 'devloop: pronta per essere implementata da un agent'),
    'working': ('fbca04', 'devloop: implementazione in corso'),
    'review': ('1d76db', 'devloop: in attesa di review automatica'),
    'changes': ('d93f0b', 'devloop: modifiche richieste dalla review'),
    'ready_to_merge': ('0e8a16', 'devloop: approvata, in attesa del merge'),
    'needs_operator': ('b60205', 'devloop: serve una decisione dell\'operatore'),
    'merge_hold': ('5319e7', 'devloop: merge trattenuto dal supervisore o dall\'operatore'),
    'needs_supervisor': ('d876e3', 'devloop: escalation non critica, decide il supervisore'),
}
# The label a PR resumes from when the supervisor retries a step that failed on it (#182).
RESUME_STEP = {'review': 'review', 'fix': 'changes', 'sync': 'review'}
LEVEL_LABEL_STYLE = ('5319e7', 'devloop: la PR richiede una review di livello 2')
# Priority labels of ready issues (#146) and their rank: the lower, the earlier an issue starts.
PRIORITY_LABELS = {'priority:high': 'high', 'priority:normal': 'normal', 'priority:low': 'low'}
PRIORITY_RANK = {'high': 0, 'normal': 1, 'low': 2}
# How many prerequisites outside the queue a snapshot reads at most to follow the dependencies
# of the ready issues (#277): beyond them, cycles are not looked for.
MAX_DEPENDENCY_READS = 50
PRIORITY_LABEL_STYLE = {
    'priority:high': ('b60205', 'devloop: priorità alta, parte prima delle altre issue pronte'),
    'priority:normal': ('c2e0c6', 'devloop: priorità normale (implicita)'),
    'priority:low': ('c5def5', 'devloop: priorità bassa, parte dopo le altre issue pronte'),
}
ZERO_LABEL_STYLE = ('c5def5', 'devloop: livello 0, nessuna review se il diff è nell\'allowlist')
_RECEIPT_FIELDS = {'pr': 'PR', 'base': 'commit di base', 'head': 'head',
                   'policy': 'versione della policy', 'level': 'livello',
                   'reviewers': 'reviewer richiesti',
                   'profiles': 'identità, modello, effort o argomenti dei reviewer'}
_CLOSED_TEXT = {'resolved': 'risolto', 'withdrawn': 'ritirato'}
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
}


@dataclass
class PullRequest:
    number: int
    issue: int
    head_ref: str
    head_sha: str
    title: str
    body: str
    labels: set
    meta: dict
    reviews: list = field(default_factory=list)
    fixes: list = field(default_factory=list)
    # Conflict resolutions of syncs (``sync`` markers), with who resolved them (#111).
    syncs: list = field(default_factory=list)
    checks: str = 'none'
    failed_checks: list = field(default_factory=list)
    failed_runs: list = field(default_factory=list)
    # Comments trusted users wrote in person after the latest review record, and whether one of
    # them asks for a new review of the same head (``REREVIEW_COMMAND``).
    operator_comments: list = field(default_factory=list)
    rereview: bool = False
    # The same, written after the latest review of an earlier round: what a reviewer joining the
    # latest round sees, as the round's first reviewer did.
    round_comments: list = field(default_factory=list)
    merge_state: str = 'clean'  # clean, conflict, unknown (see ports.CodeReview.merge_state)
    # Policy records the dispatcher wrote (``policy`` markers), and whether one written after
    # the latest review found that the approval's receipt no longer matches.
    policy: list = field(default_factory=list)
    stale_receipt: bool = False
    # The latest of those records found stale after the latest review (#252), None if none.
    revoked: dict = None
    # The head a level-0 promotion made ready (#107), when it is the latest record of the PR.
    zero_head: str = None
    # Why that promotion no longer holds, as the snapshot found re-checking it (``''``: it holds).
    zero_problem: str = ''
    # Findings (#110): closures the dispatcher recorded (``finding`` markers), closures asked by
    # trusted users' commands and not recorded yet, and why some finding has no owner.
    closures: list = field(default_factory=list)
    commands: list = field(default_factory=list)
    finding_problems: list = field(default_factory=list)
    # CI events the dispatcher's ledger already recorded (#133), as ``(head, state, round,
    # checks)``: reading the same checks again for the same head and round records nothing new.
    ci_recorded: list = field(default_factory=list)
    # With a ledger, every check of the head from the read that gave ``checks`` (#133): the CI
    # a review round saw, recorded whatever its verdict. None when not read.
    check_details: list = None
    # Why the acceptance map does not hold for an approval of the head that would be reused or
    # kept (#134), as the snapshot found re-checking it: the titles of the findings a review
    # would add. Empty when it holds or there is no such approval.
    acceptance: list = field(default_factory=list)
    # Merge (#172): GitHub's ``mergeable`` and ``mergeStateStatus`` as read, the hold or release
    # command still to apply (``{event, by, reason}``), and the merge mode of a PR in
    # agent:ready-to-merge (None when it is not, the mode is not computable or merging is off;
    # whether it may be merged now is ``merge_ready``).
    mergeable: str = None
    merge_status: str = None
    hold: dict = None
    merge_mode: str = None
    # Fixes asked with ``FIX_COMMAND`` and not run yet (#195), oldest first, as
    # ``{comment, by, request}``: one fix of the current head answers all of them.
    fix_requests: list = field(default_factory=list)
    # In agent:review, when the latest round approved an earlier head and the head differs from
    # it only by clean merges of the base branch (#235), or approved this very head and the
    # dispatcher then revoked the approval because the base touched the PR (#252):
    # ``{reason, round, from}``, what the next review records as ``resync``. None otherwise.
    resync: dict = None
    # Re-runs of failed CI jobs the dispatcher recorded (``rerun`` markers, #200), and whether
    # the CI still shows the very failed checks a re-run of the head replaces: the re-run has
    # not started yet, and the failure is not a new one.
    reruns: list = field(default_factory=list)
    rerun_waiting: bool = False
    # In agent:review, a head without CI that the review waits for (#263): why, as the log says
    # it ('' when it does not wait), and the checks the base branch requires that are still
    # missing after ``required_timeout_minutes`` (empty unless the PR is to escalate).
    ci_wait: str = ''
    ci_missing: list = field(default_factory=list)
    # Operator merge (#248): the heads whose ``operator_merge`` notice belongs to the current
    # promotion, the comment id of the notice of the current head (one per head, edited when
    # the head is promoted again), how many promotions the policy records count, and whether
    # ``agent:needs-operator`` is the dispatcher's own for that notice
    # (``Dispatcher._operator_merge_owned``): then it does not stop devloop, and goes when the
    # readiness decays.
    operator_merges: list = field(default_factory=list)
    operator_notice: int = None
    promotions: int = 0
    operator_owned: bool = False


def _timestamp(value):
    """Seconds since the epoch of an ISO 8601 date with its zone, as GitHub writes them, or
    None when ``value`` is not one."""
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment.timestamp() if moment.tzinfo is not None else None


def operator_blocked(labels, lb):
    """Whether ``labels`` hand an issue or a PR to the operator: ``agent:needs-operator`` or
    its name before #248 (``Labels.operator``)."""
    return bool(set(labels) & lb.operator())


def operator_held(pr, lb):
    """Whether devloop leaves ``pr`` to the operator: an operator label that is not the
    dispatcher's own label of an operator merge (#248)."""
    return operator_blocked(pr.labels, lb) and not pr.operator_owned


def operator_merge_due(pr, cfg):
    """Whether a ready PR whose merge is the operator's (#248) still lacks its
    ``operator_merge`` notice for the current head: ``[merge]`` off, or the PR's own mode off
    (a level ``off``, an operator-only path). A PR whose mode is not known is left as it is."""
    if cfg.labels.ready_to_merge not in pr.labels or pr.head_sha in pr.operator_merges:
        return False
    return (not cfg.merge.enabled()
            or pr.merge_mode is not None and pr.merge_mode not in MERGING)


def rerun_of(reruns, head):
    """The re-run recorded for ``head`` among ``reruns`` (#200), or None: one per head."""
    return next((r for r in reruns if r['head'] == head), None)


def rerun_waiting(record, details):
    """Whether the checks ``details`` of a head still fail with exactly the checks its re-run
    ``record`` replaces (#200): a re-run job is a new check run, with a new link."""
    failed = {c.get('link') for c in details if c.get('bucket') == 'fail'}
    return bool(failed) and failed <= set(record['checks'])


@dataclass
class Snapshot:
    prs: list
    # dicts: number, title, labels (set), labeler and ready_at (login and time of the latest
    # ``labeled`` event of agent:ready), admitted (whether that labeler may admit the issue,
    # ``label_admitters``), priority (``issue_priority``), and with ``preflight``
    # configured, for the issues that may start or are blocked by the check, ``preflight``
    # (``_preflight_view``). For those same issues, ``merged``: the numbers of the merged
    # devloop PRs that resolved them (#228), which ``plan`` never starts again. For those not
    # resolved, their native dependencies (#277, ``Dispatcher._dependency_view``): ``blocked_by``
    # (the open issues that block them, as ``issue_ref``), ``blocks`` (the numbers of those ready
    # issues they block), ``cycle`` (the issues of the dependency cycle they are on, empty
    # without one) and ``cycle_reported`` (whether the dispatcher already reported that cycle).
    ready_issues: list
    working_issues: list  # issue numbers
    # Issues and PRs in agent:needs-supervisor with a supervisor command to apply (#182):
    # dicts ``{number, command, by, step, pr}`` (``_supervisor_view``); a retry of an issue's
    # step has ``blocked_by`` too, its open blocks (#277), and waits while it is not empty.
    supervised: list = field(default_factory=list)


@dataclass(frozen=True)
class Action:
    kind: str
    number: int
    reason: str = ''


def schedule(actions, issue_of):
    """How ``max_parallel_actions`` above 1 runs ``actions`` (#176): a list of phases, run one
    after the other, each a list of lanes that may run at once, each lane a list of
    ``(position in actions, action)`` to run in that order.

    Actions on the same number share a lane, in plan order, and so do an action on a PR and one
    on its issue (``issue_of``: PR number to issue number). A global action
    (``GLOBAL_ACTIONS``) is a phase of its own: it starts after every action planned before it
    has ended, and those planned after it start once it has ended."""
    phases, batch = [], []
    for position, action in enumerate(actions):
        if action.kind in GLOBAL_ACTIONS:
            if batch:
                phases.append(_lanes(batch, issue_of))
            phases.append([[(position, action)]])
            batch = []
        else:
            batch.append((position, action))
    if batch:
        phases.append(_lanes(batch, issue_of))
    return phases


def _lanes(batch, issue_of):
    lanes = []  # [numbers, entries]
    for position, action in batch:
        numbers = {action.number}
        if action.number in issue_of:
            numbers.add(issue_of[action.number])
        joined = [lane for lane in lanes if lane[0] & numbers]
        lane = joined[0] if joined else [set(), []]
        if not joined:
            lanes.append(lane)
        for other in joined[1:]:
            lane[0] |= other[0]
            lane[1] += other[1]
            lanes.remove(other)
        lane[0] |= numbers
        lane[1].append((position, action))
        lane[1].sort(key=lambda entry: entry[0])
    return [entries for _, entries in lanes]


def numbers_of(number, issue_of):
    """The numbers an action on ``number`` touches: ``number``, and its issue when it is a PR
    (``issue_of``: PR number to issue number)."""
    return {number, issue_of[number]} if number in issue_of else {number}


def admit(actions, issue_of, running, slots):
    """Continuous mode (#227): the actions of a plan to start now, in plan order, while
    ``running`` actions still run and ``slots`` places are free.

    A global action (``GLOBAL_ACTIONS``) starts only when nothing runs, alone and before the
    others; while one is planned and something runs, nothing starts, so that the running actions
    end and it runs next. While a global action runs, nothing starts either. Of several actions
    on the same number, or on a PR and its issue, only the first starts: the next plan, which
    sees its result, decides the others."""
    if any(action.kind in GLOBAL_ACTIONS for action, _ in running):
        return []
    first = next((a for a in actions if a.kind in GLOBAL_ACTIONS), None)
    if first is not None:
        return [] if running else [first]
    chosen, taken = [], set()
    for action in actions:
        if len(chosen) >= slots:
            break
        numbers = numbers_of(action.number, issue_of)
        if numbers & taken:
            continue
        taken |= numbers
        chosen.append(action)
    return chosen


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


def label_names(labels):
    return {label['name'] if isinstance(label, dict) else label for label in labels}


def label_admitters(author, trusted_users, supervisor=None, devloop_logins=()):
    """The logins whose ``agent:ready`` and priority labels count on an issue opened by
    ``author`` (#178): the trusted users, and ``supervisor`` (the login of the supervisor's
    app, None without one) only when the author is a trusted user or one of
    ``devloop_logins``, the logins of devloop's apps."""
    admitters = set(trusted_users)
    if supervisor and author and (author in admitters or author in devloop_logins):
        admitters.add(supervisor)
    return admitters


def issue_priority(labels, events, trusted_users, priority_labels=()):
    """The priority of a ready issue (#146): the highest among the priority labels it has now
    whose latest ``labeled`` event comes from ``trusted_users``, the logins that may set it
    (``label_admitters``), ``normal`` without any. Labels in
    ``priority_labels`` count as ``high``. ``events`` are the issue's ``labeled`` events, oldest
    first, as ``{label, by, at}``."""
    names = dict(PRIORITY_LABELS, **{name: 'high' for name in priority_labels})
    added_by = {event['label']: event['by'] for event in events}
    valid = [names[name] for name in labels
             if name in names and added_by.get(name) in trusted_users]
    return min(valid, key=PRIORITY_RANK.__getitem__, default='normal')


def start_order(issue):
    """Sort key of the ready issues (#146): priority, then (#277) an issue that blocks other
    ready issues before one that blocks none, then the oldest current agent:ready, number."""
    return (PRIORITY_RANK[issue.get('priority', 'normal')], 0 if issue.get('blocks') else 1,
            issue.get('ready_at') or '', issue['number'])


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


def queue_lines(snapshot):
    """The ready issues in start order, with priority and agent:ready time, as ``status`` and
    ``tick --dry-run`` show them, and their dependencies (#277): the open issues that block
    them, the ready issues they block, the cycle they are on. Which of them start, and when, is
    up to ``plan``."""
    def dependencies(issue):
        text = ''
        if issue.get('blocked_by'):
            text += ', blocked by ' + ', '.join(issue['blocked_by'])
        if issue.get('blocks'):
            text += ', blocks ' + ', '.join(f'#{n}' for n in issue['blocks'])
        if issue.get('cycle'):
            text += ', dependency cycle ' + ', '.join(issue['cycle'])
        return text

    return [f'ready {position}. #{issue["number"]} priority={issue.get("priority", "normal")} '
            f'agent:ready={issue.get("ready_at") or "?"} labelled by {issue["labeler"]}'
            f'{dependencies(issue)}: {issue["title"]}'
            for position, issue in enumerate(sorted(snapshot.ready_issues, key=start_order), 1)]


def zero_labels_hold(pr, rp):
    """Whether a level-0 promotion of ``pr`` still holds as far as its head and labels show:
    the promoted head is the current one, level 0 is enabled, its label is there and the level-2
    label is not."""
    return (pr.zero_head == pr.head_sha and rp.zero_enabled()
            and rp.zero_label in pr.labels and rp.label not in pr.labels)


def merge_command(body):
    """The hold or release command of a comment (#172): ``(event, reason)`` when the first line
    after the attribution header is ``/devloop hold <reason>`` or ``/devloop release``,
    ``(event, None)`` for a hold without a reason, None when the comment is no such command."""
    lines = [line.strip() for line in (body or '').strip().splitlines()]
    while lines and (not lines[0] or lines[0].startswith('>')):
        lines.pop(0)
    tokens = lines[0].split(None, 2) if lines else []
    if len(tokens) < 2 or tokens[0] != '/devloop' or tokens[1] not in ('hold', 'release'):
        return None
    reason = tokens[2].strip() if len(tokens) > 2 else ''
    if tokens[1] == 'release':
        return 'release', reason
    return 'hold', reason or None


def fix_command(body):
    """The request of a fix command (#195): when the first line after the attribution header is
    ``/devloop fix``, the rest of that line and the lines below it, stripped (``''`` when there
    is none); None when the comment is no such command."""
    lines = (body or '').strip().splitlines()
    while lines and (not lines[0].strip() or lines[0].strip().startswith('>')):
        lines.pop(0)
    tokens = lines[0].split(None, 2) if lines else []
    if tokens[:2] != FIX_COMMAND.split():
        return None
    rest = tokens[2] if len(tokens) > 2 else ''
    return '\n'.join([rest, *lines[1:]]).strip()


def requested_fixes(fixes):
    """The fixes that ``FIX_COMMAND`` asked for (#195), among the fix records ``fixes``."""
    return [f for f in fixes if f.get('requests')]


def merge_ready(pr, cfg):
    """Whether a ready PR meets, as the snapshot shows it, the merge directives that do not
    depend on its receipt (#172): CI ``pass``, ``MERGEABLE`` and ``CLEAN``, neither
    ``agent:needs-operator`` (under either name) nor ``agent:merge-hold``, no open finding, an
    acceptance map that holds."""
    lb = cfg.labels
    return (lb.ready_to_merge in pr.labels
            and not pr.labels & (lb.operator() | {lb.merge_hold, lb.needs_supervisor})
            and pr.checks == 'pass' and pr.mergeable == 'MERGEABLE'
            and pr.merge_status == 'CLEAN' and not pr.acceptance
            and not open_findings(pr.reviews, pr.closures))


def parked(pr, cfg):
    """Whether ``pr`` waits for a person (#199): ``agent:needs-operator``, or
    ``agent:ready-to-merge`` when devloop does not merge it, because ``[merge]`` is off or the
    PR's mode is (level override, operator-only path). A ready PR whose mode is not known counts
    as one devloop works on."""
    lb = cfg.labels
    if operator_blocked(pr.labels, lb):
        return True
    if lb.ready_to_merge not in pr.labels:
        return False
    return (not cfg.merge.enabled()
            or pr.merge_mode is not None and pr.merge_mode not in MERGING)


def pr_counts(snapshot, cfg):
    """``(working, parked)``: how many open devloop PRs count against ``max_open_prs`` and how
    many against ``max_parked_prs`` (#199)."""
    held = sum(1 for pr in snapshot.prs if parked(pr, cfg))
    return len(snapshot.prs) - held, held


def pr_counts_line(snapshot, cfg):
    """The two counters, as the tick log and ``status`` show them."""
    working, held = pr_counts(snapshot, cfg)
    return (f'open PRs: {working}/{cfg.max_open_prs} in progress (max_open_prs), '
            f'{held}/{cfg.max_parked_prs} parked (max_parked_prs)')


def plan(snapshot, cfg, busy=frozenset(), reserved=0):
    """Pure planning: the actions one tick should take, in order.

    In continuous mode (#227) ``busy`` holds the numbers of the issues and PRs that actions
    still running touch (``numbers_of``): no action of the plan touches them. ``reserved`` is
    how many of those actions may open a PR (an implementation, a readiness check, a supervisor
    command on an issue): each holds a slot of ``max_open_prs``, as it did in its own tick."""
    lb = cfg.labels
    actions = []
    issue_of = {pr.number: pr.issue for pr in snapshot.prs}
    if busy:
        snapshot = replace(
            snapshot,
            ready_issues=[i for i in snapshot.ready_issues if i['number'] not in busy],
            supervised=[s for s in snapshot.supervised
                        if not numbers_of(s['number'], issue_of) & busy])
    pr_issues = {pr.issue for pr in snapshot.prs}
    # Only the PRs devloop works on take the slots of max_open_prs; those waiting for a person
    # have their own limit, and once it is reached no new issue starts (#199), except a
    # priority:high one (#274): it may be the fix the parked PRs wait for.
    working, held = pr_counts(snapshot, cfg)
    slots = cfg.max_open_prs - working - reserved
    parked_full = held >= cfg.max_parked_prs
    free = 0 if parked_full else slots
    for item in snapshot.supervised:
        # A supervisor command (#182). A retry of an issue's step runs that step, and takes a
        # free PR slot as the step itself would; without one it waits for a later tick. While
        # the issue is blocked by an open issue (#277) it waits too, and takes no slot.
        if item['command'] == RETRY and not item['pr'] and item['step'] in ISSUE_STEPS:
            if item.get('blocked_by') or free <= 0:
                continue
            free -= 1
        actions.append(Action('supervise', item['number'], f'{item["command"]} by {item["by"]}'))
    for number in snapshot.working_issues:
        if number not in pr_issues:
            actions.append(Action('stale', number, 'agent:working without an open devloop PR'))
        else:
            actions.append(Action('settle', number, 'agent:working but its PR is open'))
    for pr in snapshot.prs:
        # The dispatcher's own label of an operator merge (#248) does not stop devloop.
        if lb.needs_supervisor in pr.labels or operator_held(pr, lb):
            continue
        if not pr.meta:
            actions.append(Action('lost_meta', pr.number, 'PR body no longer ends with its marker'))
            continue
        if not pr.labels & {lb.review, lb.changes, lb.ready_to_merge}:
            # e.g. a crash between creating the PR and labelling it
            actions.append(Action('reconcile', pr.number, 'devloop PR without a state label'))
            continue
        if pr.finding_problems:
            actions.append(Action('unattributable', pr.number,
                                  'findings whose owner cannot be reconstructed'))
            continue
        if pr.hold:
            # A hold or release of the merge (#172) is applied first, so that no merge of this
            # tick overtakes it; the next tick plans with it.
            actions.append(Action('hold', pr.number, f'{pr.hold["event"]} by {pr.hold["by"]}'))
            continue
        # The latest round, and the reviewers it still waits for: its verdict counts only once
        # every reviewer its receipt requires has reviewed.
        latest = current_round(pr.reviews)
        pending = pending_reviewers(latest)
        verdict = round_verdict(latest) if latest and not pending else None
        head = latest[0]['head_sha'] if latest else None
        rounds_left = counted_rounds(pr.reviews) < cfg.max_review_rounds
        if lb.ready_to_merge in pr.labels:
            # Readiness belongs to the approved commit: re-check it on every tick. A level-0
            # promotion belongs to its head and to the labels that allowed it, and the snapshot
            # re-checks its receipt, expected checks and native reviews (``zero_problem``).
            rp = cfg.review_policy
            zero = pr.zero_head is not None
            if zero and (pr.zero_problem or not zero_labels_hold(pr, rp)):
                actions.append(Action('revoke', pr.number, 'the level 0 promotion no longer holds'))
            elif not zero and (verdict != 'approve' or head != pr.head_sha):
                actions.append(Action('revoke', pr.number, 'head is not the approved commit'))
            elif not zero and pr.acceptance:
                # An approval that did not see the acceptance map, e.g. one recorded before
                # #134: back to review, where a new review adds the finding.
                actions.append(Action('revoke', pr.number, 'the acceptance map does not hold'))
            elif pr.merge_state == 'conflict':
                actions.append(Action('sync', pr.number, 'conflicts with the base branch'))
            elif pr.checks == 'fail' and pr.rerun_waiting:
                pass  # the failed jobs were re-run (#200): not merged, nor a round, yet
            elif pr.checks == 'fail':
                actions.append(Action('ci_failed', pr.number, 'CI failed after approval')
                               if rounds_left else Action('round_limit', pr.number))
            elif pr.checks == 'pending':
                # CI re-running on the approved commit: not ready until it passes again. The
                # recorded approval is replayed from agent:review, without a new review.
                actions.append(Action('revoke', pr.number, 'CI pending on the approved commit'))
            elif pr.merge_mode in MERGING and merge_ready(pr, cfg):
                # The merge directives are checked again just before the merge (#172).
                actions.append(Action('merge', pr.number, f'merge mode {pr.merge_mode}'))
            elif operator_merge_due(pr, cfg):
                # The merge is the operator's (#248): tell, once per head, and why.
                actions.append(Action('operator_merge', pr.number,
                                      f'merge mode {pr.merge_mode or cfg.merge.mode}'))
            continue
        still_open = open_findings(pr.reviews, pr.closures)
        # Also from agent:changes: a CI round sends the PR there, and a re-run may pass on the
        # same head before the implementer starts.
        ci_green = (pr.checks == 'pass' and any(f['owner'] == CI for f in still_open))
        if pr.commands or ci_green:
            # Closures are recorded first; the next tick plans with them.
            actions.append(Action('findings', pr.number, 'CI passed on the head' if ci_green
                                  else 'operator command'))
            continue
        if pr.fix_requests:
            # A fix of the current head asked by a trusted user or the supervisor (#195): no
            # review first, and no round, since it writes no review record.
            by = ', '.join(dict.fromkeys(r['by'] for r in pr.fix_requests))
            actions.append(Action('fix', pr.number, f'{FIX_COMMAND} by {by}'))
            continue
        # A request for changes whose findings were all closed by CI or the operator: nothing
        # is left to fix, and the head needs a new review.
        settled = verdict == 'request_changes' and bool(pr.closures) and not still_open
        if lb.changes in pr.labels:
            actions.append(Action('reconcile', pr.number, 'every open finding is closed')
                           if settled else Action('fix', pr.number))
        elif lb.review in pr.labels:
            answered = bool(latest) and any(f['round'] == latest[0]['round'] for f in pr.fixes)
            # A trusted user's rereview request voids the recorded verdict for this head.
            current = (bool(latest) and head == pr.head_sha and not answered
                       and not pr.rereview and not settled)
            # A round still waiting for a reviewer continues: it is not a new round.
            partial = current and bool(pending)
            replay = current and not pending
            # An approval counts only with a policy receipt that still matched the last time
            # it was checked; one recorded before receipts existed needs a new review.
            unbound = replay and (any(receipt_of(r) is None for r in latest) or pr.stale_receipt)
            # Nor is an approval reused when the acceptance map does not hold (#134): a new
            # review adds the finding, as a new round within the usual limit.
            unmapped = replay and verdict == 'approve' and bool(pr.acceptance)
            # A review needed only because a clean sync moved the approved head (#235), or
            # because the base touched the PR after its approval (#252), is not a new round
            # against the limit; a CI failure or a rereview still is.
            resync = pr.resync is not None and not pr.rereview and pr.checks != 'fail'
            if replay and verdict != 'approve':
                actions.append(Action('apply_review', pr.number, 'verdict already recorded'))
            elif pr.merge_state == 'conflict':
                # Neither reviewed nor promoted: the merge would change the head anyway.
                actions.append(Action('sync', pr.number, 'conflicts with the base branch'))
            elif pr.merge_state == 'unknown':
                continue  # GitHub is still computing mergeability: next tick
            elif pr.checks == 'pending':
                continue
            elif pr.ci_missing:
                # The checks the base branch requires never appeared on the head (#263).
                actions.append(Action('ci_missing', pr.number, 'required checks missing: '
                                      + ', '.join(pr.ci_missing)))
            elif pr.ci_wait:
                continue  # a head without CI yet (#263): as if its CI were pending
            elif pr.checks == 'fail' and pr.rerun_waiting:
                continue  # the failed jobs were re-run (#200) and have not started yet
            elif replay and not unbound and not unmapped and pr.checks != 'fail':
                actions.append(Action('apply_review', pr.number, 'approval already recorded'))
            elif partial and pr.checks != 'fail':
                actions.append(Action('review', pr.number, 'the round waits for '
                                      + ', '.join(pending)))
            elif not rounds_left and not resync:
                # Every new round, a CI failure or a review, counts against the limit.
                actions.append(Action('round_limit', pr.number))
            elif pr.checks == 'fail':
                actions.append(Action('ci_failed', pr.number))
            else:
                reason = ('new review requested by a trusted user' if pr.rereview
                          else 'the approval was revoked because the base touched the PR'
                          if resync and pr.resync['reason'] == BASE_CHANGED
                          else 'the approved head was synced with the base' if resync
                          else 'the approval has no matching policy receipt' if unbound
                          else 'the acceptance map does not hold' if unmapped
                          else 'every finding of the request for changes is closed' if settled
                          else '')
                actions.append(Action('review', pr.number, reason))
    # A ready issue that a merged devloop PR already resolved is never checked nor implemented
    # again (#228): it is escalated, once, and waits for whoever decides it.
    held_back = set()
    for issue in snapshot.ready_issues:
        if issue.get('merged'):
            held_back.add(issue['number'])
            if not issue['labels'] & (lb.operator() | {lb.needs_supervisor}):
                actions.append(Action('merged_issue', issue['number'], 'resolved by merged PR '
                                      + ', '.join(f'#{n}' for n in issue['merged'])))
    # A ready issue blocked by an open issue (#277) neither is checked nor starts, and takes no
    # slot; one on a dependency cycle is reported once, on the issue itself.
    for issue in snapshot.ready_issues:
        if issue.get('cycle') and not issue.get('cycle_reported'):
            actions.append(Action('dependency_cycle', issue['number'],
                                  'dependency cycle ' + ', '.join(issue['cycle'])))
        if issue.get('blocked_by'):
            held_back.add(issue['number'])
    # With max_parked_prs reached only priority:high issues start, still within the slots of
    # max_open_prs (#274), and the reason says why.
    urgent = ''
    if parked_full:
        urgent = (f'priority high: starts although {held}/{cfg.max_parked_prs} PRs are parked '
                  '(max_parked_prs)')
        free = slots
    if cfg.preflight:
        actions += _plan_preflight(snapshot, cfg, pr_issues | held_back, free, urgent)
    elif free > 0:
        blocked = set(cfg.blocked_labels) | lb.operator() | {lb.working, lb.needs_supervisor}
        candidates = [i for i in snapshot.ready_issues
                      if not (i['labels'] & blocked) and i['number'] not in pr_issues
                      and i['number'] not in held_back and i['admitted']
                      and (not urgent or i.get('priority') == 'high')]
        candidates.sort(key=start_order)
        actions += [Action('implement', i['number'], urgent) for i in candidates[:free]]
    capped, spent = [], 0
    for action in actions:
        if busy and numbers_of(action.number, issue_of) & busy:
            continue  # still running (#227): the plan after it ends decides
        if action.kind in EXPENSIVE:
            if spent >= cfg.max_actions_per_tick:
                continue
            spent += 1
        capped.append(action)
    return capped


def _plan_preflight(snapshot, cfg, pr_issues, free, urgent=''):
    """New work with the readiness check (#114), for the ready issues not in ``pr_issues`` (an
    open PR, a merged one, #228, or an open block, #277). Issues start in the usual order, up to
    ``free``: one whose check for its current text passed is implemented, one without a valid
    check is checked first (implemented on a later tick if ready). With ``urgent``, the reason
    why ``max_parked_prs`` is reached (#274), only ``priority:high`` issues start, and their
    actions carry it. A failed check that is still
    valid blocks the issue again if its ``agent:needs-operator`` was removed. An issue the check
    blocked is checked again, whatever ``free``, when its text changed or a trusted user asked
    for it; any other ``agent:needs-operator`` (or its name before #248) is left alone. An issue in
    ``agent:needs-supervisor`` waits for the supervisor (#182)."""
    lb = cfg.labels
    excluded = set(cfg.blocked_labels) | {lb.working, lb.needs_supervisor}
    issues = [i for i in snapshot.ready_issues
              if not (i['labels'] & excluded) and i['number'] not in pr_issues
              and i['admitted']]
    issues.sort(key=start_order)
    actions, taken = [], 0
    for issue in issues:
        view = issue.get('preflight')
        if view is None:
            continue  # not read by the snapshot: nothing is known about its check
        state, reason = preflight_state(view)
        if operator_blocked(issue['labels'], lb):
            if view['owned'] and state == 'run':
                actions.append(Action('preflight', issue['number'], reason))
        elif state == 'not_ready':
            actions.append(Action('preflight_block', issue['number'],
                                  'the readiness check of the current text failed'))
        elif taken < free and (not urgent or issue.get('priority') == 'high'):
            taken += 1
            actions.append(Action('implement', issue['number'], urgent) if state == 'ready'
                           else Action('preflight', issue['number'],
                                       f'{reason}; {urgent}' if urgent else reason))
    return actions


def preflight_state(view):
    """``(state, reason)`` of an issue's readiness check: ``run`` when a check is needed,
    otherwise ``ready`` or ``not_ready``, the outcome of the latest check, which holds while the
    issue text keeps its hash."""
    record = view['record']
    if view['recheck']:
        return 'run', f'{RECHECK_COMMAND} by a trusted user'
    if record is None:
        return 'run', 'no readiness check yet'
    if record['hash'] != view['hash']:
        return 'run', 'the issue text changed since the readiness check'
    return ('ready' if record['ready'] else 'not_ready'), ''


def devloop_text(body):
    """Text devloop or its agents published: it starts with the AGENTS.md header."""
    return (body or '').lstrip().startswith('> [!NOTE]')


def control_command(body):
    """Whether a comment is a devloop command: its first line starts with ``/devloop``."""
    lines = (body or '').strip().splitlines()
    return bool(lines) and lines[0].split()[:1] == ['/devloop']


def preflight_hash(title, body, comments, trusted_users, automatic, accepted=None):
    """The hash that binds a readiness check to the issue text: title, body and the comments of
    trusted users, without devloop's own records (``automatic(comment)``) and without
    ``/devloop`` commands (a supervisor command after the attribution header too, #182). Any
    other trusted comment counts, header or not, and so does a dispatcher record of defaults
    the supervisor accepted (``accepted(comment)``, #182), as a trusted comment would."""
    accepted = accepted or (lambda comment: False)
    kept = [[(c.get('author') or {}).get('login'), c.get('body') or ''] for c in comments
            if accepted(c) or ((c.get('author') or {}).get('login') in trusted_users
                               and not automatic(c) and not control_command(c.get('body'))
                               and supervisor_command(c.get('body')) is None)]
    data = json.dumps([title or '', body or '', kept], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def normalize_preflight(raw):
    """The outcome of a readiness check: ready only without gaps and without a split, whatever
    the agent says (a ``default_available`` gap blocks too)."""
    gaps = [dict(g, code_refs=list(g['code_refs'])) for g in raw['gaps']]
    ready = bool(raw['ready']) and not gaps and not raw['split'].strip()
    return {'ready': ready, 'gaps': gaps, 'split': raw['split']}


def pending_reviewers(records):
    """The reviewers an agent round still waits for: those the receipt of its first review
    requires and that have not reviewed yet. A CI round, or a review without a receipt, is
    complete as it is."""
    receipt = receipt_of(records[0]) if records and records[0]['source'] == 'agent' else None
    if receipt is None:
        return []
    done = {owner_of(r) for r in records}
    return [key for key in receipt['reviewers'] if key not in done]


def _round_records(reviews, number):
    """The records of round ``number``, one per owner, as ``current_round`` reads the latest."""
    records, owners = [], set()
    for record in reviews:
        if record['round'] == number and owner_of(record) not in owners:
            owners.add(owner_of(record))
            records.append(record)
    return records


def waived_rounds(reviews):
    """The rounds that do not count against ``max_review_rounds``: each incomplete agent round
    that a later round replaced only because the base branch advanced, as the later round's
    records say (``restarts``, #160); and each round that reviewed a head differing only by
    clean merges of the base from the head the round just before approved (``resync``,
    #235), or that reviewed again the head the round just before approved, after the dispatcher
    revoked that approval because the base touched the PR (``resync``, #252)."""
    waived = set()
    for record in reviews:
        restart = record.get('restarts')
        number = restart.get('round') if isinstance(restart, dict) else None
        if (isinstance(number, int) and not isinstance(number, bool) and number < record['round']
                and pending_reviewers(_round_records(reviews, number))):
            waived.add(number)
        if _resynced(reviews, record):
            waived.add(record['round'])
    return waived


def _resynced(reviews, record):
    """Whether ``record`` reviewed a resync after approval (#235) or the same head after a
    revocation for the base (#252): its ``resync`` names the round just before its own, a
    complete agent round that approved the head ``from``; the record reviews another head after
    a resync, that same head after a revocation."""
    resync = record.get('resync')
    if not isinstance(resync, dict) or resync.get('reason') not in (RESYNC_AFTER_APPROVAL,
                                                                    BASE_CHANGED):
        return False
    number = resync.get('round')
    if isinstance(number, bool) or number != record['round'] - 1:
        return False
    approved = _round_records(reviews, number)
    same = record.get('head_sha') == resync.get('from')
    return (bool(approved) and approved[0]['source'] == 'agent'
            and not pending_reviewers(approved) and round_verdict(approved) == 'approve'
            and all(r.get('head_sha') == resync.get('from') for r in approved)
            and same == (resync['reason'] == BASE_CHANGED))


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


def synced_from(syncs, head, reviewed, limit=10):
    """Whether ``head`` comes from ``reviewed`` only through syncs whose conflicts an agent
    resolved (#250): walking back from ``head``, each step is a ``sync`` marker whose
    ``head_sha`` is the current commit and whose ``from`` is the previous one, up to
    ``reviewed`` (at most ``limit`` steps). A head that someone else moved is not."""
    back = {s['head_sha']: s.get('from') for s in syncs if isinstance(s.get('from'), str)}
    for _ in range(limit):
        if head not in back:
            return False
        head = back[head]
        if head == reviewed:
            return True
    return False


def counted_rounds(reviews):
    """How many rounds count against ``max_review_rounds``: every round, CI rounds and rereviews
    included, except the waived ones (``waived_rounds``). Round numbers stay progressive."""
    return len({r['round'] for r in reviews} - waived_rounds(reviews))


def _parents(path):
    return {path[:i] for i, c in enumerate(path) if c == '/'}


def touched_by_base(paths, touched):
    """The paths of a PR's diff that a change of the base branch, ``touched``, reaches (#160):
    the same path, a directory above it or a path under it (a file replaced by a directory, or
    the reverse, would conflict even with no file in common)."""
    touched = set(touched)
    dirs = set().union(*map(_parents, touched)) if touched else set()
    return sorted({p for p in paths if p in touched or p in dirs or _parents(p) & touched})


def skipped_models(used):
    """``{"skipped_models": [{model, reason, until}]}`` for the marker of a turn that ran with
    ``used`` after skipping models remembered unavailable (#151), or ``{}``. Informative only:
    no receipt, independence check nor approval reads it."""
    if not used.skipped:
        return {}
    return {'skipped_models': [{'model': model, 'reason': reason, 'until': until}
                               for model, reason, until in used.skipped]}


class Models:
    """The models one action may still run ``agent`` with (``AgentConfig.chain``), in order, and
    those it tried or left out, as ``(model, why)`` pairs (#121); ``skipped``, those it left out
    because remembered unavailable, ``(model, reason, until)`` each (#151)."""

    def __init__(self, agent):
        self.agent = agent
        self.left = agent.chain()
        self.tried = []
        self.skipped = []


class UntrustedChange(RuntimeError):
    """The issue text changed after a trusted user approved it with the ready label."""


def ready_approval(labeled, ready_label, admitters):
    """The ``(login, at)`` of the latest ``labeled`` event of ``ready_label`` in ``labeled``
    (``(label, login, at)``, oldest first) when one of ``admitters`` added it, else None. The
    latest event decides (#178), as for the queue: an approval replaced since by someone else's
    label no longer counts."""
    latest = [(who, at) for name, who, at in labeled if name == ready_label][-1:]
    return latest[0] if latest and latest[0][0] in admitters else None


def untrusted_change(history, ready_label, trusted_users, admitters=None):
    """Why the issue's title or body can no longer be trusted, or None.

    Trust comes from one of ``admitters`` (``label_admitters``, the trusted users by default)
    adding ``ready_label`` in its latest ``labeled`` event (``ready_approval``); a later change
    to the title or the body by anyone outside ``trusted_users`` voids it until the label is
    added again. GitHub timestamps have one-second granularity, so an untrusted change in the
    same second as the label cannot be proven to precede it and voids trust too.
    """
    admitters = trusted_users if admitters is None else admitters
    approved = ready_approval(history['labeled'], ready_label, admitters)
    if not approved:
        return f'nessuna label `{ready_label}` messa da un utente fidato'
    since = approved[1]
    edited = history['last_edited_at']
    if edited and edited >= since and history['editor'] not in trusted_users:
        return f'il testo è stato modificato da `{history["editor"]}` dopo la label `{ready_label}`'
    for who, at in history['renamed']:
        if at >= since and who not in trusted_users:
            return f'il titolo è stato modificato da `{who}` dopo la label `{ready_label}`'
    return None


def operator_comments(comments, trusted_users):
    """The comments in ``comments`` that a trusted user wrote in person.

    Every text devloop publishes starts with the AGENTS.md attribution header. An actor without
    a GitHub App publishes with the operator's own login, so the header, not the author alone,
    tells the text of devloop and its agents apart from the operator's.
    """
    return [c for c in comments
            if (c.get('author') or {}).get('login') in trusted_users
            and not (c.get('body') or '').lstrip().startswith('> [!NOTE]')]


def requests_rereview(body):
    """Whether a comment asks for a new review: its first line is ``REREVIEW_COMMAND``."""
    lines = (body or '').strip().splitlines()
    return bool(lines) and lines[0].strip() == REREVIEW_COMMAND


def format_operator_comments(comments):
    if not comments:
        return ''
    intro = ('A trusted user asked for a new review of this commit after the previous verdict. '
             if any(requests_rereview(c.get('body')) for c in comments) else '')
    rendered = '\n\n'.join(f'### Comment by {c["author"]["login"]}\n\n{c["body"]}'
                           for c in comments)
    return ('## Comments from trusted users since the previous verdict\n\n' + intro
            + 'The operator wrote them after the previous round, for example to settle a point '
              'that round left to a human.\n\n' + rendered)


def _template(name, **values):
    text = (PROMPT_DIR / f'{name}.md').read_text(encoding='utf-8')
    return string.Template(text).safe_substitute({k: str(v) for k, v in values.items()})


# The changed files shown for each mapped document in a prompt (#203); the rest are counted.
DOCS_PATHS_SHOWN = 5


def docs_map_section(entries):
    """The documentation section of the first implement prompt (#203): its diff is still empty,
    so it gets the whole ``[docs] map``. Empty without a map."""
    if not entries:
        return ''
    lines = '\n'.join(f'- `{key}` → ' + ', '.join(f'`{d}`' for d in documents)
                      for key, documents in entries)
    return ('## Documentation map\n\n'
            'devloop maps code paths to the documents that describe them (`[docs] map`; a path '
            'ending in `/` covers every file under it). Your diff is still empty, so this is the '
            'whole map. Before finishing, apply it to the files you changed: update every '
            'document mapped to one of them whose described behaviour your change alters. A file '
            'that no entry covers asks for no document.\n\n' + lines)


def _docs_list(documents):
    lines = []
    for document, paths in documents.items():
        shown = ', '.join(f'`{p}`' for p in paths[:DOCS_PATHS_SHOWN])
        more = (f' and {len(paths) - DOCS_PATHS_SHOWN} more' if len(paths) > DOCS_PATHS_SHOWN
                else '')
        lines.append(f'- `{document}`, mapped from {shown}{more}')
    return '\n'.join(lines)


def docs_diff_section(documents):
    """The documentation section of a later implement attempt and of a fix (#203): the
    documents mapped to the files of the current diff (``DocsMap.documents``). Empty when no
    file of the diff is mapped."""
    if not documents:
        return ''
    return ('## Documents mapped to the diff\n\n'
            'The files this branch changes map to these documents (`[docs] map`). Update each '
            'one whose described behaviour your change alters; leave the others as they '
            'are.\n\n' + _docs_list(documents))


def docs_review_section(documents):
    """The documentation section of a review prompt (#203): the documents mapped to the
    files of the pull request's diff, and how to judge them. Empty when no file is mapped."""
    if not documents:
        return ''
    return ('## Documents mapped to the diff\n\n'
            'The files this pull request changes map to these documents (`[docs] map`):\n\n'
            + _docs_list(documents)
            + '\n\nA behaviour described in one of these documents that the change alters without '
              'updating that document is a **blocking** finding. When a mapped document stays '
              'correct without changes, say so in `checks_performed`.')


def _format_findings(findings):
    lines = []
    for f in findings:
        where = f' ({f["file"]}{":" + str(f["line"]) if f["line"] > 0 else ""})' if f['file'] else ''
        lines.append(f'- **{f.get("key") or f["id"]}**{where}: {f["title"]}\n  {f["detail"]}'
                     + (f'\n  Suggested fix: {f["suggested_fix"]}' if f['suggested_fix'] else ''))
    return '\n'.join(lines) or '(none)'


def format_fix_requests(requests):
    """The section of a fix prompt with the requests of ``FIX_COMMAND`` (#195), ``{comment, by,
    request}``; empty without any."""
    if not requests:
        return ''
    rendered = '\n\n'.join(f'### Request by {r["by"]}\n\n{r["request"]}' for r in requests)
    return ('## Requests from the operator\n\n'
            f'A trusted user or the supervisor asked for this fix with `{FIX_COMMAND}`, without '
            'waiting for a review. Treat each request below as a requirement of this fix, as you '
            'would a blocking finding: implement it, or return `status: "blocked"` if it needs a '
            'product decision or contradicts the issue. List no response for it in `responses`, '
            'which is for the open findings only. Your `summary` must say how you handled each '
            'request.\n\n' + rendered)


def _format_requested(fix):
    return (f'Fix asked with `{FIX_COMMAND}` on {str(fix.get("from") or "?")[:12]}:\n'
            + '\n'.join(f'- by {r.get("by")}: {r.get("request", "")}' for r in fix['requests']))


def format_previous_rounds(reviews, fixes, closures=()):
    rounds = {review['round'] for review in reviews}
    # Fixes asked with FIX_COMMAND before any review of their round (#195).
    parts = [_format_requested(fix) for fix in requested_fixes(fixes)
             if fix['round'] not in rounds]
    if not reviews:
        return '\n\n'.join(parts) or 'This is the first round.'
    for position, review in enumerate(reviews):
        owner = owner_of(review)
        parts.append(f'### Round {review["round"]}: {review["verdict"]} on {review["head_sha"][:12]}'
                     f' ({review["source"]}, reviewer {owner})\n\n{review["summary"]}\n\n'
                     'Blocking findings:\n' + _format_findings(
                         [dict(f, key=f.get('key') or finding_key(review['round'], owner, f['id']))
                          for f in review['blocking_findings']]))
        if reviews[position + 1:position + 2] and reviews[position + 1]['round'] == review['round']:
            continue  # the fix answers the whole round: after its last review
        for fix in fixes:
            if fix['round'] == review['round'] and fix.get('requests'):
                parts.append(_format_requested(fix))
            if fix['round'] == review['round'] and (fix['responses'] or not fix.get('requests')):
                parts.append('Implementer responses:\n' + '\n'.join(
                    f'- {r["finding_id"]}: {r["action"]}: {r["note"]}' for r in fix['responses']))
    if closures:
        parts.append('### Findings closed by CI or by the operator\n\n' + '\n'.join(
            f'- {c["key"]}: {c["status"]} by '
            + ('CI' if c['by'] == CI else f'the operator ({c["author"]})') + f': {c["reason"]}'
            for c in closures))
    return '\n\n'.join(parts)


class Dispatcher:
    """``gh`` acts as the dispatcher: it implements the ports of ``ports.Backend`` (tracker,
    code review and CI), and the dispatcher uses nothing else of it. ``identities``
    (``apps.Identities``) gives each agent its own GitHub identity; without it every actor uses
    ``gh``. ``activity`` (``activity.Activity``) records the running action for ``devloop
    status``. ``ledger`` (``ledger.Ledger``) records evidence and telemetry of each step (#133)."""

    def __init__(self, cfg, gh: Backend, ws, runner, log=print, identities=None, activity=None,
                 ledger=None, merger=None, pinner=None, memory=None, clock=time.time):
        self.cfg = cfg
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
        self.ledger = ledger
        # The models found unavailable (``unavailability.Memory``, #151), or None: every turn
        # starts from the configured model.
        self.memory = memory
        # Continuous mode (#227): set by ``devloop run`` with parallel actions, a tick starts
        # its actions in these ``Workers`` and does not wait for them.
        self.workers = None
        # Whether a tick of this process already checked the rename of agent:needs-operator
        # (#248, ``_transition_labels``).
        self.labels_checked = False

    # Identities ------------------------------------------------------------
    def _as(self, actor, permissions) -> Backend:
        """The GitHub client ``actor`` acts with, holding only ``permissions``."""
        return self.identities.github(actor, permissions) if self.identities else self.gh

    def _git_auth(self, actor, permissions):
        return self.identities.git_auth(actor, permissions) if self.identities else None

    def _app_login(self, actor):
        return self.identities.login(actor) if self.identities else None

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
                    problems.append(f'la review del round {record["round"]} non registra il '
                                    'reviewer')
                continue
            elif record['reviewer'] in (implementer, CI) or record['reviewer'] not in self.cfg.agents:
                # Only a configured agent reviews: never the implementer, nor one of devloop's
                # own apps, such as the merger, whose app only merges (#201).
                continue
            else:
                actor = record['reviewer']
            if not (actor and self._wrote(login, actor)):
                continue
            if kind == 'review':
                owner = owner_of(record)
                wrong = [f['key'] for f in record['blocking_findings']
                         if 'key' in f and f['key'] != finding_key(record['round'], owner, f['id'])]
                if wrong:
                    if problems is not None:
                        problems.append(f'la review del round {record["round"]} di `{owner}` '
                                        'attribuisce finding ad altri: '
                                        + ', '.join(f'`{k}`' for k in wrong))
                    continue
            found.append((position, record))
        return found

    def _writes_records(self, login):
        """Whether ``login`` may write some devloop record: an agent's, or the dispatcher's."""
        return any(self._wrote(login, actor) for actor in (*self.cfg.agents, DISPATCHER))

    def _finding_commands(self, pr, comments, state):
        """Closures asked with ``FINDING_COMMAND`` lines and not recorded yet, one per open
        finding, from comments trusted users wrote in person. A command from anyone else,
        malformed, or naming a finding that never existed is ignored and logged; one naming a
        finding already closed has nothing left to do."""
        open_keys = {f['key'] for f in state.open}
        pending = {}
        for comment in comments:
            body = comment.get('body') or ''
            if body.lstrip().startswith('> [!NOTE]'):
                continue  # text devloop or its agents published: never a command
            login = (comment.get('author') or {}).get('login')
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

    def _may_hold(self, login):
        """Whether ``login`` may hold or release a merge (#172), or ask for a fix (#195): a
        trusted user, or the app of the agent configured as ``roles.supervisor``."""
        supervisor = self._app_login(self.cfg.supervisor) if self.cfg.supervisor else None
        return login in self.cfg.trusted_users or bool(supervisor and login == supervisor)

    def _admitters(self, author):
        """The logins whose ``agent:ready`` and priority labels count on an issue opened by
        ``author`` (#178, ``label_admitters``): the supervisor's app counts only when it has
        one, and only on issues of trusted users or of devloop's apps."""
        supervisor = self._app_login(self.cfg.supervisor) if self.cfg.supervisor else None
        if not supervisor:
            return set(self.cfg.trusted_users)
        devloop = {self._app_login(actor) for actor in (*self.cfg.agents, DISPATCHER)} - {None}
        return label_admitters(author, self.cfg.trusted_users, supervisor, devloop)

    def _untrusted_change(self, history):
        """``untrusted_change`` of an issue, with the admitters of its author."""
        return untrusted_change(history, self.cfg.labels.ready, self.cfg.trusted_users,
                                self._admitters(history['author']))

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
        and the key of an issue step's retries."""
        return preflight_hash(history['title'], history['body'], comments,
                              self.cfg.trusted_users, self._automatic, self._accepted)

    def _devloop_author(self, login):
        """Whether an issue opened by ``login`` may have its escalations decided by the
        supervisor: a trusted user or one of devloop's apps."""
        apps = {self._app_login(actor) for actor in (*self.cfg.agents, DISPATCHER)} - {None}
        return bool(login) and (login in self.cfg.trusted_users or login in apps)

    def _dispatcher_records(self, comments, kind, event=None):
        """``(position, record)`` of the records of ``kind`` the dispatcher signed."""
        found = []
        for position, comment in enumerate(comments):
            record = read_marker(comment.get('body', ''), kind)
            if (record is not None and (event is None or record.get('event') == event)
                    and self._signed((comment.get('author') or {}).get('login'))):
                found.append((position, record))
        return found

    def _supervised(self, number, cls, step, pr=None, reviewer=None, key=None):
        """``(route, why)`` of an escalation of class ``cls`` raised by ``step`` on issue or PR
        ``number`` (``pr``: the PR, None for an issue). ``route`` is None when it goes to the
        operator; otherwise the payload of an escalation the supervisor decides: the step, the
        key of its input and, for a PR, the label a retry resumes from. ``why`` says why a
        non-critical class in ``auto`` still goes to the operator, '' otherwise. ``key``: for an
        issue, the key of the input the step ran on (``_issue_key``), bound before it ran; an
        issue step without one goes to the operator.

        Only in merge mode ``auto`` with a supervisor, and only when nothing makes the
        escalation critical: a PR whose own merge mode is not ``auto`` (the override of its
        level, an ``operator_only_paths`` path), a PR the supervisor implemented, an issue
        opened by someone else than a trusted user or a devloop app, an issue text no longer
        approved by ``agent:ready``, the ``phase:design`` label, or anything that cannot be
        verified."""
        cfg, lb = self.cfg, self.cfg.labels
        if critical(cls) or cfg.merge.mode != 'auto' or not cfg.supervisor:
            return None, ''
        if step not in RETRY_STEPS:
            return None, f'il passo `{step}` non è fra quelli che il supervisore può ripetere'
        try:
            labels = set()
            if pr is not None:
                if not pr.meta.get('implementer'):
                    return None, ('il corpo della PR non termina più con il suo marker: chi l\'ha '
                                  'implementata e la sua modalità di merge non sono verificabili')
                labels |= pr.labels
                if pr.meta.get('implementer') == cfg.supervisor:
                    return None, 'il supervisore ha implementato questa PR'
                mode = self._merge_mode(pr)
                if mode != 'auto':
                    return None, (f'la modalità di merge di questa PR è `{mode or "non calcolabile"}`'
                                  ', non `auto` (override del livello o `operator_only_paths`)')
            issue = pr.issue if pr is not None else number
            history = self.gh.issue_history(issue)
            if not self._devloop_author(history['author']):
                return None, (f'l\'issue #{issue} è stata aperta da `{history["author"]}`, che non '
                              'è un utente fidato né un\'app di devloop')
            untrusted = self._untrusted_change(history)
            if untrusted:
                return None, untrusted
            labels |= label_names(self.gh.issue(issue).get('labels') or [])
            if DESIGN_LABEL in labels:
                return None, f'la label `{DESIGN_LABEL}` è presente'
            if pr is not None:
                key = {'step': step, 'head': pr.head_sha}
                if reviewer:
                    key['reviewer'] = reviewer
                resume = getattr(lb, RESUME_STEP.get(step, 'review'))
                if step == 'fix' and lb.review in pr.labels and lb.changes not in pr.labels:
                    # A `/devloop fix` asked in agent:review (#195) resumes there: agent:changes
                    # would answer findings already fixed, or have none to fix.
                    resume = lb.review
                if step == 'sync' and lb.changes in pr.labels:
                    # A sync that a fix started in agent:changes (#250) resumes there: from
                    # agent:review the next tick would review the synced head, or answer only
                    # the `/devloop fix` requests, leaving the round's findings unfixed.
                    resume = lb.changes
                return {'step': step, 'key': key, 'resume': resume}, ''
            if key is None and cls in (PREFLIGHT_DEFAULT, MERGED_ISSUE):
                # A failed check reused, not run again, or an issue a merged PR resolved (#228),
                # whose step never runs: no retry uses its key (a retry admits neither class),
                # and accept-defaults checks the hash of the check's own record.
                key = self._issue_key(step, self.ws.base_head(),
                                      self._request_hash(history, self.gh.issue_comments(number)))
            if not isinstance(key, dict) or key.get('step') != step:
                return None, 'l\'input del passo non è stato registrato prima dell\'esecuzione'
            return {'step': step, 'key': key}, ''
        except (GitHubError, WorkspaceError) as exc:
            return None, f'non è stato possibile verificare che l\'escalation non sia critica ({exc})'

    @staticmethod
    def _issue_key(step, base, request):
        """The key of the input of an issue's ``step``: the commit of the base its checkout
        started from and the hash of the request its prompt got, both taken before the step
        runs, so that a base or a text that changes during the turn is not mistaken for the
        input that failed."""
        return {'step': step, 'base': base, 'request': request}

    def _current_key(self, number, key, pr):
        """The key ``key`` would have now, for the same step (and reviewer) on issue or PR
        ``number``: a retry of a superseded input is refused."""
        if pr is not None:
            current = {'step': key.get('step'), 'head': self.gh.pr_head(number)}
            if 'reviewer' in key:
                current['reviewer'] = key['reviewer']
            return current
        history = self.gh.issue_history(number)
        return {'step': key.get('step'), 'base': self.ws.base_head(),
                'request': self._request_hash(history, self.gh.issue_comments(number))}

    def _supervisor_view(self, number, comments):
        """The supervisor command still to apply on issue or PR ``number``, from its
        ``comments``: ``{command, reason, by, comment, step}`` or None. Only a command written
        after the latest escalation of the dispatcher counts. A dispatcher ``supervision``
        record spends the command it handled, whose comment id it carries, and every command
        before it; among those still waiting the latest wins. A command counts only in merge
        mode ``auto`` and from the supervisor's identity or a trusted user (``_may_hold``); any
        other, and an escalate without a reason, is ignored and logged. devloop's own records
        are never commands."""
        escalations = self._dispatcher_records(comments, 'notice', 'escalation')
        if not escalations:
            return None
        start, latest = escalations[-1]
        spent = None
        for position, record in self._dispatcher_records(comments, 'supervision'):
            applied = record.get('comment')
            if isinstance(applied, int) and not isinstance(applied, bool):
                spent = applied if spent is None else max(spent, applied)
            else:
                start = max(start, position)  # a record without the id spends what precedes it
        found = None
        for comment in comments[start + 1:]:
            body = comment.get('body') or ''
            parsed = None if carries_record(body) else supervisor_command(body)
            if parsed is None:
                continue
            ident = comment.get('id')
            if spent is not None and (not isinstance(ident, int) or ident <= spent):
                continue
            command, reason = parsed
            login = (comment.get('author') or {}).get('login')
            why = (f'merge mode is "{self.cfg.merge.mode}", not "auto"'
                   if self.cfg.merge.mode != 'auto'
                   else 'the author is neither the supervisor nor a trusted user'
                   if not self._may_hold(login)
                   else f'malformed, expected `/devloop {command} <text>`' if reason is None
                   else None)
            if why:
                self.log(f'  #{number}: ignored `/devloop {command}` by {login}: {why}')
                continue
            found = {'command': command, 'reason': reason, 'by': login, 'comment': ident,
                     'step': latest.get('step')}
        return found

    def _to_operator(self, number, reason, cls, remove=()):
        """Hand an item in ``agent:needs-supervisor`` to the operator: a critical escalation of
        class ``cls``."""
        lb = self.cfg.labels
        self.gh.comment(number, render_escalation(reason, cls=cls))
        self.gh.edit_labels(number, add=[lb.needs_operator],
                            remove=[lb.needs_supervisor, *remove])

    def _do_supervise(self, target):
        """Apply the supervisor command waiting on an issue or PR in ``agent:needs-supervisor``
        (#182), read again now. Each command leaves a dispatcher ``supervision`` record with the
        command, its author and the outcome, written before the labels change: a record whose
        labels are lost spends the command, so that a retry is never run twice for one
        command.

        - ``escalate``: the item goes to ``agent:needs-operator`` with the reason.
        - ``accept-defaults``: only for a ``preflight_default`` escalation (``_accept_defaults``).
        - ``decide``: only for a ``preflight_default`` escalation, with the supervisor's text
          (``_decide``, #258).
        - ``retry``: only for ``tool_failure`` and ``sync_failed``, and only while the input of
          the step is the one that failed (the key of the escalation). A PR goes back to the
          label of its step, and the next tick runs it; an issue's step runs now. Retries
          write no review record, so they count no round. A retry of an issue blocked by an
          open issue (#277) waits, unspent, until the last block closes. After
          ``MAX_RETRIES`` retries for the same key (counted per reviewer, ``retries_done``) the
          next one is not run and the item goes to ``agent:needs-operator``, as it does when
          what made the escalation non-critical (``_supervised``) no longer holds.

        An escalation whose class is critical or unknown, or that was not routed to the
        supervisor, accepts no command: the item goes to ``agent:needs-operator``."""
        lb = self.cfg.labels
        is_pr = isinstance(target, PullRequest)
        number = target.number if is_pr else target
        if is_pr:
            fresh = self.gh.pr_state(number)
            labels = label_names(fresh.get('labels') or [])
            comments = self.gh.pr_comments(number)
        else:
            labels = label_names(self.gh.issue(number).get('labels') or [])
            comments = self.gh.issue_comments(number)
        if lb.needs_supervisor not in labels or operator_blocked(labels, lb):
            self.log('  not in agent:needs-supervisor any more; nothing to do')
            return
        command = self._supervisor_view(number, comments)
        if command is None:
            self.log('  no supervisor command to apply')
            return
        escalation = self._dispatcher_records(comments, 'notice', 'escalation')[-1][1]
        cls = escalation.get('class')
        name, by = command['command'], command['by']
        # A tool failure of an issue loses agent:ready on its way to the operator, as any failed
        # step does; the readiness check keeps it.
        keep_ready = is_pr or cls == PREFLIGHT_DEFAULT
        human_remove = [] if keep_ready else [lb.ready]

        def trace(outcome, text, **extra):
            record = dict(command=name, by=by, reason=command['reason'] or '',
                          comment=command['comment'], outcome=outcome, **extra)
            record['class'] = cls if isinstance(cls, str) else None
            self.gh.comment(number, render_supervision(
                record, f'`/devloop {name}` di `{by}`: {text.strip()}'))
            self.log(f'  {name} by {by}: {outcome}')

        if critical(cls) or escalation.get('to') != 'supervisor':
            trace('refused', 'rifiutato. L\'escalation è critica (classe '
                  f'`{cls}`) o non è affidata al supervisore: decide l\'operatore.')
            return self._to_operator(number, f'Il supervisore non può decidere questa escalation '
                                     f'(classe `{cls}`): `/devloop {name}` di `{by}` è stato '
                                     'rifiutato.', 'supervisor_refused', human_remove)
        if name == ESCALATE:
            reason = ' '.join(command['reason'].split())
            trace('escalated', f'l\'escalation passa all\'operatore: {reason}')
            return self._to_operator(number, f'Il supervisore `{by}` passa all\'operatore '
                                     f'l\'escalation di classe `{cls}`: {reason}',
                                     'supervisor_escalated', human_remove)
        if name in (ACCEPT_DEFAULTS, DECIDE):
            if is_pr or cls != PREFLIGHT_DEFAULT:
                return trace('refused', 'rifiutato: vale solo per un\'escalation `preflight_default` '
                             'del controllo di prontezza. Il comando è speso; l\'escalation '
                             'resta al supervisore.')
            if name == DECIDE:
                return self._decide(number, labels, comments, trace, command['reason'])
            return self._accept_defaults(number, labels, comments, trace)
        if cls not in (TOOL_FAILURE, SYNC_FAILED):
            return trace('refused', 'rifiutato: vale solo per le escalation `tool_failure` e '
                         '`sync_failed`. Il comando è speso; l\'escalation resta al supervisore.')
        # An issue a merged devloop PR already resolved is never checked nor implemented again
        # (#228), whatever escalation it waits in: the retry becomes a merged_issue escalation.
        merged = [] if is_pr else self._merged_prs(number)
        if merged:
            trace('refused', 'rifiutato: l\'issue è già risolta da una PR devloop mergiata ('
                  + ', '.join(f'#{m["number"]}' for m in merged) + '), quindi ripeterne il '
                  'passo duplicherebbe il lavoro. Il comando è speso; l\'issue passa a '
                  'un\'escalation `merged_issue`.')
            return self._do_merged_issue(number, merged)
        key, step = escalation.get('key'), escalation.get('step')
        if (not isinstance(key, dict) or step not in RETRY_STEPS or key.get('step') != step
                or is_pr == (step in ISSUE_STEPS) or (step == 'preflight'
                                                      and not self.cfg.preflight)):
            trace('refused', 'rifiutato: l\'escalation non registra un passo ripetibile.')
            return self._to_operator(number, 'L\'escalation non registra un passo che il '
                                     f'supervisore possa ripetere: `/devloop {name}` di `{by}` è '
                                     'stato rifiutato.', 'supervisor_refused', human_remove)
        current = self._current_key(number, key, target if is_pr else None)

        def superseded():
            return trace('refused', 'rifiutato: l\'input del passo è cambiato dopo '
                         'l\'escalation (' + ', '.join(f'`{k}`' for k in sorted(current)
                                                        if current[k] != key.get(k))
                         + '), quindi il comando si riferisce a un input superato. Il comando è '
                         'speso; l\'escalation resta al supervisore.')

        # What made the escalation non-critical is checked again, on data read now: a
        # phase:design label, an untrusted text or author, the PR's own merge mode or its
        # implementer may have changed while it waited. A PR is checked on its labels and head
        # read now and on its thread rebuilt from the comments read now, as the merge does: the
        # snapshot does not read the thread of a PR in agent:needs-supervisor, and without its
        # review and policy records the level recorded earlier would be lost. A PR whose head
        # moved is refused first: its merge mode would be the one of another diff.
        if is_pr:
            if current != key:
                return superseded()
            target = replace(target, labels=labels,
                             head_sha=fresh.get('headRefOid') or target.head_sha, reviews=[],
                             fixes=[], syncs=[], closures=[], commands=[], finding_problems=[],
                             policy=[], zero_head=None, zero_problem='', stale_receipt=False,
                             revoked=None, operator_comments=[], rereview=False,
                             round_comments=[], acceptance=[], hold=None)
            if target.meta:
                self._read_thread(target, comments)
        route, why = self._supervised(number, cls, step, target if is_pr else None,
                                      key.get('reviewer'), key=key)
        if route is None:
            why = why or 'la modalità di merge non è più `auto` con un supervisore'
            trace('refused', f'rifiutato: l\'escalation è diventata critica ({why}).')
            return self._to_operator(number, 'Il supervisore non può più decidere questa '
                                     f'escalation: {why}. `/devloop {name}` di `{by}` è stato '
                                     'rifiutato.', 'supervisor_refused', human_remove)
        if current != key:
            return superseded()
        # An issue blocked by an open issue (#277), read again now, does not run its step: the
        # command waits, with no record and no attempt spent, until the last block closes.
        if not is_pr:
            blocked_by = self._dependency_view([number])[number]['blocked_by']
            if blocked_by:
                self.log('  the retry waits, blocked by ' + ', '.join(blocked_by))
                return
        # Counted per reviewer: a review whose reviewers failed together, then one by one, keeps
        # the retries each one already had.
        done = retries_done(key, [r.get('key') for _, r in
                                  self._dispatcher_records(comments, 'supervision')
                                  if r['command'] == RETRY and r['outcome'] == 'applied'])
        if done >= MAX_RETRIES:
            trace('limit', f'non eseguito: il passo `{step}` è già stato ripetuto {done} volte '
                  'per lo stesso input.', key=key)
            return self._to_operator(number, f'Il passo `{step}` è fallito di nuovo dopo {done} '
                                     f'tentativi autorizzati dal supervisore (al massimo '
                                     f'{MAX_RETRIES}): `/devloop {name}` di `{by}` non è stato '
                                     'eseguito.', 'retry_limit', human_remove)
        trace('applied', f'il passo `{step}` viene ripetuto (tentativo {done + 1} di '
              f'{MAX_RETRIES}), senza contare un round di review.', key=key, attempt=done + 1)
        if is_pr:
            resume = escalation.get('resume')
            self.gh.edit_labels(number, add=[resume if resume in (lb.review, lb.changes)
                                             else lb.review], remove=[lb.needs_supervisor])
            return
        self.gh.edit_labels(number, remove=[lb.needs_supervisor])
        try:
            getattr(self, f'_do_{step}')(number)
        except (AgentError, GitHubError, WorkspaceError, UntrustedChange) as exc:
            self.log(f'  failed: {exc}')
            self._escalate_failure(Action(step, number, 'retry'), None, exc)

    def _accept_defaults(self, number, labels, comments, trace):
        """Accept the defaults of the latest readiness check of issue ``number`` (#182): only
        when every gap is ``default_available``, no split is needed, the issue has no
        ``phase:design`` label, was opened by a trusted user or a devloop app, its text is
        still approved, and the check is the one of the current text. The dispatcher's record
        lists the defaults and counts as a trusted comment, so the issue is checked again with
        them and, if ready, implemented. Anything else is refused: a gap the operator must
        decide sends the issue to ``agent:needs-operator``, a superseded check leaves it to the
        supervisor."""
        lb = self.cfg.labels
        record = self._decidable_check(number, labels, comments, trace,
                                       'I default del controllo di prontezza non possono essere '
                                       'accettati dal supervisore', 'i default si riferiscono')
        if record is None:
            return
        defaults = [{'description': g['description'], 'proposed_default': g['proposed_default']}
                    for g in record['gaps']]
        listed = '\n'.join(f'- {d["description"].strip()}\n  *Default accettato:* '
                           f'{d["proposed_default"].strip()}' for d in defaults)
        trace('accepted', 'default del controllo di prontezza accettati. Questo record conta come '
              'un commento fidato: l\'issue torna in coda e il controllo si ripete con questi '
              f'default.\n\n{listed}', defaults=defaults, hash=record['hash'])
        self.gh.edit_labels(number, remove=[lb.needs_supervisor])

    def _decide(self, number, labels, comments, trace, text):
        """Record the supervisor's decision ``text`` on the gaps of the latest readiness check
        of issue ``number`` (#258), under the conditions of ``_accept_defaults``: the supervisor
        may accept some defaults and replace others. The dispatcher's record carries the text
        and the gaps it answers, and counts as a trusted comment: ``agent:needs-supervisor``
        goes, and the readiness check runs again with the decision as context. The supervisor's
        own comment never reaches a prompt: only this record does."""
        lb = self.cfg.labels
        record = self._decidable_check(number, labels, comments, trace,
                                       'Le lacune del controllo di prontezza non possono essere '
                                       'decise dal supervisore', 'la decisione si riferisce')
        if record is None:
            return
        gaps = [{'description': g['description'], 'proposed_default': g['proposed_default']}
                for g in record['gaps']]
        listed = '\n'.join(f'- {g["description"].strip()}\n  *Default proposto:* '
                           f'{g["proposed_default"].strip()}' for g in gaps)
        quoted = '\n'.join(f'> {line}'.rstrip() for line in text.strip().splitlines())
        trace('decided', 'decisione sulle lacune del controllo di prontezza registrata. Questo '
              'record conta come un commento fidato: l\'issue torna in coda e il controllo si '
              f'ripete con questa decisione.\n\nLacune:\n\n{listed}\n\n**Decisione:**\n\n{quoted}',
              gaps=gaps, hash=record['hash'])
        self.gh.edit_labels(number, remove=[lb.needs_supervisor])

    def _decidable_check(self, number, labels, comments, trace, refusal, what):
        """The latest readiness check of issue ``number`` when the supervisor may decide its
        gaps (``accept-defaults`` or ``decide``), else None after the refusal is traced: every
        gap ``default_available`` and no split, no ``phase:design`` label, an issue opened by a
        trusted user or a devloop app whose text is still approved, and a check of the current
        text. A gap the operator must decide sends the issue to ``agent:needs-operator`` with
        ``refusal``; a superseded check leaves it to the supervisor. ``what``: what the command
        decides and its verb (``'i default si riferiscono'``), for the refusal of a superseded
        check."""
        history = self.gh.issue_history(number)
        records = self._preflight_records(comments)
        record = records[-1][1] if records else None
        problem = ('nessun controllo di prontezza fallito da decidere'
                   if record is None or record['ready']
                   else 'non tutte le lacune sono `default_available`, o serve una suddivisione: '
                        'decide l\'operatore' if preflight_class(record) != PREFLIGHT_DEFAULT
                   else f'l\'issue ha la label `{DESIGN_LABEL}`' if DESIGN_LABEL in labels
                   else 'l\'issue non è stata aperta da un utente fidato né da un\'app di devloop'
                   if not self._devloop_author(history['author'])
                   else self._untrusted_change(history))
        if problem:
            trace('refused', f'rifiutato: {problem}.')
            self._to_operator(number, f'{refusal}: {problem}.', 'operator_decision')
            return None
        if record['hash'] != self._request_hash(history, comments):
            trace('refused', 'rifiutato: il testo dell\'issue è cambiato dopo il controllo di '
                  f'prontezza, quindi {what} a un input superato. Il comando è speso; '
                  'l\'escalation resta al supervisore.')
            return None
        return record

    def _hold_command(self, pr, comments):
        """The hold or release command still to apply (#172), ``{event, by, reason, comment}``
        or None: the latest valid one not spent yet. A dispatcher ``hold`` record spends the
        command it applied, whose comment id it carries, and every command before it (GitHub's
        comment ids grow with time): a command written after that one stays to apply even when
        it precedes the record. Commands are accepted only when the merge mode of the PR, with
        the override of its level and its operator-only paths, is ``auto``, and from
        ``_may_hold``; any other, and a hold without a reason, is ignored and logged. devloop's
        own records are never commands."""
        spent, last = None, -1
        for position, comment in enumerate(comments):
            record = read_marker(comment.get('body', ''), 'hold')
            if record is None or not self._wrote((comment.get('author') or {}).get('login'),
                                                 DISPATCHER):
                continue
            applied = record.get('comment')
            if isinstance(applied, int) and not isinstance(applied, bool):
                spent = applied if spent is None else max(spent, applied)
            else:
                last = position  # a record without the id spends what precedes it
        mode = None
        found = None
        for comment in comments[last + 1:]:
            body = comment.get('body') or ''
            parsed = None if carries_record(body) else merge_command(body)
            if parsed is None:
                continue
            number = comment.get('id')
            if spent is not None and (not isinstance(number, int) or number <= spent):
                continue
            event, reason = parsed
            login = (comment.get('author') or {}).get('login')
            if self.cfg.merge.mode != 'auto':
                # Overrides only restrict: without "auto" in [merge] no PR is in "auto".
                why = f'merge mode is "{self.cfg.merge.mode}", not "auto"'
            elif not self._may_hold(login):
                why = 'the author is neither the supervisor nor a trusted user'
            elif reason is None:
                why = f'malformed, expected `{HOLD_COMMAND} <reason>`'
            else:
                if mode is None:
                    mode = self._merge_mode(pr) or 'unknown'
                why = (None if mode == 'auto'
                       else 'the merge mode of this PR is not computable now' if mode == 'unknown'
                       else f'merge mode of this PR is "{mode}", not "auto"')
            if why:
                self.log(f'  PR #{pr.number}: ignored `/devloop {event}` by {login}: {why}')
                continue
            found = {'event': event, 'by': login, 'reason': reason, 'comment': number}
        return found

    def _read_thread(self, pr, comments):
        """Fill ``pr`` with what its conversation records, from the identities allowed to write
        it: reviews, fixes, syncs, finding closures and commands, policy records, and the hold
        or release command still to apply (#172). The snapshot reads it once per tick; the merge
        reads it again just before merging, so that it never relies on an older view."""
        problems = []
        entries = sorted(self._marker_entries(pr, comments, 'review', problems),
                         key=lambda e: (e[1]['round'], e[0]))
        pr.reviews = [record for _, record in entries]
        pr.fixes = self._markers(pr, comments, 'fix')
        pr.syncs = self._markers(pr, comments, 'sync')
        pr.reruns = self._markers(pr, comments, 'rerun')
        pr.closures =[c for record in self._markers(pr, comments, 'finding')
                       for c in record['closed']]
        state = finding_state(pr.reviews, pr.closures)
        pr.finding_problems = problems + state.problems
        pr.commands = self._finding_commands(pr, comments, state)
        policy = self._marker_entries(pr, comments, 'policy')
        pr.policy = [record for _, record in policy]
        if self.ledger is not None:
            pr.ci_recorded = [
                (r.get('head'), r.get('state'), r.get('round'), r.get('checks'))
                for _, r in ledger_entries(comments, self._signed)
                if r['type'] == 'event' and r.get('event') == 'ci_round']
        last_review = max((position for position, _ in entries), default=-1)
        if policy and policy[-1][0] > last_review:
            record = policy[-1][1]
            if (record['event'] == 'promoted'
                    and record['receipt']['level'] == ZERO_LEVEL):
                pr.zero_head = record['receipt']['head']
        if entries:
            last = max(position for position, _ in entries)
            # A receipt found stale after the latest review voids its approval.
            stale = [record for position, record in policy
                     if position > last and record['event'] == 'stale']
            pr.stale_receipt = bool(stale)
            pr.revoked = stale[-1] if stale else None
            # Only what trusted users wrote after the latest verdict can void it.
            since = comments[last + 1:]
            pr.operator_comments = operator_comments(since, self.cfg.trusted_users)
            pr.rereview = any(requests_rereview(c.get('body'))
                              for c in pr.operator_comments)
            latest = entries[-1][1]['round']
            before = max((position for position, record in entries
                          if record['round'] < latest), default=-1)
            pr.round_comments = operator_comments(comments[before + 1:],
                                                  self.cfg.trusted_users)
        # Operator merge (#248): one notice per head, bound to the promotion it was written or
        # last edited for, and whether agent:needs-operator is the dispatcher's own.
        promotions = [position for position, record in policy if record['event'] == 'promoted']
        pr.promotions = len(promotions)
        notices = self._dispatcher_records(comments, 'notice')
        merges = [(position, record) for position, record in notices
                  if record['event'] == 'operator_merge']
        current = [(position, record) for position, record in merges
                   if record['promotion'] == pr.promotions]
        pr.operator_merges = [record['head'] for _, record in current]
        of_head = next((position for position, record in reversed(merges)
                        if record['head'] == pr.head_sha), None)
        pr.operator_notice = comments[of_head].get('id') if of_head is not None else None
        pr.operator_owned = self._operator_merge_owned(
            pr, current[-1] if current else None, notices, policy,
            promotions[-1] if promotions else -1)
        if pr.labels & {self.cfg.labels.review, self.cfg.labels.changes}:
            pr.fix_requests = self._fix_requests(pr, comments)
        # Last: whether a command is accepted depends on the merge mode of the PR, which its
        # review and policy records determine (#172).
        pr.hold = self._hold_command(pr, comments)

    def _fix_requests(self, pr, comments):
        """The fixes asked with ``FIX_COMMAND`` and not run yet (#195), ``{comment, by,
        request}``, oldest first. A fix record that answers commands spends them, whose comment
        ids it carries, and every command before them (GitHub's comment ids grow with time), as
        a hold record does (``_hold_command``); one without the ids spends what precedes it. A
        command counts from a trusted user or the supervisor's identity (``_may_hold``), with a
        request; a command on a head where a requested fix already ran is ignored until a new
        commit, so that commands cannot loop on the same head. Every ignored command is logged.
        devloop's own records are never commands."""
        spent, last, ran = None, -1, set()
        for position, record in self._marker_entries(pr, comments, 'fix'):
            if not record.get('requests'):
                continue
            ran.add(record.get('from'))
            ids = [r.get('comment') for r in record['requests']]
            if all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
                spent = max([*ids, *([spent] if spent is not None else [])])
            else:
                last = position
        found = []
        for comment in comments[last + 1:]:
            body = comment.get('body') or ''
            request = None if carries_record(body) else fix_command(body)
            if request is None:
                continue
            ident = comment.get('id')
            if spent is not None and (not isinstance(ident, int) or ident <= spent):
                continue
            login = (comment.get('author') or {}).get('login')
            why = ('the author is neither the supervisor nor a trusted user'
                   if not self._may_hold(login)
                   else f'malformed, expected `{FIX_COMMAND}` followed by the request'
                   if not request
                   else f'a fix asked with `{FIX_COMMAND}` already ran on head '
                        f'{pr.head_sha[:12]}: a new command counts after a new commit'
                   if pr.head_sha in ran else None)
            if why:
                self.log(f'  PR #{pr.number}: ignored `{FIX_COMMAND}` by {login}: {why}')
                continue
            found.append({'comment': ident, 'by': login, 'request': request})
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
        A sync whose conflicts an agent resolved, a fix or any other commit gives None.

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
            if not self.ws.clean_resync(pr.head_ref, pr.head_sha, approved):
                return None
        except WorkspaceError as exc:
            self.log(f'  resync of #{pr.number} not verifiable: {exc}')
            return None
        return {'reason': RESYNC_AFTER_APPROVAL, 'round': latest[0]['round'], 'from': approved}

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
        their body still ends with the marker. Who merged them does not matter."""
        found = []
        for raw in self.gh.merged_prs(f'{self.cfg.branch_prefix}{number}'):
            pr = self._devloop_pr(dict(raw, headRefOid=raw.get('headRefOid') or ''))
            if pr is not None and pr.issue == number:
                found.append({'number': pr.number,
                              'merge_commit': (raw.get('mergeCommit') or {}).get('oid')})
        return found

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
        ref, prefix = raw['headRefName'], self.cfg.branch_prefix
        suffix = ref[len(prefix):] if ref.startswith(prefix) else ''
        if not (suffix.isascii() and suffix.isdigit()):
            return None
        issue = int(suffix)
        meta = read_marker(raw.get('body', ''), 'pr')
        if meta is not None and (meta['issue'] != issue
                                 or not self._wrote(author, meta['implementer'])):
            meta = None
        return PullRequest(number=raw['number'], issue=issue,
                           head_ref=ref, head_sha=raw['headRefOid'],
                           title=raw['title'], body=raw.get('body', ''),
                           labels=label_names(raw['labels']), meta=meta or {},
                           merge_state=self.gh.merge_state(raw),
                           mergeable=raw.get('mergeable'), merge_status=raw.get('mergeStateStatus'))

    def snapshot(self):
        lb = self.cfg.labels
        prs = []
        for raw in self.gh.open_prs():
            pr = self._devloop_pr(raw)
            if pr is None:
                continue
            if pr.meta and pr.labels & {lb.review, lb.changes, lb.ready_to_merge}:
                self._read_thread(pr, self.gh.pr_comments(pr.number))
            # In agent:changes CI matters only to close an open CI finding (``plan``).
            ci_open = lb.changes in pr.labels and any(
                f['owner'] == CI for f in open_findings(pr.reviews, pr.closures))
            if pr.labels & {lb.review, lb.ready_to_merge} or ci_open:
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
                if (pr.meta and lb.review in pr.labels and pr.checks == 'none'
                        and self.cfg.local_tests != 'full'):
                    pr.ci_wait, pr.ci_missing = self._ci_wait(pr)
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
            prs.append(pr)
        ready = []
        pr_issues = {pr.issue for pr in prs}
        skip = set(self.cfg.blocked_labels) | {lb.working, lb.needs_supervisor}
        for raw in self.gh.issues_with_label(lb.ready):
            labels = label_names(raw['labels'])
            events = self.gh.labeled_events(raw['number'])
            # The latest agent:ready: its author admits the issue and its time is the issue's
            # seniority in the queue (#146).
            admitted = ([e for e in events if e['label'] == lb.ready] or [{}])[-1]
            # Whose labels count depends on who opened the issue (#178).
            admitters = self._admitters(author_login(raw.get('author')))
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
                issue['preflight'] = self._preflight_view(issue['number'], issue['labels'])
        working = [raw['number'] for raw in self.gh.issues_with_label(lb.working)]
        return Snapshot(prs=prs, ready_issues=ready, working_issues=working,
                        supervised=self._supervised_items(prs))

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
        with a supervisor command to apply (#182)."""
        lb = self.cfg.labels
        items = [(pr.number, True, self.gh.pr_comments) for pr in prs
                 if lb.needs_supervisor in pr.labels and not operator_blocked(pr.labels, lb)]
        items += [(raw['number'], False, self.gh.issue_comments)
                  for raw in self.gh.issues_with_label(lb.needs_supervisor)
                  if not operator_blocked(label_names(raw['labels']), lb)]
        found = []
        for number, is_pr, read in items:
            view = self._supervisor_view(number, read(number))
            if view is not None:
                found.append(dict(view, number=number, pr=is_pr))
        # A retry of an issue's step waits while the issue is blocked (#277), as a ready issue
        # does: ``blocked_by`` holds its open blocks.
        retries = [item for item in found if item['command'] == RETRY and not item['pr']
                   and item.get('step') in ISSUE_STEPS]
        views = self._dependency_view([item['number'] for item in retries]) if retries else {}
        for item in retries:
            item['blocked_by'] = views[item['number']]['blocked_by']
            if item['blocked_by']:
                self.log(f'  issue #{item["number"]}: the retry waits, blocked by '
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
        home = self.cfg.repo.lower()

        def key(item):
            repo = item.get('repo') or ''
            return ('' if repo.lower() == home else repo, item['number'])

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
        self.gh.comment(number, render_cycle({'cycle': cycle}))
        self.log('  dependency cycle reported: ' + ', '.join(cycle))

    # Readiness check (#114) ---------------------------------------------------
    def _preflight_records(self, comments):
        """``(position, record)`` of the readiness checks in ``comments``, each written by the
        identity of the configured agent it names."""
        found = []
        for position, comment in enumerate(comments):
            record = read_marker(comment.get('body', ''), 'preflight')
            login = (comment.get('author') or {}).get('login')
            if record and record['agent'] in self.cfg.agents and self._wrote(login, record['agent']):
                found.append((position, record))
        return found

    def _published(self, comment):
        """Whether ``comment`` may be text devloop published: the AGENTS.md header, from an
        identity allowed to write devloop records. Without GitHub Apps that is the operator's
        login too, so this alone cannot tell devloop from the operator."""
        return (devloop_text(comment.get('body'))
                and self._writes_records((comment.get('author') or {}).get('login')))

    def _ledger_record(self, comment):
        """Whether ``comment`` is a ledger record (#133) signed by the dispatcher."""
        return (read_marker(comment.get('body', ''), 'ledger') is not None
                and self._signed((comment.get('author') or {}).get('login')))

    def _automatic(self, comment):
        """Whether ``comment`` is one of devloop's own records: published text (``_published``)
        that ends with a valid devloop marker. Only these leave the readiness hash: any other
        trusted comment, even one starting with the header, may carry a requirement."""
        return self._published(comment) and carries_record(comment.get('body'))

    def _recheck_requested(self, number, comments):
        """Whether a trusted user asked in person for a new check with ``RECHECK_COMMAND`` as
        the first line of a comment written after devloop's latest record there: each request
        starts one check. Requests from anyone else are ignored and logged."""
        last = max((position for position, c in enumerate(comments) if self._automatic(c)),
                   default=-1)
        asked = False
        for comment in comments[last + 1:]:
            body = comment.get('body') or ''
            lines = body.strip().splitlines()
            if devloop_text(body) or not lines or lines[0].strip() != RECHECK_COMMAND:
                continue
            login = (comment.get('author') or {}).get('login')
            if login in self.cfg.trusted_users:
                asked = True
            else:
                self.log(f'  issue #{number}: ignored `{RECHECK_COMMAND}` by {login}: the author '
                         'is not a trusted user')
        return asked

    def _owns_block(self, number, comments, records):
        """Whether the issue's ``agent:needs-operator`` belongs to the readiness check: the
        latest check added it when the label was absent (its record says how many ``labeled``
        events of the label came before), the dispatcher wrote the only ``labeled`` event
        since, and no other text devloop may have published (another escalation,
        ``_published``) came after that check: an identity that writes no devloop record cannot
        void the block's attribution. The label counts under both its names (#248), the events
        of the old one included (``_operator_labelers``). Anything else, or anything uncertain,
        leaves the block to the operator."""
        if not records:
            return False
        position, record = records[-1]
        # The dispatcher's ledger records (#133), such as the turn of a recheck, are no
        # escalation: they leave the block's attribution as it is.
        if not record['blocked'] or any(self._published(c) and not self._ledger_record(c)
                                        for c in comments[position + 1:]):
            return False
        labelers = self._operator_labelers(number)
        return (len(labelers) == record['labeled'] + 1
                and self._wrote(labelers[-1], DISPATCHER))

    def _owned_block(self, labels):
        """Whether ``labels`` carry the operator label under one name only (#248): with both,
        which one the readiness check added cannot be told, and the block is the operator's."""
        return len(set(labels) & self.cfg.labels.operator()) == 1

    def _preflight_view(self, number, labels):
        """What planning needs about an issue's readiness check, read now from GitHub:
        ``{hash, record, recheck, owned}``, ``record`` being the latest check or None."""
        history = self.gh.issue_history(number)
        comments = self.gh.issue_comments(number)
        records = self._preflight_records(comments)
        return {'hash': self._request_hash(history, comments),
                'record': records[-1][1] if records else None,
                'recheck': self._recheck_requested(number, comments),
                'owned': (self._owned_block(labels)
                          and self._owns_block(number, comments, records))}

    def _publish_preflight(self, number, outcome, digest, agent_key, note='', base=None):
        """Record a check's outcome on the issue and apply it to ``agent:needs-operator``: add
        the label when the check fails and the label is absent, remove it when the check passes
        and the label belongs to the readiness check, under whichever name it has (#248). A
        label someone else set stays. ``base``: the commit the check ran on, None for an
        outcome reused (the latest base then)."""
        lb = self.cfg.labels
        agent = self.cfg.agent(agent_key)
        labels = label_names(self.gh.issue(number).get('labels') or [])
        comments = self.gh.issue_comments(number)
        records = self._preflight_records(comments)
        present = operator_blocked(labels, lb)
        owned = self._owned_block(labels) and self._owns_block(number, comments, records)
        shown = ', '.join(f'`{name}`' for name in sorted(labels & lb.operator()))
        record = dict(outcome, hash=digest, agent=agent_key, blocked=False, labeled=0)
        author = self._as(agent_key, ISSUE_COMMENT)
        head = self.runner.header(agent)
        if outcome['ready']:
            if owned:
                # Label first: if the comment is lost, the next tick checks the issue again.
                self.gh.edit_labels(number, remove=sorted(labels & lb.operator()))
                note = note or 'Il blocco messo dal controllo di prontezza è stato rimosso.'
            elif present:
                note = (f'{shown} resta: non risulta messo dal controllo di prontezza, '
                        'quindi lo rimuove una persona.')
            author.comment(number, render_preflight(record, head, note))
            self.log('  ready' + (', block removed' if owned else ''))
            return
        # The class of the escalation (#182): in "auto" gaps that are all defaults go to the
        # supervisor, with a dispatcher escalation that carries the key of the check's input.
        record['class'] = cls = preflight_class(outcome)
        supervised, why = (None, '') if present else self._supervised(
            number, cls, 'preflight',
            key=self._issue_key('preflight', base, digest) if base else None)
        if supervised:
            author.comment(number, render_preflight(record, head, note))
            self.gh.comment(number, render_escalation(
                'Il controllo di prontezza ha trovato solo lacune con un default proposto '
                '(`default_available`): il supervisore può accettarli con '
                f'`/devloop {ACCEPT_DEFAULTS}`, o decidere diversamente con `/devloop {DECIDE} '
                '<decisioni>`.', cls=cls, supervised=supervised))
            self.gh.edit_labels(number, add=[lb.needs_supervisor])
            self.log(f'  not ready: {len(record["gaps"])} gap(s), to the supervisor')
            return
        if why:
            note = note or (f'In modalità di merge `auto` queste lacune andrebbero al supervisore, '
                            f'ma l\'escalation resta all\'operatore: {why}.')
        if owned:
            record.update(blocked=True, labeled=records[-1][1]['labeled'])
        elif present:
            note = note or (f'{shown} era già presente e non è attribuito al controllo '
                            'di prontezza: un nuovo esito positivo non lo rimuove.')
        else:
            record.update(blocked=True, labeled=len(self._operator_labelers(number)))
        # Comment first: if the label is lost, the next tick blocks the issue again.
        author.comment(number, render_preflight(record, head, note))
        if record['blocked'] and not owned:
            self.gh.edit_labels(number, add=[lb.needs_operator])
        self.log(f'  not ready: {len(record["gaps"])} gap(s)')

    def _do_preflight(self, number):
        """Run the readiness check on a checkout of the base branch, with the context the
        implementer gets, then record and apply its outcome."""
        cfg = self.cfg
        agent = cfg.agent(cfg.preflight)
        history = self.gh.issue_history(number)
        reason = self._untrusted_change(history)
        if reason:
            raise UntrustedChange(f'issue #{number}: {reason}')
        comments = self.gh.issue_comments(number)
        digest = self._request_hash(history, comments)
        prompt = _template('preflight', repo=cfg.repo, base_branch=cfg.base_branch,
                           issue_number=number, issue_title=history['title'],
                           issue_body=history['body'],
                           issue_comments=_render_issue_comments(
                               comments, cfg.trusted_users,
                               [read_marker(c['body'], 'supervision')
                                for c in comments if self._accepted(c)]),
                           language=cfg.language)
        name = f'issue-{number}-preflight'
        path = self.ws.base_copy(name)
        try:
            # The input the check runs on (#182), bound before it runs: the commit of its
            # checkout and the hash of the request its prompt gets.
            base = self.ws.head(path)
            key = self._issue_key('preflight', base, digest)
            # A denied permission is retried once, in the same checkout (#180), as in ``_turn``.
            for label in (name, f'{name}-retry'):
                entry = self._open_turn((number, 'preflight'), agent, 'review', label)
                try:
                    raw = self.runner.run(agent, 'review', prompt, 'preflight', path, label)
                except PermissionDenied as exc:
                    self._close_turn(entry, 'failure', exc)
                    error = self._permission_denied(agent, exc, retry=label == name)
                    if error is None:
                        continue
                    error.key = key
                    raise error from exc
                except AgentError as exc:
                    self._close_turn(entry, 'unavailable' if isinstance(exc, ModelUnavailable)
                                     else 'failure', exc)
                    exc.key = key
                    raise
                self._close_turn(entry, 'success')
                break
        finally:
            self.ws.remove(name)
        self._publish_preflight(number, normalize_preflight(raw), digest, agent.key, base=base)

    def _do_preflight_block(self, number):
        """Block again an issue whose latest check, still valid for its text, failed, after its
        ``agent:needs-operator`` was removed: the outcome is reused, not recomputed."""
        view = self._preflight_view(number, ())
        if preflight_state(view)[0] != 'not_ready':
            self.log('  the readiness check changed since the snapshot; next tick')
            return
        record = view['record']
        outcome = {k: record[k] for k in ('ready', 'gaps', 'split')}
        self._publish_preflight(number, outcome, view['hash'], record['agent'],
                                note='Esito del controllo precedente, riusato: il testo '
                                     'dell\'issue non è cambiato.')

    # Tick ----------------------------------------------------------------
    def tick(self, dry_run=False):
        if self.workers is not None and not dry_run:
            return self._tick_continuous()
        self._auto_pin(dry_run)
        self._transition_labels(dry_run)
        snap = self.snapshot()
        self._flag_incomplete(snap, dry_run)
        actions = plan(snap, self.cfg)
        prs = {pr.number: pr for pr in snap.prs}
        self.log(f'{"[dry-run] " if dry_run else ""}{pr_counts_line(snap, self.cfg)}')
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
            self._run_action(action, prs)
        return actions

    def _tick_continuous(self):
        """A tick of the continuous mode (#227): plan around the actions still running and
        start in ``self.workers`` those that fit the free slots (``admit``), without waiting for
        them. Returns the actions started.

        The pin after a merge needs every action ended, since a new copy restarts the process:
        while one is pending and something runs, nothing starts."""
        workers = self.workers
        # An action that ends from here on wakes the loop again: its result is not in this plan.
        workers.clear()
        running = workers.running()
        if not running:
            self._auto_pin(False)
        elif self.pinner is not None and self.cfg.pin.auto and self.pinner.pending():
            self.log(f'auto-pin pending: no action starts until the {len(running)} running '
                     'ones end')
            return []
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
        reserved = sum(1 for action, _ in running
                       if action.kind in ('implement', 'preflight')
                       or action.kind == 'supervise' and action.number not in prs)
        actions = plan(snap, self.cfg, busy, reserved)
        started = admit(actions, issue_of, running, workers.free())
        self.log(f'{pr_counts_line(snap, self.cfg)}; running: '
                 + (', '.join(f'{a.kind} #{a.number}' for a, _ in running) or 'none'))
        for action in started:
            workers.start(action, numbers_of(action.number, issue_of),
                          lambda action=action: self._run_started(action, prs))
        return started

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
            note = transition_labels(self.gh, lb)[1]
        except GitHubError as exc:
            self.log(f'labels: the rename of {LEGACY_NEEDS_OPERATOR} could not be checked, '
                     f'next tick: {exc}')
            return
        if note:
            self.log(f'labels: {note}')
        self.labels_checked = True

    def _auto_pin(self, dry_run):
        """With ``[pin] auto = true`` and a pin requested by a merge (#174), pin
        ``origin/<base_branch>`` before planning. A failed pin stops the tick with ``PinError``
        and leaves the pointer and the request as they were; a new copy raises ``Repinned``, so
        that the process starts again from it.

        A copy refused by the smoke test (``SmokeError``, #260) is not a failed tick: the request
        is dropped, ``current`` stays, planning goes on from the running copy, and an alert goes
        to ``[run].alert_issue`` once per refused commit."""
        if self.pinner is None or not self.cfg.pin.auto:
            return
        request = self.pinner.pending()
        if not request:
            return
        ref = f'origin/{self.cfg.base_branch}'
        why = f'the merge of #{request.get("pr")}' if request.get('pr') else 'a merge'
        if dry_run:
            self.log(f'[dry-run] auto-pin of {ref} pending after {why}')
            return
        self.log(f'auto-pin of {ref} after {why}')
        try:
            copy = self.pinner.pin(ref)
        except SmokeError as exc:
            self.pinner.cancel()
            self.log(f'auto-pin of {ref} refused, the pinned copy is unchanged and the request '
                     f'is dropped: {exc}')
            self._alert_refused_pin(ref, exc)
            return
        except (PinError, ConfigError, OSError) as exc:
            raise PinError(f'auto-pin of {ref} failed, the pinned copy is unchanged: {exc}') \
                from exc
        if Path(copy).resolve() != INSTALL_ROOT.resolve():
            raise Repinned(copy)

    def _alert_refused_pin(self, ref, exc):
        """The alert of a copy refused by the smoke test of an auto-pin (#260), on
        ``[run].alert_issue``, once per commit: its commit and failed step, never the error's
        text (it stays in the local log). One attempt; a failure is logged."""
        issue = self.cfg.run.alert_issue
        if not issue:
            self.log('  no [run].alert_issue is configured: no alert for the refused pin')
            return
        if self.pinner.alerted(exc.sha):
            self.log(f'  the refusal of {exc.sha[:12]} was already reported on #{issue}')
            return
        body = publishable(
            dispatcher_header()
            + '\n## Pin automatico rifiutato dallo smoke test\n\n'
            + f'La copia di `{ref}` al commit `{exc.sha[:12]}` non ha superato lo smoke test '
              f'al passo `{exc.step}`. `current` resta sulla copia precedente, la richiesta di '
              'pin è annullata e il ciclo continua.\n\n'
            + 'Il dettaglio è nel log locale; la copia rifiutata resta in '
              f'`rejected-{exc.sha[:12]}` nella directory di stato, per l\'analisi.')
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
                    self.log(f'  unexpected error: {type(exc).__name__}: {exc}')
                    try:
                        self._escalate_failure(action, prs.get(action.number), RuntimeError(
                            f'errore inatteso del dispatcher ({type(exc).__name__}: {exc})'))
                    except Exception as again:
                        self.log(f'  could not escalate: {again}')
                    continue
                first = (str(failed).strip().splitlines() or [type(failed).__name__])[0]
                outcomes[position] = f'failed: {first}' if failed else 'done'

    # Helpers -------------------------------------------------------------
    def _escalate(self, number, reason, detail='', remove=(), cls='failure', supervised=None,
                  why=''):
        """Escalate issue or PR ``number`` with an escalation of class ``cls`` (#182). With
        ``supervised`` (``_supervised``) it goes to ``agent:needs-supervisor`` and an issue
        keeps ``agent:ready``; otherwise to ``agent:needs-operator``, with ``why`` a non-critical
        class in ``auto`` stays with the operator."""
        lb = self.cfg.labels
        if supervised:
            self.gh.comment(number, render_escalation(reason, detail, cls, supervised))
            self.gh.edit_labels(number, add=[lb.needs_supervisor],
                                remove=[label for label in remove if label != lb.ready])
            return
        if why:
            reason = (reason.rstrip() + f'\n\nIn modalità di merge `auto` questa escalation '
                      f'andrebbe al supervisore, ma resta all\'operatore: {why}.')
        self.gh.comment(number, render_escalation(reason, detail, cls))
        self.gh.edit_labels(number, add=[lb.needs_operator], remove=remove)

    def _log_hint(self, exc):
        log = getattr(exc, 'log', None)
        hint = f' Log locale: `{Path(log).name}` in `{self.cfg.state_dir}/logs`.' if log else ''
        if getattr(exc, 'tail', ''):
            hint += (f' Le ultime righe dell\'output (al massimo {TAIL_LINES}, senza segreti né '
                     'percorsi assoluti) sono in fondo al commento.')
        return hint

    def _escalate_failure(self, action, pr, exc):
        """Escalate a failed action. Its class (#182): ``tool_failure`` when the agent or its
        model produced no result (``AgentError``: a crash, a timeout, a stall, an unavailable
        model, a denied permission), ``untrusted`` for an issue text no longer trusted,
        ``failure`` for anything else (``gh``, git, the dispatcher itself)."""
        lb = self.cfg.labels
        reason = f'Il passo `{action.kind}` è fallito: {_failure_text(exc)}{self._log_hint(exc)}'
        cls = (TOOL_FAILURE if isinstance(exc, AgentError)
               else 'untrusted' if isinstance(exc, UntrustedChange) else 'failure')
        number = pr.number if pr is not None else action.number
        remove = ([lb.review, lb.changes, lb.ready_to_merge] if pr is not None
                  else [lb.working, lb.ready])
        try:
            supervised, why = self._supervised(number, cls, action.kind, pr,
                                               getattr(exc, 'reviewer', None),
                                               key=getattr(exc, 'key', None))
            self._escalate(number, reason, getattr(exc, 'tail', ''), remove=remove, cls=cls,
                           supervised=supervised, why=why)
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
        # implement turn's retries.
        issue = dict(self.gh.issue(number), title=history['title'], body=history['body'],
                     labeled=history['labeled'], admitters=self._admitters(history['author']),
                     accepted=accepted, request=self._request_hash(history, listed))
        return issue, _render_issue_comments(issue.get('comments') or [], self.cfg.trusted_users,
                                             [read_marker(c['body'], 'supervision')
                                              for c in accepted])

    # Ledger (#133) -----------------------------------------------------------
    def _signed(self, login):
        """Whether ``login`` signs as the dispatcher, as its other records do."""
        return self._wrote(login, DISPATCHER)

    def _open_turn(self, where, candidate, mode, label):
        """The ledger record of a turn about to start, or None without a ledger. A failed write
        raises ``GitHubError``: the turn does not start."""
        if self.ledger is None or where is None:
            return None
        number, kind = where
        return self.ledger.open_turn(
            number, kind=kind, agent=candidate.key, identity=self._app_login(candidate.key),
            mode=mode, model=candidate.model, effort=candidate.effort,
            fallback_for=candidate.fallback_for, label=label)

    def _close_turn(self, entry, outcome, error=None):
        """Close a turn's record with what the CLI attested and the last command its output
        exposes (#215). A failed write leaves the turn open, and the next tick marks it
        incomplete."""
        if entry is None:
            return
        try:
            self.ledger.close_turn(entry, outcome, getattr(self.runner, 'last_report', None),
                                   error, getattr(self.runner, 'last_command', None))
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
        modules = select_modules(self.ws.worktree_changes(path), self.ws.test_modules(path))
        return (module_command(modules) if modules else None), {'mode': mode, 'modules': modules}

    def _run_tests(self, where, path, attempt, command):
        """Run ``command``, recording it in the ledger with the tree tested; returns
        ``(passed, tail, entry)``."""
        if self.ledger is None or where is None:
            return (*self.ws.run_tests(path, command, self.cfg.test_timeout_seconds), None)
        before = self.ws.tree(path)
        started = time.monotonic()
        passed, tail = self.ws.run_tests(path, command, self.cfg.test_timeout_seconds)
        duration = int((time.monotonic() - started) * 1000)
        after = self.ws.tree(path)
        entry = self.ledger.verification(where[0], kind=where[1], attempt=attempt,
                                         command=command, passed=passed,
                                         duration_ms=duration, tree_before=before,
                                         tree_after=after)
        return passed, tail, entry

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
                self.log(f'  the tree of {commit[:12]} is not the one tested: commit not bound')
        except GitHubError as exc:
            self.log(f'  verification record on #{tested.number} not updated: {exc}')

    def _ledger_event(self, number, event, **fields):
        if self.ledger is not None:
            self.ledger.event(number, event, **fields)

    def _read_ci(self, head):
        """``(state, checks)`` of ``head``'s CI: with a ledger, from the same read that gives
        every check to record (``check_details``); without one, ``checks`` is None."""
        if self.ledger is None:
            return self.gh.checks(head)[0], None
        details = self.gh.check_details(head)
        return summarize_checks(details)[0], details

    def _record_ci(self, pr, head, state, details, round_no):
        """Record the CI of ``head`` as a ``ci_round`` event (#133), failed or passed: each
        check with its outcome and, for a GitHub Actions run, its id (a commit status has none).
        The same checks read again for the same head and round are not recorded twice."""
        if self.ledger is None or details is None:
            return
        checks = [{'name': c['name'], 'outcome': c.get('conclusion') or c['bucket'],
                   'run_id': c.get('run_id')} for c in details]
        key = (head, state, round_no, checks)
        if key in pr.ci_recorded:
            return
        # A level-0 promotion has no review round.
        extra = {'round': round_no} if round_no is not None else {}
        self.ledger.event(pr.number, 'ci_round', head=head, state=state, **extra,
                          checks=checks)
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
        once checked."""
        if self.ledger is None:
            return
        lb = self.cfg.labels
        try:
            traced = self.ledger.pending()
        except GitHubError as exc:
            self.log(f'  trace of the open turns not readable: {exc}')
            traced = []
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

    def _permission_denied(self, candidate, exc, retry):
        """Log an agy turn stopped on a denied permission (``PermissionDenied``, #180) with the
        permission and its target. Returns None when ``retry``, the turn being run once more;
        otherwise the error to escalate, which names the permission denied again."""
        target = exc.target or 'target non riportato da agy'
        if retry:
            self.log(f'  {candidate.key}: model {candidate.model} denied the '
                     f'"{exc.permission}" permission ({target}); retrying once')
            return None
        self.log(f'  {candidate.key}: model {candidate.model} denied the '
                 f'"{exc.permission}" permission again ({target})')
        return PermissionDenied(f'{exc}; permission `{exc.permission}` denied again after one '
                                'retry', exc.log, exc.permission, exc.target)

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

    def _turn(self, models, mode, prompt, schema, path, label, allowed=None, where=None):
        """Run one agent turn on the first of ``models`` (``Models``) that is available, and
        return its output and the agent configuration it ran with.

        A model whose turn fails as unavailable (``ModelUnavailable``) leaves ``models``, so the
        action never tries it again, and the next one runs with the same prompt and worktree,
        plus a note about the partial work. Every other failure ends the turn as before, listing
        the models tried or left out if any (``ModelsUnavailable`` with ``last_failed``). A model
        that ``allowed`` refuses leaves ``models`` without running. When none is left, an agent
        without fallback models fails as before #121; one with them fails listing the models
        tried and why (``ModelsUnavailable``).

        An agy turn stopped on a permission headless mode cannot ask (``PermissionDenied``) is a
        failure of the tool (#180): the same model runs the same turn once more, in the same
        directory, with ``-retry`` in its label. A second denial on the same model fails as any
        other error, naming the permission.

        An agy turn that ended with no result and no error (``NoOutput``, #222) leaves
        ``models`` as an unavailable model does, without being remembered: the next model runs.
        On the last model of the chain the same model runs once more, with ``-retry`` in its
        label; a second empty run fails as any other error.

        ``where``, ``(number, kind)``, is the issue or PR whose ledger records each CLI
        invocation as a turn (#133): written before the CLI starts, closed when it ends.

        With a memory (#151), an agent with fallback models records each model found
        unavailable, and skips a remembered one until its record expires, without running it:
        it leaves ``models`` and is listed in ``models.skipped`` and on the configuration
        returned (``skipped``). When every model of the chain is remembered, no CLI runs and
        the turn fails listing each one with its reason and time (``ModelsUnavailable``). A
        model that answers is forgotten."""
        failed = None
        retried = set()  # the models already denied a permission in this call
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
            text = prompt + FALLBACK_NOTE[mode] if failed else prompt
            suffix = f'-{candidate.model}' if candidate.fallback_for else ''
            # Its own log: the first attempt's stays as it was.
            suffix += '-retry' * ((candidate.model in retried) + (candidate.model in emptied))
            entry = self._open_turn(where, candidate, mode, label + suffix)
            try:
                output = self.runner.run(candidate, mode, text, schema, path, label + suffix)
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
                    retried.add(candidate.model)
                    models.left.insert(0, candidate)
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
                error.tail = exc.tail
                raise error from exc
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

    def _write_turn(self, agent, prompt, schema, path, label, where=None):
        """Run a writing agent, then the local tests (``_verify``); retry with the failures.
        ``prompt`` is the text, or a function of the attempt (0 first) that renders it, so that
        each attempt gets what its worktree holds then (#203).
        Returns the output, ``None`` on success or a reason to escalate, the agent configuration
        of the last turn (``_turn``), the ledger record of the passing test, to bind to the
        commit that follows (None without a ledger or without a passing test), and the mode and
        modules of the local tests of the last attempt (None when no test was due: a blocked
        turn, or an implementation without changes). A model found
        unavailable is not tried again in later attempts."""
        models = Models(agent)
        failures = ''
        for attempt in range(1 + self.cfg.implement_test_retries):
            text = (prompt(attempt) if callable(prompt) else prompt) + failures
            result, used = self._turn(models, 'write', text, schema, path,
                                      f'{label}-{attempt + 1}', where=where)
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
            failures += ('\n\n## Test failures after your previous attempt\n\nThe test command '
                         f'failed. Fix the cause, then finish again.\n\n```text\n{tail}\n```\n')
        return (result, ('I test locali falliscono anche dopo i tentativi di correzione.', tail),
                used, None, local)

    def _commit_message(self, agent, message, issue):
        message = message.strip() + f'\n\nRefs #{issue}\n'
        if agent.commit_trailer:
            message += f'\n{agent.commit_trailer}\n'
        return message

    def _blocked(self, number, agent, reason, remove, on_pr):
        author = self._as(agent.key, PR_COMMENT if on_pr else ISSUE_COMMENT)
        author.comment(number, publishable(self.runner.header(agent) + '\n## Lavoro bloccato\n\n'
                                           + reason.strip() + '\n\n' + RESUME,
                                           'notice', {'event': 'blocked', 'class': 'blocked',
                                                      'critical': True, 'to': 'operator'}))
        self.gh.edit_labels(number, add=[self.cfg.labels.needs_operator], remove=remove)

    def _protected_changes(self, path, since=None):
        """Changed files under ``protected_paths``, which devloop never pushes."""
        return [f for f in self.ws.changed_files(path, since)
                if any(f.startswith(prefix) for prefix in self.cfg.protected_paths)]

    def _escalate_protected(self, number, files, remove):
        listed = '\n'.join(f'- `{f}`' for f in files[:20])
        self._escalate(number, 'La modifica tocca percorsi protetti (`protected_paths`), che devloop '
                               'non pubblica: un workflow o una action locale su un branch di questo '
                               'repository verrebbe eseguito dalla CI prima di qualsiasi review. Il '
                               'commit resta nel worktree locale; la modifica va applicata a mano.'
                               '\n\n' + listed, remove=remove, cls='protected_paths')

    def _approval_current(self, pr, review):
        """Re-read head and CI just before promoting: approval holds only for the reviewed commit
        with CI passed or absent. Otherwise the PR stays in review for the next tick."""
        head = self.gh.pr_head(pr.number)
        if head != review['head_sha']:
            self.log(f'  not promoted: head moved to {head[:12]} after the review')
            return False
        state, details = self._read_ci(head)
        if state not in ('pass', 'none'):
            self.log(f'  not promoted: CI is {state}')
            return False
        self._record_ci(pr, head, state, details, review['round'])
        return True

    # Review policy -----------------------------------------------------------
    def _receipt(self, pr):
        """The policy receipt of the PR's pushed head, computed now from the running
        dispatcher's policy: the full diff against the latest base commit, every ``labeled``
        event of the level label on the PR, and the highest level already recorded on it."""
        return self._receipt_and_paths(pr)[0]

    def _receipt_and_paths(self, pr):
        """``_receipt`` and every path of the diff it was computed on."""
        policy = self.cfg.review_policy
        base, head, changes = self.ws.review_diff(pr.head_ref)
        records = pr.reviews + pr.policy
        # Level 0 (#107): only read when enabled, so that without it nothing changes.
        zero = (self.gh.label_events(pr.number, policy.zero_label) if policy.zero_enabled()
                else [])
        receipt = evaluate(policy, pr=pr.number, base=base, head=head, changes=changes,
                           labelers=self.gh.labelers(pr.number, policy.label),
                           trusted_users=self.cfg.trusted_users,
                           previous_level=recorded_level(records),
                           select=lambda level: self._select(pr, level)[0],
                           profile=lambda key: self._profile(key, 'review'),
                           zero_events=zero, refused=zero_refused(records))
        return receipt, sorted({c['path'] for c in changes})

    def _base_check(self, old, new, paths):
        """Whether the base branch moving from ``old`` to ``new`` left the PR alone (#160), as
        a record: ``{from, to, outcome}``, the outcome ``disjoint``, ``touched`` (with
        ``paths``, the files of the PR it reached) or ``unverifiable`` (the commits in between
        cannot be listed). Every commit in between counts, not only their net diff.
        ``_base_checks`` adds the mergeability on the current base."""
        try:
            touched = self.ws.base_paths(old, new)
        except WorkspaceError as exc:
            self.log(f'  base {old[:12]}..{new[:12]} not verifiable: {exc}')
            return {'from': old, 'to': new, 'outcome': 'unverifiable'}
        hits = touched_by_base(paths, touched)
        if hits:
            return {'from': old, 'to': new, 'outcome': 'touched',
                    'paths': hits[:MAX_LISTED_PATHS]}
        return {'from': old, 'to': new, 'outcome': 'disjoint'}

    def _base_checks(self, records, current, paths):
        """For records whose receipts differ from ``current`` in the base at most: the check of
        each earlier base up to the current one (``_base_check``), ``[]`` when no base
        differs. None when a receipt is missing or differs in anything else.

        A base that left the PR's files alone is accepted only if the receipt's head also
        merges into the current base without conflicts, verified on these two exact commits:
        otherwise its check becomes ``conflict`` (with ``paths``, the files in conflict) or
        ``unverifiable``. The mergeability GitHub reported in the snapshot may predate the
        base."""
        checks, seen = [], set()
        for record in records:
            changed = differences(receipt_of(record), current)
            if changed and changed != ['base']:
                return None
            old = receipt_of(record)['base']
            if changed and old not in seen:
                seen.add(old)
                checks.append(self._base_check(old, current['base'], paths))
        if any(c['outcome'] == 'disjoint' for c in checks):
            merge = self._merge_check(current['base'], current['head'])
            if merge['outcome'] != 'clean':
                checks = [dict(c, **merge) if c['outcome'] == 'disjoint' else c for c in checks]
        return checks

    def _merge_check(self, base, head):
        """Whether ``head`` merges into ``base`` without conflicts: ``{outcome}``, ``clean``,
        ``conflict`` (with ``paths``) or ``unverifiable``."""
        try:
            clean, files = self.ws.merge_check(base, head)
        except WorkspaceError as exc:
            self.log(f'  merge of {head[:12]} into {base[:12]} not verifiable: {exc}')
            return {'outcome': 'unverifiable'}
        if clean:
            return {'outcome': 'clean'}
        self.log(f'  {head[:12]} conflicts with the base {base[:12]}')
        return {'outcome': 'conflict', 'paths': files[:MAX_LISTED_PATHS]}

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

    def _select(self, pr, level):
        """``(reviewers, problem)``: the reviewers ``level`` requires on the PR, in order, or
        none and why.

        Level 1 keeps the rule from before #111: the PR's recorded reviewer, else the first of
        the pool, never the PR's implementer. Level 2 takes the first pair of the pool, in
        configuration order, that is distinct and independent of everyone who wrote code on
        the PR: the implementer, every fixer and every agent that resolved conflicts in a sync,
        as their markers record them, and the agent configured now under the implementer's
        key, which runs fixes and syncs, with each of its models (#121). When a marker does not record who wrote the code,
        independence cannot be verified."""
        cfg, implementer = self.cfg, pr.meta['implementer']
        if level == 1:
            for key in (pr.meta['reviewer'], *cfg.pool()):
                if key in cfg.agents and key != implementer:
                    return [key], ''
            return [], (f'nessun reviewer configurato è diverso dall\'implementer della PR '
                        f'(`{implementer}`)')
        authors = self._authors(pr)
        if authors is None:
            return [], ('non è verificabile che i reviewer siano indipendenti da chi ha scritto '
                        'il codice: la PR, una sua correzione o una risoluzione di conflitti '
                        'non registra identità e tupla dell\'agent (marker precedenti a #111)')
        candidates = [(key, self._profile(key, 'review')) for key in cfg.pool()
                      if key in cfg.agents and key != implementer]
        candidates = [(key, p) for key, p in candidates if all(independent(p, a) for a in authors)]
        for group in itertools.combinations(candidates, level):
            if all(distinct(a[1], b[1]) for a, b in itertools.combinations(group, 2)):
                return [key for key, _ in group], ''
        pool = ', '.join(f'`{key}`' for key in cfg.pool())
        return [], (f'il pool di reviewer ({pool}) non ne ha abbastanza: due reviewer contano '
                    'come distinti solo se differiscono sia per identità GitHub sia per la tupla '
                    '(kind, model, effort, review_args), e nessuno può avere l\'identità o il '
                    'modello (kind, model, effort) dell\'implementer o di chi ha corretto la PR')

    def _policy_comment(self, pr, title, text, receipt, event, bases=(), cls=None):
        """A dispatcher ``policy`` record; ``bases``: the base checks it relied on (#160);
        ``cls``: the class of the escalation it records, if any (#182)."""
        payload = {'event': event, 'receipt': receipt}
        if bases:
            payload['bases'] = list(bases)
        if cls:
            payload['class'] = cls
        self.gh.comment(pr.number, publishable(
            dispatcher_header() + f'\n## {title}\n\n{text.strip()}\n\n'
            + describe_receipt(receipt), 'policy', payload))

    def _escalate_level(self, pr, receipt):
        """Not enough independent reviewers for the level: no review, no promotion, the
        operator decides."""
        lb, level = self.cfg.labels, receipt['level']
        needed = 'un reviewer indipendente' if level == 1 else f'{level} reviewer indipendenti'
        self._policy_comment(
            pr, f'Review di livello {level}: serve un intervento dell\'operatore',
            f'La policy di review del dispatcher richiede per questa PR il livello {level}, '
            f'quindi {needed}, ma {self._select(pr, level)[1]}. devloop non revisiona né '
            'promuove la PR: la review e la decisione sul merge spettano a una persona. Il '
            'livello resta acquisito anche se la label viene rimossa o il diff cambia.',
            receipt, 'level', cls='review_level')
        self.gh.edit_labels(pr.number, add=[lb.needs_operator],
                            remove=[lb.review, lb.changes, lb.ready_to_merge])

    def _promote(self, pr, records):
        """Promote a round whose approvals reach the quorum: every reviewer the receipt
        recomputed now requires approved the head with that same receipt. Otherwise record why
        and leave the PR in review for a new review of the same head."""
        lb = self.cfg.labels
        current, paths = self._receipt_and_paths(pr)
        if current['level'] == ZERO_LEVEL:
            return self._promote_zero(pr, current)
        if len(current['reviewers']) < current['level']:
            return self._escalate_level(pr, current)
        bases = self._approvals_match(pr, records, current, paths)
        if bases is None:
            return
        by = ', '.join(f'`{key}`' for key in current['reviewers'])
        branch = self.cfg.base_branch
        revalidated = (f'\n\nLe approvazioni date su una base precedente restano valide sulla '
                       f'base corrente: i commit intermedi di `{branch}` non toccano file della '
                       'PR.\n\n' + _describe_bases(bases, branch)) if bases else ''
        self._policy_comment(
            pr, 'Pronta per il merge',
            f'Approvazione del commit `{records[0]["head_sha"][:12]}` da parte di {by} '
            'confermata: la ricevuta di policy ricalcolata coincide con quella della review'
            + (', a parte la base' if bases else '') + '.' + revalidated,
            current, 'promoted', bases)
        self.gh.edit_labels(pr.number, add=[lb.ready_to_merge], remove=[lb.review, lb.changes])

    def _approvals_match(self, pr, records, current, paths, step='promozione'):
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
        approved = {owner_of(r) for r in records if r['verdict'] == 'approve'}
        missing = [key for key in current['reviewers'] if key not in approved]
        branch = self.cfg.base_branch
        if changed or missing:
            what = ('l\'approvazione non ha una ricevuta di policy'
                    if any(receipt_of(r) is None for r in records)
                    else 'cambiati: ' + ', '.join(_RECEIPT_FIELDS[k] for k in changed) if changed
                    else 'manca l\'approvazione di ' + ', '.join(f'`{k}`' for k in missing))
            detail = (f'\n\nCommit di `{branch}` fra la base dell\'approvazione e quella '
                      'corrente:\n\n' + _describe_bases(bases, branch)) if bases else ''
            payload = {'event': 'stale', 'receipt': current, 'bases': bases or []}
            # The new review of this same head does not count against the limit when only
            # the base revoked the approval (#252); any other revocation opens a round as usual.
            rounds = ('La nuova review dello stesso commit apre un nuovo round, che non conta '
                      'per il limite dei round; i finding aperti restano invariati.'
                      if records and base_revocation(payload, records[0]['head_sha'])
                      else 'La nuova review apre un nuovo round; i finding aperti restano '
                      'invariati.')
            self._policy_comment(
                pr, f'Approvazione non valida per {"il" if step == "merge" else "la"} {step}',
                f'La ricevuta di policy ricalcolata prima {"del" if step == "merge" else "della"} '
                f'{step} non coincide con '
                f'quella dell\'approvazione ({what}). La PR resta in review: serve una nuova '
                'review, anche dello stesso commit. ' + rounds
                + detail, current, 'stale', bases)
            self.gh.edit_labels(pr.number, add=[lb.review],
                                remove=[lb.changes, lb.ready_to_merge, *self._released(pr)])
            return None
        return bases

    def _promote_zero(self, pr, receipt):
        """Level 0 (#107): promote the head without a review, only if no finding is open, the
        head is still the receipt's, every expected check run succeeded on it and no trusted
        user's native request for changes stands. Running checks are waited for; anything else
        goes to a human. The merge stays the operator's."""
        lb = self.cfg.labels
        left = open_findings(pr.reviews, pr.closures)
        if left:
            return self._refuse_zero(pr, receipt, 'restano finding bloccanti aperti, che nessuna '
                                     'review chiude al livello 0:\n\n' + '\n'.join(
                                         f'- `{f["key"]}` ({f["title"]})' for f in left)
                                     + f'\n\nUn utente fidato può chiuderli con `{FINDING_COMMAND} '
                                     '<round/reviewer/ID> resolved|withdrawn <motivazione>`')
        head = self.gh.pr_head(pr.number)
        if head != receipt['head']:
            self.log(f'  not promoted: head moved to {head[:12]}')
            return
        overall, details = self._read_ci(head)
        if overall in ('pending', 'fail'):
            self.log(f'  not promoted: CI is {overall}')
            return
        self._record_ci(pr, head, overall, details, None)
        states = check_states(self.cfg.review_policy.zero_checks, self.gh.check_runs(head))
        outcome = check_outcome(states)
        listed = '\n'.join(f'- `{s["name"]}` (app {s["app_id"]}): `{s["state"]}`' for s in states)
        if outcome == 'pending':
            self.log('  not promoted: expected checks still running')
            return
        if outcome != 'success':
            return self._refuse_zero(pr, receipt, 'non tutti i controlli attesi si sono conclusi '
                                     'con `success` sul head corrente (un controllo mancante, '
                                     '`skipped`, `neutral` o `cancelled` non vale come '
                                     'successo):\n\n' + listed, states)
        blocking = changes_requested(self.gh.pr_reviews(pr.number), self.cfg.trusted_users)
        if blocking:
            return self._refuse_zero(pr, receipt, 'una richiesta di modifiche (`REQUEST_CHANGES`) '
                                     'di ' + ', '.join(f'`{who}`' for who in blocking)
                                     + ' è ancora valida: va dismessa o sostituita da '
                                     'un\'approvazione dello stesso autore', states)
        self.gh.comment(pr.number, publishable(
            dispatcher_header() + '\n## Pronta per il merge (livello 0)\n\n'
            f'Il commit `{head[:12]}` non richiede review: la label di livello 0 è stata messa da '
            'un utente fidato, il diff è interamente nell\'allowlist e tutti i controlli attesi '
            'si sono conclusi con `success`. Il merge resta un\'azione dell\'operatore.\n\n'
            + listed + '\n\n' + describe_receipt(receipt),
            'policy', {'event': 'promoted', 'receipt': receipt, 'checks': states}))
        self.gh.edit_labels(pr.number, add=[lb.ready_to_merge], remove=[lb.review, lb.changes])

    def _zero_problem(self, pr):
        """Why a PR ready at level 0 no longer is, or ``''``: on every tick its receipt is
        recomputed from the running dispatcher's policy (label history, level-2 precedence,
        allowlist), its expected check runs must still have succeeded on the head, and no
        trusted user's native request for changes may stand. What cannot be verified does not
        hold."""
        try:
            receipt = self._receipt(pr)
            if receipt['head'] != pr.head_sha:
                return f'il head è passato a `{receipt["head"][:12]}`'
            if receipt['level'] != ZERO_LEVEL:
                return ('la ricevuta di policy ricalcolata ora assegna il livello '
                        f'{receipt["level"]}:\n\n' + describe_receipt(receipt))
            states = check_states(self.cfg.review_policy.zero_checks,
                                  self.gh.check_runs(pr.head_sha))
            if check_outcome(states) != 'success':
                return ('non tutti i controlli attesi risultano conclusi con `success` '
                        'sull\'esecuzione più recente del head:\n\n'
                        + '\n'.join(f'- `{s["name"]}` (app {s["app_id"]}): `{s["state"]}`'
                                    for s in states))
            blocking = changes_requested(self.gh.pr_reviews(pr.number), self.cfg.trusted_users)
            if blocking:
                return ('una richiesta di modifiche (`REQUEST_CHANGES`) di '
                        + ', '.join(f'`{who}`' for who in blocking) + ' è ancora valida')
        except (GitHubError, WorkspaceError) as exc:
            self.log(f'  level 0 of #{pr.number} not verifiable: {exc}')
            return 'non è stato possibile verificarlo di nuovo'
        return ''

    def _acceptance_problems(self, pr):
        """Why the acceptance map of ``pr`` does not hold for the approval of its head that
        would be reused or kept (#134): the titles of the findings :func:`acceptance_findings`
        derives from the criteria of the issue text, read now, and the latest map. Empty without
        such an approval. An approval recorded before #134 never saw the map: one of a PR without
        it, whose issue has criteria, must not be promoted or stay ready. An issue text that is
        not trusted cannot be checked: it does not hold, and the new review refuses it."""
        latest = current_round(pr.reviews)
        if not (latest and not pending_reviewers(latest) and round_verdict(latest) == 'approve'
                and latest[0]['head_sha'] == pr.head_sha):
            return []
        # The issue text as ``_issue_context`` reads it, without the comments it does not need.
        history = self.gh.issue_history(pr.issue)
        reason = self._untrusted_change(history)
        if reason:
            return [f'testo dell\'issue non verificabile: {reason}']
        found = acceptance_findings(acceptance_criteria(history['body']),
                                    latest_map(pr.meta, pr.fixes))
        return [f['title'] for f in found]

    def _refuse_zero(self, pr, receipt, why, states=()):
        """Level 0 holds but the head cannot be promoted: the operator decides."""
        lb = self.cfg.labels
        self.gh.comment(pr.number, publishable(
            dispatcher_header() + '\n## Livello 0: serve un intervento dell\'operatore\n\n'
            f'La policy di review assegna a questa PR il livello 0, ma {why.strip()}.\n\n'
            'devloop non promuove la PR. Per riprendere: risolvere la causa, togliere '
            f'`{lb.needs_operator}` e rimettere `{lb.review}` (il livello 0 viene ricontrollato), '
            f'oppure togliere anche la label `{self.cfg.review_policy.zero_label}` per una review '
            'di livello 1.\n\n' + describe_receipt(receipt),
            'policy', {'event': 'level', 'receipt': receipt, 'checks': list(states),
                       'class': 'level_zero'}))
        self.gh.edit_labels(pr.number, add=[lb.needs_operator],
                            remove=[lb.review, lb.changes, lb.ready_to_merge])

    def _apply_round(self, pr, records, announce, reviews):
        """Apply a complete round, ``records`` (one per reviewer). ``reviews``: every review of
        the PR up to the round included. Every finding still open counts here, whoever owns
        it: an approval does not close another owner's finding."""
        lb = self.cfg.labels
        transient = [lb.review, lb.changes, lb.ready_to_merge]
        verdict = round_verdict(records)
        if verdict == 'approve':
            left = open_findings(reviews, pr.closures)
            if left:
                # Owners outside the round (CI, an earlier reviewer): no agent closes them.
                self._escalate(pr.number, _open_findings_reason(left), remove=transient,
                               cls='open_findings')
            elif self._approval_current(pr, records[0]):
                self._promote(pr, records)
        elif verdict == 'needs_operator':
            if announce:
                # The reviewer's record asks for the decision; this one classifies it (#182).
                self.gh.comment(pr.number, render_escalation(
                    'Un reviewer chiede una decisione dell\'operatore: il motivo è nella sua '
                    'review.', cls='reviewer_decision'))
            self.gh.edit_labels(pr.number, add=[lb.needs_operator], remove=transient)
        elif counted_rounds(reviews) >= self.cfg.max_review_rounds:
            if announce:
                self.gh.comment(pr.number, render_escalation(
                    f'Raggiunto il limite di {self.cfg.max_review_rounds} round di review con '
                    'finding bloccanti ancora aperti.', cls='round_limit'))
            self.gh.edit_labels(pr.number, add=[lb.needs_operator], remove=transient)
        else:
            # A CI failure on a ready PR (#248): the dispatcher's own operator label goes too.
            self.gh.edit_labels(pr.number, add=[lb.changes],
                                remove=[lb.review, lb.ready_to_merge, *self._released(pr)])

    # Actions -------------------------------------------------------------
    def _do_revoke(self, pr):
        lb = self.cfg.labels
        latest = current_round(pr.reviews)
        complete = latest and not pending_reviewers(latest)
        approved = (latest[0]['head_sha'] if complete and round_verdict(latest) == 'approve'
                    else None)
        if pr.zero_head is not None:
            title = 'Livello 0 sospeso'
            why = (pr.zero_problem.strip() if pr.zero_problem
                   else 'il head, le label di livello o la CI sono cambiati')
            # A list ends its own paragraph; a sentence needs its full stop.
            end = '' if why.splitlines()[-1].startswith('- ') else '.'
            reason = (f'La PR era pronta per il merge al livello 0 sul commit '
                      f'`{pr.zero_head[:12]}`, ma {why}{end}\n\nLa PR torna in review e il '
                      'livello viene ricalcolato')
        elif approved == pr.head_sha and pr.acceptance:
            title = 'Approvazione sospesa'
            reason = (f'L\'approvazione del commit `{approved[:12]}` non vale per la mappa di '
                      'copertura dei criteri di accettazione (#134):\n\n'
                      + '\n'.join(f'- {problem}' for problem in pr.acceptance)
                      + '\n\nLa PR torna in review: una nuova review, che conta come un round, '
                      'registra il finding bloccante')
        elif approved == pr.head_sha:
            title = 'Readiness sospesa'
            reason = (f'La CI del commit approvato `{approved[:12]}` è di nuovo in corso. La PR '
                      'torna in review: l\'approvazione resta registrata e verrà riapplicata '
                      'senza una nuova review se la CI passa e la ricevuta di policy coincide '
                      'ancora')
        elif approved:
            title = 'Approvazione revocata'
            reason = (f'Il commit `{pr.head_sha[:12]}` non è quello approvato (`{approved[:12]}`). '
                      'La PR torna in review')
        else:
            title = 'Approvazione revocata'
            reason = 'Non risulta una review devloop approvata. La PR torna in review'
        released = self._released(pr)
        if released:
            reason += f', e `{lb.needs_operator}`, che il dispatcher aveva messo per il merge, è tolta'
        self.gh.comment(pr.number,
                        publishable(dispatcher_header() + f'\n## {title}\n\n' + reason + '.'))
        self.gh.edit_labels(pr.number, add=[lb.review], remove=[lb.ready_to_merge, *released])

    def _do_hold(self, pr):
        """Apply a hold or release command (#172): add or remove ``agent:merge-hold``, then
        record who asked and why in a dispatcher ``hold`` record, which spends the command.
        Label first: if the record is lost, the next tick applies the same command again."""
        lb, command = self.cfg.labels, pr.hold
        reason = ' '.join(command['reason'].split())
        if command['event'] == 'hold':
            self.gh.edit_labels(pr.number, add=[lb.merge_hold])
            title = 'Merge trattenuto'
            text = (f'`{command["by"]}` ha trattenuto il merge: {reason}\n\ndevloop non mergia '
                    f'la PR finché resta `{lb.merge_hold}`; `{RELEASE_COMMAND}` lo permette di '
                    'nuovo.')
        else:
            self.gh.edit_labels(pr.number, remove=[lb.merge_hold])
            title = 'Merge rilasciato'
            text = (f'`{command["by"]}` ha rilasciato il merge' + (f': {reason}' if reason else '.')
                    + f'\n\ndevloop può di nuovo mergiare la PR quando è `{lb.ready_to_merge}` e '
                    'valgono le direttive di merge.')
        self.gh.comment(pr.number, publishable(dispatcher_header() + f'\n## {title}\n\n{text}',
                                               'hold', dict(command, head=pr.head_sha)))
        self.log(f'  {command["event"]} by {command["by"]}')
        pr.hold = None

    def _do_merge(self, pr):
        """Merge a ready PR (#172), with a merge commit on the approved head, with the local
        ``gh`` login. Every merge directive is read again just before, with reviews, closures,
        policy records and commands rebuilt from the comments read now: the PR still open,
        ``agent:ready-to-merge`` without ``agent:needs-operator`` nor ``agent:merge-hold``, no hold
        or release command waiting, the head unchanged, ``MERGEABLE`` and ``CLEAN``, CI
        ``pass``, no open finding, the merge mode of the recomputed receipt, and the conditions
        of the promotion: the approvals of every required reviewer with a matching receipt, or
        at level 0 the checks of a level-0 promotion. When one is missing devloop does not
        merge: what the per-tick checks of a ready PR revoke is left to the next tick, a stale
        receipt or an open finding is handled as at promotion."""
        cfg, lb, rp = self.cfg, self.cfg.labels, self.cfg.review_policy
        fresh = self.gh.pr_state(pr.number)
        labels = label_names(fresh.get('labels') or [])
        head = fresh.get('headRefOid') or ''

        def skip(why):
            self.log(f'  not merged: {why}')

        if fresh.get('state') != 'OPEN':
            return skip(f'the PR is {fresh.get("state")}')
        if lb.ready_to_merge not in labels or labels & (lb.operator() | {lb.merge_hold,
                                                                         lb.needs_supervisor}):
            return skip('labels changed: ' + ', '.join(sorted(labels)))
        if head != pr.head_sha:
            return skip(f'head moved to {head[:12]}')
        status = (fresh.get('mergeable'), fresh.get('mergeStateStatus'))
        if status != ('MERGEABLE', 'CLEAN'):
            return skip('GitHub reports {} / {}'.format(*status))
        # Reviews, closures, policy records and commands as the conversation shows them now,
        # with the same identity checks as the snapshot: never the snapshot's older view.
        pr = replace(pr, labels=labels, reviews=[], fixes=[], syncs=[], closures=[],
                     commands=[], finding_problems=[], policy=[], zero_head=None,
                     zero_problem='', stale_receipt=False, revoked=None, operator_comments=[],
                     rereview=False, round_comments=[], acceptance=[], hold=None)
        self._read_thread(pr, self.gh.pr_comments(pr.number))
        if pr.hold:
            return skip('a hold or release command waits to be applied')
        if pr.finding_problems:
            return skip('findings whose owner cannot be reconstructed')
        if pr.stale_receipt:
            return skip('a policy record found the receipt of the approval stale')
        ci, _ = self._read_ci(head)
        if ci != 'pass':
            return skip(f'CI is {ci}')
        left = open_findings(pr.reviews, pr.closures)
        if left:
            return self._escalate(pr.number, _open_findings_reason(left),
                                  remove=[lb.review, lb.changes, lb.ready_to_merge],
                                  cls='open_findings')
        if pr.zero_head is None and self._acceptance_problems(pr):
            return skip('the acceptance map does not hold')
        current, paths = self._receipt_and_paths(pr)
        if current['head'] != head:
            return skip(f'the pushed head is {current["head"][:12]}')
        mode = cfg.merge.mode_for(current['level'], paths)
        if mode not in MERGING:
            return skip(f'the merge mode of this PR is {mode}')
        bases = []
        if pr.zero_head is not None:
            # Level 0: the same checks as its promotion, and its labels still in place.
            problem = self._zero_problem(pr)
            if not problem and not (pr.zero_head == head and rp.zero_label in labels
                                    and rp.label not in labels):
                problem = 'il head o le label di livello sono cambiati'
            if problem:
                pr.zero_problem = problem
                return self._do_revoke(pr)
        else:
            latest = current_round(pr.reviews)
            if not (latest and not pending_reviewers(latest)
                    and round_verdict(latest) == 'approve' and latest[0]['head_sha'] == head):
                return skip('the head is not the approved commit')
            if len(current['reviewers']) < current['level']:
                return self._escalate_level(pr, current)
            bases = self._approvals_match(pr, latest, current, paths, step='merge')
            if bases is None:
                return None
        self.merger.merge_pr(pr.number, head)
        self.log(f'  merged {head[:12]} at level {current["level"]} in mode {mode}')
        if self.pinner is not None and cfg.pin.auto and cfg.pin.touches(paths):
            # The next tick pins the base with this merge, before planning (#174).
            try:
                self.pinner.request(pr.number, head)
                self.log(f'  auto-pin of origin/{cfg.base_branch} requested for the next tick')
            except OSError as exc:
                self.log(f'  auto-pin NOT requested, pin by hand: {exc}')
        payload = {'head': head, 'level': current['level'], 'mode': mode, 'receipt': current}
        merged_by = self._app_login(MERGER)
        if merged_by:
            # Signed by the merger's app (#201); a merge with the ambient login names nobody.
            payload['merged_by'] = merged_by
        if bases:
            payload['bases'] = bases
        try:
            self.gh.comment(pr.number, publishable(
                dispatcher_header() + '\n## Mergiata da devloop\n\n'
                f'devloop ha mergiato il commit `{head[:12]}` con un merge commit, legato a quel '
                f'head (`--match-head-commit`), in modalità `{mode}`, al livello '
                f'{current["level"]}. Subito prima ha ricontrollato le direttive di merge: CI '
                '`pass`, `MERGEABLE` e `CLEAN`, nessun finding aperto, nessuna label '
                f'`{lb.needs_operator}` o `{lb.merge_hold}`, e le condizioni della promozione con la '
                'ricevuta di policy ricalcolata. Il branch resta.\n\n' + describe_receipt(current),
                'merged', payload))
        except GitHubError as exc:
            # The PR is merged: escalating it would only label a closed PR.
            self.log(f'  the merged record could not be written: {exc}')
        self._close_merged_issue(pr, head, mode)
        return None

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
                                     lb.needs_operator)
        if pr.operator_notice is None:
            self.gh.comment(pr.number, body)
        else:
            self.gh.edit_comment(pr.operator_notice, body)
            self.log(f'  promoted again: updated the notice of {pr.head_sha[:12]}')
        self.gh.edit_labels(pr.number, add=[lb.needs_operator])
        self.log(f'  the merge of {pr.head_sha[:12]} is the operator\'s')

    def _operator_merge_reasons(self, pr):
        """Why devloop does not merge the ready ``pr`` (#248), as Markdown lines: ``[merge]``
        off, a level ``off`` in ``[merge.levels]``, the operator-only paths its diff touches.
        ``''`` (logged) when the head moved or devloop may merge it."""
        merge = self.cfg.merge
        if not merge.enabled():
            return '- `[merge] mode = "off"`: devloop non mergia le PR.'
        receipt, paths = self._current_receipt(pr)
        if receipt is None:
            return ''
        lines = []
        reserved = sorted(p for p in paths if merge.operator_only(p))
        if reserved:
            shown = ', '.join(f'`{p}`' for p in reserved[:MAX_LISTED_PATHS])
            more = (f' e altri {len(reserved) - MAX_LISTED_PATHS}'
                    if len(reserved) > MAX_LISTED_PATHS else '')
            lines.append('- la PR tocca percorsi riservati all\'operatore '
                         f'(`[merge] operator_only_paths`): {shown}{more}.')
        level = receipt['level']
        if any(lvl == level and mode == 'off' for lvl, mode in merge.levels):
            lines.append(f'- `[merge.levels]` imposta `off` per il livello di review {level} di '
                         'questa PR.')
        if not lines:
            self.log(f'  merge mode {merge.mode_for(level, paths)}: devloop may merge this PR')
        return '\n'.join(lines)

    def _close_merged_issue(self, pr, head, mode):
        """Close the issue of ``pr``, the one its marker names, after devloop merged it (#228),
        with the dispatcher's identity: "Closes #N" has no effect when the merger's app merges.
        A comment with a ``closed`` marker records the PR, the merged head, the merge commit
        and the mode; then the issue is closed as completed and loses ``agent:ready``. An issue
        that is not open (GitHub closed it, or a person did) is left alone. A failure is logged
        and does not touch the merged PR: an issue left open with ``agent:ready`` is never
        started again (``_do_merged_issue``)."""
        lb = self.cfg.labels
        number = pr.meta.get('issue')
        if not isinstance(number, int):
            self.log('  the PR marker names no issue: none closed')
            return
        try:
            issue = self.gh.issue(number)
            state = issue.get('state')
            if state != 'OPEN':
                self.log(f'  issue #{number} is {state or "in an unknown state"}: left alone')
                return
            commit = None
            try:
                commit = (self.gh.pr_state(pr.number).get('mergeCommit') or {}).get('oid')
            except GitHubError as exc:
                self.log(f'  merge commit of #{pr.number} not read: {exc}')
            self.gh.comment(number, publishable(
                dispatcher_header() + '\n## Risolta da una PR mergiata\n\n'
                f'devloop ha mergiato la PR #{pr.number} (head `{head[:12]}`, merge commit '
                + (f'`{commit[:12]}`' if commit else 'non letto') + f') in modalità `{mode}`, e '
                'chiude questa issue come completata. Con il merge dell\'app merger le parole '
                'chiave di chiusura della PR non hanno effetto, quindi la chiude il dispatcher.',
                'closed', {'pr': pr.number, 'head': head, 'merge_commit': commit, 'mode': mode}))
            self.gh.close_issue(number)
            if lb.ready in label_names(issue.get('labels') or []):
                self.gh.edit_labels(number, remove=[lb.ready])
            self.log(f'  issue #{number} closed')
        except GitHubError as exc:
            self.log(f'  issue #{number} NOT closed, close it by hand: {exc}')

    def _do_merged_issue(self, number, merged=None):
        """Escalate a ready issue that a merged devloop PR already resolved (#228), instead of
        checking or implementing it again: class ``merged_issue``, non-critical. In ``auto``
        the supervisor decides it and the issue keeps ``agent:ready`` meanwhile (no retry is
        admitted: closing the issue, or ``/devloop escalate``, settles it); otherwise it goes
        to the operator, without ``agent:ready`` nor ``agent:needs-supervisor``. ``merged``:
        the merged PRs already read (``_do_supervise``, a retry refused for them)."""
        lb = self.cfg.labels
        if merged is None:
            merged = self._merged_prs(number)
        if not merged:
            self.log('  no merged devloop PR found any more; next tick')
            return
        listed = ', '.join(f'#{m["number"]}' + (f' (merge commit `{m["merge_commit"][:12]}`)'
                                                if m['merge_commit'] else '') for m in merged)
        reason = (f'L\'issue è aperta e ha `{lb.ready}`, ma è già stata mergiata una PR devloop '
                  f'che la risolve: {listed}. devloop non la controlla né la implementa di nuovo, '
                  'per non duplicare il lavoro. Se è risolta va chiusa; se serve altro lavoro, '
                  'va aperta una nuova issue.')
        supervised, why = self._supervised(number, MERGED_ISSUE, 'implement')
        # From a refused retry the issue is in agent:needs-supervisor: it leaves it only for
        # the operator.
        remove = [lb.ready] if supervised else [lb.ready, lb.needs_supervisor]
        self._escalate(number, reason, remove=remove, cls=MERGED_ISSUE,
                       supervised=supervised, why=why)

    def _do_stale(self, number):
        lb = self.cfg.labels
        self._escalate(number, 'Implementazione interrotta: la issue era in lavorazione ma non '
                               'esiste una PR devloop aperta.', remove=[lb.working],
                       cls='interrupted')

    def _do_settle(self, number):
        self.gh.edit_labels(number, remove=[self.cfg.labels.working])

    def _do_reconcile(self, pr):
        self.gh.edit_labels(pr.number, add=[self.cfg.labels.review],
                            remove=[self.cfg.labels.changes])

    def _do_findings(self, pr):
        """Record the closures of open findings: those trusted users asked for, and the CI
        findings once CI passes on the PR's current head."""
        closed = list(pr.commands)
        ci = [f for f in open_findings(pr.reviews, pr.closures) if f['owner'] == CI]
        if ci and pr.checks == 'pass':
            if self.gh.pr_head(pr.number) != pr.head_sha:
                self.log('  head moved since the snapshot; CI findings stay open')
            else:
                closed += [{'key': f['key'], 'status': 'resolved', 'by': CI,
                            'author': self._app_login(DISPATCHER) or 'devloop dispatcher',
                            'reason': f'CI verde sul commit {pr.head_sha[:12]}',
                            'head_sha': pr.head_sha} for f in ci]
        if not closed:
            return
        lines = []
        for c in closed:
            who = 'la CI' if c['by'] == CI else f'l\'operatore `{c["author"]}`'
            lines.append(f'- `{c["key"]}`: {_CLOSED_TEXT[c["status"]]} da {who}: '
                         + ' '.join(c['reason'].split()))
        self.gh.comment(pr.number, publishable(
            dispatcher_header() + '\n## Finding chiusi\n\n' + '\n'.join(lines)
            + '\n\nDa ora questi finding sono chiusi: nessuna review successiva li riapre.',
            'finding', {'closed': closed}))
        pr.closures = pr.closures + closed
        pr.commands = []

    def _do_unattributable(self, pr):
        lb = self.cfg.labels
        self._escalate(pr.number, 'Non è possibile ricostruire il proprietario di alcuni finding '
                                  'dai marker di questa PR, quindi devloop non sa chi può '
                                  'chiuderli e non prosegue: la review e la decisione sul merge '
                                  'spettano a una persona.\n\n'
                       + '\n'.join(f'- {p}' for p in pr.finding_problems),
                       remove=[lb.review, lb.changes, lb.ready_to_merge], cls='unattributable')

    def _do_lost_meta(self, pr):
        lb = self.cfg.labels
        self._escalate(pr.number, 'La descrizione della PR non termina più con il marker devloop '
                                  f'della issue #{pr.issue}, quindi implementer e reviewer non sono '
                                  'noti. Ripristinare la riga del marker (si trova nella cronologia '
                                  'delle modifiche della descrizione) come ultima riga.',
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
        self._escalate(pr.number, f'Sul head `{pr.head_sha[:12]}` non risulta alcuna CI '
                                  f'{self.cfg.ci.required_timeout_minutes} minuti dopo il push, '
                                  f'ma `{self.cfg.base_branch}` richiede i check {names}: la '
                                  'review non parte senza la CI. Verificare che la CI sia '
                                  'partita sul head, o rilanciarla.',
                       remove=[lb.review], cls='ci_missing')

    def _do_round_limit(self, pr):
        lb = self.cfg.labels
        self._escalate(pr.number, f'Raggiunto il limite di {self.cfg.max_review_rounds} round di '
                                  'review: un nuovo round (review o CI fallita) non parte senza una '
                                  'decisione dell\'operatore.',
                       remove=[lb.review, lb.changes, lb.ready_to_merge], cls='round_limit')

    def _do_implement(self, number):
        cfg, lb = self.cfg, self.cfg.labels
        agent = cfg.agent(cfg.implementer)
        if cfg.preflight and preflight_state(self._preflight_view(number, ()))[0] != 'ready':
            # The text changed, or a new check was asked, after the snapshot.
            self.log('  the readiness check does not cover the current text; next tick')
            return
        issue, comments = self._issue_context(number)
        if self.ledger is not None:
            # The request as the implementer gets it, at the takeover (#133): not necessarily
            # the text at the time of the label.
            self.ledger.request(number, title=issue['title'], body=issue.get('body') or '',
                                labeled=ready_approval(issue['labeled'], lb.ready,
                                                       issue['admitters']),
                                comments=_trusted_comments(issue.get('comments') or [],
                                                           cfg.trusted_users)
                                + issue['accepted'])
        self.gh.edit_labels(number, add=[lb.working], remove=[lb.ready])
        name, branch = f'issue-{number}', f'{cfg.branch_prefix}{number}'
        path = self.ws.new_branch(name, branch)
        # The input the turn runs on (#182), bound before it runs: the commit its branch starts
        # from and the request its prompt gets.
        key = self._issue_key('implement', self.ws.head(path), issue['request'])
        criteria = acceptance_criteria(issue.get('body'))

        def prompt(attempt):
            # #203: the first attempt has no diff yet and gets the whole map; a later one the
            # documents mapped to the diff it left.
            docs = (docs_map_section(self._mapped_entries(path)) if attempt == 0
                    else docs_diff_section(self._mapped_docs(path)))
            return _template('implement', repo=cfg.repo, base_branch=cfg.base_branch,
                             issue_number=number, issue_title=issue['title'],
                             issue_body=issue.get('body') or '', issue_comments=comments,
                             acceptance_criteria=format_criteria(criteria),
                             test_cmd=cfg.test_cmd, language=cfg.language, docs=docs)
        try:
            result, failure, used, tested, local = self._write_turn(
                agent, prompt, 'implement', path, f'issue-{number}-implement',
                where=(number, 'implement'))
        except AgentError as exc:
            exc.key = key
            raise
        if result['status'] == 'blocked':
            return self._blocked(number, used, result['blocked_reason'], remove=[lb.working],
                                 on_pr=False)
        if failure:
            return self._escalate(number, failure[0], failure[1], remove=[lb.working],
                                  cls='tests_failed')
        auth = self._git_auth(agent.key, IMPLEMENT)
        commit = self.ws.commit_all(
            path, self._commit_message(agent, result['commit_message'], number), author=auth)
        self._bind_tested(tested, path, commit)
        protected = self._protected_changes(path)
        if protected:
            return self._escalate_protected(number, protected, remove=[lb.working])
        self.ws.push(path, branch, replace=True, auth=auth)
        self._pushed(path)
        # The marker keeps the map the reviewer checks until a fix replaces it (#134).
        meta = {'issue': number, 'implementer': agent.key, 'reviewer': cfg.reviewer,
                'profile': self._profile(agent.key, 'write', used),
                'acceptance_map': result['acceptance_map'],
                # Informative only (#169): no receipt nor approval depends on it.
                'local_tests': local, **skipped_models(used)}
        body = publishable(self.runner.header(used) + '\n' + result['pr_body'].strip()
                           + '\n\n' + render_map(result['acceptance_map'])
                           + f'\n\nCloses #{number}', 'pr', meta)
        pr_number, url = self._as(agent.key, IMPLEMENT).create_pr(
            base=cfg.base_branch, head=branch, title=result['pr_title'], body=body)
        self.gh.edit_labels(pr_number, add=[lb.review])
        self.gh.edit_labels(number, remove=[lb.working])
        self.ws.remove(name)
        self.log(f'  opened {url}')

    def _current_receipt(self, pr):
        """``(receipt, paths)`` of the snapshot's head (see ``_receipt_and_paths``), or
        ``(None, None)`` (logged) when the head moved since."""
        receipt, paths = self._receipt_and_paths(pr)
        if receipt['head'] != pr.head_sha:
            self.log('  head moved since the snapshot; retrying next tick')
            return None, None
        return receipt, paths

    def _do_ci_failed(self, pr):
        receipt, paths = self._current_receipt(pr)
        if receipt is None:
            return
        if receipt['level'] > 1 and len(receipt['reviewers']) < receipt['level']:
            return self._escalate_level(pr, receipt)
        if self._rerun_unrelated(pr, paths):
            return
        log_tail = '\n'.join(self.gh.failed_log(run) for run in pr.failed_runs[:2])
        review = ci_failure_review(failed_checks=pr.failed_checks, log_tail=log_tail,
                                   previous_open=open_findings(pr.reviews, pr.closures),
                                   round_no=last_round(pr.reviews) + 1,
                                   max_rounds=self._last_round_allowed(pr.reviews),
                                   head_sha=pr.head_sha)
        rerun = rerun_of(pr.reruns, pr.head_sha)
        if rerun is not None:
            review['note'] = (
                'I job falliti di questo commit erano già stati rilanciati una volta, perché '
                'nessun modulo fallito era pertinente alla PR (run '
                + ', '.join(str(r) for r in rerun['runs']) + '; moduli: '
                + (', '.join(rerun['modules']) or 'nessuno') + '). Anche il rilancio è fallito: '
                'questo fallimento conta come round.')
        review['receipt'] = receipt
        if self.ledger is not None:
            details = self.gh.check_details(pr.head_sha)
            self._record_ci(pr, pr.head_sha, summarize_checks(details)[0], details,
                            review['round'])
        self.gh.comment(pr.number, render_review(review, dispatcher_header()))
        self._apply_round(pr, [review], announce=True, reviews=pr.reviews + [review])

    def _rerun_unrelated(self, pr, paths):
        """Re-run once the failed jobs of the head's CI when its failure does not concern the
        PR (#200); whether it did. Only at the first failure of a head (``rerun`` marker), with
        ``ci_rerun_unrelated``, when every failed check is a GitHub Actions job whose own log
        names the failed test modules (``failed_modules``) and none of them is pertinent to
        ``paths``,
        the PR's diff, as ``local_tests = "changed"`` would select them (``select_modules``).
        A re-run that GitHub refuses, such as a 403 without ``actions: write``, is logged and
        the failure is a round, as before."""
        if not self.cfg.ci_rerun_unrelated or rerun_of(pr.reruns, pr.head_sha) is not None:
            return False
        details = self.gh.check_details(pr.head_sha)
        failed = [c for c in details if c.get('bucket') == 'fail']
        runs = sorted({c.get('run_id') for c in failed}, key=str)
        if not failed or None in runs:
            self.log('  CI rerun: ' + ('no failed check read again' if not failed else
                                       'not every failed check is a GitHub Actions run')
                     + '; CI round')
            return False
        # Each failed job on its own: a run whose suite fails only in modules not pertinent to
        # the PR may hold another failed job, such as a benchmark, that names none.
        modules = set()
        for check in failed:
            job = job_id(check.get('link'))
            found = None if job is None else failed_modules(
                self.gh.failed_log(check['run_id'], max_lines=None, job_id=job))
            if found is None:
                self.log(f'  CI rerun: the log of the failed job {check.get("name")} (run '
                         f'{check["run_id"]}) does not tell which test modules failed; CI round')
                return False
            modules.update(found)
        modules = sorted(modules)
        pertinent = select_modules(paths, modules)
        if pertinent:
            self.log(f'  CI rerun: failed modules pertinent to the PR: {", ".join(pertinent)}; '
                     'CI round')
            return False
        try:
            rerunner = self._as(DISPATCHER, RERUN)
            for run in runs:
                rerunner.rerun_failed_jobs(run)
        except GitHubError as exc:
            self.log(f'  CI rerun refused, the failure is a round as before (the dispatcher\'s '
                     f'app needs actions: write): {exc}')
            return False
        record = {'head': pr.head_sha, 'runs': runs, 'modules': modules,
                  'checks': sorted(c['link'] for c in failed)}
        self.gh.comment(pr.number, publishable(
            dispatcher_header() + '\n## CI rilanciata\n\n'
            f'La CI di `{pr.head_sha[:12]}` è fallita solo in moduli di test non pertinenti al '
            f'diff della PR ({", ".join(f"`{m}`" for m in modules)}): il dispatcher ha '
            'rilanciato una volta i job falliti (run '
            + ', '.join(str(r) for r in runs) + '), senza aprire un round. Se anche il '
            'rilancio fallisce, il fallimento conta come round.', 'rerun', record))
        self._ledger_event(pr.number, 'ci_rerun', head=pr.head_sha, runs=runs, modules=modules)
        self.log(f'  CI rerun of {", ".join(str(r) for r in runs)}: failed modules not '
                 f'pertinent to the PR ({", ".join(modules)})')
        return True

    def _last_round_allowed(self, reviews):
        """The number of the last round ``max_review_rounds`` allows, given the waived rounds
        among ``reviews``: what records and prompts show as the limit."""
        return self.cfg.max_review_rounds + len(waived_rounds(reviews))

    def _do_apply_review(self, pr):
        self._apply_round(pr, current_round(pr.reviews), announce=False, reviews=pr.reviews)

    def _open_round(self, pr, records):
        """Whether the round ``records`` is still open for this head: an agent round of the
        same head, not answered by a fix nor voided by a rereview request, still waiting for a
        reviewer. Whether a new review joins it depends on the receipt (``_do_review``)."""
        return (bool(records) and records[0]['source'] == 'agent'
                and records[0]['head_sha'] == pr.head_sha and not pr.rereview
                and not any(f['round'] == records[0]['round'] for f in pr.fixes)
                and bool(pending_reviewers(records)))

    def _do_review(self, pr):
        """One review by one reviewer: each review call is an action of its own. The first
        reviewer of a round opens it; the next ones, on later actions, join it while it reviews
        the same head under the same receipt. The round is applied once every required reviewer
        has reviewed; a review already recorded in it is never run again.

        With ``max_parallel_actions`` above 1 (#176) the action runs at once every reviewer the
        round still waits for, each in its own copy of the worktree and on the same prompt, and
        records each verdict as before. A reviewer that fails leaves the others' verdicts
        recorded, and the round waits only for it; its failure ends the action as before. A
        fallback model must also be distinct from the profiles the other reviewers of the
        action have reserved.

        When only the base changed since the round's reviews (#160), the round continues if no
        commit of the base branch in between reached the PR's files, and the review records the
        check (``bases``). Otherwise the round restarts, and the new one records which round it
        replaces (``restarts``): that round does not count against ``max_review_rounds``.

        A new round on a head that a clean sync moved from the approved one (#235,
        ``PullRequest.resync``), or on the approved head after the base touching the PR revoked
        its approval (#252), records ``resync`` and does not count either."""
        cfg = self.cfg
        receipt, paths = self._current_receipt(pr)
        if receipt is None:
            return
        if receipt['level'] == ZERO_LEVEL:
            return self._promote_zero(pr, receipt)
        if len(receipt['reviewers']) < receipt['level']:
            return self._escalate_level(pr, receipt)
        latest = current_round(pr.reviews)
        bases = self._base_checks(latest, receipt, paths) if self._open_round(pr, latest) else None
        if bases and any(c['outcome'] == 'conflict' for c in bases):
            # Neither continued nor restarted: the sync merges the base and changes the head,
            # once GitHub reports the conflict too.
            self.log('  the PR conflicts with the new base; no review until the sync')
            return
        joins = bases is not None and all(c['outcome'] == 'disjoint' for c in bases)
        restarts = resync = None
        if joins:
            round_no, done, comments = latest[0]['round'], latest, pr.round_comments
            counted = counted_rounds(pr.reviews)
        else:
            round_no, done, comments = last_round(pr.reviews) + 1, [], pr.operator_comments
            counted = counted_rounds(pr.reviews) + 1
            if bases:
                # Abandoned only because the base advanced: the round does not count.
                restarts = {'round': latest[0]['round'], 'bases': bases}
                counted -= 1
                self.log(f'  round {latest[0]["round"]} restarts: the base touched the PR')
            elif pr.resync and not pr.rereview:
                # Only a clean sync moved the approved head (#235), or only the base touching
                # the PR revoked its approval (#252): this round does not count.
                resync = pr.resync
                counted -= 1
                why = ('approval was revoked because the base touched the PR'
                       if resync['reason'] == BASE_CHANGED else 'head was synced with the base')
                self.log(f'  the {why} after round {resync["round"]}: this round does not count')
        if counted > cfg.max_review_rounds:
            return self._do_round_limit(pr)
        max_rounds = cfg.max_review_rounds + round_no - counted
        reviewed = {owner_of(r) for r in done}
        waited = [key for key in receipt['reviewers'] if key not in reviewed]
        # One reviewer per action, or with parallel actions (#176) every one the round waits
        # for, each in a thread of its own.
        keys = waited if cfg.max_parallel_actions > 1 else waited[:1]
        issue, _ = self._issue_context(pr.issue)
        name = f'pr-{pr.number}'
        path = self.ws.existing_branch(name, pr.head_ref)
        if self.ws.head(path) != pr.head_sha:
            self.log('  head moved since the snapshot; retrying next tick')
            return
        if not joins and cfg.local_tests != 'full' and pr.checks == 'none':
            # The first reviewer of a round runs it; the next ones review the same head.
            if not self._full_suite_without_ci(pr, path, name):
                return
        # Each reviewer of a round judges the head on its own: earlier rounds only.
        earlier = [r for r in pr.reviews if r['round'] < round_no]
        previous_open = open_findings(earlier, pr.closures)
        # The criteria of the issue text read now, and the latest map of the PR (#134).
        criteria = acceptance_criteria(issue.get('body'))
        acceptance_map = latest_map(pr.meta, pr.fixes)
        prompt = _template('review', repo=cfg.repo, base_branch=cfg.base_branch,
                           pr_number=pr.number, round=round_no, max_rounds=max_rounds,
                           implementer=pr.meta.get('implementer', cfg.implementer),
                           ci=pr.checks, test_cmd=cfg.test_cmd,
                           issue_number=pr.issue, issue_title=issue['title'],
                           issue_body=issue.get('body') or '', pr_body=pr.body,
                           acceptance_criteria=format_criteria(criteria),
                           acceptance_map=format_map(acceptance_map),
                           docs=docs_review_section(self._mapped_docs(path)),
                           diff_stat=self.ws.diff_stat(path),
                           previous=format_previous_rounds(earlier, pr.fixes, pr.closures),
                           operator_comments=format_operator_comments(comments),
                           language=cfg.language)
        level2 = receipt['level'] > 1
        authors = (self._authors(pr) or []) if level2 else []
        peers = [p for p in map(profile_of, done) if p]
        # The profile each reviewer of this action runs with, reserved before any turn starts
        # and replaced atomically when a fallback model takes over (#176); a reviewer that
        # fails gives its reservation up.
        reserved = {key: self._profile(key, 'review') for key in keys}
        reserving = threading.Lock()

        def allowed_for(key):
            if not level2:
                return None

            def allowed(candidate):
                # A fallback model must keep the independence the pair was selected for: of
                # who wrote the code, of the round's other reviewers as their records show
                # them, and of the reviewers running alongside it in this action.
                p = self._profile(key, 'review', candidate)
                with reserving:
                    others = [q for k, q in reserved.items() if k != key]
                    if not (all(independent(p, a) for a in authors)
                            and all(distinct(p, q) for q in peers + others)):
                        return False
                    reserved[key] = p
                    return True
            return allowed

        def review_turn(key):
            # The reviewer works in a disposable copy, where its tests may write; the PR
            # worktree must come out of the review untouched, or the verdict is discarded.
            copy_name, copy = (self.ws.review_copy(path, key) if len(keys) > 1
                               else self.ws.review_copy(path))
            try:
                return self._turn(Models(cfg.agent(key)), 'review', prompt, 'review', copy,
                                  f'pr-{pr.number}-review', allowed_for(key),
                                  where=(pr.number, 'review'))
            except BaseException as exc:
                with reserving:
                    reserved.pop(key, None)
                if isinstance(exc, AgentError):
                    exc.reviewer = key  # part of the key of the supervisor's retries (#182)
                raise
            finally:
                self.ws.remove(copy_name)

        if len(keys) == 1:
            turns = [(keys[0], review_turn(keys[0]), None)]
        else:
            turns = self._together(keys, review_turn)
        failures = [(key, exc) for key, _, exc in turns if exc is not None]
        for key, exc in failures:
            self.log(f'  review by {key} failed: {exc}')
        if not self.ws.pristine(path, pr.head_sha):
            raise WorkspaceError(f'the worktree of PR #{pr.number} changed during the review; '
                                 'verdict discarded')
        if level2 and self.gh.pr_head(pr.number) != pr.head_sha:
            # A verdict on a superseded head joins no round: the new head is reviewed afresh.
            self.log('  head moved during the review; verdict discarded')
            self.ws.remove(name)
            if failures:
                raise _review_failure(failures)
            return
        records = list(done)
        for key, (raw, used), _ in (turn for turn in turns if turn[2] is None):
            review = normalize_review(raw, previous_open=previous_open, round_no=round_no,
                                      max_rounds=max_rounds, head_sha=pr.head_sha,
                                      ci=pr.checks, reviewer=key, criteria=criteria,
                                      acceptance_map=acceptance_map)
            review['receipt'] = receipt
            if joins and bases:
                review['bases'] = bases  # the round's earlier bases, checked up to this one
            if restarts:
                review['restarts'] = restarts
            if resync:
                review['resync'] = resync
            review['profile'] = self._profile(key, 'review', used)
            review.update(skipped_models(used))
            reviewer_gh = self._as(key, REVIEW)
            reviewer_gh.comment(pr.number, render_review(review, self.runner.header(used)))
            if self._app_login(key):
                # A native review under the reviewer's own identity, visible to branch
                # protection. The marker comment above stays the record devloop reads.
                try:
                    reviewer_gh.submit_review(pr.number, commit=pr.head_sha,
                                              **render_native_review(review,
                                                                     self.runner.header(used)))
                except GitHubError as exc:
                    self.log(f'  native review not submitted: {exc}')
            records.append(review)
        new = records[len(done):]
        if new and pr.checks != 'pending':
            # The CI the round reviewed, whatever its verdict (#133).
            self._record_ci(pr, pr.head_sha, pr.checks, pr.check_details, round_no)
        waiting = pending_reviewers(records)
        if new and not waiting:
            self._apply_round(pr, records, announce=True, reviews=earlier + records)
        self.ws.remove(name)
        for position, review in enumerate(new):
            last = position == len(new) - 1
            self.log(f'  verdict of {review["reviewer"]}: {review["verdict"]}'
                     + (f'; the round waits for {", ".join(waiting)}' if waiting and last
                        else ''))
        if failures:
            # The verdicts above stay recorded: the round waits only for these reviewers.
            raise _review_failure(failures)

    def _together(self, keys, turn):
        """``(key, result, exception)`` of ``turn(key)`` for each of ``keys``, in order, run
        at once in threads of their own that share the action's log prefix (#176)."""
        prefix = self.log.prefix

        def call(key):
            with self.log.prefixed(prefix):
                try:
                    return key, turn(key), None
                except Exception as exc:  # returned to the action
                    return key, None, exc
        with ThreadPoolExecutor(max_workers=len(keys),
                                thread_name_prefix='devloop-review') as pool:
            return list(pool.map(call, keys))

    def _full_suite_without_ci(self, pr, path, name):
        """The CI gate when the local tests may not have covered the whole suite (#169): with
        ``local_tests`` other than ``full`` a review needs the CI passed, and a head without CI
        gets the full ``test_cmd`` locally first, in a disposable copy of the PR worktree.
        Returns whether the review may start; a failure escalates the PR."""
        lb = self.cfg.labels
        self.log(f'  no CI on {pr.head_sha[:12]} with local_tests = {self.cfg.local_tests}: '
                 'full suite before the review')
        copy_name, copy = self.ws.review_copy(path)
        try:
            passed, tail, _ = self._run_tests((pr.number, 'review'), copy, 1, self.cfg.test_cmd)
        finally:
            self.ws.remove(copy_name)
        if passed:
            return True
        self.ws.remove(name)
        self._escalate(pr.number, f'Sul head `{pr.head_sha[:12]}` non risulta alcuna CI e, con '
                                  f'`local_tests = "{self.cfg.local_tests}"`, i test locali non '
                                  'hanno eseguito la suite completa. La suite completa '
                                  f'(`{self.cfg.test_cmd}`), eseguita in locale prima della '
                                  'review, fallisce: la review non parte.', tail,
                       remove=[lb.review], cls='tests_failed')
        return False

    def _do_fix(self, pr):
        cfg, lb = self.cfg, self.cfg.labels
        if pr.fix_requests:
            return self._do_requested_fix(pr)
        if not pr.reviews:
            return self._escalate(pr.number, 'Modifiche richieste ma nessuna review devloop da '
                                             'correggere.', remove=[lb.changes],
                                  cls='inconsistent')
        # One fix answers the whole round, with every finding still open, whoever owns it.
        latest = current_round(pr.reviews)[0]
        findings = open_findings(pr.reviews, pr.closures)
        if not findings:
            return self._escalate(pr.number, 'Modifiche richieste ma nessun finding aperto.',
                                  remove=[lb.changes], cls='inconsistent')
        if pr.meta['implementer'] not in cfg.agents:
            return self._escalate(
                pr.number, f'L\'implementer della PR (`{pr.meta["implementer"]}`) non è più '
                           'configurato: serve una decisione su chi corregge.',
                remove=[lb.changes], cls='configuration')
        agent = cfg.agent(pr.meta['implementer'])
        issue, _ = self._issue_context(pr.issue)
        name = f'pr-{pr.number}'
        path = self.ws.existing_branch(name, pr.head_ref)
        head = self.ws.head(path)
        # A sync that a fix started, because the base conflicted with the branch, moved the
        # reviewed head on purpose (#250): the fix answers the round on the synced head.
        if head != latest['head_sha'] and not synced_from(pr.syncs, head, latest['head_sha']):
            self.log('  branch changed after the review; sending it back to review')
            self.gh.edit_labels(pr.number, add=[lb.review], remove=[lb.changes])
            return
        start, merged = self._align_fix(pr, agent, path, remove=[lb.changes])
        if start is None:
            return

        def prompt(attempt):
            # The documents mapped to the diff each attempt starts from (#203).
            return _template('fix', repo=cfg.repo, pr_number=pr.number, issue_number=pr.issue,
                             issue_title=issue['title'], issue_body=issue.get('body') or '',
                             findings=_format_findings(findings), test_cmd=cfg.test_cmd,
                             acceptance_criteria=format_criteria(
                                 acceptance_criteria(issue.get('body'))),
                             acceptance_map=format_map(latest_map(pr.meta, pr.fixes)),
                             docs=docs_diff_section(self._mapped_docs(path)),
                             requests='', base_branch=cfg.base_branch, language=cfg.language)
        result, failure, used, tested, local = self._write_turn(
            agent, prompt, 'fix', path, f'pr-{pr.number}-fix-r{latest["round"]}',
            where=(pr.number, 'fix'))
        self._publish_fix(pr, agent, name, path, (result, failure, used, tested, local),
                          since=start, round_no=latest['round'], remove=[lb.changes],
                          merged=merged)

    def _align_fix(self, pr, agent, path, remove):
        """Bring the branch of a fix in ``path`` up to ``origin/<base>`` before its agent starts
        (#250): CI runs on the PR's merge commit with the base, so a branch behind the base may
        fail for code the fix would not see. Returns ``(start, merged)``: the head the agent
        starts from, and the ``{base, head, from}`` of the merge commit the dispatcher made, or
        None when the branch already contained the base. A merge without conflicts is committed
        with the implementer's identity, as a sync's, and pushed with the fix. With conflicts
        the fix does not start: the usual sync runs instead, leaving the PR's labels as they are,
        and the fix runs on a later tick. ``start`` is None when the fix does not start (the
        sync, or a merge that touches ``protected_paths``, escalated with ``remove``)."""
        base = f'origin/{self.cfg.base_branch}'
        previous = self.ws.head(path)
        if self.ws.contains_base(path):
            return previous, None
        auth = self._git_auth(agent.key, IMPLEMENT)
        files = self.ws.start_merge(path, author=auth)
        if files is None:
            return previous, None
        if files:
            self.log(f'  the branch is behind {base} with conflicts in {", ".join(files)}: '
                     'sync first, the fix runs on a later tick')
            self._do_sync(pr, fix_remove=remove)
            return None, None
        target = self.ws.merge_head(path)
        head = self.ws.commit_all(path, f'Merge {base} into {pr.head_ref}\n\nRefs #{pr.issue}\n',
                                  author=auth)
        # As for a sync, the merge never differs from the base under protected_paths.
        protected = self._protected_changes(path, since=base)
        if protected:
            self._escalate_protected(pr.number, protected, remove=remove)
            return None, None
        self.log(f'  merged {base} ({target[:12]}) into {pr.head_ref} before the fix: {head[:12]}')
        return head, {'base': target, 'head': head, 'from': previous}

    def _publish_fix(self, pr, agent, name, path, turn, since, round_no, remove, extra=None,
                     merged=None):
        """Publish the outcome of a fix ``turn`` (``_write_turn``'s result) in worktree
        ``path``: escalate a blocked agent or failing tests (removing ``remove``), otherwise
        commit and push the changes made since commit ``since``, record the fix of round
        ``round_no`` (with ``extra`` fields in its marker) and send the PR back to review.
        ``merged``: the merge of the base the dispatcher made before the turn (``_align_fix``),
        pushed with the fix even when the agent changed nothing."""
        lb = self.cfg.labels
        result, failure, used, tested, local = turn
        if result['status'] == 'blocked':
            return self._blocked(pr.number, used, result['blocked_reason'], remove=remove,
                                 on_pr=True)
        if failure:
            return self._escalate(pr.number, failure[0], failure[1], remove=remove,
                                  cls='tests_failed')
        changes = self.ws.has_changes(path)
        if changes or merged:
            auth = self._git_auth(agent.key, IMPLEMENT)
            if changes:
                commit = self.ws.commit_all(
                    path, self._commit_message(agent, result['commit_message'], pr.issue),
                    author=auth)
                self._bind_tested(tested, path, commit)
                protected = self._protected_changes(path, since=since)
                if protected:
                    return self._escalate_protected(pr.number, protected, remove=remove)
            self.ws.push(path, pr.head_ref, auth=auth)
            self._pushed(path)
        if merged:
            # As for a sync without an agent (#133): an event, not a turn.
            self._ledger_event(pr.number, 'sync', base=merged['base'], head=merged['head'],
                               previous_head=merged['from'])
        fix = dict(result, head_sha=self.ws.head(path),
                   profile=self._profile(agent.key, 'write', used), local_tests=local,
                   **skipped_models(used), **(extra or {}))
        if merged:
            fix['base_merge'] = dict(merged, branch=self.cfg.base_branch)
        self._as(agent.key, PR_COMMENT).comment(
            pr.number, render_fix(fix, self.runner.header(used), round_no))
        self.gh.edit_labels(pr.number, add=[lb.review], remove=[lb.changes])
        self.ws.remove(name)

    def _do_requested_fix(self, pr):
        """A fix of the current head asked with ``FIX_COMMAND`` by a trusted user or the
        supervisor (#195), on a PR in ``agent:review`` or ``agent:changes``: the implementer gets
        the requests as requirements, and in ``agent:changes`` the findings still open too. It
        follows the rules of every fix (local tests, protected paths, push, back to
        ``agent:review``) and writes no review record, so it spends no round. Its record carries
        the comments that asked for it and the head it ran on: they are spent, and a new command
        counts on that head only after a new commit (``_fix_requests``). The record belongs to
        the latest round, which it answers, so the next tick reviews the head again; before any
        review it belongs to round 0."""
        cfg, lb = self.cfg, self.cfg.labels
        remove = [lb.review, lb.changes]
        if pr.meta['implementer'] not in cfg.agents:
            return self._escalate(
                pr.number, f'L\'implementer della PR (`{pr.meta["implementer"]}`) non è più '
                           f'configurato: serve una decisione su chi esegue `{FIX_COMMAND}`.',
                remove=remove, cls='configuration')
        agent = cfg.agent(pr.meta['implementer'])
        requests = pr.fix_requests
        findings = (open_findings(pr.reviews, pr.closures) if lb.changes in pr.labels else [])
        round_no = last_round(pr.reviews)
        issue, _ = self._issue_context(pr.issue)
        name = f'pr-{pr.number}'
        path = self.ws.existing_branch(name, pr.head_ref)
        if self.ws.head(path) != pr.head_sha:
            self.log('  head moved since the snapshot; retrying next tick')
            return
        start, merged = self._align_fix(pr, agent, path, remove=remove)
        if start is None:
            return

        def prompt(attempt):
            return _template('fix', repo=cfg.repo, pr_number=pr.number, issue_number=pr.issue,
                             issue_title=issue['title'], issue_body=issue.get('body') or '',
                             findings=_format_findings(findings), test_cmd=cfg.test_cmd,
                             requests=format_fix_requests(requests),
                             acceptance_criteria=format_criteria(
                                 acceptance_criteria(issue.get('body'))),
                             acceptance_map=format_map(latest_map(pr.meta, pr.fixes)),
                             docs=docs_diff_section(self._mapped_docs(path)),
                             base_branch=cfg.base_branch, language=cfg.language)
        turn = self._write_turn(agent, prompt, 'fix', path, f'pr-{pr.number}-fix-request',
                                where=(pr.number, 'fix'))
        self._publish_fix(pr, agent, name, path, turn, since=start, round_no=round_no,
                          remove=remove, extra={'requests': requests, 'from': pr.head_sha},
                          merged=merged)

    def _do_sync(self, pr, fix_remove=None):
        """Merge the base branch into a conflicting PR. The implementer resolves the conflicts
        in the PR worktree, the dispatcher concludes the merge, pushes it and sends the PR back
        to review. No review record is written, so no round is spent.

        ``fix_remove``: the sync was started by a fix whose branch conflicts with the base
        (#250). Escalations remove these labels, and on success the labels stay as they are, so
        that the fix runs on the synced head on a later tick."""
        cfg, lb = self.cfg, self.cfg.labels
        remove = [lb.review, lb.ready_to_merge] if fix_remove is None else fix_remove
        base = f'origin/{cfg.base_branch}'
        if pr.meta['implementer'] not in cfg.agents:
            return self._escalate(
                pr.number, f'La PR è in conflitto con `{cfg.base_branch}` e il suo implementer '
                           f'(`{pr.meta["implementer"]}`) non è più configurato: serve una '
                           'decisione su chi risolve i conflitti.', remove=remove,
                cls='configuration')
        agent = cfg.agent(pr.meta['implementer'])
        issue, _ = self._issue_context(pr.issue)
        name = f'pr-{pr.number}'
        path = self.ws.existing_branch(name, pr.head_ref)
        if self.ws.head(path) != pr.head_sha:
            self.log('  head moved since the snapshot; retrying next tick')
            return
        # Git wants a committer even to start the merge: the implementer's, as for the commit.
        files = self.ws.start_merge(path, author=self._git_auth(agent.key, IMPLEMENT))
        if files is None:
            self.log(f'  the branch already contains {base}; waiting for GitHub to recompute')
            self.ws.remove(name)
            return
        target = self.ws.merge_head(path)
        listed = _format_conflicts((f, self.ws.base_commits(path, f)) for f in files)
        message = f'Merge {base} into {pr.head_ref}'
        tested = None
        if files:
            message += '\n\nConflicts resolved in:\n' + '\n'.join(f'- {f}' for f in files)

            # A failed sync is non-critical (#182) unless a file in conflict is sensitive: a
            # path of the review policy's sensitive level or of ``operator_only_paths``.
            sensitive = [f for f in files if self.cfg.review_policy.covers(f)
                         or self.cfg.merge.operator_only(f)]

            def fail(reason, detail='', cls=None):
                cls = cls or ('sync_sensitive' if sensitive else SYNC_FAILED)
                supervised, why = self._supervised(pr.number, cls, 'sync', pr)
                self._escalate(pr.number, f'Sincronizzazione con `{cfg.base_branch}` non '
                                          f'riuscita. {reason}\n\nFile in conflitto:\n\n{listed}',
                               detail, remove=remove, cls=cls, supervised=supervised, why=why)
            prompt = _template('sync', repo=cfg.repo, pr_number=pr.number, issue_number=pr.issue,
                               issue_title=issue['title'], issue_body=issue.get('body') or '',
                               base_branch=cfg.base_branch, conflicts=listed,
                               test_cmd=cfg.test_cmd, language=cfg.language)
            try:
                result, failure, used, tested, _ = self._write_turn(
                    agent, prompt, 'sync', path, f'pr-{pr.number}-sync',
                    where=(pr.number, 'sync'))
            except AgentError as exc:
                # An agent that could not start, even after its retry (#218), is a failure of
                # the tool, not a conflict to resolve: ``tool_failure`` prevails.
                return fail(f'Il passo `sync` è fallito: {_failure_text(exc)}'
                            f'{self._log_hint(exc)}', exc.tail,
                            cls=TOOL_FAILURE if _start_failure(exc) else None)
            if result['status'] == 'blocked':
                return self._blocked(pr.number, used, result['blocked_reason'].strip()
                                     + f'\n\nFile in conflitto con `{cfg.base_branch}`:\n\n'
                                     + listed, remove=remove, on_pr=True)
            if failure:
                return fail(failure[0], failure[1])
            # The dispatcher started the merge and concludes it: an agent that committed, reset
            # or aborted it leaves a result that is not the reviewed branch plus the base.
            if self.ws.head(path) != pr.head_sha or self.ws.merge_head(path) != target:
                return fail('Il merge in corso è stato concluso o annullato durante la '
                            'risoluzione dei conflitti, quindi non viene pubblicato.')
            left = self.ws.conflict_markers(path, files)
            if left:
                return fail('Restano marcatori di conflitto in '
                            + ', '.join(f'`{f}`' for f in left) + '.')
            message = self._commit_message(agent, message, pr.issue)
        else:
            message += f'\n\nRefs #{pr.issue}\n'
        auth = self._git_auth(agent.key, IMPLEMENT)
        head = self.ws.commit_all(path, message, author=auth)
        self._bind_tested(tested, path, head)
        # The branch never differs from the base under protected_paths, and neither may the merge.
        protected = self._protected_changes(path, since=base)
        if protected:
            return self._escalate_protected(pr.number, protected, remove=remove)
        self.ws.push(path, pr.head_ref, auth=auth)
        self._pushed(path)
        then = ('La PR torna in review sul nuovo head' if fix_remove is None else
                'La correzione in attesa parte dal nuovo head al prossimo tick')
        text = (f'\n## Sincronizzazione con `{cfg.base_branch}`\n\n'
                f'Merge di `{base}` (`{target[:12]}`) nel branch, nuovo head `{head[:12]}`. '
                f'{then}; la sincronizzazione non consuma un round di review.\n\n')
        if files:
            text += f'File in conflitto risolti:\n\n{listed}\n\n{result["summary"].strip()}'
            # Who resolved the conflicts wrote code on the PR: the marker keeps its profile,
            # so a later change of configuration cannot make it a reviewer of its own code.
            # ``from``: the head it synced, which a fix waiting for it recognizes (#250).
            self._as(agent.key, PR_COMMENT).comment(
                pr.number, publishable(self.runner.header(used) + text, 'sync',
                                       {'head_sha': head, 'from': pr.head_sha,
                                        'profile': self._profile(agent.key, 'write', used),
                                        **skipped_models(used)}))
        else:
            # A sync without an agent is an event, not a turn (#133).
            self._ledger_event(pr.number, 'sync', base=target, head=head, previous_head=pr.head_sha)
            self.gh.comment(pr.number, publishable(dispatcher_header() + text
                                                   + 'Il merge locale non aveva conflitti.'))
        if fix_remove is None:
            # A ready PR whose merge is the operator's (#248) loses the dispatcher's label too.
            self.gh.edit_labels(pr.number, add=[lb.review],
                                remove=[lb.ready_to_merge, lb.changes, *self._released(pr)])
        self.ws.remove(name)
        self.log(f'  merged {base} into {pr.head_ref}: {head[:12]}')


def _open_findings_reason(findings):
    """Why an approval is not promoted while other owners' findings are open."""
    listed = '\n'.join(f'- `{f["key"]}` ({f["title"]})' for f in findings)
    return ('L\'approvazione non chiude i finding aperti da altri: un finding lo chiude solo il '
            'reviewer che l\'ha aperto, la CI verde (per i finding `ci`) o l\'operatore. Restano '
            f'aperti:\n\n{listed}\n\nUn utente fidato può chiuderli con un commento sulla PR '
            f'che contiene una riga `{FINDING_COMMAND} <round/reviewer/ID> resolved|withdrawn '
            '<motivazione>`, poi riportare la PR in `agent:review`: l\'approvazione registrata '
            'viene riapplicata senza una nuova review.')


def _review_failure(failures):
    """The exception that ends a review action whose reviewers ``failures`` (``(key, exc)``)
    failed: the only one, or one that lists them all when every one is an agent failure."""
    if len(failures) == 1 or not all(isinstance(exc, AgentError) for _, exc in failures):
        return failures[0][1]
    error = AgentError('; '.join(f'`{key}`: {str(exc).strip()}' for key, exc in failures),
                       getattr(failures[0][1], 'log', None))
    error.reviewer = ','.join(key for key, _ in failures)
    return error


def _trusted_comments(comments, trusted_users):
    """The comments of trusted users: the ones that reach the prompts."""
    return [c for c in comments if (c.get('author') or {}).get('login') in trusted_users]


def _render_issue_comments(comments, trusted_users, accepted=()):
    """The comments of trusted users as model input, or ''. ``accepted``: the dispatcher's
    records of defaults the supervisor accepted (#182) or of its decisions on the gaps of a
    readiness check (#258), which count as trusted comments."""
    parts = [f'### Comment by {c["author"]["login"]}\n\n{c["body"]}'
             for c in _trusted_comments(comments, trusted_users)]
    parts += [_render_acceptance(record) for record in accepted]
    rendered = '\n\n'.join(parts)
    return ('## Issue comments\n\n' + rendered) if rendered else ''


def _render_acceptance(record):
    """A record of accepted defaults (#182), or of a decision on the gaps of a readiness check
    (#258), as model input."""
    if record['command'] == DECIDE:
        listed = '\n'.join(f'- {g["description"].strip()}\n  Proposed default: '
                           f'{g["proposed_default"].strip()}' for g in record.get('gaps') or [])
        return (f'### Decision of the supervisor ({record["by"]}) on the gaps of the readiness '
                'check, recorded by devloop\n\n'
                f'The gaps the readiness check found:\n\n{listed}\n\n'
                'The supervisor decided them as follows: follow this decision, which may accept '
                f'some proposed defaults and replace others.\n\n{record["reason"].strip()}')
    listed = '\n'.join(f'- {d["description"].strip()}\n  Accepted default: '
                       f'{d["proposed_default"].strip()}' for d in record.get('defaults') or [])
    return (f'### Defaults accepted by the supervisor ({record["by"]}), recorded by devloop\n\n'
            'The defaults the readiness check proposed for these gaps are accepted: follow '
            f'them.\n\n{listed}')


def _failure_text(exc):
    """``exc`` as the end of a sentence: a one-line message gets its full stop, one that ends
    with a list (``ModelsUnavailable``) a blank line."""
    text = str(exc).rstrip()
    return text + ('\n\n' if '\n' in text else '.')


def _start_failure(exc):
    """Whether the failed turn ``exc`` is an agent that could not start (#218), also when the
    chain of models wraps it (``ModelsUnavailable``)."""
    return isinstance(exc, StartFailure) or isinstance(exc.__cause__, StartFailure)


def _describe_bases(checks, branch):
    """The base checks of ``_base_check`` as a Markdown list."""
    lines = []
    for check in checks:
        span = f'`{check["from"][:12]}` → `{check["to"][:12]}`'
        if check['outcome'] == 'disjoint':
            lines.append(f'- {span}: nessun commit intermedio di `{branch}` tocca file della PR.')
        elif check['outcome'] == 'touched':
            lines.append(f'- {span}: i commit intermedi di `{branch}` toccano file della PR: '
                         + ', '.join(f'`{p}`' for p in check['paths']) + '.')
        elif check['outcome'] == 'conflict':
            files = ', '.join(f'`{p}`' for p in check.get('paths') or [])
            lines.append(f'- {span}: i commit intermedi di `{branch}` non toccano file della PR, '
                         'ma la PR non si unisce senza conflitti alla base corrente'
                         + (f' ({files})' if files else '') + ': serve una sincronizzazione.')
        else:
            lines.append(f'- {span}: non è stato possibile elencare i file toccati dai commit '
                         f'intermedi (per esempio `{branch}` è stato riscritto), o verificare '
                         'che la PR si unisca senza conflitti alla base corrente: la non '
                         'interferenza non è dimostrata.')
    return '\n'.join(lines)


def _format_conflicts(conflicts):
    """``(file, base commits)`` pairs as a Markdown list."""
    lines = []
    for name, commits in conflicts:
        lines.append(f'- `{name}`')
        lines += [f'  - `{c.split(" ", 1)[0]}` {c.partition(" ")[2]}'.rstrip() for c in commits]
    return '\n'.join(lines) or '(none)'


def label_transition(gh: Tracker, labels):
    """Where the repository stands in the rename of ``agent:needs-operator`` from its name
    before #248 (``LEGACY_NEEDS_OPERATOR``): ``rename`` when only the old label exists,
    ``both`` when both do, None when the old one is gone."""
    names = set(gh.repo_labels())
    if LEGACY_NEEDS_OPERATOR not in names:
        return None
    return 'both' if labels.needs_operator in names else 'rename'


def both_labels_warning(labels):
    """What ``setup`` and ``status`` report when both names of the operator label exist."""
    return (f'both {LEGACY_NEEDS_OPERATOR} and {labels.needs_operator} exist: setup renames '
            f'nothing and touches neither, and both block as {labels.needs_operator}. Move the '
            f'issues and PRs of {LEGACY_NEEDS_OPERATOR} to {labels.needs_operator}, then delete '
            f'{LEGACY_NEEDS_OPERATOR}')


def transition_labels(gh: Tracker, labels):
    """The transition of #248, for ``devloop setup`` and the first tick of a process:
    ``agent:needs-operator`` is renamed from its old name when only that exists, so that the
    issues and PRs that have it keep it; with both names, neither label is touched
    (``both_labels_warning``). Returns ``(label_transition, note for the log)``, the note ''
    when there is nothing to say."""
    transition = label_transition(gh, labels)
    note = ''
    if transition == 'rename':
        color, description = LABEL_STYLE['needs_operator']
        gh.rename_label(LEGACY_NEEDS_OPERATOR, labels.needs_operator, color, description)
        note = (f'renamed {LEGACY_NEEDS_OPERATOR} to {labels.needs_operator}: the issues and PRs '
                'that had it keep it')
    elif transition == 'both':
        note = both_labels_warning(labels)
    return transition, note


def ensure_labels(gh: Tracker, labels, review_policy=None):
    """Create or update devloop's labels, and return what the transition of #248 did, for the
    log ('' when nothing): see ``transition_labels``."""
    transition, note = transition_labels(gh, labels)
    for attr, (color, description) in LABEL_STYLE.items():
        if attr == 'needs_operator' and transition == 'both':
            continue
        gh.ensure_label(getattr(labels, attr), color, description)
    if review_policy is not None:
        gh.ensure_label(review_policy.label, *LEVEL_LABEL_STYLE)
        if review_policy.zero_enabled():
            gh.ensure_label(review_policy.zero_label, *ZERO_LABEL_STYLE)
    for name, (color, description) in PRIORITY_LABEL_STYLE.items():
        gh.ensure_label(name, color, description)
    return note
