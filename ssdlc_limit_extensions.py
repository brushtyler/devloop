"""Common contract for manual limit extensions of a stopped bounded run (#162).

An operator proposes, authorizes and activates an extension of named global
limits or of the run's absolute deadline, without a controller change per case.
Every extension needs an explicit operator authorization bound to the exact
proposal hash, with a mandatory expiry checked at the first atomic activation.
There is no automatic or delegated path.

The extension is applied in place to the selected limits root: the native ticket
(``--limits-profile``) or the selected I2 generation. Counters, observed costs,
unknown usage, reserves and attempts stay unchanged; only the named dimensions
of the frozen profile change. Each extension is recorded in a bound chain in the
envelope binding, so the parent envelope stays reconstructible and verifiable.
A run with a feedback ledger records the same extension in that ledger, so the
recording authority and the admitted generations bound to the parent envelope
follow it (``feedback_lineage``); recording allowances and scope stay separate.
The specialized contracts (verification repair, deadline extension, operator
recovery) and their receipts are untouched. No model is called.

``reverify_after_timeout`` (#167) changes no limit: it authorizes one more
verification of the same captured candidate after a verifier timeout, with
longer timeouts for the timed-out checks only. Activation records the grant in
the task journal; the resumed scheduler runs it without a developer call.
"""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

from ssdlc_deadline_extensions import LIMIT_CAPABILITY as DEADLINE_CAPABILITY
from ssdlc_tasks import TaskError, TaskJournal, atomic_json, digest, ticket_lock, validate_document


# The mirrored entry in the deadline-extension chain (DEADLINE_CAPABILITY) lets the
# historical accounting anchor of reconciled runs follow the extended deadline.
CONTRACT = 'operator-limit-extension-v1'
REVERIFY = 'reverify_after_timeout'
# Operations that change the frozen profile and join the bound chain.
LIMIT_OPERATIONS = ('extend_global_limit', 'extend_deadline')
OPERATIONS = LIMIT_OPERATIONS + (REVERIFY,)
CHECK_TIMEOUT = 'check_timeout.'
MONEY = ('metered_usd', 'cli_estimate_usd')
COUNTS = ('invocations', 'development_attempts')
CAPS = ('role_invocations', 'phase_invocations')
STOPPED = ('ESCALATED_TO_SUPERVISOR', 'FAILED', 'HALTED_BUDGET_EXCEEDED')
STORE = 'limit-extensions'
PENDING = 'limits/extension-pending.json'
# Run inputs a proposal binds besides limits, journals and selector.
INPUTS = ('state.json', '02-design/plan.json', '00-recon/quality-contract.json',
          '01-po/requirements.md', '02-design/adr.md', '02-design/threat-model.md')
# Feedback dispositions that still wait for an automatic or native step bound to
# the exact source counters and journals: an extension would invalidate them.
FEEDBACK_PENDING = ('WAITING', 'NATIVE_AMENDMENT_REQUIRED', 'NATIVE_REPAIR_RETURN_REQUIRED',
                    'OWNER_REVALIDATION_REQUIRED')
ENTRY_FIELDS = {'contract', 'operation', 'generation', 'proposal_sha256', 'authorization_sha256',
                'source_policy_sha256', 'changes'}
DEADLINE_FIELDS = {'capability', 'started_at', 'old_deadline_at', 'new_deadline_at',
                   'old_wall_seconds', 'new_wall_seconds', 'source_policy_sha256'}


class LimitExtensionError(TaskError):
    pass


def _hex(value, size=64):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{%d}' % size, value) is not None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


# --- bound chain ---------------------------------------------------------

def unwind(envelope, generation):
    """Return the envelope before the extensions applied to this limits root.

    Entries are removed from the end while they belong to ``generation`` (None
    for a native ticket). Each removal must restore exactly the recorded parent
    envelope. Inherited entries of an earlier generation stay: they are part of
    the envelope that generation's selector already binds.
    """
    current = copy.deepcopy(envelope)
    binding = current.get('binding')
    if not isinstance(binding, dict) or 'limit_extensions' not in binding:
        return current
    entries = binding['limit_extensions']
    if not isinstance(entries, list) or not entries or len(entries) > 1000:
        raise LimitExtensionError('Invalid limit extension chain')
    while entries and isinstance(entries[-1], dict) and entries[-1].get('generation') == generation:
        entry = entries.pop()
        if (set(entry) - {'deadline'} != ENTRY_FIELDS or entry['contract'] != CONTRACT
                or entry['operation'] not in LIMIT_OPERATIONS or not _hex(entry['proposal_sha256'])
                or not _hex(entry['authorization_sha256']) or not _hex(entry['source_policy_sha256'])
                or not isinstance(entry['changes'], list) or not entry['changes']
                or ('deadline' in entry) != (entry['operation'] == 'extend_deadline')):
            raise LimitExtensionError('Limit extension entry differs from its contract')
        policy = current['policy']
        if entry['operation'] == 'extend_deadline':
            mirrored = binding.get('deadline_extensions')
            if (not isinstance(mirrored, list) or not mirrored or mirrored[-1] != entry['deadline']
                    or set(entry['deadline']) != DEADLINE_FIELDS
                    or entry['deadline']['capability'] != DEADLINE_CAPABILITY
                    or policy.get('wall_seconds') != entry['deadline']['new_wall_seconds']
                    or entry['changes'] != [dict(dimension='deadline_at', old=entry['deadline']['old_deadline_at'],
                                                 new=entry['deadline']['new_deadline_at'])]):
                raise LimitExtensionError('Deadline extension differs from its bound chain')
            mirrored.pop()
            if not mirrored:
                del binding['deadline_extensions']
            policy['wall_seconds'] = entry['deadline']['old_wall_seconds']
        else:
            for change in reversed(entry['changes']):
                if not isinstance(change, dict) or set(change) != {'dimension', 'old', 'new'}:
                    raise LimitExtensionError('Limit extension change is malformed')
                container, key = _locate(policy, change['dimension'])
                if container.get(key) != change['new']:
                    raise LimitExtensionError('Limit extension differs from the frozen profile')
                container[key] = change['old']
        if not entries:
            del binding['limit_extensions']
        if digest(current) != entry['source_policy_sha256']:
            raise LimitExtensionError('Limit extension chain does not restore its parent envelope')
    return current


def _locate(policy, dimension):
    if not isinstance(dimension, str):
        raise LimitExtensionError('Unknown limit dimension')
    if dimension in MONEY or dimension in COUNTS:
        return policy, dimension
    family, _, key = dimension.partition('.')
    if family in CAPS and key and isinstance(policy.get(family), dict):
        return policy[family], key
    raise LimitExtensionError('Unknown limit dimension')


def entries(envelope):
    return copy.deepcopy(envelope.get('binding', {}).get('limit_extensions', []))


