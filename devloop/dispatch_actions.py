"""The work actions of the dispatcher: the mixin ``ActionsMixin`` of ``Dispatcher``.

Moved from ``devloop/dispatcher.py`` without changes (#296, step S6 of #261): the implementation
of an issue (``_do_implement``), a failed CI (``_do_ci_failed``, ``_current_receipt``,
``_last_round_allowed``) and its re-run when the failure does not concern the PR
(``_rerun_unrelated``), the review (``_do_review``, ``_open_round``, ``_together``,
``_full_suite_without_ci``), the fixes (``_do_fix``, ``_align_fix``, ``_publish_fix``,
``_do_requested_fix``) and the sync with the base branch (``_do_sync``). ``Dispatcher`` inherits
the methods. The module-level helpers they need (``rerun_of``, ``synced_from``,
``skipped_models``, ``Models``, ``ready_approval``) moved with them; since #297 (step S7) they are
imported from here, not re-exported by ``devloop/dispatcher.py``.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .acceptance import acceptance_criteria, format_criteria, format_map, latest_map, render_map
from .agents import AgentError
from .apps import DISPATCHER, IMPLEMENT, PR_COMMENT, RERUN, REVIEW
from .context import (_failure_text, _format_conflicts, _format_findings, _review_failure,
                      _start_failure, _template, _trusted_comments, docs_diff_section,
                      docs_map_section, docs_review_section, format_ci_summary,
                      format_fix_requests, format_operator_comments, format_previous_rounds)
from .github import GitHubError, job_id
from .ledger import fix_register
from .localtests import failed_modules, select_modules, suite_summary
from .markdown import (dispatcher_header, publishable, render_fix, render_native_review,
                       render_review, without_attribution)
from .planning import (BASE_CHANGED, FIX_COMMAND, RESYNC_AFTER_APPROVAL, ci_joins_round,
                       counted_rounds, pending_reviewers, preflight_state, voided_approval,
                       waived_rounds, waiting_reviewers)
from .policy import ZERO_LEVEL, distinct, independent, profile_of
from .ports import summarize_checks
from .review import (ci_failure_review, current_round, last_round, normalize_review,
                     open_findings, owner_of, round_verdict)
from .supervision import SYNC_FAILED, TOOL_FAILURE
from .workspace import WorkspaceError


# The native review of an agent with an app is its review record (#325): a failed submission
# is tried again, after a pause doubled each time, before the step escalates.
REVIEW_ATTEMPTS = 3
REVIEW_RETRY_PAUSE = 5  # seconds before the second attempt


def rerun_of(reruns, head):
    """The re-run recorded for ``head`` among ``reruns`` (#200), or None: one per head."""
    return next((r for r in reruns if r['head'] == head), None)


def synced_from(syncs, head, reviewed, limit=10):
    """Whether ``head`` comes from ``reviewed`` only through syncs whose conflicts an agent
    resolved (#250): walking back from ``head``, each step is a ``sync`` marker whose
    ``head_sha`` is the current commit and whose ``from`` is the previous one, up to
    ``reviewed`` (at most ``limit`` steps). A head that someone else moved is not."""
    back = {s['head_sha']: s.get('from') for s in syncs if isinstance(s.get('from'), str)}
    for _ in range(limit):
        if head not in back:
            return False
        head = back[head]
        if head == reviewed:
            return True
    return False


def skipped_models(used):
    """``{"skipped_models": [{model, reason, until}]}`` for the marker of a turn that ran with
    ``used`` after skipping models remembered unavailable (#151), or ``{}``. Informative only:
    no receipt, independence check nor approval reads it."""
    if not used.skipped:
        return {}
    return {'skipped_models': [{'model': model, 'reason': reason, 'until': until}
                               for model, reason, until in used.skipped]}


class Models:
    """The models one action may still run ``agent`` with (``AgentConfig.chain``), in order, and
    those it tried or left out, as ``(model, why)`` pairs (#121); ``skipped``, those it left out
    because remembered unavailable, ``(model, reason, until)`` each (#151)."""

    def __init__(self, agent):
        self.agent = agent
        self.left = agent.chain()
        self.tried = []
        self.skipped = []


def ready_approval(labeled, ready_label, admitters):
    """The ``(login, at)`` of the latest ``labeled`` event of ``ready_label`` in ``labeled``
    (``(label, login, at)``, oldest first) when one of ``admitters`` added it, else None. The
    latest event decides (#178), as for the queue: an approval replaced since by someone else's
    label no longer counts."""
    latest = [(who, at) for name, who, at in labeled if name == ready_label][-1:]
    return latest[0] if latest and latest[0][0] in admitters else None


class ActionsMixin:
    """The work actions of ``Dispatcher``: implement, CI failure, review, fix and sync (#296).
    They use the dispatcher state and its other methods through ``self``."""

    def _story_root(self, number, comments):
        """With ``story_identity`` (#363), the repository of which issue ``number`` is the root
        of a story at its takeover, for its ``request`` record; None when the identity is off,
        and for a child, which inherits its story only from the dispatcher's marker
        (``Ledger.story_of``) and does not become a root. A story declared in any text is not
        inherited: the issue is the root of a story of its own."""
        if not self.cfg.story_identity:
            return None
        story = self.ledger.story_of(number, comments, self.cfg.repo)
        if story is not None and story['issue'] != number:
            self.log(f'  #{number} is a child of story {story["story"]} (root '
                     f'#{story["issue"]})')
            return None
        return self.cfg.repo

    def _do_implement(self, number):
        cfg, lb = self.cfg, self.cfg.labels
        agent = cfg.agent(cfg.implementer)
        if cfg.preflight and preflight_state(self._preflight_view(number, ()))[0] != 'ready':
            # The text changed, or a new check was asked, after the snapshot.
            self.log('  the readiness check does not cover the current text; next tick')
            return
        issue, comments = self._issue_context(number)
        if self.ledger is not None:
            # The request as the implementer gets it, at the takeover (#133): not necessarily
            # the text at the time of the label.
            self.ledger.request(number, title=issue['title'], body=issue.get('body') or '',
                                labeled=ready_approval(issue['labeled'], lb.ready,
                                                       issue['admitters']),
                                comments=_trusted_comments(issue.get('comments') or [],
                                                           cfg.trusted_users)
                                + issue['accepted'],
                                story_repo=self._story_root(number, issue['thread']))
        self.gh.edit_labels(number, add=[lb.working], remove=[lb.ready])
        name, branch = f'issue-{number}', f'{cfg.branch_prefix}{number}'
        path = self.ws.new_branch(name, branch)
        # The input the turn runs on (#182), bound before it runs: the commit its branch starts
        # from and the request its prompt gets.
        start = self.ws.head(path)
        key = self._issue_key('implement', start, issue['request'])
        criteria = acceptance_criteria(issue.get('body'))
        # The verified resolution of a unanimous conclave on this issue, if any (#142).
        decision, _, resolution = self._resolution_for(number, issue)

        def prompt(attempt):
            # #203: the first attempt has no diff yet and gets the whole map; a later one the
            # documents mapped to the diff it left.
            docs = (docs_map_section(self._mapped_entries(path)) if attempt == 0
                    else docs_diff_section(self._mapped_docs(path)))
            return _template('implement', repo=cfg.repo, base_branch=cfg.base_branch,
                             issue_number=number, issue_title=issue['title'],
                             issue_body=issue.get('body') or '', issue_comments=comments,
                             acceptance_criteria=format_criteria(criteria),
                             test_cmd=cfg.test_cmd, language=cfg.language, docs=docs,
                             conclave=resolution)
        try:
            result, failure, used, tested, local = self._write_turn(
                agent, prompt, 'implement', path, f'issue-{number}-implement',
                where=(number, 'implement'))
        except AgentError as exc:
            exc.key = key
            raise
        if result['status'] == 'blocked':
            return self._blocked(number, used, result['blocked_reason'], remove=[lb.working],
                                 on_pr=False, step='implement', head=start, conclave=decision)
        if failure:
            return self._escalate(number, failure[0], failure[1], remove=[lb.working],
                                  cls='tests_failed')
        auth = self._git_auth(agent.key, IMPLEMENT)
        commit = self.ws.commit_all(
            path, self._commit_message(agent, result['commit_message'], number), author=auth)
        self._bind_tested(tested, path, commit)
        protected = self._protected_changes(path)
        if protected:
            return self._escalate_protected(number, protected, remove=[lb.working])
        veto = self._veto_after(number, None, decision) if decision is not None else None
        if veto:
            # Nothing produced with a vetoed resolution is published (#142).
            return self._veto_suspend(number, decision, veto, [lb.working],
                                      'l\'implementazione prodotta con quella risoluzione non è '
                                      'pubblicata: il commit resta nel worktree locale.')
        self.ws.push(path, branch, replace=True, auth=auth)
        self._pushed(path)
        # The marker keeps the map the reviewer checks until a fix replaces it (#134).
        meta = {'issue': number, 'implementer': agent.key, 'reviewer': cfg.reviewer,
                'profile': self._profile(agent.key, 'write', used),
                'acceptance_map': result['acceptance_map'],
                # Informative only (#169): no receipt nor approval depends on it.
                'local_tests': local, **skipped_models(used)}
        if decision is not None:
            meta['conclave'] = decision  # the resolution its prompt had (#142)
        body = publishable(self.runner.header(used) + '\n' + result['pr_body'].strip()
                           + '\n\n' + render_map(result['acceptance_map'])
                           + f'\n\nCloses #{number}', 'pr', meta)
        pr_number, url = self._as(agent.key, IMPLEMENT).create_pr(
            base=cfg.base_branch, head=branch, title=result['pr_title'], body=body)
        self.gh.edit_labels(pr_number, add=[lb.review])
        self.gh.edit_labels(number, remove=[lb.working])
        self.ws.remove(name)
        self.log(f'  opened {url}')

    def _current_receipt(self, pr):
        """``(receipt, paths)`` of the snapshot's head (see ``_receipt_and_paths``), or
        ``(None, None)`` (logged) when the head moved since."""
        receipt, paths = self._receipt_and_paths(pr)
        if receipt['head'] != pr.head_sha:
            self.log('  head moved since the snapshot; retrying next tick')
            return None, None
        return receipt, paths

    def _do_ci_failed(self, pr):
        receipt, paths = self._current_receipt(pr)
        if receipt is None:
            return
        if receipt['level'] > 1 and len(receipt['reviewers']) < receipt['level']:
            return self._escalate_level(pr, receipt)
        if self._rerun_unrelated(pr, paths):
            return
        log_tail = '\n'.join(self.gh.failed_log(run) for run in pr.failed_runs[:2])
        # One head, one round (#352, #359): a failure on the head of a round that asked for
        # changes, or approved, is a ci finding of that round, not a round of its own.
        joined = ci_joins_round(pr)
        voided = bool(joined) and voided_approval(joined)
        round_no = joined[0]['round'] if joined else last_round(pr.reviews) + 1
        earlier = [r for r in pr.reviews if r['round'] < round_no]
        review = ci_failure_review(failed_checks=pr.failed_checks, log_tail=log_tail,
                                   previous_open=open_findings(earlier, pr.closures),
                                   round_no=round_no,
                                   max_rounds=self._last_round_allowed(pr.reviews),
                                   head_sha=pr.head_sha)
        notes = []
        if voided:
            notes.append(f'La review del round {round_no} ha approvato questo stesso commit: '
                         'il fallimento della CI fa decadere l\'approvazione ed entra nel suo '
                         'round, non ne apre uno nuovo; il verdetto del round diventa '
                         '`request_changes` e la correzione risponde ai finding della CI.')
        elif joined:
            notes.append(f'La review del round {round_no} ha chiesto modifiche su questo stesso '
                         'commit: il fallimento della CI entra nel suo round, non ne apre uno '
                         'nuovo, e una sola correzione risponde a tutti i finding.')
        rerun = rerun_of(pr.reruns, pr.head_sha)
        if rerun is not None:
            notes.append(
                'I job falliti di questo commit erano già stati rilanciati una volta, perché '
                'nessun modulo fallito era pertinente alla PR (run '
                + ', '.join(str(r) for r in rerun['runs']) + '; moduli: '
                + (', '.join(rerun['modules']) or 'nessuno') + '). Anche il rilancio è fallito: '
                + ('questo fallimento entra nel round.' if joined
                   else 'questo fallimento conta come round.'))
        review['note'] = ' '.join(notes)
        review['receipt'] = receipt
        if self.ledger is not None:
            # The event of a joined round is a section of that round's register (#325).
            details = self.gh.check_details(pr.head_sha)
            # Its approval voided by the failure leaves a trace in the event (#359).
            self._record_ci(pr, pr.head_sha, summarize_checks(details)[0], details,
                            review['round'], jobs=self._ci_jobs(details),
                            voided_approval=voided)
        self.gh.comment(pr.number, render_review(review, dispatcher_header()))
        records = joined + [review]
        if joined and waiting_reviewers(records, self.cfg.conclave):
            # The round still waits for its other reviewers: it is applied after them.
            self.log(f'  CI failure joins round {round_no}, which waits for '
                     + ', '.join(waiting_reviewers(records, self.cfg.conclave)))
            return
        if joined:
            self.log(f'  CI failure joins round {round_no}'
                     + (', voiding its approval' if voided else ''))
        self._apply_round(pr, records, announce=True, reviews=earlier + records)

    def _rerun_unrelated(self, pr, paths):
        """Re-run once the failed jobs of the head's CI when its failure does not concern the
        PR (#200); whether it did. Only at the first failure of a head (``rerun`` marker), with
        ``ci_rerun_unrelated``, when every failed check is a GitHub Actions job whose own log
        names the failed test modules (``failed_modules``) and none of them is pertinent to
        ``paths``,
        the PR's diff, as ``local_tests = "changed"`` would select them (``select_modules``).
        A re-run that GitHub refuses, such as a 403 without ``actions: write``, is logged and
        the failure is a round, as before."""
        if not self.cfg.ci_rerun_unrelated or rerun_of(pr.reruns, pr.head_sha) is not None:
            return False
        details = self.gh.check_details(pr.head_sha)
        failed = [c for c in details if c.get('bucket') == 'fail']
        runs = sorted({c.get('run_id') for c in failed}, key=str)
        if not failed or None in runs:
            self.log('  CI rerun: ' + ('no failed check read again' if not failed else
                                       'not every failed check is a GitHub Actions run')
                     + '; CI round')
            return False
        # Each failed job on its own: a run whose suite fails only in modules not pertinent to
        # the PR may hold another failed job, such as a benchmark, that names none.
        modules = set()
        for check in failed:
            job = job_id(check.get('link'))
            found = None if job is None else failed_modules(
                self.gh.failed_log(check['run_id'], max_lines=None, job_id=job))
            if found is None:
                self.log(f'  CI rerun: the log of the failed job {check.get("name")} (run '
                         f'{check["run_id"]}) does not tell which test modules failed; CI round')
                return False
            modules.update(found)
        modules = sorted(modules)
        pertinent = select_modules(paths, modules)
        if pertinent:
            self.log(f'  CI rerun: failed modules pertinent to the PR: {", ".join(pertinent)}; '
                     'CI round')
            return False
        try:
            rerunner = self._as(DISPATCHER, RERUN)
            for run in runs:
                rerunner.rerun_failed_jobs(run)
        except GitHubError as exc:
            self.log(f'  CI rerun refused, the failure is a round as before (the dispatcher\'s '
                     f'app needs actions: write): {exc}')
            return False
        record = {'head': pr.head_sha, 'runs': runs, 'modules': modules,
                  'checks': sorted(c['link'] for c in failed)}
        self.gh.comment(pr.number, publishable(
            dispatcher_header() + '\n## CI rilanciata\n\n'
            f'La CI di `{pr.head_sha[:12]}` è fallita solo in moduli di test non pertinenti al '
            f'diff della PR ({", ".join(f"`{m}`" for m in modules)}): il dispatcher ha '
            'rilanciato una volta i job falliti (run '
            + ', '.join(str(r) for r in runs) + '), senza aprire un round. Se anche il '
            'rilancio fallisce, il fallimento conta come round.', 'rerun', record))
        self._ledger_event(pr.number, 'ci_rerun', head=pr.head_sha, runs=runs, modules=modules)
        self.log(f'  CI rerun of {", ".join(str(r) for r in runs)}: failed modules not '
                 f'pertinent to the PR ({", ".join(modules)})')
        return True

    def _ci_jobs(self, details):
        """The checks ``details`` (``check_details``) as the summary of the CI shows them
        (#300): ``{name, outcome, bucket, duration_s}``, and for a completed GitHub Actions job
        ``tests``, the final lines of ``scripts/run-tests.py`` its log holds
        (``suite_summary``, None when it holds none), or ``log: unavailable`` when the log
        could not be read: that only leaves the summary out, it never stops the action."""
        jobs = []
        for check in details:
            job = {'name': check.get('name'), 'outcome': check.get('conclusion')
                   or check.get('bucket'), 'bucket': check.get('bucket'),
                   'duration_s': check.get('duration_s')}
            number = job_id(check.get('link')) if check.get('run_id') is not None else None
            if number is not None and check.get('bucket') != 'pending':
                try:
                    log = self.gh.job_log(number)
                except GitHubError as exc:
                    self.log(f'  log of the job {check.get("name")} not readable: {exc}')
                    log = None
                if log:
                    job['tests'] = suite_summary(log)
                else:
                    job['log'] = 'unavailable'
            jobs.append(job)
        return jobs

    def _ci_summary(self, pr):
        """``(section, jobs)``: the summary of the CI of ``pr``'s head for the review and fix
        prompts (#300, ``format_ci_summary``) and its jobs (``_ci_jobs``, None when the checks
        could not be read). The checks of the snapshot when it read them."""
        try:
            details = (pr.check_details if pr.check_details is not None
                       else self.gh.check_details(pr.head_sha))
        except GitHubError as exc:
            self.log(f'  checks of {pr.head_sha[:12]} not readable for the CI summary: {exc}')
            return format_ci_summary(pr.head_sha, pr.checks, None), None
        jobs = self._ci_jobs(details)
        return format_ci_summary(pr.head_sha, summarize_checks(details)[0], jobs), jobs

    def _last_round_allowed(self, reviews):
        """The number of the last round ``max_review_rounds`` allows, given the waived rounds
        among ``reviews``: what records and prompts show as the limit."""
        return self.cfg.max_review_rounds + len(waived_rounds(reviews))

    def _open_round(self, pr, records):
        """Whether the round ``records`` is still open for this head: an agent round of the
        same head, not answered by a fix nor voided by a rereview request, still waiting for a
        reviewer (not one a ``needs_operator`` suspended, with the conclave, #141). Whether a
        new review joins it depends on the receipt (``_do_review``)."""
        return (bool(records) and records[0]['source'] == 'agent'
                and records[0]['head_sha'] == pr.head_sha and not pr.rereview
                and not any(f['round'] == records[0]['round'] for f in pr.fixes)
                and bool(waiting_reviewers(records, self.cfg.conclave)))

    def _do_review(self, pr):
        """One review by one reviewer: each review call is an action of its own. The first
        reviewer of a round opens it; the next ones, on later actions, join it while it reviews
        the same head under the same receipt. The round is applied once every required reviewer
        has reviewed; a review already recorded in it is never run again.

        With ``max_parallel_actions`` above 1 (#176) the action runs at once every reviewer the
        round still waits for, each in its own copy of the worktree and on the same prompt, and
        records each verdict as before. A reviewer that fails leaves the others' verdicts
        recorded, and the round waits only for it; its failure ends the action as before. A
        fallback model must also be distinct from the profiles the other reviewers of the
        action have reserved.

        When only the base changed since the round's reviews (#160), the round continues if no
        commit of the base branch in between reached the PR's files, and the review records the
        check (``bases``). Otherwise the round restarts, and the new one records which round it
        replaces (``restarts``): that round does not count against ``max_review_rounds``.

        A new round on a head that a clean sync moved from the approved one (#235,
        ``PullRequest.resync``), or on the approved head after the base touching the PR revoked
        its approval (#252), records ``resync`` and does not count either."""
        cfg = self.cfg
        receipt, paths = self._current_receipt(pr)
        if receipt is None:
            return
        if receipt['level'] == ZERO_LEVEL:
            return self._promote_zero(pr, receipt)
        if len(receipt['reviewers']) < receipt['level']:
            return self._escalate_level(pr, receipt)
        latest = current_round(pr.reviews)
        bases = self._base_checks(latest, receipt, paths) if self._open_round(pr, latest) else None
        if bases and any(c['outcome'] == 'conflict' for c in bases):
            # Neither continued nor restarted: the sync merges the base and changes the head,
            # once GitHub reports the conflict too.
            self.log('  the PR conflicts with the new base; no review until the sync')
            return
        joins = bases is not None and all(c['outcome'] == 'disjoint' for c in bases)
        restarts = resync = None
        if joins:
            round_no, done, comments = latest[0]['round'], latest, pr.round_comments
            counted = counted_rounds(pr.reviews)
        else:
            round_no, done, comments = last_round(pr.reviews) + 1, [], pr.operator_comments
            counted = counted_rounds(pr.reviews) + 1
            if bases:
                # Abandoned only because the base advanced: the round does not count.
                restarts = {'round': latest[0]['round'], 'bases': bases}
                counted -= 1
                self.log(f'  round {latest[0]["round"]} restarts: the base touched the PR')
            elif pr.resync and not pr.rereview:
                # Only a clean sync moved the approved head (#235), or only the base touching
                # the PR revoked its approval (#252): this round does not count.
                resync = pr.resync
                counted -= 1
                why = ('approval was revoked because the base touched the PR'
                       if resync['reason'] == BASE_CHANGED else 'head was synced with the base')
                self.log(f'  the {why} after round {resync["round"]}: this round does not count')
        if counted > cfg.max_review_rounds:
            return self._do_round_limit(pr)
        max_rounds = cfg.max_review_rounds + round_no - counted
        reviewed = {owner_of(r) for r in done}
        waited = [key for key in receipt['reviewers'] if key not in reviewed]
        # One reviewer per action, or with parallel actions (#176) every one the round waits
        # for, each in a thread of its own.
        keys = waited if cfg.max_parallel_actions > 1 else waited[:1]
        issue, _ = self._issue_context(pr.issue)
        name = f'pr-{pr.number}'
        path = self.ws.existing_branch(name, pr.head_ref)
        if self.ws.head(path) != pr.head_sha:
            self.log('  head moved since the snapshot; retrying next tick')
            return
        # Within the window of a just pushed head (#263) the review starts as if its CI were
        # pending (#271): the reviewer is told so, and no full suite runs locally in its place.
        ci = 'pending' if pr.ci_wait else pr.checks
        if not joins and cfg.local_tests != 'full' and ci == 'none':
            # The first reviewer of a round runs it; the next ones review the same head.
            if not self._full_suite_without_ci(pr, path, name):
                return
        # Each reviewer of a round judges the head on its own: earlier rounds only.
        earlier = [r for r in pr.reviews if r['round'] < round_no]
        previous_open = open_findings(earlier, pr.closures)
        # The criteria of the issue text read now, and the latest map of the PR (#134).
        criteria = acceptance_criteria(issue.get('body'))
        acceptance_map = latest_map(pr.meta, pr.fixes)
        # The jobs of the head's CI, their durations and test summaries (#300).
        ci_summary, ci_jobs = self._ci_summary(pr)
        # The verified resolution of a unanimous conclave on the issue or the PR (#142).
        decision, _, resolution = self._resolution_for(pr.issue, issue, pr, pr.head_sha)
        # Blind to the author (#389): no implementer key, no attribution header, no marker. The
        # key stays in the PR's marker and in the ledger, for the selection of the reviewers.
        prompt = _template('review', repo=cfg.repo, base_branch=cfg.base_branch,
                           pr_number=pr.number, round=round_no, max_rounds=max_rounds,
                           ci=ci, ci_summary=ci_summary, test_cmd=cfg.test_cmd,
                           issue_number=pr.issue, issue_title=issue['title'],
                           issue_body=without_attribution(issue.get('body')),
                           pr_body=without_attribution(pr.body),
                           acceptance_criteria=format_criteria(criteria),
                           acceptance_map=format_map(acceptance_map),
                           docs=docs_review_section(self._mapped_docs(path)),
                           diff_stat=self.ws.diff_stat(path),
                           previous=format_previous_rounds(earlier, pr.fixes, pr.closures),
                           operator_comments=format_operator_comments(comments),
                           language=cfg.language, conclave=resolution)
        level2 = receipt['level'] > 1
        authors = (self._authors(pr) or []) if level2 else []
        peers = [p for p in map(profile_of, done) if p]
        # The profile each reviewer of this action runs with, reserved before any turn starts
        # and replaced atomically when a fallback model takes over (#176); a reviewer that
        # fails gives its reservation up.
        reserved = {key: self._profile(key, 'review') for key in keys}
        reserving = threading.Lock()

        def allowed_for(key):
            if not level2:
                return None

            def allowed(candidate):
                # A fallback model must keep the independence the pair was selected for: of
                # who wrote the code, of the round's other reviewers as their records show
                # them, and of the reviewers running alongside it in this action.
                p = self._profile(key, 'review', candidate)
                with reserving:
                    others = [q for k, q in reserved.items() if k != key]
                    if not (all(independent(p, a) for a in authors)
                            and all(distinct(p, q) for q in peers + others)):
                        return False
                    reserved[key] = p
                    return True
            return allowed

        def review_turn(key):
            # The reviewer works in a disposable copy, where its tests may write; the PR
            # worktree must come out of the review untouched, or the verdict is discarded.
            copy_name, copy = (self.ws.review_copy(path, key) if len(keys) > 1
                               else self.ws.review_copy(path))
            try:
                # Every turn of the round goes to the round's register (#325).
                return self._turn(Models(cfg.agent(key)), 'review', prompt, 'review', copy,
                                  f'pr-{pr.number}-review', allowed_for(key),
                                  where=(pr.number, 'review', round_no))
            except BaseException as exc:
                with reserving:
                    reserved.pop(key, None)
                if isinstance(exc, AgentError):
                    exc.reviewer = key  # part of the key of the supervisor's retries (#182)
                raise
            finally:
                self.ws.remove(copy_name)

        if len(keys) == 1:
            turns = [(keys[0], review_turn(keys[0]), None)]
        else:
            turns = self._together(keys, review_turn)
        failures = [(key, exc) for key, _, exc in turns if exc is not None]
        for key, exc in failures:
            self.log(f'  review by {key} failed: {exc}')
        if not self.ws.pristine(path, pr.head_sha):
            raise WorkspaceError(f'the worktree of PR #{pr.number} changed during the review; '
                                 'verdict discarded')
        if level2 and self.gh.pr_head(pr.number) != pr.head_sha:
            # A verdict on a superseded head joins no round: the new head is reviewed afresh.
            self.log('  head moved during the review; verdict discarded')
            self.ws.remove(name)
            if failures:
                raise _review_failure(failures)
            return
        veto = (self._veto_after(pr.issue, pr.number, decision)
                if decision is not None and len(failures) < len(turns) else None)
        if veto:
            # A verdict given with a vetoed resolution is not published (#142).
            self.ws.remove(name)
            return self._veto_suspend(pr.number, decision, veto,
                                      [self.cfg.labels.review, self.cfg.labels.changes,
                                       self.cfg.labels.ready_to_merge],
                                      'la review data con quella risoluzione non è pubblicata.')
        records = list(done)
        for key, (raw, used), _ in (turn for turn in turns if turn[2] is None):
            review = normalize_review(raw, previous_open=previous_open, round_no=round_no,
                                      max_rounds=max_rounds, head_sha=pr.head_sha,
                                      ci=ci, reviewer=key, criteria=criteria,
                                      acceptance_map=acceptance_map)
            review['receipt'] = receipt
            if joins and bases:
                review['bases'] = bases  # the round's earlier bases, checked up to this one
            if restarts:
                review['restarts'] = restarts
            if resync:
                review['resync'] = resync
            review['profile'] = self._profile(key, 'review', used)
            review.update(skipped_models(used))
            if decision is not None:
                review['conclave'] = decision  # the resolution its prompt had (#142)
            reviewer_gh = self._as(key, REVIEW)
            if self._app_login(key):
                # One native review under the reviewer's own app (#325): its body is the
                # record devloop reads, and its event the verdict branch protection sees.
                self._submit_review(reviewer_gh, pr, key,
                                    render_native_review(review, self.runner.header(used)))
            else:
                # An agent without an app publishes with the operator's login: the comment
                # stays its record, as before.
                reviewer_gh.comment(pr.number, render_review(review, self.runner.header(used)))
            records.append(review)
        new = records[len(done):]
        if new and ci != 'pending':
            # The CI the round reviewed, whatever its verdict (#133).
            self._record_ci(pr, pr.head_sha, pr.checks, pr.check_details, round_no,
                            jobs=ci_jobs)
        # With the conclave (#141) the first needs_operator suspends the round: it is applied
        # now, and the reviewers it still waited for are not asked.
        waiting = waiting_reviewers(records, cfg.conclave)
        if new and not waiting:
            self._apply_round(pr, records, announce=True, reviews=earlier + records)
        self.ws.remove(name)
        for position, review in enumerate(new):
            last = position == len(new) - 1
            self.log(f'  verdict of {review["reviewer"]}: {review["verdict"]}'
                     + (f'; the round waits for {", ".join(waiting)}' if waiting and last
                        else ''))
        if failures:
            # The verdicts above stay recorded: the round waits only for these reviewers.
            raise _review_failure(failures)

    review_retry_pause = REVIEW_RETRY_PAUSE

    def _submit_review(self, reviewer_gh, pr, key, native):
        """Publish ``native`` (``render_native_review``), the review record of ``key``, as a
        native review of ``pr``'s head with the reviewer's client (#325). A failure is tried
        again up to ``REVIEW_ATTEMPTS`` times in all, after a pause doubled each time; before
        each new attempt the reviews are read again, and a review already published with the
        same body by the reviewer's app ends the retries. After the last failure it raises
        ``GitHubError``: the step escalates like any failed write, and the verdicts already
        published stay recorded."""
        login = self._app_login(key)
        error = None
        for attempt in range(1, REVIEW_ATTEMPTS + 1):
            if attempt > 1:
                time.sleep(self.review_retry_pause * 2 ** (attempt - 2))
                try:
                    published = any(r.get('login') == login and r.get('body') == native['body']
                                    for r in self.gh.pr_reviews(pr.number))
                except GitHubError as exc:
                    self.log(f'  reviews of #{pr.number} not readable: {exc}')
                    published = False
                if published:
                    self.log(f'  native review of {key} found published after the error')
                    return
            try:
                reviewer_gh.submit_review(pr.number, commit=pr.head_sha, **native)
                return
            except GitHubError as exc:
                error = exc
                self.log(f'  native review of {key} not submitted (attempt {attempt} of '
                         f'{REVIEW_ATTEMPTS}): {exc}')
        raise GitHubError(f'the review of {key} was not published: its native review, the '
                          f'record devloop reads, failed {REVIEW_ATTEMPTS} times ({error})')

    def _together(self, keys, turn):
        """``(key, result, exception)`` of ``turn(key)`` for each of ``keys``, in order, run
        at once in threads of their own that share the action's log prefix (#176)."""
        prefix = self.log.prefix

        def call(key):
            with self.log.prefixed(prefix):
                try:
                    return key, turn(key), None
                except Exception as exc:  # returned to the action
                    return key, None, exc
        with ThreadPoolExecutor(max_workers=len(keys),
                                thread_name_prefix='devloop-review') as pool:
            return list(pool.map(call, keys))

    def _full_suite_without_ci(self, pr, path, name, approved=False):
        """The CI gate when the local tests may not have covered the whole suite (#169): with
        ``local_tests`` other than ``full`` a review needs the CI passed, and a head without CI
        gets the full ``test_cmd`` locally first, in a disposable copy of the PR worktree.
        Returns whether the review may start; a failure escalates the PR. ``approved``: the
        review already approved the head while its CI was not known (#271), and the suite
        runs before the promotion instead."""
        lb = self.cfg.labels
        when = 'promotion' if approved else 'review'
        self.log(f'  no CI on {pr.head_sha[:12]} with local_tests = {self.cfg.local_tests}: '
                 f'full suite before the {when}')
        copy_name, copy = self.ws.review_copy(path)
        try:
            passed, tail, _ = self._run_tests((pr.number, 'review'), copy, 1, self.cfg.test_cmd)
        finally:
            self.ws.remove(copy_name)
        if passed:
            return True
        self.ws.remove(name)
        self._escalate(pr.number, f'Sul head `{pr.head_sha[:12]}` non risulta alcuna CI e, con '
                                  f'`local_tests = "{self.cfg.local_tests}"`, i test locali non '
                                  'hanno eseguito la suite completa. La suite completa '
                                  f'(`{self.cfg.test_cmd}`), eseguita in locale prima della '
                                  + ('promozione, fallisce: l\'approvazione, data prima che la CI '
                                     'fosse nota, non porta la PR al merge.' if approved else
                                     'review, fallisce: la review non parte.'), tail,
                       remove=[lb.review], cls='tests_failed')
        return False

    def _do_fix(self, pr):
        cfg, lb = self.cfg, self.cfg.labels
        if pr.fix_requests:
            return self._do_requested_fix(pr)
        if not pr.reviews:
            return self._escalate(pr.number, 'Modifiche richieste ma nessuna review devloop da '
                                             'correggere.', remove=[lb.changes],
                                  cls='inconsistent')
        # One fix answers the whole round, with every finding still open, whoever owns it.
        latest = current_round(pr.reviews)[0]
        findings = open_findings(pr.reviews, pr.closures)
        if not findings:
            return self._escalate(pr.number, 'Modifiche richieste ma nessun finding aperto.',
                                  remove=[lb.changes], cls='inconsistent')
        if pr.meta['implementer'] not in cfg.agents:
            return self._escalate(
                pr.number, f'L\'implementer della PR (`{pr.meta["implementer"]}`) non è più '
                           'configurato: serve una decisione su chi corregge.',
                remove=[lb.changes], cls='configuration')
        agent = cfg.agent(pr.meta['implementer'])
        issue, _ = self._issue_context(pr.issue)
        name = f'pr-{pr.number}'
        path = self.ws.existing_branch(name, pr.head_ref)
        head = self.ws.head(path)
        # A sync that a fix started, because the base conflicted with the branch, moved the
        # reviewed head on purpose (#250): the fix answers the round on the synced head.
        if head != latest['head_sha'] and not synced_from(pr.syncs, head, latest['head_sha']):
            self.log('  branch changed after the review; sending it back to review')
            self.gh.edit_labels(pr.number, add=[lb.review], remove=[lb.changes])
            return
        # The resolution of a unanimous conclave, bound to the head the fix starts from (#142).
        decision, _, resolution = self._resolution_for(pr.issue, issue, pr, head)
        start, merged = self._align_fix(pr, agent, path, remove=[lb.changes])
        if start is None:
            return
        ci_summary = self._ci_summary(pr)[0]

        def prompt(attempt):
            # The documents mapped to the diff each attempt starts from (#203).
            return _template('fix', repo=cfg.repo, pr_number=pr.number, issue_number=pr.issue,
                             issue_title=issue['title'], issue_body=issue.get('body') or '',
                             findings=_format_findings(findings), test_cmd=cfg.test_cmd,
                             ci_summary=ci_summary,
                             acceptance_criteria=format_criteria(
                                 acceptance_criteria(issue.get('body'))),
                             acceptance_map=format_map(latest_map(pr.meta, pr.fixes)),
                             docs=docs_diff_section(self._mapped_docs(path)),
                             requests='', base_branch=cfg.base_branch, language=cfg.language,
                             conclave=resolution)
        # One register for the fix's turns, local tests and sync (#355).
        where = self._fix_where(pr.number, latest['round'])
        result, failure, used, tested, local = self._write_turn(
            agent, prompt, 'fix', path, f'pr-{pr.number}-fix-r{latest["round"]}', where=where)
        self._publish_fix(pr, agent, name, path, (result, failure, used, tested, local),
                          since=start, round_no=latest['round'], remove=[lb.changes],
                          merged=merged, conclave=decision, where=where)

    def _align_fix(self, pr, agent, path, remove):
        """Bring the branch of a fix in ``path`` up to ``origin/<base>`` before its agent starts
        (#250): CI runs on the PR's merge commit with the base, so a branch behind the base may
        fail for code the fix would not see. Returns ``(start, merged)``: the head the agent
        starts from, and the ``{base, head, from}`` of the merge commit the dispatcher made, or
        None when the branch already contained the base. A merge without conflicts is committed
        with the implementer's identity, as a sync's, and pushed with the fix. With conflicts
        the fix does not start: the usual sync runs instead, leaving the PR's labels as they are,
        and the fix runs on a later tick. ``start`` is None when the fix does not start (the
        sync, or a merge that touches ``protected_paths``, escalated with ``remove``)."""
        base = f'origin/{self.cfg.base_branch}'
        previous = self.ws.head(path)
        if self.ws.contains_base(path):
            return previous, None
        auth = self._git_auth(agent.key, IMPLEMENT)
        files = self.ws.start_merge(path, author=auth)
        if files is None:
            return previous, None
        if files:
            self.log(f'  the branch is behind {base} with conflicts in {", ".join(files)}: '
                     'sync first, the fix runs on a later tick')
            self._do_sync(pr, fix_remove=remove)
            return None, None
        target = self.ws.merge_head(path)
        head = self.ws.commit_all(path, f'Merge {base} into {pr.head_ref}\n\nRefs #{pr.issue}\n',
                                  author=auth)
        # As for a sync, the merge never differs from the base under protected_paths.
        protected = self._protected_changes(path, since=base)
        if protected:
            self._escalate_protected(pr.number, protected, remove=remove)
            return None, None
        self.log(f'  merged {base} ({target[:12]}) into {pr.head_ref} before the fix: {head[:12]}')
        return head, {'base': target, 'head': head, 'from': previous}

    def _publish_fix(self, pr, agent, name, path, turn, since, round_no, remove, extra=None,
                     merged=None, conclave=None, where=None):
        """Publish the outcome of a fix ``turn`` (``_write_turn``'s result) in worktree
        ``path``: escalate a blocked agent or failing tests (removing ``remove``), otherwise
        commit and push the changes made since commit ``since``, record the fix of round
        ``round_no`` (with ``extra`` fields in its marker) and send the PR back to review.
        ``merged``: the merge of the base the dispatcher made before the turn (``_align_fix``),
        pushed with the fix even when the agent changed nothing. ``conclave``: the comment of
        the conclave decision whose resolution the turn had (#142), recorded with the fix or the
        block; a veto of it written meanwhile publishes nothing. ``where``: the one the turn
        ran with (``_fix_where``), whose register gets the ``sync`` event of ``merged`` (#355)."""
        lb = self.cfg.labels
        result, failure, used, tested, local = turn
        if result['status'] == 'blocked':
            return self._blocked(pr.number, used, result['blocked_reason'], remove=remove,
                                 on_pr=True, step='fix', head=pr.head_sha, conclave=conclave)
        if failure:
            return self._escalate(pr.number, failure[0], failure[1], remove=remove,
                                  cls='tests_failed')
        veto = self._veto_after(pr.issue, pr.number, conclave) if conclave is not None else None
        if veto:
            return self._veto_suspend(pr.number, conclave, veto, remove,
                                      'la correzione prodotta con quella risoluzione non è '
                                      'pubblicata.')
        changes = self.ws.has_changes(path)
        if changes or merged:
            auth = self._git_auth(agent.key, IMPLEMENT)
            if changes:
                commit = self.ws.commit_all(
                    path, self._commit_message(agent, result['commit_message'], pr.issue),
                    author=auth)
                self._bind_tested(tested, path, commit)
                protected = self._protected_changes(path, since=since)
                if protected:
                    return self._escalate_protected(pr.number, protected, remove=remove)
            self.ws.push(path, pr.head_ref, auth=auth)
            self._pushed(path)
        if merged:
            # As for a sync without an agent (#133): an event, not a turn, in the fix's
            # register (#355).
            self._ledger_event(pr.number, 'sync', register=fix_register(where),
                               base=merged['base'], head=merged['head'],
                               previous_head=merged['from'])
        fix = dict(result, head_sha=self.ws.head(path),
                   profile=self._profile(agent.key, 'write', used), local_tests=local,
                   **skipped_models(used), **(extra or {}))
        if merged:
            fix['base_merge'] = dict(merged, branch=self.cfg.base_branch)
        if conclave is not None:
            fix['conclave'] = conclave
        self._as(agent.key, PR_COMMENT).comment(
            pr.number, render_fix(fix, self.runner.header(used), round_no))
        self.gh.edit_labels(pr.number, add=[lb.review], remove=[lb.changes])
        self.ws.remove(name)

    def _do_requested_fix(self, pr):
        """A fix of the current head asked with ``FIX_COMMAND`` by a trusted user or the
        supervisor (#195), on a PR in ``agent:review`` or ``agent:changes``: the implementer gets
        the requests as requirements, and in ``agent:changes`` the findings still open too. It
        follows the rules of every fix (local tests, protected paths, push, back to
        ``agent:review``) and writes no review record, so it spends no round. Its record carries
        the comments that asked for it and the head it ran on: they are spent, and a new command
        counts on that head only after a new commit (``_fix_requests``). The record belongs to
        the latest round, which it answers, so the next tick reviews the head again; before any
        review it belongs to round 0."""
        cfg, lb = self.cfg, self.cfg.labels
        remove = [lb.review, lb.changes]
        if pr.meta['implementer'] not in cfg.agents:
            return self._escalate(
                pr.number, f'L\'implementer della PR (`{pr.meta["implementer"]}`) non è più '
                           f'configurato: serve una decisione su chi esegue `{FIX_COMMAND}`.',
                remove=remove, cls='configuration')
        agent = cfg.agent(pr.meta['implementer'])
        requests = pr.fix_requests
        findings = (open_findings(pr.reviews, pr.closures) if lb.changes in pr.labels else [])
        round_no = last_round(pr.reviews)
        issue, _ = self._issue_context(pr.issue)
        name = f'pr-{pr.number}'
        path = self.ws.existing_branch(name, pr.head_ref)
        if self.ws.head(path) != pr.head_sha:
            self.log('  head moved since the snapshot; retrying next tick')
            return
        decision, _, resolution = self._resolution_for(pr.issue, issue, pr, pr.head_sha)
        start, merged = self._align_fix(pr, agent, path, remove=remove)
        if start is None:
            return
        ci_summary = self._ci_summary(pr)[0]

        def prompt(attempt):
            return _template('fix', repo=cfg.repo, pr_number=pr.number, issue_number=pr.issue,
                             issue_title=issue['title'], issue_body=issue.get('body') or '',
                             findings=_format_findings(findings), test_cmd=cfg.test_cmd,
                             ci_summary=ci_summary,
                             requests=format_fix_requests(requests),
                             acceptance_criteria=format_criteria(
                                 acceptance_criteria(issue.get('body'))),
                             acceptance_map=format_map(latest_map(pr.meta, pr.fixes)),
                             docs=docs_diff_section(self._mapped_docs(path)),
                             base_branch=cfg.base_branch, language=cfg.language,
                             conclave=resolution)
        where = self._fix_where(pr.number, round_no)
        turn = self._write_turn(agent, prompt, 'fix', path, f'pr-{pr.number}-fix-request',
                                where=where)
        self._publish_fix(pr, agent, name, path, turn, since=start, round_no=round_no,
                          remove=remove, extra={'requests': requests, 'from': pr.head_sha},
                          merged=merged, conclave=decision, where=where)

    def _sync_rounds_note(self, pr, conflicts, fix):
        """What the comment of a sync of ``pr`` says of the next review (#289): with
        ``conflicts`` an agent resolved, the content of the PR changed and the review counts
        against ``max_review_rounds``, with the rounds left; without, the review of an approved
        head does not count (#235), and any other review counts as it would have anyway.
        ``fix``: a fix waits for the sync, and its review is the next one."""
        limit = self.cfg.max_review_rounds
        latest = current_round(pr.reviews)
        approved = (bool(latest) and latest[0]['source'] == 'agent'
                    and not pending_reviewers(latest) and round_verdict(latest) == 'approve'
                    and all(r['head_sha'] == pr.head_sha for r in latest))
        resynced = bool(pr.resync) and pr.resync.get('reason') == RESYNC_AFTER_APPROVAL
        if not conflicts and not fix and not pr.rereview and (approved or resynced):
            return ('La review del nuovo head **non conterà** come round: differisce da quello '
                    'approvato solo per il merge senza conflitti della base (conta invece se la '
                    'CI fallisce o se viene chiesta una nuova review con `/devloop rereview`).')
        which = 'dopo la correzione' if fix else 'successiva'
        if fix:
            why = 'come ogni review dopo una correzione'
        elif conflicts:
            why = 'perché l\'agent ha risolto dei conflitti e il contenuto della PR è cambiato'
        else:
            why = 'come avrebbe contato senza la sincronizzazione'
        number = counted_rounds(pr.reviews) + 1
        if number > limit:
            where = (f'contati già {number - 1} round di {limit} (`max_review_rounds`); nessun '
                     f'round rimasto: la review {which} richiederà una decisione dell\'operatore')
        elif number == limit:
            where = f'round {number} di {limit} (`max_review_rounds`), l\'ultimo rimasto'
        else:
            where = (f'round {number} di {limit} (`max_review_rounds`); dopo questo ne '
                     f'resteranno {limit - number}')
        return f'La review {which} **conterà** come round, {why}: {where}.'

    def _do_sync(self, pr, fix_remove=None):
        """Merge the base branch into a conflicting PR. The implementer resolves the conflicts
        in the PR worktree, the dispatcher concludes the merge, pushes it and sends the PR back
        to review. No review record is written, so no round is spent.

        ``fix_remove``: the sync was started by a fix whose branch conflicts with the base
        (#250). Escalations remove these labels, and on success the labels stay as they are, so
        that the fix runs on the synced head on a later tick."""
        cfg, lb = self.cfg, self.cfg.labels
        remove = [lb.review, lb.ready_to_merge] if fix_remove is None else fix_remove
        base = f'origin/{cfg.base_branch}'
        if pr.meta['implementer'] not in cfg.agents:
            return self._escalate(
                pr.number, f'La PR è in conflitto con `{cfg.base_branch}` e il suo implementer '
                           f'(`{pr.meta["implementer"]}`) non è più configurato: serve una '
                           'decisione su chi risolve i conflitti.', remove=remove,
                cls='configuration')
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
        tested = None
        if files:
            message += '\n\nConflicts resolved in:\n' + '\n'.join(f'- {f}' for f in files)

            # A failed sync is non-critical (#182) unless a file in conflict is sensitive: a
            # path of the review policy's sensitive level or of ``operator_only_paths``.
            sensitive = [f for f in files if self.cfg.review_policy.covers(f)
                         or self.cfg.merge.operator_only(f)]

            def fail(reason, detail='', cls=None):
                cls = cls or ('sync_sensitive' if sensitive else SYNC_FAILED)
                supervised, why = self._supervised(pr.number, cls, 'sync', pr)
                self._escalate(pr.number, f'Sincronizzazione con `{cfg.base_branch}` non '
                                          f'riuscita. {reason}\n\nFile in conflitto:\n\n{listed}',
                               detail, remove=remove, cls=cls, supervised=supervised, why=why,
                               retry=None if supervised else self._retry_payload('sync', pr))
            prompt = _template('sync', repo=cfg.repo, pr_number=pr.number, issue_number=pr.issue,
                               issue_title=issue['title'], issue_body=issue.get('body') or '',
                               base_branch=cfg.base_branch, conflicts=listed,
                               test_cmd=cfg.test_cmd, language=cfg.language)
            try:
                result, failure, used, tested, _ = self._write_turn(
                    agent, prompt, 'sync', path, f'pr-{pr.number}-sync',
                    where=(pr.number, 'sync'))
            except AgentError as exc:
                # An agent that could not start, even after its retry (#218), is a failure of
                # the tool, not a conflict to resolve: ``tool_failure`` prevails.
                return fail(f'Il passo `sync` è fallito: {_failure_text(exc)}'
                            f'{self._log_hint(exc)}', exc.tail,
                            cls=TOOL_FAILURE if _start_failure(exc) else None)
            if result['status'] == 'blocked':
                return self._blocked(pr.number, used, result['blocked_reason'].strip()
                                     + f'\n\nFile in conflitto con `{cfg.base_branch}`:\n\n'
                                     + listed, remove=remove, on_pr=True, step='sync',
                                     head=pr.head_sha)
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
        self._bind_tested(tested, path, head)
        # The branch never differs from the base under protected_paths, and neither may the merge.
        protected = self._protected_changes(path, since=base)
        if protected:
            return self._escalate_protected(pr.number, protected, remove=remove)
        self.ws.push(path, pr.head_ref, auth=auth)
        self._pushed(path)
        then = ('La PR torna in review sul nuovo head' if fix_remove is None else
                'La correzione in attesa parte dal nuovo head al prossimo tick')
        text = (f'\n## Sincronizzazione con `{cfg.base_branch}`\n\n'
                f'Merge di `{base}` (`{target[:12]}`) nel branch, nuovo head `{head[:12]}`. '
                f'{then}; la sincronizzazione non consuma un round di review. '
                f'{self._sync_rounds_note(pr, bool(files), fix_remove is not None)}\n\n')
        if files:
            text += f'File in conflitto risolti:\n\n{listed}\n\n{result["summary"].strip()}'
            # Who resolved the conflicts wrote code on the PR: the marker keeps its profile,
            # so a later change of configuration cannot make it a reviewer of its own code.
            # ``from``: the head it synced, which a fix waiting for it recognizes (#250).
            self._as(agent.key, PR_COMMENT).comment(
                pr.number, publishable(self.runner.header(used) + text, 'sync',
                                       {'head_sha': head, 'from': pr.head_sha,
                                        'profile': self._profile(agent.key, 'write', used),
                                        **skipped_models(used)}))
        else:
            # A sync without an agent is an event, not a turn (#133).
            self._ledger_event(pr.number, 'sync', base=target, head=head, previous_head=pr.head_sha)
            self.gh.comment(pr.number, publishable(dispatcher_header() + text
                                                   + 'Il merge locale non aveva conflitti.'))
        if fix_remove is None:
            # A ready PR whose merge is the operator's (#248) loses the dispatcher's label too.
            self.gh.edit_labels(pr.number, add=[lb.review],
                                remove=[lb.ready_to_merge, lb.changes, *self._released(pr)])
            if lb.ready_to_merge in pr.labels:
                # The new head voids the receipt of the promotion (#135).
                self._supersede_receipts(pr.number, f'la sincronizzazione ha portato il head a '
                                             f'`{head[:12]}`')
        self.ws.remove(name)
        self.log(f'  merged {base} into {pr.head_ref}: {head[:12]}')
