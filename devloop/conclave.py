"""The conclave of three agents on a block (#141, part 2 of #114): pure logic.

When the implementer or a fixer stops as ``blocked``, or a reviewer asks for the operator
(``needs_operator``), and ``conclave`` is configured, the dispatcher convenes three distinct
agents, the one that stopped among them. The protocol is finite:

1. the agent that stopped gets its reason and the context, and writes **one** resolution
   (``conclave_proposal``), or declares that the gap falls in the perimeter reserved to the
   operator;
2. the other two get the context, the reason and exactly that text, bound by its hash, and vote
   ``APPROVA`` or ``BLOCCA`` with a reason (``conclave_vote``). No negotiated revision.

The outcome is ``UNANIMOUS`` (the proposal and two ``APPROVA``), ``BLOCKED`` (one ``BLOCCA``, a
member that failed or timed out, a context that changed) or ``RESERVED`` (a member flagged the
reserved perimeter), which prevails over everything. This module builds, checks and renders the
``conclave`` record; ``dispatch_conclave`` runs it.

Since #142 (part 3 of #114) a ``UNANIMOUS`` decision unblocks the work once (``resumed``): the
step that stopped runs again from the published commit, with the resolution in its prompt
(``resolution_section``), and the changes the agent left uncommitted are kept as evidence. A
trusted user annuls the latest decision with ``/devloop veto`` (``veto_command``).

Since #605 (ADR 0002, second precision of 2026-11-23, point 3), with ``[roles.coordinator]`` a
voter that fails (``await_substitute``) does not close the conclave: its vote waits
(``WAITING``) for the coordinator's substitute vote until a deadline recorded in the record
(``substitute``). The coordinator votes with ``/devloop vote <conclave> <sha256>
APPROVE|BLOCK|RESERVED <reason>`` (``vote_command``), bound to the conclave's comment and to the
hash of its proposal (``apply_substitute``). One substitute vote per conclave: a second member
that fails leaves it to the operator. Past the deadline the vote is missing
(``expire_substitute``). The proposer is never substituted.
"""

import hashlib
import re

from .markdown import dispatcher_header, publishable
from .policy import independent, valid_profile

UNANIMOUS, BLOCKED, RESERVED = 'UNANIMOUS', 'BLOCKED', 'RESERVED'
OUTCOMES = (UNANIMOUS, BLOCKED, RESERVED)
APPROVE, BLOCK = 'APPROVA', 'BLOCCA'
# The state of each contribution: not started, started (the record written before the call),
# done, or missing (with the reason).
PENDING, STARTED, DONE, MISSING = 'pending', 'started', 'done', 'missing'
# A vote whose member is unavailable, waiting for the coordinator's substitute vote (#605).
WAITING = 'waiting'
STATES = (PENDING, STARTED, DONE, MISSING, WAITING)
# The steps whose block convenes a conclave: the implementer and a fixer that stop as
# ``blocked``, a reviewer whose verdict is ``needs_operator``.
STEPS = ('implement', 'fix', 'review')
BLOCK_KINDS = ('blocked', 'review')
# Limits of what a member may write, so that the record (text and marker) stays well within a
# GitHub comment. A contribution beyond them is a failed one.
MAX_RESOLUTION = 4000
MAX_TEXT = 1000
MAX_OPTIONS = 5
MAX_OPTION = 200
# The diff of the agent that stopped, as the prompts show it at most.
MAX_DIFF = 20000

# The command with which a trusted user annuls the latest decision of a conclave (#142): the
# first line of a comment, after the attribution header.
VETO_COMMAND = '/devloop veto'

# The substitute vote of the coordinator (#605): the command, its verdicts (``APPROVE`` and
# ``BLOCK`` are the ``APPROVA`` and ``BLOCCA`` of ``conclave_vote``; ``RESERVED`` flags the
# reserved perimeter) and the states of the wait: waiting, voted, expired without a vote, void
# (a second member failed, or the context changed).
VOTE_COMMAND = '/devloop vote'
SUBSTITUTE_VERDICTS = ('APPROVE', 'BLOCK', 'RESERVED')
SUB_WAITING, SUB_VOTED, SUB_EXPIRED, SUB_VOID = 'waiting', 'voted', 'expired', 'void'
SUB_STATES = (SUB_WAITING, SUB_VOTED, SUB_EXPIRED, SUB_VOID)
# The refused commands a record keeps: the latest ones.
MAX_REFUSED = 5
_PROPOSAL_HASH = re.compile(r'[0-9a-f]{64}')
_COMMENT_ID = re.compile(r'[1-9][0-9]{0,19}')

