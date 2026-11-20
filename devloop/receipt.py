"""The receipt of a PR promoted to ``agent:ready-to-merge`` (#135, part 3 of #133).

When a PR is promoted, the dispatcher writes a comment that tells the operator who merges, at a
glance, what was asked, who did what with which model and at which cost, what was verified on
which commit, how the findings were closed and how the acceptance criteria are covered. It is
bound to the approved head and to the promotion, and it has two forms in one comment: the
readable text, with the dispatcher's header, and a ``receipt`` marker with its identity (PR,
issue, head, base, policy version, level, number of the promotion, digest of the content), the
totals and the data declared missing. Only the dispatcher's identity writes it. Since #338 that
comment is the promotion's own: it carries the ``policy`` record of the promotion and the
``receipt`` record, and a merge by devloop adds its section and its ``merged`` record to it.

Its content comes **only** from the dispatcher's ledger records (#133) and from the markers,
never from text written by agents: no summary, no finding title, no error text. The final
acceptance map is the one part an agent declared: the dispatcher recorded it in its markers, and
the receipt keeps its test references and reasons as they are, shown only as inert code. A value
that was not attested is ``None`` in the data and ``non riportato`` in the text, never 0.
With the budget on (#393), a section of its own shows the state of the budget of the issue's
story as the dispatcher's journal records it (``budget.view``), its dollars not attested as
``non attestato``; with the budget off there is no such section, and the content is the one
before #393.

Before the promotion the content is checked against the markers (:func:`contradictions`). A
contradiction blocks the promotion; data that is simply missing, as in PRs started before #133,
does not, and the receipt declares it (:func:`missing`).

When the approval is revoked the receipt is marked superseded (:func:`superseded`): its comment
is edited with a warning, and its marker records ``state: superseded``. A new promotion writes a
new receipt, after marking every earlier one.
"""

import hashlib
import json

from .agents import TELEMETRY_FIELDS
from .budget import NOT_ATTESTED, consumed_usd, usd
from .markdown import dispatcher_header, publishable, split_markers, with_record
from .planning import counted_rounds
from .policy import describe as describe_policy
from .review import CI, CLOSING, OPERATOR, finding_key, finding_state, owner_of

NOT_REPORTED = 'non riportato'
# The class of the escalation of a receipt that contradicts the markers: critical.
CONFLICT = 'receipt_conflict'
STATES = ('current', 'superseded')
TOKENS = ('input_tokens', 'output_tokens', 'cache_read_input_tokens',
          'cache_creation_input_tokens', 'thinking_tokens')
# The turn fields a receipt keeps: no error text, which may quote what an agent wrote.
TURN_FIELDS = ('kind', 'agent', 'identity', 'model', 'effort', 'fallback_for', 'model_real',
               'state', 'started_at', 'duration_ms', 'outcome')
# Data a PR started before #133 or #134 may lack: declared, never reconstructed.
MISSING_TEXT = {
    'request': 'snapshot della richiesta (issue presa in carico prima di #133)',
    'turns': 'turni degli agent (nessun record del registro: lavoro avviato prima di #133)',
    'implement': 'turno dell\'implementazione (nessun record sull\'issue)',
    'ci': 'esito della CI sul head approvato nel registro',
    'acceptance_map': 'mappa di copertura dei criteri (PR aperta prima di #134)',
    'criteria': 'criteri di accettazione (testo dell\'issue non verificabile)',
}
_VERDICT = {'approve': 'approvata', 'request_changes': 'modifiche richieste',
            'needs_operator': 'decisione dell\'operatore',
            'unproven': 'nessun finding bloccante con evidenza'}
_CLOSED = {'resolved': 'risolto', 'withdrawn': 'ritirato'}
_MAP_STATUS = {'covered': 'coperto', 'not_covered': 'non coperto'}
# What a receipt keeps of the advisory review of its head (#369).
ADVISORY_FIELDS = ('head', 'reviewer', 'outcome', 'opinion')


# Content -------------------------------------------------------------------
def _known_sum(values):
    """The sum of the values that are known, or None when none is: never 0 for nothing."""
    known = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return sum(known) if known else None


