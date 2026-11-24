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

from .i18n import ENGLISH, Catalog
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

# The reasons devloop writes in the record (a contribution missing, a context changed, a
# substitute vote refused or not given) are composed in ``language``, the project's (#632), when
# they are written: the ``why_`` texts of ``TEXTS``. The functions that write them default to
# English, as ``render``.


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


def too_long(raw, main_key, main_limit, language=ENGLISH):
    """Why ``raw`` exceeds the limits, in ``language``, or ''."""
    t = TEXTS.texts(language)
    if len(raw.get(main_key) or '') > main_limit:
        return t('why_field_long', field=main_key, limit=main_limit)
    for key in ('reserved_reason', 'operator_question', 'reason'):
        if len(raw.get(key) or '') > MAX_TEXT:
            return t('why_field_long', field=key, limit=MAX_TEXT)
    options = raw.get('options') or []
    if len(options) > MAX_OPTIONS or any(len(o) > MAX_OPTION for o in options):
        return t('why_options', count=MAX_OPTIONS, limit=MAX_OPTION)
    return ''


def _reserved_fields(raw):
    return {'reserved': bool(raw['reserved']),
            'reserved_reason': raw['reserved_reason'].strip() if raw['reserved'] else '',
            'operator_question': raw['operator_question'].strip() if raw['reserved'] else '',
            'options': [o.strip() for o in raw['options'] if o.strip()] if raw['reserved']
            else []}


def apply_proposal(record, raw, profile=None, language=ENGLISH):
    """``record`` with the proposer's schema-valid output ``raw``, given with ``profile``. A
    reserved perimeter ends the protocol: no proposal, and the votes are not asked. A proposal
    without text, or beyond the limits, is a failed contribution, its reason in ``language``."""
    t = TEXTS.texts(language)
    proposal = record['proposal']
    why = too_long(raw, 'resolution', MAX_RESOLUTION, language)
    if why:
        return missing(record, 'proposal', t('why_over_limits', why=why), language)
    if raw['reserved']:
        proposal.update(state=DONE, profile=profile, **_reserved_fields(raw))
        for vote in record['votes']:
            vote.update(state=MISSING, missing=t('why_not_asked'))
        return record
    text = raw['resolution']
    if not text.strip():
        return missing(record, 'proposal', t('why_empty'), language)
    proposal.update(state=DONE, resolution=text, hash=text_hash(text), profile=profile,
                    **_reserved_fields(raw))
    record['hash'] = proposal['hash']
    return record


def apply_vote(record, index, raw, proposal_hash, profile=None, language=ENGLISH):
    """``record`` with the schema-valid vote ``raw`` of voter ``index``, given with ``profile``
    on the proposal whose hash is ``proposal_hash``; a vote beyond the limits is missing, its
    reason in ``language``."""
    vote = record['votes'][index]
    why = too_long(raw, 'reason', MAX_TEXT, language)
    if why:
        return missing(record, index, TEXTS.texts(language)('why_over_limits', why=why),
                       language)
    vote.update(state=DONE, vote=raw['vote'], reason=raw['reason'].strip(),
                proposal_hash=proposal_hash, profile=profile, **_reserved_fields(raw))
    return record


def missing(record, which, reason, language=ENGLISH):
    """``record`` with contribution ``which`` (``'proposal'`` or a voter's index) missing for
    ``reason``. A missing proposal leaves nothing to vote on: the votes are missing too. A
    contribution missing while a vote waits for the coordinator's substitute vote ends the wait
    (#605): one substitute vote per conclave, so two members missing leave it to the operator,
    however the second one went missing (a failed call, an output over the limits, a call
    started without a result). The reasons it adds are in ``language``."""
    t = TEXTS.texts(language)
    item = record['proposal'] if which == 'proposal' else record['votes'][which]
    item.update(state=MISSING, missing=reason)
    if which == 'proposal':
        for vote in record['votes']:
            if vote['state'] in (PENDING, STARTED):
                vote.update(state=MISSING, missing=t('why_no_proposal'))
    if waiting_substitute(record) is not None:
        _void(record, t('why_second_member'))
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