STARTED_WITHOUT_RESULT = ('chiamata avviata senza un risultato durevole (riavvio o errore del '
                          'dispatcher): conta come `BLOCCA` e non si ripete')


def text_hash(text):
    """The hash that binds a text: sha256 of its UTF-8 bytes."""
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def _contribution(agent):
    # ``profile``: the profile the contribution was actually given with, a fallback model
    # included, recorded with it; None until then.
    return {'agent': agent, 'state': PENDING, 'missing': '', 'reserved': False,
            'reserved_reason': '', 'operator_question': '', 'options': [], 'profile': None}


def new_record(block, step, members, issue, issue_hash, commit, head, diff_hash):
    """The record of a conclave about to start: ``members`` is the proposer, then the two
    voters. ``head``: the PR's head when it opens, None on an issue."""
    proposal = dict(_contribution(members[0]), resolution='', hash='')
    votes = [dict(_contribution(key), vote='', reason='', proposal_hash='')
             for key in members[1:]]
    return {'block': block, 'step': step, 'issue': issue, 'issue_hash': issue_hash,
            'commit': commit, 'head': head, 'diff_hash': diff_hash, 'proposal': proposal,
            'votes': votes, 'stale': '', 'hash': '', 'outcome': ''}


def too_long(raw, main_key, main_limit):
    """Why ``raw`` exceeds the limits, or ''."""
    if len(raw.get(main_key) or '') > main_limit:
        return f'`{main_key}` supera {main_limit} caratteri'
    for key in ('reserved_reason', 'operator_question', 'reason'):
        if len(raw.get(key) or '') > MAX_TEXT:
            return f'`{key}` supera {MAX_TEXT} caratteri'
    options = raw.get('options') or []
    if len(options) > MAX_OPTIONS or any(len(o) > MAX_OPTION for o in options):
        return f'più di {MAX_OPTIONS} opzioni, o un\'opzione oltre {MAX_OPTION} caratteri'
    return ''


def _reserved_fields(raw):
    return {'reserved': bool(raw['reserved']),
            'reserved_reason': raw['reserved_reason'].strip() if raw['reserved'] else '',
            'operator_question': raw['operator_question'].strip() if raw['reserved'] else '',
            'options': [o.strip() for o in raw['options'] if o.strip()] if raw['reserved']
            else []}


def apply_proposal(record, raw, profile=None):
    """``record`` with the proposer's schema-valid output ``raw``, given with ``profile``. A
    reserved perimeter ends the protocol: no proposal, and the votes are not asked. A proposal
    without text, or beyond the limits, is a failed contribution."""
    proposal = record['proposal']
    why = too_long(raw, 'resolution', MAX_RESOLUTION)
    if why:
        return missing(record, 'proposal', f'output oltre i limiti: {why}')
    if raw['reserved']:
        proposal.update(state=DONE, profile=profile, **_reserved_fields(raw))
        for vote in record['votes']:
            vote.update(state=MISSING, missing='non richiesto: il proponente ha segnalato il '
                                               'perimetro riservato all\'operatore')
        return record
    text = raw['resolution']
    if not text.strip():
        return missing(record, 'proposal', 'risoluzione vuota')
    proposal.update(state=DONE, resolution=text, hash=text_hash(text), profile=profile,
                    **_reserved_fields(raw))
    record['hash'] = proposal['hash']
    return record