def _turn(record, where):
    turn = {k: record.get(k) for k in TURN_FIELDS}
    telemetry = record.get('telemetry') or {}
    turn['telemetry'] = {k: telemetry.get(k) for k in TELEMETRY_FIELDS}
    turn['on'] = where
    return turn


def _event(record):
    keep = ('event', 'head', 'state', 'round', 'base', 'previous_head', 'at')
    event = {k: record[k] for k in keep if k in record}
    if isinstance(record.get('checks'), list):
        event['checks'] = [{'name': c.get('name'), 'outcome': c.get('outcome'),
                            'duration_s': c.get('duration_s')} for c in record['checks']]
    return event


def _verification(record):
    return {k: record.get(k) for k in ('kind', 'attempt', 'outcome', 'duration_ms', 'commit',
                                       'at')}


def _rounds(reviews):
    rounds = {}
    for review in reviews:
        entry = rounds.setdefault(review['round'], {'round': review['round'],
                                                    'head': review['head_sha'], 'verdicts': []})
        entry['verdicts'].append({'reviewer': owner_of(review), 'verdict': review['verdict']})
    return [rounds[n] for n in sorted(rounds)]


def _findings(reviews, closures):
    """Every blocking finding ever opened, ``{key, status, by, author, round}``: ``open``, or
    how it was closed and by whom (its owner in a later round, ``ci`` or ``operator`` with the
    author). A finding closed under the rules before #110 has no recorded closer (None)."""
    owners, order, closed = {}, [], {}
    for review in reviews:
        owner = owner_of(review)
        for p in review['previous_findings']:
            key = p.get('key')
            if (key and p.get('status') in CLOSING and owners.get(key) == owner
                    and key not in closed):
                closed[key] = {'status': p['status'], 'by': owner, 'author': None,
                               'round': review['round']}
        for f in review['blocking_findings']:
            key = f.get('key') or finding_key(review['round'], owner, f['id'])
            if key not in owners:
                owners[key] = owner
                order.append(key)
    for c in closures:
        key = c['key']
        if key in owners and key not in closed and (c['by'] == OPERATOR or owners[key] == CI):
            closed[key] = {'status': c['status'], 'by': c['by'], 'author': c.get('author'),
                           'round': None}
    still_open = {f['key'] for f in finding_state(reviews, closures).open}
    found = []
    for key in order:
        if key in still_open:
            found.append({'key': key, 'status': 'open', 'by': None, 'author': None,
                          'round': None})
        else:
            found.append(dict({'key': key, 'status': 'closed', 'by': None, 'author': None,
                               'round': None}, **closed.get(key, {})))
    return found


def _acceptance(criteria, acceptance_map):
    """The final map (#134) as the markers record it: each criterion as the issue states it,
    and for each entry its status, the test references ``{file, name}`` and the reason. The
    references and the reason are data the dispatcher recorded in its markers, kept as they
    are and shown only as inert code (:func:`_literal`); the criterion text the agent repeated
    is left out, the issue's own text stands for it."""
    if acceptance_map is None:
        return {'criteria': criteria, 'map': None}
    entries = [{'status': e['status'],
                'tests': [{'file': t['file'], 'name': t['name']} for t in e['tests']],
                'reason': e['reason']} for e in acceptance_map]
    return {'criteria': criteria, 'map': entries}


def totals(turns, verifications, ci, reviews):
    """The totals of a receipt: rounds (all and those counted against the limit), turns, the
    summed duration of the turns, the known tokens and cost, how many turns reported no cost
    and no tokens; the duration of the verifications apart: the local tests and the checks of
    the CI on the approved head."""
    telemetry = [t['telemetry'] for t in turns]
    return {
        'rounds': len({r['round'] for r in reviews}),
        'counted_rounds': counted_rounds(reviews),
        'turns': len(turns),
        'turns_ms': _known_sum(t['duration_ms'] for t in turns),
        'tokens': {k: _known_sum(t.get(k) for t in telemetry) for k in TOKENS},
        'cost_usd': _known_sum(t.get('cost_usd') for t in telemetry),
        'cost_unreported': sum(1 for t in telemetry if t.get('cost_usd') is None),
        'tokens_unreported': sum(1 for t in telemetry if all(t.get(k) is None for k in TOKENS)),
        'verifications_ms': _known_sum(v['duration_ms'] for v in verifications),
        'ci_s': (_known_sum(c.get('duration_s') for c in ci.get('checks') or ())
                 if ci else None),
    }


