"""Escalation classes and the supervisor's commands (#182).

Every escalation of the dispatcher has a class, recorded in its marker. A class is either
non-critical, which the supervisor may decide in merge mode ``auto``, or critical, which always
goes to the operator. A class that is not known here is critical.

With ``[merge].mode = "auto"`` a non-critical escalation goes to ``agent:needs-supervisor``
instead of ``agent:needs-operator``; with ``off`` and ``strict`` every escalation goes to the
operator, as before. The supervisor answers with a command, the first line of a comment after
the attribution header:

- ``/devloop retry``: run the failed step again, without counting a round; at most
  ``MAX_RETRIES`` times per step and input, then the escalation becomes critical;
- ``/devloop accept-defaults``: accept the defaults the readiness check proposed;
- ``/devloop decide <text>``: decide the gaps of the readiness check otherwise (#258): the text,
  the rest of the comment, reaches the next check through the dispatcher's record;
- ``/devloop escalate <reason>``: hand the escalation to the operator;
- ``/devloop grant-round <reason>``: grant one more review round to a PR at the round limit, only
  on a ``round_grantable`` escalation (#405, ADR 0005, P5-1). The reason carries the analysis of
  the causes (rule 9 of section 3.1 of the ADR): without one the command is malformed. Granting
  rounds is a delegation of the operator, off by default (``[delegations] rounds``), within the
  caps below;
- ``/devloop grant-budget <reason>``: extend once the budget envelope of a story whose turn it
  refused, only on a ``budget_grantable`` escalation (#411, ADR 0005, P5-7): its turn caps and
  its time, by ``BUDGET_TOLERANCE_PERCENT`` of their values, never its dollars nor tokens. The
  reason carries the analysis of the causes, as for ``grant-round``. A delegation of the
  operator, off by default (``[delegations] budget``), within the daily cap below. The
  coordinator's identity grants it too (#595), also on a ``budget`` escalation to the operator,
  under the same conditions and caps;
- ``/devloop capacity <key> <value> <duration> <reason>``: a temporary capacity limit (#412, ADR
  0005, P5-8), only on the issue of the daily summary (``[report] issue``), never on an
  escalation: ``max_open_prs`` or ``max_parked_prs`` set to ``value`` for ``duration`` (``<N>h``
  or ``<N>m``), within the caps below, then the configured value again. The reason carries the
  analysis of the causes, as for ``grant-round``. A delegation of the operator, off by default
  (``[delegations] capacity``); for the coordinator's identity (#595) it is on with
  ``[roles.coordinator]``, under the same caps;
- ``/devloop merge <sha> <reason>``: merge a ready PR whose merge is the operator's (an
  ``operator_merge``, #248), only from the supervisor's identity and only when every condition
  of the delegation holds on ``sha``, its head (#486, ADR 0005, precision of 2026-11-21). A
  delegation of the operator, off by default (``[delegations] merge``), within its own daily cap
  (``MAX_DELEGATED_MERGES``), separate from the common one. From the coordinator's identity
  (``[roles.coordinator]``, #529, ADR 0002, precision of 2026-11-22) it is the coordinator's
  reserved merge: the conditions of the delegation without its exclusions for the trust rule,
  ``docs/adr/`` and the step on the host, an operator's ``/devloop approve`` on the issue when
  the trust policy or ``docs/adr/`` is touched, and a daily cap of its own
  (``MAX_COORDINATOR_MERGES``);
- ``/devloop merge-budget <N> <reason>``: only from a trusted user, on the issue of the daily
  summary (``[report] issue``): ``N`` more delegated merges for the UTC day the command is
  written, from 1 to ``MAX_MERGE_BUDGET``. The command of the supervisor or of an app does not
  count;
- ``/devloop rereview <reason>``: a new review round of a PR whose review failed (#491, a
  decision of the operator recorded in ADR 0005): only on a ``tool_failure`` escalation of a
  review, and only while a reviewer of the quorum of the latest round has no verdict. The new
  round chooses its reviewers with the current configuration, has a new receipt and counts
  against ``max_review_rounds``. When every reviewer of the quorum of that round has a verdict
  the command is refused, so that it never overrides one. Only the supervisor's identity uses
  it so: a trusted user's ``/devloop rereview`` stays the request of a new review it always was
  (``context.requests_rereview``), and is never a command of an escalation.

A trusted user may use the same commands on an escalation to the operator (#321), in any merge
mode: ``retry`` of a step it records, critical classes included, and ``accept-defaults`` or
``decide`` on a ``preflight_default`` readiness check. Since #553 also ``grant-round <reason>`` on
a ``round_limit`` escalation of a PR, on the head it recorded: one more round for that PR only,
outside the caps of the delegation and without changing ``max_review_rounds``. The coordinator's
identity may do the same at most ``MAX_COORDINATOR_OPERATOR_ROUNDS`` times per PR (the
operator's decision on #553); any further round on that PR stays the operator's. A command that
does not apply is always answered, with how to resume.

The coordinator's identity closes findings on an ``open_findings`` escalation to the operator
(#606, ADR 0002, second precision of 2026-11-23, point 3): ``/devloop finding
<round/reviewer/ID> resolved|withdrawn <reason>``, the form of the operator's closure, as the
first line of a comment, which may hold more such lines, one per finding. Each needs its reason;
a finding a ``trust_policy`` review opened or kept open stays the operator's, and so does every
finding while a reviewer of the latest round asks for changes on the head. When no finding is
left open the PR goes back to ``agent:review``, where the recorded approval is applied again.
"""

