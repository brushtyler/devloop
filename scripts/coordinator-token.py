#!/usr/bin/env python3
"""Run a command as the coordinator's GitHub App, with a token limited to the permissions it needs.

Usage:
    python3 scripts/coordinator-token.py [--state-dir DIR] [--perm NAME=LEVEL ...] -- \\
        <command> [args...]

Examples:
    DEVLOOP_APP_KEY_DIR=/home/coordinator/.ghapps python3 scripts/coordinator-token.py \\
        --state-dir /home/devloop/ssdlc/.ssdlc/devloop -- gh issue comment 76 --body-file x.md
    DEVLOOP_APP_KEY_DIR=/home/coordinator/.ghapps python3 scripts/coordinator-token.py \\
        --state-dir /home/devloop/ssdlc/.ssdlc/devloop --perm actions=read -- \\
        gh run view 123 --log-failed

The coordinator's role app (#528, #549; ADR 0002, precision of 2026-11-22) of the project whose
state_dir is DIR (default: .ssdlc/devloop of this clone): ``repo`` and ``[apps.coordinator]`` of
the local overlay of that state_dir, ``config.local.toml``, set over ``devloop/config.toml`` of
this clone (ADR 0006, R6). The project file cannot hold them (its schema is closed), so it is not
read. It signs the coordinator's commands, comments and labels; devloop counts them, within the
closed perimeter of the coordinator, only with [roles.coordinator]. scripts/agent-token.py does
not expose it, and devloop never reads its key: the key stays in the key_dir of the
coordinator's system user ($DEVLOOP_APP_KEY_DIR, else apps.key_dir of the configuration), not in
devloop's. apps.key_dir of the overlay is devloop's key_dir: the coordinator always sets
DEVLOOP_APP_KEY_DIR to its own (COORDINATOR_KEY_DIR of ops/params.env).

Without --perm the token gets issues=write and pull_requests=write: enough for commands,
comments and labels. Only issues and pull_requests (read or write), and actions, checks,
statuses and metadata read, for the diagnosis of the CI, may be asked for: the coordinator
neither implements nor merges, and its app has no contents permission.

The token reaches the command through GH_TOKEN only; it is never printed.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloop.apps import COORDINATOR, AppAuth, find_key  # noqa: E402
from devloop.config import (DEFAULT_CONFIG, ConfigError, apply_overlay, parse_config,  # noqa: E402
                            read_toml)

DEFAULT = {'issues': 'write', 'pull_requests': 'write'}
# The permissions of ops/github/apps/coordinator.json, none above its level.
ALLOWED = {'issues': {'read', 'write'}, 'pull_requests': {'read', 'write'},
           'actions': {'read'}, 'checks': {'read'}, 'statuses': {'read'},
           'metadata': {'read'}}
# The launcher's default state_dir, of this clone.
STATE = REPO_ROOT / '.ssdlc' / 'devloop'
LOCAL_CONFIG = 'config.local.toml'
STATE_DIR = '--state-dir'
# The credentials of the caller, never passed on beside the app's token.
CREDENTIALS = ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN')


def load_config(state):
    """The configuration of the project of ``state`` for ``repo`` and ``[apps]``, the only keys
    the token needs: ``devloop/config.toml`` of this clone with the overlay of ``state`` set
    over it (ADR 0006, R6). The facts of the project (``test_cmd`` among them) are in its
    project file: an empty ``test_cmd`` stands in for it, and the token never uses it."""
    overlay = Path(state) / LOCAL_CONFIG
    if not overlay.is_file():
        raise SystemExit(f'coordinator-token: no local overlay {overlay}: the project\'s repo and '
                         f'[apps.coordinator] are there; pass {STATE_DIR} with devloop\'s '
                         'state_dir of the project')
    try:
        data = apply_overlay(read_toml(DEFAULT_CONFIG), read_toml(overlay, 'local config'))
        return parse_config({'test_cmd': '', **data})
    except ConfigError as exc:
        raise SystemExit(f'coordinator-token: invalid config: {exc}') from exc


def parse(argv):
    """``(state, permissions, command)`` from the command line, or exit with the usage."""
    if '--' not in argv or (argv and argv[0] in {'-h', '--help'}):
        raise SystemExit(__doc__.strip())
    split = argv.index('--')
    options, command = argv[:split], argv[split + 1:]
    if not command:
        raise SystemExit(__doc__.strip())
    state, permissions = STATE, {}
    while options:
        if options[0] == STATE_DIR and len(options) >= 2:
            state, options = Path(options[1]), options[2:]
            continue
        if options[0] != '--perm' or len(options) < 2 or '=' not in options[1]:
            raise SystemExit(f'coordinator-token: unexpected option {options[0]!r}; use '
                             f'{STATE_DIR} DIR or --perm NAME=LEVEL')
        name, level = options[1].split('=', 1)
        if level not in ALLOWED.get(name, ()):
            allowed = ', '.join(f'{k}={"|".join(sorted(v))}' for k, v in ALLOWED.items())
            raise SystemExit(f'coordinator-token: permission {name}={level} is not allowed '
                             f'({allowed})')
        permissions[name] = level
        options = options[2:]
    return state, permissions or dict(DEFAULT), command


def main(argv):
    state, permissions, command = parse(argv)
    cfg = load_config(state)
    configured = cfg.apps.get(COORDINATOR)
    if configured is None:
        raise SystemExit(f'coordinator-token: no [apps.coordinator] in the overlay of {state}')
    # Only the coordinator's key is read, from the key_dir of the user running this script.
    key_dir = os.environ.get('DEVLOOP_APP_KEY_DIR') or cfg.app_key_dir
    app = AppAuth(configured.app_id, configured.slug, find_key(key_dir, configured.slug), cfg.repo)
    env = {k: v for k, v in os.environ.items() if k not in CREDENTIALS}
    env['GH_TOKEN'] = app.token(permissions)
    return subprocess.run(command, env=env).returncode


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
