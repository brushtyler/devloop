"""Dispatcher configuration (TOML)."""

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .policy import MAX_LEVEL, MIN_LEVEL, ReviewPolicy

DEFAULT_CONFIG = Path(__file__).resolve().parent / 'config.toml'
# The root of the running dispatcher's installation (a pinned checkout, see docs/devloop.md):
# relative ``sensitive_path_lists`` are read from here, never from a PR branch.
INSTALL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SENSITIVE_LISTS = ('schemas/recovery-grader-baseline.json',)
AGENT_KINDS = ('claude', 'codex', 'agy')
STALL_ACTIONS = ('warn', 'abort')


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AgentConfig:
    key: str
    kind: str
    display_name: str
    model: str
    model_label: str
    effort: str
    version_cmd: tuple
    write_args: tuple = ()
    review_args: tuple = ()
    commit_trailer: str = ''


@dataclass(frozen=True)
class AppConfig:
    """A GitHub App: its id and slug. The private key is found by slug in ``app_key_dir``."""
    app_id: int
    slug: str


@dataclass(frozen=True)
class Labels:
    ready: str = 'agent:ready'
    working: str = 'agent:working'
    review: str = 'agent:review'
    changes: str = 'agent:changes'
    ready_to_merge: str = 'agent:ready-to-merge'
    needs_human: str = 'agent:needs-human'

    def all(self):
        return (self.ready, self.working, self.review, self.changes,
                self.ready_to_merge, self.needs_human)


@dataclass(frozen=True)
class Config:
    repo: str
    base_branch: str
    branch_prefix: str
    state_dir: str
    test_cmd: str
    language: str
    implementer: str
    reviewer: str  # the first of ``reviewers``, recorded in the PR marker
    agents: dict
    trusted_users: tuple
    # The reviewer pool, in order (``roles.reviewers``; ``roles.reviewer`` is a pool of one).
    reviewers: tuple = ()
    max_open_prs: int = 2
    max_review_rounds: int = 3
    max_actions_per_tick: int = 3
    implement_test_retries: int = 1
    agent_timeout_seconds: int = 3600
    test_timeout_seconds: int = 3600
    agent_command_timeout_seconds: int = 600
    agent_stall_seconds: int = 900
    agent_stall_action: str = 'warn'
    blocked_labels: tuple = ('bloccata',)
    priority_labels: tuple = ('prerequisito',)
    protected_paths: tuple = ('.github/',)
    apps: dict = field(default_factory=dict)
    app_key_dir: str = '~/.ghapps'
    labels: Labels = field(default_factory=Labels)
    review_policy: ReviewPolicy = field(default_factory=ReviewPolicy)

    def agent(self, key):
        return self.agents[key]

    def pool(self):
        """The reviewer pool, in configuration order."""
        return self.reviewers or (self.reviewer,)


def _require(table, key, kind, where):
    if key not in table:
        raise ConfigError(f'{where}: missing {key!r}')
    value = table[key]
    if not isinstance(value, kind) or isinstance(value, bool) and kind is int:
        raise ConfigError(f'{where}.{key}: expected {kind.__name__}')
    return value


def _str_list(table, key, where, default=()):
    value = table.get(key, list(default))
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f'{where}.{key}: expected a list of strings')
    return tuple(value)


def _listed_files(name, root):
    """The keys of the ``files`` object of the JSON file ``name`` (relative to ``root``)."""
    where = f'review_policy.sensitive_path_lists: {name}'
    path = Path(name) if Path(name).is_absolute() else Path(root) / name
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ConfigError(f'{where}: {exc}') from exc
    files = data.get('files') if isinstance(data, dict) else None
    if not isinstance(files, dict) or not all(isinstance(k, str) and k for k in files):
        raise ConfigError(f'{where}: expected an object "files" keyed by path')
    return tuple(files)


def parse_review_policy(table, root, reviewers=()):
    """The policy of ``[review_policy]``. A list file that cannot be read stops devloop: the
    policy never runs with fewer sensitive paths than configured. ``reviewers`` is the pool
    as ``ReviewPolicy.reviewers`` records it."""
    if not isinstance(table, dict):
        raise ConfigError('review_policy: expected a table')
    where = 'review_policy'
    default = ReviewPolicy()
    level = table.get('default_level', default.default_level)
    if (not isinstance(level, int) or isinstance(level, bool)
            or not MIN_LEVEL <= level <= MAX_LEVEL):
        raise ConfigError(f'{where}.default_level: must be an integer from {MIN_LEVEL} to '
                          f'{MAX_LEVEL}')
    label = table.get('label', default.label)
    if not isinstance(label, str) or not label.strip():
        raise ConfigError(f'{where}.label: expected a non-empty string')
    paths = _str_list(table, 'sensitive_paths', where, default.sensitive_paths)
    files = []
    for name in _str_list(table, 'sensitive_path_lists', where, DEFAULT_SENSITIVE_LISTS):
        files += _listed_files(name, root)
    return ReviewPolicy(default_level=level, label=label, sensitive_paths=paths,
                        sensitive_files=tuple(files), reviewers=tuple(reviewers))


