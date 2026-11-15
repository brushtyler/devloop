"""Turns a reviewer's structured output into a consistent review record, and tracks findings.

The reviewer proposes a verdict; devloop enforces the rules that make the
verdict machine-actionable:

* ``approve`` only with no blocking finding and nothing left unresolved;
* ``request_changes`` only with at least one open finding to fix;
* every blocking finding of earlier rounds is accounted for, and one the
  reviewer does not assess stays open.

Every blocking finding has an owner and an identity, ``round/reviewer/ID`` (``2/codex/R2-1``),
recorded in the marker. Only its owner closes it, in a later review. A finding of the reviewer
``ci`` is closed by the dispatcher when CI passes on the current head, and the operator can close
any finding with an explicit command: both are dispatcher records (``finding`` markers). Another
reviewer's assessment never closes a finding.
"""

import re

from .schema import load_schema, validate

VERDICTS = ('approve', 'request_changes', 'needs_human')
CI = 'ci'
OPERATOR = 'operator'
CLOSING = ('resolved', 'withdrawn')
KEY = re.compile(r'[1-9][0-9]*/[A-Za-z0-9_.-]+/R[1-9][0-9]*-[1-9][0-9]*')
FINDING_COMMAND = '/devloop finding'
_COMMAND = re.compile(r'/devloop finding\s+(\S+)\s+(\S+)\s+(\S.*)')


def finding_key(round_no, owner, finding_id):
    return f'{round_no}/{owner}/{finding_id}'


def owner_of(review):
    """The owner of a review's findings: ``ci`` for a CI round, else the recorded reviewer."""
    return CI if review.get('source') == 'ci' else review.get('reviewer')


def _with_ids(findings, round_no, start):
    return [dict(f, id=f'R{round_no}-{start + i}') for i, f in enumerate(findings)]


def _keyed(review):
    """Whether a record carries finding keys (written since #110). Older records have none and
    are read with the rules of their time."""
    return all('key' in f for f in review['blocking_findings'] + review['previous_findings'])


class FindingState:
    """``open``: blocking findings still open, oldest first, each with its ``key`` and
    ``owner``. ``known``: the key of every finding ever opened. ``problems``: why the owner of
    some finding cannot be reconstructed."""

    def __init__(self, open_, known, problems):
        self.open = open_
        self.known = known
        self.problems = problems


def finding_state(reviews, closures=()):
    """The findings left open by ``reviews`` (sorted by round) and ``closures`` (the entries of
    the dispatcher's ``finding`` records).

    A keyed review closes only the findings it owns, then opens its own. An older record without
    keys is read as before (migration): the open set becomes the earlier findings it marks
    unresolved plus its own, each attributed to the reviewer recorded in the round that opened
    it; a finding whose round is unknown cannot be attributed. A closure is final: by ``ci`` it
    closes only a CI finding, by ``operator`` any finding.
    """
    open_, seen, known, problems = {}, {}, set(), []
    for review in reviews:
        owner, round_no = owner_of(review), review['round']
        if not isinstance(owner, str) or not owner:
            problems.append(f'il round {round_no} non registra il reviewer')
            continue
        if _keyed(review):
            for p in review['previous_findings']:
                earlier = open_.get(p['key'])
                if p['status'] in CLOSING and earlier is not None and earlier['owner'] == owner:
                    del open_[p['key']]
            own = review['blocking_findings']
        else:
            carried = {}
            for p in review['previous_findings']:
                if p.get('status') != 'unresolved':
                    continue
                earlier = seen.get(p.get('id'))
                if earlier is None:
                    problems.append(f'il round {round_no} lascia aperto il finding '
                                    f'`{p.get("id")}`, di cui non risulta il round di origine')
                    continue
                carried[earlier['key']] = earlier
            open_ = carried
            own = [dict(f, key=finding_key(round_no, owner, f['id']))
                   for f in review['blocking_findings']]
        for f in own:
            f = dict(f, owner=owner)
            open_[f['key']] = f
            seen.setdefault(f['id'], f)
            known.add(f['key'])
    for closure in closures:
        f = open_.get(closure['key'])
        if f is not None and (closure['by'] == OPERATOR or f['owner'] == CI):
            del open_[closure['key']]
    return FindingState(list(open_.values()), known, problems)


def open_findings(reviews, closures=()):
    """Blocking findings still open, oldest first (see :func:`finding_state`)."""
    return finding_state(reviews, closures).open


