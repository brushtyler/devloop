#!/usr/bin/env python3
"""Run a command as an agent's GitHub App of the project, with a token limited to the permissions
it needs: a thin wrapper of ``devloop token`` (ADR 0006, R6).

Usage:
    python3 scripts/agent-token.py [--state-dir DIR] <actor> [--perm NAME=LEVEL ...] -- \\
        <command> [args...]

Examples:
    python3 scripts/agent-token.py claude -- gh pr comment 70 --body-file reply.md
    python3 scripts/agent-token.py codex --perm pull_requests=write -- \\
        gh api -X POST repos/<owner>/<name>/pulls/71/reviews --input review.json

It runs ``scripts/devloop-pinned --state-dir <state_dir> token <actor> ... -- <command>``:
devloop's pinned copy of that state_dir, with the configuration of its project (the
installation's config.toml, the project file and the local overlay), whose ``repo`` and
``[apps]`` give the repository and the app; only the actor's key is read, from
``$DEVLOOP_APP_KEY_DIR`` or the project's ``key_dir``. The state_dir is DIR, or
``.devloop`` of the main checkout of this clone, the launcher's default, also when the script
runs from a linked worktree: git's common directory names the main checkout. Like the launcher,
it stops when that directory is missing and the clone still has the state_dir of before #563
(``OLD_STATE``): it never falls back on it. Run it from
the checkout of the managed repository, or one of its worktrees: the command runs in the current
directory.

Actors: claude, codex, agy. devloop's own apps (dispatcher, merger, implementer) are reserved
to devloop, and the supervisor's role app is not exposed either: the supervisor uses
scripts/supervisor-token.py. Without --perm the token gets issues=write and
pull_requests=write; contents=write and any other write permission are refused: pushes and
merges are operator actions. The token reaches the command through GH_TOKEN only; it is never
printed. ``python3 -m devloop token --help`` lists the rules; ``devloop/token.py`` applies them.

It imports nothing from the clone: the pinned copy is the only devloop code that runs.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / 'scripts' / 'devloop-pinned'
# The launcher's default state_dir, under the main checkout of this clone.
STATE = Path('.devloop')
# The default state_dir of before #563, never used: present without STATE, it stops the script.
OLD_STATE = Path('.ssdlc') / 'devloop'
STATE_DIR = '--state-dir'


def main_checkout(root=ROOT):
    """The main checkout of the clone ``root`` is in: ``root`` itself, or the checkout a linked
    worktree belongs to, whose ``.git`` is git's common directory. ``root`` when git cannot say,
    or when the common directory is not a checkout's ``.git`` (a bare repository)."""
    try:
        done = subprocess.run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                              cwd=root, capture_output=True, text=True)
    except OSError:
        return root
    common = done.stdout.strip()
    if done.returncode != 0 or not common:
        return root
    common = Path(common)
    return common.parent if common.name == '.git' else root


def command(argv, python=sys.executable):
    """The command line of the launcher for ``argv``, or SystemExit with the usage."""
    if not argv or argv[0] in ('-h', '--help'):
        raise SystemExit(__doc__.strip())
    state = None
    if argv[0] == STATE_DIR:
        if len(argv) < 2:
            raise SystemExit(f'agent-token: {STATE_DIR} needs a directory')
        state, argv = Path(argv[1]), argv[2:]
    elif argv[0].startswith(STATE_DIR + '='):
        state, argv = Path(argv[0][len(STATE_DIR) + 1:]), argv[1:]
    if not argv or '--' not in argv:
        raise SystemExit(__doc__.strip())
    if state is None:
        root = main_checkout()
        state = root / STATE
        if not state.exists() and (root / OLD_STATE).exists():
            raise SystemExit(f'agent-token: {root / OLD_STATE} is the state_dir of before #563, '
                             f'and {state} does not exist: move it as ops/host/migrate.md says, '
                             'section "Percorsi devloop e istanza devloop@devloop"')
    # The launcher wants it absolute; with it, it leaves the current directory as it is.
    return [python, str(LAUNCHER), STATE_DIR, os.path.abspath(state), 'token', *argv]


def main(argv=None, execv=os.execv):
    args = command(sys.argv[1:] if argv is None else list(argv))
    execv(args[0], args)


if __name__ == '__main__':
    main()
