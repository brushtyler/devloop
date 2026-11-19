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
from .config import INSTALL_ROOT
from .github import GitHubError
from .hoststep import render_step
from .journal import ATTEMPT_ID, PHASES, STORY_ID
from .policy import EVENTS, valid_receipt
from .review import LEGACY_VERDICT, current_verdict, own_open_findings, trust_policy
from .supervision import COMMANDS, MAX_RETRIES, OUTCOMES, critical, describe

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


# The longest body GitHub accepts for a comment or a review, in characters.
MAX_BODY = 65536
TRUNCATED = ('> **Testo troncato:** la review supera il limite di 65 536 caratteri di un testo di '
             'GitHub, e devloop ne ha accorciato solo la parte leggibile. Il record completo, '
             'con tutti i finding, è nel marker.')


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
            and c.get('status') in ('resolved', 'withdrawn') and c.get('by') in ('ci', 'operator')
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
            and isinstance(g.get('repository_dependent', False), bool))


def _valid_preflight(p):
    """A readiness check (#114): ``ready`` never with ``blocked``, which records that the check
    added ``agent:needs-operator`` after ``labeled`` earlier ``labeled`` events of that label
    (#248: of the label under both its names). ``base`` (#270), the commit of the base branch
    the check ran on, is missing from the records written before."""
    return (all(isinstance(p.get(k), str) for k in ('hash', 'agent', 'split'))
            and isinstance(p.get('base', ''), str)
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
    to, as the count of promotions recorded on the PR."""
    if n.get('event') == 'operator_merge':
        return (isinstance(n.get('head'), str) and _is_int(n.get('labeled'))
                and _is_int(n.get('promotion')))
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
        return isinstance(r.get('command'), str) and r.get('outcome') in ('pass', 'fail')
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
    ``decide`` (#258) that was applied carries its text in ``reason`` and the gaps it answers."""
    lists = [s.get('defaults', []), s.get('gaps', [])]
    return (s.get('command') in COMMANDS and s.get('outcome') in OUTCOMES
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


_VALID = {'review': _valid_review, 'fix': _valid_fix, 'pr': _valid_pr, 'policy': _valid_policy,
          'finding': _valid_finding, 'sync': _valid_sync, 'preflight': _valid_preflight,
          'notice': _valid_notice, 'ledger': _valid_ledger, 'merged': _valid_merged,
          'hold': _valid_hold, 'supervision': _valid_supervision, 'rerun': _valid_rerun,
          'closed': _valid_closed, 'cycle': _valid_cycle, 'report': _valid_report,
          'receipt': _valid_pr_receipt, 'conclave': _valid_conclave,
          'advisory': _valid_advisory, 'story': _valid_story}


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


_VERDICT_TEXT = {
    'approve': 'Approvata',
    'request_changes': 'Modifiche richieste',
    'needs_operator': 'Serve una decisione dell\'operatore',
}
# A ``needs_operator`` for the trust policy (#434).
_TRUST_TEXT = 'Policy di fiducia: review e merge dell\'operatore'
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
        # A finding the reviewer marked as one of security (#405, ADR 0005, D5).
        tag = ' *(sicurezza)*' if f.get('security') is True else ''
        lines.append(f'- **{f.get("key") or f["id"]}**{_location(f)}: {f["title"]}{tag}')
        if f['detail']:
            lines.append(f'  {f["detail"]}'.replace('\n', '\n  '))
        if f['suggested_fix']:
            lines.append(f'  *Correzione suggerita:* {f["suggested_fix"]}'.replace('\n', '\n  '))
    return '\n'.join(lines)


def render_review(review, head, native=None):
    """Markdown body (after the header) for a normalized review record. ``native``: the event
    of the native review that carries it, stated for a ``needs_operator`` verdict (#418)."""
    trust = trust_policy(review)
    verdict = _TRUST_TEXT if trust else _VERDICT_TEXT[review['verdict']]
    parts = [f'## Review automatica, round {review["round"]}/{review["max_rounds"]}', '',
             f'**Verdetto:** {verdict}  ',
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
    # #235: the approved head was synced with the base, without conflicts.
    # #252: the same head again, after the base touching the PR revoked its approval.
    if review.get('resync'):
        why = ('il dispatcher ha revocato quell\'approvazione perché la base è avanzata toccando '
               'file della PR, o senza che la non interferenza fosse dimostrabile, e questo '
               'round rivede lo stesso commit'
               if review['resync'].get('reason') == 'base_changed'
               else 'il nuovo head ne differisce solo per il merge della base senza conflitti')
        parts += ['', f'> Il round {review["resync"]["round"]} aveva approvato '
                      f'`{review["resync"]["from"][:12]}`; {why}: questo round non conta per il '
                      'limite dei round.']
    parts +=['', review['summary'].strip()]
    if review['verdict'] == 'needs_operator' and review['human_reason']:
        title = 'Policy di fiducia toccata' if trust else 'Decisione richiesta'
        parts += ['', f'### {title}', '', review['human_reason'].strip()]
    if trust:
        # #434: the loop goes on; only the merge is the operator's.
        parts += ['', '> La PR tocca la policy di fiducia: devloop non si ferma. Con finding '
                      'bloccanti aperti la PR torna all\'implementer per la correzione, come per '
                      'una richiesta di modifiche; senza, e con la CI verde, va all\'operatore '
                      'una sola volta, per la review e il merge.']
    if review['verdict'] == 'needs_operator' and native:
        outcome = ('restano aperti finding bloccanti del reviewer'
                   if native == 'REQUEST_CHANGES'
                   else 'non resta aperto nessun finding bloccante del reviewer')
        wait = ('il merge resta all\'operatore' if trust
                else 'la decisione resta all\'operatore, e devloop tiene la PR in attesa della '
                     'sua decisione')
        parts += ['', f'> Review nativa `{native}`: {outcome}. Non è un\'approvazione del merge: '
                      f'{wait}.']
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
    return fitted(head, '\n'.join(parts), 'review', review)


def fitted(head, text, kind, payload, limit=MAX_BODY):
    """``publishable(head + text)`` with its marker, within ``limit`` characters (#325). A
    longer body keeps the header and the marker whole and cuts only ``text``, the readable part,
    followed by ``TRUNCATED``. When header, notice and marker alone exceed ``limit`` it raises
    ``BodyTooLong``: nothing of the record is dropped, and nothing else is published."""
    body = publishable(head + '\n' + text, kind, payload)
    if len(body) <= limit:
        return body
    top = neutralize(head).rstrip('\n') + '\n'
    tail = '\n\n' + TRUNCATED + '\n\n' + marker(kind, payload) + '\n'
    room = limit - len(top) - len(tail)
    if room < 0:
        raise BodyTooLong(f'the {kind} record does not fit the {limit} characters of a GitHub '
                          f'text even without its readable part ({len(top) + len(tail)} '
                          'characters with header, notice and marker): not published')
    # A prefix of neutralized text cannot open an HTML comment either.
    return top + neutralize(text)[:room].rstrip('\n') + tail


_REVIEW_EVENT = {'approve': 'APPROVE', 'request_changes': 'REQUEST_CHANGES'}


def native_event(review):
    """The event of the native review that carries ``review``. ``approve`` and
    ``request_changes`` mirror the verdict. ``needs_operator`` mirrors the reviewer's technical
    outcome (#418): ``REQUEST_CHANGES`` while blocking findings of its own stay open, otherwise
    ``APPROVE``, so that its earlier ``REQUEST_CHANGES`` does not block the merge after the
    operator's decision. The body still states the verdict, and devloop keeps the PR with the
    operator: it reads the verdict from the record, never from the event. No review is ever
    dismissed."""
    verdict = review['verdict']
    if verdict == 'needs_operator':
        return 'REQUEST_CHANGES' if own_open_findings(review) else 'APPROVE'
    return _REVIEW_EVENT[verdict]


def render_native_review(review, head):
    """``{event, body}`` of the GitHub review of an agent with an app of its own (#325): the
    whole review with its marker (``render_review``), the record devloop reads, and the event
    of ``native_event`` for GitHub and branch protection."""
    event = native_event(review)
    return {'event': event, 'body': render_review(review, head, native=event)}


def render_fix(fix, head, round_no):
    requests = fix.get('requests') or []
    if requests:
        # A fix asked with /devloop fix (#195): it cites the comments that asked for it.
        parts = [f'## Correzioni richieste con `/devloop fix` su `{fix["from"][:12]}`', '']
        for r in requests:
            cited = f'commento {r["comment"]}' if r.get('comment') is not None else 'commento'
            parts += [f'Richiesta di `{r["by"]}` ({cited}):', '',
                      '\n'.join('> ' + line for line in r['request'].splitlines()), '']
        parts.append(fix['summary'].strip())
    else:
        parts = [f'## Correzioni per il round {round_no}', '', fix['summary'].strip()]
    merged = fix.get('base_merge')
    if merged:
        # The base merged into the branch before the fix (#250), pushed with it.
        parts += ['', f'Prima della correzione il dispatcher ha fatto il merge di '
                      f'`origin/{merged["branch"]}` (`{merged["base"][:12]}`) nel branch, senza '
                      f'conflitti: merge commit `{merged["head"][:12]}`, pubblicato con la '
                      'correzione. Il merge non consuma un round di review.']
    if fix['responses']:
        parts += ['', '| Finding | Azione | Nota |', '|---|---|---|']
        for r in fix['responses']:
            note = r['note'].replace('|', '\\|').replace('\n', ' ')
            parts.append(f'| {r["finding_id"]} | {_ACTION_TEXT[r["action"]]} | {note} |')
    payload = {'round': round_no, 'head_sha': fix['head_sha'], 'responses': fix['responses']}
    if requests:
        payload['requests'] = [{'comment': r.get('comment'), 'by': r['by'],
                                'request': r['request']} for r in requests]
        payload['from'] = fix['from']
    if 'acceptance_map' in fix:
        # The latest map of the PR from now on (#134).
        parts += ['', render_map(fix['acceptance_map'])]
        payload['acceptance_map'] = fix['acceptance_map']
    if 'host_step' in fix:
        # The step on the host of the PR from now on (#462), '' when there is none.
        step = (fix['host_step'] or '').strip() if isinstance(fix['host_step'], str) else ''
        if step:
            parts += ['', render_step(step)]
        payload['host_step'] = step
    if 'profile' in fix:
        payload['profile'] = fix['profile']  # who wrote the fix (#111)
    if 'local_tests' in fix:
        payload['local_tests'] = fix['local_tests']  # informative only (#169)
    if fix.get('skipped_models'):
        payload['skipped_models'] = fix['skipped_models']  # informative only (#151)
    if fix.get('conclave') is not None:
        # The conclave decision whose resolution the fix had (#142).
        parts += ['', f'La correzione ha seguito la risoluzione del conclave registrata nel '
                      f'commento {fix["conclave"]}.']
        payload['conclave'] = fix['conclave']
    return publishable(head + '\n' + '\n'.join(parts), 'fix', payload)


_GAP_TEXT = {'operator_decision': 'Decisione dell\'operatore',
             'default_available': 'Default disponibile, da accettare',
             'tool_unavailable': 'Strumento non disponibile'}
PREFLIGHT_RESUME = (
    'Per proseguire, un utente fidato risponde nella issue: decide i punti aperti o accetta i '
    'default proposti in un commento, oppure modifica titolo o corpo. Il controllo si ripete al '
    'tick successivo; per ripeterlo senza modifiche basta un commento con `/devloop recheck` '
    'come prima riga, oppure rimettere `agent:ready` (un utente fidato o il supervisore). Se '
    'una lacuna dipende dal contenuto del branch base, il controllo si ripete da solo quando il '
    'branch base avanza. Se l\'esito è positivo devloop rimuove `agent:needs-operator` che aveva '
    'messo e l\'issue torna in coda.')


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
                if gap.get('repository_dependent'):
                    parts.append('  *Dipende dal branch base:* il controllo si ripete quando '
                                 'avanza.')
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


RESUME = ('Per riprendere: correggere la causa, **rimuovere `agent:needs-operator`** e impostare la '
          'label del passo da eseguire (`agent:ready` sulla issue, `agent:review` o '
          '`agent:changes` sulla PR).')


SUPERVISOR_RESUME = (
    'Escalation non critica: decide il supervisore (modalità di merge `auto`). Comandi, come prima '
    'riga di un commento, dall\'identità di `[roles].supervisor` o da un utente fidato:\n\n'
    '- `/devloop retry`: ripete il passo fallito, senza contare un round (al massimo 2 volte per '
    'passo e input, poi l\'escalation passa all\'operatore);\n'
    '- `/devloop accept-defaults`: accetta i default proposti dal controllo di prontezza e rimette '
    'l\'issue in coda;\n'
    '- `/devloop decide <decisioni>`: decide le lacune del controllo di prontezza anche '
    'diversamente dai default (il testo è il resto del commento) e rimette l\'issue in coda;\n'
    '- `/devloop grant-round <motivo>`: concede un round di review in più a una PR al limite dei '
    'round (`round_grantable`), entro i tetti della delega; il motivo, che può continuare nelle '
    'righe successive, riporta o cita l\'analisi delle cause;\n'
    '- `/devloop escalate <motivo>`: passa l\'escalation all\'operatore (`agent:needs-operator`).')


def operator_retry_text(step):
    """How a trusted user runs again the step ``step`` of an escalation to the operator
    (#321)."""
    return (f'In alternativa, un utente fidato può ripetere il passo `{step}` con un commento che '
            'abbia `/devloop retry` come prima riga: devloop toglie `agent:needs-operator` e '
            f'ripete il passo, al massimo {MAX_RETRIES} volte per passo e input (contando anche i '
            'retry del supervisore).')


def render_escalation(reason, detail='', cls='failure', supervised=None, retry=None, extra=None):
    """An escalation (#182: of class ``cls``). ``supervised``, for a non-critical one the
    supervisor decides, holds the step, the key of its input and what a retry resumes; without
    it the escalation goes to the operator. ``retry``, for an escalation to the operator, holds
    the same for a step that a trusted user may run again with ``/devloop retry`` (#321).
    ``extra``: more fields of the marker, such as the conclave decision a veto annulled
    (#142)."""
    title = 'Escalation al supervisore' if supervised else 'Serve un intervento dell\'operatore'
    resume = SUPERVISOR_RESUME if supervised else RESUME
    if retry and not supervised:
        resume += '\n\n' + operator_retry_text(retry['step'])
    body = (dispatcher_header() + f'\n## {title}\n\n' + reason.strip() + '\n\n'
            + f'**Classe:** `{cls}` ({describe(cls)}), '
            + ('critica' if critical(cls) else 'non critica') + '.\n\n'
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


def render_cycle(record):
    """The dispatcher's report of a dependency cycle (#277) on an issue of the queue, with a
    ``cycle`` marker: the same cycle is reported once on each issue."""
    issues = ', '.join(record['cycle'])
    return publishable(
        dispatcher_header() + '\n## Ciclo di dipendenze\n\n'
        f'Le dipendenze "blocked by" fra {issues} formano un ciclo: nessuna di queste issue parte '
        'finché il ciclo resta. Il supervisore o l\'operatore rimuove una delle dipendenze; '
        'devloop ricalcola da solo quali issue possono partire, senza altri interventi.\n',
        'cycle', record)


def render_operator_merge(head, labeled, promotion, reasons, label):
    """The dispatcher's comment on a ready PR that devloop does not merge (#248): the merge is
    the operator's, for ``reasons`` (Markdown lines), and ``label`` stays only while the head
    ``head`` is ready. ``labeled``: how many ``labeled`` events of ``label`` came before;
    ``promotion``: how many promotions the PR's policy records count."""
    body = (dispatcher_header() + '\n## Il merge spetta all\'operatore\n\n'
            f'Il commit `{head[:12]}` è pronto per il merge, ma devloop non lo mergia:\n\n'
            + reasons.strip() + '\n\n'
            f'devloop ha aggiunto `{label}` e continua a seguire la PR: la sincronizza se va in '
            'conflitto con la base, ricontrolla la CI e, con un nuovo commit, revoca la prontezza '
            f'e la rimanda in review, togliendo `{label}`.\n')
    return publishable(body, 'notice', {'event': 'operator_merge', 'head': head,
                                        'labeled': labeled, 'promotion': promotion})


def render_supervision(record, text, title='Decisione del supervisore'):
    """The dispatcher's trace of a supervisor command (#182), or of a command on an escalation
    to the operator (#321, with its own ``title``): what was asked, by whom, and the outcome,
    in a ``supervision`` marker."""
    return publishable(dispatcher_header() + f'\n## {title}\n\n{text.strip()}',
                       'supervision', record)
