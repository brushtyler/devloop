"""Dispatcher configuration (TOML)."""

import dataclasses
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .localtests import MODES as LOCAL_TESTS_MODES
from .policy import MAX_LEVEL, MIN_LEVEL, ZERO_LEVEL, ReviewPolicy

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


# Merge modes (#172), from the most restrictive: ``off`` (a person merges), ``strict`` (devloop
# merges a ready PR that meets the merge directives), ``auto`` (as strict, and the supervisor may
# hold a merge).
MERGE_MODES = ('off', 'strict', 'auto')
DEFAULT_OPERATOR_ONLY = ('docs/adr/', 'AGENTS.md', 'CLAUDE.md')


@dataclass(frozen=True)
class MergePolicy:
    """``[merge]`` (#172): the mode, its overrides per review level and the paths whose PRs a
    person always merges."""
    mode: str = 'off'
    # ``(level, mode)`` pairs from ``[merge.levels]``.
    levels: tuple = ()
    # Directory prefixes (``docs/adr/``) or files relative to the root (``AGENTS.md``).
    operator_only_paths: tuple = DEFAULT_OPERATOR_ONLY

    def enabled(self):
        return self.mode != 'off'

    def operator_only(self, path):
        """Whether ``path`` is, or is under, an entry of ``operator_only_paths``."""
        path = path.strip('/')
        for entry in (e.strip().strip('/') for e in self.operator_only_paths):
            if entry and (path == entry or path.startswith(entry + '/')):
                return True
        return False

    def mode_for(self, level, paths):
        """The mode of a PR at review ``level`` whose diff touches ``paths``: the most
        restrictive of ``mode`` and the override of ``level``, and ``off`` when a path is
        reserved to the operator."""
        if any(self.operator_only(p) for p in paths):
            return 'off'
        modes = [self.mode] + [m for lvl, m in self.levels if lvl == level]
        return min(modes, key=MERGE_MODES.index)