def feedback_lineage(state, envelope):
    """(limits, profile) digests this envelope had before the extensions the feedback ledger recorded.

    The first pair is the envelope itself. A recorded extension extends the
    lineage only when it produced the previous pair and is bound in the
    envelope's own chain (which ``LimitsStore._load`` validates), so a feedback
    authority frozen on, or a generation admitted with, the parent envelope
    keeps exactly that authority after an operator limit extension.
    """
    pairs = [(digest(envelope), digest(envelope['policy']))]
    rows = state.get('limit_extensions') if isinstance(state, dict) else None
    if not isinstance(rows, list) or not rows:
        return pairs
    bound = {(e.get('proposal_sha256'), e.get('authorization_sha256'))
             for e in entries(envelope) if isinstance(e, dict)}
    for row in reversed(rows):
        if (isinstance(row, dict) and row.get('contract') == CONTRACT
                and (row.get('limits_sha256'), row.get('profile_sha256')) == pairs[-1]
                and (row.get('proposal_sha256'), row.get('authorization_sha256')) in bound):
            pairs.append((row.get('parent_limits_sha256'), row.get('parent_profile_sha256')))
    return pairs


# --- source ---------------------------------------------------------------

def _ticket(path):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in [path, *path.parents]):
        raise LimitExtensionError('Symlink in limit extension ticket path')
    path = path.resolve()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', path.name) or path.name in ('.', '..'):
        raise LimitExtensionError('Invalid ticket')
    return path


def _json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise LimitExtensionError('Missing or unsafe file: ' + path.name)
    return json.loads(path.read_text())


def _root(ticket):
    """Locate the limits root without loading it: native ticket or selected generation."""
    if (ticket / 'feedback').is_symlink():
        raise LimitExtensionError('Unsafe feedback ledger')
    if (ticket / 'execution/active.json').exists():
        from ssdlc_transitions import TransitionStore
        projection = TransitionStore(ticket / 'execution').load()
        identity = projection.get('transition_id', '')
        if not _hex(identity, 32):
            raise LimitExtensionError('Selected generation identity is invalid')
        root = ticket / 'execution-runtimes' / identity
        if root.is_symlink() or _json(root / 'generation.json') != projection['runtime_binding']:
            raise LimitExtensionError('Selected generation binding changed')
        return root, 'reconciled', identity, digest(_json(ticket / 'execution/active.json'))
    if (ticket / 'execution/pending.json').exists():
        raise LimitExtensionError('An execution preparation is pending; resolve it first')
    if (ticket / 'continuations').exists():
        raise LimitExtensionError('Only native bounded runs and selected I2 generations are eligible')
    if not (ticket / 'limits/policy.json').is_file() or (ticket / 'limits/policy.json').is_symlink():
        raise LimitExtensionError('The run has no frozen limits profile (--limits-profile)')
    return ticket, 'native', None, None


def _complete_journal(path):
    raw = path.read_bytes() if path.exists() else b''
    if raw and not raw.endswith(b'\n'):
        raise LimitExtensionError('Interrupted journal tail requires reconciliation: ' + path.parent.name)


def _inputs(root):
    """Hashes of the run inputs a proposal is reviewed against; a missing one is null."""
    result = {}
    for ref in INPUTS:
        path = root / ref
        if path.is_symlink() or path.exists() and not path.is_file():
            raise LimitExtensionError('Unsafe run input: ' + ref)
        result[ref] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    return result


def _feedback(ticket):
    """The feedback ledger of the run, read without locks or repairs, or None."""
    if not (ticket / 'feedback').exists():
        return None
    from ssdlc_feedback import FeedbackStore
    try:
        store = FeedbackStore(ticket)
        authority, state = store._read()
    except (TaskError, OSError, ValueError, KeyError, TypeError):
        raise LimitExtensionError('The feedback recording authority is incomplete or invalid; '
                                  'reconcile it first') from None
    path = store.root / 'task-journal.jsonl'
    if authority.get('status') != 'ACTIVE' or state is None:
        raise LimitExtensionError('The feedback recording authority is incomplete; reconcile it first')
    requests = [dict(request_id=identity, disposition=store.disposition(identity))
                for identity in sorted(state.get('requests', {}))]
    return dict(authority_sha256=digest(authority), journal=TaskJournal.head(path), state=state,
                requests=[row for row in requests if row['disposition'] not in ('APPLIED', 'IN_SCOPE_RETURN_APPLIED')])


def consumption(envelope, counters, scheduler):
    """Review facts at proposal time; unknown usage stays null, never 0."""
    from ssdlc_recovery import LimitsStore
    rows = counters['invocations']
    by_role, by_phase = {}, {}
    for row in rows.values():
        by_role[row.get('role')] = by_role.get(row.get('role'), 0) + 1
        by_phase[row.get('phase')] = by_phase.get(row.get('phase'), 0) + 1
    observed, reserves, accounted = {}, {}, {}
    for kind in MONEY:
        known = [row['usage'][kind] for row in rows.values() if row.get('usage', {}).get(kind) is not None]
        observed[kind] = sum(known) if known else None
        reserves[kind] = sum(row.get('conservative_usage', {}).get(kind, 0) for row in rows.values())
        accounted[kind] = sum(LimitsStore._kind_charge(row, kind) for row in rows.values())
    unknown = sorted(identity for identity, row in rows.items()
                     if row.get('status') in ('returned', 'terminated', 'historical')
                     and all(row.get('usage', {}).get(kind) is None for kind in MONEY))
    result = dict(invocations=len(rows), by_role=dict(sorted((str(k), v) for k, v in by_role.items())),
                  by_phase=dict(sorted((str(k), v) for k, v in by_phase.items())),
                  observed_usd=observed, reserved_usd=reserves, accounted_usd=accounted,
                  combined_accounted_usd=LimitsStore.accounted_total(counters, envelope['policy']),
                  combined_reported_usd_cap=counters.get('combined_reported_usd_cap'),
                  unknown_usage_invocations=unknown, transports=len(counters.get('transports', [])),
                  diagnoses=len(counters.get('diagnoses', [])),
                  started_at=counters['started_at'], deadline_at=counters['deadline_at'],
                  development_attempts=None, attempt_limit=None)
    if scheduler is not None:
        result['development_attempts'] = {tid: row['attempts'] for tid, row in sorted(scheduler['tasks'].items())}
        result['attempt_limit'] = scheduler.get('attempt_limit')
    return result


def inspect(ticket, *, clock=time.time):
    """Read-only source facts and eligibility. Writes nothing and repairs nothing."""
    from ssdlc_recovery import LimitsStore
    ticket = _ticket(ticket)
    root, kind, generation, selector = _root(ticket)
    if (root / PENDING).exists() or (root / PENDING).is_symlink():
        raise LimitExtensionError('A limit extension activation is pending; repeat `limits activate`')
    if kind == 'reconciled':
        from ssdlc_reconciliation import resolve_execution
        if resolve_execution(ticket) != root:
            raise LimitExtensionError('Selected generation changed')
    _complete_journal(root / 'limits/task-journal.jsonl')
    envelope, counters = LimitsStore(root, clock)._load(repair=False)
    state = _json(root / 'state.json')
    scheduler_path = root / '03-dev/tasks/task-journal.jsonl'
    scheduler = None
    if scheduler_path.exists():
        _complete_journal(scheduler_path)
        scheduler = _peek(scheduler_path)
    return dict(ticket=ticket, root=root, source=kind, generation=generation, selector_sha256=selector,
                envelope=envelope, counters=counters, state=state, scheduler=scheduler,
                limits_head=TaskJournal.head(root / 'limits/task-journal.jsonl'),
                scheduler_head=TaskJournal.head(scheduler_path) if scheduler is not None else None,
                inputs=_inputs(root), feedback=_feedback(ticket))