def build(*, pr, issue, head, promotion, policy, request, issue_records, pr_records, reviews,
          closures, approved_round, criteria, acceptance_map, advisory=None, budget=None):
    """The content of the receipt of PR ``pr`` (issue ``issue``) promoted on ``head``.

    ``policy``: the policy receipt recomputed for the promotion; ``request``: the latest
    snapshot of the issue (None if none); ``issue_records`` and ``pr_records``: what
    ``ledger.collect`` reads on the issue and on the PR; ``reviews`` and ``closures``: the
    review markers and the dispatcher's finding closures; ``approved_round``: the number of the
    approved round (None at level 0); ``criteria``: the criteria of the issue text (None when it
    cannot be trusted); ``acceptance_map``: the latest map (None when none is recorded);
    ``advisory``: the dispatcher's record of the advisory review of ``head`` (#369), or None.
    Without one the content has no ``advisory`` key, and its digest is the one before #369.
    ``budget``: with the budget on (#393), the state of the budget of the issue's story from
    the journal (``budget.view``), or ``{'story': None}`` when the issue belongs to no story;
    None with the budget off, and then the content has no ``budget`` key."""
    turns = ([_turn(t, 'issue') for t in issue_records['turns']]
             + [_turn(t, 'pr') for t in pr_records['turns']])
    events = [_event(e) for e in pr_records['events']]
    ci_events = [e for e in events if e.get('event') == 'ci_round' and e.get('head') == head
                 and e.get('round') == approved_round]
    ci = ci_events[-1] if ci_events else None
    verifications = ([_verification(v) for v in issue_records['verifications']]
                     + [_verification(v) for v in pr_records['verifications']])
    content = {
        'pr': pr, 'issue': issue, 'head': head, 'promotion': promotion,
        'request': dict(request) if request else None,
        'turns': turns, 'events': events,
        'verifications': {'local': verifications,
                          'head': [v for v in verifications if v['commit'] == head],
                          'ci': ci, 'ci_events': ci_events},
        'rounds': _rounds(reviews), 'approved_round': approved_round,
        'findings': _findings(reviews, closures),
        'policy': policy,
        'acceptance': _acceptance(criteria, acceptance_map),
        'totals': totals(turns, verifications, ci, reviews),
    }
    if advisory is not None:
        # Only the identity and the opinion, never the text the agent wrote (#369).
        content['advisory'] = {k: advisory.get(k) for k in ADVISORY_FIELDS}
    if budget is not None:
        content['budget'] = budget
    content['missing'] = missing(content)
    return content


def missing(content):
    """The data the receipt lacks and declares, as keys of ``MISSING_TEXT``: never a reason to
    block the promotion."""
    gaps = []
    if content['request'] is None:
        gaps.append('request')
    if not content['turns']:
        gaps.append('turns')
    elif not any(t['kind'] == 'implement' for t in content['turns']):
        gaps.append('implement')
    if content['verifications']['ci'] is None:
        gaps.append('ci')
    acceptance = content['acceptance']
    if acceptance['criteria'] is None:
        gaps.append('criteria')
    if acceptance['map'] is None:
        gaps.append('acceptance_map')
    return gaps


