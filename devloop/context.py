"""The texts devloop prepares for the prompts and the comments.

Moved from ``devloop/dispatcher.py`` without changes (#292, step S2 of #261): the comments of
trusted users as prompt input, the prompt templates, the sections of the documentation map, the
findings, fix requests and previous rounds of a fix or review prompt, the reasons and lists of
the dispatcher's comments, and the transition of the operator label of #248 with the label
styles it uses. Since #297 (step S7) ``devloop/dispatcher.py`` no longer re-exports these names:
import them from here.
"""

import re
import string
from pathlib import Path

from .agents import AgentError, StartFailure
from .config import LEGACY_NEEDS_OPERATOR
from .markdown import without_attribution
from .planning import FIX_COMMAND
from .ports import Tracker
from .review import CI, FINDING_COMMAND, finding_key, findings_only, owner_of
from .supervision import DECIDE

PROMPT_DIR = Path(__file__).resolve().parent / 'prompts'
REREVIEW_COMMAND = '/devloop rereview'
# The decision of the operator on the trust policy of an issue (#483): the first line of a
# comment of a trusted user, followed by the text of the decision.
APPROVE_COMMAND = '/devloop approve'
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


def text_changed_at(history):
    """When the title or the body of issue ``history`` (``Tracker.issue_history``) last changed:
    the latest of its body's ``last_edited_at`` and of its ``renamed`` events; None when neither
    changed since the issue was opened."""
    times = [history.get('last_edited_at'), *(at for _, at in history.get('renamed') or ())]
    return max((t for t in times if isinstance(t, str)), default=None)


def approve_command(body, after_header=False):
    """The text of the decision of a ``APPROVE_COMMAND`` (#483): the rest of the comment after
    the command, its first line included, stripped (``''`` without a text); None when the
    comment is no such command. The command is the first line of the comment; with
    ``after_header``, the first line after the attribution header and any other quoted line, as
    ``supervision.supervisor_command`` reads it, to find the commands that are refused."""
    lines = (body or '').strip().splitlines()
    if after_header:
        while lines and (not lines[0].strip() or lines[0].strip().startswith('>')):
            lines.pop(0)
    tokens = lines[0].strip().split(None, 2) if lines else []
    if tokens[:2] != APPROVE_COMMAND.split():
        return None
    return '\n'.join([tokens[2] if len(tokens) > 2 else '', *lines[1:]]).strip()


def _app(login, devloop_logins):
    """Whether ``login`` is an app: one of devloop's (the supervisor's identity included) or any
    other GitHub App (``<slug>[bot]``)."""
    return login in devloop_logins or (login or '').endswith('[bot]')


def issue_decisions(comments, history, trusted_users, devloop_logins, working, taken=False):
    """The comments of an issue that record a decision of the operator on it (#472), oldest
    first: the ``APPROVE_COMMAND`` with a text (``approve_command``, #483) a trusted user wrote
    in person (``operator_comments``), not from an app (``_app``: one of ``devloop_logins``,
    the supervisor's identity, the agents' and the dispatcher's apps, or any other), after the
    latest change of the issue's text (``text_changed_at``) and before devloop took it on, the
    first ``labeled`` event of ``working`` after that change. Any other comment, an approval
    without the command too, decides nothing. A change of the text after a command drops it.
    With ``taken`` (a review) the issue must have been taken on after that change: without
    such an event no command counts. The comment is read as it is now, so its latest change
    (``updated_at``), not only its creation (``created_at``), must come before the takeover: a
    comment rewritten after it does not count. A comment without both times, or written in the
    same second as a bound, does not count. A history read only in part (``complete`` not
    True, #483) admits none: the takeover or the latest change might be among the events not
    read. What a command approves is the agents' to judge."""
    if history.get('complete') is not True:
        return []
    changed = text_changed_at(history)
    takeovers = sorted(at for label, _, at in history.get('labeled') or ()
                       if label == working and isinstance(at, str)
                       and (changed is None or at > changed))
    bound = takeovers[0] if takeovers else None
    if taken and bound is None:
        return []
    found = []
    for c in operator_comments(comments, trusted_users):
        at, edited = c.get('created_at'), c.get('updated_at')
        if (_app(c['author']['login'], devloop_logins) or not approve_command(c.get('body'))
                or not (isinstance(at, str) and at) or not (isinstance(edited, str) and edited)
                or (changed is not None and at <= changed)
                or (bound is not None and max(at, edited) >= bound)):
            continue
        found.append(c)
    return found