def _peek(path):
    return TaskJournal._replay(path.read_bytes())[0]


def eligibility(source):
    """Applicability conditions; the same refusal at proposal and activation."""
    status = source['state'].get('status')
    if status not in STOPPED:
        raise LimitExtensionError('Limit extension requires a stopped run; status is ' + str(status))
    if any(row.get('status') in ('reserved', 'uncertain') for row in source['counters']['invocations'].values()):
        raise LimitExtensionError('Unresolved invocation outcome must be reconciled before an extension')
    scheduler = source['scheduler']
    if scheduler is not None and any(row.get('status') not in ('FAILED', 'COMPLETED', 'PENDING')
                                     for row in scheduler['tasks'].values()):
        raise LimitExtensionError('Active task work must be reconciled before an extension')
    feedback = source['feedback']
    if feedback is not None:
        if any(call.get('status') == 'INTENT' for call in feedback['state'].get('role_calls', [])):
            raise LimitExtensionError('Unknown feedback role outcome must be reconciled before an extension')
        pending = [row['request_id'] for row in feedback['requests'] if row['disposition'] in FEEDBACK_PENDING]
        if pending:
            raise LimitExtensionError('A feedback request still waits for a step bound to the current limits; '
                                      'complete it before an extension: ' + ', '.join(pending)[:120])


def parent(source):
    feedback = source['feedback']
    if feedback is not None:
        # Unresolved requests are listed for the operator: they are not converted.
        feedback = dict(authority_sha256=feedback['authority_sha256'], journal=feedback['journal'],
                        unresolved_requests=copy.deepcopy(feedback['requests']))
    return dict(envelope_sha256=digest(source['envelope']), profile_sha256=digest(source['envelope']['policy']),
                counters_sha256=digest(source['counters']), limits_journal=source['limits_head'],
                scheduler_journal=source['scheduler_head'], selector_sha256=source['selector_sha256'],
                state_status=source['state'].get('status'), inputs=copy.deepcopy(source['inputs']),
                feedback=feedback)


# --- proposal -------------------------------------------------------------

def _evidence_hash(root, ref):
    path = Path(ref) if isinstance(ref, str) else None
    if path is None or path.is_absolute() or '..' in path.parts:
        raise LimitExtensionError('Unsafe timed-out check evidence reference')
    full = root / '03-dev/tasks' / path
    if any(p.is_symlink() for p in [full, *full.parents][:len(path.parts)]) or not full.is_file():
        raise LimitExtensionError('Timed-out check evidence is missing or unsafe')
    return hashlib.sha256(full.read_bytes()).hexdigest()


def reverification(source, task_id):
    """Facts of the captured verifier timeout a reverification is bound to.

    The same candidate of the current attempt, its exact failure and capture,
    each timed-out check with its applied timeout, measured duration and log,
    and the verifications the attempt already used. Exit 124 alone justifies
    nothing: a timed-out check without a measured duration is refused.
    """
    from ssdlc_tasks import VERIFICATIONS_PER_ATTEMPT
    from ssdlc_verification_repair import require_timeout
    scheduler = source['scheduler']
    if scheduler is None or not isinstance(task_id, str) or task_id not in scheduler['tasks']:
        raise LimitExtensionError('A reverification names a task of the task journal')
    row = scheduler['tasks'][task_id]
    if row.get('status') != 'FAILED' or not row.get('failed_attempts') or not scheduler.get('candidate_isolation'):
        raise LimitExtensionError('A reverification requires a failed task with a captured candidate')
    try:
        require_timeout(dict(journal=scheduler, task_id=task_id))
    except TaskError as exc:
        raise LimitExtensionError(str(exc)) from None
    failure = row['failed_attempts'][-1]
    if failure['attempt'] != row['attempts']:
        raise LimitExtensionError('A reverification uses the captured candidate of the current attempt')
    grants = row.get('timeout_reverifications', [])
    if grants and grants[-1].get('status') in ('AUTHORIZED', 'RUNNING'):
        raise LimitExtensionError('A timeout reverification of this task is already authorized')
    used = [r for r in row.get('capture_verifications', []) if r.get('attempt') == failure['attempt']]
    if len(used) >= VERIFICATIONS_PER_ATTEMPT:
        raise LimitExtensionError('The candidate already used its %d verifications; another verification '
                                  'is refused' % VERIFICATIONS_PER_ATTEMPT)
    plan, quality = _json(source['root'] / '02-design/plan.json'), _json(source['root'] / '00-recon/quality-contract.json')
    task = [t for t in plan.get('tasks', []) if t.get('task_id') == task_id]
    if len(task) != 1:
        raise LimitExtensionError('The task is missing from the plan')
    checks = {}
    for check in task[0]['verification']['checks'] + quality['checks']:
        if check['check_id'] in checks:
            raise LimitExtensionError('Duplicate check identity')
        checks[check['check_id']] = check
    timed_out = []
    for result in failure['checks']:
        if result['exit_code'] != 124:
            continue
        if result['check_id'] not in checks:
            raise LimitExtensionError('The timed-out check is missing from the plan or quality contract')
        if not _number(result.get('elapsed_seconds')) or type(result['elapsed_seconds']) is bool:
            raise LimitExtensionError('Exit 124 alone does not justify a longer timeout: the timed-out check '
                                      + result['check_id'][:80] + ' has no measured duration')
        if _evidence_hash(source['root'], result['log_ref']) != result['sha256']:
            raise LimitExtensionError('Timed-out check evidence changed')
        configured, applied = checks[result['check_id']]['timeout_sec'], result.get('applied_timeout_sec')
        if type(applied) is not int or applied < configured:
            raise LimitExtensionError('The timed-out check ' + result['check_id'][:80]
                                      + ' does not record the timeout it ran with')
        # The configured timeout stays unchanged; the applied one is the timeout
        # this failure actually ran with, longer after a previous reverification.
        timed_out.append(dict(check_id=result['check_id'], exit_code=124, timeout_sec=configured,
                              applied_timeout_sec=applied, elapsed_seconds=result['elapsed_seconds'],
                              log_ref=result['log_ref'], log_sha256=result['sha256']))
    return dict(task_id=task_id, attempt=failure['attempt'], failure_sha256=digest(failure),
                capture_sha256=row['capture_sha256'], candidate_tree=failure['tree'],
                verifications_used=len(used), verification_limit=VERIFICATIONS_PER_ATTEMPT,
                invocation_timeout_seconds=source['envelope']['policy']['invocation_timeout_seconds'],
                timed_out_checks=timed_out)


