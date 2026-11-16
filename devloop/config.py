"""Dispatcher configuration (TOML)."""

import dataclasses
import json
import re
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
# A fallback model never reasons below ``high`` (issue #121).
FALLBACK_EFFORTS = ('high', 'xhigh', 'max')
# agy model names end with their reasoning level: these suffixes are below ``high``.
AGY_LOW_SUFFIXES = ('minimal', 'low', 'medium')
# What tells a failed turn that its model is unavailable (quota or budget exhausted, model not
# available, rate limit), searched case-insensitively in the CLI's stdout and stderr.
DEFAULT_UNAVAILABLE_PATTERNS = ('RESOURCE_EXHAUSTED', 'quota', '429', 'model .* not available')
# Commands an agy turn may run besides ``test_cmd`` (issue #122): agy matches a
# ``command(<prefix>)`` rule by token prefix. The write prompts ask for no `git add` nor
# `git commit` (the dispatcher commits), so write turns get the same list as reviews.
AGY_DEFAULT_COMMANDS = ('python3 -m unittest', 'git status', 'git diff', 'git log', 'git show')
# A command made of one of these alone allows any program or git subcommand: never allowed.
BARE_COMMANDS = re.compile(r'(python[0-9.]*|bash|sh|dash|zsh|ksh|fish|env|node|perl|ruby|'
                           r'sudo|xargs|exec|eval|command|git)')
# Shell syntax, quotes and pattern characters have no place in a token-prefix rule.
UNSAFE_COMMAND_CHARS = frozenset('*?()[]{};&|<>$`\\\'"\n\r\t#~')
# Arguments that would replace or bypass the per-turn permissions devloop passes to agy.
AGY_FORBIDDEN_ARGS = ('--dangerously-skip-permissions', '--settings')


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class FallbackModel:
    """A model an agent falls back to when the ones before it are unavailable (issue #121)."""
    model: str
    model_label: str
    effort: str


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
    # Tried in order, each at most once per action, only when the models before it are
    # unavailable: a failed turn whose output matches ``unavailable_patterns`` or whose exit
    # code is in ``unavailable_exit_codes`` (issue #121).
    fallback_models: tuple = ()
    unavailable_patterns: tuple = DEFAULT_UNAVAILABLE_PATTERNS
    unavailable_exit_codes: tuple = ()
    # Set on the configurations ``chain`` derives for fallback models: the configured model.
    fallback_for: str = ''
    # agy only (issue #122): the command prefixes its write and review turns may run, passed
    # each turn as ``permissions.allow`` rules with ``--settings``.
    write_commands: tuple = ()
    review_commands: tuple = ()

    def chain(self):
        """The agent with its configured model, then with each fallback model in order: same
        key, CLI and arguments, with the fallback's model, label and effort."""
        return [self] + [dataclasses.replace(self, model=f.model, model_label=f.model_label,
                                             effort=f.effort, fallback_models=(),
                                             fallback_for=self.model)
                         for f in self.fallback_models]


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
    # The agent of the readiness check before implement (#114), or '' when it is disabled.
    preflight: str = ''
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


def _fallback_models(table, where, kind, model, effort):
    """``fallback_models``: an ordered list of ``{model, model_label, effort}`` tables. A
    missing effort is the agent's. Every effort must be ``high`` or above, and so must, for agy,
    the level its model name ends with."""
    value = table.get('fallback_models', [])
    where = f'{where}.fallback_models'
    if not isinstance(value, list):
        raise ConfigError(f'{where}: expected a list of tables')
    models, seen = [], {model}
    for position, item in enumerate(value):
        at = f'{where}[{position}]'
        if not isinstance(item, dict):
            raise ConfigError(f'{at}: expected a table with model, model_label and effort')
        unknown = set(item) - {'model', 'model_label', 'effort'}
        if unknown:
            raise ConfigError(f'{at}: unknown keys {sorted(unknown)}')
        name = _require(item, 'model', str, at)
        label = _require(item, 'model_label', str, at)
        level = item.get('effort', effort)
        if not name.strip() or not label.strip():
            raise ConfigError(f'{at}: model and model_label must not be empty')
        if level not in FALLBACK_EFFORTS:
            raise ConfigError(f'{at}.effort: {level!r} is not allowed for a fallback, use one '
                              f'of {FALLBACK_EFFORTS}')
        if kind == 'agy' and name.lower().rsplit('-', 1)[-1] in AGY_LOW_SUFFIXES:
            raise ConfigError(f'{at}.model: {name!r} reasons below high')
        if name in seen:
            raise ConfigError(f'{at}.model: {name!r} is already the agent\'s model or a fallback')
        seen.add(name)
        models.append(FallbackModel(model=name, model_label=label, effort=level))
    return tuple(models)


