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
from .i18n import Catalog
from .journal import OPERATOR_ROUNDS, JournalError
from .ledger import entries as ledger_entries
from .markdown import (carries_record, operator_retry_text, preflight_resume, read_marker,
                       render_escalation, render_supervision, resume_text)
from .planning import (FIX_COMMAND, Action, PullRequest, closed_by_first, findings_decreasing,
                       label_names, last_counted_ci, missing_verdicts, operator_blocked,
                       waiver)
from .policy import ZERO_LEVEL, profile_of
from .review import COORDINATOR as CLOSED_BY_COORDINATOR
from .review import (FINDING_COMMAND, current_round, current_verdict, finding_commands,
                     finding_state, flow_verdict, owner_of, trust_policy_findings)
from .supervision import (ACCEPT_DEFAULTS, BUDGET_GRANTABLE, CAPACITY, CAPACITY_REFUSED, DECIDE,
                          DESIGN_LABEL, ESCALATE, FINDING, GRANT_BUDGET, GRANT_ROUND, ISSUE_STEPS,
                          MAX_CAPACITY, MAX_CAPACITY_HOURS, MAX_COORDINATOR_OPERATOR_ROUNDS,
                          MAX_MERGE_BUDGET,
                          MAX_RETRIES, MERGE, MERGE_BUDGET, MERGED_ISSUE, OPEN_FINDINGS,
                          PREFLIGHT_DEFAULT, REREVIEW, RETRY, RETRY_STEPS,
                          RETRY_CLASSES, ROUND_GRANTABLE, TOOL_FAILURE, capacity_arguments,
                          changed_input, command_line, coordinator_command,
                          coordinator_refusal, critical,
                          merge_budget_arguments, operator_rounds_text, preflight_class,
                          retries_done, supervisor_command)
from .workspace import WorkspaceError

# The fields of a turn the budget envelope refused (#411) that a ``budget`` escalation to the
# operator records (#595): the story, the kind and the agent of the turn, for the coordinator's
# extension of the envelope (``_operator_grant_budget``), and the refusal's category.
BUDGET_FIELDS = ('story', 'kind', 'agent', 'category')


def budget_fields(source):
    """The ``BUDGET_FIELDS`` of ``source`` that are strings: a ``BudgetRefused`` or the record
    of an escalation (#595)."""
    values = (source.get(k) if isinstance(source, dict) else getattr(source, k, None)
              for k in BUDGET_FIELDS)
    return {k: v for k, v in zip(BUDGET_FIELDS, values) if isinstance(v, str)}


