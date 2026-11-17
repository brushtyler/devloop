"""Pinned installations (#174): ``devloop pin <ref>`` and the pin after a merge.

devloop runs from a read-only copy of one commit of ``origin``, never from the checkout it
changes. ``pin`` resolves ``<ref>`` to a commit reachable from the refs of ``origin`` (or to a
copy already verified, reused without a fetch: the rollback), extracts ``devloop/`` and the files
of ``review_policy.sensitive_path_lists`` of that commit into ``<state_dir>/pinned-<sha12>``
(not the documents of ``[docs] map``: they belong to the repository, #210), makes it read-only, checks that it imports and that its configuration, with the local overlay,
loads, runs the smoke test of a new copy (``Smoke``, #260: ``tick --dry-run`` and one round of
``watch`` from the copy), and only then points ``<state_dir>/current`` at it with an atomic
rename. A failure leaves ``current`` as it was. Earlier copies stay for the rollback; a copy that
fails the smoke test stays as ``rejected-<sha12>``, for the analysis.

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

from .config import DEFAULT_SENSITIVE_LISTS, ConfigError, apply_overlay, read_toml

POINTER = 'current'
PREFIX = 'pinned-'
STAMP = 'pin.json'
LOCAL_CONFIG = 'config.local.toml'
PENDING = 'pin-pending.json'
LOCK = 'pin.lock'
# A copy refused by the smoke test (#260), kept for the analysis; never a pinned copy.
REJECTED = 'rejected-'
# The commits whose refusal by the smoke test of an auto-pin was published as an alert (#260).
ALERTED = 'pin-alerted.json'
MAX_ALERTED = 50
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
# The smoke test (#260) runs ``python3 -m devloop <args>`` of the copy through this wrapper: an
# exception that escapes devloop leaves, on stderr, a marker line with the names of its types and
# its message, so that a transient error (network, GitHub 5xx) is told apart from one of the code.
SMOKE_MARK = 'devloop-smoke-exception '
SMOKE = '''
import json, runpy, sys
sys.argv = ['devloop', *sys.argv[1:]]
try:
    runpy.run_module('devloop', run_name='__main__', alter_sys=True)
except Exception as exc:
    info = {'types': [t.__name__ for t in type(exc).__mro__], 'message': str(exc)}
    print(%r + json.dumps(info), file=sys.stderr, flush=True)
    raise
''' % SMOKE_MARK
# The watch round of the smoke test: its own state file, at most this many seconds.
SMOKE_WATCH_SECONDS = 30
SMOKE_TICK_SECONDS = 300
# The types of an exception that is transient whatever its message.
TRANSIENT_TYPES = frozenset({'TimeoutError', 'ConnectionError', 'URLError', 'timeout'})
# The failure classes of ``watch.failure_class`` that are transient: retried once.
TRANSIENT_CLASSES = frozenset({'server error', 'network'})
ROUND_FAILED = re.compile(r'devloop watch: round failed \(([^)]*)\)')


class PinError(Exception):
    pass


class SmokeError(PinError):
    """The smoke test of the copy of ``sha`` failed at ``step``; the copy is kept at ``copy``."""

    def __init__(self, message, sha, step, copy=None):
        super().__init__(message)
        self.sha, self.step, self.copy = sha, step, copy


class StepFailed(Exception):
    """A step of the smoke test failed; ``transient`` when a second attempt may succeed."""

    def __init__(self, message, transient=False, step=None):
        super().__init__(message)
        self.transient, self.step = transient, step


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
    """The files of ``review_policy.sensitive_path_lists`` that the copy of ``sha`` needs: read
    from config.toml of that commit with the overlay ``local`` applied. Each must be a relative
    path inside the root, and a regular file in the commit."""
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
    # The documents of [docs] map (#203) are not extracted (#210): they belong to the repository
    # devloop works on, and the dispatcher looks for them in the worktree of each turn.
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
    try:
        proc = subprocess.run([sys.executable, '-P', '-B', '-c', VERIFY, str(copy),
                               _overlay(local)], cwd=copy, env=_copy_env(copy),
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise PinError(f'{copy.name}: the check did not end in {timeout} s') from None
    if proc.returncode != 0:
        raise PinError(f'{copy.name}: the check failed: {_last_line(proc)}')


def _copy_env(copy):
    """The environment of a Python with only ``copy`` on its module path."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('PYTHON')}
    env.pop('DEVLOOP_LOCK_FD', None)
    env.update(PYTHONPATH=str(copy), PYTHONDONTWRITEBYTECODE='1')
    return env