import re

from .i18n import ENGLISH, ITALIAN, Catalog, language_code

# Non-critical classes: the supervisor decides them in ``auto``.
TOOL_FAILURE = 'tool_failure'            # the agent or its model produced no result
PREFLIGHT_DEFAULT = 'preflight_default'  # readiness gaps all ``default_available``
SYNC_FAILED = 'sync_failed'              # a sync failed, no conflict on a sensitive path
MERGED_ISSUE = 'merged_issue'            # a ready issue that a merged devloop PR resolved (#228)
# A PR at the round limit whose next round the supervisor may grant (#405, ADR 0005, P5-1): only
# with ``[delegations] rounds`` on and every condition of the delegation verified.
ROUND_GRANTABLE = 'round_grantable'
# A turn the budget envelope of its story refused at a limit that the supervisor's one extension
# would cover (#411, ADR 0005, P5-7): only with ``[delegations] budget`` on and every condition
# of the delegation verified.
BUDGET_GRANTABLE = 'budget_grantable'
NON_CRITICAL = (TOOL_FAILURE, PREFLIGHT_DEFAULT, SYNC_FAILED, MERGED_ISSUE, ROUND_GRANTABLE,
                BUDGET_GRANTABLE)

# Critical classes: always to the operator. Any other value is critical too.
CRITICAL = {
    'round_limit': 'limite dei round di review',
    'operator_decision': 'decisione dell\'operatore chiesta dal controllo di prontezza',
    'untrusted': 'autore non fidato o testo modificato dopo `agent:ready`',
    'reviewer_decision': 'un reviewer chiede una decisione dell\'operatore',
    'open_findings': 'finding aperti che nessun agent può più chiudere',
    'review_level': 'reviewer indipendenti insufficienti per il livello di review',
    'level_zero': 'promozione al livello 0 non possibile',
    'protected_paths': 'modifica a `protected_paths`',
    'sync_sensitive': 'conflitti di sincronizzazione su percorsi sensibili',
    'tests_failed': 'test che falliscono, o nessuna modifica',
    'blocked': 'l\'agent si è dichiarato bloccato',
    'interrupted': 'implementazione interrotta',
    'unattributable': 'finding senza proprietario ricostruibile',
    'lost_meta': 'marker della PR perso',
    'inconsistent': 'stato della PR incoerente',
    'receipt_conflict': 'ricevuta della PR in contraddizione con i marker',
    'configuration': 'configurazione da decidere',
    'failure': 'errore di `gh`, di git o del dispatcher',
    'retry_limit': 'tentativi del supervisore esauriti',
    'supervisor_escalated': 'passata all\'operatore dal supervisore',
    'supervisor_refused': 'comando del supervisore non ammesso per questa escalation',
    'conclave_veto': 'decisione del conclave annullata da un utente fidato con `/devloop veto`',
    'uncertain_turn': 'turni senza esito da riconciliare con `/devloop reconcile`',
    'attempt': 'tentativo della PR non confermato dal journal, o già concluso',
    'budget': 'turno non coperto dall\'envelope di budget della storia',
    'host_tests': 'test rossi in locale anche sulla base, verdi nella sua CI',
}
UNCERTAIN_TURN = 'uncertain_turn'  # a turn refused by the block on uncertain outcomes (#362)
# An approval that leaves open findings no agent can close any more: the operator's, and the
# coordinator's but for those the trust policy holds (#606).
OPEN_FINDINGS = 'open_findings'
# Tests red on the branch and on its base commit, green in the CI of that commit (#497).
HOST_TESTS = 'host_tests'