@dataclass(frozen=True)
class PinPolicy:
    """``[pin]`` (#174): whether a merge by devloop that touches ``paths`` (``devloop/`` and the
    files of ``review_policy.sensitive_path_lists``) pins ``origin/<base_branch>`` at the next
    tick."""
    auto: bool = False
    paths: tuple = ('devloop/',) + DEFAULT_SENSITIVE_LISTS

    def touches(self, changed):
        """Whether a path of ``changed`` is, or is under, an entry of ``paths``."""
        for path in changed:
            path = path.strip('/')
            for entry in self.paths:
                entry = entry.strip().strip('/')
                if entry and (path == entry or path.startswith(entry + '/')):
                    return True
        return False


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
    merge_hold: str = 'agent:merge-hold'

    def all(self):
        return (self.ready, self.working, self.review, self.changes,
                self.ready_to_merge, self.needs_human, self.merge_hold)


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
    # Actions of one tick on different issues and PRs that may run at once (#176); 1 runs them
    # one at a time, in plan order.
    max_parallel_actions: int = 1
    implement_test_retries: int = 1
    # Which tests run locally after a writing turn (#169): see ``localtests``.
    local_tests: str = 'full'
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
    merge: MergePolicy = field(default_factory=MergePolicy)
    # The agent whose identity, besides trusted_users, may hold and release a merge in ``auto``
    # mode (#172), or '' when none is configured.
    supervisor: str = ''
    pin: PinPolicy = field(default_factory=PinPolicy)

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
    git, which would allow any command. An executable given by path (``/usr/bin/python3``,
    ``venv/bin/python``) is judged by its name."""
    if any(c in UNSAFE_COMMAND_CHARS for c in value):
        raise ConfigError(f'{where}: {value!r} is not a plain command prefix')
    tokens = value.split()
    if not tokens:
        raise ConfigError(f'{where}: empty command')
    if len(tokens) == 1 and BARE_COMMANDS.fullmatch(tokens[0].rsplit('/', 1)[-1]):
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


def parse_merge(table, supervisor):
    """``[merge]`` (#172): ``auto``, also as the override of a level, needs
    ``roles.supervisor``."""
    where = 'merge'
    if not isinstance(table, dict):
        raise ConfigError(f'{where}: expected a table')
    unknown = set(table) - {'mode', 'levels', 'operator_only_paths'}
    if unknown:
        raise ConfigError(f'{where}: unknown keys {sorted(unknown)}')
    mode = table.get('mode', 'off')
    if mode not in MERGE_MODES:
        raise ConfigError(f'{where}.mode: must be one of {MERGE_MODES}')
    raw = table.get('levels', {})
    if not isinstance(raw, dict):
        raise ConfigError(f'{where}.levels: expected a table of "<level>" = "<mode>"')
    levels = []
    for key, value in raw.items():
        if key not in {str(n) for n in range(ZERO_LEVEL, MAX_LEVEL + 1)}:
            raise ConfigError(f'{where}.levels: {key!r} is not a review level '
                              f'({ZERO_LEVEL} to {MAX_LEVEL})')
        if value not in MERGE_MODES:
            raise ConfigError(f'{where}.levels.{key}: must be one of {MERGE_MODES}')
        levels.append((int(key), value))
    paths = _str_list(table, 'operator_only_paths', where, DEFAULT_OPERATOR_ONLY)
    for entry in paths:
        parts = entry.strip().rstrip('/').split('/')
        if (entry.startswith('/') or any(p in ('', '.', '..') for p in parts)
                or any(c in entry for c in '*?[]\\')):
            raise ConfigError(f'{where}.operator_only_paths: invalid entry {entry!r}')
    if 'auto' in (mode, *(m for _, m in levels)) and not supervisor:
        raise ConfigError(f'{where}: "auto" needs roles.supervisor')
    return MergePolicy(mode=mode, levels=tuple(sorted(levels)), operator_only_paths=paths)


def parse_pin(table, lists):
    """``[pin]`` (#174): ``auto`` (default false). ``lists`` are the names of
    ``review_policy.sensitive_path_lists``, watched with ``devloop/``."""
    if not isinstance(table, dict):
        raise ConfigError('pin: expected a table')
    unknown = set(table) - {'auto'}
    if unknown:
        raise ConfigError(f'pin: unknown keys {sorted(unknown)}')
    auto = table.get('auto', False)
    if not isinstance(auto, bool):
        raise ConfigError('pin.auto: expected true or false')
    return PinPolicy(auto=auto, paths=('devloop/', *lists))


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
    supervisor = ''
    if 'supervisor' in roles:
        supervisor = _require(roles, 'supervisor', str, 'roles')
        if supervisor not in agents:
            raise ConfigError(f'roles.supervisor: unknown agent {supervisor!r}')
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
                'max_parallel_actions', 'implement_test_retries', 'agent_timeout_seconds', 'test_timeout_seconds',
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
    local_tests = data.get('local_tests', Config.local_tests)
    if not isinstance(local_tests, str) or local_tests not in LOCAL_TESTS_MODES:
        raise ConfigError(f'local_tests: must be one of {LOCAL_TESTS_MODES}')
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
        local_tests=local_tests,
        merge=parse_merge(data.get('merge', {}), supervisor), supervisor=supervisor,
        pin=parse_pin(data.get('pin', {}), sensitive_lists(data)),
        review_policy=parse_review_policy(
            data.get('review_policy', {}), root,
            [(k, apps[k].slug if k in apps else None, agents[k].kind, agents[k].model,
              agents[k].effort, agents[k].review_args,
              tuple((f.model, f.effort) for f in agents[k].fallback_models),
              agents[k].review_commands) for k in pool]),
        **ints)


def sensitive_lists(data):
    """The names in ``review_policy.sensitive_path_lists`` of the raw configuration ``data``."""
    table = data.get('review_policy', {})
    if not isinstance(table, dict):
        raise ConfigError('review_policy: expected a table')
    return _str_list(table, 'sensitive_path_lists', 'review_policy', DEFAULT_SENSITIVE_LISTS)


# The keys a local overlay may set (#174), the same as config.toml: ``None`` is a value, a dict
# a table, ``'*'`` any name (an agent, an app, a level).
_AGENT_KEYS = dict.fromkeys(('kind', 'display_name', 'model', 'model_label', 'effort',
                             'version_cmd', 'write_args', 'review_args', 'commit_trailer',
                             'fallback_models', 'unavailable_patterns', 'unavailable_exit_codes',
                             'write_commands', 'review_commands'))
OVERLAY_KEYS = {
    **dict.fromkeys(('repo', 'base_branch', 'branch_prefix', 'state_dir', 'test_cmd',
                     'local_tests', 'language', 'trusted_users', 'preflight', 'max_open_prs',
                     'max_review_rounds', 'max_actions_per_tick', 'max_parallel_actions',
                     'implement_test_retries', 'agent_timeout_seconds', 'test_timeout_seconds',
                     'agent_command_timeout_seconds', 'agent_stall_seconds',
                     'agent_stall_action', 'blocked_labels', 'priority_labels',
                     'protected_paths')),
    'review_policy': {**dict.fromkeys(('default_level', 'label', 'sensitive_paths',
                                       'sensitive_path_lists')),
                      'zero': dict.fromkeys(('label', 'allowlist', 'checks'))},
    'merge': {'mode': None, 'operator_only_paths': None, 'levels': {'*': None}},
    'roles': dict.fromkeys(('implementer', 'reviewer', 'reviewers', 'supervisor')),
    'agents': {'*': _AGENT_KEYS},
    'apps': {'key_dir': None, '*': dict.fromkeys(('app_id', 'slug'))},
    'pin': {'auto': None},
}


def _check_overlay(table, schema, where):
    for key, value in table.items():
        at = f'{where}.{key}' if where else key
        if key in schema:
            expected = schema[key]
        elif '*' in schema:
            expected = schema['*']
        else:
            raise ConfigError(f'local config: unknown key {at!r}')
        if isinstance(expected, dict):
            if not isinstance(value, dict):
                raise ConfigError(f'local config: {at} must be a table')
            _check_overlay(value, expected, at)


def apply_overlay(data, overlay):
    """``data`` with the keys of ``overlay`` (the local config, #174) set over it: tables merge
    key by key, any other value (a list too) replaces the one below. A key that config.toml
    cannot have is refused."""
    _check_overlay(overlay, OVERLAY_KEYS, '')

    def merge(base, top):
        out = dict(base)
        for key, value in top.items():
            below = out.get(key)
            out[key] = (merge(below, value) if isinstance(value, dict) and isinstance(below, dict)
                        else value)
        return out
    return merge(data, overlay)


def read_toml(path, what='config'):
    try:
        with open(path, 'rb') as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f'{what} {path}: {exc}') from exc


def load_data(path=None, local=None):
    """The raw configuration: ``path`` (default: config.toml of this installation) with the
    local overlay ``local`` (#174) applied, when given."""
    data = read_toml(Path(path) if path else DEFAULT_CONFIG)
    if local:
        data = apply_overlay(data, read_toml(Path(local), 'local config'))
    return data


def load_config(path=None, local=None):
    """The configuration of ``path`` (default: config.toml of this installation) with the local
    overlay ``local`` (#174) applied, when given."""
    return parse_config(load_data(path, local))
