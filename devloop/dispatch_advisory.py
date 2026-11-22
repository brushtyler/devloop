"""The advisory review of the dispatcher (#369): the mixin ``AdvisoryMixin`` of ``Dispatcher``.

With ``[review_policy] advisory_reviewer`` set (off by default), a devloop PR in
``agent:needs-operator`` (an escalation to the operator, or a ready PR whose merge is the
operator's, #248) gets one advisory review of its head: one more reading for the person who
decides, by the configured agent, which may be the PR's implementer. It is one read-only turn
(``advisory`` in the ledger) in a disposable checkout of the head, published as a native review
of type ``COMMENT`` with the agent's identity, titled ``ADVISORY_TITLE``, with an ``advisory``
marker. The dispatcher then writes its summary for the operator, with the opinion only, and
adds it to the current receipt of the same head (#135), if any.

It produces no effect: no round, no finding, no label, no promotion. devloop reads it to know
whether a head already has one (``_advisory_done``) and to show its opinion in a receipt, and
for one decision only: with the merge delegated to the supervisor (``[delegations] merge``,
#486), a ``/devloop merge`` of a head is refused unless its advisory review has the opinion
``ok`` or ``dubbi`` (``supervision.MERGE_OPINIONS``). It stays out of the quorum. An action that fails is never escalated: the PR is already the operator's.
Except a turn refused by the block on uncertain turns (``TurnBlocked``, #362): it escalates as
``uncertain_turn`` once for the turns that block the work, and the PR gets no advisory action
while those turns, already escalated, still block it (``_advisory_held``).

One turn per head, even when it failed or its publication did. The result of the turn is kept
in ``state_dir`` (``AdvisoryResults``) until the summary is written, and a native advisory review
of the configured agent already published stands for it: a later action publishes what is
missing (the native review, the receipt, then the summary, last) without a new turn. A head is
done once it has the dispatcher's summary. Before the turn and before any publication the PR is
read again: open, on the same head, in ``agent:needs-operator`` and not with the supervisor.

An action that fails before the result of its turn is kept (an error of the dispatcher, of git
or of GitHub, #381) is recorded for its head in ``state_dir`` (``AdvisoryFailures``): the head
gets one retry, not before ``RETRY_SECONDS``, and after ``MAX_ATTEMPTS`` failed actions no turn
again. A new head starts over. A failure that cannot be written holds the head in the process,
with no new turn until a restart.
"""

import json
import os
import threading
from dataclasses import replace
from pathlib import Path

from .agents import AgentError
from .apps import REVIEW
from .budget import BUDGET, BudgetRefused
from .context import _template, format_operator_decisions, format_previous_rounds, prompt_values
from .dispatch_actions import Models
from .journal import JournalError, TurnBlocked
from .ledger import timestamp as ledger_timestamp
from .markdown import (ADVISORY_OPINIONS, MAX_BODY, dispatcher_header, fitted, publishable,
                       read_marker, without_attribution)
from .planning import label_names, operator_blocked
from .policy import ZERO_LEVEL
from .supervision import UNCERTAIN_TURN
from . import receipt as pr_receipt
from .review import current_round

ADVISORY_TITLE = 'Review consultiva: non conta per il quorum'
# The section of the findings of the quorum whose evidence does not hold, and of the fixes that
# were not needed (#481).
UNFOUNDED_TITLE = 'Finding senza evidenza o corretti senza necessità'
# What the readable text keeps of the agent's answer: at most so many points per list.
MAX_POINTS = 20
# An action that failed on a head without a result of its turn kept (#381): the turn of that
# head runs at most ``MAX_ATTEMPTS`` times, the next one not before ``RETRY_SECONDS``.
MAX_ATTEMPTS = 2
RETRY_SECONDS = 60


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
            'unfounded': points('unfounded'),
            'opinion': raw['opinion'], 'opinion_reason': text('opinion_reason')}


def advisory_record(head, reviewer, opinion=None):
    """The ``advisory`` record of ``head`` by ``reviewer``: ``done`` with its opinion, or
    ``failed`` without one."""
    return {'head': head, 'reviewer': reviewer, 'outcome': 'done' if opinion else 'failed',
            'opinion': opinion}