# Hold and release a merge in ``auto`` mode (#172): the first line of a comment, after the
# attribution header, from the supervisor's identity or a trusted user.
HOLD_COMMAND = '/devloop hold'
RELEASE_COMMAND = '/devloop release'
# The label a PR resumes from when the supervisor retries a step that failed on it (#182).
RESUME_STEP = {'review': 'review', 'fix': 'changes', 'sync': 'review'}
# The texts of the escalations and of the answers to the commands (#633), in the project's
# ``language`` (``i18n``); English for a language without translation. Commands, labels, classes,
# keys and the fields of the ``supervision`` records are never translated, and no text here is
# read back: the records are read through their markers. The titles of the traces: of a command
# on an escalation to the supervisor (#182) or to the operator (#321), of a ``/devloop capacity``
# (#412), of a delegated merge and of an extension of its daily cap (#486), of the coordinator's
# reserved merge (#529). The reasons of a refusal are composed in the same language, and so are
# those that ``supervision``, ``budget`` and ``journal`` give.
_KEYS = ' or '.join(f'`{key}`' for key in MAX_CAPACITY)
_CHIAVI = ' o '.join(f'`{key}`' for key in MAX_CAPACITY)
TEXTS = Catalog(
    en={
        # Titles and usage.
        'supervisor_title': 'Decision of the supervisor',
        'operator_title': 'Command on an escalation to the operator',
        'capacity_title': 'Temporary capacity limit',
        'merge_title': 'Merge delegated to the supervisor',
        'coordinator_merge_title': 'Reserved merge of the coordinator',
        'merge_budget_title': 'Delegated merges: extension of the daily cap',
        'capacity_usage': (f'The command is written as `/devloop {CAPACITY} <key> <value> '
                           '<duration> <reason>`, as the first line of a comment on this issue: '
                           f'the key is {_KEYS}, the duration `<N>h` or `<N>m`, the reason, '
                           'which may go on in the following lines, gives or cites the analysis '
                           'of the causes.'),
        'merge_budget_usage': (f'The command is written as `/devloop {MERGE_BUDGET} <N> '
                               '<reason>`, as the first line of a comment of a trusted user on '
                               f'this issue: `N` from 1 to {MAX_MERGE_BUDGET} more delegated '
                               'merges for the UTC day it is written in, the reason required.'),
        # Pieces.
        'trace': '`/devloop {name}` by `{by}`: {text}',
        'the_pr': 'the PR',
        'the_issue': 'the issue',
        'The_pr': 'The PR',
        'The_issue': 'The issue',
        'or': ' or ',
        'reason_arg': '<reason>',
        'decisions_arg': '<decisions>',
        'text_arg': '<text>',
        'reason_title': '**Reason:**',
        'causes_title': '**Reason and analysis of the causes:**',
        'resolved': 'resolved',
        'withdrawn': 'withdrawn',
        'of_code': 'of the code',
        'of_delegation': 'of the delegation',
        # Outcomes.
        'refused': 'refused: {why}.',
        'not_applicable': 'not applicable: {why}. {how}',
        'spent': 'The command is spent; {stays}',
        'item_stays': '{where} stays in `{label}`.',
        'escalation_stays': 'the escalation stays with the supervisor.',
        # Why an escalation stays with the operator (``_supervised``).
        'why_step': 'the step `{step}` is not among those the supervisor may run again',
        'why_marker': ('the body of the PR no longer ends with its marker: who implemented it '
                       'and its merge mode cannot be verified'),
        'why_own_budget': ('the supervisor implemented this PR, and the grant of budget is not '
                           'among the escalations of process it decides on the PRs of its own '
                           'agent too'),
        'why_mode': ('the merge mode of this PR is `{mode}`, not `auto` (override of the level '
                     'or `operator_only_paths`)'),
        'not_computable': 'not computable',
        'why_author': ('issue #{issue} was opened by `{author}`, who is neither a trusted user '
                       'nor an app of devloop'),
        'why_design': 'the label `{label}` is present',
        'why_key': 'the input of the step was not recorded before it ran',
        'why_unverifiable': 'it could not be verified that the escalation is not critical ({error})',
        'why_operator': ('In merge mode `auto` this escalation would go to the supervisor, but '
                         'it stays with the operator: {why}.'),
        'why_round': ('In merge mode `auto` the supervisor could grant another round, but the '
                      'escalation stays with the operator: {why}.'),
        'preflight_defaults': ('The readiness check found only gaps with a proposed default '
                               '(`default_available`): the supervisor may accept them with '
                               '`/devloop {accept}`, or decide otherwise with `/devloop {decide} '
                               '<decisions>`.'),
        'budget_grantable': ('The supervisor may extend the envelope of this story once, in turns '
                             'and time only, with `/devloop grant-budget <reason>`, where the '
                             'reason gives or cites the analysis of the causes: {extension}.'),
        # Commands not applicable (``_supervisor_view``).
        'missing_text': ('the text is missing, the command is written as `/devloop {command} '
                         '{arguments}`'),
        'operator_escalation': ('the escalation is entrusted to the operator, not to the '
                                'supervisor: a trusted user decides'),
        'not_auto': ('the merge mode is `{mode}`, not `auto`, so the supervisor\'s commands do '
                     'not apply'),
        # Commands on an escalation to the supervisor (``_do_supervise``).
        'only_merge': ('applies only to a ready PR whose merge is the operator\'s, not to an '
                       'escalation to the supervisor'),
        'only_finding': 'applies only to an `open_findings` escalation to the operator',
        'only_report': ('applies only to the issue of the summary (`[report] issue`), not to an '
                        'escalation'),
        'refused_critical': ('refused. The escalation is critical (class `{cls}`) or not '
                             'entrusted to the supervisor: the operator decides.'),
        'to_operator_critical': ('The supervisor cannot decide this escalation (class `{cls}`): '
                                 '`/devloop {name}` of `{by}` was refused.'),
        'escalated': 'the escalation goes to the operator: {reason}',
        'to_operator_escalated': ('The supervisor `{by}` hands the escalation of class `{cls}` '
                                  'to the operator: {reason}'),
        'only_round_grantable': ('it applies only to a `{cls}` escalation of a PR at the round '
                                 'limit'),
        'only_budget_grantable': ('it applies only to a `{cls}` escalation of a turn refused by '
                                  'the budget envelope'),
        'only_preflight_default': ('it applies only to a `preflight_default` escalation of the '
                                   'readiness check'),
        'only_retry': ('it applies only to the `tool_failure`, `sync_failed` and `push_failed` '
                       'escalations'),
        'only_rereview': 'it applies only to a `tool_failure` escalation of the review of a PR',
        'merged_why': ('the issue is already resolved by a merged devloop PR ({merged}), so '
                       'running its step again would duplicate the work'),
        'merged_spent': 'The command is spent; the issue goes to a `merged_issue` escalation.',
        'merged_resume': ('If it is resolved it should be closed; if more work is needed, a new '
                          'issue should be opened.'),
        'not_retryable': 'the escalation does not record a step that can be run again',
        'to_operator_not_retryable': ('The escalation does not record a step the supervisor may '
                                      'run again: `/devloop {name}` of `{by}` was refused.'),
        'superseded': ('the input of the step changed after the escalation ({changed}), so the '
                       'command refers to a superseded input'),
        'superseded_grant': ('the input of the step changed after the escalation ({changed}), so '
                             'the grant refers to a superseded input'),
        'no_longer_auto': 'the merge mode is no longer `auto` with a supervisor',
        'became_critical': 'refused: the escalation became critical ({why}).',
        'to_operator_became': ('The supervisor can no longer decide this escalation: {why}. '
                               '`/devloop {name}` of `{by}` was refused.'),
        'limit': ('not run: the step `{step}` was already run again {done} times for the same '
                  'input.'),
        'to_operator_limit': ('The step `{step}` failed again after {done} attempts authorized '
                              'by the supervisor (at most {max}): `/devloop {name}` of `{by}` '
                              'was not run.'),
        # The supervisor's rereview (#491).
        'rereview_asked': ('in the latest round a reviewer asked for a decision of the operator '
                           '({asked}), and a new review would override it'),
        'rereview_no_round': ('no review round of this head waits for the verdict of a reviewer '
                              'of the quorum: `/devloop {retry}` already opens a round with the '
                              'current configuration'),
        'rereview_again': ('after the latest round with a verdict a new review was already '
                           'asked, by a trusted user or by the supervisor, and its round wrote '
                           'no verdict: another one would repeat it without counting for '
                           '`max_review_rounds`'),
        'rereview_first': ('the latest round was closed by the first reviewer of the quorum, who '
                           'did not approve ({verdicts}): with `second_reviewer = "{second}"` '
                           'the second reviewer was not asked and no verdict is missing, and a '
                           'new review would override its own'),
        'rereview_all': ('every reviewer of the quorum of the latest round already has a verdict '
                         '({verdicts}), and a new review would override it'),
        'rereview_applied': ('a new review round opens in place of round {round}, which lacks '
                             'the verdict of {reviewers}. The new round chooses the reviewers '
                             'with the current configuration, has a new receipt and counts for '
                             '`max_review_rounds`; the merge stays where its mode puts it.'),
        'rereview_operator': ('the escalation is entrusted to the operator: the supervisor may '
                              'ask for a new review there only after a `tool_failure` of the '
                              'review of a PR'),
        'not_supervisors': 'the escalation is not the supervisor\'s ({why})',
        'no_supervisor': 'no supervisor configured',
        # Grants of rounds (#405, #553) and of budget (#411, #595).
        'cap_pr': ('{granted} rounds were already granted to this PR, the maximum of the '
                   'delegation (`[delegations] max_rounds_per_pr` = {cap})'),
        'grants_unknown': ('today\'s grants cannot be verified: the journal of the turns is not '
                           'available or not readable'),
        'cap_day': ('{today} grants were already made today, the daily maximum (`[delegations] '
                    'max_grants_per_day` = {cap})'),
        'owners': 'the owner of some findings cannot be rebuilt',
        'security': ('security findings stay open, or findings that do not say whether they '
                     'are: {keys}'),
        'no_progress': ('the latest counted round is not a failed CI and the open blocking '
                        'findings are not fewer than those of the round before'),
        'rounds_why': 'the delegation of the rounds does not apply: {why}',
        'head_moved': ('the head of the PR changed after the escalation, so the grant refers to '
                       'a superseded input'),
        'to_operator_round': ('The supervisor cannot grant another round to this PR: {why}. '
                              '`/devloop {command}` of `{by}` was refused.\n\n{rounds}'),
        'rounds_off': ('the delegation of the rounds is off (`[delegations] rounds`), or the '
                       'merge mode is no longer `auto` with a supervisor'),
        'daily_reached': ('meanwhile today\'s grants reached the daily maximum (`[delegations] '
                          'max_grants_per_day` = {cap})'),
        'round_granted': ('one more round granted to this PR: its limit becomes {limit} rounds '
                          '(`max_review_rounds` = {max}, plus {total} granted). Rounds granted '
                          'to this PR by the delegation: {granted} of {cap}; grants of today: '
                          '{daily} of {daily_cap}. The PR goes back to `{resume}`.'),
        'turn_unknown': ('the refused turn cannot be rebuilt: its agent (`{agent}`) is not '
                         'configured, or the kind of the turn is missing'),
        'journal_unreadable': 'the journal of the turns is not readable ({error})',
        'budget_why': 'the delegation of the budget does not apply: {why}',
        'to_operator_budget': ('The supervisor cannot extend the budget envelope of this story: '
                               '{why}. `/devloop {command}` of `{by}` was refused.\n\nThe values '
                               'of the envelope and the extensions beyond the one delegated to '
                               'the supervisor stay decisions of the operator (ADR 0004, D2).'),
        'budget_off': 'the delegation of the budget is off (`[delegations] budget`)',
        'no_turn_step': 'the escalation does not record the step of the refused turn',
        'other_story': ('the story of the work (`{story}`) is not the one of the escalation '
                        '(`{expected}`)'),
        'story_spent': 'meanwhile the story `{story}` already had its one extension',
        'extension_lapsed': ('the extension of this command already lapsed, because the first '
                             'turn after the grant was refused all the same: a new grant is '
                             'needed'),
        'stays_there': '{what} stays in `{held}`{how}: the step `{step}` does not resume from here.',
        'where_operator': ', where the operator decides as before',
        'where_latest': ', with its latest escalation',
        'pr_back': 'The PR goes back to `{label}`.',
        'merged_after': ('The issue is however already resolved by a merged devloop PR '
                         '({merged}): the step `{step}` does not resume and the issue goes to a '
                         '`merged_issue` escalation.'),
        'step_now': 'The step `{step}` of the issue resumes now, as after a retry.',
        'recovered': ('A previous run stopped after recording the extension in the journal of '
                      'the turns: here its trace is completed, without applying it again. '
                      '{where}'),
        'extended': ('the caps of the budget envelope of the story `{story}` are extended: '
                     '{extension} ({percent}% of the values fixed at its first booking, rounded '
                     'down; no dollar nor token). It is the one extension of the story: another '
                     'refusal goes to the operator. Grants of the day: {daily} of {cap}. {where}'),
        # Capacity limits (#412) and delegated merges (#486) on the issue of the summary.
        'capacity_refuse': ('not applicable: {why}. The command is spent; the capacity limits '
                            'stay those of before.\n\n{usage}'),
        'report_only': ('on the issue of the summary only `/devloop {capacity}` and `/devloop '
                        '{merge_budget}` apply'),
        'capacity_off': ('the delegation of the capacity limits is off (`[delegations] '
                         'capacity`): only the operator changes the limits'),
        'capacity_malformed': ('the command is not complete or not well formed: key, integer '
                               'value, duration and reason are needed'),
        'capacity_refused_key': ('`{setting}` is not delegated: devloop applies it only when '
                                 '`devloop run` restarts, so only the operator changes it'),
        'capacity_unknown_key': '`{setting}` is not among the keys of the delegation',
        'value_code': ('the value {value} is out of the limits of the code: `{setting}` goes '
                       'from 1 to {cap} (cap of the code)'),
        'value_delegation': ('the value {value} is out of the limits of the delegation: '
                             '`{setting}` goes from 1 to {cap} (`[delegations] '
                             'capacity_{setting}`)'),
        'hours_code': 'the duration exceeds the maximum of {hours} hours (cap of the code)',
        'hours_delegation': ('the duration exceeds the maximum of {hours} hours (`[delegations] '
                             'capacity_max_hours`)'),
        'time_expiry': ('the time of the command cannot be read, so the expiry cannot be '
                        'verified'),
        'elapsed': ('the duration, counted from when the command was written ({written}), is '
                    'already over'),
        'capacity_daily': ('today\'s grants already reached the daily maximum (`[delegations] '
                           'max_grants_per_day` = {cap})'),
        'capacity_granted': ('`{setting}` is {value} until {until} (configured value: '
                             '{configured}; cap {of}: {cap}). At the expiry the configured value '
                             'comes back; '
                             'another `/devloop {capacity}` on the same key replaces this one. '
                             'Grants of today: {daily} of {daily_cap}.'),
        'merge_budget_refuse': ('not applicable: {why}. The command is spent; the cap of the '
                                'delegated merges stays the one of before.\n\n{usage}'),
        'merge_budget_trusted': ('only a trusted user (`trusted_users`) extends the cap of the '
                                 'delegated merges, never the supervisor nor an app'),
        'merge_off': 'the delegation of the merge is off (`[delegations] merge`)',
        'merge_budget_malformed': ('the command is not complete or not well formed: an integer '
                                   'from 1 to {max} and the reason are needed'),
        'time_day': 'the time of the command cannot be read, so the day cannot be verified',
        'day_over': 'the UTC day of the command ({day}) is already over',
        'merge_budget_granted': ('on {day} (UTC) the merges delegated to the supervisor may be '
                                 '{n} more: the cap of the day becomes {cap} (`[delegations] '
                                 'max_merges_per_day` = {max}, plus the extensions of the day). '
                                 'The next day it is the configured one again.'),
        # Retries and how to resume.
        'retry_applied': ('the step `{step}` runs again (attempt {attempt} of {max}), without '
                          'counting a review round.'),
        'manual': ('remove `{label}` and set the label of the step to run (`agent:ready` on the '
                   'issue, `agent:review` or `agent:changes` on the PR).'),
        'without_supervisor': 'To resume without the supervisor: {manual}',
        'supervisor_resume': ('To resume, the supervisor or a trusted user writes as the first '
                              'line of a comment {valid}. To resume without the supervisor: '
                              '{manual}'),
        'preflight_alternative': ('Otherwise, a trusted user decides the gaps of the readiness '
                                  'check with `/devloop {accept}` or `/devloop {decide} '
                                  '<decisions>` as the first line of a comment.'),
        'operator_merge_resume': ('The PR is ready and its merge is the operator\'s: it is '
                                  'merged by hand, as the dispatcher\'s comment on the '
                                  'operator\'s merge explains.'),
        # Commands on an escalation to the operator (#321).
        'merge_delegation': ('`/devloop {merge}` is a delegation of the supervisor; the operator '
                             'merges the PR by hand'),
        'merge_pr_only': ('`/devloop {merge}` applies only to a ready PR whose merge is the '
                          'operator\'s'),
        'already_operator': 'the escalation is already with the operator',
        'grant_budget_delegation': ('`/devloop {command}` is a delegation of the supervisor, '
                                    'only on a `{cls}` escalation; the values of the envelope '
                                    'and the other extensions stay decisions of the operator'),
        'decide_only': ('it applies only to an issue whose latest readiness check failed with '
                        'gaps all `default_available` and no split'),
        'preflight_no_retry': 'the readiness check is not run again with `/devloop retry`',
        'not_step': ('what brought {where} here is not an escalation that records a step to run '
                     'again'),
        'preflight_ready': ('the readiness check starts again by adding `{ready}` back, which the '
                            'issue no longer has'),
        'preflight_ready_resume': 'To resume: remove `{operator}` and add `{ready}` back.',
        'operator_limit': ('not run: the step `{step}` was already run again {done} times for the '
                           'same input (at most {max}). {stays}\n\n{resume}'),
        # The coordinator's closure of findings (#606).
        'finding_coordinator_only': ('`/devloop {finding}` as a command of an escalation is the '
                                     'coordinator\'s only; a trusted user closes a finding with '
                                     'the same line and then puts the PR back in `{review}`'),
        'finding_open_only': ('the coordinator\'s `/devloop {finding}` applies only to a `{cls}` '
                              'escalation of a PR'),
        'finding_form': ('each line is written as `{form} <round/reviewer/ID> '
                         'resolved|withdrawn <reason>`, and the reason is required'),
        'finding_owners': 'some findings of the PR have no owner that can be rebuilt',
        'finding_head': ('the latest review round is not of the head of the PR (`{head}`): a new '
                         'review is needed, not the closure of a finding'),
        'finding_asking': ('in round {round} {who} does not approve the head and still asks for '
                           'changes or a decision: the coordinator closes a finding, it does not '
                           'override a verdict'),
        'finding_trust': ('a reviewer marked `trust_policy` {keys}: the findings on the trust '
                          'policy stay the operator\'s'),
        'finding_absent': '{keys} is not open on this PR',
        'closed_left': ('closed by the coordinator:\n\n{listed}\n\n{left} stay open: the PR '
                        'stays in `{label}`.'),
        'closed_all': ('closed by the coordinator:\n\n{listed}\n\nNo finding stays open: the PR '
                       'goes back to `{review}`, where the recorded approval is applied again '
                       'without a new review; promotion and merge follow their rules.'),
        'grant_budget_class': ('`/devloop {command}` extends the budget envelope only on a '
                               '`{budget}` escalation to the operator or a `{grantable}` one to '
                               'the supervisor; on an escalation of another class it does not '
                               'apply'),
        'old_budget_escalation': ('the escalation does not record the step, the story, the kind '
                                  'or the agent of the refused turn (it predates #595), so the '
                                  'extension cannot be verified'),
        'not_granted': 'the extension is not granted: {why}',
        'story_spent_operator': ('meanwhile the story `{story}` already had its one extension: a '
                                 'second one is the operator\'s'),
        'lapsed_operator': 'the extension of this command already lapsed: a new grant is needed',
        'grant_round_class': ('`/devloop {command}` grants one more round only to a PR in a '
                              '`round_limit` escalation; on an escalation of another class it '
                              'does not apply'),
        'no_head': ('the escalation does not record the head of the PR (it predates #553), so '
                    'the grant cannot be verified'),
        'coordinator_rounds': ('{before} round was already granted to this PR on an escalation '
                               'to the operator: the coordinator grants at most {max} per PR, '
                               'the operator grants the others'),
        'no_journal': 'the round cannot be recorded: the journal of the turns is not available',
        'journal_failed': 'the round cannot be recorded in the journal of the turns',
        'operator_round_granted': ('one more round granted to this PR only: its limit becomes '
                                   '{limit} rounds (`max_review_rounds` = {max}, unchanged, plus '
                                   '{total} granted). The round is outside the caps of the '
                                   'supervisor\'s delegation; rounds granted to this PR on an '
                                   'escalation to the operator: {granted}. The PR goes back to '
                                   '`{resume}`.'),
        # The gaps of a readiness check (#182, #258).
        'defaults_refusal': ('The defaults of the readiness check cannot be accepted by the '
                             'supervisor'),
        'defaults_what': 'the defaults refer',
        'accepted_default': '*Accepted default:*',
        'accepted': ('defaults of the readiness check accepted. This record counts as a trusted '
                     'comment: the issue goes back in the queue and the check runs again with '
                     'these defaults.'),
        'decide_refusal': 'The gaps of the readiness check cannot be decided by the supervisor',
        'decide_what': 'the decision refers',
        'proposed_default': '*Proposed default:*',
        'decided': ('decision on the gaps of the readiness check recorded. This record counts as '
                    'a trusted comment: the issue goes back in the queue and the check runs '
                    'again with this decision.\n\nGaps:\n\n{listed}\n\n**Decision:**\n\n'
                    '{quoted}'),
        'no_check': 'no failed readiness check to decide',
        'not_defaults': ('not every gap is `default_available`, or a split is needed: the '
                         'operator decides'),
        'design': 'the issue has the label `{label}`',
        'issue_author': 'the issue was not opened by a trusted user nor by an app of devloop',
        'coordinator_trust': ('the readiness check declares the trust policy touched, or does '
                              'not say whether it touches it: the coordinator does not decide '
                              'it, the operator decides'),
        'text_changed': ('the text of the issue changed after the readiness check, so {what} to '
                         'a superseded input'),
    },
    it={
        'supervisor_title': 'Decisione del supervisore',
        'operator_title': 'Comando su un\'escalation all\'operatore',
        'capacity_title': 'Limite di capacità temporaneo',
        'merge_title': 'Merge delegato al supervisore',
        'coordinator_merge_title': 'Merge riservato del coordinatore',
        'merge_budget_title': 'Merge delegati: estensione del tetto giornaliero',
        'capacity_usage': (f'Il comando si scrive come `/devloop {CAPACITY} <chiave> <valore> '
                           '<durata> <motivo>`, come prima riga di un commento su questa issue: '
                           f'la chiave è {_CHIAVI}, la durata `<N>h` o `<N>m`, il motivo, che '
                           'può continuare nelle righe successive, riporta o cita l\'analisi '
                           'delle cause.'),
        'merge_budget_usage': (f'Il comando si scrive come `/devloop {MERGE_BUDGET} <N> '
                               '<motivo>`, come prima riga di un commento di un utente fidato su '
                               f'questa issue: `N` da 1 a {MAX_MERGE_BUDGET} merge delegati in '
                               'più per il giorno UTC in cui è scritto, il motivo obbligatorio.'),
        'trace': '`/devloop {name}` di `{by}`: {text}',
        'the_pr': 'la PR',
        'the_issue': 'l\'issue',
        'The_pr': 'La PR',
        'The_issue': 'L\'issue',
        'or': ' oppure ',
        'reason_arg': '<motivo>',
        'decisions_arg': '<decisioni>',
        'text_arg': '<testo>',
        'reason_title': '**Motivo:**',
        'causes_title': '**Motivo e analisi delle cause:**',
        'resolved': 'risolto',
        'withdrawn': 'ritirato',
        'of_code': 'del codice',
        'of_delegation': 'della delega',
        'refused': 'rifiutato: {why}.',
        'not_applicable': 'non applicabile: {why}. {how}',
        'spent': 'Il comando è speso; {stays}',
        'item_stays': '{where} resta in `{label}`.',
        'escalation_stays': 'l\'escalation resta al supervisore.',
        'why_step': 'il passo `{step}` non è fra quelli che il supervisore può ripetere',
        'why_marker': ('il corpo della PR non termina più con il suo marker: chi l\'ha '
                       'implementata e la sua modalità di merge non sono verificabili'),
        'why_own_budget': ('il supervisore ha implementato questa PR, e la concessione di budget '
                           'non è fra le escalation di processo che decide anche sulle PR del '
                           'proprio agent'),
        'why_mode': ('la modalità di merge di questa PR è `{mode}`, non `auto` (override del '
                     'livello o `operator_only_paths`)'),
        'not_computable': 'non calcolabile',
        'why_author': ('l\'issue #{issue} è stata aperta da `{author}`, che non è un utente '
                       'fidato né un\'app di devloop'),
        'why_design': 'la label `{label}` è presente',
        'why_key': 'l\'input del passo non è stato registrato prima dell\'esecuzione',
        'why_unverifiable': ('non è stato possibile verificare che l\'escalation non sia critica '
                             '({error})'),
        'why_operator': ('In modalità di merge `auto` questa escalation andrebbe al supervisore, '
                         'ma resta all\'operatore: {why}.'),
        'why_round': ('In modalità di merge `auto` il supervisore potrebbe concedere un altro '
                      'round, ma l\'escalation resta all\'operatore: {why}.'),
        'preflight_defaults': ('Il controllo di prontezza ha trovato solo lacune con un default '
                               'proposto (`default_available`): il supervisore può accettarli '
                               'con `/devloop {accept}`, o decidere diversamente con `/devloop '
                               '{decide} <decisioni>`.'),
        'budget_grantable': ('Il supervisore può estendere una volta l\'envelope di questa '
                             'storia, solo in turni e tempo, con `/devloop grant-budget '
                             '<motivo>`, dove il motivo riporta o cita l\'analisi delle cause: '
                             '{extension}.'),
        'missing_text': ('manca il testo, il comando va scritto come `/devloop {command} '
                         '{arguments}`'),
        'operator_escalation': ('l\'escalation è affidata all\'operatore, non al supervisore: '
                                'decide un utente fidato'),
        'not_auto': ('la modalità di merge è `{mode}`, non `auto`, quindi i comandi del '
                     'supervisore non valgono'),
        'only_merge': ('vale solo su una PR pronta il cui merge spetta all\'operatore, non su '
                       'un\'escalation al supervisore'),
        'only_finding': 'vale solo su un\'escalation `open_findings` all\'operatore',
        'only_report': ('vale solo sulla issue del riepilogo (`[report] issue`), non su '
                        'un\'escalation'),
        'refused_critical': ('rifiutato. L\'escalation è critica (classe `{cls}`) o non è '
                             'affidata al supervisore: decide l\'operatore.'),
        'to_operator_critical': ('Il supervisore non può decidere questa escalation (classe '
                                 '`{cls}`): `/devloop {name}` di `{by}` è stato rifiutato.'),
        'escalated': 'l\'escalation passa all\'operatore: {reason}',
        'to_operator_escalated': ('Il supervisore `{by}` passa all\'operatore l\'escalation di '
                                  'classe `{cls}`: {reason}'),
        'only_round_grantable': ('vale solo per un\'escalation `{cls}` di una PR al limite dei '
                                 'round'),
        'only_budget_grantable': ('vale solo per un\'escalation `{cls}` di un turno rifiutato '
                                  'dall\'envelope di budget'),
        'only_preflight_default': ('vale solo per un\'escalation `preflight_default` del '
                                   'controllo di prontezza'),
        'only_retry': ('vale solo per le escalation `tool_failure`, `sync_failed` e '
                       '`push_failed`'),
        'only_rereview': 'vale solo per un\'escalation `tool_failure` della review di una PR',
        'merged_why': ('l\'issue è già risolta da una PR devloop mergiata ({merged}), quindi '
                       'ripeterne il passo duplicherebbe il lavoro'),
        'merged_spent': ('Il comando è speso; l\'issue passa a un\'escalation `merged_issue`.'),
        'merged_resume': ('Se è risolta va chiusa; se serve altro lavoro, va aperta una nuova '
                          'issue.'),
        'not_retryable': 'l\'escalation non registra un passo ripetibile',
        'to_operator_not_retryable': ('L\'escalation non registra un passo che il supervisore '
                                      'possa ripetere: `/devloop {name}` di `{by}` è stato '
                                      'rifiutato.'),
        'superseded': ('l\'input del passo è cambiato dopo l\'escalation ({changed}), quindi il '
                       'comando si riferisce a un input superato'),
        'superseded_grant': ('l\'input del passo è cambiato dopo l\'escalation ({changed}), '
                             'quindi la concessione si riferisce a un input superato'),
        'no_longer_auto': 'la modalità di merge non è più `auto` con un supervisore',
        'became_critical': 'rifiutato: l\'escalation è diventata critica ({why}).',
        'to_operator_became': ('Il supervisore non può più decidere questa escalation: {why}. '
                               '`/devloop {name}` di `{by}` è stato rifiutato.'),
        'limit': ('non eseguito: il passo `{step}` è già stato ripetuto {done} volte per lo '
                  'stesso input.'),
        'to_operator_limit': ('Il passo `{step}` è fallito di nuovo dopo {done} tentativi '
                              'autorizzati dal supervisore (al massimo {max}): `/devloop {name}` '
                              'di `{by}` non è stato eseguito.'),
        'rereview_asked': ('nell\'ultimo round un reviewer ha chiesto una decisione '
                           'dell\'operatore ({asked}), e una nuova review la scavalcherebbe'),
        'rereview_no_round': ('nessun round di review di questo head aspetta il verdetto di un '
                              'reviewer del quorum: `/devloop {retry}` apre già un round con la '
                              'configurazione attuale'),
        'rereview_again': ('dopo l\'ultimo round con un verdetto è già stata chiesta una nuova '
                           'review, da un utente fidato o dal supervisore, e il suo round non ha '
                           'scritto alcun verdetto: un\'altra lo ripeterebbe senza contare per '
                           '`max_review_rounds`'),
        'rereview_first': ('l\'ultimo round è stato chiuso dal primo reviewer del quorum, che non '
                           'ha approvato ({verdicts}): con `second_reviewer = "{second}"` il '
                           'secondo reviewer non era richiesto e non manca alcun verdetto, e una '
                           'nuova review scavalcherebbe il suo'),
        'rereview_all': ('ogni reviewer del quorum dell\'ultimo round ha già un verdetto '
                         '({verdicts}), e una nuova review lo scavalcherebbe'),
        'rereview_applied': ('si apre un round di review nuovo al posto del round {round}, in cui '
                             'manca il verdetto di {reviewers}. Il round nuovo sceglie i reviewer '
                             'con la configurazione attuale, ha una ricevuta nuova e conta per '
                             '`max_review_rounds`; il merge resta dove lo mette la sua '
                             'modalità.'),
        'rereview_operator': ('l\'escalation è affidata all\'operatore: il supervisore vi può '
                              'chiedere una nuova review solo dopo un `tool_failure` della review '
                              'di una PR'),
        'not_supervisors': 'l\'escalation non è del supervisore ({why})',
        'no_supervisor': 'nessun supervisore configurato',
        'cap_pr': ('a questa PR sono già stati concessi {granted} round, il massimo della delega '
                   '(`[delegations] max_rounds_per_pr` = {cap})'),
        'grants_unknown': ('le concessioni di oggi non sono verificabili: il journal dei turni '
                           'non è disponibile o non è leggibile'),
        'cap_day': ('oggi sono già state fatte {today} concessioni, il massimo giornaliero '
                    '(`[delegations] max_grants_per_day` = {cap})'),
        'owners': 'il proprietario di alcuni finding non è ricostruibile',
        'security': ('restano aperti finding di sicurezza, o finding che non dicono se lo sono: '
                     '{keys}'),
        'no_progress': ('l\'ultimo round contato non è una CI fallita e i finding bloccanti '
                        'aperti non sono meno di quelli del round precedente'),
        'rounds_why': 'la delega dei round non si applica: {why}',
        'head_moved': ('il head della PR è cambiato dopo l\'escalation, quindi la concessione si '
                       'riferisce a un input superato'),
        'to_operator_round': ('Il supervisore non può concedere un altro round a questa PR: '
                              '{why}. `/devloop {command}` di `{by}` è stato rifiutato.\n\n'
                              '{rounds}'),
        'rounds_off': ('la delega dei round è spenta (`[delegations] rounds`), o la modalità di '
                       'merge non è più `auto` con un supervisore'),
        'daily_reached': ('nel frattempo le concessioni di oggi hanno raggiunto il massimo '
                          'giornaliero (`[delegations] max_grants_per_day` = {cap})'),
        'round_granted': ('concesso un round in più a questa PR: il suo limite passa a {limit} '
                          'round (`max_review_rounds` = {max}, più {total} concessi). Round '
                          'concessi a questa PR dalla delega: {granted} di {cap}; concessioni di '
                          'oggi: {daily} di {daily_cap}. La PR torna in `{resume}`.'),
        'turn_unknown': ('il turno rifiutato non è ricostruibile: il suo agent (`{agent}`) non è '
                         'configurato, o il tipo del turno manca'),
        'journal_unreadable': 'il journal dei turni non è leggibile ({error})',
        'budget_why': 'la delega del budget non si applica: {why}',
        'to_operator_budget': ('Il supervisore non può estendere l\'envelope di budget di questa '
                               'storia: {why}. `/devloop {command}` di `{by}` è stato '
                               'rifiutato.\n\nI valori dell\'envelope e le estensioni oltre '
                               'l\'unica delegata al supervisore restano decisioni '
                               'dell\'operatore (ADR 0004, D2).'),
        'budget_off': 'la delega del budget è spenta (`[delegations] budget`)',
        'no_turn_step': 'l\'escalation non registra il passo del turno rifiutato',
        'other_story': ('la storia del lavoro (`{story}`) non è quella dell\'escalation '
                        '(`{expected}`)'),
        'story_spent': 'nel frattempo la storia `{story}` ha già avuto la sua unica estensione',
        'extension_lapsed': ('l\'estensione di questo comando è già decaduta, perché il primo '
                             'turno dopo la concessione è stato rifiutato comunque: serve una '
                             'nuova concessione'),
        'stays_there': '{what} resta in `{held}`{how}: il passo `{step}` non riparte da qui.',
        'where_operator': ', dove decide l\'operatore come prima',
        'where_latest': ', con la sua escalation più recente',
        'pr_back': 'La PR torna in `{label}`.',
        'merged_after': ('L\'issue è però già risolta da una PR devloop mergiata ({merged}): il '
                         'passo `{step}` non riparte e l\'issue passa a un\'escalation '
                         '`merged_issue`.'),
        'step_now': 'Il passo `{step}` dell\'issue riparte ora, come dopo un retry.',
        'recovered': ('Un\'elaborazione precedente si era fermata dopo aver registrato '
                      'l\'estensione nel journal dei turni: qui se ne completa la traccia, senza '
                      'applicarla di nuovo. {where}'),
        'extended': ('estesi i tetti dell\'envelope di budget della storia `{story}`: '
                     '{extension} (il {percent}% dei valori fissati alla sua prima '
                     'prenotazione, arrotondato per difetto; nessun dollaro né token). È '
                     'l\'unica estensione della storia: un altro rifiuto va all\'operatore. '
                     'Concessioni del giorno: {daily} di {cap}. {where}'),
        'capacity_refuse': ('non applicabile: {why}. Il comando è speso; i limiti di capacità '
                            'restano quelli di prima.\n\n{usage}'),
        'report_only': ('sulla issue del riepilogo valgono solo `/devloop {capacity}` e '
                        '`/devloop {merge_budget}`'),
        'capacity_off': ('la delega dei limiti di capacità è spenta (`[delegations] capacity`): '
                         'i limiti li cambia solo l\'operatore'),
        'capacity_malformed': ('il comando non è completo o non è ben formato: servono chiave, '
                               'valore intero, durata e motivo'),
        'capacity_refused_key': ('`{setting}` non è delegata: devloop la applica solo al '
                                 'riavvio di `devloop run`, quindi la cambia solo l\'operatore'),
        'capacity_unknown_key': '`{setting}` non è fra le chiavi della delega',
        'value_code': ('il valore {value} è fuori dai limiti del codice: `{setting}` va da 1 a '
                       '{cap} (tetto del codice)'),
        'value_delegation': ('il valore {value} è fuori dai limiti della delega: `{setting}` va '
                             'da 1 a {cap} (`[delegations] capacity_{setting}`)'),
        'hours_code': 'la durata supera il massimo di {hours} ore (tetto del codice)',
        'hours_delegation': ('la durata supera il massimo di {hours} ore (`[delegations] '
                             'capacity_max_hours`)'),
        'time_expiry': ('l\'ora del comando non è leggibile, quindi la scadenza non è '
                        'verificabile'),
        'elapsed': ('la durata, contata da quando il comando è stato scritto ({written}), è già '
                    'trascorsa'),
        'capacity_daily': ('oggi le concessioni hanno già raggiunto il massimo giornaliero '
                           '(`[delegations] max_grants_per_day` = {cap})'),
        'capacity_granted': ('`{setting}` vale {value} fino a {until} (valore configurato: '
                             '{configured}; tetto {of}: {cap}). Alla scadenza torna il valore '
                             'configurato; un altro `/devloop {capacity}` sulla stessa chiave '
                             'sostituisce questo. Concessioni di oggi: {daily} di {daily_cap}.'),
        'merge_budget_refuse': ('non applicabile: {why}. Il comando è speso; il tetto dei merge '
                                'delegati resta quello di prima.\n\n{usage}'),
        'merge_budget_trusted': ('il tetto dei merge delegati lo estende solo un utente fidato '
                                 '(`trusted_users`), mai il supervisore né un\'app'),
        'merge_off': 'la delega del merge è spenta (`[delegations] merge`)',
        'merge_budget_malformed': ('il comando non è completo o non è ben formato: servono un '
                                   'numero intero da 1 a {max} e il motivo'),
        'time_day': 'l\'ora del comando non è leggibile, quindi il giorno non è verificabile',
        'day_over': 'il giorno UTC del comando ({day}) è già finito',
        'merge_budget_granted': ('il {day} (UTC) i merge delegati al supervisore possono essere '
                                 '{n} in più: il tetto del giorno passa a {cap} (`[delegations] '
                                 'max_merges_per_day` = {max}, più le estensioni del giorno). Il '
                                 'giorno dopo torna quello configurato.'),
        'retry_applied': ('il passo `{step}` viene ripetuto (tentativo {attempt} di {max}), senza '
                          'contare un round di review.'),
        'manual': ('rimuovere `{label}` e impostare la label del passo da eseguire '
                   '(`agent:ready` sulla issue, `agent:review` o `agent:changes` sulla PR).'),
        'without_supervisor': 'Per riprendere senza il supervisore: {manual}',
        'supervisor_resume': ('Per riprendere, il supervisore o un utente fidato scrive come '
                              'prima riga di un commento {valid}. Per riprendere senza il '
                              'supervisore: {manual}'),
        'preflight_alternative': ('In alternativa, un utente fidato decide le lacune del '
                                  'controllo di prontezza con `/devloop {accept}` o `/devloop '
                                  '{decide} <decisioni>` come prima riga di un commento.'),
        'operator_merge_resume': ('La PR è pronta e il merge spetta all\'operatore: si mergia a '
                                  'mano, come spiega il commento «Il merge spetta '
                                  'all\'operatore».'),
        'merge_delegation': ('`/devloop {merge}` è una delega del supervisore; l\'operatore '
                             'mergia la PR a mano'),
        'merge_pr_only': ('`/devloop {merge}` vale solo su una PR pronta il cui merge spetta '
                          'all\'operatore'),
        'already_operator': 'l\'escalation è già all\'operatore',
        'grant_budget_delegation': ('`/devloop {command}` è una delega del supervisore, solo su '
                                    'un\'escalation `{cls}`; i valori dell\'envelope e le altre '
                                    'estensioni restano decisioni dell\'operatore'),
        'decide_only': ('vale solo su un\'issue il cui ultimo controllo di prontezza è fallito '
                        'con lacune tutte `default_available` e nessuna suddivisione'),
        'preflight_no_retry': 'il controllo di prontezza non si ripete con `/devloop retry`',
        'not_step': ('quello che ha portato qui {where} non è un\'escalation che registri un '
                     'passo da ripetere'),
        'preflight_ready': ('il controllo di prontezza riparte rimettendo `{ready}`, che l\'issue '
                            'non ha più'),
        'preflight_ready_resume': 'Per riprendere: rimuovere `{operator}` e rimettere `{ready}`.',
        'operator_limit': ('non eseguito: il passo `{step}` è già stato ripetuto {done} volte per '
                           'lo stesso input (al massimo {max}). {stays}\n\n{resume}'),
        'finding_coordinator_only': ('`/devloop {finding}` come comando di un\'escalation è solo '
                                     'del coordinatore; un utente fidato chiude un finding con '
                                     'la stessa riga e poi riporta la PR in `{review}`'),
        'finding_open_only': ('`/devloop {finding}` del coordinatore vale solo su un\'escalation '
                              '`{cls}` di una PR'),
        'finding_form': ('ogni riga va scritta come `{form} <round/reviewer/ID> '
                         'resolved|withdrawn <motivo>`, e il motivo è obbligatorio'),
        'finding_owners': 'alcuni finding della PR non hanno un proprietario ricostruibile',
        'finding_head': ('l\'ultimo round di review non è del head della PR (`{head}`): serve '
                         'una nuova review, non la chiusura di un finding'),
        'finding_asking': ('nel round {round} {who} non approva il head e chiede ancora modifiche '
                           'o una decisione: il coordinatore chiude un finding, non scavalca un '
                           'verdetto'),
        'finding_trust': ('un reviewer ha marcato `trust_policy` {keys}: i finding sulla politica '
                          'di fiducia restano dell\'operatore'),
        'finding_absent': '{keys} non è aperto su questa PR',
        'closed_left': ('chiusi dal coordinatore:\n\n{listed}\n\nRestano aperti {left}: la PR '
                        'resta in `{label}`.'),
        'closed_all': ('chiusi dal coordinatore:\n\n{listed}\n\nNessun finding resta aperto: la '
                       'PR torna in `{review}`, dove l\'approvazione registrata viene riapplicata '
                       'senza una nuova review; promozione e merge seguono le loro regole.'),
        'grant_budget_class': ('`/devloop {command}` estende l\'envelope di budget solo su '
                               'un\'escalation `{budget}` all\'operatore o `{grantable}` al '
                               'supervisore; su un\'escalation di altra classe non vale'),
        'old_budget_escalation': ('l\'escalation non registra il passo, la storia, il tipo o '
                                  'l\'agent del turno rifiutato (è precedente a #595), quindi '
                                  'l\'estensione non è verificabile'),
        'not_granted': 'l\'estensione non si concede: {why}',
        'story_spent_operator': ('nel frattempo la storia `{story}` ha già avuto la sua unica '
                                 'estensione: una seconda è dell\'operatore'),
        'lapsed_operator': ('l\'estensione di questo comando è già decaduta: serve una nuova '
                            'concessione'),
        'grant_round_class': ('`/devloop {command}` concede un round in più solo a una PR in '
                              'un\'escalation `round_limit`; su un\'escalation di altra classe '
                              'non vale'),
        'no_head': ('l\'escalation non registra il head della PR (è precedente a #553), quindi '
                    'la concessione non è verificabile'),
        'coordinator_rounds': ('a questa PR è già stato concesso {before} round su un\'escalation '
                               'all\'operatore: il coordinatore ne concede al massimo {max} per '
                               'PR, gli altri li concede l\'operatore'),
        'no_journal': ('il round non si può registrare: il journal dei turni non è '
                       'disponibile'),
        'journal_failed': 'il round non si può registrare nel journal dei turni',
        'operator_round_granted': ('concesso un round in più a questa sola PR: il suo limite '
                                   'passa a {limit} round (`max_review_rounds` = {max}, '
                                   'invariato, più {total} concessi). Il round è fuori dai tetti '
                                   'della delega del supervisore; round concessi a questa PR su '
                                   'un\'escalation all\'operatore: {granted}. La PR torna in '
                                   '`{resume}`.'),
        'defaults_refusal': ('I default del controllo di prontezza non possono essere accettati '
                             'dal supervisore'),
        'defaults_what': 'i default si riferiscono',
        'accepted_default': '*Default accettato:*',
        'accepted': ('default del controllo di prontezza accettati. Questo record conta come un '
                     'commento fidato: l\'issue torna in coda e il controllo si ripete con '
                     'questi default.'),
        'decide_refusal': ('Le lacune del controllo di prontezza non possono essere decise dal '
                           'supervisore'),
        'decide_what': 'la decisione si riferisce',
        'proposed_default': '*Default proposto:*',
        'decided': ('decisione sulle lacune del controllo di prontezza registrata. Questo record '
                    'conta come un commento fidato: l\'issue torna in coda e il controllo si '
                    'ripete con questa decisione.\n\nLacune:\n\n{listed}\n\n**Decisione:**\n\n'
                    '{quoted}'),
        'no_check': 'nessun controllo di prontezza fallito da decidere',
        'not_defaults': ('non tutte le lacune sono `default_available`, o serve una '
                         'suddivisione: decide l\'operatore'),
        'design': 'l\'issue ha la label `{label}`',
        'issue_author': 'l\'issue non è stata aperta da un utente fidato né da un\'app di devloop',
        'coordinator_trust': ('il controllo di prontezza dichiara toccata la policy di fiducia, '
                              'o non dice se la tocca: il coordinatore non la decide, decide '
                              'l\'operatore'),
        'text_changed': ('il testo dell\'issue è cambiato dopo il controllo di prontezza, quindi '
                         '{what} a un input superato'),
    })


