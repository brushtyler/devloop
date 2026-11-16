"""The dispatcher: reads GitHub state, plans the next steps, runs them.

State lives on GitHub only (labels plus hidden markers in comments), so a tick can crash at any
point and the next one resumes.

Identities: with GitHub Apps configured (``apps``), the dispatcher acts with its own app and each
agent with its own, and every record is bound to the identity that may write it: a PR to its
implementer's app, a review to its reviewer's app, a CI verdict to the dispatcher's app. Without
an app an actor uses the ambient ``gh`` login, and its records must come from ``trusted_users``.

Issue  agent:ready ─► agent:working ─► PR opened
PR     agent:review ─► (CI fail | request_changes) ─► agent:changes ─► agent:review ─► …
                   └─► approve ─► agent:ready-to-merge   (a human merges)
       any step can escalate to agent:needs-human.

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
whose pool has not enough independent reviewers goes to agent:needs-human. A round is promoted
only if every required reviewer approved the head with the receipt recomputed just before,
otherwise a new review is needed.

Base advancing (#160): a receipt that differs only in the commit of base still holds, both to
join an open round and to promote, if no commit of the base branch in between touched a file of
the PR's diff (``_base_check``). An open round abandoned only because the base touched the PR
is replaced by a new one and does not count against ``max_review_rounds`` (``counted_rounds``).

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
not ready goes to agent:needs-human; the check removes that label only if it added it itself.

Start order (#146): ready issues start by priority (``priority:high``, ``normal``, ``low``; a
``priority_labels`` label counts as high), then by the oldest current ``agent:ready``, then by
number. A priority label counts only if a trusted user added it.
"""

import hashlib
import itertools
import json
import string
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from .acceptance import (acceptance_criteria, acceptance_findings, format_criteria, format_map,
                         latest_map, render_map)
from .agents import AgentError, ModelsUnavailable, ModelUnavailable
from .apps import DISPATCHER, IMPLEMENT, ISSUE_COMMENT, PR_COMMENT, REVIEW
from .github import GitHubError, merge_state, summarize_checks
from .ledger import entries as ledger_entries
from .ledger import open_turns
from .localtests import module_command, select_modules
from .markdown import (RESUME, carries_record, dispatcher_header, publishable, read_marker,
                       render_escalation, render_fix, render_native_review, render_preflight, render_review)
from .policy import describe as describe_receipt
from .policy import (BINDING, MAX_LISTED_PATHS, ZERO_LEVEL, agent_profile, changes_requested,
                     check_outcome, check_states, differences, distinct, evaluate, independent,
                     profile_of, receipt_of, recorded_level, zero_refused)
from .review import (CI, FINDING_COMMAND, OPERATOR, ci_failure_review, current_round,
                     finding_commands, finding_key, finding_state, last_round, normalize_review,
                     open_findings, owner_of, round_verdict)
from .workspace import WorkspaceError

PROMPT_DIR = Path(__file__).resolve().parent / 'prompts'
REREVIEW_COMMAND = '/devloop rereview'
RECHECK_COMMAND = '/devloop recheck'
EXPENSIVE = ('review', 'fix', 'implement', 'sync', 'preflight')
LABEL_STYLE = {
    'ready': ('0e8a16', 'devloop: pronta per essere implementata da un agent'),
    'working': ('fbca04', 'devloop: implementazione in corso'),
    'review': ('1d76db', 'devloop: in attesa di review automatica'),
    'changes': ('d93f0b', 'devloop: modifiche richieste dalla review'),
    'ready_to_merge': ('0e8a16', 'devloop: approvata, in attesa del merge umano'),
    'needs_human': ('b60205', 'devloop: serve una decisione umana'),
}
LEVEL_LABEL_STYLE = ('5319e7', 'devloop: la PR richiede una review di livello 2')
# Priority labels of ready issues (#146) and their rank: the lower, the earlier an issue starts.
PRIORITY_LABELS = {'priority:high': 'high', 'priority:normal': 'normal', 'priority:low': 'low'}
PRIORITY_RANK = {'high': 0, 'normal': 1, 'low': 2}
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
    merge_state: str = 'clean'  # clean, conflict, unknown (see github.merge_state)
    # Policy records the dispatcher wrote (``policy`` markers), and whether one written after
    # the latest review found that the approval's receipt no longer matches.
    policy: list = field(default_factory=list)
    stale_receipt: bool = False
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


@dataclass
class Snapshot:
    prs: list
    # dicts: number, title, labels (set), labeler and ready_at (login and time of the latest
    # ``labeled`` event of agent:ready), priority (``issue_priority``), and with ``preflight``
    # configured, for the issues that may start or are blocked by the check, ``preflight``
    # (``_preflight_view``)
    ready_issues: list
    working_issues: list  # issue numbers


@dataclass(frozen=True)
class Action:
    kind: str
    number: int
    reason: str = ''


def label_names(labels):
    return {label['name'] if isinstance(label, dict) else label for label in labels}


def issue_priority(labels, events, trusted_users, priority_labels=()):
    """The priority of a ready issue (#146): the highest among the priority labels it has now
    whose latest ``labeled`` event comes from a trusted user, ``normal`` without any. Labels in
    ``priority_labels`` count as ``high``. ``events`` are the issue's ``labeled`` events, oldest
    first, as ``{label, by, at}``."""
    names = dict(PRIORITY_LABELS, **{name: 'high' for name in priority_labels})
    added_by = {event['label']: event['by'] for event in events}
    valid = [names[name] for name in labels
             if name in names and added_by.get(name) in trusted_users]
    return min(valid, key=PRIORITY_RANK.__getitem__, default='normal')


def start_order(issue):
    """Sort key of the ready issues (#146): priority, the oldest current agent:ready, number."""
    return (PRIORITY_RANK[issue.get('priority', 'normal')], issue.get('ready_at') or '',
            issue['number'])


def queue_lines(snapshot):
    """The ready issues in start order, with priority and agent:ready time, as ``status`` and
    ``tick --dry-run`` show them. Which of them start, and when, is up to ``plan``."""
    return [f'ready {position}. #{issue["number"]} priority={issue.get("priority", "normal")} '
            f'agent:ready={issue.get("ready_at") or "?"} labelled by {issue["labeler"]}: '
            f'{issue["title"]}'
            for position, issue in enumerate(sorted(snapshot.ready_issues, key=start_order), 1)]


