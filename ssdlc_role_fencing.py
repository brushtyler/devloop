"""Automatic fencing of role containers (#45, part c1). Protocol: docs/writer-fencing.md.

Only runs **admitted** to automatic fencing use it: a run is admitted when its state
was created with ``writer_fencing: v1`` and no refusal has been recorded since. The
marker is written only when a new run creates its state; nothing adds it to an
existing run or ticket, not a resume and not ``ssdlc_writer_gate.open_gate()``.

For a container of an admitted run, the host:

1. reads the payload identity the operator configured (``SSDLC_CONTAINER_PAYLOAD_USER``,
   numeric ``UID:GID``, never root);
2. checks, as that identity and without supplementary groups, that every host path the
   container uses is accessible. It never changes permissions, ownership or credentials;
3. opens the canonical ticket's gate, which must be root-owned ``0600``, and binds its
   identity;
4. checks that the generation the controller runs is the selected one.

Then the container runs the PID 1 holder (``ssdlc_container_holder``) with that
identity. Any refusal is a ``GateError`` with a stable code: the caller records it in
the run's state and the run stays on the manual path. A role of an admitted run that
would run without a role container (local execution, Docker unavailable) is refused the
same way (``ROLE_OUTSIDE_CONTAINER``) before it starts.
"""
import os
from pathlib import Path
import subprocess

import ssdlc_container_holder as holder
import ssdlc_writer_gate as wg
from ssdlc_writer_gate import GateError


MARKER_KEY = 'writer_fencing'
MARKER = 'v1'
REFUSAL_KEY = 'writer_fencing_refusal'
PAYLOAD_USER_ENV = 'SSDLC_CONTAINER_PAYLOAD_USER'
IDENTITY_MISSING = 'CONTAINER_PAYLOAD_IDENTITY_MISSING'
ACCESS_DENIED = 'CONTAINER_PAYLOAD_ACCESS_DENIED'
OUTSIDE_CONTAINER = 'ROLE_OUTSIDE_CONTAINER'
HOLDER_PATH = f'/opt/ssdlc/{holder.HOLDER}'
DETAIL_MAX = 500

# Accesses a host path may need, as test(1) operators.
READ = ('-r', '-x')
WRITE = ('-r', '-w', '-x')
READ_FILE = ('-r',)
WRITE_FILE = ('-r', '-w')

# Runs as the payload identity: prints the first path it cannot use, as test(1) sees it.
_PROBE = 'while [ "$#" -gt 1 ]; do test "$1" "$2" || { printf "%s\\n" "$2"; exit 0; }; shift 2; done'


def admitted(state):
    """Whether a run uses automatic fencing: marked at creation and never refused since."""
    return isinstance(state, dict) and state.get(MARKER_KEY) == MARKER and REFUSAL_KEY not in state


def payload_identity(environ=None):
    """``(uid, gid)`` configured by the operator, never root."""
    environ = os.environ if environ is None else environ
    value = (environ.get('SSDLC_CONTAINER_PAYLOAD_USER', '') or '').strip()
    if not value:
        raise GateError(IDENTITY_MISSING, f'{PAYLOAD_USER_ENV} is not set; automatic fencing needs an '
                                          'unprivileged numeric UID:GID chosen by the operator')
    return holder.parse_payload_user(value)


def probe_access(uid, gid, needs, *, run=subprocess.run):
    """The first host path ``uid:gid`` cannot use, or None.

    ``needs`` maps a path to test(1) operators. The probe runs ``/bin/sh`` with that
    UID and GID and no supplementary groups, as the holder runs the payload, so modes,
    owners and ACLs are judged by the kernel. It only reads metadata.
    """
    argv = ['/bin/sh', '-c', _PROBE, 'ssdlc-access-probe']
    for path, tests in needs.items():
        for flag in tests:
            argv += [flag, str(path)]
    try:
        done = run(argv, user=uid, group=gid, extra_groups=[], cwd='/', env={'PATH': '/usr/bin:/bin'},
                   capture_output=True, text=True, timeout=30, close_fds=True)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise GateError(ACCESS_DENIED, f'access probe could not run as {uid}:{gid}: {type(exc).__name__}') from None
    if done.returncode != 0:
        raise GateError(ACCESS_DENIED, f'access probe failed as {uid}:{gid} with exit code {done.returncode}')
    return done.stdout.strip() or None


class Fencing:
    """What the holder of one role container needs; the gate itself is not held here."""

    def __init__(self, ticket_root, generation, identity, uid, gid):
        self.ticket_root, self.generation, self.identity = Path(ticket_root), generation, identity
        self.uid, self.gid = uid, gid

    def command(self, payload):
        """``(entrypoint, command)`` for ``docker run``: see ``holder_command``."""
        return holder.holder_command(self.ticket_root, self.generation, f'{self.uid}:{self.gid}', payload,
                                     identity=self.identity, holder=HOLDER_PATH)


def prepare(ticket_root, runtime, uid, gid, needs, *, probe=None):
    """Check access, the gate and the generation for a container of ``runtime``.

    ``ticket_root`` is the canonical ticket, ``runtime`` the generation the controller
    runs. The gate is created only on first initialization (``open_gate``) and closed
    again: the lock is the holder's, inside the container.
    """
    denied = (probe or probe_access)(uid, gid, needs)
    if denied:
        raise GateError(ACCESS_DENIED, denied)
    ticket_root, runtime = Path(ticket_root), Path(runtime)
    try:
        expected = runtime.relative_to(ticket_root).as_posix()
    except ValueError:
        raise GateError('generation_invalid', 'the controller runtime is outside the canonical ticket') from None
    gate = wg.open_gate(ticket_root, create=True)
    try:
        holder.check_private(gate)
        identity = dict(gate.identity)
    finally:
        gate.close()
    generation = holder.current_generation(ticket_root)
    if wg.generation_key(generation) != wg.generation_key(expected):
        raise GateError('stale_generation', 'the controller does not run the selected generation')
    return Fencing(ticket_root, generation, identity, uid, gid)


def refusal_record(error, when):
    return dict(code=str(error.code), detail=str(error.detail)[:DETAIL_MAX], refused_at=when)


def describe(error):
    return f'{error.code}: {error.detail}' if error.detail else str(error.code)


def holder_refusal(returncode, stderr):
    """The holder's refusal when it stopped before announcing the payload, else None.

    After the ready line the payload may have run and could print a refusal of its own,
    so only a refusal without that line proves that nothing was started.
    """
    if returncode != holder.REFUSED_EXIT:
        return None
    lines = str(stderr or '').splitlines()
    if any(line.startswith(holder.READY_PREFIX) for line in lines):
        return None
    return holder.parse_refusal(stderr)


def strip_ready(stderr):
    return ''.join(line for line in str(stderr).splitlines(keepends=True) if not line.startswith(holder.READY_PREFIX))
