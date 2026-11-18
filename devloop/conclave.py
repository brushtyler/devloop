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
``conclave`` record; ``dispatch_conclave`` runs it. Part 2 only records the decision: nothing is
unblocked, and the item stays with the operator.
"""

import hashlib

from .markdown import dispatcher_header, publishable
from .policy import independent, valid_profile

UNANIMOUS, BLOCKED, RESERVED = 'UNANIMOUS', 'BLOCKED', 'RESERVED'
OUTCOMES = (UNANIMOUS, BLOCKED, RESERVED)
APPROVE, BLOCK = 'APPROVA', 'BLOCCA'
# The state of each contribution: not started, started (the record written before the call),
# done, or missing (with the reason).
PENDING, STARTED, DONE, MISSING = 'pending', 'started', 'done', 'missing'
STATES = (PENDING, STARTED, DONE, MISSING)
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


def _too_long(raw, main_key, main_limit):
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
    why = _too_long(raw, 'resolution', MAX_RESOLUTION)
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
    why = _too_long(raw, 'reason', MAX_TEXT)
    if why:
        return missing(record, index, f'output oltre i limiti: {why}')
    vote.update(state=DONE, vote=raw['vote'], reason=raw['reason'].strip(),
                proposal_hash=proposal_hash, profile=profile, **_reserved_fields(raw))
    return record


def missing(record, which, reason):
    """``record`` with contribution ``which`` (``'proposal'`` or a voter's index) missing for
    ``reason``. A missing proposal leaves nothing to vote on: the votes are missing too."""
    item = record['proposal'] if which == 'proposal' else record['votes'][which]
    item.update(state=MISSING, missing=reason)
    if which == 'proposal':
        for vote in record['votes']:
            if vote['state'] in (PENDING, STARTED):
                vote.update(state=MISSING, missing='nessuna proposta da votare')
    return record


def make_stale(record, reason):
    """``record`` whose frozen context no longer holds: every contribution not done is missing,
    and the outcome is ``BLOCKED`` (unless a member already flagged the reserved perimeter)."""
    record['stale'] = reason
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
    ``BLOCCA`` give ``BLOCKED``; two ``APPROVA`` on the proposal give ``UNANIMOUS``."""
    items = [record['proposal'], *record['votes']]
    if any(item['state'] in (PENDING, STARTED) for item in items):
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
            and (item['state'] != MISSING or bool(item['missing'])))


def valid_block(block):
    if not isinstance(block, dict) or block.get('kind') not in BLOCK_KINDS:
        return False
    if block['kind'] == 'blocked':
        return (isinstance(block.get('comment'), int) and not isinstance(block['comment'], bool)
                and _is_str(block.get('agent')) and block.get('step') in STEPS)
    return (isinstance(block.get('round'), int) and not isinstance(block['round'], bool)
            and _is_str(block.get('reviewer')) and _is_str(block.get('head')))


def valid_record(r):
    """The structure of a ``conclave`` marker."""
    return (valid_block(r.get('block')) and r.get('step') in STEPS
            and isinstance(r.get('issue'), int) and not isinstance(r['issue'], bool)
            and all(_is_str(r.get(k)) for k in ('issue_hash', 'commit', 'diff_hash', 'stale',
                                                'hash'))
            and (r.get('head') is None or _is_str(r['head']))
            and _valid_item(r.get('proposal'), vote=False)
            and isinstance(r.get('votes'), list) and len(r['votes']) == 2
            and all(_valid_item(v, vote=True) for v in r['votes'])
            and r.get('outcome') in ('', *OUTCOMES))


def consistent(record):
    """Whether ``record`` holds together, as the dispatcher reads it back: the content hash is
    the hash of the proposal's text, every vote was given on that hash, and a recorded outcome
    is the one its contributions give, and every contribution records the profile it was given
    with, the three distinct by identity and by ``(kind, model, effort)``, fallback models
    included. Returns ``(ok, why)``."""
    proposal = record['proposal']
    done = [item for item in [proposal, *record['votes']] if item['state'] == DONE]
    if any(item['profile'] is None for item in done):
        return False, 'un contributo non registra il profilo con cui è stato dato'
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
    return True, ''


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
    for vote in record['votes']:
        if vote['state'] == DONE:
            line = f'- `{vote["agent"]}`: **{vote["vote"]}**'
            if vote['reason']:
                line += ' — ' + vote['reason'].replace('\n', '\n  ')
            parts.append(line)
            if vote['reserved']:
                parts += _reserved_lines(vote)
        else:
            parts.append(f'- `{vote["agent"]}`: {_state_text(vote)}')
    parts += ['', '### Esito', '']
    result = record['outcome']
    if not result:
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
    parts += ['', 'Il conclave registra la decisione e non sblocca il lavoro: l\'escalation resta '
                  'all\'operatore.']
    return publishable(dispatcher_header() + '\n' + '\n'.join(parts).rstrip() + '\n',
                       'conclave', record)
