"""The conclave of the dispatcher (#141): the mixin ``ConclaveMixin`` of ``Dispatcher``.

With ``conclave`` configured, an issue or PR in ``agent:needs-operator`` whose latest escalation
is a block (``_conclave_block``: an implementer or fixer that stopped as ``blocked``, or a
review round whose verdict is ``needs_operator``) gets a conclave of the three configured agents.
Each call (the proposal, then each vote) is one ``conclave`` action, which takes a slot of
``max_actions_per_tick``; the state lives in one dispatcher comment with a ``conclave`` marker,
edited as the conclave goes on: written before each call (``started``), then with its result. A
call not started yet runs on a later tick; a call started without a durable result counts as a
failed one. One conclave per block: the marker is bound to the block's durable reference, and a
block that already has one never gets another. While it runs the labels do not change.

Since #142 (part 3 of #114) a ``UNANIMOUS`` decision restarts the work once, with one more
``conclave`` action (``_conclave_resume``): the implementation from the base branch, a fix from
the PR's head, a review as a new review of the same head. Only the latest decision the
dispatcher verified reaches the prompts of the issue and of its PR (``_conclave_resolution``),
while the issue text and, on a PR, the head it was taken on hold, and until a trusted user
vetoes it (``/devloop veto``): the work it restarted is then suspended (``_do_veto``), and
nothing produced with it is published after the veto.

Since #605, with ``[roles.coordinator]`` a voter that fails leaves its vote waiting for the
coordinator's substitute vote (``_conclave_substitute``), until
``conclave_substitute_timeout_seconds`` from the failure: meanwhile the conclave is due only for
a command of the coordinator's identity, or once the deadline passed (``_substitute_due``).

Pure logic, the record and its rendering are in :mod:`devloop.conclave`.
"""

from itertools import combinations

from .activity import iso
from .agents import AgentError
from .conclave import (DONE, MAX_DIFF, MAX_OPTION, MAX_OPTIONS, MAX_RESOLUTION, MAX_TEXT,
                       PENDING, STARTED, UNANIMOUS, VETO_COMMAND, VOTE_COMMAND, apply_proposal,
                       apply_substitute, apply_vote, await_substitute, calls_left, consistent,
                       describe_block, expire_substitute, make_stale, missing, new_record,
                       next_call, outcome, refuse_substitute, render, resolution_section,
                       settle_started, substitute_problem, substitute_vote, text_hash,
                       too_long, veto_command,
                       vote_command, waiting_substitute)
from .context import _template, prompt_values
from .dispatch_actions import Models
from .dispatch_supervision import UntrustedChange, _epoch
from .github import GitHubError
from .ledger import comment_ref
from .markdown import carries_record, read_marker
from .planning import (Action, PullRequest, label_names, operator_blocked, operator_held,
                       preflight_state, waiting_reviewers)