def _reviewer_pool(roles, agents, implementer):
    """``roles.reviewers``, or ``roles.reviewer`` as a pool of one: known agents, no repeats,
    never the implementer."""
    if 'reviewers' in roles:
        if 'reviewer' in roles:
            raise ConfigError('roles: set either reviewer or reviewers, not both')
        pool = _str_list(roles, 'reviewers', 'roles')
        if not pool:
            raise ConfigError('roles.reviewers: at least one agent is required')
        if len(set(pool)) != len(pool):
            raise ConfigError('roles.reviewers: an agent is listed twice')
    else:
        pool = (_require(roles, 'reviewer', str, 'roles'),)
    for key in pool:
        if key not in agents:
            raise ConfigError(f'roles.reviewers: unknown agent {key!r}')
        if key == implementer:
            raise ConfigError('roles: the reviewer must be a different agent from the implementer')
    return pool


def parse_config(data, root=INSTALL_ROOT):
    """``root`` is where relative ``review_policy.sensitive_path_lists`` are read."""
    agents = {}
    for key, table in (data.get('agents') or {}).items():
        where = f'agents.{key}'
        kind = _require(table, 'kind', str, where)
        if kind not in AGENT_KINDS:
            raise ConfigError(f'{where}.kind: must be one of {AGENT_KINDS}')
        agents[key] = AgentConfig(
            key=key, kind=kind,
            display_name=_require(table, 'display_name', str, where),
            model=_require(table, 'model', str, where),
            model_label=_require(table, 'model_label', str, where),
            effort=_require(table, 'effort', str, where),
            version_cmd=_str_list(table, 'version_cmd', where, (kind, '--version')),
            write_args=_str_list(table, 'write_args', where),
            review_args=_str_list(table, 'review_args', where),
            commit_trailer=table.get('commit_trailer', ''))
    roles = data.get('roles') or {}
    implementer = _require(roles, 'implementer', str, 'roles')
    if implementer not in agents:
        raise ConfigError(f'roles.implementer: unknown agent {implementer!r}')
    pool = _reviewer_pool(roles, agents, implementer)
    trusted = _str_list(data, 'trusted_users', 'config')
    if not trusted:
        raise ConfigError('trusted_users: at least one GitHub login is required')
    if any(login.endswith('[bot]') for login in trusted):
        raise ConfigError('trusted_users: bots and apps cannot approve work')
    apps_table = dict(data.get('apps') or {})
    key_dir = apps_table.pop('key_dir', '~/.ghapps')
    if not isinstance(key_dir, str):
        raise ConfigError('apps.key_dir: expected str')
    apps = {}
    for actor, table in apps_table.items():
        where = f'apps.{actor}'
        if actor != 'dispatcher' and actor not in agents:
            raise ConfigError(f'{where}: not the dispatcher nor a configured agent')
        if not isinstance(table, dict):
            raise ConfigError(f'{where}: expected a table')
        apps[actor] = AppConfig(app_id=_require(table, 'app_id', int, where),
                                slug=_require(table, 'slug', str, where))
    if len({app.app_id for app in apps.values()}) != len(apps):
        raise ConfigError('apps: each actor needs its own app')
    ints = {}
    for key in ('max_open_prs', 'max_review_rounds', 'max_actions_per_tick',
                'implement_test_retries', 'agent_timeout_seconds', 'test_timeout_seconds',
                'agent_command_timeout_seconds', 'agent_stall_seconds'):
        if key in data:
            value = _require(data, key, int, 'config')
            if value < (0 if key == 'implement_test_retries' else 1):
                raise ConfigError(f'{key}: out of range')
            ints[key] = value
    if (ints.get('agent_command_timeout_seconds', Config.agent_command_timeout_seconds)
            > ints.get('test_timeout_seconds', Config.test_timeout_seconds)):
        raise ConfigError('agent_command_timeout_seconds: must not exceed test_timeout_seconds')
    stall_action = data.get('agent_stall_action', Config.agent_stall_action)
    if stall_action not in STALL_ACTIONS:
        raise ConfigError(f'agent_stall_action: must be one of {STALL_ACTIONS}')
    return Config(
        repo=_require(data, 'repo', str, 'config'),
        base_branch=data.get('base_branch', 'main'),
        branch_prefix=data.get('branch_prefix', 'agent/issue-'),
        state_dir=data.get('state_dir', '.ssdlc/devloop'),
        test_cmd=_require(data, 'test_cmd', str, 'config'),
        language=data.get('language', 'English'),
        implementer=implementer, reviewer=pool[0], reviewers=pool, agents=agents,
        trusted_users=trusted,
        blocked_labels=_str_list(data, 'blocked_labels', 'config', ('bloccata',)),
        priority_labels=_str_list(data, 'priority_labels', 'config', ('prerequisito',)),
        protected_paths=_str_list(data, 'protected_paths', 'config', ('.github/',)),
        apps=apps, app_key_dir=key_dir, agent_stall_action=stall_action,
        review_policy=parse_review_policy(
            data.get('review_policy', {}), root,
            [(k, apps[k].slug if k in apps else None, agents[k].kind, agents[k].model,
              agents[k].effort, agents[k].review_args) for k in pool]),
        **ints)


def load_config(path=None):
    path = Path(path) if path else DEFAULT_CONFIG
    with open(path, 'rb') as fh:
        return parse_config(tomllib.load(fh))
