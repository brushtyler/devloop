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
from .context import _render_issue_comments, _template
from .dispatch_actions import Models, ready_approval
from .dispatch_supervision import UntrustedChange
from .markdown import carries_record, read_marker, render_escalation, render_preflight
from .planning import (RECHECK_COMMAND, label_names, operator_blocked, preflight_state,
                       repository_bound)
from .supervision import ACCEPT_DEFAULTS, DECIDE, preflight_class
from .workspace import WorkspaceError


def devloop_text(body):
    """Text devloop or its agents published: it starts with the AGENTS.md header."""
    return (body or '').lstrip().startswith('> [!NOTE]')


def normalize_preflight(raw):
    """The outcome of a readiness check: ready only without gaps and without a split, whatever
    the agent says (a ``default_available`` gap blocks too). A gap depends on the base branch
    (``repository_dependent``, #270) only when the agent says so."""
    gaps = [dict(g, code_refs=list(g['code_refs']),
                 repository_dependent=g.get('repository_dependent') is True)
            for g in raw['gaps']]
    ready = bool(raw['ready']) and not gaps and not raw['split'].strip()
    return {'ready': ready, 'gaps': gaps, 'split': raw['split']}


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

    def _preflight_view(self, number, labels, base_head=None):
        """What planning needs about an issue's readiness check, read now from GitHub:
        ``{hash, record, recheck, owned, relabeled, base}``, ``record`` being the latest check
        or None. ``relabeled`` and ``base`` (#270) are those of ``preflight_state``: the second
        is read, with ``base_head`` (``ws.base_head`` by default), only for a failure that
        depends on the base branch (``repository_bound``)."""
        history = self.gh.issue_history(number)
        comments = self.gh.issue_comments(number)
        records = self._preflight_records(comments)
        record = records[-1][1] if records else None
        base = None
        if repository_bound(record):
            try:
                base = (base_head or self.ws.base_head)()
            except WorkspaceError as exc:
                self.log(f'  issue #{number}: the base branch not read, the failed readiness '
                         f'check holds: {exc}')
        return {'hash': self._request_hash(history, comments),
                'record': record,
                'recheck': self._recheck_requested(number, comments),
                'owned': (self._owned_block(labels)
                          and self._owns_block(number, comments, records)),
                'relabeled': self._relabeled(history, comments, records),
                'base': base}

    def _relabeled(self, history, comments, records):
        """The login that added ``agent:ready`` again after the latest readiness check, when
        that check failed (#270), else None. Only the latest ``labeled`` event of the label
        counts, and only when one of the issue's admitters wrote it (``ready_approval``): a
        trusted user or, where its labels count, the supervisor's app. It must come after the
        comment of the check, by its time; a comment without one never counts as relabeled.
        GitHub timestamps have one-second granularity: a label in the same second as the check
        cannot be proven to follow it, and does not count."""
        if not records or records[-1][1]['ready']:
            return None
        approved = ready_approval(history['labeled'], self.cfg.labels.ready,
                                  self._admitters(history['author']))
        written = comments[records[-1][0]].get('created_at')
        if approved and isinstance(written, str) and approved[1] > written:
            return approved[0]
        return None

    def _publish_preflight(self, number, outcome, digest, agent_key, note='', base=None,
                           used=None):
        """Record a check's outcome on the issue and apply it to ``agent:needs-operator``: add
        the label when the check fails and the label is absent, remove it when the check passes
        and the label belongs to the readiness check, under whichever name it has (#248). A
        label someone else set stays. ``base``: the commit the check ran on, recorded with the
        outcome (#270); None for an outcome reused (the latest base then), which keeps the
        commit of its own record, if any. ``used``: the configuration the check ran with, a
        fallback model's included (#148), for the header; the configured agent by default."""
        lb = self.cfg.labels
        agent = used or self.cfg.agent(agent_key)
        labels = label_names(self.gh.issue(number).get('labels') or [])
        comments = self.gh.issue_comments(number)
        records = self._preflight_records(comments)
        present = operator_blocked(labels, lb)
        owned = self._owned_block(labels) and self._owns_block(number, comments, records)
        shown = ', '.join(f'`{name}`' for name in sorted(labels & lb.operator()))
        record = dict(outcome, hash=digest, agent=agent_key, blocked=False, labeled=0)
        if base:
            record['base'] = base
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
        self._publish_preflight(number, normalize_preflight(raw), digest, agent.key, base=base,
                                used=used)

    def _do_preflight_block(self, number):
        """Block again an issue whose latest check, still valid for its text (and its base,
        #270), failed, after its ``agent:needs-operator`` was removed: the outcome is reused,
        not recomputed, with the commit it ran on."""
        view = self._preflight_view(number, ())
        if preflight_state(view)[0] != 'not_ready':
            self.log('  the readiness check changed since the snapshot; next tick')
            return
        record = view['record']
        outcome = {k: record[k] for k in ('ready', 'gaps', 'split', 'base') if k in record}
        self._publish_preflight(number, outcome, view['hash'], record['agent'],
                                note='Esito del controllo precedente, riusato: il testo '
                                     'dell\'issue non è cambiato.')
