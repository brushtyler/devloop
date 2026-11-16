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

Level 0 (#107) needs no review: when a trusted user's label allows it and the whole diff is in
the operator's allowlist, the head goes to ready-to-merge once every expected check run
succeeded on it, no finding is open and no trusted user's request for changes stands.

Findings (#110): every blocking finding has an owner, ``round/reviewer/ID``. Only its owner
closes it in a later review; the dispatcher closes CI findings when CI passes on the current
head, and a trusted user can close any finding with ``/devloop finding <key> resolved|withdrawn
<reason>``. Both closures are dispatcher ``finding`` records. An approval is not promoted while
another owner's finding is open.
"""

import itertools
import string
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from .agents import AgentError
from .apps import DISPATCHER, IMPLEMENT, ISSUE_COMMENT, PR_COMMENT, REVIEW
from .github import GitHubError, merge_state
from .markdown import (RESUME, dispatcher_header, publishable, read_marker, render_escalation,
                       render_fix, render_native_review, render_review)
from .policy import describe as describe_receipt
from .policy import (BINDING, ZERO_LEVEL, agent_profile, changes_requested, check_outcome,
                     check_states, differences, distinct, evaluate, independent, profile_of,
                     receipt_of, recorded_level, zero_refused)
from .review import (CI, FINDING_COMMAND, OPERATOR, ci_failure_review, current_round,
                     finding_commands, finding_key, finding_state, last_round, normalize_review,
                     open_findings, owner_of, round_verdict)
from .workspace import WorkspaceError

PROMPT_DIR = Path(__file__).resolve().parent / 'prompts'
REREVIEW_COMMAND = '/devloop rereview'
EXPENSIVE = ('review', 'fix', 'implement', 'sync')
LABEL_STYLE = {
    'ready': ('0e8a16', 'devloop: pronta per essere implementata da un agent'),
    'working': ('fbca04', 'devloop: implementazione in corso'),
    'review': ('1d76db', 'devloop: in attesa di review automatica'),
    'changes': ('d93f0b', 'devloop: modifiche richieste dalla review'),
    'ready_to_merge': ('0e8a16', 'devloop: approvata, in attesa del merge umano'),
    'needs_human': ('b60205', 'devloop: serve una decisione umana'),
}
LEVEL_LABEL_STYLE = ('5319e7', 'devloop: la PR richiede una review di livello 2')
ZERO_LABEL_STYLE = ('c5def5', 'devloop: livello 0, nessuna review se il diff è nell\'allowlist')
_RECEIPT_FIELDS = {'pr': 'PR', 'base': 'commit di base', 'head': 'head',
                   'policy': 'versione della policy', 'level': 'livello',
                   'reviewers': 'reviewer richiesti',
                   'profiles': 'identità, modello, effort o argomenti dei reviewer'}
_CLOSED_TEXT = {'resolved': 'risolto', 'withdrawn': 'ritirato'}


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


@dataclass
class Snapshot:
    prs: list
    ready_issues: list   # dicts: number, title, labels (set), labeler
    working_issues: list  # issue numbers


@dataclass(frozen=True)
class Action:
    kind: str
    number: int
    reason: str = ''


def label_names(labels):
    return {label['name'] if isinstance(label, dict) else label for label in labels}


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
        rounds_left = last_round(pr.reviews) < cfg.max_review_rounds
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
            if replay and verdict != 'approve':
                actions.append(Action('apply_review', pr.number, 'verdict already recorded'))
            elif pr.merge_state == 'conflict':
                # Neither reviewed nor promoted: the merge would change the head anyway.
                actions.append(Action('sync', pr.number, 'conflicts with the base branch'))
            elif pr.merge_state == 'unknown':
                continue  # GitHub is still computing mergeability: next tick
            elif pr.checks == 'pending':
                continue
            elif replay and not unbound and pr.checks != 'fail':
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
                          else 'every finding of the request for changes is closed' if settled
                          else '')
                actions.append(Action('review', pr.number, reason))
    free = cfg.max_open_prs - len(snapshot.prs)
    if free > 0:
        blocked = set(cfg.blocked_labels) | {lb.working, lb.needs_human}
        candidates = [i for i in snapshot.ready_issues
                      if not (i['labels'] & blocked) and i['number'] not in pr_issues
                      and i['labeler'] in cfg.trusted_users]
        candidates.sort(key=lambda i: (not (i['labels'] & set(cfg.priority_labels)), i['number']))
        actions += [Action('implement', i['number']) for i in candidates[:free]]
    capped, spent = [], 0
    for action in actions:
        if action.kind in EXPENSIVE:
            if spent >= cfg.max_actions_per_tick:
                continue
            spent += 1
        capped.append(action)
    return capped


def pending_reviewers(records):
    """The reviewers an agent round still waits for: those the receipt of its first review
    requires and that have not reviewed yet. A CI round, or a review without a receipt, is
    complete as it is."""
    receipt = receipt_of(records[0]) if records and records[0]['source'] == 'agent' else None
    if receipt is None:
        return []
    done = {owner_of(r) for r in records}
    return [key for key in receipt['reviewers'] if key not in done]


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
    (``activity.Activity``) records the running action for ``devloop status``."""

    def __init__(self, cfg, gh, ws, runner, log=print, identities=None, activity=None):
        self.cfg = cfg
        self.gh = gh
        self.ws = ws
        self.runner = runner
        self.log = log
        self.identities = identities
        self.activity = activity

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
                pr.checks, pr.failed_checks, pr.failed_runs = self.gh.checks(pr.head_sha)
            if (lb.ready_to_merge in pr.labels and pr.zero_head is not None
                    and zero_labels_hold(pr, self.cfg.review_policy)):
                pr.zero_problem = self._zero_problem(pr)
            prs.append(pr)
        ready = []
        for raw in self.gh.issues_with_label(lb.ready):
            ready.append({'number': raw['number'], 'title': raw['title'],
                          'labels': label_names(raw['labels']),
                          'labeler': self.gh.last_labeler(raw['number'], lb.ready)})
        working = [raw['number'] for raw in self.gh.issues_with_label(lb.working)]
        return Snapshot(prs=prs, ready_issues=ready, working_issues=working)

    # Tick ----------------------------------------------------------------
    def tick(self, dry_run=False):
        snap = self.snapshot()
        actions = plan(snap, self.cfg)
        prs = {pr.number: pr for pr in snap.prs}
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
        reason = f'Il passo `{action.kind}` è fallito: {exc}.{self._log_hint(exc)}'
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
        issue = dict(self.gh.issue(number), title=history['title'], body=history['body'])
        comments = [c for c in issue.get('comments') or []
                    if (c.get('author') or {}).get('login') in self.cfg.trusted_users]
        rendered = '\n\n'.join(f'### Comment by {c["author"]["login"]}\n\n{c["body"]}' for c in comments)
        return issue, ('## Issue comments\n\n' + rendered) if rendered else ''

    def _write_turn(self, agent, prompt, schema, path, label):
        """Run a writing agent, then the tests; retry with the failures. Returns the output and
        ``None`` on success, or the output and a reason to escalate."""
        for attempt in range(1 + self.cfg.implement_test_retries):
            result = self.runner.run(agent, 'write', prompt, schema, path, f'{label}-{attempt + 1}')
            if result['status'] == 'blocked':
                return result, None
            if not self.ws.has_changes(path) and schema == 'implement':
                return result, ('Implementazione senza modifiche ai file.', '')
            passed, tail = self.ws.run_tests(path, self.cfg.test_cmd, self.cfg.test_timeout_seconds)
            if passed:
                return result, None
            prompt += ('\n\n## Test failures after your previous attempt\n\nThe test command failed. '
                       f'Fix the cause, then finish again.\n\n```text\n{tail}\n```\n')
        return result, ('I test locali falliscono anche dopo i tentativi di correzione.', tail)

    def _commit_message(self, agent, message, issue):
        message = message.strip() + f'\n\nRefs #{issue}\n'
        if agent.commit_trailer:
            message += f'\n{agent.commit_trailer}\n'
        return message

    def _blocked(self, number, agent, reason, remove, on_pr):
        author = self._as(agent.key, PR_COMMENT if on_pr else ISSUE_COMMENT)
        author.comment(number, publishable(self.runner.header(agent) + '\n## Lavoro bloccato\n\n'
                                           + reason.strip() + '\n\n' + RESUME))
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
        state = self.gh.checks(head)[0]
        if state not in ('pass', 'none'):
            self.log(f'  not promoted: CI is {state}')
            return False
        return True

    # Review policy -----------------------------------------------------------
    def _receipt(self, pr):
        """The policy receipt of the PR's pushed head, computed now from the running
        dispatcher's policy: the full diff against the latest base commit, every ``labeled``
        event of the level label on the PR, and the highest level already recorded on it."""
        policy = self.cfg.review_policy
        base, head, changes = self.ws.review_diff(pr.head_ref)
        records = pr.reviews + pr.policy
        # Level 0 (#107): only read when enabled, so that without it nothing changes.
        zero = (self.gh.label_events(pr.number, policy.zero_label) if policy.zero_enabled()
                else [])
        return evaluate(policy, pr=pr.number, base=base, head=head, changes=changes,
                        labelers=self.gh.labelers(pr.number, policy.label),
                        trusted_users=self.cfg.trusted_users,
                        previous_level=recorded_level(records),
                        select=lambda level: self._select(pr, level)[0],
                        profile=lambda key: self._profile(key, 'review'),
                        zero_events=zero, refused=zero_refused(records))

    def _profile(self, key, mode):
        """The identity and tuple agent ``key`` acts with now in ``mode`` (``write`` or
        ``review``), as markers record them."""
        return agent_profile(self.cfg.agent(key), self._app_login(key), mode)

    def _select(self, pr, level):
        """``(reviewers, problem)``: the reviewers ``level`` requires on the PR, in order, or
        none and why.

        Level 1 keeps the rule from before #111: the PR's recorded reviewer, else the first of
        the pool, never the PR's implementer. Level 2 takes the first pair of the pool, in
        configuration order, that is distinct and independent of everyone who wrote code on
        the PR: the implementer, every fixer and every agent that resolved conflicts in a sync,
        as their markers record them, and the agent configured now under the implementer's
        key, which runs fixes and syncs. When a marker does not record who wrote the code,
        independence cannot be verified."""
        cfg, implementer = self.cfg, pr.meta['implementer']
        if level == 1:
            for key in (pr.meta['reviewer'], *cfg.pool()):
                if key in cfg.agents and key != implementer:
                    return [key], ''
            return [], (f'nessun reviewer configurato è diverso dall\'implementer della PR '
                        f'(`{implementer}`)')
        authors = [profile_of(pr.meta), *map(profile_of, pr.fixes + pr.syncs)]
        if None in authors:
            return [], ('non è verificabile che i reviewer siano indipendenti da chi ha scritto '
                        'il codice: la PR, una sua correzione o una risoluzione di conflitti '
                        'non registra identità e tupla dell\'agent (marker precedenti a #111)')
        if implementer in cfg.agents:
            authors.append(self._profile(implementer, 'write'))
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

    def _policy_comment(self, pr, title, text, receipt, event):
        self.gh.comment(pr.number, publishable(
            dispatcher_header() + f'\n## {title}\n\n{text.strip()}\n\n'
            + describe_receipt(receipt), 'policy', {'event': event, 'receipt': receipt}))

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
        current = self._receipt(pr)
        if current['level'] == ZERO_LEVEL:
            return self._promote_zero(pr, current)
        if len(current['reviewers']) < current['level']:
            return self._escalate_level(pr, current)
        changed = [k for k in BINDING
                   if any(k in differences(receipt_of(r), current) for r in records)]
        approved = {owner_of(r) for r in records if r['verdict'] == 'approve'}
        missing = [key for key in current['reviewers'] if key not in approved]
        if changed or missing:
            what = ('l\'approvazione non ha una ricevuta di policy'
                    if any(receipt_of(r) is None for r in records)
                    else 'cambiati: ' + ', '.join(_RECEIPT_FIELDS[k] for k in changed) if changed
                    else 'manca l\'approvazione di ' + ', '.join(f'`{k}`' for k in missing))
            self._policy_comment(
                pr, 'Approvazione non valida per la promozione',
                f'La ricevuta di policy ricalcolata prima della promozione non coincide con '
                f'quella dell\'approvazione ({what}). La PR resta in review: serve una nuova '
                'review, anche dello stesso commit. Round e finding aperti restano invariati.',
                current, 'stale')
            self.gh.edit_labels(pr.number, add=[lb.review], remove=[lb.changes, lb.ready_to_merge])
            return
        by = ', '.join(f'`{key}`' for key in current['reviewers'])
        self._policy_comment(
            pr, 'Pronta per il merge',
            f'Approvazione del commit `{records[0]["head_sha"][:12]}` da parte di {by} '
            'confermata: la ricevuta di policy ricalcolata coincide con quella della review.',
            current, 'promoted')
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
        overall = self.gh.checks(head)[0]
        if overall in ('pending', 'fail'):
            self.log(f'  not promoted: CI is {overall}')
            return
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
        elif records[0]['round'] >= self.cfg.max_review_rounds:
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
        issue, comments = self._issue_context(number)
        self.gh.edit_labels(number, add=[lb.working], remove=[lb.ready])
        name, branch = f'issue-{number}', f'{cfg.branch_prefix}{number}'
        path = self.ws.new_branch(name, branch)
        prompt = _template('implement', repo=cfg.repo, base_branch=cfg.base_branch,
                           issue_number=number, issue_title=issue['title'],
                           issue_body=issue.get('body') or '', issue_comments=comments,
                           test_cmd=cfg.test_cmd, language=cfg.language)
        result, failure = self._write_turn(agent, prompt, 'implement', path, f'issue-{number}-implement')
        if result['status'] == 'blocked':
            return self._blocked(number, agent, result['blocked_reason'], remove=[lb.working],
                                 on_pr=False)
        if failure:
            return self._escalate(number, failure[0], failure[1], remove=[lb.working])
        auth = self._git_auth(agent.key, IMPLEMENT)
        self.ws.commit_all(path, self._commit_message(agent, result['commit_message'], number),
                           author=auth)
        protected = self._protected_changes(path)
        if protected:
            return self._escalate_protected(number, protected, remove=[lb.working])
        self.ws.push(path, branch, replace=True, auth=auth)
        meta = {'issue': number, 'implementer': agent.key, 'reviewer': cfg.reviewer,
                'profile': self._profile(agent.key, 'write')}
        body = publishable(self.runner.header(agent) + '\n' + result['pr_body'].strip()
                           + f'\n\nCloses #{number}', 'pr', meta)
        pr_number, url = self._as(agent.key, IMPLEMENT).create_pr(
            base=cfg.base_branch, head=branch, title=result['pr_title'], body=body)
        self.gh.edit_labels(pr_number, add=[lb.review])
        self.gh.edit_labels(number, remove=[lb.working])
        self.ws.remove(name)
        self.log(f'  opened {url}')

    def _current_receipt(self, pr):
        """The receipt of the snapshot's head, or None (logged) when the head moved since."""
        receipt = self._receipt(pr)
        if receipt['head'] != pr.head_sha:
            self.log('  head moved since the snapshot; retrying next tick')
            return None
        return receipt

    def _do_ci_failed(self, pr):
        receipt = self._current_receipt(pr)
        if receipt is None:
            return
        if receipt['level'] > 1 and len(receipt['reviewers']) < receipt['level']:
            return self._escalate_level(pr, receipt)
        log_tail = '\n'.join(self.gh.failed_log(run) for run in pr.failed_runs[:2])
        review = ci_failure_review(failed_checks=pr.failed_checks, log_tail=log_tail,
                                   previous_open=open_findings(pr.reviews, pr.closures),
                                   round_no=last_round(pr.reviews) + 1,
                                   max_rounds=self.cfg.max_review_rounds, head_sha=pr.head_sha)
        review['receipt'] = receipt
        self.gh.comment(pr.number, render_review(review, dispatcher_header()))
        self._apply_round(pr, [review], announce=True, reviews=pr.reviews + [review])

    def _do_apply_review(self, pr):
        self._apply_round(pr, current_round(pr.reviews), announce=False, reviews=pr.reviews)

    def _continues(self, pr, records, receipt):
        """Whether the round ``records`` is still in progress for this head and ``receipt``:
        an agent round of the same head, not answered by a fix nor voided by a rereview
        request, with the same receipt, still waiting for a reviewer."""
        return (bool(records) and records[0]['source'] == 'agent'
                and records[0]['head_sha'] == pr.head_sha and not pr.rereview
                and not any(f['round'] == records[0]['round'] for f in pr.fixes)
                and not any(differences(receipt_of(r), receipt) for r in records)
                and bool(pending_reviewers(records)))

    def _do_review(self, pr):
        """One review by one reviewer: each review call is an action of its own. The first
        reviewer of a round opens it; the next ones, on later actions, join it while it reviews
        the same head under the same receipt. The round is applied once every required reviewer
        has reviewed; a review already recorded in it is never run again."""
        cfg = self.cfg
        receipt = self._current_receipt(pr)
        if receipt is None:
            return
        if receipt['level'] == ZERO_LEVEL:
            return self._promote_zero(pr, receipt)
        if len(receipt['reviewers']) < receipt['level']:
            return self._escalate_level(pr, receipt)
        latest = current_round(pr.reviews)
        if self._continues(pr, latest, receipt):
            round_no, done, comments = latest[0]['round'], latest, pr.round_comments
        else:
            round_no, done, comments = last_round(pr.reviews) + 1, [], pr.operator_comments
        if round_no > cfg.max_review_rounds:
            return self._do_round_limit(pr)
        reviewed = {owner_of(r) for r in done}
        agent = cfg.agent(next(key for key in receipt['reviewers'] if key not in reviewed))
        issue, _ = self._issue_context(pr.issue)
        name = f'pr-{pr.number}'
        path = self.ws.existing_branch(name, pr.head_ref)
        if self.ws.head(path) != pr.head_sha:
            self.log('  head moved since the snapshot; retrying next tick')
            return
        # Each reviewer of a round judges the head on its own: earlier rounds only.
        earlier = [r for r in pr.reviews if r['round'] < round_no]
        previous_open = open_findings(earlier, pr.closures)
        prompt = _template('review', repo=cfg.repo, base_branch=cfg.base_branch,
                           pr_number=pr.number, round=round_no, max_rounds=cfg.max_review_rounds,
                           implementer=pr.meta.get('implementer', cfg.implementer),
                           ci=pr.checks, test_cmd=cfg.test_cmd,
                           issue_number=pr.issue, issue_title=issue['title'],
                           issue_body=issue.get('body') or '', pr_body=pr.body,
                           diff_stat=self.ws.diff_stat(path),
                           previous=format_previous_rounds(earlier, pr.fixes, pr.closures),
                           operator_comments=format_operator_comments(comments),
                           language=cfg.language)
        # The reviewer works in a disposable copy, where its tests may write; the PR worktree
        # must come out of the review untouched, or the verdict is discarded.
        copy_name, copy = self.ws.review_copy(path)
        try:
            raw = self.runner.run(agent, 'review', prompt, 'review', copy, f'pr-{pr.number}-review')
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
                                  max_rounds=cfg.max_review_rounds, head_sha=pr.head_sha,
                                  ci=pr.checks, reviewer=agent.key)
        review['receipt'] = receipt
        review['profile'] = self._profile(agent.key, 'review')
        reviewer_gh = self._as(agent.key, REVIEW)
        reviewer_gh.comment(pr.number, render_review(review, self.runner.header(agent)))
        if self._app_login(agent.key):
            # A native review under the reviewer's own identity, visible to branch protection.
            # The marker comment above stays the record devloop reads.
            try:
                reviewer_gh.submit_review(pr.number, commit=pr.head_sha,
                                          **render_native_review(review, self.runner.header(agent)))
            except GitHubError as exc:
                self.log(f'  native review not submitted: {exc}')
        records = done + [review]
        waiting = pending_reviewers(records)
        if not waiting:
            self._apply_round(pr, records, announce=True, reviews=earlier + records)
        self.ws.remove(name)
        self.log(f'  verdict of {agent.key}: {review["verdict"]}'
                 + (f'; the round waits for {", ".join(waiting)}' if waiting else ''))

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
                           language=cfg.language)
        result, failure = self._write_turn(agent, prompt, 'fix', path,
                                           f'pr-{pr.number}-fix-r{latest["round"]}')
        if result['status'] == 'blocked':
            return self._blocked(pr.number, agent, result['blocked_reason'], remove=[lb.changes],
                                 on_pr=True)
        if failure:
            return self._escalate(pr.number, failure[0], failure[1], remove=[lb.changes])
        if self.ws.has_changes(path):
            auth = self._git_auth(agent.key, IMPLEMENT)
            self.ws.commit_all(path, self._commit_message(agent, result['commit_message'], pr.issue),
                               author=auth)
            protected = self._protected_changes(path, since=latest['head_sha'])
            if protected:
                return self._escalate_protected(pr.number, protected, remove=[lb.changes])
            self.ws.push(path, pr.head_ref, auth=auth)
        fix = dict(result, head_sha=self.ws.head(path), profile=self._profile(agent.key, 'write'))
        self._as(agent.key, PR_COMMENT).comment(
            pr.number, render_fix(fix, self.runner.header(agent), latest['round']))
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
                result, failure = self._write_turn(agent, prompt, 'sync', path,
                                                   f'pr-{pr.number}-sync')
            except AgentError as exc:
                return fail(f'Il passo `sync` è fallito: {exc}.{self._log_hint(exc)}')
            if result['status'] == 'blocked':
                return self._blocked(pr.number, agent, result['blocked_reason'].strip()
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
                pr.number, publishable(self.runner.header(agent) + text, 'sync',
                                       {'head_sha': head,
                                        'profile': self._profile(agent.key, 'write')}))
        else:
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


def run_forever(dispatcher, interval, sleep=time.sleep):
    while True:
        try:
            dispatcher.tick()
        except (GitHubError, WorkspaceError) as exc:
            dispatcher.log(f'tick failed: {exc}')
        sleep(interval)
