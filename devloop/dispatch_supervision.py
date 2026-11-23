"""Supervision and commands of the dispatcher: the mixin ``SupervisionMixin`` of ``Dispatcher``.

Moved from ``devloop/dispatcher.py`` without changes (#293, step S3 of #261): the escalations the
supervisor decides (``_supervised``, ``_supervisor_view``, ``_to_operator``, ``_do_supervise``,
``_accept_defaults``, ``_decide``), the hold, release and fix commands of a PR
(``_hold_command``, ``_fix_requests``) and the reading of a PR's thread (``_read_thread``), with
the module-level definitions they need. ``Dispatcher`` inherits the methods; since #297 (step S7)
the module-level names are imported from here, not re-exported by ``devloop/dispatcher.py``.
"""

from dataclasses import replace
from datetime import datetime

from .activity import iso
from .agents import AgentError
from .apps import COORDINATOR, DISPATCHER
from .budget import BUDGET, extension_text
from .budget import grantable as budget_grantable
from .config import same_agent
from .context import REREVIEW_COMMAND, operator_comments, requests_rereview
from .github import GitHubError
from .journal import JournalError
from .ledger import entries as ledger_entries
from .markdown import (PREFLIGHT_RESUME, RESUME, carries_record, operator_retry_text,
                       read_marker, render_escalation, render_supervision)
from .planning import (FIX_COMMAND, Action, PullRequest, closed_by_first, findings_decreasing,
                       label_names, last_counted_ci, missing_verdicts, operator_blocked,
                       waiver)
from .policy import ZERO_LEVEL, profile_of
from .review import current_round, current_verdict, finding_state, owner_of
from .supervision import (ACCEPT_DEFAULTS, BUDGET_GRANTABLE, CAPACITY, CAPACITY_REFUSED, DECIDE,
                          DESIGN_LABEL, ESCALATE, GRANT_BUDGET, GRANT_ROUND, ISSUE_STEPS,
                          MAX_CAPACITY, MAX_MERGE_BUDGET, MAX_RETRIES, MERGE, MERGE_BUDGET,
                          MERGED_ISSUE, PREFLIGHT_DEFAULT, REREVIEW, RETRY, RETRY_STEPS,
                          ROUND_GRANTABLE, SYNC_FAILED, TOOL_FAILURE, capacity_arguments,
                          changed_input, command_line, coordinator_command,
                          coordinator_refusal, critical,
                          merge_budget_arguments, preflight_class, retries_done,
                          supervisor_command)
from .workspace import WorkspaceError

# Hold and release a merge in ``auto`` mode (#172): the first line of a comment, after the
# attribution header, from the supervisor's identity or a trusted user.
HOLD_COMMAND = '/devloop hold'
RELEASE_COMMAND = '/devloop release'
# The label a PR resumes from when the supervisor retries a step that failed on it (#182).
RESUME_STEP = {'review': 'review', 'fix': 'changes', 'sync': 'review'}
# The titles of the dispatcher's trace of a command: on an escalation to the supervisor (#182)
# or to the operator (#321).
SUPERVISOR_TITLE = 'Decisione del supervisore'
OPERATOR_TITLE = 'Comando su un\'escalation all\'operatore'
# The title of the dispatcher's trace of a ``/devloop capacity`` command (#412).
CAPACITY_TITLE = 'Limite di capacità temporaneo'
CAPACITY_USAGE = (f'Il comando si scrive come `/devloop {CAPACITY} <chiave> <valore> <durata> '
                  '<motivo>`, come prima riga di un commento su questa issue: la chiave è '
                  + ' o '.join(f'`{key}`' for key in MAX_CAPACITY) + ', la durata `<N>h` o '
                  '`<N>m`, il motivo, che può continuare nelle righe successive, riporta o cita '
                  'l\'analisi delle cause.')
# The titles of the dispatcher's trace of a delegated merge and of an extension of its daily cap
# (#486), and how the extension is written.
MERGE_TITLE = 'Merge delegato al supervisore'
COORDINATOR_MERGE_TITLE = 'Merge riservato del coordinatore'  # #529
MERGE_BUDGET_TITLE = 'Merge delegati: estensione del tetto giornaliero'
MERGE_BUDGET_USAGE = (f'Il comando si scrive come `/devloop {MERGE_BUDGET} <N> <motivo>`, come '
                      'prima riga di un commento di un utente fidato su questa issue: `N` da 1 a '
                      f'{MAX_MERGE_BUDGET} merge delegati in più per il giorno UTC in cui è '
                      'scritto, il motivo obbligatorio.')


def _epoch(stamp):
    """The seconds since the epoch of an ISO 8601 time in UTC as GitHub and devloop write it
    (``2026-11-19T10:00:00Z``), or None."""
    if not isinstance(stamp, str) or not stamp.endswith('Z'):
        return None
    try:
        return datetime.fromisoformat(stamp[:-1] + '+00:00').timestamp()
    except ValueError:
        return None


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


class UntrustedChange(RuntimeError):
    """The issue text changed after a trusted user approved it with the ready label."""


