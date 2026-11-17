"""The texts devloop prepares for the prompts and the comments.

Moved from ``devloop/dispatcher.py`` without changes (#292, step S2 of #261): the comments of
trusted users as prompt input, the prompt templates, the sections of the documentation map, the
findings, fix requests and previous rounds of a fix or review prompt, the reasons and lists of
the dispatcher's comments, and the transition of the operator label of #248 with the label
styles it uses. ``devloop/dispatcher.py`` re-exports every name.
"""

import string
from pathlib import Path

from .agents import AgentError, StartFailure
from .config import LEGACY_NEEDS_OPERATOR
from .planning import FIX_COMMAND
from .ports import Tracker
from .review import CI, FINDING_COMMAND, finding_key, owner_of
from .supervision import DECIDE

PROMPT_DIR = Path(__file__).resolve().parent / 'prompts'
REREVIEW_COMMAND = '/devloop rereview'
LABEL_STYLE = {
    'ready': ('0e8a16', 'devloop: pronta per essere implementata da un agent'),
    'working': ('fbca04', 'devloop: implementazione in corso'),
    'review': ('1d76db', 'devloop: in attesa di review automatica'),
    'changes': ('d93f0b', 'devloop: modifiche richieste dalla review'),
    'ready_to_merge': ('0e8a16', 'devloop: approvata, in attesa del merge'),
    'needs_operator': ('b60205', 'devloop: serve una decisione dell\'operatore'),
    'merge_hold': ('5319e7', 'devloop: merge trattenuto dal supervisore o dall\'operatore'),
    'needs_supervisor': ('d876e3', 'devloop: escalation non critica, decide il supervisore'),
}


def requested_fixes(fixes):
    """The fixes that ``FIX_COMMAND`` asked for (#195), among the fix records ``fixes``."""
    return [f for f in fixes if f.get('requests')]


def operator_comments(comments, trusted_users):
    """The comments in ``comments`` that a trusted user wrote in person.

    Every text devloop publishes starts with the AGENTS.md attribution header. An actor without
    a GitHub App publishes with the operator's own login, so the header, not the author alone,
    tells the text of devloop and its agents apart from the operator's.
    """
    return [c for c in comments
            if (c.get('author') or {}).get('login') in trusted_users
            and not (c.get('body') or '').lstrip().startswith('> [!NOTE]')]


def requests_rereview(body):
    """Whether a comment asks for a new review: its first line is ``REREVIEW_COMMAND``."""
    lines = (body or '').strip().splitlines()
    return bool(lines) and lines[0].strip() == REREVIEW_COMMAND


def format_operator_comments(comments):
    if not comments:
        return ''
    intro = ('A trusted user asked for a new review of this commit after the previous verdict. '
             if any(requests_rereview(c.get('body')) for c in comments) else '')
    rendered = '\n\n'.join(f'### Comment by {c["author"]["login"]}\n\n{c["body"]}'
                           for c in comments)
    return ('## Comments from trusted users since the previous verdict\n\n' + intro
            + 'The operator wrote them after the previous round, for example to settle a point '
              'that round left to a human.\n\n' + rendered)


def _template(name, **values):
    text = (PROMPT_DIR / f'{name}.md').read_text(encoding='utf-8')
    return string.Template(text).safe_substitute({k: str(v) for k, v in values.items()})


# The changed files shown for each mapped document in a prompt (#203); the rest are counted.
DOCS_PATHS_SHOWN = 5


def docs_map_section(entries):
    """The documentation section of the first implement prompt (#203): its diff is still empty,
    so it gets the whole ``[docs] map``. Empty without a map."""
    if not entries:
        return ''
    lines = '\n'.join(f'- `{key}` → ' + ', '.join(f'`{d}`' for d in documents)
                      for key, documents in entries)
    return ('## Documentation map\n\n'
            'devloop maps code paths to the documents that describe them (`[docs] map`; a path '
            'ending in `/` covers every file under it). Your diff is still empty, so this is the '
            'whole map. Before finishing, apply it to the files you changed: update every '
            'document mapped to one of them whose described behaviour your change alters. A file '
            'that no entry covers asks for no document.\n\n' + lines)