def _unavailable(table, where):
    """``(unavailable_patterns, unavailable_exit_codes)`` of an agent."""
    patterns = _str_list(table, 'unavailable_patterns', where, DEFAULT_UNAVAILABLE_PATTERNS)
    for pattern in patterns:
        if not pattern:
            raise ConfigError(f'{where}.unavailable_patterns: empty pattern')
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f'{where}.unavailable_patterns: {pattern!r}: {exc}') from exc
    codes = table.get('unavailable_exit_codes', [])
    if (not isinstance(codes, list)
            or not all(isinstance(c, int) and not isinstance(c, bool) and c != 0 for c in codes)):
        raise ConfigError(f'{where}.unavailable_exit_codes: expected a list of non-zero integers')
    return patterns, tuple(codes)


def _command(value, where):
    """``value`` as a token-prefix command (its tokens joined by one space), or ConfigError when
    it is empty, contains shell, quoting or pattern syntax, or is a bare interpreter, shell or
    git, which would allow any command."""
    if any(c in UNSAFE_COMMAND_CHARS for c in value):
        raise ConfigError(f'{where}: {value!r} is not a plain command prefix')
    tokens = value.split()
    if not tokens:
        raise ConfigError(f'{where}: empty command')
    if len(tokens) == 1 and BARE_COMMANDS.fullmatch(tokens[0]):
        raise ConfigError(f'{where}: {value!r} alone would allow any command')
    return ' '.join(tokens)


def _agy_commands(table, where, kind, test_cmd):
    """``(write_commands, review_commands)`` of an agent (issue #122). Only agy has them: by
    default the tokens of ``test_cmd`` followed by ``AGY_DEFAULT_COMMANDS``, for both modes.
    ``write_args`` and ``review_args`` may not bypass or replace them."""
    keys = ('write_commands', 'review_commands')
    if kind != 'agy':
        for key in keys:
            if key in table:
                raise ConfigError(f'{where}.{key}: only agy agents take command permissions')
        return (), ()
    for key in ('write_args', 'review_args'):
        for arg in _str_list(table, key, where):
            if arg.split('=', 1)[0] in AGY_FORBIDDEN_ARGS:
                raise ConfigError(f'{where}.{key}: {arg.split("=", 1)[0]} is not allowed: '
                                  'devloop passes the permissions of each turn itself')
    lists = []
    for key in keys:
        configured = key in table
        raw = _str_list(table, key, where) if configured else (test_cmd, *AGY_DEFAULT_COMMANDS)
        commands = []
        for value in raw:
            command = _command(value, f'{where}.{key}' if configured else
                               f'{where}.{key} (default from test_cmd)')
            if command not in commands:
                commands.append(command)
        lists.append(tuple(commands))
    return tuple(lists)


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
    zero_label, allowlist, checks = _zero_policy(table.get('zero', {}), label, default)
    return ReviewPolicy(default_level=level, label=label, sensitive_paths=paths,
                        sensitive_files=tuple(files), reviewers=tuple(reviewers),
                        zero_label=zero_label, zero_allowlist=allowlist, zero_checks=checks)


