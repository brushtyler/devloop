"""Planning of devloop: the snapshot of issues and PRs, the actions of a tick and the queue.

Moved from ``devloop/dispatcher.py`` without changes (#291, step S1 of #261): ``plan`` turns a
``Snapshot`` into the ordered ``Action`` list of a tick, ``schedule`` and ``admit`` decide how
those actions run in parallel and in continuous mode, and the helpers here give the priority and
start order of ready issues, the state of a readiness check, the review rounds that count and the
paths a base change reaches. Since #297 (step S7) ``devloop/dispatcher.py`` no longer re-exports
these names: import them from here.
"""

from dataclasses import dataclass, field, replace

from .policy import receipt_of
from .review import CI, current_round, flow_verdict, open_findings, owner_of, round_verdict
from .supervision import BUDGET_GRANTABLE, GRANT_BUDGET, ISSUE_STEPS, RETRY

RECHECK_COMMAND = '/devloop recheck'
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
EXPENSIVE = ('review', 'fix', 'implement', 'sync', 'preflight', 'merge', 'supervise',
             'conclave', 'advisory')
# Actions that write to the whole repository or to devloop itself (the pin of #174): with
# ``max_parallel_actions`` above 1 each runs alone, after the actions planned before it and
# before those planned after it (#176), and in continuous mode only when nothing runs (#227).
GLOBAL_ACTIONS = frozenset({'pin'})
# Actions that write to the base branch (the merge of #172). In continuous mode they run in a
# lane of their own (#264): one at a time, alongside the actions on other numbers, since those
# work in their own worktrees and already handle a base that moves (#160, #235, #250, #252),
# as they must for a merge made by a person. In a tick's ``schedule`` each is still a phase of
# its own, as a global action.
MERGE_LANE = frozenset({'merge'})
# Priority labels of ready issues (#146) and their rank: the lower, the earlier an issue starts.
PRIORITY_LABELS = {'priority:high': 'high', 'priority:normal': 'normal', 'priority:low': 'low'}
PRIORITY_RANK = {'high': 0, 'normal': 1, 'low': 2}


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
    # Why the attempt of the marker does not hold (#384, ``attempt_identity``), ``''`` when it
    # holds or the marker has none: an attempt the journal does not confirm, or one concluded
    # already, as for a PR reopened after its outcome.
    attempt_problem: str = ''
    # CI events the dispatcher's ledger already recorded (#133), as ``(head, state, round,
    # checks)``: reading the same checks again for the same head and round records nothing new.
    ci_recorded: list = field(default_factory=list)
    # The review rounds the supervisor granted to this PR beyond ``max_review_rounds`` (#405,
    # ``/devloop grant-round``), as the dispatcher's ``supervision`` records say: its limit is
    # ``max_review_rounds`` plus these, besides the rounds that do not count.
    granted_rounds: int = 0
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
    # In agent:review, a head without CI that waits for it (#263), as if its CI were pending: the
    # review starts, the approval waits (#271). Why, as the log says it ('' when it does not
    # wait), and the checks the base branch requires that are still
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
    # A trusted user's ``/devloop veto`` of the latest conclave decision on the PR or its issue
    # that the dispatcher has not applied yet (#142): ``{comment, by, reason, conclave}``.
    veto: dict = None


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
    # Issues and PRs in agent:needs-supervisor with a supervisor command to apply (#182), or in
    # agent:needs-operator with a trusted user's command (#321): dicts ``{number, command, by,
    # step, pr, anchor}`` (``_supervisor_view``), with ``refusal`` for a command only to answer;
    # a retry of an issue's step has ``blocked_by`` too, its open blocks (#277), and waits
    # while it is not empty.
    supervised: list = field(default_factory=list)
    # With ``conclave`` (#141), the issues and PRs in agent:needs-operator whose block still
    # needs its conclave: dicts ``{number, calls, reason}``, ``calls`` being the calls left
    # (``conclave.calls_left``), one action each.
    conclaves: list = field(default_factory=list)
    # With ``review_policy.advisory_reviewer`` (#369), the devloop PRs in agent:needs-operator
    # whose head has no advisory review yet: dicts ``{number, head}``, one action each.
    advisories: list = field(default_factory=list)