def _overlay(local):
    """The absolute path of the overlay ``local``, or '' when it does not exist."""
    return str(Path(local).resolve()) if local and Path(local).exists() else ''


def _last_line(proc):
    lines = (proc.stderr or proc.stdout or '').strip().splitlines()
    return lines[-1] if lines else f'exit {proc.returncode}'


def _github_class(message):
    """The failure class ``watch.failure_class`` gives a GitHubError with ``message``."""
    from .watch import FAILURE_CLASSES  # watch imports the dispatcher, which imports pin
    for name, pattern in FAILURE_CLASSES:
        if pattern.search(message):
            return name
    return None


class Smoke:
    """The smoke test of a new copy (#260), after ``verify`` has loaded its configuration with
    the overlay. From the copy, in the main checkout ``root`` and with the overlay, it runs
    ``devloop tick --dry-run`` (it plans, without agents and without writes) and one round of
    ``devloop watch`` on a state file of its own, at most ``watch_seconds``. A step that fails
    with a transient error (a network error, a GitHub 5xx, its time limit) runs once more; any
    other failure, or a second one, raises ``StepFailed`` with the ``step``."""

    def __init__(self, root, log=print, run=subprocess.run, watch_seconds=SMOKE_WATCH_SECONDS,
                 tick_seconds=SMOKE_TICK_SECONDS):
        self.root, self.log, self._run = Path(root), log, run
        self.watch_seconds, self.tick_seconds = watch_seconds, tick_seconds

    def __call__(self, copy, local):
        copy = Path(copy).resolve()
        overlay = _overlay(local)
        prefix = ['--local-config', overlay] if overlay else []
        seconds = str(self.watch_seconds)
        with tempfile.TemporaryDirectory(prefix='devloop-smoke-') as tmp:
            state = Path(tmp) / 'watch-state.json'
            steps = (
                ('tick --dry-run', [*prefix, 'tick', '--dry-run'], self.tick_seconds,
                 self._tick_ok, None),
                ('watch', [*prefix, 'watch', '--until-change', '--interval', seconds,
                           '--timeout', seconds, '--state', str(state)],
                 self.watch_seconds + 60, self._watch_ok, state),
            )
            for step, args, timeout, check, fresh in steps:
                for attempt in (1, 2):
                    if fresh is not None and fresh.exists():
                        fresh.unlink()  # each round of watch starts from a new baseline
                    try:
                        check(self._step(copy, args, timeout))
                        break
                    except StepFailed as exc:
                        exc.step = step
                        if not exc.transient or attempt == 2:
                            raise
                        self.log(f'smoke test: {step} failed with a transient error ({exc}), '
                                 'trying once more')
        self.log('smoke test passed: ' + ', '.join(step for step, *_ in steps))

    def _step(self, copy, args, timeout):
        try:
            return self._run([sys.executable, '-P', '-B', '-c', SMOKE, *args], cwd=self.root,
                             env=_copy_env(copy), capture_output=True, text=True,
                             timeout=timeout)
        except subprocess.TimeoutExpired:
            raise StepFailed(f'did not end in {timeout} s', transient=True) from None

    @staticmethod
    def _failure(proc):
        """The ``StepFailed`` of a step that exited with an error: transient for a network
        error or a GitHub 5xx left by the wrapper's marker, else an error of the code."""
        marks = [line[len(SMOKE_MARK):] for line in (proc.stderr or '').splitlines()
                 if line.startswith(SMOKE_MARK)]
        try:
            info = json.loads(marks[-1]) if marks else None
            types, message = list(info['types']), str(info['message'])
        except (ValueError, KeyError, TypeError):
            return StepFailed(_last_line(proc))
        transient = bool(TRANSIENT_TYPES & set(types)) or (
            'GitHubError' in types and _github_class(message) in TRANSIENT_CLASSES)
        first = message.strip().splitlines()[0] if message.strip() else ''
        return StepFailed(f'{types[0]}: {first}' if first else types[0], transient)

    def _tick_ok(self, proc):
        if proc.returncode != 0:
            raise self._failure(proc)

    def _watch_ok(self, proc):
        """0 (news) or 3 (none) after a round that ended, and no round failed."""
        if proc.returncode not in (0, 3):
            raise self._failure(proc)
        failed = ROUND_FAILED.findall(proc.stderr or '')
        if failed:
            raise StepFailed(f'a round failed ({", ".join(failed)})',
                             transient=all(name in TRANSIENT_CLASSES for name in failed))
        if not (proc.stdout or '').strip():
            raise StepFailed(f'no round ended in {self.watch_seconds} s', transient=True)


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