# The steps a ``retry`` can run again, and the PR label each one resumes from.
RETRY_STEPS = ('preflight', 'implement', 'review', 'fix', 'sync')
ISSUE_STEPS = ('preflight', 'implement')
# The fields of a retry key that do not bind a retry of its step (#475): an implementation starts
# again from the current base, with the work its timed-out turn saved reapplied, so a base branch
# that moved since the escalation neither supersedes the retry nor starts a new count of retries.
UNBOUND = {'implement': ('base',)}
MAX_RETRIES = 2
# The caps of the supervisor's delegations (#405, ADR 0005, D4): constants of the code, which the
# configuration and the local overlay may only lower (``[delegations]``), never raise. The rounds
# granted to one PR, and the grants of one UTC day, every delegation together, retries excluded.
MAX_GRANTED_ROUNDS = 2
MAX_DAILY_GRANTS = 3
# The rounds granted on an escalation to the operator (``round_limit``, #553) are outside those
# caps: a trusted user grants them one at a time, each with its reason. The coordinator's identity
# may grant at most this many of them on one PR, whoever granted the others (the operator's
# decision on #553): any further one stays the operator's.
MAX_COORDINATOR_OPERATOR_ROUNDS = 1
# What the escalation of a round limit to the operator says of how to grant more rounds (#553),
# by language (#633): ``operator_rounds_text``. The Italian one for the texts of the dispatcher
# that #628 has not translated yet.
_OPERATOR_ROUNDS = {
    ENGLISH: ('A trusted user may grant one more round to this PR only with `/devloop grant-round '
              '<reason>`, on the head of this escalation; by raising `max_review_rounds` the '
              'operator grants more rounds to every PR instead.'),
    ITALIAN: ('Un utente fidato può concedere un round in più a questa sola PR con '
              '`/devloop grant-round <motivo>`, sul head di questa escalation; alzando '
              '`max_review_rounds` l\'operatore concede invece altri round a tutte le PR.'),
}
OPERATOR_ROUNDS_TEXT = _OPERATOR_ROUNDS[ITALIAN]


def operator_rounds_text(language=ENGLISH):
    """How a trusted user grants one more round on a ``round_limit`` escalation (#553), in
    ``language`` (#633)."""
    return _OPERATOR_ROUNDS[language_code(language)]


# The tolerance of the budget envelope (#411, ADR 0005, D3): the one extension of a story adds
# this share of each of its turn caps and of its time, rounded down (``floor``), so that it never
# goes beyond it. A constant of the code, like the caps above.
BUDGET_TOLERANCE_PERCENT = 25
# The caps of the temporary capacity limits (#412, ADR 0005, P5-8, D4 and the operator's decision
# on the issue): the keys the supervisor may set with ``/devloop capacity``, each up to its cap,
# for at most ``MAX_CAPACITY_HOURS``. Only keys devloop reads again without a restart:
# ``max_parallel_actions`` sizes the workers when ``devloop run`` starts, and is refused.
MAX_CAPACITY = {'max_open_prs': 6, 'max_parked_prs': 6}
MAX_CAPACITY_HOURS = 24
CAPACITY_REFUSED = ('max_parallel_actions',)
# The delegated merges (#486, ADR 0005, precision of 2026-11-21): at most so many per UTC day, a
# cap of their own that does not take the common one of ``MAX_DAILY_GRANTS`` (a deliberate
# exception to it, decided by the operator). A constant of the code, like the caps above: the
# configuration may only lower it. A trusted user extends the cap of one day with ``/devloop
# merge-budget <N>``, ``N`` from 1 to ``MAX_MERGE_BUDGET``; the commands of a day add up.
MAX_DELEGATED_MERGES = 5
MAX_MERGE_BUDGET = 20
# The reserved merges of the coordinator (#529, ADR 0002, precision of 2026-11-22): at most so
# many per UTC day, a cap of their own, apart from the delegated merges of the supervisor. A
# constant of the code: ``[roles.coordinator] max_merges_per_day`` may only lower it, and no
# command extends it.
MAX_COORDINATOR_MERGES = 10
# The trust rule itself (#486, the operator's decision of 2026-11-21): a delegated merge is
# refused when the diff touches one of these files, this one included, since it holds the list.
# A constant of the code: the configuration may only add to it (``[delegations]
# merge_trust_paths``), and changing it is itself a change of the rule.
TRUST_RULE_PATHS = ('AGENTS.md', 'CLAUDE.md', 'devloop/prompts/review.md',
                    'devloop/prompts/advisory.md', 'devloop/prompts/preflight.md',
                    'devloop/policy.py', 'devloop/supervision.py', 'devloop/context.py',
                    'ops/host/ssdlc/project.toml')