@dataclass(frozen=True)
class Action:
    kind: str
    number: int
    reason: str = ''
    # Whether the action may open a PR although its kind does not say so: a conclave that
    # restarts an implementation (#142). While it runs it holds a slot of ``max_open_prs``.
    opens: bool = False


def schedule(actions, issue_of):
    """How ``max_parallel_actions`` above 1 runs ``actions`` (#176): a list of phases, run one
    after the other, each a list of lanes that may run at once, each lane a list of
    ``(position in actions, action)`` to run in that order.

    Actions on the same number share a lane, in plan order, and so do an action on a PR and one
    on its issue (``issue_of``: PR number to issue number). A global action
    (``GLOBAL_ACTIONS``) or a merge (``MERGE_LANE``) is a phase of its own: it starts after
    every action planned before it has ended, and those planned after it start once it has
    ended."""
    phases, batch = [], []
    for position, action in enumerate(actions):
        if action.kind in GLOBAL_ACTIONS or action.kind in MERGE_LANE:
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


def runs_issue_step(item):
    """Whether the supervisor command ``item`` (``_supervisor_view``, with ``pr``) runs an
    issue's step when it applies, as the step itself would: a retry of an issue's step (#182),
    or a ``grant-budget`` on an issue's ``budget_grantable`` escalation (#411), which runs the
    step the envelope refused. It then waits while the issue is blocked (#277) and takes a free
    PR slot. A command only to answer (#321) runs nothing."""
    if item['pr'] or item.get('refusal') or item.get('step') not in ISSUE_STEPS:
        return False
    if item['command'] == GRANT_BUDGET:
        return (item.get('anchor') or {}).get('record', {}).get('class') == BUDGET_GRANTABLE
    return item['command'] == RETRY


def numbers_of(number, issue_of):
    """The numbers an action on ``number`` touches: ``number``, and its issue when it is a PR
    (``issue_of``: PR number to issue number)."""
    return {number, issue_of[number]} if number in issue_of else {number}


def admit(actions, issue_of, running, slots):
    """Continuous mode (#227): the actions of a plan to start now, in plan order, while
    ``running`` actions still run and ``slots`` places are free.

    A global action (``GLOBAL_ACTIONS``) starts only when nothing runs, alone and before the
    others; while one is planned and something runs, nothing starts, so that the running actions
    end and it runs next. While a global action runs, nothing starts either. A merge
    (``MERGE_LANE``, #264) is not global: it starts alongside the actions on other numbers and
    holds back none of them, but only one merge runs at a time; a merge planned while another
    runs waits for a later plan. Of several actions on the same number, or on a PR and its
    issue, only the first starts: the next plan, which sees its result, decides the others."""
    if any(action.kind in GLOBAL_ACTIONS for action, _ in running):
        return []
    first = next((a for a in actions if a.kind in GLOBAL_ACTIONS), None)
    if first is not None:
        return [] if running else [first]
    merging = any(action.kind in MERGE_LANE for action, _ in running)
    chosen, taken = [], set()
    for action in actions:
        if len(chosen) >= slots:
            break
        numbers = numbers_of(action.number, issue_of)
        if numbers & taken:
            continue
        taken |= numbers
        if action.kind in MERGE_LANE:
            if merging:
                continue
            merging = True
        chosen.append(action)
    return chosen


def label_names(labels):
    return {label['name'] if isinstance(label, dict) else label for label in labels}


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


