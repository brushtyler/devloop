"""GitHub text: the AGENTS.md attribution header, hidden markers, review rendering.

Markers are the dispatcher's control records, and they share a GitHub account with text written
by models (summaries, findings, notes, PR bodies, logs). Two rules keep the two apart:

- every published body goes through ``publishable``, which neutralizes HTML comments in the
  text, so the text cannot contain a marker at all;
- ``read_marker`` accepts markers only in the block of lines that ends a body, one marker per
  line and at most one per kind, only of the expected kind and only with a valid structure. A
  body usually ends with one marker; the comment of a promotion ends with its records, one per
  line (#338).
"""

import base64
import functools
import json
import re
import subprocess
from pathlib import Path

from .acceptance import render_map
from .authorization import AUTHORIZE_COMMAND, PRIORITIES
from .authorization import OUTCOMES as AUTHORIZE_OUTCOMES
from .config import INSTALL_ROOT
from .generated import generated_record, render_generated
from .github import GitHubError
from .hoststep import render_step
from .i18n import ENGLISH, Catalog
from .journal import ATTEMPT_ID, PHASES, STORY_ID
from .policy import EVENTS, valid_receipt
from .review import (LEGACY_VERDICT, current_verdict, findings_only, own_open_findings,
                     trust_policy)
from .supervision import MAX_RETRIES, OUTCOMES, critical, describe, recorded_command

UNKNOWN = 'non noto'
UNKNOWN_VERSION = 'non nota'
_EFFORT_LABELS = {'xhigh': 'Extra High', 'max': 'Max'}
_MARKER = re.compile(r'<!-- devloop:(?P<kind>[a-z]+):v1 (?P<payload>[A-Za-z0-9+/=]+) -->')
_VERSION = re.compile(r'\d+\.\d+(?:\.\d+)?')

# Every text devloop publishes opens with the name of the identity it publishes with and the
# version of devloop (#232): the texts of the AI agents add agent, model and reasoning level in
# the same block, those of bots without AI (the dispatcher, one day the merger) nothing else.
# Records published before #232 carry the agent header alone, the dispatcher's with ``devloop
# dispatcher`` as the agent: every form is devloop text.
DISPATCHER = 'devloop dispatcher'
# The stamp ``devloop pin`` writes in the root of a pinned copy (``pin.STAMP``).
PIN_STAMP = 'pin.json'
_SHA = re.compile(r'[0-9a-f]{40}')
_SHA256 = re.compile(r'[0-9a-f]{64}')


def parse_version(text):
    match = _VERSION.search(text or '')
    return match.group(0) if match else None


def effort_label(effort):
    effort = (effort or '').strip().lower()
    if not effort:
        return UNKNOWN
    return _EFFORT_LABELS.get(effort, effort.title())


def attribution(bot=None, version=None, agent=None):
    """The one ``> [!NOTE]`` block of a text published to GitHub (AGENTS.md, #232), composed
    here only. ``bot``: the name of the identity devloop publishes with, followed by the
    ``version`` of devloop; ``agent``: ``(name, cli_version, model_label, effort)`` of the AI
    agent that wrote the text. A bot without AI has ``bot`` alone; a text of an agent that
    devloop publishes has both, in one block; a text an agent publishes outside devloop has
    ``agent`` alone."""
    lines = ['> [!NOTE]']
    if bot:
        lines += [f'> **Bot:** {bot}', f'> **Version:** {version or UNKNOWN_VERSION}']
    if agent:
        name, cli_version, model_label, effort = agent
        cli = f'CLI v{cli_version}' if cli_version else f'CLI {UNKNOWN}'
        lines += [f'> **Agent:** {name} ({cli})', f'> **Model:** {model_label or UNKNOWN}',
                  f'> **Reasoning Level:** {effort_label(effort)}']
    return '\n'.join(lines) + '\n'


def header(agent_name, cli_version, model_label, effort, bot=None, version=None):
    """The header of a text an AI agent wrote. devloop publishes it with ``bot``, the name of
    the agent's identity (``agent_bot``), and ``version``, the version of devloop (#232);
    without ``bot`` it is the header an agent writes outside devloop."""
    return attribution(bot, version, (agent_name, cli_version, model_label, effort))


def agent_bot(key):
    """The name of the identity devloop publishes the texts of agent ``key`` with (#232):
    ``devloop claude-agent``, ``devloop codex-agent``, ``devloop agy-agent``."""
    return f'devloop {key}-agent'