def quoted(text):
    """``text`` as a Markdown quote, one ``>`` per line."""
    return '\n'.join(f'> {line}'.rstrip() for line in (text or '').splitlines())


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

    def _texts(self):
        """The ``TEXTS`` of the project's ``language`` (#633)."""
        return TEXTS.texts(self.cfg.language)

    def _where(self, is_pr, capital=False):
        """How the texts name the PR or the issue, in the project's language."""
        return self._texts()(('The_' if capital else 'the_') + ('pr' if is_pr else 'issue'))

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
        cfg, t = self.cfg, self._texts()
        if critical(cls) or cfg.merge.mode != 'auto' or not cfg.supervisor:
            return None, ''
        if step not in RETRY_STEPS:
            return None, t('why_step', step=step)
        try:
            labels = set()
            if pr is not None:
                if not pr.meta.get('implementer'):
                    return None, t('why_marker')
                labels |= pr.labels
                if cls == BUDGET_GRANTABLE and self._supervisor_implemented(pr):
                    # The escalations of process the supervisor decides on its agent's PRs too
                    # (#527) are those the operator listed on #524; an extension of the budget
                    # is not among them.
                    return None, t('why_own_budget')
                mode = 'auto' if any_merge else self._merge_mode(pr)
                if mode != 'auto':
                    return None, t('why_mode', mode=mode or t('not_computable'))
            issue = pr.issue if pr is not None else number
            history = self.gh.issue_history(issue)
            if not self._devloop_author(history['author']):
                return None, t('why_author', issue=issue, author=history['author'])
            untrusted = self._untrusted_change(history)
            if untrusted:
                return None, untrusted
            labels |= label_names(self.gh.issue(issue).get('labels') or [])
            if DESIGN_LABEL in labels:
                return None, t('why_design', label=DESIGN_LABEL)
            if pr is not None:
                return self._retry_payload(step, pr, reviewer), ''
            if key is None and cls in (PREFLIGHT_DEFAULT, MERGED_ISSUE):
                # A failed check reused, not run again, or an issue a merged PR resolved (#228),
                # whose step never runs: no retry uses its key (a retry admits neither class),
                # and accept-defaults checks the hash of the check's own record.
                key = self._issue_key(step, self.ws.base_head(),
                                      self._request_hash(history, self.gh.issue_comments(number)))
            if not isinstance(key, dict) or key.get('step') != step:
                return None, t('why_key')
            return {'step': step, 'key': key}, ''
        except (GitHubError, WorkspaceError) as exc:
            return None, t('why_unverifiable', error=exc)

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
        out the base of an implementation, #475). A PR whose head is the commit of the push kept
        for the same step and head (#645, ``_landed_push``) has the input of that step: the head
        moved only because the push reached GitHub although both attempts lost the answer."""
        if pr is not None:
            head = self.gh.pr_head(number)
            landed = self._landed_push(number, head)
            if landed is not None and landed.matches(key.get('step'), key):
                head = key.get('head')
            current = {'step': key.get('step'), 'head': head}
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
        t = self._texts()
        for comment in comments[start + 1:]:
            body = comment.get('body') or ''
            login = (comment.get('author') or {}).get('login')
            parsed = None if carries_record(body) else supervisor_command(body)
            if parsed is None and not carries_record(body) and self._is_coordinator(login):
                # The coordinator's commands no supervisor command names (#528): the reserved
                # ones, such as ``reconcile``, and those not listed, such as ``veto``, are
                # refused below; the delegated ones read elsewhere (``recheck``, ``fix``,
                # ``hold``, ``release``) are none of this reader's. Its ``finding`` (#606) is a
                # command of an escalation, applied by ``_coordinator_findings``.
                parsed = coordinator_command(body)
                if (parsed is not None and coordinator_refusal(parsed[0]) is None
                        and parsed[0] != FINDING):
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
            outside = (coordinator_refusal(command, self.cfg.language)
                       if self._is_coordinator(login) else None)
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
                          t('missing_text', command=command,
                            arguments='<sha> ' + t('reason_arg'))) if reason is None
                    else (None, None))
            elif operator:
                allowed = login in self.cfg.trusted_users or rereview or delegated
                why, refusal = (
                    ('the author is not a trusted user', None) if not allowed
                    and not self._may_hold(login)
                    else ('the escalation is the operator\'s, not the supervisor\'s',
                          t('operator_escalation')) if not allowed
                    else (f'malformed, expected `/devloop {command} <text>`',
                          t('missing_text', command=command, arguments=t('text_arg')))
                    if reason is None
                    else (None, None))
            else:
                why, refusal = (
                    ('the author is neither the supervisor nor a trusted user', None)
                    if not (self._may_hold(login) or delegated)
                    else (f'merge mode is "{self.cfg.merge.mode}", not "auto"',
                          t('not_auto', mode=self.cfg.merge.mode))
                    if self.cfg.merge.mode != 'auto'
                    else (f'malformed, expected `/devloop {command} <text>`',
                          t('missing_text', command=command, arguments=t('text_arg')))
                    if reason is None
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

    def _to_operator(self, number, reason, cls, remove=(), retry=None, extra=None):
        """Hand an item in ``agent:needs-supervisor`` to the operator: a critical escalation of
        class ``cls``. ``retry``: the step of the supervisor's escalation, its key and what a
        retry resumes, recorded again so that the operator may still retry it (#321).
        ``extra``: more fields of its record, such as the head of a ``round_limit`` (#553)."""
        lb = self.cfg.labels
        self.gh.comment(number, render_escalation(reason, cls=cls, retry=retry, extra=extra,
                                                  language=self.cfg.language))
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
        t = self._texts()

        def tracer(command, cls):
            """The ``trace(outcome, text, **extra)`` of the ``supervision`` record of
            ``command`` on an escalation of class ``cls``."""
            name, by = command['command'], command['by']

            def trace(outcome, text, records=(), **extra):
                record = dict(command=name, by=by, reason=command['reason'] or '',
                              comment=command['comment'], outcome=outcome, **extra)
                record['class'] = cls if isinstance(cls, str) else None
                coordinator = name == MERGE and self._is_coordinator(by)
                if coordinator:
                    # The coordinator's reserved merge (#529): the summary lists it apart.
                    record['role'] = COORDINATOR
                self.gh.comment(number, render_supervision(
                    record, t('trace', name=name, by=by, text=text.strip()),
                    t('coordinator_merge_title' if coordinator and operator
                      else 'merge_title' if name == MERGE and operator
                      else 'operator_title' if operator else 'supervisor_title'), records))
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
                 if cls in (*RETRY_CLASSES, BUDGET_GRANTABLE)
                 and self._retryable(escalation, is_pr) else None)
        # A tool failure of an issue loses agent:ready on its way to the operator, as any failed
        # step does; the readiness check keeps it, and so does a readiness check the operator
        # may retry, which runs again only with agent:ready.
        keep_ready = is_pr or cls == PREFLIGHT_DEFAULT
        human_remove = [] if keep_ready else [lb.ready]
        carried_remove = [] if keep_ready or (carry and carry['step'] == 'preflight') \
            else [lb.ready]
        trace = tracer(command, cls)
        stays = t('spent', stays=t('escalation_stays'))

        def refused(why, superseded=False):
            """The answer to a command that does not apply: the escalation stays with the
            supervisor."""
            return trace('refused', t('refused', why=why) + f' {stays}\n\n'
                         + self._supervisor_resume(escalation, is_pr, superseded=superseded))

        if command.get('refusal'):
            where = self._where(is_pr)
            how = (t('spent', stays=t('item_stays', where=where, label=lb.needs_operator))
                   + '\n\n' + self._operator_resume(command['anchor'], is_pr) if operator
                   else t('spent', stays=t('item_stays', where=where, label=lb.needs_supervisor))
                   + '\n\n' + self._supervisor_resume(escalation, is_pr))
            return trace('refused', t('not_applicable', why=command['refusal'], how=how))
        if name in (CAPACITY, MERGE_BUDGET) or name in (MERGE, FINDING) and not operator:
            # A temporary capacity limit (#412) and an extension of the delegated merges (#486)
            # are never a decision on an escalation; a delegated merge applies only to a PR
            # whose merge is the operator's (#486), the coordinator's closure of findings only
            # on an ``open_findings`` escalation, always the operator's (#606).
            label = lb.needs_operator if operator else lb.needs_supervisor
            resume = (self._operator_resume(command['anchor'], is_pr) if operator
                      else self._supervisor_resume(escalation, is_pr))
            what = t('only_merge' if name == MERGE
                     else 'only_finding' if name == FINDING else 'only_report')
            return trace('refused', t('not_applicable', why=f'`/devloop {name}` {what}',
                                      how=t('spent', stays=t('item_stays',
                                                             where=self._where(is_pr),
                                                             label=label))
                                      + f'\n\n{resume}'))
        if operator and name == REREVIEW:
            # Only the supervisor's reaches here: a trusted user's is no command (#491).
            return self._operator_rereview(target, number, labels, fresh if is_pr else None,
                                           comments, command, trace)
        if operator:
            return self._operator_command(target, number, labels, comments, command, trace,
                                          fresh if is_pr else None)
        if critical(cls) or escalation.get('to') != 'supervisor':
            trace('refused', t('refused_critical', cls=cls))
            return self._to_operator(number, t('to_operator_critical', cls=cls, name=name, by=by),
                                     'supervisor_refused', carried_remove, carry)
        if name == ESCALATE:
            reason = ' '.join(command['reason'].split())
            trace('escalated', t('escalated', reason=reason))
            return self._to_operator(number, t('to_operator_escalated', by=by, cls=cls,
                                               reason=reason),
                                     'supervisor_escalated', carried_remove, carry)
        if name == GRANT_ROUND:
            if not is_pr or cls != ROUND_GRANTABLE:
                return refused(t('only_round_grantable', cls=ROUND_GRANTABLE))
            return self._grant_round(target, number, fresh, labels, comments, command,
                                     escalation, trace)
        if name == GRANT_BUDGET:
            if cls != BUDGET_GRANTABLE:
                return refused(t('only_budget_grantable', cls=BUDGET_GRANTABLE))
            return self._grant_budget(target, number, fresh if is_pr else None, labels,
                                      comments, command, escalation, trace, human_remove)
        if name in (ACCEPT_DEFAULTS, DECIDE):
            if is_pr or cls != PREFLIGHT_DEFAULT:
                return refused(t('only_preflight_default'))
            if name == DECIDE:
                return self._decide(number, labels, comments, trace, command['reason'],
                                    coordinator=self._is_coordinator(by))
            return self._accept_defaults(number, labels, comments, trace)
        if cls not in RETRY_CLASSES:
            return refused(t('only_retry'))
        if name == REREVIEW and not (is_pr and cls == TOOL_FAILURE
                                     and escalation.get('step') == 'review'):
            return refused(t('only_rereview'))
        # An issue a merged devloop PR already resolved is never checked nor implemented again
        # (#228), whatever escalation it waits in: the retry becomes a merged_issue escalation.
        merged = [] if is_pr else self._merged_prs(number)
        if merged:
            trace('refused', self._merged_refusal(merged))
            return self._do_merged_issue(number, merged)
        key, step = escalation.get('key'), escalation.get('step')
        if not self._retryable(escalation, is_pr):
            trace('refused', t('refused', why=t('not_retryable')))
            return self._to_operator(number, t('to_operator_not_retryable', name=name, by=by),
                                     'supervisor_refused', human_remove)
        current = self._current_key(number, key, target if is_pr else None)
        # The base of an implementation does not bind its retry (#475): the step starts again
        # from the current base with the saved work of its timed-out turn reapplied.
        changed = changed_input(current, key)

        def superseded():
            return refused(t('superseded', changed=', '.join(f'`{k}`' for k in changed)),
                           superseded=True)

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
            why = why or t('no_longer_auto')
            trace('refused', t('became_critical', why=why))
            return self._to_operator(number, t('to_operator_became', why=why, name=name, by=by),
                                     'supervisor_refused', carried_remove, carry)
        if changed:
            return superseded()
        if name == REREVIEW:
            return self._rereview(number, target, command, escalation, trace)

        def limit(done):
            trace('limit', t('limit', step=step, done=done), key=key)
            self._to_operator(number, t('to_operator_limit', step=step, done=done,
                                        max=MAX_RETRIES, name=name, by=by),
                              'retry_limit', human_remove)

        self._apply_retry(number, is_pr, comments, escalation, [lb.needs_supervisor], trace,
                          limit)

    def _merged_refusal(self, merged):
        """The answer to a command on an issue the merged devloop PRs ``merged`` resolved
        (#228): it becomes a ``merged_issue`` escalation."""
        t = self._texts()
        return (t('refused', why=t('merged_why', merged=', '.join(f'#{m["number"]}'
                                                                  for m in merged)))
                + ' ' + t('merged_spent'))

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
        lb, t = self.cfg.labels, self._texts()
        label = lb.needs_operator if operator else lb.needs_supervisor
        stays = (t('spent', stays=t('item_stays', where=t('the_pr'), label=label))
                 + f'\n\n{resume}' if operator
                 else t('spent', stays=t('escalation_stays')) + '\n\n'
                 + self._supervisor_resume(escalation, True))
        latest = current_round(pr.reviews)
        missing = missing_verdicts(pr)
        asked = [owner_of(r) for r in latest
                 if current_verdict(r.get('verdict')) == 'needs_operator']
        verdicts = ', '.join(f'`{owner_of(r)}`: `{current_verdict(r.get("verdict"))}`'
                             for r in latest)
        why = (t('rereview_asked', asked=', '.join(f'`{a}`' for a in asked)) if asked
               else t('rereview_no_round', retry=RETRY)
               if not latest or latest[0]['head_sha'] != pr.head_sha
               else t('rereview_again') if pr.rereview
               # The first reviewer closed the round without approving (#521): the second was
               # not asked, and no verdict is missing.
               else t('rereview_first', verdicts=verdicts,
                      second=latest[0]['receipt']['second_reviewer'])
               if closed_by_first(latest)
               else t('rereview_all', verdicts=verdicts) if not missing
               else '')
        if why:
            return trace('refused', t('refused', why=why) + f' {stays}')
        reviewers = ', '.join(f'`{key}`' for key in missing)
        trace('applied', t('rereview_applied', round=latest[0]['round'], reviewers=reviewers)
              + f'\n\n{t("reason_title")}\n\n' + quoted(command['reason']),
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
        lb, t = self.cfg.labels, self._texts()
        anchor = command['anchor']
        escalation = anchor['record']
        is_pr = isinstance(target, PullRequest)
        resume = self._operator_resume(anchor, is_pr)

        def refuse(why):
            return trace('refused', t('refused', why=why) + ' '
                         + t('spent', stays=t('item_stays', where=self._where(is_pr),
                                              label=lb.needs_operator))
                         + f'\n\n{resume}')

        if self.cfg.merge.mode != 'auto' or not self.cfg.supervisor:
            return refuse(t('not_auto', mode=self.cfg.merge.mode))
        if not (is_pr and anchor['kind'] == 'notice' and escalation.get('event') == 'escalation'
                and escalation.get('class') == TOOL_FAILURE
                and escalation.get('step') == 'review' and self._retryable(escalation, True)):
            return refuse(t('rereview_operator'))
        key = escalation['key']
        changed = changed_input(self._current_key(number, key, target), key)
        if changed:
            return refuse(t('superseded', changed=', '.join(f'`{k}`' for k in changed)))
        pr = self._fresh_pr(target, labels, fresh, comments)
        route, why = self._supervised(number, TOOL_FAILURE, 'review', pr, key.get('reviewer'),
                                      key=key, any_merge=True)
        if route is None:
            return refuse(t('not_supervisors', why=why or t('no_supervisor')))
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
                     round_comments=[], acceptance=[], hold=None, granted_rounds=0,
                     operator_rounds=0)
        if pr.meta:
            self._read_thread(pr, comments)
        return pr

    def _granted_rounds(self, comments, operator=False):
        """How many review rounds were granted on a PR (#405): the dispatcher's
        ``supervision`` records of a ``grant-round`` that was granted, in its ``comments``,
        the supervisor's and those granted on an escalation to the operator (#553) together.
        ``operator``: only the latter, outside the caps of the delegation (``operator`` true
        in the record)."""
        return sum(1 for _, r in self._dispatcher_records(comments, 'supervision')
                   if r.get('command') == GRANT_ROUND and r.get('outcome') == 'granted'
                   and (not operator or r.get('operator') is True))

    @staticmethod
    def _delegated_rounds(pr):
        """The rounds granted to ``pr`` by the supervisor's delegation (#405): those that count
        against its cap, without the ones granted on an escalation to the operator (#553)."""
        return pr.granted_rounds - pr.operator_rounds

    def _round_resume(self, pr):
        """The label a PR at its round limit goes back to when a round is granted (#405)."""
        lb = self.cfg.labels
        return lb.changes if lb.changes in pr.labels else lb.review

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
        cfg = self.cfg
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
        delegated = self._delegated_rounds(pr)
        t = self._texts()
        why = (t('cap_pr', granted=delegated, cap=cap) if delegated >= cap
               else t('grants_unknown') if today is None
               else t('cap_day', today=today, cap=daily_cap) if today >= daily_cap
               else t('owners') if pr.finding_problems or state.problems
               else t('security', keys=', '.join(f'`{k}`' for k in security)) if security
               else t('no_progress')
               if not (last_counted_ci(reviews, **waiver(cfg, pr.fixes, pr.closures))
                       or findings_decreasing(reviews, pr.closures))
               else '')
        if why:
            return None, t('rounds_why', why=why)
        return {'head': pr.head_sha, 'resume': self._round_resume(pr),
                'granted': delegated}, ''

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
        lb, t = self.cfg.labels, self._texts()
        by = command['by']
        head = fresh.get('headRefOid') or target.head_sha
        resume = escalation.get('resume')
        resume = resume if resume in (lb.review, lb.changes) else lb.review
        if head != escalation.get('head'):
            return trace('refused', t('refused', why=t('head_moved')) + ' '
                         + t('spent', stays=t('escalation_stays')) + '\n\n'
                         + self._supervisor_resume(escalation, True, superseded=True))

        def refuse(why):
            trace('refused', t('refused', why=why))
            # The head and the label to resume go with the escalation, so that a trusted user
            # may grant the round there (#553).
            return self._to_operator(number, t('to_operator_round', why=why, command=GRANT_ROUND,
                                               by=by,
                                               rounds=operator_rounds_text(self.cfg.language)),
                                     'round_limit', extra={'head': head, 'resume': resume})

        pr = self._fresh_pr(target, labels, fresh, comments)
        route, why = self._round_grant(pr, pr.reviews)
        if route is None:
            return refuse(why or t('rounds_off'))
        rules = self.cfg.delegations
        # The daily cap is checked again and the grant recorded in one step (#405): another
        # grant applied in the meantime, on another PR, may have taken the last one.
        daily = self.ledger.journal.grant('rounds', number, command['comment'], by,
                                          rules.max_grants_per_day)
        if daily is None:
            return refuse(t('rounds_why', why=t('daily_reached', cap=rules.max_grants_per_day)))
        # The cap of the delegation counts its own rounds only, not those granted on an
        # escalation to the operator (#553); the limit of the PR counts both.
        granted = self._delegated_rounds(pr) + 1
        figures = {'head': head, 'granted': granted, 'cap': rules.max_rounds_per_pr,
                   'left': rules.max_rounds_per_pr - granted, 'daily': daily,
                   'daily_cap': rules.max_grants_per_day,
                   'daily_left': rules.max_grants_per_day - daily}
        total = pr.granted_rounds + 1
        limit = self.cfg.max_review_rounds + total
        trace('granted', t('round_granted', limit=limit, max=self.cfg.max_review_rounds,
                           total=total, granted=granted, cap=rules.max_rounds_per_pr,
                           daily=daily, daily_cap=rules.max_grants_per_day, resume=resume)
              + f'\n\n{t("causes_title")}\n\n' + quoted(command['reason']),
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
        cfg, t = self.cfg, self._texts()
        cap = cfg.delegations.max_grants_per_day
        today = self._grants_today()
        if today is None:
            return None, t('grants_unknown')
        if today >= cap:
            return None, t('cap_day', today=today, cap=cap)
        if not isinstance(agent, str) or agent not in cfg.agents or not isinstance(kind, str):
            return None, t('turn_unknown', agent=agent)
        try:
            story, extension, why = budget_grantable(
                cfg.budget, self.ledger.journal, repo=cfg.repo, issue=issue, kind=kind,
                agent=cfg.agent(agent), language=cfg.language)
        except JournalError as exc:
            return None, t('journal_unreadable', error=exc)
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
        t = self._texts()
        route, why = self._supervised(number, BUDGET_GRANTABLE, step, pr, reviewer, key=key)
        if route is None:
            return None, why and t('budget_why', why=why)
        issue = pr.issue if pr is not None else number
        found, why = self._budget_extension(issue, exc.kind or step, exc.agent)
        if found is None:
            return None, t('budget_why', why=why)
        return dict(route, **found, category=exc.category), ''

    def _pending_extension(self, number, comments, is_pr, operator=False):
        """The extension of the budget envelope that the journal recorded for a command on
        issue or PR ``number`` and whose records a stopped or failed run did not write (#411):
        ``{escalation, command, grant, daily, resume}``, or None.

        It is pending when a ``budget_grantable`` escalation of the dispatcher to the supervisor,
        or a ``budget`` escalation to the operator that records its story (#595), in
        ``comments``, any of them and not only the latest, names a story whose one extension
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
            # A budget escalation to the operator records its story too since #595: the
            # coordinator's extension on it (``_operator_grant_budget``) is completed the same
            # way, its records only, since the item is with the operator.
            if (record.get('class') == BUDGET_GRANTABLE and record.get('to') == 'supervisor'
                    or record.get('class') == BUDGET) and isinstance(story, str):
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
                  and escalation.get('class') == BUDGET_GRANTABLE
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
        lb, t = self.cfg.labels, self._texts()
        rules = self.cfg.delegations
        by = command['by']
        is_pr = fresh is not None
        carry = ({k: escalation[k] for k in ('step', 'key', 'resume') if k in escalation}
                 if self._retryable(escalation, is_pr) else None)

        def refuse(why):
            trace('refused', t('refused', why=why))
            return self._to_operator(number, t('to_operator_budget', why=why,
                                               command=GRANT_BUDGET, by=by),
                                     BUDGET, remove, carry, extra=budget_fields(escalation))

        # An issue a merged devloop PR already resolved is never implemented again (#228), as
        # for a retry: the command becomes a merged_issue escalation, and nothing is extended.
        merged = [] if is_pr else self._merged_prs(number)
        if merged:
            trace('refused', self._merged_refusal(merged))
            return self._do_merged_issue(number, merged)
        key, step = escalation.get('key'), escalation.get('step')
        if not rules.budget:
            return refuse(t('budget_off'))
        if carry is None:
            return refuse(t('no_turn_step'))
        current = self._current_key(number, key, target if is_pr else None)

        def superseded():
            changed = ', '.join(f'`{k}`' for k in sorted(current) if current[k] != key.get(k))
            return trace('refused', t('refused', why=t('superseded_grant', changed=changed))
                         + ' ' + t('spent', stays=t('escalation_stays')) + '\n\n'
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
            return refuse(t('budget_why', why=why) if why else t('no_longer_auto'))
        if current != key:
            return superseded()
        found, why = self._budget_extension(pr.issue if is_pr else number,
                                            escalation.get('kind'), escalation.get('agent'))
        if found is None:
            return refuse(t('budget_why', why=why))
        story, extension = found['story'], found['extension']
        if story != escalation.get('story'):
            return refuse(t('other_story', story=story, expected=escalation.get('story')))
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
            return refuse(t('story_spent', story=story))
        if problem == 'lapsed':
            return refuse(t('extension_lapsed'))
        if problem:
            return refuse(t('daily_reached', cap=rules.max_grants_per_day))
        return self._finish_extension(number, is_pr, comments, escalation, command, trace,
                                      {'story': story, 'extension': extension}, daily)

    def _finish_extension(self, number, is_pr, comments, escalation, command, trace, grant,
                          daily, recovered=False, resume=True, operator=False, remove=None):
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
        (``trace_only``, ``_operator_anchor``), or behind a later escalation; and on the
        coordinator's extension on a ``budget`` escalation to the operator (#595), whose step
        resumes then with a ``retry``.

        ``remove``: the labels a resumed item loses, ``agent:needs-supervisor`` by default; the
        operator labels for the coordinator's extension on an escalation to the operator
        (``_operator_grant_budget``). The coordinator's records carry ``role: coordinator``."""
        lb = self.cfg.labels
        rules = self.cfg.delegations
        by, ident, step = command['by'], command['comment'], escalation.get('step')
        story, extension = grant['story'], grant['extension']
        remove = [lb.needs_supervisor] if remove is None else remove
        merged = self._merged_prs(number) if resume and recovered and not is_pr else []
        if resume and recovered and not is_pr and not merged and self._issue_blocked(number):
            return None
        if recovered:
            self.log(f'  the extension of the command of comment {ident} is in the journal '
                     'already: completing its records')
        figures = {'story': story, 'extension': extension, 'daily': daily,
                   'daily_cap': rules.max_grants_per_day,
                   'daily_left': rules.max_grants_per_day - daily}
        if self._is_coordinator(by):
            figures['role'] = COORDINATOR  # #595
        # The ledger event before the supervision record, which spends the command: a run
        # stopped between the two finds the event and does not write it again.
        if not self._granted_event(comments, ident):
            self.ledger.event(number, 'delegation_grant', delegation='budget', by=by,
                              comment=ident, **figures)
        extra = {}
        t = self._texts()
        if not resume:
            held = lb.needs_operator if operator else lb.needs_supervisor
            where = t('stays_there', what=self._where(is_pr, capital=True), held=held,
                      how=t('where_operator' if operator else 'where_latest'), step=step)
            extra['trace_only'] = True
        elif is_pr:
            label = escalation.get('resume')
            label = label if label in (lb.review, lb.changes) else lb.review
            where = t('pr_back', label=label)
        elif merged:
            where = t('merged_after', merged=', '.join(f'#{m["number"]}' for m in merged),
                      step=step)
        else:
            where = t('step_now', step=step)
        if recovered:
            where = t('recovered', where=where)
        trace('granted', t('extended', story=story,
                           extension=extension_text(extension, self.cfg.language),
                           percent=extension['percent'], daily=daily,
                           cap=rules.max_grants_per_day, where=where)
              + f'\n\n{t("causes_title")}\n\n' + quoted(command['reason']),
              **figures, **extra)
        if not resume:
            return None
        if merged:
            return self._do_merged_issue(number, merged)
        if is_pr:
            self.gh.edit_labels(number, add=[label], remove=remove)
            return None
        self.gh.edit_labels(number, remove=remove)
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
        (fail closed).

        The coordinator's identity (#595) has the same delegation, on with
        ``[roles.coordinator]`` whatever ``[delegations] capacity`` says: its grants are in force
        while the table is on (``_capacity_limits``)."""
        cfg = self.cfg
        issue = cfg.report.issue
        limits = {}
        active = cfg.delegations.capacity or cfg.coordinator is not None
        if issue and (active or not dry_run):
            try:
                comments = self.gh.issue_comments(issue)
                if not dry_run and self._capacity_commands(issue, comments):
                    comments = self.gh.issue_comments(issue)
                if active:
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
                         f'a temporary limit of {limits[key]["by"]} (configured: '
                         f'{getattr(cfg, key)})')
        self._capacity_seen = limits
        if not limits:
            return cfg
        return replace(cfg, **{key: limit['value'] for key, limit in limits.items()})

    def _capacity_limits(self, comments):
        """The temporary capacity limits in force, from the ``comments`` of ``[report] issue``:
        ``{key: {value, until, by}}``. Each comes from the dispatcher's ``supervision`` record
        of a ``capacity`` command it ``granted``; for each key the latest grant replaces the
        earlier ones, and is in force until its ``until``. A grant whose value is above the cap
        the configuration sets now (lowered after the grant) is not applied; for a grant of the
        coordinator's identity the cap is the code's (``MAX_CAPACITY``, #611), never the lower
        one of ``[delegations]``. A grant of the
        coordinator's identity (``role: coordinator``, #595) counts only with
        ``[roles.coordinator]`` on, any other only with ``[delegations] capacity`` on: turning
        either off revokes its grants. The latest grant of a key is chosen first, and only then
        checked: one that does not apply leaves the configured value, never an earlier grant it
        replaced."""
        rules, now = self.cfg.delegations, self.clock()
        latest, roles = {}, {}
        for _, record in self._dispatcher_records(comments, 'supervision'):
            if record.get('command') != CAPACITY or record.get('outcome') != 'granted':
                continue
            key, value, until = record.get('setting'), record.get('value'), record.get('until')
            if (rules.capacity_cap(key) is not None and isinstance(value, int)
                    and not isinstance(value, bool) and _epoch(until) is not None):
                latest[key] = {'value': value, 'until': until, 'by': record.get('by')}
                roles[key] = record.get('role') == COORDINATOR
        limits = {}
        for key, limit in latest.items():
            if _epoch(limit['until']) <= now:
                continue
            if not (self.cfg.coordinator is not None if roles[key] else rules.capacity):
                self.log(f'capacity: {key} = {limit["value"]} by {limit["by"]} is not applied: '
                         'its delegation is off')
                continue
            cap = MAX_CAPACITY[key] if roles[key] else rules.capacity_cap(key)
            if not 1 <= limit['value'] <= cap:
                self.log(f'capacity: {key} = {limit["value"]} is above the cap of the '
                         f'{"code" if roles[key] else "configuration"} ({cap}): not applied')
                continue
            limits[key] = limit
        return limits

    def _capacity_commands(self, issue, comments):
        """Answer the ``/devloop`` commands written on ``[report] issue`` after the latest one
        answered (#412), oldest first, each with a ``supervision`` record (``_capacity_command``)
        that spends it and every command before it. Only commands from the supervisor's
        identity or a trusted user count (``_may_hold``), and the coordinator's ``capacity``
        (#595); any other is ignored and only logged. Returns how many were answered."""
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
            if not (self._may_hold(login) or parsed[0] == CAPACITY
                    and self._coordinator_may(login, CAPACITY)):
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
        makes it apply from this tick until its ``until``; last the ledger ``event``.

        From the coordinator's identity (#595, ADR 0002, precision of 2026-11-23) the same
        conditions hold, except that ``[roles.coordinator]``, not ``[delegations] capacity``,
        turns the delegation on, and that the value and the duration are checked against the
        caps of the code only (``MAX_CAPACITY``, ``MAX_CAPACITY_HOURS``), not against the lower
        ones of ``[delegations]``, which bind the supervisor (#611): the coordinator decides
        beyond the delegation. Its records carry ``role: coordinator``."""
        if name == MERGE_BUDGET:
            return self._merge_budget_command(issue, comment, text, by)
        cfg = self.cfg
        rules = cfg.delegations
        ident = comment.get('id')
        args = capacity_arguments(text) if name == CAPACITY else None
        coordinator = self._is_coordinator(by)
        t = self._texts()

        def trace(outcome, what, **extra):
            if coordinator:
                extra['role'] = COORDINATOR
            record = dict(command=name, by=by, comment=ident, outcome=outcome,
                          reason=args[3] if args else text or '', **extra)
            self.gh.comment(issue, render_supervision(record, t('trace', name=name, by=by,
                                                                text=what.strip()),
                                                      t('capacity_title')))
            self.log(f'capacity: {name} by {by}: {outcome}')

        def refuse(why):
            return trace('refused', t('capacity_refuse', why=why, usage=t('capacity_usage')))

        if name != CAPACITY:
            return refuse(t('report_only', capacity=CAPACITY, merge_budget=MERGE_BUDGET))
        if not (rules.capacity or coordinator):
            return refuse(t('capacity_off'))
        if cfg.merge.mode != 'auto':
            return refuse(t('not_auto', mode=cfg.merge.mode))
        if args is None:
            return refuse(t('capacity_malformed'))
        key, value, minutes, _ = args
        if key in CAPACITY_REFUSED:
            return refuse(t('capacity_refused_key', setting=key))
        # The coordinator decides beyond the delegation, within the caps of the code (#611).
        cap = MAX_CAPACITY.get(key) if coordinator else rules.capacity_cap(key)
        hours = MAX_CAPACITY_HOURS if coordinator else rules.capacity_max_hours
        if cap is None:
            return refuse(t('capacity_unknown_key', setting=key))
        if not 1 <= value <= cap:
            return refuse(t('value_code' if coordinator else 'value_delegation', value=value,
                            setting=key, cap=cap))
        if minutes > hours * 60:
            return refuse(t('hours_code' if coordinator else 'hours_delegation', hours=hours))
        written = _epoch(comment.get('created_at'))
        if written is None:
            return refuse(t('time_expiry'))
        until = written + minutes * 60
        if until <= self.clock():
            return refuse(t('elapsed', written=iso(written)))
        journal = getattr(self.ledger, 'journal', None)
        try:
            daily = (None if journal is None else
                     journal.grant('capacity', issue, ident, by, rules.max_grants_per_day))
        except JournalError as exc:
            self.log(f'capacity: the grant cannot be recorded: {exc}')
            journal = None
        if journal is None:
            return refuse(t('grants_unknown'))
        if daily is None:
            return refuse(t('capacity_daily', cap=rules.max_grants_per_day))
        configured = getattr(cfg, key)
        figures = {'setting': key, 'value': value, 'configured': configured, 'cap': cap,
                   'minutes': minutes, 'until': iso(until), 'daily': daily,
                   'daily_cap': rules.max_grants_per_day,
                   'daily_left': rules.max_grants_per_day - daily}
        trace('granted', t('capacity_granted', setting=key, value=value, until=iso(until),
                           configured=configured,
                           of=t('of_code' if coordinator else 'of_delegation'), cap=cap,
                           capacity=CAPACITY, daily=daily, daily_cap=rules.max_grants_per_day)
              + f'\n\n{t("causes_title")}\n\n' + quoted(args[3]), **figures)
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
        t = self._texts()

        def trace(outcome, what, **extra):
            record = dict(command=MERGE_BUDGET, by=by, comment=ident, outcome=outcome,
                          reason=args[1] if args else text or '', **extra)
            self.gh.comment(issue, render_supervision(record, t('trace', name=MERGE_BUDGET,
                                                                by=by, text=what.strip()),
                                                      t('merge_budget_title')))
            self.log(f'merge budget: {MERGE_BUDGET} by {by}: {outcome}')

        def refuse(why):
            return trace('refused', t('merge_budget_refuse', why=why,
                                      usage=t('merge_budget_usage')))

        if by not in cfg.trusted_users:
            return refuse(t('merge_budget_trusted'))
        if not rules.merge:
            return refuse(t('merge_off'))
        if args is None:
            return refuse(t('merge_budget_malformed', max=MAX_MERGE_BUDGET))
        written = _epoch(comment.get('created_at'))
        if written is None:
            return refuse(t('time_day'))
        day, today = iso(written)[:10], iso(self.clock())[:10]
        if day < today:
            return refuse(t('day_over', day=day))
        n, _ = args
        before = sum(e['n'] for e in self._merge_budget_extensions(day))
        cap = rules.max_merges_per_day + before + n
        trace('granted', t('merge_budget_granted', day=day, n=n, cap=cap,
                           max=rules.max_merges_per_day)
              + f'\n\n{t("reason_title")}\n\n' + quoted(args[1]),
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
        instead. A push of the same step and key kept after a transient error of the remote
        (#645, ``_arm_pending_push``) makes the step push those commits again, without a new
        turn; on a PR whose branch already got that push (``_landed_push``), the step is
        completed now, with no new push."""
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
        trace('applied', self._texts()('retry_applied', step=step, attempt=done + 1,
                                       max=MAX_RETRIES), key=key, attempt=done + 1)
        # A push that failed for a transient error of the remote (#645) and whose commits the
        # dispatcher kept: the step, when it runs, pushes them again without a new turn.
        pending = self._arm_pending_push(number, step, key)
        if is_pr:
            resume = escalation.get('resume')
            resume = resume if resume in (lb.review, lb.changes) else lb.review
            self.gh.edit_labels(number, add=[resume], remove=remove)
            if pending is not None and self._landed_push(number, self.gh.pr_head(number)):
                # The push reached the branch although both attempts lost the answer: the step
                # is completed now, since the head it left may plan no step at all. A veto
                # written since suspends it instead, without the label just given back.
                self._complete_landed(pending, remove=[resume])
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
        lb, t = self.cfg.labels, self._texts()
        manual = t('manual', label=lb.needs_supervisor)
        if self.cfg.merge.mode != 'auto':
            return t('without_supervisor', manual=manual)
        cls = escalation.get('class')
        reason = t('reason_arg')
        valid = []
        if not superseded and not is_pr and cls == PREFLIGHT_DEFAULT:
            valid = [f'`/devloop {ACCEPT_DEFAULTS}`',
                     f'`/devloop {DECIDE} {t("decisions_arg")}`']
        elif (not superseded and cls in RETRY_CLASSES
              and self._retryable(escalation, is_pr)):
            valid = [f'`/devloop {RETRY}`']
        elif not superseded and is_pr and cls == ROUND_GRANTABLE:
            valid = [f'`/devloop {GRANT_ROUND} {reason}`']
        elif not superseded and cls == BUDGET_GRANTABLE:
            valid = [f'`/devloop {GRANT_BUDGET} {reason}`']
        valid.append(f'`/devloop {ESCALATE} {reason}`')
        return t('supervisor_resume', valid=t('or').join(valid), manual=manual)

    def _operator_resume(self, anchor, is_pr=None):
        """How the operator resumes an item in ``agent:needs-operator`` put there by
        ``anchor`` (``_supervisor_view``), for the answer to a command (#321), in the project's
        language (#633)."""
        record = anchor['record']
        language = self.cfg.language
        t = self._texts()
        resume = resume_text(language)
        if anchor['kind'] == 'preflight':
            return preflight_resume(language)
        if anchor.get('origin') == PREFLIGHT_DEFAULT and not is_pr:
            return resume + '\n\n' + t('preflight_alternative', accept=ACCEPT_DEFAULTS,
                                       decide=DECIDE)
        if record.get('event') == 'operator_merge':
            return t('operator_merge_resume')
        if record.get('event') == 'escalation' and is_pr is not None \
                and self._retryable(record, is_pr):
            return resume + '\n\n' + operator_retry_text(record['step'], language)
        if (is_pr and record.get('event') == 'escalation'
                and record.get('class') == 'round_limit' and isinstance(record.get('head'), str)):
            return resume + '\n\n' + operator_rounds_text(language)  # #553
        return resume

    def _operator_command(self, target, number, labels, comments, command, trace, fresh=None):
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
        - ``grant-round``: only on a ``round_limit`` escalation of a PR, on the head it recorded
          (``_operator_grant_round``, #553): one more round for that PR only.
        - ``grant-budget``: only from the coordinator's identity, on a ``budget`` escalation
          (``_operator_grant_budget``, #595); a trusted user's is answered: it is a delegation
          to the supervisor (#411), and the values of the envelope stay the operator's.
        - ``merge``: only from the supervisor's identity, on a PR (``_delegated_merge``, #486),
          or from the coordinator's, its reserved merge (#529); a trusted user's is answered:
          the operator merges by hand.
        - ``finding``: only from the coordinator's identity, on an ``open_findings`` escalation
          of a PR (``_coordinator_findings``, #606)."""
        lb, t = self.cfg.labels, self._texts()
        resume_ = resume_text(self.cfg.language)
        is_pr = isinstance(target, PullRequest)
        name, anchor = command['command'], command['anchor']
        kind, record = anchor['kind'], anchor['record']
        stays = t('spent', stays=t('item_stays', where=self._where(is_pr),
                                   label=lb.needs_operator))
        how = self._operator_resume(anchor, is_pr)

        def refuse(why, resume=how):
            return trace('refused', t('not_applicable', why=why, how=f'{stays}\n\n{resume}'))

        if name == MERGE:
            supervisor = self._supervisor_login()
            coordinator = self._is_coordinator(command['by'])
            if not coordinator and (not supervisor or command['by'] != supervisor):
                # A delegation of the operator to the supervisor (#486): the operator merges a
                # PR by hand, as before.
                return refuse(t('merge_delegation', merge=MERGE))
            if not is_pr:
                return refuse(t('merge_pr_only', merge=MERGE))
            return self._delegated_merge(target, number, comments, command, trace)
        if name == FINDING:
            # Only the coordinator's identity writes it as a command (#606): a trusted user's
            # line is the closure it always was, read once the PR is back in review.
            return self._coordinator_findings(target, number, labels, fresh, comments, command,
                                              trace, refuse)
        if name == ESCALATE:
            return refuse(t('already_operator'))
        if name == GRANT_ROUND:
            # One more round for this PR only, on a round_limit escalation (#553).
            return self._operator_grant_round(target, number, labels, fresh, comments, command,
                                              trace, refuse)
        if name == GRANT_BUDGET:
            if self._is_coordinator(command['by']):
                # The one extension of the story, delegated to the coordinator also on a budget
                # escalation to the operator (#595).
                return self._operator_grant_budget(target, number, labels, comments, command,
                                                   trace, refuse)
            # A delegation of the operator to the supervisor (#411): the values of the envelope
            # and the extensions beyond it stay the operator's (ADR 0004, D2).
            return refuse(t('grant_budget_delegation', command=GRANT_BUDGET,
                            cls=BUDGET_GRANTABLE))
        if name in (ACCEPT_DEFAULTS, DECIDE):
            # The check itself, or a supervisor's escalation of it handed to the operator.
            check = (kind == 'preflight' and preflight_class(record) == PREFLIGHT_DEFAULT
                     or anchor.get('origin') == PREFLIGHT_DEFAULT)
            if is_pr or not check:
                return refuse(t('decide_only'))
            if name == DECIDE:
                return self._decide(number, labels, comments, trace, command['reason'],
                                    operator=how,
                                    coordinator=self._is_coordinator(command['by']))
            return self._accept_defaults(number, labels, comments, trace, operator=how)
        if kind == 'preflight':
            return refuse(t('preflight_no_retry'))
        # A supervisor's escalation of a check's defaults or of a resolved issue, handed to the
        # operator, records no failed step, even when an earlier record kept its routing key.
        if (record.get('event') != 'escalation' or not self._retryable(record, is_pr)
                or anchor.get('origin') in (PREFLIGHT_DEFAULT, MERGED_ISSUE)):
            return refuse(t('not_step', where=self._where(is_pr)))
        key, step = record['key'], record['step']
        # The check that restarts an implementation waiting for its base (#459) runs on an
        # issue in agent:working, which keeps it.
        if step == 'preflight' and lb.ready not in labels and not (
                lb.working in labels and self._base_wait(comments, 'implement')):
            return refuse(t('preflight_ready', ready=lb.ready),
                          t('preflight_ready_resume', operator=lb.needs_operator,
                            ready=lb.ready))
        merged = [] if is_pr else self._merged_prs(number)
        if merged:
            return refuse(t('merged_why', merged=', '.join(f'#{m["number"]}' for m in merged)),
                          t('merged_resume'))
        # As for the supervisor, the base of an implementation does not bind its retry (#475).
        changed = changed_input(self._current_key(number, key, target if is_pr else None), key)
        if changed:
            return refuse(t('superseded', changed=', '.join(f'`{k}`' for k in changed)),
                          resume_)

        def limit(done):
            trace('limit', t('operator_limit', step=step, done=done, max=MAX_RETRIES,
                             stays=stays, resume=resume_), key=key)

        self._apply_retry(number, is_pr, comments, record,
                          sorted(labels & (lb.operator() | {lb.needs_supervisor})), trace, limit)

    def _coordinator_findings(self, target, number, labels, fresh, comments, command, trace,
                              refuse):
        """Apply the coordinator's ``/devloop finding`` on PR ``number`` in an ``open_findings``
        escalation to the operator (#606, ADR 0002, second precision of 2026-11-23, point 3):
        the closure of findings no agent can close any more, with the form of the operator's
        (``review.FINDING_COMMAND``). Its comment may hold one such line per finding, each with
        its key, ``resolved`` or ``withdrawn``, and its reason; the command applies whole or not
        at all. With ``pr`` and its thread read now (``_fresh_pr``) it is refused:

        - from any identity but the coordinator's, or on another escalation;
        - with a line without a reason, or malformed;
        - while a reviewer of the latest round does not approve the PR's head, or that round is
          not of the head: the coordinator closes a finding, it never overrides a verdict;
        - for a finding a ``trust_policy`` review opened or keeps open
          (``trust_policy_findings``): it stays the operator's;
        - for a finding not open on the PR.

        The closures are a ``finding`` record of the dispatcher (``by: coordinator``) in the
        comment of the ``supervision`` record that spends the command (``closed``), so that the
        one is never written without the other. When no finding is left open the PR goes back to
        ``agent:review``, where the recorded approval is applied again without a new review; the
        promotion and the merge follow their rules. Otherwise it stays with the operator, and
        the escalation still takes the coordinator's commands. ``refuse(why)`` answers why it
        does not apply."""
        lb, t = self.cfg.labels, self._texts()
        anchor = command['anchor']
        record = anchor['record']
        if not self._is_coordinator(command['by']):
            return refuse(t('finding_coordinator_only', finding=FINDING, review=lb.review))
        if (not isinstance(target, PullRequest) or anchor['kind'] != 'notice'
                or record.get('event') != 'escalation' or record.get('class') != OPEN_FINDINGS):
            return refuse(t('finding_open_only', finding=FINDING, cls=OPEN_FINDINGS))
        body = next((c.get('body') or '' for c in comments
                     if not c.get('native') and command['comment'] is not None
                     and c.get('id') == command['comment']), None)
        if body is None:
            body = f'{FINDING_COMMAND} {command["reason"]}'  # only the first line is known
        lines = finding_commands(body)
        if not lines or any(parsed is None for _, parsed in lines):
            return refuse(t('finding_form', form=FINDING_COMMAND))
        pr = self._fresh_pr(target, labels, fresh, comments)
        if pr.finding_problems:
            return refuse(t('finding_owners'))
        latest = current_round(pr.reviews)
        if not latest or latest[0]['head_sha'] != pr.head_sha:
            return refuse(t('finding_head', head=pr.head_sha[:12]))
        asking = [owner_of(r) for r in latest if flow_verdict(r) != 'approve']
        if asking:
            return refuse(t('finding_asking', round=latest[0]['round'],
                            who=', '.join(f'`{who}`' for who in asking)))
        state = finding_state(pr.reviews, pr.closures)
        still_open = {f['key'] for f in state.open}
        held = trust_policy_findings(pr.reviews)
        asked = {}
        for _, (key, status, reason) in lines:
            asked.setdefault(key, (status, reason))
        trust = [key for key in asked if key in held]
        if trust:
            return refuse(t('finding_trust', keys=', '.join(f'`{key}`' for key in trust)))
        absent = [key for key in asked if key not in still_open]
        if absent:
            return refuse(t('finding_absent', keys=', '.join(f'`{key}`' for key in absent)))
        author = command['by']
        closed = [{'key': key, 'status': status, 'by': CLOSED_BY_COORDINATOR, 'author': author,
                   'reason': reason, 'head_sha': pr.head_sha}
                  for key, (status, reason) in asked.items()]
        listed = '\n'.join(f'- `{c["key"]}`: {t(c["status"])}: '
                           + ' '.join(c['reason'].split()) for c in closed)
        left = [f['key'] for f in state.open if f['key'] not in asked]
        records = [('finding', {'closed': closed})]
        if left:
            return trace('closed', t('closed_left', listed=listed,
                                     left=', '.join(f'`{key}`' for key in left),
                                     label=lb.needs_operator),
                         records=records, head=pr.head_sha)
        trace('closed', t('closed_all', listed=listed, review=lb.review),
              records=records, head=pr.head_sha)
        self.gh.edit_labels(number, add=[lb.review],
                            remove=sorted(labels & (lb.operator() | {lb.needs_supervisor})))

    def _operator_grant_budget(self, target, number, labels, comments, command, trace, refuse):
        """Apply the coordinator's ``/devloop grant-budget <reason>`` on issue or PR ``number``
        in a ``budget`` escalation to the operator (#595, ADR 0002, precision of 2026-11-23):
        the one extension of the envelope of its story, under the conditions and caps of the
        supervisor's (``_grant_budget``, #411, ADR 0005, P5-7), whatever ``[delegations]
        budget`` says. ``refuse(why, resume)`` answers why it does not apply, and the item stays
        with the operator.

        It applies only when the latest record that put the item there is a ``budget``
        escalation, in merge mode ``auto`` (as the coordinator's ``capacity``, #611), that
        records the step of the refused turn with its key, and the story and the agent of that
        turn (written since #595; an older one is refused); on an issue no merged devloop PR resolved; on the input the escalation recorded (the PR's
        head, the issue's base and request); and when ``_budget_extension`` grants it now: the
        story never extended (a second extension stays the operator's), the extension covers
        the turn, the daily cap of the grants (``max_grants_per_day``) not reached. An issue
        blocked by an open issue waits, with nothing recorded nor spent. Granted, as
        ``_grant_budget``: the journal first, then the records and the labels
        (``_finish_extension``), the operator labels removed."""
        lb, t = self.cfg.labels, self._texts()
        is_pr = isinstance(target, PullRequest)
        anchor = command['anchor']
        record, by = anchor['record'], command['by']
        if not (anchor['kind'] == 'notice' and record.get('event') == 'escalation'
                and record.get('class') == BUDGET):
            return refuse(t('grant_budget_class', command=GRANT_BUDGET, budget=BUDGET,
                            grantable=BUDGET_GRANTABLE))
        if self.cfg.merge.mode != 'auto':
            return refuse(t('not_auto', mode=self.cfg.merge.mode))
        fields = budget_fields(record)
        if not self._retryable(record, is_pr) or not all(k in fields for k in ('story', 'agent')):
            return refuse(t('old_budget_escalation'))
        merged = [] if is_pr else self._merged_prs(number)
        if merged:
            return refuse(t('merged_why', merged=', '.join(f'#{m["number"]}' for m in merged)),
                          t('merged_resume'))
        key = record['key']
        current = self._current_key(number, key, target if is_pr else None)
        if current != key:
            changed = [k for k in sorted(current) if current[k] != key.get(k)]
            return refuse(t('superseded_grant', changed=', '.join(f'`{k}`' for k in changed)),
                          resume_text(self.cfg.language))
        # The kind of the refused turn, or its step, as for the supervisor's (``_budget_grant``).
        found, why = self._budget_extension(target.issue if is_pr else number,
                                            fields.get('kind') or record['step'], fields['agent'])
        if found is None:
            return refuse(t('not_granted', why=why))
        story, extension = found['story'], found['extension']
        if story != fields['story']:
            return refuse(t('other_story', story=story, expected=fields['story']))
        if not is_pr and self._issue_blocked(number):
            return None
        rules = self.cfg.delegations
        daily, problem = self.ledger.journal.extend(story, extension, number, command['comment'],
                                                    by, rules.max_grants_per_day,
                                                    reason=command['reason'] or '')
        if problem == 'spent':
            return refuse(t('story_spent_operator', story=story))
        if problem == 'lapsed':
            return refuse(t('lapsed_operator'))
        if problem:
            return refuse(t('daily_reached', cap=rules.max_grants_per_day))
        return self._finish_extension(number, is_pr, comments, record, command, trace,
                                      {'story': story, 'extension': extension}, daily,
                                      remove=sorted(labels & (lb.operator()
                                                              | {lb.needs_supervisor})))

    def _operator_grant_round(self, target, number, labels, fresh, comments, command, trace,
                              refuse):
        """Apply a trusted user's ``/devloop grant-round <reason>`` on PR ``number`` in a
        ``round_limit`` escalation to the operator (#553, approved by the operator on the
        issue): one more review round for this PR only, never a change of
        ``max_review_rounds``, outside the caps of the supervisor's delegation, which it does
        not touch. The reason is required (``_supervisor_view``). ``refuse(why, resume)``
        answers why it does not apply, and the PR stays with the operator.

        It applies only when the latest record that put the PR there is a ``round_limit``
        escalation (any other class, or none, is refused) and only on the head that escalation
        recorded: a head moved since, or an escalation written before #553 that records none,
        is refused. From the coordinator's identity (#528) at most
        ``MAX_COORDINATOR_OPERATOR_ROUNDS`` such rounds per PR, whoever granted the others:
        any further one stays the operator's.

        Granted, it is recorded as a round of the delegation: a ``grant`` in the journal of the
        turns first (``operator_grant``, outside the daily cap; without a journal it does not
        apply), then the ``supervision`` record with outcome ``granted`` and ``operator`` true,
        which raises the limit of the PR and spends the command, then the ledger ``event``
        ``delegation_grant`` of the delegation ``operator_rounds``, each with its author; last
        the labels: the PR goes back to the label the escalation recorded (``agent:review`` or
        ``agent:changes``)."""
        lb, t = self.cfg.labels, self._texts()
        resume_ = resume_text(self.cfg.language)
        anchor = command['anchor']
        record, by = anchor['record'], command['by']
        if not (isinstance(target, PullRequest) and anchor['kind'] == 'notice'
                and record.get('event') == 'escalation' and record.get('class') == 'round_limit'):
            return refuse(t('grant_round_class', command=GRANT_ROUND))
        head = (fresh or {}).get('headRefOid') or target.head_sha
        if not isinstance(record.get('head'), str):
            return refuse(t('no_head'), resume_)
        if head != record['head']:
            return refuse(t('head_moved'), resume_)
        coordinator = self._is_coordinator(by)
        before = self._granted_rounds(comments, operator=True)
        if coordinator and before >= MAX_COORDINATOR_OPERATOR_ROUNDS:
            return refuse(t('coordinator_rounds', before=before,
                            max=MAX_COORDINATOR_OPERATOR_ROUNDS), resume_)
        journal = getattr(self.ledger, 'journal', None)
        if journal is None:
            return refuse(t('no_journal'), resume_)
        try:
            journal.operator_grant(number, command['comment'], by)
        except JournalError as exc:
            self.log(f'  the grant cannot be recorded: {exc}')
            return refuse(t('journal_failed'), resume_)
        granted = before + 1
        figures = {'head': head, 'granted': granted, 'operator': True}
        if coordinator:
            figures.update(role=COORDINATOR, cap=MAX_COORDINATOR_OPERATOR_ROUNDS,
                           left=MAX_COORDINATOR_OPERATOR_ROUNDS - granted)
        total = self._granted_rounds(comments) + 1
        limit = self.cfg.max_review_rounds + total
        resume = record.get('resume')
        resume = resume if resume in (lb.review, lb.changes) else lb.review
        trace('granted', t('operator_round_granted', limit=limit,
                           max=self.cfg.max_review_rounds, total=total, granted=granted,
                           resume=resume)
              + f'\n\n{t("reason_title")}\n\n' + quoted(command['reason']),
              **figures)
        self.ledger.event(number, 'delegation_grant', delegation=OPERATOR_ROUNDS, by=by,
                          comment=command['comment'], **figures)
        self.gh.edit_labels(number, add=[resume],
                            remove=sorted(labels & (lb.operator() | {lb.needs_supervisor})))

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
        t = self._texts()
        record = self._decidable_check(number, labels, comments, trace, t('defaults_refusal'),
                                       t('defaults_what'), operator)
        if record is None:
            return
        defaults = [{'description': g['description'], 'proposed_default': g['proposed_default']}
                    for g in record['gaps']]
        listed = '\n'.join(f'- {d["description"].strip()}\n  {t("accepted_default")} '
                           f'{d["proposed_default"].strip()}' for d in defaults)
        trace('accepted', t('accepted') + f'\n\n{listed}', defaults=defaults,
              hash=record['hash'])
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
        t = self._texts()
        record = self._decidable_check(number, labels, comments, trace, t('decide_refusal'),
                                       t('decide_what'), operator, coordinator)
        if record is None:
            return
        gaps = [{'description': g['description'], 'proposed_default': g['proposed_default']}
                for g in record['gaps']]
        listed = '\n'.join(f'- {g["description"].strip()}\n  {t("proposed_default")} '
                           f'{g["proposed_default"].strip()}' for g in gaps)
        trace('decided', t('decided', listed=listed, quoted=quoted(text.strip())),
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
        decides and its verb (``'the defaults refer'``), for the refusal of a superseded
        check. ``operator``: a command on an escalation to the operator (#321), refused with an
        answer only, the issue staying in ``agent:needs-operator``; a string is how the answer
        says to resume (``_operator_resume``), ``preflight_resume`` otherwise. Every text in the
        project's language (#633).

        ``coordinator``: a ``decide`` of the coordinator's identity (#528), which decides no gap
        of the trust policy, of the credentials or of the budget. The last two are never
        ``default_available`` (the readiness check classifies them as ``operator_decision``,
        refused above); the first is refused here unless the check declares ``trust_policy``
        ``none``, and a check that does not declare it is refused too (it fails closed)."""
        t = self._texts()
        history = self.gh.issue_history(number)
        records = self._preflight_records(comments)
        record = records[-1][1] if records else None
        problem = (t('no_check') if record is None or record['ready']
                   else t('not_defaults') if preflight_class(record) != PREFLIGHT_DEFAULT
                   else t('design', label=DESIGN_LABEL) if DESIGN_LABEL in labels
                   else t('issue_author') if not self._devloop_author(history['author'])
                   else self._untrusted_change(history)
                   or (t('coordinator_trust')
                       if coordinator and record.get('trust_policy') != 'none' else None))
        stays = (t('item_stays', where=t('the_issue'), label=self.cfg.labels.needs_operator)
                 + '\n\n' + (operator if isinstance(operator, str)
                             else preflight_resume(self.cfg.language)) if operator
                 else t('escalation_stays') + '\n\n'
                 + self._supervisor_resume({'class': PREFLIGHT_DEFAULT}, False, superseded=True))
        if problem:
            if operator:
                trace('refused', t('refused', why=problem) + ' ' + t('spent', stays=stays))
                return None
            trace('refused', t('refused', why=problem))
            self._to_operator(number, f'{refusal}: {problem}.', 'operator_decision')
            return None
        if record['hash'] != self._request_hash(history, comments):
            trace('refused', t('refused', why=t('text_changed', what=what)) + ' '
                  + t('spent', stays=stays))
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
        state = finding_state(pr.reviews, pr.closures, self.cfg.language)
        pr.finding_problems = problems + state.problems
        pr.commands = self._finding_commands(pr, comments, state)
        policy = self._marker_entries(pr, comments, 'policy')
        pr.policy = [record for _, record in policy]
        pr.granted_rounds = self._granted_rounds(comments)
        pr.operator_rounds = self._granted_rounds(comments, operator=True)
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