# Where the delegations, their caps, ``operator_only_paths`` and the conditions of the merge are
# set and applied: a delegated merge never changes them either (the same decision). devloop
# cannot tell from the diff alone whether a change of these files touches them, so it refuses
# every change of theirs (it fails closed).
DELEGATION_PATHS = ('devloop/config.py', 'devloop/config.toml', 'devloop/dispatch_merge.py',
                    'devloop/dispatch_supervision.py')
# The directory of the ADRs: a delegated merge admits there only added lines, each block of them
# with the link to a comment of the repository by a trusted user (#486).
ADR_DIR = 'docs/adr/'
# The opinions of the advisory review of the head (#369) that admit a delegated merge: never
# ``contrario``, and never none.
MERGE_OPINIONS = ('ok', 'dubbi')
# An issue with this label is in a design phase: its escalations stay critical.
DESIGN_LABEL = 'phase:design'

RETRY, ACCEPT_DEFAULTS, DECIDE, ESCALATE = 'retry', 'accept-defaults', 'decide', 'escalate'
# The coordinator's closure of findings on an ``open_findings`` escalation (#606): the form of the
# operator's ``/devloop finding`` (``review.FINDING_COMMAND``), applied as a command of the
# escalation. Not in ``COMMANDS``: a trusted user's line stays the closure it always was, read
# while the PR is in ``agent:review``, ``agent:changes`` or ``agent:ready-to-merge``.
FINDING = 'finding'
GRANT_ROUND = 'grant-round'
GRANT_BUDGET = 'grant-budget'
CAPACITY = 'capacity'
MERGE = 'merge'
MERGE_BUDGET = 'merge-budget'
REREVIEW = 'rereview'  # the supervisor's, on a review that failed (#491)
COMMANDS = (RETRY, ACCEPT_DEFAULTS, DECIDE, ESCALATE, GRANT_ROUND, GRANT_BUDGET, CAPACITY,
            MERGE, MERGE_BUDGET, REREVIEW)
# The commands that need a text: without one they are malformed.
NEEDS_TEXT = (DECIDE, ESCALATE, GRANT_ROUND, GRANT_BUDGET, CAPACITY, MERGE, MERGE_BUDGET,
              REREVIEW)
# The commands whose text is the rest of the comment, not only the rest of its first line.
LONG_TEXT = (DECIDE, GRANT_ROUND, GRANT_BUDGET, CAPACITY, MERGE, MERGE_BUDGET, REREVIEW)
# The closed perimeter of the coordinator's commands (#528, the operator's decision recorded in
# ADR 0002, precision of 2026-11-22): from the identity of ``[roles.coordinator]`` only the
# delegated commands count, each as a trusted user's, under the same conditions and caps; the
# reserved ones are the operator's in person, and any other command, ``escalate`` included, is
# refused (it fails closed). A constant of the code, like the trust rule: the configuration
# cannot widen it. ``merge`` is the coordinator's reserved merge (#529, point 3b of #524), under
# conditions of its own (``MergeMixin._delegated_merge``). The resume with the labels
# (``agent:ready`` added again) and its priority labels (#595) are in ``Dispatcher._admitters``.
# ``capacity`` and ``grant-budget`` are delegated since #595 (ADR 0002, precision of 2026-11-23):
# ``capacity`` within the caps of the supervisor's delegation, on with ``[roles.coordinator]``
# whatever ``[delegations] capacity`` says (``_capacity_command``); ``grant-budget`` the one
# extension of a story, on a ``budget_grantable`` escalation as the supervisor's and on a
# ``budget`` escalation to the operator (``_operator_grant_budget``). ``finding`` since #606 (ADR
# 0002, second precision of 2026-11-23, point 3): the closure of findings on an ``open_findings``
# escalation, never of one the trust policy holds, nor while a reviewer asks for changes on the
# head (``_coordinator_findings``). ``vote`` is the substitute vote in a conclave waiting for it
# (#605, ADR 0002, second precision of 2026-11-23, point 3), read only there
# (``ConclaveMixin._conclave_substitute``), never on an escalation.
# ``authorize`` is the authorization of an issue (#607, same precision, point 1), read only by
# ``Dispatcher._do_authorize`` (``authorization``), never on an escalation; from any other
# identity it is refused and recorded.
COORDINATOR_DELEGATED = (RETRY, REREVIEW, 'recheck', 'fix', 'hold', 'release', GRANT_ROUND,
                         GRANT_BUDGET, CAPACITY, ACCEPT_DEFAULTS, DECIDE, MERGE, FINDING, 'vote',
                         'authorize')