def await_substitute(record, index, reason, deadline, conclave, after, language=ENGLISH):
    """``record`` whose voter ``index`` failed for ``reason``, with a coordinator: the vote
    waits for the coordinator's substitute vote until ``deadline`` (ISO 8601, UTC), on the
    conclave of comment ``conclave``; ``after`` is the latest comment id read before the wait,
    so that only a command written later counts. One substitute vote per conclave: when a
    member is already substituted, or waits, this vote is missing, and so is the waiting one;
    when another vote is already missing (an output over the limits, a call started without a
    result), this one is missing too, and no wait opens: two members missing leave the
    conclave to the operator at once. The reasons it adds are in ``language``."""
    if record.get('substitute'):
        return missing(record, index, reason, language)
    if any(vote['state'] == MISSING for i, vote in enumerate(record['votes']) if i != index):
        return missing(record, index,
                       TEXTS.texts(language)('why_other_member', reason=reason), language)
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


def expire_substitute(record, language=ENGLISH):
    """``record`` whose substitute vote did not come before its deadline: the vote is missing,
    and the conclave goes to the operator as before (#605). The reason is in ``language``."""
    if waiting_substitute(record) is not None:
        _void(record, TEXTS.texts(language)('why_expired'), SUB_EXPIRED)
    return record


def refuse_substitute(record, comment, why):
    """``record`` with the command of comment ``comment`` refused for ``why``: spent, and kept
    among the latest ``MAX_REFUSED``."""
    substitute = record['substitute']
    substitute['last'] = max(substitute['last'], comment)
    substitute['refused'] = (substitute['refused']
                             + [{'comment': comment, 'why': why}])[-MAX_REFUSED:]
    return record


def substitute_problem(record, command, language=ENGLISH):
    """Why the coordinator's ``command`` (``vote_command``) does not apply to the substitute
    vote ``record`` waits for, in ``language`` (that of the command's ``error`` too), or ''.
    Who wrote it and when are the dispatcher's checks."""
    t = TEXTS.texts(language)
    substitute = record['substitute']
    if command['error']:
        return command['error']
    if command['conclave'] != substitute['conclave']:
        return t('why_other_conclave', named=command['conclave'],
                 conclave=substitute['conclave'])
    if command['hash'] != record['hash']:
        return t('why_hash')
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


def make_stale(record, reason, language=ENGLISH):
    """``record`` whose frozen context no longer holds: every contribution not done is missing,
    and the outcome is ``BLOCKED`` (unless a member already flagged the reserved perimeter).
    The reasons it adds are in ``language``."""
    record['stale'] = reason
    changed = TEXTS.texts(language)('why_stale', reason=reason)
    if waiting_substitute(record) is not None:
        _void(record, changed)
    for item in [record['proposal'], *record['votes']]:
        if item['state'] in (PENDING, STARTED):
            item.update(state=MISSING, missing=changed)
    return record


def settle_started(record, language=ENGLISH):
    """``record`` with every call started without a durable result marked missing: it counts as
    a failure, ``BLOCCA``, and is not repeated. The reason is in ``language``."""
    why = TEXTS.texts(language)('why_started')
    if record['proposal']['state'] == STARTED:
        missing(record, 'proposal', why, language)
    for index, vote in enumerate(record['votes']):
        if vote['state'] == STARTED:
            missing(record, index, why, language)
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


def vote_command(body, language=ENGLISH):
    """The coordinator's substitute vote in ``body`` (#605), or None when the first line after
    the attribution header is no ``/devloop vote``: ``{conclave, hash, verdict, reason,
    error}``, ``conclave`` the comment id of the conclave (None when it is not one), ``reason``
    the rest of the comment from the rest of the first line on, and ``error``, in ``language``
    (the project's, #632), why the command is malformed ('' when it is not)."""
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
        command['error'] = TEXTS.texts(language)('why_malformed')
    elif len(reason) > MAX_TEXT:
        command['error'] = TEXTS.texts(language)('why_reason_long', limit=MAX_TEXT)
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


def _closing(t, record):
    """The last lines of the comment: what the outcome means for the work."""
    if record['outcome'] != UNANIMOUS:
        return [t('closing_blocked')]
    lines = [t('closing_unanimous', veto=VETO_COMMAND)]
    resumed = record.get('resumed')
    if resumed is not None:
        lines += ['', t('resumed', step=record['step'])]
        evidence = resumed.get('evidence')
        if evidence:
            lines.append(t('evidence', file=evidence['file'], bytes=evidence['bytes'],
                           sha256=evidence['sha256']))
    return lines


