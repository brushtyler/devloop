"""Receipt, level and merge of the dispatcher: the mixin ``MergeMixin`` of ``Dispatcher``.

Moved from ``devloop/dispatcher.py`` without changes (#295, step S5 of #261): the check of an
approval before its promotion (``_approval_current``), the policy receipt of a PR
(``_receipt``, ``_receipt_and_paths``), the checks of an advanced base (``_base_check``,
``_base_checks``, ``_merge_check``), the choice of the reviewers (``_select``), the policy
records and the escalation of a level (``_policy_comment``, ``_publish_policy``,
``_escalate_level``), the promotion (``_promote``, ``_promote_zero``) and its level-0 and
acceptance checks (``_zero_problem``, ``_acceptance_problems``, ``_refuse_zero``), the receipt
of a promoted PR (#135: ``_checked_receipt``, ``_supersede_receipts``), the revocation of a
readiness (``_do_revoke``), the
merge commands and the merge (``_do_hold``, ``_do_merge``, ``_record_merge``) and the issues a
merge resolved (``_close_merged_issue``, ``_do_merged_issue``). Since #338 one comment carries a
promotion, its receipt and the merge that follows it. ``Dispatcher`` inherits the methods.
Since #634 their texts are in the project's ``language`` (``TEXTS``); the records are not.
"""

import itertools
import threading
from dataclasses import replace
from types import SimpleNamespace

from .acceptance import acceptance_criteria, acceptance_findings, latest_map
from .apps import COORDINATOR, MERGER
from .context import _describe_bases, _open_findings_reason, comment_link
from .dispatch_supervision import RELEASE_COMMAND, merge_command
from . import receipt as pr_receipt
from .github import GitHubError
from .hoststep import UNREADABLE as UNREADABLE_STEP
from .hoststep import latest as latest_host_step
from .i18n import Catalog
from .journal import COORDINATOR_MERGE, DELEGATED_MERGE, JournalError
from .ledger import collect as ledger_collect
from .ledger import timestamp as ledger_timestamp
from .markdown import (MAX_BODY, carries_record, dispatcher_header, publishable, read_marker,
                       split_markers, with_record)
from .pin import ScanFailed, merge_known
from .planning import MERGING, label_names, pending_reviewers, touched_by_base, waiver
from .policy import describe as describe_receipt
from .policy import (MAX_LISTED_PATHS, ZERO_LEVEL, another_model, changes_requested,
                     check_outcome, check_states, differences, distinct, evaluate, independent, profile_of,
                     receipt_of, recorded_level, zero_refused)
from .config import LEGACY_NEEDS_OPERATOR, provider_quorum, same_agent
from .receipt import CONFLICT as RECEIPT_CONFLICT
from .review import (DECISION, FINDING_COMMAND, current_round, open_findings, owner_of,
                     round_verdict, trust_policy, trust_policy_parts)
from .supervision import (ADR_DIR, MAX_COORDINATOR_MERGES, MAX_MERGE_BUDGET, MERGE,
                          MERGE_BUDGET, MERGE_OPINIONS, MERGED_ISSUE, adr_paths, adr_problems,
                          merge_arguments, rule_paths)
from .workspace import WorkspaceError