COORDINATOR_RESERVED = ('approve', MERGE_BUDGET, 'reconcile')
# The name of a command of the coordinator's identity outside ``COMMANDS`` (#528), such as
# ``reconcile`` or ``veto``: refused on an escalation, with its name in the record of the
# refusal, so it is kept to a plain word that fits a code span.
COMMAND_NAME = re.compile(r'[a-z][a-z0-9-]{0,31}')
# What a supervision record says happened to a command.
# ``closed``: the coordinator's closure of findings (#606); it does not move the escalation it
# answers, which stays the anchor of the commands that follow while findings stay open.
OUTCOMES = ('applied', 'accepted', 'decided', 'escalated', 'refused', 'limit', 'granted',
            'merged', 'closed')


def coordinator_refusal(command, language=ENGLISH):
    """``(log, text)`` of why ``command`` of the coordinator's identity does not count (#528),
    or None for a delegated command (``COORDINATOR_DELEGATED``). ``text``, the answer, in
    ``language`` (#633); ``log`` in English."""
    if command in COORDINATOR_DELEGATED:
        return None
    italian = language_code(language) == ITALIAN
    if command in COORDINATOR_RESERVED:
        return ('reserved to the operator, never the coordinator\'s',
                f'`/devloop {command}` è riservato all\'operatore di persona, mai al coordinatore'
                if italian else
                f'`/devloop {command}` is reserved to the operator in person, never to the '
                'coordinator')
    return ('not delegated to the coordinator',
            f'`/devloop {command}` non è tra i comandi delegati al coordinatore' if italian
            else f'`/devloop {command}` is not among the commands delegated to the coordinator')


def recorded_command(command, outcome):
    """Whether a ``supervision`` record may carry ``command`` with ``outcome``: a command of
    ``COMMANDS``, the coordinator's ``finding`` (#606), or, refused, any other of the
    coordinator's identity (``coordinator_command``, #528), so that its refusal spends it."""
    return command in (*COMMANDS, FINDING) or (outcome == 'refused' and isinstance(command, str)
                                               and COMMAND_NAME.fullmatch(command) is not None)


def critical(cls):
    """Whether an escalation of class ``cls`` goes to the operator whatever the merge mode: every
    class but the non-critical ones, an unknown or missing one included."""
    return cls not in NON_CRITICAL


def retry_input(key):
    """The input a retry of ``key`` is bound to: the key without the fields its step does not
    depend on (``UNBOUND``, #475)."""
    unbound = UNBOUND.get(key.get('step'), ())
    return {k: v for k, v in key.items() if k not in unbound}


def changed_input(current, key):
    """The fields of the retry key ``key`` that its input read now, ``current``, no longer has,
    sorted: a retry is superseded when there is any. The fields ``retry_input`` leaves out do
    not count."""
    now, then = retry_input(current), retry_input(key)
    return sorted(k for k in set(now) | set(then) if now.get(k) != then.get(k))


def _reviewers(key):
    """``(input, reviewers)`` of a retry key: its input (``retry_input``) without its reviewer,
    and the set of the reviewers it names (``{''}`` when it names none)."""
    rest = {k: v for k, v in retry_input(key).items() if k != 'reviewer'}
    names = {name for name in str(key.get('reviewer') or '').split(',') if name}
    return rest, names or {''}


def retries_done(key, keys):
    """How many retries of the input of ``key`` the keys of the applied retries ``keys`` already
    count: the most among the reviewers ``key`` names. A review whose reviewers failed together
    and then one by one keeps the count of each reviewer, so that none of them runs more than
    ``MAX_RETRIES`` more times on the same input."""
    rest, names = _reviewers(key)
    spent = [_reviewers(k) for k in keys if isinstance(k, dict)]
    return max(sum(1 for other, who in spent if other == rest and name in who) for name in names)


