"""Writer gate for automatic fencing (#45, part a). Protocol: docs/writer-fencing.md.

One stable ``writer.gate`` file per canonical ticket, shared by every generation.
Writers hold ``LOCK_SH`` for their whole life and recheck the generation after
acquiring it; a transition takes a non-blocking ``LOCK_EX`` only from source
revalidation to selector publication. Only the kernel flock is evidence: the
registry beside the gate never authorizes anything. A host command marks its
generation and records its tree before it executes, and runs under a supervisor
that is a child subreaper (#132): only the supervisor's record that the kernel left
it no child (``ECHILD``) proves the tree ended. Any tree without that proof, or run
without the mechanism, refuses automatic fencing for the generation
(``HOST_TREE_UNVERIFIABLE``). Process scans are never used as evidence.

Role containers of admitted runs take the gate through their PID 1 holder
(``ssdlc_role_fencing``, part c1); native transitions of those runs take ``LOCK_EX``
through ``ssdlc_fencing_proof`` (part c2).
"""
import contextlib
import ctypes
import errno
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import signal
import stat
import subprocess
import sys
import time
import types

from ssdlc_locks import current_lock, record_lock


GATE_NAME = 'writer.gate'
# Second hard link to the gate inode, published before the gate path: it marks the
# ticket as initialized and keeps the canonical identity outside the gate path.
ANCHOR_NAME = 'writer-gate.anchor'
REGISTRY_NAME = 'writer-gate.d'
INIT_WAIT = 5.0
KIND = 'ssdlc-writer-gate-v1'
RECORD_MAX = 4096
MAX_WAIT = 300.0
HOST_TREE_UNVERIFIABLE = 'HOST_TREE_UNVERIFIABLE'
# Host markers written with per-command tree records (#132). A current-generation
# marker without it was written before host trees could be proven and always refuses.
TREE_PROTOCOL = 'host-tree-v1'
TREE_MECHANISM = 'subreaper'
PR_SET_CHILD_SUBREAPER, PR_GET_CHILD_SUBREAPER = 36, 37
SUPERVISOR_START = 30.0
LOG = logging.getLogger(__name__)

# Host-local filesystems whose flock is enforced by this kernel for every opener.
LOCAL_FILESYSTEMS = {
    0xEF53: 'ext2/ext3/ext4', 0x58465342: 'xfs', 0x9123683E: 'btrfs', 0x01021994: 'tmpfs',
    0xF2F52010: 'f2fs', 0x2FC12FC1: 'zfs', 0xCA451A4E: 'bcachefs',
}
# Known refusals get a readable name; any other type is refused as unknown.
REFUSED_FILESYSTEMS = {
    0x6969: 'nfs', 0xFF534D42: 'cifs', 0xFE534D42: 'smb2', 0x517B: 'smb', 0x65735546: 'fuse',
    0x01021997: 'v9fs', 0x00C36400: 'ceph', 0x5346414F: 'afs', 0x01161970: 'gfs2',
    0x7461636F: 'ocfs2', 0x0BD00BD0: 'lustre', 0x794C7630: 'overlayfs',
}


class GateError(Exception):
    """A diagnosable refusal. It never carries or implies a positive proof."""

    def __init__(self, code, detail=''):
        self.code, self.detail = code, detail
        super().__init__(f'writer gate refused ({code}): {detail}' if detail else f'writer gate refused ({code})')


def _require_linux():
    if not sys.platform.startswith('linux') or not os.path.isdir('/proc/self/fdinfo'):
        raise GateError('unsupported_platform', 'automatic fencing needs Linux flock and /proc')


class _StatFs(ctypes.Structure):
    # Linux struct statfs starts with f_type (__fsword_t); the rest is padding here.
    _fields_ = [('f_type', ctypes.c_long), ('_rest', ctypes.c_byte * 256)]


_libc = None


def filesystem_type(fd):
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
    buf = _StatFs()
    if _libc.fstatfs(fd, ctypes.byref(buf)) != 0:
        raise GateError('unsupported_filesystem', 'fstatfs failed: ' + os.strerror(ctypes.get_errno()))
    return buf.f_type & 0xFFFFFFFF


def check_filesystem(fd):
    magic = filesystem_type(fd)
    name = LOCAL_FILESYSTEMS.get(magic)
    if name is None:
        kind = REFUSED_FILESYSTEMS.get(magic, f'unknown filesystem type {magic:#x}')
        raise GateError('unsupported_filesystem', f'{kind} is not a supported local filesystem with kernel flock')
    return name


def gate_path(ticket_root):
    return Path(ticket_root) / GATE_NAME


