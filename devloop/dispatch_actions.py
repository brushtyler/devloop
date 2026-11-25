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

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .acceptance import acceptance_criteria, format_criteria, format_map, latest_map, render_map
from .agents import AgentError, TurnCancelled
from .apps import DISPATCHER, IMPLEMENT, PR_COMMENT, RERUN, REVIEW
from .budget import BudgetRefused
from .dispatch_base import BaseBroken, HostTests
from .context import (_cli_state_refused, _failure_text, _format_conflicts, _format_findings,
                      _review_failure, _start_failure, _template, _trusted_comments,
                      docs_diff_section, docs_map_section, docs_review_section, format_ci_summary,
                      format_fix_requests, format_operator_comments, format_operator_decisions,
                      format_previous_rounds, owner_assessments, prompt_values)
from .generated import generated_record, render_generated
from .github import GitHubError, GitHubUncertain, LogUnreadable, job_id
from .i18n import Catalog
from .hoststep import format_step, render_step
from .hoststep import latest as latest_host_step
from .ledger import FixRegister, attempt_branch, attempt_marker, fix_register
from .localtests import RUN_TESTS_LOG, failed_modules, select_modules, suite_summary
from .markdown import (dispatcher_header, publishable, render_fix, render_native_review,
                       render_review, without_attribution)
from .config import foreign, provider_quorum, same_agent
from .planning import (BASE_CHANGED, FIX_COMMAND, RESYNC_AFTER_APPROVAL, RESYNC_CONFLICTS,
                       carried_resync, ci_joins_round, closed_by_first, counted_rounds,
                       findings_reviewers, implementer_choice, next_requested, pending_reviewers,
                       preflight_state, prerequisite_list, prerequisite_number, voided_approval,
                       waived_rounds, waiver, waiting_reviewers)
from .policy import (PARALLEL_CANCEL, SEQUENTIAL, ZERO_LEVEL, another_model, distinct,
                     independent, profile_of)
from .ports import summarize_checks
from .review import (FINDINGS_ONLY, ci_failure_review, current_round, findings_only,
                     flow_verdict, last_round, normalize_review, open_findings, owner_of,
                     round_verdict)
from .review import TEXTS as REVIEW_TEXTS
from .supervision import ESCALATE, PREREQUISITE, RETRY, SYNC_FAILED, TOOL_FAILURE
from .workspace import TransientPushError, WorkspaceError


# The texts of the actions the dispatcher composes itself (implementation, CI rerun, full suite
# without CI, fix, sync), in the project's ``language`` (#635); English for a language without
# translation.
TEXTS = Catalog(
    en={
        'same_agent': ('the implementer chosen (`{agent}`, {why}) is the same agent as the '
                       'readiness check (`{preflight}`)'),
        'no_implementer': ('The implementer of the issue cannot be determined: {problem}. A '
                           'decision on who implements it is needed.'),
        'veto_implement': ('the implementation produced with that resolution is not published: '
                           'the commit stays in the local worktree.'),
        'veto_review': 'the review given with that resolution is not published.',
        'veto_fix': 'the correction produced with that resolution is not published.',
        'rerun_title': 'CI run again',
        'rerun_text': ('The CI of `{head}` failed only in test modules not pertinent to the diff '
                       'of the PR ({modules}): the dispatcher ran the failed jobs again once '
                       '(run {runs}), without opening a round. If the new run fails too, the '
                       'failure counts as a round.'),
        'suite_promotion': ('promotion, fails: the approval, given before the CI was known, does '
                            'not bring the PR to the merge.'),
        'suite_review': 'review, fails: the review does not start.',
        'suite_failed': ('On head `{head}` there is no CI and, with `local_tests = "{mode}"`, the '
                         'local tests did not run the full suite. The full suite (`{command}`), '
                         'run locally before the {then}'),
        'no_review': 'Changes requested but no devloop review to correct.',
        'no_findings': 'Changes requested but no open finding.',
        'fix_unconfigured': ('The implementer of the PR (`{agent}`) is no longer configured: a '
                             'decision on who corrects is needed.'),
        'requested_unconfigured': ('The implementer of the PR (`{agent}`) is no longer configured: '
                                   'a decision on who runs `{command}` is needed.'),
        'granted_more': ' plus {granted} granted',
        'sync_free': ('The review of the new head **will not count** as a round: it differs from '
                      'the approved one only by the merge without conflicts of the base (it '
                      'counts instead if the CI fails or if a new review is asked with '
                      '`/devloop rereview`).'),
        'then_over': ('{counted} rounds of {limit} already counted ({what}), none left: the PR '
                      'will go to the escalation for the limit of the rounds (`round_limit`), as '
                      'for every round beyond the limit'),
        'then_last': 'it will be round {number} of {limit} ({what}), the last one left',
        'then_left': ('it will be round {number} of {limit} ({what}); after it {left} will be '
                      'left'),
        'sync_conflicts_approved': ('The review of the new head starts even with no round left '
                                    'and **will not count** as a round if it approves: it differs '
                                    'from the approved one only by the merge of the base, with '
                                    'the conflicts the agent resolved; approved, the PR is ready '
                                    'for the merge again without an escalation for the limit of '
                                    'the rounds. If instead it opens blocking findings it **will '
                                    'count**: {then} (it counts also if the CI fails or if a new '
                                    'review is asked with `/devloop rereview`).'),
        'which_fix': 'after the correction',
        'which_next': 'that follows',
        'why_fix': 'as every review after a correction',
        'why_conflicts': 'since the agent resolved conflicts and the content of the PR changed',
        'why_plain': 'as it would have counted without the synchronisation',
        'where_over': ('{counted} rounds of {limit} already counted ({what}); no round left: the '
                       'review {which} will need a decision of the operator'),
        'where_last': 'round {number} of {limit} ({what}), the last one left',
        'where_left': 'round {number} of {limit} ({what}); after it {left} will be left',
        'sync_counts': 'The review {which} **will count** as a round, {why}: {where}.',
        'sync_unconfigured': ('The PR conflicts with `{branch}` and its implementer (`{agent}`) is '
                              'no longer configured: a decision on who resolves the conflicts is '
                              'needed.'),
        'sync_failed': ('Synchronisation with `{branch}` failed. {reason}\n\n'
                        'Files in conflict:\n\n{listed}'),
        'sync_step_failed': 'The step `sync` failed: {failure}{hint}',
        'sync_blocked': '\n\nFiles in conflict with `{branch}`:\n\n',
        'sync_merge_moved': ('The merge in progress was concluded or aborted during the '
                             'resolution of the conflicts, so it is not published.'),
        'sync_markers': 'Conflict markers are left in {files}.',
        'sync_review': 'The PR goes back to review on the new head',
        'sync_fix': 'The waiting correction starts from the new head at the next tick',
        'sync_title': 'Synchronisation with `{branch}`',
        'sync_text': ('Merge of `{base}` (`{target}`) into the branch, new head `{head}`. '
                      '{then}; the synchronisation spends no review round. {note}'),
        'sync_resolved': 'Files in conflict resolved:',
        'sync_clean': 'The local merge had no conflicts.',
    },
    it={
        'same_agent': ('l\'implementer scelto (`{agent}`, {why}) è lo stesso agent del controllo '
                       'di prontezza (`{preflight}`)'),
        'no_implementer': ('L\'implementer dell\'issue non è determinabile: {problem}. Serve una '
                           'decisione su chi la implementa.'),
        'veto_implement': ('l\'implementazione prodotta con quella risoluzione non è pubblicata: '
                           'il commit resta nel worktree locale.'),
        'veto_review': 'la review data con quella risoluzione non è pubblicata.',
        'veto_fix': 'la correzione prodotta con quella risoluzione non è pubblicata.',
        'rerun_title': 'CI rilanciata',
        'rerun_text': ('La CI di `{head}` è fallita solo in moduli di test non pertinenti al diff '
                       'della PR ({modules}): il dispatcher ha rilanciato una volta i job falliti '
                       '(run {runs}), senza aprire un round. Se anche il rilancio fallisce, il '
                       'fallimento conta come round.'),
        'suite_promotion': ('promozione, fallisce: l\'approvazione, data prima che la CI fosse '
                            'nota, non porta la PR al merge.'),
        'suite_review': 'review, fallisce: la review non parte.',
        'suite_failed': ('Sul head `{head}` non risulta alcuna CI e, con `local_tests = '
                         '"{mode}"`, i test locali non hanno eseguito la suite completa. La suite '
                         'completa (`{command}`), eseguita in locale prima della {then}'),
        'no_review': 'Modifiche richieste ma nessuna review devloop da correggere.',
        'no_findings': 'Modifiche richieste ma nessun finding aperto.',
        'fix_unconfigured': ('L\'implementer della PR (`{agent}`) non è più configurato: serve una '
                             'decisione su chi corregge.'),
        'requested_unconfigured': ('L\'implementer della PR (`{agent}`) non è più configurato: '
                                   'serve una decisione su chi esegue `{command}`.'),
        'granted_more': ' più {granted} concessi',
        'sync_free': ('La review del nuovo head **non conterà** come round: differisce da quello '
                      'approvato solo per il merge senza conflitti della base (conta invece se la '
                      'CI fallisce o se viene chiesta una nuova review con `/devloop '
                      'rereview`).'),
        'then_over': ('contati già {counted} round di {limit} ({what}), nessuno rimasto: la PR '
                      'andrà all\'escalation per il limite dei round (`round_limit`), come per '
                      'ogni round oltre il limite'),
        'then_last': 'sarà il round {number} di {limit} ({what}), l\'ultimo rimasto',
        'then_left': ('sarà il round {number} di {limit} ({what}); dopo questo ne resteranno '
                      '{left}'),
        'sync_conflicts_approved': ('La review del nuovo head parte anche senza round rimasti e '
                                    '**non conterà** come round se approva: differisce da quello '
                                    'approvato solo per il merge della base, con i conflitti '
                                    'risolti dall\'agent; approvata, la PR torna pronta per il '
                                    'merge senza un\'escalation per il limite dei round. Se '
                                    'invece apre finding bloccanti **conterà**: {then} (conta '
                                    'anche se la CI fallisce o se viene chiesta una nuova review '
                                    'con `/devloop rereview`).'),
        'which_fix': 'dopo la correzione',
        'which_next': 'successiva',
        'why_fix': 'come ogni review dopo una correzione',
        'why_conflicts': ('perché l\'agent ha risolto dei conflitti e il contenuto della PR è '
                          'cambiato'),
        'why_plain': 'come avrebbe contato senza la sincronizzazione',
        'where_over': ('contati già {counted} round di {limit} ({what}); nessun round rimasto: '
                       'la review {which} richiederà una decisione dell\'operatore'),
        'where_last': 'round {number} di {limit} ({what}), l\'ultimo rimasto',
        'where_left': 'round {number} di {limit} ({what}); dopo questo ne resteranno {left}',
        'sync_counts': 'La review {which} **conterà** come round, {why}: {where}.',
        'sync_unconfigured': ('La PR è in conflitto con `{branch}` e il suo implementer '
                              '(`{agent}`) non è più configurato: serve una decisione su chi '
                              'risolve i conflitti.'),
        'sync_failed': ('Sincronizzazione con `{branch}` non riuscita. {reason}\n\n'
                        'File in conflitto:\n\n{listed}'),
        'sync_step_failed': 'Il passo `sync` è fallito: {failure}{hint}',
        'sync_blocked': '\n\nFile in conflitto con `{branch}`:\n\n',
        'sync_merge_moved': ('Il merge in corso è stato concluso o annullato durante la '
                             'risoluzione dei conflitti, quindi non viene pubblicato.'),
        'sync_markers': 'Restano marcatori di conflitto in {files}.',
        'sync_review': 'La PR torna in review sul nuovo head',
        'sync_fix': 'La correzione in attesa parte dal nuovo head al prossimo tick',
        'sync_title': 'Sincronizzazione con `{branch}`',
        'sync_text': ('Merge di `{base}` (`{target}`) nel branch, nuovo head `{head}`. {then}; '
                      'la sincronizzazione non consuma un round di review. {note}'),
        'sync_resolved': 'File in conflitto risolti:',
        'sync_clean': 'Il merge locale non aveva conflitti.',
    })

