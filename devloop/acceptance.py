"""Acceptance criteria of an issue and the coverage map the writing agents return (#134).

The criteria are the items of a checkbox list (``- [ ]``, ``- [x]``) or of a numbered list
(``1.``, ``1)``) in the first section of the issue whose heading is "Criteri di accettazione" or
"Acceptance criteria". Implementer and fixer return ``acceptance_map``: one entry per criterion,
in the order of the issue, ``{criterion, tests: [{file, name}], status, reason}``. The latest map
of a PR holds: the one of the latest fix that carries one, else the one recorded when the PR was
opened. A sync carries no map, so the previous one still holds after it.

:func:`acceptance_findings` turns the map into the blocking findings devloop adds to every review
on its own, whatever the reviewer proposes: a criterion ``not_covered`` without a reason, a map
missing while the issue has criteria, and a map with a different number of entries than the
criteria. A ``not_covered`` criterion with a reason is for the reviewer to judge. An issue without
recognisable criteria needs no map: a missing one counts as the empty map. The dispatcher applies
the same findings before reusing or keeping an approval recorded without seeing the map.
"""

import json
import re

from .schema import SchemaError, load_schema, validate

TITLES = ('criteri di accettazione', 'acceptance criteria')
_HEADING = re.compile(r'(#{1,6})\s+(.*?)\s*#*\s*')
_ITEM = re.compile(r'( {0,3})(?:[-*+]\s+\[[ xX]\]|\d{1,9}[.)])\s+(\S.*)')
_FENCE = re.compile(r'\s{0,3}(`{3,}|~{3,})(.*)')


def _opens(match):
    """The fence ``match`` opens, or None: a backtick fence whose info string has a backtick
    opens nothing (CommonMark)."""
    if match is None or (match.group(1)[0] == '`' and '`' in match.group(2)):
        return None
    return match.group(1)


def _closes(match, fence):
    """Whether ``match`` closes the block ``fence`` opened: the same character, at least as
    many times, and nothing else on the line (CommonMark)."""
    return (match is not None and match.group(1)[0] == fence[0]
            and len(match.group(1)) >= len(fence) and not match.group(2).strip())


def acceptance_criteria(body):
    """The criteria of the issue ``body``, in order (an empty list when it has none)."""
    criteria, level, current, fence = [], None, None, None
    for line in (body or '').splitlines():
        stripped = line.strip()
        fenced = _FENCE.match(line)
        if fence is None and _opens(fenced):
            fence, current = _opens(fenced), None
            continue
        if fence is not None:
            if _closes(fenced, fence):
                fence = None
            current = None
            continue
        heading = _HEADING.fullmatch(stripped) if not line.startswith('    ') else None
        if heading:
            depth = len(heading.group(1))
            if level is not None and depth <= level:
                break
            title = heading.group(2).strip().rstrip(':').strip().lower()
            if level is None and title in TITLES:
                level = depth
            current = None
            continue
        if level is None:
            continue
        item = _ITEM.fullmatch(line.rstrip())
        if item:
            criteria.append(' '.join(item.group(2).split()))
            current = len(criteria) - 1
        elif stripped and current is not None and line[:1].isspace():
            criteria[current] += ' ' + ' '.join(stripped.split())
        else:
            current = None
    return criteria


def _map_schema():
    return load_schema('implement')['properties']['acceptance_map']


def valid_map(value):
    """Whether ``value`` is a well-formed acceptance map."""
    try:
        validate(value, _map_schema())
    except SchemaError:
        return False
    return True


def latest_map(meta, fixes):
    """The map that holds for a PR: the latest fix record that carries one, else the PR marker
    ``meta``. None when no record carries one, or the latest that does is malformed."""
    for record in [meta or {}, *(fixes or ())][::-1]:
        if 'acceptance_map' in record:
            return record['acceptance_map'] if valid_map(record['acceptance_map']) else None
    return None


def _finding(title, detail, suggested_fix):
    # A finding of the coverage map (#134) is never one of security (#405, the review schema).
    return {'file': '', 'line': 0, 'title': title, 'detail': detail,
            'suggested_fix': suggested_fix, 'security': False}


def acceptance_findings(criteria, acceptance_map):
    """The blocking findings devloop raises on its own for ``acceptance_map`` (None: no map
    recorded) against the ``criteria`` of the issue."""
    criteria = list(criteria or ())
    if acceptance_map is None:
        if not criteria:
            return []  # no recognisable criteria: a missing map is the empty map
        return [_finding('Mappa di copertura dei criteri di accettazione assente',
                         f'L\'issue ha {len(criteria)} criteri di accettazione riconoscibili, ma '
                         'la PR non registra una mappa di copertura (`acceptance_map`).',
                         'Fornire `acceptance_map` con una voce per ogni criterio, nell\'ordine '
                         'dell\'issue.')]
    found = []
    if criteria and len(acceptance_map) != len(criteria):
        found.append(_finding('La mappa di copertura non corrisponde ai criteri di accettazione',
                              f'L\'issue ha {len(criteria)} criteri di accettazione '
                              f'riconoscibili, la mappa ha {len(acceptance_map)} voci.',
                              'Fornire una voce di `acceptance_map` per ogni criterio, '
                              'nell\'ordine dell\'issue.'))
    for position, entry in enumerate(acceptance_map, 1):
        if entry['status'] == 'not_covered' and not entry['reason'].strip():
            found.append(_finding(f'Criterio di accettazione {position} non coperto, senza '
                                  'motivo', entry['criterion'].strip(),
                                  'Aggiungere i test che verificano il criterio, oppure '
                                  'spiegare in `reason` perché resta non coperto.'))
    return found


def format_criteria(criteria):
    """The criteria as prompt text."""
    if not criteria:
        return ('None: the issue has no recognisable acceptance criteria, so the expected '
                '`acceptance_map` is empty (`[]`). That is allowed and blocks nothing.')
    return '\n'.join(f'{i}. {c}' for i, c in enumerate(criteria, 1))


def format_map(acceptance_map):
    """The map as prompt text."""
    if acceptance_map is None:
        return 'No acceptance map is recorded for this pull request.'
    if not acceptance_map:
        return 'The recorded acceptance map is empty (`[]`).'
    return '```json\n' + json.dumps(acceptance_map, indent=2, ensure_ascii=False) + '\n```'


def _cell(text):
    return ' '.join(str(text).split()).replace('|', '\\|')


_STATUS_TEXT = {'covered': 'coperto', 'not_covered': 'non coperto'}


def render_map(acceptance_map):
    """The map as Markdown for GitHub, under its own heading."""
    parts = ['### Copertura dei criteri di accettazione', '']
    if not acceptance_map:
        parts.append('Mappa vuota: nessun criterio di accettazione riconoscibile nell\'issue.')
        return '\n'.join(parts)
    parts += ['| # | Criterio | Stato | Test | Motivo |', '|---|---|---|---|---|']
    for i, entry in enumerate(acceptance_map, 1):
        tests = ', '.join(f'`{_cell(t["file"])}`: {_cell(t["name"])}' for t in entry['tests'])
        parts.append(f'| {i} | {_cell(entry["criterion"])} | {_STATUS_TEXT[entry["status"]]} '
                     f'| {tests or "—"} | {_cell(entry["reason"]) or "—"} |')
    return '\n'.join(parts)