# Rendering ----------------------------------------------------------------------------------
# The texts of the conclave's comment (#632), in the project's ``language`` (``i18n``); English
# for a language without translation. The votes (``APPROVA``, ``BLOCCA``), the outcomes and the
# commands are values of the record and of the agents' answers: they are never translated. The
# reasons the record holds (a contribution missing, a context changed) are shown as recorded:
# devloop composes them in the project's language when it writes them (the ``why_`` texts).
TEXTS = Catalog(
    en={
        'outcome_UNANIMOUS': 'the proposal and two `APPROVA`: three agreements',
        'outcome_BLOCKED': 'at least one `BLOCCA`, a missing contribution or a changed context',
        'outcome_RESERVED': 'a member flagged the perimeter reserved to the operator',
        'step_implement': 'implementation',
        'step_fix': 'fix',
        'step_review': 'review',
        'empty': '(empty)',
        'block_blocked': '`{agent}` stopped (`blocked`) in step `{step}`, comment {comment}',
        'block_review': ('verdict `needs_operator` of `{reviewer}` in round {round} on commit '
                         '`{head}`'),
        'waiting': '*unavailable, waiting for the substitute vote:* {missing}',
        'missing': '*missing:* {missing}',
        'started': '*in progress*',
        'pending': '*not asked yet*',
        'reserved': '  *Reserved perimeter:* {reason}',
        'no_reason': '(reason not given)',
        'expired': ('No substitute vote of the coordinator in place of `{member}` by {deadline} '
                    '(UTC).'),
        'void': 'The wait for the substitute vote in place of `{member}` ended without a vote.',
        'refused': '- Command `{command}` of comment {comment} refused: {why}.',
        'title': '## Conclave on the block',
        'block': '**Block:** {block}  ',
        'step': ('**Step:** {step} · **Issue:** #{issue} · **Hash of the issue:** '
                 '`{issue_hash}`  '),
        'commit': '**Reference commit:** `{commit}`',
        'head': ' · **Head of the PR:** `{head}`',
        'proposal': '### Proposal of `{agent}`',
        'proposal_reserved': ('The proposer declares that the gap falls in the perimeter '
                              'reserved to the operator: no proposal.'),
        'proposal_hash': 'Hash of the proposal: `{hash}`',
        'votes': '### Votes',
        'substituted': (' (substitute vote `{verdict}` of the coordinator `{by}`, model '
                        '`{kind}` `{model}`, in place of `{agent}`, unavailable: {why})'),
        'outcome': '### Outcome',
        'awaiting': ('Conclave waiting for the substitute vote of the coordinator in place of '
                     '`{member}`, until {deadline} (UTC). The coordinator votes with '
                     '`{command} {conclave} {hash} APPROVE|BLOCK|RESERVED <reason>` as the first '
                     'line of a comment; without a vote by the deadline the block stays with '
                     'the operator.'),
        'open': 'Conclave in progress: it resumes at the next tick from the recorded point.',
        'stale': '\nThe context changed before the conclusion: {stale}.',
        'question_title': '### Question for the operator',
        'question': 'From `{agent}`: {question}',
        'no_question': '(question not given)',
        'closing_blocked': ('The conclave records the decision and does not unblock the work: '
                            'the escalation stays with the operator.'),
        'closing_unanimous': ('With `UNANIMOUS` the stopped step runs again once, with the '
                              'resolution in its prompt. A trusted user annuls it by writing '
                              '`{veto}` as the first line of a comment on the issue or on the '
                              'PR. The normal review of the PR and the human merge stay '
                              'required.'),
        'resumed': ('**Resumed:** step `{step}` runs again from the published commit, with the '
                    'resolution in its prompt.'),
        'evidence': ('The uncommitted changes of the stopped agent are not taken up as work: '
                     'they are saved as evidence in `{file}` (log of the dispatcher, {bytes} '
                     'bytes, SHA-256 `{sha256}`).'),
        # The reasons devloop writes in a new record (#632): a contribution missing, a context
        # changed, a substitute vote refused or not given.
        'why_started': ('call started without a durable result (restart or error of the '
                        'dispatcher): it counts as `BLOCCA` and is not repeated'),
        'why_field_long': '`{field}` exceeds {limit} characters',
        'why_options': 'more than {count} options, or an option over {limit} characters',
        'why_over_limits': 'output beyond the limits: {why}',
        'why_not_asked': ('not asked: the proposer flagged the perimeter reserved to the '
                          'operator'),
        'why_empty': 'empty resolution',
        'why_no_proposal': 'no proposal to vote on',
        'why_second_member': ('a second member did not vote: one substitute vote per '
                              'conclave'),
        'why_other_member': ('{reason}; another member did not vote: one substitute vote per '
                             'conclave'),
        'why_expired': 'no substitute vote of the coordinator by the deadline',
        'why_other_conclave': 'the command names conclave {named}, not this one ({conclave})',
        'why_hash': 'the SHA-256 is not the one of the current proposal of the conclave',
        'why_stale': 'context changed: {reason}',
        'why_malformed': ('malformed command: it is written as `/devloop vote <conclave> '
                          '<sha256> APPROVE|BLOCK|RESERVED <reason>`'),
        'why_reason_long': 'the reason exceeds {limit} characters',
        'why_issue_changed': 'the text of the issue changed',
        'why_head_moved': 'the head of the PR moved to `{head}`',
        'why_diff_changed': 'the uncommitted changes of the stopped agent changed',
        'why_untrusted': 'the text of the issue is no longer trusted ({error})',
        'why_head_not_commit': 'the head of the PR (`{head}`) is not the commit of the block',
        'why_hash_mismatch': 'the text of the proposal does not match its hash',
        'why_failed': 'agent failed or timed out: {error}',
        'why_unreadable_time': ('the time of the command, of its latest change or the deadline '
                                'cannot be read'),
        'why_late': 'written or changed after the deadline of the substitute vote ({deadline})',
    },
    it={
        'outcome_UNANIMOUS': 'la proposta e due `APPROVA`: tre consensi',
        'outcome_BLOCKED': 'almeno un `BLOCCA`, un contributo mancante o un contesto cambiato',
        'outcome_RESERVED': 'un membro ha segnalato il perimetro riservato all\'operatore',
        'step_implement': 'implementazione',
        'step_fix': 'correzione',
        'step_review': 'review',
        'empty': '(vuoto)',
        'block_blocked': ('`{agent}` si è fermato (`blocked`) nel passo `{step}`, commento '
                          '{comment}'),
        'block_review': ('verdetto `needs_operator` di `{reviewer}` nel round {round} sul '
                         'commit `{head}`'),
        'waiting': '*indisponibile, in attesa del voto sostitutivo:* {missing}',
        'missing': '*mancante:* {missing}',
        'started': '*in corso*',
        'pending': '*non ancora richiesto*',
        'reserved': '  *Perimetro riservato:* {reason}',
        'no_reason': '(motivo non indicato)',
        'expired': ('Nessun voto sostitutivo del coordinatore al posto di `{member}` entro '
                    '{deadline} (UTC).'),
        'void': ('L\'attesa del voto sostitutivo al posto di `{member}` è finita senza '
                 'voto.'),
        'refused': '- Comando `{command}` del commento {comment} rifiutato: {why}.',
        'title': '## Conclave sul blocco',
        'block': '**Blocco:** {block}  ',
        'step': ('**Passo:** {step} · **Issue:** #{issue} · **Hash dell\'issue:** '
                 '`{issue_hash}`  '),
        'commit': '**Commit di riferimento:** `{commit}`',
        'head': ' · **Head della PR:** `{head}`',
        'proposal': '### Proposta di `{agent}`',
        'proposal_reserved': ('Il proponente dichiara che la lacuna cade nel perimetro '
                              'riservato all\'operatore: nessuna proposta.'),
        'proposal_hash': 'Hash della proposta: `{hash}`',
        'votes': '### Voti',
        'substituted': (' (voto sostitutivo `{verdict}` del coordinatore `{by}`, modello '
                        '`{kind}` `{model}`, al posto di `{agent}`, indisponibile: {why})'),
        'outcome': '### Esito',
        'awaiting': ('Conclave in attesa del voto sostitutivo del coordinatore al posto di '
                     '`{member}`, fino a {deadline} (UTC). Il coordinatore vota con '
                     '`{command} {conclave} {hash} APPROVE|BLOCK|RESERVED <motivo>` come prima '
                     'riga di un commento; senza un voto entro la scadenza il blocco resta '
                     'all\'operatore.'),
        'open': 'Conclave in corso: riprende al prossimo tick dal punto registrato.',
        'stale': '\nIl contesto è cambiato prima della conclusione: {stale}.',
        'question_title': '### Domanda per l\'operatore',
        'question': 'Da `{agent}`: {question}',
        'no_question': '(domanda non indicata)',
        'closing_blocked': ('Il conclave registra la decisione e non sblocca il lavoro: '
                            'l\'escalation resta all\'operatore.'),
        'closing_unanimous': ('Con `UNANIMOUS` il passo fermo riparte una volta, con la '
                              'risoluzione nel prompt. Un utente fidato la annulla scrivendo '
                              '`{veto}` come prima riga di un commento sull\'issue o sulla PR. '
                              'La review normale della PR e il merge umano restano '
                              'obbligatori.'),
        'resumed': ('**Ripreso:** il passo `{step}` riparte dal commit pubblicato, con la '
                    'risoluzione nel prompt.'),
        'evidence': ('Le modifiche non committate dell\'agent fermo non sono riprese come '
                     'lavoro: sono salvate come evidenza in `{file}` (log del dispatcher, '
                     '{bytes} byte, SHA-256 `{sha256}`).'),
        'why_started': ('chiamata avviata senza un risultato durevole (riavvio o errore del '
                        'dispatcher): conta come `BLOCCA` e non si ripete'),
        'why_field_long': '`{field}` supera {limit} caratteri',
        'why_options': 'più di {count} opzioni, o un\'opzione oltre {limit} caratteri',
        'why_over_limits': 'output oltre i limiti: {why}',
        'why_not_asked': ('non richiesto: il proponente ha segnalato il perimetro riservato '
                          'all\'operatore'),
        'why_empty': 'risoluzione vuota',
        'why_no_proposal': 'nessuna proposta da votare',
        'why_second_member': ('un secondo membro non ha votato: un solo voto sostitutivo per '
                              'conclave'),
        'why_other_member': ('{reason}; un altro membro non ha votato: un solo voto sostitutivo '
                             'per conclave'),
        'why_expired': 'nessun voto sostitutivo del coordinatore entro la scadenza',
        'why_other_conclave': ('il comando nomina il conclave {named}, non questo '
                               '({conclave})'),
        'why_hash': 'lo SHA-256 non è quello della proposta corrente del conclave',
        'why_stale': 'contesto cambiato: {reason}',
        'why_malformed': ('comando non ben formato: va scritto come `/devloop vote <conclave> '
                          '<sha256> APPROVE|BLOCK|RESERVED <motivo>`'),
        'why_reason_long': 'il motivo supera {limit} caratteri',
        'why_issue_changed': 'il testo dell\'issue è cambiato',
        'why_head_moved': 'l\'head della PR è passato a `{head}`',
        'why_diff_changed': 'le modifiche non committate dell\'agent fermo sono cambiate',
        'why_untrusted': 'il testo dell\'issue non è più fidato ({error})',
        'why_head_not_commit': ('l\'head della PR (`{head}`) non è il commit del blocco'),
        'why_hash_mismatch': 'il testo della proposta non corrisponde al suo hash',
        'why_failed': 'agent fallito o in timeout: {error}',
        'why_unreadable_time': ('l\'ora del comando, della sua ultima modifica o la scadenza '
                                'non sono leggibili'),
        'why_late': ('scritto o modificato dopo la scadenza del voto sostitutivo '
                     '({deadline})'),
    })


