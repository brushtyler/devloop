"""Git worktrees for agent work. The main checkout's working tree is never touched."""

import base64
import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import nullcontext
from pathlib import Path

from .agentuser import SHARED_DIR, TMP, SharedDir, with_git_config
from .localtests import failed_cases, prepared_output

# The opening and closing lines git writes around a conflict (`=======` alone also underlines
# Markdown and reST headings, so it is not a reliable sign).
_CONFLICT_MARKER = re.compile(rb'^(?:<{7}|>{7})(?: |\r?$)', re.MULTILINE)


class WorkspaceError(RuntimeError):
    pass


# What git reports of a push that failed for a transient error of the remote (#645): a 5xx answer
# of the server or a connection lost on the way, the push's own time limit included. A push is
# repeated as it is, so trying once more cannot do what the first attempt would not have done.
_PUSH_TRANSIENT = re.compile(
    r'Internal Server Error|Bad Gateway|Service Unavailable|Gateway Time-?out'
    r'|(?:HTTP|returned error:)\s*50[0-4]\b'
    r'|Could not resolve host|Connection (?:reset|refused|timed out)|Failed to connect'
    r'|Operation timed out|remote end hung up unexpectedly|early EOF|unexpected disconnect'
    r'|Empty reply from server|Recv failure|SSL_ERROR_SYSCALL|GnuTLS recv error'
    r'|TLS connection was non-properly terminated|timed out after \d+s', re.IGNORECASE)
# A refusal of the remote, which a second attempt would only repeat: it prevails over any sign of
# a transient error in the same message. Permissions, the rules of the branch, a conflict.
_PUSH_REFUSED = re.compile(
    r'\bGH0\d\d\b|protected branch|rule violation|hook declined|denied|\b40[13]\b'
    r'|non-fast-forward|fetch first|stale info|refusing to push', re.IGNORECASE)


class TransientPushError(WorkspaceError):
    """A push that failed for a transient error of the remote (#645, ``transient_push``): a 5xx
    answer or a connection lost. Any other failure of a push stays a ``WorkspaceError``."""


class PrepareFailed(WorkspaceError):
    """The step that prepares a worktree (``[prepare] cmd``, #696) failed or timed out: a
    critical escalation to the operator (``prepare_failed``). ``tail``: the end of its output,
    for the escalation, which cleans it as it cleans the output of the tests."""

    def __init__(self, message, tail=''):
        super().__init__(message)
        self.tail = tail


def transient_push(message):
    """Whether ``message``, the error of a failed ``git push``, is a transient error of the remote
    (#645): a 5xx answer (``Internal Server Error``, ``Bad Gateway``, ``Service Unavailable``) or
    a connection lost, and no refusal (permissions, the rules of the branch, a conflict)."""
    text = str(message or '')
    return bool(_PUSH_TRANSIENT.search(text)) and not _PUSH_REFUSED.search(text)


def partial_text(data):
    """Output that ``communicate()`` collected before a timeout: bytes even in text mode."""
    if isinstance(data, bytes):
        return data.decode('utf-8', 'replace').replace('\r\n', '\n')
    return data or ''


def _kill_group(proc, kill=None):
    """Kill the process group of ``proc`` and return what it wrote. ``kill(pgid)``, if given,
    kills it first as another user (``agentuser.AgentUser.kill_group``, #415): the processes of
    a command started as the agent user are not this user's to signal."""
    if kill is not None:
        kill(proc.pid)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        return proc.communicate(timeout=5)
    except subprocess.TimeoutExpired as exc:
        # A descendant that left the group (its own session) still holds the pipes: stop
        # waiting, but keep what was read so far (issue #215).
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe:
                pipe.close()
        proc.wait()
        return partial_text(exc.output), partial_text(exc.stderr)


class Cancelled(Exception):
    """The command of ``run_in_group`` was stopped because its ``cancel`` event was set (#521);
    ``partial_output`` is what it wrote until then, ``(stdout, stderr)``."""

    def __init__(self, partial_output):
        super().__init__('cancelled')
        self.partial_output = partial_output


# How often ``run_in_group`` checks its ``cancel`` event, in seconds.
CANCEL_POLL_SECONDS = 1.0


def run_in_group(argv, *, timeout=None, input=None, watch=None, watch_interval=30.0, kill=None,
                 cancel=None, **kwargs):
    """Like ``subprocess.run(..., capture_output=True, text=True)``, but the command gets its own
    process group, and a timeout kills the whole group: the test runner's workers, or the
    background commands an agent CLI started, do not outlive it. ``kill(pgid)``, if given,
    kills the group too, as another user: the agent user's processes (#415).

    ``watch(pid)``, if given, is called every ``watch_interval`` seconds while the command runs;
    an exception it raises kills the whole group and propagates, with what the command wrote
    until then in its ``partial_output`` attribute, ``(stdout, stderr)``.

    ``cancel`` (a ``threading.Event``), if given, is checked every ``CANCEL_POLL_SECONDS``
    while the command runs (#521): once set, the whole group is killed and ``Cancelled`` is
    raised, with what the command wrote until then."""
    kwargs.pop('capture_output', None)
    kwargs.pop('text', None)
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else None,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True, **kwargs)
    deadline = None if timeout is None else time.monotonic() + timeout
    watched = None if watch is None else time.monotonic() + watch_interval
    while True:
        now = time.monotonic()
        wait = None if deadline is None else max(0.0, deadline - now)
        if watch is not None:
            until = max(0.0, watched - now)
            wait = until if wait is None else min(wait, until)
        if cancel is not None:
            wait = CANCEL_POLL_SECONDS if wait is None else min(wait, CANCEL_POLL_SECONDS)
        try:
            # Retrying communicate() after its timeout loses no output and keeps writing the
            # input given to the first call; passing input again raises ValueError.
            pending, input = input, None
            out, err = proc.communicate(input=pending, timeout=wait)
            break
        except subprocess.TimeoutExpired:
            if ((watch is None and cancel is None)
                    or (deadline is not None and time.monotonic() >= deadline)):
                out, err = _kill_group(proc, kill)
                raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err) from None
        if cancel is not None and cancel.is_set():
            raise Cancelled(_kill_group(proc, kill))
        if watch is None or time.monotonic() < watched:
            continue
        watched = time.monotonic() + watch_interval
        try:
            watch(proc.pid)
        except BaseException as exc:
            partial = _kill_group(proc, kill)
            try:
                exc.partial_output = partial
            except AttributeError:
                pass
            raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


# A new file beyond this size is shown in a pending diff by its size and hash only (#141).
MAX_NEW_FILE = 256 * 1024