def contradictions(content, *, implementer, approved_head):
    """Where the receipt and the markers disagree, as Italian sentences: each blocks the
    promotion. Missing data never counts here (:func:`missing`).

    - the head of the policy receipt is not the approved head (the latest round's, or the
      level-0 one);
    - the ledger records, for the approved head and round, a CI other than ``pass`` or ``none``
      as its latest state, while the promotion relies on a CI passed or absent;
    - the request snapshot names another issue than the PR marker;
    - a fix or sync turn on the PR was run by another agent than the PR's implementer, or a
      review turn by the implementer itself."""
    found = []
    head = content['head']
    if approved_head != head or content['policy'].get('head') != head:
        found.append(f'il commit approvato (`{str(approved_head)[:12]}`) e il head della '
                     f'ricevuta di policy (`{str(content["policy"].get("head"))[:12]}`) non '
                     f'coincidono con il head promosso (`{head[:12]}`)')
    ci = content['verifications']['ci']
    if ci is not None and ci.get('state') not in ('pass', 'none'):
        found.append(f'il registro riporta la CI del head approvato `{head[:12]}` come '
                     f'`{ci.get("state")}`, ma la promozione la richiede passata o assente')
    request = content['request']
    if request is not None and request.get('issue') not in (None, content['issue']):
        found.append(f'lo snapshot della richiesta riguarda l\'issue #{request.get("issue")}, il '
                     f'marker della PR l\'issue #{content["issue"]}')
    for turn in content['turns']:
        if turn['on'] != 'pr':
            continue
        if turn['kind'] in ('fix', 'sync') and turn['agent'] != implementer:
            found.append(f'un turno `{turn["kind"]}` del {turn["started_at"]} è di '
                         f'`{turn["agent"]}`, ma il marker della PR indica come implementer '
                         f'`{implementer}`')
        elif turn['kind'] == 'review' and turn['agent'] == implementer:
            found.append(f'un turno di review del {turn["started_at"]} è dell\'implementer '
                         f'`{implementer}`')
    return found