def refused_approvals(comments, trusted_users, devloop_logins):
    """The ``APPROVE_COMMAND`` of ``comments`` that devloop refuses and says so (#483), oldest
    first, as ``(comment, reason)``: one written by an app (``_app``: the supervisor's identity,
    an app of devloop or any other), one of a trusted user after the attribution header (the
    text of an agent or of the supervisor with the operator's login), and one of a trusted user
    not on the first line of the comment or without the text of the decision. The command of
    anyone else is ignored, as any text of an untrusted author; one written outside the bounds
    of ``issue_decisions`` simply does not count."""
    found = []
    for c in comments:
        body = c.get('body') or ''
        if approve_command(body, after_header=True) is None:
            continue
        login = (c.get('author') or {}).get('login')
        if _app(login, devloop_logins):
            reason = ('l\'autore è il supervisore o un\'app: una decisione sulla policy di '
                      'fiducia la prende solo l\'operatore, di persona')
        elif login not in trusted_users:
            continue
        elif body.lstrip().startswith('> [!NOTE]'):
            reason = ('il commento porta l\'intestazione degli agent: è il testo di un agent o '
                      'del supervisore, non dell\'operatore di persona')
        elif approve_command(body) is None:
            reason = 'il comando non è la prima riga del commento'
        elif not approve_command(body):
            reason = 'manca il testo della decisione dopo il comando'
        else:
            continue
        found.append((c, reason))
    return found


def format_operator_decisions(decisions):
    """The section "Decisions of the operator on the issue" of a preflight, review or advisory
    prompt (#472): the commands ``issue_decisions`` keeps, or a line that says there are none."""
    head = '## Decisions of the operator on the issue\n\n'
    if not decisions:
        return head + (f'None: no comment on the issue counts as a decision of the operator. '
                       f'An approval written without `{APPROVE_COMMAND}` is not one.')
    rendered = '\n\n'.join(f'### Comment by {c["author"]["login"]}, {c["created_at"]}\n\n'
                           f'{c["body"]}' for c in decisions)
    return (head + f'The `{APPROVE_COMMAND}` commands a trusted user wrote in person on the '
                   'issue after the latest change of its title or body and before devloop took '
                   'it on, each followed by the text of the decision. A decision of the '
                   'operator, as one written in the body of the issue: see "Trust policy". It '
                   'covers only what its text approves, within the limits and on the '
                   'conditions it sets; any other comment, an approval without the command too, '
                   'decides nothing.\n\n' + rendered)


def trust_policy_rule():
    """The rule on the trust policy the review and the advisory review prompts share (#447),
    from the "Trust policy" section of the review prompt up to the request of its verdict: the
    readiness check applies it to the issue (#472)."""
    text = (PROMPT_DIR / 'review.md').read_text(encoding='utf-8')
    start = text.index('## Trust policy\n') + len('## Trust policy\n')
    return text[start:text.index('\nWhen the change needs the operator under this rule', start)
                ].strip()


def requests_rereview(body):
    """Whether a comment asks for a new review: its first line is ``REREVIEW_COMMAND``."""
    lines = (body or '').strip().splitlines()
    return bool(lines) and lines[0].strip() == REREVIEW_COMMAND


def format_operator_comments(comments):
    if not comments:
        return ''
    intro = ('A trusted user asked for a new review of this commit after the previous verdict. '
             if any(requests_rereview(c.get('body')) for c in comments) else '')
    # Without attribution headers (#389): the reviewer does not learn who wrote the change.
    rendered = '\n\n'.join(f'### Comment by {c["author"]["login"]}\n\n'
                           f'{without_attribution(c["body"])}' for c in comments)
    return ('## Comments from trusted users since the previous verdict\n\n' + intro
            + 'The operator wrote them after the previous round, for example to settle a point '
              'that round left to a human.\n\n' + rendered)