def apply_vote(record, index, raw, proposal_hash, profile=None):
    """``record`` with the schema-valid vote ``raw`` of voter ``index``, given with ``profile``
    on the proposal whose hash is ``proposal_hash``."""
    vote = record['votes'][index]
    why = too_long(raw, 'reason', MAX_TEXT)
    if why:
        return missing(record, index, f'output oltre i limiti: {why}')
    vote.update(state=DONE, vote=raw['vote'], reason=raw['reason'].strip(),
                proposal_hash=proposal_hash, profile=profile, **_reserved_fields(raw))
    return record


def missing(record, which, reason):
    """``record`` with contribution ``which`` (``'proposal'`` or a voter's index) missing for
    ``reason``. A missing proposal leaves nothing to vote on: the votes are missing too. A
    contribution missing while a vote waits for the coordinator's substitute vote ends the wait
    (#605): one substitute vote per conclave, so two members missing leave it to the operator,
    however the second one went missing (a failed call, an output over the limits, a call
    started without a result)."""
    item = record['proposal'] if which == 'proposal' else record['votes'][which]
    item.update(state=MISSING, missing=reason)
    if which == 'proposal':
        for vote in record['votes']:
            if vote['state'] in (PENDING, STARTED):
                vote.update(state=MISSING, missing='nessuna proposta da votare')
    if waiting_substitute(record) is not None:
        _void(record, 'un secondo membro non ha votato: un solo voto sostitutivo per conclave')
    return record


# The substitute vote (#605) -----------------------------------------------------------------
def waiting_substitute(record):
    """The ``substitute`` of ``record`` while it waits for the coordinator's vote, or None."""
    substitute = record.get('substitute')
    return substitute if substitute and substitute['state'] == SUB_WAITING else None


def substitute_vote(record):
    """The vote of ``record`` the coordinator gave in place of a member (#605), or None."""
    substitute = record.get('substitute')
    if substitute and substitute['state'] == SUB_VOTED:
        return record['votes'][substitute['index']]
    return None


def await_substitute(record, index, reason, deadline, conclave, after):
    """``record`` whose voter ``index`` failed for ``reason``, with a coordinator: the vote
    waits for the coordinator's substitute vote until ``deadline`` (ISO 8601, UTC), on the
    conclave of comment ``conclave``; ``after`` is the latest comment id read before the wait,
    so that only a command written later counts. One substitute vote per conclave: when a
    member is already substituted, or waits, this vote is missing, and so is the waiting one;
    when another vote is already missing (an output over the limits, a call started without a
    result), this one is missing too, and no wait opens: two members missing leave the
    conclave to the operator at once."""
    if record.get('substitute'):
        return missing(record, index, reason)
    if any(vote['state'] == MISSING for i, vote in enumerate(record['votes']) if i != index):
        return missing(record, index, f'{reason}; un altro membro non ha votato: un solo voto '
                                      'sostitutivo per conclave')
    record['votes'][index].update(state=WAITING, missing=reason)
    record['substitute'] = {'index': index, 'member': record['votes'][index]['agent'],
                            'why': reason, 'deadline': deadline, 'conclave': conclave,
                            'state': SUB_WAITING, 'last': after, 'comment': None, 'by': '',
                            'verdict': '', 'at': '', 'refused': []}
    return record


def _void(record, reason, state=SUB_VOID):
    substitute = record['substitute']
    record['votes'][substitute['index']].update(state=MISSING,
                                                missing=f'{substitute["why"]}; {reason}')
    substitute['state'] = state


def expire_substitute(record):
    """``record`` whose substitute vote did not come before its deadline: the vote is missing,
    and the conclave goes to the operator as before (#605)."""
    if waiting_substitute(record) is not None:
        _void(record, 'nessun voto sostitutivo del coordinatore entro la scadenza', SUB_EXPIRED)
    return record


def refuse_substitute(record, comment, why):
    """``record`` with the command of comment ``comment`` refused for ``why``: spent, and kept
    among the latest ``MAX_REFUSED``."""
    substitute = record['substitute']
    substitute['last'] = max(substitute['last'], comment)
    substitute['refused'] = (substitute['refused']
                             + [{'comment': comment, 'why': why}])[-MAX_REFUSED:]
    return record


