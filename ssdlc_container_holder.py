"""In-container holder of the writer gate (#45, part b). Protocol: docs/writer-fencing.md.

The holder runs as PID 1 of a role container, in place of ``--init``/tini. Before any
payload or inference it, as root:

1. checks the host requirements (Linux, no ``CAP_SYS_PTRACE``) and becomes non-dumpable;
2. opens the gate ``O_RDONLY | O_CLOEXEC`` from the read-only ticket mount, without
   creating or writing anything, refuses a gate that is not root-owned ``0600``, and
   takes ``LOCK_SH``;
3. rereads the generation from the authoritative source (``ssdlc_transitions.runtime_root``)
   and refuses unless it is the one the host passed.

The payload then runs with an unprivileged UID and GID, no supplementary groups,
``no_new_privs`` and no capabilities, bounding set included. It cannot open the gate,
so it can neither take ``LOCK_EX`` nor release or hold the lock. The gate descriptor
stays with PID 1: it is close-on-exec and closed in the child before ``exec``. PID 1
forwards signals, reaps zombies and, when the payload ends, kills and reaps the rest of
its PID namespace before it releases the lock by exiting.

Linux closes PID 1's files before it kills the rest of the namespace, so the lock alone
does not prove that a container has ended: a transition also checks with Docker
(``running_containers``) that no container of the run is running.

Role containers of runs admitted to automatic fencing start this holder
(``ssdlc_role_fencing``, part c1); every other container keeps ``--init``.
"""
import argparse
import ctypes
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

if __name__ == '__main__':
    # Started with ``python3 -I``: the toolkit is this file's directory, nothing else.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ssdlc_writer_gate as wg  # noqa: E402
from ssdlc_writer_gate import GateError


HOLDER = 'ssdlc_container_holder.py'
REFUSED_EXIT = 122
REFUSED_PREFIX = 'ssdlc-holder-refused '
READY_PREFIX = 'ssdlc-holder-ready '
PAYLOAD_PRIVILEGED = 'CONTAINER_PAYLOAD_PRIVILEGED'
PTRACE_CAPABLE = 'CONTAINER_PTRACE_CAPABLE'
NOT_PID1 = 'CONTAINER_HOLDER_NOT_PID1'
NOT_ROOT = 'CONTAINER_HOLDER_NOT_ROOT'
GATE_EXPOSED = 'CONTAINER_GATE_EXPOSED'
DROP_FAILED = 'CONTAINER_PRIVILEGE_DROP_FAILED'
CAP_SYS_PTRACE = 19
CAPABILITY_FIELDS = ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')
# Signals PID 1 forwards to the payload; anything else keeps the kernel's PID 1 rules.
FORWARDED = (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM, signal.SIGUSR1, signal.SIGUSR2,
             signal.SIGWINCH, signal.SIGALRM)

PR_SET_DUMPABLE = 4
PR_CAPBSET_DROP = 24
PR_SET_NO_NEW_PRIVS = 38
PR_CAP_AMBIENT = 47
PR_CAP_AMBIENT_CLEAR_ALL = 4
CAPABILITY_VERSION_3 = 0x20080522


# ---------------------------------------------------------------------------
# Host requirements and identities.

def _status(text=None):
    if text is None:
        text = Path('/proc/self/status').read_text()
    fields = {}
    for line in text.splitlines():
        key, _, value = line.partition(':')
        fields[key.strip()] = value.strip()
    return fields


def capabilities(text=None):
    """Capability sets of this process, as integers, from ``/proc/self/status``."""
    fields = _status(text)
    try:
        return {name: int(fields[name], 16) for name in CAPABILITY_FIELDS}
    except (KeyError, ValueError):
        raise GateError(PTRACE_CAPABLE, 'capability sets cannot be read from /proc/self/status') from None


def check_host_requirements(status=None):
    """Refuse automatic fencing unless the host gives what the holder relies on.

    Linux with flock and /proc, and no ``CAP_SYS_PTRACE`` in any capability set (the
    Docker default). Yama and seccomp are never changed here.
    """
    wg._require_linux()
    held = [name for name, value in capabilities(status).items() if value >> CAP_SYS_PTRACE & 1]
    if held:
        raise GateError(PTRACE_CAPABLE, f'CAP_SYS_PTRACE is present in {", ".join(held)}; '
                                        'a traced holder could lose its gate descriptor')


