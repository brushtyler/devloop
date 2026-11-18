"""Liveness, coverage, activity and next action of a run (workflow-status part 2, #347).

The canonical state of the run, its liveness and its activity are kept apart:

- ``state`` is the native ``state.json`` status, as the engine wrote it, pause at the
  phase boundary included;
- ``liveness`` comes from the kernel. ``RUNNING`` when a process holds the ticket's
  ``writer.gate`` or ``ticket.lock`` (``/proc/locks``); otherwise ``UNVERIFIED`` unless
  every writer of the run is covered, ``STALE`` for work the engine left in progress,
  ``QUIESCENT`` for a run at rest. Without complete coverage it is never ``QUIESCENT``;
- ``activity`` is what the persisted records say is in progress (an invocation, a
  verification) or ``idle``; nothing is deduced from the provider.

A stop keeps the native classification (``ssdlc_supervisor.RECOVERY_MATRIX`` classes)
and the next action carries a command only when every argument is available; an
authorization, a limit value or the parameters of a resume are never invented.

Every observation reads without writing: the gate is located with ``lstat`` and never
opened, the registry is read only if it exists, Docker is only listed.
"""
import os
from pathlib import Path
import stat
import subprocess
import sys
import types

import ssdlc_writer_gate as wg

RUNNING, QUIESCENT, STALE, UNVERIFIED = 'RUNNING', 'QUIESCENT', 'STALE', 'UNVERIFIED'
INVOCATION, VERIFICATION, IDLE = 'invocation', 'verification', 'idle'
TICKET_LOCK = 'ticket.lock'
LOCKS = (wg.GATE_NAME, TICKET_LOCK)

# Declared in state.json by a controller that holds a writer lease for its whole life
# and runs every host command through ``run_host()`` (#345). Without it the controller
# and its host commands are not covered by the gate.
CONTROLLER_KEY, CONTROLLER_PROTOCOL = 'writer_fencing_controller', wg.TREE_PROTOCOL
COMPONENTS = ('writers', 'controller', 'host_trees', 'containers')

# Native ``state.json`` statuses. Any other status is ``unknown`` and, like work in
# progress, never counts as a run at rest.
OUTCOMES = {
    'IN_PROGRESS': 'in_progress',
    'PAUSED_AT_PHASE_BOUNDARY': 'paused',
    'PO_READY': 'ready', 'DESIGN_READY': 'ready', 'PLAN_READY': 'ready',
    'CONTINUATION_IMPORTED': 'ready', 'CONTINUATION_VERIFIED': 'ready',
    'AWAITING_SUPERVISOR_SIGNOFF': 'awaiting_signoff',
    'APPROVED_AND_MERGED': 'completed',
    'FAILED': 'stopped', 'HALTED_BUDGET_EXCEEDED': 'stopped', 'ESCALATED_TO_SUPERVISOR': 'stopped',
    'CONTINUATION_BLOCKED': 'stopped', 'CONTINUATION_CHECKS_FAILED': 'stopped',
}
AT_REST = {'paused', 'ready', 'awaiting_signoff', 'completed', 'stopped'}

# What each native class needs before anyone acts; none of it is read from files.
PREREQUISITES = {
    'transient_environment_provider': ['the supervisor retries read-only preflight within the persisted allowance'],
    'authentication': ['authentication refreshed out of band by the operator'],
    'integrity': ['the operator inspects the evidence and reconciles integrity'],
    'limits': ['a limit explicitly raised by the operator: no value is proposed'],
    'code_defect': ['an allowance explicitly raised by the operator, if needed'],
    'uncertain_outcome': ['the operator inspects and reconciles the invocation outcome'],
    'operator_decision': ['the operator resolves the scope or environment decision'],
    'unsupported_transition': ['the operator chooses a supported entrypoint after the compatibility check'],
}
RESUME = 'resume parameters chosen by the operator (entrypoint, phase, goal): they are not reconstructed from files'


# ---------------------------------------------------------------------------
# Kernel observation (read-only).

def _proc_locks():
    try:
        return Path('/proc/locks').read_text().splitlines(), None
    except OSError as exc:
        return None, f'/proc/locks is not readable: {type(exc).__name__}'


