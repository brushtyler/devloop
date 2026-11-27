#!/usr/bin/env python3
"""Clone, update or check the clone of a role of the managed repository with the token of the
role's app (#717): the clone of a private managed repository, for the supervisor and the
coordinator.

Usage, through the token script of the role, with contents=read asked explicitly:
    python3 scripts/supervisor-token.py --state-dir <state_dir> --perm contents=read -- \\
        python3 scripts/role-git.py {clone|fetch|check} <clone>
    python3 scripts/coordinator-token.py --state-dir <state_dir> --perm contents=read -- \\
        python3 scripts/role-git.py {clone|fetch|check} <clone>

- ``clone DEST``: the first clone, into DEST (absent or empty), with ``origin`` at
  ``https://github.com/<repo>.git``;
- ``fetch CLONE``: the branches of the repository into the remote branches of ``origin`` of
  CLONE;
- ``check CLONE``: whether the repository reads with the token from CLONE, writing nothing
  (``git ls-remote``; ``ops/host/check-host.sh``).

The repository is ``GH_REPO`` and the token ``GH_TOKEN``, both set by the token script: the
project's ``repo`` and a token of the role's app. The token goes to git with the defences of the
fetches of devloop (#716, ``devloop.workspace.app_auth_env``): only in the environment of that
one git command, never in the clone's config nor on the command line; credential helpers reset,
askpass programs and SSH off, only the HTTPS transport; a URL rewrite (``insteadOf``,
``pushInsteadOf``) that matches the URL is refused. The token is never printed.
"""

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloop.workspace import WorkspaceError, clone, fetch_clone, reach  # noqa: E402

ACTIONS = {'clone': clone, 'fetch': fetch_clone, 'check': reach}
# owner/name of GitHub, as ops/lib.sh checks REPO.
REPO = re.compile(r'^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$')


def main(argv, actions=ACTIONS):
    """Run the action of ``argv`` (``ACTIONS``); returns 0 or exits with a message."""
    if len(argv) != 2 or argv[0] not in actions:
        raise SystemExit(__doc__.strip())
    action, path = argv
    token = os.environ.get('GH_TOKEN', '')
    repo = os.environ.get('GH_REPO', '').strip().strip('/')
    if repo.lower().startswith('github.com/'):
        repo = repo[len('github.com/'):]
    if not token or not REPO.match(repo):
        raise SystemExit('role-git: no GH_TOKEN or GH_REPO: run it through '
                         'scripts/supervisor-token.py or scripts/coordinator-token.py with '
                         '--perm contents=read')
    try:
        actions[action](f'https://github.com/{repo}.git', token, path)
    except WorkspaceError as exc:
        raise SystemExit(f'role-git: {exc}') from exc
    print(f'role-git: {action} of {repo}: done')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