def _timeouts(source, requested, task_id):
    """Longer timeouts of timed-out checks only, within invocation_timeout_seconds."""
    facts = reverification(source, task_id)
    timed_out = {c['check_id']: c for c in facts['timed_out_checks']}
    seen, changes = set(), []
    for item in requested:
        if not isinstance(item, dict) or set(item) - {'dimension', 'old', 'new'} or 'dimension' not in item:
            raise LimitExtensionError('A change names its dimension and new value')
        dimension, new = item['dimension'], item.get('new')
        if dimension in seen:
            raise LimitExtensionError('Duplicate limit dimension')
        seen.add(dimension)
        check = dimension[len(CHECK_TIMEOUT):] if isinstance(dimension, str) and dimension.startswith(CHECK_TIMEOUT) else None
        if check not in timed_out:
            raise LimitExtensionError('Only a timed-out check of the failed verification may receive a longer '
                                      'timeout: ' + str(dimension)[:80])
        old, applied = timed_out[check]['timeout_sec'], timed_out[check]['applied_timeout_sec']
        if 'old' in item and item['old'] != old:
            raise LimitExtensionError('Declared old value differs from the check timeout: ' + dimension[:80])
        if type(new) is not int or not applied < new <= facts['invocation_timeout_seconds']:
            raise LimitExtensionError('A reverification timeout must be longer than the timed-out one and within '
                                      'invocation_timeout_seconds')
        changes.append(dict(dimension=dimension, old=old, new=new))
    return changes, copy.deepcopy(source['envelope']['policy'])


def _changes(source, operation, requested, now, task_id=None):
    """Exact old/new values of the named dimensions, and the resulting profile."""
    if operation not in OPERATIONS:
        raise LimitExtensionError('Unknown limit extension operation')
    if not isinstance(requested, list) or not requested or len(requested) > 32:
        raise LimitExtensionError('An extension names at least one dimension')
    if operation == REVERIFY:
        return _timeouts(source, requested, task_id)
    policy = copy.deepcopy(source['envelope']['policy'])
    seen, changes = set(), []
    for item in requested:
        if not isinstance(item, dict) or set(item) - {'dimension', 'old', 'new'} or 'dimension' not in item:
            raise LimitExtensionError('A change names its dimension and new value')
        dimension, new = item['dimension'], item.get('new')
        if dimension in seen:
            raise LimitExtensionError('Duplicate limit dimension')
        seen.add(dimension)
        if dimension == 'combined_reported_usd_cap':
            raise LimitExtensionError('combined_reported_usd_cap cannot be modified by this contract')
        if operation == 'extend_deadline':
            if dimension != 'deadline_at' or len(requested) != 1:
                raise LimitExtensionError('extend_deadline changes only the absolute deadline')
            old = source['counters']['deadline_at']
            if (not _number(new) or new <= old or not float(new - old).is_integer()
                    or not (new - old) < 10 ** 9):
                raise LimitExtensionError('Deadline extension requires a later deadline by an integral number of seconds')
            if new <= now:
                raise LimitExtensionError('The proposed deadline has already passed')
            policy['wall_seconds'] += int(new - old)
            changes.append(dict(dimension='deadline_at', old=old, new=new))
            continue
        if dimension == 'deadline_at' or dimension == 'wall_seconds':
            raise LimitExtensionError('The deadline changes only through extend_deadline')
        try:
            container, key = _locate(policy, dimension)
        except LimitExtensionError:
            raise LimitExtensionError('Limit dimension not admitted: ' + str(dimension)[:80]) from None
        if key not in container:
            raise LimitExtensionError('Only an existing cap can be extended: ' + str(dimension)[:80])
        old = container[key]
        if 'old' in item and item['old'] != old:
            raise LimitExtensionError('Declared old value differs from the frozen profile: ' + dimension)
        money = dimension in MONEY
        if (not _number(new) or not money and type(new) is not int or new <= old):
            raise LimitExtensionError('An extension only increases ' + dimension)
        container[key] = new
        changes.append(dict(dimension=dimension, old=old, new=new))
    try:
        validate_document('recovery-limits', policy)
    except TaskError as exc:
        raise LimitExtensionError('Extended profile is invalid: ' + str(exc)[:200]) from None
    if 'development_attempts' in seen and source['scheduler'] is not None:
        limit = source['scheduler'].get('attempt_limit')
        if limit is not None and limit != source['envelope']['policy']['development_attempts']:
            raise LimitExtensionError('Persistent task attempt limit differs from the profile; cannot extend it')
    return changes, policy


def _evidence(value):
    if not isinstance(value, list):
        raise LimitExtensionError('Evidence is a list')
    return copy.deepcopy(value)


def build(source, request, now):
    """Pure host-computed proposal; grants nothing."""
    if not isinstance(request, dict) or set(request) - {'schema_version', 'operation', 'changes', 'cause',
                                                        'evidence', 'actor', 'task_id', 'profile'}:
        raise LimitExtensionError('Unknown field in the extension request')
    if request.get('schema_version', 1) != 1:
        raise LimitExtensionError('Unsupported extension request version')
    operation = request.get('operation')
    task = request.get('task_id')
    if operation == REVERIFY and task is None:
        raise LimitExtensionError('A reverification names its task')
    changes, profile = _changes(source, operation, request.get('changes'), now, task)
    if 'profile' in request and request['profile'] != profile:
        raise LimitExtensionError('The requested profile changes dimensions it does not name; '
                                  'no implicit increase is allowed')
    if task is not None and (source['scheduler'] is None or task not in source['scheduler']['tasks']):
        raise LimitExtensionError('Unknown task reference')
    proposal = dict(schema_version=1, contract=CONTRACT, operation=operation,
                    run=dict(ticket=source['ticket'].name, source=source['source'],
                             generation=source['generation'], task_id=task),
                    parent=parent(source),
                    consumption=consumption(source['envelope'], source['counters'], source['scheduler']),
                    changes=changes, new_profile=profile, cause=request.get('cause'),
                    evidence=_evidence(request.get('evidence')), actor=request.get('actor'),
                    applicability=dict(source_statuses=list(STOPPED), resolved_invocations=True,
                                       quiescent_feedback_ledger=True, quiescent_writers=True,
                                       operator_authorization='exact-proposal-sha256-with-expiry'),
                    created_at=now)
    if operation == 'extend_deadline':
        old_wall = source['envelope']['policy']['wall_seconds']
        proposal['deadline'] = dict(started_at=source['counters']['started_at'],
                                    old_deadline_at=changes[0]['old'], new_deadline_at=changes[0]['new'],
                                    old_wall_seconds=old_wall, new_wall_seconds=profile['wall_seconds'],
                                    expired_at_proposal=now >= changes[0]['old'])
    if operation == REVERIFY:
        proposal['reverification'] = reverification(source, task)
    _schema('limit-extension-proposal', proposal)
    return proposal


def _schema(name, value):
    try:
        validate_document(name, value)
    except TaskError as exc:
        raise LimitExtensionError(name + ' is invalid: ' + str(exc)[:200]) from None