def _docs_list(documents):
    lines = []
    for document, paths in documents.items():
        shown = ', '.join(f'`{p}`' for p in paths[:DOCS_PATHS_SHOWN])
        more = (f' and {len(paths) - DOCS_PATHS_SHOWN} more' if len(paths) > DOCS_PATHS_SHOWN
                else '')
        lines.append(f'- `{document}`, mapped from {shown}{more}')
    return '\n'.join(lines)


def docs_diff_section(documents):
    """The documentation section of a later implement attempt and of a fix (#203): the
    documents mapped to the files of the current diff (``DocsMap.documents``). Empty when no
    file of the diff is mapped."""
    if not documents:
        return ''
    return ('## Documents mapped to the diff\n\n'
            'The files this branch changes map to these documents (`[docs] map`). Update each '
            'one whose described behaviour your change alters; leave the others as they '
            'are.\n\n' + _docs_list(documents))


def docs_review_section(documents):
    """The documentation section of a review prompt (#203): the documents mapped to the
    files of the pull request's diff, and how to judge them. Empty when no file is mapped."""
    if not documents:
        return ''
    return ('## Documents mapped to the diff\n\n'
            'The files this pull request changes map to these documents (`[docs] map`):\n\n'
            + _docs_list(documents)
            + '\n\nA behaviour described in one of these documents that the change alters without '
              'updating that document is a **blocking** finding. When a mapped document stays '
              'correct without changes, say so in `checks_performed`.')


def _format_findings(findings):
    lines = []
    for f in findings:
        where = f' ({f["file"]}{":" + str(f["line"]) if f["line"] > 0 else ""})' if f['file'] else ''
        lines.append(f'- **{f.get("key") or f["id"]}**{where}: {f["title"]}\n  {f["detail"]}'
                     + (f'\n  Suggested fix: {f["suggested_fix"]}' if f['suggested_fix'] else ''))
    return '\n'.join(lines) or '(none)'


def format_fix_requests(requests):
    """The section of a fix prompt with the requests of ``FIX_COMMAND`` (#195), ``{comment, by,
    request}``; empty without any."""
    if not requests:
        return ''
    rendered = '\n\n'.join(f'### Request by {r["by"]}\n\n{r["request"]}' for r in requests)
    return ('## Requests from the operator\n\n'
            f'A trusted user or the supervisor asked for this fix with `{FIX_COMMAND}`, without '
            'waiting for a review. Treat each request below as a requirement of this fix, as you '
            'would a blocking finding: implement it, or return `status: "blocked"` if it needs a '
            'product decision or contradicts the issue. List no response for it in `responses`, '
            'which is for the open findings only. Your `summary` must say how you handled each '
            'request.\n\n' + rendered)


def _format_requested(fix):
    return (f'Fix asked with `{FIX_COMMAND}` on {str(fix.get("from") or "?")[:12]}:\n'
            + '\n'.join(f'- by {r.get("by")}: {r.get("request", "")}' for r in fix['requests']))