def parse_payload_user(value):
    """``UID:GID`` of the payload: numeric and never root."""
    if value is None:
        raise GateError(PAYLOAD_PRIVILEGED, 'no payload user: the payload would run as root')
    uid, sep, gid = str(value).partition(':')
    if not sep or not uid.isdigit() or not gid.isdigit():
        raise GateError('invalid_payload_user', 'payload user must be numeric UID:GID')
    uid, gid = int(uid), int(gid)
    if uid == 0 or gid == 0:
        raise GateError(PAYLOAD_PRIVILEGED, 'the payload would run as root; automatic fencing needs an '
                                            'unprivileged UID and GID, use the manual path')
    return uid, gid


def current_generation(ticket_root):
    """The selected generation, following ``runtime_root`` selectors and precedence.

    It is the runtime root relative to the canonical ticket (``.`` for a legacy run).
    A missing or invalid state is a refusal, never a default.
    """
    from ssdlc_transitions import runtime_root
    ticket_root = Path(ticket_root)
    if not ticket_root.is_dir():
        raise GateError('generation_invalid', 'canonical ticket root is missing')
    try:
        root = Path(runtime_root(ticket_root))
    except Exception as exc:
        raise GateError('generation_invalid', f'{type(exc).__name__}: {exc}') from None
    try:
        generation = root.relative_to(ticket_root).as_posix()
    except ValueError:
        raise GateError('generation_invalid', 'selected runtime is outside the canonical ticket') from None
    check_state(root, ticket_root.name)
    return generation


def check_state(root, ticket_id):
    """The selected runtime's ``state.json``: a regular file holding this ticket's state.

    ``runtime_root`` returns a legacy ticket without reading it, so a missing, truncated
    or foreign state would otherwise let the payload start.
    """
    from ssdlc_transitions import _regular
    try:
        state = json.loads(_regular(Path(root), 'state.json').read_text(encoding='utf-8'))
    except Exception as exc:
        raise GateError('generation_invalid', f'state.json is unreadable: {type(exc).__name__}: {exc}') from None
    if not isinstance(state, dict):
        raise GateError('generation_invalid', 'state.json does not hold a JSON object')
    if state.get('ticket_id') != ticket_id:
        raise GateError('generation_invalid', 'state.json belongs to another ticket')


def gate_identity(value):
    if value is None:
        return None
    device, sep, inode = str(value).partition(':')
    if not sep or not device.isdigit() or not inode.isdigit():
        raise GateError('invalid_gate_identity', 'gate identity must be DEVICE:INODE')
    return dict(device=int(device), inode=int(inode))


# ---------------------------------------------------------------------------
# The shared lock, held by PID 1 only.

def check_private(gate):
    """The payload must not be able to open the gate inode through any path."""
    info = os.fstat(gate.fd)
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
        raise GateError(GATE_EXPOSED, f'gate inode is owned by UID {info.st_uid} with mode '
                                      f'{stat.S_IMODE(info.st_mode):04o}; it must be root-owned 0600')