def plan(snapshot, cfg, busy=frozenset(), reserved=0, merging=None, implement=True):
    """Pure planning: the actions one tick should take, in order.

    In continuous mode (#227) ``busy`` holds the numbers of the issues and PRs that actions
    still running touch (``numbers_of``): no action of the plan touches them. ``reserved`` is
    how many of those actions may open a PR (an implementation, a readiness check, a supervisor
    command on an issue, a conclave that restarts an implementation, #142): each holds a slot
    of ``max_open_prs``, as it did in its own tick.
    ``merging`` tells whether a merge runs (continuous mode only, None in a tick): merges run one
    at a time (``MERGE_LANE``, #264), so only the first merge of the plan, and none while one
    runs, takes a slot of ``max_actions_per_tick``. The others stay in the plan, where ``admit``
    holds them back with their numbers, and take no slot from the actions that can start now.
    A merge that finds the limit already spent is dropped and does not take the lane: so is
    every merge after it, which would otherwise start in its place past the limit.
    Without ``implement`` (a restart pending in continuous mode, #342) no implementation is
    planned, and an issue that would start one takes no slot of ``max_open_prs`` nor of
    ``max_actions_per_tick``: they stay to the other actions, readiness checks included."""
    lb = cfg.labels
    actions = []
    issue_of = {pr.number: pr.issue for pr in snapshot.prs}
    if busy:
        snapshot = replace(
            snapshot,
            ready_issues=[i for i in snapshot.ready_issues if i['number'] not in busy],
            supervised=[s for s in snapshot.supervised
                        if not numbers_of(s['number'], issue_of) & busy],
            # A conclave still running takes no second PR slot (#142).
            conclaves=[c for c in snapshot.conclaves
                       if not numbers_of(c['number'], issue_of) & busy],
            advisories=[a for a in snapshot.advisories
                        if not numbers_of(a['number'], issue_of) & busy])
    pr_issues = {pr.issue for pr in snapshot.prs}
    # Only the PRs devloop works on take the slots of max_open_prs; those waiting for a person
    # have their own limit, and once it is reached no new issue starts (#199), except a
    # priority:high one (#274): it may be the fix the parked PRs wait for.
    working, held = pr_counts(snapshot, cfg)
    slots = cfg.max_open_prs - working - reserved
    parked_full = held >= cfg.max_parked_prs
    free = 0 if parked_full else slots
    for item in snapshot.supervised:
        # A supervisor command (#182). A retry of an issue's step, or the extension of the
        # budget envelope of an issue's refused step (#411), runs that step, and takes a free PR
        # slot as the step itself would; without one it waits for a later tick. While the issue
        # is blocked by an open issue (#277) it waits too, and takes no slot. A command only to
        # answer (#321) takes no slot.
        if runs_issue_step(item):
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
        if pr.veto:
            # A trusted user vetoed the latest conclave decision (#142): the work it restarted
            # stops before anything else, a merge included.
            actions.append(Action('veto', pr.number, f'/devloop veto by {pr.veto["by"]}'))
            continue
        if not pr.meta:
            actions.append(Action('lost_meta', pr.number, 'PR body no longer ends with its marker'))
            continue
        if pr.attempt_problem:
            # Its attempt not confirmed by the journal, or concluded (#384): no work, no merge.
            actions.append(Action('attempt', pr.number, 'the attempt of its marker does not hold'))
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
        # every reviewer its receipt requires has reviewed, or, with the conclave (#141), once
        # one of them asked for the operator.
        latest = current_round(pr.reviews)
        pending = waiting_reviewers(latest, cfg.conclave)
        verdict = round_verdict(latest) if latest and not pending else None
        head = latest[0]['head_sha'] if latest else None
        rounds_left = counted_rounds(pr.reviews) < cfg.max_review_rounds + pr.granted_rounds
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
            elif pr.checks == 'fail' and ci_joins_round(pr):
                # One head, one round (#359): the failure joins the round that approved this
                # head, voids its approval and takes no round of the limit.
                actions.append(Action('ci_failed', pr.number,
                                      'CI failed after approval: it joins its round'))
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
            blind = not settled and changes_before_ci(pr)
            if settled:
                actions.append(Action('reconcile', pr.number, 'every open finding is closed'))
            elif blind and (pr.checks == 'pending' or pr.ci_wait
                            or (pr.checks == 'fail' and pr.rerun_waiting)):
                # The review asked for changes with the CI of this head running (#271): the
                # fix waits for its outcome, so that a failure joins the same fix.
                continue
            elif blind and pr.checks == 'fail' and ci_joins_round(pr):
                # One head, one round (#352): the failure joins the round that asked for
                # changes, as a ci finding, and takes no round of the limit.
                actions.append(Action('ci_failed', pr.number,
                                      'CI failed after the review: it joins its round'))
            elif blind and pr.checks == 'fail':
                actions.append(Action('ci_failed', pr.number, 'CI failed after the review')
                               if rounds_left else Action('round_limit', pr.number))
            else:
                actions.append(Action('fix', pr.number))
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
            elif pr.ci_missing:
                # The checks the base branch requires never appeared on the head (#263).
                actions.append(Action('ci_missing', pr.number, 'required checks missing: '
                                      + ', '.join(pr.ci_missing)))
            elif pr.checks == 'fail' and pr.rerun_waiting:
                continue  # the failed jobs were re-run (#200) and have not started yet
            elif ((pr.checks == 'pending' or pr.ci_wait) and cfg.review_policy.zero_enabled()
                  and cfg.review_policy.zero_label in pr.labels):
                # Level 0 (#107) has no review to start early and needs every expected check:
                # it waits for the CI, as before #271.
                continue
            elif replay and not unbound and not unmapped and pr.checks != 'fail':
                if pr.checks == 'pending' or pr.ci_wait:
                    # The review started with the CI running (#271), or on a head without CI
                    # yet (#263): the approval waits for the CI of this same head. Passed, it
                    # is applied without a new review; failed, it goes to ci_failed below.
                    continue
                actions.append(Action('apply_review', pr.number, 'approval already recorded'))
            elif replay and pr.checks == 'fail' and ci_joins_round(pr):
                # One head, one round (#359): the CI of the approved head failed, the approval
                # waiting for it or not. The failure joins that round and voids the approval.
                actions.append(Action('ci_failed', pr.number,
                                      'CI failed after the approval: it joins its round'))
            elif partial and pr.checks == 'fail' and ci_joins_round(pr):
                # The round in progress has a request for changes (#352), or only approvals so
                # far (#359): the failure joins it, and the round still waits for its other
                # reviewers.
                actions.append(Action('ci_failed', pr.number,
                                      'CI failed during the review: it joins its round'))
            elif partial and (pr.checks != 'fail' or ci_in_round(latest)):
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
                          else 'CI pending: the merge waits for it'
                          if pr.checks == 'pending' or pr.ci_wait
                          else '')
                actions.append(Action('review', pr.number, reason))
    # The conclave on a block (#141): one action per call still due, each taking a slot of
    # max_actions_per_tick; those beyond it run on a later tick, from the recorded point. The
    # restart of an implementation after a unanimous decision (#142) also takes a free PR slot,
    # as the step itself would; without one it waits for a later tick. The action says so
    # (``opens``): in continuous mode it keeps the slot while it runs (``reserved``).
    for item in snapshot.conclaves:
        opens = bool(item.get('opens'))
        if opens:
            if free <= 0:
                continue
            free -= 1
        actions += [Action('conclave', item['number'], item['reason'], opens)] * item['calls']
    # The advisory review of a PR in agent:needs-operator (#369): one action per head, which
    # takes a slot of max_actions_per_tick and no PR slot.
    for item in snapshot.advisories:
        actions.append(Action('advisory', item['number'],
                              f'advisory review of {item["head"][:12]} for the operator'))
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
        actions += _plan_preflight(snapshot, cfg, pr_issues | held_back, free, urgent, implement)
    elif free > 0 and implement:
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
        lane = merging is not None and action.kind in MERGE_LANE
        if lane and merging:
            # The merge lane is taken (#264): ``admit`` holds it back, with its numbers.
            capped.append(action)
            continue
        if action.kind in EXPENSIVE:
            if spent >= cfg.max_actions_per_tick:
                continue
            spent += 1
        if lane:
            merging = True  # only a merge the plan keeps takes the lane
        capped.append(action)
    return capped