# An optional span of a template (ADR 0006, R4): ``{{?name:text}}`` is ``text`` when the value
# ``name`` is not empty, and nothing when it is; a span alone on its lines takes them with it.
_OPTIONAL = re.compile(r'(^)?\{\{\?(\w+):(.*?)\}\}(\n)?', re.S | re.M)


def _english_list(items):
    """``a``, ``a and b``, ``a, b and c``."""
    items = list(items)
    return ' and '.join(filter(None, (', '.join(items[:-1]), *items[-1:])))


def prompt_values(cfg):
    """The values of the project's data in the prompts (``cfg.prompts``, ADR 0006, R4), each
    '' when the project does not give it: ``instructions`` and ``fix_instructions`` as an
    English list, ``instructions_items`` joined by commas, for a list the template goes on."""
    facts = cfg.prompts
    return {'instructions': _english_list(facts.instructions),
            'instructions_items': ', '.join(facts.instructions),
            'fix_instructions': _english_list(facts.fix_instructions),
            'module_test_cmd': facts.module_test_cmd,
            'module_test_helpers': facts.module_test_helpers,
            'adr_dir': facts.adr_dir, 'adr_decided': facts.adr_decided,
            'adr_open_value': facts.adr_open_value, 'adr_open_choice': facts.adr_open_choice}


def _template(name, **values):
    """The prompt ``name`` with ``values``. An optional span (``_OPTIONAL``) whose value is not
    among ``values`` is an error (``KeyError``): the caller forgot ``prompt_values``."""
    text = (PROMPT_DIR / f'{name}.md').read_text(encoding='utf-8')

    def span(match):
        start, key, body, newline = match.groups()
        if str(values[key]):
            return body + (newline or '')
        return '' if start is not None and newline else (newline or '')

    text = _OPTIONAL.sub(span, text)
    return string.Template(text).safe_substitute({k: str(v) for k, v in values.items()})


def _duration(job):
    seconds = job.get('duration_s')
    if seconds is None:
        return 'still running' if job.get('bucket') == 'pending' else 'duration not known'
    minutes, rest = divmod(int(seconds), 60)
    return f'{seconds}s' if not minutes else f'{seconds}s ({minutes}m {rest:02d}s)'


def _plain(text):
    """``text`` fit for inline code in a prompt: no backticks, no line breaks."""
    return ' '.join(str(text).replace('`', "'").split())


def format_ci_summary(head, state, jobs, conflict_base=None):
    """The section of the review and fix prompts with the CI of ``head`` (#300): each check
    with its outcome and duration, and for a job whose log holds the final lines of
    ``scripts/run-tests.py`` (``localtests.suite_summary``) those lines. ``jobs`` as
    ``Dispatcher._ci_jobs`` returns them; None when the checks could not be read. A job log
    that could not be read is reported as such: the summary is not available, nothing more.
    ``conflict_base``: the base branch ``head`` conflicts with (#392); without checks the
    section says that GitHub runs no CI on such a head, so there is no summary to give."""
    title = '## CI of the head\n\n'
    if jobs is None:
        return (title + f'The summary of the CI of `{head[:12]}` is not available: its checks '
                        'could not be read.')
    if not jobs and conflict_base:
        return (title + f'There is no CI summary of `{head[:12]}`: the head conflicts with '
                        f'`{conflict_base}`, and GitHub runs no CI on a pull request in '
                        'conflict. The CI runs on the head your fix produces.')
    if not jobs:
        return title + f'GitHub reports no CI check on `{head[:12]}` yet.'
    lines = [title + f'The checks of `{head[:12]}` as GitHub reports them (CI state: **{state}**), '
             'with the duration of each one and, for a job that runs `scripts/run-tests.py`, '
             'the final lines of the suite read from the job log. The dispatcher read them for '
             'you: use them when the issue asks for a measure of the suite.', '']
    for job in jobs:
        lines.append(f'- `{_plain(job.get("name"))}`: {_plain(job.get("outcome"))}, '
                     f'{_duration(job)}')
        tests = job.get('tests')
        if tests:
            lines.append(f'  - `{_plain(tests["ran"])}`')
            if tests.get('slowest'):
                lines.append(f'  - `{_plain(tests["slowest"])}`')
        elif job.get('log') == 'unavailable':
            lines.append('  - the job log could not be read: its test summary is not available')
    return '\n'.join(lines)


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