# The native review of an agent with an app is its review record (#325): a failed submission
# is tried again, after a pause doubled each time, before the step escalates.
REVIEW_ATTEMPTS = 3
REVIEW_RETRY_PAUSE = 5  # seconds before the second attempt
# The push of a step's branch that fails for a transient error of the remote (#645) is tried once
# more, after this pause in seconds.
PUSH_RETRY_PAUSE = 10


# The steps whose push a retry repeats (#645), and what each publishes after it.
PUSH_STEPS = ('implement', 'fix', 'sync')


@dataclass
class PendingPush:
    """The push of a step's branch (#645): ``step`` (``PUSH_STEPS``) of issue or PR ``number``,
    the ``key`` of the step's input (for an issue the key of its implementation, ``_issue_key``;
    for a PR ``{'head': ...}``, the head the step started from), the worktree ``name`` at
    ``path``, the ``commit`` to push to ``branch`` with the app of ``agent`` (``replace``:
    ``--force``), and ``data``, what the step publishes after the push, prepared before it
    (``_after_push``) together with what its veto check reads (``_push_vetoed``). Every field is
    JSON, so that the push survives a restart (``PendingPushes``). A push that fails twice for a
    transient error of the remote is kept until a retry of the same step and key ``armed`` it,
    or the step runs again."""
    step: str
    number: int
    key: dict
    name: str
    path: str
    commit: str
    branch: str
    agent: str
    data: dict
    replace: bool = False
    armed: bool = False

    @property
    def pr(self):
        """Whether the step is a PR's (a fix or a sync)."""
        return self.step != 'implement'

    @classmethod
    def from_entry(cls, entry):
        """The push kept as ``entry`` (``asdict``), or None when it is not valid."""
        if not isinstance(entry, dict) or set(entry) != {f.name for f in fields(cls)}:
            return None
        pending = cls(**entry)
        texts = (pending.name, pending.path, pending.commit, pending.branch, pending.agent)
        if (pending.step not in PUSH_STEPS or not isinstance(pending.number, int)
                or not isinstance(pending.key, dict) or not isinstance(pending.data, dict)
                or not all(isinstance(v, str) and v for v in texts)
                or not isinstance(pending.replace, bool) or not isinstance(pending.armed, bool)):
            return None
        return pending

    def matches(self, step, key):
        """Whether a retry of ``step`` with ``key`` is the retry of this push: an implementation
        of the same input for an issue, a fix or a sync of the same head for a PR (a sync that a
        fix started fails as the fix, #250)."""
        if not isinstance(key, dict):
            return False
        if self.pr:
            return step in ('fix', 'sync') and key.get('head') == self.key.get('head')
        return step == 'implement' and key == self.key


