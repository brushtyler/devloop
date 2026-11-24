#!/usr/bin/env python3
"""Run a command as the supervisor's GitHub App, with a token limited to the permissions it needs.

Usage:
    python3 scripts/supervisor-token.py [--state-dir DIR] [--perm NAME=LEVEL ...] \\
        [--allow-host-details] -- <command> [args...]

Examples:
    python3 scripts/supervisor-token.py --state-dir /home/devloop/devloop/.devloop -- \\
        gh issue comment 76 --body-file diary.md
    python3 scripts/supervisor-token.py --state-dir /home/devloop/devloop/.devloop -- \\
        gh issue edit 408 --add-label agent:ready

The supervisor's role app (ADR 0005, P5-4) of the project whose state_dir is DIR (default:
.devloop of this clone): ``repo`` and ``[apps.supervisor]`` of the local overlay of that
state_dir, ``config.local.toml``, where the project's identities are since ADR 0006, R6, set over
``devloop/config.toml`` of this clone. The project file cannot hold them (its schema is closed),
so it is not read. It signs the supervisor's commands, labels, issues and diary; devloop
recognises it as the supervisor only with [roles.supervisor] as a table. scripts/agent-token.py
does not expose it, and devloop never reads its key: the key stays in the key_dir of the
supervisor's system user ($DEVLOOP_APP_KEY_DIR, else apps.key_dir of the configuration), not in
devloop's.

Without --perm the token gets issues=write and pull_requests=write: enough for comments and
labels. Only issues and pull_requests (read or write) and metadata=read may be asked for: the
supervisor neither implements nor merges, and its app has no contents permission.

The token reaches the command through GH_TOKEN only; it is never printed.

The command also gets GH_REPO set to the project's ``repo`` (#571): the token is valid only
there, and ``gh`` would otherwise pick the repository of the current directory's clone, the
source of devloop for a project with the source apart (SOURCE_REPO). A GH_REPO of the caller
naming another repository is refused before any key is read.

Before any key is read, the text the command publishes (``--body``, ``--title``,
``--body-file``, their short forms, standard input with ``-``; for ``gh api``, ``--input`` and the
fields, a file read with ``-F key=@file`` and standard input with ``key=@-`` included) is checked
(#583, ``devloop.redact``): one
that still holds an absolute path of the host (under /home/, /etc/, /var/lib/, /usr/local/lib/,
/run/, /root/) or the name of a key file is refused. --allow-host-details publishes it anyway;
each use is written on standard error and in the system log (syslog, devloop-publication).
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloop.apps import SUPERVISOR, AppAuth, find_key  # noqa: E402
from devloop.config import (DEFAULT_CONFIG, ConfigError, apply_overlay, parse_config,  # noqa: E402
                            read_toml)
from devloop.redact import allow_option, publication_check  # noqa: E402

DEFAULT = {'issues': 'write', 'pull_requests': 'write'}
ALLOWED = {'issues': {'read', 'write'}, 'pull_requests': {'read', 'write'},
           'metadata': {'read'}}
# The launcher's default state_dir, of this clone.
STATE = REPO_ROOT / '.devloop'
LOCAL_CONFIG = 'config.local.toml'
STATE_DIR = '--state-dir'


def load_config(state):
    """The configuration of the project of ``state`` for ``repo`` and ``[apps]``, the only keys
    the token needs: ``devloop/config.toml`` of this clone with the overlay of ``state`` set
    over it (ADR 0006, R6). The facts of the project (``test_cmd`` among them) are in its
    project file: an empty ``test_cmd`` stands in for it, and the token never uses it."""
    overlay = Path(state) / LOCAL_CONFIG
    if not overlay.is_file():
        raise SystemExit(f'supervisor-token: no local overlay {overlay}: the project\'s repo and '
                         f'[apps.supervisor] are there; pass {STATE_DIR} with devloop\'s '
                         'state_dir of the project')
    try:
        data = apply_overlay(read_toml(DEFAULT_CONFIG), read_toml(overlay, 'local config'))
        return parse_config({'test_cmd': '', **data})
    except ConfigError as exc:
        raise SystemExit(f'supervisor-token: invalid config: {exc}') from exc


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
            raise SystemExit(f'supervisor-token: unexpected option {options[0]!r}; use '
                             f'{STATE_DIR} DIR or --perm NAME=LEVEL')
        name, level = options[1].split('=', 1)
        if level not in ALLOWED.get(name, ()):
            allowed = ', '.join(f'{k}={"|".join(sorted(v))}' for k, v in ALLOWED.items())
            raise SystemExit(f'supervisor-token: permission {name}={level} is not allowed '
                             f'({allowed})')
        permissions[name] = level
        options = options[2:]
    return state, permissions or dict(DEFAULT), command


def project_repo(repo):
    """``repo`` of the project as GH_REPO for the command (#571), or exit when the caller's
    GH_REPO names another repository: the token would not reach it. GH_REPO may carry the host
    (``github.com/OWNER/REPO``)."""
    chosen = os.environ.get('GH_REPO', '').strip().strip('/')
    if chosen.lower().startswith('github.com/'):
        chosen = chosen[len('github.com/'):]
    if chosen and chosen.lower() != repo.lower():
        raise SystemExit(f'supervisor-token: GH_REPO={chosen} is not the project\'s repository '
                         f'{repo}, the only one the token reaches; unset it or pass the '
                         f'{STATE_DIR} of that project')
    return repo


def main(argv):
    allow, argv = allow_option(argv)
    state, permissions, command = parse(argv)
    # The last check before publishing (#583): no path of the host nor key file in the text.
    stdin = publication_check('supervisor-token', command, allow)
    cfg = load_config(state)
    repo = project_repo(cfg.repo)
    configured = cfg.apps.get(SUPERVISOR)
    if configured is None:
        raise SystemExit(f'supervisor-token: no [apps.supervisor] in the overlay of {state}')
    # Only the supervisor's key is read, from the key_dir of the user running this script.
    key_dir = os.environ.get('DEVLOOP_APP_KEY_DIR') or cfg.app_key_dir
    app = AppAuth(configured.app_id, configured.slug, find_key(key_dir, configured.slug), cfg.repo)
    env = {k: v for k, v in os.environ.items()
           if k not in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN')}
    env['GH_TOKEN'] = app.token(permissions)
    # gh acts on the project's repository, not on the origin of the current clone (#571).
    env['GH_REPO'] = repo
    if stdin is not None:  # the body read from standard input for the check, passed on
        return subprocess.run(command, env=env, input=stdin, text=True).returncode
    return subprocess.run(command, env=env).returncode


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
