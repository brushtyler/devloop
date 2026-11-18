"""The advisory review of the dispatcher (#369): the mixin ``AdvisoryMixin`` of ``Dispatcher``.

With ``[review_policy] advisory_reviewer`` set (off by default), a devloop PR in
``agent:needs-operator`` (an escalation to the operator, or a ready PR whose merge is the
operator's, #248) gets one advisory review of its head: one more reading for the person who
decides, by the configured agent, which may be the PR's implementer. It is one read-only turn
(``advisory`` in the ledger) in a disposable checkout of the head, published as a native review
of type ``COMMENT`` with the agent's identity, titled ``ADVISORY_TITLE``, with an ``advisory``
marker. The dispatcher then writes its summary for the operator, with the opinion only, and
adds it to the current receipt of the same head (#135), if any.

It produces no effect: no round, no finding, no label, no promotion. devloop reads it only to
know whether a head already has one (``_advisory_done``) and to show its opinion in a receipt:
never to decide. One per head: a head with a dispatcher summary, or with a native advisory
review of the configured agent, never gets another, even when the turn failed. An action that
fails is never escalated: the PR is already the operator's.
"""

from dataclasses import replace

from .agents import AgentError
from .apps import REVIEW
from .context import _template, format_previous_rounds
from .dispatch_actions import Models
from .github import GitHubError
from .markdown import (ADVISORY_OPINIONS, MAX_BODY, dispatcher_header, fitted, publishable,
                       read_marker)
from .planning import label_names, operator_blocked
from .policy import ZERO_LEVEL
from . import receipt as pr_receipt
from .review import current_round

ADVISORY_TITLE = 'Review consultiva: non conta per il quorum'
# What the readable text keeps of the agent's answer: at most so many points per list.
MAX_POINTS = 20


def _short(exc, limit=300):
    """The first line of ``exc``, at most ``limit`` characters."""
    text = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
    return text if len(text) <= limit else text[:limit - 1] + '…'


def normalize_advisory(raw):
    """The agent's answer (``schemas/advisory.json``) as the review shows it: texts stripped,
    lists of non-empty strings, at most ``MAX_POINTS`` each. An opinion outside
    ``ADVISORY_OPINIONS`` raises ``AgentError``: the turn counts as failed."""
    if not isinstance(raw, dict) or raw.get('opinion') not in ADVISORY_OPINIONS:
        raise AgentError('advisory review: the answer has no valid opinion')

    def text(key):
        value = raw.get(key)
        return value.strip() if isinstance(value, str) else ''

    def points(key):
        value = raw.get(key)
        items = value if isinstance(value, list) else []
        return [item.strip() for item in items if isinstance(item, str) and item.strip()][
            :MAX_POINTS]
    return {'summary': text('summary'), 'residual_risks': points('residual_risks'),
            'operator_checks': points('operator_checks'), 'overlooked': points('overlooked'),
            'opinion': raw['opinion'], 'opinion_reason': text('opinion_reason')}


def advisory_record(head, reviewer, opinion=None):
    """The ``advisory`` record of ``head`` by ``reviewer``: ``done`` with its opinion, or
    ``failed`` without one."""
    return {'head': head, 'reviewer': reviewer, 'outcome': 'done' if opinion else 'failed',
            'opinion': opinion}


def _bullets(items):
    return [f'- {item}' for item in items] if items else ['_Nessuno indicato._']


def render_advisory_review(advisory, reviewer, head_sha, header):
    """``{event, body}`` of the native review of the advisory agent (#369): always ``COMMENT``,
    never an approval nor a request for changes, with the ``advisory`` record as its marker."""
    parts = [f'## {ADVISORY_TITLE}', '',
             f'**Commit:** `{head_sha[:12]}` · **Agent:** `{reviewer}` · **Parere non '
             f'vincolante:** **{advisory["opinion"]}**', '',
             '> Questa review è una lettura in più per l\'operatore. Non conta per il quorum né '
             'per il limite dei round, non apre finding e non cambia label né promozione: '
             'devloop non la legge per decidere.', '',
             '### Riassunto della PR', '', advisory['summary'] or '_Non indicato._', '',
             '### Rischi residui', '', *_bullets(advisory['residual_risks']), '',
             '### Cosa guarderebbe l\'operatore', '', *_bullets(advisory['operator_checks']), '',
             '### Punti che i reviewer del quorum potrebbero aver trascurato', '',
             *_bullets(advisory['overlooked']), '',
             '### Parere finale', '',
             f'**{advisory["opinion"]}**' + (f': {advisory["opinion_reason"]}'
                                              if advisory['opinion_reason'] else '')]
    record = advisory_record(head_sha, reviewer, advisory['opinion'])
    return {'event': 'COMMENT', 'body': fitted(header, '\n'.join(parts), 'advisory', record)}