def zero_labels_hold(pr, rp):
    """Whether a level-0 promotion of ``pr`` still holds as far as its head and labels show:
    the promoted head is the current one, level 0 is enabled, its label is there and the level-2
    label is not."""
    return (pr.zero_head == pr.head_sha and rp.zero_enabled()
            and rp.zero_label in pr.labels and rp.label not in pr.labels)


def plan(snapshot, cfg):
    """Pure planning: the actions one tick should take, in order."""
    lb = cfg.labels
    actions = []
    pr_issues = {pr.issue for pr in snapshot.prs}
    for number in snapshot.working_issues:
        if number not in pr_issues:
            actions.append(Action('stale', number, 'agent:working without an open devloop PR'))
        else:
            actions.append(Action('settle', number, 'agent:working but its PR is open'))
    for pr in snapshot.prs:
        if lb.needs_human in pr.labels:
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
            elif pr.checks == 'fail':
                actions.append(Action('ci_failed', pr.number, 'CI failed after approval')
                               if rounds_left else Action('round_limit', pr.number))
            elif pr.checks == 'pending':
                # CI re-running on the approved commit: not ready until it passes again. The
                # recorded approval is replayed from agent:review, without a new review.
                actions.append(Action('revoke', pr.number, 'CI pending on the approved commit'))
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
            if replay and verdict != 'approve':
                actions.append(Action('apply_review', pr.number, 'verdict already recorded'))
            elif pr.merge_state == 'conflict':
                # Neither reviewed nor promoted: the merge would change the head anyway.
                actions.append(Action('sync', pr.number, 'conflicts with the base branch'))
            elif pr.merge_state == 'unknown':
                continue  # GitHub is still computing mergeability: next tick
            elif pr.checks == 'pending':
                continue
            elif replay and not unbound and not unmapped and pr.checks != 'fail':
                actions.append(Action('apply_review', pr.number, 'approval already recorded'))
            elif partial and pr.checks != 'fail':
                actions.append(Action('review', pr.number, 'the round waits for '
                                      + ', '.join(pending)))
            elif not rounds_left:
                # Every new round, a CI failure or a review, counts against the limit.
                actions.append(Action('round_limit', pr.number))
            elif pr.checks == 'fail':
                actions.append(Action('ci_failed', pr.number))
            else:
                reason = ('new review requested by a trusted user' if pr.rereview
                          else 'the approval has no matching policy receipt' if unbound
                          else 'the acceptance map does not hold' if unmapped
                          else 'every finding of the request for changes is closed' if settled
                          else '')
                actions.append(Action('review', pr.number, reason))
    free = cfg.max_open_prs - len(snapshot.prs)
    if cfg.preflight:
        actions += _plan_preflight(snapshot, cfg, pr_issues, free)
    elif free > 0:
        blocked = set(cfg.blocked_labels) | {lb.working, lb.needs_human}
        candidates = [i for i in snapshot.ready_issues
                      if not (i['labels'] & blocked) and i['number'] not in pr_issues
                      and i['labeler'] in cfg.trusted_users]
        candidates.sort(key=start_order)
        actions += [Action('implement', i['number']) for i in candidates[:free]]
    capped, spent = [], 0
    for action in actions:
        if action.kind in EXPENSIVE:
            if spent >= cfg.max_actions_per_tick:
                continue
            spent += 1
        capped.append(action)
    return capped


def _plan_preflight(snapshot, cfg, pr_issues, free):
    """New work with the readiness check (#114). Issues start in the usual order, up to
    ``free``: one whose check for its current text passed is implemented, one without a valid
    check is checked first (implemented on a later tick if ready). A failed check that is still
    valid blocks the issue again if its ``agent:needs-human`` was removed. An issue the check
    blocked is checked again, whatever ``free``, when its text changed or a trusted user asked
    for it; any other ``agent:needs-human`` is left alone."""
    lb = cfg.labels
    excluded = set(cfg.blocked_labels) | {lb.working}
    issues = [i for i in snapshot.ready_issues
              if not (i['labels'] & excluded) and i['number'] not in pr_issues
              and i['labeler'] in cfg.trusted_users]
    issues.sort(key=start_order)
    actions, taken = [], 0
    for issue in issues:
        view = issue.get('preflight')
        if view is None:
            continue  # not read by the snapshot: nothing is known about its check
        state, reason = preflight_state(view)
        if lb.needs_human in issue['labels']:
            if view['owned'] and state == 'run':
                actions.append(Action('preflight', issue['number'], reason))
        elif state == 'not_ready':
            actions.append(Action('preflight_block', issue['number'],
                                  'the readiness check of the current text failed'))
        elif taken < free:
            taken += 1
            actions.append(Action('implement', issue['number']) if state == 'ready'
                           else Action('preflight', issue['number'], reason))
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


def preflight_hash(title, body, comments, trusted_users, automatic):
    """The hash that binds a readiness check to the issue text: title, body and the comments of
    trusted users, without devloop's own records (``automatic(comment)``) and without
    ``/devloop`` commands. Any other trusted comment counts, header or not."""
    kept = [[(c.get('author') or {}).get('login'), c.get('body') or ''] for c in comments
            if (c.get('author') or {}).get('login') in trusted_users
            and not automatic(c) and not control_command(c.get('body'))]
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
    """The rounds that do not count against ``max_review_rounds`` (#160): each incomplete agent
    round that a later round replaced only because the base branch advanced, as the later
    round's records say (``restarts``)."""
    waived = set()
    for record in reviews:
        restart = record.get('restarts')
        number = restart.get('round') if isinstance(restart, dict) else None
        if (isinstance(number, int) and not isinstance(number, bool) and number < record['round']
                and pending_reviewers(_round_records(reviews, number))):
            waived.add(number)
    return waived


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


class Models:
    """The models one action may still run ``agent`` with (``AgentConfig.chain``), in order, and
    those it tried or left out, as ``(model, why)`` pairs (#121)."""

    def __init__(self, agent):
        self.agent = agent
        self.left = agent.chain()
        self.tried = []


