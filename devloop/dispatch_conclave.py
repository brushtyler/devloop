"""The conclave of the dispatcher (#141): the mixin ``ConclaveMixin`` of ``Dispatcher``.

With ``conclave`` configured, an issue or PR in ``agent:needs-operator`` whose latest escalation
is a block (``_conclave_block``: an implementer or fixer that stopped as ``blocked``, or a
review round whose verdict is ``needs_operator``) gets a conclave of the three configured agents.
Each call (the proposal, then each vote) is one ``conclave`` action, which takes a slot of
``max_actions_per_tick``; the state lives in one dispatcher comment with a ``conclave`` marker,
edited as the conclave goes on: written before each call (``started``), then with its result. A
call not started yet runs on a later tick; a call started without a durable result counts as a
failed one. One conclave per block: the marker is bound to the block's durable reference, and a
block that already has one never gets another. The labels do not change: part 2 of #114 only
records the decision, the escalation stays with the operator.

Pure logic, the record and its rendering are in :mod:`devloop.conclave`.
"""

from itertools import combinations

from .agents import AgentError
from .conclave import (DONE, MAX_DIFF, MAX_OPTION, MAX_OPTIONS, MAX_RESOLUTION, MAX_TEXT,
                       PENDING, apply_proposal, apply_vote, calls_left, consistent,
                       describe_block, make_stale, missing, new_record, next_call, outcome,
                       render, settle_started, text_hash)
from .context import _template
from .dispatch_actions import Models
from .dispatch_supervision import UntrustedChange
from .github import GitHubError
from .ledger import comment_ref
from .markdown import read_marker
from .planning import PullRequest, label_names, operator_blocked, operator_held, waiting_reviewers
from .policy import independent
from .review import current_round, owner_of, round_verdict

# The steps whose ``blocked`` notice convenes a conclave (#141): the sync is not one of them.
BLOCKED_STEPS = ('implement', 'fix')
# What moves an item on after its escalation, as ``_operator_anchor`` reads it (#321): the
# escalation is no longer the latest word on it.
_MOVING = ('review', 'fix', 'sync')


def _short(exc, limit=300):
    """The first line of ``exc``, at most ``limit`` characters."""
    text = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
    return text if len(text) <= limit else text[:limit - 1] + '…'