def normalize_review(raw, *, previous_open, round_no, max_rounds, head_sha, ci, reviewer):
    """Validate ``raw`` against the review schema and return a review record.

    ``previous_open`` comes from :func:`open_findings`. The reviewer may name a finding by its
    key or, when unambiguous, by its ID. Its assessment of a finding it does not own is
    recorded, but does not close it.
    """
    validate(raw, load_schema('review'))
    notes = []
    blocking = [dict(f, key=finding_key(round_no, reviewer, f['id']))
                for f in _with_ids(raw['blocking_findings'], round_no, 1)]
    non_blocking = _with_ids(raw['non_blocking_findings'], round_no, len(blocking) + 1)
    ids = [f['id'] for f in previous_open]
    by_ref = {f['id']: f for f in previous_open if ids.count(f['id']) == 1}
    by_ref.update({f['key']: f for f in previous_open})
    assessed = {}
    for p in raw['previous_findings']:
        target = by_ref.get(p['id'])
        if target is not None:
            assessed.setdefault(target['key'], p)
    previous = []
    for finding in previous_open:
        p = assessed.get(finding['key'])
        previous.append({'id': finding['id'], 'key': finding['key'],
                         'status': p['status'] if p else 'unresolved',
                         'note': p['note'] if p else 'Non valutato dal reviewer.'})
    if len(assessed) < len(previous_open):
        notes.append('Il reviewer non ha valutato tutti i finding precedenti: quelli mancanti restano aperti.')
    unresolved = [p for p in previous if p['status'] == 'unresolved']
    foreign = [p['key'] for p, f in zip(previous, previous_open)
               if p['status'] in CLOSING and f['owner'] != reviewer]

    verdict = raw['verdict']
    human_reason = raw['human_reason']
    if verdict == 'approve' and (blocking or unresolved):
        verdict = 'request_changes'
        notes.append('Verdetto corretto da approve a request_changes: restano finding bloccanti aperti.')
    elif verdict == 'request_changes' and not (blocking or unresolved):
        verdict = 'needs_human'
        human_reason = human_reason or 'Il reviewer ha chiesto modifiche senza indicare finding bloccanti.'
        notes.append('Verdetto incoerente: request_changes senza finding bloccanti.')
    elif verdict == 'needs_human' and not human_reason.strip():
        human_reason = 'Il reviewer non ha indicato la decisione richiesta.'
    if foreign:
        notes.append('Restano aperti i finding di altri proprietari che il reviewer considera '
                     'chiusi (' + ', '.join(foreign) + '): li chiude solo chi li ha aperti, la '
                     'CI o l\'operatore.')

    return {
        'source': 'agent', 'reviewer': reviewer, 'verdict': verdict,
        'summary': raw['summary'], 'human_reason': human_reason,
        'blocking_findings': blocking, 'non_blocking_findings': non_blocking,
        'previous_findings': previous, 'checks_performed': raw['checks_performed'],
        'round': round_no, 'max_rounds': max_rounds, 'head_sha': head_sha, 'ci': ci,
        'note': ' '.join(notes),
    }


def ci_failure_review(*, failed_checks, log_tail, previous_open, round_no, max_rounds, head_sha):
    """A review record that sends a PR with failing CI back to the implementer."""
    names = ', '.join(failed_checks) or 'CI'
    finding = {'id': f'R{round_no}-1', 'key': finding_key(round_no, CI, f'R{round_no}-1'),
               'file': '', 'line': 0,
               'title': f'CI fallita: {names}',
               'detail': log_tail.strip() or 'Log non disponibile.',
               'suggested_fix': 'Correggere la causa del fallimento e far passare la CI.'}
    return {
        'source': 'ci', 'reviewer': CI, 'verdict': 'request_changes',
        'summary': f'La CI non è verde ({names}). La review del codice riprende dopo la correzione.',
        'human_reason': '', 'blocking_findings': [finding], 'non_blocking_findings': [],
        'previous_findings': [{'id': f['id'], 'key': f['key'], 'status': 'unresolved',
                               'note': 'Non valutato: CI fallita.'} for f in previous_open],
        'checks_performed': [], 'round': round_no, 'max_rounds': max_rounds,
        'head_sha': head_sha, 'ci': 'fail', 'note': '',
    }


def finding_commands(body):
    """Each line of ``body`` that starts with ``FINDING_COMMAND``, as ``(line, parsed)``:
    ``parsed`` is ``(key, status, reason)``, or None when the line is malformed (unknown status,
    a key not of the form ``round/reviewer/ID``, or no reason)."""
    found = []
    for line in (body or '').splitlines():
        line = line.strip()
        if not line.startswith(FINDING_COMMAND):
            continue
        match = _COMMAND.fullmatch(line)
        ok = match and KEY.fullmatch(match.group(1)) and match.group(2) in CLOSING
        found.append((line, (match.group(1), match.group(2), match.group(3).strip())
                      if ok else None))
    return found