class UntrustedChange(RuntimeError):
    """The issue text changed after a trusted user approved it with the ready label."""


def untrusted_change(history, ready_label, trusted_users):
    """Why the issue's title or body can no longer be trusted, or None.

    Trust comes from a trusted user adding ``ready_label``; a later change to the title or the
    body by anyone else voids it until a trusted user adds the label again. GitHub timestamps
    have one-second granularity, so an untrusted change in the same second as the label cannot
    be proven to precede it and voids trust too.
    """
    approved = [at for name, who, at in history['labeled']
                if name == ready_label and who in trusted_users]
    if not approved:
        return f'nessuna label `{ready_label}` messa da un utente fidato'
    since = approved[-1]
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


def _format_findings(findings):
    lines = []
    for f in findings:
        where = f' ({f["file"]}{":" + str(f["line"]) if f["line"] > 0 else ""})' if f['file'] else ''
        lines.append(f'- **{f.get("key") or f["id"]}**{where}: {f["title"]}\n  {f["detail"]}'
                     + (f'\n  Suggested fix: {f["suggested_fix"]}' if f['suggested_fix'] else ''))
    return '\n'.join(lines) or '(none)'


def format_previous_rounds(reviews, fixes, closures=()):
    if not reviews:
        return 'This is the first round.'
    parts = []
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
            if fix['round'] == review['round']:
                parts.append('Implementer responses:\n' + '\n'.join(
                    f'- {r["finding_id"]}: {r["action"]}: {r["note"]}' for r in fix['responses']))
    if closures:
        parts.append('### Findings closed by CI or by the operator\n\n' + '\n'.join(
            f'- {c["key"]}: {c["status"]} by '
            + ('CI' if c['by'] == CI else f'the operator ({c["author"]})') + f': {c["reason"]}'
            for c in closures))
    return '\n\n'.join(parts)