def substitute_problem(record, command):
    """Why the coordinator's ``command`` (``vote_command``) does not apply to the substitute
    vote ``record`` waits for, in Italian, or ''. Who wrote it and when are the dispatcher's
    checks."""
    substitute = record['substitute']
    if command['error']:
        return command['error']
    if command['conclave'] != substitute['conclave']:
        return (f'il comando nomina il conclave {command["conclave"]}, non questo '
                f'({substitute["conclave"]})')
    if command['hash'] != record['hash']:
        return 'lo SHA-256 non è quello della proposta corrente del conclave'
    return ''


def apply_substitute(record, command, comment, by, profile, at):
    """``record`` with the coordinator's substitute vote ``command`` (``vote_command``, already
    checked), written in comment ``comment`` by ``by`` with ``profile`` and recorded at ``at``:
    ``APPROVE`` and ``BLOCK`` are ``APPROVA`` and ``BLOCCA``, ``RESERVED`` flags the reserved
    perimeter, which ends the conclave ``RESERVED``."""
    substitute = record['substitute']
    reserved = command['verdict'] == 'RESERVED'
    record['votes'][substitute['index']].update(
        state=DONE, missing='', vote=APPROVE if command['verdict'] == 'APPROVE' else BLOCK,
        reason=command['reason'], proposal_hash=record['hash'], profile=profile,
        reserved=reserved, reserved_reason=command['reason'] if reserved else '',
        operator_question='', options=[])
    substitute.update(state=SUB_VOTED, comment=comment, by=by, verdict=command['verdict'],
                      at=at, last=max(substitute['last'], comment))
    return record


def make_stale(record, reason):
    """``record`` whose frozen context no longer holds: every contribution not done is missing,
    and the outcome is ``BLOCKED`` (unless a member already flagged the reserved perimeter)."""
    record['stale'] = reason
    if waiting_substitute(record) is not None:
        _void(record, f'contesto cambiato: {reason}')
    for item in [record['proposal'], *record['votes']]:
        if item['state'] in (PENDING, STARTED):
            item.update(state=MISSING, missing=f'contesto cambiato: {reason}')
    return record


def settle_started(record):
    """``record`` with every call started without a durable result marked missing: it counts as
    a failure, ``BLOCCA``, and is not repeated."""
    if record['proposal']['state'] == STARTED:
        missing(record, 'proposal', STARTED_WITHOUT_RESULT)
    for index, vote in enumerate(record['votes']):
        if vote['state'] == STARTED:
            missing(record, index, STARTED_WITHOUT_RESULT)
    return record


def outcome(record):
    """The outcome of ``record``, or None while a contribution is still due: after a voter
    flags the reserved perimeter the other vote is still collected, as after a ``BLOCCA``,
    since it may write its own question for the operator. Only a proposer that flags it ends
    the protocol at once (``apply_proposal`` marks the votes missing). Once every contribution
    is in, ``RESERVED`` prevails; then a stale context, a missing proposal, a missing vote or a
    ``BLOCCA`` give ``BLOCKED``; two ``APPROVA`` on the proposal give ``UNANIMOUS``. A vote
    waiting for the coordinator's substitute vote (#605) is still due."""
    items = [record['proposal'], *record['votes']]
    if any(item['state'] in (PENDING, STARTED, WAITING) for item in items):
        return None
    if any(item['state'] == DONE and item['reserved'] for item in items):
        return RESERVED
    if record['stale'] or record['proposal']['state'] != DONE:
        return BLOCKED
    if all(v['state'] == DONE and v['vote'] == APPROVE and v['proposal_hash'] == record['hash']
           for v in record['votes']):
        return UNANIMOUS
    return BLOCKED


def next_call(record):
    """``'proposal'``, a voter's index, or None: the next contribution still to ask for."""
    if record['proposal']['state'] == PENDING:
        return 'proposal'
    if record['proposal']['state'] != DONE:
        return None
    return next((i for i, v in enumerate(record['votes']) if v['state'] == PENDING), None)


def calls_left(record):
    """How many calls the conclave still needs (at least one action while it is open, to settle
    the calls started without a result and record the outcome)."""
    if record is None:
        return 3
    if record['outcome']:
        return 0
    due = sum(1 for item in [record['proposal'], *record['votes']]
              if item['state'] == PENDING)
    return max(due, 1)


