"""GitHub text: the AGENTS.md attribution header, hidden markers, review rendering.

Markers are the dispatcher's control records, and they share a GitHub account with text written
by models (summaries, findings, notes, PR bodies, logs). Two rules keep the two apart:

- every published body goes through ``publishable``, which neutralizes HTML comments in the
  text, so the text cannot contain a marker at all;
- ``read_marker`` accepts one marker per body, only as its last line, only of the expected kind
  and only with a valid structure.
"""

import base64
import json
import re

from .acceptance import render_map
from .policy import EVENTS, valid_receipt
from .supervision import COMMANDS, OUTCOMES, critical, describe

UNKNOWN = 'non noto'
_EFFORT_LABELS = {'xhigh': 'Extra High', 'max': 'Max'}
_MARKER = re.compile(r'<!-- devloop:(?P<kind>[a-z]+):v1 (?P<payload>[A-Za-z0-9+/=]+) -->')
_VERSION = re.compile(r'\d+\.\d+(?:\.\d+)?')

DISPATCHER = {'agent': 'devloop dispatcher', 'model': 'nessuno (testo generato dal dispatcher)',
              'effort': 'n/a'}


def parse_version(text):
    match = _VERSION.search(text or '')
    return match.group(0) if match else None


def effort_label(effort):
    effort = (effort or '').strip().lower()
    if not effort:
        return UNKNOWN
    return _EFFORT_LABELS.get(effort, effort.title())


def header(agent_name, cli_version, model_label, effort):
    """The header AGENTS.md requires on every text published to GitHub."""
    agent = f'{agent_name} (CLI v{cli_version})' if cli_version else f'{agent_name} (CLI {UNKNOWN})'
    return ('> [!NOTE]\n'
            f'> **Agent:** {agent}\n'
            f'> **Model:** {model_label or UNKNOWN}\n'
            f'> **Reasoning Level:** {effort_label(effort)}\n')


def dispatcher_header():
    return ('> [!NOTE]\n'
            f'> **Agent:** {DISPATCHER["agent"]}\n'
            f'> **Model:** {DISPATCHER["model"]}\n'
            f'> **Reasoning Level:** {DISPATCHER["effort"]}\n')


def marker(kind, payload):
    data = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    return f'<!-- devloop:{kind}:v1 {base64.b64encode(data).decode()} -->'


def neutralize(text):
    """Text that cannot open an HTML comment, and therefore cannot carry a marker."""
    return (text or '').replace('<!--', '&lt;!--')


def publishable(body, kind=None, payload=None):
    """The only way devloop builds a body for GitHub: neutralized text plus, optionally, the
    controller's marker as the last line."""
    body = neutralize(body).rstrip('\n') + '\n'
    if kind is not None:
        body += '\n' + marker(kind, payload) + '\n'
    return body


def _is_int(value, minimum=0):
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _list_of_dicts(value):
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def _valid_review(r):
    return (r.get('verdict') in _VERDICT_TEXT and _is_int(r.get('round'), 1)
            and isinstance(r.get('head_sha'), str) and isinstance(r.get('summary'), str)
            and r.get('source') in ('agent', 'ci')
            and all(_list_of_dicts(r.get(k)) for k in ('blocking_findings',
                                                        'non_blocking_findings',
                                                        'previous_findings'))
            and all(isinstance(f.get('id'), str) for f in r['blocking_findings'])
            and all(isinstance(f.get('key', ''), str)
                    for f in r['blocking_findings'] + r['previous_findings']))


def _valid_fix(f):
    return (_is_int(f.get('round'), 1) and isinstance(f.get('head_sha'), str)
            and _list_of_dicts(f.get('responses')))


def _valid_sync(s):
    return isinstance(s.get('head_sha'), str)


def _valid_pr(m):
    return _is_int(m.get('issue'), 1) and all(isinstance(m.get(k), str)
                                              for k in ('implementer', 'reviewer'))


def _valid_policy(p):
    return p.get('event') in EVENTS and valid_receipt(p.get('receipt'))


def _valid_closure(c):
    return (isinstance(c, dict) and isinstance(c.get('key'), str)
            and c.get('status') in ('resolved', 'withdrawn') and c.get('by') in ('ci', 'operator')
            and all(isinstance(c.get(k), str) for k in ('author', 'reason', 'head_sha')))


def _valid_finding(f):
    closed = f.get('closed')
    return isinstance(closed, list) and bool(closed) and all(_valid_closure(c) for c in closed)


GAP_KINDS = ('operator_decision', 'default_available', 'tool_unavailable')