def _reject(state, sha, tmp):
    """Keep the copy ``tmp`` refused by the smoke test as ``state/rejected-<sha12>``, in place
    of an earlier one of the same commit; its path, or None when it could not be kept."""
    kept = Path(state) / f'{REJECTED}{sha[:12]}'
    try:
        if kept.is_symlink():
            kept.unlink()
        _remove(kept)
        os.rename(tmp, kept)
    except OSError:
        _remove(tmp)
        return None
    return kept


def pin(root, state, ref, local=None, log=print, fetch=True, smoke=True):
    """Pin ``ref``: the verified copy of its commit, which ``state/current`` now points at.
    ``local`` is the overlay (used when the file exists). On any failure ``current`` is left as
    it was.

    A new copy passes the smoke test (#260) before ``current`` moves: ``smoke`` is True for
    ``Smoke``, a callable ``smoke(copy, local)`` raising ``StepFailed``, or False to skip it
    (``--no-smoke``, logged). A copy that fails it stays as ``rejected-<sha12>`` and the pin
    raises ``SmokeError``. A copy already pinned (the rollback) is not tested again."""
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
            failed = None
            try:
                _set_mode(tmp, writable=False)
                verify(tmp, files, local)
                if smoke is False:
                    log(f'smoke test of {sha[:12]} skipped (--no-smoke)')
                else:
                    try:
                        (Smoke(root, log=log) if smoke is True else smoke)(tmp, local)
                    except StepFailed as exc:
                        failed = exc
                if failed is None:
                    os.rename(tmp, dest)
            except BaseException:
                _remove(tmp)
                raise
            if failed is not None:
                kept = _reject(state, sha, tmp)
                step = getattr(failed, 'step', None) or 'smoke test'
                raise SmokeError(f'smoke test of {sha[:12]} failed at {step}: {failed}; '
                                 + (f'the copy is kept in {kept}' if kept else
                                    'the copy could not be kept'), sha, step, kept)
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

    def __init__(self, root, state, local=None, log=print, smoke=True):
        self.root, self.state, self.local, self.log = Path(root), Path(state), local, log
        self.smoke = smoke

    def request(self, pr, head):
        request(self.state, pr, head)

    def pending(self):
        return pending(self.state)

    def cancel(self):
        """Drop the request: a copy refused by the smoke test is not tried again (#260)."""
        clear_pending(self.state)

    def pin(self, ref):
        return pin(self.root, self.state, ref, self.local, log=self.log, smoke=self.smoke)

    def _alerted(self):
        try:
            data = json.loads((self.state / ALERTED).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return []
        return [sha for sha in data if isinstance(sha, str)] if isinstance(data, list) else []

    def alerted(self, sha):
        """Whether the refusal of ``sha`` by the smoke test was already published."""
        return sha in self._alerted()

    def mark_alerted(self, sha):
        shas = [s for s in self._alerted() if s != sha][-(MAX_ALERTED - 1):] + [sha]
        path = self.state / ALERTED
        tmp = path.with_name(f'.{ALERTED}-{os.getpid()}')
        tmp.write_text(json.dumps(shas) + '\n', encoding='utf-8')
        os.replace(tmp, path)


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