def _quote(t, text):
    return '\n'.join('> ' + line for line in (text.strip() or t('empty')).splitlines())


def describe_block(block, language=ENGLISH):
    """The block as the comment and the prompts name it, in ``language`` (#632): the comment
    in the project's, the prompts, written in English, in English."""
    t = TEXTS.texts(language)
    if block['kind'] == 'blocked':
        return t('block_blocked', agent=block['agent'], step=block['step'],
                 comment=block['comment'])
    return t('block_review', reviewer=block['reviewer'], round=block['round'],
             head=block['head'][:12])


def _state_text(t, item):
    if item['state'] == WAITING:
        return t('waiting', missing=item['missing'])
    if item['state'] == MISSING:
        return t('missing', missing=item['missing'])
    if item['state'] == STARTED:
        return t('started')
    if item['state'] == PENDING:
        return t('pending')
    return ''


def _reserved_lines(t, item):
    return [t('reserved', reason=item['reserved_reason'] or t('no_reason'))]


def _substitute_lines(t, record):
    """The lines on the substitute vote (#605): how the wait ended without one, and the
    commands refused."""
    substitute = record.get('substitute')
    if not substitute:
        return []
    lines = []
    if substitute['state'] == SUB_EXPIRED:
        lines.append(t('expired', member=substitute['member'], deadline=substitute['deadline']))
    elif substitute['state'] == SUB_VOID:
        lines.append(t('void', member=substitute['member']))
    for refused in substitute['refused']:
        lines.append(t('refused', command=VOTE_COMMAND, comment=refused['comment'],
                       why=refused['why']))
    return [''] + lines if lines else []