def _valid_gap(g):
    return (isinstance(g, dict) and g.get('kind') in GAP_KINDS
            and isinstance(g.get('description'), str) and isinstance(g.get('proposed_default'), str)
            and isinstance(g.get('code_refs'), list)
            and all(isinstance(ref, str) for ref in g['code_refs']))


def _valid_preflight(p):
    """A readiness check (#114): ``ready`` never with ``blocked``, which records that the check
    added ``agent:needs-human`` after ``labeled`` earlier ``labeled`` events of that label."""
    return (all(isinstance(p.get(k), str) for k in ('hash', 'agent', 'split'))
            and all(isinstance(p.get(k), bool) for k in ('ready', 'blocked'))
            and not (p['ready'] and p['blocked']) and _is_int(p.get('labeled'))
            and isinstance(p.get('gaps'), list) and all(_valid_gap(g) for g in p['gaps']))


NOTICES = ('escalation', 'blocked')


def _valid_notice(n):
    """A text devloop publishes without a record of its own (#114): an escalation to a human,
    or an agent that stopped. Its marker tells it apart from the operator's own text when both
    are written with the same login. Since #182 it carries the escalation's ``class``; a notice
    without one, or with one that is not known, is critical (``supervision.critical``)."""
    return n.get('event') in NOTICES


LEDGER_TYPES = ('request', 'turn', 'event', 'verification')
TURN_STATES = ('open', 'closed', 'incomplete')


def _valid_ledger(r):
    """A dispatcher record of evidence and telemetry (#133). A turn that is not closed has no
    end, outcome nor telemetry: they are null, never guessed."""
    if r.get('type') not in LEDGER_TYPES:
        return False
    if r['type'] == 'turn':
        return (r.get('state') in TURN_STATES
                and all(isinstance(r.get(k), str) for k in ('kind', 'agent', 'started_at'))
                and (r['state'] == 'closed'
                     or all(r.get(k) is None for k in ('ended_at', 'outcome', 'telemetry'))))
    if r['type'] == 'request':
        return isinstance(r.get('hash'), str) and isinstance(r.get('comments'), list)
    if r['type'] == 'verification':
        return isinstance(r.get('command'), str) and r.get('outcome') in ('pass', 'fail')
    return isinstance(r.get('event'), str)


def _valid_merged(m):
    """A merge devloop made (#172): the merged head, its receipt, level and merge mode."""
    return (isinstance(m.get('head'), str) and m.get('mode') in ('strict', 'auto')
            and _is_int(m.get('level')) and valid_receipt(m.get('receipt')))


def _valid_hold(h):
    """A hold or release command the dispatcher applied (#172), with its author and reason,
    and in ``comment`` the id of the comment that carried it (the commands it spends)."""
    return (h.get('event') in ('hold', 'release')
            and all(isinstance(h.get(k), str) for k in ('by', 'reason')))


def _valid_supervision(s):
    """A supervisor command the dispatcher handled (#182): the command, its author, the outcome
    and in ``comment`` the id of the comment that carried it (the commands it spends)."""
    defaults = s.get('defaults', [])
    return (s.get('command') in COMMANDS and s.get('outcome') in OUTCOMES
            and isinstance(s.get('by'), str) and isinstance(s.get('reason', ''), str)
            and isinstance(defaults, list)
            and all(isinstance(d, dict) and isinstance(d.get('description'), str)
                    and isinstance(d.get('proposed_default'), str) for d in defaults))


_VALID = {'review': _valid_review, 'fix': _valid_fix, 'pr': _valid_pr, 'policy': _valid_policy,
          'finding': _valid_finding, 'sync': _valid_sync, 'preflight': _valid_preflight,
          'notice': _valid_notice, 'ledger': _valid_ledger, 'merged': _valid_merged,
          'hold': _valid_hold, 'supervision': _valid_supervision}