def generation_key(generation):
    """Canonical encoding of a generation, so markers compare across processes."""
    try:
        return json.dumps(generation, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        raise GateError('invalid_generation', 'a generation must be JSON-serializable') from None


# ---------------------------------------------------------------------------
# Process identity: PID, boot id and start time, never the PID alone.

def self_identity():
    return process_identity(os.getpid())


def process_identity(pid):
    """Live identity triple and session id, or None when the process is gone or a zombie."""
    from ssdlc_supervisor import Proc
    try:
        ident, state, _ = Proc().stat(pid)
    except (OSError, ValueError, IndexError):
        return None
    if state == 'Z':
        return None
    return dict(pid=ident.pid, boot_id=ident.boot_id, start_ticks=ident.start_ticks), ident.sid


def is_identity(identity):
    return (isinstance(identity, dict) and type(identity.get('pid')) is int
            and isinstance(identity.get('boot_id'), str) and type(identity.get('start_ticks')) is int)


def is_live(identity):
    """Diagnostics only: whether this exact process life still runs."""
    if not is_identity(identity):
        return False
    observed = process_identity(identity['pid'])
    return observed is not None and observed[0] == {k: identity[k] for k in ('pid', 'boot_id', 'start_ticks')}


# ---------------------------------------------------------------------------
# The stable gate file.

class _Gate:
    def __init__(self, ticket_root, dirfd, fd, identity, filesystem, expected):
        self.ticket_root, self.dirfd, self.fd = ticket_root, dirfd, fd
        self.identity, self.filesystem, self.expected = identity, filesystem, expected
        self.path = Path(ticket_root).absolute() / GATE_NAME

    def verify(self):
        _verify(self.dirfd, self.fd, self.identity)

    def close(self):
        # Closing our private descriptor is the release: it is O_CLOEXEC and never
        # shared, so no explicit LOCK_UN can drop a lock somebody else relies on.
        for fd in (self.fd, self.dirfd):
            with contextlib.suppress(OSError):
                os.close(fd)


def _read_record(fd):
    data = os.pread(fd, RECORD_MAX + 1, 0)
    try:
        record = json.loads(data) if len(data) <= RECORD_MAX else None
    except ValueError:
        record = None
    if (not isinstance(record, dict) or set(record) != {'kind', 'schema_version', 'inode', 'created_at', 'nonce'}
            or record['kind'] != KIND or record['schema_version'] != 1 or type(record['inode']) is not int
            or not isinstance(record['nonce'], str)):
        raise GateError('gate_corrupt', 'gate file does not hold a writer gate record')
    return record


def _lstat(dirfd, name):
    try:
        return os.stat(name, dir_fd=dirfd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _verify(dirfd, fd, expected=None):
    opened = os.fstat(fd)
    linked = _lstat(dirfd, GATE_NAME)
    if linked is None:
        raise GateError('gate_missing', 'gate path disappeared; the gate is never recreated')
    if stat.S_ISLNK(linked.st_mode):
        raise GateError('gate_symlink', 'gate path is a symlink')
    if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(linked.st_mode):
        raise GateError('gate_not_regular', 'gate path is not a regular file')
    if (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino):
        raise GateError('gate_replaced', 'gate path no longer names the opened gate')
    record = _read_record(fd)
    if record['inode'] != opened.st_ino:
        raise GateError('gate_replaced', f'gate record belongs to inode {record["inode"]}, file is inode {opened.st_ino}')
    anchor = _lstat(dirfd, ANCHOR_NAME)
    if anchor is None:
        raise GateError('gate_anchor_missing',
                        f'{ANCHOR_NAME} is missing: the gate cannot be told apart from a recreated one')
    if not stat.S_ISREG(anchor.st_mode) or (anchor.st_dev, anchor.st_ino) != (opened.st_dev, opened.st_ino):
        raise GateError('gate_replaced', f'gate is not the inode anchored by {ANCHOR_NAME}')
    identity = dict(device=opened.st_dev, inode=opened.st_ino)
    if expected is not None and dict(device=expected['device'], inode=expected['inode']) != identity:
        raise GateError('gate_replaced', 'gate identity differs from the bound identity')
    return identity


def _write_all(fd, data):
    while data:
        data = data[os.write(fd, data):]


def _create(dirfd):
    """Publish a complete gate with link(2): anchor first, then the gate path.

    Called only under the initialization lock, with neither name present. link(2)
    never replaces an existing path, so a conflict is a refusal, not a replacement.
    The inode is private (0600): a container payload running with another UID cannot
    open it (ssdlc_container_holder).
    """
    tmp = f'.{GATE_NAME}.{secrets.token_hex(8)}.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dirfd)
    try:
        try:
            record = dict(kind=KIND, schema_version=1, inode=os.fstat(fd).st_ino,
                          created_at=time.time(), nonce=secrets.token_hex(16))
            _write_all(fd, json.dumps(record, sort_keys=True).encode() + b'\n')
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            for name in (ANCHOR_NAME, GATE_NAME):
                os.link(tmp, name, src_dir_fd=dirfd, dst_dir_fd=dirfd, follow_symlinks=False)
        except FileExistsError:
            raise GateError('gate_init_conflict', 'gate created outside the initialization lock') from None
        os.fsync(dirfd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=dirfd)


@contextlib.contextmanager
def _init_lock(dirfd):
    """Serialize first initialization on a private descriptor of the ticket directory."""
    fd = os.open('.', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=dirfd)
    try:
        deadline = time.monotonic() + INIT_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise GateError('gate_init_contended',
                                    f'gate initialization still locked after {INIT_WAIT:.0f}s') from None
                time.sleep(0.01)
        yield
    finally:
        os.close(fd)


def _initialize(dirfd):
    """Create the gate only on a ticket that never had one; never recreate it."""
    with _init_lock(dirfd):
        if _lstat(dirfd, GATE_NAME) is not None:
            return  # the anchor is checked by _verify, after the gate's own diagnostics
        if _lstat(dirfd, ANCHOR_NAME) is not None:
            raise GateError('gate_missing', 'gate path disappeared after initialization; the gate is never recreated')
        if _lstat(dirfd, REGISTRY_NAME) is not None:
            raise GateError('gate_missing', f'gate and anchor are missing but {REGISTRY_NAME} exists: '
                                            'the gate was initialized before and is never recreated')
        _create(dirfd)


def open_gate(ticket_root, expected_identity=None, *, create=True):
    """Open and verify the canonical ticket's gate, creating it only on first initialization.

    With ``create=False`` nothing is initialized or written: on a read-only mount of
    the ticket, as in a container, a missing gate is refused instead.
    """
    _require_linux()
    root = Path(ticket_root)
    if not root.is_absolute():
        raise GateError('ticket_root_invalid', 'canonical ticket root must be absolute')
    try:
        dirfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise GateError('ticket_root_invalid', f'{root}: {exc.strerror}') from None
    fd = None
    try:
        filesystem = check_filesystem(dirfd)
        if create and (_lstat(dirfd, GATE_NAME) is None or _lstat(dirfd, ANCHOR_NAME) is None):
            _initialize(dirfd)
        linked = _lstat(dirfd, GATE_NAME)
        if linked is None:
            raise GateError('gate_missing', 'gate path disappeared after initialization')
        if stat.S_ISLNK(linked.st_mode):
            raise GateError('gate_symlink', 'gate path is a symlink')
        if not stat.S_ISREG(linked.st_mode):
            raise GateError('gate_not_regular', 'gate path is not a regular file')
        try:
            fd = os.open(GATE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOCTTY | os.O_NONBLOCK,
                         dir_fd=dirfd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise GateError('gate_symlink', 'gate path is a symlink') from None
            if exc.errno == errno.ENOENT:
                raise GateError('gate_missing', 'gate path disappeared while opening') from None
            raise GateError('gate_unreadable', exc.strerror) from None
        check_filesystem(fd)
        identity = _verify(dirfd, fd, expected_identity)
        return _Gate(root, dirfd, fd, identity, filesystem, expected_identity)
    except BaseException:
        for value in (fd, dirfd):
            if value is not None:
                with contextlib.suppress(OSError):
                    os.close(value)
        raise


# ---------------------------------------------------------------------------
# Registry beside the gate. Writer records are diagnostics: they never grant or
# deny anything. A host marker is never evidence of quiescence; it only refuses
# automatic fencing for its generation, and nothing here removes it.

def _registry(gate, create=True):
    """The registry descriptor; with ``create=False`` nothing is created and None means absent."""
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(REGISTRY_NAME, 0o755, dir_fd=gate.dirfd)
    try:
        return os.open(REGISTRY_NAME, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=gate.dirfd)
    except OSError as exc:
        if not create and exc.errno == errno.ENOENT:
            return None
        raise GateError('registry_invalid', f'{REGISTRY_NAME}: {exc.strerror}') from None


def _publish(gate, name, record):
    """Publish a complete record with link(2); return False if ``name`` already exists."""
    rfd = _registry(gate)
    try:
        tmp = f'.{name}.{secrets.token_hex(4)}.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=rfd)
        try:
            try:
                _write_all(fd, json.dumps(record, sort_keys=True).encode())
            finally:
                os.close(fd)
            try:
                os.link(tmp, name, src_dir_fd=rfd, dst_dir_fd=rfd, follow_symlinks=False)
            except FileExistsError:
                return False
            return True
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp, dir_fd=rfd)
    finally:
        os.close(rfd)


def _register(gate, identity):
    name = f'w-{identity["pid"]}-{identity["start_ticks"]}-{secrets.token_hex(4)}.json'
    _publish(gate, name, dict(role='writer', identity=identity, recorded_at=time.time()))
    return name


def _unregister(gate, name):
    with contextlib.suppress(OSError, GateError):
        rfd = _registry(gate)
        try:
            os.unlink(name, dir_fd=rfd)
        finally:
            os.close(rfd)


def host_marker_name(key):
    return f'h-{hashlib.sha256(key.encode()).hexdigest()[:32]}.json'


def _historical_key(record, name):
    """The generation of a well-formed host marker, or None when it cannot be trusted.

    A marker records a past generation only when its generation is a canonical
    encoding produced by ``generation_key`` and the marker name is the one derived
    from it. Anything else is unreadable and refuses like a current-generation one.
    """
    key = record.get('generation')
    if not isinstance(key, str):
        return None
    try:
        if generation_key(json.loads(key)) != key:
            return None
    except (ValueError, GateError):
        return None
    return key if name == host_marker_name(key) else None


def _read_entry(rfd, name):
    """One registry record, or None when it is not a readable regular JSON file."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=rfd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            return json.loads(os.read(fd, RECORD_MAX + 1))
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return None


def _mark_host(gate, key, leader, controller):
    """Durably mark that a host command runs in generation ``key``. Never removed here."""
    name = host_marker_name(key)
    created = _publish(gate, name, dict(role='host', generation=key, identity=leader, trees=TREE_PROTOCOL,
                                        controller=controller, recorded_at=time.time()))
    if not created:
        # An existing marker is accepted only if it is a valid marker of this very
        # generation: a malformed or inconsistent one is never taken as ours.
        rfd = _registry(gate)
        try:
            record = _read_entry(rfd, name)
        finally:
            os.close(rfd)
        if (not isinstance(record, dict) or record.get('role') != 'host'
                or _historical_key(record, name) != key):
            raise GateError('registry_invalid', f'host marker {name} exists but is not a valid marker of this generation')


_TREE_ID = re.compile(r'[0-9a-f]{32}')


def tree_record_name(tree):
    return f't-{tree}.json'


def tree_conclusion_name(tree):
    return f'c-{tree}.json'


def _tree_key(record):
    """The generation of a well-formed tree record, or None when it cannot be trusted."""
    tree, key = record.get('tree'), record.get('generation')
    if not isinstance(tree, str) or not _TREE_ID.fullmatch(tree) or record.get('name') != tree_record_name(tree):
        return None
    if not isinstance(key, str):
        return None
    try:
        return key if generation_key(json.loads(key)) == key else None
    except (ValueError, GateError):
        return None


def _record_tree(gate, tree, key, supervisor, controller, mechanism, reason):
    """Durably record one host command's tree before it executes. Never removed here."""
    record = dict(role='host-tree', tree=tree, generation=key, mechanism=mechanism, reason=reason,
                  supervisor=supervisor, controller=controller, recorded_at=time.time())
    if not _publish(gate, tree_record_name(tree), record):
        raise GateError('registry_invalid', f'host tree record {tree_record_name(tree)} already exists')


def _records(gate, create=True):
    """All registry records, plus the names of entries that cannot be read as records.

    With ``create=False`` (read-only observers) a missing registry is no record at all.
    """
    rfd = _registry(gate, create)
    if rfd is None:
        return [], []
    try:
        found, malformed = [], []
        for name in sorted(os.listdir(rfd)):
            if name.startswith('.') and name.endswith('.tmp'):
                continue  # an unpublished record: link(2) has not made it visible yet
            record = _read_entry(rfd, name)
            role = record.get('role') if isinstance(record, dict) else None
            if role == 'writer' and name.startswith('w-'):
                found.append(dict(record, name=name, live=is_live(record.get('identity'))))
            elif role == 'host' and name.startswith('h-'):
                found.append(dict(record, name=name, live=is_live(record.get('identity'))))
            elif role == 'host-tree' and name.startswith('t-'):
                found.append(dict(record, name=name, live=is_live(record.get('supervisor'))))
            elif role == 'host-tree-concluded' and name.startswith('c-'):
                found.append(dict(record, name=name, live=False))
            else:
                LOG.warning('writer-gate malformed registry entry ticket=%s name=%s', gate.ticket_root, name)
                malformed.append(name)
        return found, malformed
    finally:
        os.close(rfd)


def _kernel_holders(gate):
    """Kernel-held flocks on the gate inode (blocked waiters are not holders)."""
    device, inode = gate.identity['device'], gate.identity['inode']
    holders = []
    try:
        lines = Path('/proc/locks').read_text().splitlines()
    except OSError:
        return holders
    for line in lines:
        words = line.split()
        if len(words) < 8 or words[1] != 'FLOCK':
            continue
        try:
            major, minor, number = words[5].split(':')
            if (int(number), int(major, 16), int(minor, 16)) == (inode, os.major(device), os.minor(device)):
                holders.append(dict(mode=words[3], pid=int(words[4])))
        except ValueError:
            continue
    return holders


def holders(gate):
    """Diagnostics only: who holds the gate, stale writer records and host markers."""
    try:
        records, malformed = _records(gate)
    except GateError:
        records, malformed = [], []
    kernel = []
    for held in _kernel_holders(gate):
        observed = process_identity(held['pid'])
        identity = observed[0] if observed else None
        registered = any(r['role'] == 'writer' and r['live'] and r['identity'] == identity for r in records)
        kernel.append(dict(held, identity=identity, registered=registered))
    concluded = _conclusions(records)
    return dict(kernel=kernel, malformed=malformed,
                host_trees=[dict(tree=r.get('tree'), generation=r.get('generation'), mechanism=r.get('mechanism'),
                                 supervisor=r.get('supervisor'), supervisor_live=r['live'],
                                 concluded=_concluded(r, concluded))
                            for r in records if r['role'] == 'host-tree'],
                stale_records=[r['name'] for r in records if r['role'] == 'writer' and not r['live']],
                host_markers=[dict(generation=r.get('generation'), leader=r.get('identity'), leader_live=r['live'])
                              for r in records if r['role'] == 'host'])


def _describe(found):
    parts = [f'{h["mode"]} pid {h["pid"]} ' + ('registered writer' if h['registered'] else 'unregistered holder')
             for h in found['kernel']] or ['holders not attributable']
    if found['stale_records']:
        parts.append(f'{len(found["stale_records"])} stale registry records ignored')
    return '; '.join(parts)


# ---------------------------------------------------------------------------
# Writers: LOCK_SH for their whole life, generation rechecked after acquiring.

def _bounded(timeout):
    if not isinstance(timeout, (int, float)) or not 0 <= timeout <= MAX_WAIT:
        raise GateError('invalid_wait', f'writer waits are bounded to {MAX_WAIT:.0f}s')
    return float(timeout)


def _acquire_shared(gate, timeout, poll):
    deadline = time.monotonic() + _bounded(timeout)
    contended = False
    while True:
        try:
            fcntl.flock(gate.fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            if contended:
                LOG.info('writer-gate shared acquired after contention ticket=%s', gate.ticket_root)
            return
        except BlockingIOError:
            pass
        if not contended:
            contended = True
            LOG.warning('writer-gate contention ticket=%s request=shared holders=%s',
                        gate.ticket_root, _describe(holders(gate)))
        if time.monotonic() >= deadline:
            raise GateError('writer_wait_timeout', f'gate still exclusively held after {timeout}s')
        time.sleep(poll)


class WriterLease:
    """A live shared lease for one generation. Not serializable, not a capability."""

    def __init__(self, ticket_root, read_generation, *, expected_identity=None, poll=0.05):
        self.ticket_root, self.read_generation = Path(ticket_root), read_generation
        self.expected_identity, self.poll = expected_identity, poll
        self.generation = self.identity = self.lease = None
        self._gate = self._stack = None

    def _hold(self, generation, timeout):
        key = generation_key(generation)
        gate = open_gate(self.ticket_root, self.expected_identity)
        stack = contextlib.ExitStack()
        stack.callback(gate.close)
        try:
            _acquire_shared(gate, timeout, self.poll)
            gate.verify()  # the path still names the inode we hold
            # Recheck after acquiring: a transition may have published while we waited.
            if generation_key(self.read_generation()) != key:
                raise GateError('stale_generation', 'the ticket moved to another generation; this writer must not restart')
            self.lease = stack.enter_context(record_lock(gate.path, gate.fd, shared=True))
            name = _register(gate, self_identity()[0])
            stack.callback(_unregister, gate, name)
        except BaseException:
            stack.close()
            raise
        self._gate, self._stack, self.generation = gate, stack, generation
        self.identity = self.expected_identity = gate.identity

    def check(self):
        if self._gate is None:
            raise GateError('lease_lost', 'writer lease is not held')
        self._gate.verify()  # path identity first, so a replaced gate is named as such
        try:
            self.lease.validate(self._gate.path)
        except Exception as exc:
            raise GateError('lease_lost', str(exc)) from None
        if generation_key(self.read_generation()) != generation_key(self.generation):
            raise GateError('stale_generation', 'generation changed under a live writer lease')
        return dict(kind='writer-gate-shared-v1', **self.identity, writer=self_identity()[0])

    def release(self):
        stack, self._stack, self._gate, self.lease = self._stack, None, None, None
        if stack is not None:
            stack.close()


@contextlib.contextmanager
def writer(ticket_root, generation, read_generation, *, timeout=30.0, poll=0.05, expected_identity=None):
    lease = WriterLease(ticket_root, read_generation, expected_identity=expected_identity, poll=poll)
    lease._hold(generation, timeout)
    try:
        yield lease
    finally:
        lease.release()


# ---------------------------------------------------------------------------
# Transitions: non-blocking LOCK_EX as proof that no writer is registered.

class TransitionProof:
    def __init__(self, gate, lease, generation):
        self._gate, self.lease, self.identity = gate, lease, gate.identity
        self.generation = generation  # read under LOCK_EX: the source being revalidated
        self.closed = False

    def check(self):
        if self.closed:
            raise GateError('lease_lost', 'transition proof is closed')
        self._gate.verify()
        try:
            self.lease.validate(self._gate.path)
        except Exception as exc:
            raise GateError('lease_lost', str(exc)) from None
        return dict(kind='writer-gate-exclusive-v1', **self.identity, holder=self_identity()[0])


def _conclusions(records):
    """Supervisor conclusions by tree id; a record whose name does not match its tree is none."""
    found = {}
    for record in records:
        tree = record.get('tree') if record['role'] == 'host-tree-concluded' else None
        if isinstance(tree, str) and _TREE_ID.fullmatch(tree) and record['name'] == tree_conclusion_name(tree):
            found[tree] = record
    return found


def _concluded(tree, conclusions):
    """Whether the tree's own subreaper recorded that the kernel left it no child."""
    done = conclusions.get(tree.get('tree'))
    return (tree.get('mechanism') == TREE_MECHANISM and done is not None
            and done.get('generation') == tree.get('generation') and is_identity(tree.get('supervisor'))
            and done.get('supervisor') == tree.get('supervisor'))


def _unproven_host_trees(records, key):
    """Why the host trees of generation ``key`` are not proven finished; empty when they are.

    A host marker or tree record whose generation cannot be read counts as current. A
    current-generation marker written before tree records existed, a tree run without
    the subreaper, and a tree without its supervisor's conclusion all refuse. Records
    of a well-formed earlier generation are history.
    """
    reasons = []
    for record in records:
        if record['role'] != 'host':
            continue
        marked = _historical_key(record, record['name'])
        if marked is None:
            reasons.append(f'host marker {record["name"]} has no readable generation')
        elif marked == key and record.get('trees') != TREE_PROTOCOL:
            reasons.append(f'host marker {record["name"]} has no tree records')
    conclusions = _conclusions(records)
    for record in records:
        if record['role'] != 'host-tree':
            continue
        tree = _tree_key(record)
        if tree is None:
            reasons.append(f'tree record {record["name"]} has no readable generation')
        elif tree != key:
            continue
        elif record.get('mechanism') != TREE_MECHANISM:
            reasons.append(f'tree {record["tree"]} ran without a kernel mechanism: '
                           f'{record.get("reason") or "no reason recorded"}')
        elif not _concluded(record, conclusions):
            state = 'still running' if record['live'] else 'ended without recording the proof'
            reasons.append(f'tree {record["tree"]} has no subreaper conclusion (supervisor {state})')
    return reasons


def _refuse_uncovered(gate, key, coverage):
    records, malformed = _records(gate)
    if malformed:
        raise GateError('registry_corrupt', f'unreadable registry entries {malformed}; an operator must inspect them')
    unproven = _unproven_host_trees(records, key)
    if unproven:
        # Only the subreaper's recorded ECHILD proves a host tree ended: no process scan stands in.
        raise GateError(HOST_TREE_UNVERIFIABLE,
                        f'{len(unproven)} host process tree(s) of the current generation cannot be proven '
                        f'finished ({"; ".join(unproven)}); use the manual path')
    reasons = []
    for probe in coverage:
        try:
            reason = probe()
        except Exception as exc:
            reason = f'coverage probe failed: {type(exc).__name__}'
        if reason:
            reasons.append(str(reason))
    if reasons:
        raise GateError('uncovered_execution', '; '.join(reasons))


@contextlib.contextmanager
def transition(ticket_root, read_generation, *, coverage=(), expected_identity=None):
    """Hold LOCK_EX only around source revalidation and selector publication."""
    gate = open_gate(ticket_root, expected_identity)
    try:
        try:
            fcntl.flock(gate.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            detail = _describe(holders(gate))
            LOG.warning('writer-gate contention ticket=%s request=exclusive holders=%s', gate.ticket_root, detail)
            raise GateError('writers_active', detail) from None
        gate.verify()
        with record_lock(gate.path, gate.fd) as lease:
            generation = read_generation()
            _refuse_uncovered(gate, generation_key(generation), coverage)
            proof = TransitionProof(gate, lease, generation)
            proof.check()
            started = time.monotonic()
            try:
                yield proof
            finally:
                proof.closed = True
                LOG.info('writer-gate exclusive released ticket=%s held=%.3fs', gate.ticket_root,
                         time.monotonic() - started)
    finally:
        gate.close()


def handoff(lease, publish, *, held=(), coverage=()):
    """Delegated feedback: trade our own LOCK_SH for a short LOCK_EX, never convert.

    The caller already holds its serialization locks (``held``); every gate step is
    non-blocking, so no lock order can deadlock. ``publish(proof)`` revalidates the
    source, publishes the selector and returns the new generation. On refusal the
    lease resumes on the unchanged generation, or fails if that moved.
    """
    def receipts():
        for item in held:
            if item.shared or current_lock(item.path) is not item:
                raise GateError('lease_lost', 'serialization lock receipt changed during handoff')

    receipts()
    lease.check()
    old = lease.generation
    lease.release()
    try:
        with transition(lease.ticket_root, lease.read_generation, coverage=coverage,
                        expected_identity=lease.expected_identity) as proof:
            if generation_key(proof.generation) != generation_key(old):
                raise GateError('stale_generation', 'the generation moved before the handoff took the gate')
            new = publish(proof)
    except BaseException:
        lease._hold(old, 0)
        receipts()
        raise
    lease._hold(new, 0)
    receipts()
    return new


# ---------------------------------------------------------------------------
# Trusted host commands (#132): run inside a live writer lease, without the gate
# descriptor, under a supervisor in its own session that is a child subreaper.
# The generation is marked and the tree recorded before the command executes;
# neither is ever cleared. A tree is proven finished only by its supervisor's
# conclusion, written after the kernel answered ECHILD to waitpid(-1): every
# orphaned descendant is reparented to the subreaper before its parent's exit is
# reported, so no descendant can be missed the way a /proc scan misses one. A
# supervisor that dies first never writes it, and the generation stays refused.

SUPERVISOR = os.path.abspath(__file__)
_libc_prctl = None


def _prctl(option, arg):
    global _libc_prctl
    if _libc_prctl is None:
        _libc_prctl = ctypes.CDLL(None, use_errno=True)
    if _libc_prctl.prctl(ctypes.c_int(option), ctypes.c_ulong(arg), ctypes.c_ulong(0), ctypes.c_ulong(0),
                         ctypes.c_ulong(0)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def host_tree_support():
    """None when host trees can be proven here, else why they cannot (the reason refuses)."""
    if not sys.platform.startswith('linux'):
        return 'child subreapers need Linux'
    value = ctypes.c_int(-1)
    try:
        _prctl(PR_GET_CHILD_SUBREAPER, ctypes.addressof(value))
    except (OSError, AttributeError) as exc:
        return f'PR_GET_CHILD_SUBREAPER unavailable: {getattr(exc, "strerror", None) or exc}'
    return None


def _resolve(program, env):
    """Resolve the executable like Popen would, so a missing one still raises FileNotFoundError."""
    if os.sep in program:
        return program
    found = shutil.which(program, path=(os.environ if env is None else env).get('PATH', os.defpath))
    if found is None:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), program)
    return found


def _read_report(fd):
    """The supervisor's one-line start report, or '' if it ended or stalled before writing it."""
    data, deadline = b'', time.monotonic() + SUPERVISOR_START
    while not data.endswith(b'\n'):
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            return ''
        chunk = os.read(fd, 4096)
        if not chunk:
            return ''
        data += chunk
    return data.decode(errors='replace').strip()


def run_host(lease, argv, *, cwd=None, env=None, input=None, timeout=None, capture_output=False, text=False):
    """Run a trusted host command; return once its whole process tree has ended.

    On ``timeout`` the supervisor kills the whole tree, records its conclusion and
    ``TimeoutExpired`` is raised. Without the subreaper the command still runs, but
    its tree record names the reason and refuses automatic fencing.
    """
    lease.check()
    key = generation_key(lease.generation)
    argv = [os.fspath(arg) for arg in argv]
    program = _resolve(argv[0], env)
    unavailable = host_tree_support()
    tree = secrets.token_hex(16)
    pipe = subprocess.PIPE if capture_output else None
    report_r, report_w = os.pipe()
    try:
        child = subprocess.Popen(
            [sys.executable, '-E', '-s', SUPERVISOR, '--host-tree', str(lease._gate.ticket_root), tree, key,
             unavailable or TREE_MECHANISM, str(report_w), program, *argv[1:]],
            cwd=cwd, env=env, text=text, stdout=pipe, stderr=pipe, stdin=subprocess.PIPE,
            close_fds=True, pass_fds=(report_w,), start_new_session=True)
    except BaseException:
        os.close(report_r)
        raise
    finally:
        os.close(report_w)
    mechanism = None
    try:
        report = _read_report(report_r)
        if not report:
            raise GateError(HOST_TREE_UNVERIFIABLE, 'host tree supervisor did not start')
        # Not reaped and still waiting on its barrier, so /proc/<pid> describes our child.
        observed = process_identity(child.pid)
        if observed is None or observed[1] != child.pid:
            raise GateError(HOST_TREE_UNVERIFIABLE, 'host command session could not be identified')
        if report == TREE_MECHANISM:
            mechanism, reason = TREE_MECHANISM, None
        else:
            reason = report.removeprefix('unavailable').strip() or 'the supervisor reported no mechanism'
            LOG.warning('writer-gate host tree unprovable ticket=%s tree=%s reason=%s',
                        lease.ticket_root, tree, reason)
        controller = self_identity()[0]
        # Both published before the command runs: they outlive a SIGKILL of this controller.
        _mark_host(lease._gate, key, observed[0], controller)
        _record_tree(lease._gate, tree, key, observed[0], controller, mechanism, reason)
        with contextlib.suppress(BrokenPipeError):
            _write_all(child.stdin.fileno(), b'\n')
    except BaseException:
        # EOF on the barrier: the command is never executed.
        try:
            child.communicate(timeout=SUPERVISOR_START)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
        raise
    finally:
        os.close(report_r)
    LOG.info('writer-gate host command started ticket=%s supervisor=%s tree=%s mechanism=%s generation=%s',
             lease.ticket_root, child.pid, tree, mechanism, key)
    try:
        out, err = child.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            if mechanism == TREE_MECHANISM:
                os.kill(child.pid, signal.SIGTERM)  # the supervisor kills its whole tree, then concludes
            else:
                os.killpg(child.pid, signal.SIGKILL)
        child.communicate()
        raise
    lease.check()
    return subprocess.CompletedProcess(argv, child.returncode, out, err)


# ---------------------------------------------------------------------------
# The host tree supervisor: ``python3 ssdlc_writer_gate.py --host-tree ...``,
# started by run_host() in its own session. It becomes a child subreaper, reports
# on a private pipe, waits for the controller's line on stdin (the barrier), runs
# the command in its own process group and reaps every descendant. Only then does
# it publish ``c-<tree>.json``. It never holds the gate.

_STOPPING = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


def _initial_environ():
    """The environment exactly as the controller passed it, before Python touched it."""
    try:
        raw = Path('/proc/self/environ').read_bytes()
    except OSError:
        return os.environb
    return dict(item.split(b'=', 1) for item in raw.split(b'\0') if b'=' in item)


def _barrier():
    """Read exactly one line from stdin; False on EOF (the controller died or refused)."""
    while True:
        byte = os.read(0, 1)
        if not byte:
            return False
        if byte == b'\n':
            return True


def _children():
    try:
        return [int(pid) for pid in Path(f'/proc/self/task/{os.getpid()}/children').read_text().split()]
    except (OSError, ValueError):
        return []


def _conclude(dirfd, tree, key, supervisor, status):
    record = dict(role='host-tree-concluded', tree=tree, generation=key, supervisor=supervisor,
                  status=status, concluded_at=time.time())
    with contextlib.suppress(OSError, GateError):
        # Without this record the tree stays unproven: failing here only refuses.
        _publish(types.SimpleNamespace(dirfd=dirfd), tree_conclusion_name(tree), record)


def _supervise(args):
    ticket_root, tree, key, mechanism, report_fd, *command = args
    report_fd = int(report_fd)
    state = dict(stop=False, group=None)

    def kill_tree():
        # Only to stop the tree: listing children here is never evidence.
        if state['group'] is not None:  # still unreaped, so its group id is not reused
            with contextlib.suppress(OSError):
                os.killpg(state['group'], signal.SIGKILL)
        for pid in _children():  # unreaped children: their PIDs cannot be reused
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)

    def stop(signum, frame):
        state['stop'] = True
        kill_tree()
    for sig in _STOPPING:
        signal.signal(sig, stop)

    reason, dirfd = (None if mechanism == TREE_MECHANISM else mechanism), None
    if reason is None:
        try:
            dirfd = os.open(ticket_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            reason = f'ticket directory unreachable for the conclusion: {exc.strerror}'
    if reason is None:
        try:
            _prctl(PR_SET_CHILD_SUBREAPER, 1)
            supervisor = self_identity()[0]
        except Exception as exc:
            reason = f'PR_SET_CHILD_SUBREAPER failed: {getattr(exc, "strerror", None) or type(exc).__name__}'
    reason = None if reason is None else ' '.join(reason.split())
    _write_all(report_fd, (TREE_MECHANISM if reason is None else f'unavailable {reason}').encode() + b'\n')
    os.close(report_fd)
    env = _initial_environ()
    default = (signal.SIGPIPE, signal.SIGXFSZ, *_STOPPING)
    if not _barrier():
        if reason is None:
            _conclude(dirfd, tree, key, supervisor, 'not_started')
        os._exit(125)
    if reason is not None:  # no mechanism: behave like the plain barrier, the record already refuses
        for sig in default:
            signal.signal(sig, signal.SIG_DFL)
        try:
            os.execve(command[0], command, env)
        finally:
            os._exit(127)
    status = None
    try:
        state['group'] = child = os.posix_spawn(command[0], command, env, setpgroup=0, setsigdef=default)
    except OSError:
        child = None
    if state['stop']:
        kill_tree()
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):  # never keep the caller's pipes open past the command
        os.dup2(null, fd)
    os.close(null)
    while True:
        try:
            pid, waited = os.waitpid(-1, os.WNOHANG if state['stop'] else 0)
        except ChildProcessError:
            break  # ECHILD: the kernel reparents every orphan here, so the whole tree has ended
        if pid == 0:
            kill_tree()
            time.sleep(0.05)
        elif pid == child:
            status, state['group'] = waited, None
    if status is None:
        _conclude(dirfd, tree, key, supervisor, 'not_executed')
        os._exit(127)
    if os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
        _conclude(dirfd, tree, key, supervisor, f'signal {sig}')
        import resource
        with contextlib.suppress(OSError, ValueError):  # end like the command, without a core dump
            resource.setrlimit(resource.RLIMIT_CORE, (0, resource.getrlimit(resource.RLIMIT_CORE)[1]))
        with contextlib.suppress(OSError, ValueError):  # SIGKILL and SIGSTOP keep their action
            signal.signal(sig, signal.SIG_DFL)
        os.kill(os.getpid(), sig)
        os._exit(128 + sig)
    code = os.waitstatus_to_exitcode(status)
    _conclude(dirfd, tree, key, supervisor, f'exit {code}')
    os._exit(code)


if __name__ == '__main__' and sys.argv[1:2] == ['--host-tree']:
    _supervise(sys.argv[2:])
