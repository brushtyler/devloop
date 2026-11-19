"""Turns a reviewer's structured output into a consistent review record, and tracks findings.

The reviewer proposes a verdict; devloop enforces the rules that make the
verdict machine-actionable:

* ``approve`` only with no blocking finding and none of its own earlier findings left
  unresolved (another owner's open finding is the dispatcher's to check);
* ``request_changes`` only with at least one open finding to fix;
* every blocking finding of earlier rounds is accounted for, and one the
  reviewer does not assess stays open;
* the acceptance map of the PR (#134, :mod:`.acceptance`): a criterion ``not_covered`` without a
  reason, a missing map while the issue has criteria, or a map that does not list every criterion
  is a blocking finding of the review, whatever verdict the reviewer proposes;
* a ``needs_operator`` says why (#434, ``operator_reason``): ``trust_policy`` when the PR touches
  the trust policy, ``decision`` for anything else, and a record without the field is a
  ``decision``. Only a ``decision`` stops the loop at once (``flow_verdict``): a ``trust_policy``
  goes on as a request for changes while blocking findings of its own stay open, and otherwise
  as an approval whose merge is the operator's, unless the same reviewer later approves the
  latest round as the operator decided on 2026-11-19 (#461, ``trust_policy_parts``).

Every blocking finding has an owner and an identity, ``round/reviewer/ID`` (``2/codex/R2-1``),
recorded in the marker. Only its owner closes it, in a later review. A finding of the reviewer
``ci`` is closed by the dispatcher when CI passes on the current head, and the operator can close
any finding with an explicit command: both are dispatcher records (``finding`` markers). Another
reviewer's assessment never closes a finding.
"""

import re

from .acceptance import acceptance_findings
from .schema import load_schema, validate, with_defaults

VERDICTS = ('approve', 'request_changes', 'needs_operator')
# Why a reviewer asks for the operator (#434): the PR touches the trust policy, or anything else
# needs the operator's decision. ``''`` with the other verdicts.
TRUST_POLICY = 'trust_policy'
DECISION = 'decision'
# The name of ``needs_operator`` before #248: records published with it are still read, as
# ``needs_operator`` (``current_verdict``).
LEGACY_VERDICT = 'needs_human'
CI = 'ci'
OPERATOR = 'operator'
CLOSING = ('resolved', 'withdrawn')
KEY = re.compile(r'[1-9][0-9]*/[A-Za-z0-9_.-]+/R[1-9][0-9]*-[1-9][0-9]*')
FINDING_COMMAND = '/devloop finding'
_COMMAND = re.compile(r'/devloop finding\s+(\S+)\s+(\S+)\s+(\S.*)')


def current_verdict(verdict):
    """``verdict`` under its current name: ``LEGACY_VERDICT``, from a record published before
    #248, is ``needs_operator``."""
    return 'needs_operator' if verdict == LEGACY_VERDICT else verdict


def trust_policy(review):
    """Whether ``review`` asks for the operator because the PR touches the trust policy
    (#434). A ``needs_operator`` without ``operator_reason``, recorded before #434, is a
    ``decision``."""
    return (current_verdict(review.get('verdict')) == 'needs_operator'
            and review.get('operator_reason') == TRUST_POLICY)


def own_open_findings(review):
    """The blocking findings of its own that ``review`` leaves open: those it opens, and those
    of its reviewer from earlier rounds it marks ``unresolved``. Another owner's finding is not
    its to close (#111), so it does not count."""
    owner = review.get('reviewer')
    earlier = [p for p in review['previous_findings'] if p['status'] == 'unresolved'
               and str(p.get('key', '')).split('/')[1:2] == [owner]]
    return list(review['blocking_findings']) + earlier


def flow_verdict(review):
    """The verdict of ``review`` as devloop acts on it (#434). A ``needs_operator`` for the
    trust policy does not stop the loop: ``request_changes`` while blocking findings of its
    own stay open, otherwise ``approve``, whose merge stays the operator's
    (``trust_policy_parts``). Every other verdict is itself, under its current name: a
    ``needs_operator`` for a decision, or without a reason, stops the loop at once."""
    if trust_policy(review):
        return 'request_changes' if own_open_findings(review) else 'approve'
    return current_verdict(review['verdict'])


def finding_key(round_no, owner, finding_id):
    return f'{round_no}/{owner}/{finding_id}'


def owner_of(review):
    """The owner of a review's findings: ``ci`` for a CI round, else the recorded reviewer."""
    return CI if review.get('source') == 'ci' else review.get('reviewer')


def _with_ids(findings, round_no, start):
    return [dict(f, id=f'R{round_no}-{start + i}') for i, f in enumerate(findings)]