def format_previous_rounds(reviews, fixes, closures=()):
    rounds = {review['round'] for review in reviews}
    # Fixes asked with FIX_COMMAND before any review of their round (#195).
    parts = [_format_requested(fix) for fix in requested_fixes(fixes)
             if fix['round'] not in rounds]
    if not reviews:
        return '\n\n'.join(parts) or 'This is the first round.'
    for position, review in enumerate(reviews):
        owner = owner_of(review)
        parts.append(f'### Round {review["round"]}: {review["verdict"]} on {review["head_sha"][:12]}'
                     f' ({review["source"]}, reviewer {owner})\n\n{review["summary"]}\n\n'
                     'Blocking findings:\n' + _format_findings(
                         [dict(f, key=f.get('key') or finding_key(review['round'], owner, f['id']))
                          for f in review['blocking_findings']]))
        if reviews[position + 1:position + 2] and reviews[position + 1]['round'] == review['round']:
            continue  # the fix answers the whole round: after its last review
        for fix in fixes:
            if fix['round'] == review['round'] and fix.get('requests'):
                parts.append(_format_requested(fix))
            if fix['round'] == review['round'] and (fix['responses'] or not fix.get('requests')):
                parts.append('Implementer responses:\n' + '\n'.join(
                    f'- {r["finding_id"]}: {r["action"]}: {r["note"]}' for r in fix['responses']))
    if closures:
        parts.append('### Findings closed by CI or by the operator\n\n' + '\n'.join(
            f'- {c["key"]}: {c["status"]} by '
            + ('CI' if c['by'] == CI else f'the operator ({c["author"]})') + f': {c["reason"]}'
            for c in closures))
    return '\n\n'.join(parts)


def _open_findings_reason(findings):
    """Why an approval is not promoted while other owners' findings are open."""
    listed = '\n'.join(f'- `{f["key"]}` ({f["title"]})' for f in findings)
    return ('L\'approvazione non chiude i finding aperti da altri: un finding lo chiude solo il '
            'reviewer che l\'ha aperto, la CI verde (per i finding `ci`) o l\'operatore. Restano '
            f'aperti:\n\n{listed}\n\nUn utente fidato può chiuderli con un commento sulla PR '
            f'che contiene una riga `{FINDING_COMMAND} <round/reviewer/ID> resolved|withdrawn '
            '<motivazione>`, poi riportare la PR in `agent:review`: l\'approvazione registrata '
            'viene riapplicata senza una nuova review.')


def _review_failure(failures):
    """The exception that ends a review action whose reviewers ``failures`` (``(key, exc)``)
    failed: the only one, or one that lists them all when every one is an agent failure."""
    if len(failures) == 1 or not all(isinstance(exc, AgentError) for _, exc in failures):
        return failures[0][1]
    error = AgentError('; '.join(f'`{key}`: {str(exc).strip()}' for key, exc in failures),
                       getattr(failures[0][1], 'log', None))
    error.reviewer = ','.join(key for key, _ in failures)
    return error


def _trusted_comments(comments, trusted_users):
    """The comments of trusted users: the ones that reach the prompts."""
    return [c for c in comments if (c.get('author') or {}).get('login') in trusted_users]


def _render_issue_comments(comments, trusted_users, accepted=()):
    """The comments of trusted users as model input, or ''. ``accepted``: the dispatcher's
    records of defaults the supervisor accepted (#182) or of its decisions on the gaps of a
    readiness check (#258), which count as trusted comments."""
    parts = [f'### Comment by {c["author"]["login"]}\n\n{c["body"]}'
             for c in _trusted_comments(comments, trusted_users)]
    parts += [_render_acceptance(record) for record in accepted]
    rendered = '\n\n'.join(parts)
    return ('## Issue comments\n\n' + rendered) if rendered else ''


def _render_acceptance(record):
    """A record of accepted defaults (#182), or of a decision on the gaps of a readiness check
    (#258), as model input."""
    if record['command'] == DECIDE:
        listed = '\n'.join(f'- {g["description"].strip()}\n  Proposed default: '
                           f'{g["proposed_default"].strip()}' for g in record.get('gaps') or [])
        return (f'### Decision of the supervisor ({record["by"]}) on the gaps of the readiness '
                'check, recorded by devloop\n\n'
                f'The gaps the readiness check found:\n\n{listed}\n\n'
                'The supervisor decided them as follows: follow this decision, which may accept '
                f'some proposed defaults and replace others.\n\n{record["reason"].strip()}')
    listed = '\n'.join(f'- {d["description"].strip()}\n  Accepted default: '
                       f'{d["proposed_default"].strip()}' for d in record.get('defaults') or [])
    return (f'### Defaults accepted by the supervisor ({record["by"]}), recorded by devloop\n\n'
            'The defaults the readiness check proposed for these gaps are accepted: follow '
            f'them.\n\n{listed}')