# Reading ------------------------------------------------------------------------------------
def _is_str(value):
    return isinstance(value, str)


def _valid_item(item, vote):
    keys = ('agent', 'missing', 'reserved_reason', 'operator_question')
    keys += ('vote', 'reason', 'proposal_hash') if vote else ('resolution', 'hash')
    return (isinstance(item, dict) and item.get('state') in STATES
            and all(_is_str(item.get(k)) for k in keys)
            and isinstance(item.get('reserved'), bool)
            and isinstance(item.get('options'), list) and all(map(_is_str, item['options']))
            and (not vote or item['vote'] in ('', APPROVE, BLOCK))
            and (item.get('profile') is None or valid_profile(item['profile']))
            and (item['state'] not in (MISSING, WAITING) or bool(item['missing'])))


def _is_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def valid_substitute(record):
    """The ``substitute`` field of a record (#605): absent or None without a substitute vote,
    else the wait and, once voted, the vote; a ``WAITING`` vote only while it waits."""
    s = record.get('substitute')
    waiting = [i for i, v in enumerate(record['votes']) if v['state'] == WAITING]
    if s is None:
        return not waiting
    if not (isinstance(s, dict) and s.get('index') in (0, 1) and s.get('state') in SUB_STATES
            and all(_is_str(s.get(k)) for k in ('member', 'why', 'deadline', 'by', 'verdict',
                                                'at'))
            and _is_id(s.get('conclave')) and _is_id(s.get('last'))
            and (s.get('comment') is None or _is_id(s['comment']))
            and isinstance(s.get('refused'), list)
            and all(isinstance(r, dict) and _is_id(r.get('comment')) and _is_str(r.get('why'))
                    for r in s['refused'])):
        return False
    vote = record['votes'][s['index']]
    if s['member'] != vote['agent']:
        return False
    if s['state'] == SUB_WAITING:
        return waiting == [s['index']]
    if s['state'] == SUB_VOTED:
        return (not waiting and vote['state'] == DONE and s['comment'] is not None
                and s['verdict'] in SUBSTITUTE_VERDICTS)
    return not waiting and vote['state'] == MISSING


def valid_block(block):
    if not isinstance(block, dict) or block.get('kind') not in BLOCK_KINDS:
        return False
    if block['kind'] == 'blocked':
        return (isinstance(block.get('comment'), int) and not isinstance(block['comment'], bool)
                and _is_str(block.get('agent')) and block.get('step') in STEPS)
    return (isinstance(block.get('round'), int) and not isinstance(block['round'], bool)
            and _is_str(block.get('reviewer')) and _is_str(block.get('head')))


def valid_resumed(resumed):
    """The ``resumed`` field of a record (#142): absent or None before the work restarts, then
    ``{evidence}``, the file of the dispatcher's logs that keeps the changes the agent that
    stopped left uncommitted (``{file, sha256, bytes}``), or None when there were none."""
    if resumed is None:
        return True
    if not isinstance(resumed, dict):
        return False
    evidence = resumed.get('evidence')
    return evidence is None or (isinstance(evidence, dict)
                                and _is_str(evidence.get('file')) and bool(evidence['file'])
                                and _is_str(evidence.get('sha256'))
                                and isinstance(evidence.get('bytes'), int)
                                and not isinstance(evidence['bytes'], bool))


def valid_record(r):
    """The structure of a ``conclave`` marker."""
    return (valid_block(r.get('block')) and r.get('step') in STEPS
            and valid_resumed(r.get('resumed'))
            and isinstance(r.get('issue'), int) and not isinstance(r['issue'], bool)
            and all(_is_str(r.get(k)) for k in ('issue_hash', 'commit', 'diff_hash', 'stale',
                                                'hash'))
            and (r.get('head') is None or _is_str(r['head']))
            and _valid_item(r.get('proposal'), vote=False)
            and isinstance(r.get('votes'), list) and len(r['votes']) == 2
            and all(_valid_item(v, vote=True) for v in r['votes'])
            and valid_substitute(r)
            and r.get('outcome') in ('', *OUTCOMES))