def render_advisory_summary(record, error=''):
    """The dispatcher's summary of the advisory review of a head for the operator (#369), with
    the ``advisory`` record: who wrote it and its opinion, never the agent's text."""
    head, who = f'`{record["head"][:12]}`', f'`{record["reviewer"]}`'
    if record['outcome'] == 'done':
        text = (f'Sul commit {head} la PR ha ricevuto la review consultiva di {who}: una review '
                f'GitHub di tipo `COMMENT` intitolata "{ADVISORY_TITLE}". Parere non '
                f'vincolante: **{record["opinion"]}**.')
    else:
        text = (f'La review consultiva di {who} sul commit {head} non è riuscita'
                + (f' ({error})' if error else '') + '.')
    text += ('\n\nNon conta per il quorum né per il limite dei round, non apre finding e non '
             'cambia label né promozione: devloop non la legge per decidere. Una nuova review '
             'consultiva arriva solo con un nuovo head.')
    return publishable(dispatcher_header() + '\n## Review consultiva per l\'operatore\n\n'
                       + text + '\n', 'advisory', record)


class AdvisoryMixin:
    """The methods of ``Dispatcher`` for the advisory review (#369). They use the dispatcher
    state and its other methods through ``self``."""

    # Reading ------------------------------------------------------------------------------
    def _advisory_items(self, prs):
        """The devloop PRs in ``agent:needs-operator`` (not in ``agent:needs-supervisor``) whose
        head has no advisory review yet, for the snapshot: ``{number, head}``. Nothing when
        ``advisory_reviewer`` is off."""
        key = self.cfg.review_policy.advisory_reviewer
        if not key:
            return []
        lb = self.cfg.labels
        return [{'number': pr.number, 'head': pr.head_sha} for pr in prs
                if pr.meta and lb.needs_supervisor not in pr.labels
                and operator_blocked(pr.labels, lb)
                and not self._advisory_done(pr.number, pr.head_sha, key)]

    def _advisory_record(self, comments, head):
        """The latest ``advisory`` record of ``head`` the dispatcher signed in ``comments``,
        or None."""
        found = [record for _, record in self._dispatcher_records(comments, 'advisory')
                 if record['head'] == head]
        return found[-1] if found else None

    def _advisory_done(self, number, head, key):
        """Whether ``head`` of PR ``number`` already has its advisory review: the dispatcher's
        summary of it, or a published native review with an ``advisory`` record of ``head``
        written by ``key``'s identity (a summary lost after the review)."""
        if self._advisory_record(self.gh.pr_comments(number), head) is not None:
            return True
        for review in self.gh.pr_reviews(number):
            record = read_marker(review.get('body') or '', 'advisory')
            if (record is not None and review.get('state') != 'PENDING'
                    and record['head'] == head and record['reviewer'] == key
                    and self._wrote(review.get('login'), key)):
                return True
        return False

    # The action ---------------------------------------------------------------------------
    def _do_advisory(self, pr):
        """The advisory review of the head of ``pr``, once, while the PR is still in
        ``agent:needs-operator`` on the same head: the agent's turn, its native ``COMMENT``
        review, the dispatcher's summary and the receipt of the head, if current."""
        cfg = self.cfg
        key = cfg.review_policy.advisory_reviewer
        if not key:
            self.log('  advisory review off')
            return
        lb = cfg.labels
        fresh = self.gh.pr_state(pr.number)
        labels = label_names(fresh.get('labels') or [])
        if not operator_blocked(labels, lb) or lb.needs_supervisor in labels:
            self.log('  labels changed since the snapshot: no advisory review')
            return
        head = pr.head_sha
        if fresh.get('headRefOid') not in (None, head):
            self.log('  head moved since the snapshot: no advisory review')
            return
        if self._advisory_done(pr.number, head, key):
            self.log('  the head already has its advisory review')
            return
        view = replace(pr, labels=set(labels))
        self._read_thread(view, self._pr_thread(pr.number))
        issue, _ = self._issue_context(pr.issue)
        why = ('its merge is the operator\'s (the merge mode, a level `off` or an operator-only '
               'path).' if view.operator_owned
               else 'an escalation handed it to the operator.')
        name = f'pr-{pr.number}-advisory'
        path = self.ws.commit_copy(name, head, branch=pr.head_ref)
        advisory = error = None
        try:
            prompt = _template('advisory', repo=cfg.repo, pr_number=pr.number, head=head,
                               base_branch=cfg.base_branch, why=why,
                               implementer=pr.meta.get('implementer', cfg.implementer),
                               issue_number=pr.issue, issue_title=issue['title'],
                               issue_body=issue.get('body') or '', pr_body=pr.body,
                               diff_stat=self.ws.diff_stat(path),
                               previous=format_previous_rounds(view.reviews, view.fixes,
                                                               view.closures),
                               language=cfg.language)
            raw, used = self._turn(Models(cfg.agent(key)), 'review', prompt, 'advisory', path,
                                   name, where=(pr.number, 'advisory'))
            advisory = normalize_advisory(raw)
        except AgentError as exc:
            error = _short(exc)
            self.log(f'  advisory review by {key} failed: {error}')
        finally:
            self.ws.remove(name)
        if self.gh.pr_head(pr.number) != head:
            self.log('  head moved during the advisory review: not published')
            return
        if advisory is not None:
            # With the agent's identity, never APPROVE nor REQUEST_CHANGES: GitHub accepts a
            # COMMENT also from the PR's author, the implementer.
            self._submit_review(self._as(key, REVIEW), view, key,
                                render_advisory_review(advisory, key, head,
                                                       self.runner.header(used)))
        record = advisory_record(head, key, advisory['opinion'] if advisory else None)
        self.gh.comment(pr.number, render_advisory_summary(record, error or ''))
        self.log(f'  advisory review of {key}: '
                 + (advisory['opinion'] if advisory else 'failed'))
        self._advisory_receipt(view, record)

    def _advisory_receipt(self, pr, record):
        """Add ``record``, the advisory review of a head, to the current receipt of that same
        head (#135), if any: its section and its ``receipt`` record rewritten with digest and
        totals recomputed, while promotion, policy and state stay. A receipt superseded or of
        another head is left as it is; a failure is logged, since the summary already tells
        the operator."""
        if self.ledger is None:
            return
        head = record['head']
        try:
            comments = self.gh.pr_comments(pr.number)
            current = [(p, r) for p, r in self._dispatcher_records(comments, 'receipt')
                       if r['state'] == 'current' and r['head'] == head]
            if not current:
                return
            position, marker = current[-1]
            comment = comments[position]
            promotions = [r for _, r in self._dispatcher_records(comments, 'policy')
                          if r.get('event') == 'promoted']
            if comment.get('id') is None or len(promotions) < marker['promotion']:
                self.log('  receipt of the head not updated: its comment or its promotion '
                         'cannot be found')
                return
            policy = promotions[marker['promotion'] - 1]['receipt']
            if policy.get('head') != head:
                return
            latest = current_round(pr.reviews)
            approved = None if policy['level'] == ZERO_LEVEL or not latest else latest[0]['round']
            content = self._receipt_content(pr, policy, marker['promotion'], approved,
                                            pr.reviews, comments, advisory=record)
            body = pr_receipt.with_advisory(comment['body'], content)
            if body is None or len(body) > MAX_BODY:
                self.log('  receipt of the head not updated: it cannot hold the advisory '
                         'section')
                return
            self.gh.edit_comment(comment['id'], body)
            self.log(f'  receipt of promotion {marker["promotion"]} updated with the advisory '
                     'review')
        except GitHubError as exc:  # the summary is written: the receipt is an extra
            self.log(f'  receipt of the head not updated: {exc}')