# The descriptions of the classes in English (#632), for the texts that follow the project's
# ``language``: the escalations (#633) and the answer to a recheck that runs no check.
_ENGLISH = {
    TOOL_FAILURE: 'the agent or the model produced no result',
    PREFLIGHT_DEFAULT: 'gaps of the readiness check with a proposed default',
    SYNC_FAILED: 'sync with the base failed',
    MERGED_ISSUE: 'issue still open, but resolved by a devloop PR already merged',
    ROUND_GRANTABLE: 'limit of the review rounds, with one more round the supervisor may grant',
    BUDGET_GRANTABLE: ('budget envelope of the story used up, with the one extension the '
                       'supervisor may grant'),
    'round_limit': 'limit of the review rounds',
    'operator_decision': 'decision of the operator asked by the readiness check',
    'untrusted': 'untrusted author or text changed after `agent:ready`',
    'reviewer_decision': 'a reviewer asks for a decision of the operator',
    'open_findings': 'open findings that no agent can close any more',
    'review_level': 'not enough independent reviewers for the review level',
    'level_zero': 'promotion at level 0 not possible',
    'protected_paths': 'change to `protected_paths`',
    'sync_sensitive': 'sync conflicts on sensitive paths',
    'tests_failed': 'failing tests, or no change',
    'blocked': 'the agent declared itself blocked',
    'interrupted': 'implementation interrupted',
    'unattributable': 'finding without an owner that can be rebuilt',
    'lost_meta': 'marker of the PR lost',
    'inconsistent': 'inconsistent state of the PR',
    'receipt_conflict': 'receipt of the PR in contradiction with the markers',
    'configuration': 'configuration to decide',
    'failure': 'error of `gh`, of git or of the dispatcher',
    'retry_limit': 'attempts of the supervisor used up',
    'supervisor_escalated': 'passed to the operator by the supervisor',
    'supervisor_refused': 'command of the supervisor not admitted for this escalation',
    'conclave_veto': 'decision of the conclave voided by a trusted user with `/devloop veto`',
    'uncertain_turn': 'turns without an outcome to reconcile with `/devloop reconcile`',
    'attempt': 'attempt of the PR not confirmed by the journal, or already concluded',
    'budget': 'turn not covered by the budget envelope of the story',
    'host_tests': 'tests red locally on the base too, green in its CI',
}
_ENGLISH_UNKNOWN = 'class not recognised, hence critical'


def describe(cls, language=ENGLISH):
    """A short description of ``cls``, for comments, in ``language`` (#632, #633): Italian, or
    English for any other language (``i18n.language_code``)."""
    if language_code(language) != ITALIAN:
        return _ENGLISH.get(cls, _ENGLISH_UNKNOWN) if isinstance(cls, str) else _ENGLISH_UNKNOWN
    if cls == TOOL_FAILURE:
        return 'l\'agent o il modello non ha prodotto un risultato'
    if cls == PREFLIGHT_DEFAULT:
        return 'lacune del controllo di prontezza con un default proposto'
    if cls == SYNC_FAILED:
        return 'sincronizzazione con la base non riuscita'
    if cls == MERGED_ISSUE:
        return 'issue ancora aperta, ma risolta da una PR devloop già mergiata'
    if cls == ROUND_GRANTABLE:
        return 'limite dei round di review, con un round in più che il supervisore può concedere'
    if cls == BUDGET_GRANTABLE:
        return ('envelope di budget della storia esaurito, con l\'unica estensione che il '
                'supervisore può concedere')
    return CRITICAL.get(cls, 'classe non riconosciuta, quindi critica')


def preflight_class(outcome):
    """The class of a failed readiness check: ``preflight_default`` when every gap is
    ``default_available`` and no split is needed, otherwise ``operator_decision``."""
    gaps = outcome.get('gaps') or []
    if gaps and all(g.get('kind') == 'default_available' for g in gaps) \
            and not (outcome.get('split') or '').strip():
        return PREFLIGHT_DEFAULT
    return 'operator_decision'


def command_line(body):
    """The first line of a comment after the attribution header, stripped, or '' (#528): where
    an agent, such as the coordinator, writes a command."""
    lines = (body or '').strip().splitlines()
    while lines and (not lines[0].strip() or lines[0].strip().startswith('>')):
        lines.pop(0)
    return lines[0].strip() if lines else ''


