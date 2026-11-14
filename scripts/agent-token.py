#!/usr/bin/env python3
"""Run a command as an agent's GitHub App, with a token limited to the permissions it needs.

Usage:
    python3 scripts/agent-token.py <actor> [--perm NAME=LEVEL ...] -- <command> [args...]

Examples:
    python3 scripts/agent-token.py claude -- gh pr comment 70 --body-file reply.md
    python3 scripts/agent-token.py codex --perm pull_requests=write -- \\
        gh api -X POST repos/brushtyler/ssdlc/pulls/71/reviews --input review.json

Actors: claude, codex, agy. The dispatcher's app is reserved to devloop: devloop trusts some of
the markers it reads only when the dispatcher wrote them.

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

from devloop.apps import Identities  # noqa: E402
from devloop.config import load_config  # noqa: E402

ACTORS = ('claude', 'codex', 'agy')
DEFAULT = {'issues': 'write', 'pull_requests': 'write'}
ALLOWED = {'issues': {'read', 'write'}, 'pull_requests': {'read', 'write'},
           'contents': {'read'}, 'metadata': {'read'}, 'checks': {'read'},
           'statuses': {'read'}, 'actions': {'read'}}


def parse(argv):
    """``(actor, permissions, command)`` from the command line, or exit with the usage."""
    if '--' not in argv or argv[0] in {'-h', '--help'}:
        raise SystemExit(__doc__.strip())
    split = argv.index('--')
    head, command = argv[:split], argv[split + 1:]
    if not head or not command:
        raise SystemExit(__doc__.strip())
    actor, options = head[0].lower(), head[1:]
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
    app = Identities.from_config(load_config(REPO_ROOT / 'devloop/config.toml')).apps.get(actor)
    if app is None:
        raise SystemExit(f'agent-token: no GitHub App configured for {actor!r} in devloop/config.toml')
    env = {k: v for k, v in os.environ.items()
           if k not in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN')}
    env['GH_TOKEN'] = app.token(permissions)
    return subprocess.run(command, env=env).returncode


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