def _zero_policy(table, level_label, default):
    """``(label, allowlist, checks)`` of ``[review_policy.zero]`` (#107). An entry of the
    allowlist is a directory prefix or a file relative to the root, never the root itself, an
    absolute path, a ``.`` or ``..`` segment or a pattern. A check is ``{name, app_id}``."""
    where = 'review_policy.zero'
    if not isinstance(table, dict):
        raise ConfigError(f'{where}: expected a table')
    label = table.get('label', default.zero_label)
    if not isinstance(label, str) or not label.strip():
        raise ConfigError(f'{where}.label: expected a non-empty string')
    if label == level_label:
        raise ConfigError(f'{where}.label: must differ from review_policy.label')
    allowlist = _str_list(table, 'allowlist', where)
    for entry in allowlist:
        parts = entry.strip().rstrip('/').split('/')
        if (entry.startswith('/') or any(p in ('', '.', '..') for p in parts)
                or any(c in entry for c in '*?[]\\')):
            raise ConfigError(f'{where}.allowlist: invalid entry {entry!r}')
    checks = table.get('checks', [])
    if not isinstance(checks, list):
        raise ConfigError(f'{where}.checks: expected a list of {{name, app_id}} tables')
    expected = []
    for check in checks:
        if not isinstance(check, dict) or set(check) != {'name', 'app_id'}:
            raise ConfigError(f'{where}.checks: expected a list of {{name, app_id}} tables')
        name, app_id = check['name'], check['app_id']
        if (not isinstance(name, str) or not name.strip() or not isinstance(app_id, int)
                or isinstance(app_id, bool) or app_id < 1):
            raise ConfigError(f'{where}.checks: a check needs a non-empty name and an app_id')
        expected.append((name, app_id))
    if len(set(expected)) != len(expected):
        raise ConfigError(f'{where}.checks: a check is listed twice')
    return label, allowlist, tuple(expected)


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
    test_cmd = _require(data, 'test_cmd', str, 'config')
    agents = {}
    for key, table in (data.get('agents') or {}).items():
        where = f'agents.{key}'
        kind = _require(table, 'kind', str, where)
        if kind not in AGENT_KINDS:
            raise ConfigError(f'{where}.kind: must be one of {AGENT_KINDS}')
        model = _require(table, 'model', str, where)
        effort = _require(table, 'effort', str, where)
        patterns, codes = _unavailable(table, where)
        write_commands, review_commands = _agy_commands(table, where, kind, test_cmd)
        agents[key] = AgentConfig(
            key=key, kind=kind,
            display_name=_require(table, 'display_name', str, where),
            model=model,
            model_label=_require(table, 'model_label', str, where),
            effort=effort,
            version_cmd=_str_list(table, 'version_cmd', where, (kind, '--version')),
            write_args=_str_list(table, 'write_args', where),
            review_args=_str_list(table, 'review_args', where),
            commit_trailer=table.get('commit_trailer', ''),
            fallback_models=_fallback_models(table, where, kind, model, effort),
            unavailable_patterns=patterns, unavailable_exit_codes=codes,
            write_commands=write_commands, review_commands=review_commands)
    roles = data.get('roles') or {}
    implementer = _require(roles, 'implementer', str, 'roles')
    if implementer not in agents:
        raise ConfigError(f'roles.implementer: unknown agent {implementer!r}')
    pool = _reviewer_pool(roles, agents, implementer)
    preflight = ''
    if 'preflight' in data:
        preflight = _require(data, 'preflight', str, 'config')
        if preflight not in agents:
            raise ConfigError(f'preflight: unknown agent {preflight!r}')
        if preflight == implementer:
            raise ConfigError('preflight: must be a different agent from the implementer')
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
        test_cmd=test_cmd,
        language=data.get('language', 'English'),
        implementer=implementer, reviewer=pool[0], reviewers=pool, agents=agents,
        preflight=preflight,
        trusted_users=trusted,
        blocked_labels=_str_list(data, 'blocked_labels', 'config', ('bloccata',)),
        priority_labels=_str_list(data, 'priority_labels', 'config', ('prerequisito',)),
        protected_paths=_str_list(data, 'protected_paths', 'config', ('.github/',)),
        apps=apps, app_key_dir=key_dir, agent_stall_action=stall_action,
        review_policy=parse_review_policy(
            data.get('review_policy', {}), root,
            [(k, apps[k].slug if k in apps else None, agents[k].kind, agents[k].model,
              agents[k].effort, agents[k].review_args,
              tuple((f.model, f.effort) for f in agents[k].fallback_models)) for k in pool]),
        **ints)


def load_config(path=None):
    path = Path(path) if path else DEFAULT_CONFIG
    with open(path, 'rb') as fh:
        return parse_config(tomllib.load(fh))
