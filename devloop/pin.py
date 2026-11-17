"""Pinned installations (#174): ``devloop pin <ref>`` and the pin after a merge.

devloop runs from a read-only copy of one commit of ``origin``, never from the checkout it
changes. ``pin`` resolves ``<ref>`` to a commit reachable from the refs of ``origin`` (or to a
copy already verified, reused without a fetch: the rollback), extracts ``devloop/`` and the files
of ``review_policy.sensitive_path_lists`` and the documents of ``[docs] map`` (#203) of that
commit into ``<state_dir>/pinned-<sha12>``,
makes it read-only, checks that it imports and that its configuration, with the local overlay,
loads, and only then points ``<state_dir>/current`` at it with an atomic rename. A failure
leaves ``current`` as it was. Earlier copies stay for the rollback.

With ``[pin] auto = true`` a merge by devloop that touches ``devloop/`` or a list file records a
request (``pin-pending.json``), and the next tick pins ``origin/<base_branch>`` before planning
(``AutoPin``): ``run`` and ``tick`` then start again from the new copy (``Repinned``).
"""

import fcntl
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .config import (DEFAULT_SENSITIVE_LISTS, ConfigError, apply_overlay, docs_entries,
                     read_toml)

POINTER = 'current'
PREFIX = 'pinned-'
STAMP = 'pin.json'
LOCAL_CONFIG = 'config.local.toml'
PENDING = 'pin-pending.json'
LOCK = 'pin.lock'
SHA = re.compile(r'[0-9a-f]{40}')
HEX = re.compile(r'[0-9a-f]{7,40}')
# The branches of origin, and its tags, fetched apart from the local tags of refs/tags/.
REMOTE_BRANCHES = 'refs/remotes/origin/'
REMOTE_TAGS = 'refs/devloop/origin-tags/'
# Run in the copy, with only the copy on the module path: the package and the configuration of
# the copy, with the overlay, must load, and the lists must be read from the copy.
VERIFY = '''
import sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
import devloop.__main__
from devloop import config
for module in (devloop.__main__, config):
    if not Path(module.__file__).resolve().is_relative_to(root):
        sys.exit(f'{module.__name__} imported from {module.__file__}, not from the copy')
if config.INSTALL_ROOT.resolve() != root:
    sys.exit(f'the copy reads its lists from {config.INSTALL_ROOT}')
cfg = config.load_config(None, sys.argv[2] or None)
print(len(cfg.review_policy.sensitive_files))
'''


class PinError(Exception):
    pass


class Repinned(Exception):
    """A new copy is pinned: the process starts again from ``copy``."""

    def __init__(self, copy):
        super().__init__(f'pinned {copy}')
        self.copy = Path(copy)


def _git(root, *args, binary=False):
    proc = subprocess.run(['git', *args], cwd=root, capture_output=True, text=not binary)
    if proc.returncode != 0:
        err = proc.stderr if not binary else proc.stderr.decode(errors='replace')
        raise PinError(f'git {" ".join(args)}: {err.strip() or "exit " + str(proc.returncode)}')
    return proc.stdout


def _verified_copies(state):
    """``{sha: copy}`` of the copies under ``state`` with a stamp naming their commit."""
    copies = {}
    for copy in sorted(Path(state).glob(PREFIX + '*')):
        try:
            sha = json.loads((copy / STAMP).read_text(encoding='utf-8'))['commit']
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if isinstance(sha, str) and SHA.fullmatch(sha) and copy.name == PREFIX + sha[:12]:
            copies[sha] = copy
    return copies


