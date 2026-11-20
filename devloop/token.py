"""``devloop token <actor> [--perm NAME=LEVEL ...] -- <command> [args...]`` (ADR 0006, R6).

Runs a command as an agent's GitHub App, with an installation token limited to the permissions
it needs. The repository and the app are those of the configuration of the project devloop runs
with: ``repo`` and ``[apps]`` of the local overlay of its ``state_dir``, never of the
``config.toml`` beside a checkout. Run it through the launcher (``scripts/devloop-pinned
[--state-dir DIR] token ...``, or ``scripts/agent-token.py``, the thin wrapper that calls it),
so that it runs from the pinned copy with the project file and the overlay of that state_dir.

Actors: claude, codex, agy. The dispatcher's app is reserved to devloop: devloop trusts some of
the markers it reads only when the dispatcher wrote them. So is the merger's app, the only one
with contents=write, used only for devloop's merges, and the implementer's role app (ADR 0005,
P5-3): only devloop commits, pushes, opens PRs and writes the records of fixes with it. The
supervisor's role app is not exposed either (ADR 0005, P5-4): no review or interactive session
may write a command as the supervisor, who mints its tokens with scripts/supervisor-token.py.

Without --perm the token gets issues=write and pull_requests=write: enough for comments,
reviews and labels, not for pushing, merging or changing the repository. contents=write and any
other write permission are refused: pushes and merges are operator actions.

Only the actor's key is read, from ``$DEVLOOP_APP_KEY_DIR`` or ``[apps] key_dir`` of the
project: each repository has its own set of apps and its own key_dir (ADR 0006, D3). The token
reaches the command through GH_TOKEN only; it is never printed.
"""

import os
import subprocess

from .apps import DISPATCHER, IMPLEMENTER, MERGER, SUPERVISOR, AppAuth, find_key

ACTORS = ('claude', 'codex', 'agy')
# devloop's own apps, never exposed here.
RESERVED = (DISPATCHER, MERGER, IMPLEMENTER)
DEFAULT = {'issues': 'write', 'pull_requests': 'write'}
ALLOWED = {'issues': {'read', 'write'}, 'pull_requests': {'read', 'write'},
           'contents': {'read'}, 'metadata': {'read'}, 'checks': {'read'},
           'statuses': {'read'}, 'actions': {'read'}}
# The credentials of the caller, never passed on beside the app's token.
CREDENTIALS = ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN')
# What the launcher sets for devloop itself (``scripts/devloop-pinned``), not for the command.
LAUNCHER_ENV = ('PYTHONPATH', 'DEVLOOP_STATE_DIR')


class TokenError(Exception):
    pass


def actor_of(name):
    """The actor ``name`` names, in lower case, or ``TokenError`` for one this command does not
    expose."""
    actor = name.lower()
    if actor in RESERVED:
        raise TokenError(f'the {actor} app is reserved to devloop')
    if actor == SUPERVISOR:
        raise TokenError('the supervisor app is not exposed here; the supervisor uses '
                         'scripts/supervisor-token.py')
    if actor not in ACTORS:
        raise TokenError(f'unknown actor {actor!r}; use one of {", ".join(ACTORS)}')
    return actor


def permissions_of(values):
    """The permissions of the ``NAME=LEVEL`` strings ``values``, ``DEFAULT`` without any, or
    ``TokenError`` for one that is not allowed."""
    permissions = {}
    for value in values:
        if '=' not in value:
            raise TokenError(f'unexpected permission {value!r}; use --perm NAME=LEVEL')
        name, level = value.split('=', 1)
        if level not in ALLOWED.get(name, ()):
            allowed = ', '.join(f'{k}={"|".join(sorted(v))}' for k, v in ALLOWED.items())
            raise TokenError(f'permission {name}={level} is not allowed ({allowed})')
        permissions[name] = level
    return permissions or dict(DEFAULT)


def run(cfg, actor, permissions, command, *, environ=None, runner=None, auth=None):
    """Run ``command`` with a token of the app of ``actor`` in ``cfg`` (the configuration of
    the project) on ``cfg.repo``, limited to ``permissions``; its exit code. ``TokenError``
    when the project has no app for ``actor``; ``OSError`` when its key cannot be read.
    ``environ`` (default: this process's), ``runner`` (``subprocess.run``) and ``auth``
    (``AppAuth``) are there for the tests."""
    environ = os.environ if environ is None else environ
    runner = runner or subprocess.run
    auth = auth or AppAuth
    configured = cfg.apps.get(actor)
    if configured is None:
        raise TokenError(f'no GitHub App configured for {actor!r} in [apps] of the '
                         f'configuration of {cfg.repo} (the local overlay of its state_dir)')
    key_dir = environ.get('DEVLOOP_APP_KEY_DIR') or cfg.app_key_dir
    app = auth(configured.app_id, configured.slug, find_key(key_dir, configured.slug), cfg.repo)
    env = {k: v for k, v in environ.items() if k not in CREDENTIALS + LAUNCHER_ENV}
    env['GH_TOKEN'] = app.token(permissions)
    return runner(command, env=env).returncode
