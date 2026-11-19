"""Pinned installations (#174): ``devloop pin <ref>`` and the pin after a merge.

devloop runs from a read-only copy of one commit of ``origin``, never from the checkout it
changes. ``pin`` resolves ``<ref>`` to a commit reachable from the refs of ``origin`` (or to a
copy already verified, reused without a fetch: the rollback), extracts ``devloop/``, ``core/``
when the commit has it (#365), and the files of ``review_policy.sensitive_path_lists`` of that
commit into ``<state_dir>/pinned-<sha12>``
(not the documents of ``[docs] map``: they belong to the repository, #210), makes it read-only, checks that it imports and that its configuration, with the local overlay
and the project file of ``state_dir`` (ADR 0006, R2: never a file of the commit), loads, runs the smoke test of a new copy (``Smoke``, #260: ``tick --dry-run`` and one round of
``watch`` from the copy), and only then points ``<state_dir>/current`` at it with an atomic
rename. A failure leaves ``current`` as it was. Earlier copies stay for the rollback; a copy that
fails the smoke test stays as ``rejected-<sha12>``, for the analysis.

With ``[pin] auto = true`` a merge by devloop that touches ``devloop/``, ``core/`` or a list file records a
request (``pin-pending.json``), and the next tick pins ``origin/<base_branch>`` before planning
(``AutoPin``): ``run`` and ``tick`` then start again from the new copy (``Repinned``). In the
continuous mode of ``run`` the pin runs at once, and the restart waits until no action runs (#301).
A merge by anyone else (the operator's, #430) records the same request: at each tick, before
the pin, ``AutoPin.observe`` fetches ``origin/<base_branch>`` and, when it advanced since the
head last seen (``pin-base.json``; the first time, since the commit of ``current``) with changes
to those paths, requests the pin of the new head. ``devloop status`` says when the copy it runs
from is behind ``origin/<base_branch>`` on those paths (``AutoPin.behind``).

The installation's source (ADR 0006, R1) is the clone the pin takes the code from: the
managed repository itself, as for ssdlc, unless the configuration names another clone in
``source`` (``source_root``). The smoke test still runs in the managed repository. The pin after
a merge is only for a source that is the managed repository: a merge in another project never
touches the installation.

Each successful pin, of a new copy or a reused one, records its time in ``pin-times.json``. With
``keep`` (``[gc] keep_pins``, #380) the pin then keeps ``current`` and the ``keep`` other copies
pinned most recently (``prune``), and removes the others, except a copy a process still uses: that
one stays, and a later cleanup tries again.
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
from contextlib import contextmanager, nullcontext
from pathlib import Path, PurePosixPath

from .config import DEFAULT_SENSITIVE_LISTS, INSTALL_ROOT, ConfigError, compose, read_toml

POINTER = 'current'
# The time of the last successful pin of each copy (#380): the order of ``prune``.
TIMES = 'pin-times.json'
PREFIX = 'pinned-'
STAMP = 'pin.json'
LOCAL_CONFIG = 'config.local.toml'
# The packages besides ``devloop/`` a copy holds when its commit has them: ``core`` (ADR 0004,
# section 3.4), which devloop imports for the budget envelope (#365).
CODE_PACKAGES = ('core',)
PENDING = 'pin-pending.json'
# The head of origin/<base_branch> the last tick saw (#430): an advance from it requests a pin.
BASE_SEEN = 'pin-base.json'
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
if len(sys.argv) > 3 and sys.argv[3]:
    import inspect
    if 'project' not in inspect.signature(config.load_config).parameters:
        sys.exit('the copy cannot read the project file ' + sys.argv[3])
    cfg = config.load_config(None, sys.argv[2] or None, sys.argv[3])
else:
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


def source_root(managed, source):
    """The main checkout of the installation's source (ADR 0006, R1), where a pin takes the
    code from: ``managed``, the managed repository's main checkout, when ``source`` is empty;
    else the clone at ``source`` (absolute, with ``~``, or relative to ``managed``), which must be
    the top of a git checkout. Raises ``PinError`` otherwise."""
    managed = Path(managed)
    if not source:
        return managed
    path = Path(source).expanduser()
    path = path if path.is_absolute() else managed / path
    if not path.is_dir():
        raise PinError(f'source {source}: not a directory')
    top = _git(path, 'rev-parse', '--show-toplevel').strip()
    if not top or Path(top).resolve() != path.resolve():
        raise PinError(f'source {source}: not the top of a git checkout')
    common = _git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir').strip()
    return Path(common).parent


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


def resolve(root, state, ref, fetch=True, fetch_lock=None):
    """The commit of ``ref``. An abbreviation of a copy already verified is that copy's commit,
    without a fetch. Otherwise the branches and tags of ``origin`` are updated first, dropping
    those deleted on ``origin``; a bare name is a branch of ``origin``, else a tag of ``origin``;
    and the commit must be reachable from a branch or a tag of ``origin``: a commit only local
    (or reachable only from a local tag, or from a branch since deleted on ``origin``) is
    refused. ``fetch_lock``, when given, is held during the fetch: the one of the workspace, so
    that the fetch of a pin does not run with those of the running actions (#301)."""
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
        with fetch_lock or nullcontext():
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


def required_files(root, sha, local, project=None):
    """The files of ``review_policy.sensitive_path_lists`` that the copy of ``sha`` needs: read
    from config.toml of that commit with the overlay ``local`` and the project file ``project``
    (ADR 0006, R2, never a file of the commit) composed, when they exist. Each must be a
    relative path inside the root, and a regular file in the commit."""
    try:
        data = tomllib.loads(_git(root, 'show', f'{sha}:devloop/config.toml'))
    except ValueError as exc:
        raise PinError(f'devloop/config.toml of {sha[:12]}: {exc}') from exc
    try:
        overlay = (read_toml(Path(local), 'local config')
                   if local and Path(local).exists() else None)
        table = (read_toml(Path(project), 'project file')
                 if project and Path(project).exists() else None)
        data = compose(data, table, overlay)
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


def verify(copy, files, local, timeout=120, project=None):
    """Check the copy: every required file is there, and in a Python with only the copy on its
    module path the package imports and the configuration, with the overlay and the project
    file ``project`` (ADR 0006, R2) when it exists, loads its lists from the copy. A copy whose
    code cannot read the project file is refused while there is one: it would run without it."""
    copy = Path(copy)
    for name in ['devloop/__init__.py', 'devloop/__main__.py', 'devloop/config.toml', *files]:
        if not (copy / name).is_file() or (copy / name).is_symlink():
            raise PinError(f'{copy.name}: {name} is missing')
    try:
        proc = subprocess.run([sys.executable, '-P', '-B', '-c', VERIFY, str(copy),
                               _overlay(local), _overlay(project)], cwd=copy,
                              env=_copy_env(copy),
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
    """The absolute path of the overlay ``local`` (or of the project file), or '' when it does
    not exist."""
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

    def __call__(self, copy, local, project=None):
        copy = Path(copy).resolve()
        overlay = _overlay(local)
        prefix = ['--local-config', overlay] if overlay else []
        # The project file (ADR 0006, R2), as the launcher passes it.
        if _overlay(project):
            prefix += ['--project-config', _overlay(project)]
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


def _has_tree(root, sha, name):
    """Whether ``name`` is a directory of commit ``sha``."""
    entry = _git(root, 'ls-tree', '--full-tree', sha, '--', name).split('\t', 1)[0].split()
    return len(entry) > 1 and entry[1] == 'tree'


def _extract(root, sha, files, dest):
    """``devloop/``, ``core/`` when the commit has it (devloop imports it since #365), and
    ``files`` of ``sha`` into a new temporary directory next to ``dest``."""
    packages = ['devloop'] + [name for name in CODE_PACKAGES if _has_tree(root, sha, name)]
    data = _git(root, 'archive', '--format=tar', sha, '--', *packages, *files, binary=True)
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


def pin(root, state, ref, local=None, log=print, fetch=True, smoke=True, fetch_lock=None,
        keep=None, in_use=None, managed=None, project=None):
    """Pin ``ref``: the verified copy of its commit, which ``state/current`` now points at.
    ``local`` is the overlay and ``project`` the project file (ADR 0006, R2), each used when
    the file exists: the copy is checked with them. On any failure ``current`` is left as
    it was. ``root`` is the main checkout of the installation's source, whose ``origin`` the
    commit comes from; ``managed`` (default: ``root``) that of the managed repository, where the
    smoke test runs (ADR 0006, R1).

    A new copy passes the smoke test (#260) before ``current`` moves: ``smoke`` is True for
    ``Smoke``, a callable ``smoke(copy, local)`` raising ``StepFailed``, or False to skip it
    (``--no-smoke``, logged). A copy that fails it stays as ``rejected-<sha12>`` and the pin
    raises ``SmokeError``. A copy already pinned (the rollback) is not tested again.
    ``fetch_lock`` is held during the fetch (``resolve``).

    The pin records its time (``TIMES``). With ``keep`` (an integer, ``[gc] keep_pins``, #380)
    it then removes the copies beyond ``current`` and the ``keep`` pinned most recently
    (``prune``, with ``in_use``); a failure there is logged and the pin stands."""
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    with _locked(state):
        sha = resolve(root, state, ref, fetch=fetch, fetch_lock=fetch_lock)
        dest = state / f'{PREFIX}{sha[:12]}'
        reused = dest.exists() or dest.is_symlink()
        if reused:
            if _verified_copies(state).get(sha) != dest or dest.is_symlink():
                raise PinError(f'{dest.name} exists but is not the copy of {sha[:12]}')
            files = required_files(root, sha, local, project)
            verify(dest, files, local, project=project)
        else:
            files = required_files(root, sha, local, project)
            tmp = _extract(root, sha, files, dest)
            failed = None
            try:
                _set_mode(tmp, writable=False)
                verify(tmp, files, local, project=project)
                if smoke is False:
                    log(f'smoke test of {sha[:12]} skipped (--no-smoke)')
                else:
                    try:
                        # A smoke test of the caller's gets the project file only when there
                        # is one: one written before ADR 0006, R2 takes two arguments.
                        (Smoke(managed or root, log=log) if smoke is True else smoke)(
                            tmp, local, *((project,) if _overlay(project) else ()))
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
        try:
            _record_time(state, dest)
            if keep is not None:
                prune(state, keep, log=log, in_use=in_use)
        except OSError as exc:
            log(f'gc: the cleanup of the pinned copies failed, the pin stands: {exc}')
    return dest


def _times(state):
    """``{copy name: time of its last successful pin}`` (``TIMES``); {} when unreadable."""
    try:
        data = json.loads((Path(state) / TIMES).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, (int, float))
            and not isinstance(v, bool)}


def _record_time(state, copy=None, clock=time.time):
    """Record now as the time of the last successful pin of ``copy`` (#380), when given, and
    forget the copies no longer in ``state``."""
    state = Path(state)
    times = {name: at for name, at in _times(state).items() if (state / name).is_dir()}
    if copy is not None:
        times[Path(copy).name] = clock()
    tmp = state / f'.{TIMES}-{os.getpid()}'
    tmp.write_text(json.dumps(times, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    os.replace(tmp, state / TIMES)


def _uids(entry):
    """The user ids (real, effective, saved, filesystem) of process ``entry`` of ``/proc``, read
    in its ``status``, which every user can read even when the process is not dumpable (its
    other entries then belong to root). Raises ``OSError``: ``FileNotFoundError`` or
    ``ProcessLookupError`` when the process is gone."""
    for line in (entry / 'status').read_text(encoding='utf-8', errors='replace').splitlines():
        if line.startswith('Uid:'):
            return {int(uid) for uid in line.split()[1:] if uid.isdigit()}
    raise OSError(f'{entry / "status"}: no Uid line')


GONE = (FileNotFoundError, ProcessLookupError)


def copies_in_use(copies, proc=Path('/proc'), uid=None):
    """The names of ``copies`` that a process uses (#380): one whose ``PYTHONPATH``, working
    directory or command line names the copy or a path inside it, as the launcher, a restart
    and the checks of a pin do. The processes of the user ``uid`` (default: this one) count,
    those of other users do not; a process gone meanwhile does not count either. None when it
    cannot be told: ``proc`` is not there, or a live process of the user cannot be read (one
    not dumpable, ``PR_SET_DUMPABLE`` 0, hides its environment and working directory)."""
    proc = Path(proc)
    if not proc.is_dir():
        return None
    uid = os.getuid() if uid is None else uid
    roots = {os.path.realpath(c): Path(c).name for c in copies}

    def names(path):
        path = os.path.realpath(path) if path else ''
        return {name for root, name in roots.items()
                if path == root or path.startswith(root + os.sep)}

    used = set()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if uid not in _uids(entry):
                continue  # a process of another user
            environ = (entry / 'environ').read_bytes().split(b'\0')
            cmdline = (entry / 'cmdline').read_bytes().split(b'\0')
            try:
                cwd = os.readlink(entry / 'cwd')
            except GONE:
                cwd = None  # a zombie has none
        except GONE:
            continue  # gone meanwhile
        except (OSError, ValueError):
            return None  # alive, of the user, and not readable: it may use any copy
        for var in environ:
            if var.startswith(b'PYTHONPATH='):
                for part in os.fsdecode(var[len(b'PYTHONPATH='):]).split(os.pathsep):
                    used |= names(part)
        for arg in cmdline:
            if arg.startswith(b'/'):
                used |= names(os.fsdecode(arg))
        used |= names(cwd)
    return used


def prune(state, keep, log=print, in_use=None):
    """Remove the pinned copies of ``state`` beyond ``current`` and the ``keep`` others pinned
    most recently (#380): ordered by the time of their last successful pin (``TIMES``), else by
    the time the directory was last modified. Never ``current``, nor the copy this process runs
    from, nor one a process uses (``in_use(copies)``, default ``copies_in_use``: a set of names,
    or None when it cannot be told, and then nothing is removed); such a copy stays and a later
    cleanup tries again. Only verified copies count (``pinned-<sha12>`` with its stamp); a
    rejected copy is never touched. Each removal is logged. Returns the names removed.
    The caller holds the lock of the pins."""
    state = Path(state)
    pointed = current(state)
    running = INSTALL_ROOT.resolve()
    times = _times(state)

    def when(copy):
        if copy.name in times:
            return times[copy.name]
        try:
            return copy.stat().st_mtime
        except OSError:
            return 0

    others = sorted((copy for copy in _verified_copies(state).values()
                     if not copy.is_symlink() and copy.resolve() != pointed),
                    key=lambda copy: (when(copy), copy.name), reverse=True)
    extra = [copy for copy in others[keep:] if copy.resolve() != running]
    if not extra:
        return []
    used = (in_use or copies_in_use)(extra)
    if used is None:
        log(f'gc: {len(extra)} pinned copies beyond the last {keep} not removed: the processes '
            'that may use them cannot be read; a later cleanup tries again')
        return []
    removed = []
    for copy in extra:
        if copy.name in used:
            log(f'gc: {copy.name} is still in use, kept for a later cleanup')
            continue
        try:
            _remove(copy)
        except OSError as exc:
            log(f'gc: the pinned copy {copy.name} could not be removed: {exc}')
            continue
        removed.append(copy.name)
        log(f'gc: removed the pinned copy {copy.name}')
    if removed and (state / TIMES).exists():
        _record_time(state)
    return removed


def prune_now(state, keep, log=print, in_use=None):
    """``prune`` under the lock of the pins, for a cleanup outside a pin; a pin running now
    leaves the copies for a later cleanup. Returns the names removed."""
    try:
        with _locked(state):
            return prune(state, keep, log=log, in_use=in_use)
    except PinError as exc:
        log(f'gc: pinned copies not cleaned up: {exc}')
        return []


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


def copy_commit(copy):
    """The commit of the pinned copy ``copy``, read in its stamp, or None when ``copy`` is not
    one (devloop running from a checkout)."""
    if copy is None:
        return None
    try:
        sha = json.loads((Path(copy) / STAMP).read_text(encoding='utf-8'))['commit']
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return sha if isinstance(sha, str) and SHA.fullmatch(sha) else None


def install_changes(root, old, new, touches):
    """The files that differ between commits ``old`` and ``new`` and that ``touches`` (``[pin]
    paths``) counts as the installation's, in git's order. Raises ``PinError`` when git cannot
    compare them."""
    names = _git(root, 'diff', '--name-only', '--no-renames', '-z', old, new, '--')
    return [name for name in names.split('\0') if name and touches([name])]


def base_head(root, branch, fetch_lock=None):
    """The head of ``origin/<branch>`` after a fetch of that branch alone (held under
    ``fetch_lock``), into the ref ``resolve`` uses."""
    with fetch_lock or nullcontext():
        _git(root, 'fetch', '--quiet', '--no-tags', 'origin',
             f'+refs/heads/{branch}:{REMOTE_BRANCHES}{branch}')
    return last_fetched(root, branch)


def last_fetched(root, branch):
    """The head of ``origin/<branch>`` as last fetched, without a fetch."""
    sha = _git(root, 'rev-parse', '--verify', '--quiet', '--end-of-options',
               f'{REMOTE_BRANCHES}{branch}^{{commit}}').strip()
    if not SHA.fullmatch(sha):
        raise PinError(f'origin/{branch}: not a commit')
    return sha


class AutoPin:
    """The pin after a merge (#174), as the dispatcher uses it. ``fetch_lock`` is the lock of
    the workspace's shared git commands, held during the fetch of a pin that runs while actions
    run (#301). ``keep`` (``[gc] keep_pins`` with ``[gc] pins``, else None) is how many copies
    besides ``current`` a successful pin keeps (#380)."""

    def __init__(self, root, state, local=None, log=print, smoke=True, fetch_lock=None,
                 keep=None, project=None):
        self.root, self.state, self.local, self.log = Path(root), Path(state), local, log
        self.smoke, self.fetch_lock, self.keep = smoke, fetch_lock, keep
        self.project = project  # the project file (ADR 0006, R2), or None

    def request(self, pr, head):
        request(self.state, pr, head)

    def pending(self):
        return pending(self.state)

    def cancel(self):
        """Drop the request: a copy refused by the smoke test is not tried again (#260)."""
        clear_pending(self.state)

    def _seen(self):
        try:
            sha = json.loads((self.state / BASE_SEEN).read_text(encoding='utf-8'))['head']
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return sha if isinstance(sha, str) and SHA.fullmatch(sha) else None

    def _record_seen(self, sha):
        path = self.state / BASE_SEEN
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f'.{BASE_SEEN}-{os.getpid()}')
        tmp.write_text(json.dumps({'head': sha, 'at': int(time.time())}) + '\n',
                       encoding='utf-8')
        os.replace(tmp, path)

    def observe(self, branch, touches):
        """The pin after a merge nobody announced (#430), such as one of the operator: fetch
        ``origin/<branch>`` and, when its head advanced since the head the last call saw (the
        first time: since the commit of ``current``) with changes to the files ``touches``
        counts, request the pin of the new head, as a merge by devloop does. Nothing is
        requested when a request is already pending, nor when ``current`` already holds the
        new head; when git cannot compare the two commits (a head no longer known) the pin is
        requested all the same. The head is then recorded as seen, so that a copy refused by the
        smoke test is not tried again until the branch advances. Returns the head requested,
        or None. Raises ``PinError`` or ``OSError`` when the branch cannot be read: the next
        tick tries again."""
        head = base_head(self.root, branch, self.fetch_lock)
        seen = self._seen()
        if head == seen:
            return None
        pinned = copy_commit(current(self.state))
        since = seen or pinned
        requested = None
        if since and head != pinned and not self.pending():
            try:
                changed = install_changes(self.root, since, head, touches)
            except PinError as exc:
                self.log(f'auto-pin: the changes of origin/{branch} since {since[:12]} cannot '
                         f'be read ({exc}): pin requested all the same')
                changed = None
            if changed is None or changed:
                self.request(None, head)
                requested = head
                if changed:
                    self.log(f'auto-pin: origin/{branch} advanced to {head[:12]} with '
                             f'{len(changed)} changed files of the installation ('
                             + ', '.join(changed[:5]) + (', …' if len(changed) > 5 else '')
                             + '): pin requested')
        self._record_seen(head)
        return requested

    def behind(self, branch, touches, copy=None):
        """The line of ``devloop status`` when the pinned ``copy`` (default: the one this
        process runs from) differs from ``origin/<branch>``, as last fetched, in the files
        ``touches`` counts (#430); None when it does not, or when ``copy`` is not a pinned
        copy. Raises ``PinError`` when git cannot read the branch or compare the commits."""
        copy = INSTALL_ROOT if copy is None else copy
        running = copy_commit(copy)
        if running is None:
            return None
        head = last_fetched(self.root, branch)
        if head == running:
            return None
        changed = install_changes(self.root, running, head, touches)
        if not changed:
            return None
        try:
            _git(self.root, 'merge-base', '--is-ancestor', running, head)
            how = 'behind'
        except PinError:
            how = 'not an ancestor of'
        return (f'pinned copy: {Path(copy).name} ({running[:12]}) is {how} origin/{branch} '
                f'({head[:12]}, as last fetched) in {len(changed)} files of the installation: '
                + ', '.join(changed[:5]) + (', …' if len(changed) > 5 else ''))

    def pin(self, ref):
        return pin(self.root, self.state, ref, self.local, log=self.log, smoke=self.smoke,
                   fetch_lock=self.fetch_lock, keep=self.keep, project=self.project)

    def current(self):
        """The copy ``current`` points at now, or None: the operator may pin another one by
        hand while a restart waits (#301)."""
        return current(self.state)

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