def _new_file_diff(root, name):
    """New file ``name`` of worktree ``root``, not tracked, as a diff that adds it (#141): its
    lines when it is UTF-8 text up to ``MAX_NEW_FILE`` bytes, otherwise its size only; the
    target of a symbolic link. The sha256 of the exact bytes is always in the header, so two
    contents differ in the text, and so in its hash, even where the lines look alike. Lines are
    split at LF only, as git does: CR, U+2028 and every other separator stay in the line where
    the file has them. Deterministic: the same content gives the same text."""
    path = root / name
    head = f'diff --git a/{name} b/{name}\nnew file, not tracked'
    tail = f'\n--- /dev/null\n+++ b/{name}\n'
    try:
        if path.is_symlink():
            target = os.readlink(path)
            digest = hashlib.sha256(os.fsencode(target)).hexdigest()
            return (head + f', symbolic link, sha256 {digest}' + tail
                    + f'@@ -0,0 +1 @@\n+{target}\n\\ No newline at end of file\n')
        data = path.read_bytes()
    except OSError as exc:
        return head + tail + f'(not readable: {type(exc).__name__})\n'
    head += f', {len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()}' + tail
    try:
        text = data.decode('utf-8') if b'\0' not in data and len(data) <= MAX_NEW_FILE else None
    except UnicodeDecodeError:
        text = None
    if text is None:
        return head + f'(binary or larger than {MAX_NEW_FILE} bytes)\n'
    if not text:
        return head + '(empty)\n'
    lines = text.split('\n')
    last = lines.pop()  # '' when the file ends with LF
    body = ''.join(f'+{line}\n' for line in lines)
    if last:
        lines.append(last)
        body += f'+{last}\n\\ No newline at end of file\n'
    return head + f'@@ -0,0 +1,{len(lines)} @@\n' + body


def git_config_env(pairs):
    """``GIT_CONFIG_*`` variables that apply ``pairs`` on top of every config file, in order
    (an empty value resets multi-valued keys such as ``credential.helper``)."""
    env = {'GIT_CONFIG_COUNT': str(len(pairs))}
    for i, (key, value) in enumerate(pairs):
        env[f'GIT_CONFIG_KEY_{i}'] = key
        env[f'GIT_CONFIG_VALUE_{i}'] = value
    return env


# Applied to every git command the dispatcher runs in a worktree (#415), over every config file:
# no hook and no fsmonitor program runs, whatever a config says.
WORKTREE_GIT_CONFIG = (('core.hooksPath', os.devnull), ('core.fsmonitor', 'false'))

# In the environment of the local tests and of the agents' turns (#660): Python writes no
# bytecode, so a project that tracks its __pycache__/ does not see it rewritten in the commit. A
# project that is not Python ignores it.
NO_BYTECODE = {'PYTHONDONTWRITEBYTECODE': '1'}

# The variable that gives the step that prepares a worktree its cache (#696, ``run_prepare``).
PREPARE_CACHE_ENV = 'DEVLOOP_PREPARE_CACHE'


def _identity_env(author):
    """Variables that make ``author`` (``{name, email}``) both author and committer of one git
    command, or None for the ambient identity."""
    if not author:
        return None
    return {'GIT_AUTHOR_NAME': author['name'], 'GIT_AUTHOR_EMAIL': author['email'],
            'GIT_COMMITTER_NAME': author['name'], 'GIT_COMMITTER_EMAIL': author['email']}


# git commands that update state shared by every worktree of the repository (remote-tracking
# refs, the list of worktrees): parallel actions (#176) run them one at a time.
_SHARED_GIT = ('fetch', 'worktree')
# git commands that only read the repository: the only ones a read-only workspace runs (#260),
# ``merge-tree`` included because the objects it writes go out of the repository.
_READ_GIT = ('rev-parse', 'rev-list', 'diff', 'diff-tree', 'cat-file', 'merge-base', 'log',
             'ls-files', 'status', 'merge-tree')