def consistent(record):
    """Whether ``record`` holds together, as the dispatcher reads it back: the content hash is
    the hash of the proposal's text, every vote was given on that hash, and a recorded outcome
    is the one its contributions give, and every contribution records the profile it was given
    with, the three distinct by identity and by ``(kind, model, effort)``, fallback models
    included. The substitute vote of the coordinator (#605) is outside the last check: it is
    recorded as such, with the coordinator's model. Returns ``(ok, why)``."""
    proposal = record['proposal']
    done = [item for item in [proposal, *record['votes']] if item['state'] == DONE]
    if any(item['profile'] is None for item in done):
        return False, 'un contributo non registra il profilo con cui è stato dato'
    substituted = substitute_vote(record)
    done = [item for item in done if item is not substituted]
    for position, a in enumerate(done):
        for b in done[position + 1:]:
            if not independent(a['profile'], b['profile']):
                return False, (f'`{a["agent"]}` e `{b["agent"]}` hanno contribuito con la stessa '
                               'identità o la stessa tupla (kind, model, effort)')
    if proposal['state'] == DONE and not proposal['reserved']:
        if record['hash'] != text_hash(proposal['resolution']) or proposal['hash'] != record['hash']:
            return False, 'l\'hash della proposta non corrisponde al suo testo'
    for vote in record['votes']:
        if vote['state'] == DONE and vote['proposal_hash'] != record['hash']:
            return False, f'il voto di `{vote["agent"]}` non è legato alla proposta registrata'
    if record['outcome'] and record['outcome'] != outcome(record):
        return False, 'l\'esito registrato non è quello dei contributi'
    if record.get('resumed') is not None and record['outcome'] != UNANIMOUS:
        return False, 'il lavoro risulta ripreso senza una decisione `UNANIMOUS`'
    return True, ''


# Commands and prompts (#142) ----------------------------------------------------------------
def veto_command(body):
    """The reason of a ``/devloop veto`` command (#142): when the first line of ``body`` after
    the attribution header is ``/devloop veto``, the rest of that line (``''`` without one);
    None when the comment is no such command."""
    lines = [line.strip() for line in (body or '').strip().splitlines()]
    while lines and (not lines[0] or lines[0].startswith('>')):
        lines.pop(0)
    tokens = lines[0].split(None, 2) if lines else []
    if tokens[:2] != VETO_COMMAND.split():
        return None
    return tokens[2].strip() if len(tokens) > 2 else ''


def vote_command(body):
    """The coordinator's substitute vote in ``body`` (#605), or None when the first line after
    the attribution header is no ``/devloop vote``: ``{conclave, hash, verdict, reason,
    error}``, ``conclave`` the comment id of the conclave (None when it is not one), ``reason``
    the rest of the comment from the rest of the first line on, and ``error``, in Italian, why
    the command is malformed ('' when it is not)."""
    lines = (body or '').strip().splitlines()
    while lines and (not lines[0].strip() or lines[0].strip().startswith('>')):
        lines.pop(0)
    tokens = lines[0].strip().split(None, 2) if lines else []
    if tokens[:2] != VOTE_COMMAND.split():
        return None
    parts = (tokens[2] if len(tokens) > 2 else '').split(None, 3)
    parts += [''] * (4 - len(parts))
    conclave, digest, verdict = parts[:3]
    reason = '\n'.join([parts[3], *lines[1:]]).strip()
    command = {'conclave': int(conclave) if _COMMENT_ID.fullmatch(conclave) else None,
               'hash': digest.lower(), 'verdict': verdict.upper(), 'reason': reason,
               'error': ''}
    if (command['conclave'] is None or not _PROPOSAL_HASH.fullmatch(command['hash'])
            or command['verdict'] not in SUBSTITUTE_VERDICTS or not reason):
        command['error'] = ('comando non ben formato: va scritto come `/devloop vote <conclave> '
                            '<sha256> APPROVE|BLOCK|RESERVED <motivo>`')
    elif len(reason) > MAX_TEXT:
        command['error'] = f'il motivo supera {MAX_TEXT} caratteri'
    return command