def acquire(gate, ticket_root, generation, *, timeout=30.0, poll=0.05):
    """Take ``LOCK_SH``, then recheck the gate and the generation under it.

    Unlike ``ssdlc_writer_gate.writer`` it writes nothing: the registry stays the host's.
    """
    deadline = time.monotonic() + wg._bounded(timeout)
    while True:
        try:
            fcntl.flock(gate.fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise GateError('writer_wait_timeout', f'gate still exclusively held after {timeout}s') from None
            time.sleep(poll)
    gate.verify()
    if wg.generation_key(current_generation(ticket_root)) != wg.generation_key(generation):
        raise GateError('stale_generation', 'the ticket moved to another generation; the payload must not start')


def hold(ticket_root, generation, *, identity=None, timeout=30.0):
    gate = wg.open_gate(ticket_root, identity, create=False)
    try:
        check_private(gate)
        acquire(gate, ticket_root, generation, timeout=timeout)
    except BaseException:
        gate.close()
        raise
    return gate


# ---------------------------------------------------------------------------
# The payload: unprivileged, without the gate descriptor.

_libc = None


def _c():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    return _libc


def _prctl(option, arg=0, *, ignore=()):
    if _c().prctl(option, arg, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        if error not in ignore:
            raise OSError(error, f'prctl({option}): {os.strerror(error)}')


class _CapHeader(ctypes.Structure):
    _fields_ = [('version', ctypes.c_uint32), ('pid', ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [('effective', ctypes.c_uint32), ('permitted', ctypes.c_uint32), ('inheritable', ctypes.c_uint32)]


def non_dumpable():
    """No core dumps, no same-UID ptrace, and /proc/1 readable by root only."""
    _prctl(PR_SET_DUMPABLE, 0)


def _last_capability():
    try:
        return int(Path('/proc/sys/kernel/cap_last_cap').read_text())
    except (OSError, ValueError):
        return 63


def drop_privileges(uid, gid):
    """Leave root for good: bounding set first, since it needs CAP_SETPCAP."""
    for capability in range(_last_capability() + 1):
        _prctl(PR_CAPBSET_DROP, capability, ignore={22})  # EINVAL: unknown to this kernel
    _prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, ignore={22})
    os.setgroups([])
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)
    header, data = _CapHeader(CAPABILITY_VERSION_3, 0), (_CapData * 2)()
    if _c().capset(ctypes.byref(header), data) != 0:
        error = ctypes.get_errno()
        raise OSError(error, f'capset: {os.strerror(error)}')
    _prctl(PR_SET_NO_NEW_PRIVS, 1)
    fields = _status()
    if (os.getresuid() != (uid, uid, uid) or os.getresgid() != (gid, gid, gid) or os.getgroups()
            or any(capabilities().values()) or fields.get('NoNewPrivs') != '1'):
        raise GateError(DROP_FAILED, 'payload identity, groups, capabilities or no_new_privs differ from the request')


def _child(report, uid, gid, command, drop):
    """Runs in the forked child: never returns, never runs Python code after exec."""
    try:
        for number in FORWARDED + (signal.SIGPIPE, signal.SIGXFSZ):
            signal.signal(number, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_SETMASK, [])
        # Close every inherited descriptor but stdio and the report pipe: the gate
        # descriptor is close-on-exec already, and is closed here as well.
        os.closerange(3, report)
        os.closerange(report + 1, os.sysconf('SC_OPEN_MAX'))
        try:
            drop(uid, gid)
        except GateError:
            raise
        except Exception as exc:
            raise GateError(DROP_FAILED, f'{type(exc).__name__}: {exc}') from None
        try:
            os.execvp(command[0], command)
        except OSError as exc:
            raise GateError('payload_exec_failed', f'{command[0]}: {exc.strerror}') from None
    except BaseException as exc:
        code = exc.code if isinstance(exc, GateError) else DROP_FAILED
        detail = exc.detail if isinstance(exc, GateError) else type(exc).__name__
        try:
            wg._write_all(report, json.dumps(dict(code=code, detail=detail)).encode())
        finally:
            os._exit(REFUSED_EXIT)
    os._exit(REFUSED_EXIT)


def _reap_namespace():
    """As PID 1, kill what is left of the namespace and reap it before the lock goes."""
    if os.getpid() != 1:
        return  # never signal outside a PID namespace this process does not own
    try:
        os.kill(-1, signal.SIGKILL)
    except ProcessLookupError:
        pass
    while True:
        try:
            os.waitpid(-1, 0)
        except ChildProcessError:
            return


def supervise(gate, uid, gid, command, *, drop=drop_privileges):
    """Run the payload, forward signals, reap zombies; return the payload's exit code.

    ``drop`` exists for tests that cannot change UID; ``main`` always drops.
    """
    signal.pthread_sigmask(signal.SIG_BLOCK, FORWARDED)
    report, report_end = os.pipe2(os.O_CLOEXEC)
    sys.stdout.flush()
    sys.stderr.flush()
    child = os.fork()
    if child == 0:
        os.close(report)
        _child(report_end, uid, gid, command, drop)
    os.close(report_end)

    def forward(number, _frame):
        try:
            os.kill(child, number)
        except ProcessLookupError:
            pass
    for number in FORWARDED:
        signal.signal(number, forward)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, FORWARDED)
    failure = b''
    while True:
        chunk = os.read(report, 4096)
        if not chunk:
            break
        failure += chunk
    os.close(report)
    status = None
    while status is None:
        try:
            done, raw = os.waitpid(-1, 0)
        except ChildProcessError:
            break
        if done == child:
            status = raw
    for number in FORWARDED:  # the payload's PID is free now: never signal its next owner
        signal.signal(number, signal.SIG_IGN)
    _reap_namespace()
    gate.close()
    if failure:
        try:
            refused = json.loads(failure)
            raise GateError(str(refused['code']), str(refused['detail']))
        except (ValueError, KeyError, TypeError):
            raise GateError(DROP_FAILED, 'payload could not be started') from None
    return 255 if status is None else _exit_code(status)


def _exit_code(status):
    code = os.waitstatus_to_exitcode(status)
    return 128 - code if code < 0 else code


# ---------------------------------------------------------------------------
# Entry point (PID 1) and host-side helpers.

def refuse(error, stream=None):
    stream = sys.stderr if stream is None else stream
    stream.write(REFUSED_PREFIX + json.dumps(dict(code=error.code, detail=error.detail), sort_keys=True) + '\n')
    stream.flush()
    return REFUSED_EXIT


def parse_refusal(text):
    """The holder's refusal in a container's stderr, as a ``GateError``, or None."""
    for line in reversed(str(text).splitlines()):
        if line.startswith(REFUSED_PREFIX):
            try:
                record = json.loads(line[len(REFUSED_PREFIX):])
                return GateError(str(record['code']), str(record['detail']))
            except (ValueError, KeyError, TypeError):
                return GateError('holder_report_invalid', 'unreadable holder refusal')
    return None


def _arguments(argv):
    parser = argparse.ArgumentParser(prog=HOLDER, description='SSDLC PID 1 holder of the writer gate')
    parser.add_argument('--ticket-root', required=True)
    parser.add_argument('--generation', required=True, help='expected generation, as JSON')
    parser.add_argument('--payload-user', help='numeric UID:GID of the payload, never root')
    parser.add_argument('--gate-identity', help='DEVICE:INODE the host bound')
    parser.add_argument('--wait', type=float, default=30.0, help='bounded wait for LOCK_SH, seconds')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command[:1] == ['--']:
        args.command = args.command[1:]
    if not args.command:
        parser.error('a payload command is required after --')
    return args


def main(argv=None):
    args = _arguments(sys.argv[1:] if argv is None else argv)
    try:
        uid, gid = parse_payload_user(args.payload_user)
        if os.getpid() != 1:
            raise GateError(NOT_PID1, 'the holder must be PID 1 of the container, in place of --init')
        if os.geteuid() != 0:
            raise GateError(NOT_ROOT, 'the holder must start as root to keep the gate from the payload')
        check_host_requirements()
        non_dumpable()
        try:
            generation = json.loads(args.generation)
        except ValueError:
            raise GateError('invalid_generation', 'expected generation is not JSON') from None
        gate = hold(args.ticket_root, generation, identity=gate_identity(args.gate_identity), timeout=args.wait)
    except GateError as exc:
        return refuse(exc)
    sys.stderr.write(READY_PREFIX + json.dumps(dict(generation=generation, **gate.identity), sort_keys=True) + '\n')
    try:
        return supervise(gate, uid, gid, args.command)
    except GateError as exc:
        return refuse(exc)


def holder_command(ticket_root, generation, payload_user, payload, *, identity=None, wait=None,
                   holder=f'/opt/ssdlc/{HOLDER}', python='python3'):
    """Entrypoint and command for ``docker run``: ``[--entrypoint, python] + [image] + command``.

    The container must start as root (no ``--user``) and without ``--init``; the ticket
    is mounted read-only at the same path. A root payload is refused here already.
    """
    uid, gid = parse_payload_user(payload_user)
    command = ['-I', holder, '--ticket-root', str(ticket_root), '--generation', wg.generation_key(generation),
               '--payload-user', f'{uid}:{gid}']
    if identity is not None:
        command += ['--gate-identity', f'{identity["device"]}:{identity["inode"]}']
    if wait is not None:
        command += ['--wait', str(wg._bounded(wait))]
    return ['--entrypoint', python], command + ['--', *map(str, payload)]


def running_containers(label, *, run=subprocess.run):
    """Coverage probe for ``transition``: a reason while a container of the run may run.

    Needed after ``LOCK_EX`` because Linux closes PID 1's files before the rest of its
    namespace dies. Only containers with ``label`` are listed; none is ever signalled.
    A Docker failure is a reason too, never a proof.
    """
    if not isinstance(label, str) or '=' not in label or not label.split('=', 1)[0]:
        raise GateError('invalid_label', 'a run label must be KEY=VALUE')

    def probe():
        argv = ['docker', 'ps', '-a', '-q', '--no-trunc', '--filter', f'label={label}']
        for state in ('created', 'restarting', 'running', 'paused'):
            argv += ['--filter', f'status={state}']
        try:
            listed = run(argv, capture_output=True, text=True, timeout=15, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            return f'docker could not list the containers of {label}: {type(exc).__name__}'
        found = listed.stdout.split()
        return f'{len(found)} container(s) labelled {label} may still run' if found else None
    return probe


if __name__ == '__main__':
    sys.exit(main())
