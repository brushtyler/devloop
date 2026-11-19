#!/usr/bin/env python3
"""Run a command as the supervisor's GitHub App, with a token limited to the permissions it needs.

Usage:
    python3 scripts/supervisor-token.py [--perm NAME=LEVEL ...] -- <command> [args...]

Examples:
    python3 scripts/supervisor-token.py -- gh issue comment 76 --body-file diary.md
    python3 scripts/supervisor-token.py -- gh issue edit 408 --add-label agent:ready
    python3 scripts/supervisor-token.py -- gh pr comment 71 --body-file retry.md

The supervisor's role app (ADR 0005, P5-4): [apps.supervisor] of devloop/config.toml. It signs
the supervisor's commands, labels, issues and diary; devloop recognises it as the supervisor
only with [roles.supervisor] as a table. scripts/agent-token.py does not expose it, and devloop
never reads its key: the key stays in the key_dir of the supervisor's system user
($DEVLOOP_APP_KEY_DIR, else apps.key_dir of the configuration), not in devloop's.

Without --perm the token gets issues=write and pull_requests=write: enough for comments and
labels. Only issues and pull_requests (read or write) and metadata=read may be asked for: the
supervisor neither implements nor merges, and its app has no contents permission.

The token reaches the command through GH_TOKEN only; it is never printed.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloop.apps import SUPERVISOR, AppAuth, find_key  # noqa: E402
from devloop.config import parse_config, read_toml  # noqa: E402

DEFAULT = {'issues': 'write', 'pull_requests': 'write'}
ALLOWED = {'issues': {'read', 'write'}, 'pull_requests': {'read', 'write'},
           'metadata': {'read'}}


def load_config(path):
    """The configuration of ``path`` for ``repo`` and ``[apps]``, the only keys the token needs.
    Since ADR 0006, R3 the facts of the project (``test_cmd`` among them) are in the project
    file on the host, not in ``path``: an empty ``test_cmd`` stands in for it when ``path`` has
    none, and the token never uses it."""
    return parse_config({'test_cmd': '', **read_toml(path)})


def parse(argv):
    """``(permissions, command)`` from the command line, or exit with the usage."""
    if '--' not in argv or (argv and argv[0] in {'-h', '--help'}):
        raise SystemExit(__doc__.strip())
    split = argv.index('--')
    options, command = argv[:split], argv[split + 1:]
    if not command:
        raise SystemExit(__doc__.strip())
    permissions = {}
    while options:
        if options[0] != '--perm' or len(options) < 2 or '=' not in options[1]:
            raise SystemExit(f'supervisor-token: unexpected option {options[0]!r}; use '
                             '--perm NAME=LEVEL')
        name, level = options[1].split('=', 1)
        if level not in ALLOWED.get(name, ()):
            allowed = ', '.join(f'{k}={"|".join(sorted(v))}' for k, v in ALLOWED.items())
            raise SystemExit(f'supervisor-token: permission {name}={level} is not allowed '
                             f'({allowed})')
        permissions[name] = level
        options = options[2:]
    return permissions or dict(DEFAULT), command


def main(argv):
    permissions, command = parse(argv)
    cfg = load_config(REPO_ROOT / 'devloop/config.toml')
    configured = cfg.apps.get(SUPERVISOR)
    if configured is None:
        raise SystemExit('supervisor-token: no [apps.supervisor] in devloop/config.toml')
    # Only the supervisor's key is read, from the key_dir of the user running this script.
    key_dir = os.environ.get('DEVLOOP_APP_KEY_DIR') or cfg.app_key_dir
    app = AppAuth(configured.app_id, configured.slug, find_key(key_dir, configured.slug), cfg.repo)
    env = {k: v for k, v in os.environ.items()
           if k not in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN')}
    env['GH_TOKEN'] = app.token(permissions)
    return subprocess.run(command, env=env).returncode


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