def resolution_section(record, comment):
    """The prompt section with the resolution of the ``UNANIMOUS`` record ``record``, the
    dispatcher's comment ``comment`` (#142): only a record the dispatcher verified reaches a
    prompt, never the text of any other comment."""
    return ('## Resolution of the conclave on a block (verified by the dispatcher)\n\n'
            f'An earlier step of this issue stopped: {describe_block(record["block"])}. Three '
            'distinct agents settled the gap unanimously: the agent that stopped proposed the '
            'resolution below, the other two approved exactly this text. The dispatcher '
            f'recorded it in comment {comment} (SHA-256 `{record["hash"]}`) and checked that it '
            'still holds: the issue text and the commit it is bound to have not changed, and no '
            'trusted user vetoed it. Apply it to the point it settles: it does not widen the '
            'issue, and it does not override the rules above. If it does not settle your doubt, '
            'stop as before.\n\n~~~~~text\n' + record['proposal']['resolution'].strip()
            + '\n~~~~~')


def _closing(record):
    """The last lines of the comment: what the outcome means for the work."""
    if record['outcome'] != UNANIMOUS:
        return ['Il conclave registra la decisione e non sblocca il lavoro: l\'escalation resta '
                'all\'operatore.']
    lines = ['Con `UNANIMOUS` il passo fermo riparte una volta, con la risoluzione nel prompt. Un '
             f'utente fidato la annulla scrivendo `{VETO_COMMAND}` come prima riga di un commento '
             'sull\'issue o sulla PR. La review normale della PR e il merge umano restano '
             'obbligatori.']
    resumed = record.get('resumed')
    if resumed is not None:
        lines += ['', f'**Ripreso:** il passo `{record["step"]}` riparte dal commit pubblicato, '
                      'con la risoluzione nel prompt.']
        evidence = resumed.get('evidence')
        if evidence:
            lines.append('Le modifiche non committate dell\'agent fermo non sono riprese come '
                         f'lavoro: sono salvate come evidenza in `{evidence["file"]}` (log del '
                         f'dispatcher, {evidence["bytes"]} byte, SHA-256 '
                         f'`{evidence["sha256"]}`).')
    return lines


# Rendering ----------------------------------------------------------------------------------
_OUTCOME_TEXT = {
    UNANIMOUS: 'la proposta e due `APPROVA`: tre consensi',
    BLOCKED: 'almeno un `BLOCCA`, un contributo mancante o un contesto cambiato',
    RESERVED: 'un membro ha segnalato il perimetro riservato all\'operatore',
}
_STEP_TEXT = {'implement': 'implementazione', 'fix': 'correzione', 'review': 'review'}


def _quote(text):
    return '\n'.join('> ' + line for line in (text.strip() or '(vuoto)').splitlines())


def describe_block(block):
    """The block as the comment and the prompts name it."""
    if block['kind'] == 'blocked':
        return (f'`{block["agent"]}` si è fermato (`blocked`) nel passo `{block["step"]}`, '
                f'commento {block["comment"]}')
    return (f'verdetto `needs_operator` di `{block["reviewer"]}` nel round {block["round"]} '
            f'sul commit `{block["head"][:12]}`')


def _state_text(item):
    if item['state'] == WAITING:
        return f'*indisponibile, in attesa del voto sostitutivo:* {item["missing"]}'
    if item['state'] == MISSING:
        return f'*mancante:* {item["missing"]}'
    if item['state'] == STARTED:
        return '*in corso*'
    if item['state'] == PENDING:
        return '*non ancora richiesto*'
    return ''


def _reserved_lines(item):
    lines = [f'  *Perimetro riservato:* {item["reserved_reason"] or "(motivo non indicato)"}']
    return lines


