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
that was not attested is ``None`` in the data and ``not reported`` in the text (``non
riportato`` in Italian), never 0. With the budget on (#393), a section of its own shows the
state of the budget of the issue's story as the dispatcher's journal records it
(``budget.view``), its dollars not attested as ``not attested``; with the budget off there is
no such section, and the content is the one before #393. The readable text is in the
project's ``language`` (#634, ``TEXTS``); the content, its digest and the records are the same
in every language.

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
from .budget import TEXTS as BUDGET_TEXTS
from .i18n import ENGLISH, Catalog
from .markdown import dispatcher_header, publishable, split_markers, with_record
from .planning import counted_rounds
from .policy import describe as describe_policy
from .review import (AUTHORITIES, CI, CLOSING, FINDINGS_ONLY, finding_key, finding_state,
                     findings_only, owner_of)

# The class of the escalation of a receipt that contradicts the markers: critical.
CONFLICT = 'receipt_conflict'
STATES = ('current', 'superseded')
TOKENS = ('input_tokens', 'output_tokens', 'cache_read_input_tokens',
          'cache_creation_input_tokens', 'thinking_tokens')
# The turn fields a receipt keeps: no error text, which may quote what an agent wrote.
TURN_FIELDS = ('kind', 'agent', 'identity', 'model', 'effort', 'fallback_for', 'model_real',
               'state', 'started_at', 'duration_ms', 'outcome')
# Data a PR started before #133 or #134 may lack: declared, never reconstructed. Each key has
# its text in ``TEXTS``, as ``missing_<key>``.
MISSING = ('request', 'turns', 'implement', 'ci', 'acceptance_map', 'criteria')
# What a receipt keeps of the advisory review of its head (#369).
ADVISORY_FIELDS = ('head', 'reviewer', 'outcome', 'opinion')

# The texts of a receipt (#634), in the project's ``language`` (``i18n``); English for a
# language without translation. The content, its digest and the ``receipt`` record never
# change with the language: only the readable text does. The title (``title``) is recognised in
# every language (``with_advisory``), so that a receipt published before a change of
# ``language`` still takes the advisory review of its head.
TEXTS = Catalog(
    en={
        'not_reported': 'not reported',
        'missing_request': 'snapshot of the request (issue taken over before #133)',
        'missing_turns': 'turns of the agents (no ledger record: work started before #133)',
        'missing_implement': 'turn of the implementation (no record on the issue)',
        'missing_ci': 'outcome of the CI on the approved head in the ledger',
        'missing_acceptance_map': 'coverage map of the criteria (PR opened before #134)',
        'missing_criteria': 'acceptance criteria (issue text not verifiable)',
        'verdict_approve': 'approved',
        'verdict_request_changes': 'changes requested',
        'verdict_needs_operator': 'decision of the operator',
        'verdict_unproven': 'no blocking finding with evidence',
        'closed_resolved': 'resolved',
        'closed_withdrawn': 'withdrawn',
        'closed': 'closed',
        'open': 'open',
        'map_covered': 'covered',
        'map_not_covered': 'not covered',
        # Why a receipt contradicts the markers: each blocks the promotion.
        'conflict_head': ('the approved commit (`{approved}`) and the head of the policy '
                          'receipt (`{policy}`) do not match the promoted head (`{head}`)'),
        'conflict_ci': ('the ledger reports the CI of the approved head `{head}` as `{state}`, '
                        'but the promotion requires it passed or absent'),
        'conflict_request': ('the snapshot of the request concerns issue #{request}, the marker '
                             'of the PR issue #{issue}'),
        'conflict_author': ('a `{kind}` turn of {at} is by `{agent}`, but the marker of the PR '
                            'names `{implementer}` as implementer'),
        'conflict_review': 'a review turn of {at} is by the implementer `{implementer}`',
        'title': '## Receipt of PR #{pr}',
        'intro': ('Promotion no. {promotion} to `agent:ready-to-merge`, bound to the approved '
                  'commit {head}. The content comes only from the dispatcher\'s records and from '
                  'the markers, never from text written by the agents; a value not attested is '
                  '`{not_reported}`, never 0.'),
        'missing': '> **Missing historical data**, declared and not reconstructed:',
        'request': '### Request',
        'totals': '### Totals',
        'turns': '### Turns',
        'events': '### CI and sync events',
        'verifications': '### Verifications on the approved head',
        'rounds': '### Rounds and verdicts',
        'findings': '### Findings',
        'policy': '### Policy',
        'policy_level': 'Level **{level}**.',
        'criteria': '### Acceptance criteria',
        'advisory': '### Advisory review',
        'budget': '### Budget of the story',
        'request_none': 'Not recorded: the issue was taken over before #133.',
        'request_issue': '- **Issue:** #{issue}',
        'request_hash': '- **Hash of title and body (SHA-256):** {hash}',
        'request_label': '- **Label `agent:ready` added by:** {by}, at {at}',
        'request_comments': '- **Trusted comments given to the implementer:** {comments}',
        'request_taken': '- **Taken over:** {at}',
        'totals_rounds': '- **Rounds:** `{rounds}` (counted for the limit: `{counted}`)',
        'totals_turns': '- **Turns:** `{turns}`',
        'totals_turns_ms': '- **Summed duration of the turns (ms):** {ms}',
        'totals_tokens': ('- **Known tokens:** input {input}, output {output}, cache read '
                          '{cache_read}, cache creation {cache_creation}, reasoning (counted '
                          'apart) {thinking}'),
        'totals_cost': '- **Known cost (USD):** {cost}',
        'totals_unreported': ('- **Turns with cost not reported:** `{cost}`; with tokens not '
                              'reported: `{tokens}`'),
        'totals_apart': 'Duration of the verifications, apart from that of the turns:',
        'totals_local': '- **Local tests (ms):** {ms}',
        'totals_ci': '- **CI checks on the approved head (s):** {s}',
        'turns_none': 'No turn recorded.',
        'turns_header': ('| # | Where | Step | Agent | Configured model | Real model | Start | '
                         'Outcome | Duration (ms) | Tokens in/out | Cost (USD) |'),
        'fallback': ' (fallback of {model})',
        'events_none': 'No event recorded.',
        'events_header': '| Event | Head | State | Round | Base | Previous head | When |',
        'local_head': '**Local tests on the approved head** (`{head}`):',
        'local_line': '- {kind}, attempt {attempt}: {outcome}, {ms} ms, at {at}',
        'local_none': ('No local verification bound to this commit (verifications are bound to '
                       'the commit created right after the test, if the tree is the same).'),
        'local_all': 'Local verifications recorded in all: `{n}`.',
        'ci_head': '**CI on the approved head:**',
        'ci_none': ('Outcome {not_reported}: no `ci_round` event recorded for this commit and '
                    'this round.'),
        'ci_state': 'State {state}, round {round}, recorded at {at}.',
        'ci_header': '| Check | Outcome | Duration (s) |',
        'rounds_none': 'No review round (level 0).',
        'rounds_header': '| Round | Head | Reviewer | Verdict |',
        'rounds_approved': ' (approved, promoted)',
        'findings_none': 'No blocking finding.',
        'findings_header': '| Finding | State | Closed by |',
        'findings_legacy': '{not_reported} (markers before #110)',
        'findings_round': '`{by}`, in round {round}',
        'map_none': 'Map {not_reported}: the PR records no coverage map.',
        'map_empty': 'Empty map: no recognisable acceptance criterion in the issue.',
        'map_intro': ('Criteria as the issue text states them; state, tests and reason as the '
                      'latest map records them. Tests and reason are declared by the agent that '
                      'wrote the map: they are shown as code, without interpreting them.'),
        'map_header': ('| # | Criterion | State | Declared tests | Declared reason |'),
        'budget_none': 'The issue belongs to no story of the journal: no budget envelope.',
        'budget_intro': ('Story `{story}` (root #{issue}), state of the journal at {at}. '
                         'Consumed: the turns with a confirmed outcome and the dollars observed; '
                         'reserved: the turns without a confirmed outcome and the reserves not '
                         'covered by an observed cost. A value no turn attests is '
                         '`{not_attested}`, never 0.'),
        'budget_header': '| Dimension | Consumed | Reserved | Available | Cap |',
        'budget_turns': 'Turns',
        'budget_turns_of': 'Turns `{name}`',
        'budget_phase': 'Phase `{name}`',
        'budget_gap': ' (turns without an attested value: {n})',
        'budget_time': 'Time (s)',
        'budget_deadline': 'Start of the story {started}, deadline {deadline}.',
        'advisory_failed': 'The advisory review of {who} on commit {head} did not succeed.',
        'advisory_done': ('Non-binding opinion of {who} on commit {head}: **{opinion}**. It '
                          'counts neither for the quorum nor for the round limit, opens no '
                          'finding and changes neither labels nor promotion: the text is in '
                          'the GitHub review of type `COMMENT` "{title}".'),
        'superseded': ('> **Receipt superseded** at {at}: {why}. It no longer holds for the '
                       'merge; a new promotion writes a new receipt.'),
    },
    it={
        'not_reported': 'non riportato',
        'missing_request': 'snapshot della richiesta (issue presa in carico prima di #133)',
        'missing_turns': ('turni degli agent (nessun record del registro: lavoro avviato prima '
                          'di #133)'),
        'missing_implement': 'turno dell\'implementazione (nessun record sull\'issue)',
        'missing_ci': 'esito della CI sul head approvato nel registro',
        'missing_acceptance_map': 'mappa di copertura dei criteri (PR aperta prima di #134)',
        'missing_criteria': 'criteri di accettazione (testo dell\'issue non verificabile)',
        'verdict_approve': 'approvata',
        'verdict_request_changes': 'modifiche richieste',
        'verdict_needs_operator': 'decisione dell\'operatore',
        'verdict_unproven': 'nessun finding bloccante con evidenza',
        'closed_resolved': 'risolto',
        'closed_withdrawn': 'ritirato',
        'closed': 'chiuso',
        'open': 'aperto',
        'map_covered': 'coperto',
        'map_not_covered': 'non coperto',
        'conflict_head': ('il commit approvato (`{approved}`) e il head della ricevuta di policy '
                          '(`{policy}`) non coincidono con il head promosso (`{head}`)'),
        'conflict_ci': ('il registro riporta la CI del head approvato `{head}` come `{state}`, '
                        'ma la promozione la richiede passata o assente'),
        'conflict_request': ('lo snapshot della richiesta riguarda l\'issue #{request}, il '
                             'marker della PR l\'issue #{issue}'),
        'conflict_author': ('un turno `{kind}` del {at} è di `{agent}`, ma il marker della PR '
                            'indica come implementer `{implementer}`'),
        'conflict_review': 'un turno di review del {at} è dell\'implementer `{implementer}`',
        'title': '## Ricevuta della PR #{pr}',
        'intro': ('Promozione n. {promotion} in `agent:ready-to-merge`, legata al commit '
                  'approvato {head}. Il contenuto viene solo dai record del dispatcher e dai '
                  'marker, mai da testo scritto dagli agent; un valore non attestato è '
                  '`{not_reported}`, mai 0.'),
        'missing': '> **Dati storici mancanti**, dichiarati e non ricostruiti:',
        'request': '### Richiesta',
        'totals': '### Totali',
        'turns': '### Turni',
        'events': '### Eventi della CI e di sync',
        'verifications': '### Verifiche sul head approvato',
        'rounds': '### Round e verdetti',
        'findings': '### Finding',
        'policy': '### Policy',
        'policy_level': 'Livello **{level}**.',
        'criteria': '### Criteri di accettazione',
        'advisory': '### Review consultiva',
        'budget': '### Budget della storia',
        'request_none': 'Non registrato: l\'issue è stata presa in carico prima di #133.',
        'request_issue': '- **Issue:** #{issue}',
        'request_hash': '- **Hash di titolo e corpo (SHA-256):** {hash}',
        'request_label': '- **Label `agent:ready` messa da:** {by}, il {at}',
        'request_comments': '- **Commenti fidati dati all\'implementer:** {comments}',
        'request_taken': '- **Presa in carico:** {at}',
        'totals_rounds': '- **Round:** `{rounds}` (contano per il limite: `{counted}`)',
        'totals_turns': '- **Turni:** `{turns}`',
        'totals_turns_ms': '- **Durata sommata dei turni (ms):** {ms}',
        'totals_tokens': ('- **Token noti:** input {input}, output {output}, cache in lettura '
                          '{cache_read}, cache in creazione {cache_creation}, ragionamento '
                          '(contato a parte) {thinking}'),
        'totals_cost': '- **Costo noto (USD):** {cost}',
        'totals_unreported': ('- **Turni con costo non riportato:** `{cost}`; con token non '
                              'riportati: `{tokens}`'),
        'totals_apart': 'Durata delle verifiche, separata da quella dei turni:',
        'totals_local': '- **Test locali (ms):** {ms}',
        'totals_ci': '- **Check della CI sul head approvato (s):** {s}',
        'turns_none': 'Nessun turno registrato.',
        'turns_header': ('| # | Dove | Passo | Agent | Modello configurato | Modello reale | '
                         'Inizio | Esito | Durata (ms) | Token in/out | Costo (USD) |'),
        'fallback': ' (fallback di {model})',
        'events_none': 'Nessun evento registrato.',
        'events_header': '| Evento | Head | Stato | Round | Base | Head precedente | Quando |',
        'local_head': '**Test locali sul head approvato** (`{head}`):',
        'local_line': '- {kind}, tentativo {attempt}: {outcome}, {ms} ms, il {at}',
        'local_none': ('Nessuna verifica locale legata a questo commit (le verifiche si legano '
                       'al commit creato subito dopo il test, se l\'albero è lo stesso).'),
        'local_all': 'Verifiche locali registrate in tutto: `{n}`.',
        'ci_head': '**CI sul head approvato:**',
        'ci_none': ('Esito {not_reported}: nessun evento `ci_round` registrato per questo '
                    'commit e questo round.'),
        'ci_state': 'Stato {state}, round {round}, registrato il {at}.',
        'ci_header': '| Check | Esito | Durata (s) |',
        'rounds_none': 'Nessun round di review (livello 0).',
        'rounds_header': '| Round | Head | Reviewer | Verdetto |',
        'rounds_approved': ' (approvato, promosso)',
        'findings_none': 'Nessun finding bloccante.',
        'findings_header': '| Finding | Stato | Chiuso da |',
        'findings_legacy': '{not_reported} (marker precedenti a #110)',
        'findings_round': '`{by}`, nel round {round}',
        'map_none': 'Mappa {not_reported}: la PR non registra una mappa di copertura.',
        'map_empty': 'Mappa vuota: nessun criterio di accettazione riconoscibile nell\'issue.',
        'map_intro': ('Criteri come li riporta il testo dell\'issue; stato, test e motivo come li '
                      'registra l\'ultima mappa. Test e motivo sono dichiarati dall\'agent che ha '
                      'scritto la mappa: sono riportati come codice, senza interpretarli.'),
        'map_header': '| # | Criterio | Stato | Test dichiarati | Motivo dichiarato |',
        'budget_none': ('L\'issue non appartiene a nessuna storia del journal: nessun envelope '
                        'di budget.'),
        'budget_intro': ('Storia `{story}` (radice #{issue}), stato del journal al {at}. '
                         'Consumato: i turni con un esito confermato e i dollari osservati; '
                         'prenotato: i turni senza esito confermato e le riserve non coperte da '
                         'un costo osservato. Un valore che nessun turno attesta è '
                         '`{not_attested}`, mai 0.'),
        'budget_header': '| Dimensione | Consumato | Prenotato | Disponibile | Tetto |',
        'budget_turns': 'Turni',
        'budget_turns_of': 'Turni `{name}`',
        'budget_phase': 'Fase `{name}`',
        'budget_gap': ' (turni senza valore attestato: {n})',
        'budget_time': 'Tempo (s)',
        'budget_deadline': 'Inizio della storia {started}, scadenza {deadline}.',
        'advisory_failed': 'La review consultiva di {who} sul commit {head} non è riuscita.',
        'advisory_done': ('Parere non vincolante di {who} sul commit {head}: **{opinion}**. Non '
                          'conta per il quorum né per il limite dei round, non apre finding e '
                          'non cambia label né promozione: il testo è nella review GitHub di '
                          'tipo `COMMENT` "{title}".'),
        'superseded': ('> **Ricevuta superata** il {at}: {why}. Non vale più per il merge; una '
                       'nuova promozione scrive una nuova ricevuta.'),
    })


def missing_text(key, language=ENGLISH):
    """The text of ``key`` of ``MISSING``, a datum the receipt lacks, in ``language``."""
    return TEXTS.texts(language)(f'missing_{key}')


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
    if record.get('role') == FINDINGS_ONLY:
        # The turn of a findings-only review (#416); without one the content is the one before.
        turn['role'] = FINDINGS_ONLY
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
        verdict = {'reviewer': owner_of(review), 'verdict': review['verdict']}
        if findings_only(review):
            # #416: recorded as finding-only; without one the content is the one before.
            verdict['role'] = FINDINGS_ONLY
        entry['verdicts'].append(verdict)
    return [rounds[n] for n in sorted(rounds)]


def _findings(reviews, closures):
    """Every blocking finding ever opened, ``{key, status, by, author, round}``: ``open``, or
    how it was closed and by whom (its owner in a later round, ``ci``, ``operator`` or
    ``coordinator``, #606, with the author). A finding closed under the rules before #110 has no recorded closer (None)."""
    owners, order, closed, quorum = {}, [], {}, set()
    for review in reviews:
        owner = owner_of(review)
        for p in review['previous_findings']:
            key = p.get('key')
            if (key and p.get('status') in CLOSING and owners.get(key) == owner
                    and key not in closed and key not in quorum):
                closed[key] = {'status': p['status'], 'by': owner, 'author': None,
                               'round': review['round']}
        for f in review['blocking_findings']:
            key = f.get('key') or finding_key(review['round'], owner, f['id'])
            if key not in owners:
                owners[key] = owner
                order.append(key)
                if findings_only(review):
                    quorum.add(key)
    state = finding_state(reviews, closures)
    # A findings-only finding (#416) is closed by the quorum of a round, never by its owner.
    for key, closure in state.quorum_closed.items():
        closed[key] = dict(closure, author=None)
    for c in closures:
        key = c['key']
        if key in owners and key not in closed and (c['by'] in AUTHORITIES or owners[key] == CI):
            closed[key] = {'status': c['status'], 'by': c['by'], 'author': c.get('author'),
                           'round': None}
    still_open = {f['key'] for f in state.open}
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


def totals(turns, verifications, ci, reviews, waiver=None):
    """The totals of a receipt: rounds (all and those counted against the limit, with the
    ``waiver`` of ``planning.waiver`` for the rounds after a ``/devloop fix``, #536), turns, the
    summed duration of the turns, the known tokens and cost, how many turns reported no cost
    and no tokens; the duration of the verifications apart: the local tests and the checks of
    the CI on the approved head."""
    telemetry = [t['telemetry'] for t in turns]
    return {
        'rounds': len({r['round'] for r in reviews}),
        'counted_rounds': counted_rounds(reviews, **(waiver or {})),
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
          closures, approved_round, criteria, acceptance_map, advisory=None, budget=None,
          waiver=None):
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
    None with the budget off, and then the content has no ``budget`` key. ``waiver``: the
    arguments of ``planning.waiver``, for the rounds after a ``/devloop fix`` that do not count
    (#536)."""
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
        'totals': totals(turns, verifications, ci, reviews, waiver),
    }
    if advisory is not None:
        # Only the identity and the opinion, never the text the agent wrote (#369).
        content['advisory'] = {k: advisory.get(k) for k in ADVISORY_FIELDS}
    if budget is not None:
        content['budget'] = budget
    content['missing'] = missing(content)
    return content


def missing(content):
    """The data the receipt lacks and declares, as keys of ``MISSING``: never a reason to
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


def contradictions(content, *, implementer, approved_head, language=ENGLISH):
    """Where the receipt and the markers disagree, as sentences in ``language`` (#634): each
    blocks the promotion. Missing data never counts here (:func:`missing`).

    - the head of the policy receipt is not the approved head (the latest round's, or the
      level-0 one);
    - the ledger records, for the approved head and round, a CI other than ``pass`` or ``none``
      as its latest state, while the promotion relies on a CI passed or absent;
    - the request snapshot names another issue than the PR marker;
    - a fix or sync turn on the PR was run by another agent than the PR's implementer, or a
      review turn by the implementer itself."""
    t = TEXTS.texts(language)
    found = []
    head = content['head']
    if approved_head != head or content['policy'].get('head') != head:
        found.append(t('conflict_head', approved=str(approved_head)[:12],
                       policy=str(content['policy'].get('head'))[:12], head=head[:12]))
    ci = content['verifications']['ci']
    if ci is not None and ci.get('state') not in ('pass', 'none'):
        found.append(t('conflict_ci', head=head[:12], state=ci.get('state')))
    request = content['request']
    if request is not None and request.get('issue') not in (None, content['issue']):
        found.append(t('conflict_request', request=request.get('issue'),
                       issue=content['issue']))
    for turn in content['turns']:
        if turn['on'] != 'pr':
            continue
        if turn['kind'] in ('fix', 'sync') and turn['agent'] != implementer:
            found.append(t('conflict_author', kind=turn['kind'], at=turn['started_at'],
                           agent=turn['agent'], implementer=implementer))
        elif turn['kind'] == 'review' and turn['agent'] == implementer:
            found.append(t('conflict_review', at=turn['started_at'], implementer=implementer))
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
def _v(t, value):
    """A value as the receipt shows it: ``not_reported`` of ``t`` when it was not attested."""
    if value is None:
        return t('not_reported')
    if isinstance(value, list):
        return ', '.join(f'`{v}`' for v in value) if value else t('not_reported')
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


def _sha(t, value):
    return f'`{value[:12]}`' if isinstance(value, str) and value else t('not_reported')


def _cost(t, value):
    return t('not_reported') if value is None else f'`{value:.4f}`'


def _known(t, prefix, value, default=None):
    """The text of ``<prefix>_<value>`` in ``t``; when the catalogue does not know it,
    ``default`` or the value as a table cell."""
    key = f'{prefix}_{value}'
    if key in TEXTS.tables[ENGLISH]:
        return t(key)
    return _cell(value) if default is None else default


def _request(t, request):
    if request is None:
        return [t('request_none')]
    return [t('request_issue', issue=request.get('issue')),
            t('request_hash', hash=_v(t, request.get('hash'))),
            t('request_label', by=_v(t, request.get('labeled_by')),
              at=_v(t, request.get('labeled_at'))),
            t('request_comments', comments=_v(t, request.get('comments'))),
            t('request_taken', at=_v(t, request.get('taken_at')))]


def _totals(t, totals):
    tokens = totals['tokens']
    return [t('totals_rounds', rounds=totals['rounds'], counted=totals['counted_rounds']),
            t('totals_turns', turns=totals['turns']),
            t('totals_turns_ms', ms=_v(t, totals['turns_ms'])),
            t('totals_tokens', input=_v(t, tokens['input_tokens']),
              output=_v(t, tokens['output_tokens']),
              cache_read=_v(t, tokens['cache_read_input_tokens']),
              cache_creation=_v(t, tokens['cache_creation_input_tokens']),
              thinking=_v(t, tokens['thinking_tokens'])),
            t('totals_cost', cost=_cost(t, totals['cost_usd'])),
            t('totals_unreported', cost=totals['cost_unreported'],
              tokens=totals['tokens_unreported']),
            '', t('totals_apart'), '',
            t('totals_local', ms=_v(t, totals['verifications_ms'])),
            t('totals_ci', s=_v(t, totals['ci_s']))]


def _turns(t, turns):
    if not turns:
        return [t('turns_none')]
    lines = [t('turns_header'), '|---|---|---|---|---|---|---|---|---|---|---|']
    for i, turn in enumerate(turns, 1):
        model = _v(t, turn['model']) + (t('fallback', model=_v(t, turn['fallback_for']))
                                        if turn.get('fallback_for') else '')
        outcome = turn['outcome'] or (turn['state'] if turn['state'] != 'closed' else None)
        tel = turn['telemetry']
        step = _v(t, turn['kind']) + (' (finding-only)' if turn.get('role') == FINDINGS_ONLY
                                      else '')
        lines.append(f'| {i} | {turn["on"]} | {step} | {_v(t, turn["agent"])} | '
                     f'{_cell(model)} | {_cell(_v(t, turn["model_real"]))} | '
                     f'{_v(t, turn["started_at"])} | {_v(t, outcome)} | '
                     f'{_v(t, turn["duration_ms"])} | '
                     f'{_v(t, tel["input_tokens"])} / {_v(t, tel["output_tokens"])} | '
                     f'{_cost(t, tel["cost_usd"])} |')
    return lines


def _events(t, events):
    if not events:
        return [t('events_none')]
    lines = [t('events_header'), '|---|---|---|---|---|---|---|']
    for e in events:
        lines.append(f'| {_v(t, e.get("event"))} | {_sha(t, e.get("head"))} | '
                     f'{_v(t, e.get("state"))} | {_v(t, e.get("round"))} | '
                     f'{_sha(t, e.get("base"))} | {_sha(t, e.get("previous_head"))} | '
                     f'{_v(t, e.get("at"))} |')
    return lines


def _verifications(t, content):
    v, head = content['verifications'], content['head']
    lines = [t('local_head', head=head[:12]), '']
    if v['head']:
        lines += [t('local_line', kind=_v(t, x['kind']), attempt=_v(t, x['attempt']),
                    outcome=_v(t, x['outcome']), ms=_v(t, x['duration_ms']), at=_v(t, x['at']))
                  for x in v['head']]
    else:
        lines.append(t('local_none'))
    lines += ['', t('local_all', n=len(v['local'])), '', t('ci_head'), '']
    ci = v['ci']
    if ci is None:
        lines.append(t('ci_none', not_reported=t('not_reported')))
        return lines
    lines.append(t('ci_state', state=_v(t, ci.get('state')), round=_v(t, ci.get('round')),
                   at=_v(t, ci.get('at'))))
    if ci.get('checks'):
        lines += ['', t('ci_header'), '|---|---|---|']
        lines += [f'| {_cell(c.get("name"))} | {_v(t, c.get("outcome"))} | '
                  f'{_v(t, c.get("duration_s"))} |' for c in ci['checks']]
    return lines


def _rounds_text(rounds, approved, language=ENGLISH):
    t = TEXTS.texts(language)
    if not rounds:
        return [t('rounds_none')]
    lines = [t('rounds_header'), '|---|---|---|---|']
    for r in rounds:
        for v in r['verdicts']:
            mark = t('rounds_approved') if r['round'] == approved else ''
            # #416: a findings-only review, whose verdict does not count for the quorum.
            who = _v(t, v['reviewer']) + (' (finding-only)' if v.get('role') == FINDINGS_ONLY
                                          else '')
            lines.append(f'| {r["round"]}{mark} | {_sha(t, r["head"])} | {who} | '
                         f'{_known(t, "verdict", v["verdict"])} |')
    return lines


def _findings_text(t, findings):
    if not findings:
        return [t('findings_none')]
    lines = [t('findings_header'), '|---|---|---|']
    for f in findings:
        if f['status'] == 'open':
            state, who = t('open'), '—'
        else:
            state = _known(t, 'closed', f['status'], t('closed'))
            if f['by'] is None:
                who = t('findings_legacy', not_reported=t('not_reported'))
            elif f['by'] in (CI, *AUTHORITIES):
                who = f'`{f["by"]}`' + (f' (`{f["author"]}`)' if f.get('author') else '')
            else:
                who = t('findings_round', by=f['by'], round=f['round'])
        lines.append(f'| `{f["key"]}` | {state} | {who} |')
    return lines


def _acceptance_text(t, acceptance):
    criteria, entries = acceptance['criteria'], acceptance['map']
    if entries is None:
        return [t('map_none', not_reported=t('not_reported'))]
    if not entries:
        return [t('map_empty')]
    lines = [t('map_intro'), '', t('map_header'), '|---|---|---|---|---|']
    for i, e in enumerate(entries, 1):
        text = (_cell(criteria[i - 1]) if criteria is not None and i <= len(criteria)
                else t('not_reported'))
        tests = '<br>'.join(f'{_literal(test["file"])}: {_literal(test["name"])}'
                            for test in e['tests']) or '—'
        lines.append(f'| {i} | {text} | {_known(t, "map", e["status"])} | '
                     f'{tests} | {_literal(e["reason"])} |')
    return lines


def render(content, marker=None, language=ENGLISH):
    """The comment of a receipt on its own: the dispatcher's header, every section, the
    ``receipt`` marker. A promotion carries it in its own comment instead (:func:`section`,
    #338); this form is the one of the receipts written before, and of a promotion too long for
    one comment. ``language``: the project's (#634), of the readable text; the record is the
    same in every language."""
    return publishable(dispatcher_header() + '\n' + section(content, language), 'receipt',
                       marker or record(content))


def title(pr, language=ENGLISH):
    """The title of the receipt of PR ``pr`` in ``language`` (#634)."""
    return TEXTS.texts(language)('title', pr=pr)


def titles(pr):
    """The title of the receipt of PR ``pr`` in every language devloop has (#634): what
    recognises a receipt published before a change of ``language``."""
    return tuple(text.format(pr=pr) for text in TEXTS.variants('title'))


def section(content, language=ENGLISH):
    """The readable text of a receipt, without header nor marker: its title and every
    section, in ``language`` (#634)."""
    t = TEXTS.texts(language)
    head, promotion = content['head'], content['promotion']
    parts = [title(content['pr'], language), '',
             t('intro', promotion=promotion, head=_sha(t, head),
               not_reported=t('not_reported'))]
    if content['missing']:
        parts += ['', t('missing'), '>']
        parts += [f'> - {t("missing_" + key)}' for key in content['missing']]
    parts += ['', t('request'), '', *_request(t, content['request']),
              '', t('totals'), '', *_totals(t, content['totals']),
              '', t('turns'), '', *_turns(t, content['turns']),
              '', t('events'), '', *_events(t, content['events']),
              '', t('verifications'), '', *_verifications(t, content),
              '', t('rounds'), '',
              *_rounds_text(content['rounds'], content['approved_round'], language),
              '', t('findings'), '', *_findings_text(t, content['findings']),
              '', t('policy'), '', t('policy_level', level=content['policy']['level']), '',
              describe_policy(content['policy'], language),
              '', t('criteria'), '', *_acceptance_text(t, content['acceptance'])]
    if content.get('advisory') is not None:
        parts += ['', t('advisory'), '', *_advisory_text(t, content['advisory'], language)]
    if content.get('budget') is not None:
        parts += ['', t('budget'), '', *_budget_text(t, content['budget'], language)]
    return '\n'.join(parts)


def _budget_text(t, found, language=ENGLISH):
    """The state of the budget of the story (#393, ``budget.view``): consumed, reserved and
    available per dimension; a value no turn attests is ``not_attested`` of the budget's texts
    (``non attestato`` in Italian), never 0."""
    if found.get('story') is None:
        return [t('budget_none')]
    b = BUDGET_TEXTS.texts(language)

    def usd(value):
        return b('not_attested') if value is None else f'{value:.2f} USD'

    lines = [t('budget_intro', story=found['story'], issue=found['issue'], at=found['at'],
               not_attested=b('not_attested')), '',
             t('budget_header'), '|---|---|---|---|---|']

    def count(label, c):
        cap = '—' if c['cap'] is None else c['cap']
        available = '—' if c['available'] is None else c['available']
        return f'| {label} | {c["consumed"]} | {c["reserved"]} | {available} | {cap} |'

    lines.append(count(t('budget_turns'), found['turns']))
    lines += [count(t('budget_turns_of', name=name), c) for name, c in found['roles'].items()]
    lines += [count(t('budget_phase', name=name), c) for name, c in found['phases'].items()]
    for kind, d in found['usd'].items():
        gap = t('budget_gap', n=d['unattested']) if d['unattested'] else ''
        consumed = usd(d['consumed'])
        if d['consumed'] is not None and d['unattested']:
            consumed = b('at_least', value=consumed)
        lines.append(f'| `{kind}` | {consumed}{gap} | {usd(d["reserved"])} | '
                     f'{usd(d["available"])} | {usd(d["cap"])} |')
    time_ = found['time']
    lines.append(f'| {t("budget_time")} | {time_["consumed"]} | — | {time_["available"]} | '
                 f'{time_["cap"]} |')
    lines += ['', t('budget_deadline', started=time_['started_at'],
                    deadline=time_['deadline_at'])]
    return lines


def _advisory_text(t, advisory, language=ENGLISH):
    """The advisory review of the head (#369): who wrote it and its opinion, never its text."""
    # Imported here: the module of the advisory review imports this one.
    from .dispatch_advisory import advisory_titles
    who, head = _v(t, advisory['reviewer']), _sha(t, advisory['head'])
    if advisory['outcome'] != 'done':
        return [t('advisory_failed', who=who, head=head)]
    return [t('advisory_done', who=who, head=head, opinion=advisory['opinion'],
              title=advisory_titles(language)['advisory_title'])]


def with_advisory(body, content, language=ENGLISH):
    """``body``, the comment of the current receipt of ``content``'s head, with its receipt
    rewritten from ``content``, which carries the advisory review of that head (#369): the
    section, in ``language`` (#634), and the ``receipt`` record, with digest and totals
    recomputed, while promotion, policy and state stay. The text before the receipt's title and
    the other records of the comment stay as they are. The title is recognised in every
    language devloop has (``titles``), so that a receipt published before a change of
    ``language`` is rewritten too. None when the comment has no such title."""
    lines = split_markers(body)[0].splitlines()
    known = titles(content['pr'])
    start = next((i for i, line in enumerate(lines) if line.strip() in known), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith('## ')),
               len(lines))
    text = '\n'.join(lines[:start] + section(content, language).splitlines()
                     + [''] * (end < len(lines)) + lines[end:])
    return with_record(body, 'receipt', record(content), text=text)


def superseded(body, marker, why, at, language=ENGLISH):
    """``body``, the comment of a current receipt with ``marker``, marked as superseded at
    ``at`` because ``why``: a warning before its first title, in ``language`` (#634), and the
    marker with ``state: superseded``. The other records of the comment, such as the ``policy``
    record of the promotion that carries the receipt (#338), stay as they are."""
    lines = split_markers(body)[0].splitlines()
    first = next((i for i, line in enumerate(lines) if line.startswith('## ')), len(lines))
    warning = ['> [!WARNING]',
               TEXTS.texts(language)('superseded', at=at, why=' '.join(why.split())), '']
    text = '\n'.join(lines[:first] + warning + lines[first:])
    return with_record(body, 'receipt', dict(marker, state='superseded', superseded_at=at,
                                             reason=' '.join(why.split())), text=text)