def read_marker(text, kind):
    """The record of ``kind`` carried by ``text``, or None.

    Only the last non-empty line counts, so a marker quoted or embedded in the text is ignored.
    """
    lines = (text or '').strip().splitlines()
    match = _MARKER.fullmatch(lines[-1].strip()) if lines else None
    if not match or match.group('kind') != kind:
        return None
    try:
        data = json.loads(base64.b64decode(match.group('payload'), validate=True))
    except (ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and _VALID[kind](data) else None


def carries_record(text):
    """Whether ``text`` ends with a valid devloop marker, of any kind."""
    return any(read_marker(text, kind) is not None for kind in _VALID)


_VERDICT_TEXT = {
    'approve': 'Approvata',
    'request_changes': 'Modifiche richieste',
    'needs_human': 'Serve una decisione umana',
}
_STATUS_TEXT = {'resolved': 'risolto', 'unresolved': 'non risolto', 'withdrawn': 'ritirato'}
_ACTION_TEXT = {'fixed': 'corretto', 'disputed': 'contestato', 'not_applicable': 'non applicabile'}


def _location(finding):
    if not finding['file']:
        return ''
    line = f':{finding["line"]}' if finding['line'] > 0 else ''
    return f' `{finding["file"]}{line}`'


def _findings(findings):
    lines = []
    for f in findings:
        lines.append(f'- **{f.get("key") or f["id"]}**{_location(f)}: {f["title"]}')
        if f['detail']:
            lines.append(f'  {f["detail"]}'.replace('\n', '\n  '))
        if f['suggested_fix']:
            lines.append(f'  *Correzione suggerita:* {f["suggested_fix"]}'.replace('\n', '\n  '))
    return '\n'.join(lines)


def render_review(review, head):
    """Markdown body (after the header) for a normalized review record."""
    parts = [f'## Review automatica, round {review["round"]}/{review["max_rounds"]}', '',
             f'**Verdetto:** {_VERDICT_TEXT[review["verdict"]]}  ',
             f'**Commit:** `{review["head_sha"][:12]}` · **CI:** {review["ci"]}']
    receipt = review.get('receipt')
    if valid_receipt(receipt):
        parts[-1] += '  '
        parts.append(f'**Livello di review:** {receipt["level"]} · **Base:** '
                     f'`{receipt["base"][:12]}` · **Policy:** `{receipt["policy"][:12]}`')
    if review.get('note'):
        parts += ['', f'> {review["note"]}']
    # #160: the base advanced during the round.
    if review.get('bases'):
        spans = ', '.join(f'`{c["from"][:12]}` → `{c["to"][:12]}`' for c in review['bases'])
        parts += ['', f'> La base è avanzata durante il round ({spans}) senza toccare file della '
                      'PR: le review già date restano valide e il round continua.']
    if review.get('restarts'):
        parts += ['', f'> Il round {review["restarts"]["round"]} è stato abbandonato perché la '
                      'base è avanzata toccando file della PR, o senza che la non interferenza '
                      'fosse dimostrabile: quel round non conta per il limite dei round.']
    parts += ['', review['summary'].strip()]
    if review['verdict'] == 'needs_human' and review['human_reason']:
        parts += ['', '### Decisione richiesta', '', review['human_reason'].strip()]
    if review['blocking_findings']:
        parts += ['', '### Bloccanti', '', _findings(review['blocking_findings'])]
    if review['non_blocking_findings']:
        parts += ['', '### Non bloccanti', '', _findings(review['non_blocking_findings'])]
    if review['previous_findings']:
        parts += ['', '### Finding dei round precedenti', '', '| ID | Stato | Nota |', '|---|---|---|']
        for p in review['previous_findings']:
            note = p['note'].replace('|', '\\|').replace('\n', ' ')
            parts.append(f'| {p.get("key") or p["id"]} | {_STATUS_TEXT[p["status"]]} | {note} |')
    if review['checks_performed']:
        parts += ['', '<details><summary>Controlli eseguiti</summary>', '']
        parts += [f'- {c}' for c in review['checks_performed']]
        parts += ['', '</details>']
    return publishable(head + '\n' + '\n'.join(parts), 'review', review)


_REVIEW_EVENT = {'approve': 'APPROVE', 'request_changes': 'REQUEST_CHANGES',
                 'needs_human': 'COMMENT'}


def render_native_review(review, head):
    """``{event, body}`` of the GitHub review that mirrors a devloop review. It carries no
    marker: devloop reads only the review comment."""
    body = (head + f'\n**Verdetto devloop:** {_VERDICT_TEXT[review["verdict"]]} (round '
            f'{review["round"]}/{review["max_rounds"]}, commit `{review["head_sha"][:12]}`). '
            'I dettagli sono nel commento della review automatica.')
    return {'event': _REVIEW_EVENT[review['verdict']], 'body': publishable(body)}


def render_fix(fix, head, round_no):
    parts = [f'## Correzioni per il round {round_no}', '', fix['summary'].strip()]
    if fix['responses']:
        parts += ['', '| Finding | Azione | Nota |', '|---|---|---|']
        for r in fix['responses']:
            note = r['note'].replace('|', '\\|').replace('\n', ' ')
            parts.append(f'| {r["finding_id"]} | {_ACTION_TEXT[r["action"]]} | {note} |')
    payload = {'round': round_no, 'head_sha': fix['head_sha'], 'responses': fix['responses']}
    if 'acceptance_map' in fix:
        # The latest map of the PR from now on (#134).
        parts += ['', render_map(fix['acceptance_map'])]
        payload['acceptance_map'] = fix['acceptance_map']
    if 'profile' in fix:
        payload['profile'] = fix['profile']  # who wrote the fix (#111)
    if 'local_tests' in fix:
        payload['local_tests'] = fix['local_tests']  # informative only (#169)
    if fix.get('skipped_models'):
        payload['skipped_models'] = fix['skipped_models']  # informative only (#151)
    return publishable(head + '\n' + '\n'.join(parts), 'fix', payload)


_GAP_TEXT = {'operator_decision': 'Decisione dell\'operatore',
             'default_available': 'Default disponibile, da accettare',
             'tool_unavailable': 'Strumento non disponibile'}
PREFLIGHT_RESUME = (
    'Per proseguire, un utente fidato risponde nella issue: decide i punti aperti o accetta i '
    'default proposti in un commento, oppure modifica titolo o corpo. Il controllo si ripete al '
    'tick successivo; per ripeterlo senza modifiche basta un commento con `/devloop recheck` '
    'come prima riga. Se l\'esito è positivo devloop rimuove `agent:needs-human` che aveva messo '
    'e l\'issue torna in coda.')


def render_preflight(record, head, note=''):
    """The comment of a readiness check (#114): ``record`` is its ``preflight`` marker."""
    if record['ready']:
        parts = ['## Controllo di prontezza superato', '',
                 'L\'issue è implementabile in una RUN: l\'implementazione può partire.']
    else:
        parts = ['## Controllo di prontezza: issue non pronta', '',
                 'L\'implementer non parte finché le lacune qui sotto non sono risolte.']
        if record['gaps']:
            parts += ['', '### Lacune', '']
            for gap in record['gaps']:
                parts.append(f'- **{_GAP_TEXT[gap["kind"]]}:** {gap["description"].strip()}'
                             .replace('\n', '\n  '))
                if gap['code_refs']:
                    parts.append('  *Riferimenti:* ' + ', '.join(f'`{ref}`'
                                                                 for ref in gap['code_refs']))
                if gap['proposed_default'].strip():
                    parts.append(f'  *Default proposto:* {gap["proposed_default"].strip()}'
                                 .replace('\n', '\n  '))
        if record['split'].strip():
            parts += ['', '### Suddivisione proposta', '', record['split'].strip()]
        if not record['gaps'] and not record['split'].strip():
            parts += ['', 'Il controllo non ha indicato lacune né una suddivisione.']
        parts += ['', PREFLIGHT_RESUME]
    if note:
        parts += ['', f'> {note}']
    return publishable(head + '\n' + '\n'.join(parts), 'preflight', record)


RESUME = ('Per riprendere: correggere la causa, **rimuovere `agent:needs-human`** e impostare la '
          'label del passo da eseguire (`agent:ready` sulla issue, `agent:review` o '
          '`agent:changes` sulla PR).')


SUPERVISOR_RESUME = (
    'Escalation non critica: decide il supervisore (modalità di merge `auto`). Comandi, come prima '
    'riga di un commento, dall\'identità di `[roles].supervisor` o da un utente fidato:\n\n'
    '- `/devloop retry`: ripete il passo fallito, senza contare un round (al massimo 2 volte per '
    'passo e input, poi l\'escalation passa all\'operatore);\n'
    '- `/devloop accept-defaults`: accetta i default proposti dal controllo di prontezza e rimette '
    'l\'issue in coda;\n'
    '- `/devloop escalate <motivo>`: passa l\'escalation all\'operatore (`agent:needs-human`).')


def render_escalation(reason, detail='', cls='failure', supervised=None):
    """An escalation (#182: of class ``cls``). ``supervised``, for a non-critical one the
    supervisor decides, holds the step, the key of its input and what a retry resumes; without
    it the escalation goes to the operator."""
    title = 'Escalation al supervisore' if supervised else 'Serve un intervento umano'
    body = (dispatcher_header() + f'\n## {title}\n\n' + reason.strip() + '\n\n'
            + f'**Classe:** `{cls}` ({describe(cls)}), '
            + ('critica' if critical(cls) else 'non critica') + '.\n\n'
            + (SUPERVISOR_RESUME if supervised else RESUME) + '\n')
    if detail:
        body += '\n```text\n' + detail.strip()[-6000:].replace('```', "'''") + '\n```\n'
    payload = {'event': 'escalation', 'class': cls, 'critical': critical(cls),
               'to': 'supervisor' if supervised else 'operator'}
    if supervised:
        payload.update(supervised)
    return publishable(body, 'notice', payload)


def render_supervision(record, text):
    """The dispatcher's trace of a supervisor command (#182): what was asked, by whom, and the
    outcome, in a ``supervision`` marker."""
    return publishable(dispatcher_header() + f'\n## Decisione del supervisore\n\n{text.strip()}',
                       'supervision', record)