def _holders(lines, info, name):
    """Kernel-held flocks on the inode of ``info`` (blocked waiters are not holders)."""
    found = []
    for line in lines:
        words = line.split()
        if len(words) < 8 or words[1] != 'FLOCK':  # a waiter's line reads ``N: -> FLOCK …``
            continue
        try:
            major, minor, number = words[5].split(':')
            if (int(number), int(major, 16), int(minor, 16)) == (
                    info.st_ino, os.major(info.st_dev), os.minor(info.st_dev)):
                found.append(dict(lock=name, mode=words[3], pid=int(words[4])))
        except ValueError:
            continue
    return found


def _lstat(dirfd, name):
    try:
        return os.stat(name, dir_fd=dirfd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _open_ticket(ticket_root):
    return os.open(ticket_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


class Kernel:
    """Read-only observation of the writers of a ticket and of their coverage.

    ``writers`` lists the kernel holders of the ticket's locks; ``coverage`` says, for
    each component, why its writers are not covered (None when they are). Tests replace
    either method to simulate writers and coverage.
    """

    def __init__(self, docker=subprocess.run):
        self.docker = docker

    def writers(self, ticket_root):
        """``{'holders': [...], 'reason': None | str}``; a reason means the gate cannot be observed."""
        if not sys.platform.startswith('linux'):
            return dict(holders=[], reason='kernel locks are observable only on Linux')
        try:
            dirfd = _open_ticket(ticket_root)
        except OSError as exc:
            return dict(holders=[], reason=f'ticket directory is not readable: {type(exc).__name__}')
        try:
            reason = None
            try:
                wg.check_filesystem(dirfd)
            except wg.GateError as exc:
                reason = exc.detail
            gate, anchor = _lstat(dirfd, wg.GATE_NAME), _lstat(dirfd, wg.ANCHOR_NAME)
            if gate is None:
                reason = reason or 'the ticket has no writer gate: its writers are not observable'
            elif (not stat.S_ISREG(gate.st_mode) or anchor is None or not stat.S_ISREG(anchor.st_mode)
                  or (anchor.st_dev, anchor.st_ino) != (gate.st_dev, gate.st_ino)):
                reason = reason or 'the writer gate is not the inode anchored by its anchor'
                gate = None
            lock = _lstat(dirfd, TICKET_LOCK)
        finally:
            os.close(dirfd)
        lines, unreadable = _proc_locks()
        if lines is None:
            return dict(holders=[], reason=unreadable)
        holders = []
        if gate is not None:
            holders += _holders(lines, gate, wg.GATE_NAME)
        if lock is not None and stat.S_ISREG(lock.st_mode):
            holders += _holders(lines, lock, TICKET_LOCK)
        return dict(holders=holders, reason=reason)

    def coverage(self, ticket_root, generation, fencing):
        """Why each component of the run is not covered by the gate (None when it is)."""
        out = dict(controller=None if fencing.get('controller') else
                   'the controller does not declare that it holds the gate and runs its host commands '
                   f'through run_host() ({CONTROLLER_KEY}: {CONTROLLER_PROTOCOL}, #345)')
        out['host_trees'] = self._host_trees(ticket_root, generation)
        if not fencing.get('admitted'):
            out['containers'] = ('the run is not admitted to automatic fencing (writer_fencing v1 missing or '
                                 'refused): its role containers do not hold the gate')
        elif any(out.values()):
            out['containers'] = 'not checked with Docker: coverage is already incomplete'
        else:
            from ssdlc_fencing_proof import ticket_containers
            out['containers'] = ticket_containers(ticket_root, run=self.docker)[0]
        return out

    @staticmethod
    def _host_trees(ticket_root, generation):
        try:
            dirfd = _open_ticket(ticket_root)
        except OSError as exc:
            return f'ticket directory is not readable: {type(exc).__name__}'
        try:
            records, malformed = wg._records(types.SimpleNamespace(dirfd=dirfd, ticket_root=ticket_root), create=False)
        except wg.GateError as exc:
            return f'the writer gate registry is not readable: {exc.code}'
        finally:
            os.close(dirfd)
        if malformed:
            return f'unreadable writer gate registry entries: {", ".join(malformed)}'
        unproven = wg._unproven_host_trees(records, wg.generation_key(generation))
        return '; '.join(unproven) or None


def fencing(state):
    """The coverage the run declares in ``state.json``: role containers and controller."""
    from ssdlc_role_fencing import admitted, REFUSAL_KEY
    state = state if isinstance(state, dict) else {}
    return dict(admitted=admitted(state),
                controller=state.get(CONTROLLER_KEY) == CONTROLLER_PROTOCOL and REFUSAL_KEY not in state)


# ---------------------------------------------------------------------------
# Classification (pure).

def _coverage(observations, components):
    reasons = dict(components)
    seen = [o['reason'] for o in observations if o.get('reason')]
    reasons['writers'] = '; '.join(dict.fromkeys(seen)) or None
    parts = {name: dict(covered=reasons.get(name) is None and name in reasons,
                        reason=reasons.get(name) if name in reasons else 'not observed')
             for name in COMPONENTS}
    return dict(complete=all(p['covered'] for p in parts.values()), components=parts)


def _writers(observations):
    found = []
    for observation in observations:
        for holder in observation.get('holders') or ():
            if holder not in found:
                found.append(holder)
    return found


def _stop(run, outcome, liveness):
    """The native class of a stop, as ``ssdlc_supervisor.classify`` names it, or None."""
    from ssdlc_recovery import STOPS
    from ssdlc_supervisor import RECOVERY_MATRIX
    status = run.get('status')
    failure = run.get('failure') if isinstance(run.get('failure'), dict) else {}
    category = failure.get('category')
    if liveness == STALE:
        cls, code = 'uncertain_outcome', 'terminal_unbound'
        detail = 'work in progress with no writer left: the controller ended without a recorded outcome'
    elif outcome != 'stopped':
        return None
    # The precedence of the supervisor's table: the recorded category first
    # (authentication, integrity, the recovery stops, a resource limit), then the
    # budget halt, then the escalation, whatever status carries the category.
    elif category in ('AUTH_MISSING', 'AUTH_EXPIRED', 'AUTHENTICATION'):
        cls = 'authentication'
        code = {'AUTH_MISSING': 'auth_missing', 'AUTH_EXPIRED': 'auth_expired'}.get(category, 'auth_unverified')
        detail = None
    elif category == 'INTEGRITY':
        cls, code, detail = 'integrity', 'integrity_failure', None
    elif category in STOPS:
        cls, code, detail = STOPS[category], category.lower(), None
    elif category == 'ENVIRONMENT_RESOURCE':
        cls, code, detail = 'limits', 'resource_limit', None
    elif status == 'HALTED_BUDGET_EXCEEDED':
        cls, code, detail = 'limits', 'budget_exceeded', None
    elif status == 'ESCALATED_TO_SUPERVISOR':
        cls, code, detail = 'operator_decision', 'operator_decision', None
    else:
        # The supervisor binds the other categories to its controller events; without
        # them the outcome stays unbound, as in its own fallback row.
        cls, code = 'uncertain_outcome', 'terminal_unbound'
        detail = (f'failure category {category} is bound to an outcome only by the supervisor\'s events'
                  if category else 'no failure category recorded')
    return {'class': cls, 'reason_code': code, 'mode': RECOVERY_MATRIX[cls]['mode'], 'category': category,
            'detail': detail}


def _activity(tasks, limits, liveness):
    invocations, verifications = [], []
    for node in (tasks or {}).get('nodes') or ():
        if node.get('status') == 'RUNNING':
            invocations.append(dict(source='task journal', task_id=node['task_id']))
        elif node.get('status') == 'VERIFYING':
            verifications.append(dict(source='task journal', task_id=node['task_id']))
    active = (limits or {}).get('status') == 'active'
    for identity, row in sorted(((limits or {}).get('state') or {}).get('invocations', {}).items()) if active else ():
        if isinstance(row, dict) and row.get('status') == 'reserved':
            invocations.append(dict(source='limits journal', invocation_id=identity,
                                    role=row.get('role'), phase=row.get('phase')))
    result = dict(invocations=invocations, verifications=verifications)
    if liveness in (QUIESCENT, STALE):
        reason = ('no writer runs: the recorded work is interrupted, not in progress'
                  if invocations or verifications else None)
        return dict(result, value=IDLE, reason=reason)
    if invocations:
        return dict(result, value=INVOCATION, reason=None)
    if verifications:
        return dict(result, value=VERIFICATION, reason=None)
    return dict(result, value=None, reason=(
        'no invocation or verification is recorded in progress, and the others are not persisted: '
        'inactivity is proven only when no writer runs'))


def _command(argv, repo):
    return dict(argv=argv, cwd=str(Path(repo).absolute()))


def _next_action(ticket, repo, run, outcome, liveness, stop, coverage):
    from ssdlc_supervisor import RECOVERY_MATRIX
    action = dict(action=None, mode='none', command=None, prerequisites=[])
    if liveness == RUNNING:
        action.update(action='Wait: processes of the run hold its locks.')
        return action
    if outcome == 'completed':
        action.update(action='None: the run is approved and merged.')
    elif stop is not None:
        action.update(action=RECOVERY_MATRIX[stop['class']]['action'], mode=stop['mode'],
                      prerequisites=list(PREREQUISITES[stop['class']]) + [RESUME])
    elif outcome == 'paused':
        boundary = run.get('phase_boundary') if isinstance(run.get('phase_boundary'), dict) else {}
        phase = boundary.get('next_phase')
        target = f'--from-phase {phase}' if isinstance(phase, str) and phase.isprintable() else 'the next phase'
        action.update(action=f'Resume from the phase boundary with {target}.', mode='operator-only',
                      prerequisites=[RESUME])
    elif outcome == 'awaiting_signoff':
        action.update(action='Supervisor sign-off: approve, or reject with a reason.', mode='operator-only',
                      command=_command(['./ssdlc', 'approve', ticket], repo),
                      prerequisites=['the supervisor reviews the evidence pack: approval is the supervisor\'s decision',
                                     'rejecting needs the supervisor\'s reason (reject --reason): no command is proposed'])
    elif outcome == 'ready':
        action.update(action='The requested step is ready: continue explicitly with the next entrypoint.',
                      mode='operator-only', prerequisites=[RESUME])
    else:
        action.update(action='Verify by other means whether processes of the run still run before any action.',
                      mode='operator-only')
    if liveness == UNVERIFIED and outcome != 'completed':
        reasons = [p['reason'] for p in coverage['components'].values() if not p['covered']]
        action['prerequisites'].insert(0, 'confirm that no process of the run still runs: liveness is not '
                                          'verifiable (' + '; '.join(reasons) + ')')
    return action


def classify(ticket, repo, run, tasks, limits, observations, components):
    """The ``status`` of a coherent snapshot: state, liveness, coverage, activity, stop and next action.

    ``run`` holds the native ``state.json`` fields (or None without a state),
    ``observations`` the kernel's writer observations taken around the collection,
    ``components`` the reason each component is not covered (None when covered).
    """
    if not run:  # Without state.json no controller declares its coverage.
        components = dict(components, controller='no state.json: no controller declares its coverage')
    run = run or {}
    status = run.get('status')
    outcome = OUTCOMES.get(status, 'unknown') if run else 'absent'
    writers = _writers(observations)
    coverage = _coverage(observations, components)
    if writers:
        liveness = RUNNING
    elif not coverage['complete']:
        liveness = UNVERIFIED
    elif outcome in AT_REST:
        liveness = QUIESCENT
    else:
        # Covered and no lock held: the controller, a gate writer, is verified gone.
        liveness = STALE
    stop = _stop(run, outcome, liveness)
    return dict(state=status, outcome=outcome, liveness=liveness, writers=writers, coverage=coverage,
                activity=_activity(tasks, limits, liveness), stop=stop,
                next_action=_next_action(ticket, repo, run, outcome, liveness, stop, coverage))
