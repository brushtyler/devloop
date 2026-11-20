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
  latest round as the operator decided on 2026-11-19 (#461, ``trust_policy_parts``);
* a blocking finding states its ``evidence`` (#481): the scenario that shows it, or the code and
  the rule it violates, with the quotation. One the reviewer leaves without evidence is recorded
  as non-blocking (``without_evidence``) and does not send the PR to the fix, unless it is one of
  security, which stays blocking: in doubt, devloop fails closed. A ``request_changes`` left with
  nothing to fix by that alone becomes ``unproven``, a neutral verdict: it does not send the PR
  to the fix, but it is no approval, neither for the quorum nor for clearing a ``trust_policy``.

Every blocking finding has an owner and an identity, ``round/reviewer/ID`` (``2/codex/R2-1``),
recorded in the marker. Only its owner closes it, in a later review. A finding of the reviewer
``ci`` is closed by the dispatcher when CI passes on the current head, and the operator can close
any finding with an explicit command: both are dispatcher records (``finding`` markers). Another
reviewer's assessment never closes a finding.

The findings-only reviewer (ADR 0005, D8, integration of 2026-11-19, #416) is the exception:
its record carries ``role`` ``FINDINGS_ONLY``, and its verdict acts only through its blocking
findings (``flow_verdict``), which send the PR to the fix as any other's. Its approval approves
nothing, and it never asks for the operator. Its findings are closed by the reviewers of the
quorum, not by it: in a later round where every reviewer the round's receipt requires marks one
``resolved`` or ``withdrawn`` (``finding_state``); while a reviewer of the quorum keeps one
``unresolved`` its approval becomes a request for changes, as for a finding of its own.
"""

import re

from .acceptance import acceptance_findings
from .policy import receipt_of
from .schema import load_schema, validate, with_defaults

# The verdicts a reviewer gives: those of the schema.
VERDICTS = ('approve', 'request_changes', 'needs_operator')
# The neutral verdict of #481, which only devloop records: a ``request_changes`` whose blocking
# findings devloop all demoted for lack of evidence. It sends the PR neither to the fix nor to
# the operator, and it approves nothing: the quorum and ``_trust_cleared`` need an explicit
# ``approve``. A reviewer cannot give it: the schema does not list it.
UNPROVEN = 'unproven'
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
# The ``role`` of the record of a findings-only review (ADR 0005, D8, #416); the records of the
# reviewers of the quorum have none.
FINDINGS_ONLY = 'findings'


def current_verdict(verdict):
    """``verdict`` under its current name: ``LEGACY_VERDICT``, from a record published before
    #248, is ``needs_operator``."""
    return 'needs_operator' if verdict == LEGACY_VERDICT else verdict


def findings_only(review):
    """Whether ``review`` is the record of a findings-only review (#416)."""
    return review.get('source') == 'agent' and review.get('role') == FINDINGS_ONLY


def trust_policy(review):
    """Whether ``review`` asks for the operator because the PR touches the trust policy
    (#434). A ``needs_operator`` without ``operator_reason``, recorded before #434, is a
    ``decision``. A findings-only review (#416) never does: the trust policy is for the
    reviewers of the quorum to recognize."""
    return (current_verdict(review.get('verdict')) == 'needs_operator'
            and review.get('operator_reason') == TRUST_POLICY and not findings_only(review))


def own_open_findings(review):
    """The blocking findings of its own that ``review`` leaves open: those it opens, and those
    of its reviewer from earlier rounds it marks ``unresolved``. Another owner's finding is not
    its to close (#111), so it does not count; a findings-only finding (#416) counts for a
    reviewer of the quorum, which closes it, and not for the findings-only reviewer."""
    owner = review.get('reviewer')
    quorum = not findings_only(review)
    earlier = [p for p in review['previous_findings'] if p['status'] == 'unresolved'
               and (quorum and p.get('findings_only') is True
                    or not p.get('findings_only') and str(p.get('key', '')).split('/')[1:2]
                    == [owner])]
    return list(review['blocking_findings']) + earlier


def flow_verdict(review):
    """The verdict of ``review`` as devloop acts on it (#434). A ``needs_operator`` for the
    trust policy does not stop the loop: ``request_changes`` while blocking findings of its
    own stay open, otherwise ``approve``, whose merge stays the operator's
    (``trust_policy_parts``). Every other verdict is itself, under its current name: a
    ``needs_operator`` for a decision, or without a reason, stops the loop at once.

    A findings-only review (#416) acts only through its findings: ``request_changes`` when it
    opens blocking ones, otherwise ``approve``, which only leaves the round to the reviewers of
    the quorum: their approvals alone reach it (``_approvals_match``)."""
    if findings_only(review):
        return 'request_changes' if review['blocking_findings'] else 'approve'
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


def proven(finding):
    """Whether a blocking finding of a reviewer stays blocking (#481): it states its
    ``evidence``, or it is one of security, which stays blocking even without one."""
    evidence = finding.get('evidence')
    return finding.get('security') is True or (isinstance(evidence, str) and bool(evidence.strip()))


def _keys(review):
    """How many of a record's findings carry a key, and how many there are. Records written
    since #110 carry one on every finding; older ones carry none."""
    items = review['blocking_findings'] + review['previous_findings']
    return sum('key' in f for f in items), len(items)


class FindingState:
    """``open``: blocking findings still open, oldest first, each with its ``key`` and
    ``owner``. ``known``: the key of every finding ever opened. ``problems``: why the owner of
    some finding cannot be reconstructed. ``quorum_closed``: the findings-only findings (#416)
    the quorum closed, by key: ``{status, by, round}``, ``by`` the reviewers of the quorum."""

    def __init__(self, open_, known, problems, quorum_closed=None):
        self.open = open_
        self.known = known
        self.problems = problems
        self.quorum_closed = quorum_closed or {}


def _quorum_closures(records, open_):
    """The findings-only findings of ``open_`` (#416) that round ``records`` closes, as
    ``{key: {status, by, round}}``: every reviewer the receipt of the round's first agent record
    requires, or without a receipt every reviewer of the round other than a findings-only one,
    assessed it ``resolved`` or ``withdrawn``. A findings-only review, the CI and another
    reviewer outside the quorum do not count."""
    agents = [r for r in records if r.get('source') == 'agent']
    receipt = receipt_of(agents[0]) if agents else None
    quorum = (list(receipt['reviewers']) if receipt is not None
              else [owner_of(r) for r in agents if not findings_only(r)])
    if not quorum:
        return {}
    statuses = {}
    for record in agents:
        owner = owner_of(record)
        if owner not in quorum or findings_only(record):
            continue
        for p in record['previous_findings']:
            statuses.setdefault(p.get('key'), {}).setdefault(owner, p.get('status'))
    closed = {}
    for key, given in statuses.items():
        finding = open_.get(key)
        if (finding is None or not finding.get('findings_only')
                or any(given.get(who) not in CLOSING for who in quorum)):
            continue
        status = 'resolved' if 'resolved' in given.values() else 'withdrawn'
        closed[key] = {'status': status, 'by': ', '.join(quorum),
                       'round': records[0]['round']}
    return closed


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

    A finding a findings-only review opens (#416) is closed neither by that reviewer nor by any
    single other one: only by the quorum of a later round, once that round's records are all
    read (``_quorum_closures``).
    """
    open_, seen, known, problems = {}, {}, set(), []
    keyed_seen = False
    quorum_closed, round_records = {}, []

    def close_round():
        for key, closure in _quorum_closures(round_records, open_).items():
            del open_[key]
            quorum_closed[key] = closure
    for review in reviews:
        if round_records and review['round'] != round_records[0]['round']:
            close_round()
            round_records = []
        round_records.append(review)
        owner, round_no = owner_of(review), review['round']
        if not isinstance(owner, str) or not owner:
            problems.append(f'il round {round_no} non registra il reviewer')
            continue
        with_key, total = _keys(review)
        if with_key == total:
            for p in review['previous_findings']:
                earlier = open_.get(p['key'])
                # A findings-only finding is the quorum's to close (#416), never its owner's.
                if (p['status'] in CLOSING and earlier is not None and earlier['owner'] == owner
                        and not earlier.get('findings_only')):
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
            f = dict(f, owner=owner, **({'findings_only': True} if findings_only(review) else {}))
            open_[f['key']] = f
            seen.setdefault(f['id'], f)
            known.add(f['key'])
    if round_records:
        close_round()
    for closure in closures:
        f = open_.get(closure['key'])
        if f is not None and (closure['by'] == OPERATOR or f['owner'] == CI):
            del open_[closure['key']]
    return FindingState(list(open_.values()), known, problems, quorum_closed)


def open_findings(reviews, closures=()):
    """Blocking findings still open, oldest first (see :func:`finding_state`)."""
    return finding_state(reviews, closures).open


def normalize_review(raw, *, previous_open, round_no, max_rounds, head_sha, ci, reviewer,
                     criteria=(), acceptance_map=None, role=None):
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

    A blocking finding of the reviewer without ``evidence`` (#481), and not of security, is
    recorded among the non-blocking ones with ``without_evidence``, and the note says so. A
    ``request_changes`` left with nothing to fix by that alone becomes ``UNPROVEN``: the
    finding does not send the PR to the fix, and the reviewer, who never approved, gives no
    approval either.

    ``role`` ``FINDINGS_ONLY`` (#416) records a findings-only review: its verdict acts only
    through its findings (``flow_verdict``). For a reviewer of the quorum, a findings-only
    finding it leaves ``unresolved`` counts as one of its own: it cannot approve.
    """
    # An output without ``operator_reason`` (#434) is read as before: a ``decision``.
    raw = with_defaults('review', raw)
    validate(raw, load_schema('review'))
    notes = []
    derived = acceptance_findings(criteria, acceptance_map)
    if derived:
        notes.append(f'devloop ha aggiunto {len(derived)} finding bloccanti dalla mappa di '
                     'copertura dei criteri di accettazione.')
    unproven = [dict(f, without_evidence=True) for f in raw['blocking_findings'] if not proven(f)]
    blocking = [dict(f, key=finding_key(round_no, reviewer, f['id']))
                for f in _with_ids([f for f in raw['blocking_findings'] if proven(f)] + derived,
                                   round_no, 1)]
    non_blocking = _with_ids(unproven + raw['non_blocking_findings'], round_no,
                             len(blocking) + 1)
    if unproven:
        notes.append(f'devloop ha registrato come non bloccanti {len(unproven)} finding che il '
                     'reviewer indicava come bloccanti senza evidenza ('
                     + ', '.join(f['id'] for f in non_blocking[:len(unproven)])
                     + '): non mandano la PR in correzione.')
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
                         'note': p['note'] if p else 'Non valutato dal reviewer.',
                         # The quorum closes it (#416): ``own_open_findings`` reads this.
                         **({'findings_only': True} if finding.get('findings_only') else {})})
    if len(assessed) < len(previous_open):
        notes.append('Il reviewer non ha valutato tutti i finding precedenti: quelli mancanti restano aperti.')
    unresolved = [p for p in previous if p['status'] == 'unresolved']
    # Another owner's open finding alone does not turn an approval into a request for changes
    # (#111): the dispatcher checks every finding before promoting or fixing.
    # A findings-only finding (#416) is the quorum's to close: for a reviewer of the quorum it
    # counts as its own, and for the findings-only reviewer, which closes none, it does not.
    quorum = role != FINDINGS_ONLY
    own_unresolved = [p for p, f in zip(previous, previous_open)
                      if p['status'] == 'unresolved' and quorum
                      and (f['owner'] == reviewer or f.get('findings_only'))]
    foreign = [p['key'] for p, f in zip(previous, previous_open)
               if p['status'] in CLOSING and f['owner'] != reviewer
               and not (quorum and f.get('findings_only'))]

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
    # Another owner's open finding is not the reviewer's to fix (#111): it does not keep the
    # request for changes alive once its own blocking findings were demoted.
    elif verdict == 'request_changes' and unproven and not (blocking or own_unresolved):
        verdict = UNPROVEN
        notes.append('Verdetto corretto da request_changes a unproven: nessun finding bloccante '
                     'ha un\'evidenza. Non manda la PR in correzione, ma non è '
                     'un\'approvazione: non conta per il quorum.')
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
    if role == FINDINGS_ONLY:
        record['role'] = FINDINGS_ONLY
        record['note'] = ' '.join(filter(None, [
            'Review di soli finding: il verdetto non conta per il quorum né come round a sé; i '
            'finding bloccanti vanno in correzione e li chiudono i reviewer del quorum.',
            record['note']]))
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
    findings of its own included (#434), else ``UNPROVEN`` if any reviewer's request for
    changes had no evidence (#481), else ``approve``. An ``UNPROVEN`` round is not approved: it
    lacks an explicit approval, and the PR gets a new review, within the round limit. Whether
    the merge of an approved PR is the operator's for the trust policy is
    ``trust_policy_parts``."""
    verdicts = {flow_verdict(r) for r in records}
    for verdict in ('needs_operator', 'request_changes', UNPROVEN):
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