def _substitute_lines(record):
    """The lines on the substitute vote (#605): how the wait ended without one, and the
    commands refused."""
    substitute = record.get('substitute')
    if not substitute:
        return []
    lines = []
    if substitute['state'] == SUB_EXPIRED:
        lines.append(f'Nessun voto sostitutivo del coordinatore al posto di '
                     f'`{substitute["member"]}` entro {substitute["deadline"]} (UTC).')
    elif substitute['state'] == SUB_VOID:
        lines.append(f'L\'attesa del voto sostitutivo al posto di `{substitute["member"]}` è '
                     'finita senza voto.')
    for refused in substitute['refused']:
        lines.append(f'- Comando `{VOTE_COMMAND}` del commento {refused["comment"]} rifiutato: '
                     f'{refused["why"]}.')
    return [''] + lines if lines else []


def render(record):
    """The dispatcher's comment of a conclave, with its ``conclave`` marker: the proposal, the
    votes with their reasons and the outcome, or the point reached while it is open."""
    proposal = record['proposal']
    parts = ['## Conclave sul blocco', '',
             f'**Blocco:** {describe_block(record["block"])}  ',
             f'**Passo:** {_STEP_TEXT[record["step"]]} · **Issue:** #{record["issue"]} · '
             f'**Hash dell\'issue:** `{record["issue_hash"][:12]}`  ',
             f'**Commit di riferimento:** `{record["commit"][:12]}`'
             + (f' · **Head della PR:** `{record["head"][:12]}`' if record['head'] else ''),
             '', f'### Proposta di `{proposal["agent"]}`', '']
    if proposal['state'] == DONE and proposal['reserved']:
        parts.append('Il proponente dichiara che la lacuna cade nel perimetro riservato '
                     'all\'operatore: nessuna proposta.')
        parts += _reserved_lines(proposal)
    elif proposal['state'] == DONE:
        parts += [_quote(proposal['resolution']), '', f'Hash della proposta: `{record["hash"]}`']
    else:
        parts.append(_state_text(proposal))
    parts += ['', '### Voti', '']
    substituted, substitute = substitute_vote(record), record.get('substitute')
    for vote in record['votes']:
        if vote['state'] == DONE:
            line = f'- `{vote["agent"]}`: **{vote["vote"]}**'
            if vote is substituted:
                profile = vote['profile']
                line += (f' (voto sostitutivo `{substitute["verdict"]}` del coordinatore '
                         f'`{substitute["by"]}`, modello `{profile["kind"]}` '
                         f'`{profile["model"]}`, al posto di `{vote["agent"]}`, indisponibile: '
                         f'{substitute["why"]})')
            if vote['reason']:
                line += ' — ' + vote['reason'].replace('\n', '\n  ')
            parts.append(line)
            if vote['reserved']:
                parts += _reserved_lines(vote)
        else:
            parts.append(f'- `{vote["agent"]}`: {_state_text(vote)}')
    parts += _substitute_lines(record)
    parts += ['', '### Esito', '']
    result = record['outcome']
    if not result and waiting_substitute(record) is not None:
        parts.append(f'Conclave in attesa del voto sostitutivo del coordinatore al posto di '
                     f'`{substitute["member"]}`, fino a {substitute["deadline"]} (UTC). Il '
                     f'coordinatore vota con `{VOTE_COMMAND} {substitute["conclave"]} '
                     f'{record["hash"]} APPROVE|BLOCK|RESERVED <motivo>` come prima riga di un '
                     'commento; senza un voto entro la scadenza il blocco resta all\'operatore.')
    elif not result:
        parts.append('Conclave in corso: riprende al prossimo tick dal punto registrato.')
    else:
        parts.append(f'**{result}**: {_OUTCOME_TEXT[result]}.')
        if record['stale']:
            parts.append(f'\nIl contesto è cambiato prima della conclusione: {record["stale"]}.')
    if result == RESERVED:
        parts += ['', '### Domanda per l\'operatore', '']
        for item in [proposal, *record['votes']]:
            if item['state'] == DONE and item['reserved']:
                parts.append(f'Da `{item["agent"]}`: '
                             + (item['operator_question'] or '(domanda non indicata)'))
                parts += [f'- {option}' for option in item['options']]
                parts.append('')
    parts += ['', *_closing(record)]
    return publishable(dispatcher_header() + '\n' + '\n'.join(parts).rstrip() + '\n',
                       'conclave', record)
