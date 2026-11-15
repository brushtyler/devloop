"""Git worktrees for agent work. The main checkout's working tree is never touched."""

import base64
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

# The opening and closing lines git writes around a conflict (`=======` alone also underlines
# Markdown and reST headings, so it is not a reliable sign).
_CONFLICT_MARKER = re.compile(rb'^(?:<{7}|>{7})(?: |\r?$)', re.MULTILINE)


class WorkspaceError(RuntimeError):
    pass


def _kill_group(proc):
    """Kill the process group of ``proc`` and return what it wrote, ``('', '')`` if unknown."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        return proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        # A descendant that left the group (its own session) still holds the pipes: give up
        # on the output rather than wait for it.
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe:
                pipe.close()
        proc.wait()
        return '', ''


def run_in_group(argv, *, timeout=None, input=None, watch=None, watch_interval=30.0, **kwargs):
    """Like ``subprocess.run(..., capture_output=True, text=True)``, but the command gets its own
    process group, and a timeout kills the whole group: the test runner's workers, or the
    background commands an agent CLI started, do not outlive it.

    ``watch(pid)``, if given, is called every ``watch_interval`` seconds while the command runs;
    an exception it raises kills the whole group and propagates."""
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
        except BaseException:
            _kill_group(proc)
            raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


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


class Workspace:
    def __init__(self, repo_root, state_dir, base_branch, run=subprocess.run, test_env=None):
        self.repo_root = Path(repo_root)
        self.root = self.repo_root / state_dir / 'worktrees'
        self.base_branch = base_branch
        self._run = run
        self._test_env = test_env

    def _git(self, *args, cwd=None, check=True, stdin=None, env=None, unset=()):
        return self._git_run(*args, cwd=cwd, check=check, stdin=stdin, env=env,
                             unset=unset).stdout

    def _git_run(self, *args, cwd=None, check=True, stdin=None, env=None, unset=()):
        """The completed git command, stdout and stderr included."""
        kwargs = {}
        if env or unset:
            inherited = {k: v for k, v in os.environ.items() if k not in unset}
            kwargs['env'] = dict(inherited, **(env or {}))
        try:
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

    def review_copy(self, path):
        """``(name, path)`` of a disposable worktree for a reviewer, detached at the HEAD of
        ``path`` and sharing its refs, so that ``origin/<base>`` resolves. What the reviewer and
        its tests write lands in the copy, which is removed with :meth:`remove`, not in
        ``path``."""
        name = f'{Path(path).name}-review'
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

    def start_merge(self, path, author=None):
        """Merge ``origin/<base>`` into the branch in ``path`` without committing.

        Returns None when the branch already contains the base (nothing to merge), otherwise
        the files left in conflict (possibly none). The merge stays in progress, with
        ``MERGE_HEAD`` set, so that :meth:`commit_all` concludes it with a merge commit.
        ``author`` (``{name, email}``) is the identity git requires even to start a merge, as
        in :meth:`commit_all`."""
        base = f'origin/{self.base_branch}'
        if self._git('rev-list', '--count', f'HEAD..{base}', cwd=path).strip() == '0':
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
        try:
            done = run(['bash', '-c', command], cwd=path, capture_output=True, text=True,
                       timeout=timeout, **kwargs)
        except subprocess.TimeoutExpired:
            return False, f'Test command timed out after {timeout}s'
        tail = '\n'.join((done.stdout + '\n' + done.stderr).strip().splitlines()[-60:])
        return done.returncode == 0, tail
