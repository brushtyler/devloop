"""Receipt, level and merge of the dispatcher: the mixin ``MergeMixin`` of ``Dispatcher``.

Moved from ``devloop/dispatcher.py`` without changes (#295, step S5 of #261): the check of an
approval before its promotion (``_approval_current``), the policy receipt of a PR
(``_receipt``, ``_receipt_and_paths``), the checks of an advanced base (``_base_check``,
``_base_checks``, ``_merge_check``), the choice of the reviewers (``_select``), the policy
records and the escalation of a level (``_policy_comment``, ``_escalate_level``), the promotion
(``_promote``, ``_promote_zero``) and its level-0 and acceptance checks (``_zero_problem``,
``_acceptance_problems``, ``_refuse_zero``), the revocation of a readiness (``_do_revoke``), the
merge commands and the merge (``_do_hold``, ``_do_merge``) and the issues a merge resolved
(``_close_merged_issue``, ``_do_merged_issue``). ``Dispatcher`` inherits the methods.
"""

import itertools
from dataclasses import replace

from .acceptance import acceptance_criteria, acceptance_findings, latest_map
from .apps import MERGER
from .context import _describe_bases, _open_findings_reason
from .dispatch_supervision import RELEASE_COMMAND
from .github import GitHubError
from .markdown import dispatcher_header, publishable
from .planning import MERGING, label_names, pending_reviewers, touched_by_base
from .policy import describe as describe_receipt
from .policy import (MAX_LISTED_PATHS, ZERO_LEVEL, changes_requested, check_outcome,
                     check_states, differences, distinct, evaluate, independent, receipt_of,
                     recorded_level, zero_refused)
from .review import FINDING_COMMAND, current_round, open_findings, round_verdict
from .supervision import MERGED_ISSUE
from .workspace import WorkspaceError