# What the advisory review decides, in the texts devloop publishes (#486): only whether the
# supervisor may merge the head, when the operator delegated the merge, or the coordinator
# (#529), when the operator turned it on.
NO_EFFECT = ('Non conta per il quorum né per il limite dei round, non apre finding e non cambia '
             'label né promozione. Il parere conta solo per il merge delegato al supervisore, '
             'quando l\'operatore lo ha attivato (`[delegations] merge`), e per il merge '
             'riservato del coordinatore (`[roles.coordinator]`): con `contrario`, o '
             'senza review consultiva, devloop rifiuta il `/devloop merge` di questo head, e il '
             'merge resta all\'operatore.')


def _bullets(items):
    return [f'- {item}' for item in items] if items else ['_Nessuno indicato._']


def render_advisory_review(advisory, reviewer, head_sha, header):
    """``{event, body}`` of the native review of the advisory agent (#369): always ``COMMENT``,
    never an approval nor a request for changes, with the ``advisory`` record as its marker."""
    parts = [f'## {ADVISORY_TITLE}', '',
             f'**Commit:** `{head_sha[:12]}` · **Agent:** `{reviewer}` · **Parere non '
             f'vincolante:** **{advisory["opinion"]}**', '',
             f'> Questa review è una lettura in più per l\'operatore. {NO_EFFECT}', '',
             '### Riassunto della PR', '', advisory['summary'] or '_Non indicato._', '',
             '### Rischi residui', '', *_bullets(advisory['residual_risks']), '',
             '### Cosa guarderebbe l\'operatore', '', *_bullets(advisory['operator_checks']), '',
             '### Punti che i reviewer del quorum potrebbero aver trascurato', '',
             *_bullets(advisory['overlooked']), '',
             # #481: the findings of the quorum whose evidence does not hold, and the fixes
             # that were not needed.
             f'### {UNFOUNDED_TITLE}', '', *_bullets(advisory.get('unfounded', [])), '',
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
    text += (f'\n\n{NO_EFFECT} Una nuova review consultiva arriva solo con un nuovo head.')
    return publishable(dispatcher_header() + '\n## Review consultiva per l\'operatore\n\n'
                       + text + '\n', 'advisory', record)


class _StateFile:
    """A JSON object of entries by PR number, kept in ``state_dir`` (``FILE``), so that a
    restart does not lose them; without ``state_dir`` they last as long as the process. A file
    that cannot be read counts as empty. Parallel actions (#176) share it under a lock."""

    FILE = None

    def __init__(self, state_dir=None):
        self._path = Path(state_dir) / self.FILE if state_dir is not None else None
        self._memory = {}
        self._lock = threading.Lock()

    def _load(self):
        if self._path is None:
            return dict(self._memory)
        try:
            data = json.loads(self._path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _store(self, data):
        """Write ``data``; ``OSError`` when the file cannot be written."""
        if self._path is None:
            self._memory = dict(data)
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f'.{self._path.name}.{os.getpid()}.tmp')
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        os.replace(tmp, self._path)

    def drop(self, number):
        """Forget the entry of PR ``number``."""
        with self._lock:
            data = self._load()
            if data.pop(str(number), None) is not None:
                self._store(data)


class AdvisoryResults(_StateFile):
    """The advisory turns run and not yet published (#369), one per PR: ``{head, reviewer,
    advisory, error, header}``, ``advisory`` being the normalized answer (None for a failed
    turn, then ``error`` says why) and ``header`` the attribution of the native review. Kept in
    ``state_dir`` (``FILE``); an entry that is not valid counts as missing."""

    FILE = 'advisory-results.json'

    def get(self, number, head, reviewer):
        """The result kept for ``head`` of PR ``number`` by ``reviewer``, or None."""
        with self._lock:
            entry = self._load().get(str(number))
        if (not isinstance(entry, dict) or entry.get('head') != head
                or entry.get('reviewer') != reviewer
                or not isinstance(entry.get('header'), str)
                or not isinstance(entry.get('error'), str)):
            return None
        if entry.get('advisory') is not None:
            try:
                entry['advisory'] = normalize_advisory(entry['advisory'])
            except AgentError:
                return None
        return entry

    def put(self, number, entry):
        """Keep ``entry`` for PR ``number``, in place of the one before."""
        with self._lock:
            data = self._load()
            data[str(number)] = entry
            self._store(data)


class AdvisoryFailures(_StateFile):
    """The advisory actions that failed on a head (#381), one entry per PR: ``{head, failures,
    retry_at}``, ``retry_at`` the epoch before which the head gets no new turn. Kept in
    ``state_dir`` (``FILE``), so that the limit and the pause hold across a restart; an entry
    that is not valid, or of another head, counts as missing."""

    FILE = 'advisory-failures.json'

    def __init__(self, state_dir=None):
        super().__init__(state_dir)
        # The failures that could not be written, by PR number: they hold in this process,
        # and before the file, so that a failed write never lifts the limit nor the pause.
        self._unsaved = {}

    def get(self, number, head):
        """The failures recorded for ``head`` of PR ``number``, or None."""
        with self._lock:
            entry = self._unsaved.get(str(number)) or self._load().get(str(number))
        if (not isinstance(entry, dict) or entry.get('head') != head
                or not isinstance(entry.get('failures'), int)
                or not isinstance(entry.get('retry_at'), (int, float))):
            return None
        return entry

    def record(self, number, head, now):
        """One more failure of ``head`` of PR ``number``: the entry written, which replaces the
        one of an earlier head. ``OSError`` when the file cannot be written: the budget of the
        head can no longer be kept across a restart, so the failure is kept in this process as
        ``MAX_ATTEMPTS`` failures, and the head gets no new turn until a restart."""
        with self._lock:
            data = self._load()
            entry = self._unsaved.get(str(number)) or data.get(str(number))
            failures = (entry['failures'] if isinstance(entry, dict)
                        and entry.get('head') == head
                        and isinstance(entry.get('failures'), int) else 0)
            entry = {'head': head, 'failures': failures + 1, 'retry_at': now + RETRY_SECONDS}
            data[str(number)] = entry
            try:
                self._store(data)
            except OSError:
                self._unsaved[str(number)] = {**entry,
                                              'failures': max(entry['failures'], MAX_ATTEMPTS)}
                raise
            self._unsaved.pop(str(number), None)
        return entry

    def drop(self, number):
        """Forget the failures of PR ``number``, also those not written."""
        with self._lock:
            self._unsaved.pop(str(number), None)
        super().drop(number)


class AdvisoryMixin:
    """The methods of ``Dispatcher`` for the advisory review (#369). They use the dispatcher
    state and its other methods through ``self``, and ``self.advisory_results``
    (``AdvisoryResults``)."""

    # Reading ------------------------------------------------------------------------------
    def _advisory_items(self, prs):
        """The devloop PRs in ``agent:needs-operator`` (not in ``agent:needs-supervisor``) whose
        head has no advisory review yet, for the snapshot: ``{number, head}``. Nothing when
        ``advisory_reviewer`` is off."""
        key = self.cfg.review_policy.advisory_reviewer
        if not key:
            return []
        lb = self.cfg.labels
        found = []
        for pr in prs:
            if (not pr.meta or pr.attempt_problem or lb.needs_supervisor in pr.labels
                    or not operator_blocked(pr.labels, lb)):
                # A PR whose attempt does not hold (#384) gets no work, nor an advisory review.
                continue
            comments = self.gh.pr_comments(pr.number)
            if self._advisory_record(comments, pr.head_sha) is not None:
                continue
            if self._advisory_retry_held(pr.number, pr.head_sha, key) is not None:
                continue
            try:
                if self._advisory_held(pr.number, comments, pr.head_sha):
                    continue
            except JournalError as exc:  # TurnBlocked included: the action escalates it
                self.log(f'  PR #{pr.number}: advisory review planned, its turns: {exc}')
            found.append({'number': pr.number, 'head': pr.head_sha})
        return found

    def _advisory_held(self, number, comments, head):
        """With ``block_uncertain_turns`` (#362), whether the work of PR ``number`` has turns
        that block it (``_check_turns``) all already named by an ``uncertain_turn`` escalation
        of the dispatcher in ``comments`` (its ``turns``): the advisory review waits for their
        reconciliation, without a new action nor a new escalation. Blocking turns not escalated
        yet raise ``TurnBlocked``, which the action escalates. False when nothing blocks.

        With the budget on (#365), likewise when the envelope of the story would refuse the
        advisory turn (``_check_budget``): held when an escalation of class ``budget`` of the
        dispatcher in ``comments`` already names the same refusal (its ``category``) on
        ``head``, the current head of the PR (#396), until the story covers the turn again;
        otherwise ``BudgetRefused``, which the action escalates once per head. An escalation
        of an earlier head, or without one, holds nothing: a new head is told to the operator
        with an escalation of its own, never held in silence."""
        escalations = [record for _, record in self._dispatcher_records(comments, 'notice',
                                                                         'escalation')]
        try:
            self._check_turns(number)
        except TurnBlocked as exc:
            named = {turn for record in escalations
                     if record.get('class') == UNCERTAIN_TURN
                     and isinstance(record.get('turns'), list)
                     for turn in record['turns']}
            if {t['turn'] for t in exc.turns} <= named:
                return True
            raise
        try:
            self._check_budget(number, 'advisory',
                               self.cfg.agent(self.cfg.review_policy.advisory_reviewer))
        except BudgetRefused as exc:
            if any(record.get('class') == BUDGET and record.get('category') == exc.category
                   and record.get('head') == head for record in escalations):
                self.log(f'  PR #{number}: advisory review of {head[:12]} held: its budget '
                         f'refusal ({exc.category}) is already escalated for this head')
                return True
            raise
        return False

    def _advisory_retry_held(self, number, head, key):
        """Why ``head`` of PR ``number`` gets no new turn of ``key`` now (#381), or None: after
        a failed action, nothing kept of its turn, the head waits ``RETRY_SECONDS`` for its one
        retry, and after ``MAX_ATTEMPTS`` failures it gets no turn again. A head whose turn
        left a result, kept or already published as ``key``'s native review, is never held:
        publishing the rest runs no turn."""
        failed = self.advisory_failures.get(number, head)
        if failed is None or self.advisory_results.get(number, head, key) is not None:
            return None
        if failed['failures'] >= MAX_ATTEMPTS:
            held = f'failed {failed["failures"]} times on this head: not tried again'
        elif self.clock() < failed['retry_at']:
            until = ledger_timestamp(lambda: failed['retry_at'])
            held = f'failed on this head: retry not before {until}'
        else:
            return None
        return None if self._advisory_published(number, head, key) is not None else held

    def _advisory_record(self, comments, head):
        """The latest ``advisory`` record of ``head`` the dispatcher signed in ``comments``,
        or None."""
        found = [record for _, record in self._dispatcher_records(comments, 'advisory')
                 if record['head'] == head]
        return found[-1] if found else None

    def _advisory_done(self, number, head):
        """Whether ``head`` of PR ``number`` already has its advisory review: the dispatcher's
        summary of it, the last thing published. A head with only the native review, or only
        a result kept in ``advisory_results``, is not done: its action publishes the rest
        without a new turn."""
        return self._advisory_record(self.gh.pr_comments(number), head) is not None

    def _advisory_published(self, number, head, key):
        """The ``advisory`` record of the native review of ``head`` published by ``key``'s
        identity, or None."""
        for review in self.gh.pr_reviews(number):
            record = read_marker(review.get('body') or '', 'advisory')
            if (record is not None and review.get('state') != 'PENDING'
                    and record['head'] == head and record['reviewer'] == key
                    and self._wrote(review.get('login'), key)):
                return record
        return None

    def _advisory_open(self, number, head, when):
        """The labels of PR ``number`` read again, when the PR is still the operator's on
        ``head``: open, on that head, in ``agent:needs-operator`` and not in
        ``agent:needs-supervisor``. Otherwise None, and nothing is published; ``when`` is for
        the log."""
        lb = self.cfg.labels
        fresh = self.gh.pr_state(number)
        labels = label_names(fresh.get('labels') or [])
        if fresh.get('state') not in (None, 'OPEN'):
            self.log(f'  the PR is no longer open {when}: no advisory review published')
            return None
        if fresh.get('headRefOid') != head:
            self.log(f'  head moved {when}: no advisory review published')
            return None
        if not operator_blocked(labels, lb) or lb.needs_supervisor in labels:
            self.log(f'  labels changed {when}: no advisory review published')
            return None
        return labels

    def _advisory_forget(self, number):
        for store, what in ((self.advisory_results, 'result'),
                            (self.advisory_failures, 'failures')):
            try:
                store.drop(number)
            except OSError as exc:
                self.log(f'  advisory {what} of #{number} not forgotten: {exc}')

    # The action ---------------------------------------------------------------------------
    def _do_advisory(self, pr):
        """``_advisory_action``, with its failure recorded for the head (#381): whatever the
        exception, which then propagates as before. Without a result of the turn kept, the
        head gets one retry, not before ``RETRY_SECONDS`` (``_advisory_retry_held``). A turn
        refused by the block on uncertain turns (``TurnBlocked``, #362), or by the budget
        envelope (``BudgetRefused``, #365), is no failure of the action: it escalates, and
        ``_advisory_held`` holds the review until reconciled, or covered again."""
        try:
            self._advisory_action(pr)
        except TurnBlocked:
            raise
        except Exception:
            try:
                failed = self.advisory_failures.record(pr.number, pr.head_sha, self.clock())
            except OSError as exc:
                self.log(f'  advisory failure of #{pr.number} not recorded: {exc}: no new turn '
                         'on this head until a restart')
                failed = None
            if failed is None:
                raise  # the action's own error
            until = ledger_timestamp(lambda: failed['retry_at'])
            self.log(f'  advisory action on {pr.head_sha[:12]} failed ({failed["failures"]} of '
                     f'{MAX_ATTEMPTS}): ' + (f'a new turn not before {until}'
                                             if failed['failures'] < MAX_ATTEMPTS
                                             else 'no new turn on this head'))
            raise

    def _advisory_action(self, pr):
        """The advisory review of the head of ``pr``, once, while the PR is still open in
        ``agent:needs-operator`` on the same head: the agent's turn, its native ``COMMENT``
        review, the receipt of the head, if current, and the dispatcher's summary, last.

        The turn runs only when the head has neither a native advisory review of the
        configured agent nor a result kept from an earlier action: otherwise only what is
        missing is published. The result of a turn is kept before anything is published, and
        the PR is read again after the turn: when it left the operator, its head moved or it
        was closed, nothing is published. A failed write raises ``GitHubError``: the action is
        not escalated, and a later one publishes the kept result."""
        key = self.cfg.review_policy.advisory_reviewer
        if not key:
            self.log('  advisory review off')
            return
        head = pr.head_sha
        labels = self._advisory_open(pr.number, head, 'since the snapshot')
        if labels is None:
            return
        problem = self._attempt_problem(pr.number, pr.issue, pr.meta.get('attempt'))
        if problem:
            # The attempt of the PR does not hold (#384): no turn and nothing published, nor a
            # failure recorded: the PR is the operator's (``_do_attempt``).
            self.log(f'  no advisory review: {problem}')
            return
        if self._advisory_done(pr.number, head):
            self.log('  the head already has its advisory review')
            self._advisory_forget(pr.number)
            return
        view = replace(pr, labels=set(labels))
        self._read_thread(view, self._pr_thread(pr.number))
        published = self._advisory_published(pr.number, head, key)
        if published is not None:
            # The native review is there, its summary is not: no new turn.
            self.log(f'  advisory review of {key} already published: no new turn')
            self._advisory_finish(view, advisory_record(head, key, published.get('opinion')))
            return
        kept = self.advisory_results.get(pr.number, head, key)
        if kept is not None:
            self.log(f'  advisory review of {key} already run on this head: no new turn')
        else:
            held = self._advisory_retry_held(pr.number, head, key)
            if held is not None:
                self.log(f'  no advisory turn: {held}')
                return
            # With the block on uncertain turns (#362), before the issue is read and the
            # checkout made: blocking turns not escalated yet raise ``TurnBlocked``, which
            # escalates; those already escalated hold the review back until reconciled. So does
            # a refusal of the budget envelope (#365), until the story covers the turn again.
            if self._advisory_held(pr.number, self.gh.pr_comments(pr.number), head):
                self.log('  advisory review held: its work has uncertain turns, or a budget '
                         'refusal, already escalated')
                return
            kept = self._advisory_turn(view, key)
            if self._advisory_open(pr.number, head, 'during the advisory review') is None:
                return
        advisory = kept['advisory']
        if advisory is not None:
            # With the agent's identity, never APPROVE nor REQUEST_CHANGES: GitHub accepts a
            # COMMENT also from the PR's author, the implementer.
            self._submit_review(self._as(key, REVIEW), view, key,
                                render_advisory_review(advisory, key, head, kept['header']))
        self._advisory_finish(view, advisory_record(head, key,
                                                    advisory['opinion'] if advisory else None),
                              kept['error'])

    def _advisory_turn(self, pr, key):
        """Run the advisory turn of ``key`` on the head of ``pr`` and keep its result in
        ``advisory_results`` before anything is published: ``{head, reviewer, advisory, error,
        header}``. A failed turn (``AgentError``) is a result too, without ``advisory``."""
        cfg = self.cfg
        head = pr.head_sha
        issue, _ = self._issue_context(pr.issue)
        why = ('its merge is the operator\'s (the merge mode, a level `off` or an operator-only '
               'path).' if pr.operator_owned
               else 'an escalation handed it to the operator.')
        name = f'pr-{pr.number}-advisory'
        path = self.ws.commit_copy(name, head, branch=pr.head_ref)
        advisory, error, header = None, '', ''
        try:
            prompt = _template('advisory', **prompt_values(cfg), repo=cfg.repo, pr_number=pr.number, head=head,
                               base_branch=cfg.base_branch, why=why,
                               issue_number=pr.issue, issue_title=issue['title'],
                               # Blind to the author, as the review (#389).
                               issue_body=without_attribution(issue.get('body')),
                               operator_decisions=format_operator_decisions(
                                   issue['decisions']),
                               pr_body=without_attribution(pr.body),
                               diff_stat=self.ws.diff_stat(path),
                               previous=format_previous_rounds(pr.reviews, pr.fixes,
                                                               pr.closures),
                               language=cfg.language)
            raw, used = self._turn(Models(cfg.agent(key)), 'review', prompt, 'advisory', path,
                                   name, where=(pr.number, 'advisory'))
            advisory = normalize_advisory(raw)
            header = self.runner.header(used)
        except AgentError as exc:
            error = _short(exc)
            self.log(f'  advisory review by {key} failed: {error}')
        finally:
            self.ws.remove(name)
        self.log(f'  advisory review of {key}: ' + (advisory['opinion'] if advisory else 'failed'))
        kept = {'head': head, 'reviewer': key, 'advisory': advisory, 'error': error,
                'header': header}
        try:
            self.advisory_results.put(pr.number, kept)
        except OSError as exc:  # published right away all the same
            self.log(f'  advisory result of #{pr.number} not kept: {exc}')
        return kept

    def _advisory_finish(self, pr, record, error=''):
        """Publish what follows the native review of ``record``: the receipt of the head, if
        current, then the dispatcher's summary, last, since it ends the advisory review of the
        head. Then the kept result is forgotten."""
        self._advisory_receipt(pr, record)
        self.gh.comment(pr.number, render_advisory_summary(record, error))
        self.log(f'  advisory summary written: {record["opinion"] or "failed"}')
        self._advisory_forget(pr.number)

    def _advisory_receipt(self, pr, record):
        """Add ``record``, the advisory review of a head, to the current receipt of that same
        head (#135), if any: its section and its ``receipt`` record rewritten with digest and
        totals recomputed, while promotion, policy and state stay. A receipt superseded or of
        another head is left as it is, and so is one that cannot hold the section, which is
        logged. A failed read or edit raises ``GitHubError``: the summary is not written yet,
        and a later action edits the receipt again."""
        if self.ledger is None:
            return
        head = record['head']
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
            self.log('  receipt of the head not updated: its comment or its promotion cannot '
                     'be found')
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
            self.log('  receipt of the head not updated: it cannot hold the advisory section')
            return
        self.gh.edit_comment(comment['id'], body)
        self.log(f'  receipt of promotion {marker["promotion"]} updated with the advisory '
                 'review')