# The texts of promotions, merges and their refusals (#634), in the project's ``language``
# (``i18n``); English for a language without translation. Markers, commands, labels and the
# values of the records are never translated.
TEXTS = Catalog(
    en={
        # Why a level has not enough reviewers (``_select``).
        'select_unverifiable_one': (
            'it cannot be verified that the reviewer is an agent other than the implementer of '
            'the PR (`{implementer}`): the marker does not record its profile and its key is '
            'not configured'),
        'select_none_one': ('no configured reviewer is an agent other than the implementer of '
                            'the PR (`{implementer}`)'),
        'select_unverifiable': (
            'it cannot be verified that the reviewers are independent of who wrote the code: '
            'the PR, one of its fixes or a resolution of conflicts does not record the identity '
            'and the tuple of the agent (markers before #111)'),
        'select_pool': (
            'the reviewer pool ({pool}) does not have enough: two reviewers count as distinct '
            'only if they differ both in GitHub identity and in the tuple (kind, model, effort, '
            'review_args), and none may have the identity or the model (kind, model, effort) of '
            'the implementer or of who fixed the PR'),
        'select_pool_provider': (
            'the reviewer pool ({pool}) does not have enough: every reviewer must have a GitHub '
            'identity and a tuple (kind, model, effort) other than those of who wrote the code '
            'of the PR, two reviewers count as distinct only if they differ both in identity and '
            'in the tuple (kind, model, effort, review_args), and at least one reviewer must be '
            'of a provider other than that of every author (at level 1, the only one)'),
        # The escalation of a level (``_escalate_level``).
        'needed_one': 'one independent reviewer',
        'needed_many': '{level} independent reviewers',
        'level_title': 'Review of level {level}: the operator is needed',
        'level_text': (
            'The dispatcher\'s review policy requires level {level} for this PR, hence {needed}, '
            'but {why}. devloop neither reviews nor promotes the PR: the review and the decision '
            'on the merge are up to a person. The level stays acquired even if the label is '
            'removed or the diff changes.'),
        # The promotion (``_promote``, ``_promote_zero``).
        'ready_title': 'Ready for merge',
        'ready_text': ('Approval of commit `{head}` by {by} confirmed: the recomputed policy '
                       'receipt matches the one of the review{but}.{revalidated}'),
        'but_base': ', apart from the base',
        'revalidated': ('\n\nThe approvals given on an earlier base still hold on the current '
                        'base: the intermediate commits of `{branch}` touch no file of the PR.'
                        '\n\n{bases}'),
        'zero_ready_title': 'Ready for merge (level 0)',
        'zero_ready_text': (
            'Commit `{head}` needs no review: the label of level 0 was added by a trusted user, '
            'the diff is entirely in the allowlist and every expected check ended with '
            '`success`. The merge stays an action of the operator.'),
        'zero_open_findings': (
            'blocking findings remain open, which no review closes at level 0:\n\n{listed}\n\n'
            'A trusted user can close them with `{command} <round/reviewer/ID> '
            'resolved|withdrawn <reason>`'),
        'zero_checks_failed': (
            'not every expected check ended with `success` on the current head (a missing '
            'check, `skipped`, `neutral` or `cancelled` is no success):\n\n{listed}'),
        'zero_changes_requested': (
            'a request for changes (`REQUEST_CHANGES`) by {who} still stands: it must be '
            'dismissed or replaced by an approval of the same author'),
        # A level 0 that no longer holds (``_zero_problem``).
        'zero_head_moved': 'the head moved to `{head}`',
        'zero_level_now': 'the policy receipt recomputed now assigns level {level}:\n\n{policy}',
        'zero_checks_now': ('not every expected check ended with `success` on the most recent '
                            'run of the head:\n\n{listed}'),
        'zero_changes_now': 'a request for changes (`REQUEST_CHANGES`) by {who} still stands',
        'zero_unverifiable': 'it could not be verified again',
        'zero_labels_changed': 'the head or the level labels changed',
        'refuse_zero_title': 'Level 0: the operator is needed',
        'refuse_zero_text': (
            'The review policy assigns level 0 to this PR, but {why}.\n\ndevloop does not '
            'promote the PR. To resume: fix the cause, remove `{needs_operator}` and add '
            '`{review}` again (level 0 is checked again), or remove the label `{zero}` too for '
            'a review of level 1.'),
        'untrusted_issue': 'issue text not verifiable: {reason}',
        # The receipt that contradicts the markers (``_checked_receipt``).
        'receipt_conflict': (
            'The receipt of the PR, built from the records of the dispatcher\'s ledger and from '
            'the markers, holds a contradiction for commit `{head}`: devloop does not promote '
            'the PR.\n\n{problems}\n\nMissing historical data does not block: here the data is '
            'there and does not match. Which record is right must be checked before adding '
            '`{review}` again.'),
        # Why a receipt is superseded (``_supersede_receipts``).
        'supersede_promotion': 'promotion no. {promotion} replaces it',
        'supersede_level': 'level {level} requires the operator',
        'supersede_zero': 'level 0 no longer holds for the head',
        'supersede_stale': ('the recomputed policy receipt no longer matches the one of the '
                            'approval'),
        'supersede_round': 'round {round} does not approve commit `{head}`',
        'supersede_sync': 'the synchronisation moved the head to `{head}`',
        'supersede_escalation': 'escalation `{cls}`: the PR moved to `{label}`',
        # A readiness revoked (``_do_revoke``).
        'zero_suspended': 'Level 0 suspended',
        'zero_suspended_text': ('The PR was ready for merge at level 0 on commit `{head}`, but '
                                '{why}{end}\n\nThe PR goes back to review and the level is '
                                'recomputed'),
        'zero_suspended_why': 'the head, the level labels or the CI changed',
        'approval_suspended': 'Approval suspended',
        'approval_suspended_text': (
            'The approval of commit `{head}` does not hold for the coverage map of the '
            'acceptance criteria (#134):\n\n{problems}\n\nThe PR goes back to review: a new '
            'review, which counts as a round, records the blocking finding'),
        'readiness_suspended': 'Readiness suspended',
        'readiness_suspended_text': (
            'The CI of the approved commit `{head}` is running again. The PR goes back to '
            'review: the approval stays recorded and will be applied again without a new review '
            'if the CI passes and the policy receipt still matches'),
        'approval_revoked': 'Approval revoked',
        'revoked_moved': ('Commit `{head}` is not the approved one (`{approved}`). The PR goes '
                          'back to review'),
        'revoked_none': 'No approved devloop review is on record. The PR goes back to review',
        'released': ', and `{label}`, which the dispatcher had added for the merge, is removed',
        # The hold and release commands (``_do_hold``).
        'hold_title': 'Merge held',
        'hold_text': ('`{by}` held the merge: {reason}\n\ndevloop does not merge the PR while '
                      'it stays `{label}`; `{release}` allows it again.'),
        'release_title': 'Merge released',
        'release_text': ('`{by}` released the merge{reason}\n\ndevloop can merge the PR again '
                         'when it is `{ready}` and the merge directives hold.'),
        # The merge (``_do_merge``, ``_execute_merge``, ``_record_merge``).
        'held_title': 'Merge not executed',
        'held_text': (
            'Right before the merge call devloop read the labels of the PR again, at {at} '
            '(UTC), and they do not allow the merge: {labels}. It needs `{ready}`, without '
            '`{needs_operator}`, `{hold}` or `{needs_supervisor}`. The PR is not merged.'),
        'executor_app': 'with the app `{app}`',
        'executor_gh': 'with the local `gh` login',
        'merged_title': 'Merged by devloop',
        'merged_text': (
            'devloop decided the merge and executed it {executor}: commit `{head}` with a merge '
            'commit, bound to that head (`--match-head-commit`), in mode `{mode}`, at level '
            '{level}. Right before it checked the merge directives again: CI `pass`, '
            '`MERGEABLE` and `CLEAN`, no open finding, no label `{needs_operator}` or `{hold}`, '
            'and the conditions of the promotion with the recomputed policy receipt. It read '
            'the labels of the PR again right before the merge call, at {at} (UTC). The branch '
            'stays.'),
        'merged_coordinator_title': 'Merged by devloop on the command of the coordinator',
        'merged_coordinator_text': (
            'The merge was up to the operator. The coordinator `{by}` asked for it with '
            '`/devloop {command}` (comment {comment}, reserved merge of the coordinator, ADR '
            '0002, #529); right before, devloop verified every condition of the reserved merge '
            'and executed it {executor}: commit `{head}` with a merge commit, bound to that head '
            '(`--match-head-commit`), at level {level}. It read the labels of the PR again '
            'right before the merge call, at {at} (UTC), without `{hold}`. The branch stays.'
            '\n\n**Reason of the coordinator:**\n\n{reason}'),
        'merged_delegated_title': 'Merged by devloop on delegation of the supervisor',
        'merged_delegated_text': (
            'The merge was up to the operator. The supervisor `{by}` asked for it with '
            '`/devloop {command}` (comment {comment}, delegation `[delegations] merge`, #486); '
            'right before, devloop verified every condition of the delegation and executed it '
            '{executor}: commit `{head}` with a merge commit, bound to that head '
            '(`--match-head-commit`), at level {level}. It read the labels of the PR again '
            'right before the merge call, at {at} (UTC), without `{hold}`. The branch stays.'
            '\n\n**Reason of the supervisor:**\n\n{reason}'),
        'recomputed': 'Policy receipt recomputed for the merge:',
        # The alarm of a step on the host (``_comment_host_step``).
        'host_title': 'Step on the host to do before the pin',
        'host_held': ('devloop does not pin this commit nor the later ones automatically: the '
                      'automatic pin resumes after a pin by hand (`devloop pin`) of a commit '
                      'that contains the merge, to do after the step.'),
        'host_off': 'The automatic pin is off: the pin by hand is to do after the step.',
        'host_outside': ('PR #{pr} was merged outside devloop (merge `{merge}`{issue}) and '
                         'declares'),
        'host_merged': 'devloop merged PR #{pr} (head `{head}`{issue}), which declares',
        'host_text': ('{what} a step on the host to do with the loop stopped before the pin of '
                      'the new code:\n\n{step}\n\n{pinning}'),
        # The issue a merge resolved (``_close_merged_issue``, ``_do_merged_issue``).
        'closed_title': 'Resolved by a merged PR',
        'closed_text': (
            'devloop merged PR #{pr} (head `{head}`, merge commit {commit}) in mode `{mode}`, '
            'and closes this issue as completed. With the merge of the merger app the closing '
            'keywords of the PR have no effect, so the dispatcher closes it.'),
        'not_read': 'not read',
        'merged_issue': (
            'The issue is open and has `{ready}`, but a devloop PR that resolves it was already '
            'merged: {listed}. devloop neither checks nor implements it again, so as not to '
            'duplicate the work. If it is resolved it should be closed; if more work is needed, '
            'a new issue should be opened.'),
        # The delegated merge and the reserved merge of the coordinator (``_delegated_merge``).
        'merge_stays': ('The command is spent; the PR stays in `{label}`, and the merge is up '
                        'to the operator.'),
        'merge_refused': 'refused: {why}. {stays}',
        'coordinator_off': ('the merge of the coordinator is off (`[roles.coordinator] '
                            'max_merges_per_day` = 0)'),
        'no_merger': ('the merge of the coordinator runs only with the merger app, and devloop '
                      'does not have it (`[apps.merger]`)'),
        'delegation_off': 'the delegation of the merge is off (`[delegations] merge`)',
        'mode_not_auto': 'the merge mode is `{mode}`, not `auto` with a supervisor',
        'syntax': ('the command is written as `/devloop merge <sha> <reason>`, with the full '
                   'sha (40 characters) of the head of the PR and the reason'),
        'not_operator_merge': ('the PR is not waiting for the merge of the operator '
                               '(`operator_merge`): it is with the operator for another reason'),
        'not_open': 'the PR is not open (`{state}`)',
        'sha_not_head': ('the sha of the command (`{sha}`) is not the current head of the PR '
                         '(`{head}`)'),
        'labels_not_operator': ('the labels of the PR are no longer those of a merge of the '
                                'operator: {labels}'),
        'not_owned': ('`{label}` is no longer the dispatcher\'s label for the merge of the '
                      'operator'),
        'comment_gone': 'the comment of the command is no longer in the conversation',
        'held_after': 'a `/devloop hold` written after the command cancelled it',
        'owners_unknown': 'the owner of some findings cannot be reconstructed',
        'stale_receipt': 'a policy record found the receipt of the approval stale',
        'open_findings': 'blocking findings remain open: {keys}',
        'not_approved': 'the sha `{sha}` is not the one the latest review round approved',
        'not_clean': ('GitHub does not report the PR as `MERGEABLE` and `CLEAN` '
                      '(`{mergeable}`, `{status}`)'),
        'ci_not_green': 'the CI of the head is not green (`{ci}`)',
        'acceptance': 'the coverage map of the acceptance criteria does not hold for the approval',
        'receipt_unavailable': 'the policy receipt cannot be recomputed ({error})',
        'pushed_head': 'the pushed head is `{head}`',
        'trust_paths': 'the PR touches the trust rule or the delegations: {paths}',
        'conclave_restart': ('the work of the PR restarted after the decision of the conclave '
                             '{decision}: the merge stays human'),
        'mode_outside_adr': ('the merge mode of the PR, outside `{adr}`, is `{mode}`, not '
                             '`auto` (paths reserved to the operator or an override of the '
                             'level)'),
        'host_step': ('the PR declares a step on the host (`host_step`), which stays with the '
                      'operator'),
        'no_quorum': 'the PR has no quorum of reviewers for its level',
        'uncovered': 'the change is not covered by a decision of the operator: {what}',
        'missing_review': 'the review of {keys} on `{sha}` is missing',
        'trust_declared': ('a reviewer of the quorum declares the trust policy touched '
                           '(`trust_policy`)'),
        'advisory_missing': 'the advisory review of `{sha}` is missing: `ok` or `dubbi` is needed',
        'advisory_opinion': ('the advisory review of `{sha}` has the opinion `{opinion}`: `ok` '
                             'or `dubbi` is needed'),
        'advisory_none': ('the advisory review of `{sha}` has no opinion: `ok` or `dubbi` is '
                          'needed'),
        'approvals_stale': ('refused: the approvals no longer match the recomputed policy '
                            'receipt, and the PR goes back to review.'),
        'what_coordinator': 'merges of the coordinator',
        'limit_coordinator': ('`[roles.coordinator] max_merges_per_day` = {cap}, at most '
                              '{max}'),
        'what_delegated': 'delegated merges',
        'limit_delegated': ('`[delegations] max_merges_per_day` = {cap}, plus the extensions of '
                            'today'),
        'journal_unavailable': ('today\'s {what} cannot be verified: the turn journal is not '
                                'available or not readable'),
        'daily_cap': 'today\'s {what} already reached the daily maximum ({cap}: {limit})',
        'labels_now': ('the labels of the PR, read again right before the merge call at {at} '
                       '(UTC), are no longer those of a merge of the operator: {labels}'),
        'merge_failed': 'the merge did not succeed ({error})',
        'merge_not_run': 'the merge was not executed',
        'approval_comment': '(comment {id})',
        'with_approval': ('; decision of the operator: the `/devloop approve` {link} on issue '
                          '#{issue}'),
        'after_conclave': ('; work restarted after the unanimous decision of the conclave '
                           '{decision}, approved after the restart'),
        'merged_by_coordinator': (
            'PR merged by the coordinator, commit `{head}`, with the conditions of the reserved '
            'merge verified on that sha; opinion of the advisory review: `{opinion}`{approval}'
            '{conclave}. Merges of the coordinator today: {daily} of {cap}.\n\n**Reason:**\n\n'
            '{reason}'),
        'merged_by_delegation': (
            'PR merged on delegation, commit `{head}`, with the conditions of the delegation '
            'verified on that sha; opinion of the advisory review: `{opinion}`. Delegated '
            'merges today: {daily} of {cap}.\n\n**Reason:**\n\n{reason}'),
        # The operator's decision a reserved merge needs (``_operator_approval``).
        'approval_trust': 'a reviewer declared the trust policy touched',
        'approval_adr': 'the diff touches `{adr}`',
        'approval_and': ' and ',
        'approval_no_issue': '{what}, and the PR does not name the issue it closes',
        'approval_unreadable': '{what}, and issue #{issue} is not readable ({error})',
        'approval_none': (
            '{what}, and issue #{issue} that the PR closes has no valid `/devloop approve` of '
            'the operator, written in person after the latest change of the text and before '
            'devloop took it over'),
        # The changes under docs/adr/ of a delegated merge (``_adr_problem``).
        'adr_unreadable': 'the changes under `{adr}` are not readable ({error})',
        'adr_not_text': 'the changes under `{adr}` are not all readable as text',
        'adr_problems': ('under `{adr}` only added lines with the link to a decision of the '
                         'operator are allowed: {problems}'),
        'adr_untrusted': ('a block added in `{path}` quotes only comments that are not of a '
                          'trusted user, or that cannot be read: {links}'),
    },
    it={
        'select_unverifiable_one': (
            'non è verificabile che il reviewer sia un agent diverso dall\'implementer della PR '
            '(`{implementer}`): il marker non ne registra il profilo e la sua chiave non è '
            'configurata'),
        'select_none_one': ('nessun reviewer configurato è un agent diverso dall\'implementer '
                            'della PR (`{implementer}`)'),
        'select_unverifiable': (
            'non è verificabile che i reviewer siano indipendenti da chi ha scritto il codice: '
            'la PR, una sua correzione o una risoluzione di conflitti non registra identità e '
            'tupla dell\'agent (marker precedenti a #111)'),
        'select_pool': (
            'il pool di reviewer ({pool}) non ne ha abbastanza: due reviewer contano come '
            'distinti solo se differiscono sia per identità GitHub sia per la tupla (kind, '
            'model, effort, review_args), e nessuno può avere l\'identità o il modello (kind, '
            'model, effort) dell\'implementer o di chi ha corretto la PR'),
        'select_pool_provider': (
            'il pool di reviewer ({pool}) non ne ha abbastanza: ogni reviewer deve avere '
            'un\'identità GitHub e una tupla (kind, model, effort) diverse da quelle di chi ha '
            'scritto il codice della PR, due reviewer contano come distinti solo se '
            'differiscono sia per identità sia per la tupla (kind, model, effort, review_args), '
            'e almeno un reviewer deve essere di un fornitore diverso da quello di tutti gli '
            'autori (al livello 1, l\'unico)'),
        'needed_one': 'un reviewer indipendente',
        'needed_many': '{level} reviewer indipendenti',
        'level_title': 'Review di livello {level}: serve un intervento dell\'operatore',
        'level_text': (
            'La policy di review del dispatcher richiede per questa PR il livello {level}, '
            'quindi {needed}, ma {why}. devloop non revisiona né promuove la PR: la review e la '
            'decisione sul merge spettano a una persona. Il livello resta acquisito anche se la '
            'label viene rimossa o il diff cambia.'),
        'ready_title': 'Pronta per il merge',
        'ready_text': ('Approvazione del commit `{head}` da parte di {by} confermata: la '
                       'ricevuta di policy ricalcolata coincide con quella della review{but}.'
                       '{revalidated}'),
        'but_base': ', a parte la base',
        'revalidated': ('\n\nLe approvazioni date su una base precedente restano valide sulla '
                        'base corrente: i commit intermedi di `{branch}` non toccano file della '
                        'PR.\n\n{bases}'),
        'zero_ready_title': 'Pronta per il merge (livello 0)',
        'zero_ready_text': (
            'Il commit `{head}` non richiede review: la label di livello 0 è stata messa da un '
            'utente fidato, il diff è interamente nell\'allowlist e tutti i controlli attesi si '
            'sono conclusi con `success`. Il merge resta un\'azione dell\'operatore.'),
        'zero_open_findings': (
            'restano finding bloccanti aperti, che nessuna review chiude al livello 0:\n\n'
            '{listed}\n\nUn utente fidato può chiuderli con `{command} <round/reviewer/ID> '
            'resolved|withdrawn <motivazione>`'),
        'zero_checks_failed': (
            'non tutti i controlli attesi si sono conclusi con `success` sul head corrente (un '
            'controllo mancante, `skipped`, `neutral` o `cancelled` non vale come successo):'
            '\n\n{listed}'),
        'zero_changes_requested': (
            'una richiesta di modifiche (`REQUEST_CHANGES`) di {who} è ancora valida: va '
            'dismessa o sostituita da un\'approvazione dello stesso autore'),
        'zero_head_moved': 'il head è passato a `{head}`',
        'zero_level_now': ('la ricevuta di policy ricalcolata ora assegna il livello {level}:'
                           '\n\n{policy}'),
        'zero_checks_now': ('non tutti i controlli attesi risultano conclusi con `success` '
                            'sull\'esecuzione più recente del head:\n\n{listed}'),
        'zero_changes_now': ('una richiesta di modifiche (`REQUEST_CHANGES`) di {who} è ancora '
                             'valida'),
        'zero_unverifiable': 'non è stato possibile verificarlo di nuovo',
        'zero_labels_changed': 'il head o le label di livello sono cambiati',
        'refuse_zero_title': 'Livello 0: serve un intervento dell\'operatore',
        'refuse_zero_text': (
            'La policy di review assegna a questa PR il livello 0, ma {why}.\n\ndevloop non '
            'promuove la PR. Per riprendere: risolvere la causa, togliere `{needs_operator}` e '
            'rimettere `{review}` (il livello 0 viene ricontrollato), oppure togliere anche la '
            'label `{zero}` per una review di livello 1.'),
        'untrusted_issue': 'testo dell\'issue non verificabile: {reason}',
        'receipt_conflict': (
            'La ricevuta della PR, costruita dai record del registro del dispatcher e dai '
            'marker, contiene una contraddizione per il commit `{head}`: devloop non promuove '
            'la PR.\n\n{problems}\n\nI dati storici mancanti non bloccano: qui i dati ci sono e '
            'non coincidono. Va verificato quale record è corretto prima di rimettere '
            '`{review}`.'),
        'supersede_promotion': 'la promozione n. {promotion} la sostituisce',
        'supersede_level': 'il livello {level} richiede l\'operatore',
        'supersede_zero': 'il livello 0 non vale più per il head',
        'supersede_stale': ('la ricevuta di policy ricalcolata non coincide più con quella '
                            'dell\'approvazione'),
        'supersede_round': 'il round {round} non approva il commit `{head}`',
        'supersede_sync': 'la sincronizzazione ha portato il head a `{head}`',
        'supersede_escalation': 'escalation `{cls}`: la PR è passata a `{label}`',
        'zero_suspended': 'Livello 0 sospeso',
        'zero_suspended_text': ('La PR era pronta per il merge al livello 0 sul commit `{head}`, '
                                'ma {why}{end}\n\nLa PR torna in review e il livello viene '
                                'ricalcolato'),
        'zero_suspended_why': 'il head, le label di livello o la CI sono cambiati',
        'approval_suspended': 'Approvazione sospesa',
        'approval_suspended_text': (
            'L\'approvazione del commit `{head}` non vale per la mappa di copertura dei criteri '
            'di accettazione (#134):\n\n{problems}\n\nLa PR torna in review: una nuova review, '
            'che conta come un round, registra il finding bloccante'),
        'readiness_suspended': 'Readiness sospesa',
        'readiness_suspended_text': (
            'La CI del commit approvato `{head}` è di nuovo in corso. La PR torna in review: '
            'l\'approvazione resta registrata e verrà riapplicata senza una nuova review se la '
            'CI passa e la ricevuta di policy coincide ancora'),
        'approval_revoked': 'Approvazione revocata',
        'revoked_moved': ('Il commit `{head}` non è quello approvato (`{approved}`). La PR torna '
                          'in review'),
        'revoked_none': 'Non risulta una review devloop approvata. La PR torna in review',
        'released': ', e `{label}`, che il dispatcher aveva messo per il merge, è tolta',
        'hold_title': 'Merge trattenuto',
        'hold_text': ('`{by}` ha trattenuto il merge: {reason}\n\ndevloop non mergia la PR '
                      'finché resta `{label}`; `{release}` lo permette di nuovo.'),
        'release_title': 'Merge rilasciato',
        'release_text': ('`{by}` ha rilasciato il merge{reason}\n\ndevloop può di nuovo '
                         'mergiare la PR quando è `{ready}` e valgono le direttive di merge.'),
        'held_title': 'Merge non eseguito',
        'held_text': (
            'Subito prima della chiamata di merge devloop ha riletto le label della PR, alle '
            '{at} (UTC), e non permettono il merge: {labels}. Serve `{ready}`, senza '
            '`{needs_operator}`, `{hold}` né `{needs_supervisor}`. La PR non è mergiata.'),
        'executor_app': 'con l\'app `{app}`',
        'executor_gh': 'con il login `gh` locale',
        'merged_title': 'Mergiata da devloop',
        'merged_text': (
            'devloop ha deciso il merge e l\'ha eseguito {executor}: commit `{head}` con un '
            'merge commit, legato a quel head (`--match-head-commit`), in modalità `{mode}`, al '
            'livello {level}. Subito prima ha ricontrollato le direttive di merge: CI `pass`, '
            '`MERGEABLE` e `CLEAN`, nessun finding aperto, nessuna label `{needs_operator}` o '
            '`{hold}`, e le condizioni della promozione con la ricevuta di policy ricalcolata. '
            'Le label della PR le ha rilette subito prima della chiamata di merge, alle {at} '
            '(UTC). Il branch resta.'),
        'merged_coordinator_title': 'Mergiata da devloop su comando del coordinatore',
        'merged_coordinator_text': (
            'Il merge spettava all\'operatore. Il coordinatore `{by}` l\'ha chiesto con '
            '`/devloop {command}` (commento {comment}, merge riservato del coordinatore, ADR '
            '0002, #529); devloop ha verificato subito prima tutte le condizioni del merge '
            'riservato e l\'ha eseguito {executor}: commit `{head}` con un merge commit, legato '
            'a quel head (`--match-head-commit`), al livello {level}. Le label della PR le ha '
            'rilette subito prima della chiamata di merge, alle {at} (UTC), senza `{hold}`. Il '
            'branch resta.\n\n**Motivo del coordinatore:**\n\n{reason}'),
        'merged_delegated_title': 'Mergiata da devloop su delega del supervisore',
        'merged_delegated_text': (
            'Il merge spettava all\'operatore. Il supervisore `{by}` l\'ha chiesto con '
            '`/devloop {command}` (commento {comment}, delega `[delegations] merge`, #486); '
            'devloop ha verificato subito prima tutte le condizioni della delega e l\'ha '
            'eseguito {executor}: commit `{head}` con un merge commit, legato a quel head '
            '(`--match-head-commit`), al livello {level}. Le label della PR le ha rilette '
            'subito prima della chiamata di merge, alle {at} (UTC), senza `{hold}`. Il branch '
            'resta.\n\n**Motivo del supervisore:**\n\n{reason}'),
        'recomputed': 'Ricevuta di policy ricalcolata per il merge:',
        'host_title': 'Passo sull\'host da fare prima del pin',
        'host_held': ('devloop non fa il pin automatico di questo commit né dei successivi: il '
                      'pin automatico riprende dopo un pin a mano (`devloop pin`) di un commit '
                      'che contiene il merge, da fare dopo il passo.'),
        'host_off': 'Il pin automatico è spento: il pin a mano va fatto dopo il passo.',
        'host_outside': ('La PR #{pr} è stata mergiata fuori da devloop (merge `{merge}`'
                         '{issue}) e dichiara'),
        'host_merged': 'devloop ha mergiato la PR #{pr} (head `{head}`{issue}), che dichiara',
        'host_text': ('{what} un passo sull\'host da fare a loop fermo prima del pin del nuovo '
                      'codice:\n\n{step}\n\n{pinning}'),
        'closed_title': 'Risolta da una PR mergiata',
        'closed_text': (
            'devloop ha mergiato la PR #{pr} (head `{head}`, merge commit {commit}) in modalità '
            '`{mode}`, e chiude questa issue come completata. Con il merge dell\'app merger le '
            'parole chiave di chiusura della PR non hanno effetto, quindi la chiude il '
            'dispatcher.'),
        'not_read': 'non letto',
        'merged_issue': (
            'L\'issue è aperta e ha `{ready}`, ma è già stata mergiata una PR devloop che la '
            'risolve: {listed}. devloop non la controlla né la implementa di nuovo, per non '
            'duplicare il lavoro. Se è risolta va chiusa; se serve altro lavoro, va aperta una '
            'nuova issue.'),
        'merge_stays': ('Il comando è speso; la PR resta in `{label}`, e il merge spetta '
                        'all\'operatore.'),
        'merge_refused': 'rifiutato: {why}. {stays}',
        'coordinator_off': ('il merge del coordinatore è spento (`[roles.coordinator] '
                            'max_merges_per_day` = 0)'),
        'no_merger': ('il merge del coordinatore si esegue solo con l\'app merger, e devloop '
                      'non ce l\'ha (`[apps.merger]`)'),
        'delegation_off': 'la delega del merge è spenta (`[delegations] merge`)',
        'mode_not_auto': 'la modalità di merge è `{mode}`, non `auto` con un supervisore',
        'syntax': ('il comando va scritto come `/devloop merge <sha> <motivo>`, con lo sha '
                   'completo (40 caratteri) dell\'head della PR e il motivo'),
        'not_operator_merge': ('la PR non aspetta il merge dell\'operatore (`operator_merge`): è '
                               'all\'operatore per un\'altra ragione'),
        'not_open': 'la PR non è aperta (`{state}`)',
        'sha_not_head': ('lo sha del comando (`{sha}`) non è l\'head attuale della PR '
                         '(`{head}`)'),
        'labels_not_operator': ('le label della PR non sono più quelle di un merge '
                                'dell\'operatore: {labels}'),
        'not_owned': ('`{label}` non è più la label del dispatcher per il merge '
                      'dell\'operatore'),
        'comment_gone': 'il commento del comando non è più nella conversazione',
        'held_after': 'un `/devloop hold` scritto dopo il comando lo ha annullato',
        'owners_unknown': 'il proprietario di alcuni finding non è ricostruibile',
        'stale_receipt': ('un record di policy ha trovato superata la ricevuta '
                          'dell\'approvazione'),
        'open_findings': 'restano finding bloccanti aperti: {keys}',
        'not_approved': 'lo sha `{sha}` non è quello approvato dall\'ultimo round di review',
        'not_clean': ('GitHub non riporta la PR come `MERGEABLE` e `CLEAN` (`{mergeable}`, '
                      '`{status}`)'),
        'ci_not_green': 'la CI dell\'head non è verde (`{ci}`)',
        'acceptance': ('la mappa di copertura dei criteri di accettazione non vale per '
                       'l\'approvazione'),
        'receipt_unavailable': 'la ricevuta di policy non è ricalcolabile ({error})',
        'pushed_head': 'l\'head pubblicato è `{head}`',
        'trust_paths': 'la PR tocca la regola di fiducia o le deleghe: {paths}',
        'conclave_restart': ('il lavoro della PR è ripartito dopo la decisione del conclave '
                             '{decision}: il merge resta umano'),
        'mode_outside_adr': ('la modalità di merge della PR, fuori da `{adr}`, è `{mode}`, non '
                             '`auto` (percorsi riservati all\'operatore o un override del '
                             'livello)'),
        'host_step': ('la PR dichiara un passo sull\'host (`host_step`), che resta '
                      'all\'operatore'),
        'no_quorum': 'la PR non ha un quorum di reviewer per il suo livello',
        'uncovered': 'la modifica non risulta coperta da una decisione dell\'operatore: {what}',
        'missing_review': 'manca la review di {keys} su `{sha}`',
        'trust_declared': ('un reviewer del quorum dichiara toccata la policy di fiducia '
                           '(`trust_policy`)'),
        'advisory_missing': 'la review consultiva di `{sha}` manca: serve `ok` o `dubbi`',
        'advisory_opinion': ('la review consultiva di `{sha}` ha parere `{opinion}`: serve `ok` '
                             'o `dubbi`'),
        'advisory_none': 'la review consultiva di `{sha}` non ha un parere: serve `ok` o `dubbi`',
        'approvals_stale': ('rifiutato: le approvazioni non corrispondono più alla ricevuta di '
                            'policy ricalcolata, e la PR torna in review.'),
        'what_coordinator': 'i merge del coordinatore',
        'limit_coordinator': ('`[roles.coordinator] max_merges_per_day` = {cap}, al massimo '
                              '{max}'),
        'what_delegated': 'i merge delegati',
        'limit_delegated': ('`[delegations] max_merges_per_day` = {cap}, più le estensioni di '
                            'oggi'),
        'journal_unavailable': ('{what} di oggi non sono verificabili: il journal dei turni non '
                                'è disponibile o non è leggibile'),
        'daily_cap': 'oggi {what} hanno già raggiunto il massimo giornaliero ({cap}: {limit})',
        'labels_now': ('le label della PR, rilette subito prima della chiamata di merge alle '
                       '{at} (UTC), non sono più quelle di un merge dell\'operatore: {labels}'),
        'merge_failed': 'il merge non è riuscito ({error})',
        'merge_not_run': 'il merge non è stato eseguito',
        'approval_comment': '(commento {id})',
        'with_approval': ('; decisione dell\'operatore: il `/devloop approve` {link} sulla issue '
                          '#{issue}'),
        'after_conclave': ('; lavoro ripartito dopo la decisione unanime del conclave '
                           '{decision}, approvato dopo la ripartenza'),
        'merged_by_coordinator': (
            'PR mergiata dal coordinatore, commit `{head}`, con le condizioni del merge '
            'riservato verificate su quello sha; parere della review consultiva: `{opinion}`'
            '{approval}{conclave}. Merge del coordinatore di oggi: {daily} di {cap}.\n\n'
            '**Motivo:**\n\n{reason}'),
        'merged_by_delegation': (
            'PR mergiata su delega, commit `{head}`, con le condizioni della delega verificate '
            'su quello sha; parere della review consultiva: `{opinion}`. Merge delegati di oggi: '
            '{daily} di {cap}.\n\n**Motivo:**\n\n{reason}'),
        'approval_trust': 'un reviewer ha dichiarato toccata la policy di fiducia',
        'approval_adr': 'il diff tocca `{adr}`',
        'approval_and': ' e ',
        'approval_no_issue': '{what}, e la PR non nomina la issue che chiude',
        'approval_unreadable': '{what}, e la issue #{issue} non è leggibile ({error})',
        'approval_none': (
            '{what}, e la issue #{issue} che la PR chiude non ha un `/devloop approve` valido '
            'dell\'operatore, scritto di persona dopo l\'ultima modifica del testo e prima che '
            'devloop la prendesse in carico'),
        'adr_unreadable': 'le modifiche sotto `{adr}` non sono leggibili ({error})',
        'adr_not_text': 'le modifiche sotto `{adr}` non sono tutte leggibili come testo',
        'adr_problems': ('sotto `{adr}` sono ammesse solo righe aggiunte con il link a una '
                         'decisione dell\'operatore: {problems}'),
        'adr_untrusted': ('un blocco aggiunto in `{path}` cita solo commenti che non sono di un '
                          'utente fidato, o che non si leggono: {links}'),
    })