def _plan_preflight(snapshot, cfg, pr_issues, free, urgent='', implement=True):
    """New work with the readiness check (#114), for the ready issues not in ``pr_issues`` (an
    open PR, a merged one, #228, or an open block, #277). Issues start in the usual order, up to
    ``free``: one whose check for its current text passed is implemented, one without a valid
    check is checked first (implemented on a later tick if ready). With ``urgent``, the reason
    why ``max_parked_prs`` is reached (#274), only ``priority:high`` issues start, and their
    actions carry it. A failed check that is still
    valid blocks the issue again if its ``agent:needs-operator`` was removed. An issue the check
    blocked is checked again, whatever ``free``, when its text changed, a trusted user asked
    for it or someone who may admit it added ``agent:ready`` again (#270); when the base branch
    advanced past a failure that depends on it (#270), only within ``free``, as any check. Any
    other ``agent:needs-operator`` (or its name before #248) is left alone. An issue in
    ``agent:needs-supervisor`` waits for the supervisor (#182). Without ``implement`` (#342) an
    issue whose check passed waits, and takes no slot from the checks of the others."""
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
                if reason == BASE_ADVANCED:
                    # Nobody asked for this check: it takes a slot, as a first check does.
                    if taken >= free or (urgent and issue.get('priority') != 'high'):
                        continue
                    taken += 1
                actions.append(Action('preflight', issue['number'], reason))
        elif state == 'not_ready':
            actions.append(Action('preflight_block', issue['number'],
                                  'the readiness check of the current text failed'))
        elif state == 'ready' and not implement:
            continue  # a restart is pending (#342): implemented after it
        elif taken < free and (not urgent or issue.get('priority') == 'high'):
            taken += 1
            actions.append(Action('implement', issue['number'], urgent) if state == 'ready'
                           else Action('preflight', issue['number'],
                                       f'{reason}; {urgent}' if urgent else reason))
    return actions