class MergeMixin:
    """The methods of ``Dispatcher`` for receipts, review levels and merges (#295). They use
    the dispatcher state and its other methods through ``self``."""

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

    def _policy_comment(self, pr, title, text, receipt, event, bases=(), cls=None):
        """A dispatcher ``policy`` record; ``bases``: the base checks it relied on (#160);
        ``cls``: the class of the escalation it records, if any (#182)."""
        payload = {'event': event, 'receipt': receipt}
        if bases:
            payload['bases'] = list(bases)
        if cls:
            payload['class'] = cls
        self.gh.comment(pr.number, publishable(
            dispatcher_header() + f'\n## {title}\n\n{text.strip()}\n\n'
            + describe_receipt(receipt), 'policy', payload))

    def _escalate_level(self, pr, receipt):
        """Not enough independent reviewers for the level: no review, no promotion, the
        operator decides."""
        lb, level = self.cfg.labels, receipt['level']
        needed = 'un reviewer indipendente' if level == 1 else f'{level} reviewer indipendenti'
        self._policy_comment(
            pr, f'Review di livello {level}: serve un intervento dell\'operatore',
            f'La policy di review del dispatcher richiede per questa PR il livello {level}, '
            f'quindi {needed}, ma {self._select(pr, level)[1]}. devloop non revisiona né '
            'promuove la PR: la review e la decisione sul merge spettano a una persona. Il '
            'livello resta acquisito anche se la label viene rimossa o il diff cambia.',
            receipt, 'level', cls='review_level')
        self.gh.edit_labels(pr.number, add=[lb.needs_operator],
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
        bases = self._approvals_match(pr, records, current, paths)
        if bases is None:
            return
        by = ', '.join(f'`{key}`' for key in current['reviewers'])
        branch = self.cfg.base_branch
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
        reason = self._untrusted_change(history)
        if reason:
            return [f'testo dell\'issue non verificabile: {reason}']
        found = acceptance_findings(acceptance_criteria(history['body']),
                                    latest_map(pr.meta, pr.fixes))
        return [f['title'] for f in found]

    def _refuse_zero(self, pr, receipt, why, states=()):
        """Level 0 holds but the head cannot be promoted: the operator decides."""
        lb = self.cfg.labels
        self.gh.comment(pr.number, publishable(
            dispatcher_header() + '\n## Livello 0: serve un intervento dell\'operatore\n\n'
            f'La policy di review assegna a questa PR il livello 0, ma {why.strip()}.\n\n'
            'devloop non promuove la PR. Per riprendere: risolvere la causa, togliere '
            f'`{lb.needs_operator}` e rimettere `{lb.review}` (il livello 0 viene ricontrollato), '
            f'oppure togliere anche la label `{self.cfg.review_policy.zero_label}` per una review '
            'di livello 1.\n\n' + describe_receipt(receipt),
            'policy', {'event': 'level', 'receipt': receipt, 'checks': list(states),
                       'class': 'level_zero'}))
        self.gh.edit_labels(pr.number, add=[lb.needs_operator],
                            remove=[lb.review, lb.changes, lb.ready_to_merge])

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
        released = self._released(pr)
        if released:
            reason += f', e `{lb.needs_operator}`, che il dispatcher aveva messo per il merge, è tolta'
        self.gh.comment(pr.number,
                        publishable(dispatcher_header() + f'\n## {title}\n\n' + reason + '.'))
        self.gh.edit_labels(pr.number, add=[lb.review], remove=[lb.ready_to_merge, *released])

    def _do_hold(self, pr):
        """Apply a hold or release command (#172): add or remove ``agent:merge-hold``, then
        record who asked and why in a dispatcher ``hold`` record, which spends the command.
        Label first: if the record is lost, the next tick applies the same command again."""
        lb, command = self.cfg.labels, pr.hold
        reason = ' '.join(command['reason'].split())
        if command['event'] == 'hold':
            self.gh.edit_labels(pr.number, add=[lb.merge_hold])
            title = 'Merge trattenuto'
            text = (f'`{command["by"]}` ha trattenuto il merge: {reason}\n\ndevloop non mergia '
                    f'la PR finché resta `{lb.merge_hold}`; `{RELEASE_COMMAND}` lo permette di '
                    'nuovo.')
        else:
            self.gh.edit_labels(pr.number, remove=[lb.merge_hold])
            title = 'Merge rilasciato'
            text = (f'`{command["by"]}` ha rilasciato il merge' + (f': {reason}' if reason else '.')
                    + f'\n\ndevloop può di nuovo mergiare la PR quando è `{lb.ready_to_merge}` e '
                    'valgono le direttive di merge.')
        self.gh.comment(pr.number, publishable(dispatcher_header() + f'\n## {title}\n\n{text}',
                                               'hold', dict(command, head=pr.head_sha)))
        self.log(f'  {command["event"]} by {command["by"]}')
        pr.hold = None

    def _do_merge(self, pr):
        """Merge a ready PR (#172), with a merge commit on the approved head, with the local
        ``gh`` login. Every merge directive is read again just before, with reviews, closures,
        policy records and commands rebuilt from the comments read now: the PR still open,
        ``agent:ready-to-merge`` without ``agent:needs-operator`` nor ``agent:merge-hold``, no hold
        or release command waiting, the head unchanged, ``MERGEABLE`` and ``CLEAN``, CI
        ``pass``, no open finding, the merge mode of the recomputed receipt, and the conditions
        of the promotion: the approvals of every required reviewer with a matching receipt, or
        at level 0 the checks of a level-0 promotion. When one is missing devloop does not
        merge: what the per-tick checks of a ready PR revoke is left to the next tick, a stale
        receipt or an open finding is handled as at promotion."""
        cfg, lb, rp = self.cfg, self.cfg.labels, self.cfg.review_policy
        fresh = self.gh.pr_state(pr.number)
        labels = label_names(fresh.get('labels') or [])
        head = fresh.get('headRefOid') or ''

        def skip(why):
            self.log(f'  not merged: {why}')

        if fresh.get('state') != 'OPEN':
            return skip(f'the PR is {fresh.get("state")}')
        if lb.ready_to_merge not in labels or labels & (lb.operator() | {lb.merge_hold,
                                                                         lb.needs_supervisor}):
            return skip('labels changed: ' + ', '.join(sorted(labels)))
        if head != pr.head_sha:
            return skip(f'head moved to {head[:12]}')
        status = (fresh.get('mergeable'), fresh.get('mergeStateStatus'))
        if status != ('MERGEABLE', 'CLEAN'):
            return skip('GitHub reports {} / {}'.format(*status))
        # Reviews, closures, policy records and commands as the conversation shows them now,
        # with the same identity checks as the snapshot: never the snapshot's older view.
        pr = replace(pr, labels=labels, reviews=[], fixes=[], syncs=[], closures=[],
                     commands=[], finding_problems=[], policy=[], zero_head=None,
                     zero_problem='', stale_receipt=False, revoked=None, operator_comments=[],
                     rereview=False, round_comments=[], acceptance=[], hold=None)
        self._read_thread(pr, self.gh.pr_comments(pr.number))
        if pr.hold:
            return skip('a hold or release command waits to be applied')
        if pr.finding_problems:
            return skip('findings whose owner cannot be reconstructed')
        if pr.stale_receipt:
            return skip('a policy record found the receipt of the approval stale')
        ci, _ = self._read_ci(head)
        if ci != 'pass':
            return skip(f'CI is {ci}')
        left = open_findings(pr.reviews, pr.closures)
        if left:
            return self._escalate(pr.number, _open_findings_reason(left),
                                  remove=[lb.review, lb.changes, lb.ready_to_merge],
                                  cls='open_findings')
        if pr.zero_head is None and self._acceptance_problems(pr):
            return skip('the acceptance map does not hold')
        current, paths = self._receipt_and_paths(pr)
        if current['head'] != head:
            return skip(f'the pushed head is {current["head"][:12]}')
        mode = cfg.merge.mode_for(current['level'], paths)
        if mode not in MERGING:
            return skip(f'the merge mode of this PR is {mode}')
        bases = []
        if pr.zero_head is not None:
            # Level 0: the same checks as its promotion, and its labels still in place.
            problem = self._zero_problem(pr)
            if not problem and not (pr.zero_head == head and rp.zero_label in labels
                                    and rp.label not in labels):
                problem = 'il head o le label di livello sono cambiati'
            if problem:
                pr.zero_problem = problem
                return self._do_revoke(pr)
        else:
            latest = current_round(pr.reviews)
            if not (latest and not pending_reviewers(latest)
                    and round_verdict(latest) == 'approve' and latest[0]['head_sha'] == head):
                return skip('the head is not the approved commit')
            if len(current['reviewers']) < current['level']:
                return self._escalate_level(pr, current)
            bases = self._approvals_match(pr, latest, current, paths, step='merge')
            if bases is None:
                return None
        self.merger.merge_pr(pr.number, head)
        self.log(f'  merged {head[:12]} at level {current["level"]} in mode {mode}')
        if self.pinner is not None and cfg.pin.auto and cfg.pin.touches(paths):
            # The next tick pins the base with this merge, before planning (#174).
            try:
                self.pinner.request(pr.number, head)
                self.log(f'  auto-pin of origin/{cfg.base_branch} requested for the next tick')
            except OSError as exc:
                self.log(f'  auto-pin NOT requested, pin by hand: {exc}')
        payload = {'head': head, 'level': current['level'], 'mode': mode, 'receipt': current}
        merged_by = self._app_login(MERGER)
        if merged_by:
            # Signed by the merger's app (#201); a merge with the ambient login names nobody.
            payload['merged_by'] = merged_by
        if bases:
            payload['bases'] = bases
        try:
            self.gh.comment(pr.number, publishable(
                dispatcher_header() + '\n## Mergiata da devloop\n\n'
                f'devloop ha mergiato il commit `{head[:12]}` con un merge commit, legato a quel '
                f'head (`--match-head-commit`), in modalità `{mode}`, al livello '
                f'{current["level"]}. Subito prima ha ricontrollato le direttive di merge: CI '
                '`pass`, `MERGEABLE` e `CLEAN`, nessun finding aperto, nessuna label '
                f'`{lb.needs_operator}` o `{lb.merge_hold}`, e le condizioni della promozione con la '
                'ricevuta di policy ricalcolata. Il branch resta.\n\n' + describe_receipt(current),
                'merged', payload))
        except GitHubError as exc:
            # The PR is merged: escalating it would only label a closed PR.
            self.log(f'  the merged record could not be written: {exc}')
        self._close_merged_issue(pr, head, mode)
        return None

    def _close_merged_issue(self, pr, head, mode):
        """Close the issue of ``pr``, the one its marker names, after devloop merged it (#228),
        with the dispatcher's identity: "Closes #N" has no effect when the merger's app merges.
        A comment with a ``closed`` marker records the PR, the merged head, the merge commit
        and the mode; then the issue is closed as completed and loses ``agent:ready``. An issue
        that is not open (GitHub closed it, or a person did) is left alone. A failure is logged
        and does not touch the merged PR: an issue left open with ``agent:ready`` is never
        started again (``_do_merged_issue``)."""
        lb = self.cfg.labels
        number = pr.meta.get('issue')
        if not isinstance(number, int):
            self.log('  the PR marker names no issue: none closed')
            return
        try:
            issue = self.gh.issue(number)
            state = issue.get('state')
            if state != 'OPEN':
                self.log(f'  issue #{number} is {state or "in an unknown state"}: left alone')
                return
            commit = None
            try:
                commit = (self.gh.pr_state(pr.number).get('mergeCommit') or {}).get('oid')
            except GitHubError as exc:
                self.log(f'  merge commit of #{pr.number} not read: {exc}')
            self.gh.comment(number, publishable(
                dispatcher_header() + '\n## Risolta da una PR mergiata\n\n'
                f'devloop ha mergiato la PR #{pr.number} (head `{head[:12]}`, merge commit '
                + (f'`{commit[:12]}`' if commit else 'non letto') + f') in modalità `{mode}`, e '
                'chiude questa issue come completata. Con il merge dell\'app merger le parole '
                'chiave di chiusura della PR non hanno effetto, quindi la chiude il dispatcher.',
                'closed', {'pr': pr.number, 'head': head, 'merge_commit': commit, 'mode': mode}))
            self.gh.close_issue(number)
            if lb.ready in label_names(issue.get('labels') or []):
                self.gh.edit_labels(number, remove=[lb.ready])
            self.log(f'  issue #{number} closed')
        except GitHubError as exc:
            self.log(f'  issue #{number} NOT closed, close it by hand: {exc}')

    def _do_merged_issue(self, number, merged=None):
        """Escalate a ready issue that a merged devloop PR already resolved (#228), instead of
        checking or implementing it again: class ``merged_issue``, non-critical. In ``auto``
        the supervisor decides it and the issue keeps ``agent:ready`` meanwhile (no retry is
        admitted: closing the issue, or ``/devloop escalate``, settles it); otherwise it goes
        to the operator, without ``agent:ready`` nor ``agent:needs-supervisor``. ``merged``:
        the merged PRs already read (``_do_supervise``, a retry refused for them)."""
        lb = self.cfg.labels
        if merged is None:
            merged = self._merged_prs(number)
        if not merged:
            self.log('  no merged devloop PR found any more; next tick')
            return
        listed = ', '.join(f'#{m["number"]}' + (f' (merge commit `{m["merge_commit"][:12]}`)'
                                                if m['merge_commit'] else '') for m in merged)
        reason = (f'L\'issue è aperta e ha `{lb.ready}`, ma è già stata mergiata una PR devloop '
                  f'che la risolve: {listed}. devloop non la controlla né la implementa di nuovo, '
                  'per non duplicare il lavoro. Se è risolta va chiusa; se serve altro lavoro, '
                  'va aperta una nuova issue.')
        supervised, why = self._supervised(number, MERGED_ISSUE, 'implement')
        # From a refused retry the issue is in agent:needs-supervisor: it leaves it only for
        # the operator.
        remove = [lb.ready] if supervised else [lb.ready, lb.needs_supervisor]
        self._escalate(number, reason, remove=remove, cls=MERGED_ISSUE,
                       supervised=supervised, why=why)
