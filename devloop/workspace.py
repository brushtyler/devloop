"""Git worktrees for agent work. The main checkout's working tree is never touched."""

import base64
import hashlib
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import nullcontext
from pathlib import Path

# The opening and closing lines git writes around a conflict (`=======` alone also underlines
# Markdown and reST headings, so it is not a reliable sign).
_CONFLICT_MARKER = re.compile(rb'^(?:<{7}|>{7})(?: |\r?$)', re.MULTILINE)


class WorkspaceError(RuntimeError):
    pass


def partial_text(data):
    """Output that ``communicate()`` collected before a timeout: bytes even in text mode."""
    if isinstance(data, bytes):
        return data.decode('utf-8', 'replace').replace('\r\n', '\n')
    return data or ''


def _kill_group(proc):
    """Kill the process group of ``proc`` and return what it wrote."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
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


def run_in_group(argv, *, timeout=None, input=None, watch=None, watch_interval=30.0, **kwargs):
    """Like ``subprocess.run(..., capture_output=True, text=True)``, but the command gets its own
    process group, and a timeout kills the whole group: the test runner's workers, or the
    background commands an agent CLI started, do not outlive it.

    ``watch(pid)``, if given, is called every ``watch_interval`` seconds while the command runs;
    an exception it raises kills the whole group and propagates, with what the command wrote
    until then in its ``partial_output`` attribute, ``(stdout, stderr)``."""
    kwargs.pop('capture_output', None)
    kwargs.pop('text', None)
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else None,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True, **kwargs)
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        wait = None if deadline is None else max(0.0, deadline - time.monotonic())
        if watch is not None:
            wait = watch_interval if wait is None else min(wait, watch_interval)
        try:
            # Retrying communicate() after its timeout loses no output and keeps writing the
            # input given to the first call; passing input again raises ValueError.
            pending, input = input, None
            out, err = proc.communicate(input=pending, timeout=wait)
            break
        except subprocess.TimeoutExpired:
            if watch is None or (deadline is not None and time.monotonic() >= deadline):
                out, err = _kill_group(proc)
                raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err) from None
        try:
            watch(proc.pid)
        except BaseException as exc:
            partial = _kill_group(proc)
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
    one."""

    def __init__(self, repo_root, state_dir, base_branch, run=subprocess.run, test_env=None,
                 private_tmp=False):
        self.repo_root = Path(repo_root)
        self.root = self.repo_root / state_dir / 'worktrees'
        self.base_branch = base_branch
        self._run = run
        self._test_env = test_env
        self.private_tmp = private_tmp
        self._shared = threading.RLock()
        self._read_only = None

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

    def _git(self, *args, cwd=None, check=True, stdin=None, env=None, unset=()):
        return self._git_run(*args, cwd=cwd, check=check, stdin=stdin, env=env,
                             unset=unset).stdout

    def _git_run(self, *args, cwd=None, check=True, stdin=None, env=None, unset=()):
        """The completed git command, stdout and stderr included."""
        if self._read_only is not None:
            if args[0] == 'fetch':
                return subprocess.CompletedProcess(['git', *args], 0, '', '')
            if args[0] not in _READ_GIT:
                raise WorkspaceError(f'git {args[0]}: not run, the workspace is read-only')
            env = dict(env or {}, **self._read_only)
        kwargs = {}
        if env or unset:
            inherited = {k: v for k, v in os.environ.items() if k not in unset}
            kwargs['env'] = dict(inherited, **(env or {}))
        try:
            with self._shared if args[:1] and args[0] in _SHARED_GIT else nullcontext():
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

    def _fresh(self, path):
        if path.exists():
            self._git('worktree', 'remove', '--force', str(path), check=False)
            if path.exists():
                shutil.rmtree(path)
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

    def pristine(self, path, head):
        """Whether ``path`` is still at commit ``head``, with no change and no new file, not
        even an ignored one."""
        if self.head(path) != head:
            return False
        return not self._git('status', '--porcelain', '--ignored', '--untracked-files=all',
                             cwd=path).strip()

    def remove(self, name):
        path = self.path(name)
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
        self._git('fetch', 'origin', self.base_branch,
                  f'+refs/heads/{branch}:refs/remotes/origin/{branch}')
        base = f'origin/{self.base_branch}'
        commit = head
        for _ in range(limit):
            if commit == approved:
                return commit != head
            line = self._git('rev-list', '--parents', '-n1', commit).split()
            if len(line) != 3:
                return False
            first, merged = line[1:]
            if self._git_run('merge-base', '--is-ancestor', merged, base,
                             check=False).returncode:
                return False
            tree, clean, _ = self._merge_tree(first, merged)
            if not clean or tree != self._git('rev-parse', f'{commit}^{{tree}}').strip():
                return False
            commit = first
        return False

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
        out = self._git('diff', '--name-only', '-z', '--diff-filter=U', cwd=path)
        return [name for name in out.split('\0') if name]

    def merge_head(self, path):
        """The commit being merged into ``path``, or None when no merge is in progress."""
        return self._git('rev-parse', '-q', '--verify', 'MERGE_HEAD', cwd=path,
                         check=False).strip() or None

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
        included, stays as it is."""
        index = Path(path) / self._git('rev-parse', '--git-path', 'index', cwd=path).strip()
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / 'index'
            if index.exists():
                shutil.copyfile(index, copy)
            env = {'GIT_INDEX_FILE': str(copy)}
            if not index.exists():
                self._git('read-tree', 'HEAD', cwd=path, env=env)
            self._git('add', '-A', cwd=path, env=env)
            return self._git('write-tree', cwd=path, env=env).strip()

    def commit_tree(self, path, commit):
        """The tree of ``commit``."""
        return self._git('rev-parse', f'{commit}^{{tree}}', cwd=path).strip()

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
        authentication headers are disabled."""
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
        self._git('push', *(['--force'] if replace else []), remote,
                  f'HEAD:refs/heads/{branch}', cwd=path, env=env, unset=unset)

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
        """``(passed, output_tail)`` for the project test command in ``path``."""
        kwargs = {'env': self._test_env} if self._test_env is not None else {}
        run = run_in_group if self._run is subprocess.run else self._run
        with (tempfile.TemporaryDirectory(prefix='devloop-tests-') if self.private_tmp
              else nullcontext()) as tmp:
            if tmp is not None:
                base = os.environ if self._test_env is None else self._test_env
                kwargs['env'] = dict(base, TMPDIR=tmp)
            try:
                done = run(['bash', '-c', command], cwd=path, capture_output=True, text=True,
                           timeout=timeout, **kwargs)
            except subprocess.TimeoutExpired:
                return False, f'Test command timed out after {timeout}s'
        tail = '\n'.join((done.stdout + '\n' + done.stderr).strip().splitlines()[-60:])
        return done.returncode == 0, tail