BASE_ADVANCED = 'the base branch advanced since the failed readiness check, whose gaps depend on it'


def repository_bound(record):
    """Whether a readiness check's failure holds only for the commit of the base branch it ran
    on (#270): it failed with at least one gap ``repository_dependent`` and recorded that commit
    (``base``). A record written before #270 has neither, and holds for its text alone."""
    return (record is not None and not record['ready'] and isinstance(record.get('base'), str)
            and any(g.get('repository_dependent') is True for g in record['gaps']))


def preflight_state(view):
    """``(state, reason)`` of an issue's readiness check: ``run`` when a check is needed,
    otherwise ``ready`` or ``not_ready``, the outcome of the latest check, which holds while the
    issue text keeps its hash. A failed check holds also only while no one who may admit the
    issue added ``agent:ready`` again after it (``relabeled``, the login, #270) and, when it is
    ``repository_bound``, while the base branch stays at the commit it ran on (``base``, the
    current one, None when unknown: the outcome holds)."""
    record = view['record']
    if view['recheck']:
        return 'run', f'{RECHECK_COMMAND} by a trusted user'
    if record is None:
        return 'run', 'no readiness check yet'
    if record['hash'] != view['hash']:
        return 'run', 'the issue text changed since the readiness check'
    if not record['ready'] and view.get('relabeled'):
        return 'run', f'agent:ready added again by {view["relabeled"]} after the failed check'
    if repository_bound(record) and view.get('base') and view['base'] != record['base']:
        return 'run', BASE_ADVANCED
    return ('ready' if record['ready'] else 'not_ready'), ''


