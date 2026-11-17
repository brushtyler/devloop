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
(``_do_supervise``). Critical and unknown classes always go to agent:needs-operator.

Requested fixes (#195): ``/devloop fix`` from a trusted user or the supervisor's identity, on a
PR in agent:review or agent:changes, runs a fix of the current head with the rest of the comment
as a requirement, without a review and without a round (``_do_requested_fix``). One such fix per
head: a later command on the same head counts only after a new commit (``_fix_requests``).

CI of a new head (#263, ``[ci]``): with ``local_tests`` other than ``full``, a PR in agent:review
whose head has no CI is treated for ``grace_minutes`` after its push as if its CI were pending
(``_ci_wait``): the push the dispatcher recorded, else the commit date. Then the full suite runs
locally, as before, unless the base branch requires checks: the PR waits for them, and after
``required_timeout_minutes`` it is escalated (``ci_missing``).

Review during the CI (#271): a review starts with the CI of the head pending, or within that
window, without the full suite locally; its record says CI ``pending``. The approval is promoted
only once the CI of the same head passes, replayed without a new review (``_approval_current``);
a CI that fails makes it a ``ci_failed`` round. An approval that saw no CI, on a head still
without CI past the window, gets the full suite locally before its promotion
(``_do_apply_review``).
"""

import hashlib
import itertools
import json
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
from .context import (DOCS_PATHS_SHOWN, LABEL_STYLE, PROMPT_DIR, REREVIEW_COMMAND,
                      _describe_bases, _docs_list, _failure_text, _format_conflicts,
                      _format_findings, _format_requested, _open_findings_reason,
                      _render_acceptance, _render_issue_comments, _review_failure,
                      _start_failure, _template, _trusted_comments, both_labels_warning,
                      docs_diff_section, docs_map_section, docs_review_section,
                      format_fix_requests, format_operator_comments, format_previous_rounds,
                      label_transition, operator_comments, requested_fixes,
                      requests_rereview, transition_labels)
from .dispatch_actions import (ActionsMixin, Models, ready_approval, rerun_of, skipped_models,
                               synced_from)
from .dispatch_merge import MergeMixin
from .dispatch_preflight import PreflightMixin, devloop_text, normalize_preflight
from .dispatch_supervision import (HOLD_COMMAND, RELEASE_COMMAND, RESUME_STEP, SupervisionMixin,
                                   UntrustedChange, fix_command, merge_command)
from .github import GitHubError, author_login, job_id
from .ledger import entries as ledger_entries
from .ledger import open_turns
from .localtests import failed_modules, module_command, select_modules
from .markdown import (RESUME, carries_record, dispatcher_header, publishable, read_marker,
                       render_cycle, render_escalation, render_fix, render_native_review,
                       render_operator_merge,
                       render_preflight, render_review, render_supervision)
from .activity import iso, restart_line
from .pin import PREFIX, PinError, Repinned, SmokeError
from .planning import (BASE_CHANGED, EXPENSIVE, FIX_COMMAND, GLOBAL_ACTIONS, MERGE_LANE,
                       MERGING, PRIORITY_LABELS, PRIORITY_RANK, RECHECK_COMMAND,
                       RESYNC_AFTER_APPROVAL, Action, PullRequest, Snapshot, _lanes,
                       _parents, _plan_preflight, _resynced, _round_records, admit,
                       counted_rounds, issue_priority, label_names, merge_ready,
                       numbers_of, operator_blocked, operator_held, operator_merge_due,
                       parked, pending_reviewers, plan, pr_counts, preflight_state,
                       queue_lines, schedule, start_order, touched_by_base, waived_rounds,
                       zero_labels_hold)
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

LEVEL_LABEL_STYLE = ('5319e7', 'devloop: la PR richiede una review di livello 2')
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


def _timestamp(value):
    """Seconds since the epoch of an ISO 8601 date with its zone, as GitHub writes them, or
    None when ``value`` is not one."""
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment.timestamp() if moment.tzinfo is not None else None


def _ci_unknown(records):
    """Whether a reviewer of the round ``records`` judged its head before the CI outcome was
    known (#271): its record says CI ``pending``, also for a head without CI yet in the window
    of #263."""
    return any(r.get('ci') == 'pending' for r in records)


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


class Dispatcher(SupervisionMixin, PreflightMixin, MergeMixin, ActionsMixin):
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
        # Continuous mode (#301): ``(sha12, since)`` of the new pinned copy whose restart waits
        # for the running actions, or None.
        self._restart = None
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

        The pin after a merge runs at once, also while actions run (#301): a new copy only
        moves ``current``, and the restart from it waits for the first tick at which nothing
        runs, while this one plans and starts actions with the code it loaded, as without
        ``[pin] auto``. After ``[pin] restart_max_wait_minutes`` of a pending restart nothing
        starts until the running actions end and the restart happens. A merge that requests a
        pin while this tick reads GitHub (#264) is pinned by the next tick."""
        workers = self.workers
        # An action that ends from here on wakes the loop again: its result is not in this plan.
        workers.clear()
        running = workers.running()
        self._auto_pin(False, defer=bool(running))
        if not running:
            self._restart_if_pending()
        elif self._restart is not None:
            if self._restart_wait_over(running):
                return []
            self.log(restart_line(self._restart[0], iso(self._restart[1]), len(running)))
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
        merging = any(action.kind in MERGE_LANE for action, _ in running)
        actions = plan(snap, self.cfg, busy, reserved, merging)
        # The limit may pass while the snapshot reads GitHub: checked again before any start.
        if running and self._restart is not None and self._restart_wait_over(running):
            return []
        started = admit(actions, issue_of, running, workers.free())
        self.log(f'{pr_counts_line(snap, self.cfg)}; running: '
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
        self.log(f'{restart_line(sha, iso(since), len(running))}: over [pin] '
                 f'restart_max_wait_minutes ({limit}), no action starts until the restart')
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

    def _auto_pin(self, dry_run, defer=False):
        """With ``[pin] auto = true`` and a pin requested by a merge (#174), pin
        ``origin/<base_branch>`` before planning. A failed pin stops the tick with ``PinError``
        and leaves the pointer and the request as they were; a new copy raises ``Repinned``, so
        that the process starts again from it. With ``defer`` (actions run, #301) a new copy
        only records the restart, which waits for them (``_set_restart``).

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

    def _apply_round(self, pr, records, announce, reviews, tested=False):
        """Apply a complete round, ``records`` (one per reviewer). ``reviews``: every review of
        the PR up to the round included. Every finding still open counts here, whoever owns
        it: an approval does not close another owner's finding. ``tested``: the full suite
        passed locally on the head after a review that did not know its CI (#271)."""
        lb = self.cfg.labels
        transient = [lb.review, lb.changes, lb.ready_to_merge]
        verdict = round_verdict(records)
        if verdict == 'approve':
            left = open_findings(reviews, pr.closures)
            if left:
                # Owners outside the round (CI, an earlier reviewer): no agent closes them.
                self._escalate(pr.number, _open_findings_reason(left), remove=transient,
                               cls='open_findings')
            elif self._approval_current(pr, records[0],
                                        blind=not tested and _ci_unknown(records)):
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

    def _do_apply_review(self, pr):
        """Apply the round already recorded. An approval given while the CI was not known
        (#271), on a head that got no CI past the window of #263 and with ``local_tests`` other
        than ``full``, gets the full suite locally first, as a review would have before it."""
        records = current_round(pr.reviews)
        tested = False
        if (round_verdict(records) == 'approve' and pr.checks == 'none'
                and self.cfg.local_tests != 'full' and _ci_unknown(records)):
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
