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
- ``/devloop escalate <reason>``: hand the escalation to the operator.
"""

# Non-critical classes: the supervisor decides them in ``auto``.
TOOL_FAILURE = 'tool_failure'            # the agent or its model produced no result
PREFLIGHT_DEFAULT = 'preflight_default'  # readiness gaps all ``default_available``
SYNC_FAILED = 'sync_failed'              # a sync failed, no conflict on a sensitive path
MERGED_ISSUE = 'merged_issue'            # a ready issue that a merged devloop PR resolved (#228)
NON_CRITICAL = (TOOL_FAILURE, PREFLIGHT_DEFAULT, SYNC_FAILED, MERGED_ISSUE)

# Critical classes: always to the operator. Any other value is critical too.
CRITICAL = {
    'round_limit': 'limite dei round di review',
    'operator_decision': 'decisione dell\'operatore chiesta dal controllo di prontezza',
    'untrusted': 'autore non fidato o testo modificato dopo `agent:ready`',
    'reviewer_decision': 'un reviewer chiede una decisione dell\'operatore',
    'open_findings': 'finding aperti che solo l\'operatore può chiudere',
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
}

# The steps a ``retry`` can run again, and the PR label each one resumes from.
RETRY_STEPS = ('preflight', 'implement', 'review', 'fix', 'sync')
ISSUE_STEPS = ('preflight', 'implement')
MAX_RETRIES = 2
# An issue with this label is in a design phase: its escalations stay critical.
DESIGN_LABEL = 'phase:design'

RETRY, ACCEPT_DEFAULTS, DECIDE, ESCALATE = 'retry', 'accept-defaults', 'decide', 'escalate'
COMMANDS = (RETRY, ACCEPT_DEFAULTS, DECIDE, ESCALATE)
# The commands that need a text: without one they are malformed.
NEEDS_TEXT = (DECIDE, ESCALATE)
# What a supervision record says happened to a command.
OUTCOMES = ('applied', 'accepted', 'decided', 'escalated', 'refused', 'limit')


def critical(cls):
    """Whether an escalation of class ``cls`` goes to the operator whatever the merge mode: every
    class but the non-critical ones, an unknown or missing one included."""
    return cls not in NON_CRITICAL


def _reviewers(key):
    """``(input, reviewers)`` of a retry key: the key without its reviewer, and the set of the
    reviewers it names (``{''}`` when it names none)."""
    rest = {k: v for k, v in key.items() if k != 'reviewer'}
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


def describe(cls):
    """A short Italian description of ``cls``, for comments."""
    if cls == TOOL_FAILURE:
        return 'l\'agent o il modello non ha prodotto un risultato'
    if cls == PREFLIGHT_DEFAULT:
        return 'lacune del controllo di prontezza con un default proposto'
    if cls == SYNC_FAILED:
        return 'sincronizzazione con la base non riuscita'
    if cls == MERGED_ISSUE:
        return 'issue ancora aperta, ma risolta da una PR devloop già mergiata'
    return CRITICAL.get(cls, 'classe non riconosciuta, quindi critica')


def preflight_class(outcome):
    """The class of a failed readiness check: ``preflight_default`` when every gap is
    ``default_available`` and no split is needed, otherwise ``operator_decision``."""
    gaps = outcome.get('gaps') or []
    if gaps and all(g.get('kind') == 'default_available' for g in gaps) \
            and not (outcome.get('split') or '').strip():
        return PREFLIGHT_DEFAULT
    return 'operator_decision'


def supervisor_command(body):
    """The supervisor command of a comment: ``(command, text)`` when the first line after the
    attribution header is ``/devloop retry``, ``/devloop accept-defaults``, ``/devloop decide
    <text>`` or ``/devloop escalate <reason>``; None when the comment is no such command. The
    reason of an escalate is the rest of its line; the text of a decide is the rest of the
    comment, from the rest of its first line on (#258). A decide or an escalate without a text
    has None as text, and is malformed."""
    lines = (body or '').strip().splitlines()
    while lines and (not lines[0].strip() or lines[0].strip().startswith('>')):
        lines.pop(0)
    tokens = lines[0].strip().split(None, 2) if lines else []
    if len(tokens) < 2 or tokens[0] != '/devloop' or tokens[1] not in COMMANDS:
        return None
    reason = tokens[2].strip() if len(tokens) > 2 else ''
    if tokens[1] == DECIDE:
        reason = '\n'.join([reason, *lines[1:]]).strip()
    if tokens[1] in NEEDS_TEXT:
        return tokens[1], reason or None
    return tokens[1], reason
