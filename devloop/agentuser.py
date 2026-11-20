"""The system user agents and tests run as (issue #415).

With ``[agent_user] name`` set, the dispatcher starts every agent CLI (claude, codex, agy) and
the test command as that user, through ``sudo``, so that they cannot read what only the
dispatcher's user may: the private keys of the GitHub Apps (``key_dir``), ``state_dir`` (journal,
logs, registers) and the local overlay. Without it, agents run as the dispatcher's own user, as
before: the isolated environment (``agents.isolated_env``) then prevents only the accidental use
of credentials.

The mechanism, chosen by the operator on #415:

- ``sudo -n -E -u <user> -- <argv>``: ``-n`` never asks for a password (a missing rule fails at
  once), ``-E`` keeps the isolated environment devloop builds, which needs the ``SETENV`` tag in
  sudoers. The sudoers rule (``ops/host/sudoers-agent``) lists only the CLIs, ``BASH`` (the test
  command and the startup probe) and ``KILL``; ``!use_pty``, so that the command stays in the
  process group devloop started;
- a timeout or a stall stops the turn by killing its process group as the agent user
  (``kill_group``): the dispatcher's user cannot signal processes of another user;
- what the agents read or write besides their worktree (the codex final message, the schema of
  the turn, the empty ``gh`` config, the temporary directories of the tests) is in
  ``<state_dir>/agent-shared`` (``SharedDir``), a directory without secrets that the agent user
  enters but cannot write. The dispatcher creates there, opening every component without
  following links, the schemas (in ``schemas``, which the agent user cannot write either) and one
  new directory per turn or test run (in ``turns`` and ``tmp``): the only places of the shared
  directory the agent user writes. What it wrote is read back without following links, and only
  from a regular file with a single link;
- the git commands the dispatcher runs in a worktree take the repository metadata from where the
  dispatcher put them, never from what the worktree points to, and run no hook
  (``workspace.Workspace``); the agent user cannot write the repository's ``.git``;
- at startup ``check`` refuses to run when the agent user can read or enter ``key_dir``, or
  anything in ``state_dir`` but the worktrees and the shared directory (a directory it can enter
  without listing it is refused too: its files would be in reach), or can read the overlay; when
  it can write the repository's ``.git``, the worktrees directory or the fixed directories of the
  shared one; and when it cannot write the directories made for its turns and test runs.
  ``[agent_user] check = false`` turns it off, for development only.

One user runs every agent, implementers and reviewers (#438): a turn must not change what the
turns after it use. The files of the CLIs that carry instructions or configuration, and the CLIs
themselves, belong to root (``ops/host/setup-agent-user.sh``); the agent user writes only its
credentials, sessions, caches and logs. ``check`` refuses an installation where the agent user
can write, own (an owner changes the mode), create, rename or remove any of them
(``cli_items``): the directories of the CLIs
(``CLI_DIRS``: no new entry, such as an instruction file, an agent, a skill or a hook a turn
would add), what they hold that carries instructions or configuration and ``~/.local``
(``CLI_SEALED``: nothing writable in them), and each CLI in ``~/.local/bin`` with the directory
of the file it leads to (``CLIS``). A directory above them that the agent user can write protects
them only when it is sticky and neither it nor the entry is the agent user's: the home, which
the agent user writes, is root's and sticky. A link among them, or inside a sealed one, is
followed, and what it leads to must be protected the same way; the sealed files must stay
readable by the agent user. The probe runs only the system's tools, never those of the agent
user's PATH. What the CLIs create on their first use in their directories (``CLI_WRITABLE``,
#494: the ``bin`` of agy's shell tool) stays the agent user's, and ``check`` wants it writable;
before each agy turn the dispatcher reads it as the agent user and refuses the turn when it
holds anything but the wrapper agy writes there (``AgentUser.agy_bin_problems``). In every
claude turn, the auto memory, which ``~/.claude/projects`` keeps and
shares among the worktrees of a repository, is off, also over the settings of the project that a
turn writes in the worktree (``agents.CLAUDE_TURN_SETTINGS``, given with ``--settings``), and only
the MCP servers of the command line count, never those of ``~/.claude.json``, which the agent
user writes (``agents.CLAUDE_TURN_ENV``, ``agents.build_command``).
"""

import base64
import binascii
import contextlib
import errno
import hashlib
import os
import pwd
import secrets
import shutil
import stat
import subprocess
from pathlib import Path