def owner_assessments(reviews):
    """The latest assessment of each finding by its owner (#481), ``{key: (round, status,
    note)}``: after a dispute, the owner keeps a finding open by answering with new evidence in
    its ``note``, and the implementer verifies that, not only the evidence it disputed. The
    assessments of other reviewers, and those of records without keys, are left out."""
    latest = {}
    for review in reviews:
        owner = owner_of(review)
        for p in review.get('previous_findings', ()):
            key, note = p.get('key'), p.get('note')
            parts = key.split('/') if isinstance(key, str) else []
            if len(parts) >= 3 and parts[1] == owner and isinstance(note, str) and note.strip():
                latest[key] = (review['round'], p.get('status'), note.strip())
    return latest


def _format_findings(findings, assessments=None):
    """The findings of a prompt, each with its evidence and, from ``assessments`` (see
    :func:`owner_assessments`), the latest assessment of its owner (#481)."""
    lines = []
    for f in findings:
        where = f' ({f["file"]}{":" + str(f["line"]) if f["line"] > 0 else ""})' if f['file'] else ''
        evidence = f.get('evidence')
        assessed = (assessments or {}).get(f.get('key'))
        lines.append(f'- **{f.get("key") or f["id"]}**{where}: {f["title"]}\n  {f["detail"]}'
                     # The evidence the reviewer gave (#481), for the implementer to verify.
                     + (f'\n  Evidence: {evidence.strip()}'
                        if isinstance(evidence, str) and evidence.strip() else '')
                     # The owner's answer to a dispute, with its new evidence (#481).
                     + (f'\n  Latest assessment by its owner (round {assessed[0]}, '
                        f'{assessed[1]}): {assessed[2]}' if assessed else '')
                     + (f'\n  Suggested fix: {f["suggested_fix"]}' if f['suggested_fix'] else ''))
    return '\n'.join(lines) or '(none)'


def _format_assessments(review):
    """A review's assessments of the earlier findings (#481), with the new evidence that
    answers a dispute in their ``note``."""
    return '\n'.join(f'- {p.get("key") or p.get("id")}: {p.get("status")}: {p.get("note", "")}'
                     for p in review.get('previous_findings', ()))


def _unproven(review):
    """A review's blocking findings recorded as non-blocking for lack of evidence (#481)."""
    return [f for f in review.get('non_blocking_findings', ())
            if f.get('without_evidence') is True]


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
        # A findings-only review (#416): its findings are closed by the quorum.
        role = ', findings-only' if findings_only(review) else ''
        parts.append(f'### Round {review["round"]}: {review["verdict"]} on {review["head_sha"][:12]}'
                     f' ({review["source"]}, reviewer {owner}{role})\n\n{review["summary"]}\n\n'
                     'Blocking findings:\n' + _format_findings(
                         [dict(f, key=f.get('key') or finding_key(review['round'], owner, f['id']))
                          for f in review['blocking_findings']]))
        # #481: the owner's answers to the disputes, and the findings demoted for lack of
        # evidence, which the advisory review judges.
        if review.get('previous_findings'):
            parts.append('Assessments of earlier findings:\n' + _format_assessments(review))
        if _unproven(review):
            parts.append('Blocking findings without evidence, recorded by devloop as '
                         'non-blocking:\n' + _format_findings(
                             [dict(f, key=finding_key(review['round'], owner, f['id']))
                              for f in _unproven(review)]))
        if reviews[position + 1:position + 2] and reviews[position + 1]['round'] == review['round']:
            continue  # the fix answers the whole round: after its last review
        for fix in fixes:
            if fix['round'] == review['round'] and fix.get('requests'):
                parts.append(_format_requested(fix))
            if fix['round'] == review['round'] and (fix['responses'] or not fix.get('requests')):
                parts.append('Implementer responses:\n' + '\n'.join(
                    f'- {r["finding_id"]}: {r["action"]}: {without_attribution(r["note"])}'
                    for r in fix['responses']))
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