class SupervisionMixin:
    """The methods of ``Dispatcher`` for supervision and commands (#293). They use the
    dispatcher state and its other methods through ``self``."""

    def _supervisor_implemented(self, pr):
        """Whether the supervisor's agent implemented ``pr`` (ADR 0002, principle 4). The agent
        that actually ran is compared, not only the key (``same_agent``, ADR 0005, P5-3, #407):
        the ``kind``, the ``model`` or the provider of the profile in the PR's marker (of the
        configured agent the marker names, for a marker without a profile) equal to those of the
        supervisor, the supervisor actor (``[roles.supervisor]`` as a table, P5-4, #408, with the
        ``provider`` it declares) or the agent ``roles.supervisor`` names. With an agent key, a
        PR whose implementer is that key is the supervisor's in any case. A PR whose agent
        cannot be read counts as the supervisor's, so it goes to the operator. Since #527 it
        holds back only an extension of the budget (``_supervised``): the escalations of process
        the supervisor decides on its agent's PRs too."""
        cfg = self.cfg
        implementer = pr.meta.get('implementer')
        actor = cfg.supervisor_actor
        if actor is None:
            if implementer == cfg.supervisor:
                return True
            # With the agent key of before, too, the agent is compared and not only the key
            # (ADR 0005, P5-3, #407): the keys renamed by role (``claude-implementer``) never
            # equal the supervisor's, and the check must not stop firing with them.
            actor = cfg.agents.get(cfg.supervisor)
            if actor is None:
                return False
        profile = profile_of(pr.meta)
        if profile is None and implementer in cfg.agents:
            agent = cfg.agents[implementer]
            profile = {'kind': agent.kind, 'model': agent.model}
        if profile is None:
            return True
        return same_agent(profile, actor)

    def _supervised(self, number, cls, step, pr=None, reviewer=None, key=None, any_merge=False):
        """``(route, why)`` of an escalation of class ``cls`` raised by ``step`` on issue or PR
        ``number`` (``pr``: the PR, None for an issue). ``route`` is None when it goes to the
        operator; otherwise the payload of an escalation the supervisor decides: the step, the
        key of its input and, for a PR, the label a retry resumes from. ``why`` says why a
        non-critical class in ``auto`` still goes to the operator, '' otherwise. ``key``: for an
        issue, the key of the input the step ran on (``_issue_key``), bound before it ran; an
        issue step without one goes to the operator.

        Only in merge mode ``auto`` with a supervisor, and only when nothing makes the
        escalation critical: a PR whose own merge mode is not ``auto`` (the override of its
        level, an ``operator_only_paths`` path), a ``budget_grantable`` escalation on a PR the
        supervisor's agent implemented, an issue opened by someone else than a trusted user or a
        devloop app, an issue text no longer approved by ``agent:ready``, the ``phase:design``
        label, or anything that cannot be verified. With ``any_merge`` the PR's own merge mode is
        not checked: the supervisor's rereview (#491) does not touch the merge, which stays where
        that mode puts it.

        Every other non-critical class goes to the supervisor on its agent's PRs too (#527,
        principle 4 of ADR 0002 as clarified on #524): ``retry``, ``rereview``, ``grant-round``
        within the caps, ``accept-defaults`` and ``decide`` are escalations of process, which
        never override nor replace a reviewer's verdict; a verdict that asks for the operator
        is a critical class, which never reaches the supervisor. This replaces option (a) of
        ADR 0005, P5-2 (#406), and with it ``[delegations] retry_own_prs``, now without
        effect."""
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
                if cls == BUDGET_GRANTABLE and self._supervisor_implemented(pr):
                    # The escalations of process the supervisor decides on its agent's PRs too
                    # (#527) are those the operator listed on #524; an extension of the budget
                    # is not among them.
                    return None, ('il supervisore ha implementato questa PR, e la concessione di '
                                  'budget non è fra le escalation di processo che decide anche '
                                  'sulle PR del proprio agent')
                mode = 'auto' if any_merge else self._merge_mode(pr)
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
                return self._retry_payload(step, pr, reviewer), ''
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

    def _retry_payload(self, step, pr=None, reviewer=None, key=None):
        """What a retry of ``step`` needs, recorded in its escalation: the step, the key of its
        input and, for a PR (``pr``), the label a retry resumes from. None when ``step`` is not
        one a retry runs again or, for an issue, when ``key`` (``_issue_key``, bound before the
        step ran) is missing. An escalation to the operator records it too (#321)."""
        lb = self.cfg.labels
        if step not in RETRY_STEPS:
            return None
        if pr is None:
            if not isinstance(key, dict) or key.get('step') != step:
                return None
            return {'step': step, 'key': key}
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
        return {'step': step, 'key': key, 'resume': resume}

    def _retryable(self, escalation, is_pr):
        """Whether ``escalation`` records a step a retry can run again on an issue
        (``is_pr`` false) or a PR: a known step of the right kind, with its key."""
        key, step = escalation.get('key'), escalation.get('step')
        return (isinstance(key, dict) and step in RETRY_STEPS and key.get('step') == step
                and is_pr != (step in ISSUE_STEPS)
                and not (step == 'preflight' and not self.cfg.preflight))

    @staticmethod
    def _issue_key(step, base, request):
        """The key of the input of an issue's ``step``: the commit of the base its checkout
        started from and the hash of the request its prompt got, both taken before the step
        runs, so that a base or a text that changes during the turn is not mistaken for the
        input that failed."""
        return {'step': step, 'base': base, 'request': request}

    def _current_key(self, number, key, pr):
        """The key ``key`` would have now, for the same step (and reviewer) on issue or PR
        ``number``: a retry of a superseded input is refused (``changed_input``, which leaves
        out the base of an implementation, #475)."""
        if pr is not None:
            current = {'step': key.get('step'), 'head': self.gh.pr_head(number)}
            if 'reviewer' in key:
                current['reviewer'] = key['reviewer']
            return current
        history = self.gh.issue_history(number)
        return {'step': key.get('step'), 'base': self.ws.base_head(),
                'request': self._request_hash(history, self.gh.issue_comments(number))}

    def _operator_anchor(self, comments):
        """``(start, anchor)`` of an item in ``agent:needs-operator`` (#321), from its
        ``comments``: the position after which its commands count, and the record that put it
        there, ``{kind, record}``. The record is the latest of the dispatcher's notices (an
        operator merge counts from the promotion it was last written for, since a head promoted
        again edits its notice), its policy records of an escalation (``review_level``,
        ``level_zero``) and the failed readiness checks. When the item moved on after it (a
        command applied, a passed check, a review, fix or sync, a policy record of no
        escalation), the label is no longer that record's: ``kind`` is None and the commands
        count after what moved it. ``origin``, for a supervisor's escalation handed to the
        operator (``supervisor_escalated``, ``supervisor_refused``), is the class of the
        escalation the supervisor had."""
        policy = self._dispatcher_records(comments, 'policy')
        promotions = [p for p, r in policy if r.get('event') == 'promoted']
        anchors = []
        for position, record in self._dispatcher_records(comments, 'notice'):
            promotion = record.get('promotion')
            if (record.get('event') == 'operator_merge' and isinstance(promotion, int)
                    and not isinstance(promotion, bool) and 0 < promotion <= len(promotions)):
                position = max(position, promotions[promotion - 1])
            anchors.append((position, 'notice', record))
        anchors += [(p, 'policy', r) for p, r in policy if r.get('class')]
        checks = self._preflight_records(comments)
        anchors += [(p, 'preflight', r) for p, r in checks if not r['ready']]
        anchors.sort(key=lambda entry: entry[0])
        start, kind, record = anchors[-1] if anchors else (-1, None, {})
        moved = {p for p, r in checks if r['ready']}
        moved |= {p for p, r in policy if not r.get('class')}
        # The records of an extension of the budget envelope completed on an item already with
        # the operator (``trace_only``, #411) move nothing: the item stays the operator's.
        moved |= {p for p, r in self._dispatcher_records(comments, 'supervision')
                  if r.get('outcome') in ('applied', 'accepted', 'decided', 'granted')
                  and r.get('trace_only') is not True}
        for position, comment in enumerate(comments):
            body = comment.get('body', '')
            if any(read_marker(body, kind) is not None for kind in ('review', 'fix', 'sync')) \
                    and self._writes_records((comment.get('author') or {}).get('login')):
                moved.add(position)
        after = [p for p in moved if p > start]
        if after:
            return max(after), {'kind': None, 'record': {}}
        anchor = {'kind': kind, 'record': record}
        if kind == 'notice' and record.get('class') in ('supervisor_escalated',
                                                        'supervisor_refused'):
            handed = [r for p, r in self._dispatcher_records(comments, 'notice', 'escalation')
                      if p < start and r.get('to') == 'supervisor']
            if handed and isinstance(handed[-1].get('class'), str):
                anchor['origin'] = handed[-1]['class']
        return start, anchor

    def _supervisor_view(self, number, comments, operator=False):
        """The command still to apply on issue or PR ``number``, from its ``comments``:
        ``{command, reason, by, comment, step, anchor}`` or None. ``anchor`` is the record the
        command answers, ``{kind, record}``; ``refusal``, when present, the answer to a command
        that is not applicable (#321): it is only answered, and spent.

        On an item in ``agent:needs-supervisor`` (``operator`` false) only a command written
        after the latest escalation of the dispatcher counts, in merge mode ``auto`` and from
        the supervisor's identity or a trusted user (``_may_hold``). On an item in
        ``agent:needs-operator`` (#321) a command counts after the record that put it there
        (``_operator_anchor``), in any merge mode, from a trusted user; one from the
        supervisor's identity is
        answered as not applicable, except ``/devloop merge`` (#486), a delegation to the
        supervisor that ``_operator_command`` applies. A dispatcher ``supervision`` record spends the command it
        handled, whose comment id it carries, and every command before it. Among the commands
        still waiting the latest applicable one wins; without one, the latest not applicable
        (outside ``auto``, a decide or an escalate without a text, the supervisor on an
        operator's escalation) gets an answer. A command from anyone else is ignored and only
        logged. devloop's own records are never commands. A ``/devloop rereview`` counts only
        from the supervisor's identity (#491): a trusted user's is the request of a new review
        it always was (``requests_rereview``), and is skipped here.

        The coordinator's identity (``[roles.coordinator]``, #528) has a closed perimeter: a
        command of ``COORDINATOR_DELEGATED`` counts as a trusted user's, its rereview included
        (skipped here); any other, reserved to the operator or not listed, ``merge`` and
        ``escalate`` included, is answered as not applicable (``coordinator_refusal``)."""
        if operator:
            start, anchor = self._operator_anchor(comments)
            latest = anchor['record']
        else:
            escalations = self._dispatcher_records(comments, 'notice', 'escalation')
            if not escalations:
                return None
            (start, latest), kind = escalations[-1], 'notice'
            anchor = {'kind': kind, 'record': latest}
        spent = None
        for position, record in self._dispatcher_records(comments, 'supervision'):
            applied = record.get('comment')
            if isinstance(applied, int) and not isinstance(applied, bool):
                spent = applied if spent is None else max(spent, applied)
            else:
                start = max(start, position)  # a record without the id spends what precedes it
        found = refused = supervised = None
        for comment in comments[start + 1:]:
            body = comment.get('body') or ''
            login = (comment.get('author') or {}).get('login')
            parsed = None if carries_record(body) else supervisor_command(body)
            if parsed is None and not carries_record(body) and self._is_coordinator(login):
                # The coordinator's commands no supervisor command names (#528): the reserved
                # ones, such as ``reconcile``, and those not listed, such as ``veto``, are
                # refused below; the delegated ones read elsewhere (``recheck``, ``fix``,
                # ``hold``, ``release``) are none of this reader's.
                parsed = coordinator_command(body)
                if parsed is not None and coordinator_refusal(parsed[0]) is None:
                    continue
            if parsed is None:
                continue
            ident = comment.get('id')
            if spent is not None and (not isinstance(ident, int) or ident <= spent):
                continue
            command, reason = parsed
            if command == REREVIEW and (login in self.cfg.trusted_users
                                        and login != self._supervisor_login()
                                        or self._coordinator_may(login, REREVIEW)):
                # A trusted user's rereview is the request of a new review it always was
                # (``requests_rereview``), never a command of an escalation (#491); so is the
                # coordinator's, which counts as a trusted user's (#528, ``_read_thread``).
                continue
            item = {'command': command, 'reason': reason, 'by': login, 'comment': ident,
                    'step': latest.get('step'), 'anchor': anchor}
            # The coordinator's identity (#528): a delegated command counts as a trusted
            # user's; any other is answered as refused, the closed perimeter.
            delegated = self._coordinator_may(login, command)
            outside = (coordinator_refusal(command) if self._is_coordinator(login)
                       else None)
            # The supervisor's rereview also counts on an escalation to the operator (#491), of
            # a PR whose merge is the operator's: ``_operator_rereview`` decides whether it
            # applies. A trusted user's command waiting there wins over it.
            rereview = (operator and command == REREVIEW and login is not None
                        and login == self._supervisor_login())
            if outside:
                why, refusal = outside
            elif operator and command == MERGE:
                # A delegation of the operator to the supervisor (#486): the supervisor's
                # identity writes it on an operator merge, and ``_operator_command`` answers a
                # trusted user's; the coordinator's is its reserved merge (#529); anyone else's
                # is ignored.
                why, refusal = (
                    ('the author is neither the supervisor, the coordinator nor a trusted user',
                     None)
                    if not (self._may_hold(login) or delegated)
                    else (f'malformed, expected `/devloop {command} <sha> <reason>`',
                          f'manca il testo, il comando va scritto come `/devloop {command} <sha> '
                          '<motivo>`') if reason is None
                    else (None, None))
            elif operator:
                allowed = login in self.cfg.trusted_users or rereview or delegated
                why, refusal = (
                    ('the author is not a trusted user', None) if not allowed
                    and not self._may_hold(login)
                    else ('the escalation is the operator\'s, not the supervisor\'s',
                          'l\'escalation è affidata all\'operatore, non al supervisore: decide '
                          'un utente fidato') if not allowed
                    else (f'malformed, expected `/devloop {command} <text>`',
                          f'manca il testo, il comando va scritto come `/devloop {command} '
                          '<testo>`') if reason is None
                    else (None, None))
            else:
                why, refusal = (
                    ('the author is neither the supervisor nor a trusted user', None)
                    if not (self._may_hold(login) or delegated)
                    else (f'merge mode is "{self.cfg.merge.mode}", not "auto"',
                          f'la modalità di merge è `{self.cfg.merge.mode}`, non `auto`, quindi '
                          'i comandi del supervisore non valgono')
                    if self.cfg.merge.mode != 'auto'
                    else (f'malformed, expected `/devloop {command} <text>`',
                          f'manca il testo, il comando va scritto come `/devloop {command} '
                          '<testo>`') if reason is None
                    else (None, None))
            if why:
                self.log(f'  #{number}: ignored `/devloop {command}` by {login}: {why}')
                if refusal:
                    refused = dict(item, reason=reason or '', refusal=refusal)
                continue
            if rereview:
                supervised = item
                continue
            found = item
        return found or supervised or refused

    def _to_operator(self, number, reason, cls, remove=(), retry=None):
        """Hand an item in ``agent:needs-supervisor`` to the operator: a critical escalation of
        class ``cls``. ``retry``: the step of the supervisor's escalation, its key and what a
        retry resumes, recorded again so that the operator may still retry it (#321)."""
        lb = self.cfg.labels
        self.gh.comment(number, render_escalation(reason, cls=cls, retry=retry))
        self.gh.edit_labels(number, add=[lb.needs_operator],
                            remove=[lb.needs_supervisor, *remove])

    def _do_supervise(self, target, merge=False):
        """Apply the supervisor command waiting on an issue or PR in ``agent:needs-supervisor``
        (#182), read again now. Each command leaves a dispatcher ``supervision`` record with the
        command, its author and the outcome, written before the labels change: a record whose
        labels are lost spends the command, so that a retry is never run twice for one
        command.

        - ``escalate``: the item goes to ``agent:needs-operator`` with the reason.
        - ``accept-defaults``: only for a ``preflight_default`` escalation (``_accept_defaults``).
        - ``decide``: only for a ``preflight_default`` escalation, with the supervisor's text
          (``_decide``, #258).
        - ``grant-round``: only for a ``round_grantable`` escalation of a PR (``_grant_round``,
          #405).
        - ``grant-budget``: only for a ``budget_grantable`` escalation of an issue or a PR
          (``_grant_budget``, #411).
        - ``rereview``: only from the supervisor's identity, for a ``tool_failure`` escalation
          of a PR's review whose latest round lacks the verdict of a reviewer of the quorum: a
          new round with the current configuration (``_rereview``, #491). Its input is checked
          again as for a retry.
        - ``retry``: only for ``tool_failure`` and ``sync_failed``, and only while the input of
          the step is the one that failed (the key of the escalation; for ``implement`` its
          request, not its base, #475: ``retry_input``). A PR goes back to the
          label of its step, and the next tick runs it; an issue's step runs now. Retries
          write no review record, so they count no round. A retry of an issue blocked by an
          open issue (#277) waits, unspent, until the last block closes. After
          ``MAX_RETRIES`` retries for the same key (counted per reviewer, ``retries_done``) the
          next one is not run and the item goes to ``agent:needs-operator``, as it does when
          what made the escalation non-critical (``_supervised``) no longer holds.

        An escalation whose class is critical or unknown, or that was not routed to the
        supervisor, accepts no command: the item goes to ``agent:needs-operator``. A command
        that is not applicable (``_supervisor_view``) is answered and spent, and the item stays
        where it is (#321). An item in ``agent:needs-operator`` gets ``_operator_command``. The
        escalations to the operator keep the step a retry runs and its key (#321).

        Before any command, an extension of the budget envelope that the journal recorded and a
        stopped or failed run did not trace (``_pending_extension``, #411) is completed with its
        own command (``_finish_extension``), whatever command is the latest now, also on an item
        in ``agent:needs-operator``, where only its records are written.

        ``merge``: the action is a ``delegated_merge``, planned in the merge lane (#486). A
        ``/devloop merge`` to apply on a PR, read now by an action the plan did not put in that
        lane (``merge`` false: the command was written after the plan), is not applied: it
        stays pending, unspent, and a later tick plans it as a ``delegated_merge``, so that it
        never merges alongside another merge."""
        lb = self.cfg.labels
        is_pr = isinstance(target, PullRequest)
        number = target.number if is_pr else target
        if is_pr:
            fresh = self.gh.pr_state(number)
            labels = label_names(fresh.get('labels') or [])
            comments = self._pr_thread(number)
        else:
            labels = label_names(self.gh.issue(number).get('labels') or [])
            comments = self.gh.issue_comments(number)
        operator = operator_blocked(labels, lb)
        if not operator and lb.needs_supervisor not in labels:
            self.log('  in neither agent:needs-supervisor nor agent:needs-operator any more; '
                     'nothing to do')
            return

        def tracer(command, cls):
            """The ``trace(outcome, text, **extra)`` of the ``supervision`` record of
            ``command`` on an escalation of class ``cls``."""
            name, by = command['command'], command['by']

            def trace(outcome, text, **extra):
                record = dict(command=name, by=by, reason=command['reason'] or '',
                              comment=command['comment'], outcome=outcome, **extra)
                record['class'] = cls if isinstance(cls, str) else None
                coordinator = name == MERGE and self._is_coordinator(by)
                if coordinator:
                    # The coordinator's reserved merge (#529): the summary lists it apart.
                    record['role'] = COORDINATOR
                self.gh.comment(number, render_supervision(
                    record, f'`/devloop {name}` di `{by}`: {text.strip()}',
                    COORDINATOR_MERGE_TITLE if coordinator and operator
                    else MERGE_TITLE if name == MERGE and operator
                    else OPERATOR_TITLE if operator else SUPERVISOR_TITLE))
                self.log(f'  {name} by {by}: {outcome}')
            return trace

        # An extension of the budget envelope the journal recorded, whose records a stopped or
        # failed run did not write (#411), is completed first, whatever command is the latest
        # now, whatever changed since and wherever the item is: it is part of the envelope of
        # the story already. Its step resumes only from its own escalation, still the
        # supervisor's; on an item with the operator only the records are written.
        pending = self._pending_extension(number, comments, is_pr, operator)
        if pending is not None:
            escalation, command = pending['escalation'], pending['command']
            return self._finish_extension(number, is_pr, comments, escalation, command,
                                          tracer(command, escalation.get('class')),
                                          pending['grant'], pending['daily'], recovered=True,
                                          resume=pending['resume'], operator=operator)
        command = self._supervisor_view(number, comments, operator)
        if command is None:
            self.log('  no command to apply')
            return
        if not merge and is_pr and command['command'] == MERGE and not command.get('refusal'):
            # The plan's own test (``planning.plan``): this command runs only in the merge lane.
            self.log('  `/devloop merge` written after the plan: it waits, unspent, for the '
                     'merge lane of a later tick')
            return
        escalation = command['anchor']['record']
        cls = escalation.get('class')
        name, by = command['command'], command['by']
        # The step a retry runs again stays recorded on the way to the operator (#321), only for
        # the classes whose step the operator may retry: the failures a retry admits, and a turn
        # the budget refused (#411), which the operator may retry as when the refusal goes to the
        # operator directly. The key of a preflight_default or merged_issue escalation only
        # routes it.
        carry = ({k: escalation[k] for k in ('step', 'key', 'resume') if k in escalation}
                 if cls in (TOOL_FAILURE, SYNC_FAILED, BUDGET_GRANTABLE)
                 and self._retryable(escalation, is_pr) else None)
        # A tool failure of an issue loses agent:ready on its way to the operator, as any failed
        # step does; the readiness check keeps it, and so does a readiness check the operator
        # may retry, which runs again only with agent:ready.
        keep_ready = is_pr or cls == PREFLIGHT_DEFAULT
        human_remove = [] if keep_ready else [lb.ready]
        carried_remove = [] if keep_ready or (carry and carry['step'] == 'preflight') \
            else [lb.ready]
        trace = tracer(command, cls)
        stays = 'Il comando è speso; l\'escalation resta al supervisore.'
        if command.get('refusal'):
            where = 'la PR' if is_pr else 'l\'issue'
            how = (f'Il comando è speso; {where} resta in `{lb.needs_operator}`.\n\n'
                   + self._operator_resume(command['anchor'], is_pr) if operator
                   else f'Il comando è speso; {where} resta in `{lb.needs_supervisor}`.\n\n'
                   + self._supervisor_resume(escalation, is_pr))
            return trace('refused', f'non applicabile: {command["refusal"]}. {how}')
        if name in (CAPACITY, MERGE_BUDGET) or name == MERGE and not operator:
            # A temporary capacity limit (#412) and an extension of the delegated merges (#486)
            # are never a decision on an escalation; a delegated merge applies only to a PR
            # whose merge is the operator's (#486).
            where = 'la PR' if is_pr else 'l\'issue'
            label = lb.needs_operator if operator else lb.needs_supervisor
            resume = (self._operator_resume(command['anchor'], is_pr) if operator
                      else self._supervisor_resume(escalation, is_pr))
            what = ('vale solo su una PR pronta il cui merge spetta all\'operatore, non su '
                    'un\'escalation al supervisore' if name == MERGE
                    else 'vale solo sulla issue del riepilogo (`[report] issue`), non su '
                         'un\'escalation')
            return trace('refused', f'non applicabile: `/devloop {name}` {what}. Il '
                         f'comando è speso; {where} resta in `{label}`.\n\n{resume}')
        if operator and name == REREVIEW:
            # Only the supervisor's reaches here: a trusted user's is no command (#491).
            return self._operator_rereview(target, number, labels, fresh if is_pr else None,
                                           comments, command, trace)
        if operator:
            return self._operator_command(target, number, labels, comments, command, trace)
        if critical(cls) or escalation.get('to') != 'supervisor':
            trace('refused', 'rifiutato. L\'escalation è critica (classe '
                  f'`{cls}`) o non è affidata al supervisore: decide l\'operatore.')
            return self._to_operator(number, f'Il supervisore non può decidere questa escalation '
                                     f'(classe `{cls}`): `/devloop {name}` di `{by}` è stato '
                                     'rifiutato.', 'supervisor_refused', carried_remove, carry)
        if name == ESCALATE:
            reason = ' '.join(command['reason'].split())
            trace('escalated', f'l\'escalation passa all\'operatore: {reason}')
            return self._to_operator(number, f'Il supervisore `{by}` passa all\'operatore '
                                     f'l\'escalation di classe `{cls}`: {reason}',
                                     'supervisor_escalated', carried_remove, carry)
        if name == GRANT_ROUND:
            if not is_pr or cls != ROUND_GRANTABLE:
                return trace('refused', 'rifiutato: vale solo per un\'escalation '
                             f'`{ROUND_GRANTABLE}` di una PR al limite dei round. {stays}\n\n'
                             + self._supervisor_resume(escalation, is_pr))
            return self._grant_round(target, number, fresh, labels, comments, command,
                                     escalation, trace)
        if name == GRANT_BUDGET:
            if cls != BUDGET_GRANTABLE:
                return trace('refused', 'rifiutato: vale solo per un\'escalation '
                             f'`{BUDGET_GRANTABLE}` di un turno rifiutato dall\'envelope di '
                             f'budget. {stays}\n\n' + self._supervisor_resume(escalation, is_pr))
            return self._grant_budget(target, number, fresh if is_pr else None, labels,
                                      comments, command, escalation, trace, human_remove)
        if name in (ACCEPT_DEFAULTS, DECIDE):
            if is_pr or cls != PREFLIGHT_DEFAULT:
                return trace('refused', 'rifiutato: vale solo per un\'escalation `preflight_default` '
                             f'del controllo di prontezza. {stays}\n\n'
                             + self._supervisor_resume(escalation, is_pr))
            if name == DECIDE:
                return self._decide(number, labels, comments, trace, command['reason'],
                                    coordinator=self._is_coordinator(by))
            return self._accept_defaults(number, labels, comments, trace)
        if cls not in (TOOL_FAILURE, SYNC_FAILED):
            return trace('refused', 'rifiutato: vale solo per le escalation `tool_failure` e '
                         f'`sync_failed`. {stays}\n\n' + self._supervisor_resume(escalation, is_pr))
        if name == REREVIEW and not (is_pr and cls == TOOL_FAILURE
                                     and escalation.get('step') == 'review'):
            return trace('refused', 'rifiutato: vale solo per un\'escalation `tool_failure` della '
                         f'review di una PR. {stays}\n\n'
                         + self._supervisor_resume(escalation, is_pr))
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
        if not self._retryable(escalation, is_pr):
            trace('refused', 'rifiutato: l\'escalation non registra un passo ripetibile.')
            return self._to_operator(number, 'L\'escalation non registra un passo che il '
                                     f'supervisore possa ripetere: `/devloop {name}` di `{by}` è '
                                     'stato rifiutato.', 'supervisor_refused', human_remove)
        current = self._current_key(number, key, target if is_pr else None)
        # The base of an implementation does not bind its retry (#475): the step starts again
        # from the current base with the saved work of its timed-out turn reapplied.
        changed = changed_input(current, key)

        def superseded():
            return trace('refused', 'rifiutato: l\'input del passo è cambiato dopo '
                         'l\'escalation (' + ', '.join(f'`{k}`' for k in changed)
                         + f'), quindi il comando si riferisce a un input superato. {stays}\n\n'
                         + self._supervisor_resume(escalation, is_pr, superseded=True))

        # What made the escalation non-critical is checked again, on data read now: a
        # phase:design label, an untrusted text or author, the PR's own merge mode or its
        # implementer may have changed while it waited. A PR is checked on its labels and head
        # read now and on its thread rebuilt from the comments read now, as the merge does: the
        # snapshot does not read the thread of a PR in agent:needs-supervisor, and without its
        # review and policy records the level recorded earlier would be lost. A PR whose head
        # moved is refused first: its merge mode would be the one of another diff.
        if is_pr:
            if changed:
                return superseded()
            target = self._fresh_pr(target, labels, fresh, comments)
        route, why = self._supervised(number, cls, step, target if is_pr else None,
                                      key.get('reviewer'), key=key)
        if route is None:
            why = why or 'la modalità di merge non è più `auto` con un supervisore'
            trace('refused', f'rifiutato: l\'escalation è diventata critica ({why}).')
            return self._to_operator(number, 'Il supervisore non può più decidere questa '
                                     f'escalation: {why}. `/devloop {name}` di `{by}` è stato '
                                     'rifiutato.', 'supervisor_refused', carried_remove, carry)
        if changed:
            return superseded()
        if name == REREVIEW:
            return self._rereview(number, target, command, escalation, trace)

        def limit(done):
            trace('limit', f'non eseguito: il passo `{step}` è già stato ripetuto {done} volte '
                  'per lo stesso input.', key=key)
            self._to_operator(number, f'Il passo `{step}` è fallito di nuovo dopo {done} '
                              f'tentativi autorizzati dal supervisore (al massimo '
                              f'{MAX_RETRIES}): `/devloop {name}` di `{by}` non è stato '
                              'eseguito.', 'retry_limit', human_remove)

        self._apply_retry(number, is_pr, comments, escalation, [lb.needs_supervisor], trace,
                          limit)

    def _do_delegated_merge(self, target):
        """A ``/devloop merge`` of the supervisor on a PR (#486), planned in the merge lane
        since it writes to the base branch: applied as any command (``_do_supervise``)."""
        return self._do_supervise(target, merge=True)

    def _rereview(self, number, pr, command, escalation, trace, operator=False, resume=''):
        """Apply the supervisor's ``/devloop rereview <reason>`` on PR ``number`` in a
        ``tool_failure`` escalation of its review (#491, the operator's decision recorded in ADR
        0005), with ``pr`` and its thread read now (``_fresh_pr``) and the conditions of the
        escalation verified again (``_supervised``, the head of its key).

        A retry repeats the same round, with the profiles it recorded when it opened: a reviewer
        whose models were exhausted would fail again. This command opens a new round instead:
        its reviewers are chosen with the current configuration, it has a new receipt and it
        counts against ``max_review_rounds``, as the rereview of a trusted user does. It
        applies only while a reviewer of the quorum of the latest round of this head has no
        verdict (``missing_verdicts``): when every one has a verdict, or one of them asked for
        the operator, it is refused and the escalation stays with the supervisor, so that a new
        review never overrides a verdict. It is refused too when a new review was already asked
        after the latest review record (``pr.rereview``: a trusted user's request, or a
        rereview of the supervisor applied): a round that wrote no record does not count against
        ``max_review_rounds``, and without this the command would have no limit. It applies on a
        PR the supervisor's agent implemented too (#527): it never overrides a verdict, and
        these refusals hold there as anywhere. With ``operator`` the PR is in
        ``agent:needs-operator`` (``_operator_rereview``): a refusal leaves it there, with ``resume``, how the operator
        resumes it.

        Applied, the ``supervision`` record is written first: written after the latest review
        record, it voids the open round as a trusted user's request would (``_read_thread``),
        and spends the command. Then the PR goes back to ``agent:review``, where the next tick
        opens the new round; the merge is untouched and stays where the merge mode puts it."""
        lb = self.cfg.labels
        label = lb.needs_operator if operator else lb.needs_supervisor
        stays = (f'Il comando è speso; la PR resta in `{label}`.\n\n{resume}' if operator
                 else 'Il comando è speso; l\'escalation resta al supervisore.\n\n'
                      + self._supervisor_resume(escalation, True))
        latest = current_round(pr.reviews)
        missing = missing_verdicts(pr)
        asked = [owner_of(r) for r in latest
                 if current_verdict(r.get('verdict')) == 'needs_operator']
        verdicts = ', '.join(f'`{owner_of(r)}`: `{current_verdict(r.get("verdict"))}`'
                             for r in latest)
        why = ('nell\'ultimo round un reviewer ha chiesto una decisione dell\'operatore ('
               + ', '.join(f'`{a}`' for a in asked) + '), e una nuova review la scavalcherebbe'
               if asked
               else 'nessun round di review di questo head aspetta il verdetto di un reviewer '
                    f'del quorum: `/devloop {RETRY}` apre già un round con la configurazione '
                    'attuale' if not latest or latest[0]['head_sha'] != pr.head_sha
               else 'dopo l\'ultimo round con un verdetto è già stata chiesta una nuova review, '
                    'da un utente fidato o dal supervisore, e il suo round non ha scritto alcun '
                    'verdetto: un\'altra lo ripeterebbe senza contare per `max_review_rounds`'
               if pr.rereview
               # The first reviewer closed the round without approving (#521): the second was
               # not asked, and no verdict is missing.
               else 'l\'ultimo round è stato chiuso dal primo reviewer del quorum, che non ha '
                    'approvato (' + verdicts + '): con `second_reviewer = "'
                    + latest[0]['receipt']['second_reviewer'] + '"` il secondo reviewer non era '
                    'richiesto e non manca alcun verdetto, e una nuova review scavalcherebbe il '
                    'suo' if closed_by_first(latest)
               else 'ogni reviewer del quorum dell\'ultimo round ha già un verdetto ('
                    + verdicts + '), e una nuova review lo scavalcherebbe' if not missing
               else '')
        if why:
            return trace('refused', f'rifiutato: {why}. {stays}')
        reviewers = ', '.join(f'`{key}`' for key in missing)
        trace('applied', f'si apre un round di review nuovo al posto del round '
              f'{latest[0]["round"]}, in cui manca il verdetto di {reviewers}. Il round nuovo '
              'sceglie i reviewer con la configurazione attuale, ha una ricevuta nuova e conta '
              'per `max_review_rounds`; il merge resta dove lo mette la sua modalità.\n\n'
              '**Motivo:**\n\n'
              + '\n'.join(f'> {line}'.rstrip() for line in command['reason'].splitlines()),
              round=latest[0]['round'], reviewers=missing)
        self.gh.edit_labels(number, add=[lb.review], remove=[label])

    def _operator_rereview(self, target, number, labels, fresh, comments, command, trace):
        """The supervisor's ``/devloop rereview <reason>`` on an item in ``agent:needs-operator``
        (#491). The command does not touch the merge, so it also applies to a PR whose merge is
        the operator's: only to the ``tool_failure`` escalation of a PR's review, in merge mode
        ``auto`` with a supervisor, on the head of its key, and when every condition of
        ``_supervised`` but the PR's own merge mode (the override of its level, an
        ``operator_only_paths`` path) still holds on the PR read now. Then ``_rereview``
        decides, as on an escalation to the supervisor. Any other case is refused with the
        reason, and the item stays with the operator."""
        lb = self.cfg.labels
        anchor = command['anchor']
        escalation = anchor['record']
        is_pr = isinstance(target, PullRequest)
        resume = self._operator_resume(anchor, is_pr)
        where = 'la PR' if is_pr else 'l\'issue'

        def refuse(why):
            return trace('refused', f'rifiutato: {why}. Il comando è speso; {where} resta in '
                         f'`{lb.needs_operator}`.\n\n{resume}')

        if self.cfg.merge.mode != 'auto' or not self.cfg.supervisor:
            return refuse(f'la modalità di merge è `{self.cfg.merge.mode}`, non `auto`, quindi '
                          'i comandi del supervisore non valgono')
        if not (is_pr and anchor['kind'] == 'notice' and escalation.get('event') == 'escalation'
                and escalation.get('class') == TOOL_FAILURE
                and escalation.get('step') == 'review' and self._retryable(escalation, True)):
            return refuse('l\'escalation è affidata all\'operatore: il supervisore vi può chiedere '
                          'una nuova review solo dopo un `tool_failure` della review di una PR')
        key = escalation['key']
        changed = changed_input(self._current_key(number, key, target), key)
        if changed:
            return refuse('l\'input del passo è cambiato dopo l\'escalation ('
                          + ', '.join(f'`{k}`' for k in changed)
                          + '), quindi il comando si riferisce a un input superato')
        pr = self._fresh_pr(target, labels, fresh, comments)
        route, why = self._supervised(number, TOOL_FAILURE, 'review', pr, key.get('reviewer'),
                                      key=key, any_merge=True)
        if route is None:
            return refuse('l\'escalation non è del supervisore ('
                          + (why or 'nessun supervisore configurato') + ')')
        return self._rereview(number, pr, command, escalation, trace, operator=True,
                              resume=resume)

    def _fresh_pr(self, pr, labels, fresh, comments):
        """``pr`` with its ``labels`` and head (``fresh``, its state) read now and with what its
        thread records rebuilt from the ``comments`` read now, as the merge does: the snapshot
        does not read the thread of a PR in ``agent:needs-supervisor``, and without its review
        and policy records the level recorded earlier would be lost."""
        pr = replace(pr, labels=labels, head_sha=fresh.get('headRefOid') or pr.head_sha,
                     reviews=[], fixes=[], syncs=[], closures=[], commands=[],
                     finding_problems=[], policy=[], zero_head=None, zero_problem='',
                     stale_receipt=False, revoked=None, operator_comments=[], rereview=False,
                     round_comments=[], acceptance=[], hold=None, granted_rounds=0)
        if pr.meta:
            self._read_thread(pr, comments)
        return pr

    def _granted_rounds(self, comments):
        """How many review rounds the supervisor granted on a PR (#405): the dispatcher's
        ``supervision`` records of a ``grant-round`` that was granted, in its ``comments``."""
        return sum(1 for _, r in self._dispatcher_records(comments, 'supervision')
                   if r.get('command') == GRANT_ROUND and r.get('outcome') == 'granted')

    def _grants_today(self):
        """The grants of the supervisor's delegations of this UTC day, as the journal of the
        turns records them (#405), or None when they cannot be read: no journal, or one that
        cannot be read. The daily cap is then not verifiable, and no grant applies."""
        journal = getattr(self.ledger, 'journal', None)
        if journal is None:
            return None
        try:
            return len(journal.grants())
        except JournalError as exc:
            self.log(f'  the grants of today cannot be read: {exc}')
            return None

    def _round_grant(self, pr, reviews):
        """``(route, why)`` of ``pr`` at its round limit (#405, ADR 0005, P5-1), with its review
        records ``reviews``: ``route`` is the payload of a ``round_grantable`` escalation to the
        supervisor, None when the next round is the operator's; ``why`` says why the delegation,
        when on, does not apply ('' otherwise). With ``[delegations] rounds`` off it never
        applies, and says nothing.

        The delegation applies only when every condition holds, each verified now: those of
        ``_supervised`` (merge mode ``auto`` with a supervisor, a PR in merge mode ``auto`` that
        the supervisor did not implement, a trusted issue still approved, no
        ``phase:design``); the caps of the PR and of the day not reached; findings whose owner
        is known, none of them open with ``security`` other than false (a finding that does not
        say counts as one); and the latest counted round a CI failure, or fewer blocking findings
        open than after the round before. Anything that cannot be verified refuses it."""
        cfg, lb = self.cfg, self.cfg.labels
        rules = cfg.delegations
        if not rules.rounds:
            return None, ''
        route, why = self._supervised(pr.number, ROUND_GRANTABLE, 'review', pr)
        if route is None:
            return None, why
        cap, daily_cap = rules.max_rounds_per_pr, rules.max_grants_per_day
        today = self._grants_today()
        state = finding_state(reviews, pr.closures)
        security = [f['key'] for f in state.open if f.get('security') is not False]
        why = (f'a questa PR sono già stati concessi {pr.granted_rounds} round, il massimo della '
               f'delega (`[delegations] max_rounds_per_pr` = {cap})'
               if pr.granted_rounds >= cap
               else 'le concessioni di oggi non sono verificabili: il journal dei turni non è '
                    'disponibile o non è leggibile' if today is None
               else f'oggi sono già state fatte {today} concessioni, il massimo giornaliero '
                    f'(`[delegations] max_grants_per_day` = {daily_cap})'
               if today >= daily_cap
               else 'il proprietario di alcuni finding non è ricostruibile'
               if pr.finding_problems or state.problems
               else 'restano aperti finding di sicurezza, o finding che non dicono se lo sono: '
                    + ', '.join(f'`{k}`' for k in security) if security
               else 'l\'ultimo round contato non è una CI fallita e i finding bloccanti aperti '
                    'non sono meno di quelli del round precedente'
               if not (last_counted_ci(reviews, **waiver(cfg, pr.fixes, pr.closures))
                       or findings_decreasing(reviews, pr.closures))
               else '')
        if why:
            return None, f'la delega dei round non si applica: {why}'
        resume = lb.changes if lb.changes in pr.labels else lb.review
        return {'head': pr.head_sha, 'resume': resume, 'granted': pr.granted_rounds}, ''

    def _grant_round(self, target, number, fresh, labels, comments, command, escalation, trace):
        """Apply ``/devloop grant-round <reason>`` on PR ``number`` in a ``round_grantable``
        escalation (#405, ADR 0005, P5-1): one more review round for this PR only, never a
        change of ``max_review_rounds``. The conditions of the delegation are verified again on
        the PR read now (``_round_grant``): a head moved since the escalation is refused and
        the escalation stays with the supervisor; a delegation turned off, a cap reached or a
        condition that no longer holds hand the PR to the operator as ``round_limit``.

        Granted, the grant is written in the journal of the turns first, for the daily cap
        (``journal.TurnJournal.grant``), which checks the cap again and records the grant in one
        step under its lock: two grants applied together on different PRs never pass the cap,
        and the one that finds it reached hands its PR to the operator as ``round_limit``. Then
        the ``supervision`` record, which raises the
        limit of the PR and spends the command; then the ledger ``event`` with the delegation,
        the caps, the value granted and what remains; last the labels: the PR goes back to the
        label it was in (``agent:review`` or ``agent:changes``) and the next tick goes on."""
        lb = self.cfg.labels
        by = command['by']
        head = fresh.get('headRefOid') or target.head_sha
        if head != escalation.get('head'):
            return trace('refused', 'rifiutato: il head della PR è cambiato dopo l\'escalation, '
                         'quindi la concessione si riferisce a un input superato. Il comando è '
                         'speso; l\'escalation resta al supervisore.\n\n'
                         + self._supervisor_resume(escalation, True, superseded=True))
        def refuse(why):
            trace('refused', f'rifiutato: {why}.')
            return self._to_operator(number, 'Il supervisore non può concedere un altro round a '
                                     f'questa PR: {why}. `/devloop {GRANT_ROUND}` di `{by}` è '
                                     'stato rifiutato.\n\nL\'operatore può concedere altri round '
                                     'alzando `max_review_rounds` prima di riprendere la PR.',
                                     'round_limit')

        pr = self._fresh_pr(target, labels, fresh, comments)
        route, why = self._round_grant(pr, pr.reviews)
        if route is None:
            return refuse(why or ('la delega dei round è spenta (`[delegations] rounds`), o la '
                                  'modalità di merge non è più `auto` con un supervisore'))
        rules = self.cfg.delegations
        # The daily cap is checked again and the grant recorded in one step (#405): another
        # grant applied in the meantime, on another PR, may have taken the last one.
        daily = self.ledger.journal.grant('rounds', number, command['comment'], by,
                                          rules.max_grants_per_day)
        if daily is None:
            return refuse('la delega dei round non si applica: nel frattempo le concessioni di '
                          'oggi hanno raggiunto il massimo giornaliero (`[delegations] '
                          f'max_grants_per_day` = {rules.max_grants_per_day})')
        granted = pr.granted_rounds + 1
        figures = {'head': head, 'granted': granted, 'cap': rules.max_rounds_per_pr,
                   'left': rules.max_rounds_per_pr - granted, 'daily': daily,
                   'daily_cap': rules.max_grants_per_day,
                   'daily_left': rules.max_grants_per_day - daily}
        limit = self.cfg.max_review_rounds + granted
        resume = escalation.get('resume')
        resume = resume if resume in (lb.review, lb.changes) else lb.review
        trace('granted', f'concesso un round in più a questa PR: il suo limite passa a {limit} '
              f'round (`max_review_rounds` = {self.cfg.max_review_rounds}, più {granted} '
              f'concessi). Round concessi a questa PR: {granted} di {rules.max_rounds_per_pr}; '
              f'concessioni di oggi: {daily} di {rules.max_grants_per_day}. La PR torna in '
              f'`{resume}`.\n\n**Motivo e analisi delle cause:**\n\n'
              + '\n'.join(f'> {line}'.rstrip() for line in command['reason'].splitlines()),
              **figures)
        self.ledger.event(number, 'delegation_grant', delegation='rounds', by=by,
                          comment=command['comment'], **figures)
        self.gh.edit_labels(number, add=[resume], remove=[lb.needs_supervisor])

    def _budget_extension(self, issue, kind, agent):
        """``(found, why)`` of the one extension of the envelope of the story of issue
        ``issue`` (#411, ADR 0005, P5-7), for a turn of ``kind`` of the agent ``agent`` (its
        key) that the envelope refused: ``found`` is ``{story, extension, kind, agent}`` when
        the delegation applies now, None otherwise, with ``why``. It applies only within the
        daily cap of the delegations, for an agent still configured, and when the extension can
        be granted (``budget.grantable``): the story never extended, refused at a limit that
        the extension would cover. Anything that cannot be verified refuses it."""
        cfg = self.cfg
        cap = cfg.delegations.max_grants_per_day
        today = self._grants_today()
        if today is None:
            return None, ('le concessioni di oggi non sono verificabili: il journal dei turni '
                          'non è disponibile o non è leggibile')
        if today >= cap:
            return None, (f'oggi sono già state fatte {today} concessioni, il massimo '
                          f'giornaliero (`[delegations] max_grants_per_day` = {cap})')
        if not isinstance(agent, str) or agent not in cfg.agents or not isinstance(kind, str):
            return None, ('il turno rifiutato non è ricostruibile: il suo agent (`'
                          f'{agent}`) non è configurato, o il tipo del turno manca')
        try:
            story, extension, why = budget_grantable(
                cfg.budget, self.ledger.journal, repo=cfg.repo, issue=issue, kind=kind,
                agent=cfg.agent(agent))
        except JournalError as exc:
            return None, f'il journal dei turni non è leggibile ({exc})'
        if why:
            return None, why
        return {'story': story, 'extension': extension, 'kind': kind, 'agent': agent}, ''

    def _budget_grant(self, number, step, pr, reviewer, key, exc):
        """``(route, why)`` of the turn of ``step`` on issue or PR ``number`` (``pr``: the PR,
        None for an issue) that the budget envelope of its story refused (``exc``, a
        ``BudgetRefused``) (#411, ADR 0005, P5-7). ``route`` is the payload of a
        ``budget_grantable`` escalation to the supervisor: the one of ``_supervised`` (the step,
        the key of its input and, for a PR, the label it resumes from) with the story, the
        extension, the kind and the agent of the refused turn; None when the escalation goes to
        the operator as ``budget``, with ``why`` the delegation, when on, does not apply ('' if
        it does). With ``[delegations] budget`` off it never applies, and says nothing.

        The delegation applies only when every condition holds, each verified now: those of
        ``_supervised`` (merge mode ``auto`` with a supervisor, a step the supervisor may
        resume, a PR in merge mode ``auto`` that the supervisor did not implement, a trusted
        issue still approved, no ``phase:design``) and those of ``_budget_extension``. The turns
        of a conclave or of an advisory review, on an item already with the operator, never
        apply."""
        if not self.cfg.delegations.budget or step not in RETRY_STEPS:
            return None, ''
        route, why = self._supervised(number, BUDGET_GRANTABLE, step, pr, reviewer, key=key)
        if route is None:
            return None, why and f'la delega del budget non si applica: {why}'
        issue = pr.issue if pr is not None else number
        found, why = self._budget_extension(issue, exc.kind or step, exc.agent)
        if found is None:
            return None, f'la delega del budget non si applica: {why}'
        return dict(route, **found, category=exc.category), ''

    def _pending_extension(self, number, comments, is_pr, operator=False):
        """The extension of the budget envelope that the journal recorded for a command on
        issue or PR ``number`` and whose records a stopped or failed run did not write (#411):
        ``{escalation, command, grant, daily, resume}``, or None.

        It is pending when a ``budget_grantable`` escalation of the dispatcher to the supervisor
        in ``comments``, any of them and not only the latest, names a story whose one extension
        the journal holds, granted on ``number`` by a command that no dispatcher
        ``supervision`` record of the thread carries; ``escalation`` is the latest one of that
        story. The command is rebuilt from the journal (its comment, author and text), so it
        does not depend on the command that is the latest now; a text the journal lacks is read
        from the comment, when still there. ``daily`` is the count of the grants of its UTC day
        up to it. None too when the journal cannot be read.

        Completing the records never depends on where the item is now: on an item handed to the
        operator meanwhile (``operator``, for instance by the ``failure`` escalation of a
        ``GitHubError`` while the records were written) or behind a later escalation they are
        written all the same. ``resume`` says whether the step of the refused turn resumes too:
        only on an item still in ``agent:needs-supervisor`` whose latest escalation is that
        one, with a step the supervisor may resume (``_retryable``)."""
        escalations = self._dispatcher_records(comments, 'notice', 'escalation')
        candidates = {}
        for position, record in escalations:
            story = record.get('story')
            if record.get('class') == BUDGET_GRANTABLE and record.get('to') == 'supervisor' \
                    and isinstance(story, str):
                candidates[story] = (position, record)  # the latest escalation of each story
        journal = getattr(self.ledger, 'journal', None)
        if not candidates or journal is None:
            return None
        traced = {r.get('comment') for _, r in self._dispatcher_records(comments, 'supervision')}
        found = None
        try:
            for story, (position, escalation) in sorted(candidates.items(),
                                                       key=lambda item: -item[1][0]):
                grant = journal.extension(story)
                if grant is not None and grant.get('number') == number \
                        and grant.get('comment') not in traced:
                    found = position, escalation, grant
                    break
            if found is None:
                return None
            position, escalation, grant = found
            ident = grant.get('comment')
            day = [g.get('comment') for g in journal.grants(str(grant.get('at', ''))[:10])]
        except JournalError as exc:
            self.log(f'  the extensions of stories {", ".join(sorted(candidates))} cannot be '
                     f'read: {exc}')
            return None
        resume = (not operator and position == escalations[-1][0]
                  and self._retryable(escalation, is_pr))
        reason = grant.get('reason')
        if not isinstance(reason, str):
            parsed = [supervisor_command(c.get('body') or '') for c in comments
                      if c.get('id') == ident]
            reason = parsed[0][1] if parsed and parsed[0] and parsed[0][1] else ''
        command = {'command': GRANT_BUDGET, 'by': grant.get('by'), 'reason': reason,
                   'comment': ident}
        return {'escalation': escalation, 'command': command, 'grant': grant,
                'daily': day.index(ident) + 1 if ident in day else 1, 'resume': resume}

    def _issue_blocked(self, number):
        """Whether issue ``number`` is blocked by an open issue (#277), read now: its step
        does not run, as for a retry (``_apply_retry``)."""
        blocked_by = self._dependency_view([number])[number]['blocked_by']
        if blocked_by:
            self.log('  the extension waits, blocked by ' + ', '.join(blocked_by))
        return bool(blocked_by)

    def _granted_event(self, comments, comment):
        """Whether ``comments`` hold the ledger ``event`` ``delegation_grant`` of the budget
        written for the command of comment ``comment`` (#411), by the dispatcher."""
        return any(r.get('event') == 'delegation_grant' and r.get('delegation') == 'budget'
                   and r.get('comment') == comment
                   for _, r in ledger_entries(comments, self._signed))

    def _grant_budget(self, target, number, fresh, labels, comments, command, escalation, trace,
                      remove):
        """Apply ``/devloop grant-budget <reason>`` on issue or PR ``number`` in a
        ``budget_grantable`` escalation (#411, ADR 0005, P5-7): the one extension of the budget
        envelope of its story, in turns and time only, by ``BUDGET_TOLERANCE_PERCENT`` of the
        frozen values rounded down, never in dollars nor tokens. ``fresh``: the state of the PR
        read now, None for an issue.

        An issue a merged devloop PR already resolved (#228) is refused first and becomes a
        ``merged_issue`` escalation, as for a retry. The extension is bound to the input of the
        refused turn, as a retry is: when the key of the escalation is no longer the current one
        (the PR's head, or the issue's base or request) the command is refused and the
        escalation stays with the supervisor. The conditions are verified again now
        (``_supervised`` on the PR read now and ``_budget_extension``), and the story must be
        the one of the escalation: a delegation turned off, a story already extended, a cap
        reached, an extension that would no longer cover the turn, or anything that cannot be
        verified hand the item to the operator as ``budget``, removing ``remove``. Granted, the
        extension is written in the journal of the turns first (``TurnJournal.extend``), which
        checks again that the story has none and the daily cap, and records it, in one step
        under its lock: from then on it is part of the envelope of the story. Then the ledger
        ``event`` with the delegation, the extension and the daily figures; then the
        ``supervision`` record, which spends the command; last the labels: a PR goes back to
        the label of its step, and the next tick runs it; an issue loses
        ``agent:needs-supervisor`` and its step runs now, as after a retry. An issue blocked by
        an open issue (#277) waits, with nothing recorded nor spent.

        A run stopped after the journal recorded the extension and before the ``supervision``
        record, or failed there and handed to the operator, is completed by ``_do_supervise``
        before any command (``_pending_extension`` and ``_finish_extension``), never here."""
        lb = self.cfg.labels
        rules = self.cfg.delegations
        by = command['by']
        is_pr = fresh is not None
        carry = ({k: escalation[k] for k in ('step', 'key', 'resume') if k in escalation}
                 if self._retryable(escalation, is_pr) else None)

        def refuse(why):
            trace('refused', f'rifiutato: {why}.')
            return self._to_operator(number, 'Il supervisore non può estendere l\'envelope di '
                                     f'budget di questa storia: {why}. `/devloop {GRANT_BUDGET}` '
                                     f'di `{by}` è stato rifiutato.\n\nI valori dell\'envelope e '
                                     'le estensioni oltre l\'unica delegata al supervisore '
                                     'restano decisioni dell\'operatore (ADR 0004, D2).',
                                     BUDGET, remove, carry)

        # An issue a merged devloop PR already resolved is never implemented again (#228), as
        # for a retry: the command becomes a merged_issue escalation, and nothing is extended.
        merged = [] if is_pr else self._merged_prs(number)
        if merged:
            trace('refused', 'rifiutato: l\'issue è già risolta da una PR devloop mergiata ('
                  + ', '.join(f'#{m["number"]}' for m in merged) + '), quindi ripeterne il '
                  'passo duplicherebbe il lavoro. Il comando è speso; l\'issue passa a '
                  'un\'escalation `merged_issue`.')
            return self._do_merged_issue(number, merged)
        key, step = escalation.get('key'), escalation.get('step')
        if not rules.budget:
            return refuse('la delega del budget è spenta (`[delegations] budget`)')
        if carry is None:
            return refuse('l\'escalation non registra il passo del turno rifiutato')
        current = self._current_key(number, key, target if is_pr else None)

        def superseded():
            return trace('refused', 'rifiutato: l\'input del passo è cambiato dopo '
                         'l\'escalation (' + ', '.join(f'`{k}`' for k in sorted(current)
                                                        if current[k] != key.get(k))
                         + '), quindi la concessione si riferisce a un input superato. Il '
                         'comando è speso; l\'escalation resta al supervisore.\n\n'
                         + self._supervisor_resume(escalation, is_pr, superseded=True))

        # As a retry: the extension is bound to the input of the refused turn (the key of the
        # escalation). A PR whose head moved is refused first, its merge mode would be the one
        # of another diff; an issue's base and request after its other checks.
        if is_pr and current != key:
            return superseded()
        pr = self._fresh_pr(target, labels, fresh, comments) if is_pr else None
        route, why = self._supervised(number, BUDGET_GRANTABLE, step, pr,
                                      key.get('reviewer'), key=key)
        if route is None:
            return refuse(f'la delega del budget non si applica: {why}' if why else
                          'la modalità di merge non è più `auto` con un supervisore')
        if current != key:
            return superseded()
        found, why = self._budget_extension(pr.issue if is_pr else number,
                                            escalation.get('kind'), escalation.get('agent'))
        if found is None:
            return refuse(f'la delega del budget non si applica: {why}')
        story, extension = found['story'], found['extension']
        if story != escalation.get('story'):
            return refuse(f'la storia del lavoro (`{story}`) non è quella dell\'escalation '
                          f'(`{escalation.get("story")}`)')
        # As a retry (``_apply_retry``): an issue blocked by an open issue (#277) does not run
        # its step, so the command waits, unspent, until the last block closes.
        if not is_pr and self._issue_blocked(number):
            return None
        # The text of the command goes in the journal with the extension: a run stopped before
        # the records completes them with this command, whatever the thread holds by then.
        daily, problem = self.ledger.journal.extend(story, extension, number,
                                                    command['comment'], by,
                                                    rules.max_grants_per_day,
                                                    reason=command['reason'] or '')
        if problem == 'spent':
            return refuse(f'nel frattempo la storia `{story}` ha già avuto la sua unica '
                          'estensione')
        if problem == 'lapsed':
            return refuse('l\'estensione di questo comando è già decaduta, perché il primo '
                          'turno dopo la concessione è stato rifiutato comunque: serve una '
                          'nuova concessione')
        if problem:
            return refuse('nel frattempo le concessioni di oggi hanno raggiunto il massimo '
                          f'giornaliero (`[delegations] max_grants_per_day` = '
                          f'{rules.max_grants_per_day})')
        return self._finish_extension(number, is_pr, comments, escalation, command, trace,
                                      {'story': story, 'extension': extension}, daily)

    def _finish_extension(self, number, is_pr, comments, escalation, command, trace, grant,
                          daily, recovered=False, resume=True, operator=False):
        """Write the records of the extension ``grant`` (``{story, extension}``) of the budget
        envelope that the journal recorded for ``command`` on issue or PR ``number``, in the
        ``budget_grantable`` escalation ``escalation`` (#411), and resume its step: ``daily``
        is the count of the grants of its UTC day up to it. First the ledger ``event``
        ``delegation_grant``, unless ``comments`` hold it already (``_granted_event``); then the
        ``supervision`` record ``granted`` with ``trace``, which spends the command; last the
        labels: a PR goes back to the label of its step, and the next tick runs it; an issue
        loses ``agent:needs-supervisor`` and its step runs now, as after a retry.

        ``recovered``: the extension of a run stopped before its records
        (``_pending_extension``). It is part of the envelope of the story already, so nothing is
        checked nor applied again, whatever changed since: an issue a merged devloop PR resolved
        meanwhile (#228) gets its records without running its step, and becomes a
        ``merged_issue`` escalation; an issue blocked by an open issue (#277) waits, with nothing
        written, until the last block closes.

        ``resume`` false (``_pending_extension``): only the records are completed, and the item
        stays where it is, its labels unchanged and no step run. So on an item handed to the
        operator meanwhile (``operator``), whose block the record does not end
        (``trace_only``, ``_operator_anchor``), or behind a later escalation."""
        lb = self.cfg.labels
        rules = self.cfg.delegations
        by, ident, step = command['by'], command['comment'], escalation.get('step')
        story, extension = grant['story'], grant['extension']
        merged = self._merged_prs(number) if resume and recovered and not is_pr else []
        if resume and recovered and not is_pr and not merged and self._issue_blocked(number):
            return None
        if recovered:
            self.log(f'  the extension of the command of comment {ident} is in the journal '
                     'already: completing its records')
        figures = {'story': story, 'extension': extension, 'daily': daily,
                   'daily_cap': rules.max_grants_per_day,
                   'daily_left': rules.max_grants_per_day - daily}
        # The ledger event before the supervision record, which spends the command: a run
        # stopped between the two finds the event and does not write it again.
        if not self._granted_event(comments, ident):
            self.ledger.event(number, 'delegation_grant', delegation='budget', by=by,
                              comment=ident, **figures)
        extra = {}
        if not resume:
            held = lb.needs_operator if operator else lb.needs_supervisor
            what = 'La PR' if is_pr else 'L\'issue'
            where = (f'{what} resta in `{held}`' + (', dove decide l\'operatore come prima'
                                                    if operator else
                                                    ', con la sua escalation più recente')
                     + f': il passo `{step}` non riparte da qui.')
            extra['trace_only'] = True
        elif is_pr:
            label = escalation.get('resume')
            label = label if label in (lb.review, lb.changes) else lb.review
            where = f'La PR torna in `{label}`.'
        elif merged:
            where = ('L\'issue è però già risolta da una PR devloop mergiata ('
                     + ', '.join(f'#{m["number"]}' for m in merged) + f'): il passo `{step}` '
                     'non riparte e l\'issue passa a un\'escalation `merged_issue`.')
        else:
            where = f'Il passo `{step}` dell\'issue riparte ora, come dopo un retry.'
        if recovered:
            where = ('Un\'elaborazione precedente si era fermata dopo aver registrato '
                     'l\'estensione nel journal dei turni: qui se ne completa la traccia, senza '
                     f'applicarla di nuovo. {where}')
        reason = command['reason'] or ''
        trace('granted', f'estesi i tetti dell\'envelope di budget della storia `{story}`: '
              f'{extension_text(extension)} (il {extension["percent"]}% dei valori fissati alla '
              'sua prima prenotazione, arrotondato per difetto; nessun dollaro né token). È '
              'l\'unica estensione della storia: un altro rifiuto va all\'operatore. Concessioni '
              f'del giorno: {daily} di {rules.max_grants_per_day}. {where}\n\n**Motivo e '
              'analisi delle cause:**\n\n'
              + '\n'.join(f'> {line}'.rstrip() for line in reason.splitlines()),
              **figures, **extra)
        if not resume:
            return None
        if merged:
            return self._do_merged_issue(number, merged)
        if is_pr:
            self.gh.edit_labels(number, add=[label], remove=[lb.needs_supervisor])
            return None
        self.gh.edit_labels(number, remove=[lb.needs_supervisor])
        try:
            getattr(self, f'_do_{step}')(number)
        except (AgentError, GitHubError, WorkspaceError, UntrustedChange) as exc:
            self.log(f'  failed: {exc}')
            self._escalate_failure(Action(step, number, 'grant-budget'), None, exc)
        return None

    # Temporary capacity limits (#412, ADR 0005, P5-8) ------------------------
    def _capacity(self, dry_run=False):
        """The configuration this tick plans with: ``self.cfg`` with the temporary capacity
        limits of the supervisor in force (#412, ADR 0005, P5-8). ``self.cfg`` itself never
        changes: when a limit expires, or the delegation is turned off, the next tick plans with
        the configured value again.

        With ``[report] issue`` set, the dispatcher reads its comments and answers the
        ``/devloop capacity`` commands still to answer (``_capacity_commands``; not in a dry
        run). Only with ``[delegations] capacity`` on does it take the limits in force from its
        own ``supervision`` records (``_capacity_limits``); with the delegation off each command
        is refused as not applicable and spent (ADR 0005, §3.1, rule 6), so turning the
        delegation on later grants nothing written before. A dry run with the delegation off
        does not read the issue. When the issue cannot be read the configured values apply
        (fail closed)."""
        cfg = self.cfg
        issue = cfg.report.issue
        limits = {}
        if issue and (cfg.delegations.capacity or not dry_run):
            try:
                comments = self.gh.issue_comments(issue)
                if not dry_run and self._capacity_commands(issue, comments):
                    comments = self.gh.issue_comments(issue)
                if cfg.delegations.capacity:
                    limits = self._capacity_limits(comments)
            except GitHubError as exc:
                self.log(f'capacity: #{issue} cannot be read or answered ({exc}): the configured '
                         'limits apply')
                limits = {}
        seen = getattr(self, '_capacity_seen', {})
        for key in sorted(seen.keys() | limits.keys()):
            if key not in limits:
                self.log(f'capacity: {key} back to the configured {getattr(cfg, key)}')
            elif seen.get(key) != limits[key]:
                self.log(f'capacity: {key} = {limits[key]["value"]} until {limits[key]["until"]}, '
                         f'a temporary limit of the supervisor (configured: {getattr(cfg, key)})')
        self._capacity_seen = limits
        if not limits:
            return cfg
        return replace(cfg, **{key: limit['value'] for key, limit in limits.items()})

    def _capacity_limits(self, comments):
        """The temporary capacity limits in force, from the ``comments`` of ``[report] issue``:
        ``{key: {value, until, by}}``. Each comes from the dispatcher's ``supervision`` record
        of a ``capacity`` command it ``granted``; for each key the latest grant replaces the
        earlier ones, and is in force until its ``until``. A grant whose value is above the cap
        the configuration sets now (lowered after the grant) is not applied."""
        rules, now = self.cfg.delegations, self.clock()
        latest = {}
        for _, record in self._dispatcher_records(comments, 'supervision'):
            if record.get('command') != CAPACITY or record.get('outcome') != 'granted':
                continue
            key, value, until = record.get('setting'), record.get('value'), record.get('until')
            if (rules.capacity_cap(key) is not None and isinstance(value, int)
                    and not isinstance(value, bool) and _epoch(until) is not None):
                latest[key] = {'value': value, 'until': until, 'by': record.get('by')}
        limits = {}
        for key, limit in latest.items():
            if _epoch(limit['until']) <= now:
                continue
            if not 1 <= limit['value'] <= rules.capacity_cap(key):
                self.log(f'capacity: {key} = {limit["value"]} is above the cap of the '
                         f'configuration ({rules.capacity_cap(key)}): not applied')
                continue
            limits[key] = limit
        return limits

    def _capacity_commands(self, issue, comments):
        """Answer the ``/devloop`` commands written on ``[report] issue`` after the latest one
        answered (#412), oldest first, each with a ``supervision`` record (``_capacity_command``)
        that spends it and every command before it. Only commands from the supervisor's
        identity or a trusted user count (``_may_hold``); any other is ignored and only logged.
        Returns how many were answered."""
        spent, start = None, -1
        for position, record in self._dispatcher_records(comments, 'supervision'):
            applied = record.get('comment')
            if isinstance(applied, int) and not isinstance(applied, bool):
                spent = applied if spent is None else max(spent, applied)
            else:
                start = max(start, position)  # a record without the id spends what precedes it
        answered = 0
        for comment in comments[start + 1:]:
            body = comment.get('body') or ''
            parsed = None if carries_record(body) else supervisor_command(body)
            if parsed is None:
                continue
            ident = comment.get('id')
            if spent is not None and (not isinstance(ident, int) or ident <= spent):
                continue
            login = (comment.get('author') or {}).get('login')
            if not self._may_hold(login):
                self.log(f'capacity: #{issue}: ignored `/devloop {parsed[0]}` by {login}: the '
                         'author is neither the supervisor nor a trusted user')
                continue
            self._capacity_command(issue, comment, *parsed, login)
            answered += 1
        return answered

    def _capacity_command(self, issue, comment, name, text, by):
        """Answer the command ``name`` with its ``text`` of ``by`` in ``comment`` of ``[report]
        issue`` (#412, ADR 0005, P5-8). Only ``/devloop capacity <key> <value> <duration>
        <reason>`` applies, and only when every condition holds, each verified now: ``[delegations]
        capacity`` on; merge mode ``auto``; a well-formed command with the analysis of the causes in its reason; a key the
        delegation covers (``max_parallel_actions`` is refused: it needs a restart); a value
        from 1 to the cap of the key and a duration up to ``capacity_max_hours``, caps of the
        code the configuration may only lower; a duration not over yet, counted from when the
        command was written; the daily cap of the grants not reached.

        A refusal is answered with a ``supervision`` record, and changes nothing. A grant is
        written in the journal of the turns first, which checks the daily cap again and
        records it in one step (as ``_grant_round``); then the ``supervision`` record, which
        makes it apply from this tick until its ``until``; last the ledger ``event``."""
        if name == MERGE_BUDGET:
            return self._merge_budget_command(issue, comment, text, by)
        cfg = self.cfg
        rules = cfg.delegations
        ident = comment.get('id')
        args = capacity_arguments(text) if name == CAPACITY else None

        def trace(outcome, what, **extra):
            record = dict(command=name, by=by, comment=ident, outcome=outcome,
                          reason=args[3] if args else text or '', **extra)
            self.gh.comment(issue, render_supervision(record, f'`/devloop {name}` di `{by}`: '
                                                      f'{what.strip()}', CAPACITY_TITLE))
            self.log(f'capacity: {name} by {by}: {outcome}')

        def refuse(why):
            return trace('refused', f'non applicabile: {why}. Il comando è speso; i limiti di '
                         f'capacità restano quelli di prima.\n\n{CAPACITY_USAGE}')

        if name != CAPACITY:
            return refuse(f'sulla issue del riepilogo valgono solo `/devloop {CAPACITY}` e '
                          f'`/devloop {MERGE_BUDGET}`')
        if not rules.capacity:
            return refuse('la delega dei limiti di capacità è spenta (`[delegations] capacity`): '
                          'i limiti li cambia solo l\'operatore')
        if cfg.merge.mode != 'auto':
            return refuse(f'la modalità di merge è `{cfg.merge.mode}`, non `auto`, quindi i '
                          'comandi del supervisore non valgono')
        if args is None:
            return refuse('il comando non è completo o non è ben formato: servono chiave, valore '
                          'intero, durata e motivo')
        key, value, minutes, _ = args
        if key in CAPACITY_REFUSED:
            return refuse(f'`{key}` non è delegata: devloop la applica solo al riavvio di '
                          '`devloop run`, quindi la cambia solo l\'operatore')
        cap = rules.capacity_cap(key)
        if cap is None:
            return refuse(f'`{key}` non è fra le chiavi della delega')
        if not 1 <= value <= cap:
            return refuse(f'il valore {value} è fuori dai limiti della delega: `{key}` va da 1 a '
                          f'{cap} (`[delegations] capacity_{key}`)')
        if minutes > rules.capacity_max_hours * 60:
            return refuse(f'la durata supera il massimo di {rules.capacity_max_hours} ore '
                          '(`[delegations] capacity_max_hours`)')
        written = _epoch(comment.get('created_at'))
        if written is None:
            return refuse('l\'ora del comando non è leggibile, quindi la scadenza non è '
                          'verificabile')
        until = written + minutes * 60
        if until <= self.clock():
            return refuse(f'la durata, contata da quando il comando è stato scritto '
                          f'({iso(written)}), è già trascorsa')
        journal = getattr(self.ledger, 'journal', None)
        try:
            daily = (None if journal is None else
                     journal.grant('capacity', issue, ident, by, rules.max_grants_per_day))
        except JournalError as exc:
            self.log(f'capacity: the grant cannot be recorded: {exc}')
            journal = None
        if journal is None:
            return refuse('le concessioni di oggi non sono verificabili: il journal dei turni non '
                          'è disponibile o non è leggibile')
        if daily is None:
            return refuse('oggi le concessioni hanno già raggiunto il massimo giornaliero '
                          f'(`[delegations] max_grants_per_day` = {rules.max_grants_per_day})')
        configured = getattr(cfg, key)
        figures = {'setting': key, 'value': value, 'configured': configured, 'cap': cap,
                   'minutes': minutes, 'until': iso(until), 'daily': daily,
                   'daily_cap': rules.max_grants_per_day,
                   'daily_left': rules.max_grants_per_day - daily}
        trace('granted', f'`{key}` vale {value} fino a {iso(until)} (valore configurato: '
              f'{configured}; tetto della delega: {cap}). Alla scadenza torna il valore '
              f'configurato; un altro `/devloop {CAPACITY}` sulla stessa chiave sostituisce '
              f'questo. Concessioni di oggi: {daily} di {rules.max_grants_per_day}.\n\n'
              '**Motivo e analisi delle cause:**\n\n'
              + '\n'.join(f'> {line}'.rstrip() for line in args[3].splitlines()), **figures)
        if self.ledger is not None:
            self.ledger.event(issue, 'delegation_grant', delegation='capacity', by=by,
                              comment=ident, **figures)

    def _merge_budget_command(self, issue, comment, text, by):
        """Answer ``/devloop merge-budget <N> <reason>`` of ``by`` in ``comment`` of ``[report]
        issue`` (#486): ``N`` more delegated merges for the UTC day the command was written.
        Only a user of ``trusted_users`` extends the cap, never the supervisor's identity nor an
        app: their command is refused and does not count. It applies with ``[delegations]
        merge`` on, ``N`` from 1 to ``MAX_MERGE_BUDGET`` and a reason, on a day not over yet;
        the commands of a day add up, and the next day the cap is the configured one again.
        Each answer is a ``supervision`` record on the issue: ``granted``, with ``value`` (N),
        ``day`` and the cap of that day with it (``cap``), is what the delegated merges of the
        day count (``_merge_budget_extensions``); ``refused`` changes nothing."""
        cfg, rules = self.cfg, self.cfg.delegations
        ident = comment.get('id')
        args = merge_budget_arguments(text)

        def trace(outcome, what, **extra):
            record = dict(command=MERGE_BUDGET, by=by, comment=ident, outcome=outcome,
                          reason=args[1] if args else text or '', **extra)
            self.gh.comment(issue, render_supervision(record, f'`/devloop {MERGE_BUDGET}` di '
                                                      f'`{by}`: {what.strip()}',
                                                      MERGE_BUDGET_TITLE))
            self.log(f'merge budget: {MERGE_BUDGET} by {by}: {outcome}')

        def refuse(why):
            return trace('refused', f'non applicabile: {why}. Il comando è speso; il tetto dei '
                         f'merge delegati resta quello di prima.\n\n{MERGE_BUDGET_USAGE}')

        if by not in cfg.trusted_users:
            return refuse('il tetto dei merge delegati lo estende solo un utente fidato '
                          '(`trusted_users`), mai il supervisore né un\'app')
        if not rules.merge:
            return refuse('la delega del merge è spenta (`[delegations] merge`)')
        if args is None:
            return refuse('il comando non è completo o non è ben formato: servono un numero '
                          f'intero da 1 a {MAX_MERGE_BUDGET} e il motivo')
        written = _epoch(comment.get('created_at'))
        if written is None:
            return refuse('l\'ora del comando non è leggibile, quindi il giorno non è '
                          'verificabile')
        day, today = iso(written)[:10], iso(self.clock())[:10]
        if day < today:
            return refuse(f'il giorno UTC del comando ({day}) è già finito')
        n, _ = args
        before = sum(e['n'] for e in self._merge_budget_extensions(day))
        cap = rules.max_merges_per_day + before + n
        trace('granted', f'il {day} (UTC) i merge delegati al supervisore possono essere {n} in '
              f'più: il tetto del giorno passa a {cap} (`[delegations] max_merges_per_day` = '
              f'{rules.max_merges_per_day}, più le estensioni del giorno). Il giorno dopo torna '
              f'quello configurato.\n\n**Motivo:**\n\n'
              + '\n'.join(f'> {line}'.rstrip() for line in args[1].splitlines()),
              value=n, day=day, cap=cap)
        if self.ledger is not None:
            self.ledger.event(issue, 'delegation_grant', delegation='merge_budget', by=by,
                              comment=ident, value=n, day=day, cap=cap)

    def _apply_retry(self, number, is_pr, comments, escalation, remove, trace, limit):
        """Run again the step of ``escalation`` on issue or PR ``number`` (#182), its input
        already checked, removing the labels ``remove``: a PR goes back to the label of its
        step, an issue's step runs now. An issue blocked by an open issue (#277), read again
        now, does not run its step: the command waits, with no record and no attempt spent,
        until the last block closes. After ``MAX_RETRIES`` applied retries for the same key,
        the supervisor's and the operator's together (``retries_done``), ``limit(done)`` runs
        instead."""
        lb = self.cfg.labels
        key, step = escalation['key'], escalation['step']
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
            return limit(done)
        trace('applied', f'il passo `{step}` viene ripetuto (tentativo {done + 1} di '
              f'{MAX_RETRIES}), senza contare un round di review.', key=key, attempt=done + 1)
        if is_pr:
            resume = escalation.get('resume')
            self.gh.edit_labels(number, add=[resume if resume in (lb.review, lb.changes)
                                             else lb.review], remove=remove)
            return
        self.gh.edit_labels(number, remove=remove)
        try:
            getattr(self, f'_do_{step}')(number)
        except (AgentError, GitHubError, WorkspaceError, UntrustedChange) as exc:
            self.log(f'  failed: {exc}')
            self._escalate_failure(Action(step, number, 'retry'), None, exc)

    def _supervisor_resume(self, escalation, is_pr, superseded=False):
        """How an item in ``agent:needs-supervisor`` with ``escalation`` resumes, for the
        answer to a command that does not apply (#321): the commands that apply to its class in
        ``auto`` (none outside it; with ``superseded``, an input changed since the escalation,
        no retry nor decision of that input) and the manual steps."""
        lb = self.cfg.labels
        manual = (f'rimuovere `{lb.needs_supervisor}` e impostare la label del passo da eseguire '
                  '(`agent:ready` sulla issue, `agent:review` o `agent:changes` sulla PR).')
        if self.cfg.merge.mode != 'auto':
            return 'Per riprendere senza il supervisore: ' + manual
        cls = escalation.get('class')
        valid = []
        if not superseded and not is_pr and cls == PREFLIGHT_DEFAULT:
            valid = [f'`/devloop {ACCEPT_DEFAULTS}`', f'`/devloop {DECIDE} <decisioni>`']
        elif (not superseded and cls in (TOOL_FAILURE, SYNC_FAILED)
              and self._retryable(escalation, is_pr)):
            valid = [f'`/devloop {RETRY}`']
        elif not superseded and is_pr and cls == ROUND_GRANTABLE:
            valid = [f'`/devloop {GRANT_ROUND} <motivo>`']
        elif not superseded and cls == BUDGET_GRANTABLE:
            valid = [f'`/devloop {GRANT_BUDGET} <motivo>`']
        valid.append(f'`/devloop {ESCALATE} <motivo>`')
        return ('Per riprendere, il supervisore o un utente fidato scrive come prima riga di un '
                'commento ' + ' oppure '.join(valid) + '. Per riprendere senza il supervisore: '
                + manual)

    def _operator_resume(self, anchor, is_pr=None):
        """How the operator resumes an item in ``agent:needs-operator`` put there by
        ``anchor`` (``_supervisor_view``), for the answer to a command (#321)."""
        record = anchor['record']
        if anchor['kind'] == 'preflight':
            return PREFLIGHT_RESUME
        if anchor.get('origin') == PREFLIGHT_DEFAULT and not is_pr:
            return (RESUME + '\n\nIn alternativa, un utente fidato decide le lacune del '
                    f'controllo di prontezza con `/devloop {ACCEPT_DEFAULTS}` o `/devloop '
                    f'{DECIDE} <decisioni>` come prima riga di un commento.')
        if record.get('event') == 'operator_merge':
            return ('La PR è pronta e il merge spetta all\'operatore: si mergia a mano, come '
                    'spiega il commento «Il merge spetta all\'operatore».')
        if record.get('event') == 'escalation' and is_pr is not None \
                and self._retryable(record, is_pr):
            return RESUME + '\n\n' + operator_retry_text(record['step'])
        return RESUME

    def _operator_command(self, target, number, labels, comments, command, trace):
        """Apply a command of a trusted user on an issue or PR in ``agent:needs-operator``
        (#321), or answer why it does not apply and how to resume; the item stays with the
        operator unless a retry runs.

        - ``retry``: only when the latest record that put the item there (``_operator_anchor``)
          is an escalation that
          records a step a retry runs again, with its key (``_retryable``), whatever its class,
          critical included: a retry repeats the step and bypasses none of its checks. As for
          the supervisor, only while the input of the step is the one that failed, at most
          ``MAX_RETRIES`` times per key (the supervisor's retries count too), never on an issue
          a merged devloop PR resolved, and a blocked issue waits. A readiness check runs again
          only on an issue that still has ``agent:ready``, or ``agent:working`` while its
          implementation waits for the base branch (#459, ``_base_wait``). A PR goes back to the label of its
          step, an issue's step runs now; the operator labels (and ``agent:needs-supervisor``)
          go.
        - ``accept-defaults`` and ``decide``: only on an issue whose latest record is a failed
          readiness check of class ``preflight_default``, or a ``preflight_default``
          escalation the supervisor handed to the operator, under the conditions of the
          supervisor's commands (``_accept_defaults``, ``_decide``).
        - ``escalate``: never, the escalation is already the operator's.
        - ``grant-round`` and ``grant-budget``: never, they are delegations to the supervisor
          (#405, #411).
        - ``merge``: only from the supervisor's identity, on a PR (``_delegated_merge``, #486),
          or from the coordinator's, its reserved merge (#529); a trusted user's is answered:
          the operator merges by hand."""
        lb = self.cfg.labels
        is_pr = isinstance(target, PullRequest)
        name, anchor = command['command'], command['anchor']
        kind, record = anchor['kind'], anchor['record']
        stays = ('Il comando è speso; ' + ('la PR' if is_pr else 'l\'issue')
                 + f' resta in `{lb.needs_operator}`.')
        how = self._operator_resume(anchor, is_pr)

        def refuse(why, resume=how):
            return trace('refused', f'non applicabile: {why}. {stays}\n\n{resume}')

        if name == MERGE:
            supervisor = self._supervisor_login()
            coordinator = self._is_coordinator(command['by'])
            if not coordinator and (not supervisor or command['by'] != supervisor):
                # A delegation of the operator to the supervisor (#486): the operator merges a
                # PR by hand, as before.
                return refuse(f'`/devloop {MERGE}` è una delega del supervisore; l\'operatore '
                              'mergia la PR a mano')
            if not is_pr:
                return refuse(f'`/devloop {MERGE}` vale solo su una PR pronta il cui merge spetta '
                              'all\'operatore')
            return self._delegated_merge(target, number, comments, command, trace)
        if name == ESCALATE:
            return refuse('l\'escalation è già all\'operatore')
        if name == GRANT_ROUND:
            # A delegation of the operator to the supervisor (#405): the operator grants more
            # rounds by raising max_review_rounds, as before.
            return refuse(f'`/devloop {GRANT_ROUND}` è una delega del supervisore, solo su '
                          f'un\'escalation `{ROUND_GRANTABLE}`; l\'operatore concede altri round '
                          'alzando `max_review_rounds`')
        if name == GRANT_BUDGET:
            # A delegation of the operator to the supervisor (#411): the values of the envelope
            # and the extensions beyond it stay the operator's (ADR 0004, D2).
            return refuse(f'`/devloop {GRANT_BUDGET}` è una delega del supervisore, solo su '
                          f'un\'escalation `{BUDGET_GRANTABLE}`; i valori dell\'envelope e le '
                          'altre estensioni restano decisioni dell\'operatore')
        if name in (ACCEPT_DEFAULTS, DECIDE):
            # The check itself, or a supervisor's escalation of it handed to the operator.
            check = (kind == 'preflight' and preflight_class(record) == PREFLIGHT_DEFAULT
                     or anchor.get('origin') == PREFLIGHT_DEFAULT)
            if is_pr or not check:
                return refuse('vale solo su un\'issue il cui ultimo controllo di prontezza è '
                              'fallito con lacune tutte `default_available` e nessuna '
                              'suddivisione')
            if name == DECIDE:
                return self._decide(number, labels, comments, trace, command['reason'],
                                    operator=how,
                                    coordinator=self._is_coordinator(command['by']))
            return self._accept_defaults(number, labels, comments, trace, operator=how)
        if kind == 'preflight':
            return refuse('il controllo di prontezza non si ripete con `/devloop retry`')
        # A supervisor's escalation of a check's defaults or of a resolved issue, handed to the
        # operator, records no failed step, even when an earlier record kept its routing key.
        if (record.get('event') != 'escalation' or not self._retryable(record, is_pr)
                or anchor.get('origin') in (PREFLIGHT_DEFAULT, MERGED_ISSUE)):
            return refuse('quello che ha portato qui ' + ('la PR' if is_pr else 'l\'issue')
                          + ' non è un\'escalation che registri un passo da ripetere')
        key, step = record['key'], record['step']
        # The check that restarts an implementation waiting for its base (#459) runs on an
        # issue in agent:working, which keeps it.
        if step == 'preflight' and lb.ready not in labels and not (
                lb.working in labels and self._base_wait(comments, 'implement')):
            return refuse(f'il controllo di prontezza riparte rimettendo `{lb.ready}`, che '
                          'l\'issue non ha più', f'Per riprendere: rimuovere '
                          f'`{lb.needs_operator}` e rimettere `{lb.ready}`.')
        merged = [] if is_pr else self._merged_prs(number)
        if merged:
            return refuse('l\'issue è già risolta da una PR devloop mergiata ('
                          + ', '.join(f'#{m["number"]}' for m in merged) + '), quindi '
                          'ripeterne il passo duplicherebbe il lavoro',
                          'Se è risolta va chiusa; se serve altro lavoro, va aperta una nuova '
                          'issue.')
        # As for the supervisor, the base of an implementation does not bind its retry (#475).
        changed = changed_input(self._current_key(number, key, target if is_pr else None), key)
        if changed:
            return refuse('l\'input del passo è cambiato dopo l\'escalation ('
                          + ', '.join(f'`{k}`' for k in changed)
                          + '), quindi il comando si riferisce a un input superato', RESUME)

        def limit(done):
            trace('limit', f'non eseguito: il passo `{step}` è già stato ripetuto {done} volte '
                  f'per lo stesso input (al massimo {MAX_RETRIES}). {stays}\n\n{RESUME}', key=key)

        self._apply_retry(number, is_pr, comments, record,
                          sorted(labels & (lb.operator() | {lb.needs_supervisor})), trace, limit)

    def _accept_defaults(self, number, labels, comments, trace, operator=False):
        """Accept the defaults of the latest readiness check of issue ``number`` (#182): only
        when every gap is ``default_available``, no split is needed, the issue has no
        ``phase:design`` label, was opened by a trusted user or a devloop app, its text is
        still approved, and the check is the one of the current text. The dispatcher's record
        lists the defaults and counts as a trusted comment, so the issue is checked again with
        them and, if ready, implemented. Anything else is refused: a gap the operator must
        decide sends the issue to ``agent:needs-operator``, a superseded check leaves it to the
        supervisor. ``operator``: a trusted user's command on an issue in
        ``agent:needs-operator`` (#321), which stays there when refused and loses the operator
        labels when accepted."""
        record = self._decidable_check(number, labels, comments, trace,
                                       'I default del controllo di prontezza non possono essere '
                                       'accettati dal supervisore', 'i default si riferiscono',
                                       operator)
        if record is None:
            return
        defaults = [{'description': g['description'], 'proposed_default': g['proposed_default']}
                    for g in record['gaps']]
        listed = '\n'.join(f'- {d["description"].strip()}\n  *Default accettato:* '
                           f'{d["proposed_default"].strip()}' for d in defaults)
        trace('accepted', 'default del controllo di prontezza accettati. Questo record conta come '
              'un commento fidato: l\'issue torna in coda e il controllo si ripete con questi '
              f'default.\n\n{listed}', defaults=defaults, hash=record['hash'])
        self.gh.edit_labels(number, remove=self._decided_labels(labels, operator))

    def _decide(self, number, labels, comments, trace, text, operator=False, coordinator=False):
        """Record the supervisor's decision ``text`` on the gaps of the latest readiness check
        of issue ``number`` (#258), under the conditions of ``_accept_defaults``: the supervisor
        may accept some defaults and replace others. The dispatcher's record carries the text
        and the gaps it answers, and counts as a trusted comment: ``agent:needs-supervisor``
        goes, and the readiness check runs again with the decision as context. The supervisor's
        own comment never reaches a prompt: only this record does. ``operator``: as for
        ``_accept_defaults`` (#321). ``coordinator``: the command is the coordinator's (#528),
        refused on an issue whose check declares the trust policy touched
        (``_decidable_check``)."""
        record = self._decidable_check(number, labels, comments, trace,
                                       'Le lacune del controllo di prontezza non possono essere '
                                       'decise dal supervisore', 'la decisione si riferisce',
                                       operator, coordinator)
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
        self.gh.edit_labels(number, remove=self._decided_labels(labels, operator))

    def _decided_labels(self, labels, operator):
        """The labels an issue loses once its gaps are decided: ``agent:needs-supervisor`` and,
        for a command on an escalation to the operator (#321), the operator labels it has."""
        lb = self.cfg.labels
        if not operator:
            return [lb.needs_supervisor]
        return sorted(labels & (lb.operator() | {lb.needs_supervisor}))

    def _decidable_check(self, number, labels, comments, trace, refusal, what, operator=False,
                         coordinator=False):
        """The latest readiness check of issue ``number`` when the supervisor may decide its
        gaps (``accept-defaults`` or ``decide``), else None after the refusal is traced: every
        gap ``default_available`` and no split, no ``phase:design`` label, an issue opened by a
        trusted user or a devloop app whose text is still approved, and a check of the current
        text. A gap the operator must decide sends the issue to ``agent:needs-operator`` with
        ``refusal``; a superseded check leaves it to the supervisor. ``what``: what the command
        decides and its verb (``'i default si riferiscono'``), for the refusal of a superseded
        check. ``operator``: a command on an escalation to the operator (#321), refused with an
        answer only, the issue staying in ``agent:needs-operator``; a string is how the answer
        says to resume (``_operator_resume``), ``PREFLIGHT_RESUME`` otherwise.

        ``coordinator``: a ``decide`` of the coordinator's identity (#528), which decides no gap
        of the trust policy, of the credentials or of the budget. The last two are never
        ``default_available`` (the readiness check classifies them as ``operator_decision``,
        refused above); the first is refused here unless the check declares ``trust_policy``
        ``none``, and a check that does not declare it is refused too (it fails closed)."""
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
                   else self._untrusted_change(history)
                   or ('il controllo di prontezza dichiara toccata la policy di fiducia, o non '
                       'dice se la tocca: il coordinatore non la decide, decide l\'operatore'
                       if coordinator and record.get('trust_policy') != 'none' else None))
        stays = (f'l\'issue resta in `{self.cfg.labels.needs_operator}`.\n\n'
                 + (operator if isinstance(operator, str) else PREFLIGHT_RESUME) if operator
                 else 'l\'escalation resta al supervisore.\n\n'
                 + self._supervisor_resume({'class': PREFLIGHT_DEFAULT}, False, superseded=True))
        if problem:
            if operator:
                trace('refused', f'rifiutato: {problem}. Il comando è speso; {stays}')
                return None
            trace('refused', f'rifiutato: {problem}.')
            self._to_operator(number, f'{refusal}: {problem}.', 'operator_decision')
            return None
        if record['hash'] != self._request_hash(history, comments):
            trace('refused', 'rifiutato: il testo dell\'issue è cambiato dopo il controllo di '
                  f'prontezza, quindi {what} a un input superato. Il comando è speso; {stays}')
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
            elif not (self._may_hold(login) or self._coordinator_may(login, event)):
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

    def _closures(self, pr, comments):
        """The finding closures the dispatcher recorded in ``comments`` (``finding`` markers),
        oldest first, each with the id of the comment that records it (``comment``, only in
        memory): GitHub's comment ids grow with time, so the id tells whether a closure came
        before a ``/devloop fix`` (#536). A comment without an id leaves it None."""
        found = []
        for position, record in self._marker_entries(pr, comments, 'finding'):
            ident = comments[position].get('id')
            found += [dict(c, comment=ident if isinstance(ident, int)
                           and not isinstance(ident, bool) else None)
                      for c in record['closed']]
        return found

    def _fixes(self, pr, comments, entries):
        """The fix records of ``comments`` (``_markers``), each request of a ``/devloop fix``
        with the review records of ``entries`` (``(position in comments, record)``, as
        ``_marker_entries`` gives them) published before its comment (``after``, ``(round,
        owner)`` pairs, only in memory): the quorum approved the head the fix ran on only if
        its approval came before the request (#536). A review comment comes before when it
        precedes the request in the conversation; a native review, placed among the comments
        by its time (``with_native_reviews``), only when its time is earlier than the time of
        the request, not equal. A request whose comment is not in ``comments`` has none."""
        where = {c.get('id'): n for n, c in enumerate(comments)
                 if isinstance(c.get('id'), int) and not isinstance(c.get('id'), bool)
                 and not c.get('native')}
        found = []
        for record in self._markers(pr, comments, 'fix'):
            if isinstance(record.get('requests'), list):
                record = dict(record, requests=[
                    dict(q, after=self._reviewed_before(comments, entries, where.get(
                        q.get('comment')))) if isinstance(q, dict) else q
                    for q in record['requests']])
            found.append(record)
        return found

    @staticmethod
    def _reviewed_before(comments, entries, asked):
        """The ``(round, owner)`` of the review records of ``entries`` published before the
        comment at position ``asked`` of ``comments`` (``_fixes``); ``[]`` when it is None."""
        if asked is None:
            return []
        at = _epoch(comments[asked].get('created_at'))
        before = []
        for position, record in entries:
            if position >= asked:
                continue
            if comments[position].get('native'):
                time = _epoch(comments[position].get('created_at'))
                if time is None or at is None or time >= at:
                    continue
            before.append((record['round'], owner_of(record)))
        return before

    def _read_thread(self, pr, comments):
        """Fill ``pr`` with what its conversation records, from the identities allowed to write
        it: reviews, fixes, syncs, finding closures and commands, policy records, and the hold
        or release command still to apply (#172). The snapshot reads it once per tick; the merge
        reads it again just before merging, so that it never relies on an older view."""
        problems = []
        entries = sorted(self._marker_entries(pr, comments, 'review', problems),
                         key=lambda e: (e[1]['round'], e[0]))
        pr.reviews = [record for _, record in entries]
        pr.fixes = self._fixes(pr, comments, entries)
        pr.syncs = self._markers(pr, comments, 'sync')
        pr.reruns = self._markers(pr, comments, 'rerun')
        pr.closures = self._closures(pr, comments)
        state = finding_state(pr.reviews, pr.closures)
        pr.finding_problems = problems + state.problems
        pr.commands = self._finding_commands(pr, comments, state)
        policy = self._marker_entries(pr, comments, 'policy')
        pr.policy = [record for _, record in policy]
        pr.granted_rounds = self._granted_rounds(comments)
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
            # So does the supervisor's rereview the dispatcher applied (#491): its record. And
            # the coordinator's (#528), as a trusted user's: only its request, its text never
            # reaches a prompt.
            pr.rereview = (any(requests_rereview(c.get('body')) for c in pr.operator_comments)
                           or any(self._coordinator_may((c.get('author') or {}).get('login'),
                                                        REREVIEW)
                                  and not carries_record(c.get('body') or '')
                                  and (command_line(c.get('body')).split()[:2]
                                       == REREVIEW_COMMAND.split())
                                  for c in since)
                           or any(position > last and r.get('command') == REREVIEW
                                  and r.get('outcome') == 'applied' for position, r
                                  in self._dispatcher_records(comments, 'supervision')))
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
                   if not (self._may_hold(login) or self._coordinator_may(login, 'fix'))
                   else f'malformed, expected `{FIX_COMMAND}` followed by the request'
                   if not request
                   else f'a fix asked with `{FIX_COMMAND}` already ran on head '
                        f'{pr.head_sha[:12]}: a new command counts after a new commit'
                   if pr.head_sha in ran else None)
            if why:
                self.log(f'  PR #{pr.number}: ignored `{FIX_COMMAND}` by {login}: {why}')
                continue
            # ``url``: the permalink of the command for the fix prompt (#506), not recorded.
            found.append({'comment': ident, 'by': login, 'request': request,
                          'url': comment.get('url')})
        return found