def validate(proposal, source, now):
    """The proposal still describes exactly this source: same inputs, generation and consumption."""
    _schema('limit-extension-proposal', proposal)
    if proposal['operation'] not in OPERATIONS:
        raise LimitExtensionError('Unknown limit extension operation')
    eligibility(source)
    expected_run = dict(ticket=source['ticket'].name, source=source['source'], generation=source['generation'],
                        task_id=proposal['run']['task_id'])
    if proposal['run'] != expected_run:
        raise LimitExtensionError('The proposal belongs to another run or generation')
    if proposal['parent'] != parent(source):
        raise LimitExtensionError('Generation, inputs or consumption changed since the proposal')
    if proposal['consumption'] != consumption(source['envelope'], source['counters'], source['scheduler']):
        raise LimitExtensionError('Consumption changed since the proposal')
    changes, profile = _changes(source, proposal['operation'], [dict(dimension=c['dimension'], old=c['old'],
                                new=c['new']) for c in proposal['changes']], now, proposal['run']['task_id'])
    if changes != proposal['changes']:
        raise LimitExtensionError('Proposal changes differ from the frozen profile')
    if proposal['new_profile'] != profile:
        raise LimitExtensionError('The proposed profile changes dimensions it does not name; '
                                  'no implicit increase is allowed')
    if (proposal['operation'] == REVERIFY
            and proposal['reverification'] != reverification(source, proposal['run']['task_id'])):
        raise LimitExtensionError('The reverification differs from the captured timeout')
    if proposal['operation'] == 'extend_deadline':
        deadline = proposal['deadline']
        if (deadline['started_at'] != source['counters']['started_at']
                or deadline['old_deadline_at'] != changes[0]['old'] or deadline['new_deadline_at'] != changes[0]['new']
                or deadline['old_wall_seconds'] != source['envelope']['policy']['wall_seconds']
                or deadline['new_wall_seconds'] != profile['wall_seconds']):
            raise LimitExtensionError('Deadline extension differs from the run history')


def _store(ticket):
    path = ticket / STORE
    if path.is_symlink():
        raise LimitExtensionError('Unsafe limit extension store')
    return path


def _folder(ticket, sha):
    if not _hex(sha):
        raise LimitExtensionError('A proposal is named by its SHA-256')
    folder = _store(ticket) / sha
    if folder.is_symlink():
        raise LimitExtensionError('Unsafe limit extension record')
    return folder


def _write_once(path, value, message):
    if path.exists() or path.is_symlink():
        if _json(path) != value:
            raise LimitExtensionError(message)
        return
    atomic_json(path, value)


def propose(ticket, request, *, clock=time.time, fault=lambda point: None):
    """Store an exact proposal under its hash. Nothing is authorized or applied."""
    ticket = _ticket(ticket)
    with ticket_lock(ticket / 'ticket.lock'):
        source = inspect(ticket, clock=clock)
        eligibility(source)
        now = clock()
        proposal = build(source, request, now)
        validate(proposal, source, now)
        sha = digest(proposal)
        fault('proposal_built')
        folder = _folder(ticket, sha)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_once(folder / 'proposal.json', proposal, 'Proposal identity reused with different content')
        fault('proposal_written')
    return dict(proposal_sha256=sha, proposal=proposal, path=str(folder / 'proposal.json'),
                authorized=False, model_dispatches=0)


def _proposal(ticket, sha):
    folder = _folder(ticket, sha)
    proposal = _json(folder / 'proposal.json')
    if digest(proposal) != sha:
        raise LimitExtensionError('Stored proposal differs from its hash')
    _schema('limit-extension-proposal', proposal)
    return folder, proposal


def approve(ticket, sha, *, expires_at, actor, source_fenced, clock=time.time, fault=lambda point: None):
    """Record the operator's explicit authorization of one exact proposal."""
    ticket = _ticket(ticket)
    if source_fenced is not True:
        raise LimitExtensionError('The operator must attest that writers are fenced')
    if not _number(expires_at) or type(expires_at) is bool:
        raise LimitExtensionError('An authorization requires an explicit expires_at')
    with ticket_lock(ticket / 'ticket.lock'):
        folder, proposal = _proposal(ticket, sha)
        now = clock()
        if expires_at <= now:
            raise LimitExtensionError('The authorization expiry has already passed')
        if (folder / 'receipt.json').exists():
            raise LimitExtensionError('The proposal is already active')
        authorization = dict(schema_version=1, contract=CONTRACT, proposal_sha256=sha, authorized=True,
                             source_fenced=True, actor=actor, expires_at=expires_at, approved_at=now)
        _schema('limit-extension-authorization', authorization)
        path = folder / 'authorization.json'
        if path.exists() or path.is_symlink():
            existing = _json(path)
            if {k: v for k, v in existing.items() if k != 'approved_at'} != {
                    k: v for k, v in authorization.items() if k != 'approved_at'}:
                raise LimitExtensionError('A different authorization already exists; propose again')
            return dict(proposal_sha256=sha, authorization=existing, authorization_sha256=digest(existing))
        fault('authorization_built')
        atomic_json(path, authorization)
        fault('authorization_written')
    return dict(proposal_sha256=sha, authorization=authorization, authorization_sha256=digest(authorization))


def _authorization(folder, sha):
    path = folder / 'authorization.json'
    if not path.exists():
        raise LimitExtensionError('Activation requires the exact operator authorization')
    authorization = _json(path)
    _schema('limit-extension-authorization', authorization)
    if authorization['proposal_sha256'] != sha:
        raise LimitExtensionError('The authorization binds a different proposal')
    return authorization


# --- activation -----------------------------------------------------------

def _extended(source, proposal, authorization_sha, sha):
    envelope = copy.deepcopy(source['envelope'])
    envelope['policy'] = copy.deepcopy(proposal['new_profile'])
    counters = copy.deepcopy(source['counters'])
    entry = dict(contract=CONTRACT, operation=proposal['operation'], generation=source['generation'],
                 proposal_sha256=sha, authorization_sha256=authorization_sha,
                 source_policy_sha256=digest(source['envelope']), changes=copy.deepcopy(proposal['changes']))
    if proposal['operation'] == 'extend_deadline':
        d = proposal['deadline']
        entry['deadline'] = dict(capability=DEADLINE_CAPABILITY, started_at=d['started_at'],
                                 old_deadline_at=d['old_deadline_at'], new_deadline_at=d['new_deadline_at'],
                                 old_wall_seconds=d['old_wall_seconds'], new_wall_seconds=d['new_wall_seconds'],
                                 source_policy_sha256=digest(source['envelope']))
        envelope['binding'].setdefault('deadline_extensions', []).append(copy.deepcopy(entry['deadline']))
        counters['deadline_at'] = d['new_deadline_at']
    envelope['binding'].setdefault('limit_extensions', []).append(entry)
    counters['policy_sha256'] = digest(envelope)
    attempts = None
    if source['scheduler'] is not None and 'attempt_limit' in source['scheduler']:
        for change in proposal['changes']:
            if change['dimension'] == 'development_attempts':
                attempts = dict(old=change['old'], new=change['new'])
    if unwind(envelope, source['generation']) != unwind(source['envelope'], source['generation']):
        raise LimitExtensionError('Extension chain does not restore its parent')
    feedback = None
    if source['feedback'] is not None:
        feedback = dict(contract=CONTRACT, proposal_sha256=sha, authorization_sha256=authorization_sha,
                        generation=source['generation'], parent_limits_sha256=digest(source['envelope']),
                        limits_sha256=digest(envelope), parent_profile_sha256=digest(source['envelope']['policy']),
                        profile_sha256=digest(envelope['policy']))
    return envelope, counters, attempts, feedback