def supervisor_command(body):
    """The supervisor command of a comment: ``(command, text)`` when the first line after the
    attribution header is ``/devloop retry``, ``/devloop accept-defaults``, ``/devloop decide
    <text>``, ``/devloop escalate <reason>``, ``/devloop grant-round <reason>``, ``/devloop
    grant-budget <reason>``, ``/devloop capacity <arguments>`` or ``/devloop rereview
    <reason>``, or ``/devloop merge <arguments>`` and ``/devloop merge-budget <arguments>``
    (#486, whose arguments ``merge_arguments`` and ``merge_budget_arguments`` read); None when
    the comment is no such command. The reason of an escalate is the rest of its line; the text
    of a decide, the reason of a grant-round, a grant-budget or a rereview and the arguments of a
    capacity are the rest of the comment, from the rest of its first line on (#258; #405, #411,
    #412 and #491, whose analysis of the causes may take several lines; ``capacity_arguments``
    reads those of a capacity). A decide, an escalate, a grant, a capacity or a rereview without
    a text has None as text, and is malformed."""
    lines = (body or '').strip().splitlines()
    while lines and (not lines[0].strip() or lines[0].strip().startswith('>')):
        lines.pop(0)
    tokens = lines[0].strip().split(None, 2) if lines else []
    if len(tokens) < 2 or tokens[0] != '/devloop' or tokens[1] not in COMMANDS:
        return None
    reason = tokens[2].strip() if len(tokens) > 2 else ''
    if tokens[1] in LONG_TEXT:
        reason = '\n'.join([reason, *lines[1:]]).strip()
    if tokens[1] in NEEDS_TEXT:
        return tokens[1], reason or None
    return tokens[1], reason


def coordinator_command(body):
    """``(command, text)`` of a ``/devloop <command>`` on the first line after the attribution
    header (``command_line``), whatever ``command`` is, when its name is a plain word
    (``COMMAND_NAME``); None otherwise. What the escalation reader reads of the coordinator's
    identity (#528) when ``supervisor_command`` reads nothing: the reserved commands, such as
    ``reconcile`` or ``approve``, and those not listed, such as ``veto``, refused there. The
    text is the rest of the first line."""
    tokens = command_line(body).split(None, 2)
    if len(tokens) < 2 or tokens[0] != '/devloop' or not COMMAND_NAME.fullmatch(tokens[1]):
        return None
    return tokens[1], tokens[2].strip() if len(tokens) > 2 else ''


# Far more than any cap or duration of a capacity command needs (#412).
_MAX_DIGITS = 9


def _whole(text):
    """``text`` as a whole number when it is up to ``_MAX_DIGITS`` ASCII digits; None for
    anything else. ``str.isdigit`` accepts characters such as ``²`` that ``int`` refuses, and
    ``int`` refuses a string of thousands of digits: both would stop the tick (#412)."""
    if not text or len(text) > _MAX_DIGITS or not all('0' <= c <= '9' for c in text):
        return None
    return int(text)


def capacity_minutes(text):
    """The minutes of the duration ``text`` of a capacity command (#412): ``<N>h`` or ``<N>m``,
    ``N`` a whole number above 0 in ASCII digits; None for anything else."""
    text = (text or '').strip().lower()
    if len(text) < 2 or text[-1] not in 'hm':
        return None
    amount = _whole(text[:-1])
    if amount is None or amount <= 0:
        return None
    return amount * 60 if text[-1] == 'h' else amount


def capacity_arguments(text):
    """``(key, value, minutes, reason)`` of the text of ``/devloop capacity <key> <value>
    <duration> <reason>`` (#412), as ``supervisor_command`` returns it; the reason, the analysis
    of the causes, is the rest of the text and may take several lines. None when a part is
    missing or malformed: a value that is not a whole number, a duration other than ``<N>h`` or
    ``<N>m``, no reason. Whether the key, the value and the duration are within the caps is the
    dispatcher's check."""
    parts = (text or '').split(None, 3)
    if len(parts) < 4:
        return None
    value = _whole(parts[1])
    minutes = capacity_minutes(parts[2])
    reason = parts[3].strip()
    if value is None or minutes is None or not reason:
        return None
    return parts[0], value, minutes, reason


# A full commit id, as ``/devloop merge`` names the head it merges (#486): an abbreviated one
# could name another commit, so it is refused.
_SHA = re.compile(r'[0-9a-f]{40}')


def merge_arguments(text):
    """``(sha, reason)`` of the text of ``/devloop merge <sha> <reason>`` (#486), as
    ``supervisor_command`` returns it: ``sha`` the full id of the head, in lower case; the
    reason, which may take several lines, is the rest of the text. None when either is missing
    or the id is not a full one."""
    parts = (text or '').split(None, 1)
    if len(parts) < 2 or not _SHA.fullmatch(parts[0]) or not parts[1].strip():
        return None
    return parts[0], parts[1].strip()