def resolve(root, state, ref, fetch=True):
    """The commit of ``ref``. An abbreviation of a copy already verified is that copy's commit,
    without a fetch. Otherwise the branches and tags of ``origin`` are updated first, dropping
    those deleted on ``origin``; a bare name is a branch of ``origin``, else a tag of ``origin``;
    and the commit must be reachable from a branch or a tag of ``origin``: a commit only local
    (or reachable only from a local tag, or from a branch since deleted on ``origin``) is
    refused."""
    if not ref or ref.startswith('-') or any(c.isspace() for c in ref):
        raise PinError(f'invalid ref {ref!r}')
    if HEX.fullmatch(ref):
        known = [sha for sha in _verified_copies(state) if sha.startswith(ref)]
        if len(known) > 1:
            raise PinError(f'{ref} is ambiguous among the pinned copies')
        if known:
            return known[0]
    if fetch:
        # The tags of origin go to a namespace of their own, apart from the local tags, which
        # prove nothing; --prune drops the branches and tags no longer on origin.
        _git(root, 'fetch', '--quiet', '--prune', '--no-tags', 'origin',
             f'+refs/heads/*:{REMOTE_BRANCHES}*', f'+refs/tags/*:{REMOTE_TAGS}*')
    name = ref
    if ref != 'HEAD' and not ref.startswith(('origin/', 'refs/')):
        for remote in (f'{REMOTE_BRANCHES}{ref}', f'{REMOTE_TAGS}{ref}'):
            if _git(root, 'for-each-ref', '--format=%(refname)', remote).strip() == remote:
                name = remote
                break
    try:
        sha = _git(root, 'rev-parse', '--verify', '--quiet', '--end-of-options',
                   f'{name}^{{commit}}').strip()
    except PinError:
        raise PinError(f'{ref}: not a commit') from None
    if not SHA.fullmatch(sha):
        raise PinError(f'{ref}: not a commit')
    if not _git(root, 'for-each-ref', '--contains', sha, '--count=1', '--format=%(refname)',
                REMOTE_BRANCHES, REMOTE_TAGS).strip():
        raise PinError(f'{ref} ({sha[:12]}) is not reachable from the refs of origin: a local '
                       'commit is never pinned')
    return sha


def required_files(root, sha, local):
    """The files of ``review_policy.sensitive_path_lists`` and the documents of ``[docs] map``
    that the copy of ``sha`` needs: read from config.toml of that commit with the overlay
    ``local`` applied. Each must be a relative path inside the root, and a regular file in the
    commit."""
    try:
        data = tomllib.loads(_git(root, 'show', f'{sha}:devloop/config.toml'))
    except ValueError as exc:
        raise PinError(f'devloop/config.toml of {sha[:12]}: {exc}') from exc
    try:
        if local and Path(local).exists():
            data = apply_overlay(data, read_toml(Path(local), 'local config'))
    except ConfigError as exc:
        raise PinError(str(exc)) from exc
    table = data.get('review_policy', {})
    names = table.get('sensitive_path_lists', list(DEFAULT_SENSITIVE_LISTS)) if isinstance(
        table, dict) else None
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise PinError('review_policy.sensitive_path_lists: expected a list of strings')
    files = []
    for name in names:
        parts = PurePosixPath(name).parts
        if (not name or name.startswith('/') or '\\' in name
                or any(p in ('.', '..') for p in parts) or name != '/'.join(parts)):
            raise PinError(f'sensitive_path_lists: {name!r} is not a relative path inside the '
                           'root')
        _require_blob(root, sha, name, 'sensitive_path_lists')
        if name not in files:
            files.append(name)
    # The documents of [docs] map (#203): the copy's configuration checks that they exist.
    try:
        documents = [d for _, docs in docs_entries(data) for d in docs]
    except ConfigError as exc:
        raise PinError(str(exc)) from exc
    for name in documents:
        if name not in files:
            _require_blob(root, sha, name, 'docs.map')
            files.append(name)
    return files


def _require_blob(root, sha, name, what):
    """PinError unless ``name`` is a regular file of commit ``sha``."""
    entry = _git(root, 'ls-tree', '--full-tree', sha, '--', name).split('\t', 1)[0].split()
    if not entry or entry[0] not in ('100644', '100755') or entry[1] != 'blob':
        raise PinError(f'{what}: {name} is not a file of {sha[:12]}')


def _set_mode(copy, writable):
    for path in [copy, *Path(copy).rglob('*')]:
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(0o755 if writable else 0o555)
        else:
            path.chmod(0o644 if writable else 0o444)


def _remove(copy):
    if Path(copy).exists():
        _set_mode(copy, writable=True)
        shutil.rmtree(copy)


