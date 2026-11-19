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
from .apps import DISPATCHER
from .config import same_agent
from .context import operator_comments, requests_rereview
from .github import GitHubError
from .journal import JournalError
from .ledger import entries as ledger_entries
from .markdown import (PREFLIGHT_RESUME, RESUME, carries_record, operator_retry_text,
                       read_marker, render_escalation, render_supervision)
from .planning import (FIX_COMMAND, Action, PullRequest, findings_decreasing, label_names,
                       last_counted_ci, operator_blocked)
from .policy import ZERO_LEVEL, profile_of
from .review import finding_state
from .supervision import (ACCEPT_DEFAULTS, CAPACITY, CAPACITY_REFUSED, DECIDE, DESIGN_LABEL,
                          ESCALATE, GRANT_ROUND, ISSUE_STEPS, MAX_CAPACITY, MAX_RETRIES,
                          MERGED_ISSUE, PREFLIGHT_DEFAULT, RETRY, RETRY_STEPS, ROUND_GRANTABLE,
                          SYNC_FAILED, TOOL_FAILURE, capacity_arguments, critical,
                          preflight_class, retries_done, supervisor_command)
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
        cannot be read counts as the supervisor's, so it goes to the operator."""
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
        level, an ``operator_only_paths`` path), a PR the supervisor implemented (except for a
        ``tool_failure`` with ``[delegations] retry_own_prs`` on, #406), an issue
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
                if self._supervisor_implemented(pr) and not (
                        cls == TOOL_FAILURE and cfg.delegations.retry_own_prs):
                    # A retry of a tool failure judges nothing (ADR 0005, P5-2, #406): with
                    # the delegation on, only that class leaves the supervisor's own PRs.
                    return None, 'il supervisore ha implementato questa PR' + (
                        ' (la delega `[delegations] retry_own_prs` è spenta)'
                        if cls == TOOL_FAILURE else '')
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
        ``number``: a retry of a superseded input is refused."""
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
        moved |= {p for p, r in self._dispatcher_records(comments, 'supervision')
                  if r.get('outcome') in ('applied', 'accepted', 'decided', 'granted')}
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
        answered as not applicable. A dispatcher ``supervision`` record spends the command it
        handled, whose comment id it carries, and every command before it. Among the commands
        still waiting the latest applicable one wins; without one, the latest not applicable
        (outside ``auto``, a decide or an escalate without a text, the supervisor on an
        operator's escalation) gets an answer. A command from anyone else is ignored and only
        logged. devloop's own records are never commands."""
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
        found = refused = None
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
            item = {'command': command, 'reason': reason, 'by': login, 'comment': ident,
                    'step': latest.get('step'), 'anchor': anchor}
            if operator:
                allowed = login in self.cfg.trusted_users
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
                    if not self._may_hold(login)
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
            found = item
        return found or refused

    def _to_operator(self, number, reason, cls, remove=(), retry=None):
        """Hand an item in ``agent:needs-supervisor`` to the operator: a critical escalation of
        class ``cls``. ``retry``: the step of the supervisor's escalation, its key and what a
        retry resumes, recorded again so that the operator may still retry it (#321)."""
        lb = self.cfg.labels
        self.gh.comment(number, render_escalation(reason, cls=cls, retry=retry))
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
        - ``grant-round``: only for a ``round_grantable`` escalation of a PR (``_grant_round``,
          #405).
        - ``retry``: only for ``tool_failure`` and ``sync_failed``, and only while the input of
          the step is the one that failed (the key of the escalation). A PR goes back to the
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
        escalations to the operator keep the step a retry runs and its key (#321)."""
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
        command = self._supervisor_view(number, comments, operator)
        if command is None:
            self.log('  no command to apply')
            return
        escalation = command['anchor']['record']
        cls = escalation.get('class')
        name, by = command['command'], command['by']
        # The step a retry runs again stays recorded on the way to the operator (#321), only for
        # the classes a retry admits: the key of a preflight_default or merged_issue escalation
        # only routes it.
        carry = ({k: escalation[k] for k in ('step', 'key', 'resume') if k in escalation}
                 if cls in (TOOL_FAILURE, SYNC_FAILED) and self._retryable(escalation, is_pr)
                 else None)
        # A tool failure of an issue loses agent:ready on its way to the operator, as any failed
        # step does; the readiness check keeps it, and so does a readiness check the operator
        # may retry, which runs again only with agent:ready.
        keep_ready = is_pr or cls == PREFLIGHT_DEFAULT
        human_remove = [] if keep_ready else [lb.ready]
        carried_remove = [] if keep_ready or (carry and carry['step'] == 'preflight') \
            else [lb.ready]

        def trace(outcome, text, **extra):
            record = dict(command=name, by=by, reason=command['reason'] or '',
                          comment=command['comment'], outcome=outcome, **extra)
            record['class'] = cls if isinstance(cls, str) else None
            self.gh.comment(number, render_supervision(
                record, f'`/devloop {name}` di `{by}`: {text.strip()}',
                OPERATOR_TITLE if operator else SUPERVISOR_TITLE))
            self.log(f'  {name} by {by}: {outcome}')

        stays = 'Il comando è speso; l\'escalation resta al supervisore.'
        if command.get('refusal'):
            where = 'la PR' if is_pr else 'l\'issue'
            how = (f'Il comando è speso; {where} resta in `{lb.needs_operator}`.\n\n'
                   + self._operator_resume(command['anchor'], is_pr) if operator
                   else f'Il comando è speso; {where} resta in `{lb.needs_supervisor}`.\n\n'
                   + self._supervisor_resume(escalation, is_pr))
            return trace('refused', f'non applicabile: {command["refusal"]}. {how}')
        if name == CAPACITY:
            # A temporary capacity limit is never a decision on an escalation (#412).
            where = 'la PR' if is_pr else 'l\'issue'
            label = lb.needs_operator if operator else lb.needs_supervisor
            resume = (self._operator_resume(command['anchor'], is_pr) if operator
                      else self._supervisor_resume(escalation, is_pr))
            return trace('refused', f'non applicabile: `/devloop {CAPACITY}` vale solo sulla '
                         'issue del riepilogo (`[report] issue`), non su un\'escalation. Il '
                         f'comando è speso; {where} resta in `{label}`.\n\n{resume}')
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
        if name in (ACCEPT_DEFAULTS, DECIDE):
            if is_pr or cls != PREFLIGHT_DEFAULT:
                return trace('refused', 'rifiutato: vale solo per un\'escalation `preflight_default` '
                             f'del controllo di prontezza. {stays}\n\n'
                             + self._supervisor_resume(escalation, is_pr))
            if name == DECIDE:
                return self._decide(number, labels, comments, trace, command['reason'])
            return self._accept_defaults(number, labels, comments, trace)
        if cls not in (TOOL_FAILURE, SYNC_FAILED):
            return trace('refused', 'rifiutato: vale solo per le escalation `tool_failure` e '
                         f'`sync_failed`. {stays}\n\n' + self._supervisor_resume(escalation, is_pr))
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

        def superseded():
            return trace('refused', 'rifiutato: l\'input del passo è cambiato dopo '
                         'l\'escalation (' + ', '.join(f'`{k}`' for k in sorted(current)
                                                        if current[k] != key.get(k))
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
            if current != key:
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
        if current != key:
            return superseded()

        def limit(done):
            trace('limit', f'non eseguito: il passo `{step}` è già stato ripetuto {done} volte '
                  'per lo stesso input.', key=key)
            self._to_operator(number, f'Il passo `{step}` è fallito di nuovo dopo {done} '
                              f'tentativi autorizzati dal supervisore (al massimo '
                              f'{MAX_RETRIES}): `/devloop {name}` di `{by}` non è stato '
                              'eseguito.', 'retry_limit', human_remove)

        self._apply_retry(number, is_pr, comments, escalation, [lb.needs_supervisor], trace,
                          limit)

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
               if not (last_counted_ci(reviews) or findings_decreasing(reviews, pr.closures))
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
            return refuse(f'sulla issue del riepilogo vale solo `/devloop {CAPACITY}`')
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
          only on an issue that still has ``agent:ready``. A PR goes back to the label of its
          step, an issue's step runs now; the operator labels (and ``agent:needs-supervisor``)
          go.
        - ``accept-defaults`` and ``decide``: only on an issue whose latest record is a failed
          readiness check of class ``preflight_default``, or a ``preflight_default``
          escalation the supervisor handed to the operator, under the conditions of the
          supervisor's commands (``_accept_defaults``, ``_decide``).
        - ``escalate``: never, the escalation is already the operator's."""
        lb = self.cfg.labels
        is_pr = isinstance(target, PullRequest)
        name, anchor = command['command'], command['anchor']
        kind, record = anchor['kind'], anchor['record']
        stays = ('Il comando è speso; ' + ('la PR' if is_pr else 'l\'issue')
                 + f' resta in `{lb.needs_operator}`.')
        how = self._operator_resume(anchor, is_pr)

        def refuse(why, resume=how):
            return trace('refused', f'non applicabile: {why}. {stays}\n\n{resume}')

        if name == ESCALATE:
            return refuse('l\'escalation è già all\'operatore')
        if name == GRANT_ROUND:
            # A delegation of the operator to the supervisor (#405): the operator grants more
            # rounds by raising max_review_rounds, as before.
            return refuse(f'`/devloop {GRANT_ROUND}` è una delega del supervisore, solo su '
                          f'un\'escalation `{ROUND_GRANTABLE}`; l\'operatore concede altri round '
                          'alzando `max_review_rounds`')
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
                                    operator=how)
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
        if step == 'preflight' and lb.ready not in labels:
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
        current = self._current_key(number, key, target if is_pr else None)
        if current != key:
            return refuse('l\'input del passo è cambiato dopo l\'escalation ('
                          + ', '.join(f'`{k}`' for k in sorted(current)
                                      if current[k] != key.get(k))
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

    def _decide(self, number, labels, comments, trace, text, operator=False):
        """Record the supervisor's decision ``text`` on the gaps of the latest readiness check
        of issue ``number`` (#258), under the conditions of ``_accept_defaults``: the supervisor
        may accept some defaults and replace others. The dispatcher's record carries the text
        and the gaps it answers, and counts as a trusted comment: ``agent:needs-supervisor``
        goes, and the readiness check runs again with the decision as context. The supervisor's
        own comment never reaches a prompt: only this record does. ``operator``: as for
        ``_accept_defaults`` (#321)."""
        record = self._decidable_check(number, labels, comments, trace,
                                       'Le lacune del controllo di prontezza non possono essere '
                                       'decise dal supervisore', 'la decisione si riferisce',
                                       operator)
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

    def _decidable_check(self, number, labels, comments, trace, refusal, what, operator=False):
        """The latest readiness check of issue ``number`` when the supervisor may decide its
        gaps (``accept-defaults`` or ``decide``), else None after the refusal is traced: every
        gap ``default_available`` and no split, no ``phase:design`` label, an issue opened by a
        trusted user or a devloop app whose text is still approved, and a check of the current
        text. A gap the operator must decide sends the issue to ``agent:needs-operator`` with
        ``refusal``; a superseded check leaves it to the supervisor. ``what``: what the command
        decides and its verb (``'i default si riferiscono'``), for the refusal of a superseded
        check. ``operator``: a command on an escalation to the operator (#321), refused with an
        answer only, the issue staying in ``agent:needs-operator``; a string is how the answer
        says to resume (``_operator_resume``), ``PREFLIGHT_RESUME`` otherwise."""
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