def _grant(proposal, sha, authorization_sha):
    """The task-journal grant of one reverification of the same captured candidate."""
    facts = proposal['reverification']
    return dict(contract=CONTRACT, operation=REVERIFY, proposal_sha256=sha, authorization_sha256=authorization_sha,
                task_id=facts['task_id'], attempt=facts['attempt'], failure_sha256=facts['failure_sha256'],
                capture_sha256=facts['capture_sha256'],
                invocation_timeout_seconds=facts['invocation_timeout_seconds'],
                timeouts={c['dimension'][len(CHECK_TIMEOUT):]: dict(old=c['old'], new=c['new'])
                          for c in proposal['changes']},
                status='AUTHORIZED')


def _record_reverification(root, grant, parent_head):
    """Append the grant to the task journal once; a replay only confirms it."""
    journal = TaskJournal(root / '03-dev/tasks')
    tasks = journal.load()
    if tasks is None or grant['task_id'] not in tasks.get('tasks', {}):
        raise LimitExtensionError('Task journal disappeared during reverification activation')
    row = tasks['tasks'][grant['task_id']]
    recorded = [g for g in row.get('timeout_reverifications', [])
                if g.get('proposal_sha256') == grant['proposal_sha256']]
    if recorded:
        if recorded != [grant]:
            raise LimitExtensionError('Task journal differs from the authorized reverification')
        return
    if dict(sequence=journal.sequence, sha256=journal.previous) != parent_head:
        raise LimitExtensionError('Task journal moved during reverification activation')
    row.setdefault('timeout_reverifications', []).append(copy.deepcopy(grant))
    journal.append('TIMEOUT_REVERIFICATION_AUTHORIZED', tasks)


def _reverification_recorded(root, proposal, sha, authorization_sha):
    path = root / '03-dev/tasks/task-journal.jsonl'
    tasks = _peek(path) if path.is_file() and not path.is_symlink() else None
    row = (tasks or {}).get('tasks', {}).get(proposal['run']['task_id'], {})
    return any(g.get('proposal_sha256') == sha and g.get('authorization_sha256') == authorization_sha
               for g in row.get('timeout_reverifications', []))


def admitted_reverification(root, grant):
    """Check a task-journal grant against sources independent of the task journal.

    The scheduler calls it before a reverification runs. The grant must be
    exactly the one its stored proposal, operator authorization and activation
    receipt produce, and every longer timeout must stay within the
    ``invocation_timeout_seconds`` of the frozen limits policy. A grant edited
    in the task journal, its bound included, is refused.
    """
    from ssdlc_recovery import LimitsStore
    root = Path(root)
    store = LimitsStore(root)
    generation = store.generation()
    if generation is None:
        ticket = root
    elif root.name == generation and root.parent.name == 'execution-runtimes':
        ticket = root.parents[1]
    else:
        raise LimitExtensionError('The limits root is not a selected generation of its ticket')
    sha = grant.get('proposal_sha256')
    folder, proposal = _proposal(ticket, sha)
    if proposal['operation'] != REVERIFY or proposal['run']['generation'] != generation:
        raise LimitExtensionError('The grant names another proposal')
    authorization = _authorization(folder, sha)
    authorization_sha = digest(authorization)
    receipt = _json(folder / 'receipt.json')
    if (receipt.get('contract') != CONTRACT or receipt.get('proposal_sha256') != sha
            or receipt.get('authorization_sha256') != authorization_sha or receipt.get('generation') != generation):
        raise LimitExtensionError('The grant has no matching activation receipt')
    expected = _grant(proposal, sha, authorization_sha)
    if {k: v for k, v in grant.items() if k != 'status'} != {k: v for k, v in expected.items() if k != 'status'}:
        raise LimitExtensionError('The grant differs from its authorized proposal')
    envelope, _ = store._load(repair=False)
    bound = envelope['policy']['invocation_timeout_seconds']
    if grant['invocation_timeout_seconds'] > bound or any(t['new'] > bound for t in grant['timeouts'].values()):
        raise LimitExtensionError('The grant exceeds invocation_timeout_seconds of the frozen policy')


def _conserved(before, after):
    """Only the policy digest and, for a deadline, the deadline may differ."""
    strip = lambda state: {k: v for k, v in state.items() if k not in ('policy_sha256', 'deadline_at')}
    return strip(before) == strip(after) and before['started_at'] == after['started_at']


def _publish_mode(root, parent_sha, new_sha):
    path = root / 'execution-mode.json'
    if path.exists() or path.is_symlink():
        marker = _json(path)
        if marker.get('policy_sha256') not in (parent_sha, new_sha) or marker.get('kind') != 'bounded-i2':
            raise LimitExtensionError('Persistent bounded mode changed during extension activation')
        if marker != dict(kind='bounded-i2', policy_sha256=new_sha, status='ACTIVE'):
            atomic_json(path, dict(kind='bounded-i2', policy_sha256=new_sha, status='ACTIVE'))
    state_path = root / 'state.json'
    state = _json(state_path)
    recorded = state.get('bounded_execution')
    if recorded is not None:
        if recorded.get('policy_sha256') not in (parent_sha, new_sha):
            raise LimitExtensionError('Ticket bounded execution binding changed during extension activation')
        if recorded != dict(kind='bounded-i2', policy_sha256=new_sha):
            state['bounded_execution'] = dict(kind='bounded-i2', policy_sha256=new_sha)
            atomic_json(state_path, state)


def _record_feedback(ticket, entry, parent_head):
    """Append the extension to the feedback ledger once; a replay only confirms it.

    The caller holds the feedback locks. Only a torn tail that is a prefix of
    this very event is trimmed (and kept for audit, as TaskJournal does); any
    other incomplete history is refused.
    """
    path = ticket / 'feedback/task-journal.jsonl'
    if path.is_symlink():
        raise LimitExtensionError('Unsafe feedback history')
    raw = path.read_bytes()
    complete = raw[:raw.rfind(b'\n') + 1]
    state, sequence, previous = TaskJournal._replay(complete)
    if state is None:
        raise LimitExtensionError('Feedback history disappeared during extension activation')
    recorded = [row for row in state.get('limit_extensions', [])
                if isinstance(row, dict) and row.get('proposal_sha256') == entry['proposal_sha256']]
    if recorded:
        if recorded != [entry] or raw != complete:
            raise LimitExtensionError('Feedback ledger differs from the authorized extension')
        return
    if dict(sequence=sequence, sha256=previous) != parent_head:
        raise LimitExtensionError('Feedback ledger moved during extension activation')
    extended = copy.deepcopy(state)
    extended.setdefault('limit_extensions', []).append(copy.deepcopy(entry))
    event = {'sequence': sequence + 1, 'previous': previous, 'event': 'FEEDBACK_LIMITS_EXTENDED', 'state': extended}
    line = (json.dumps(dict(event, sha256=digest(event)), sort_keys=True) + '\n').encode()
    if not line.startswith(raw[len(complete):]):
        raise LimitExtensionError('Incomplete feedback history requires reconciliation')
    journal = TaskJournal(ticket / 'feedback')
    journal.load()
    journal.append('FEEDBACK_LIMITS_EXTENDED', extended)