def _keys(review):
    """How many of a record's findings carry a key, and how many there are. Records written
    since #110 carry one on every finding; older ones carry none."""
    items = review['blocking_findings'] + review['previous_findings']
    return sum('key' in f for f in items), len(items)


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
    it; a finding whose round is unknown cannot be attributed. Migration covers only the
    historical prefix: once a record carries keys, a later record without them (or with keys on
    only some findings) closes nothing and only opens its own findings. A closure is final: by
    ``ci`` it closes only a CI finding, by ``operator`` any finding.
    """
    open_, seen, known, problems = {}, {}, set(), []
    keyed_seen = False
    for review in reviews:
        owner, round_no = owner_of(review), review['round']
        if not isinstance(owner, str) or not owner:
            problems.append(f'il round {round_no} non registra il reviewer')
            continue
        with_key, total = _keys(review)
        if with_key == total:
            for p in review['previous_findings']:
                earlier = open_.get(p['key'])
                if p['status'] in CLOSING and earlier is not None and earlier['owner'] == owner:
                    del open_[p['key']]
            own = review['blocking_findings']
        elif with_key or keyed_seen:
            # Not a historical record: its assessments close nothing.
            own = [dict(f, key=f.get('key') or finding_key(round_no, owner, f['id']))
                   for f in review['blocking_findings']]
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
        keyed_seen = keyed_seen or with_key > 0
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


def normalize_review(raw, *, previous_open, round_no, max_rounds, head_sha, ci, reviewer,
                     criteria=(), acceptance_map=None):
    """Validate ``raw`` against the review schema and return a review record.

    ``previous_open`` comes from :func:`open_findings`. The reviewer may name a finding by its
    key or, when unambiguous, by its ID. Its assessment of a finding it does not own is
    recorded, but does not close it.

    ``criteria`` are the acceptance criteria of the issue and ``acceptance_map`` the latest map of
    the PR (None: none recorded). The findings ``acceptance_findings`` derives from them join
    the reviewer's blocking findings, so an ``approve`` with one of them is corrected.

    ``operator_reason`` (#434) is recorded with a ``needs_operator`` only: ``trust_policy`` or
    ``decision``, the latter also when the reviewer gives none. A ``trust_policy`` with another
    verdict turns it into ``needs_operator``: the merge of a PR that touches the trust policy is
    the operator's whatever the verdict.
    """
    # An output without ``operator_reason`` (#434) is read as before: a ``decision``.
    raw = with_defaults('review', raw)
    validate(raw, load_schema('review'))
    notes = []
    derived = acceptance_findings(criteria, acceptance_map)
    if derived:
        notes.append(f'devloop ha aggiunto {len(derived)} finding bloccanti dalla mappa di '
                     'copertura dei criteri di accettazione.')
    blocking = [dict(f, key=finding_key(round_no, reviewer, f['id']))
                for f in _with_ids(raw['blocking_findings'] + derived, round_no, 1)]
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
    # Another owner's open finding alone does not turn an approval into a request for changes
    # (#111): the dispatcher checks every finding before promoting or fixing.
    own_unresolved = [p for p, f in zip(previous, previous_open)
                      if p['status'] == 'unresolved' and f['owner'] == reviewer]
    foreign = [p['key'] for p, f in zip(previous, previous_open)
               if p['status'] in CLOSING and f['owner'] != reviewer]

    verdict = raw['verdict']
    human_reason = raw['human_reason']
    reason = raw['operator_reason']
    if reason == TRUST_POLICY and verdict != 'needs_operator':
        notes.append(f'Verdetto corretto da {verdict} a needs_operator: il reviewer indica che la '
                     'PR tocca la policy di fiducia.')
        verdict = 'needs_operator'
    if verdict == 'approve' and (blocking or own_unresolved):
        verdict = 'request_changes'
        notes.append('Verdetto corretto da approve a request_changes: restano finding bloccanti aperti.')
    elif verdict == 'request_changes' and not (blocking or unresolved):
        verdict = 'needs_operator'
        human_reason = human_reason or 'Il reviewer ha chiesto modifiche senza indicare finding bloccanti.'
        notes.append('Verdetto incoerente: request_changes senza finding bloccanti.')
    elif verdict == 'needs_operator' and not human_reason.strip():
        human_reason = ('Il reviewer non ha indicato le parti della policy di fiducia toccate.'
                        if reason == TRUST_POLICY
                        else 'Il reviewer non ha indicato la decisione richiesta.')
    if foreign:
        notes.append('Restano aperti i finding di altri proprietari che il reviewer considera '
                     'chiusi (' + ', '.join(foreign) + '): li chiude solo chi li ha aperti, la '
                     'CI o l\'operatore.')

    record = {
        'source': 'agent', 'reviewer': reviewer, 'verdict': verdict,
        'summary': raw['summary'], 'human_reason': human_reason,
        'blocking_findings': blocking, 'non_blocking_findings': non_blocking,
        'previous_findings': previous, 'checks_performed': raw['checks_performed'],
        'round': round_no, 'max_rounds': max_rounds, 'head_sha': head_sha, 'ci': ci,
        'note': ' '.join(notes),
    }
    if verdict == 'needs_operator':
        record['operator_reason'] = TRUST_POLICY if reason == TRUST_POLICY else DECISION
    return record


def last_round(reviews):
    """The number of the latest round, 0 before the first. A round is one evaluation of the
    candidate: a CI failure, or the reviews of every reviewer the level requires (#111)."""
    return max((r['round'] for r in reviews), default=0)


def current_round(reviews):
    """The records of the latest round, in order, one per owner: a second record of the same
    owner in the round is a duplicate and does not count."""
    latest, records, owners = last_round(reviews), [], set()
    for record in reviews:
        if record['round'] == latest and owner_of(record) not in owners:
            owners.add(owner_of(record))
            records.append(record)
    return records


def round_verdict(records):
    """The verdict of a complete round, from the ``flow_verdict`` of each record:
    ``needs_operator`` if any reviewer asked for the operator's decision, else
    ``request_changes`` if any asked for changes, a ``needs_operator`` for the trust policy with
    findings of its own included (#434), else ``approve``. Whether the merge of an approved PR
    is the operator's for the trust policy is ``trust_policy_parts``."""
    verdicts = {flow_verdict(r) for r in records}
    for verdict in ('needs_operator', 'request_changes'):
        if verdict in verdicts:
            return verdict
    return 'approve'


def _closed_by_owner(key, owner, round_no, reviews):
    """Whether ``owner`` closed the finding ``key``, opened by or carried in its record of round
    ``round_no``, with a review of its own of a later round: never a closure of the operator or
    of the CI."""
    return bool(key) and any(
        owner_of(r) == owner and r['round'] > round_no
        and any(p.get('key') == key and p.get('status') in CLOSING
                for p in r.get('previous_findings', ()))
        for r in reviews)


def _trust_cleared(owner, reviews, final):
    """Whether the ``trust_policy`` of ``owner`` no longer holds the merge (#461): ``owner``
    approves the head of ``final``, the latest round, without the trust policy, and closed with
    its own reviews every finding its ``trust_policy`` records left open. That no reviewer of
    ``final`` gives ``trust_policy`` is the caller's to check."""
    if not any(owner_of(r) == owner and current_verdict(r.get('verdict')) == 'approve'
               for r in final):
        return False
    return all(_closed_by_owner(f.get('key'), owner, record['round'], reviews)
               for record in reviews if owner_of(record) == owner and trust_policy(record)
               for f in own_open_findings(record))


def trust_policy_parts(reviews):
    """The parts of the trust policy the reviewers of ``reviews`` say the PR touches (#434), as
    ``[(reviewer, round, human_reason)]``: the latest ``trust_policy`` record of each reviewer
    whose ``trust_policy`` still holds the merge, in order of round. Empty when none does, and
    then devloop merges as for any other PR.

    A ``trust_policy`` holds the merge for good, as after a conclave restart, unless the
    operator's decision of 2026-11-19 (#461, ADR 0005, D10) clears it: the reviewer who gave it
    approves the head of the latest round, it closed with its own reviews the findings its
    ``trust_policy`` records left open (not the operator, not the CI), and no reviewer of that
    round gives ``trust_policy``. One ``trust_policy`` in the latest round, even in a record
    that duplicates another of its reviewer, keeps every earlier one."""
    latest = {}
    for record in reviews:
        if trust_policy(record):
            latest[owner_of(record)] = record
    final = current_round(reviews)
    if not any(trust_policy(r) for r in reviews if r['round'] == last_round(reviews)):
        latest = {owner: r for owner, r in latest.items()
                  if not _trust_cleared(owner, reviews, final)}
    found = sorted(latest.values(), key=lambda r: r['round'])
    return [(owner_of(r), r['round'], (r.get('human_reason') or '').strip()) for r in found]


def ci_failure_review(*, failed_checks, log_tail, previous_open, round_no, max_rounds, head_sha):
    """A review record that sends a PR with failing CI back to the implementer."""
    names = ', '.join(failed_checks) or 'CI'
    finding = {'id': f'R{round_no}-1', 'key': finding_key(round_no, CI, f'R{round_no}-1'),
               'file': '', 'line': 0,
               'title': f'CI fallita: {names}',
               'detail': log_tail.strip() or 'Log non disponibile.',
               'suggested_fix': 'Correggere la causa del fallimento e far passare la CI.',
               # Written by devloop, not by a reviewer (#405): a CI failure is not a security
               # finding, so that the delegation of rounds may follow a CI round.
               'security': False}
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