class Dispatcher:
    """``gh`` acts as the dispatcher. ``identities`` (``apps.Identities``) gives each agent its
    own GitHub identity; without it every actor uses ``gh``. ``activity``
    (``activity.Activity``) records the running action for ``devloop status``. ``ledger``
    (``ledger.Ledger``) records evidence and telemetry of each step (#133)."""

    def __init__(self, cfg, gh, ws, runner, log=print, identities=None, activity=None,
                 ledger=None):
        self.cfg = cfg
        self.gh = gh
        self.ws = ws
        self.runner = runner
        self.log = log
        self.identities = identities
        self.activity = activity
        self.ledger = ledger

    # Identities ------------------------------------------------------------
    def _as(self, actor, permissions):
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
        dispatcher, reviews from the agent named in the record, never the implementer."""
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
            elif kind in ('policy', 'finding'):
                actor = DISPATCHER
            elif record['source'] == 'ci':
                actor = DISPATCHER
            elif not isinstance(record.get('reviewer'), str) or not record['reviewer']:
                if problems is not None and self._writes_records(login):
                    problems.append(f'la review del round {record["round"]} non registra il '
                                    'reviewer')
                continue
            elif record['reviewer'] in (implementer, CI):
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
                           merge_state=merge_state(raw.get('mergeable'),
                                                   raw.get('mergeStateStatus')))

    def snapshot(self):
        lb = self.cfg.labels
        prs = []
        for raw in self.gh.open_prs():
            pr = self._devloop_pr(raw)
            if pr is None:
                continue
            if pr.meta and pr.labels & {lb.review, lb.changes, lb.ready_to_merge}:
                comments = self.gh.pr_comments(pr.number)
                problems = []
                entries = sorted(self._marker_entries(pr, comments, 'review', problems),
                                 key=lambda e: (e[1]['round'], e[0]))
                pr.reviews = [record for _, record in entries]
                pr.fixes = self._markers(pr, comments, 'fix')
                pr.syncs = self._markers(pr, comments, 'sync')
                pr.closures = [c for record in self._markers(pr, comments, 'finding')
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
                    pr.stale_receipt = any(position > last and record['event'] == 'stale'
                                           for position, record in policy)
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
            if (lb.ready_to_merge in pr.labels and pr.zero_head is not None
                    and zero_labels_hold(pr, self.cfg.review_policy)):
                pr.zero_problem = self._zero_problem(pr)
            if pr.meta and pr.labels & {lb.review, lb.ready_to_merge} and pr.zero_head is None:
                pr.acceptance = self._acceptance_problems(pr)
            prs.append(pr)
        ready = []
        pr_issues = {pr.issue for pr in prs}
        skip = set(self.cfg.blocked_labels) | {lb.working}
        for raw in self.gh.issues_with_label(lb.ready):
            labels = label_names(raw['labels'])
            events = self.gh.labeled_events(raw['number'])
            # The latest agent:ready: its author admits the issue and its time is the issue's
            # seniority in the queue (#146).
            admitted = ([e for e in events if e['label'] == lb.ready] or [{}])[-1]
            issue = {'number': raw['number'], 'title': raw['title'], 'labels': labels,
                     'labeler': admitted.get('by'), 'ready_at': admitted.get('at'),
                     'priority': issue_priority(labels, events, self.cfg.trusted_users,
                                                self.cfg.priority_labels)}
            # The readiness check of the issues that may start or are blocked by it (#114).
            if (self.cfg.preflight and issue['labeler'] in self.cfg.trusted_users
                    and not issue['labels'] & skip and issue['number'] not in pr_issues):
                issue['preflight'] = self._preflight_view(issue['number'], issue['labels'])
            ready.append(issue)
        working = [raw['number'] for raw in self.gh.issues_with_label(lb.working)]
        return Snapshot(prs=prs, ready_issues=ready, working_issues=working)

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
        """Whether the issue's ``agent:needs-human`` belongs to the readiness check: the latest
        check added it when the label was absent (its record says how many ``labeled`` events
        of the label came before), the dispatcher wrote the only ``labeled`` event since, and
        no other text devloop may have published (another escalation, ``_published``) came
        after that check: an identity that writes no devloop record cannot void the block's
        attribution. Anything else, or anything uncertain, leaves the block to a human."""
        if not records:
            return False
        position, record = records[-1]
        # The dispatcher's ledger records (#133), such as the turn of a recheck, are no
        # escalation: they leave the block's attribution as it is.
        if not record['blocked'] or any(self._published(c) and not self._ledger_record(c)
                                        for c in comments[position + 1:]):
            return False
        labelers = self.gh.labelers(number, self.cfg.labels.needs_human)
        return (len(labelers) == record['labeled'] + 1
                and self._wrote(labelers[-1], DISPATCHER))

    def _preflight_view(self, number, labels):
        """What planning needs about an issue's readiness check, read now from GitHub:
        ``{hash, record, recheck, owned}``, ``record`` being the latest check or None."""
        history = self.gh.issue_history(number)
        comments = self.gh.issue_comments(number)
        records = self._preflight_records(comments)
        return {'hash': preflight_hash(history['title'], history['body'], comments,
                                       self.cfg.trusted_users, self._automatic),
                'record': records[-1][1] if records else None,
                'recheck': self._recheck_requested(number, comments),
                'owned': (self.cfg.labels.needs_human in labels
                          and self._owns_block(number, comments, records))}

    def _publish_preflight(self, number, outcome, digest, agent_key, note=''):
        """Record a check's outcome on the issue and apply it to ``agent:needs-human``: add the
        label when the check fails and the label is absent, remove it when the check passes and
        the label belongs to the readiness check. A label someone else set stays."""
        lb = self.cfg.labels
        agent = self.cfg.agent(agent_key)
        labels = label_names(self.gh.issue(number).get('labels') or [])
        comments = self.gh.issue_comments(number)
        records = self._preflight_records(comments)
        present = lb.needs_human in labels
        owned = present and self._owns_block(number, comments, records)
        record = dict(outcome, hash=digest, agent=agent_key, blocked=False, labeled=0)
        author = self._as(agent_key, ISSUE_COMMENT)
        head = self.runner.header(agent)
        if outcome['ready']:
            if owned:
                # Label first: if the comment is lost, the next tick checks the issue again.
                self.gh.edit_labels(number, remove=[lb.needs_human])
                note = note or 'Il blocco messo dal controllo di prontezza è stato rimosso.'
            elif present:
                note = ('`agent:needs-human` resta: non risulta messo dal controllo di prontezza, '
                        'quindi lo rimuove una persona.')
            author.comment(number, render_preflight(record, head, note))
            self.log('  ready' + (', block removed' if owned else ''))
            return
        if owned:
            record.update(blocked=True, labeled=records[-1][1]['labeled'])
        elif present:
            note = note or ('`agent:needs-human` era già presente e non è attribuito al controllo '
                            'di prontezza: un nuovo esito positivo non lo rimuove.')
        else:
            record.update(blocked=True,
                          labeled=len(self.gh.labelers(number, lb.needs_human)))
        # Comment first: if the label is lost, the next tick blocks the issue again.
        author.comment(number, render_preflight(record, head, note))
        if record['blocked'] and not owned:
            self.gh.edit_labels(number, add=[lb.needs_human])
        self.log(f'  not ready: {len(record["gaps"])} gap(s)')

    def _do_preflight(self, number):
        """Run the readiness check on a checkout of the base branch, with the context the
        implementer gets, then record and apply its outcome."""
        cfg = self.cfg
        agent = cfg.agent(cfg.preflight)
        history = self.gh.issue_history(number)
        reason = untrusted_change(history, cfg.labels.ready, cfg.trusted_users)
        if reason:
            raise UntrustedChange(f'issue #{number}: {reason}')
        comments = self.gh.issue_comments(number)
        digest = preflight_hash(history['title'], history['body'], comments, cfg.trusted_users,
                                self._automatic)
        prompt = _template('preflight', repo=cfg.repo, base_branch=cfg.base_branch,
                           issue_number=number, issue_title=history['title'],
                           issue_body=history['body'],
                           issue_comments=_render_issue_comments(comments, cfg.trusted_users),
                           language=cfg.language)
        name = f'issue-{number}-preflight'
        path = self.ws.base_copy(name)
        try:
            entry = self._open_turn((number, 'preflight'), agent, 'review', name)
            try:
                raw = self.runner.run(agent, 'review', prompt, 'preflight', path, name)
            except AgentError as exc:
                self._close_turn(entry, 'unavailable' if isinstance(exc, ModelUnavailable)
                                 else 'failure', exc)
                raise
            self._close_turn(entry, 'success')
        finally:
            self.ws.remove(name)
        self._publish_preflight(number, normalize_preflight(raw), digest, agent.key)

    def _do_preflight_block(self, number):
        """Block again an issue whose latest check, still valid for its text, failed, after its
        ``agent:needs-human`` was removed: the outcome is reused, not recomputed."""
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
        snap = self.snapshot()
        self._flag_incomplete(snap, dry_run)
        actions = plan(snap, self.cfg)
        prs = {pr.number: pr for pr in snap.prs}
        if dry_run:
            for line in queue_lines(snap):
                self.log(f'[dry-run] {line}')
        for action in actions:
            self.log(f'{"[dry-run] " if dry_run else ""}{action.kind} #{action.number}'
                     + (f': {action.reason}' if action.reason else ''))
            if dry_run:
                continue
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
        return actions

    # Helpers -------------------------------------------------------------
    def _escalate(self, number, reason, detail='', remove=()):
        self.gh.comment(number, render_escalation(reason, detail))
        self.gh.edit_labels(number, add=[self.cfg.labels.needs_human], remove=remove)

    def _log_hint(self, exc):
        log = getattr(exc, 'log', None)
        return f' Log locale: `{Path(log).name}` in `{self.cfg.state_dir}/logs`.' if log else ''

    def _escalate_failure(self, action, pr, exc):
        lb = self.cfg.labels
        reason = f'Il passo `{action.kind}` è fallito: {_failure_text(exc)}{self._log_hint(exc)}'
        try:
            if pr is not None:
                self._escalate(pr.number, reason,
                               remove=[lb.review, lb.changes, lb.ready_to_merge])
            else:
                self._escalate(action.number, reason, remove=[lb.working, lb.ready])
        except GitHubError as gh_exc:
            self.log(f'  could not escalate: {gh_exc}')

    def _issue_context(self, number):
        """The issue as model input. Title and body come from the same read as their edit
        history, and are refused if someone outside ``trusted_users`` changed them after the
        ready label."""
        history = self.gh.issue_history(number)
        reason = untrusted_change(history, self.cfg.labels.ready, self.cfg.trusted_users)
        if reason:
            raise UntrustedChange(f'issue #{number}: {reason}')
        issue = dict(self.gh.issue(number), title=history['title'], body=history['body'],
                     labeled=history['labeled'])
        return issue, _render_issue_comments(issue.get('comments') or [], self.cfg.trusted_users)

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
        """Close a turn's record with what the CLI attested. A failed write leaves the turn
        open, and the next tick marks it incomplete."""
        if entry is None:
            return
        try:
            self.ledger.close_turn(entry, outcome, getattr(self.runner, 'last_report', None),
                                   error)
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

    def _flag_incomplete(self, snap, dry_run):
        """Report the turns a previous tick opened and never closed, and mark them incomplete:
        no turn runs while a tick starts, since one dispatcher runs at a time.

        It looks first where the ledger's trace (``Ledger.pending``) says a turn was opened and
        not closed, whatever labels the issue or PR has now: an issue whose PR is open or that
        was escalated, a PR in ``agent:needs-human``. Then, in case that trace was lost, open
        devloop PRs not in ``agent:needs-human``, issues in ``agent:working`` and issues
        waiting for a readiness check. A number leaves the trace once checked."""
        if self.ledger is None:
            return
        lb = self.cfg.labels
        try:
            traced = self.ledger.pending()
        except GitHubError as exc:
            self.log(f'  trace of the open turns not readable: {exc}')
            traced = []
        numbers = list(traced)
        numbers += [pr.number for pr in snap.prs if lb.needs_human not in pr.labels]
        numbers += list(snap.working_issues)
        numbers += [i['number'] for i in snap.ready_issues if 'preflight' in i]
        for number in dict.fromkeys(numbers):
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

        ``where``, ``(number, kind)``, is the issue or PR whose ledger records each CLI
        invocation as a turn (#133): written before the CLI starts, closed when it ends."""
        failed = None
        while models.left:
            candidate = models.left.pop(0)
            if allowed is not None and not allowed(candidate):
                models.tried.append((candidate.model, 'not independent of the authors of the PR '
                                                      'or of the other reviewers of the round'))
                self.log(f'  {candidate.key}: model {candidate.model} skipped: not independent')
                continue
            text = prompt + FALLBACK_NOTE[mode] if failed else prompt
            suffix = f'-{candidate.model}' if candidate.fallback_for else ''
            entry = self._open_turn(where, candidate, mode, label + suffix)
            try:
                output = self.runner.run(candidate, mode, text, schema, path, label + suffix)
            except ModelUnavailable as exc:
                self._close_turn(entry, 'unavailable', exc)
                models.tried.append((candidate.model, f'{exc} ({exc.reason})'))
                self.log(f'  {candidate.key}: model {candidate.model} unavailable: {exc.reason}')
                failed = exc
                continue
            except AgentError as exc:
                self._close_turn(entry, 'failure', exc)
                if not models.tried:
                    raise
                # Any other failure ends the turn as before, but after models were tried or left
                # out the escalation lists them all, this one included.
                models.tried.append((candidate.model, str(exc)))
                raise ModelsUnavailable(models.agent.key, models.tried, exc.log,
                                        last_failed=True) from exc
            self._close_turn(entry, 'success')
            models.left.insert(0, candidate)  # available: later attempts of the action use it
            if candidate.fallback_for:
                self.log(f'  {candidate.key} ran on fallback model {candidate.model} '
                         f'({candidate.model_label}) instead of {candidate.fallback_for}')
            return output, candidate
        if failed is not None and not models.agent.fallback_models:
            raise failed  # an agent without fallback models: as before #121
        raise ModelsUnavailable(models.agent.key, models.tried, getattr(failed, 'log', None))

    def _write_turn(self, agent, prompt, schema, path, label, where=None):
        """Run a writing agent, then the local tests (``_verify``); retry with the failures.
        Returns the output, ``None`` on success or a reason to escalate, the agent configuration
        of the last turn (``_turn``), the ledger record of the passing test, to bind to the
        commit that follows (None without a ledger or without a passing test), and the mode and
        modules of the local tests of the last attempt (None when no test was due: a blocked
        turn, or an implementation without changes). A model found
        unavailable is not tried again in later attempts."""
        models = Models(agent)
        for attempt in range(1 + self.cfg.implement_test_retries):
            result, used = self._turn(models, 'write', prompt, schema, path,
                                      f'{label}-{attempt + 1}', where=where)
            if result['status'] == 'blocked':
                return result, None, used, None, None
            if not self.ws.has_changes(path) and schema == 'implement':
                return result, ('Implementazione senza modifiche ai file.', ''), used, None, None
            passed, tail, tested, local = self._verify(where, path, attempt + 1)
            if passed:
                return result, None, used, tested, local
            prompt += ('\n\n## Test failures after your previous attempt\n\nThe test command failed. '
                       f'Fix the cause, then finish again.\n\n```text\n{tail}\n```\n')
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
                                           'notice', {'event': 'blocked'}))
        self.gh.edit_labels(number, add=[self.cfg.labels.needs_human], remove=remove)

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
                               '\n\n' + listed, remove=remove)

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

    def _policy_comment(self, pr, title, text, receipt, event, bases=()):
        """A dispatcher ``policy`` record; ``bases``: the base checks it relied on (#160)."""
        payload = {'event': event, 'receipt': receipt}
        if bases:
            payload['bases'] = list(bases)
        self.gh.comment(pr.number, publishable(
            dispatcher_header() + f'\n## {title}\n\n{text.strip()}\n\n'
            + describe_receipt(receipt), 'policy', payload))

    def _escalate_level(self, pr, receipt):
        """Not enough independent reviewers for the level: no review, no promotion, a human
        decides."""
        lb, level = self.cfg.labels, receipt['level']
        needed = 'un reviewer indipendente' if level == 1 else f'{level} reviewer indipendenti'
        self._policy_comment(
            pr, f'Review di livello {level}: serve un intervento umano',
            f'La policy di review del dispatcher richiede per questa PR il livello {level}, '
            f'quindi {needed}, ma {self._select(pr, level)[1]}. devloop non revisiona né '
            'promuove la PR: la review e la decisione sul merge spettano a una persona. Il '
            'livello resta acquisito anche se la label viene rimossa o il diff cambia.',
            receipt, 'level')
        self.gh.edit_labels(pr.number, add=[lb.needs_human],
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
            self._policy_comment(
                pr, 'Approvazione non valida per la promozione',
                f'La ricevuta di policy ricalcolata prima della promozione non coincide con '
                f'quella dell\'approvazione ({what}). La PR resta in review: serve una nuova '
                'review, anche dello stesso commit. Round e finding aperti restano invariati.'
                + detail, current, 'stale', bases)
            self.gh.edit_labels(pr.number, add=[lb.review], remove=[lb.changes, lb.ready_to_merge])
            return
        by = ', '.join(f'`{key}`' for key in current['reviewers'])
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
        reason = untrusted_change(history, self.cfg.labels.ready, self.cfg.trusted_users)
        if reason:
            return [f'testo dell\'issue non verificabile: {reason}']
        found = acceptance_findings(acceptance_criteria(history['body']),
                                    latest_map(pr.meta, pr.fixes))
        return [f['title'] for f in found]

    def _refuse_zero(self, pr, receipt, why, states=()):
        """Level 0 holds but the head cannot be promoted: a human decides."""
        lb = self.cfg.labels
        self.gh.comment(pr.number, publishable(
            dispatcher_header() + '\n## Livello 0: serve un intervento umano\n\n'
            f'La policy di review assegna a questa PR il livello 0, ma {why.strip()}.\n\n'
            'devloop non promuove la PR. Per riprendere: risolvere la causa, togliere '
            f'`{lb.needs_human}` e rimettere `{lb.review}` (il livello 0 viene ricontrollato), '
            f'oppure togliere anche la label `{self.cfg.review_policy.zero_label}` per una review '
            'di livello 1.\n\n' + describe_receipt(receipt),
            'policy', {'event': 'level', 'receipt': receipt, 'checks': list(states)}))
        self.gh.edit_labels(pr.number, add=[lb.needs_human],
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
                self._escalate(pr.number, _open_findings_reason(left), remove=transient)
            elif self._approval_current(pr, records[0]):
                self._promote(pr, records)
        elif verdict == 'needs_human':
            self.gh.edit_labels(pr.number, add=[lb.needs_human], remove=transient)
        elif counted_rounds(reviews) >= self.cfg.max_review_rounds:
            if announce:
                self.gh.comment(pr.number, render_escalation(
                    f'Raggiunto il limite di {self.cfg.max_review_rounds} round di review con '
                    'finding bloccanti ancora aperti.'))
            self.gh.edit_labels(pr.number, add=[lb.needs_human], remove=transient)
        else:
            self.gh.edit_labels(pr.number, add=[lb.changes], remove=[lb.review, lb.ready_to_merge])

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
        self.gh.comment(pr.number,
                        publishable(dispatcher_header() + f'\n## {title}\n\n' + reason + '.'))
        self.gh.edit_labels(pr.number, add=[lb.review], remove=[lb.ready_to_merge])

    def _do_stale(self, number):
        lb = self.cfg.labels
        self._escalate(number, 'Implementazione interrotta: la issue era in lavorazione ma non '
                               'esiste una PR devloop aperta.', remove=[lb.working])

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
                       remove=[lb.review, lb.changes, lb.ready_to_merge])

    def _do_lost_meta(self, pr):
        lb = self.cfg.labels
        self._escalate(pr.number, 'La descrizione della PR non termina più con il marker devloop '
                                  f'della issue #{pr.issue}, quindi implementer e reviewer non sono '
                                  'noti. Ripristinare la riga del marker (si trova nella cronologia '
                                  'delle modifiche della descrizione) come ultima riga.',
                       remove=[lb.review, lb.changes, lb.ready_to_merge])

    def _do_round_limit(self, pr):
        lb = self.cfg.labels
        self._escalate(pr.number, f'Raggiunto il limite di {self.cfg.max_review_rounds} round di '
                                  'review: un nuovo round (review o CI fallita) non parte senza una '
                                  'decisione umana.',
                       remove=[lb.review, lb.changes, lb.ready_to_merge])

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
            labeled = [(who, at) for name, who, at in issue['labeled']
                       if name == lb.ready and who in cfg.trusted_users]
            self.ledger.request(number, title=issue['title'], body=issue.get('body') or '',
                                labeled=labeled[-1] if labeled else None,
                                comments=_trusted_comments(issue.get('comments') or [],
                                                           cfg.trusted_users))
        self.gh.edit_labels(number, add=[lb.working], remove=[lb.ready])
        name, branch = f'issue-{number}', f'{cfg.branch_prefix}{number}'
        path = self.ws.new_branch(name, branch)
        criteria = acceptance_criteria(issue.get('body'))
        prompt = _template('implement', repo=cfg.repo, base_branch=cfg.base_branch,
                           issue_number=number, issue_title=issue['title'],
                           issue_body=issue.get('body') or '', issue_comments=comments,
                           acceptance_criteria=format_criteria(criteria),
                           test_cmd=cfg.test_cmd, language=cfg.language)
        result, failure, used, tested, local = self._write_turn(
            agent, prompt, 'implement', path, f'issue-{number}-implement',
            where=(number, 'implement'))
        if result['status'] == 'blocked':
            return self._blocked(number, used, result['blocked_reason'], remove=[lb.working],
                                 on_pr=False)
        if failure:
            return self._escalate(number, failure[0], failure[1], remove=[lb.working])
        auth = self._git_auth(agent.key, IMPLEMENT)
        commit = self.ws.commit_all(
            path, self._commit_message(agent, result['commit_message'], number), author=auth)
        self._bind_tested(tested, path, commit)
        protected = self._protected_changes(path)
        if protected:
            return self._escalate_protected(number, protected, remove=[lb.working])
        self.ws.push(path, branch, replace=True, auth=auth)
        # The marker keeps the map the reviewer checks until a fix replaces it (#134).
        meta = {'issue': number, 'implementer': agent.key, 'reviewer': cfg.reviewer,
                'profile': self._profile(agent.key, 'write', used),
                'acceptance_map': result['acceptance_map'],
                # Informative only (#169): no receipt nor approval depends on it.
                'local_tests': local}
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
        receipt, _ = self._current_receipt(pr)
        if receipt is None:
            return
        if receipt['level'] > 1 and len(receipt['reviewers']) < receipt['level']:
            return self._escalate_level(pr, receipt)
        log_tail = '\n'.join(self.gh.failed_log(run) for run in pr.failed_runs[:2])
        review = ci_failure_review(failed_checks=pr.failed_checks, log_tail=log_tail,
                                   previous_open=open_findings(pr.reviews, pr.closures),
                                   round_no=last_round(pr.reviews) + 1,
                                   max_rounds=self._last_round_allowed(pr.reviews),
                                   head_sha=pr.head_sha)
        review['receipt'] = receipt
        if self.ledger is not None:
            details = self.gh.check_details(pr.head_sha)
            self._record_ci(pr, pr.head_sha, summarize_checks(details)[0], details,
                            review['round'])
        self.gh.comment(pr.number, render_review(review, dispatcher_header()))
        self._apply_round(pr, [review], announce=True, reviews=pr.reviews + [review])

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

        When only the base changed since the round's reviews (#160), the round continues if no
        commit of the base branch in between reached the PR's files, and the review records the
        check (``bases``). Otherwise the round restarts, and the new one records which round it
        replaces (``restarts``): that round does not count against ``max_review_rounds``."""
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
        restarts = None
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
        if counted > cfg.max_review_rounds:
            return self._do_round_limit(pr)
        max_rounds = cfg.max_review_rounds + round_no - counted
        reviewed = {owner_of(r) for r in done}
        agent = cfg.agent(next(key for key in receipt['reviewers'] if key not in reviewed))
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
                           diff_stat=self.ws.diff_stat(path),
                           previous=format_previous_rounds(earlier, pr.fixes, pr.closures),
                           operator_comments=format_operator_comments(comments),
                           language=cfg.language)
        allowed = None
        if receipt['level'] > 1:
            # A fallback model must keep the independence the pair was selected for: of who
            # wrote the code, and of the round's other reviewers as their records show them.
            authors = self._authors(pr) or []
            peers = [p for p in map(profile_of, done) if p]

            def allowed(candidate):
                p = self._profile(agent.key, 'review', candidate)
                return (all(independent(p, a) for a in authors)
                        and all(distinct(p, q) for q in peers))
        # The reviewer works in a disposable copy, where its tests may write; the PR worktree
        # must come out of the review untouched, or the verdict is discarded.
        copy_name, copy = self.ws.review_copy(path)
        try:
            raw, used = self._turn(Models(agent), 'review', prompt, 'review', copy,
                                   f'pr-{pr.number}-review', allowed,
                                   where=(pr.number, 'review'))
        finally:
            self.ws.remove(copy_name)
        if not self.ws.pristine(path, pr.head_sha):
            raise WorkspaceError(f'the worktree of PR #{pr.number} changed during the review; '
                                 'verdict discarded')
        if receipt['level'] > 1 and self.gh.pr_head(pr.number) != pr.head_sha:
            # A verdict on a superseded head joins no round: the new head is reviewed afresh.
            self.log('  head moved during the review; verdict discarded')
            self.ws.remove(name)
            return
        review = normalize_review(raw, previous_open=previous_open, round_no=round_no,
                                  max_rounds=max_rounds, head_sha=pr.head_sha,
                                  ci=pr.checks, reviewer=agent.key, criteria=criteria,
                                  acceptance_map=acceptance_map)
        review['receipt'] = receipt
        if joins and bases:
            review['bases'] = bases  # the round's earlier bases, checked up to this one
        if restarts:
            review['restarts'] = restarts
        review['profile'] = self._profile(agent.key, 'review', used)
        reviewer_gh = self._as(agent.key, REVIEW)
        reviewer_gh.comment(pr.number, render_review(review, self.runner.header(used)))
        if self._app_login(agent.key):
            # A native review under the reviewer's own identity, visible to branch protection.
            # The marker comment above stays the record devloop reads.
            try:
                reviewer_gh.submit_review(pr.number, commit=pr.head_sha,
                                          **render_native_review(review, self.runner.header(used)))
            except GitHubError as exc:
                self.log(f'  native review not submitted: {exc}')
        if pr.checks != 'pending':
            # The CI the round reviewed, whatever its verdict (#133).
            self._record_ci(pr, pr.head_sha, pr.checks, pr.check_details, round_no)
        records = done + [review]
        waiting = pending_reviewers(records)
        if not waiting:
            self._apply_round(pr, records, announce=True, reviews=earlier + records)
        self.ws.remove(name)
        self.log(f'  verdict of {agent.key}: {review["verdict"]}'
                 + (f'; the round waits for {", ".join(waiting)}' if waiting else ''))

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
                       remove=[lb.review])
        return False

    def _do_fix(self, pr):
        cfg, lb = self.cfg, self.cfg.labels
        if not pr.reviews:
            return self._escalate(pr.number, 'Modifiche richieste ma nessuna review devloop da '
                                             'correggere.', remove=[lb.changes])
        # One fix answers the whole round, with every finding still open, whoever owns it.
        latest = current_round(pr.reviews)[0]
        findings = open_findings(pr.reviews, pr.closures)
        if not findings:
            return self._escalate(pr.number, 'Modifiche richieste ma nessun finding aperto.',
                                  remove=[lb.changes])
        if pr.meta['implementer'] not in cfg.agents:
            return self._escalate(
                pr.number, f'L\'implementer della PR (`{pr.meta["implementer"]}`) non è più '
                           'configurato: serve una decisione su chi corregge.',
                remove=[lb.changes])
        agent = cfg.agent(pr.meta['implementer'])
        issue, _ = self._issue_context(pr.issue)
        name = f'pr-{pr.number}'
        path = self.ws.existing_branch(name, pr.head_ref)
        if self.ws.head(path) != latest['head_sha']:
            self.log('  branch changed after the review; sending it back to review')
            self.gh.edit_labels(pr.number, add=[lb.review], remove=[lb.changes])
            return
        prompt = _template('fix', repo=cfg.repo, pr_number=pr.number, issue_number=pr.issue,
                           issue_title=issue['title'], issue_body=issue.get('body') or '',
                           findings=_format_findings(findings), test_cmd=cfg.test_cmd,
                           acceptance_criteria=format_criteria(
                               acceptance_criteria(issue.get('body'))),
                           acceptance_map=format_map(latest_map(pr.meta, pr.fixes)),
                           language=cfg.language)
        result, failure, used, tested, local = self._write_turn(
            agent, prompt, 'fix', path, f'pr-{pr.number}-fix-r{latest["round"]}',
            where=(pr.number, 'fix'))
        if result['status'] == 'blocked':
            return self._blocked(pr.number, used, result['blocked_reason'], remove=[lb.changes],
                                 on_pr=True)
        if failure:
            return self._escalate(pr.number, failure[0], failure[1], remove=[lb.changes])
        if self.ws.has_changes(path):
            auth = self._git_auth(agent.key, IMPLEMENT)
            commit = self.ws.commit_all(
                path, self._commit_message(agent, result['commit_message'], pr.issue), author=auth)
            self._bind_tested(tested, path, commit)
            protected = self._protected_changes(path, since=latest['head_sha'])
            if protected:
                return self._escalate_protected(pr.number, protected, remove=[lb.changes])
            self.ws.push(path, pr.head_ref, auth=auth)
        fix = dict(result, head_sha=self.ws.head(path),
                   profile=self._profile(agent.key, 'write', used), local_tests=local)
        self._as(agent.key, PR_COMMENT).comment(
            pr.number, render_fix(fix, self.runner.header(used), latest['round']))
        self.gh.edit_labels(pr.number, add=[lb.review], remove=[lb.changes])
        self.ws.remove(name)

    def _do_sync(self, pr):
        """Merge the base branch into a conflicting PR. The implementer resolves the conflicts
        in the PR worktree, the dispatcher concludes the merge, pushes it and sends the PR back
        to review. No review record is written, so no round is spent."""
        cfg, lb = self.cfg, self.cfg.labels
        remove = [lb.review, lb.ready_to_merge]
        base = f'origin/{cfg.base_branch}'
        if pr.meta['implementer'] not in cfg.agents:
            return self._escalate(
                pr.number, f'La PR è in conflitto con `{cfg.base_branch}` e il suo implementer '
                           f'(`{pr.meta["implementer"]}`) non è più configurato: serve una '
                           'decisione su chi risolve i conflitti.', remove=remove)
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

            def fail(reason, detail=''):
                self._escalate(pr.number, f'Sincronizzazione con `{cfg.base_branch}` non '
                                          f'riuscita. {reason}\n\nFile in conflitto:\n\n{listed}',
                               detail, remove=remove)
            prompt = _template('sync', repo=cfg.repo, pr_number=pr.number, issue_number=pr.issue,
                               issue_title=issue['title'], issue_body=issue.get('body') or '',
                               base_branch=cfg.base_branch, conflicts=listed,
                               test_cmd=cfg.test_cmd, language=cfg.language)
            try:
                result, failure, used, tested, _ = self._write_turn(
                    agent, prompt, 'sync', path, f'pr-{pr.number}-sync',
                    where=(pr.number, 'sync'))
            except AgentError as exc:
                return fail(f'Il passo `sync` è fallito: {_failure_text(exc)}'
                            f'{self._log_hint(exc)}')
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
        text = (f'\n## Sincronizzazione con `{cfg.base_branch}`\n\n'
                f'Merge di `{base}` (`{target[:12]}`) nel branch, nuovo head `{head[:12]}`. La PR '
                'torna in review sul nuovo head; la sincronizzazione non consuma un round di '
                'review.\n\n')
        if files:
            text += f'File in conflitto risolti:\n\n{listed}\n\n{result["summary"].strip()}'
            # Who resolved the conflicts wrote code on the PR: the marker keeps its profile,
            # so a later change of configuration cannot make it a reviewer of its own code.
            self._as(agent.key, PR_COMMENT).comment(
                pr.number, publishable(self.runner.header(used) + text, 'sync',
                                       {'head_sha': head,
                                        'profile': self._profile(agent.key, 'write', used)}))
        else:
            # A sync without an agent is an event, not a turn (#133).
            self._ledger_event(pr.number, 'sync', base=target, head=head, previous_head=pr.head_sha)
            self.gh.comment(pr.number, publishable(dispatcher_header() + text
                                                   + 'Il merge locale non aveva conflitti.'))
        self.gh.edit_labels(pr.number, add=[lb.review], remove=[lb.ready_to_merge, lb.changes])
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


def _trusted_comments(comments, trusted_users):
    """The comments of trusted users: the ones that reach the prompts."""
    return [c for c in comments if (c.get('author') or {}).get('login') in trusted_users]


def _render_issue_comments(comments, trusted_users):
    """The comments of trusted users as model input, or ''."""
    rendered = '\n\n'.join(f'### Comment by {c["author"]["login"]}\n\n{c["body"]}'
                           for c in _trusted_comments(comments, trusted_users))
    return ('## Issue comments\n\n' + rendered) if rendered else ''


def _failure_text(exc):
    """``exc`` as the end of a sentence: a one-line message gets its full stop, one that ends
    with a list (``ModelsUnavailable``) a blank line."""
    text = str(exc).rstrip()
    return text + ('\n\n' if '\n' in text else '.')


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


def ensure_labels(gh, labels, review_policy=None):
    for attr, (color, description) in LABEL_STYLE.items():
        gh.ensure_label(getattr(labels, attr), color, description)
    if review_policy is not None:
        gh.ensure_label(review_policy.label, *LEVEL_LABEL_STYLE)
        if review_policy.zero_enabled():
            gh.ensure_label(review_policy.zero_label, *ZERO_LABEL_STYLE)
    for name, (color, description) in PRIORITY_LABEL_STYLE.items():
        gh.ensure_label(name, color, description)


def run_forever(dispatcher, interval, sleep=time.sleep):
    while True:
        try:
            dispatcher.tick()
        except (GitHubError, WorkspaceError) as exc:
            dispatcher.log(f'tick failed: {exc}')
        sleep(interval)
