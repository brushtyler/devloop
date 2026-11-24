"""Readiness check of the dispatcher: the mixin ``PreflightMixin`` of ``Dispatcher``.

Moved from ``devloop/dispatcher.py`` without changes (#294, step S4 of #261): the reading of an
issue's readiness checks (``_preflight_records``, ``_preflight_view``), the requests for a new
check (``_recheck_requested``), the attribution of the block it set (``_owns_block``), the
recording of its outcome (``_publish_preflight``) and the check itself (``_do_preflight``,
``_do_preflight_block``), with their helpers and the module-level definitions they need.
``Dispatcher`` inherits the methods; since #297 (step S7) the module-level names are imported
from here, not re-exported by ``devloop/dispatcher.py``.
"""

from .agents import AgentError
from .apps import DISPATCHER, ISSUE_COMMENT
from .context import (APPROVE_COMMAND, _render_issue_comments, _template,
                      format_operator_decisions, prompt_values, trust_policy_rule)
from .dispatch_actions import Models, ready_approval
from .dispatch_supervision import UntrustedChange
from .markdown import (carries_record, read_marker, render_approve_refused, render_escalation,
                       render_preflight, render_recheck)
from .planning import (RECHECK_COMMAND, label_names, operator_blocked, preflight_state,
                       repository_bound)
from .supervision import (ACCEPT_DEFAULTS, DECIDE, PREFLIGHT_DEFAULT, command_line, describe,
                          preflight_class)
from .workspace import WorkspaceError

# The classes of a failed readiness check (``preflight_class``): a block of one of them, with no
# escalation of another class after it, is the check's to repeat on a request of a person (#468).
PREFLIGHT_CLASSES = ('operator_decision', PREFLIGHT_DEFAULT)


# The question of a readiness check to the operator on a change to the trust policy that no
# decision covers (#472): the operator decides on the issue, before the implementation.
TRUST_QUESTION = 'Approvi la modifica della policy di fiducia descritta qui?'
# The gap of a readiness check on an issue the coordinator opened that touches the trust policy,
# or does not say whether it does, queued by no trusted user (#595, ``_coordinator_gate``).
COORDINATOR_TRUST_GAP = ('L\'issue è stata aperta dal coordinatore e il controllo di prontezza '
                         'dichiara toccata la policy di fiducia (`trust_policy` diverso da '
                         '`none`), ma l\'ultimo `agent:ready` non è di un utente fidato: la '
                         'mette in coda solo l\'operatore, rimettendo `agent:ready` di persona.')
# The same gap on any other issue not opened by a trusted user, such as the supervisor's, with
# ``[roles.coordinator]`` (#607): the coordinator's authorization does not queue it.
AUTHORIZED_TRUST_GAP = ('L\'issue non è stata aperta da un utente fidato e il controllo di '
                        'prontezza dichiara toccata la policy di fiducia (`trust_policy` diverso '
                        'da `none`), ma l\'ultimo `agent:ready` non è di un utente fidato: anche '
                        'se il coordinatore l\'ha autorizzata, la mette in coda solo '
                        'l\'operatore, rimettendo `agent:ready` di persona.')


def devloop_text(body):
    """Text devloop or its agents published: it starts with the AGENTS.md header."""
    return (body or '').lstrip().startswith('> [!NOTE]')


def normalize_preflight(raw):
    """The outcome of a readiness check: ready only without gaps and without a split, whatever
    the agent says (a ``default_available`` gap blocks too). A gap depends on the base branch
    (``repository_dependent``, #270) only when the agent says so.

    A change to the trust policy that no decision of the operator covers (``trust_policy``
    ``uncovered``, #472) is a first gap of kind ``operator_decision``, which devloop writes
    itself: the question to the operator with the agent's reason. The schema requires the two
    fields; an output without them (written before #472) is read as before."""
    gaps = [dict(g, code_refs=list(g['code_refs']),
                 repository_dependent=g.get('repository_dependent') is True)
            for g in raw['gaps']]
    trust = {}
    if 'trust_policy' in raw:
        reason = (raw.get('trust_policy_reason') or '').strip()
        trust = {'trust_policy': raw['trust_policy'], 'trust_policy_reason': reason}
        if raw['trust_policy'] == 'uncovered':
            gaps.insert(0, {'kind': 'operator_decision',
                            'description': f'{TRUST_QUESTION} {reason}'.strip(),
                            'code_refs': [], 'proposed_default': '',
                            'repository_dependent': False})
    ready = bool(raw['ready']) and not gaps and not raw['split'].strip()
    return {'ready': ready, 'gaps': gaps, 'split': raw['split'], **trust}