SUDO = 'sudo'
# Absolute paths, as sudoers lists them: a ``bash`` or ``kill`` earlier in the agent user's PATH
# is never the one started.
BASH = '/usr/bin/bash'
KILL = '/usr/bin/kill'
# In ``state_dir``: the only entries the agent user may reach besides the worktrees.
SHARED_DIR = 'agent-shared'
WORKTREES_DIR = 'worktrees'
# In the shared directory: the schemas the dispatcher writes, the directories of the turns (the
# codex final message) and of the test runs (TMPDIR), the empty ``gh`` config.
SCHEMAS = 'schemas'
TURNS = 'turns'
TMP = 'tmp'
GH_CONFIG = 'gh-config'
# In the agent user's home (#438): the directories of the CLIs, where the agent user may create
# nothing and that it may not rename; in them, what carries instructions or configuration, and
# the installed CLIs, where nothing may be writable; the CLIs devloop starts from ``.local/bin``.
CLI_DIRS = ('.claude', '.codex', '.gemini', '.gemini/antigravity-cli')
CLI_SEALED = (
    '.claude/settings.json', '.claude/CLAUDE.md', '.claude/rules', '.claude/agents',
    '.claude/commands', '.claude/skills', '.claude/hooks', '.claude/output-styles',
    '.claude/plugins',
    '.codex/config.toml', '.codex/AGENTS.md', '.codex/AGENTS.override.md', '.codex/prompts',
    '.codex/rules', '.codex/skills', '.codex/packages',
    '.gemini/settings.json', '.gemini/GEMINI.md', '.gemini/antigravity-cli/settings.json',
    '.local')
CLIS = ('claude', 'codex', 'agy')
# The entries the CLIs create on their first use inside the directories of ``CLI_DIRS``, where
# the agent user creates nothing (#494): ``ops/host/setup-agent-user.sh`` creates them as the
# agent user's, mode 700, and the startup check wants them writable. agy writes the wrapper of
# its shell tool, ``AGY_WRAPPER``, in ``AGY_BIN`` each time it starts it.
CLI_WRITABLE = ('.gemini/antigravity-cli/bin',)
AGY_BIN = '.gemini/antigravity-cli/bin'
AGY_WRAPPER = 'agentapi'
# The largest wrapper read back: the expected one is a few dozen bytes.
WRAPPER_LIMIT = 4096
# Each argument is ``<kind>:<path>``; prints, NUL-terminated, ``<problem>:<path>`` for each
# problem found: ``secret``, a path the user must neither read nor enter (r, x); ``list``, one it
# must not read (r); ``ro``, one it must enter but not write (e, w); ``tree``, one where nothing
# may be writable (w, the first path found); ``rw``, one it must write and enter (u); ``rd``,
# one it must read (n). #438: ``fixed``, one it must neither write (w) nor own (o: an owner
# changes the mode), nor create (c), nor rename or remove (m), and the same for every directory
# above it: in a directory it writes, only the sticky bit protects an entry, and only when
# neither the entry nor the directory is its own. A link among them is followed one step at a
# time, and what it leads to is fixed too; a link that cannot be read, or a chain of links that
# does not end, is refused (l). ``sealed``, a fixed one where nothing may be writable (w) or its
# own (o) either, the first path found, where everything must be readable and the directories
# enterable (n), and where a link leads to a sealed path. ``bin``, a fixed executable whose
# resolved file is fixed too, and sealed its directory. The tools come from the system's
# directories only: the agent user's PATH starts with ``~/.local/bin``, which it controls on
# the installation the probe must refuse.
_PROBE = r'''PATH=/usr/bin:/bin
out() { printf '%s:%s\0' "$1" "$2"; }
me=$(id -u)
declare -A seen
mine() { [ "$(stat -c %u -- "$1" 2>/dev/null)" = "$me" ]; }
to() {
    n=$((n + 1))
    if [ "$2" -ge 40 ] || [ "$n" -gt 1000 ] || ! T=$(readlink -- "$1"); then
        out l "$1"; return 1
    fi
    case $T in /*) ;; *) T=${1%/*}/$T ;; esac
}
fixed() {
    local q=$1 h=${2:-0} d
    [ -e "$q" ] && [ -w "$q" ] && out w "$q"
    while [ "$q" != / ] && [ -n "$q" ]; do
        mine "$q" && out o "$q"
        d=${q%/*}; d=${d:-/}
        if [ -w "$d" ]; then
            if ! [ -e "$q" ] && ! [ -L "$q" ]; then out c "$q"
            elif ! [ -k "$d" ] || mine "$q" || mine "$d"; then out m "$q"; fi
        fi
        [ -L "$q" ] && to "$q" "$h" && fixed "$T" $((h + 1))
        q=$d
    done
}
writable() { [ -e "$1" ] && find "$1" -writable -printf 'w:%p\0' -quit 2>/dev/null; }
sealed() {
    local c l
    fixed "$1"
    [ -e "$1" ] && c=$(readlink -e -- "$1") || return 0
    [ -n "${seen[$c]}" ] && return 0
    seen[$c]=1
    find -H "$1" -writable -printf 'w:%p\0' -quit 2>/dev/null
    find -H "$1" -user "$me" -printf 'o:%p\0' -quit 2>/dev/null
    find -H "$1" ! -type l \( ! -readable -o -type d ! -executable \) -printf 'n:%p\0' -quit \
        2>/dev/null
    while IFS= read -r -d '' l; do
        to "$l" 0 && sealed "$T"
    done < <(find -H "$1" -mindepth 1 -type l -print0 2>/dev/null)
}
for a; do
    k=${a%%:*}; p=${a#*:}; n=0
    case $k in
        secret) [ -r "$p" ] && out r "$p"; [ -d "$p" ] && [ -x "$p" ] && out x "$p" ;;
        list) [ -r "$p" ] && out r "$p" ;;
        ro) [ -w "$p" ] && out w "$p"; { [ -r "$p" ] && [ -x "$p" ]; } || out e "$p" ;;
        tree) writable "$p" ;;
        rw) { [ -w "$p" ] && [ -x "$p" ]; } || out u "$p" ;;
        rd) [ -r "$p" ] || out n "$p" ;;
        fixed) fixed "$p" ;;
        sealed) sealed "$p" ;;
        bin) fixed "$p"
             t=$(readlink -f -- "$p" 2>/dev/null) && [ -e "$t" ] && [ "$t" != "$p" ] \
                 && { fixed "$t"; sealed "${t%/*}"; } ;;
    esac
done
true'''
_PROBLEMS = {'r': 'can read', 'x': 'can enter', 'w': 'can write', 'e': 'cannot enter',
             'u': 'cannot write', 'n': 'cannot read', 'c': 'can create', 'o': 'owns',
             'm': 'can rename or remove', 'l': 'cannot follow the link'}