from .policy import independent
from .review import current_round, flow_verdict, owner_of, round_verdict
from .workspace import WorkspaceError

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
            if (lb.needs_supervisor in pr.labels or not operator_held(pr, lb)
                    or pr.attempt_problem):
                # A PR whose attempt does not hold (#384) gets no conclave: no work on it.
                continue
            view = self._conclave_view(pr.number, self._pr_thread(pr.number), pr)
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
        """``{number, calls, reason, opens}`` of a conclave due: ``opens`` when it restarts an
        implementation (#142), which takes a free PR slot as the step itself would."""
        record = view['record']
        if view.get('resume'):
            return {'number': number, 'calls': 1, 'opens': record['step'] == 'implement',
                    'reason': f'conclave resume: `{record["step"]}` with the unanimous '
                              'resolution'}
        what = ('opens' if record is None else
                'proposal' if record['proposal']['state'] != DONE else
                'substitute vote' if (waiting_substitute(record) is not None
                                      and next_call(record) is None) else 'votes')
        return {'number': number, 'calls': calls_left(record), 'opens': False,
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
                       'reason': record['reason'], 'conclave': record.get('conclave')}

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
        # A needs_operator for the trust policy asks for no decision (#434): never the block.
        first = next(r for r in records if flow_verdict(r) == 'needs_operator')
        reviewer = owner_of(first)
        position = max(p for p, r in entries if r['round'] == latest)
        block = {'kind': 'review', 'round': latest, 'reviewer': reviewer,
                 'head': first['head_sha']}
        reason = (first.get('human_reason') or '').strip() or first['summary'].strip()
        return position, block, {'proposer': reviewer, 'step': 'review',
                                 'commit': first['head_sha'], 'reason': reason,
                                 'conclave': first.get('conclave')}

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
        (``consistent``, logged), leaves nothing to do: a block never gets a second one. A
        ``UNANIMOUS`` one whose work has not restarted yet is due once more, with ``resume``
        (``_resume_view``, #142). A block raised by a step that already ran with a conclave's
        resolution gets no conclave: the escalation stays with the operator."""
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
                if record['outcome'] != UNANIMOUS or record.get('resumed') is not None:
                    return None
                return self._resume_view(number, comments, pr, block, details, comment, record)
            if (waiting_substitute(record) is not None and next_call(record) is None
                    and all(item['state'] != STARTED
                            for item in [record['proposal'], *record['votes']])
                    and not self._substitute_due(record, comments)):
                # Waiting for the coordinator's substitute vote (#605): nothing to do until a
                # command of the coordinator, or the deadline. A call started without a result
                # is still settled (``settle_started``): a second member missing ends the wait.
                return None
            members = [record['proposal']['agent'], *(v['agent'] for v in record['votes'])]
        else:
            comment, record = None, None
            if details.get('conclave') is not None:
                self.log(f'  #{number}: the block comes from a step that ran with the resolution '
                         f'of conclave {details["conclave"]}; the escalation stays as it is')
                return None
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
        if pr is not None:
            problem = self._attempt_problem(pr.number, pr.issue, pr.meta.get('attempt'))
            if problem:
                # Its attempt does not hold (#384): no call, no resume, nothing recorded.
                self.log(f'  no conclave: {problem}')
                return
        # The same thread the snapshot read (``_conclave_items``): on a PR, the native reviews
        # carry review records too (#325).
        comments = (self._pr_thread(number) if pr is not None
                    else self.gh.issue_comments(number))
        view = self._conclave_view(number, comments, pr)
        if view is None:
            self.log('  no conclave due')
            return
        if view.get('resume'):
            return self._conclave_resume(number, pr, view)
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
        else:
            # The coordinator's substitute vote, or the end of the wait for it (#605).
            self._conclave_substitute(number, record, comments)
        if not stale and outcome(record) is None:
            which = next_call(record)
            if which is not None:
                after = max((c['id'] for c in comments if not c.get('native')
                             and isinstance(c.get('id'), int)
                             and not isinstance(c['id'], bool)), default=0)
                comment = self._conclave_call(number, pr, record, comment, which, context,
                                              details, after)
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

    def _conclave_call(self, number, pr, record, comment, which, context, details, after=0):
        """Run call ``which`` (``'proposal'`` or a voter's index) of ``record``: a read-only
        turn in a disposable checkout of the block's commit, recorded as started before it
        runs. A failure or a timeout of the agent is a missing contribution; with
        ``[roles.coordinator]`` a voter's waits for the coordinator's substitute vote instead
        (#605), until ``conclave_substitute_timeout_seconds`` from now, and only a command
        written after comment ``after`` counts. Returns the
        comment id of the record. A call refused by the block on uncertain turns
        (``TurnBlocked``, #362), on a PR whose attempt does not hold (``JournalError``,
        #384), or not covered by the budget envelope of its story (``BudgetRefused``, #365),
        raises before anything is recorded."""
        cfg = self.cfg
        proposal = which == 'proposal'
        member = record['proposal'] if proposal else record['votes'][which]
        values = dict(prompt_values(cfg), repo=cfg.repo, issue_number=record['issue'],
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
        # With the block on uncertain turns (#362), a call of a work with turns to reconcile is
        # refused before it is recorded as started: a later tick would count it as missing.
        self._check_turns(number)
        if pr is not None:
            # Nor is a call on a PR whose attempt does not hold (#384): ``JournalError``.
            self._check_attempt(pr.number, pr.issue, pr.meta.get('attempt'))
        # Nor, with the budget on (#365), one the envelope of the story would not cover
        # (``BudgetRefused``); the booking itself stays in the turn's intent, under the lock.
        self._check_budget(number, 'conclave', cfg.agent(member['agent']))
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
                why = f'agent fallito o in timeout: {_short(exc)}'
                self.log(f'  conclave: {member["agent"]} failed: {_short(exc)}')
                if proposal or cfg.coordinator is None:
                    # The proposer is never substituted (#605), nor any member without a
                    # coordinator.
                    missing(record, which, why)
                else:
                    deadline = iso(self.clock() + cfg.conclave_substitute_timeout_seconds)
                    await_substitute(record, which, why, deadline, comment, after)
                    if waiting_substitute(record) is not None:
                        self.log(f'  conclave: waiting for the coordinator\'s substitute vote '
                                 f'until {deadline}')
            else:
                # The profile it actually ran with, a fallback model included: the later calls,
                # on this tick or the next ones, stay distinct from it.
                profile = self._profile(member['agent'], 'review', used)
                if proposal:
                    apply_proposal(record, self._redacted_proposal(raw), profile)
                else:
                    apply_vote(record, which, raw, record['hash'], profile)
        finally:
            self.ws.remove(name)
        return comment

    def _redacted_proposal(self, raw):
        """``raw``, the proposer's output, with the names of the parameters in place of the
        host's values in its resolution (#620, ``redact.Redactor.redact_text``), before its
        hash: the voters get, and the record carries, the very text the hash binds, which
        the publication then leaves as it is. As it is without a redactor, or when the output
        as the proposer gave it is beyond the limits: a failed contribution, which the
        redaction must not shorten into an accepted one (``apply_proposal`` checks the
        redacted text in its turn)."""
        redactor = getattr(self.identities, 'redactor', None)
        if (redactor is None or not isinstance(raw.get('resolution'), str)
                or too_long(raw, 'resolution', MAX_RESOLUTION)):
            return raw
        return dict(raw, resolution=redactor.redact_text(raw['resolution']))

    def _conclave_allowed(self, record, member):
        """Which models ``member`` may contribute with (the ``allowed`` of ``_turn``): a model,
        fallback included, distinct by identity and by ``(kind, model, effort)`` from the
        profile each other member contributed with, as the record keeps it, and from the
        configured profile of each member still to be asked. The three stay distinct even when
        a model is unavailable. The coordinator's substitute vote (#605) is outside the rule."""
        substituted = substitute_vote(record)
        others = [item['profile'] if item['state'] == DONE and item['profile']
                  else self._profile(item['agent'], 'review')
                  for item in [record['proposal'], *record['votes']]
                  if item is not member and item is not substituted
                  and item['state'] in (DONE, PENDING)]

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

    # The substitute vote of the coordinator (#605) ------------------------------------------
    @staticmethod
    def _vote_commands(comments, after):
        """``(comment, command)`` of the ``/devloop vote`` commands (``vote_command``) written
        in ``comments`` after comment id ``after``, whoever wrote them, oldest first. devloop's
        records and native reviews are never commands."""
        found = []
        for comment in comments:
            body, ident = comment.get('body') or '', comment.get('id')
            if (comment.get('native') or carries_record(body) or not isinstance(ident, int)
                    or isinstance(ident, bool) or ident <= after):
                continue
            command = vote_command(body)
            if command is not None:
                found.append((comment, command))
        return sorted(found, key=lambda entry: entry[0]['id'])

    def _substitute_expired(self, substitute):
        """Whether the deadline of the wait has passed; an unreadable one has (it fails
        closed)."""
        deadline = _epoch(substitute['deadline'])
        return deadline is None or self.clock() >= deadline

    def _substitute_due(self, record, comments):
        """Whether the conclave of ``record``, waiting for the coordinator's substitute vote,
        has something to do: a command of the coordinator's identity not spent yet, or the
        deadline passed. A command of anyone else is never one."""
        substitute = waiting_substitute(record)
        return (self._substitute_expired(substitute)
                or any(self._coordinator_may((c.get('author') or {}).get('login'), 'vote')
                       for c, _ in self._vote_commands(comments, substitute['last'])))

    def _coordinator_profile(self, login):
        """The profile of the coordinator's substitute vote: its identity, and the agent and
        model ``[roles.coordinator]`` declares (devloop does not run it)."""
        actor = self.cfg.coordinator
        return {'identity': login, 'kind': actor.kind, 'model': actor.model, 'effort': '',
                'args': [], 'provider': actor.provider}

    def _conclave_substitute(self, number, record, comments):
        """The coordinator's substitute vote in the conclave of ``record`` (#605), from the
        thread ``comments``, while it waits for one. The commands of the coordinator's identity
        not spent yet, oldest first: the first that names this conclave and the hash of its
        proposal, written and last changed before the deadline, is the vote; each one before it
        is refused, and recorded as such. A command of anyone else is ignored and logged. Past the deadline
        without a vote, the vote is missing and the conclave closes as before."""
        substitute = waiting_substitute(record)
        if substitute is None:
            return
        for comment, command in self._vote_commands(comments, substitute['last']):
            ident, login = comment['id'], (comment.get('author') or {}).get('login')
            if not self._coordinator_may(login, 'vote'):
                self.log(f'  #{number}: ignored `{VOTE_COMMAND}` by {login}: only the '
                         'coordinator\'s identity votes in place of a member')
                continue
            why = substitute_problem(record, command)
            # The comment is read as it is now: its latest change (``updated_at``), not only
            # its creation, must come before the deadline, or a comment written in time and
            # turned into a vote later would count.
            times = [_epoch(comment.get(key)) for key in ('created_at', 'updated_at')]
            deadline = _epoch(substitute['deadline'])
            if not why and (None in times or deadline is None):
                why = ('l\'ora del comando, della sua ultima modifica o la scadenza non sono '
                       'leggibili')
            elif not why and max(times) > deadline:
                why = (f'scritto o modificato dopo la scadenza del voto sostitutivo '
                       f'({substitute["deadline"]})')
            if why:
                refuse_substitute(record, ident, why)
                self.log(f'  #{number}: refused `{VOTE_COMMAND}` of comment {ident}: {why}')
                continue
            apply_substitute(record, command, ident, login, self._coordinator_profile(login),
                             iso(self.clock()))
            self.log(f'  #{number}: substitute vote {command["verdict"]} of the coordinator in '
                     f'place of `{substitute["member"]}`')
            return
        if self._substitute_expired(substitute):
            expire_substitute(record)
            self.log(f'  #{number}: no substitute vote by {substitute["deadline"]}: the block '
                     'stays with the operator')

    # Decisions, resume and veto (#142) --------------------------------------------------------
    def _veto_commands(self, comments, number):
        """``(comment id, login, reason)`` of the ``/devloop veto`` commands trusted users wrote
        in ``comments``, oldest first. One from anyone else, a bot or a devloop app included, is
        ignored and logged; devloop's records and native reviews are never commands."""
        found = []
        for comment in comments:
            body = comment.get('body') or ''
            if comment.get('native') or carries_record(body):
                continue
            reason = veto_command(body)
            ident = comment_ref(comment)
            if reason is None or not isinstance(ident, int) or isinstance(ident, bool):
                continue
            login = (comment.get('author') or {}).get('login')
            if login not in self.cfg.trusted_users:
                self.log(f'  #{number}: ignored `{VETO_COMMAND}` by {login}: the author is not a '
                         'trusted user')
                continue
            found.append((ident, login, reason))
        return sorted(found)

    def _conclave_decision(self, issue_number, issue_comments, pr_comments=None):
        """The latest decision of a conclave on issue ``issue_number`` and its PR, or None:
        ``{comment, record, veto, spent}``. A decision is a ``conclave`` record the dispatcher
        signed, that holds together (``consistent``), with outcome ``UNANIMOUS``, for this
        issue, on the thread of its block: an implementation on the issue (``issue_comments``),
        a fix or a review on the PR (``pr_comments``). GitHub's comment ids grow with time, on
        the issue and on its PR alike: the latest is the one with the greatest id. ``veto``: the
        first veto of a trusted user written after it on either thread (``_veto_commands``), or
        None; ``spent``: whether a notice of the dispatcher already suspended the work for that
        veto."""
        threads = [(issue_comments, ('implement',))]
        if pr_comments is not None:
            threads.append((pr_comments, ('fix', 'review')))
        found = [(cid, record) for comments, steps in threads
                 for cid, record in self._conclave_records(comments)
                 if isinstance(cid, int) and not isinstance(cid, bool)
                 and record['outcome'] == UNANIMOUS and record['step'] in steps
                 and record['issue'] == issue_number and consistent(record)[0]]
        if not found:
            return None
        comment, record = max(found, key=lambda entry: entry[0])
        vetoes = sorted(v for comments, _ in threads
                        for v in self._veto_commands(comments, issue_number) if v[0] > comment)
        spent = any(r.get('veto_of') == comment for comments, _ in threads
                    for _, r in self._dispatcher_records(comments, 'notice', 'escalation'))
        return {'comment': comment, 'record': record, 'veto': vetoes[0] if vetoes else None,
                'spent': spent}

    def _conclave_resolution(self, issue_number, request, issue_comments, pr_comments=None,
                             head=None):
        """``(comment id, record)`` of the decision whose resolution goes into the prompt of a
        step on issue ``issue_number`` or on its PR, or None (logged): the latest decision
        (``_conclave_decision``) only, while no trusted user vetoed it, the hash of the issue's
        request is still ``request``, the one it was taken on, and a decision bound to a PR's
        head is read with that same head (``head``: the head the step works on, None on an
        issue). Nothing else reaches a prompt: no comment of a bot, nor a record that does not
        hold."""
        decision = self._conclave_decision(issue_number, issue_comments, pr_comments)
        if decision is None:
            return None
        comment, record = decision['comment'], decision['record']
        why = ('vetoed by `{}`'.format(decision['veto'][1]) if decision['veto']
               else 'the text of the issue changed' if record['issue_hash'] != request
               else 'the head of the PR changed'
               if record['head'] is not None and record['head'] != head else '')
        if why:
            self.log(f'  conclave decision {comment} not in the prompt: {why}')
            return None
        return comment, record

    def _resolution_for(self, issue_number, issue, pr=None, head=None):
        """``(comment id, record, prompt section)`` of the resolution a step on issue
        ``issue_number`` (``issue``, as ``_issue_context`` returns it) or on its ``pr`` gets,
        read now, or ``(None, None, '')``. ``head``: the head the step works on."""
        pr_comments = self.gh.pr_comments(pr.number) if pr is not None else None
        found = self._conclave_resolution(issue_number, issue['request'], issue['thread'],
                                          pr_comments, head)
        if found is None:
            return None, None, ''
        self.log(f'  with the unanimous resolution of conclave {found[0]}')
        return found[0], found[1], resolution_section(found[1], found[0])

    def _veto_after(self, issue_number, pr_number, comment):
        """The first veto of a trusted user written after the decision in ``comment`` on issue
        ``issue_number`` or PR ``pr_number`` (None for none), read now, or None: what the
        dispatcher checks before publishing anything a step produced with that resolution."""
        threads = [self.gh.issue_comments(issue_number)]
        if pr_number is not None:
            threads.append(self.gh.pr_comments(pr_number))
        vetoes = sorted(v for comments in threads
                        for v in self._veto_commands(comments, issue_number) if v[0] > comment)
        return vetoes[0] if vetoes else None

    def _veto_suspend(self, number, comment, veto, remove, what):
        """Suspend issue or PR ``number`` for ``veto`` of the decision in ``comment``: an
        escalation of class ``conclave_veto`` to the operator, which records both and spends the
        veto. ``what``: what the veto stopped."""
        ident, login, reason = veto
        text = (f'`{login}` ha annullato con `{VETO_COMMAND}` (commento {ident}) la decisione del '
                f'conclave registrata nel commento {comment}: {what} I commit già pubblicati '
                'restano: devloop non li annulla.')
        if reason:
            text += f'\n\nMotivo del veto: {reason}'
        self._escalate(number, text, remove=remove, cls='conclave_veto',
                       extra={'veto_of': comment, 'veto': ident})
        self.log(f'  vetoed by {login}: suspended')

    def _resume_view(self, number, comments, pr, block, details, comment, record):
        """The view of a ``UNANIMOUS`` decision whose work has not restarted yet, due as one
        ``conclave`` action with ``resume``, or None (logged): the decision must still be the
        one that goes into the prompts (``_conclave_resolution``), not vetoed, on the same text
        of the issue and, on a PR, on the same head. The restart of an implementation also waits
        while the issue is blocked by an open issue (#277), as a ready issue and a retry do: it
        is not due, and ``resumed`` stays unspent until the block closes. The snapshot reads it
        for the plan, and ``_do_conclave`` again just before the restart."""
        issue_number = pr.issue if pr is not None else number
        if record['step'] == 'implement' and pr is None:
            blocked_by = self._dependency_view([number])[number]['blocked_by']
            if blocked_by:
                self.log(f'  #{number}: the unanimous decision {comment} waits to restart the '
                         'implementation, blocked by ' + ', '.join(blocked_by))
                return None
        try:
            issue, _ = self._issue_context(issue_number)
        except UntrustedChange as exc:
            self.log(f'  #{number}: the unanimous decision does not restart the work: '
                     f'{_short(exc)}')
            return None
        found = self._conclave_resolution(
            issue_number, issue['request'], comments if pr is None else issue['thread'],
            comments if pr is not None else None,
            self.gh.pr_head(pr.number) if pr is not None else None)
        if found is None or found[0] != comment:
            self.log(f'  #{number}: the unanimous decision {comment} does not restart the work')
            return None
        members = [record['proposal']['agent'], *(v['agent'] for v in record['votes'])]
        return {'block': block, 'details': details, 'record': record, 'comment': comment,
                'members': members, 'calls': 1, 'resume': True}

    def _conclave_resume(self, number, pr, view):
        """Restart the work blocked by ``view``'s block after a ``UNANIMOUS`` decision (#142),
        once. The changes the agent that stopped left uncommitted are not taken up as work: they
        are saved in the dispatcher's logs, and the record cites them as evidence. The record is
        marked ``resumed`` before anything restarts, so that a restart is never repeated. Then:

        - an implementation runs now, from the base branch (``new_branch``), as the step does;
        - a fix goes back to ``agent:changes``, and the next tick runs it from the PR's head;
        - a review goes back to ``agent:review``: the verdict of the block is not applied again,
          and the next tick reviews the same head in a new round, within ``max_review_rounds``.

        Each step finds the resolution itself, verified again (``_resolution_for``)."""
        lb = self.cfg.labels
        record, comment, block = view['record'], view['comment'], view['block']
        step = record['step']
        issue_number = pr.issue if pr is not None else number
        if step == 'implement':
            if (self.cfg.preflight
                    and preflight_state(self._preflight_view(number, ()))[0] != 'ready'):
                self.log('  the readiness check does not cover the current text: the work does '
                         'not restart')
                return
            opened = [p for p in map(self._devloop_pr, self.gh.open_prs())
                      if p is not None and p.issue == number]
            if opened or self._merged_prs(number):
                self.log('  the issue already has a devloop PR: the work does not restart')
                return
        evidence = None
        if block['kind'] == 'blocked':
            name = f'pr-{pr.number}' if pr is not None else f'issue-{issue_number}'
            diff = self.ws.pending_diff(name, record['commit'])
            if diff:
                file = f'conclave-{name}-{comment}.diff'
                self.ws.save_evidence(file, diff)
                evidence = {'file': file, 'sha256': text_hash(diff),
                            'bytes': len(diff.encode('utf-8'))}
                self.log(f'  uncommitted changes of the agent that stopped saved as {file}')
        record['resumed'] = {'evidence': evidence}
        self._conclave_publish(number, comment, record, pr)
        self.log(f'  conclave {comment}: `{step}` restarts with the unanimous resolution')
        if step != 'implement':
            resume = lb.changes if step == 'fix' else lb.review
            self.gh.edit_labels(number, add=[resume], remove=sorted(lb.operator()))
            return
        self.gh.edit_labels(number, remove=sorted(lb.operator()))
        try:
            self._do_implement(number)
        except (AgentError, GitHubError, WorkspaceError, UntrustedChange) as exc:
            # Escalated as the step's own failure: the conclave action itself never escalates.
            self.log(f'  failed: {exc}')
            self._escalate_failure(Action('implement', number, 'conclave'), None, exc)

    def _human_merge(self, pr):
        """The comment of the latest conclave decision that restarted work of ``pr`` or of its
        issue (#142), read now, or None. After such a restart the merge is a human act for good:
        the record stays ``resumed`` even when its resolution later lapses (a new text, a new
        head) or a trusted user vetoes it, so that devloop never merges the PR itself, whatever
        ``[merge]`` says: the snapshot plans it as mode ``off``, ``_operator_merge_reasons``
        tells why, and ``_do_merge`` checks it again just before a merge. The coordinator's
        reserved merge is the only merge of such a PR that does not need the operator's hand
        (#624, ``_conclave_merge_problem``)."""
        threads = (self.gh.issue_comments(pr.issue), self.gh.pr_comments(pr.number))
        found = [cid for comments in threads for cid, record in self._conclave_records(comments)
                 if record.get('resumed') is not None
                 and isinstance(cid, int) and not isinstance(cid, bool)]
        return max(found) if found else None

    def _conclave_merge_problem(self, pr, thread, latest, quorum):
        """Why the coordinator's reserved merge of ``pr``, whose work restarted after a conclave
        (``_human_merge``), may not go ahead (#624, ADR 0002, precision of 2026-11-24), in
        Italian, or ''. ``thread``: the PR's conversation read now (``_pr_thread``);
        ``latest``: the latest review round, complete and approving the head to merge;
        ``quorum``: the latest review of that head of each reviewer of the quorum. Every
        condition is read now, and what cannot be read refuses it:

        - every decision that restarted work of the PR or of its issue is a ``UNANIMOUS``
          record that holds together (``consistent``), of this issue, and the latest of them
          is the latest unanimous decision (``_conclave_decision``);
        - no trusted user vetoed one of them (``/devloop veto`` written after it, on either
          thread);
        - none of them lapsed: the issue's request is still the one each was taken on, and the
          text of the issue is still trusted (``_issue_context``);
        - the latest round and each review of the quorum come after the restart: their round
          is greater than every round recorded on the PR before the latest of those decisions
          on its thread (any round, for a decision on the issue, whose restart opens the PR).

        The check of the direction, that the resolution stays within the authorized request of
        the issue, is the coordinator's, written in the reason of its command."""
        issue_number = pr.issue
        try:
            issue, _ = self._issue_context(issue_number)
            pr_comments = self.gh.pr_comments(pr.number)
        except UntrustedChange as exc:
            return f'il testo della issue #{issue_number} non è più fidato ({_short(exc)})'
        except GitHubError as exc:
            return (f'le decisioni del conclave della issue #{issue_number} non sono leggibili '
                    f'({_short(exc)})')
        resumed = [(cid, record) for comments in (issue['thread'], pr_comments)
                   for cid, record in self._conclave_records(comments)
                   if record.get('resumed') is not None
                   and isinstance(cid, int) and not isinstance(cid, bool)]
        if not resumed:
            return 'la decisione del conclave che ha fatto ripartire il lavoro non si legge più'
        last = max(cid for cid, _ in resumed)
        for cid, record in resumed:
            if not (record['outcome'] == UNANIMOUS and consistent(record)[0]
                    and record['issue'] == issue_number):
                return (f'la decisione del conclave {cid}, che ha fatto ripartire il lavoro, non '
                        'è una decisione `UNANIMOUS` valida di questa issue')
            if record['issue_hash'] != issue['request']:
                return (f'la decisione del conclave {cid} è decaduta: la richiesta della issue '
                        f'#{issue_number} è cambiata dopo la decisione')
        decision = self._conclave_decision(issue_number, issue['thread'], pr_comments)
        if decision is None or decision['comment'] != last:
            return (f'la decisione del conclave {last}, che ha fatto ripartire il lavoro, non è '
                    'più l\'ultima decisione unanime sulla issue e sulla PR')
        first = min(cid for cid, _ in resumed)
        vetoes = sorted(v for comments in (issue['thread'], pr_comments)
                        for v in self._veto_commands(comments, issue_number) if v[0] > first)
        if vetoes:
            ident, login, _ = vetoes[0]
            return (f'`{login}` ha annullato con `{VETO_COMMAND}` (commento {ident}) una '
                    'decisione del conclave che ha fatto ripartire il lavoro')
        cids = {cid for cid, _ in resumed}
        positions = [p for p, c in enumerate(thread) if comment_ref(c) in cids]
        before = max([r['round'] for p, r in self._marker_entries(pr, thread, 'review')
                      if positions and p < max(positions)], default=0)
        if (not latest or latest[0]['round'] <= before
                or any(r['round'] <= before for r in quorum.values())):
            return (f'nessun round di review completo dopo la ripartenza del lavoro (decisione '
                    f'del conclave {last}) ha approvato l\'head con il quorum del livello')
        return ''

    def _conclave_pr_state(self, pr, comments):
        """What the conclave decisions of a PR devloop works on change in the snapshot (#142),
        from its thread ``comments``: ``pr.veto``, a veto of a trusted user that the dispatcher
        has not applied yet, and ``pr.rereview`` for a review block whose work restarted, so
        that the verdict of the block is not applied again and the same head gets a new review.
        The issue's comments are read only when the PR's thread holds a decision, or one of its
        records says a step ran with one."""
        used = any(r.get('conclave') is not None for r in [pr.meta, *pr.fixes, *pr.reviews])
        if not used and not any(r['outcome'] == UNANIMOUS
                                for _, r in self._conclave_records(comments)):
            return
        decision = self._conclave_decision(pr.issue, self.gh.issue_comments(pr.issue), comments)
        if decision is None:
            return
        if decision['veto'] is not None:
            if not decision['spent']:
                ident, login, reason = decision['veto']
                pr.veto = {'comment': ident, 'by': login, 'reason': reason,
                           'conclave': decision['comment']}
            return
        record, latest = decision['record'], current_round(pr.reviews)
        block = record['block']
        if (record.get('resumed') is not None and block['kind'] == 'review' and latest
                and latest[0]['round'] == block['round'] and block['head'] == pr.head_sha):
            pr.rereview = True

    def _do_veto(self, pr):
        """Apply a trusted user's veto of the latest conclave decision on an open PR devloop
        works on (#142), read again now: the PR goes to ``agent:needs-operator`` with an
        escalation of class ``conclave_veto``, and the steps after it are suspended. A PR
        merged or closed meanwhile is left as it is: the merge stays a human act."""
        lb = self.cfg.labels
        fresh = self.gh.pr_state(pr.number)
        if fresh.get('state', 'OPEN') != 'OPEN':
            self.log('  the PR is no longer open: the veto has no effect')
            return
        decision = self._conclave_decision(pr.issue, self.gh.issue_comments(pr.issue),
                                           self.gh.pr_comments(pr.number))
        if decision is None or decision['veto'] is None or decision['spent']:
            self.log('  no veto to apply')
            return
        self._veto_suspend(pr.number, decision['comment'], decision['veto'],
                           [lb.review, lb.changes, lb.ready_to_merge],
                           'i passi successivi della PR sono sospesi.')


def _diff_section(diff):
    """The prompt section with the changes the agent that stopped left uncommitted."""
    if not diff.strip():
        return ''
    shown = diff if len(diff) <= MAX_DIFF else diff[:MAX_DIFF] + '\n[... truncated ...]'
    return ('## Uncommitted changes of the agent that stopped (read only)\n\n'
            'They are not in the checkout: they are what the agent left in its worktree when it '
            'stopped, against the commit of reference.\n\n~~~~~diff\n' + shown + '\n~~~~~')