class PendingPushes:
    """The pushes of a step's branch kept after a transient error of the remote (#645), one per
    issue or PR, in ``state_dir`` (``FILE``), so that a restart does not lose them; without
    ``state_dir`` they last as long as the process, and so does one that cannot be written (it
    prevails over the file). A file that cannot be read counts as empty, and an entry that is not
    valid as missing. Parallel actions (#176) share it under a lock."""

    FILE = 'pending-pushes.json'

    def __init__(self, state_dir=None):
        self._path = Path(state_dir) / self.FILE if state_dir is not None else None
        self._memory = {}
        self._dropped = set()  # dropped in this process, still in a file that was not written
        self._lock = threading.Lock()

    def _load(self):
        if self._path is None:
            return {}
        try:
            data = json.loads(self._path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if k not in self._dropped}

    def _store(self, data):
        """Write ``data``; ``OSError`` when the file cannot be written."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f'.{self._path.name}.{os.getpid()}.tmp')
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        os.replace(tmp, self._path)

    def get(self, number):
        """The push kept for issue or PR ``number``, or None."""
        with self._lock:
            entry = self._memory.get(str(number)) or self._load().get(str(number))
        return PendingPush.from_entry(entry)

    def numbers(self):
        """The issues and PRs with a push kept."""
        with self._lock:
            keys = set(self._memory) | set(self._load())
        return sorted(int(k) for k in keys if k.isdigit())

    def put(self, pending):
        """Keep ``pending``, in place of the one before of its item. False when it could not be
        written in ``state_dir``: it is kept in this process only."""
        number, entry = str(pending.number), asdict(pending)
        with self._lock:
            self._dropped.discard(number)
            if self._path is None:
                self._memory[number] = entry
                return True
            try:
                data = self._load()
                data[number] = entry
                self._store(data)
            except (OSError, TypeError, ValueError):
                self._memory[number] = entry
                return False
            self._memory.pop(number, None)
            self._dropped.clear()  # the file written leaves them out
            return True

    def drop(self, number):
        """Forget the push kept for issue or PR ``number``. False when the file could not be
        written: the entry left there counts as missing in this process."""
        with self._lock:
            self._memory.pop(str(number), None)
            data = self._load()
            if data.pop(str(number), None) is None:
                return True
            try:
                self._store(data)
            except OSError:
                self._dropped.add(str(number))
                return False
            self._dropped.clear()
            return True


# What the findings-only reviewer (ADR 0005, D8, #416) reads after the review prompt.
FINDINGS_ONLY_NOTE = '''

## Your role in this round: findings only

You review this round besides the reviewers of the quorum, as a findings-only reviewer. Your
blocking findings go to the fix like theirs, with the same rules on evidence. Your verdict does
not count: your approval approves nothing, and only the reviewers of the quorum decide whether
your earlier findings are resolved. Report concrete, verifiable defects; do not repeat a finding
of yours that is still open.
'''


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

    push_retry_pause = PUSH_RETRY_PAUSE

    def _push(self, path, branch, *, replace=False, auth=None, check=None):
        """Push the HEAD of worktree ``path`` to ``branch`` (``Workspace.push``) and record it
        (``_pushed``); True when pushed. A push that fails for a transient error of the remote
        (#645, ``TransientPushError``: a 5xx answer, a connection lost) is tried once more after
        ``push_retry_pause`` seconds, and the log records both attempts; a second transient error
        propagates, as does any other failure at once. ``check``, run again before the second
        attempt: when true it stopped the step (a veto of the conclave written during the pause,
        which it handled), and nothing is pushed (False)."""
        try:
            self.ws.push(path, branch, replace=replace, auth=auth)
        except TransientPushError as exc:
            self.log(f'  push of {branch} failed with a transient error of the remote ({exc}); '
                     f'second attempt in {self.push_retry_pause}s')
            time.sleep(self.push_retry_pause)
            if check is not None and check():
                self.log(f'  second attempt of the push of {branch} not made: the step stopped')
                return False
            try:
                self.ws.push(path, branch, replace=replace, auth=auth)
            except TransientPushError as again:
                self.log(f'  second attempt of the push of {branch} failed too: {again}')
                raise
            self.log(f'  push of {branch} done at the second attempt')
        self._pushed(path)
        return True

    def _push_step(self, pending, auth=None):
        """Push the commit of ``pending`` (``PendingPush``, #645), then publish what its step
        prepared for after the push (``_after_push``). Its veto check (``_push_vetoed``) runs
        again before a second attempt. When the push fails twice for a transient error of the
        remote, the dispatcher keeps ``pending`` (``pending_pushes``, in ``state_dir``) for a
        retry of the step (``_arm_pending_push``) and the error propagates: escalated as
        ``push_failed``, with the key of an implementation (``exc.key``) so that the escalation
        records the step to retry. ``auth``: the credentials of the push; None takes them again
        from the app of ``pending.agent``, since a retry may come after the token of the turn
        expired, or after a restart."""
        self.pending_pushes.drop(pending.number)
        if auth is None:
            auth = self._git_auth(pending.agent, IMPLEMENT)
        try:
            pushed = self._push(Path(pending.path), pending.branch, replace=pending.replace,
                                auth=auth, check=lambda: self._push_vetoed(pending))
        except TransientPushError as exc:
            pending.armed = False
            if not self.pending_pushes.put(pending):
                self.log(f'  the push of {pending.commit[:12]} kept is not written in state_dir: '
                         'a restart loses it, and a retry then runs the step again')
            if not pending.pr:
                exc.key = pending.key
            exc.kept_push = pending.commit
            raise
        if not pushed:
            return None
        return self._after_push(pending)

    def _push_vetoed(self, pending, remove=()):
        """Whether a veto of the conclave decision whose resolution the turn of ``pending`` had
        (#142) was written since: then nothing it produced is published, and the step is
        suspended (``_veto_suspend``), removing also the labels ``remove``. Checked before the
        push, before its second attempt, before the push a retry repeats and before a retry
        completes a push that already reached GitHub (#645). A sync has no such decision."""
        data, lb = pending.data, self.cfg.labels
        decision = data.get('conclave')
        if pending.step == 'sync' or decision is None:
            return False
        if pending.pr:
            veto = self._veto_after(data['issue'], pending.number, decision)
            labels, what = list(data['remove']), 'veto_fix'
        else:
            veto = self._veto_after(pending.number, None, decision)
            labels, what = [lb.working], 'veto_implement'
        if veto:
            labels += [label for label in remove if label not in labels]
            self._veto_suspend(pending.number, decision, veto, labels,
                               TEXTS.texts(self.cfg.language)(what))
        return bool(veto)

    def _after_push(self, pending):
        """Publish what the step of ``pending`` prepared for after its push (#645): the PR of an
        implementation, the record of a fix or of a sync."""
        if pending.step == 'implement':
            return self._publish_pr(pending.number, pending.name, pending.branch, pending.agent,
                                    pending.data['pr'])
        if pending.step == 'fix':
            return self._publish_fix_record(pending.number, pending.name, pending.agent,
                                            pending.data['record'])
        return self._publish_sync_record(pending.number, pending.name, pending.agent,
                                         pending.data['record'])

    def _arm_pending_push(self, number, step, key):
        """On a retry of ``step`` with ``key`` applied on issue or PR ``number``, arm the push
        kept for it after a transient error of the remote (#645), and return it (None when there
        is none): the step, when it runs, pushes those commits again instead of starting a new
        turn. The push is kept in ``state_dir``, so a restart in between does not lose it."""
        pending = self.pending_pushes.get(number)
        if pending is None or not pending.matches(step, key):
            return None
        pending.armed = True
        if not self.pending_pushes.put(pending):
            self.log('  the retry of the push kept is not written in state_dir')
        self.log(f'  the retry pushes {pending.commit[:12]} again, without a new turn')
        return pending

    def _landed_push(self, number, head):
        """The push kept for PR ``number`` (#645) when the head of its branch on GitHub, ``head``,
        is its commit: the connection lost on both attempts lost the answer, not the push. None
        otherwise."""
        pending = self.pending_pushes.get(number)
        if pending is not None and pending.pr and head == pending.commit:
            return pending
        return None

    def _complete_landed(self, pending, remove=()):
        """Complete the step of ``pending``, armed by a retry, whose push already reached GitHub
        (``_landed_push``, #645): what it prepared for after the push is published, with no new
        push and no new turn. A veto of the resolution its turn had, written since, publishes
        nothing and suspends the step (``_push_vetoed``), removing also the labels ``remove``:
        those the retry gave back to the PR."""
        self.pending_pushes.drop(pending.number)
        if self._push_vetoed(pending, remove):
            return None
        self.log(f'  the push of {pending.commit[:12]} had already reached {pending.branch}: '
                 'the step is recorded, without a new push nor a new turn')
        return self._after_push(pending)

    def _take_pending_push(self, number, head=None):
        """The push kept for issue or PR ``number`` that a retry armed (#645), taken, or None. A
        step that runs again replaces any other kept push of the item: one not armed, or kept
        for another head of a PR (``head``, the head the step starts from, which may be the
        commit kept when its push reached GitHub already), is dropped."""
        pending = self.pending_pushes.get(number)
        if pending is None:
            return None
        self.pending_pushes.drop(number)
        if pending.armed and (head is None or head in (pending.key.get('head'), pending.commit)):
            return pending
        self.log(f'  the push of {pending.commit[:12]} kept after a transient error is dropped: '
                 'the step runs again')
        return None

    def _resume_push(self, pending, head=None):
        """Push again the commit of ``pending``, armed by a retry (#645), without a new turn: in
        its worktree, recreated at that commit when it is gone or moved (a restart, a cleanup),
        after its veto check. A PR whose ``head`` is that commit already got it: the step is only
        recorded (``_complete_landed``)."""
        if pending.pr and head == pending.commit:
            return self._complete_landed(pending)
        try:
            moved = self.ws.head(Path(pending.path)) != pending.commit
        except WorkspaceError:  # the worktree is gone
            moved = True
        if moved:
            pending.path = str(self.ws.commit_copy(pending.name, pending.commit))
        if self._push_vetoed(pending):
            return None
        self.log(f'  retry of the push: {pending.commit[:12]} to {pending.branch}, the commits '
                 'of the turn already done')
        return self._push_step(pending)

    def _do_implement(self, number):
        cfg, lb = self.cfg, self.cfg.labels
        # The supervisor's agent:ready admits only work the coordinator authorized for its
        # current text (#607), at every start: a text changed after the snapshot voids it, and
        # so does one changed while the implementation waited for the base branch (#459).
        unauthorized = self._unauthorized_ready(number)
        if unauthorized:
            return self._stop_unauthorized(number, unauthorized)
        if cfg.preflight and preflight_state(self._preflight_view(number, ()))[0] != 'ready':
            # The text changed, or a new check was asked, after the snapshot.
            self.log('  the readiness check does not cover the current text; next tick')
            return
        issue, comments = self._issue_context(number)
        pending = self._take_pending_push(number)
        if pending is not None:
            # A retry of a push that failed for a transient error of the remote (#645): the
            # commit of the turn already done, pushed again, without a new turn.
            self.gh.edit_labels(number, add=[lb.working], remove=[lb.ready])
            return self._resume_push(pending)
        # The implementer is chosen at the takeover (ADR 0005, D7, #407) and recorded in the
        # request and in the PR marker: a label changed later does not change it.
        key, why, problem = implementer_choice(cfg, issue.get('labels') or [], issue['labeled'],
                                               issue['admitters'])
        if not problem and cfg.preflight and same_agent(cfg.agent(cfg.preflight),
                                                        cfg.agent(key)):
            problem = TEXTS.texts(cfg.language)('same_agent', agent=key, why=why,
                                                preflight=cfg.preflight)
        if problem:
            return self._escalate(number, TEXTS.texts(cfg.language)('no_implementer',
                                                                    problem=problem),
                                  remove=[lb.ready], cls='configuration')
        agent = cfg.agent(key)
        if why != 'default':
            self.log(f'  implementer {key} ({why})')
        if self.ledger is not None:
            # The request as the implementer gets it, at the takeover (#133): not necessarily
            # the text at the time of the label.
            self.ledger.request(number, title=issue['title'], body=issue.get('body') or '',
                                implementer=key,
                                labeled=ready_approval(issue['labeled'], lb.ready,
                                                       issue['ready_admitters']),
                                comments=_trusted_comments(issue.get('comments') or [],
                                                           cfg.trusted_users)
                                + issue['accepted'],
                                story_repo=self._story_root(number, issue['thread']))
        # The attempt (#384), before the first turn: a second active one is refused.
        attempt = self._take_attempt(number)
        self.gh.edit_labels(number, add=[lb.working], remove=[lb.ready])
        # One branch per attempt (#384, ADR 0004 §3.2 (i)): the first keeps that of today, a
        # later one has its own, and never overwrites the branch of the previous attempt.
        name = f'issue-{number}'
        branch = attempt_branch(cfg.branch_prefix, number, attempt['n'] if attempt else 1)
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
            return _template('implement', **prompt_values(cfg), repo=cfg.repo, base_branch=cfg.base_branch,
                             issue_number=number, issue_title=issue['title'],
                             issue_body=issue.get('body') or '', issue_comments=comments,
                             acceptance_criteria=format_criteria(criteria),
                             test_cmd=cfg.test_cmd, language=cfg.language, docs=docs,
                             conclave=resolution)
        try:
            result, failure, used, tested, local = self._write_turn(
                agent, prompt, 'implement', path, f'issue-{number}-implement',
                where=(number, 'implement'), base_check=True)
        except (AgentError, BudgetRefused) as exc:
            # A turn the budget refused (#411) keeps its input too: the escalation that the
            # supervisor may answer with an extension is bound to it, as a retry is.
            exc.key = key
            raise
        if result['status'] == 'blocked':
            reason = result['blocked_reason']
            if (result.get('prerequisite') or '').strip():
                # A missing prerequisite (#672): a "blocked by", or an escalation the supervisor
                # decides, in place of the block to the operator; failing that, the block.
                handled, why = self._implement_prerequisite(number, result, key, name)
                if handled:
                    return None
                if why:
                    reason = reason.rstrip() + '\n\n' + self._texts()('prereq_stopped', why=why)
            return self._blocked(number, used, reason, remove=[lb.working],
                                 on_pr=False, step='implement', head=start, conclave=decision)
        if isinstance(failure, HostTests):
            # The host's failure (#497): to the operator, no wait for the base branch.
            return self._escalate_host(number, 'implement', failure, remove=[lb.working])
        if isinstance(failure, BaseBroken):
            # The base's failure, not the implementer's (#459): the issue keeps agent:working
            # and starts again once the base branch advances.
            self._wait_base(number, 'implement', failure, head=start)
            self.ws.remove(name)
            return
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
        generated = self._generated(path, start, tested)
        # The PR is prepared before the push, and kept with the commit when the push fails
        # twice for a transient error of the remote: a retry pushes it again and opens the PR,
        # without a new turn, after a restart too (#645).
        pending = PendingPush(
            'implement', number, key, name, str(path), commit, branch, agent.key,
            {'conclave': decision,
             'pr': self._pr_request(number, agent, used, result, local, decision, attempt,
                                    generated)},
            replace=True)
        # Nothing produced with a vetoed resolution is published (#142): checked again before
        # a second attempt of the push, and before the push a retry repeats (#645).
        if self._push_vetoed(pending):
            return
        self._push_step(pending, auth=auth)

    def _implement_prerequisite(self, number, result, key, name):
        """An implementer that stopped as ``blocked`` for a missing ``prerequisite`` (#672):
        ``(handled, why)``. With the number of its issue (``blocked_by``) the dispatcher adds it
        as "blocked by" (``_add_prerequisites``): the issue keeps ``agent:working``, its
        worktree ``name`` is removed, and the implementation starts again once the block is
        closed (``_prerequisite_wait``). Without a number, a ``prerequisite`` escalation the
        supervisor decides in merge mode "auto" (``_supervised``, bound to the input ``key`` of
        the turn), with the agent's reason. ``handled`` False: the agent's block to the
        operator stays, and ``why`` says why no "blocked by" was added ('' when none was
        asked)."""
        lb = self.cfg.labels
        wanted = prerequisite_number(result)
        if wanted:
            added, why = self._add_prerequisites(number, [wanted], 'implement')
            if not added:
                self.log(f'  the prerequisite is not added as "blocked by": {why}')
                return False, why
            self.ws.remove(name)
            return True, ''
        supervised, why = self._supervised(number, PREREQUISITE, 'implement', key=key)
        if not supervised:
            if why:
                self.log(f'  the prerequisite stays with the operator: {why}')
            return False, ''
        t = self._texts()
        self._escalate(number, t('prerequisite_implement', listed=prerequisite_list([result]))
                       + ' ' + t('prerequisite_supervisor', retry=RETRY, escalate=ESCALATE),
                       result.get('blocked_reason') or '', remove=[lb.working],
                       cls=PREREQUISITE, supervised=supervised)
        return True, ''

    def _pr_request(self, number, agent, used, result, local, decision, attempt, generated):
        """The PR of the implementation of issue ``number``, with the marker and the body of its
        turn: ``{title, body, attempt}`` (``attempt``: the id of the attempt to bind the PR to,
        or None), all JSON, for ``_publish_pr`` after the push."""
        cfg = self.cfg
        # The marker keeps the map the reviewer checks until a fix replaces it (#134). Its
        # reviewer is the first of the pool that is not the implementer's agent (#407); with the
        # reviewer rules of P5-5 (#409), the first of another provider and another tuple.
        profile = self._profile(agent.key, 'write', used)
        if cfg.review_policy.provider_quorum:
            authors = [profile] + [self._profile(agent.key, 'write', m) for m in agent.chain()]
            reviewer = next((k for k in cfg.pool() if not cfg.signs_for_role(k)
                             and foreign(self._profile(k, 'review'), authors)
                             and all(independent(self._profile(k, 'review'), a)
                                     for a in authors)), cfg.reviewer)
        else:
            reviewer = next((k for k in cfg.pool() if not cfg.signs_for_role(k)
                             and not same_agent(cfg.agent(k), agent)), cfg.reviewer)
        meta = {'issue': number, 'implementer': agent.key, 'reviewer': reviewer,
                'profile': profile,
                'acceptance_map': result['acceptance_map'],
                # Informative only (#169): no receipt nor approval depends on it.
                'local_tests': local, **skipped_models(used)}
        if decision is not None:
            meta['conclave'] = decision  # the resolution its prompt had (#142)
        if attempt is not None:
            meta['attempt'] = attempt_marker(attempt)  # the PR of this attempt (#384)
        # The step on the host the PR declares (#462): its merge holds the auto-pin.
        step = (result.get('host_step') or '').strip()
        if step:
            meta['host_step'] = step
        if generated:
            # Pointed out, not left out (#627): a tracked file may be wanted. Informative only.
            meta.update(generated_record(generated))
        language = cfg.language
        body = publishable(self.runner.header(used) + '\n' + result['pr_body'].strip()
                           + (('\n\n' + render_step(step, language)) if step else '')
                           + (('\n\n' + render_generated(generated, language))
                              if generated else '')
                           + '\n\n' + render_map(result['acceptance_map'], language)
                           + f'\n\nCloses #{number}', 'pr', meta)
        return {'title': result['pr_title'], 'body': body,
                'attempt': attempt['attempt'] if attempt is not None else None}

    def _publish_pr(self, number, name, branch, agent_key, request):
        """Open the PR ``request`` (``_pr_request``) of the implementation of issue ``number``
        whose branch ``branch`` was just pushed from worktree ``name`` by agent ``agent_key``."""
        cfg, lb = self.cfg, self.cfg.labels
        pr_number, url = self._as(agent_key, IMPLEMENT).create_pr(
            base=cfg.base_branch, head=branch, title=request['title'], body=request['body'])
        attempt = request.get('attempt')
        if attempt is not None:
            try:
                self.ledger.journal.bind_pr(attempt, pr_number)
            except GitHubError as exc:
                # The PR is open: the first turn on it records it (``_turn_attempt``), and so
                # does a new implementation of the issue before resuming the attempt
                # (``_take_attempt``), whatever the state of the PR by then.
                self.log(f'  PR #{pr_number} of attempt {attempt} not recorded: {exc}')
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
        log_tail = '\n'.join(self._failed_log(run) for run in pr.failed_runs[:2])
        # One head, one round (#352, #359): a failure on the head of a round that asked for
        # changes, or approved, is a ci finding of that round, not a round of its own.
        joined = ci_joins_round(pr)
        voided = bool(joined) and voided_approval(joined)
        round_no = joined[0]['round'] if joined else last_round(pr.reviews) + 1
        earlier = [r for r in pr.reviews if r['round'] < round_no]
        review = ci_failure_review(failed_checks=pr.failed_checks, log_tail=log_tail,
                                   previous_open=open_findings(earlier, pr.closures),
                                   round_no=round_no,
                                   max_rounds=self._last_round_allowed(pr.reviews,
                                                                       pr.granted_rounds,
                                                                       pr.fixes, pr.closures),
                                   head_sha=pr.head_sha, language=self.cfg.language)
        # The notes in the project's language (#632).
        t = REVIEW_TEXTS.texts(self.cfg.language)
        notes = []
        if voided:
            notes.append(t('ci_voided', round=round_no))
        elif joined:
            notes.append(t('ci_joined', round=round_no))
        rerun = rerun_of(pr.reruns, pr.head_sha)
        if rerun is not None:
            notes.append(t('ci_rerun', runs=', '.join(str(r) for r in rerun['runs']),
                           modules=', '.join(rerun['modules']) or t('ci_no_modules'),
                           outcome=t('ci_rerun_joined' if joined else 'ci_rerun_round')))
        review['note'] = ' '.join(notes)
        review['receipt'] = receipt
        if self.ledger is not None:
            # The event of a joined round is a section of that round's register (#325).
            details = self.gh.check_details(pr.head_sha)
            # Its approval voided by the failure leaves a trace in the event (#359).
            self._record_ci(pr, pr.head_sha, summarize_checks(details)[0], details,
                            review['round'], jobs=self._ci_jobs(details),
                            voided_approval=voided)
        self.gh.comment(pr.number, render_review(review, dispatcher_header(),
                                                 language=self.cfg.language))
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

    def _failed_log(self, run):
        """The tail of the failed log of run ``run`` for the CI finding, empty, with the reason
        in the run.log, when `gh` cannot read it (#545)."""
        try:
            return self.gh.failed_log(run)
        except LogUnreadable as exc:
            self.log(f'  the failed log of run {run} cannot be read: {exc}')
            return ''

    def _rerun_unrelated(self, pr, paths):
        """Re-run once the failed jobs of the head's CI when its failure does not concern the
        PR (#200); whether it did. Only at the first failure of a head (``rerun`` marker), with
        ``ci_rerun_unrelated``, when every failed check is a GitHub Actions job whose own log
        names the failed test modules (``failed_modules``) and none of them is pertinent to
        ``paths``,
        the PR's diff, as ``local_tests = "changed"`` would select them (``select_modules``).
        A re-run that GitHub refuses, such as a 403 without ``actions: write``, is logged and
        the failure is a round, as before. It needs the format of the CI logs and the selection
        of the project (``[tests]``, ADR 0006, R5), without which the configuration turns
        ``ci_rerun_unrelated`` off."""
        tests = self.cfg.tests
        if (not self.cfg.ci_rerun_unrelated or tests.ci_log_format != RUN_TESTS_LOG
                or not tests.selection or rerun_of(pr.reruns, pr.head_sha) is not None):
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
            try:
                found = None if job is None else failed_modules(
                    self.gh.failed_log(check['run_id'], max_lines=None, job_id=job))
            except LogUnreadable as exc:
                # Why `gh` could not read it stays in the run.log (#545).
                self.log(f'  CI rerun: the log of the failed job {check.get("name")} (run '
                         f'{check["run_id"]}) cannot be read: {exc}; CI round')
                return False
            if found is None:
                self.log(f'  CI rerun: the log of the failed job {check.get("name")} (run '
                         f'{check["run_id"]}) does not tell which test modules failed; CI round')
                return False
            modules.update(found)
        modules = sorted(modules)
        pertinent = select_modules(paths, modules, self.cfg.tests)
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
        t = TEXTS.texts(self.cfg.language)
        self.gh.comment(pr.number, publishable(
            dispatcher_header() + f'\n## {t("rerun_title")}\n\n'
            + t('rerun_text', head=pr.head_sha[:12], modules=', '.join(f'`{m}`' for m in modules),
                runs=', '.join(str(r) for r in runs)), 'rerun', record))
        self._ledger_event(pr.number, 'ci_rerun', head=pr.head_sha, runs=runs, modules=modules)
        self.log(f'  CI rerun of {", ".join(str(r) for r in runs)}: failed modules not '
                 f'pertinent to the PR ({", ".join(modules)})')
        return True

    def _ci_jobs(self, details):
        """The checks ``details`` (``check_details``) as the summary of the CI shows them
        (#300): ``{name, outcome, bucket, duration_s}``, and for a completed GitHub Actions job
        ``tests``, the final lines of ``scripts/run-tests.py`` its log holds
        (``suite_summary``, None when it holds none), or ``log: unavailable`` when the log
        could not be read: that only leaves the summary out, it never stops the action. Without
        the format of the CI logs (``[tests] ci_log_format``, ADR 0006, R5) no log is read: each
        job has its outcome and duration only."""
        jobs = []
        readable = self.cfg.tests.ci_log_format == RUN_TESTS_LOG
        for check in details:
            job = {'name': check.get('name'), 'outcome': check.get('conclusion')
                   or check.get('bucket'), 'bucket': check.get('bucket'),
                   'duration_s': check.get('duration_s')}
            number = job_id(check.get('link')) if check.get('run_id') is not None else None
            if readable and number is not None and check.get('bucket') != 'pending':
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
        conflict = self.cfg.base_branch if pr.merge_state == 'conflict' else None
        return format_ci_summary(pr.head_sha, summarize_checks(details)[0], jobs,
                                 conflict_base=conflict), jobs

    def _last_round_allowed(self, reviews, granted=0, fixes=(), closures=()):
        """The number of the last round ``max_review_rounds`` allows, given the waived rounds
        among ``reviews`` (with the PR's ``fixes`` and finding ``closures``, #536) and the
        rounds the supervisor ``granted`` (#405): what records and prompts show as the limit."""
        return (self.cfg.max_review_rounds + granted
                + len(waived_rounds(reviews, **waiver(self.cfg, fixes, closures))))

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

        With ``review_policy.second_reviewer`` other than ``parallel`` (#521), as the receipt of a
        round with two reviewers records it: ``sequential`` runs the first reviewer of the
        receipt alone, and the others only once it approved; ``parallel_cancel`` runs them
        together, and a verdict of the first that does not approve stops the turns of the others
        (``TurnCancelled``: closed ``cancelled``, never a failure). Either way a round whose first
        reviewer does not approve is closed with its verdict (``closed_by_first``), and the
        failure of a reviewer it no longer waits for does not escalate.

        When only the base changed since the round's reviews (#160), the round continues if no
        commit of the base branch in between reached the PR's files, and the review records the
        check (``bases``). Otherwise the round restarts, and the new one records which round it
        replaces (``restarts``): that round does not count against ``max_review_rounds``.

        A new round on a head that a clean sync moved from the approved one (#235,
        ``PullRequest.resync``), or on the approved head after the base touching the PR revoked
        its approval (#252), records ``resync`` and does not count either. A round replacing one
        of these only because the base advanced records ``resync`` again (#458,
        ``carried_resync``), so that it does not count under the same condition."""
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
        restarts = resync = requested = None
        waive = waiver(cfg, pr.fixes, pr.closures)
        if joins:
            round_no, done, comments = latest[0]['round'], latest, pr.round_comments
            counted = counted_rounds(pr.reviews, **waive)
        else:
            round_no, done, comments = last_round(pr.reviews) + 1, [], pr.operator_comments
            counted = counted_rounds(pr.reviews, **waive) + 1
            # A new round chooses its reviewers again (#409): one whose chain is remembered
            # unavailable gives its place to the next valid one of the pool.
            receipt = self._fresh_receipt(pr, receipt)
            if bases:
                # Abandoned only because the base advanced: the round does not count.
                restarts = {'round': latest[0]['round'], 'bases': bases}
                if latest[0]['round'] not in waived_rounds(pr.reviews, **waive):
                    counted -= 1  # it counted until now; replaced, it does not
                self.log(f'  round {latest[0]["round"]} restarts: the base touched the PR')
                # The round it replaces reviewed a resync or a revocation for the base (#458):
                # this one reviews the same change, records the same resync and does not count
                # under the same condition (only if it approves, after a sync with conflicts).
                resync = carried_resync(pr.reviews, latest)
                if resync:
                    counted -= 1
                    self.log(f'  it keeps the resync after round {resync["round"]}: this round '
                             'does not count'
                             + (' if it approves' if resync['reason'] == RESYNC_CONFLICTS
                                else ''))
            elif pr.resync and not pr.rereview:
                # Only a clean sync moved the approved head (#235), or only the base touching
                # the PR revoked its approval (#252): this round does not count. After a sync
                # with conflicts the implementer resolved (#458) it starts also beyond the
                # limit, and counts only if it does not approve.
                resync = pr.resync
                counted -= 1
                why = ('approval was revoked because the base touched the PR'
                       if resync['reason'] == BASE_CHANGED
                       else 'head was synced with the base, with conflicts resolved'
                       if resync['reason'] == RESYNC_CONFLICTS
                       else 'head was synced with the base')
                self.log(f'  the {why} after round {resync["round"]}: this round does not count'
                         + (' if it approves' if resync['reason'] == RESYNC_CONFLICTS else ''))
            if not resync:
                # The review of a correction a trusted user asked with /devloop fix on a head
                # the quorum approved (#536), within max_requested_fix_rounds: this round does
                # not count, as its records will say (``requested_rounds``). One that replaces
                # such a round because the base advanced does not count either.
                requested = next_requested(
                    pr.reviews, **waive, replacing=latest[0]['round'] if bases else None)
                if requested:
                    counted -= 1
                    self.log('  a correction asked with /devloop fix by '
                             + ', '.join(requested) + ' on an approved head: this round does '
                             'not count')
        # The limit of this PR: max_review_rounds plus the rounds the supervisor granted (#405).
        limit = cfg.max_review_rounds + pr.granted_rounds
        if counted > limit:
            return self._do_round_limit(pr)
        max_rounds = limit + round_no - counted
        # The findings-only reviewer (ADR 0005, D8, #416): the one the round recorded when it
        # joins one, else the one a new round chooses now. It is never a required reviewer: the
        # receipt names it apart (``findings``), outside the fields an approval must match.
        findings = (findings_reviewers(latest) if joins
                    else self._findings_reviewers(pr, receipt))
        findings = [key for key in findings if key in cfg.agents and not cfg.signs_for_role(key)]
        if findings:
            receipt = dict(receipt, findings=findings)
        reviewed = {owner_of(r) for r in done}
        waited = [key for key in receipt['reviewers'] + findings if key not in reviewed]
        # The mode of the second reviewer of a round with two (#521), as its receipt records
        # it: none in ``parallel``, the default.
        mode = receipt.get('second_reviewer')
        first = receipt['reviewers'][0] if mode else None
        if mode == SEQUENTIAL and first in waited:
            # Only the first of the pair: the others start on the same head once it approved.
            keys = [first]
        else:
            # One reviewer per action, or with parallel actions (#176) every one the round
            # waits for, each in a thread of its own.
            keys = waited if cfg.max_parallel_actions > 1 else waited[:1]
        # With ``parallel_cancel`` (#521) the first reviewer's verdict that does not approve
        # stops the turns of the others running alongside it.
        stop = (threading.Event() if mode == PARALLEL_CANCEL and first in keys and len(keys) > 1
                else None)
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
        prompt = _template('review', **prompt_values(cfg), repo=cfg.repo, base_branch=cfg.base_branch,
                           pr_number=pr.number, round=round_no, max_rounds=max_rounds,
                           ci=ci, ci_summary=ci_summary, test_cmd=cfg.test_cmd,
                           issue_number=pr.issue, issue_title=issue['title'],
                           issue_body=without_attribution(issue.get('body')),
                           operator_decisions=format_operator_decisions(issue['decisions']),
                           pr_body=without_attribution(pr.body),
                           acceptance_criteria=format_criteria(criteria),
                           acceptance_map=format_map(acceptance_map),
                           host_step=format_step(latest_host_step(pr.meta, pr.fixes)),
                           docs=docs_review_section(self._mapped_docs(path)),
                           diff_stat=self.ws.diff_stat(path),
                           previous=format_previous_rounds(earlier, pr.fixes, pr.closures),
                           operator_comments=format_operator_comments(comments),
                           language=cfg.language, conclave=resolution)
        level2 = receipt['level'] > 1
        by_provider = cfg.review_policy.provider_quorum
        authors = (self._authors(pr) or []) if level2 or by_provider else []
        # The findings-only reviewer's profile never counts among the quorum's (#416).
        peers = [profile_of(r) for r in done if profile_of(r) and not findings_only(r)]
        # The profile each reviewer of this action runs with, reserved before any turn starts
        # and replaced atomically when a fallback model takes over (#176); a reviewer that
        # fails gives its reservation up.
        reserved = {key: self._profile(key, 'review') for key in keys}
        reserving = threading.Lock()

        # Every model a review turn actually runs, fallbacks included, is never the same agent
        # in effect as an author (ADR 0005, P5-3, #407), at both levels: at level 1 as the
        # selection reads the implementer (``_implementer_agents``), at level 2 every author.
        same = authors if level2 else self._implementer_agents(pr)

        def allowed_by_provider(key):
            # With the reviewer rules of P5-5 (#409) a model keeps what the selection checked:
            # independent by tuple of every author, distinct from the round's other reviewers,
            # and the quorum rule (ii) of D8 on the round with this model in it, the reviewers
            # not yet run counted with the model they would run now. A fallback model is also
            # never of an author's provider, whatever the quorum (D8, "Fallback dello stesso
            # fornitore"): only the reviewer's primary model is subject to the quorum alone.
            def allowed(candidate):
                p = self._profile(key, 'review', candidate)
                if candidate.fallback_for and not foreign(p, authors):
                    return False
                with reserving:
                    others = [q for k, q in reserved.items() if k != key and k not in findings]
                    waiting = [self._profile(k, 'review') for k in receipt['reviewers']
                               if k not in reviewed and k not in reserved and k != key]
                    if not (all(independent(p, a) for a in authors)
                            and all(distinct(p, q) for q in peers + others)
                            and provider_quorum(peers + others + waiting + [p], authors)):
                        return False
                    reserved[key] = p
                    return True
            return allowed

        # The findings-only reviewer (#416): every model it runs, fallbacks included, is
        # independent of every author and of another model than theirs, whatever the effort
        # (``another_model``); the rules of the quorum do not apply to it, since it is not
        # part of the quorum.
        everyone = self._authors(pr) if findings else []

        def allowed_findings(key):
            def allowed(candidate):
                p = self._profile(key, 'review', candidate)
                return everyone is not None and all(another_model(p, a) for a in everyone)
            return allowed

        def allowed_for(key):
            if key in findings:
                return allowed_findings(key)
            if by_provider:
                return allowed_by_provider(key)
            if not level2:
                return lambda candidate: not any(same_agent(candidate, a) for a in same)

            def allowed(candidate):
                # A fallback model must keep the independence the pair was selected for: of
                # who wrote the code, of the round's other reviewers as their records show
                # them, and of the reviewers running alongside it in this action.
                if any(same_agent(candidate, a) for a in same):
                    return False
                p = self._profile(key, 'review', candidate)
                with reserving:
                    others = [q for k, q in reserved.items() if k != key and k not in findings]
                    if not (all(independent(p, a) for a in authors)
                            and all(distinct(p, q) for q in peers + others)):
                        return False
                    reserved[key] = p
                    return True
            return allowed

        # The ban of ADR 0005, P5-6 (#410), with ``review_policy.fallback_provider_ban``: at both
        # levels and whatever the quorum rule, a reviewer's fallback model is never of the
        # provider of an author of the PR, the fallbacks the implementer, the fixers and the
        # syncs actually ran included, as their markers record them. When a marker does not
        # record who wrote the code no fallback is admitted. Only the primary model is left to
        # the other rules; with no model admitted the turn fails as ``tool_failure``.
        banned = self._authors(pr) if cfg.review_policy.fallback_provider_ban else []

        def admitted(key):
            rules = allowed_for(key)
            if not cfg.review_policy.fallback_provider_ban:
                return rules

            def allowed(candidate):
                if candidate.fallback_for and (
                        banned is None
                        or not foreign(self._profile(key, 'review', candidate), banned)):
                    return False
                return rules(candidate)
            return allowed

        def normalized(key, raw):
            return normalize_review(raw, previous_open=previous_open, round_no=round_no,
                                    max_rounds=max_rounds, head_sha=pr.head_sha,
                                    ci=ci, reviewer=key, criteria=criteria,
                                    acceptance_map=acceptance_map,
                                    role=FINDINGS_ONLY if key in findings else None,
                                    language=cfg.language)

        def approves(key, raw):
            # Whether the verdict of the first reviewer leaves the round open (#521), as the
            # round reads it once recorded (``closed_by_first``); an output that cannot be
            # read cancels nothing.
            try:
                return flow_verdict(normalized(key, raw)) == 'approve'
            except Exception:  # read again, and reported, with the round's records
                return True

        def review_turn(key):
            # The reviewer works in a disposable copy, where its tests may write; the PR
            # worktree must come out of the review untouched, or the verdict is discarded.
            copy_name, copy = (self.ws.review_copy(path, key) if len(keys) > 1
                               else self.ws.review_copy(path))
            # The findings-only reviewer (#416) reads its role, and its turns are recorded as
            # findings-only in the round's register.
            extra = {'role': FINDINGS_ONLY} if key in findings else {}
            try:
                # Every turn of the round goes to the round's register (#325).
                with (self._cancellable(stop) if stop is not None and key != first
                      else nullcontext()):
                    result = self._turn(Models(cfg.agent(key)), 'review',
                                        prompt + FINDINGS_ONLY_NOTE if extra else prompt,
                                        'review', copy, f'pr-{pr.number}-review',
                                        admitted(key), where=(pr.number, 'review', round_no),
                                        **extra)
                if stop is not None and key == first and not approves(key, result[0]):
                    self.log(f'  {key} did not approve: the other reviewers of the round are '
                             'cancelled (second_reviewer = "parallel_cancel")')
                    stop.set()
                return result
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
        # A turn cancelled because the first reviewer did not approve (#521) is no failure: the
        # round it belonged to is closed, and nothing escalates.
        for key, _, exc in turns:
            if isinstance(exc, TurnCancelled):
                self.log(f'  review by {key} cancelled: the first reviewer did not approve')
        failures = [(key, exc) for key, _, exc in turns
                    if exc is not None and not isinstance(exc, TurnCancelled)]
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
                                      TEXTS.texts(self.cfg.language)('veto_review'))
        records = list(done)
        for key, (raw, used), _ in (turn for turn in turns if turn[2] is None):
            review = normalized(key, raw)
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
                                    render_native_review(review, self.runner.header(used),
                                                         language=self.cfg.language))
            else:
                # An agent without an app publishes with the operator's login: the comment
                # stays its record, as before.
                reviewer_gh.comment(pr.number, render_review(review, self.runner.header(used),
                                                             language=self.cfg.language))
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
        if failures and closed_by_first(records):
            # The first reviewer closed the round without approving (#521): it waits for no
            # other reviewer, and the failure of one it no longer waits for escalates nothing.
            self.log('  the round is closed by its first reviewer: the failures above do not '
                     'escalate')
            return
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
        published stay recorded.

        With the outbox of the publications (#385) an uncertain outcome (``GitHubUncertain``)
        ends the retries at once: the outbox already reconciled it, and only it may decide
        whether the review is sent again. A certain failure is still tried again: nothing was
        written."""
        outbox = getattr(self.identities, 'outbox', None) is not None
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
                if outbox and isinstance(exc, GitHubUncertain):
                    # Left pending in the outbox, which settles it before any later write
                    # on this PR: a new submission here could duplicate it.
                    raise GitHubError(f'the review of {key} was not published: the outcome of '
                                      f'its native review, the record devloop reads, is '
                                      f'uncertain, left to the outbox ({exc})') from exc
        raise GitHubError(f'the review of {key} was not published: its native review, the '
                          f'record devloop reads, failed {REVIEW_ATTEMPTS} times ({error})')

    def _cancellable(self, event):
        """The block in which the turns of this thread stop once ``event`` is set (#521): the
        runner's ``cancellable``; nothing with a runner without it."""
        hook = getattr(self.runner, 'cancellable', None)
        return hook(event) if hook is not None else nullcontext()

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
        t = TEXTS.texts(self.cfg.language)
        self._escalate(pr.number, t('suite_failed', head=pr.head_sha[:12],
                                    mode=self.cfg.local_tests, command=self.cfg.test_cmd,
                                    then=t('suite_promotion' if approved else 'suite_review')),
                       tail, remove=[lb.review], cls='tests_failed')
        return False

    def _do_fix(self, pr):
        cfg, lb = self.cfg, self.cfg.labels
        pending = self._take_pending_push(pr.number, pr.head_sha)
        if pending is not None:
            # A retry of a push that failed for a transient error of the remote (#645): the
            # commits of the fix, or of the sync it started, pushed again without a new turn.
            return self._resume_push(pending, pr.head_sha)
        if pr.fix_requests:
            return self._do_requested_fix(pr)
        t = TEXTS.texts(cfg.language)
        if not pr.reviews:
            return self._escalate(pr.number, t('no_review'), remove=[lb.changes],
                                  cls='inconsistent')
        # One fix answers the whole round, with every finding still open, whoever owns it.
        latest = current_round(pr.reviews)[0]
        findings = open_findings(pr.reviews, pr.closures)
        if not findings:
            return self._escalate(pr.number, t('no_findings'), remove=[lb.changes],
                                  cls='inconsistent')
        if pr.meta['implementer'] not in cfg.agents:
            return self._escalate(
                pr.number, t('fix_unconfigured', agent=pr.meta['implementer']),
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
            return _template('fix', **prompt_values(cfg), repo=cfg.repo, pr_number=pr.number,
                             issue_number=pr.issue,
                             issue_title=issue['title'], issue_body=issue.get('body') or '',
                             findings=_format_findings(findings,
                                                       owner_assessments(pr.reviews)),
                             test_cmd=cfg.test_cmd, ci_summary=ci_summary,
                             acceptance_criteria=format_criteria(
                                 acceptance_criteria(issue.get('body'))),
                             acceptance_map=format_map(latest_map(pr.meta, pr.fixes)),
                             host_step=format_step(latest_host_step(pr.meta, pr.fixes)),
                             docs=docs_diff_section(self._mapped_docs(path)),
                             requests='', base_branch=cfg.base_branch, language=cfg.language,
                             conclave=resolution)
        # One register for the fix's turns, local tests and sync (#355).
        where = self._fix_where(pr.number, latest['round'])
        result, failure, used, tested, local = self._write_turn(
            agent, prompt, 'fix', path, f'pr-{pr.number}-fix-r{latest["round"]}', where=where,
            base_check=True)
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
        ran with (``_fix_where``), whose register gets the ``sync`` event of ``merged`` (#355).
        A push that fails twice for a transient error of the remote keeps the commits for a
        retry that pushes them again, without a new turn (#645, ``_push_step``)."""
        result, failure, used, tested, local = turn
        if result['status'] == 'blocked':
            return self._blocked(pr.number, used, result['blocked_reason'], remove=remove,
                                 on_pr=True, step='fix', head=pr.head_sha, conclave=conclave)
        if isinstance(failure, HostTests):
            # The host's failure (#497): to the operator, nothing pushed.
            return self._escalate_host(pr.number, 'fix', failure, remove=remove)
        if isinstance(failure, BaseBroken):
            # The base's failure, not the fix's (#459): nothing is pushed, the labels stay, and
            # the fix runs again once the base branch advances.
            self._wait_base(pr.number, 'fix', failure, head=pr.head_sha)
            self.ws.remove(name)
            return
        if failure:
            return self._escalate(pr.number, failure[0], failure[1], remove=remove,
                                  cls='tests_failed')

        def pending(record=None):
            # The fix's push (#645): what its veto check reads, and the record it publishes
            # after the push, prepared before it so that a retry needs no new turn.
            return PendingPush('fix', pr.number, {'head': pr.head_sha}, name, str(path),
                               self.ws.head(path), pr.head_ref, agent.key,
                               {'conclave': conclave, 'issue': pr.issue, 'remove': list(remove),
                                'record': record})

        # Checked again before a second attempt of the push, and before the push a retry
        # repeats (#645).
        if self._push_vetoed(pending()):
            return None
        changes = self.ws.has_changes(path)
        generated = []

        def record():
            return self._fix_record(agent, path, result, used, local, round_no, extra, merged,
                                    conclave, where, generated)

        if not (changes or merged):
            return self._publish_fix_record(pr.number, name, agent.key, record())
        auth = self._git_auth(agent.key, IMPLEMENT)
        if changes:
            before = self.ws.head(path)
            commit = self.ws.commit_all(
                path, self._commit_message(agent, result['commit_message'], pr.issue),
                author=auth)
            self._bind_tested(tested, path, commit)
            protected = self._protected_changes(path, since=since)
            if protected:
                return self._escalate_protected(pr.number, protected, remove=remove)
            generated = self._generated(path, before, tested)
        # A push that fails twice for a transient error of the remote keeps the commits for a
        # retry that pushes them again, without a new turn (#645).
        return self._push_step(pending(record()), auth=auth)

    def _fix_record(self, agent, path, result, used, local, round_no, extra, merged, conclave,
                    where, generated):
        """The record of the fix of round ``round_no`` in worktree ``path``, at its head: what
        ``_publish_fix_record`` publishes, all JSON (#645): ``body``, the comment of the record;
        ``merged``, the merge of the base made before the turn, and ``register``, the fix
        register of ``where`` (#355) its ``sync`` event goes to."""
        fix = dict(result, head_sha=self.ws.head(path),
                   profile=self._profile(agent.key, 'write', used), local_tests=local,
                   **skipped_models(used), **(extra or {}))
        if merged:
            fix['base_merge'] = dict(merged, branch=self.cfg.base_branch)
        if generated:
            fix['generated_files'] = generated  # pointed out, not left out (#627)
        if conclave is not None:
            fix['conclave'] = conclave
        register = fix_register(where)
        return {'body': render_fix(fix, self.runner.header(used), round_no,
                                   language=self.cfg.language),
                'merged': merged,
                'register': asdict(register) if register is not None else None}

    def _publish_fix_record(self, number, name, agent_key, record):
        """Publish the ``record`` (``_fix_record``) of the fix of PR ``number`` in worktree
        ``name``, pushed when it changed anything, and send the PR back to review
        (``_publish_fix``)."""
        lb = self.cfg.labels
        merged = record['merged']
        if merged:
            # As for a sync without an agent (#133): an event, not a turn, in the fix's
            # register (#355).
            register = record['register']
            self._ledger_event(number, 'sync',
                               register=FixRegister(**register) if register else None,
                               base=merged['base'], head=merged['head'],
                               previous_head=merged['from'])
        self._as(agent_key, PR_COMMENT).comment(number, record['body'])
        self.gh.edit_labels(number, add=[lb.review], remove=[lb.changes])
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
                pr.number, TEXTS.texts(cfg.language)('requested_unconfigured',
                                                     agent=pr.meta['implementer'],
                                                     command=FIX_COMMAND),
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
            return _template('fix', **prompt_values(cfg), repo=cfg.repo, pr_number=pr.number,
                             issue_number=pr.issue,
                             issue_title=issue['title'], issue_body=issue.get('body') or '',
                             findings=_format_findings(findings,
                                                       owner_assessments(pr.reviews)),
                             test_cmd=cfg.test_cmd, ci_summary=ci_summary,
                             requests=format_fix_requests(requests),
                             acceptance_criteria=format_criteria(
                                 acceptance_criteria(issue.get('body'))),
                             acceptance_map=format_map(latest_map(pr.meta, pr.fixes)),
                             host_step=format_step(latest_host_step(pr.meta, pr.fixes)),
                             docs=docs_diff_section(self._mapped_docs(path)),
                             base_branch=cfg.base_branch, language=cfg.language,
                             conclave=resolution)
        where = self._fix_where(pr.number, round_no)
        turn = self._write_turn(agent, prompt, 'fix', path, f'pr-{pr.number}-fix-request',
                                where=where, base_check=True)
        self._publish_fix(pr, agent, name, path, turn, since=start, round_no=round_no,
                          remove=remove, extra={'requests': requests, 'from': pr.head_sha},
                          merged=merged, conclave=decision, where=where)

    def _sync_rounds_note(self, pr, conflicts, fix):
        """What the comment of a sync of ``pr`` says of the next review (#289): with
        ``conflicts`` an agent resolved, the content of the PR changed and the review counts
        against ``max_review_rounds``, with the rounds left; without, the review of an approved
        head does not count (#235), and any other review counts as it would have anyway. With
        ``conflicts`` on an approved head (#458), or on a head already synced with conflicts
        after its approval, the review starts also with no round left and counts only if it
        does not approve: the comment says both outcomes, and the round a request for changes
        would be.
        ``fix``: a fix waits for the sync, and its review is the next one."""
        # The rounds the supervisor granted (#405) raise the limit of this PR only.
        t = TEXTS.texts(self.cfg.language)
        limit = self.cfg.max_review_rounds + pr.granted_rounds
        what = '`max_review_rounds`' + (t('granted_more', granted=pr.granted_rounds)
                                         if pr.granted_rounds else '')
        latest = current_round(pr.reviews)
        approved = (bool(latest) and latest[0]['source'] == 'agent'
                    and not pending_reviewers(latest) and round_verdict(latest) == 'approve'
                    and all(r['head_sha'] == pr.head_sha for r in latest))
        reason = pr.resync.get('reason') if pr.resync else None
        resynced = reason in (RESYNC_AFTER_APPROVAL, RESYNC_CONFLICTS)
        after_approval = not fix and not pr.rereview and (approved or resynced)
        number = counted_rounds(pr.reviews, **waiver(self.cfg, pr.fixes, pr.closures)) + 1
        if after_approval and not conflicts and reason != RESYNC_CONFLICTS:
            return t('sync_free')
        counts = {'counted': number - 1, 'number': number, 'limit': limit, 'what': what,
                  'left': limit - number}
        if after_approval:
            then = t('then_over' if number > limit else 'then_last' if number == limit
                     else 'then_left', **counts)
            return t('sync_conflicts_approved', then=then)
        which = t('which_fix' if fix else 'which_next')
        why = t('why_fix' if fix else 'why_conflicts' if conflicts else 'why_plain')
        where = t('where_over' if number > limit else 'where_last' if number == limit
                  else 'where_left', which=which, **counts)
        return t('sync_counts', which=which, why=why, where=where)

    def _do_sync(self, pr, fix_remove=None):
        """Merge the base branch into a conflicting PR. The implementer resolves the conflicts
        in the PR worktree, the dispatcher concludes the merge, pushes it and sends the PR back
        to review. No review record is written, so no round is spent.

        ``fix_remove``: the sync was started by a fix whose branch conflicts with the base
        (#250). Escalations remove these labels, and on success the labels stay as they are, so
        that the fix runs on the synced head on a later tick."""
        cfg, lb = self.cfg, self.cfg.labels
        t = TEXTS.texts(cfg.language)
        remove = [lb.review, lb.ready_to_merge] if fix_remove is None else fix_remove
        base = f'origin/{cfg.base_branch}'
        pending = self._take_pending_push(pr.number, pr.head_sha)
        if pending is not None:
            # A retry of a push that failed for a transient error of the remote (#645): the
            # merge already concluded, pushed again without a new turn.
            return self._resume_push(pending, pr.head_sha)
        if pr.meta['implementer'] not in cfg.agents:
            return self._escalate(
                pr.number, t('sync_unconfigured', branch=cfg.base_branch,
                             agent=pr.meta['implementer']), remove=remove,
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
                self._escalate(pr.number, t('sync_failed', branch=cfg.base_branch,
                                            reason=reason, listed=listed),
                               detail, remove=remove, cls=cls, supervised=supervised, why=why,
                               retry=None if supervised else self._retry_payload('sync', pr))
            prompt = _template('sync', **prompt_values(cfg), repo=cfg.repo, pr_number=pr.number,
                               issue_number=pr.issue,
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
                return fail(t('sync_step_failed', failure=_failure_text(exc),
                              hint=self._log_hint(exc)), exc.tail,
                            cls=('configuration' if _cli_state_refused(exc)  # #517
                                 else TOOL_FAILURE if _start_failure(exc) else None))
            if result['status'] == 'blocked':
                return self._blocked(pr.number, used, result['blocked_reason'].strip()
                                     + t('sync_blocked', branch=cfg.base_branch)
                                     + listed, remove=remove, on_pr=True, step='sync',
                                     head=pr.head_sha)
            if failure:
                return fail(failure[0], failure[1])
            # The dispatcher started the merge and concludes it: an agent that committed, reset
            # or aborted it leaves a result that is not the reviewed branch plus the base.
            if self.ws.head(path) != pr.head_sha or self.ws.merge_head(path) != target:
                return fail(t('sync_merge_moved'))
            left = self.ws.conflict_markers(path, files)
            if left:
                return fail(t('sync_markers', files=', '.join(f'`{f}`' for f in left)))
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

        # The record of the sync is prepared before the push, and kept with the merge when the
        # push fails twice for a transient error of the remote: a retry pushes it again and
        # records it, without a new turn, after a restart too (#645).
        # A file the base removed and the resolution keeps is pointed out (#662).
        restored = self.ws.restored_files(path, pr.head_sha) if files else []
        record = self._sync_record(pr, agent, target, head, files, listed,
                                   (result, used) if files else (None, None), fix_remove,
                                   restored=restored)
        return self._push_step(PendingPush('sync', pr.number, {'head': pr.head_sha}, name,
                                           str(path), head, pr.head_ref, agent.key,
                                           {'record': record}), auth=auth)

    def _sync_record(self, pr, agent, target, head, files, listed, turn, fix_remove,
                     restored=()):
        """The record of the sync of ``pr``: the merge of ``target`` into its branch, new head
        ``head``, with the conflicts in ``files`` (``listed``) that the agent's ``turn``
        (``(result, used)``) resolved, and the files the base removed that the resolution keeps
        (``restored``, #662), pointed out as the generated files of #627. What
        ``_publish_sync_record`` publishes after the push,
        all JSON (#645): ``body``, the comment, the agent's with ``files``, otherwise the
        dispatcher's with its ledger ``event``; the ``labels`` to change (None when a fix waits
        for the sync) and the reason to ``supersede`` the receipts of a ready PR, or None."""
        cfg, lb = self.cfg, self.cfg.labels
        t = TEXTS.texts(cfg.language)
        base = f'origin/{cfg.base_branch}'
        result, used = turn
        then = t('sync_review' if fix_remove is None else 'sync_fix')
        text = (f'\n## {t("sync_title", branch=cfg.base_branch)}\n\n'
                + t('sync_text', base=base, target=target[:12], head=head[:12], then=then,
                    note=self._sync_rounds_note(pr, bool(files), fix_remove is not None))
                + '\n\n')
        record = {'event': None, 'labels': None, 'supersede': None,
                  'log': f'  merged {base} into {pr.head_ref}: {head[:12]}'}
        if files:
            text += (t('sync_resolved') + f'\n\n{listed}\n\n{result["summary"].strip()}')
            kept = [{'path': name, 'reasons': ['restored']} for name in restored]
            if kept:
                text += '\n\n' + render_generated(kept, cfg.language)
            # Who resolved the conflicts wrote code on the PR: the marker keeps its profile,
            # so a later change of configuration cannot make it a reviewer of its own code.
            # ``from``: the head it synced, which a fix waiting for it recognizes (#250).
            record['body'] = publishable(self.runner.header(used) + text, 'sync',
                                         {'head_sha': head, 'from': pr.head_sha,
                                          'profile': self._profile(agent.key, 'write', used),
                                          **skipped_models(used), **generated_record(kept)})
        else:
            # A sync without an agent is an event, not a turn (#133).
            record['event'] = {'base': target, 'head': head, 'previous_head': pr.head_sha}
            record['body'] = publishable(dispatcher_header() + text + t('sync_clean'))
        if fix_remove is None:
            # A ready PR whose merge is the operator's (#248) loses the dispatcher's label too.
            record['labels'] = {'add': [lb.review],
                                'remove': [lb.ready_to_merge, lb.changes, *self._released(pr)]}
            if lb.ready_to_merge in pr.labels:
                # The new head voids the receipt of the promotion (#135).
                record['supersede'] = self._merge_texts()('supersede_sync', head=head[:12])
        return record

    def _publish_sync_record(self, number, name, agent_key, record):
        """Publish the ``record`` (``_sync_record``) of the sync of PR ``number`` just pushed
        from worktree ``name``; then back to review (``_do_sync``)."""
        if record['event'] is None:
            self._as(agent_key, PR_COMMENT).comment(number, record['body'])
        else:
            self._ledger_event(number, 'sync', **record['event'])
            self.gh.comment(number, record['body'])
        if record['labels'] is not None:
            self.gh.edit_labels(number, add=record['labels']['add'],
                                remove=record['labels']['remove'])
            if record['supersede'] is not None:
                self._supersede_receipts(number, record['supersede'])
        self.ws.remove(name)
        self.log(record['log'])