class ConclaveMixin:
    """The methods of ``Dispatcher`` for the conclave (#141). They use the dispatcher state and
    its other methods through ``self``."""

    # Reading ------------------------------------------------------------------------------
    def _conclave_items(self, prs):
        """The issues and PRs in ``agent:needs-operator`` (not in ``agent:needs-supervisor``)
        with a conclave still due, for the snapshot: ``{number, calls, reason}``. Nothing
        without ``conclave``."""
        if not self.cfg.conclave:
            return []
        lb = self.cfg.labels
        items, seen = [], set()
        for pr in prs:
            seen.add(pr.number)
            if lb.needs_supervisor in pr.labels or not operator_held(pr, lb):
                continue
            view = self._conclave_view(pr.number, self.gh.pr_comments(pr.number), pr)
            if view is not None:
                items.append(self._conclave_item(pr.number, view))
        for label in sorted(lb.operator()):
            for raw in self.gh.issues_with_label(label):
                number = raw['number']
                if number in seen or lb.needs_supervisor in label_names(raw.get('labels') or []):
                    continue
                seen.add(number)
                view = self._conclave_view(number, self.gh.issue_comments(number))
                if view is not None:
                    items.append(self._conclave_item(number, view))
        return items

    @staticmethod
    def _conclave_item(number, view):
        record = view['record']
        what = ('opens' if record is None else
                'proposal' if record['proposal']['state'] != DONE else 'votes')
        return {'number': number, 'calls': calls_left(record),
                'reason': f'conclave {what}: block of `{view["details"]["proposer"]}`'}

    def _conclave_records(self, comments):
        """``(comment id, record)`` of the ``conclave`` records the dispatcher signed."""
        return [(comment_ref(comments[position]), record)
                for position, record in self._dispatcher_records(comments, 'conclave')]

    def _conclave_block(self, comments, pr=None):
        """``(block, details)`` of the block the item's latest escalation records, or None.

        The escalations are the dispatcher's notices, its policy records of an escalation, the
        failed readiness checks and the notices of agents that stopped. A block is the notice
        of an implementer or fixer that stopped (``blocked``), written by that agent's identity
        with the agent, the step, the commit and the reason in its marker, or, on a PR, its
        latest review round with verdict ``needs_operator``, complete or suspended by its first
        ``needs_operator`` (``waiting_reviewers``; the dispatcher's
        ``reviewer_decision`` notice after it counts as the same block). The block's durable
        reference is the notice's comment id, or the round, the reviewer of the first
        ``needs_operator`` record of the round, and its head. Anything after the latest
        escalation that moves the item on (a review, fix or sync record, a command applied, a
        passed check, a policy record of no escalation) leaves no block."""
        anchors = []  # (position, block or None, details)
        review = self._review_block(comments, pr) if pr is not None and pr.meta else None
        for position, comment in enumerate(comments):
            record = read_marker(comment.get('body', ''), 'notice')
            if record is None:
                continue
            login = (comment.get('author') or {}).get('login')
            if record.get('event') == 'blocked':
                found = self._blocked_block(record, comment, login)
                if found is not None:
                    anchors.append((position, *found))
                elif self._writes_records(login):
                    anchors.append((position, None, None))
            elif self._signed(login):
                same = (record.get('class') == 'reviewer_decision' and review is not None
                        and position > review[0])
                anchors.append((position, *(review[1:] if same else (None, None))))
        if review is not None:
            anchors.append(review)
        anchors += [(p, None, None) for p, r in self._dispatcher_records(comments, 'policy')
                    if r.get('class')]
        anchors += [(p, None, None) for p, r in self._preflight_records(comments)
                    if not r['ready']]
        if not anchors:
            return None
        anchors.sort(key=lambda entry: entry[0])
        position, block, details = anchors[-1]
        if block is None or any(p > position for p in self._conclave_moves(comments)):
            return None
        return block, details

    def _blocked_block(self, record, comment, login):
        """``(block, details)`` of an agent's ``blocked`` notice that convenes a conclave, or
        None: one written by the identity of the agent it names, for a step of
        ``BLOCKED_STEPS``, with its commit, its reason and a comment id."""
        agent, step = record.get('agent'), record.get('step')
        ref = comment_ref(comment)
        if not (isinstance(agent, str) and agent in self.cfg.agents and self._wrote(login, agent)
                and step in BLOCKED_STEPS and isinstance(record.get('head'), str)
                and record['head'] and isinstance(record.get('reason'), str)
                and isinstance(ref, int) and not isinstance(ref, bool)):
            return None
        block = {'kind': 'blocked', 'comment': ref, 'agent': agent, 'step': step}
        return block, {'proposer': agent, 'step': step, 'commit': record['head'],
                       'reason': record['reason']}

    def _review_block(self, comments, pr):
        """``(position, block, details)`` of the PR's latest review round when its verdict is
        ``needs_operator``, else None. The first ``needs_operator`` suspends the round
        (``waiting_reviewers``): it counts even before the other reviewers its receipt requires
        have reviewed. Of several reviewers who asked for the operator, the first record of the
        round is the block and its author the proposer: the round opens one conclave."""
        entries = self._marker_entries(pr, comments, 'review')
        if not entries:
            return None
        records = current_round([record for _, record in entries])
        if (waiting_reviewers(records, self.cfg.conclave)
                or round_verdict(records) != 'needs_operator'):
            return None
        latest = records[0]['round']
        first = next(r for r in records if r['verdict'] == 'needs_operator')
        reviewer = owner_of(first)
        position = max(p for p, r in entries if r['round'] == latest)
        block = {'kind': 'review', 'round': latest, 'reviewer': reviewer,
                 'head': first['head_sha']}
        reason = (first.get('human_reason') or '').strip() or first['summary'].strip()
        return position, block, {'proposer': reviewer, 'step': 'review',
                                 'commit': first['head_sha'], 'reason': reason}

    def _conclave_moves(self, comments):
        """The positions of the records that move an item on after an escalation."""
        moved = {p for p, r in self._preflight_records(comments) if r['ready']}
        moved |= {p for p, r in self._dispatcher_records(comments, 'policy')
                  if not r.get('class')}
        moved |= {p for p, r in self._dispatcher_records(comments, 'supervision')
                  if r.get('outcome') in ('applied', 'accepted', 'decided')}
        for position, comment in enumerate(comments):
            body = comment.get('body', '')
            if (any(read_marker(body, kind) is not None for kind in _MOVING)
                    and self._writes_records((comment.get('author') or {}).get('login'))):
                moved.add(position)
        return moved

    def _conclave_members(self, proposer):
        """The members, the proposer first and then the other two in configuration order, or
        None (logged) when there is no conclave: the proposer is not one of the three, or two
        of them are not distinct by identity and by ``(kind, model, effort)``."""
        keys = self.cfg.conclave
        if proposer not in keys:
            self.log(f'  conclave: `{proposer}` is not a member; the escalation stays as it is')
            return None
        profiles = {key: self._profile(key, 'review') for key in keys}
        for a, b in combinations(keys, 2):
            if not independent(profiles[a], profiles[b]):
                self.log(f'  conclave: `{a}` and `{b}` are not distinct by identity and by '
                         '(kind, model, effort); the escalation stays as it is')
                return None
        return [proposer] + [key for key in keys if key != proposer]

    def _conclave_view(self, number, comments, pr=None):
        """What issue or PR ``number`` needs from a conclave, read from its ``comments``, or
        None when nothing: ``{block, details, record, comment, members, calls}``. ``record``
        is the conclave of the block already recorded (None before it opens), ``comment`` its
        comment id. A concluded conclave, or one whose record does not hold together
        (``consistent``, logged), leaves nothing to do: a block never gets a second one."""
        found = self._conclave_block(comments, pr)
        if found is None:
            return None
        block, details = found
        records = [(cid, r) for cid, r in self._conclave_records(comments)
                   if r['block'] == block]
        if records:
            comment, record = records[0]
            ok, why = consistent(record)
            if not ok:
                self.log(f'  #{number}: the conclave record does not hold ({why}); left to the '
                         'operator')
                return None
            if record['outcome']:
                return None
            members = [record['proposal']['agent'], *(v['agent'] for v in record['votes'])]
        else:
            comment, record = None, None
            members = self._conclave_members(details['proposer'])
            if members is None:
                return None
        return {'block': block, 'details': details, 'record': record, 'comment': comment,
                'members': members, 'calls': calls_left(record)}

    # Running --------------------------------------------------------------------------------
    def _conclave_context(self, issue_number, view, pr):
        """The frozen context of every call, read now: the issue as the implementer gets it
        (title, body, trusted comments) with its hash, the PR's head, and the changes the agent
        that stopped left uncommitted in its worktree, with their hash."""
        issue, comments = self._issue_context(issue_number)
        details = view['details']
        diff = ''
        if view['block']['kind'] == 'blocked':
            name = f'pr-{pr.number}' if pr is not None else f'issue-{issue_number}'
            diff = self.ws.pending_diff(name, details['commit'])
        return {'issue': issue, 'comments': comments, 'hash': issue['request'],
                'head': self.gh.pr_head(pr.number) if pr is not None else None,
                'diff': diff, 'diff_hash': text_hash(diff)}

    @staticmethod
    def _conclave_stale(record, context):
        """Why the context frozen in ``record`` no longer holds, or ''."""
        if context['hash'] != record['issue_hash']:
            return 'il testo dell\'issue è cambiato'
        if record['head'] is not None and context['head'] != record['head']:
            return f'l\'head della PR è passato a `{(context["head"] or "")[:12]}`'
        if context['diff_hash'] != record['diff_hash']:
            return 'le modifiche non committate dell\'agent fermo sono cambiate'
        return ''

    def _conclave_recheck(self, issue_number, view, pr, record):
        """Why the context frozen in ``record`` no longer holds, read again now, or ''."""
        try:
            return self._conclave_stale(record, self._conclave_context(issue_number, view, pr))
        except UntrustedChange as exc:
            return f'il testo dell\'issue non è più fidato ({_short(exc)})'

    def _do_conclave(self, target):
        """One step of the conclave of an issue or PR: open it, or resume it from its record,
        run the next call and record the outcome once known."""
        pr = target if isinstance(target, PullRequest) else None
        number = pr.number if pr is not None else target
        lb = self.cfg.labels
        fresh = self.gh.pr_state(number) if pr is not None else self.gh.issue(number)
        labels = label_names(fresh.get('labels') or [])
        if not operator_blocked(labels, lb) or lb.needs_supervisor in labels:
            self.log('  labels changed since the snapshot: no conclave')
            return
        comments = (self.gh.pr_comments if pr is not None else self.gh.issue_comments)(number)
        view = self._conclave_view(number, comments, pr)
        if view is None:
            self.log('  no conclave due')
            return
        issue_number = pr.issue if pr is not None else number
        details, record, comment = view['details'], view['record'], view['comment']
        try:
            context, stale = self._conclave_context(issue_number, view, pr), ''
        except UntrustedChange as exc:
            context, stale = None, f'il testo dell\'issue non è più fidato ({_short(exc)})'
        if record is None:
            record = new_record(view['block'], details['step'], view['members'], issue_number,
                                context['hash'] if context else '', details['commit'],
                                (context['head'] if context else None)
                                if pr is not None else None,
                                context['diff_hash'] if context else '')
            if not stale and pr is not None and context['head'] != details['commit']:
                stale = (f'l\'head della PR (`{(context["head"] or "")[:12]}`) non è il commit '
                         'del blocco')
            self.log(f'  conclave opened: proposer {view["members"][0]}, voters '
                     + ', '.join(view['members'][1:]))
        else:
            stale = stale or self._conclave_stale(record, context)
            settle_started(record)
        if stale:
            make_stale(record, stale)
        elif outcome(record) is None:
            which = next_call(record)
            if which is not None:
                comment = self._conclave_call(number, pr, record, comment, which, context,
                                              details)
                if outcome(record) is not None:
                    # The last call is in: the frozen context must still hold now, or the
                    # outcome is BLOCKED (RESERVED still prevails), never one on an old head or
                    # an old text of the issue.
                    stale = self._conclave_recheck(issue_number, view, pr, record)
                    if stale:
                        make_stale(record, stale)
        record['outcome'] = outcome(record) or ''
        self._conclave_publish(number, comment, record, pr)
        self.log(f'  conclave: {record["outcome"] or "in progress"}')

    def _conclave_call(self, number, pr, record, comment, which, context, details):
        """Run call ``which`` (``'proposal'`` or a voter's index) of ``record``: a read-only
        turn in a disposable checkout of the block's commit, recorded as started before it
        runs. A failure or a timeout of the agent is a missing contribution. Returns the
        comment id of the record."""
        cfg = self.cfg
        proposal = which == 'proposal'
        member = record['proposal'] if proposal else record['votes'][which]
        values = dict(repo=cfg.repo, issue_number=record['issue'],
                      issue_title=context['issue']['title'],
                      issue_body=context['issue'].get('body') or '',
                      issue_comments=context['comments'], agent=member['agent'],
                      block_description=describe_block(record['block']),
                      block_reason=details['reason'].strip() or '(non indicato)',
                      commit=record['commit'], diff=_diff_section(context['diff']),
                      language=cfg.language, max_resolution=MAX_RESOLUTION, max_text=MAX_TEXT,
                      max_options=MAX_OPTIONS, max_option=MAX_OPTION)
        if proposal:
            prompt = _template('conclave_proposal', **values)
        else:
            text = record['proposal']['resolution']
            # The voters get exactly the recorded text: its hash must still be the one bound.
            if text_hash(text) != record['hash']:
                missing(record, which, 'il testo della proposta non corrisponde al suo hash')
                return comment
            prompt = _template('conclave_vote', proposer=record['proposal']['agent'],
                               proposal=text, proposal_hash=record['hash'], **values)
        kind = 'pr' if pr is not None else 'issue'
        name = f'{kind}-{number}-conclave'
        path = self.ws.commit_copy(name, record['commit'],
                                   branch=pr.head_ref if pr is not None else None)
        try:
            member['state'] = 'started'
            comment = self._conclave_publish(number, comment, record, pr)
            label = f'{kind}-{number}-conclave-' + ('proposal' if proposal else f'vote{which + 1}')
            try:
                raw, used = self._turn(Models(cfg.agent(member['agent'])), 'review', prompt,
                                       'conclave_proposal' if proposal else 'conclave_vote',
                                       path, label, self._conclave_allowed(record, member),
                                       where=(number, 'conclave'), note='conclave')
            except AgentError as exc:
                missing(record, which, f'agent fallito o in timeout: {_short(exc)}')
                self.log(f'  conclave: {member["agent"]} failed: {_short(exc)}')
            else:
                # The profile it actually ran with, a fallback model included: the later calls,
                # on this tick or the next ones, stay distinct from it.
                profile = self._profile(member['agent'], 'review', used)
                if proposal:
                    apply_proposal(record, raw, profile)
                else:
                    apply_vote(record, which, raw, record['hash'], profile)
        finally:
            self.ws.remove(name)
        return comment

    def _conclave_allowed(self, record, member):
        """Which models ``member`` may contribute with (the ``allowed`` of ``_turn``): a model,
        fallback included, distinct by identity and by ``(kind, model, effort)`` from the
        profile each other member contributed with, as the record keeps it, and from the
        configured profile of each member still to be asked. The three stay distinct even when
        a model is unavailable."""
        others = [item['profile'] if item['state'] == DONE and item['profile']
                  else self._profile(item['agent'], 'review')
                  for item in [record['proposal'], *record['votes']]
                  if item is not member and item['state'] in (DONE, PENDING)]

        def allowed(candidate):
            mine = self._profile(member['agent'], 'review', candidate)
            return all(independent(mine, other) for other in others)
        return allowed

    def _conclave_publish(self, number, comment, record, pr=None):
        """Write ``record`` on issue or PR ``number``: a new comment when ``comment`` is None,
        else an edit of it. Returns the comment id; a new comment whose id is neither returned
        nor found again raises ``GitHubError``."""
        body = render(record)
        if comment is not None:
            self.gh.edit_comment(comment, body)
            return comment
        comment = self.gh.comment(number, body)
        if comment is None:
            read = self.gh.pr_comments if pr is not None else self.gh.issue_comments
            found = [cid for cid, r in self._conclave_records(read(number))
                     if r['block'] == record['block']]
            comment = found[0] if found else None
        if comment is None:
            raise GitHubError(f'the conclave record of #{number} cannot be found again')
        return comment


def _diff_section(diff):
    """The prompt section with the changes the agent that stopped left uncommitted."""
    if not diff.strip():
        return ''
    shown = diff if len(diff) <= MAX_DIFF else diff[:MAX_DIFF] + '\n[... truncated ...]'
    return ('## Uncommitted changes of the agent that stopped (read only)\n\n'
            'They are not in the checkout: they are what the agent left in its worktree when it '
            'stopped, against the commit of reference.\n\n~~~~~diff\n' + shown + '\n~~~~~')