def _complete(ticket, root, intent, fault):
    """Idempotent steps from the published intent; never repeats an effect."""
    if intent.get('reverification') is not None:
        # No limit changes: the profile, the limits journal and the chain stay as they are.
        _record_reverification(root, intent['reverification'], intent['parent_scheduler_journal'])
        fault('scheduler_updated')
        return
    parent_sha, envelope = intent['parent_envelope_sha256'], intent['new_envelope']
    new_sha = digest(envelope)
    policy_path = root / 'limits/policy.json'
    current = digest(_json(policy_path))
    if current == parent_sha:
        atomic_json(policy_path, envelope)
    elif current != new_sha:
        raise LimitExtensionError('Limit policy changed during extension activation')
    fault('policy_written')
    journal = TaskJournal(root / 'limits')
    state = journal.load()
    if state is None:
        raise LimitExtensionError('Limit journal disappeared during extension activation')
    if state.get('policy_sha256') == parent_sha:
        if dict(sequence=journal.sequence, sha256=journal.previous) != intent['parent_limits_journal']:
            raise LimitExtensionError('Limit journal moved during extension activation')
        extended = copy.deepcopy(state)
        extended['policy_sha256'] = new_sha
        extended['deadline_at'] = intent['deadline_at']
        if not _conserved(state, extended):
            raise LimitExtensionError('Extension would change consumption')
        journal.append('LIMITS_EXTENDED', extended)
    elif state.get('policy_sha256') != new_sha or state.get('deadline_at') != intent['deadline_at']:
        raise LimitExtensionError('Limit journal differs from the authorized extension')
    fault('journal_appended')
    attempts = intent.get('attempt_limit')
    if attempts is not None:
        scheduler = TaskJournal(root / '03-dev/tasks')
        tasks = scheduler.load()
        if tasks is None:
            raise LimitExtensionError('Task journal disappeared during extension activation')
        if tasks.get('attempt_limit') == attempts['old']:
            if dict(sequence=scheduler.sequence, sha256=scheduler.previous) != intent['parent_scheduler_journal']:
                raise LimitExtensionError('Task journal moved during extension activation')
            tasks = dict(tasks, attempt_limit=attempts['new'])
            scheduler.append('ATTEMPT_LIMIT_EXTENDED', tasks)
        elif tasks.get('attempt_limit') != attempts['new']:
            raise LimitExtensionError('Task attempt limit differs from the authorized extension')
    fault('scheduler_updated')
    if intent['feedback_extension'] is not None:
        _record_feedback(ticket, intent['feedback_extension'], intent['parent_feedback_journal'])
    fault('feedback_recorded')
    _publish_mode(root, parent_sha, new_sha)
    fault('mode_published')


def _receipt(intent):
    return dict(schema_version=1, contract=CONTRACT, proposal_sha256=intent['proposal_sha256'],
                authorization_sha256=intent['authorization_sha256'], generation=intent['generation'],
                parent_envelope_sha256=intent['parent_envelope_sha256'],
                new_envelope_sha256=digest(intent['new_envelope']), activated_at=intent['activated_at'])


def _clear(path):
    if path.exists() or path.is_symlink():
        path.unlink()
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def activate(ticket, sha, *, clock=time.time, guard=lambda: None, fault=lambda point: None):
    """Apply one exact authorized proposal atomically; a replay never applies it twice."""
    import contextlib
    from ssdlc_recovery import LimitsStore
    ticket = _ticket(ticket)
    with ticket_lock(ticket / 'ticket.lock'):
        folder, proposal = _proposal(ticket, sha)
        authorization = _authorization(folder, sha)
        root, kind, generation, _ = _root(ticket)
        if (kind, generation) != (proposal['run']['source'], proposal['run']['generation']):
            raise LimitExtensionError('The proposal belongs to another generation')
        with contextlib.ExitStack() as stack:
            if (root / '03-dev/tasks').is_dir():
                stack.enter_context(ticket_lock(root / '03-dev/tasks/scheduler.lock'))
            if (ticket / 'feedback').is_dir() and not (ticket / 'feedback').is_symlink():
                # No consultation or feedback ledger write runs during the activation.
                stack.enter_context(ticket_lock(ticket / 'feedback/consultation.lock'))
                stack.enter_context(ticket_lock(ticket / 'feedback/feedback.lock'))
            stack.enter_context(ticket_lock(root / 'limits/limits.lock'))
            pending = root / PENDING
            receipt_path = folder / 'receipt.json'
            if receipt_path.exists():
                receipt = _json(receipt_path)
                if pending.exists():
                    intent = _json(pending)
                    if intent.get('proposal_sha256') != sha or _receipt(intent) != receipt:
                        raise LimitExtensionError('Another limit extension activation is pending')
                    _clear(pending)
                envelope, _ = LimitsStore(root, clock)._load(repair=False)
                if proposal['operation'] == REVERIFY:
                    if not _reverification_recorded(root, proposal, sha, receipt['authorization_sha256']):
                        raise LimitExtensionError('Active receipt differs from the task journal')
                    return dict(status='ACTIVE', proposal_sha256=sha, replayed=True, model_dispatches=0)
                if not any(e.get('proposal_sha256') == sha and e.get('authorization_sha256') == receipt['authorization_sha256']
                           for e in entries(envelope)):
                    raise LimitExtensionError('Active receipt differs from the bound limit chain')
                return dict(status='ACTIVE', proposal_sha256=sha, replayed=True, model_dispatches=0)
            if pending.exists() or pending.is_symlink():
                intent = _json(pending)
                if intent.get('proposal_sha256') != sha:
                    raise LimitExtensionError('Another limit extension activation is pending')
                if intent.get('authorization_sha256') != digest(authorization):
                    raise LimitExtensionError('The authorization changed after activation started')
                replayed = True
            else:
                guard()
                now = clock()
                if not now < authorization['expires_at']:
                    raise LimitExtensionError('The operator authorization expired before activation')
                source = inspect(ticket, clock=clock)
                if source['root'] != root:
                    raise LimitExtensionError('Selected generation changed')
                validate(proposal, source, now)
                grant = None
                if proposal['operation'] == REVERIFY:
                    envelope, counters = copy.deepcopy(source['envelope']), copy.deepcopy(source['counters'])
                    attempts, feedback = None, None
                    grant = _grant(proposal, sha, digest(authorization))
                else:
                    envelope, counters, attempts, feedback = _extended(source, proposal, digest(authorization), sha)
                intent = dict(schema_version=1, contract=CONTRACT, proposal_sha256=sha, reverification=grant,
                              authorization_sha256=digest(authorization), generation=generation,
                              parent_envelope_sha256=digest(source['envelope']), new_envelope=envelope,
                              parent_limits_journal=source['limits_head'],
                              parent_scheduler_journal=source['scheduler_head'],
                              feedback_extension=feedback,
                              parent_feedback_journal=None if feedback is None else source['feedback']['journal'],
                              deadline_at=counters['deadline_at'], attempt_limit=attempts)
                fault('before_intent')
                # The expiry is checked again immediately before the intent, the
                # first atomic step, is published, and nowhere later: a replay
                # only completes these published effects.
                now = clock()
                if not now < authorization['expires_at']:
                    raise LimitExtensionError('The operator authorization expired before activation')
                if proposal['operation'] == 'extend_deadline' and not proposal['deadline']['new_deadline_at'] > now:
                    raise LimitExtensionError('The proposed deadline has already passed')
                intent['activated_at'] = now
                atomic_json(pending, intent)
                replayed = False
            fault('intent_published')
            _complete(ticket, root, intent, fault)
            atomic_json(receipt_path, _receipt(intent))
            fault('receipt_written')
            _clear(pending)
            fault('pending_cleared')
            LimitsStore(root, clock)._load(repair=False)
    if kind == 'reconciled':
        from ssdlc_reconciliation import resolve_execution
        if resolve_execution(ticket) != root:
            raise LimitExtensionError('Selected generation changed after activation')
    return dict(status='ACTIVE', proposal_sha256=sha, replayed=replayed, model_dispatches=0)