def verify(copy, files, local, timeout=120):
    """Check the copy: every required file is there, and in a Python with only the copy on its
    module path the package imports and the configuration, with the overlay, loads its lists
    from the copy."""
    copy = Path(copy)
    for name in ['devloop/__init__.py', 'devloop/__main__.py', 'devloop/config.toml', *files]:
        if not (copy / name).is_file() or (copy / name).is_symlink():
            raise PinError(f'{copy.name}: {name} is missing')
    env = {k: v for k, v in os.environ.items() if not k.startswith('PYTHON')}
    env.update(PYTHONPATH=str(copy), PYTHONDONTWRITEBYTECODE='1')
    overlay = str(Path(local).resolve()) if local and Path(local).exists() else ''
    try:
        proc = subprocess.run([sys.executable, '-P', '-B', '-c', VERIFY, str(copy), overlay],
                              cwd=copy, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise PinError(f'{copy.name}: the check did not end in {timeout} s') from None
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        raise PinError(f'{copy.name}: the check failed: {lines[-1] if lines else proc.returncode}')


def _extract(root, sha, files, dest):
    """``devloop/`` and ``files`` of ``sha`` into a new temporary directory next to ``dest``."""
    data = _git(root, 'archive', '--format=tar', sha, '--', 'devloop', *files, binary=True)
    tmp = Path(tempfile.mkdtemp(prefix=f'.{dest.name}-', dir=dest.parent))
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(tmp, filter='data')
        (tmp / STAMP).write_text(json.dumps({'commit': sha, 'files': files}) + '\n',
                                 encoding='utf-8')
    except BaseException:
        _remove(tmp)
        raise
    return tmp


def point(state, copy):
    """Point ``state/current`` at ``copy`` (a directory in ``state``) with an atomic rename."""
    state = Path(state)
    tmp = state / f'.{POINTER}-{os.getpid()}'
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(Path(copy).name, tmp)
    os.replace(tmp, state / POINTER)


def current(state):
    """The copy ``state/current`` points at, or None."""
    pointer = Path(state) / POINTER
    return pointer.resolve() if pointer.is_symlink() and pointer.resolve().is_dir() else None


@contextmanager
def _locked(state):
    with open(Path(state) / LOCK, 'w') as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PinError('another pin is running') from None
        yield


def pin(root, state, ref, local=None, log=print, fetch=True):
    """Pin ``ref``: the verified copy of its commit, which ``state/current`` now points at.
    ``local`` is the overlay (used when the file exists). On any failure ``current`` is left as
    it was."""
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    with _locked(state):
        sha = resolve(root, state, ref, fetch=fetch)
        dest = state / f'{PREFIX}{sha[:12]}'
        reused = dest.exists() or dest.is_symlink()
        if reused:
            if _verified_copies(state).get(sha) != dest or dest.is_symlink():
                raise PinError(f'{dest.name} exists but is not the copy of {sha[:12]}')
            files = required_files(root, sha, local)
            verify(dest, files, local)
        else:
            files = required_files(root, sha, local)
            tmp = _extract(root, sha, files, dest)
            try:
                _set_mode(tmp, writable=False)
                verify(tmp, files, local)
                os.rename(tmp, dest)
            except BaseException:
                _remove(tmp)
                raise
        point(state, dest)
        clear_pending(state)
    log(f'pinned {ref} at {sha[:12]}: {dest}' + (' (copy reused)' if reused else ''))
    return dest


def request(state, pr, head):
    """Record that the merge of ``pr`` at ``head`` needs a pin at the next tick."""
    path = Path(state) / PENDING
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{PENDING}-{os.getpid()}')
    tmp.write_text(json.dumps({'pr': pr, 'head': head, 'at': int(time.time())}) + '\n',
                   encoding='utf-8')
    os.replace(tmp, path)


def pending(state):
    """The pin request recorded after a merge, or None."""
    try:
        return json.loads((Path(state) / PENDING).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {'pr': None, 'head': None}


def clear_pending(state):
    try:
        (Path(state) / PENDING).unlink()
    except FileNotFoundError:
        pass


class AutoPin:
    """The pin after a merge (#174), as the dispatcher uses it."""

    def __init__(self, root, state, local=None, log=print):
        self.root, self.state, self.local, self.log = Path(root), Path(state), local, log

    def request(self, pr, head):
        request(self.state, pr, head)

    def pending(self):
        return pending(self.state)

    def pin(self, ref):
        return pin(self.root, self.state, ref, self.local, log=self.log)


def reexec(copy, argv, lock_fd=None, lock_var='DEVLOOP_LOCK_FD', execve=os.execve):
    """Start devloop again from ``copy`` with the same arguments: ``python3 -P -m devloop``
    with only the copy on ``PYTHONPATH``. The lock passes to the new process open (its file
    descriptor in ``lock_var``), so no other instance can start in between."""
    env = dict(os.environ, PYTHONPATH=str(copy))
    if lock_fd is not None:
        os.set_inheritable(lock_fd, True)
        env[lock_var] = str(lock_fd)
    sys.stdout.flush()
    sys.stderr.flush()
    execve(sys.executable, [sys.executable, '-P', '-m', 'devloop', *argv], env)