def _failure_text(exc):
    """``exc`` as the end of a sentence: a one-line message gets its full stop, one that ends
    with a list (``ModelsUnavailable``) a blank line."""
    text = str(exc).rstrip()
    return text + ('\n\n' if '\n' in text else '.')


def _start_failure(exc):
    """Whether the failed turn ``exc`` is an agent that could not start (#218), also when the
    chain of models wraps it (``ModelsUnavailable``)."""
    return isinstance(exc, StartFailure) or isinstance(exc.__cause__, StartFailure)


def _describe_bases(checks, branch):
    """The base checks of ``_base_check`` as a Markdown list."""
    lines = []
    for check in checks:
        span = f'`{check["from"][:12]}` → `{check["to"][:12]}`'
        if check['outcome'] == 'disjoint':
            lines.append(f'- {span}: nessun commit intermedio di `{branch}` tocca file della PR.')
        elif check['outcome'] == 'touched':
            lines.append(f'- {span}: i commit intermedi di `{branch}` toccano file della PR: '
                         + ', '.join(f'`{p}`' for p in check['paths']) + '.')
        elif check['outcome'] == 'conflict':
            files = ', '.join(f'`{p}`' for p in check.get('paths') or [])
            lines.append(f'- {span}: i commit intermedi di `{branch}` non toccano file della PR, '
                         'ma la PR non si unisce senza conflitti alla base corrente'
                         + (f' ({files})' if files else '') + ': serve una sincronizzazione.')
        else:
            lines.append(f'- {span}: non è stato possibile elencare i file toccati dai commit '
                         f'intermedi (per esempio `{branch}` è stato riscritto), o verificare '
                         'che la PR si unisca senza conflitti alla base corrente: la non '
                         'interferenza non è dimostrata.')
    return '\n'.join(lines)


def _format_conflicts(conflicts):
    """``(file, base commits)`` pairs as a Markdown list."""
    lines = []
    for name, commits in conflicts:
        lines.append(f'- `{name}`')
        lines += [f'  - `{c.split(" ", 1)[0]}` {c.partition(" ")[2]}'.rstrip() for c in commits]
    return '\n'.join(lines) or '(none)'


def label_transition(gh: Tracker, labels):
    """Where the repository stands in the rename of ``agent:needs-operator`` from its name
    before #248 (``LEGACY_NEEDS_OPERATOR``): ``rename`` when only the old label exists,
    ``both`` when both do, None when the old one is gone."""
    names = set(gh.repo_labels())
    if LEGACY_NEEDS_OPERATOR not in names:
        return None
    return 'both' if labels.needs_operator in names else 'rename'


def both_labels_warning(labels):
    """What ``setup`` and ``status`` report when both names of the operator label exist."""
    return (f'both {LEGACY_NEEDS_OPERATOR} and {labels.needs_operator} exist: setup renames '
            f'nothing and touches neither, and both block as {labels.needs_operator}. Move the '
            f'issues and PRs of {LEGACY_NEEDS_OPERATOR} to {labels.needs_operator}, then delete '
            f'{LEGACY_NEEDS_OPERATOR}')


def transition_labels(gh: Tracker, labels):
    """The transition of #248, for ``devloop setup`` and the first tick of a process:
    ``agent:needs-operator`` is renamed from its old name when only that exists, so that the
    issues and PRs that have it keep it; with both names, neither label is touched
    (``both_labels_warning``). Returns ``(label_transition, note for the log)``, the note ''
    when there is nothing to say."""
    transition = label_transition(gh, labels)
    note = ''
    if transition == 'rename':
        color, description = LABEL_STYLE['needs_operator']
        gh.rename_label(LEGACY_NEEDS_OPERATOR, labels.needs_operator, color, description)
        note = (f'renamed {LEGACY_NEEDS_OPERATOR} to {labels.needs_operator}: the issues and PRs '
                'that had it keep it')
    elif transition == 'both':
        note = both_labels_warning(labels)
    return transition, note