# --- status ---------------------------------------------------------------

def expiries(ticket, deadline_at, now):
    """The run deadline and the authorizations bound to the run that an extension does not renew.

    The feedback recording authority keeps its own frozen expiry: extending the
    run deadline never renews it, so it is listed as still expired when it is.
    Read-only; nothing is repaired or renewed.
    """
    run = None if deadline_at is None else dict(deadline_at=deadline_at,
                                                 status='EXPIRED' if now >= deadline_at else 'ACTIVE')
    dependent = []
    if (ticket / 'feedback').exists() or (ticket / 'feedback').is_symlink():
        from ssdlc_feedback import FeedbackStore, recording_status
        row = dict(authorization='feedback-recording', renewed_by_run_extension=False)
        try:
            authority, state = FeedbackStore(ticket)._read()
            if authority.get('status') != 'ACTIVE' or state is None:
                raise LimitExtensionError('incomplete')
            row.update(recording_status(authority['policy'], now))
        except (TaskError, OSError, ValueError, KeyError, TypeError):
            row.update(status='INVALID', reason='FEEDBACK_RECORDING_INCOMPLETE_OR_INVALID')
        dependent.append(row)
    return dict(run_deadline=run, dependent_authorizations=dependent)


def status(ticket, *, clock=time.time):
    """Read-only list of proposals, authorizations, receipts and the bound chain."""
    ticket = _ticket(ticket)
    root, kind, generation, _ = _root(ticket)
    pending = _json(root / PENDING) if (root / PENDING).exists() else None
    records = []
    store = _store(ticket)
    for folder in sorted(store.iterdir()) if store.is_dir() else []:
        if not _hex(folder.name) or folder.is_symlink():
            continue
        record = dict(proposal_sha256=folder.name)
        try:
            proposal = _json(folder / 'proposal.json')
            record.update(operation=proposal['operation'], changes=proposal['changes'],
                          generation=proposal['run']['generation'], actor=proposal['actor'],
                          valid=digest(proposal) == folder.name)
            state = 'PROPOSED'
            if (folder / 'authorization.json').exists():
                authorization = _json(folder / 'authorization.json')
                record['expires_at'] = authorization['expires_at']
                state = 'AUTHORIZED' if clock() < authorization['expires_at'] else 'EXPIRED'
            if pending is not None and pending.get('proposal_sha256') == folder.name:
                state = 'ACTIVATING'
            if (folder / 'receipt.json').exists():
                state = 'ACTIVE'
            record['state'] = state
        except (LimitExtensionError, OSError, ValueError, KeyError, TypeError):
            record['state'] = 'INVALID'
        records.append(record)
    chain, refusal = None, None
    try:
        envelope = _json(root / 'limits/policy.json')
        chain = entries(envelope)
        if pending is None:
            unwind(envelope, generation)
    except (TaskError, OSError, ValueError, KeyError, TypeError) as exc:
        refusal = str(exc)[:240]
    deadline_at = None
    try:
        path = root / 'limits/task-journal.jsonl'
        if not path.is_symlink():
            deadline_at = _peek(path)['deadline_at']
    except (TaskError, OSError, ValueError, KeyError, TypeError):
        pass
    return dict(ticket=ticket.name, source=kind, generation=generation,
                pending=None if pending is None else pending['proposal_sha256'],
                proposals=records, chain=chain, integrity_error=refusal,
                **expiries(ticket, deadline_at, clock()), model_dispatches=0)


def describe(source, *, clock=time.time):
    """Inspection output: facts and eligibility, never authority."""
    try:
        eligibility(source)
        refusal = None
    except LimitExtensionError as exc:
        refusal = str(exc)
    return dict(ticket=source['ticket'].name, source=source['source'], generation=source['generation'],
                status=source['state'].get('status'), eligible=refusal is None, refusal=refusal,
                profile=source['envelope']['policy'], parent=parent(source),
                consumption=consumption(source['envelope'], source['counters'], source['scheduler']),
                chain=entries(source['envelope']),
                **expiries(source['ticket'], source['counters']['deadline_at'], clock()), model_dispatches=0)


def main(argv=None):
    """`limits inspect|propose|approve|activate|status`; no model calls."""
    import argparse
    import sys
    words = list(sys.argv[1:] if argv is None else argv)
    if words and words[0] == 'limits':
        words = words[1:]
    parser = argparse.ArgumentParser(prog='ssdlc limits',
                                     description='Manual limit extensions of a stopped bounded run; no model calls')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('inspect', 'propose', 'approve', 'activate', 'status'):
        command = commands.add_parser(name)
        command.add_argument('--repo', '-r', default='.')
        command.add_argument('--ticket', required=True)
        if name == 'propose':
            command.add_argument('--request', required=True, help='JSON: operation, changes, cause, evidence, actor')
        if name in ('approve', 'activate'):
            command.add_argument('--proposal', required=True, help='SHA-256 printed by limits propose')
        if name == 'approve':
            command.add_argument('--expires-at', required=True, type=float, help='Unix seconds; mandatory')
            command.add_argument('--actor', required=True)
            command.add_argument('--source-fenced', action='store_true',
                                 help='Attest that the controller, supervisor and other writers are stopped')
    args = parser.parse_args(words)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.ticket) or args.ticket in ('.', '..'):
        parser.error('Invalid ticket')
    ticket = Path(args.repo).resolve() / '.ssdlc' / args.ticket
    try:
        if args.command == 'inspect':
            result = describe(inspect(ticket))
        elif args.command == 'propose':
            result = propose(ticket, json.loads(Path(args.request).read_text()))
        elif args.command == 'approve':
            expires = int(args.expires_at) if float(args.expires_at).is_integer() else args.expires_at
            result = approve(ticket, args.proposal, expires_at=expires, actor=args.actor,
                             source_fenced=args.source_fenced)
        elif args.command == 'activate':
            from ssdlc_preparations import docker_quiescent
            def guard():
                if not docker_quiescent(dict(ticket_root=str(ticket), runtime='execution-runtimes',
                                             preparation='execution', source_root=str(ticket))):
                    raise LimitExtensionError('A container still writes to the run; fence every writer first')
            result = activate(ticket, args.proposal, guard=guard)
        else:
            result = status(ticket)
        print(json.dumps(result, sort_keys=True, default=str))
        return 0
    except (TaskError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps(dict(status='LIMITS_REFUSED', reason=str(exc)[:240], model_dispatches=0)), file=sys.stderr)
        return 2
