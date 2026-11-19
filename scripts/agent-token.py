#!/usr/bin/env python3
"""Run a command as an agent's GitHub App, with a token limited to the permissions it needs.

Usage:
    python3 scripts/agent-token.py <actor> [--perm NAME=LEVEL ...] -- <command> [args...]

Examples:
    python3 scripts/agent-token.py claude -- gh pr comment 70 --body-file reply.md
    python3 scripts/agent-token.py codex --perm pull_requests=write -- \\
        gh api -X POST repos/brushtyler/ssdlc/pulls/71/reviews --input review.json

Actors: claude, codex, agy. The dispatcher's app is reserved to devloop: devloop trusts some of
the markers it reads only when the dispatcher wrote them. So is the merger's app, the only one
with contents=write, used only for devloop's merges. The supervisor's role app is not exposed
either (ADR 0005, P5-4): no review or interactive session may write a command as the
supervisor. The supervisor gets its token with scripts/supervisor-token.py. Nor is the
implementer's role app (ADR 0005, P5-3): only devloop commits, pushes, opens PRs and writes the
records of fixes with it, and no session may write a record as the implementer.

Without --perm the token gets issues=write and pull_requests=write: enough for comments,
reviews and labels, not for pushing, merging or changing the repository. contents=write and
any other write permission are refused: pushes and merges are operator actions.

The token reaches the command through GH_TOKEN only; it is never printed.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloop.apps import AppAuth, find_key  # noqa: E402
from devloop.config import parse_config, read_toml  # noqa: E402

ACTORS = ('claude', 'codex', 'agy')
# devloop's own apps, never exposed here: the implementer's role app (#407) among them.
RESERVED = ('dispatcher', 'merger', 'implementer')
SUPERVISOR = 'supervisor'  # the supervisor's role app: scripts/supervisor-token.py only
DEFAULT = {'issues': 'write', 'pull_requests': 'write'}
ALLOWED = {'issues': {'read', 'write'}, 'pull_requests': {'read', 'write'},
           'contents': {'read'}, 'metadata': {'read'}, 'checks': {'read'},
           'statuses': {'read'}, 'actions': {'read'}}


def load_config(path):
    """The configuration of ``path`` for ``repo`` and ``[apps]``, the only keys the token needs.
    Since ADR 0006, R3 the facts of the project (``test_cmd`` among them) are in the project
    file on the host, not in ``path``: an empty ``test_cmd`` stands in for it when ``path`` has
    none, and the token never uses it."""
    return parse_config({'test_cmd': '', **read_toml(path)})


def parse(argv):
    """``(actor, permissions, command)`` from the command line, or exit with the usage."""
    if '--' not in argv or argv[0] in {'-h', '--help'}:
        raise SystemExit(__doc__.strip())
    split = argv.index('--')
    head, command = argv[:split], argv[split + 1:]
    if not head or not command:
        raise SystemExit(__doc__.strip())
    actor, options = head[0].lower(), head[1:]
    if actor in RESERVED:
        raise SystemExit(f'agent-token: the {actor} app is reserved to devloop')
    if actor == SUPERVISOR:
        raise SystemExit('agent-token: the supervisor app is not exposed here; the supervisor '
                         'uses scripts/supervisor-token.py')
    if actor not in ACTORS:
        raise SystemExit(f'agent-token: unknown actor {actor!r}; use one of {", ".join(ACTORS)}')
    permissions = {}
    while options:
        if options[0] != '--perm' or len(options) < 2 or '=' not in options[1]:
            raise SystemExit(f'agent-token: unexpected option {options[0]!r}; use --perm NAME=LEVEL')
        name, level = options[1].split('=', 1)
        if level not in ALLOWED.get(name, ()):
            allowed = ', '.join(f'{k}={"|".join(sorted(v))}' for k, v in ALLOWED.items())
            raise SystemExit(f'agent-token: permission {name}={level} is not allowed ({allowed})')
        permissions[name] = level
        options = options[2:]
    return actor, permissions or dict(DEFAULT), command


def main(argv):
    actor, permissions, command = parse(argv)
    cfg = load_config(REPO_ROOT / 'devloop/config.toml')
    configured = cfg.apps.get(actor)
    if configured is None:
        raise SystemExit(f'agent-token: no GitHub App configured for {actor!r} in devloop/config.toml')
    # Only this actor's key is read: the reserved apps' keys need not be on this host.
    key_dir = os.environ.get('DEVLOOP_APP_KEY_DIR') or cfg.app_key_dir
    app = AppAuth(configured.app_id, configured.slug, find_key(key_dir, configured.slug), cfg.repo)
    env = {k: v for k, v in os.environ.items()
           if k not in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN')}
    env['GH_TOKEN'] = app.token(permissions)
    return subprocess.run(command, env=env).returncode


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