def pending_reviewers(records):
    """The reviewers an agent round still waits for: those the receipt of its first review
    requires and that have not reviewed yet. A CI round, or a review without a receipt, is
    complete as it is."""
    receipt = receipt_of(records[0]) if records and records[0]['source'] == 'agent' else None
    if receipt is None:
        return []
    done = {owner_of(r) for r in records}
    return [key for key in receipt['reviewers'] if key not in done]


def waiting_reviewers(records, conclave=()):
    """The reviewers the round ``records`` still waits for before its verdict counts: those of
    ``pending_reviewers``, or none with ``conclave`` configured (#141) once a reviewer of the
    round asked for the operator. The first ``needs_operator`` suspends the round: the verdict
    applies at once, no other review of the round is asked, and the round opens one conclave.
    A ``needs_operator`` for the trust policy (#434) asks for no decision: it suspends nothing,
    and the round waits for every reviewer of the quorum."""
    if conclave and any(flow_verdict(r) == 'needs_operator' for r in records):
        return []
    return pending_reviewers(records)


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


def ci_unknown(records):
    """Whether a reviewer of the round ``records`` judged its head before the CI outcome was
    known (#271): its record says CI ``pending``, also for a head without CI yet in the window
    of #263."""
    return any(r.get('ci') == 'pending' for r in records)


def ci_in_round(records):
    """Whether the round ``records`` already holds the failure of the CI (#352)."""
    return any(owner_of(r) == CI for r in records)


def ci_joins_round(pr):
    """The records of the round that a CI failure of ``pr``'s head joins, or ``[]`` when the
    failure opens a CI round of its own. One head, one round: the latest round is an agent
    round of this very head, not answered by a fix nor voided by a rereview request, that holds
    no CI failure yet and in which a reviewer asked for changes (#352) or every reviewer so far
    approved (#359), the round complete or still waiting for other reviewers. Its failed checks
    become a ``ci`` finding of that round, its verdict becomes ``request_changes`` (an approval
    no longer holds: ``voided_approval``), and one fix answers everything. A round where a
    reviewer asked for the operator, and none for changes, does not take it: its escalation
    goes on as before. Nor does an approval already void without a policy receipt, or whose
    receipt was found stale after it (the base revoked it, #252): the failure is a round of
    its own, as before."""
    latest = current_round(pr.reviews)
    if (not latest or latest[0]['source'] != 'agent' or latest[0]['head_sha'] != pr.head_sha
            or pr.rereview or ci_in_round(latest)
            or any(f['round'] == latest[0]['round'] for f in pr.fixes)):
        return []
    # A needs_operator for the trust policy (#434) counts as the request for changes or the
    # approval it acts as (``flow_verdict``).
    verdicts = {flow_verdict(r) for r in latest}
    if 'request_changes' in verdicts:
        return latest
    if (verdicts == {'approve'} and not pr.stale_receipt
            and all(receipt_of(r) is not None for r in latest)):
        return latest
    return []


def voided_approval(records):
    """Whether the CI failure in the round ``records`` voided its approval (#359): every
    reviewer of the round, the ``ci`` record aside, approved."""
    reviewers = [r for r in records if owner_of(r) != CI]
    return bool(reviewers) and all(flow_verdict(r) == 'approve' for r in reviewers)


def changes_before_ci(pr):
    """Whether ``pr``, in agent:changes, waits for the CI of its head before the fix (#271):
    its latest round asked for changes on this very head before the CI outcome was known, and
    does not hold the CI failure yet. A CI that then fails joins that same round (#352,
    ``ci_joins_round``), and the same fix."""
    latest = current_round(pr.reviews)
    return (bool(latest) and latest[0]['head_sha'] == pr.head_sha and ci_unknown(latest)
            and not ci_in_round(latest))


