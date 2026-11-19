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
  ``<state_dir>/agent-shared``, a directory without secrets;
- at startup ``check`` refuses to run when the agent user can read ``key_dir``, ``state_dir`` or
  anything in them but the worktrees and the shared directory, or cannot write those two.
  ``[agent_user] check = false`` turns it off, for development only.
"""

import os
import pwd
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
# Prints, NUL-terminated, ``r:<path>`` for each path before ``--`` the user can read (a
# directory: list) and ``w:<path>`` for each path after it the user cannot write and enter.
_PROBE = ('mode=r; for p; do if [ "$p" = -- ]; then mode=w; continue; fi; '
          'if [ "$mode" = r ]; then [ -r "$p" ] && printf "r:%s\\0" "$p"; '
          'else { [ -w "$p" ] && [ -x "$p" ]; } || printf "w:%s\\0" "$p"; fi; done; true')


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

    def probe(self, readable, writable):
        """``(read, unwritable)``: the paths of ``readable`` the agent user can read, and those
        of ``writable`` it cannot write and enter. Raises AgentUserError when the probe cannot
        run as the agent user (no sudo rule, for example)."""
        argv = self.command([BASH, '-c', _PROBE, 'probe', *map(str, readable), '--',
                             *map(str, writable)])
        env = self.environment({'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                                'LC_ALL': 'C'})
        try:
            done = self._run(argv, capture_output=True, text=True, timeout=60, env=env)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentUserError(f'cannot run commands as {self.name}: {exc}') from exc
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or '').strip()[:500]
            raise AgentUserError(f'cannot run commands as {self.name} through sudo (exit '
                                 f'{done.returncode}): {detail or "no output"}')
        read, unwritable = [], []
        for item in (done.stdout or '').split('\0'):
            if item.startswith('r:'):
                read.append(item[2:])
            elif item.startswith('w:'):
                unwritable.append(item[2:])
        return read, unwritable


def _entries(path):
    """``path`` itself and what it lists, as the dispatcher's user sees it."""
    path = Path(path)
    try:
        return [path, *sorted(path.iterdir())]
    except OSError:
        return [path]


def check(agent_user, key_dir, state_dir, local_config=None, me=None):
    """Refuse an agent user that does not isolate (#415). It must be another user than the
    dispatcher's (``me``, default: the effective user), able to run through sudo, unable to read
    ``key_dir``, any key in it, ``state_dir`` (to list it), any entry of ``state_dir`` but the
    worktrees and the shared directory, and the local overlay ``local_config``; and able to
    write the worktrees and the shared directory. Raises AgentUserError, listing every
    problem."""
    me = me if me is not None else pwd.getpwuid(os.geteuid()).pw_name
    if agent_user.name == me:
        raise AgentUserError(f'agent_user.name: {me!r} is the dispatcher\'s own user: agents '
                             'need a user of their own')
    state = Path(state_dir)
    exempt = {state / WORKTREES_DIR, state / SHARED_DIR}
    secret = _entries(Path(key_dir).expanduser())
    secret += [p for p in _entries(state) if p not in exempt]
    if local_config:
        secret.append(Path(local_config))
    read, unwritable = agent_user.probe(secret, sorted(exempt))
    problems = [f'{agent_user.name} can read {p}' for p in read]
    problems += [f'{agent_user.name} cannot write {p}' for p in unwritable]
    if problems:
        raise AgentUserError('the agent user does not isolate: ' + '; '.join(problems)
                             + ' (ops/host/setup-agent-user.sh prepares the permissions; '
                               '[agent_user] check = false skips this check, in development only)')