class Workspace:
    """``private_tmp``: each run of the test command gets a temporary directory of its own
    (``TMPDIR``), removed afterwards, so that test runs of parallel actions (#176) do not share
    one.

    ``agent_user`` (``agentuser.AgentUser``, #415), if given, runs the test command as that user,
    and kills it as that user on a timeout; its private temporary directories are then new
    directories of ``<state_dir>/agent-shared/tmp`` (``agentuser.SharedDir.new_dir``), which
    that user can write.

    The git commands run in a worktree (``cwd`` in :attr:`root`) never take the repository
    from the worktree itself (#415): what an agent can change there, the ``.git`` file of the
    worktree included, could point git to a configuration or a hook of its own, which the
    dispatcher's user would then run. ``GIT_DIR``, ``GIT_COMMON_DIR`` and ``GIT_WORK_TREE`` are
    set to the metadata of the repository (:meth:`common_dir`) the dispatcher created with
    ``git worktree add``, and ``WORKTREE_GIT_CONFIG`` turns hooks and fsmonitor off."""

    def __init__(self, repo_root, state_dir, base_branch, run=subprocess.run, test_env=None,
                 private_tmp=False, agent_user=None):
        self.repo_root = Path(repo_root)
        self.root = self.repo_root / state_dir / 'worktrees'
        self.base_branch = base_branch
        self._run = run
        self._test_env = test_env
        self.private_tmp = private_tmp
        self.agent_user = agent_user
        self._shared_dir = SharedDir(self.root.parent / SHARED_DIR)
        self._shared = threading.RLock()
        self._read_only = None
        self._common = None
        # The identities of the failed tests of the latest run in each worktree (#459,
        # ``failed_cases``), read from its whole output.
        self._failed = {}
        # The fingerprint with which each worktree was last prepared (#696, ``prepare_stamp``),
        # forgotten when the worktree is created again or removed.
        self._prepared = {}

    @property
    def test_env(self):
        """The environment of the test command and of the agent turns, before the
        ``safe.directory`` of a worktree (#500); None: the dispatcher's own."""
        return self._test_env

    @property
    def shared_lock(self):
        """The lock held by the git commands of ``_SHARED_GIT``; the fetch of a pin that runs
        while actions run holds it too (#301)."""
        return self._shared

    def read_only(self, objects):
        """From now on write nothing to the repository (#260), for ``tick --dry-run``, which
        runs without the dispatcher's lock while a dispatcher may run. ``fetch`` is skipped, so
        the remote branches are those last fetched; the objects git writes (``merge-tree``) go to
        ``objects``, a directory of the caller, with those of the repository as alternates;
        optional locks are off (``status`` and ``diff`` do not refresh the index); any other
        command that writes raises WorkspaceError."""
        common = Path(self._git('rev-parse', '--git-common-dir').strip())
        self._read_only = {'GIT_OBJECT_DIRECTORY': str(objects),
                           'GIT_ALTERNATE_OBJECT_DIRECTORIES':
                               str((self.repo_root / common).resolve() / 'objects'),
                           'GIT_OPTIONAL_LOCKS': '0'}

    def common_dir(self):
        """The git directory shared by the worktrees of the repository: ``<repo_root>/.git``,
        or, when the repository root is itself a linked worktree, the common directory its
        ``.git`` file leads to. The repository root is the operator's, never an agent's."""
        if self._common is None:
            dot = self.repo_root / '.git'
            common = dot
            try:
                if dot.is_file():
                    text = dot.read_text(encoding='utf-8').strip()
                    if text.startswith('gitdir:'):
                        own = self.repo_root / text[len('gitdir:'):].strip()
                        try:
                            common = own / (own / 'commondir').read_text(encoding='utf-8').strip()
                        except FileNotFoundError:
                            common = own
            except OSError:
                pass
            self._common = Path(os.path.realpath(common))
        return self._common

    def _worktree_git_dir(self, common, work):
        """The metadata of the worktree ``work`` in ``<common>/worktrees``: the entry whose
        ``gitdir`` file, written by ``git worktree add``, names ``<work>/.git``; by default the
        entry named as the worktree, as git names it. Raises WorkspaceError when its
        ``commondir`` leads elsewhere than ``common``: git reads the refs from there whatever
        ``GIT_COMMON_DIR`` says, and the agent user cannot write these metadata
        (``agentuser.check``), so that only an alteration can do it."""
        want = os.path.normpath(work / '.git')
        meta = common / 'worktrees'
        try:
            entries = sorted(meta.iterdir())
        except OSError:
            entries = []
        found = meta / work.name
        for entry in entries:
            try:
                named = (entry / 'gitdir').read_text(encoding='utf-8').strip()
            except OSError:
                continue
            if named and os.path.normpath(named) == want:
                found = entry
                break
        try:
            target = (found / 'commondir').read_text(encoding='utf-8').strip()
        except FileNotFoundError:
            return found  # no such worktree: git fails on its own
        except OSError as exc:
            raise WorkspaceError(f'the git metadata of {work}: {exc}') from exc
        if Path(os.path.realpath(found / target)) != common:
            raise WorkspaceError(f'the git metadata of {work} were altered: {found}/commondir '
                                 f'leads to {target!r}, not to {common}; refusing to run git')
        return found

    def _worktree_env(self, cwd):
        """The git variables of a command run in ``cwd`` (#415): None outside :attr:`root`,
        otherwise ``GIT_DIR``, ``GIT_COMMON_DIR`` and ``GIT_WORK_TREE`` of the worktree that
        holds ``cwd``. The worktree is named as a child of the real path of :attr:`root`: a
        link an agent left in its place is not followed."""
        try:
            rel = Path(os.path.abspath(cwd)).relative_to(os.path.abspath(self.root))
        except ValueError:
            return None
        if not rel.parts:
            return None
        work = Path(os.path.realpath(self.root)) / rel.parts[0]
        common = self.common_dir()
        return {'GIT_DIR': str(self._worktree_git_dir(common, work)),
                'GIT_COMMON_DIR': str(common), 'GIT_WORK_TREE': str(work)}

    def _git(self, *args, cwd=None, check=True, stdin=None, env=None, unset=(), binary=False):
        return self._git_run(*args, cwd=cwd, check=check, stdin=stdin, env=env,
                             unset=unset, binary=binary).stdout

    def _git_run(self, *args, cwd=None, check=True, stdin=None, env=None, unset=(),
                 binary=False):
        """The completed git command, stdout and stderr included. ``binary``: ``stdin`` and
        stdout are bytes, neither decoded nor with their line ends translated (a patch, #374);
        stderr is decoded all the same, for the messages."""
        if self._read_only is not None:
            if args[0] == 'fetch':
                return subprocess.CompletedProcess(['git', *args], 0, '', '')
            if args[0] not in _READ_GIT:
                raise WorkspaceError(f'git {args[0]}: not run, the workspace is read-only')
            env = dict(env or {}, **self._read_only)
        trusted = self._worktree_env(cwd) if cwd is not None else None
        if trusted is not None:
            env = dict(env or {}, **trusted)
        kwargs = {}
        if env or unset:
            inherited = {k: v for k, v in os.environ.items() if k not in unset}
            kwargs['env'] = dict(inherited, **(env or {}))
            if trusted is not None:
                kwargs['env'] = with_git_config(kwargs['env'], WORKTREE_GIT_CONFIG)
        try:
            with self._shared if args[:1] and args[0] in _SHARED_GIT else nullcontext():
                if binary:
                    done = self._run(['git', *args], cwd=cwd or self.repo_root, input=stdin,
                                     capture_output=True, timeout=600, **kwargs)
                    done.stderr = done.stderr.decode('utf-8', 'replace')
                else:
                    done = self._run(['git', *args], cwd=cwd or self.repo_root, input=stdin,
                                     capture_output=True, text=True, timeout=600, **kwargs)
        except subprocess.TimeoutExpired as exc:
            raise WorkspaceError(f'git {" ".join(args[:2])}: timed out after {exc.timeout}s') from exc
        except OSError as exc:
            raise WorkspaceError(f'git could not start: {exc}') from exc
        if check and done.returncode != 0:
            raise WorkspaceError(f'git {" ".join(args[:2])}: {done.stderr.strip()[:500]}')
        return done

    def path(self, name):
        return self.root / name

    def _rmtree(self, path, ignore_errors=False):
        """Remove ``path``. With an agent user (#415), what it created with a restrictive mode
        is opened to the group class first (``AgentUser.release``) when the removal fails."""
        try:
            shutil.rmtree(path)
            return
        except OSError:
            if self.agent_user is None and not ignore_errors:
                raise
        if self.agent_user is not None:
            self.agent_user.release(path)
        shutil.rmtree(path, ignore_errors=ignore_errors)

    def _agent_tmp(self):
        """A new private temporary directory the agent user can write (#415), in
        ``<state_dir>/agent-shared/tmp`` (``SharedDir.new_dir``: no link followed, mode
        ``0770``, so that the named entries of its default ACL apply)."""
        try:
            return str(self._shared_dir.new_dir(TMP, 'devloop-tests-'))
        except OSError as exc:
            raise WorkspaceError(f'the temporary directory of the tests in '
                                 f'{self._shared_dir.path}: {exc}') from exc

    def _remove_agent_tmp(self, tmp):
        """Remove the directory of :meth:`_agent_tmp`, without following links; what the agent
        user created with a restrictive mode is opened first. Never raises."""
        name = Path(tmp).name
        try:
            self._shared_dir.remove_dir(TMP, name)
        except OSError:
            self.agent_user.release(tmp)
            try:
                self._shared_dir.remove_dir(TMP, name)
            except OSError:
                pass

    def _fresh(self, path):
        self._prepared.pop(str(path), None)
        if path.exists():
            self._git('worktree', 'remove', '--force', str(path), check=False)
            if path.exists():
                self._rmtree(path)
        self._git('worktree', 'prune')
        path.parent.mkdir(parents=True, exist_ok=True)

    def new_branch(self, name, branch):
        """A worktree on ``branch`` reset to the latest remote base branch."""
        self._git('fetch', 'origin', self.base_branch)
        path = self.path(name)
        self._fresh(path)
        self._git('worktree', 'add', '--force', '-B', branch, str(path),
                  f'origin/{self.base_branch}')
        return path

    def existing_branch(self, name, branch):
        """A worktree on ``branch`` at its pushed head."""
        self._git('fetch', 'origin', self.base_branch,
                  f'+refs/heads/{branch}:refs/remotes/origin/{branch}')
        path = self.path(name)
        self._fresh(path)
        self._git('worktree', 'add', '--force', '-B', branch, str(path), f'origin/{branch}')
        return path

    def base_copy(self, name):
        """A disposable worktree detached at the latest remote base branch, for a read-only
        turn such as the readiness check; removed with :meth:`remove`."""
        self._git('fetch', 'origin', self.base_branch)
        path = self.path(name)
        self._fresh(path)
        self._git('worktree', 'add', '--force', '--detach', str(path),
                  f'origin/{self.base_branch}')
        return path

    def base_head(self):
        """The commit of the latest remote base branch: the base an issue's step starts from,
        part of the key of the supervisor's retries (#182)."""
        self._git('fetch', 'origin', self.base_branch)
        return self._git('rev-parse', f'origin/{self.base_branch}').strip()

    def commit_copy(self, name, commit, branch=None):
        """A disposable worktree detached at ``commit``, for a read-only turn of the conclave
        (#141); removed with :meth:`remove`. ``branch``: the PR branch to fetch with the base,
        where the commit is."""
        refs = [f'+refs/heads/{branch}:refs/remotes/origin/{branch}'] if branch else []
        self._git('fetch', 'origin', self.base_branch, *refs)
        path = self.path(name)
        self._fresh(path)
        self._git('worktree', 'add', '--force', '--detach', str(path), commit)
        return path

    def pending_diff(self, name, commit):
        """What worktree ``name`` holds beyond ``commit`` (#141): ``git diff`` of the tracked
        files against it, then each new file git does not ignore, in name order, as a diff
        that adds it with its content (``_new_file_diff``): the members cannot read it in the
        checkout of the commit, and a change to it changes the diff and its hash. ``''`` when
        the worktree does not exist; nothing is written to it."""
        path = self.path(name)
        if not path.is_dir():
            return ''
        diff = self._git('diff', '--no-color', commit, cwd=path)
        new = [n for n in self._git('ls-files', '--others', '--exclude-standard', '-z',
                                    cwd=path).split('\0') if n]
        for new_name in sorted(new):
            if diff and not diff.endswith('\n'):
                diff += '\n'
            diff += _new_file_diff(path, new_name)
        return diff

    def save_evidence(self, name, text):
        """Write ``text`` as file ``name`` of the dispatcher's logs (``<state_dir>/logs``, next to
        the worktrees), and return its path: the uncommitted changes of an agent that stopped,
        kept as evidence when a conclave restarts its step from the published commit (#142).
        Bytes are written as they are (a patch, #374), a string in UTF-8."""
        logs = self.root.parent / 'logs'
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / name
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding='utf-8')
        return path

    def keep_work(self, path, commit, name):
        """Save the uncommitted work a writing turn left in ``path`` when it timed out (#374):
        everything ``git add -A`` would stage, new files included, as a patch against ``commit``
        that ``git apply`` takes (binary files included), written as evidence ``name``
        (:meth:`save_evidence`). Nothing is committed, and the index of ``path`` is untouched.
        The patch stays bytes from here to :meth:`apply_work`, never decoded: line ends (CRLF)
        and text that is not UTF-8 come back as they were. Returns ``(file, stats)``, ``stats``
        the ``(path, added, removed)`` of each file (``None`` counts for a binary one), or None
        when the worktree holds nothing beyond ``commit``."""
        tree = self.tree(path)
        if tree == self.commit_tree(path, commit):
            return None
        patch = self._git('diff', '--binary', '--full-index', '--no-color', '--no-renames',
                          '--no-ext-diff', '--no-textconv', '--src-prefix=a/', '--dst-prefix=b/',
                          commit, tree, cwd=path, binary=True)
        return self.save_evidence(name, patch), self.patch_stats(path, patch)

    def patch_stats(self, path, patch):
        """The ``(path, added, removed)`` of each file of ``patch`` (bytes), as ``git apply
        --numstat`` reads it in ``path`` without applying it; ``None`` counts for a binary
        file. A path that is not UTF-8 is shown with replacement characters."""
        stats = []
        out = self._git('apply', '--numstat', '-z', '-', cwd=path, stdin=patch, binary=True)
        for entry in out.decode('utf-8', 'replace').split('\0'):
            added, _, rest = entry.partition('\t')
            removed, _, file = rest.partition('\t')
            if file:
                stats.append((file, None if added == '-' else int(added),
                              None if removed == '-' else int(removed)))
        return stats

    def take_work(self, name):
        """The patch :meth:`keep_work` saved as ``name`` for the retry of its step (#374), as
        bytes, or None. It is taken once: the file is renamed ``<name>.used-<time>``, so that a
        later run of the step does not apply it again, and stays in the logs as evidence."""
        path = self.root.parent / 'logs' / name
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        path.rename(path.with_name(f'{name}.used-{time.strftime("%Y%m%dT%H%M%S")}'))
        return data

    def apply_work(self, path, patch):
        """``(applied, error)``: apply ``patch`` (:meth:`keep_work`, bytes) to the worktree
        ``path``, without staging or committing anything. ``git apply`` applies all of it or
        nothing. A patch that does not apply as it is, because the base moved since it was
        saved (#475), is merged three ways (``git apply --3way``) in a separate index, from the
        blobs it records: when the merge is clean its result is applied to ``path``, otherwise
        nothing is, and the error is the one of the plain ``git apply``."""
        done = self._git_run('apply', '--whitespace=nowarn', '-', cwd=path, stdin=patch,
                             check=False, binary=True)
        out = done.stdout.decode('utf-8', 'replace')
        error = (done.stderr.strip() or out.strip())[:500]
        if done.returncode == 0:
            return True, error
        merged = self._three_way(path, patch)
        if merged is None:
            return False, error
        again = self._git_run('apply', '--whitespace=nowarn', '-', cwd=path, stdin=merged,
                              check=False, binary=True)
        return (True, '') if again.returncode == 0 else (False, error)

    def _three_way(self, path, patch):
        """``patch`` merged three ways onto the HEAD of ``path`` (#475), as a patch against it
        that ``git apply`` takes, or None when the merge leaves conflicts or fails. The merge
        runs in a temporary index: neither the worktree nor its index is touched."""
        with tempfile.TemporaryDirectory(prefix='devloop-3way-') as tmp:
            env = {'GIT_INDEX_FILE': str(Path(tmp) / 'index')}
            if self._git_run('read-tree', 'HEAD', cwd=path, env=env,
                             check=False).returncode != 0:
                return None
            if self._git_run('apply', '--3way', '--cached', '--whitespace=nowarn', '-',
                             cwd=path, stdin=patch, env=env, check=False,
                             binary=True).returncode != 0:
                return None
            done = self._git_run('diff', '--cached', '--binary', '--full-index', '--no-color',
                                 '--no-renames', '--no-ext-diff', '--no-textconv',
                                 '--src-prefix=a/', '--dst-prefix=b/', 'HEAD', cwd=path,
                                 env=env, check=False, binary=True)
            return done.stdout if done.returncode == 0 and done.stdout else None

    def review_copy(self, path, reviewer=None):
        """``(name, path)`` of a disposable worktree for a reviewer, detached at the HEAD of
        ``path`` and sharing its refs, so that ``origin/<base>`` resolves. What the reviewer and
        its tests write lands in the copy, which is removed with :meth:`remove`, not in
        ``path``. ``reviewer``: the key of the reviewer, for a copy of its own when several
        review the same worktree at once (#176)."""
        name = f'{Path(path).name}-review' + (f'-{reviewer}' if reviewer else '')
        copy = self.path(name)
        self._fresh(copy)
        self._git('worktree', 'add', '--force', '--detach', str(copy), self.head(path))
        return name, copy

    def pristine(self, path, head, outputs=()):
        """Whether ``path`` is still at commit ``head``, with no change and no new file, not
        even an ignored one. ``outputs``: what the step that prepares a worktree produces
        (#696, ``[prepare] outputs``): a new file git ignores under one of them is admitted;
        a tracked file changed, or a new file git does not ignore, under them too, is not."""
        if self.head(path) != head:
            return False
        out = self._git('status', '--porcelain', '-z', '--ignored', '--untracked-files=all',
                        cwd=path)
        return all(entry.startswith('!! ') and prepared_output(entry[3:], outputs)
                   for entry in out.split('\0') if entry)

    def remove(self, name):
        path = self.path(name)
        self._prepared.pop(str(path), None)
        if path.exists():
            self._git('worktree', 'remove', '--force', str(path), check=False)
        self._git('worktree', 'prune', check=False)

    def names(self):
        return sorted(p.name for p in self.root.iterdir()) if self.root.exists() else []

    def head(self, path):
        return self._git('rev-parse', 'HEAD', cwd=path).strip()

    def has_changes(self, path):
        return bool(self._git('status', '--porcelain', cwd=path).strip())

    def diff_stat(self, path):
        return self._git('diff', '--stat', f'origin/{self.base_branch}...HEAD', cwd=path)

    def changed_files(self, path, since=None):
        """Paths added, changed or deleted since commit ``since``, or on the branch with respect
        to the base branch."""
        span = f'{since}..HEAD' if since else f'origin/{self.base_branch}...HEAD'
        # -z: without it git quotes unusual names ("\303\251", tabs, newlines), which would hide
        # them from a prefix check.
        out = self._git('diff', '--name-only', '-z', '--no-renames', span, cwd=path)
        return [name for name in out.split('\0') if name]

    def binary_files(self, path, since):
        """The paths added or changed since commit ``since`` whose content git sees as binary
        (#627): ``-`` for the lines of ``--numstat``. Deleted files are not among them."""
        out = self._git('diff', '--numstat', '-z', '--no-renames', '--diff-filter=d',
                        f'{since}..HEAD', cwd=path)
        found = []
        for line in out.split('\0'):
            added, _, rest = line.partition('\t')
            deleted, _, name = rest.partition('\t')
            if name and added == '-' and deleted == '-':
                found.append(name)
        return found

    def worktree_changes(self, path):
        """Every path in which the content of ``path`` now differs from its merge base with
        ``origin/<base>`` (#169): the branch's commits, the index, unstaged changes and new files
        git does not ignore, deleted files included."""
        base = self._git('merge-base', 'HEAD', f'origin/{self.base_branch}', cwd=path).strip()
        tracked = self._git('diff', '--name-only', '-z', '--no-renames', base, cwd=path)
        new = self._git('ls-files', '--others', '--exclude-standard', '-z', cwd=path)
        return sorted({name for name in (tracked + '\0' + new).split('\0') if name})

    def missing_files(self, path, names):
        """The ``names`` (relative paths) that are not a regular file in the worktree ``path``:
        absent, a directory or a symlink (#210)."""
        root = Path(path)
        return [name for name in names
                if (root / name).is_symlink() or not (root / name).is_file()]

    def test_modules(self, path):
        """The names of the ``tests/test_*.py`` files in ``path``."""
        tests = Path(path) / 'tests'
        return sorted(p.name for p in tests.glob('test_*.py') if p.is_file())

    def review_diff(self, branch):
        """``(base, head, changes)`` for the review policy: the latest base branch commit, the
        pushed head of ``branch``, and every path of the branch's full diff against the base
        (from their merge base, as GitHub shows it).

        ``changes`` holds one ``{path, links}`` per path: added, modified and deleted files,
        and both the old and the new path of a rename (renames are not detected). ``links``
        holds the target of the path on each side of the diff where it is a symlink."""
        self._git('fetch', 'origin', self.base_branch,
                  f'+refs/heads/{branch}:refs/remotes/origin/{branch}')
        base = self._git('rev-parse', f'origin/{self.base_branch}^{{commit}}').strip()
        head = self._git('rev-parse', f'origin/{branch}^{{commit}}').strip()
        out = self._git('diff', '--raw', '-z', '--no-renames', '--no-abbrev', '--no-ext-diff',
                        f'{base}...{head}')
        fields = out.split('\0')
        changes, i = [], 0
        while i < len(fields):
            meta = fields[i]
            if not meta.startswith(':'):
                i += 1
                continue
            old_mode, new_mode, old_sha, new_sha, status = meta[1:].split()[:5]
            # Renames and copies (not requested, but possible) carry two paths.
            count = 2 if status[:1] in ('R', 'C') else 1
            paths = [p for p in fields[i + 1:i + 1 + count] if p]
            i += 1 + count
            links = [self._git('cat-file', 'blob', sha)
                     for mode, sha in ((old_mode, old_sha), (new_mode, new_sha))
                     if mode == '120000' and sha.strip('0')]
            changes += [{'path': p, 'links': links} for p in paths]
        return base, head, changes

    def diff_blocks(self, base, head, prefix):
        """The changes under ``prefix`` of the diff from ``base`` to ``head`` (from their merge
        base, as ``review_diff``), for a delegated merge (#486): one ``{path, blocks, removed,
        binary, other}`` per file, ``blocks`` the text of each run of consecutive added lines
        (a hunk of ``--unified=0`` with no line removed), ``removed`` whether a line was removed
        or changed, or the file deleted, ``binary`` a file git shows as binary, ``other`` a
        change of mode or a path git quotes. Raises WorkspaceError when git cannot tell."""
        out = self._git('diff', '--unified=0', '--no-renames', '--no-ext-diff', '--no-color',
                        '--no-textconv', f'{base}...{head}', '--', prefix)
        files, entry, in_hunk = [], None, False
        for line in out.splitlines():
            if line.startswith('diff --git '):
                rest = line[len('diff --git '):]
                entry = {'path': rest, 'blocks': [], 'removed': False, 'binary': False,
                         'other': True}
                if rest.startswith('a/') and len(rest) % 2 == 1:
                    half = (len(rest) - 1) // 2  # 'a/<path> b/<path>', the same path twice
                    old, new = rest[2:half], rest[half + 3:]
                    if old == new and rest[half:half + 3] == ' b/':
                        entry.update(path=new, other=False)
                files.append(entry)
                in_hunk = False
            elif entry is None:
                continue
            elif line.startswith('@@'):
                in_hunk = True
                entry['blocks'].append('')
            elif in_hunk and line.startswith('+'):
                entry['blocks'][-1] += line[1:] + '\n'
            elif in_hunk and line.startswith('-'):
                entry['removed'] = True
            elif in_hunk:
                continue  # '\ No newline at end of file'
            elif line.startswith('deleted file mode'):
                entry['removed'] = True
            elif line.startswith(('old mode', 'new mode')) or (
                    line.startswith('new file mode') and line.split()[-1] != '100644'):
                entry['other'] = True  # a mode changed, or a new file that is no plain file
            elif line.startswith(('Binary files', 'GIT binary patch')):
                entry['binary'] = True
        for entry in files:
            entry['blocks'] = [b for b in entry['blocks'] if b]
        return files

    def base_paths(self, old, new):
        """Every path that a commit of the base branch after ``old``, up to ``new``, changed
        (#160): the union over each commit of the first-parent chain of ``new`` back to ``old``
        on its own, so a change later reverted still counts, with both the old and the new path
        of a rename and a merge compared with its first parent, i.e. the net diff it brought to
        the base. The commits of a merged branch are not walked (#179): those that merged the
        base into it would bring back changes the old base already had. Raises WorkspaceError
        when it cannot be listed, e.g. ``old`` is not an ancestor of ``new`` (the base branch was
        rewritten) or a commit is missing."""
        if self._git_run('merge-base', '--is-ancestor', old, new, check=False).returncode:
            raise WorkspaceError(f'{old[:12]} is not an ancestor of {new[:12]}')
        touched = set()
        for line in self._git('rev-list', '--first-parent', '--parents',
                              f'{old}..{new}').splitlines():
            commit, *parents = line.split()
            # A root commit (unrelated history merged in) adds every path it has.
            span = parents[:1] + [commit] if parents else ['--root', '--no-commit-id', commit]
            out = self._git('diff-tree', '-r', '--name-only', '-z', '--no-renames', *span)
            touched.update(name for name in out.split('\0') if name)
        return sorted(touched)

    def first_parents(self, commit, limit):
        """``(sha, subject)`` of the first-parent chain of ``commit``, ``commit`` first, at most
        ``limit`` commits (#459): on the base branch, one per merge or direct push."""
        out = self._git('log', '--first-parent', f'--max-count={int(limit)}', '--format=%H%x00%s',
                        commit)
        return [tuple(line.split('\0', 1)) for line in out.splitlines() if '\0' in line]

    def merge_check(self, base, head):
        """``(clean, files)``: whether ``head`` merges into ``base`` without conflicts, computed
        by git on these two exact commits without touching any worktree (#160), and the files
        git reports in conflict. Disjoint paths do not imply a clean merge: a directory the base
        renamed, where the PR adds a file, is a conflict on the file's location. Raises
        WorkspaceError when git cannot tell (a missing commit, a git without ``merge-tree
        --write-tree``)."""
        _, clean, files = self._merge_tree(base, head)
        return clean, files

    def _merge_tree(self, base, head):
        """``(tree, clean, files)`` of :meth:`merge_check`, with the tree git wrote."""
        for commit in (base, head):
            if self._git_run('rev-parse', '-q', '--verify', f'{commit}^{{commit}}',
                             check=False).returncode:
                raise WorkspaceError(f'{commit[:12]} is not a known commit')
        done = self._git_run('merge-tree', '--write-tree', '--no-messages', '--name-only', '-z',
                             base, head, check=False)
        # The tree written first, then the files in conflict. Exit status 1 means conflicts
        # only with a tree written: git also exits 1 on some errors.
        tree, *rest = done.stdout.split('\0')
        if done.returncode not in (0, 1) or not re.fullmatch(r'[0-9a-f]{40,64}', tree):
            raise WorkspaceError(f'git merge-tree: {done.stderr.strip()[:500]}')
        return tree, done.returncode == 0, sorted({name for name in rest if name})

    def clean_resync(self, branch, head, approved, limit=10):
        """Whether ``head`` differs from ``approved`` only by merges of the base branch that git
        concludes without conflicts (#235): walking back from ``head``, every commit up to
        ``approved`` (at most ``limit``) has exactly two parents, the second a commit of
        ``origin/<base>``, and the very tree ``git merge-tree --write-tree`` writes, without
        conflicts, for its two parents. A merge whose conflicts someone resolved, or any other
        commit, is not. Fetches ``branch`` and the base first. Raises WorkspaceError when git
        cannot tell."""
        return self.resync_merges(branch, head, approved, limit) == []

    def resync_merges(self, branch, head, approved, limit=10):
        """The merges between ``approved`` and ``head`` whose tree git does not write by itself
        (conflicts someone resolved, #458), newest first, when ``head`` differs from
        ``approved`` only by merges of the base branch: walking back from ``head``, every
        commit up to ``approved`` (at most ``limit``) has exactly two parents, the second a
        commit of ``origin/<base>``. ``[]``: every merge is the one git concludes without
        conflicts (``clean_resync``). None when ``head`` is ``approved``, another commit is in
        between, or ``approved`` is not reached. Fetches ``branch`` and the base first. Raises
        WorkspaceError when git cannot tell."""
        self._git('fetch', 'origin', self.base_branch,
                  f'+refs/heads/{branch}:refs/remotes/origin/{branch}')
        base = f'origin/{self.base_branch}'
        commit, resolved = head, []
        for _ in range(limit):
            if commit == approved:
                return resolved if commit != head else None
            line = self._git('rev-list', '--parents', '-n1', commit).split()
            if len(line) != 3:
                return None
            first, merged = line[1:]
            if self._git_run('merge-base', '--is-ancestor', merged, base,
                             check=False).returncode:
                return None
            tree, clean, _ = self._merge_tree(first, merged)
            if not clean or tree != self._git('rev-parse', f'{commit}^{{tree}}').strip():
                resolved.append(commit)
            commit = first
        return None

    def contains_base(self, path):
        """Whether the branch in ``path`` already contains ``origin/<base>`` as last fetched."""
        return self._git('rev-list', '--count', f'HEAD..origin/{self.base_branch}',
                         cwd=path).strip() == '0'

    def start_merge(self, path, author=None):
        """Merge ``origin/<base>`` into the branch in ``path`` without committing.

        Returns None when the branch already contains the base (nothing to merge), otherwise
        the files left in conflict (possibly none). The merge stays in progress, with
        ``MERGE_HEAD`` set, so that :meth:`commit_all` concludes it with a merge commit.
        ``author`` (``{name, email}``) is the identity git requires even to start a merge, as
        in :meth:`commit_all`."""
        base = f'origin/{self.base_branch}'
        if self.contains_base(path):
            return None
        target = self._git('rev-parse', base, cwd=path).strip()
        # Exit status 1 means conflicts: the outcome is read from MERGE_HEAD and the index.
        done = self._git_run('merge', '--no-commit', '--no-ff', target, cwd=path, check=False,
                             env=_identity_env(author))
        if self.merge_head(path) != target:
            detail = (done.stderr.strip() or done.stdout.strip())[:500]
            raise WorkspaceError(f'git merge {base}: the merge did not start'
                                 + (f': {detail}' if detail else ''))
        return self._unmerged(path)

    def _unmerged(self, path):
        """The paths of the index of ``path`` still unmerged (in conflict)."""
        out = self._git('diff', '--name-only', '-z', '--diff-filter=U', cwd=path)
        return sorted({name for name in out.split('\0') if name})

    def merge_head(self, path):
        """The commit being merged into ``path``, or None when no merge is in progress."""
        return self._git('rev-parse', '-q', '--verify', 'MERGE_HEAD', cwd=path,
                         check=False).strip() or None

    def stage_merge(self, path):
        """Register in the index of ``path`` the resolution the agent left in the worktree,
        when a merge is in progress (#662): everything ``git add -A`` stages, so that a file the
        agent deleted leaves the index. A file the local tests create again afterwards is then
        an untracked file, which ``.gitignore`` excludes from the commit, and no longer an
        unmerged path that ``git add -A`` would put back. Returns None without a merge in
        progress, otherwise the paths still unmerged after it."""
        if self.merge_head(path) is None:
            return None
        self._git('add', '-A', cwd=path)
        return self._unmerged(path)

    def restored_files(self, path, since):
        """The paths that ``origin/<base>`` deleted in its commits not reachable from ``since``
        and that HEAD of ``path`` still has, while the base does not (#662): files the base
        removed and the branch puts back, for instance a conflict resolved by keeping the
        branch's version of a file the base took out of the index."""
        base = f'origin/{self.base_branch}'
        out = self._git('log', '--diff-filter=D', '--name-only', '-z', '--no-renames',
                        '--format=', f'{since}..{base}', cwd=path)
        deleted = {name.strip('\n') for name in out.split('\0')} - {''}
        if not deleted:
            return []
        out = self._git('diff', '--name-only', '-z', '--no-renames', '--diff-filter=A', base,
                        'HEAD', cwd=path)
        return sorted(deleted & {name for name in out.split('\0') if name})

    def base_commits(self, path, name, limit=5):
        """``<short sha> <subject>`` of the base-branch commits, not yet on the branch, that
        changed ``name``: the ones that brought the conflict."""
        out = self._git('log', f'-n{limit}', '--format=%h %s', f'HEAD..origin/{self.base_branch}',
                        '--', name, cwd=path)
        return [line for line in out.splitlines() if line.strip()]

    def conflict_markers(self, path, names):
        """The files among ``names`` that still contain conflict markers."""
        left = []
        for name in names:
            try:
                data = (Path(path) / name).read_bytes()
            except (FileNotFoundError, IsADirectoryError):
                continue  # resolved by deleting it
            if _CONFLICT_MARKER.search(data):
                left.append(name)
        return left

    def tree(self, path):
        """The tree :meth:`commit_all` would commit now in ``path``: everything ``git add -A``
        stages, written from a copy of the index, so that the real index, a merge in progress
        included, stays as it is. The copy keeps the modification time of the index: git reads
        again the files racily clean against it (#627, R3-1), and a copy with a later time would
        take a same-size change made in the tick of the index for unchanged."""
        index = Path(path) / self._git('rev-parse', '--git-path', 'index', cwd=path).strip()
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / 'index'
            if index.exists():
                shutil.copy2(index, copy)
            env = {'GIT_INDEX_FILE': str(copy)}
            if not index.exists():
                self._git('read-tree', 'HEAD', cwd=path, env=env)
            self._git('add', '-A', cwd=path, env=env)
            return self._git('write-tree', cwd=path, env=env).strip()

    def commit_tree(self, path, commit):
        """The tree of ``commit``."""
        return self._git('rev-parse', f'{commit}^{{tree}}', cwd=path).strip()

    def tree_changes(self, path, old, new):
        """The paths whose content differs between the trees ``old`` and ``new`` (#627): the
        files a test command changed, given :meth:`tree` before and after it."""
        out = self._git('diff-tree', '-r', '--name-only', '-z', '--no-renames', old, new,
                        cwd=path)
        return sorted(name for name in out.split('\0') if name)

    def commit_all(self, path, message, author=None):
        """Commit everything; ``author`` (``{name, email}``) is both author and committer."""
        self._git('add', '-A', cwd=path)
        self._git('commit', '-F', '-', cwd=path, stdin=message, env=_identity_env(author))
        return self.head(path)

    def push(self, path, branch, *, replace=False, auth=None):
        """Push HEAD; ``replace`` overwrites a stale devloop branch left by an earlier attempt.

        With ``auth`` (``{url, token}``) the push goes over HTTPS with that token only, and the
        token travels in the environment of this one command, never in a config file or on the
        command line. No other credential can take part: URL rewrites are refused, only the
        HTTPS transport is allowed, and credential helpers, askpass programs and preconfigured
        authentication headers are disabled.

        A push that fails for a transient error of the remote (``transient_push``, #645) raises
        ``TransientPushError``; any other failure a ``WorkspaceError``."""
        env, unset, remote = None, (), 'origin'
        if auth:
            remote = auth['url']
            self._refuse_rewrites(path, remote)
            basic = base64.b64encode(f'x-access-token:{auth["token"]}'.encode()).decode()
            # Git applies the http.<url>.* entries of the most specific URL that matches and
            # drops the others, whatever their order. Keyed on the exact push URL, the reset and
            # the header win over every config file (a tie goes to the later entry, and these
            # come last); a less specific key would lose to, say, a repository-level
            # http.<this url>.extraHeader. Credential helpers are reset by the plain key: git
            # applies every matching credential.* entry in order.
            env = git_config_env([
                ('credential.helper', ''),
                (f'http.{remote}.extraHeader', ''),
                (f'http.{remote}.extraHeader', f'AUTHORIZATION: basic {basic}')])
            env.update({'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': 'false', 'SSH_ASKPASS': 'false',
                        'GIT_ALLOW_PROTOCOL': 'https', 'GIT_SSH_COMMAND': 'false'})
            # `git -c` settings inherited from a parent git are applied after GIT_CONFIG_*.
            unset = ('GIT_CONFIG_PARAMETERS',)
        try:
            self._git('push', *(['--force'] if replace else []), remote,
                      f'HEAD:refs/heads/{branch}', cwd=path, env=env, unset=unset)
        except WorkspaceError as exc:
            if transient_push(exc):
                raise TransientPushError(str(exc)) from exc
            raise

    def _refuse_rewrites(self, path, url):
        """A ``url.<base>.insteadOf`` or ``pushInsteadOf`` rule matching ``url`` could send the
        push elsewhere, for example over SSH with a personal key."""
        out = self._git('config', '--null', '--get-regexp', r'^url\..*\.(push)?insteadof$',
                        cwd=path, check=False)
        for entry in out.split('\0'):
            key, _, prefix = entry.partition('\n')
            if prefix and url.startswith(prefix):
                raise WorkspaceError(f'git config {key} rewrites {url}: refusing to push with '
                                     'credentials other than the app token')

    def run_tests(self, path, command, timeout):
        """``(passed, output_tail)`` for the project test command in ``path``. With an agent
        user (#415) the command runs as that user, and git trusts ``path``, which belongs to the
        dispatcher's user (#500, ``AgentUser.git_env``). The identities of the failed tests,
        read from the whole output, are kept for ``failed_cases`` (#459). Python writes no
        bytecode (``NO_BYTECODE``, #660)."""
        self._failed[str(path)] = None
        done = self._run_command(path, command, timeout)
        if done is None:
            return False, f'Test command timed out after {timeout}s'
        output = (done.stdout + '\n' + done.stderr).strip()
        tail = '\n'.join(output.splitlines()[-60:])
        self._failed[str(path)] = [] if done.returncode == 0 else failed_cases(output)
        return done.returncode == 0, tail

    def prepare_stamp(self, path, command, inputs):
        """The fingerprint of the step that prepares ``path`` (#696): the SHA-256 of
        ``command`` and of the content of each of ``inputs``, relative paths in the worktree. A
        file that is missing, a link or anything but a regular file counts as such, read
        without following a link and without waiting on a pipe: what an agent left there is
        only hashed, never opened beyond its own entry."""
        digest = hashlib.sha256(command.encode('utf-8'))
        for name in inputs:
            digest.update(b'\0' + name.encode('utf-8') + b'\0')
            try:
                fd = os.open(Path(path) / name,
                             os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            except OSError as exc:
                digest.update(f'missing:{exc.errno}'.encode())
                continue
            with os.fdopen(fd, 'rb') as fh:
                if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                    digest.update(b'not a regular file')
                    continue
                digest.update(b'file:' + hashlib.sha256(fh.read()).hexdigest().encode())
        return digest.hexdigest()

    def prepared(self, path):
        """The ``prepare_stamp`` with which ``path`` was last prepared (#696), or None: never
        since the worktree was created."""
        return self._prepared.get(str(path))

    def mark_prepared(self, path, stamp):
        """Record that the step prepared ``path`` with the fingerprint ``stamp`` (#696)."""
        self._prepared[str(path)] = stamp

    def run_prepare(self, path, command, timeout, cache):
        """``(passed, output_tail)`` of the step that prepares ``path`` (#696), as the test
        command runs (``run_tests``): the same user and environment, with ``timeout``. Its cache
        is ``cache``, a directory of the user that runs it, outside the worktrees and the
        ``state_dir``, which the command gets as ``DEVLOOP_PREPARE_CACHE`` and which is created,
        as that user, before it runs."""
        script = 'mkdir -p -- "$DEVLOOP_PREPARE_CACHE" && ' + command
        done = self._run_command(path, script, timeout, {PREPARE_CACHE_ENV: str(cache)})
        if done is None:
            return False, f'Prepare command timed out after {timeout}s'
        output = (done.stdout + '\n' + done.stderr).strip()
        return done.returncode == 0, '\n'.join(output.splitlines()[-60:])

    def prepare_cache(self, project):
        """The cache of the step that prepares the worktrees of ``project`` (#696):
        ``<HOME>/.cache/devloop-prepare/<project>`` of the user that runs it, the agent user's
        HOME with one, outside the worktrees and the ``state_dir``. Emptying it costs only
        time."""
        if self.agent_user is not None:
            home = Path(self.agent_user.home)
        else:
            env = os.environ if self._test_env is None else self._test_env
            home = Path(env.get('HOME') or Path.home())
        return home / '.cache' / 'devloop-prepare' / re.sub(r'[^\w.-]', '_', project)

    def _run_command(self, path, command, timeout, extra=None):
        """The completed ``bash -c command`` in ``path``, as the project's commands run (#415):
        as the agent user with one, git trusting ``path``, Python writing no bytecode, a
        private temporary directory with ``private_tmp``; ``extra`` variables over the
        environment. None when it timed out: its whole process group is killed."""
        env = dict(os.environ if self._test_env is None else self._test_env, **NO_BYTECODE,
                   **(extra or {}))
        run = run_in_group if self._run is subprocess.run else self._run
        argv = ['bash', '-c', command]
        user = self.agent_user
        if user is not None:
            env = user.git_env(os.environ if env is None else env, path)
        kwargs = {'env': env} if env is not None else {}
        if user is not None:
            argv = user.shell(command)
            if run is run_in_group:
                kwargs['kill'] = user.kill_group
        tmp = None
        if self.private_tmp:
            tmp = (self._agent_tmp() if user is not None
                   else tempfile.mkdtemp(prefix='devloop-tests-'))
            kwargs['env'] = dict(os.environ if env is None else env, TMPDIR=tmp)
        try:
            try:
                done = run(argv, cwd=path, capture_output=True, text=True, timeout=timeout,
                           **kwargs)
            except subprocess.TimeoutExpired:
                return None
        finally:
            if tmp is not None and user is not None:
                self._remove_agent_tmp(tmp)
            elif tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
        return done

    def failed_cases(self, path):
        """The identities of the tests that failed in the latest ``run_tests`` in ``path``
        (#459, ``localtests.failed_cases``): ``[]`` when it passed, None when they cannot be
        told (a timeout, an output without them, no run)."""
        return self._failed.get(str(path))

    def merge_base_copy(self, path):
        """``(name, copy, commit)``: a disposable worktree detached at ``commit``, the merge base
        of the HEAD of ``path`` with ``origin/<base>``, the base commit of the branch, for its
        failed tests (#459); removed with :meth:`remove`."""
        commit = self._git('merge-base', 'HEAD', f'origin/{self.base_branch}', cwd=path).strip()
        name = f'{Path(path).name}-base'
        copy = self.path(name)
        self._fresh(copy)
        self._git('worktree', 'add', '--force', '--detach', str(copy), commit)
        return name, copy, commit