def _checkout_sha(root):
    """The commit of the checkout whose top level is ``root``, or None: a directory inside
    another repository (a pinned copy without its stamp) is not a checkout of devloop."""
    try:
        proc = subprocess.run(['git', 'rev-parse', '--show-toplevel', 'HEAD'], cwd=root,
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = proc.stdout.splitlines() if proc.returncode == 0 else []
    if len(lines) != 2 or not _SHA.fullmatch(lines[1].strip()):
        return None
    try:
        same = Path(lines[0].strip()).resolve() == Path(root).resolve()
    except OSError:
        return None
    return lines[1].strip() if same else None


@functools.cache
def devloop_version(root=INSTALL_ROOT):
    """The version of devloop running from ``root``: the first 12 characters of the commit of
    the pinned copy (its ``pin.json``), else of the checkout, else ``UNKNOWN_VERSION``.
    Computed once per process: a new pin starts a new process."""
    try:
        commit = json.loads((Path(root) / PIN_STAMP).read_text(encoding='utf-8')).get('commit')
    except (OSError, ValueError, AttributeError):
        commit = None
    if not (isinstance(commit, str) and _SHA.fullmatch(commit)):
        commit = _checkout_sha(root)
    return commit[:12] if commit else UNKNOWN_VERSION


def bot_header(bot, version):
    """The header of a text written by a bot without AI (#232): its name and the version of
    devloop, instead of an agent, a model and a reasoning level."""
    return attribution(bot, version)


def dispatcher_header(root=INSTALL_ROOT):
    """The header of every text the dispatcher writes on its own."""
    return bot_header(DISPATCHER, devloop_version(root))


# The texts of the review, the fix and the readiness check (#632), in the project's ``language``
# (``i18n``); English for a language without translation. Markers, commands and labels are
# never translated.
TEXTS = Catalog(
    en={
        'truncated': ('> **Text truncated:** the review exceeds the limit of 65,536 characters '
                      'of a GitHub text, and devloop shortened only its readable part. The full '
                      'record, with every finding, is in the marker.'),
        # Review.
        'verdict_approve': 'Approved',
        'verdict_request_changes': 'Changes requested',
        'verdict_needs_operator': 'An operator decision is needed',
        'verdict_unproven': 'No blocking finding with evidence: not approved',
        'verdict_trust': 'Trust policy: review and merge by the operator',
        'status_resolved': 'resolved',
        'status_unresolved': 'unresolved',
        'status_withdrawn': 'withdrawn',
        'security': ' *(security)*',
        'without_evidence': ' *(blocking without evidence: recorded as non-blocking)*',
        'evidence': '*Evidence:*',
        'suggested_fix': '*Suggested fix:*',
        'review_title': '## Automatic review, round {round}/{max_rounds}',
        'findings_only_title': '## Findings-only review, round {round}/{max_rounds}',
        'verdict': '**Verdict:** {verdict}  ',
        'review_level': ('**Review level:** {level} · **Base:** `{base}` · **Policy:** '
                         '`{policy}`'),
        'bases': ('> The base advanced during the round ({spans}) without touching files of the '
                  'PR: the reviews already given stay valid and the round goes on.'),
        'restarts': ('> Round {round} was abandoned because the base advanced touching files of '
                     'the PR, or without non-interference being provable: that round does not '
                     'count toward the round limit.'),
        'resync_base_changed': ('the dispatcher revoked that approval because the base advanced '
                                'touching files of the PR, or without non-interference being '
                                'provable, and this round reviews the same commit'),
        'resync_conflicts': ('the new head differs from it only by the merge of the base, with '
                             'conflicts resolved by the agent'),
        'resync_clean': ('the new head differs from it only by the merge of the base, without '
                         'conflicts'),
        'resync_counts_conflicts': ('this round does not count toward the round limit if it '
                                    'approves; if it opens blocking findings it counts'),
        'resync_counts': 'this round does not count toward the round limit',
        'resync': '> Round {round} had approved `{sha}`; {why}: {counts}.',
        'trust_touched': 'Trust policy touched',
        'decision_required': 'Decision required',
        'trust_note': ('> The PR touches the trust policy: devloop does not stop. With open '
                       'blocking findings the PR goes back to the implementer for the fix, as '
                       'for a request for changes; without them, and with the CI green, it goes '
                       'to the operator once, for the review and the merge.'),
        'native_open': 'blocking findings of the reviewer stay open',
        'native_none': 'no blocking finding of the reviewer stays open',
        'native_wait_trust': 'the merge stays with the operator',
        'native_wait': ('the decision stays with the operator, and devloop holds the PR waiting '
                        'for it'),
        'native': '> Native review `{native}`: {outcome}. It is not an approval of the merge: '
                  '{wait}.',
        'blocking': '### Blocking',
        'non_blocking': '### Non-blocking',
        'previous': '### Findings of the previous rounds',
        'previous_table': '| ID | Status | Note |',
        'checks': '<details><summary>Checks performed</summary>',
        # Fix.
        'action_fixed': 'fixed',
        'action_disputed': 'disputed',
        'action_not_applicable': 'not applicable',
        'fix_requested_title': '## Fixes asked with `/devloop fix` on `{sha}`',
        'comment_id': 'comment {comment}',
        'comment': 'comment',
        'fix_request': 'Request of `{by}` ({cited}):',
        'fix_title': '## Fixes for round {round}',
        'base_merge': ('Before the fix the dispatcher merged `origin/{branch}` (`{base}`) into '
                       'the branch, without conflicts: merge commit `{head}`, pushed with the '
                       'fix. The merge does not use up a review round.'),
        'responses_table': '| Finding | Action | Note |',
        'fix_conclave': ('The fix followed the resolution of the conclave recorded in comment '
                         '{comment}.'),
        # Readiness check.
        'gap_operator_decision': 'Operator decision',
        'gap_default_available': 'Default available, to be accepted',
        'gap_tool_unavailable': 'Tool not available',
        'preflight_resume': (
            'To go on, a trusted user answers in the issue: they decide the open points or '
            'accept the proposed defaults in a comment, or they change title or body. The check '
            'runs again at the next tick; to run it again without changes, a comment with '
            '`/devloop recheck` as its first line is enough, or `agent:ready` added again (by a '
            'trusted user or the supervisor). If a gap depends on the content of the base '
            'branch, the check runs again by itself when the base branch advances. If the '
            'outcome is positive devloop removes the `agent:needs-operator` it had added and the '
            'issue goes back to the queue.'),
        'refused_intro': 'These commands do not count as a decision of the operator:',
        'refused_line': '- the comment of `{login}` of {at}: {reason}.',
        'unknown_time': 'an unknown time',
        'refused_title': '`/devloop approve` refused',
        'approve_rule': ('A decision on the trust policy is taken only by the operator, in '
                         'person: a comment whose first line is `/devloop approve`, followed by '
                         'the text of the decision, written after the latest change of the text '
                         'of the issue and before the takeover.'),
        'ready_title': '## Readiness check passed',
        'ready_text': ('The issue can be implemented in one RUN: the implementation can '
                       'start.'),
        'not_ready_title': '## Readiness check: issue not ready',
        'not_ready_text': 'The implementer does not start until the gaps below are resolved.',
        'gaps': '### Gaps',
        'gap_base': '  *Depends on the base branch:* the check runs again when it advances.',
        'gap_refs': '  *References:* ',
        'gap_default': '  *Proposed default:* {default}',
        'gap_prerequisite': '  *Missing prerequisite:* {text}',
        'gap_prerequisite_number': '  *Missing prerequisite:* {text} (#{number})',
        'prerequisites_resume': (
            'devloop added the native "blocked by" {issues} to this issue, without '
            '`agent:needs-operator`: the issue leaves the queue and the check runs again once '
            'they are all closed. Whoever finds the dependency wrong removes it.'),
        'split': '### Proposed split',
        'no_gaps': 'The check indicated neither gaps nor a split.',
        'trust_covered': ('**Trust policy:** touched, covered by a decision of the '
                          'operator.'),
        'recheck_command': 'The command `/devloop recheck`',
        'recheck_relabeled': '`agent:ready` added again',
        'recheck_title': '## Readiness check not run again',
        'recheck_text': ('{what} of `{by}` does not run the readiness check again: {reason}.\n\n'
                         'The block stays, and is resolved as its comment says. When the latest '
                         'block of the issue is again the readiness check\'s, a new '
                         '`/devloop recheck` runs it again.'),
        # Why a request runs no check (``PreflightMixin._preflight_block``,
        # ``_supervisor_block``), the ``reason`` of the ``recheck`` record.
        'why_both_names': ('`agent:needs-operator` is there under both its names, and which of '
                           'the two is the check\'s cannot be told'),
        'why_not_set': ('the latest readiness check did not set `agent:needs-operator`, which '
                        'was already there'),
        'why_escalation': ('after the readiness check there is an escalation of class {cls} '
                           '({described}): the latest block of the issue is that one, not the '
                           'readiness check'),
        'why_no_class': 'not stated',
        'why_added_again': ('`agent:needs-operator` was added again after the readiness check, '
                            'and not by the check'),
        'why_supervisor': 'the issue is in `{label}` and waits for the supervisor',
        'why_supervisor_class': ('the issue is in `{label}` and waits for the supervisor, for an '
                                 'escalation of class `{cls}` ({described}): the latest block of '
                                 'the issue is that one, not the readiness check'),
        # The notes of the readiness check (``PreflightMixin._publish_preflight``).
        'note_block_removed': 'The block set by the readiness check was removed.',
        'note_label_stays': ('{labels} stays: it was not set by the readiness check, so a '
                             'person removes it.'),
        'note_not_supervised': ('In merge mode `auto` these gaps would go to the supervisor, '
                                'but the escalation stays with the operator: {why}.'),
        'note_label_present': ('{labels} was already there and is not attributed to the '
                               'readiness check: a new positive outcome does not remove it.'),
        'note_reused': ('Outcome of the previous check, reused: the text of the issue has not '
                        'changed.'),
        'note_no_prerequisite_block': ('The gaps name only missing prerequisites, but devloop '
                                       'does not add their "blocked by": {why}.'),
    },
    it={
        'truncated': ('> **Testo troncato:** la review supera il limite di 65 536 caratteri di '
                      'un testo di GitHub, e devloop ne ha accorciato solo la parte leggibile. '
                      'Il record completo, con tutti i finding, è nel marker.'),
        'verdict_approve': 'Approvata',
        'verdict_request_changes': 'Modifiche richieste',
        'verdict_needs_operator': 'Serve una decisione dell\'operatore',
        'verdict_unproven': 'Nessun finding bloccante con evidenza: non approvata',
        'verdict_trust': 'Policy di fiducia: review e merge dell\'operatore',
        'status_resolved': 'risolto',
        'status_unresolved': 'non risolto',
        'status_withdrawn': 'ritirato',
        'security': ' *(sicurezza)*',
        'without_evidence': ' *(bloccante senza evidenza: registrato come non bloccante)*',
        'evidence': '*Evidenza:*',
        'suggested_fix': '*Correzione suggerita:*',
        'review_title': '## Review automatica, round {round}/{max_rounds}',
        'findings_only_title': '## Review di soli finding, round {round}/{max_rounds}',
        'verdict': '**Verdetto:** {verdict}  ',
        'review_level': ('**Livello di review:** {level} · **Base:** `{base}` · **Policy:** '
                         '`{policy}`'),
        'bases': ('> La base è avanzata durante il round ({spans}) senza toccare file della PR: '
                  'le review già date restano valide e il round continua.'),
        'restarts': ('> Il round {round} è stato abbandonato perché la base è avanzata toccando '
                     'file della PR, o senza che la non interferenza fosse dimostrabile: quel '
                     'round non conta per il limite dei round.'),
        'resync_base_changed': ('il dispatcher ha revocato quell\'approvazione perché la base è '
                                'avanzata toccando file della PR, o senza che la non '
                                'interferenza fosse dimostrabile, e questo round rivede lo '
                                'stesso commit'),
        'resync_conflicts': ('il nuovo head ne differisce solo per il merge della base, con '
                             'conflitti risolti dall\'agent'),
        'resync_clean': ('il nuovo head ne differisce solo per il merge della base senza '
                         'conflitti'),
        'resync_counts_conflicts': ('questo round non conta per il limite dei round se approva; '
                                    'se apre finding bloccanti conta'),
        'resync_counts': 'questo round non conta per il limite dei round',
        'resync': '> Il round {round} aveva approvato `{sha}`; {why}: {counts}.',
        'trust_touched': 'Policy di fiducia toccata',
        'decision_required': 'Decisione richiesta',
        'trust_note': ('> La PR tocca la policy di fiducia: devloop non si ferma. Con finding '
                       'bloccanti aperti la PR torna all\'implementer per la correzione, come per '
                       'una richiesta di modifiche; senza, e con la CI verde, va all\'operatore '
                       'una sola volta, per la review e il merge.'),
        'native_open': 'restano aperti finding bloccanti del reviewer',
        'native_none': 'non resta aperto nessun finding bloccante del reviewer',
        'native_wait_trust': 'il merge resta all\'operatore',
        'native_wait': ('la decisione resta all\'operatore, e devloop tiene la PR in attesa '
                        'della sua decisione'),
        'native': ('> Review nativa `{native}`: {outcome}. Non è un\'approvazione del merge: '
                   '{wait}.'),
        'blocking': '### Bloccanti',
        'non_blocking': '### Non bloccanti',
        'previous': '### Finding dei round precedenti',
        'previous_table': '| ID | Stato | Nota |',
        'checks': '<details><summary>Controlli eseguiti</summary>',
        'action_fixed': 'corretto',
        'action_disputed': 'contestato',
        'action_not_applicable': 'non applicabile',
        'fix_requested_title': '## Correzioni richieste con `/devloop fix` su `{sha}`',
        'comment_id': 'commento {comment}',
        'comment': 'commento',
        'fix_request': 'Richiesta di `{by}` ({cited}):',
        'fix_title': '## Correzioni per il round {round}',
        'base_merge': ('Prima della correzione il dispatcher ha fatto il merge di '
                       '`origin/{branch}` (`{base}`) nel branch, senza conflitti: merge commit '
                       '`{head}`, pubblicato con la correzione. Il merge non consuma un round di '
                       'review.'),
        'responses_table': '| Finding | Azione | Nota |',
        'fix_conclave': ('La correzione ha seguito la risoluzione del conclave registrata nel '
                         'commento {comment}.'),
        'gap_operator_decision': 'Decisione dell\'operatore',
        'gap_default_available': 'Default disponibile, da accettare',
        'gap_tool_unavailable': 'Strumento non disponibile',
        'preflight_resume': (
            'Per proseguire, un utente fidato risponde nella issue: decide i punti aperti o '
            'accetta i default proposti in un commento, oppure modifica titolo o corpo. Il '
            'controllo si ripete al tick successivo; per ripeterlo senza modifiche basta un '
            'commento con `/devloop recheck` come prima riga, oppure rimettere `agent:ready` '
            '(un utente fidato o il supervisore). Se una lacuna dipende dal contenuto del branch '
            'base, il controllo si ripete da solo quando il branch base avanza. Se l\'esito è '
            'positivo devloop rimuove `agent:needs-operator` che aveva messo e l\'issue torna in '
            'coda.'),
        'refused_intro': 'Questi comandi non valgono come decisione dell\'operatore:',
        'refused_line': '- il commento di `{login}` del {at}: {reason}.',
        'unknown_time': 'momento non noto',
        'refused_title': '`/devloop approve` rifiutati',
        'approve_rule': ('Una decisione sulla policy di fiducia la prende solo l\'operatore, di '
                         'persona: un commento la cui prima riga è `/devloop approve`, seguita '
                         'dal testo della decisione, scritto dopo l\'ultima modifica del testo '
                         'dell\'issue e prima della presa in carico.'),
        'ready_title': '## Controllo di prontezza superato',
        'ready_text': ('L\'issue è implementabile in una RUN: l\'implementazione può '
                       'partire.'),
        'not_ready_title': '## Controllo di prontezza: issue non pronta',
        'not_ready_text': ('L\'implementer non parte finché le lacune qui sotto non sono '
                           'risolte.'),
        'gaps': '### Lacune',
        'gap_base': '  *Dipende dal branch base:* il controllo si ripete quando avanza.',
        'gap_refs': '  *Riferimenti:* ',
        'gap_default': '  *Default proposto:* {default}',
        'gap_prerequisite': '  *Prerequisito mancante:* {text}',
        'gap_prerequisite_number': '  *Prerequisito mancante:* {text} (#{number})',
        'prerequisites_resume': (
            'devloop ha aggiunto a questa issue il "blocked by" nativo {issues}, senza '
            '`agent:needs-operator`: l\'issue esce dalla coda e il controllo si ripete quando '
            'sono tutte chiuse. Chi trova la dipendenza sbagliata la rimuove.'),
        'split': '### Suddivisione proposta',
        'no_gaps': 'Il controllo non ha indicato lacune né una suddivisione.',
        'trust_covered': ('**Policy di fiducia:** toccata, coperta da una decisione '
                          'dell\'operatore.'),
        'recheck_command': 'Il comando `/devloop recheck`',
        'recheck_relabeled': '`agent:ready` rimesso',
        'recheck_title': '## Controllo di prontezza non ripetuto',
        'recheck_text': ('{what} di `{by}` non ripete il controllo di prontezza: {reason}.\n\n'
                         'Il blocco resta, e si risolve come dice il suo commento. Quando '
                         'l\'ultimo blocco dell\'issue torna a essere quello del controllo di '
                         'prontezza, un nuovo `/devloop recheck` lo ripete.'),
        'why_both_names': ('`agent:needs-operator` è presente con entrambi i suoi nomi, e quale '
                           'dei due sia del controllo non si può dire'),
        'why_not_set': ('l\'ultimo controllo di prontezza non ha messo `agent:needs-operator`, '
                        'che era già presente'),
        'why_escalation': ('dopo il controllo di prontezza c\'è un\'escalation di classe {cls} '
                           '({described}): l\'ultimo blocco dell\'issue è quello, non il '
                           'controllo di prontezza'),
        'why_no_class': 'non indicata',
        'why_added_again': ('`agent:needs-operator` è stato aggiunto di nuovo dopo il controllo '
                            'di prontezza, e non dal controllo'),
        'why_supervisor': 'l\'issue è in `{label}` e attende il supervisore',
        'why_supervisor_class': ('l\'issue è in `{label}` e attende il supervisore, per '
                                 'un\'escalation di classe `{cls}` ({described}): l\'ultimo '
                                 'blocco dell\'issue è quello, non il controllo di prontezza'),
        'note_block_removed': 'Il blocco messo dal controllo di prontezza è stato rimosso.',
        'note_label_stays': ('{labels} resta: non risulta messo dal controllo di prontezza, '
                             'quindi lo rimuove una persona.'),
        'note_not_supervised': ('In modalità di merge `auto` queste lacune andrebbero al '
                                'supervisore, ma l\'escalation resta all\'operatore: {why}.'),
        'note_label_present': ('{labels} era già presente e non è attribuito al controllo di '
                               'prontezza: un nuovo esito positivo non lo rimuove.'),
        'note_reused': ('Esito del controllo precedente, riusato: il testo dell\'issue non è '
                        'cambiato.'),
        'note_no_prerequisite_block': ('Le lacune indicano solo prerequisiti mancanti, ma devloop '
                                       'non aggiunge il loro "blocked by": {why}.'),
    })

# The longest body GitHub accepts for a comment or a review, in characters.
MAX_BODY = 65536
# The notice of a review cut short, in English (``TEXTS``: ``truncated`` in every language).
TRUNCATED = TEXTS.tables[ENGLISH]['truncated']


class BodyTooLong(GitHubError):
    """A body that does not fit ``MAX_BODY`` even with its readable part cut away: it is not
    published, and the step escalates like any failed write (#325)."""


def marker(kind, payload):
    data = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    return f'<!-- devloop:{kind}:v1 {base64.b64encode(data).decode()} -->'


def neutralize(text):
    """Text that cannot open an HTML comment, and therefore cannot carry a marker."""
    return (text or '').replace('<!--', '&lt;!--')


def publishable(body, kind=None, payload=None, records=()):
    """The only way devloop builds a body for GitHub: neutralized text plus, optionally, the
    controller's marker as the last line, or its markers as the last lines: ``kind`` with
    ``payload``, then each ``(kind, payload)`` of ``records``, one per line (#338)."""
    body = neutralize(body).rstrip('\n') + '\n'
    pairs = ([(kind, payload)] if kind is not None else []) + list(records)
    if pairs:
        body += '\n' + ''.join(marker(k, p) + '\n' for k, p in pairs)
    return body


def split_markers(text):
    """``(text, markers)``: ``text`` without the block of marker lines that ends it, and the
    lines of that block, in order. Only that block carries records: a marker anywhere else is
    text."""
    lines = (text or '').strip().splitlines()
    end = len(lines)
    while end and _MARKER.fullmatch(lines[end - 1].strip()):
        end -= 1
    return '\n'.join(lines[:end]).rstrip(), [line.strip() for line in lines[end:]]


_ATTRIBUTION_LINE = re.compile(r'>\s*(?:\*\*(?:Bot|Version|Agent|Model|Reasoning Level):\*\*.*)?')
_FENCE = re.compile(r'\s{0,3}(`{3,}|~{3,})')


def without_attribution(text):
    """``text`` as the reviewers read it (#389): without the attribution blocks of AGENTS.md
    (a ``> [!NOTE]`` followed only by Bot, Version, Agent, Model and Reasoning Level lines),
    wherever they are outside a fenced code block, and without the block of markers that ends
    it, which records who wrote the change. Every other line stays as it is, technical text that
    names an agent or a model included: the reviewer must not learn who the author is from the
    metadata, nothing more. The original on GitHub does not change."""
    lines = split_markers(text)[0].splitlines()
    kept, fence, i = [], None, 0
    while i < len(lines):
        line = lines[i]
        if fence is None and line.strip() == '> [!NOTE]':
            end = i + 1
            while end < len(lines) and lines[end].lstrip().startswith('>'):
                end += 1
            block = [item.strip() for item in lines[i + 1:end]]
            if (any(item != '>' for item in block)
                    and all(_ATTRIBUTION_LINE.fullmatch(item) for item in block)):
                i = end
                if not kept or not kept[-1].strip():
                    while i < len(lines) and not lines[i].strip():
                        i += 1  # no blank lines left where the block was
                continue
        opening = _FENCE.match(line)
        if fence is None and opening:
            fence = opening.group(1)
        elif fence is not None and opening and line.strip().rstrip(fence[0]) == '' \
                and len(line.strip()) >= len(fence):
            fence = None
        kept.append(line)
        i += 1
    return '\n'.join(kept).strip()


def with_record(body, kind, payload, text=None):
    """``body``, a published comment, with its record of ``kind`` set to ``payload``: the marker
    of that kind replaced where it is, or added after the others, which stay as they are.
    ``text``, when given, replaces the readable text, neutralized as by ``publishable`` (#338)."""
    readable, lines = split_markers(body)
    line = marker(kind, payload)
    kept = [line if _MARKER.fullmatch(old).group('kind') == kind else old for old in lines]
    if line not in kept:
        kept.append(line)
    return (neutralize(readable if text is None else text).rstrip('\n') + '\n\n'
            + ''.join(f'{k}\n' for k in kept))


def _is_int(value, minimum=0):
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _list_of_dicts(value):
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def _valid_review(r):
    # A record published before #248 carries ``LEGACY_VERDICT``: ``read_marker`` renames it.
    return ((r.get('verdict') in _VERDICT_TEXT or r.get('verdict') == LEGACY_VERDICT)
            and _is_int(r.get('round'), 1)
            and isinstance(r.get('head_sha'), str) and isinstance(r.get('summary'), str)
            and r.get('source') in ('agent', 'ci')
            and all(_list_of_dicts(r.get(k)) for k in ('blocking_findings',
                                                        'non_blocking_findings',
                                                        'previous_findings'))
            and all(isinstance(f.get('id'), str) for f in r['blocking_findings'])
            and all(isinstance(f.get('key', ''), str)
                    for f in r['blocking_findings'] + r['previous_findings']))


def _valid_fix(f):
    """A fix of a round. One asked with ``/devloop fix`` (#195) carries ``requests``, the
    comments that asked for it, and ``from``, the head it ran on; before any review it belongs
    to round 0."""
    requests = f.get('requests', [])
    requested = bool(requests)
    return (_is_int(f.get('round'), 0 if requested else 1) and isinstance(f.get('head_sha'), str)
            and _list_of_dicts(f.get('responses')) and _list_of_dicts(requests)
            and all(isinstance(r.get('by'), str) for r in requests)
            and (not requested or isinstance(f.get('from'), str)))


def _valid_sync(s):
    return isinstance(s.get('head_sha'), str)


# The fields of the attempt of a PR marker (#384, ``ledger.attempt_marker``).
ATTEMPT_FIELDS = frozenset(('attempt', 'story', 'phase', 'revision', 'n', 'design', 'task'))


def _valid_attempt(a):
    """The attempt of a PR (#384, ADR 0004, P2-3b): its id, the phase ``(story, phase,
    revision)`` (``implementation`` and a null revision in the current flow), its number in the
    phase and the key ``(design, task)`` of a unit generated by a plan, both null or both set.
    Every field is required, the null ones included: a marker without one does not hold."""
    return (isinstance(a, dict) and ATTEMPT_FIELDS <= set(a)
            and isinstance(a['attempt'], str) and ATTEMPT_ID.fullmatch(a['attempt']) is not None
            and isinstance(a['story'], str) and STORY_ID.fullmatch(a['story']) is not None
            and a['phase'] in PHASES and _is_int(a['n'], 1)
            and (a['revision'] is None or isinstance(a['revision'], str))
            and (a['design'] is None and a['task'] is None
                 or isinstance(a['design'], str) and isinstance(a['task'], str)))


def _valid_pr(m):
    """The marker of a devloop PR. ``attempt`` (#384) is missing from the PRs opened before, or
    without ``attempt_identity``."""
    return (_is_int(m.get('issue'), 1)
            and all(isinstance(m.get(k), str) for k in ('implementer', 'reviewer'))
            and ('attempt' not in m or _valid_attempt(m['attempt'])))


def _valid_policy(p):
    return p.get('event') in EVENTS and valid_receipt(p.get('receipt'))


def _valid_closure(c):
    return (isinstance(c, dict) and isinstance(c.get('key'), str)
            and c.get('status') in ('resolved', 'withdrawn')
            and c.get('by') in ('ci', 'operator', 'coordinator')
            and all(isinstance(c.get(k), str) for k in ('author', 'reason', 'head_sha')))


def _valid_finding(f):
    closed = f.get('closed')
    return isinstance(closed, list) and bool(closed) and all(_valid_closure(c) for c in closed)


GAP_KINDS = ('operator_decision', 'default_available', 'tool_unavailable')


def _valid_gap(g):
    """A gap of a readiness check. ``repository_dependent`` (#270), true when its resolution
    depends on the content of the base branch, is missing from the records written before."""
    return (isinstance(g, dict) and g.get('kind') in GAP_KINDS
            and isinstance(g.get('description'), str) and isinstance(g.get('proposed_default'), str)
            and isinstance(g.get('code_refs'), list)
            and all(isinstance(ref, str) for ref in g['code_refs'])
            and isinstance(g.get('repository_dependent', False), bool)
            and isinstance(g.get('prerequisite', ''), str)
            and _is_int(g.get('blocked_by', 0)))


# What a readiness check declares about the trust policy (#472): the change the issue asks for
# does not touch it, a decision of the operator covers it, or none does.
PREFLIGHT_TRUST = ('none', 'covered', 'uncovered')


def _valid_preflight(p):
    """A readiness check (#114): ``ready`` never with ``blocked``, which records that the check
    added ``agent:needs-operator`` after ``labeled`` earlier ``labeled`` events of that label
    (#248: of the label under both its names). ``base`` (#270), the commit of the base branch
    the check ran on, is missing from the records written before, and so are ``trust_policy``
    and ``trust_policy_reason`` (#472) from those written before them; ``uncovered`` is never
    ``ready``. ``refused`` (#483): the ids of the comments with a ``/devloop approve`` the
    check answered as refused, missing when it answered none. ``prerequisites`` (#672): the
    issues the dispatcher added as "blocked by" in place of the block, on a failed check only,
    never with ``blocked``; missing when it added none."""
    trust = p.get('trust_policy', 'none')
    added = p.get('prerequisites', [])
    return (all(isinstance(p.get(k), str) for k in ('hash', 'agent', 'split'))
            and isinstance(added, list) and all(_is_int(n, 1) for n in added)
            and not (added and (p.get('ready') or p.get('blocked')))
            and _comment_ids(p.get('refused', [0]))
            and isinstance(p.get('base', ''), str)
            and trust in PREFLIGHT_TRUST and isinstance(p.get('trust_policy_reason', ''), str)
            and not (trust == 'uncovered' and p.get('ready'))
            and all(isinstance(p.get(k), bool) for k in ('ready', 'blocked'))
            and not (p['ready'] and p['blocked']) and _is_int(p.get('labeled'))
            and isinstance(p.get('gaps'), list) and all(_valid_gap(g) for g in p['gaps']))


NOTICES = ('escalation', 'blocked', 'operator_merge')


def _valid_notice(n):
    """A text devloop publishes without a record of its own (#114): an escalation to the
    operator, or an agent that stopped. Its marker tells it apart from the operator's own text
    when both are written with the same login. Since #182 it carries the escalation's
    ``class``; a notice without one, or with one that is not known, is critical
    (``supervision.critical``). Since #248 also a ready PR whose merge is the operator's
    (``operator_merge``): its head, how many ``labeled`` events of ``agent:needs-operator``
    (under both its names) came before the dispatcher added it, and the promotion it belongs
    to, as the count of promotions recorded on the PR. Since #673 its ``released``, once the
    dispatcher removed its label because the configuration lets devloop merge the PR: the merge
    mode of that moment."""
    if n.get('event') == 'operator_merge':
        return (isinstance(n.get('head'), str) and _is_int(n.get('labeled'))
                and _is_int(n.get('promotion'))
                and n.get('released', 'strict') in ('strict', 'auto'))
    return n.get('event') in NOTICES


LEDGER_TYPES = ('request', 'turn', 'event', 'verification', 'round', 'fix')
TURN_STATES = ('open', 'closed', 'incomplete')
# The records a round's register holds, each in a section of its own (#325).
ROUND_RECORDS = ('turn', 'event')
# The records a fix register holds (#355): its turns, the sync before it, its local tests.
FIX_RECORDS = ('turn', 'event', 'verification')


def _valid_section(s, types):
    return (isinstance(s, dict) and s.get('type') in types
            and _is_int(s.get('section'), 1) and _valid_ledger(s))


def _valid_ledger(r):
    """A dispatcher record of evidence and telemetry (#133). A turn that is not closed has no
    end, outcome nor telemetry: they are null, never guessed. A ``round`` (#325) is the
    register of a review round: in ``records`` its review turns and its CI event, each with the
    number of its ``section``, all different. A ``fix`` (#355) is the register of a fix, of
    round 0 for one asked before any review: in ``records`` its turns, the ``sync`` event before
    it and its verifications, numbered the same way."""
    if r.get('type') not in LEDGER_TYPES:
        return False
    if r['type'] in ('round', 'fix'):
        records = r.get('records')
        types = ROUND_RECORDS if r['type'] == 'round' else FIX_RECORDS
        return (_is_int(r.get('round'), 1 if r['type'] == 'round' else 0)
                and isinstance(records, list)
                and all(_valid_section(s, types) for s in records)
                and len({s['section'] for s in records}) == len(records))
    if r['type'] == 'turn':
        # ``attempt`` (#384): missing from the turns of no attempt, as before.
        return (r.get('state') in TURN_STATES
                and all(isinstance(r.get(k), str) for k in ('kind', 'agent', 'started_at'))
                and ('attempt' not in r or isinstance(r['attempt'], str)
                     and ATTEMPT_ID.fullmatch(r['attempt']) is not None)
                and (r['state'] == 'closed'
                     or all(r.get(k) is None for k in ('ended_at', 'outcome', 'telemetry'))))
    if r['type'] == 'request':
        return isinstance(r.get('hash'), str) and isinstance(r.get('comments'), list)
    if r['type'] == 'verification':
        # ``retried`` (#598): the failed tests run again alone, only on such a retry.
        return (isinstance(r.get('command'), str) and r.get('outcome') in ('pass', 'fail')
                and ('retried' not in r or isinstance(r['retried'], list)
                     and all(isinstance(c, str) for c in r['retried'])))
    return isinstance(r.get('event'), str)


def _valid_merged(m):
    """A merge devloop made (#172): the merged head, its receipt, level and merge mode, and in
    ``merged_by`` the login of the merger's app when it merged (#201)."""
    return (isinstance(m.get('head'), str) and m.get('mode') in ('strict', 'auto')
            and _is_int(m.get('level')) and valid_receipt(m.get('receipt'))
            and isinstance(m.get('merged_by', ''), str))


def _valid_closed(c):
    """An issue the dispatcher closed after merging its PR (#228): the PR, the merged head, the
    merge commit (None when it could not be read) and the merge mode."""
    return (_is_int(c.get('pr')) and isinstance(c.get('head'), str)
            and isinstance(c.get('merge_commit'), (str, type(None)))
            and c.get('mode') in ('strict', 'auto'))


def _valid_hold(h):
    """A hold or release command the dispatcher applied (#172), with its author and reason,
    and in ``comment`` the id of the comment that carried it (the commands it spends)."""
    return (h.get('event') in ('hold', 'release')
            and all(isinstance(h.get(k), str) for k in ('by', 'reason')))


def _valid_supervision(s):
    """A supervisor command the dispatcher handled (#182): the command, its author, the outcome
    and in ``comment`` the id of the comment that carried it (the commands it spends). A
    ``decide`` (#258) that was applied carries its text in ``reason`` and the gaps it answers.
    A refused command of the coordinator's identity may be outside ``COMMANDS`` (#528,
    ``recorded_command``)."""
    lists = [s.get('defaults', []), s.get('gaps', [])]
    return (recorded_command(s.get('command'), s.get('outcome')) and s.get('outcome') in OUTCOMES
            and isinstance(s.get('by'), str) and isinstance(s.get('reason', ''), str)
            and (s['outcome'] != 'decided'
                 or (s['command'] == 'decide' and bool(s.get('reason', '').strip())))
            and all(isinstance(items, list) for items in lists)
            and all(isinstance(d, dict) and isinstance(d.get('description'), str)
                    and isinstance(d.get('proposed_default'), str)
                    for items in lists for d in items))


def _valid_rerun(r):
    """A re-run of the failed CI jobs of a head (#200): the head, the GitHub Actions runs
    re-run, the failed test modules (none pertinent to the PR), and in ``checks`` the links of
    the failed checks at the time, which tell when the re-run has replaced them."""
    return (isinstance(r.get('head'), str)
            and all(isinstance(r.get(k), list) for k in ('runs', 'modules', 'checks'))
            and all(_is_int(run, 1) for run in r['runs'])
            and all(isinstance(v, str) for k in ('modules', 'checks') for v in r[k]))


def _valid_cycle(c):
    """A dependency cycle the dispatcher reported on an issue of the queue (#277): the issues of
    the cycle, as ``#N`` (``owner/name#N`` for another repository), sorted."""
    cycle = c.get('cycle')
    return (isinstance(cycle, list) and bool(cycle)
            and all(isinstance(item, str) and item for item in cycle))


# The steps whose missing prerequisite becomes a native block of the dispatcher (#672).
PREREQUISITE_STEPS = ('preflight', 'implement')


def _valid_prerequisite(p):
    """The "blocked by" the dispatcher added to an issue for a missing prerequisite (#672): the
    step that named it (``preflight`` or ``implement``) and the numbers of the issues of the
    same repository it added as blocks, at least one. Only a block this record names counts as
    trusted when the dispatcher added it (``Dispatcher._dependency_edges``)."""
    added = p.get('added')
    return (p.get('step') in PREREQUISITE_STEPS and isinstance(added, list) and bool(added)
            and all(_is_int(n, 1) for n in added))


RECHECK_REQUESTS = ('recheck', 'relabeled')


def _valid_recheck(r):
    """The dispatcher's answer to a request of a person for a new readiness check that does not
    start one (#468): the request (``/devloop recheck`` or ``agent:ready`` added again), its
    author, why no check runs and, in ``labeled``, the time of the ``agent:ready`` it answers
    too, if any."""
    return (r.get('request') in RECHECK_REQUESTS and isinstance(r.get('by'), str)
            and isinstance(r.get('reason'), str) and isinstance(r.get('labeled', ''), str))


def _comment_ids(ids):
    """A non-empty list of comment ids."""
    return isinstance(ids, list) and bool(ids) and all(_is_int(i) for i in ids)


def _valid_approve(a):
    """The dispatcher's answer to the ``/devloop approve`` it refuses outside a readiness check
    (#483): the ids of the comments it answers, each once."""
    return _comment_ids(a.get('comments'))


def _valid_authorize(a):
    """The dispatcher's record of a ``/devloop authorize`` (#607): the comment of the command,
    its author and time, the outcome, the priority ('' without one) and, for an authorization,
    the SHA-256 of the title and the body it is bound to; for a refusal, why."""
    authorized = a.get('outcome') == 'authorized'
    return (a.get('outcome') in AUTHORIZE_OUTCOMES and _is_int(a.get('comment'), 1)
            and isinstance(a.get('by'), str) and isinstance(a.get('at'), str)
            and a.get('priority') in ('', *PRIORITIES)
            and isinstance(a.get('reason', ''), str)
            and (isinstance(a.get('hash'), str) and _SHA256.fullmatch(a['hash']) is not None
                 if authorized else 'hash' not in a))


RECEIPT_STATES = ('current', 'superseded')


def _valid_pr_receipt(r):
    """The receipt of a PR promoted to ``agent:ready-to-merge`` (#135, ``receipt.record``): its
    identity (PR, issue, approved head, base, policy version, level, number of the promotion,
    digest of the content), its totals, the data it declares missing, and whether it is still
    ``current`` or ``superseded`` by a revocation or a later promotion."""
    return (_is_int(r.get('pr'), 1) and _is_int(r.get('issue'), 1)
            and all(isinstance(r.get(k), str) and r[k] for k in ('head', 'base', 'policy',
                                                                  'digest'))
            and _is_int(r.get('level')) and _is_int(r.get('promotion'), 1)
            and isinstance(r.get('totals'), dict) and isinstance(r.get('missing'), list)
            and all(isinstance(m, str) for m in r['missing'])
            and r.get('state') in RECEIPT_STATES)


_DAY = re.compile(r'\d{4}-\d{2}-\d{2}')


def _valid_report(r):
    """The daily summary of ``devloop report`` (#256): ``today`` in the body of the report issue,
    for the day it shows, or ``archive`` in the comment that archives a closed day, with ``open``
    true when a turn of that day was still open."""
    return (r.get('event') in ('today', 'archive') and isinstance(r.get('day'), str)
            and _DAY.fullmatch(r['day']) is not None
            and isinstance(r.get('open', False), bool))


def _valid_pin(p):
    """An auto-pin that did not move ``current`` (#453), in the alert on ``[run].alert_issue``:
    the commit of ``ref`` it resolved, when (``at``, ISO 8601 UTC) and why, as the alert shows it
    (``reason``: the step of the smoke test, or the first line of the failed check, without
    absolute paths). The daily summary reads it."""
    return (isinstance(p.get('sha'), str) and _SHA.fullmatch(p['sha']) is not None
            and isinstance(p.get('ref'), str) and bool(p['ref'])
            and isinstance(p.get('reason'), str)
            and isinstance(p.get('at'), str) and _DAY.match(p['at']) is not None)


def _valid_conclave(c):
    """The conclave on a block (#141): its structure is checked by ``conclave.valid_record``,
    imported here because that module builds its comments with this one."""
    from .conclave import valid_record
    return valid_record(c)


# The non-binding opinions of an advisory review (#369).
ADVISORY_OPINIONS = ('ok', 'dubbi', 'contrario')


def _valid_advisory(a):
    """The advisory review of a head (#369): in the native review of the advisory agent, and in
    the dispatcher's summary for the operator. ``opinion`` is one of ``ADVISORY_OPINIONS`` when
    the review was written (``done``), null when it failed."""
    return (isinstance(a.get('head'), str) and bool(a['head'])
            and isinstance(a.get('reviewer'), str) and bool(a['reviewer'])
            and (a.get('outcome') == 'done' and a.get('opinion') in ADVISORY_OPINIONS
                 or a.get('outcome') == 'failed' and a.get('opinion') is None))


def _valid_story(s):
    """The story of an issue (#363, ADR 0004, P2-3a): its id, the repository and number of its
    root, and the issue that carries it. On the root, in the comment of its ``request``
    record, also the hashes of the root's ``request`` records, oldest first; a child issue has
    none of its own."""
    root = s.get('issue') == s.get('root')
    hashes = s.get('hashes')
    return (isinstance(s.get('story'), str) and STORY_ID.fullmatch(s['story']) is not None
            and isinstance(s.get('repo'), str) and bool(s['repo'])
            and _is_int(s.get('root'), 1) and _is_int(s.get('issue'), 1)
            and (isinstance(hashes, list) and bool(hashes)
                 and all(isinstance(h, str) for h in hashes) if root else hashes is None))


BASE_BROKEN_STEPS = ('implement', 'fix')


def _valid_basebroken(b):
    """A step whose local tests failed only with tests that fail on the base commit of its
    branch too (#459): the step (``implement`` or ``fix``), that commit (``base``), the head it
    started from (``head``: the PR's, or the base's for an implementation), how many tests
    failed (``count``) and the first of them (``cases``). The step waits for the base branch to
    move past ``base``."""
    return (b.get('step') in BASE_BROKEN_STEPS
            and isinstance(b.get('base'), str) and _SHA.fullmatch(b['base']) is not None
            and isinstance(b.get('head'), str) and _is_int(b.get('count'), 1)
            and isinstance(b.get('cases'), list) and bool(b['cases'])
            and all(isinstance(c, str) for c in b['cases']))


_VALID = {'review': _valid_review, 'fix': _valid_fix, 'pr': _valid_pr, 'policy': _valid_policy,
          'finding': _valid_finding, 'sync': _valid_sync, 'preflight': _valid_preflight,
          'notice': _valid_notice, 'ledger': _valid_ledger, 'merged': _valid_merged,
          'hold': _valid_hold, 'supervision': _valid_supervision, 'rerun': _valid_rerun,
          'closed': _valid_closed, 'cycle': _valid_cycle, 'recheck': _valid_recheck,
          'report': _valid_report,
          'receipt': _valid_pr_receipt, 'conclave': _valid_conclave,
          'advisory': _valid_advisory, 'story': _valid_story, 'pin': _valid_pin,
          'basebroken': _valid_basebroken, 'approve': _valid_approve,
          'authorize': _valid_authorize, 'prerequisite': _valid_prerequisite}


def read_marker(text, kind):
    """The record of ``kind`` carried by ``text``, or None.

    Only the block of marker lines that ends the text counts, so a marker quoted or embedded in
    the text is ignored. Usually the block is the last line alone; the comment of a promotion
    carries its ``policy`` and ``receipt`` records there, and after a merge its ``merged`` one
    (#338). Two markers of ``kind`` in the block are no record.
    """
    found = [match for match in map(_MARKER.fullmatch, split_markers(text)[1])
             if match.group('kind') == kind]
    if len(found) != 1:
        return None
    match = found[0]
    try:
        data = json.loads(base64.b64decode(match.group('payload'), validate=True))
    except (ValueError, json.JSONDecodeError):
        return None
    if not (isinstance(data, dict) and _VALID[kind](data)):
        return None
    if kind == 'review':
        # A verdict published before #248 is read under its current name.
        data['verdict'] = current_verdict(data['verdict'])
    return data


def carries_record(text):
    """Whether ``text`` ends with a valid devloop marker, of any kind."""
    return any(read_marker(text, kind) is not None for kind in _VALID)


# The verdicts of a review record; their texts are ``verdict_<verdict>`` of ``TEXTS``.
# #481: ``unproven``, a request for changes whose blocking findings all lacked evidence.
_VERDICT_TEXT = ('approve', 'request_changes', 'needs_operator', 'unproven')


def _location(finding):
    if not finding['file']:
        return ''
    line = f':{finding["line"]}' if finding['line'] > 0 else ''
    return f' `{finding["file"]}{line}`'


def _findings(t, findings):
    lines = []
    for f in findings:
        # A finding the reviewer marked as one of security (#405, ADR 0005, D5).
        tag = t('security') if f.get('security') is True else ''
        # A blocking finding without evidence, recorded as non-blocking (#481).
        if f.get('without_evidence') is True:
            tag += t('without_evidence')
        lines.append(f'- **{f.get("key") or f["id"]}**{_location(f)}: {f["title"]}{tag}')
        if f['detail']:
            lines.append(f'  {f["detail"]}'.replace('\n', '\n  '))
        if isinstance(f.get('evidence'), str) and f['evidence'].strip():
            lines.append(f'  {t("evidence")} {f["evidence"].strip()}'.replace('\n', '\n  '))
        if f['suggested_fix']:
            lines.append(f'  {t("suggested_fix")} {f["suggested_fix"]}'.replace('\n', '\n  '))
    return '\n'.join(lines)


def render_review(review, head, native=None, language=ENGLISH):
    """Markdown body (after the header) for a normalized review record. ``native``: the event
    of the native review that carries it, stated for a ``needs_operator`` verdict (#418).
    ``language``: the project's (#632), of the readable text; the record is the same."""
    t = TEXTS.texts(language)
    trust = trust_policy(review)
    # A ``needs_operator`` for the trust policy (#434).
    verdict = t('verdict_trust') if trust else t(f'verdict_{review["verdict"]}')
    title = 'findings_only_title' if findings_only(review) else 'review_title'
    parts = [t(title, round=review['round'], max_rounds=review['max_rounds']), '',
             t('verdict', verdict=verdict),
             f'**Commit:** `{review["head_sha"][:12]}` · **CI:** {review["ci"]}']
    receipt = review.get('receipt')
    if valid_receipt(receipt):
        parts[-1] += '  '
        parts.append(t('review_level', level=receipt['level'], base=receipt['base'][:12],
                       policy=receipt['policy'][:12]))
    if review.get('note'):
        parts += ['', f'> {review["note"]}']
    # #160: the base advanced during the round.
    if review.get('bases'):
        spans = ', '.join(f'`{c["from"][:12]}` → `{c["to"][:12]}`' for c in review['bases'])
        parts += ['', t('bases', spans=spans)]
    if review.get('restarts'):
        parts += ['', t('restarts', round=review['restarts']['round'])]
    # #235: the approved head was synced with the base, without conflicts.
    # #252: the same head again, after the base touching the PR revoked its approval.
    # #458: the approved head was synced with the base, with conflicts the implementer resolved.
    if review.get('resync'):
        reason = review['resync'].get('reason')
        why = t('resync_base_changed' if reason == 'base_changed'
                else 'resync_conflicts' if reason == 'resync_conflicts' else 'resync_clean')
        counts = t('resync_counts_conflicts' if reason == 'resync_conflicts'
                   else 'resync_counts')
        parts += ['', t('resync', round=review['resync']['round'],
                        sha=review['resync']['from'][:12], why=why, counts=counts)]
    parts += ['', review['summary'].strip()]
    if review['verdict'] == 'needs_operator' and review['human_reason']:
        title = t('trust_touched') if trust else t('decision_required')
        parts += ['', f'### {title}', '', review['human_reason'].strip()]
    if trust:
        # #434: the loop goes on; only the merge is the operator's.
        parts += ['', t('trust_note')]
    if review['verdict'] == 'needs_operator' and native:
        outcome = t('native_open' if native == 'REQUEST_CHANGES' else 'native_none')
        wait = t('native_wait_trust' if trust else 'native_wait')
        parts += ['', t('native', native=native, outcome=outcome, wait=wait)]
    if review['blocking_findings']:
        parts += ['', t('blocking'), '', _findings(t, review['blocking_findings'])]
    if review['non_blocking_findings']:
        parts += ['', t('non_blocking'), '', _findings(t, review['non_blocking_findings'])]
    if review['previous_findings']:
        parts += ['', t('previous'), '', t('previous_table'), '|---|---|---|']
        for p in review['previous_findings']:
            note = p['note'].replace('|', '\\|').replace('\n', ' ')
            parts.append(f'| {p.get("key") or p["id"]} | {t("status_" + p["status"])} | '
                         f'{note} |')
    if review['checks_performed']:
        parts += ['', t('checks'), '']
        parts += [f'- {c}' for c in review['checks_performed']]
        parts += ['', '</details>']
    return fitted(head, '\n'.join(parts), 'review', review, language=language)


def fitted(head, text, kind, payload, limit=MAX_BODY, language=ENGLISH):
    """``publishable(head + text)`` with its marker, within ``limit`` characters (#325). A
    longer body keeps the header and the marker whole and cuts only ``text``, the readable part,
    followed by the notice ``truncated`` of ``TEXTS`` in ``language``. When header, notice and
    marker alone exceed ``limit`` it raises ``BodyTooLong``: nothing of the record is dropped,
    and nothing else is published."""
    body = publishable(head + '\n' + text, kind, payload)
    if len(body) <= limit:
        return body
    top = neutralize(head).rstrip('\n') + '\n'
    tail = '\n\n' + TEXTS.texts(language)('truncated') + '\n\n' + marker(kind, payload) + '\n'
    room = limit - len(top) - len(tail)
    if room < 0:
        raise BodyTooLong(f'the {kind} record does not fit the {limit} characters of a GitHub '
                          f'text even without its readable part ({len(top) + len(tail)} '
                          'characters with header, notice and marker): not published')
    # A prefix of neutralized text cannot open an HTML comment either.
    return top + neutralize(text)[:room].rstrip('\n') + tail


# ``unproven`` (#481) neither approves nor asks for changes: ``COMMENT``.
_REVIEW_EVENT = {'approve': 'APPROVE', 'request_changes': 'REQUEST_CHANGES',
                 'unproven': 'COMMENT'}


def native_event(review):
    """The event of the native review that carries ``review``. ``approve`` and
    ``request_changes`` mirror the verdict, and ``unproven`` (#481), which does neither, is a
    ``COMMENT``. ``needs_operator`` mirrors the reviewer's technical
    outcome (#418): ``REQUEST_CHANGES`` while blocking findings of its own stay open, otherwise
    ``APPROVE``, so that its earlier ``REQUEST_CHANGES`` does not block the merge after the
    operator's decision. The body still states the verdict, and devloop keeps the PR with the
    operator: it reads the verdict from the record, never from the event. No review is ever
    dismissed. A findings-only review (#416) is always a ``COMMENT``: its approval counts
    nowhere, and its findings are closed by the quorum, not by a later review of its own."""
    verdict = review['verdict']
    if findings_only(review):
        # #416: neither an approval nor a request for changes GitHub would count.
        return 'COMMENT'
    if verdict == 'needs_operator':
        return 'REQUEST_CHANGES' if own_open_findings(review) else 'APPROVE'
    return _REVIEW_EVENT[verdict]


def render_native_review(review, head, language=ENGLISH):
    """``{event, body}`` of the GitHub review of an agent with an app of its own (#325): the
    whole review with its marker (``render_review``), the record devloop reads, and the event
    of ``native_event`` for GitHub and branch protection."""
    event = native_event(review)
    return {'event': event,
            'body': render_review(review, head, native=event, language=language)}


def render_fix(fix, head, round_no, language=ENGLISH):
    """The comment of a fix, its readable text in ``language`` (#632) and its ``fix`` record."""
    t = TEXTS.texts(language)
    requests = fix.get('requests') or []
    if requests:
        # A fix asked with /devloop fix (#195): it cites the comments that asked for it.
        parts = [t('fix_requested_title', sha=fix['from'][:12]), '']
        for r in requests:
            cited = (t('comment_id', comment=r['comment']) if r.get('comment') is not None
                     else t('comment'))
            parts += [t('fix_request', by=r['by'], cited=cited), '',
                      '\n'.join('> ' + line for line in r['request'].splitlines()), '']
        parts.append(fix['summary'].strip())
    else:
        parts = [t('fix_title', round=round_no), '', fix['summary'].strip()]
    merged = fix.get('base_merge')
    if merged:
        # The base merged into the branch before the fix (#250), pushed with it.
        parts += ['', t('base_merge', branch=merged['branch'], base=merged['base'][:12],
                        head=merged['head'][:12])]
    if fix['responses']:
        parts += ['', t('responses_table'), '|---|---|---|']
        for r in fix['responses']:
            note = r['note'].replace('|', '\\|').replace('\n', ' ')
            parts.append(f'| {r["finding_id"]} | {t("action_" + r["action"])} | {note} |')
    payload = {'round': round_no, 'head_sha': fix['head_sha'], 'responses': fix['responses']}
    if requests:
        payload['requests'] = [{'comment': r.get('comment'), 'by': r['by'],
                                'request': r['request']} for r in requests]
        payload['from'] = fix['from']
    if 'acceptance_map' in fix:
        # The latest map of the PR from now on (#134).
        parts += ['', render_map(fix['acceptance_map'], language)]
        payload['acceptance_map'] = fix['acceptance_map']
    if 'host_step' in fix:
        # The step on the host of the PR from now on (#462), '' when there is none.
        step = (fix['host_step'] or '').strip() if isinstance(fix['host_step'], str) else ''
        if step:
            parts += ['', render_step(step, language)]
        payload['host_step'] = step
    if 'profile' in fix:
        payload['profile'] = fix['profile']  # who wrote the fix (#111)
    if 'local_tests' in fix:
        payload['local_tests'] = fix['local_tests']  # informative only (#169)
    if fix.get('skipped_models'):
        payload['skipped_models'] = fix['skipped_models']  # informative only (#151)
    if fix.get('generated_files'):
        # The generated files of the fix's commit, pointed out (#627): informative only.
        parts += ['', render_generated(fix['generated_files'], language)]
        payload.update(generated_record(fix['generated_files']))
    if fix.get('conclave') is not None:
        # The conclave decision whose resolution the fix had (#142).
        parts += ['', t('fix_conclave', comment=fix['conclave'])]
        payload['conclave'] = fix['conclave']
    return publishable(head + '\n' + '\n'.join(parts), 'fix', payload)


def preflight_resume(language=ENGLISH):
    """What resumes an issue the readiness check blocked, in ``language`` (#632)."""
    return TEXTS.texts(language)('preflight_resume')


def _refused_lines(t, refused):
    """The lines that answer the ``/devloop approve`` refused (#483), ``(login, time,
    reason)``."""
    return [t('refused_intro'), '',
            *(t('refused_line', login=login, at=at or t('unknown_time'), reason=reason)
              for login, at, reason in refused)]


def render_approve_refused(ids, refused, language=ENGLISH):
    """The dispatcher's answer to the ``/devloop approve`` it refuses when no readiness check
    answers them (#483), with an ``approve`` marker that names the comments ``ids``: each is
    answered once. ``refused``: ``(login, time, reason)``, as ``render_preflight`` shows them.
    ``language``: the project's (#632), of the readable text."""
    t = TEXTS.texts(language)
    return publishable(
        dispatcher_header() + f'\n## {t("refused_title")}\n\n'
        + '\n'.join(_refused_lines(t, refused)) + '\n\n' + t('approve_rule') + '\n',
        'approve', {'comments': list(ids)})


def render_preflight(record, head, note='', refused=(), language=ENGLISH):
    """The comment of a readiness check (#114): ``record`` is its ``preflight`` marker.
    ``refused``: the ``/devloop approve`` the check did not count as decisions of the operator
    (#483), as ``(login, time, reason)``, answered here; they are not part of the record.
    ``language``: the project's (#632), of the readable text; the record is the same."""
    t = TEXTS.texts(language)
    if record['ready']:
        parts = [t('ready_title'), '', t('ready_text')]
    else:
        parts = [t('not_ready_title'), '', t('not_ready_text')]
        if record['gaps']:
            parts += ['', t('gaps'), '']
            for gap in record['gaps']:
                parts.append(f'- **{t("gap_" + gap["kind"])}:** {gap["description"].strip()}'
                             .replace('\n', '\n  '))
                if gap.get('repository_dependent'):
                    parts.append(t('gap_base'))
                if gap['code_refs']:
                    parts.append(t('gap_refs') + ', '.join(f'`{ref}`'
                                                           for ref in gap['code_refs']))
                if gap['proposed_default'].strip():
                    parts.append(t('gap_default', default=gap['proposed_default'].strip())
                                 .replace('\n', '\n  '))
                # #672: the prerequisite the gap waits for, with the number of its issue if known.
                text = ' '.join(str(gap.get('prerequisite') or '').split())
                if text:
                    number = gap.get('blocked_by')
                    parts.append(t('gap_prerequisite_number', text=text, number=number)
                                 if _is_int(number, 1) else t('gap_prerequisite', text=text))
        if record['split'].strip():
            parts += ['', t('split'), '', record['split'].strip()]
        if not record['gaps'] and not record['split'].strip():
            parts += ['', t('no_gaps')]
        added = record.get('prerequisites')
        parts += ['', t('prerequisites_resume',
                        issues=', '.join(f'#{n}' for n in added)) if added
                  else t('preflight_resume')]
    if record.get('trust_policy') == 'covered':
        # #472: the decision of the operator the check found for the trust policy, shown.
        reason = record.get('trust_policy_reason', '').strip()
        parts += ['', t('trust_covered') + (f' {reason}' if reason else '')]
    if refused:
        parts += ['', f'### {t("refused_title")}', '', *_refused_lines(t, refused)]
    if note:
        parts += ['', f'> {note}']
    return publishable(head + '\n' + '\n'.join(parts), 'preflight', record)


# The frame of an escalation and of the dispatcher's answers to the commands on it (#633), in the
# project's ``language`` (``i18n``); English for a language without translation. The reason is
# the caller's. Escalations are read only through their ``notice`` marker, never by title.
ESCALATION_TEXTS = Catalog(
    en={
        'operator_title': 'The operator is needed',
        'supervisor_title': 'Escalation to the supervisor',
        'class_line': '**Class:** `{cls}` ({described}), {severity}.',
        'critical': 'critical',
        'non_critical': 'non-critical',
        'resume': ('To resume: fix the cause, **remove `agent:needs-operator`** and set the '
                   'label of the step to run (`agent:ready` on the issue, `agent:review` or '
                   '`agent:changes` on the PR).'),
        'supervisor_resume': (
            'Non-critical escalation: the supervisor decides (merge mode `auto`). Commands, as '
            'the first line of a comment, from the identity of `[roles].supervisor` or from a '
            'trusted user:\n\n'
            '- `/devloop retry`: runs the failed step again, without counting a round (at most '
            '{retries} times per step and input, then the escalation goes to the operator);\n'
            '- `/devloop accept-defaults`: accepts the defaults proposed by the readiness check '
            'and puts the issue back in the queue;\n'
            '- `/devloop decide <decisions>`: decides the gaps of the readiness check, also '
            'differently from the defaults (the text is the rest of the comment), and puts the '
            'issue back in the queue;\n'
            '- `/devloop grant-round <reason>`: grants one more review round to a PR at the '
            'round limit (`round_grantable`), within the caps of the delegation; the reason, '
            'which may go on in the following lines, gives or cites the analysis of the '
            'causes;\n'
            '- `/devloop grant-budget <reason>`: extends once the budget envelope of the story, '
            'in turns and time only (`budget_grantable`), within the caps of the delegation; the '
            'reason, as for `grant-round`, gives or cites the analysis of the causes;\n'
            '- `/devloop escalate <reason>`: hands the escalation to the operator '
            '(`agent:needs-operator`).'),
        'operator_retry': ('Otherwise, a trusted user may run the step `{step}` again with a '
                           'comment whose first line is `/devloop retry`: devloop removes '
                           '`agent:needs-operator` and runs the step again, at most {retries} '
                           'times per step and input (the supervisor\'s retries count too).'),
        'supervision_title': 'Decision of the supervisor',
    },
    it={
        'operator_title': 'Serve un intervento dell\'operatore',
        'supervisor_title': 'Escalation al supervisore',
        'class_line': '**Classe:** `{cls}` ({described}), {severity}.',
        'critical': 'critica',
        'non_critical': 'non critica',
        'resume': ('Per riprendere: correggere la causa, **rimuovere `agent:needs-operator`** e '
                   'impostare la label del passo da eseguire (`agent:ready` sulla issue, '
                   '`agent:review` o `agent:changes` sulla PR).'),
        'supervisor_resume': (
            'Escalation non critica: decide il supervisore (modalità di merge `auto`). Comandi, '
            'come prima riga di un commento, dall\'identità di `[roles].supervisor` o da un '
            'utente fidato:\n\n'
            '- `/devloop retry`: ripete il passo fallito, senza contare un round (al massimo '
            '{retries} volte per passo e input, poi l\'escalation passa all\'operatore);\n'
            '- `/devloop accept-defaults`: accetta i default proposti dal controllo di prontezza '
            'e rimette l\'issue in coda;\n'
            '- `/devloop decide <decisioni>`: decide le lacune del controllo di prontezza anche '
            'diversamente dai default (il testo è il resto del commento) e rimette l\'issue in '
            'coda;\n'
            '- `/devloop grant-round <motivo>`: concede un round di review in più a una PR al '
            'limite dei round (`round_grantable`), entro i tetti della delega; il motivo, che può '
            'continuare nelle righe successive, riporta o cita l\'analisi delle cause;\n'
            '- `/devloop grant-budget <motivo>`: estende una volta l\'envelope di budget della '
            'storia, solo in turni e tempo (`budget_grantable`), entro i tetti della delega; il '
            'motivo, come per `grant-round`, riporta o cita l\'analisi delle cause;\n'
            '- `/devloop escalate <motivo>`: passa l\'escalation all\'operatore '
            '(`agent:needs-operator`).'),
        'operator_retry': ('In alternativa, un utente fidato può ripetere il passo `{step}` con '
                           'un commento che abbia `/devloop retry` come prima riga: devloop '
                           'toglie `agent:needs-operator` e ripete il passo, al massimo {retries} '
                           'volte per passo e input (contando anche i retry del supervisore).'),
        'supervision_title': 'Decisione del supervisore',
    })


def resume_text(language=ENGLISH):
    """How the operator resumes an escalation, in ``language`` (#633)."""
    return ESCALATION_TEXTS.texts(language)('resume')



# The mention of the operator in a request for a decision (#593), in the project's ``language``.
MENTION_TEXTS = Catalog(
    en={'mention': 'For the operator: {logins}'},
    it={'mention': 'Per l\'operatore: {logins}'})


def with_mentions(body, logins, language=ENGLISH):
    """``body``, a comment that asks the operator for a decision, with a line that mentions
    ``logins`` (``operator_mentions``, #593) at the end of its readable text, before the markers
    that end it, which stay as they are. GitHub notifies a login mentioned, on the phone too
    with GitHub Mobile. Without logins, ``body`` as it is."""
    if not logins:
        return body
    readable, markers = split_markers(body)
    line = MENTION_TEXTS.texts(language)('mention',
                                         logins=' '.join(f'@{login}' for login in logins))
    out = readable.rstrip('\n') + '\n\n' + line + '\n'
    if markers:
        out += '\n' + ''.join(f'{m}\n' for m in markers)
    return out


def operator_retry_text(step, language=ENGLISH):
    """How a trusted user runs again the step ``step`` of an escalation to the operator
    (#321), in ``language`` (#633)."""
    return ESCALATION_TEXTS.texts(language)('operator_retry', step=step, retries=MAX_RETRIES)


def render_escalation(reason, detail='', cls='failure', supervised=None, retry=None, extra=None,
                      language=ENGLISH):
    """An escalation (#182: of class ``cls``). ``supervised``, for a non-critical one the
    supervisor decides, holds the step, the key of its input and what a retry resumes; without
    it the escalation goes to the operator. ``retry``, for an escalation to the operator, holds
    the same for a step that a trusted user may run again with ``/devloop retry`` (#321).
    ``extra``: more fields of the marker, such as the conclave decision a veto annulled
    (#142). ``language``: the project's (#633), of the title, the class and how to resume; the
    ``reason`` is the caller's, the marker the same."""
    t = ESCALATION_TEXTS.texts(language)
    title = t('supervisor_title' if supervised else 'operator_title')
    resume = (t('supervisor_resume', retries=MAX_RETRIES) if supervised else t('resume'))
    if retry and not supervised:
        resume += '\n\n' + operator_retry_text(retry['step'], language)
    body = (dispatcher_header() + f'\n## {title}\n\n' + reason.strip() + '\n\n'
            + t('class_line', cls=cls, described=describe(cls, language=language),
                severity=t('non_critical' if not critical(cls) else 'critical')) + '\n\n'
            + resume + '\n')
    if detail:
        body += '\n```text\n' + detail.strip()[-6000:].replace('```', "'''") + '\n```\n'
    payload = {'event': 'escalation', 'class': cls, 'critical': critical(cls),
               'to': 'supervisor' if supervised else 'operator'}
    if supervised:
        payload.update(supervised)
    elif retry:
        payload.update(retry)
    payload.update(extra or {})
    return publishable(body, 'notice', payload)


# The dependency cycles, the coordinator's authorizations and the operator's merges, in the
# project's ``language`` (#635); English for a language without translation.
NOTICE_TEXTS = Catalog(
    en={
        'cycle_title': 'Dependency cycle',
        'cycle_text': ('The "blocked by" dependencies among {issues} form a cycle: none of these '
                       'issues starts while the cycle stays. The supervisor or the operator '
                       'removes one of the dependencies; devloop works out by itself which issues '
                       'may start, with no other action.'),
        'prerequisite_title': 'Prerequisite added as "blocked by"',
        'prerequisite_text': ('The {step} named a missing prerequisite: devloop adds {issues} '
                              'as native "blocked by" of this issue, in place of an escalation '
                              'to the operator. The issue leaves the queue and resumes once '
                              'they are all closed, without other checks. This record is what '
                              'makes these blocks of the dispatcher count; whoever finds one '
                              'wrong removes it.'),
        'step_preflight': 'readiness check',
        'step_implement': 'implementer',
        'priority': 'priority `{priority}`',
        'no_priority': 'no priority given',
        'authorized_title': 'Work authorized by the coordinator',
        'authorized_text': ('`{by}` authorized the issue with `{command}` of {at} ({priority}). '
                            'The authorization holds for the title and the body of this moment '
                            '(SHA-256 `{hash}`): if they change, it lapses and a new `{command}` '
                            'is needed. Now `{ready}` of the supervisor starts the issue; if the '
                            'readiness check declares that it touches the trust policy, only the '
                            'operator queues it.'),
        'refused_title': '`{command}` refused',
        'refused_text': 'The command of `{by}` of {at} does not authorize the issue: {reason}.',
        'operator_merge_title': 'The merge is the operator\'s',
        'operator_merge_text': ('Commit `{head}` is ready for the merge, but devloop does not '
                                'merge it:\n\n{reasons}\n\n'
                                'devloop added `{label}` and keeps following the PR: it syncs it '
                                'if it conflicts with the base, checks the CI again and, with a '
                                'new commit, revokes the readiness and sends it back to review, '
                                'removing `{label}`.'),
        'operator_release_title': 'The merge passed to devloop',
        'operator_release_text': ('With the current configuration the merge mode of this PR is '
                                  '`{mode}`: none of the reasons above is left. devloop removed '
                                  '`{label}` and merges commit `{head}` itself, once every merge '
                                  'directive holds again.'),
    },
    it={
        'cycle_title': 'Ciclo di dipendenze',
        'cycle_text': ('Le dipendenze "blocked by" fra {issues} formano un ciclo: nessuna di '
                       'queste issue parte finché il ciclo resta. Il supervisore o l\'operatore '
                       'rimuove una delle dipendenze; devloop ricalcola da solo quali issue '
                       'possono partire, senza altri interventi.'),
        'prerequisite_title': 'Prerequisito aggiunto come "blocked by"',
        'prerequisite_text': ('Il {step} ha indicato un prerequisito mancante: devloop aggiunge '
                              '{issues} come "blocked by" nativo di questa issue, al posto di '
                              'un\'escalation all\'operatore. L\'issue esce dalla coda e '
                              'riprende quando sono tutte chiuse, senza altri controlli. Questo '
                              'record fa contare questi blocchi del dispatcher; chi ne trova uno '
                              'sbagliato lo rimuove.'),
        'step_preflight': 'controllo di prontezza',
        'step_implement': 'implementer',
        'priority': 'priorità `{priority}`',
        'no_priority': 'nessuna priorità indicata',
        'authorized_title': 'Lavoro autorizzato dal coordinatore',
        'authorized_text': ('`{by}` ha autorizzato l\'issue con `{command}` del {at} '
                            '({priority}). L\'autorizzazione vale per il titolo e il corpo di '
                            'questo momento (SHA-256 `{hash}`): se cambiano, decade e serve un '
                            'nuovo `{command}`. Ora `{ready}` del supervisore fa partire '
                            'l\'issue; se il controllo di prontezza la dichiara toccare la policy '
                            'di fiducia, la mette in coda solo l\'operatore.'),
        'refused_title': '`{command}` rifiutato',
        'refused_text': ('Il comando di `{by}` del {at} non autorizza l\'issue: {reason}.'),
        'operator_merge_title': 'Il merge spetta all\'operatore',
        'operator_merge_text': ('Il commit `{head}` è pronto per il merge, ma devloop non lo '
                                'mergia:\n\n{reasons}\n\n'
                                'devloop ha aggiunto `{label}` e continua a seguire la PR: la '
                                'sincronizza se va in conflitto con la base, ricontrolla la CI e, '
                                'con un nuovo commit, revoca la prontezza e la rimanda in review, '
                                'togliendo `{label}`.'),
        'operator_release_title': 'Il merge passa a devloop',
        'operator_release_text': ('Con la configurazione attuale la modalità di merge di questa PR '
                                  'è `{mode}`: non resta nessuno dei motivi sopra. devloop ha '
                                  'tolto `{label}` e mergia da sé il commit `{head}`, quando '
                                  'tutte le direttive di merge valgono di nuovo.'),
    })


def render_cycle(record, language=ENGLISH):
    """The dispatcher's report of a dependency cycle (#277) on an issue of the queue, with a
    ``cycle`` marker: the same cycle is reported once on each issue. In ``language`` (#635)."""
    t = NOTICE_TEXTS.texts(language)
    return publishable(
        dispatcher_header() + f'\n## {t("cycle_title")}\n\n'
        + t('cycle_text', issues=', '.join(record['cycle'])) + '\n', 'cycle', record)


def render_prerequisite(record, language=ENGLISH):
    """The dispatcher's record of the "blocked by" it adds to an issue for a missing prerequisite
    (#672), with a ``prerequisite`` marker: the step that named it and the issues added. Only a
    block of the dispatcher that such a record names counts. In ``language``."""
    t = NOTICE_TEXTS.texts(language)
    return publishable(
        dispatcher_header() + f'\n## {t("prerequisite_title")}\n\n'
        + t('prerequisite_text', step=t('step_' + record['step']),
            issues=', '.join(f'#{n}' for n in record['added'])) + '\n',
        'prerequisite', record)


def render_recheck(record, language=ENGLISH):
    """The dispatcher's answer to a request for a new readiness check that starts none (#468),
    with a ``recheck`` marker: each request is answered once. ``language``: the project's
    (#632), of the readable text; the ``reason`` of the record is shown as recorded, composed in
    the same language by the dispatcher (``why_*`` of ``TEXTS``)."""
    t = TEXTS.texts(language)
    what = t('recheck_command' if record['request'] == 'recheck' else 'recheck_relabeled')
    return publishable(
        dispatcher_header() + f'\n{t("recheck_title")}\n\n'
        + t('recheck_text', what=what, by=record['by'], reason=record['reason']) + '\n',
        'recheck', record)


def render_authorize(record, ready_label='agent:ready', language=ENGLISH):
    """The dispatcher's record of a ``/devloop authorize`` (#607), with an ``authorize`` marker:
    each command is recorded once. In ``language`` (#635)."""
    t = NOTICE_TEXTS.texts(language)
    if record['outcome'] == 'authorized':
        priority = (t('priority', priority=record['priority']) if record['priority']
                    else t('no_priority'))
        text = (f'## {t("authorized_title")}\n\n'
                + t('authorized_text', by=record['by'], command=AUTHORIZE_COMMAND,
                    at=record['at'], priority=priority, hash=record['hash'][:12],
                    ready=ready_label) + '\n')
    else:
        text = (f'## {t("refused_title", command=AUTHORIZE_COMMAND)}\n\n'
                + t('refused_text', by=record['by'], at=record['at'], reason=record['reason'])
                + '\n')
    return publishable(dispatcher_header() + '\n' + text, 'authorize', record)


def render_operator_merge(head, labeled, promotion, reasons, label, language=ENGLISH):
    """The dispatcher's comment on a ready PR that devloop does not merge (#248): the merge is
    the operator's, for ``reasons`` (Markdown lines), and ``label`` stays only while the head
    ``head`` is ready. ``labeled``: how many ``labeled`` events of ``label`` came before;
    ``promotion``: how many promotions the PR's policy records count. In ``language``
    (#635)."""
    t = NOTICE_TEXTS.texts(language)
    body = (dispatcher_header() + f'\n## {t("operator_merge_title")}\n\n'
            + t('operator_merge_text', head=head[:12], reasons=reasons.strip(), label=label)
            + '\n')
    return publishable(body, 'notice', {'event': 'operator_merge', 'head': head,
                                        'labeled': labeled, 'promotion': promotion})


def render_operator_release(body, record, mode, label, language=ENGLISH):
    """``body``, the ``operator_merge`` notice of a ready PR (#248) with its ``record``, edited
    when the dispatcher removes its ``label`` because no reason is left (#673): its text, and a
    section that says the merge passed to devloop, in merge mode ``mode``; its record with
    ``released``. In ``language`` (#635)."""
    t = NOTICE_TEXTS.texts(language)
    text = (split_markers(body)[0] + f'\n\n## {t("operator_release_title")}\n\n'
            + t('operator_release_text', mode=mode, label=label, head=record['head'][:12])
            + '\n')
    return with_record(body, 'notice', dict(record, released=mode), text=text)


def render_supervision(record, text, title=None, records=(), language=ENGLISH):
    """The dispatcher's trace of a supervisor command (#182), or of a command on an escalation
    to the operator (#321, with its own ``title``): what was asked, by whom, and the outcome,
    in a ``supervision`` marker. ``records``: more ``(kind, payload)`` markers of the same
    comment, such as the ``finding`` record of the coordinator's closures (#606), written
    together with the trace that spends their command. Without ``title``, the supervisor's
    decision in ``language`` (#633); ``text`` is the caller's, in the same language."""
    if title is None:
        title = ESCALATION_TEXTS.texts(language)('supervision_title')
    return publishable(dispatcher_header() + f'\n## {title}\n\n{text.strip()}',
                       'supervision', record, records)