def counted_rounds(reviews):
    """How many rounds count against ``max_review_rounds``: every round, CI rounds and rereviews
    included, except the waived ones (``waived_rounds``). Round numbers stay progressive."""
    return len({r['round'] for r in reviews} - waived_rounds(reviews))


def last_counted_ci(reviews):
    """Whether the latest round of ``reviews`` that counts against the limit (``counted_rounds``)
    is a CI failure, as ``round_reasons`` names it (#405)."""
    counted = sorted({r['round'] for r in reviews} - waived_rounds(reviews))
    return bool(counted) and _round_records(reviews, counted[-1])[0]['source'] != 'agent'


def findings_decreasing(reviews, closures=()):
    """Whether the blocking findings left open after the latest round of ``reviews`` are fewer
    than those left open after the round before it (#405), with the ``closures`` recorded. A PR
    with a single round has none before: not decreasing."""
    rounds = sorted({r['round'] for r in reviews})
    if len(rounds) < 2:
        return False
    before = [r for r in reviews if r['round'] < rounds[-1]]
    return len(open_findings(reviews, closures)) < len(open_findings(before, closures))


def round_reasons(reviews, fixes=(), syncs=()):
    """Every round of ``reviews``, oldest first, as ``(round, reason, counts)`` with the reason
    in Italian (#289) and whether it counts against ``max_review_rounds`` (``counted_rounds``).
    A counted round is a CI failure, the review of a head an agent's conflict resolution
    produced (``syncs``, the ``sync`` markers), the review after a fix (``fixes``) of the round
    before, the first review, or a new review of the same or of another head. A waived round
    (``waived_rounds``) is a review after a resync or a revocation for the base, or a round the
    base interrupted and a later one replaced."""
    waived = waived_rounds(reviews)
    synced = {s.get('head_sha') for s in syncs} - {None}
    fixed = {f.get('round') for f in fixes}
    reasons, previous = [], None
    for number in sorted({r['round'] for r in reviews}):
        first = _round_records(reviews, number)[0]
        if number in waived:
            resync = first.get('resync')
            reason = resync.get('reason') if isinstance(resync, dict) else None
            if reason == RESYNC_AFTER_APPROVAL:
                why = 'review di un head approvato, spostato solo da una sync senza conflitti'
            elif reason == BASE_CHANGED:
                why = 'nuova review di un head approvato, dopo la revoca per la base'
            else:
                why = 'round interrotto dall\'avanzare della base e sostituito dal successivo'
                reasons.append((number, why, False))
                continue  # the round replacing it follows what came before it
            reasons.append((number, why, False))
        else:
            if first['source'] != 'agent':
                why = 'CI fallita'
            elif first.get('head_sha') in synced:
                why = 'review dopo una sync con conflitti risolti dall\'agent'
            elif (previous['round'] if previous else 0) in fixed:
                # A fix answers the round before (round 0: a requested fix before any review).
                why = 'review dopo una correzione'
            elif previous is None:
                why = 'prima review'
            elif previous.get('head_sha') == first.get('head_sha'):
                why = 'nuova review dello stesso head'
            else:
                why = 'review di un nuovo head'
            records = _round_records(reviews, number)
            if first['source'] == 'agent' and ci_in_round(records):
                why += ', con la CI fallita dello stesso head'  # one head, one round (#352)
                if voided_approval(records):
                    why += ', che ne ha fatto decadere l\'approvazione'  # #359
            reasons.append((number, why, True))
        previous = first
    return reasons


def _parents(path):
    return {path[:i] for i, c in enumerate(path) if c == '/'}


def touched_by_base(paths, touched):
    """The paths of a PR's diff that a change of the base branch, ``touched``, reaches (#160):
    the same path, a directory above it or a path under it (a file replaced by a directory, or
    the reverse, would conflict even with no file in common)."""
    touched = set(touched)
    dirs = set().union(*map(_parents, touched)) if touched else set()
    return sorted({p for p in paths if p in touched or p in dirs or _parents(p) & touched})