def digest(content):
    data = json.dumps(content, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def record(content):
    """The ``receipt`` marker: the identity of the receipt, its totals and its missing data."""
    policy = content['policy']
    return {'pr': content['pr'], 'issue': content['issue'], 'head': content['head'],
            'base': policy['base'], 'policy': policy['policy'], 'level': policy['level'],
            'promotion': content['promotion'], 'digest': digest(content),
            'totals': content['totals'], 'missing': content['missing'], 'state': 'current'}


# Rendering -----------------------------------------------------------------
def _v(value):
    """A value as the receipt shows it: ``non riportato`` when it was not attested."""
    if value is None:
        return NOT_REPORTED
    if isinstance(value, list):
        return ', '.join(f'`{v}`' for v in value) if value else NOT_REPORTED
    return f'`{value}`'


def _cell(value):
    return ' '.join(str(value).split()).replace('|', '\\|')


def _literal(value):
    """``value``, data recorded in a marker but written by an agent, as an inert code span for
    a table cell: one line, a fence longer than any run of backticks it holds, ``|`` escaped
    (GFM tables unescape it inside code spans). No Markdown, link, mention or HTML of it
    renders; ``—`` when empty."""
    text = ' '.join(str(value).split())
    if not text:
        return '—'
    longest = run = 0
    for char in text:
        run = run + 1 if char == '`' else 0
        longest = max(longest, run)
    fence = '`' * (longest + 1)
    pad = ' ' if text[0] == '`' or text[-1] == '`' else ''
    return f'{fence}{pad}{text}{pad}{fence}'.replace('|', '\\|')


def _sha(value):
    return f'`{value[:12]}`' if isinstance(value, str) and value else NOT_REPORTED


def _cost(value):
    return NOT_REPORTED if value is None else f'`{value:.4f}`'


def _request(request):
    if request is None:
        return ['Non registrato: l\'issue è stata presa in carico prima di #133.']
    return [f'- **Issue:** #{request.get("issue")}',
            f'- **Hash di titolo e corpo (SHA-256):** {_v(request.get("hash"))}',
            f'- **Label `agent:ready` messa da:** {_v(request.get("labeled_by"))}, il '
            f'{_v(request.get("labeled_at"))}',
            f'- **Commenti fidati dati all\'implementer:** {_v(request.get("comments"))}',
            f'- **Presa in carico:** {_v(request.get("taken_at"))}']


def _totals(t):
    tokens = t['tokens']
    return [f'- **Round:** `{t["rounds"]}` (contano per il limite: `{t["counted_rounds"]}`)',
            f'- **Turni:** `{t["turns"]}`',
            f'- **Durata sommata dei turni (ms):** {_v(t["turns_ms"])}',
            '- **Token noti:** input ' + _v(tokens['input_tokens']) + ', output '
            + _v(tokens['output_tokens']) + ', cache in lettura '
            + _v(tokens['cache_read_input_tokens']) + ', cache in creazione '
            + _v(tokens['cache_creation_input_tokens']) + ', ragionamento (contato a parte) '
            + _v(tokens['thinking_tokens']),
            f'- **Costo noto (USD):** {_cost(t["cost_usd"])}',
            f'- **Turni con costo non riportato:** `{t["cost_unreported"]}`; con token non '
            f'riportati: `{t["tokens_unreported"]}`',
            '', 'Durata delle verifiche, separata da quella dei turni:', '',
            f'- **Test locali (ms):** {_v(t["verifications_ms"])}',
            f'- **Check della CI sul head approvato (s):** {_v(t["ci_s"])}']


def _turns(turns):
    if not turns:
        return ['Nessun turno registrato.']
    lines = ['| # | Dove | Passo | Agent | Modello configurato | Modello reale | Inizio | Esito '
             '| Durata (ms) | Token in/out | Costo (USD) |',
             '|---|---|---|---|---|---|---|---|---|---|---|']
    for i, t in enumerate(turns, 1):
        model = _v(t['model']) + (f' (fallback di {_v(t["fallback_for"])})'
                                  if t.get('fallback_for') else '')
        outcome = t['outcome'] or (t['state'] if t['state'] != 'closed' else None)
        tel = t['telemetry']
        lines.append(f'| {i} | {t["on"]} | {_v(t["kind"])} | {_v(t["agent"])} | '
                     f'{_cell(model)} | {_cell(_v(t["model_real"]))} | {_v(t["started_at"])} | '
                     f'{_v(outcome)} | {_v(t["duration_ms"])} | '
                     f'{_v(tel["input_tokens"])} / {_v(tel["output_tokens"])} | '
                     f'{_cost(tel["cost_usd"])} |')
    return lines


def _events(events):
    if not events:
        return ['Nessun evento registrato.']
    lines = ['| Evento | Head | Stato | Round | Base | Head precedente | Quando |',
             '|---|---|---|---|---|---|---|']
    for e in events:
        lines.append(f'| {_v(e.get("event"))} | {_sha(e.get("head"))} | {_v(e.get("state"))} | '
                     f'{_v(e.get("round"))} | {_sha(e.get("base"))} | '
                     f'{_sha(e.get("previous_head"))} | {_v(e.get("at"))} |')
    return lines


def _verifications(content):
    v, head = content['verifications'], content['head']
    lines = [f'**Test locali sul head approvato** (`{head[:12]}`):', '']
    if v['head']:
        lines += [f'- {_v(x["kind"])}, tentativo {_v(x["attempt"])}: {_v(x["outcome"])}, '
                  f'{_v(x["duration_ms"])} ms, il {_v(x["at"])}' for x in v['head']]
    else:
        lines.append('Nessuna verifica locale legata a questo commit (le verifiche si legano '
                     'al commit creato subito dopo il test, se l\'albero è lo stesso).')
    lines += ['', f'Verifiche locali registrate in tutto: `{len(v["local"])}`.', '',
              '**CI sul head approvato:**', '']
    ci = v['ci']
    if ci is None:
        lines.append(f'Esito {NOT_REPORTED}: nessun evento `ci_round` registrato per questo '
                     'commit e questo round.')
        return lines
    lines.append(f'Stato {_v(ci.get("state"))}, round {_v(ci.get("round"))}, registrato il '
                 f'{_v(ci.get("at"))}.')
    if ci.get('checks'):
        lines += ['', '| Check | Esito | Durata (s) |', '|---|---|---|']
        lines += [f'| {_cell(c.get("name"))} | {_v(c.get("outcome"))} | '
                  f'{_v(c.get("duration_s"))} |' for c in ci['checks']]
    return lines


def _rounds_text(rounds, approved):
    if not rounds:
        return ['Nessun round di review (livello 0).']
    lines = ['| Round | Head | Reviewer | Verdetto |', '|---|---|---|---|']
    for r in rounds:
        for v in r['verdicts']:
            mark = ' (approvato, promosso)' if r['round'] == approved else ''
            lines.append(f'| {r["round"]}{mark} | {_sha(r["head"])} | {_v(v["reviewer"])} | '
                         f'{_VERDICT.get(v["verdict"], _cell(v["verdict"]))} |')
    return lines


def _findings_text(findings):
    if not findings:
        return ['Nessun finding bloccante.']
    lines = ['| Finding | Stato | Chiuso da |', '|---|---|---|']
    for f in findings:
        if f['status'] == 'open':
            state, who = 'aperto', '—'
        else:
            state = _CLOSED.get(f['status'], 'chiuso')
            if f['by'] is None:
                who = f'{NOT_REPORTED} (marker precedenti a #110)'
            elif f['by'] in (CI, OPERATOR):
                who = f'`{f["by"]}`' + (f' (`{f["author"]}`)' if f.get('author') else '')
            else:
                who = f'`{f["by"]}`, nel round {f["round"]}'
        lines.append(f'| `{f["key"]}` | {state} | {who} |')
    return lines


def _acceptance_text(acceptance):
    criteria, entries = acceptance['criteria'], acceptance['map']
    if entries is None:
        return [f'Mappa {NOT_REPORTED}: la PR non registra una mappa di copertura.']
    if not entries:
        return ['Mappa vuota: nessun criterio di accettazione riconoscibile nell\'issue.']
    lines = ['Criteri come li riporta il testo dell\'issue; stato, test e motivo come li '
             'registra l\'ultima mappa. Test e motivo sono dichiarati dall\'agent che ha scritto '
             'la mappa: sono riportati come codice, senza interpretarli.', '',
             '| # | Criterio | Stato | Test dichiarati | Motivo dichiarato |', '|---|---|---|---|---|']
    for i, e in enumerate(entries, 1):
        text = (_cell(criteria[i - 1]) if criteria is not None and i <= len(criteria)
                else NOT_REPORTED)
        tests = '<br>'.join(f'{_literal(t["file"])}: {_literal(t["name"])}'
                            for t in e['tests']) or '—'
        lines.append(f'| {i} | {text} | {_MAP_STATUS.get(e["status"], _cell(e["status"]))} | '
                     f'{tests} | {_literal(e["reason"])} |')
    return lines


def render(content, marker=None):
    """The comment of a receipt on its own: the dispatcher's header, every section, the
    ``receipt`` marker. A promotion carries it in its own comment instead (:func:`section`,
    #338); this form is the one of the receipts written before, and of a promotion too long for
    one comment."""
    return publishable(dispatcher_header() + '\n' + section(content), 'receipt',
                       marker or record(content))


def section(content):
    """The readable text of a receipt, without header nor marker: its title and every
    section."""
    head, promotion = content['head'], content['promotion']
    parts = [f'## Ricevuta della PR #{content["pr"]}', '',
             f'Promozione n. {promotion} in `agent:ready-to-merge`, legata al commit approvato '
             f'{_sha(head)}. Il contenuto viene solo dai record del dispatcher e dai marker, mai '
             'da testo scritto dagli agent; un valore non attestato è `non riportato`, mai 0.']
    if content['missing']:
        parts += ['', '> **Dati storici mancanti**, dichiarati e non ricostruiti:', '>']
        parts += [f'> - {MISSING_TEXT[key]}' for key in content['missing']]
    parts += ['', '### Richiesta', '', *_request(content['request']),
              '', '### Totali', '', *_totals(content['totals']),
              '', '### Turni', '', *_turns(content['turns']),
              '', '### Eventi della CI e di sync', '', *_events(content['events']),
              '', '### Verifiche sul head approvato', '', *_verifications(content),
              '', '### Round e verdetti', '',
              *_rounds_text(content['rounds'], content['approved_round']),
              '', '### Finding', '', *_findings_text(content['findings']),
              '', '### Policy', '', f'Livello **{content["policy"]["level"]}**.', '',
              describe_policy(content['policy']),
              '', '### Criteri di accettazione', '', *_acceptance_text(content['acceptance'])]
    if content.get('advisory') is not None:
        parts += ['', '### Review consultiva', '', *_advisory_text(content['advisory'])]
    if content.get('budget') is not None:
        parts += ['', '### Budget della storia', '', *_budget_text(content['budget'])]
    return '\n'.join(parts)


def _budget_text(found):
    """The state of the budget of the story (#393, ``budget.view``): consumed, reserved and
    available per dimension; a value no turn attests is ``non attestato``, never 0."""
    if found.get('story') is None:
        return ['L\'issue non appartiene a nessuna storia del journal: nessun envelope di '
                'budget.']
    lines = [f'Storia `{found["story"]}` (radice #{found["issue"]}), stato del journal al '
             f'{found["at"]}. Consumato: i turni con un esito confermato e i dollari osservati; '
             'prenotato: i turni senza esito confermato e le riserve non coperte da un costo '
             f'osservato. Un valore che nessun turno attesta è `{NOT_ATTESTED}`, mai 0.', '',
             '| Dimensione | Consumato | Prenotato | Disponibile | Tetto |',
             '|---|---|---|---|---|']

    def count(label, c):
        cap = '—' if c['cap'] is None else c['cap']
        available = '—' if c['available'] is None else c['available']
        return f'| {label} | {c["consumed"]} | {c["reserved"]} | {available} | {cap} |'

    lines.append(count('Turni', found['turns']))
    lines += [count(f'Turni `{name}`', c) for name, c in found['roles'].items()]
    lines += [count(f'Fase `{name}`', c) for name, c in found['phases'].items()]
    for kind, d in found['usd'].items():
        gap = (f' (turni senza valore attestato: {d["unattested"]})' if d['unattested']
               else '')
        lines.append(f'| `{kind}` | {consumed_usd(d)}{gap} | {usd(d["reserved"])} | '
                     f'{usd(d["available"])} | {usd(d["cap"])} |')
    t = found['time']
    lines.append(f'| Tempo (s) | {t["consumed"]} | — | {t["available"]} | {t["cap"]} |')
    lines += ['', f'Inizio della storia {t["started_at"]}, scadenza {t["deadline_at"]}.']
    return lines


def _advisory_text(advisory):
    """The advisory review of the head (#369): who wrote it and its opinion, never its text."""
    who, head = _v(advisory['reviewer']), _sha(advisory['head'])
    if advisory['outcome'] != 'done':
        return [f'La review consultiva di {who} sul commit {head} non è riuscita.']
    return [f'Parere non vincolante di {who} sul commit {head}: **{advisory["opinion"]}**. '
            'Non conta per il quorum né per il limite dei round, non apre finding e non '
            'cambia label né promozione: il testo è nella review GitHub di tipo `COMMENT` '
            '"Review consultiva: non conta per il quorum".']


def with_advisory(body, content):
    """``body``, the comment of the current receipt of ``content``'s head, with its receipt
    rewritten from ``content``, which carries the advisory review of that head (#369): the
    section and the ``receipt`` record, with digest and totals recomputed, while promotion,
    policy and state stay. The text before the receipt's title and the other records of the
    comment stay as they are. None when the comment has no such title."""
    lines = split_markers(body)[0].splitlines()
    title = f'## Ricevuta della PR #{content["pr"]}'
    start = next((i for i, line in enumerate(lines) if line.strip() == title), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith('## ')),
               len(lines))
    text = '\n'.join(lines[:start] + section(content).splitlines()
                     + [''] * (end < len(lines)) + lines[end:])
    return with_record(body, 'receipt', record(content), text=text)


def superseded(body, marker, why, at):
    """``body``, the comment of a current receipt with ``marker``, marked as superseded at
    ``at`` because ``why``: a warning before its first title, and the marker with ``state:
    superseded``. The other records of the comment, such as the ``policy`` record of the
    promotion that carries the receipt (#338), stay as they are."""
    lines = split_markers(body)[0].splitlines()
    title = next((i for i, line in enumerate(lines) if line.startswith('## ')), len(lines))
    warning = ['> [!WARNING]',
               f'> **Ricevuta superata** il {at}: {" ".join(why.split())}. Non vale più per il '
               'merge; una nuova promozione scrive una nuova ricevuta.', '']
    text = '\n'.join(lines[:title] + warning + lines[title:])
    return with_record(body, 'receipt', dict(marker, state='superseded', superseded_at=at,
                                             reason=' '.join(why.split())), text=text)