# A directory opened without following a link in its last component.
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class AgentUserError(RuntimeError):
    """The agent user is unusable or does not isolate: devloop does not start."""


class AgentUser:
    """The system user ``name`` the agents run as. ``home``: its home directory (default: from
    the password database), where its CLIs keep their logins and session transcripts. ``run``
    runs the probes and the kills (``subprocess.run``)."""

    def __init__(self, name, home=None, run=subprocess.run):
        self.name = name
        if home is None:
            try:
                home = pwd.getpwnam(name).pw_dir
            except KeyError:
                raise AgentUserError(f'agent_user.name: no system user {name!r}') from None
        self.home = Path(home)
        self._run = run

    def command(self, argv):
        """``argv`` run as the agent user, with the caller's environment."""
        return [SUDO, '-n', '-E', '-u', self.name, '--', *argv]

    def environment(self, env):
        """``env`` with the agent user's identity: its HOME, where its CLIs keep their logins."""
        return dict(env, HOME=str(self.home), USER=self.name, LOGNAME=self.name)

    def shell(self, command):
        """The test command ``command`` run by ``BASH`` as the agent user."""
        return self.command([BASH, '-c', command])

    def kill_group(self, pgid):
        """Kill, as the agent user, every process of the process group ``pgid``: the turn or
        the test command and what it left in the background. Never raises: the caller kills
        what its own user may and waits for the rest."""
        try:
            self._run(self.command([KILL, '-KILL', '--', f'-{pgid}']), capture_output=True,
                      text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass

    def release(self, path):
        """Give the group class of every file the agent user created under ``path`` read,
        write and search: with the default ACLs of the shared directory and of the worktrees,
        the dispatcher's user can then remove them, also those created with a mode such as
        ``0700``. Never raises."""
        script = 'chmod -R g+rwX -- "$1" 2>/dev/null; true'
        try:
            self._run(self.command([BASH, '-c', script, 'release', str(path)]),
                      capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError):
            pass

    def agy_bin_problems(self):
        """The problems of ``AGY_BIN`` in the agent user's home, read as that user
        (``wrapper_problems``, #494): agy writes it, so a turn could leave there, for the turns
        after it, a wrapper of agy's shell tool that runs something else, or another
        executable. Raises AgentUserError when it cannot be read."""
        argv = self.command([BASH, '-c', _WRAPPER_PROBE, 'wrapper', str(self.home / AGY_BIN),
                             AGY_WRAPPER, str(WRAPPER_LIMIT + 1)])
        env = self.environment({'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                                'LC_ALL': 'C'})
        try:
            done = self._run(argv, capture_output=True, timeout=60, env=env)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentUserError(f'cannot run commands as {self.name}: {exc}') from exc
        if done.returncode != 0:
            detail = os.fsdecode(done.stderr or b'').strip()[:500]
            raise AgentUserError(f'cannot read {self.home / AGY_BIN} as {self.name} (exit '
                                 f'{done.returncode}): {detail or "no output"}')
        return wrapper_problems(self.home, done.stdout or b'')

    def probe(self, items):
        """The problems the agent user has with ``items``, ``(kind, path)`` pairs (``_PROBE``),
        as ``(problem, path)`` pairs, ``problem`` a key of ``_PROBLEMS``. Raises AgentUserError
        when the probe cannot run as the agent user (no sudo rule, for example)."""
        argv = self.command([BASH, '-c', _PROBE, 'probe',
                             *(f'{kind}:{path}' for kind, path in items)])
        env = self.environment({'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                                'LC_ALL': 'C'})
        try:
            done = self._run(argv, capture_output=True, text=True, timeout=120, env=env)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentUserError(f'cannot run commands as {self.name}: {exc}') from exc
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or '').strip()[:500]
            raise AgentUserError(f'cannot run commands as {self.name} through sudo (exit '
                                 f'{done.returncode}): {detail or "no output"}')
        found = []
        for item in (done.stdout or '').split('\0'):
            problem, sep, path = item.partition(':')
            if sep and problem in _PROBLEMS:
                found.append((problem, path))
        return found


class UnsafeFile(OSError):
    """What the agent user left in the shared directory is not a regular file of its own."""


class SharedDir:
    """``path``, the shared directory (``<state_dir>/agent-shared``), as the dispatcher's user
    writes and reads it (#415). Its parent, ``state_dir``, is the dispatcher's; from there every
    component is opened without following a link (``O_NOFOLLOW``, relative to the directory
    opened before it), so that a link or a substitute in any component is refused, not
    followed. The agent user cannot write the shared directory nor its fixed subdirectories
    (``ops/host/setup-agent-user.sh``): what the dispatcher creates there, with an exclusive
    creation, cannot be replaced; the agent user writes only in the directories :meth:`new_dir`
    makes."""

    MAX_READ = 16 * 1024 * 1024

    def __init__(self, path):
        self.path = Path(path)

    @contextlib.contextmanager
    def _dir(self, *parts, create=False):
        """A descriptor of ``path/<parts>``; ``create``: make the missing directories, mode
        ``0755`` (the default ACL of their parent applies)."""
        if create:  # state_dir, the dispatcher's
            self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for part in (self.path.name, *parts):
                if create:
                    try:
                        os.mkdir(part, 0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                inner = os.open(part, _DIR, dir_fd=fd)
                os.close(fd)
                fd = inner
            yield fd
        finally:
            os.close(fd)

    def ensure(self, name):
        """The fixed subdirectory ``name`` (``GH_CONFIG``, for example), created if missing."""
        with self._dir(name, create=True):
            pass
        return self.path / name

    def write(self, sub, name, data):
        """Create the new file ``sub/name`` with ``data`` (bytes), mode ``0644``: never an
        existing file nor a link."""
        with self._dir(sub, create=True) as fd:
            out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                          | os.O_CLOEXEC, 0o644, dir_fd=fd)
            with os.fdopen(out, 'wb') as handle:
                handle.write(data)
        return self.path / sub / name

    def new_dir(self, sub, prefix):
        """A new directory ``sub/<prefix><random>``, mode ``0770``, so that the named entries of
        the default ACL of ``sub`` apply: the agent user writes there."""
        with self._dir(sub, create=True) as fd:
            for _ in range(100):
                name = f'{prefix}{secrets.token_hex(6)}'
                try:
                    os.mkdir(name, 0o770, dir_fd=fd)
                except FileExistsError:
                    continue
                inner = os.open(name, _DIR, dir_fd=fd)
                try:
                    os.fchmod(inner, 0o770)  # the umask applies without a default ACL
                finally:
                    os.close(inner)
                return self.path / sub / name
        raise FileExistsError(errno.EEXIST, f'no free name in {self.path / sub}')

    def read(self, sub, *names, limit=MAX_READ):
        """The bytes of the file ``sub/<names>``, or None when it does not exist. Raises
        UnsafeFile when it is not a regular file with a single link (a symbolic link, a hard
        link to another file, a FIFO) or is larger than ``limit``, OSError when a component is
        a link or not a directory."""
        *dirs, name = names
        try:
            with self._dir(sub, *dirs) as fd:
                try:
                    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                                     | os.O_CLOEXEC, dir_fd=fd)
                except FileNotFoundError:
                    return None
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        raise UnsafeFile(errno.ELOOP, 'a symbolic link', name) from None
                    raise
                with os.fdopen(handle, 'rb') as file:
                    info = os.fstat(file.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise UnsafeFile(errno.EPERM, 'not a regular file with a single link',
                                         name)
                    data = file.read(limit + 1)
        except FileNotFoundError:
            return None
        if len(data) > limit:
            raise UnsafeFile(errno.EFBIG, f'larger than {limit} bytes', name)
        return data

    def remove(self, sub, name):
        """Remove the file ``sub/name``, if any (a link itself, never its target)."""
        try:
            with self._dir(sub) as fd:
                os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            pass

    def remove_dir(self, sub, name):
        """Remove the directory ``sub/name`` and what it holds, without following links.
        Raises OSError when something cannot be removed (``AgentUser.release`` first)."""
        try:
            with self._dir(sub) as fd:
                shutil.rmtree(name, dir_fd=fd)
        except FileNotFoundError:
            pass


def agy_wrapper(home):
    """The wrapper of agy's shell tool in ``AGY_BIN`` of ``home`` (#494), as the current version
    of agy writes it each time it starts the tool; ``wrapper_problems`` accepts it also without
    its final newline."""
    return f'#!/bin/sh\nexec "{home}/.local/bin/agy" agentapi "$@"\n'.encode()


# Run as the agent user, on ``AGY_BIN``: NUL-terminated records, ``l:`` when it is a link,
# ``n:`` when it is not a directory, ``e:<type>:<name>`` for each entry (the type is find's
# ``%y``: ``f`` a regular file, ``l`` a link, ``d`` a directory), then ``c:<base64>``, the first
# bytes of ``AGY_WRAPPER`` when it is a regular file. Nothing when ``AGY_BIN`` does not exist; an
# exit status other than 0 when it cannot be listed or read. Only the system's tools, as for
# ``_PROBE``.
_WRAPPER_PROBE = r'''PATH=/usr/bin:/bin
set -o pipefail
d=$1 f=$1/$2
if [ -L "$d" ]; then printf 'l:\0'; exit 0; fi
[ -e "$d" ] || exit 0
if ! [ -d "$d" ]; then printf 'n:\0'; exit 0; fi
find "$d" -mindepth 1 -maxdepth 1 -printf 'e:%y:%f\0' || exit 1
if [ -f "$f" ] && ! [ -L "$f" ]; then
    printf 'c:'
    head -c "$3" -- "$f" | base64 -w0 || exit 1
    printf '\0'
fi'''


def _shown(name, width=80):
    """``name``, which the agent user chose, as a short quoted text."""
    text = repr(name)
    return text if len(text) <= width else text[:width - 3] + '...'


def wrapper_problems(home, records):
    """The problems of ``AGY_BIN`` in ``home`` from ``records``, the output of
    ``_WRAPPER_PROBE`` (bytes): none when it holds only ``AGY_WRAPPER`` with the content of
    ``agy_wrapper``, or holds nothing, or does not exist."""
    expected = agy_wrapper(home)
    where = Path(home) / AGY_BIN
    problems, entries, content = [], [], None
    for record in records.split(b'\0'):
        kind, _, rest = record.partition(b':')
        if kind == b'l':
            problems.append(f'{where} is a symbolic link')
        elif kind == b'n':
            problems.append(f'{where} is not a directory')
        elif kind == b'e':
            kind, _, name = rest.partition(b':')
            entries.append((kind.decode('ascii', 'replace'), os.fsdecode(name)))
        elif kind == b'c':
            try:
                content = base64.b64decode(rest, validate=True)
            except binascii.Error:
                content = None
    for kind, name in entries:
        if name != AGY_WRAPPER:
            problems.append(f'{where} holds {_shown(name)}, which agy does not write there')
        elif kind != 'f':
            problems.append(f'{where / name} is not a regular file (type {kind!r})')
        elif content is None:
            problems.append(f'{where / name} could not be read')
        elif content not in (expected, expected.rstrip(b'\n')):
            size = (f'more than {WRAPPER_LIMIT}' if len(content) > WRAPPER_LIMIT
                    else str(len(content)))
            digest = hashlib.sha256(content).hexdigest()[:16]
            problems.append(f'{where / name} is not the wrapper agy writes: {size} bytes, '
                            f'sha256 {digest}..., instead of {expected.decode()!r}')
    return problems


def _entries(path):
    """``path`` itself and what it lists, as the dispatcher's user sees it."""
    path = Path(path)
    try:
        return [path, *sorted(path.iterdir())]
    except OSError:
        return [path]


def cli_items(home):
    """The probe items of the CLIs in ``home``, the agent user's home (#438): what a turn must
    not change for the turns after it, and what the CLIs must write (#494)."""
    home = Path(home)
    return ([('fixed', home / d) for d in CLI_DIRS]
            + [('sealed', home / p) for p in CLI_SEALED]
            + [('bin', home / '.local' / 'bin' / cli) for cli in CLIS]
            + [('rw', home / p) for p in CLI_WRITABLE])


def check(agent_user, key_dir, state_dir, local_config=None, me=None, git_dir=None):
    """Refuse an agent user that does not isolate (#415). It must be another user than the
    dispatcher's (``me``, default: the effective user) and able to run through sudo. It must
    not read nor enter ``key_dir`` and any entry of ``state_dir`` but the worktrees and the
    shared directory (refusing to enter covers what they hold, also in a directory it could
    enter without listing it), nor read the keys, the overlay ``local_config`` and ``state_dir``
    itself (it only enters it). It must not write anything in ``git_dir`` (the repository's
    ``.git``), nor the worktrees directory and the fixed directories of the shared one, which
    it must enter; it must write the directories the dispatcher makes for its turns and test
    runs, and read the schemas. It must not write, own, create, rename nor remove the files of
    the CLIs in its home that later turns use (``cli_items``, #438), and must write the entries
    its CLIs create on their first use (``CLI_WRITABLE``, #494). Raises AgentUserError, listing
    every problem."""
    me = me if me is not None else pwd.getpwuid(os.geteuid()).pw_name
    if agent_user.name == me:
        raise AgentUserError(f'agent_user.name: {me!r} is the dispatcher\'s own user: agents '
                             'need a user of their own')
    state = Path(state_dir)
    exempt = {state / WORKTREES_DIR, state / SHARED_DIR}
    items = [('list', state)]
    items += [('secret', p) for p in _entries(Path(key_dir).expanduser())]
    items += [('secret', p) for p in _entries(state)[1:] if p not in exempt]
    if local_config:
        items.append(('secret', Path(local_config)))
    if git_dir:
        items.append(('tree', Path(git_dir)))
    items += cli_items(agent_user.home)
    shared = SharedDir(state / SHARED_DIR)
    items += [('ro', state / WORKTREES_DIR), ('ro', shared.path)]
    made = []
    try:
        try:
            for sub in (SCHEMAS, TURNS, TMP):
                shared.ensure(sub)
                items.append(('ro', shared.path / sub))
            for sub in (TURNS, TMP):
                made.append((sub, shared.new_dir(sub, '.check-')))
                items.append(('rw', made[-1][1]))
            schema = shared.write(SCHEMAS, f'.check-{secrets.token_hex(6)}.json', b'{}\n')
            made.append((SCHEMAS, schema))
            items.append(('rd', schema))
        except OSError as exc:
            raise AgentUserError(f'the shared directory {shared.path}: {exc}') from exc
        found = agent_user.probe(items)
    finally:
        for sub, path in made:
            try:
                if sub == SCHEMAS:
                    shared.remove(sub, path.name)
                else:
                    shared.remove_dir(sub, path.name)
            except OSError:
                pass
    problems = [f'{agent_user.name} {_PROBLEMS[problem]} {path}'
                for problem, path in dict.fromkeys(found)]
    if problems:
        raise AgentUserError('the agent user does not isolate: ' + '; '.join(problems)
                             + ' (ops/host/setup-agent-user.sh prepares the permissions; '
                               '[agent_user] check = false skips this check, in development only)')