def _quoted(text):
    """``text`` as a Markdown quote, line by line."""
    return '\n'.join(f'> {line}'.rstrip() for line in text.splitlines())


class MergeHeld(Exception):
    """The labels of the PR, read again just before the merge call (#519), hold the merge:
    ``labels`` as that read gave them, ``at`` its time (``ledger.timestamp``)."""

    def __init__(self, labels, at):
        super().__init__(f'labels read again at {at}: ' + ', '.join(sorted(labels)))
        self.labels, self.at = labels, at


class MergeMixin:
    """The methods of ``Dispatcher`` for receipts, review levels and merges (#295). They use
    the dispatcher state and its other methods through ``self``."""

    # The alarm of a hold of the auto-pin goes out once (#473): the merge worker and the tick
    # may both try it, the tick when the merge's own alarm has not gone out yet.
    _host_alert_lock = threading.Lock()

    def _merge_texts(self):
        """The texts of promotions and merges (``TEXTS``) in the project's language (#634)."""
        return TEXTS.texts(self.cfg.language)

    def _describe_receipt(self, receipt):
        """``policy.describe`` of ``receipt`` in the project's language (#634)."""
        return describe_receipt(receipt, self.cfg.language)

    def _approval_current(self, pr, review, blind=False):
        """Re-read head and CI just before promoting: approval holds only for the reviewed commit
        with CI passed or absent. Otherwise the PR stays in review for the next tick.

        ``blind``: the round approved the head before its CI was known (#271), and no full suite
        ran locally since. Then an absent CI is not enough, unless ``local_tests = "full"``:
        the approval waits for the CI of the same head, or for the full suite."""
        head = self.gh.pr_head(pr.number)
        if head != review['head_sha']:
            self.log(f'  not promoted: head moved to {head[:12]} after the review')
            return False
        state, details = self._read_ci(head)
        if state not in ('pass', 'none'):
            self.log(f'  not promoted: CI is {state}')
            return False
        if state == 'none' and blind and self.cfg.local_tests != 'full':
            self.log(f'  not promoted: no CI on {head[:12]} yet, and the approval did not see it')
            return False
        self._record_ci(pr, head, state, details, review['round'])
        return True

    # Review policy -----------------------------------------------------------
    def _receipt(self, pr):
        """The policy receipt of the PR's pushed head, computed now from the running
        dispatcher's policy: the full diff against the latest base commit, every ``labeled``
        event of the level label on the PR, and the highest level already recorded on it."""
        return self._receipt_and_paths(pr)[0]

    def _receipt_and_paths(self, pr, reviews=None):
        """``_receipt`` and every path of the diff it was computed on. ``reviews``: every review
        of the PR up to the round being promoted, which the snapshot may not have yet
        (``_promote``): the reviewers that round chose are read from them (#409)."""
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
                           select=lambda level: self._select(pr, level, reviews=reviews)[0],
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

    def _implementer_agents(self, pr):
        """What a level 1 reviewer is compared with (``same_agent``, ADR 0005, P5-3, #407): the
        profile of the PR's marker, if any, and each model of the agent configured under the
        implementer's key, if configured; ``[]`` when neither is known."""
        cfg, implementer = self.cfg, pr.meta['implementer']
        authors = [p for p in [profile_of(pr.meta)] if p is not None]
        if implementer in cfg.agents:
            authors += cfg.agent(implementer).chain()
        return authors

    def _select(self, pr, level, fresh=False, reviews=None):
        """``(reviewers, problem)``: the reviewers ``level`` requires on the PR, in order, or
        none and why.

        Level 1 keeps the rule from before #111: the PR's recorded reviewer, else the first of
        the pool, never the PR's implementer. Level 2 takes the first pair of the pool, in
        configuration order, that is distinct and independent of everyone who wrote code on
        the PR: the implementer, every fixer and every agent that resolved conflicts in a sync,
        as their markers record them, and the agent configured now under the implementer's
        key, which runs fixes and syncs, with each of its models (#121). When a marker does not record who wrote the code,
        independence cannot be verified.

        At both levels a reviewer that is the same agent in effect as the implementer
        (``same_agent``: the same kind, model or provider; at level 2 as any author) is
        excluded, never by comparing keys (ADR 0005, P5-3, #407): ``claude-implementer`` and
        ``claude-reviewer`` are different keys of the same agent. Nor does an agent that signs
        with a role app review. The review turn checks each model it actually runs, fallbacks
        included, in the same way (``ActionsMixin._do_review``).

        With ``review_policy.provider_quorum`` (ADR 0005, P5-5, #409) the reviewers are chosen
        by ``_select_by_provider`` instead; ``fresh`` and ``reviews`` matter only there."""
        if self.cfg.review_policy.provider_quorum:
            return self._select_by_provider(pr, level, fresh, reviews)
        cfg, implementer, t = self.cfg, pr.meta['implementer'], self._merge_texts()

        def eligible(key, authors):
            return (key in cfg.agents and not cfg.signs_for_role(key)
                    and not any(same_agent(cfg.agent(key), a) for a in authors))
        if level == 1:
            authors = self._implementer_agents(pr)
            if not authors:
                return [], t('select_unverifiable_one', implementer=implementer)
            for key in (pr.meta['reviewer'], *cfg.pool()):
                if eligible(key, authors):
                    return [key], ''
            return [], t('select_none_one', implementer=implementer)
        authors = self._authors(pr)
        if authors is None:
            return [], t('select_unverifiable')
        candidates = [(key, self._profile(key, 'review')) for key in cfg.pool()
                      if eligible(key, authors)]
        candidates = [(key, p) for key, p in candidates if all(independent(p, a) for a in authors)]
        for group in itertools.combinations(candidates, level):
            if all(distinct(a[1], b[1]) for a, b in itertools.combinations(group, 2)):
                return [key for key, _ in group], ''
        return [], t('select_pool', pool=', '.join(f'`{key}`' for key in cfg.pool()))

    def _select_by_provider(self, pr, level, fresh, reviews=None):
        """``(reviewers, problem)`` with the reviewer rules of ADR 0005, P5-5 (#409), at both
        levels:

        - a reviewer is independent of every author of the PR (``_authors``: the implementer,
          every fixer, every sync with conflicts, the models of the implementer's key): another
          GitHub identity and another ``(kind, model, effort)``. The kind or the provider alone
          no longer excludes it: ``claude-reviewer`` with Sonnet may review Opus's code;
        - the quorum rule (ii) of D8 (``provider_quorum``): at level 1 the only reviewer is of a
          provider other than every author's; at level 2 at least one of the pair is, and the
          two are distinct (``distinct``);
        - candidates in order: at level 1 the PR's recorded reviewer, then the pool; at level 2
          the pairs of the pool in configuration order.

        Without ``fresh``, the reviewers of the latest round's receipt are kept while they still
        meet these rules: a reviewer is never replaced in the middle of a round, and the receipt
        a promotion or a merge recomputes is the one the round was approved with. The latest
        round is read from ``reviews`` when given, else from the snapshot's: a promotion passes
        the records of the round it applies, which the snapshot does not have when the round
        ended in the same action (parallel reviewers), so that a round that chose other
        reviewers than the previous one is promoted with its own. ``fresh`` (a
        new round, ``_fresh_receipt``) chooses again: first the groups that leave out fewest
        owners of open findings, who alone may close them; among those, a group without a
        reviewer whose whole chain is remembered unavailable (``_unavailable_reviewer``), so
        the next valid reviewer of the pool takes its place; then the configuration order.
        When every valid group has such a reviewer, the first is kept, as before: its turn
        fails as ``tool_failure``."""
        cfg, t = self.cfg, self._merge_texts()
        authors = self._authors(pr)
        if authors is None:
            return [], t('select_unverifiable')
        keys = (pr.meta['reviewer'], *cfg.pool()) if level == 1 else cfg.pool()
        candidates = {}
        for key in dict.fromkeys(keys):
            if key in cfg.agents and not cfg.signs_for_role(key):
                profile = self._profile(key, 'review')
                if all(independent(profile, a) for a in authors):
                    candidates[key] = profile

        def valid(group):
            profiles = [candidates.get(key) for key in group]
            return (len(set(group)) == level and None not in profiles
                    and all(distinct(a, b) for a, b in itertools.combinations(profiles, 2))
                    and provider_quorum(profiles, authors))
        kept = None if fresh else self._round_reviewers(
            pr.reviews if reviews is None else reviews, level)
        if kept is not None and valid(kept):
            return kept, ''
        groups = [list(g) for g in itertools.combinations(candidates, level) if valid(g)]
        if not groups:
            return [], t('select_pool_provider',
                         pool=', '.join(f'`{key}`' for key in cfg.pool()))
        # The owners of open findings who may review: only they close their findings.
        owners = {f['owner'] for f in open_findings(pr.reviews, pr.closures)} & set(candidates)
        out = {key for key in candidates if self._unavailable_reviewer(key)}
        # ``min`` keeps the first of equals: the configuration order decides the rest.
        chosen = min(groups, key=lambda g: (len(owners - set(g)), bool(out & set(g))))
        for key in sorted(out & (set(groups[0]) - set(chosen))):
            self.log(f'  reviewer {key} skipped: every model of its chain is remembered '
                     'unavailable')
        return chosen, ''

    def _findings_reviewers(self, pr, receipt):
        """The findings-only reviewer of a new round of ``pr`` under ``receipt`` (ADR 0005, D8,
        integration of 2026-11-19, #416), as ``[key]``, or ``[]``. It reviews only when
        ``review_policy.findings_reviewer`` is set, the level is 2, the PR's implementer signs
        with the implementer app (P5-3, #407) and is the same agent in effect as it
        (``same_agent``), and its profile is independent of every author of the PR with a
        model of another name, whatever the effort (``another_model``). It is never one of the
        reviewers the receipt requires: it adds its findings to their round, and its approval
        never counts for the quorum."""
        cfg, key = self.cfg, self.cfg.review_policy.findings_reviewer
        implementer = profile_of(pr.meta)
        if (not key or receipt['level'] != 2 or key in receipt['reviewers']
                or key not in cfg.agents or cfg.signs_for_role(key) or implementer is None
                or not cfg.signs_for_role(pr.meta.get('implementer'))
                or not same_agent(cfg.agent(key), implementer)):
            return []
        authors = self._authors(pr)
        profile = self._profile(key, 'review')
        if authors is None or not all(another_model(profile, a) for a in authors):
            self.log(f'  findings-only reviewer {key} skipped: not independent of the authors '
                     'of the PR, or of the model of one of them')
            return []
        return [key]

    def _round_reviewers(self, reviews, level):
        """The reviewers the receipt of the latest agent round of ``reviews`` requires, when it
        was given at ``level``; else None."""
        records = [r for r in current_round(reviews) if r.get('source') == 'agent']
        receipt = receipt_of(records[0]) if records else None
        if receipt is None or receipt['level'] != level or len(receipt['reviewers']) != level:
            return None
        return list(receipt['reviewers'])

    def _unavailable_reviewer(self, key):
        """Whether every model of the chain of reviewer ``key`` is remembered unavailable
        (#151): its turn would run no CLI. Only an agent with fallback models is remembered."""
        agent = self.cfg.agent(key)
        if self.memory is None or not agent.fallback_models:
            return False
        return all(self.memory.active(key, m.model) is not None for m in agent.chain())

    def _fresh_receipt(self, pr, receipt):
        """``receipt`` with the reviewers a new round chooses (``_select_by_provider`` with
        ``fresh``), with ``review_policy.provider_quorum``; else ``receipt`` as it is."""
        if not self.cfg.review_policy.provider_quorum or receipt['level'] == ZERO_LEVEL:
            return receipt
        reviewers = self._select(pr, receipt['level'], fresh=True)[0]
        if reviewers == receipt['reviewers']:
            return receipt
        return dict(receipt, reviewers=reviewers,
                    profiles=[self._profile(key, 'review') for key in reviewers])

    def _policy_comment(self, pr, title, text, receipt, event, bases=(), cls=None,
                        prepared=None):
        """A dispatcher ``policy`` record; ``bases``: the base checks it relied on (#160);
        ``cls``: the class of the escalation it records, if any (#182); ``prepared``: the
        receipt of a promotion, written in the same comment (``_publish_policy``)."""
        payload = {'event': event, 'receipt': receipt}
        if bases:
            payload['bases'] = list(bases)
        if cls:
            payload['class'] = cls
        self._publish_policy(pr, dispatcher_header() + f'\n## {title}\n\n{text.strip()}\n\n'
                             + self._describe_receipt(receipt), payload, prepared)

    def _publish_policy(self, pr, text, payload, prepared=None):
        """Write the ``policy`` record ``payload`` with ``text``. With ``prepared``, the receipt
        of a promotion (#135, ``_checked_receipt``), every receipt still current on the PR is
        marked superseded first, so that only the latest promotion's holds; then one comment
        carries both (#338): the receipt's section after ``text``, its ``receipt`` record after
        the ``policy`` one. When the two do not fit one comment of GitHub they are written as
        two, the promotion and then its receipt, as before #338. A failure raises: the promotion
        stops before its label, as for any ``gh`` failure."""
        if prepared is None:
            self.gh.comment(pr.number, publishable(text, 'policy', payload))
            return
        content, thread = prepared
        language = self.cfg.language
        self._supersede_receipts(pr.number, self._merge_texts()(
            'supersede_promotion', promotion=content['promotion']), thread, strict=True)
        body = publishable(text.rstrip() + '\n\n' + pr_receipt.section(content, language),
                           'policy', payload, records=[('receipt', pr_receipt.record(content))])
        if len(body) <= MAX_BODY:
            self.gh.comment(pr.number, body)
        else:
            self.log('  promotion and receipt too long for one comment: written apart')
            self.gh.comment(pr.number, publishable(text, 'policy', payload))
            self.gh.comment(pr.number, pr_receipt.render(content, language=language))
        self.log(f'  receipt of promotion {content["promotion"]} written')

    def _escalate_level(self, pr, receipt):
        """Not enough independent reviewers for the level: no review, no promotion, the
        operator decides."""
        lb, level, t = self.cfg.labels, receipt['level'], self._merge_texts()
        needed = t('needed_one') if level == 1 else t('needed_many', level=level)
        self._policy_comment(
            pr, t('level_title', level=level),
            t('level_text', level=level, needed=needed, why=self._select(pr, level)[1]),
            receipt, 'level', cls='review_level')
        self.gh.edit_labels(pr.number, add=[lb.needs_operator],
                            remove=[lb.review, lb.changes, lb.ready_to_merge])
        if lb.ready_to_merge in pr.labels:
            self._supersede_receipts(pr.number, t('supersede_level', level=level))

    def _promote(self, pr, records, reviews=None):
        """Promote a round whose approvals reach the quorum: every reviewer the receipt
        recomputed now requires approved the head with that same receipt. Otherwise record why
        and leave the PR in review for a new review of the same head. ``reviews``: every review
        of the PR up to the round included, for its receipt (#135); the snapshot's by default."""
        lb = self.cfg.labels
        current, paths = self._receipt_and_paths(pr, reviews)
        if current['level'] == ZERO_LEVEL:
            return self._promote_zero(pr, current)
        if len(current['reviewers']) < current['level']:
            return self._escalate_level(pr, current)
        bases = self._approvals_match(pr, records, current, paths)
        if bases is None:
            return
        coherent, prepared = self._checked_receipt(pr, current, records[0]['head_sha'],
                                                   records[0]['round'],
                                                   pr.reviews if reviews is None else reviews)
        if not coherent:
            return None
        by = ', '.join(f'`{key}`' for key in current['reviewers'])
        branch, t = self.cfg.base_branch, self._merge_texts()
        revalidated = t('revalidated', branch=branch, bases=_describe_bases(
            bases, branch, self.cfg.language)) if bases else ''
        self._policy_comment(
            pr, t('ready_title'),
            t('ready_text', head=records[0]['head_sha'][:12], by=by,
              but=t('but_base') if bases else '', revalidated=revalidated),
            current, 'promoted', bases, prepared=prepared)
        self.gh.edit_labels(pr.number, add=[lb.ready_to_merge], remove=[lb.review, lb.changes])
        return None

    def _promote_zero(self, pr, receipt):
        """Level 0 (#107): promote the head without a review, only if no finding is open, the
        head is still the receipt's, every expected check run succeeded on it and no trusted
        user's native request for changes stands. Running checks are waited for; anything else
        goes to a human. The merge stays the operator's."""
        lb, t = self.cfg.labels, self._merge_texts()
        left = open_findings(pr.reviews, pr.closures)
        if left:
            return self._refuse_zero(pr, receipt, t(
                'zero_open_findings', command=FINDING_COMMAND,
                listed='\n'.join(f'- `{f["key"]}` ({f["title"]})' for f in left)))
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
            return self._refuse_zero(pr, receipt, t('zero_checks_failed', listed=listed), states)
        blocking = changes_requested(self.gh.pr_reviews(pr.number), self.cfg.trusted_users)
        if blocking:
            return self._refuse_zero(pr, receipt, t(
                'zero_changes_requested', who=', '.join(f'`{who}`' for who in blocking)), states)
        coherent, prepared = self._checked_receipt(pr, receipt, head, None, pr.reviews)
        if not coherent:
            return None
        self._publish_policy(
            pr, dispatcher_header() + f'\n## {t("zero_ready_title")}\n\n'
            + t('zero_ready_text', head=head[:12]) + '\n\n'
            + listed + '\n\n' + self._describe_receipt(receipt),
            {'event': 'promoted', 'receipt': receipt, 'checks': states}, prepared)
        self.gh.edit_labels(pr.number, add=[lb.ready_to_merge], remove=[lb.review, lb.changes])
        return None

    # PR receipt (#135) -----------------------------------------------------
    def _checked_receipt(self, pr, current, approved_head, approved_round, reviews):
        """``(coherent, prepared)`` for the promotion of ``pr`` with the policy receipt
        ``current``: ``prepared`` is ``(content, thread)``, the receipt built from the ledger
        records of the issue and of the PR and from the markers, and the PR comments read for
        it; None without a ledger, which writes no receipt. When the receipt contradicts the
        markers (``receipt.contradictions``) the PR goes to the operator with a comment that
        lists why, and ``coherent`` is False: no promotion. Missing data never blocks."""
        if self.ledger is None:
            return True, None
        thread = self.gh.pr_comments(pr.number)
        # The advisory review of the same head, already recorded (#369), goes in the receipt.
        content = self._receipt_content(pr, current, pr.promotions + 1, approved_round, reviews,
                                        thread, self._advisory_record(thread, current['head']))
        problems = pr_receipt.contradictions(content, implementer=pr.meta['implementer'],
                                             approved_head=approved_head,
                                             language=self.cfg.language)
        if problems:
            lb = self.cfg.labels
            self.log(f'  not promoted: the receipt contradicts the markers ({len(problems)})')
            self._escalate(
                pr.number, self._merge_texts()(
                    'receipt_conflict', head=approved_head[:12], review=lb.review,
                    problems='\n'.join(f'- {p}' for p in problems)),
                remove=[lb.review, lb.changes, lb.ready_to_merge], cls=RECEIPT_CONFLICT)
            return False, None
        return True, (content, thread)

    def _receipt_content(self, pr, policy, promotion, approved_round, reviews, thread,
                         advisory=None):
        """The content of the receipt of promotion ``promotion`` of ``pr`` with the policy
        receipt ``policy`` (``receipt.build``): from the ledger records of the issue and of the
        PR (``thread``, its comments) and from the markers; ``advisory``, the dispatcher's
        record of the advisory review of the head (#369), or None."""
        on_issue = ledger_collect(self.gh.issue_comments(pr.issue), self._signed)
        on_pr = ledger_collect(thread, self._signed)
        # The criteria as the issue states them, read now; a text changed by someone outside
        # trusted_users is not quoted.
        history = self.gh.issue_history(pr.issue)
        criteria = (None if self._untrusted_change(history)
                    else acceptance_criteria(history['body']))
        return pr_receipt.build(
            pr=pr.number, issue=pr.issue, head=policy['head'], promotion=promotion,
            policy=policy, request=on_issue['request'], issue_records=on_issue,
            pr_records=on_pr, reviews=reviews, closures=pr.closures,
            approved_round=approved_round, criteria=criteria,
            acceptance_map=latest_map(pr.meta, pr.fixes), advisory=advisory,
            budget=self._budget_view(pr.issue), waiver=waiver(self.cfg, pr.fixes, pr.closures))

    def _supersede_receipts(self, number, why, comments=None, strict=False):
        """Mark superseded, because ``why``, every receipt of PR ``number`` still current: the
        dispatcher's ``receipt`` records read from ``comments`` (read now when None). An
        approval revoked leaves no receipt current: a revocation, a new head, a stale policy
        receipt, a failed round and every escalation that takes ``agent:ready-to-merge`` away
        (``_escalate``) come here. Without ``strict`` a failure is logged: the next promotion
        marks it again before writing its own."""
        if self.ledger is None:
            return
        try:
            if comments is None:
                comments = self.gh.pr_comments(number)
            at = ledger_timestamp(self.clock)
            for position, record in self._dispatcher_records(comments, 'receipt'):
                if record['state'] != 'current':
                    continue
                comment = comments[position]
                if comment.get('id') is None:
                    raise GitHubError(f'receipt on #{number}: comment id unknown, not edited')
                self.gh.edit_comment(comment['id'], pr_receipt.superseded(
                    comment['body'], record, why, at, self.cfg.language))
                self.log(f'  receipt of promotion {record["promotion"]} marked superseded')
        except GitHubError as exc:
            if strict:
                raise
            self.log(f'  receipts of #{number} not marked superseded: {exc}')

    def _zero_problem(self, pr):
        """Why a PR ready at level 0 no longer is, or ``''``: on every tick its receipt is
        recomputed from the running dispatcher's policy (label history, level-2 precedence,
        allowlist), its expected check runs must still have succeeded on the head, and no
        trusted user's native request for changes may stand. What cannot be verified does not
        hold."""
        t = self._merge_texts()
        try:
            receipt = self._receipt(pr)
            if receipt['head'] != pr.head_sha:
                return t('zero_head_moved', head=receipt['head'][:12])
            if receipt['level'] != ZERO_LEVEL:
                return t('zero_level_now', level=receipt['level'],
                         policy=self._describe_receipt(receipt))
            states = check_states(self.cfg.review_policy.zero_checks,
                                  self.gh.check_runs(pr.head_sha))
            if check_outcome(states) != 'success':
                return t('zero_checks_now', listed='\n'.join(
                    f'- `{s["name"]}` (app {s["app_id"]}): `{s["state"]}`' for s in states))
            blocking = changes_requested(self.gh.pr_reviews(pr.number), self.cfg.trusted_users)
            if blocking:
                return t('zero_changes_now', who=', '.join(f'`{who}`' for who in blocking))
        except (GitHubError, WorkspaceError) as exc:
            self.log(f'  level 0 of #{pr.number} not verifiable: {exc}')
            return t('zero_unverifiable')
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
            return [self._merge_texts()('untrusted_issue', reason=reason)]
        # The titles go into texts of the merge, in the project's language (#634).
        found = acceptance_findings(acceptance_criteria(history['body']),
                                    latest_map(pr.meta, pr.fixes), language=self.cfg.language)
        return [f['title'] for f in found]

    def _refuse_zero(self, pr, receipt, why, states=()):
        """Level 0 holds but the head cannot be promoted: the operator decides."""
        lb, t = self.cfg.labels, self._merge_texts()
        self.gh.comment(pr.number, publishable(
            dispatcher_header() + f'\n## {t("refuse_zero_title")}\n\n'
            + t('refuse_zero_text', why=why.strip(), needs_operator=lb.needs_operator,
                review=lb.review, zero=self.cfg.review_policy.zero_label)
            + '\n\n' + self._describe_receipt(receipt),
            'policy', {'event': 'level', 'receipt': receipt, 'checks': list(states),
                       'class': 'level_zero'}))
        self.gh.edit_labels(pr.number, add=[lb.needs_operator],
                            remove=[lb.review, lb.changes, lb.ready_to_merge])
        if lb.ready_to_merge in pr.labels:
            self._supersede_receipts(pr.number, t('supersede_zero'))

    # Actions -------------------------------------------------------------
    def _do_revoke(self, pr):
        lb, t = self.cfg.labels, self._merge_texts()
        latest = current_round(pr.reviews)
        complete = latest and not pending_reviewers(latest)
        approved = (latest[0]['head_sha'] if complete and round_verdict(latest) == 'approve'
                    else None)
        if pr.zero_head is not None:
            title = t('zero_suspended')
            why = pr.zero_problem.strip() if pr.zero_problem else t('zero_suspended_why')
            # A list ends its own paragraph; a sentence needs its full stop.
            end = '' if why.splitlines()[-1].startswith('- ') else '.'
            reason = t('zero_suspended_text', head=pr.zero_head[:12], why=why, end=end)
        elif approved == pr.head_sha and pr.acceptance:
            title = t('approval_suspended')
            reason = t('approval_suspended_text', head=approved[:12],
                       problems='\n'.join(f'- {problem}' for problem in pr.acceptance))
        elif approved == pr.head_sha:
            title = t('readiness_suspended')
            reason = t('readiness_suspended_text', head=approved[:12])
        elif approved:
            title = t('approval_revoked')
            reason = t('revoked_moved', head=pr.head_sha[:12], approved=approved[:12])
        else:
            title = t('approval_revoked')
            reason = t('revoked_none')
        released = self._released(pr)
        if released:
            reason += t('released', label=lb.needs_operator)
        self.gh.comment(pr.number,
                        publishable(dispatcher_header() + f'\n## {title}\n\n' + reason + '.'))
        self.gh.edit_labels(pr.number, add=[lb.review], remove=[lb.ready_to_merge, *released])
        # The receipt of the promotion no longer holds (#135).
        self._supersede_receipts(pr.number, title)

    def _do_hold(self, pr):
        """Apply a hold or release command (#172): add or remove ``agent:merge-hold``, then
        record who asked and why in a dispatcher ``hold`` record, which spends the command.
        Label first: if the record is lost, the next tick applies the same command again."""
        lb, command, t = self.cfg.labels, pr.hold, self._merge_texts()
        reason = ' '.join(command['reason'].split())
        if command['event'] == 'hold':
            self.gh.edit_labels(pr.number, add=[lb.merge_hold])
            title = t('hold_title')
            text = t('hold_text', by=command['by'], reason=reason, label=lb.merge_hold,
                     release=RELEASE_COMMAND)
        else:
            self.gh.edit_labels(pr.number, remove=[lb.merge_hold])
            title = t('release_title')
            text = t('release_text', by=command['by'], ready=lb.ready_to_merge,
                     reason=f': {reason}' if reason else '.')
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
        ``pass``, no open finding, the merge mode of the recomputed receipt, no reviewer whose
        ``trust_policy`` still holds the merge (#434, #461, ``trust_policy_parts``), no work of
        the PR restarted after a conclave (#142, ``_human_merge``), and the conditions
        of the promotion: the approvals of every required reviewer with a matching receipt, or
        at level 0 the checks of a level-0 promotion. When one is missing devloop does not
        merge: what the per-tick checks of a ready PR revoke is left to the next tick, a stale
        receipt or an open finding is handled as at promotion.

        A PR that declares a step on the host (``host_step``, #462) is merged too, but with
        ``[pin] auto`` its merge holds the auto-pin (``AutoPin.hold``, recorded before the merge)
        instead of requesting it, and an alarm with the step goes to ``[run].alert_issue``
        (``_alert_host_step``)."""
        cfg, lb, rp = self.cfg, self.cfg.labels, self.cfg.review_policy
        fresh = self.gh.pr_state(pr.number)
        labels = label_names(fresh.get('labels') or [])
        head = fresh.get('headRefOid') or ''

        def skip(why):
            self.log(f'  not merged: {why}')

        if fresh.get('state') != 'OPEN':
            return skip(f'the PR is {fresh.get("state")}')
        if self._labels_hold_merge(labels):
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
        thread = self._pr_thread(pr.number)
        self._read_thread(pr, thread)
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
        if trust_policy_parts(pr.reviews):
            # A reviewer said the PR touches the trust policy (#434), and no later approval of
            # the same reviewer cleared it (#461): the merge is human.
            return skip('a reviewer said it touches the trust policy: the merge is human')
        unblocked = self._human_merge(pr)
        if unblocked is not None:
            # Work of this PR restarted after a conclave (#142): the merge stays human.
            return skip(f'its work restarted after the conclave decision {unblocked}: the merge '
                        'is human')
        bases = []
        if pr.zero_head is not None:
            # Level 0: the same checks as its promotion, and its labels still in place.
            problem = self._zero_problem(pr)
            if not problem and not (pr.zero_head == head and rp.zero_label in labels
                                    and rp.label not in labels):
                problem = self._merge_texts()('zero_labels_changed')
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
        try:
            self._execute_merge(pr, thread, head, current, mode, paths, bases)
        except MergeHeld as held:
            skip(str(held))
            t = self._merge_texts()
            self.gh.comment(pr.number, publishable(
                dispatcher_header() + f'\n## {t("held_title")}\n\n' + t(
                    'held_text', at=held.at,
                    labels=', '.join(f'`{n}`' for n in sorted(held.labels)),
                    ready=lb.ready_to_merge, needs_operator=lb.needs_operator,
                    hold=lb.merge_hold, needs_supervisor=lb.needs_supervisor)))
        return None

    def _labels_hold_merge(self, labels, delegated=False):
        """Whether the labels ``labels`` of a PR hold its merge: without
        ``agent:ready-to-merge``, or with ``agent:merge-hold`` or ``agent:needs-supervisor``.
        ``agent:needs-operator`` holds an automatic merge, while a ``delegated`` one (#486)
        needs the dispatcher's own, of the ``operator_merge`` the command answers, and is held
        only by its name before #248."""
        lb = self.cfg.labels
        if lb.ready_to_merge not in labels or labels & {lb.merge_hold, lb.needs_supervisor}:
            return True
        if delegated:
            return lb.needs_operator not in labels or LEGACY_NEEDS_OPERATOR in labels
        return bool(labels & lb.operator())

    def _labels_now(self, number, delegated):
        """The time of a new read of the labels of PR ``number``, made now with the API just
        before the merge call (#519), never the read the tick or the merge started from; raises
        ``MergeHeld`` when they hold the merge (``_labels_hold_merge``)."""
        labels = label_names(self.gh.pr_state(number).get('labels') or [])
        at = ledger_timestamp(self.clock)
        if self._labels_hold_merge(labels, delegated):
            raise MergeHeld(labels, at)
        return at

    def _execute_merge(self, pr, thread, head, current, mode, paths, bases, delegation=None):
        """Merge ``head`` of ``pr``, every directive already checked (``_do_merge``, or
        ``_delegated_merge``, #486), with the merger's app, then record it: the ``merged``
        record in the comment of the promotion, the issue closed, the auto-pin requested or
        held. ``delegation``: the command of a delegated merge, ``{command, by, comment,
        reason}``, with ``role`` ``coordinator`` for the coordinator's reserved merge (#529),
        recorded in the ``merged`` record and in its text. Returns whether it
        merged: False when the hold of the auto-pin of a step on the host cannot be recorded.

        Just before the merge call, before the hold of the auto-pin too, the labels of the PR
        are read again with the API (#519): when they hold the merge, as the rules of
        ``_labels_hold_merge`` say, it raises ``MergeHeld`` and nothing is merged nor held. The
        ``merged`` comment gives the time of that read."""
        cfg, lb = self.cfg, self.cfg.labels
        labels_at = self._labels_now(pr.number, delegation is not None)

        # A step on the host declared by the PR (#462) holds the auto-pin before the merge, so
        # that no tick pins the merged base first: a hold that cannot be recorded stops it.
        step = latest_host_step(pr.meta, pr.fixes)
        holding = bool(step) and self.pinner is not None and cfg.pin.auto
        attempt = None  # the merge attempt the hold names (``pin.hold``)
        if holding:
            try:
                attempt = self.pinner.hold(pr.number, head, step, pr.meta.get('issue'))
            except OSError as exc:
                self.log(f'  not merged: it declares a step on the host and the hold of the '
                         f'auto-pin could not be recorded: {exc}')
                return False
        try:
            self.merger.merge_pr(pr.number, head)
        except BaseException:
            if holding:
                # The outcome is unknown: GitHub may have merged and its answer be lost (a
                # timeout). The hold stays, marked uncertain, until a later tick reads the PR
                # (``_settle_host_steps``): it goes if the PR was not merged, else it alerts.
                try:
                    self.pinner.doubt(pr.number, attempt, pr.meta.get('issue'))
                    self.log(f'  the merge failed: the hold of the auto-pin stays until a '
                             f'later tick reads whether #{pr.number} was merged')
                except OSError as exc:
                    self.log(f'  the merge failed and its hold of the auto-pin stays, release '
                             f'it with a pin by hand: {exc}')
            raise
        self.log(f'  merged {head[:12]} at level {current["level"]} in mode {mode}')
        if holding:
            self.log(f'  auto-pin held: #{pr.number} declares a step on the host; pin by hand '
                     'after it')
        elif self.pinner is not None and cfg.pin.auto and cfg.pin.touches(paths):
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
        if delegation is not None:
            payload['delegation'] = dict(delegation)
        # The comment names who wrote to the base (#323): the dispatcher only decides the merge.
        t = self._merge_texts()
        executor = (t('executor_app', app=merged_by.removesuffix('[bot]')) if merged_by
                    else t('executor_gh'))
        common = {'executor': executor, 'head': head[:12], 'level': current['level'],
                  'at': labels_at, 'hold': lb.merge_hold}
        if delegation is None:
            section = (f'## {t("merged_title")}\n\n'
                       + t('merged_text', mode=mode, needs_operator=lb.needs_operator, **common))
        else:
            kind = 'coordinator' if delegation.get('role') == COORDINATOR else 'delegated'
            section = (f'## {t(f"merged_{kind}_title")}\n\n'
                       + t(f'merged_{kind}_text', by=delegation['by'],
                           command=delegation['command'], comment=delegation['comment'],
                           reason=_quoted(delegation['reason']), **common))
        try:
            self._record_merge(pr, thread, head, current, section, payload)
        except GitHubError as exc:
            # The PR is merged: escalating it would only label a closed PR.
            self.log(f'  the merged record could not be written: {exc}')
        self._close_merged_issue(pr, head, mode)
        if step:
            self._alert_host_step(pr, head, step, holding, attempt)
        return True

    def _settle_host_steps(self):
        """The holds of the auto-pin whose merge failed with an unknown outcome (#462,
        ``pin.doubt``): the PR, read on GitHub, says how it went. Merged (the answer was lost),
        the hold stays and the alarm of its step is due (``_alert_held_steps``); not merged, the
        hold goes, and a new merge of that PR holds again. A PR that cannot be read is tried at
        the next tick.
        The settle names the merge attempt of the hold read here, before the state of the PR:
        a new attempt of the same PR that the merge worker makes meanwhile writes a hold of its
        own, which a state read for the earlier attempt never touches."""
        for held in self.pinner.held() or []:
            number = held.get('pr')
            if not held.get('uncertain') or not isinstance(number, int):
                continue
            try:
                state = (self.gh.pr_state(number) or {}).get('state')
            except GitHubError as exc:
                self.log(f'auto-pin: whether #{number} was merged is not known, next tick: '
                         f'{exc}')
                continue
            if state not in ('MERGED', 'OPEN', 'CLOSED'):
                self.log(f'auto-pin: #{number} is {state or "in an unknown state"}: its hold '
                         'stays')
                continue
            merged = state == 'MERGED'
            try:
                if not self.pinner.settle(number, merged, held.get('attempt')):
                    continue  # held again by a new merge attempt meanwhile
            except OSError as exc:
                self.log(f'auto-pin: the hold of #{number} not settled, next tick: {exc}')
                continue
            if not merged:
                self.log(f'auto-pin: #{number} was not merged: its hold of the auto-pin goes')
                continue
            self.log(f'auto-pin: #{number} was merged despite the error: its step on the host '
                     'holds the auto-pin')

    def _alert_held_steps(self):
        """The alarms of the holds of the auto-pin that have not gone out yet (#473,
        ``alerted`` False), tried again at each tick until they do: a comment or a label that
        failed, a process stopped between the merge and its alarm, a merge devloop did not make.
        A hold whose merge is uncertain waits for ``_settle_host_steps``. Any other hold whose
        merge commit is not known yet, alerted or not, reads its PR first: merged, its merge
        commit (``mergeCommit``) is recorded, since only a pin by hand of a commit that contains
        it releases the hold (``pin.release``), and its alarm goes out when due; closed without
        merge, the hold goes (``pin.forget``); open, or not readable, it waits for the next
        tick."""
        for held in self.pinner.held() or []:
            number = held.get('pr')
            if held.get('uncertain') or not isinstance(number, int):
                continue
            due = held.get('alerted') is False
            if not merge_known(held):
                try:
                    fresh = self.gh.pr_state(number) or {}
                except GitHubError as exc:
                    self.log(f'auto-pin: whether #{number} was merged is not known, its alert '
                             f'waits for the next tick: {exc}')
                    continue
                state = fresh.get('state')
                try:
                    if state == 'CLOSED':
                        if self.pinner.forget(number, held.get('attempt')):
                            self.log(f'auto-pin: #{number} was closed without merge: its hold '
                                     'of the auto-pin goes')
                        continue
                    if state != 'MERGED':
                        continue  # a merge that runs now alerts by itself
                    merge = (fresh.get('mergeCommit') or {}).get('oid')
                    if merge_known({'merge': merge}):
                        self.pinner.mark(number, held.get('attempt'), merge=merge)
                        held = dict(held, merge=merge)
                    else:
                        self.log(f'auto-pin: the merge commit of #{number} is not known yet: '
                                 'its hold stays after a pin by hand until it is')
                except OSError as exc:
                    self.log(f'auto-pin: the hold of #{number} not updated, next tick: {exc}')
                    continue
            if not due:
                continue
            head = held.get('head') if isinstance(held.get('head'), str) else ''
            step = held.get('step') if isinstance(held.get('step'), str) and held['step'] \
                else UNREADABLE_STEP
            pr = SimpleNamespace(number=number, meta={'issue': held.get('issue')})
            if held.get('outside'):
                self.log(f'auto-pin: #{number}, merged outside devloop, declares a step on the '
                         'host: it holds the auto-pin')
            self._alert_host_step(pr, head, step, True, held.get('attempt'),
                                  merge=held.get('merge'), outside=bool(held.get('outside')))

    def _merged_host_steps(self, commit):
        """The devloop PRs merged by ``commit`` of the base branch that declare a step on the
        host (#473), for ``AutoPin.scan``: ``[{pr, head, merge, step, issue}]``, with ``merge``
        the merge commit GitHub reports for the PR (``mergeCommit``: for a rebase, its last
        commit, which ``commit`` may not be), or None when it reports none. A PR counts when
        ``_devloop_pr`` recognises it with its marker, and its step is the latest declaration
        (``hoststep.latest``), of the marker or of the fixes its implementer recorded. A PR
        already held is not read again. ``ScanFailed`` when GitHub cannot be read."""
        held = {s.get('pr') for s in self.pinner.held() or []}
        found = []
        try:
            for number in self.gh.commit_prs(commit):
                if number in held:
                    continue
                raw = self.gh.find_pr(number)
                if raw is None or raw.get('state') != 'MERGED':
                    continue
                pr = self._devloop_pr(dict(raw, headRefOid=raw.get('headRefOid') or ''))
                if pr is None or not pr.meta:
                    continue
                fixes = self._markers(pr, self._pr_thread(number), 'fix')
                step = latest_host_step(pr.meta, fixes)
                if step:
                    merge = (raw.get('mergeCommit') or {}).get('oid')
                    found.append({'pr': number, 'head': pr.head_sha, 'step': step,
                                  'issue': pr.issue,
                                  'merge': merge if isinstance(merge, str) else None})
        except GitHubError as exc:
            raise ScanFailed(f'the pull requests merged by {commit[:12]} could not be read: '
                             f'{exc}') from exc
        return found

    def _alert_host_step(self, pr, head, step, holding, attempt=None, merge=None,
                         outside=False):
        """The alarm of a merged PR that declares a step on the host (#462): a comment on
        ``[run].alert_issue`` with the step, and ``agent:needs-operator`` on the issue the PR
        closes. Without a hold (``attempt`` None: ``[pin] auto`` off) one attempt each, and a
        failure is logged. With one (#473), the parts that went out are recorded in it
        (``commented``, then ``alerted`` once both did, or once there was nothing to publish),
        so that a part that failed is tried again at the next tick (``_alert_held_steps``) and
        one that went out is not repeated."""
        with self._host_alert_lock:
            entry = {}
            if attempt is not None:
                entry = next((s for s in self.pinner.held() or []
                              if s.get('pr') == pr.number and s.get('attempt') == attempt),
                             None)
                if entry is None or entry.get('alerted') is not False:
                    return  # released, dropped, or alerted meanwhile
            commented = self._comment_host_step(pr, head, step, holding, merge, outside) \
                if not entry.get('commented') else True
            labeled = self._label_host_step(pr)
            if attempt is None:
                return
            try:
                self.pinner.mark(pr.number, attempt, commented=commented,
                                 alerted=commented and labeled)
            except OSError as exc:
                self.log(f'  the alert of #{pr.number} not recorded in its hold, it may go out '
                         f'again: {exc}')

    def _comment_host_step(self, pr, head, step, holding, merge, outside):
        """The comment of the alarm of ``_alert_host_step``: whether it went out, or there is no
        ``[run].alert_issue`` to publish it on."""
        number = pr.meta.get('issue')
        issue = self.cfg.run.alert_issue
        if not issue:
            self.log('  no [run].alert_issue is configured: no alert for the step on the host')
            return True
        t = self._merge_texts()
        pinning = t('host_held') if holding else t('host_off')
        issue_ref = f', issue #{number}' if isinstance(number, int) else ''
        if outside:
            what = t('host_outside', pr=pr.number, merge=(merge or '')[:12], issue=issue_ref)
        else:
            what = t('host_merged', pr=pr.number, head=head[:12], issue=issue_ref)
        body = publishable(
            dispatcher_header() + f'\n## {t("host_title")}\n\n' + t(
                'host_text', what=what, pinning=pinning,
                step='\n'.join('> ' + line for line in step.splitlines())))
        try:
            self.gh.comment(issue, body)
        except Exception as exc:  # its class, never its output
            self.log(f'  the alert of the step on the host on #{issue} could not be '
                     f'published ({type(exc).__name__})')
            return False
        self.log(f'  alert of the step on the host published on #{issue}')
        return True

    def _label_host_step(self, pr):
        """``agent:needs-operator`` on the issue the PR closes, for ``_alert_host_step``:
        whether it is there, or there is no issue."""
        lb = self.cfg.labels
        number = pr.meta.get('issue')
        if not isinstance(number, int):
            return True
        try:
            self.gh.edit_labels(number, add=[lb.needs_operator])
        except GitHubError as exc:
            self.log(f'  issue #{number}: {lb.needs_operator} not added: {exc}')
            return False
        self.log(f'  issue #{number}: {lb.needs_operator} for the step on the host')
        return True

    def _record_merge(self, pr, thread, head, receipt, section, payload):
        """Write the ``merged`` record ``payload`` of the merge of ``head``, with ``section``,
        its text, in the comment of the promotion it concludes (#338): the latest ``promoted``
        record the dispatcher signed in ``thread``, read just before the merge, for the merged
        head and with no receipt superseded. The comment is edited: the section follows its
        text, with the policy receipt recomputed for the merge only when it reads differently
        from the promotion's, and the ``merged`` record follows its other records. Without such
        a comment, when the edited one would not fit GitHub or when the edit fails, the record
        is a comment of its own, as before #338."""
        policy = self._describe_receipt(receipt)
        alone = dispatcher_header() + '\n' + section + '\n\n' + policy
        promoted = self._dispatcher_records(thread, 'policy', 'promoted')
        if promoted:
            position, record = promoted[-1]
            comment = thread[position]
            body = comment.get('body') or ''
            carried = read_marker(body, 'receipt')
            if (comment.get('id') is not None and record['receipt']['head'] == head
                    and (carried is None or carried['state'] == 'current')):
                text = split_markers(body)[0] + '\n\n' + section
                if policy != self._describe_receipt(record['receipt']):
                    text += f'\n\n{self._merge_texts()("recomputed")}\n\n{policy}'
                edited = with_record(body, 'merged', payload, text=text)
                try:
                    if len(edited) <= MAX_BODY:
                        self.gh.edit_comment(comment['id'], edited)
                        self.log('  merged record added to the comment of the promotion')
                        return
                except GitHubError as exc:
                    self.log(f'  the comment of the promotion could not be edited: {exc}')
        self.gh.comment(pr.number, publishable(alone, 'merged', payload))

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
            t = self._merge_texts()
            self.gh.comment(number, publishable(
                dispatcher_header() + f'\n## {t("closed_title")}\n\n' + t(
                    'closed_text', pr=pr.number, head=head[:12], mode=mode,
                    commit=f'`{commit[:12]}`' if commit else t('not_read')),
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
        reason = self._merge_texts()('merged_issue', ready=lb.ready, listed=listed)
        supervised, why = self._supervised(number, MERGED_ISSUE, 'implement')
        # From a refused retry the issue is in agent:needs-supervisor: it leaves it only for
        # the operator.
        remove = [lb.ready] if supervised else [lb.ready, lb.needs_supervisor]
        self._escalate(number, reason, remove=remove, cls=MERGED_ISSUE,
                       supervised=supervised, why=why)

    # Delegated merge (#486) ------------------------------------------------------------
    def _merge_budget_extensions(self, day):
        """The extensions of the cap of the delegated merges of the UTC ``day`` (#486): the
        dispatcher's ``supervision`` records of a ``merge-budget`` it granted for that day on
        ``[report] issue``, as ``[{n, by, reason, comment}]``. None without the issue; when it
        cannot be read, none (logged): the cap of the configuration applies, never more."""
        issue = self.cfg.report.issue
        if not issue:
            return []
        try:
            comments = self.gh.issue_comments(issue)
        except GitHubError as exc:
            self.log(f'  the extensions of the delegated merges on #{issue} cannot be read: '
                     f'{exc}')
            return []
        found = []
        for _, record in self._dispatcher_records(comments, 'supervision'):
            n = record.get('value')
            if (record.get('command') == MERGE_BUDGET and record.get('outcome') == 'granted'
                    and record.get('day') == day and isinstance(n, int)
                    and not isinstance(n, bool) and 1 <= n <= MAX_MERGE_BUDGET):
                found.append({'n': n, 'by': record.get('by'), 'reason': record.get('reason'),
                              'comment': record.get('comment')})
        return found

    def _trusted_comment(self, number, comment, seen):
        """Whether comment ``comment`` of issue or PR ``number`` of the repository is by a user
        of ``trusted_users`` (#486), read with the API now; ``seen`` caches the comments read.
        A comment that cannot be read is not: an issue that does not exist (404), or a comment
        id that is not among its comments in the configured repository, as for a link under a
        former name of the repository (``repo_aliases``, #540) to a comment of another one."""
        if number not in seen:
            try:
                seen[number] = {c.get('id'): (c.get('author') or {}).get('login')
                                for c in self.gh.issue_comments(number)}
            except GitHubError as exc:
                self.log(f'  the comments of #{number} cannot be read: {exc}')
                seen[number] = {}
        return seen[number].get(comment) in self.cfg.trusted_users

    @staticmethod
    def _quorum_records(reviews, reviewers, head):
        """``{reviewer: record}``: the latest review of ``head`` of each of ``reviewers`` (the
        quorum of the receipt), from ``reviews``; a reviewer without one is missing."""
        found = {}
        for record in reviews:
            owner = owner_of(record)
            if owner in reviewers and record.get('head_sha') == head \
                    and record.get('source') == 'agent':
                found[owner] = record
        return found

    def _delegated_merge(self, target, number, comments, command, trace):
        """Apply ``/devloop merge <sha> <reason>`` of the supervisor's identity on PR
        ``number``, whose merge is the operator's (#486, ADR 0005, precision of 2026-11-21).
        ``comments``: the PR's thread read now. Every condition is verified now, mechanically,
        on ``sha``; when one does not hold, or cannot be verified, the command is refused with
        why, spent, and the PR stays with the operator (it fails closed):

        - ``[delegations] merge`` on, merge mode ``auto`` with a supervisor, and a command with
          the full ``sha`` and a reason;
        - the PR open, in ``agent:ready-to-merge`` with the dispatcher's own
          ``agent:needs-operator`` of an ``operator_merge`` (the record the command answers),
          without ``agent:merge-hold`` nor ``agent:needs-supervisor``, and no ``/devloop hold``
          of the supervisor or a trusted user written after the command;
        - ``sha`` the PR's head and the head the latest round approved, with the approvals of the
          quorum matching the receipt recomputed now, no open finding, an acceptance map that
          holds, ``MERGEABLE`` and ``CLEAN``, and the CI ``pass``;
        - each reviewer of the quorum, on its latest review of ``sha``, with ``operator_reason``
          ``none`` or ``decision``, never ``trust_policy``, and no ``trust_policy`` that still
          holds the merge (``trust_policy_parts``);
        - an advisory review of ``sha`` (#369) with the opinion ``ok`` or ``dubbi``;
        - no ``host_step``;
        - no path of the trust rule nor of the delegations (``supervision.rule_paths``), no work
          restarted after a conclave, and the merge mode of the PR without its paths under
          ``docs/adr/`` ``auto``: every other operator-only path, or a level ``off``, stays the
          operator's;
        - under ``docs/adr/`` only added lines, each block of them with the link to a comment
          of this repository by a trusted user, read with the API (``_adr_problem``);
        - the daily cap of the delegated merges not reached: ``max_merges_per_day`` plus the
          extensions of the day (``_merge_budget_extensions``), checked and recorded in the
          turn journal in one step (``TurnJournal.delegated_merge``).

        Then the merge, as an automatic one (``_execute_merge``), whose ``merged`` record
        carries the command and its reason, and last the ``supervision`` record ``merged``,
        which spends the command.

        From the coordinator's identity (``[roles.coordinator]``) the command is the
        coordinator's reserved merge (#529, ADR 0002, precision of 2026-11-22), under the same
        conditions but these:

        - neither ``[delegations] merge`` nor the merge mode, of the configuration or of the
          PR, counts: the PR waits for the operator's merge for whatever reason;
        - the merger's app (``[apps.merger]``) is required: without it the command is refused
          before the cap is touched, and nothing merges with the ambient ``gh`` login;
        - the paths of the trust rule and of the delegations, ``docs/adr/`` (with any change,
          not only added lines) and a ``host_step`` refuse nothing: those are the coordinator's
          cases. A step on the host holds the auto-pin, as for every merge;
        - a ``trust_policy`` of the reviewers refuses nothing by itself; but when a reviewer of
          the PR declared the trust policy touched (``trust_policy``, in any round) or the diff
          touches ``docs/adr/``, the issue the PR closes needs a valid ``/devloop approve`` of
          the operator (``_operator_approval``), and each reviewer of the quorum keeps
          ``operator_reason`` ``none`` or ``decision`` on ``sha``, as above;
        - the daily cap is its own, ``[roles.coordinator] max_merges_per_day`` (at most
          ``MAX_COORDINATOR_MERGES``), with no extension, recorded in the turn journal as a
          ``coordinator_merge`` (``TurnJournal.delegated_merge``);
        - work restarted after a conclave refuses nothing by itself (#624, ADR 0002, precision
          of 2026-11-24): the decision that restarted it must be ``UNANIMOUS`` and still
          valid, not vetoed nor lapsed, and a complete round after the restart must have
          approved ``sha`` with the quorum (``_conclave_merge_problem``). devloop's own merge
          of such a PR stays excluded (``_do_merge``).

        No decision of the operator on the PR itself is needed (#624): the coordinator merges
        the reserved PRs that meet every condition."""
        cfg, lb, rules = self.cfg, self.cfg.labels, self.cfg.delegations
        t = self._merge_texts()
        by = command['by']
        coordinator = self._is_coordinator(by)
        stays = t('merge_stays', label=lb.needs_operator)

        def refuse(why):
            return trace('refused', t('merge_refused', why=why, stays=stays))

        if coordinator:
            if cfg.coordinator.max_merges_per_day < 1:
                return refuse(t('coordinator_off'))
            if not self._app_login(MERGER):
                # Executed by the merger's app (ADR 0002), never with the ambient gh login.
                return refuse(t('no_merger'))
        elif not rules.merge:
            return refuse(t('delegation_off'))
        elif cfg.merge.mode != 'auto' or not cfg.supervisor:
            return refuse(t('mode_not_auto', mode=cfg.merge.mode))
        args = merge_arguments(command['reason'])
        if args is None:
            return refuse(t('syntax'))
        sha, reason = args
        anchor = command['anchor']
        if anchor.get('kind') != 'notice' or anchor['record'].get('event') != 'operator_merge':
            return refuse(t('not_operator_merge'))
        fresh = self.gh.pr_state(number)
        labels = label_names(fresh.get('labels') or [])
        head = fresh.get('headRefOid') or ''
        if fresh.get('state') != 'OPEN':
            return refuse(t('not_open', state=fresh.get('state')))
        if sha != head:
            return refuse(t('sha_not_head', sha=sha[:12], head=head[:12]))
        if self._labels_hold_merge(labels, delegated=True):
            return refuse(t('labels_not_operator',
                            labels=', '.join(f'`{n}`' for n in sorted(labels))))
        pr = self._fresh_pr(target, labels, fresh, comments)
        if not pr.operator_owned:
            return refuse(t('not_owned', label=lb.needs_operator))
        position = next((i for i, c in enumerate(comments) if c.get('id') == command['comment']),
                        None)
        if position is None:
            return refuse(t('comment_gone'))
        held = [c for c in comments[position + 1:]
                if not carries_record(c.get('body') or '')
                and (merge_command(c.get('body')) or ('',))[0] == 'hold'
                and (self._may_hold((c.get('author') or {}).get('login'))
                     or self._coordinator_may((c.get('author') or {}).get('login'), 'hold'))]
        if held:
            return refuse(t('held_after'))
        if pr.finding_problems:
            return refuse(t('owners_unknown'))
        if pr.stale_receipt:
            return refuse(t('stale_receipt'))
        left = open_findings(pr.reviews, pr.closures)
        if left:
            return refuse(t('open_findings', keys=', '.join(f'`{f["key"]}`' for f in left)))
        latest = current_round(pr.reviews)
        if not (latest and not pending_reviewers(latest) and round_verdict(latest) == 'approve'
                and all(r['head_sha'] == sha for r in latest)):
            return refuse(t('not_approved', sha=sha[:12]))
        if (fresh.get('mergeable'), fresh.get('mergeStateStatus')) != ('MERGEABLE', 'CLEAN'):
            return refuse(t('not_clean', mergeable=fresh.get('mergeable'),
                            status=fresh.get('mergeStateStatus')))
        ci, _ = self._read_ci(head)
        if ci != 'pass':
            return refuse(t('ci_not_green', ci=ci))
        if self._acceptance_problems(pr):
            return refuse(t('acceptance'))
        try:
            current, paths = self._receipt_and_paths(pr)
        except (GitHubError, WorkspaceError) as exc:
            return refuse(t('receipt_unavailable', error=exc))
        if current['head'] != head:
            return refuse(t('pushed_head', head=current['head'][:12]))
        touched = [] if coordinator else rule_paths(paths, rules.merge_trust_paths)
        if touched:
            return refuse(t('trust_paths', paths=', '.join(
                f'`{p}`' for p in touched[:MAX_LISTED_PATHS])))
        unblocked = self._human_merge(pr)
        if unblocked is not None and not coordinator:
            return refuse(t('conclave_restart', decision=unblocked))
        adr = adr_paths(paths)
        mode = cfg.merge.mode_for(current['level'], [p for p in paths if p not in adr])
        if mode != 'auto' and not coordinator:
            return refuse(t('mode_outside_adr', adr=ADR_DIR, mode=mode))
        if latest_host_step(pr.meta, pr.fixes) and not coordinator:
            return refuse(t('host_step'))
        if current['level'] == ZERO_LEVEL or len(current['reviewers']) < current['level']:
            return refuse(t('no_quorum'))
        quorum = self._quorum_records(pr.reviews, current['reviewers'], sha)
        missing = [k for k in current['reviewers'] if k not in quorum]
        uncovered = [k for k, r in quorum.items()
                     if r.get('operator_reason') not in (None, 'none', DECISION)]
        if missing or uncovered or (trust_policy_parts(pr.reviews) and not coordinator):
            return refuse(t('uncovered', what=t(
                'missing_review', keys=', '.join(f'`{k}`' for k in missing), sha=sha[:12])
                if missing else t('trust_declared')))
        if unblocked is not None:
            # The coordinator's merge after a unanimous conclave decision (#624).
            problem = self._conclave_merge_problem(pr, comments, latest, quorum)
            if problem:
                return refuse(problem)
        advisory = self._advisory_record(comments, sha)
        opinion = (advisory or {}).get('opinion')
        if opinion not in MERGE_OPINIONS:
            return refuse(t('advisory_missing', sha=sha[:12]) if advisory is None
                          else t('advisory_opinion', sha=sha[:12], opinion=opinion) if opinion
                          else t('advisory_none', sha=sha[:12]))
        approval = None
        if coordinator:
            # The operator's decision, for the trust policy and the ADRs (#529).
            trust = any(trust_policy(r) for r in pr.reviews)
            if trust or adr:
                problem, approval = self._operator_approval(pr, trust, adr)
                if problem:
                    return refuse(problem)
        elif adr:
            problem = self._adr_problem(current, adr)
            if problem:
                return refuse(problem)
        bases = self._approvals_match(pr, latest, current, paths, step='merge')
        if bases is None:
            return trace('refused', t('approvals_stale'))
        journal = getattr(self.ledger, 'journal', None)
        day = ledger_timestamp(self.clock)[:10]
        if coordinator:
            # A cap of its own, with no extension (#529).
            cap, event, what = (cfg.coordinator.max_merges_per_day, COORDINATOR_MERGE,
                                t('what_coordinator'))
            limit = t('limit_coordinator', cap=cap, max=MAX_COORDINATOR_MERGES)
        else:
            extensions = self._merge_budget_extensions(day)
            cap = rules.max_merges_per_day + sum(e['n'] for e in extensions)
            event, what = DELEGATED_MERGE, t('what_delegated')
            limit = t('limit_delegated', cap=rules.max_merges_per_day)
        try:
            daily = (None if journal is None else
                     journal.delegated_merge(number, command['comment'], by, head, cap, event))
        except JournalError as exc:
            self.log(f'  the {event.replace("_", " ")} cannot be recorded: {exc}')
            journal = None
        if journal is None:
            return refuse(t('journal_unavailable', what=what))
        if daily is None:
            return refuse(t('daily_cap', what=what, cap=cap, limit=limit))
        delegation = {'command': MERGE, 'by': by, 'comment': command['comment'],
                      'reason': reason}
        if coordinator:
            delegation['role'] = COORDINATOR
        try:
            merged = self._execute_merge(pr, comments, head, current, 'auto', paths, bases,
                                         delegation)
        except MergeHeld as held:
            self.log(f'  not merged: {held}')
            return refuse(t('labels_now', at=held.at,
                            labels=', '.join(f'`{n}`' for n in sorted(held.labels))))
        except GitHubError as exc:
            return refuse(t('merge_failed', error=exc))
        if not merged:
            return refuse(t('merge_not_run'))
        figures = {'head': head, 'daily': daily, 'daily_cap': cap,
                   'daily_left': cap - daily, 'opinion': opinion}
        quoted = _quoted(reason)
        if coordinator:
            if approval is not None:
                figures['approval'] = approval.get('id')
            if unblocked is not None:
                figures['conclave'] = unblocked
            decided = t('with_approval', issue=pr.meta['issue'], link=(
                comment_link(approval) or t('approval_comment', id=approval.get('id')))
                ) if approval is not None else ''
            restarted = (t('after_conclave', decision=unblocked) if unblocked is not None
                         else '')
            trace('merged', t('merged_by_coordinator', head=head[:12], opinion=opinion,
                              approval=decided, conclave=restarted, daily=daily, cap=cap,
                              reason=quoted), **figures)
            return None
        trace('merged', t('merged_by_delegation', head=head[:12], opinion=opinion, daily=daily,
                          cap=cap, reason=quoted), **figures)
        return None

    def _operator_approval(self, pr, trust, adr):
        """``(problem, approval)`` of the coordinator's reserved merge of ``pr`` (#529), whose
        reviewers declared the trust policy touched (``trust``) or whose diff touches the paths
        ``adr`` under ``docs/adr/``: the latest valid ``/devloop approve`` of the operator on
        the issue the PR closes (``_issue_decisions``, read now with the API, as a review reads
        it), and '' for ``problem``; or why there is none, and None. What cannot be read
        refuses it."""
        t = self._merge_texts()
        what = t('approval_and').join(part for part in (
            t('approval_trust') if trust else '',
            t('approval_adr', adr=ADR_DIR) if adr else '') if part)
        issue = pr.meta.get('issue') if pr.meta else None
        if not isinstance(issue, int) or isinstance(issue, bool):
            return t('approval_no_issue', what=what), None
        try:
            history = self.gh.issue_history(issue)
            comments = self.gh.issue_comments(issue)
        except GitHubError as exc:
            return t('approval_unreadable', what=what, issue=issue, error=exc), None
        decisions = self._issue_decisions(comments, history, taken=True)
        if not decisions:
            return t('approval_none', what=what, issue=issue), None
        return '', decisions[-1]

    def _adr_problem(self, current, adr):
        """Why the changes of ``adr``, the paths of the PR under ``docs/adr/``, do not only
        record decisions of the operator already written on GitHub (#486), or ''. Only added
        lines, as plain text, each block of them with the link to a comment of this repository
        whose author is in ``trusted_users``, read now with the API. What cannot be read
        refuses it."""
        t = self._merge_texts()
        try:
            files = self.ws.diff_blocks(current['base'], current['head'], ADR_DIR)
        except WorkspaceError as exc:
            return t('adr_unreadable', adr=ADR_DIR, error=exc)
        seen = {entry['path'] for entry in files}
        if any(p not in seen for p in adr):
            return t('adr_not_text', adr=ADR_DIR)
        # A link under a former name of the repository (``repo_aliases``, #540) is checked as
        # any other: its comment read by id among those of its issue in ``repo``.
        problems, blocks = adr_problems(files, self.cfg.repo, self.cfg.repo_aliases,
                                        self.cfg.language)
        if problems:
            return t('adr_problems', adr=ADR_DIR,
                     problems='; '.join(problems[:MAX_LISTED_PATHS]))
        read = {}
        for path, links in blocks:
            if not any(self._trusted_comment(n, c, read) for n, c in links):
                return t('adr_untrusted', path=path, links=', '.join(
                    f'#{n} ({c})' for n, c in links[:MAX_LISTED_PATHS]))
        return ''