class PreflightMixin:
    """The methods of ``Dispatcher`` for the readiness check (#294). They use the dispatcher
    state and its other methods through ``self``."""

    # Readiness check (#114) ---------------------------------------------------
    def _preflight_records(self, comments):
        """``(position, record)`` of the readiness checks in ``comments``, each written by the
        identity of the configured agent it names."""
        found = []
        for position, comment in enumerate(comments):
            record = read_marker(comment.get('body', ''), 'preflight')
            login = (comment.get('author') or {}).get('login')
            if record and self._wrote_as_agent(login, record['agent']):
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

    def _supervision_record(self, comment):
        """Whether ``comment`` is the dispatcher's trace of a command (#182, #321), signed by
        it: an answer, a decision or a retry, never an escalation (that is a notice)."""
        return (read_marker(comment.get('body', ''), 'supervision') is not None
                and self._signed((comment.get('author') or {}).get('login')))

    def _recheck_record(self, comment):
        """Whether ``comment`` is the dispatcher's answer to a recheck that does not run (#468),
        signed by it."""
        return (read_marker(comment.get('body', ''), 'recheck') is not None
                and self._signed((comment.get('author') or {}).get('login')))

    def _approve_record(self, comment):
        """Whether ``comment`` is the dispatcher's answer to the ``/devloop approve`` it
        refuses (#483), signed by it."""
        return (read_marker(comment.get('body', ''), 'approve') is not None
                and self._signed((comment.get('author') or {}).get('login')))

    def _answered_approvals(self, comments, records):
        """The ids of the comments with a ``/devloop approve`` already answered as refused
        (#483): those a readiness check of ``records`` (``_preflight_records``) lists in its
        ``refused``, and those of the dispatcher's ``approve`` answers."""
        ids = {i for _, record in records for i in record.get('refused', ())}
        for c in comments:
            if self._approve_record(c):
                ids.update(read_marker(c['body'], 'approve')['comments'])
        return ids

    def _unanswered_approvals(self, comments, records):
        """The ``/devloop approve`` of ``comments`` that devloop refuses (``_refused_approvals``,
        #483) and has not answered yet (``_answered_approvals``), oldest first, as ``(comment,
        reason)``. Each is answered once, by its id: a comment without one is never answered,
        since nothing could tell that it was."""
        answered = self._answered_approvals(comments, records)
        return [(c, why) for c, why in self._refused_approvals(comments)
                if isinstance(c.get('id'), int) and c['id'] not in answered]

    def _supervisor_note(self, comment):
        """Whether ``comment`` is a text of the supervisor's identity (``_supervisor_login``)
        that carries neither an escalation (``notice``) nor a readiness check (#468). Without
        an identity of its own the supervisor cannot be told apart, and this is never true."""
        supervisor = self._supervisor_login()
        body = comment.get('body', '')
        return (bool(supervisor) and (comment.get('author') or {}).get('login') == supervisor
                and read_marker(body, 'notice') is None
                and read_marker(body, 'preflight') is None)

    def _automatic(self, comment):
        """Whether ``comment`` is one of devloop's own records: published text (``_published``)
        that ends with a valid devloop marker. Only these leave the readiness hash: any other
        trusted comment, even one starting with the header, may carry a requirement."""
        return self._published(comment) and carries_record(comment.get('body'))

    def _recheck_requested(self, number, comments):
        """Whether a trusted user asked in person for a new check with ``RECHECK_COMMAND`` as
        the first line of a comment written after devloop's latest record there: each request
        starts one check. So does the coordinator's identity (#528), whose request may follow
        the attribution header of the agents. Requests from anyone else are ignored and
        logged."""
        return self._recheck_request(number, comments) is not None

    def _recheck_request(self, number, comments, log=True):
        """The latest comment ``_recheck_requested`` counts, or None. ``log``: whether to log
        the requests it ignores."""
        # The answer to a ``/devloop approve`` refused (#483) answers no request for a check.
        last = max((position for position, c in enumerate(comments)
                    if self._automatic(c) and not self._approve_record(c)), default=-1)
        asked = None
        for comment in comments[last + 1:]:
            body = comment.get('body') or ''
            login = (comment.get('author') or {}).get('login')
            if self._is_coordinator(login):
                # The coordinator is an agent: its text carries the attribution header (#528).
                if carries_record(body) or command_line(body) != RECHECK_COMMAND:
                    continue
                asked = comment
                continue
            lines = body.strip().splitlines()
            if devloop_text(body) or not lines or lines[0].strip() != RECHECK_COMMAND:
                continue
            if login in self.cfg.trusted_users:
                asked = comment
            elif log:
                self.log(f'  issue #{number}: ignored `{RECHECK_COMMAND}` by {login}: the author '
                         'is not a trusted user')
        return asked

    def _request(self, number, history, comments, records, log=True):
        """The request of a person for a new check still pending (#468), or None:
        ``{request, by}``, ``request`` being ``recheck`` (``_recheck_request``) or
        ``relabeled`` (``_relabel``), and in ``labeled`` the time of a pending ``agent:ready``
        added again, if any."""
        relabel = self._relabel(history, comments, records)
        asked = self._recheck_request(number, comments, log)
        if asked is None and relabel is None:
            return None
        if asked is not None:
            found = {'request': 'recheck', 'by': (asked.get('author') or {}).get('login')}
        else:
            found = {'request': 'relabeled', 'by': relabel[0]}
        if relabel is not None:
            found['labeled'] = relabel[1]
        return found

    def _preflight_block(self, number, labels, comments, records):
        """``(log, text)`` of why the issue's ``agent:needs-operator`` is not the readiness
        check's latest block, for a request of a person to repeat the check (#468), or None
        when it is: the label has one name only (``_owned_block``), the latest check set it
        (its record is ``blocked``, of a class of ``PREFLIGHT_CLASSES``), no escalation of
        another class came after the check (a ``notice`` from an identity allowed to write
        devloop records) and the dispatcher wrote the only ``labeled`` event of the label
        since. Unlike ``_owns_block``, any other text after the check leaves the block to it:
        the person asking has read it. ``log`` is for the log, ``text`` for the answer on the
        issue."""
        if not self._owned_block(labels):
            return ('the operator label is there under both its names',
                    '`agent:needs-operator` è presente con entrambi i suoi nomi, e quale dei '
                    'due sia del controllo non si può dire')
        if (not records or not records[-1][1]['blocked']
                or records[-1][1].get('class') not in PREFLIGHT_CLASSES):
            return ('the latest readiness check did not set the block',
                    'l\'ultimo controllo di prontezza non ha messo `agent:needs-operator`, che '
                    'era già presente')
        position, record = records[-1]
        for comment in comments[position + 1:]:
            notice = read_marker(comment.get('body', ''), 'notice')
            if (notice is not None and notice.get('class') != record['class']
                    and self._writes_records((comment.get('author') or {}).get('login'))):
                cls = notice.get('class')
                shown = f'`{cls}`' if isinstance(cls, str) else 'non indicata'
                return (f'an escalation of class {cls} came after the readiness check',
                        f'dopo il controllo di prontezza c\'è un\'escalation di classe {shown} '
                        f'({describe(cls)}): l\'ultimo blocco dell\'issue è quello, non il '
                        'controllo di prontezza')
        labelers = self._operator_labelers(number)
        if not (len(labelers) == record['labeled'] + 1 and self._wrote(labelers[-1], DISPATCHER)):
            return ('agent:needs-operator was added again after the readiness check, not by it',
                    '`agent:needs-operator` è stato aggiunto di nuovo dopo il controllo di '
                    'prontezza, e non dal controllo')
        return None

    def _supervisor_block(self, comments, records):
        """``(log, text)`` of why an issue in ``agent:needs-supervisor`` runs no check on a
        request of a person (#468), as ``_preflight_block``: it waits for the supervisor, for
        the latest escalation (a ``notice`` from an identity allowed to write devloop records)
        after the latest readiness check, if any."""
        start = records[-1][0] if records else -1
        cls = None
        for comment in comments[start + 1:]:
            notice = read_marker(comment.get('body', ''), 'notice')
            if notice is not None and self._writes_records(
                    (comment.get('author') or {}).get('login')):
                cls = notice.get('class')
        label = self.cfg.labels.needs_supervisor
        if not isinstance(cls, str):
            return (f'the issue waits for the supervisor ({label})',
                    f'l\'issue è in `{label}` e attende il supervisore')
        return (f'the issue waits for the supervisor ({label}) on an escalation of class {cls}',
                f'l\'issue è in `{label}` e attende il supervisore, per un\'escalation di classe '
                f'`{cls}` ({describe(cls)}): l\'ultimo blocco dell\'issue è quello, non il '
                'controllo di prontezza')

    def _answered(self, comments, records, request):
        """Whether the dispatcher already answered ``request``: a ``/devloop recheck`` is spent
        by any devloop record after it (``_recheck_request``), ``agent:ready`` added again by a
        ``recheck`` record after the latest check that names the time of its label."""
        if request['request'] == 'recheck':
            return False
        start = records[-1][0] if records else -1
        return any(position > start and record.get('labeled') == request['labeled']
                   for position, record in self._dispatcher_records(comments, 'recheck'))

    def _owns_block(self, number, comments, records):
        """Whether the issue's ``agent:needs-operator`` belongs to the readiness check: the
        latest check added it when the label was absent (its record says how many ``labeled``
        events of the label came before), the dispatcher wrote the only ``labeled`` event
        since, and no other text devloop may have published (another escalation,
        ``_published``) came after that check: an identity that writes no devloop record cannot
        void the block's attribution, nor can a note of the supervisor (``_supervisor_note``,
        #468). The label counts under both its names (#248), the events
        of the old one included (``_operator_labelers``). Anything else, or anything uncertain,
        leaves the block to the operator."""
        if not records:
            return False
        position, record = records[-1]
        # The dispatcher's ledger records (#133), such as the turn of a recheck, its traces of
        # commands (#321), such as the answer to a command that does not apply, and its answers
        # to a recheck that runs no check (#468) are no escalation: they leave the block's
        # attribution as it is. Nor does a comment of the supervisor that carries neither an
        # escalation nor a readiness check (#468), such as its note on a decision for the
        # operator, nor its answer to a ``/devloop approve`` refused (#483).
        if not record['blocked'] or any(self._published(c) and not self._ledger_record(c)
                                        and not self._supervision_record(c)
                                        and not self._recheck_record(c)
                                        and not self._approve_record(c)
                                        and not self._supervisor_note(c)
                                        for c in comments[position + 1:]):
            return False
        labelers = self._operator_labelers(number)
        return (len(labelers) == record['labeled'] + 1
                and self._wrote(labelers[-1], DISPATCHER))

    def _owned_block(self, labels):
        """Whether ``labels`` carry the operator label under one name only (#248): with both,
        which one the readiness check added cannot be told, and the block is the operator's."""
        return len(set(labels) & self.cfg.labels.operator()) == 1

    def _preflight_view(self, number, labels, base_head=None):
        """What planning needs about an issue's readiness check, read now from GitHub:
        ``{hash, record, recheck, owned, relabeled, refused, approvals, base}``, ``record`` being the
        latest check or None; a positive one passes ``_coordinator_gate`` again, on the labels
        of now (#595). ``relabeled`` and ``base`` (#270) are those of
        ``preflight_state``: the second is read, with ``base_head`` (``ws.base_head`` by
        default), only for a failure that depends on the base branch (``repository_bound``).
        ``owned``: whether a new check may lift ``agent:needs-operator``, the check's own block
        (``_owns_block``) or, on a request of a person, its latest one (``_preflight_block``,
        #468). ``refused``: a request of a person (``_request``) that runs no check, with why,
        until it is answered (``_do_recheck_refused``); None otherwise. On an issue in
        ``agent:needs-supervisor`` every request runs none (``_supervisor_block``).
        ``approvals``: how many ``/devloop approve`` devloop refuses and has not answered yet
        (``_unanswered_approvals``, #483)."""
        history = self.gh.issue_history(number)
        comments = self.gh.issue_comments(number)
        records = self._preflight_records(comments)
        record = records[-1][1] if records else None
        if record is not None and record['ready']:
            # A positive check reused holds for the issue's labels now (#595): on a
            # coordinator's issue that touches the trust policy, the latest agent:ready must
            # still be a trusted user's, or the outcome reads as failed (``preflight_block``).
            record = self._coordinator_gate(history, record)
        base = None
        if repository_bound(record):
            try:
                base = (base_head or self.ws.base_head)()
            except WorkspaceError as exc:
                self.log(f'  issue #{number}: the base branch not read, the failed readiness '
                         f'check holds: {exc}')
        owned = self._owned_block(labels) and self._owns_block(number, comments, records)
        request = self._request(number, history, comments, records)
        refused = why = None
        if request and not owned and operator_blocked(labels, self.cfg.labels):
            # A request of a person repeats the check of a block that is still the issue's
            # latest, even if no longer the check's own (#468); otherwise it is answered once.
            why = self._preflight_block(number, labels, comments, records)
            owned = why is None
        if request and self.cfg.labels.needs_supervisor in labels:
            # An issue that waits for the supervisor runs no check (#468): its block stays,
            # and the request is answered all the same, with the escalation that holds it.
            why = why or self._supervisor_block(comments, records)
        if why and not self._answered(comments, records, request):
            refused = dict(request, log=why[0], reason=why[1])
        return {'hash': self._request_hash(history, comments),
                'record': record,
                'recheck': request is not None and request['request'] == 'recheck',
                'owned': owned,
                'relabeled': self._relabeled(history, comments, records),
                'refused': refused,
                'approvals': len(self._unanswered_approvals(comments, records)),
                'base': base}

    def _relabeled(self, history, comments, records):
        """The login that added ``agent:ready`` again after the latest readiness check, when
        that check failed (#270), else None. Only the latest ``labeled`` event of the label
        counts, and only when one of the issue's admitters wrote it (``ready_approval``): a
        trusted user or, where its labels count, the supervisor's app. It must come after the
        comment of the check, by its time; a comment without one never counts as relabeled.
        GitHub timestamps have one-second granularity: a label in the same second as the check
        cannot be proven to follow it, and does not count."""
        relabel = self._relabel(history, comments, records)
        return relabel[0] if relabel else None

    def _relabel(self, history, comments, records):
        """``(login, time)`` of the ``agent:ready`` that ``_relabeled`` counts, or None."""
        if not records or records[-1][1]['ready']:
            return None
        approved = ready_approval(history['labeled'], self.cfg.labels.ready,
                                  self._admitters(history['author'], coordinator=True))
        written = comments[records[-1][0]].get('created_at')
        if approved and isinstance(written, str) and approved[1] > written:
            return approved[0], approved[1]
        return None

    def _publish_preflight(self, number, outcome, digest, agent_key, note='', base=None,
                           used=None, request=None, refused=()):
        """Record a check's outcome on the issue and apply it to ``agent:needs-operator``: add
        the label when the check fails and the label is absent, remove it when the check passes
        and the label belongs to the readiness check, under whichever name it has (#248). A
        label someone else set stays. ``base``: the commit the check ran on, recorded with the
        outcome (#270); None for an outcome reused (the latest base then), which keeps the
        commit of its own record, if any. ``used``: the configuration the check ran with, a
        fallback model's included (#148), for the header; the configured agent by default.
        ``request``: the request of a person (``_request``) pending when the check started, if
        any: the turn's ledger record spends a ``/devloop recheck`` before the outcome is
        recorded (#468). ``refused``: the ``/devloop approve`` the check answers as refused
        (#483), as ``(comment, reason)``: ``render_preflight`` shows them, and the record names
        their ids, so that each is answered once. Escalations and label events are read again here all
        the same."""
        lb = self.cfg.labels
        agent = used or self.cfg.agent(agent_key)
        labels = label_names(self.gh.issue(number).get('labels') or [])
        comments = self.gh.issue_comments(number)
        records = self._preflight_records(comments)
        present = operator_blocked(labels, lb)
        owned = self._owned_block(labels) and self._owns_block(number, comments, records)
        if present and not owned and (request or self._request(
                number, self.gh.issue_history(number), comments, records, log=False)):
            # Asked by a person (#468): the block is the check's while it is the issue's latest.
            owned = self._preflight_block(number, labels, comments, records) is None
        shown =', '.join(f'`{name}`' for name in sorted(labels & lb.operator()))
        record = dict(outcome, hash=digest, agent=agent_key, blocked=False, labeled=0)
        if base:
            record['base'] = base
        if refused:
            record['refused'] = [c['id'] for c, _ in refused]
        refused = [((c.get('author') or {}).get('login'), c.get('created_at'), why)
                   for c, why in refused]
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
            author.comment(number, render_preflight(record, head, note, refused))
            self.log('  ready' + (', block removed' if owned else ''))
            return
        # The class of the escalation (#182): in "auto" gaps that are all defaults go to the
        # supervisor, with a dispatcher escalation that carries the key of the check's input.
        record['class'] = cls = preflight_class(outcome)
        supervised, why = (None, '') if present else self._supervised(
            number, cls, 'preflight',
            key=self._issue_key('preflight', base, digest) if base else None)
        if supervised:
            author.comment(number, render_preflight(record, head, note, refused))
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
        author.comment(number, render_preflight(record, head, note, refused))
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
        # The request of a person that started the check, if any (#468), kept for its outcome:
        # with the ledger on, the turn's record spends a ``/devloop recheck`` before it.
        records = self._preflight_records(comments)
        request = self._request(number, history, comments, records, log=False)
        # #483: the ``/devloop approve`` that decide nothing, not answered yet, are answered
        # with this one.
        refused = self._unanswered_approvals(comments, records)
        self._log_refused(number, refused)
        prompt = _template('preflight', **prompt_values(cfg), repo=cfg.repo, base_branch=cfg.base_branch,
                           issue_number=number, issue_title=history['title'],
                           issue_body=history['body'],
                           issue_comments=_render_issue_comments(
                               comments, cfg.trusted_users,
                               [read_marker(c['body'], 'supervision')
                                for c in comments if self._accepted(c)]),
                           # #472: the rule of the reviewers, and the comments that may record
                           # a decision of the operator on the trust policy.
                           trust_policy=trust_policy_rule(),
                           operator_decisions=format_operator_decisions(
                               self._issue_decisions(comments, history)),
                           language=cfg.language)
        name = f'issue-{number}-preflight'
        path = self.ws.base_copy(name)
        try:
            # The input the check runs on (#182), bound before it runs: the commit of its
            # checkout and the hash of the request its prompt gets.
            base = self.ws.head(path)
            key = self._issue_key('preflight', base, digest)
            # The chain of models of ``_turn`` (#148): an unavailable model leaves the turn to
            # the next one, with a note fit for the check; a denied permission is retried once,
            # in the same checkout (#180, #319).
            try:
                raw, used = self._turn(Models(agent), 'review', prompt, 'preflight', path, name,
                                       where=(number, 'preflight'), note='preflight')
            except AgentError as exc:
                exc.key = key
                raise
        finally:
            self.ws.remove(name)
        outcome = self._coordinator_gate(history, normalize_preflight(raw))
        self._publish_preflight(number, outcome, digest, agent.key, base=base,
                                used=used, request=request, refused=refused)

    def _coordinator_gate(self, history, outcome):
        """``outcome`` of a readiness check, not ready with a first gap of kind
        ``operator_decision`` (``COORDINATOR_TRUST_GAP``) on an issue the coordinator opened
        (#595) when the check declares the trust policy touched (``trust_policy`` other than
        ``none``, or not declared: it fails closed) and the latest ``agent:ready`` of the
        issue's ``history`` is no trusted user's. Its labels and the supervisor's count there,
        since its app is one of devloop's (``_devloop_logins``), but a change to the trust policy
        is queued by the operator only: the issue goes to ``agent:needs-operator`` without
        starting, and the operator's ``agent:ready`` added again runs a new check that admits
        it (``_relabeled``). Any other outcome is returned as it is. A positive outcome reused
        passes the gate again (``_preflight_view``): an operator's ``agent:ready`` replaced by the
        coordinator's or the supervisor's makes it read as failed, and blocks the issue again
        (``_do_preflight_block``).

        With ``[roles.coordinator]`` the gate holds on every issue not opened by a trusted user
        (#607), such as the supervisor's: its authorization by the coordinator
        (``/devloop authorize``) does not queue a change of the trust policy, which stays the
        operator's (``AUTHORIZED_TRUST_GAP``)."""
        author = history['author']
        if (self._coordinator_login() is None or author in self.cfg.trusted_users
                or outcome.get('trust_policy') == 'none'):
            return outcome
        if ready_approval(history['labeled'], self.cfg.labels.ready, self.cfg.trusted_users):
            return outcome
        text = COORDINATOR_TRUST_GAP if self._is_coordinator(author) else AUTHORIZED_TRUST_GAP
        gap = {'kind': 'operator_decision', 'description': text,
               'code_refs': [], 'proposed_default': '', 'repository_dependent': False}
        return dict(outcome, ready=False, gaps=[gap, *outcome['gaps']])

    def _log_refused(self, number, refused):
        """Log the ``/devloop approve`` ``refused`` (#483), as ``(comment, reason)``."""
        for c, why in refused:
            login = (c.get('author') or {}).get('login')
            self.log(f'  issue #{number}: refused `{APPROVE_COMMAND}` by {login}: {why}')

    def _do_approve_refused(self, number):
        """Answer, once, the ``/devloop approve`` devloop refuses that no readiness check
        answered (#483), such as one written after the latest check: it changes neither the
        hash of the request nor anything else, so no new check runs to answer it. The comments
        are read again first: commands answered since the snapshot are left alone."""
        comments = self.gh.issue_comments(number)
        refused = self._unanswered_approvals(comments, self._preflight_records(comments))
        if not refused:
            self.log(f'  the refused `{APPROVE_COMMAND}` were answered since the snapshot')
            return
        self._log_refused(number, refused)
        self.gh.comment(number, render_approve_refused(
            [c['id'] for c, _ in refused],
            [((c.get('author') or {}).get('login'), c.get('created_at'), why)
             for c, why in refused]))

    def _do_recheck_refused(self, number):
        """Answer, once, a request of a person for a new check that runs none (#468): the
        issue's latest block is not the readiness check's. The view is read again first: a
        request answered, or able to run, since the snapshot is left alone."""
        labels = label_names(self.gh.issue(number).get('labels') or [])
        refused = self._preflight_view(number, labels)['refused']
        if refused is None:
            self.log('  the request for a new readiness check changed since the snapshot')
            return
        self.log(f'  {refused["request"]} by {refused["by"]} runs no readiness check: '
                 f'{refused["log"]}')
        record = {key: value for key, value in refused.items() if key != 'log'}
        self.gh.comment(number, render_recheck(record))

    def _do_preflight_block(self, number):
        """Block again an issue whose latest check, still valid for its text (and its base,
        #270), failed, after its ``agent:needs-operator`` was removed: the outcome is reused,
        not recomputed, with the commit it ran on."""
        view = self._preflight_view(number, ())
        if preflight_state(view)[0] != 'not_ready':
            self.log('  the readiness check changed since the snapshot; next tick')
            return
        record = view['record']
        outcome = {k: record[k] for k in ('ready', 'gaps', 'split', 'base', 'trust_policy',
                                          'trust_policy_reason') if k in record}
        self._publish_preflight(number, outcome, view['hash'], record['agent'],
                                note='Esito del controllo precedente, riusato: il testo '
                                     'dell\'issue non è cambiato.')