def render(record, language=ENGLISH):
    """The dispatcher's comment of a conclave, with its ``conclave`` marker: the proposal, the
    votes with their reasons and the outcome, or the point reached while it is open. Its
    readable text is in ``language``, the project's (#632); the record is the same."""
    t = TEXTS.texts(language)
    proposal = record['proposal']
    parts = [t('title'), '',
             t('block', block=describe_block(record['block'], language)),
             t('step', step=t('step_' + record['step']), issue=record['issue'],
               issue_hash=record['issue_hash'][:12]),
             t('commit', commit=record['commit'][:12])
             + (t('head', head=record['head'][:12]) if record['head'] else ''),
             '', t('proposal', agent=proposal['agent']), '']
    if proposal['state'] == DONE and proposal['reserved']:
        parts.append(t('proposal_reserved'))
        parts += _reserved_lines(t, proposal)
    elif proposal['state'] == DONE:
        parts += [_quote(t, proposal['resolution']), '',
                  t('proposal_hash', hash=record['hash'])]
    else:
        parts.append(_state_text(t, proposal))
    parts += ['', t('votes'), '']
    substituted, substitute = substitute_vote(record), record.get('substitute')
    for vote in record['votes']:
        if vote['state'] == DONE:
            line = f'- `{vote["agent"]}`: **{vote["vote"]}**'
            if vote is substituted:
                profile = vote['profile']
                line += t('substituted', verdict=substitute['verdict'], by=substitute['by'],
                          kind=profile['kind'], model=profile['model'], agent=vote['agent'],
                          why=substitute['why'])
            if vote['reason']:
                line += ' — ' + vote['reason'].replace('\n', '\n  ')
            parts.append(line)
            if vote['reserved']:
                parts += _reserved_lines(t, vote)
        else:
            parts.append(f'- `{vote["agent"]}`: {_state_text(t, vote)}')
    parts += _substitute_lines(t, record)
    parts += ['', t('outcome'), '']
    result = record['outcome']
    if not result and waiting_substitute(record) is not None:
        parts.append(t('awaiting', member=substitute['member'], deadline=substitute['deadline'],
                       command=VOTE_COMMAND, conclave=substitute['conclave'],
                       hash=record['hash']))
    elif not result:
        parts.append(t('open'))
    else:
        parts.append(f'**{result}**: {t("outcome_" + result)}.')
        if record['stale']:
            parts.append(t('stale', stale=record['stale']))
    if result == RESERVED:
        parts += ['', t('question_title'), '']
        for item in [proposal, *record['votes']]:
            if item['state'] == DONE and item['reserved']:
                parts.append(t('question', agent=item['agent'],
                               question=item['operator_question'] or t('no_question')))
                parts += [f'- {option}' for option in item['options']]
                parts.append('')
    parts += ['', *_closing(t, record)]
    return publishable(dispatcher_header() + '\n' + '\n'.join(parts).rstrip() + '\n',
                       'conclave', record)