def merge_budget_arguments(text):
    """``(n, reason)`` of the text of ``/devloop merge-budget <N> <reason>`` (#486): ``n`` a
    whole number in ASCII digits from 1 to ``MAX_MERGE_BUDGET``, the reason the rest of the
    text. None when a part is missing or out of bounds."""
    parts = (text or '').split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        return None
    value = _whole(parts[0])
    if value is None or not 1 <= value <= MAX_MERGE_BUDGET:
        return None
    return value, parts[1].strip()


def _under(path, entry):
    """Whether ``path`` is, or is under, ``entry`` (a file, or a directory with or without its
    final ``/``), as ``operator_only_paths`` matches."""
    path, entry = path.strip('/'), entry.strip().strip('/')
    return bool(entry) and (path == entry or path.startswith(entry + '/'))


def rule_paths(paths, extra=()):
    """The paths of ``paths`` a delegated merge never touches (#486): the trust rule
    (``TRUST_RULE_PATHS``, with ``extra``, the configuration's additions), and where the
    delegations, their caps, ``operator_only_paths`` and the merge conditions are set and
    applied (``DELEGATION_PATHS``)."""
    entries = (*TRUST_RULE_PATHS, *DELEGATION_PATHS, *extra)
    return sorted(p for p in paths if any(_under(p, entry) for entry in entries))


def adr_paths(paths):
    """The paths of ``paths`` under ``ADR_DIR``."""
    return sorted(p for p in paths if _under(p, ADR_DIR))


def comment_links(text, repo, aliases=()):
    """The comments of the repository ``repo`` that ``text`` links to (#486), as ``(number,
    comment id)``, in order: ``https://github.com/<repo>/issues/<N>#issuecomment-<id>`` or
    ``.../pull/<N>#issuecomment-<id>``. A link to another repository does not count.

    ``aliases``: the names ``repo`` had before a rename (``repo_aliases``, #540): a link under
    one of them counts as one under ``repo``. Whether the comment is in ``repo`` is the
    caller's check, with the API, as for any link: a comment of another repository that took
    the old name is not found there, and the link is refused."""
    names = '|'.join(re.escape(name) for name in (repo, *aliases))
    pattern = (r'https://github\.com/(?:' + names
               + r')/(?:issues|pull)/([1-9][0-9]{0,9})#issuecomment-([1-9][0-9]{0,19})\b')
    return [(int(n), int(c)) for n, c in re.findall(pattern, text or '')]


# The problems of the changes of ``docs/adr/`` (``adr_problems``), in the project's
# ``language`` (#634); English for a language without translation.
ADR_TEXTS = Catalog(
    en={
        'not_added': ('`{path}` does not have only added lines (lines removed or changed, a '
                      'file removed or binary, or a change of mode)'),
        'no_link': ('a block added in `{path}` does not have the link to a comment of this '
                    'repository («{first}»)'),
    },
    it={
        'not_added': ('`{path}` non ha solo righe aggiunte (righe tolte o modificate, file '
                      'rimosso o binario, o cambio di modo)'),
        'no_link': ('un blocco aggiunto in `{path}` non ha il link a un commento di questo '
                    'repository («{first}»)'),
    })


def adr_problems(files, repo, aliases=(), language=ENGLISH):
    """Why the changes of ``docs/adr/`` in ``files`` (``Workspace.diff_blocks``) do not only
    record decisions of the operator already written on GitHub (#486), as phrases in
    ``language`` (``ADR_TEXTS``, #634), and the links each block needs verified: ``(problems,
    blocks)``, ``blocks`` a list of ``(path, links)``. A file that is not plain text, or that
    loses or changes a line (a deleted line, a removed file, a new mode) has a problem; so has
    each block of added lines without a link to a comment of ``repo`` (or of one of its
    ``aliases``, ``comment_links``). Whether each comment linked is in ``repo`` and by a
    trusted user is the dispatcher's check, with the API."""
    t = ADR_TEXTS.texts(language)
    problems, blocks = [], []
    for entry in files:
        path = entry['path']
        if entry.get('binary') or entry.get('removed') or entry.get('other'):
            problems.append(t('not_added', path=path))
            continue
        for block in entry.get('blocks') or []:
            links = comment_links(block, repo, aliases)
            if not links:
                first = next((ln.strip() for ln in block.splitlines() if ln.strip()), '')
                problems.append(t('no_link', path=path, first=first[:80]))
            else:
                blocks.append((path, links))
    return problems, blocks
