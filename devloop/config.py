"""Dispatcher configuration (TOML)."""

import dataclasses
import hashlib
import json
import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .localtests import MODES as LOCAL_TESTS_MODES
from .policy import MAX_LEVEL, MIN_LEVEL, ZERO_LEVEL, ReviewPolicy
from .supervision import MAX_DAILY_GRANTS, MAX_GRANTED_ROUNDS

DEFAULT_CONFIG = Path(__file__).resolve().parent / 'config.toml'
# The root of the running dispatcher's installation (a pinned checkout, see docs/devloop/guide.md):
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
class RunPolicy:
    """``[run]`` (#177): the issue that gets the alert of ``devloop run`` after three consecutive
    failed ticks, or 0 when none is configured (the alert goes to the local log only)."""
    alert_issue: int = 0


@dataclass(frozen=True)
class ReportPolicy:
    """``[report]`` (#256): the issue of the daily summary of ``devloop report``, or 0 when none
    is configured (``--publish`` refuses, ``run`` publishes nothing), and the minutes between
    two automatic updates of ``devloop run``."""
    issue: int = 0
    interval: int = 15


@dataclass(frozen=True)
class CIPolicy:
    """``[ci]`` (#263): a head without CI, with ``local_tests`` other than ``full``, is not
    reviewed for ``grace_minutes`` after its push (the dispatcher's push, else the commit date):
    the CI may not have registered yet. After that it gets the full suite locally, as before,
    unless the base branch requires checks: then it waits for them up to
    ``required_timeout_minutes`` from the push, and is escalated after."""
    grace_minutes: int = 5
    required_timeout_minutes: int = 30


@dataclass(frozen=True)
class PinPolicy:
    """``[pin]`` (#174): whether a merge by devloop that touches ``paths`` (``devloop/``,
    ``core/``, which devloop imports since #365, and the files of
    ``review_policy.sensitive_path_lists``) pins ``origin/<base_branch>`` at the next
    tick. In continuous mode the restart from the new copy waits for the running actions, while
    new ones except implementations (#342) still start, for at most ``restart_max_wait_minutes``
    (#301): after that no action starts until the restart."""
    auto: bool = False
    paths: tuple = ('devloop/', 'core/') + DEFAULT_SENSITIVE_LISTS
    restart_max_wait_minutes: int = 30

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
class GCPolicy:
    """``[gc]`` (#380): the cleanup of ``state_dir``. ``pins``: after a successful pin and at
    each cleanup, keep ``current`` and the ``keep_pins`` copies pinned most recently, and remove
    the other ``pinned-*`` copies no process uses. ``worktrees``: remove the worktrees of closed
    issues and of closed or merged PRs. ``logs``: remove the files of ``logs/`` not modified for
    ``log_days`` days, except the evidence of an open escalation and the files of a turn still
    open in the ledger. The cleanup runs at a tick with nothing running, at most every
    ``interval_minutes`` (0: at every such tick), and with ``devloop gc``."""
    pins: bool = True
    keep_pins: int = 5
    worktrees: bool = True
    logs: bool = False
    log_days: int = 14
    interval_minutes: int = 60

    def enabled(self):
        return self.pins or self.worktrees or self.logs


@dataclass(frozen=True)
class DelegationPolicy:
    """``[delegations]`` (ADR 0005): the delegations of the operator to the supervisor, each
    with its revocation key, all off by default (D10) and revoked by turning them off.

    - ``retry_own_prs`` (P5-2, #406): a ``tool_failure`` escalation on a PR implemented by the
      supervisor's agent goes to the supervisor, who may retry it within ``MAX_RETRIES``; every
      other class on such a PR still goes to the operator.
    - ``rounds`` (P5-1, #405): the supervisor may grant one more review round to a PR at the
      round limit (``/devloop grant-round``). ``max_rounds_per_pr`` and ``max_grants_per_day``
      lower the caps of the code (``supervision.MAX_GRANTED_ROUNDS`` and ``MAX_DAILY_GRANTS``),
      never raise them."""
    retry_own_prs: bool = False
    rounds: bool = False
    max_rounds_per_pr: int = MAX_GRANTED_ROUNDS
    max_grants_per_day: int = MAX_DAILY_GRANTS


# The roles of a turn in the budget envelope (#365): the kinds of the turns charged to a story.
# A readiness check (``preflight``) comes before the takeover and is outside the envelope.
BUDGET_ROLES = ('implement', 'fix', 'review', 'sync', 'conclave', 'advisory')
# The roles every envelope caps; ``conclave`` and ``advisory`` only when they are configured.
BUDGET_BASE_ROLES = ('implement', 'fix', 'review', 'sync')
# The kinds of phase (``journal.PHASES``): the current flow has only the implementation.
BUDGET_PHASES = ('implementation',)
BUDGET_USD = ('metered_usd', 'cli_estimate_usd')
BUDGET_USAGE_MODELS = ('reserve_per_invocation', 'unmetered_quota')
# The agent kinds whose CLI reports a dollar figure (an estimate: ``core.budget.REPORTED_COST``):
# the only ones that may declare a reserve in USD. With the budget on, every agent, these
# included, needs a declared rule for unknown usage (#396).
BUDGET_REPORTED_COST = ('claude',)
# Whether devloop can extend the envelope of a story at its limit (``grant-budget``, ADR 0005).
# Until it can, ``[budget] enabled = true`` is refused at startup (#396): a story at its limit
# could leave it only by turning the budget off for every story. The tests of the envelope set
# it; nothing else does.
BUDGET_EXTENSION = False


@dataclass(frozen=True)
class BudgetPolicy:
    """``[budget]`` (#365, ADR 0004, P2-5a): the envelope of each story. Off by default (D8):
    devloop behaves as before. On, every value is explicit (D2: no default): ``turns`` of the
    story, ``role_turns`` per role (the kind of the turn), ``phase_turns`` per kind of phase,
    ``wall_seconds`` from the story's start, the dollar caps ``metered_usd`` and
    ``cli_estimate_usd`` (measured spend and estimates apart), and ``usage``, the declared rule
    for unknown usage of each agent: ``(agent, {model, reason, [metered_usd, cli_estimate_usd]})``
    pairs. Token caps are not supported yet."""
    enabled: bool = False
    turns: int = 0
    role_turns: tuple = ()   # ((role, cap), ...)
    phase_turns: tuple = ()  # ((phase, cap), ...)
    wall_seconds: int = 0
    metered_usd: float = 0.0
    cli_estimate_usd: float = 0.0
    usage: tuple = ()        # ((agent, rule as a tuple of (key, value) pairs), ...)

    def rule(self, agent):
        """The declared rule of ``agent`` for unknown usage, as a dict, or None."""
        found = dict(self.usage).get(agent)
        return dict(found) if found is not None else None


@dataclass(frozen=True)
class DocsMap:
    """``[docs] map`` (#203): code paths and the documents that describe them. A key is a
    directory prefix ending in ``/`` or a file, relative to the root; ``entries`` holds
    ``(key, documents)`` pairs in configuration order."""
    entries: tuple = ()

    def documents(self, paths):
        """``{document: [paths]}`` of the documents mapped to ``paths`` (the files of a diff),
        sorted by document, each with the sorted paths of ``paths`` it comes from. A path that
        no key covers maps to nothing."""
        found = {}
        for path in sorted(set(paths)):
            for key, documents in self.entries:
                if path == key or key.endswith('/') and path.startswith(key):
                    for document in documents:
                        found.setdefault(document, [])
                        if path not in found[document]:
                            found[document].append(path)
        return dict(sorted(found.items()))


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
    # How long a model found unavailable is skipped when the error does not say when it is
    # back (``Resets in <duration>``), with fallback models only (issue #151).
    fallback_memory_seconds: int = 3600
    # How long a model denied a permission twice in one turn is skipped, when the turn moved on
    # to the next model of the chain (agy, issue #319).
    denied_cooldown_hours: int = 6
    # Set on the configurations ``chain`` derives for fallback models: the configured model.
    fallback_for: str = ''
    # Set on the configuration a turn ran with (``Dispatcher._turn``, #151): the models of the
    # chain skipped because remembered unavailable, ``(model, reason, until)`` each.
    skipped: tuple = ()
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


# Actors that are an app of devloop's and never an agent: the dispatcher, and the merger, the app
# that only merges (#201).
APP_ONLY_ACTORS = ('dispatcher', 'merger')


@dataclass(frozen=True)
class AppConfig:
    """A GitHub App: its id and slug. The private key is found by slug in ``app_key_dir``."""
    app_id: int
    slug: str


# The name ``agent:needs-operator`` had before #248. ``devloop setup`` renames the label on
# GitHub; until then, and while a repository still has it, it blocks exactly as the new one.
LEGACY_NEEDS_OPERATOR = 'agent:needs-human'


@dataclass(frozen=True)
class Labels:
    ready: str = 'agent:ready'
    working: str = 'agent:working'
    review: str = 'agent:review'
    changes: str = 'agent:changes'
    ready_to_merge: str = 'agent:ready-to-merge'
    needs_operator: str = 'agent:needs-operator'
    merge_hold: str = 'agent:merge-hold'
    # A non-critical escalation the supervisor decides in merge mode ``auto`` (#182).
    needs_supervisor: str = 'agent:needs-supervisor'

    def all(self):
        return (self.ready, self.working, self.review, self.changes,
                self.ready_to_merge, self.needs_operator, self.merge_hold, self.needs_supervisor)

    def operator(self):
        """The labels that hand an issue or a PR to the operator (#248): ``needs_operator`` and
        its name before #248, which blocks and authorizes exactly as it does."""
        return frozenset({self.needs_operator, LEGACY_NEEDS_OPERATOR})


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
    # The three agents of the conclave on a block (#141), the agent that stopped among them, or
    # () when it is disabled.
    conclave: tuple = ()
    max_open_prs: int = 2
    # Open devloop PRs waiting for a person (``dispatcher.parked``, #199): they do not take the
    # slots of max_open_prs, and once this many are open only priority:high issues start (#274).
    max_parked_prs: int = 3
    max_review_rounds: int = 3
    max_actions_per_tick: int = 3
    # Actions of one tick on different issues and PRs that may run at once (#176); 1 runs them
    # one at a time, in plan order.
    max_parallel_actions: int = 1
    implement_test_retries: int = 1
    # Which tests run locally after a writing turn (#169): see ``localtests``.
    local_tests: str = 'full'
    # Re-run once, without a round, a CI failure whose test modules are not pertinent to the PR
    # (#200, ``Dispatcher._rerun_unrelated``).
    ci_rerun_unrelated: bool = True
    # The block on uncertain turns (#362): a turn of the journal left without outcome refuses
    # every new turn of its work until a trusted user reconciles it. Off by default (ADR 0004,
    # D8): the journal records the turns all the same.
    block_uncertain_turns: bool = False
    # The story identity (#363, ADR 0004, P2-3a): at the takeover of an issue the journal records
    # its story, and the comment of its ``request`` record carries the dispatcher's ``story``
    # marker. Off by default (ADR 0004, D8): devloop behaves as before.
    story_identity: bool = False
    # The outbox of the publications (#385, ``outbox.Outbox``): every comment, edit, native
    # review and pull request is in the journal of the turns before it is sent, and an uncertain
    # one is reconciled before any new POST. Off by default (ADR 0004, D8): devloop publishes as
    # before.
    publication_outbox: bool = False
    # The attempt identity (#384, ADR 0004, P2-3b): one active attempt per phase, recorded in the
    # journal, in the PR marker and in the turn records. It needs ``story_identity``. Off by
    # default (ADR 0004, D8): devloop behaves as before.
    attempt_identity: bool = False
    # The limit of the journal of the turns, in MiB (#397, ``journal.TurnJournal``): beyond it
    # the journal is not loaded and no event is written; from 80% of it the operator is warned.
    journal_max_mib: int = 64
    # The budget envelope of each story (#365, ADR 0004, P2-5a). Off by default (D8).
    budget: BudgetPolicy = field(default_factory=BudgetPolicy)
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
    # The delegations to the supervisor (ADR 0005), each off by default (D10).
    delegations: DelegationPolicy = field(default_factory=DelegationPolicy)
    pin: PinPolicy = field(default_factory=PinPolicy)
    run: RunPolicy = field(default_factory=RunPolicy)
    # The daily summary (#256).
    report: ReportPolicy = field(default_factory=ReportPolicy)
    # The wait for the CI of a new head (#263).
    ci: CIPolicy = field(default_factory=CIPolicy)
    # The cleanup of state_dir (#380).
    gc: GCPolicy = field(default_factory=GCPolicy)
    # Code paths and their documents, listed in the implement, fix and review prompts (#203).
    docs: DocsMap = field(default_factory=DocsMap)

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


def _memory_seconds(table, where):
    """``fallback_memory_seconds`` of an agent (#151): a positive number of seconds."""
    if 'fallback_memory_seconds' not in table:
        return AgentConfig.fallback_memory_seconds
    value = _require(table, 'fallback_memory_seconds', int, where)
    if value < 1:
        raise ConfigError(f'{where}.fallback_memory_seconds: out of range')
    return value


def _denied_cooldown(table, where):
    """``denied_cooldown_hours`` of an agent (#319): a positive number of hours."""
    if 'denied_cooldown_hours' not in table:
        return AgentConfig.denied_cooldown_hours
    value = _require(table, 'denied_cooldown_hours', int, where)
    if value < 1:
        raise ConfigError(f'{where}.denied_cooldown_hours: out of range')
    return value


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


def parse_review_policy(table, root, reviewers=(), agents=None):
    """The policy of ``[review_policy]``. A list file that cannot be read stops devloop: the
    policy never runs with fewer sensitive paths than configured. ``reviewers`` is the pool
    as ``ReviewPolicy.reviewers`` records it; ``agents``, the configured agents, among which
    ``advisory_reviewer`` must be (#369), when given."""
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
    advisory = table.get('advisory_reviewer', default.advisory_reviewer)
    if not isinstance(advisory, str):
        raise ConfigError(f'{where}.advisory_reviewer: expected a string')
    if advisory and agents is not None and advisory not in agents:
        raise ConfigError(f'{where}.advisory_reviewer: unknown agent {advisory!r}')
    return ReviewPolicy(default_level=level, label=label, sensitive_paths=paths,
                        sensitive_files=tuple(files), reviewers=tuple(reviewers),
                        zero_label=zero_label, zero_allowlist=allowlist, zero_checks=checks,
                        advisory_reviewer=advisory)


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
    """``[pin]`` (#174): ``auto`` (default false) and ``restart_max_wait_minutes`` (#301, default
    30, 0 or more). ``lists`` are the names of ``review_policy.sensitive_path_lists``, watched
    with ``devloop/`` and ``core/``."""
    if not isinstance(table, dict):
        raise ConfigError('pin: expected a table')
    unknown = set(table) - {'auto', 'restart_max_wait_minutes'}
    if unknown:
        raise ConfigError(f'pin: unknown keys {sorted(unknown)}')
    auto = table.get('auto', False)
    if not isinstance(auto, bool):
        raise ConfigError('pin.auto: expected true or false')
    wait = table.get('restart_max_wait_minutes', PinPolicy.restart_max_wait_minutes)
    if not isinstance(wait, int) or isinstance(wait, bool) or wait < 0:
        raise ConfigError('pin.restart_max_wait_minutes: expected a whole number of minutes, '
                          'at least 0')
    return PinPolicy(auto=auto, paths=('devloop/', 'core/', *lists),
                     restart_max_wait_minutes=wait)


def parse_run(table):
    """``[run]`` (#177): ``alert_issue``, optional, the number of an issue (at least 1)."""
    if not isinstance(table, dict):
        raise ConfigError('run: expected a table')
    unknown = set(table) - {'alert_issue'}
    if unknown:
        raise ConfigError(f'run: unknown keys {sorted(unknown)}')
    alert = table.get('alert_issue', 0)
    if 'alert_issue' in table and (not isinstance(alert, int) or isinstance(alert, bool)
                                   or alert < 1):
        raise ConfigError('run.alert_issue: expected the number of an issue')
    return RunPolicy(alert_issue=alert)


def parse_report(table):
    """``[report]`` (#256): ``issue``, optional, the number of an issue (at least 1);
    ``interval``, whole minutes of at least 1 (default 15)."""
    if not isinstance(table, dict):
        raise ConfigError('report: expected a table')
    unknown = set(table) - {'issue', 'interval'}
    if unknown:
        raise ConfigError(f'report: unknown keys {sorted(unknown)}')
    values = {}
    for key, what in (('issue', 'the number of an issue'),
                      ('interval', 'a whole number of minutes of at least 1')):
        if key in table:
            value = table[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ConfigError(f'report.{key}: expected {what}')
            values[key] = value
    return ReportPolicy(**values)


def parse_ci(table):
    """``[ci]`` (#263): ``grace_minutes`` (0 or more) and ``required_timeout_minutes`` (at least
    1), whole minutes."""
    if not isinstance(table, dict):
        raise ConfigError('ci: expected a table')
    unknown = set(table) - {'grace_minutes', 'required_timeout_minutes'}
    if unknown:
        raise ConfigError(f'ci: unknown keys {sorted(unknown)}')
    values = {}
    for key, low in (('grace_minutes', 0), ('required_timeout_minutes', 1)):
        if key in table:
            value = table[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < low:
                raise ConfigError(f'ci.{key}: expected a whole number of minutes, at least {low}')
            values[key] = value
    return CIPolicy(**values)


def parse_delegations(table):
    """``[delegations]`` (ADR 0005): ``retry_own_prs`` (#406) and ``rounds`` (#405), true or
    false (default false, D10); ``max_rounds_per_pr`` and ``max_grants_per_day``, whole numbers
    from 0 to the caps of the code. A value above a cap is refused: the configuration and the
    overlay may only lower the caps (D4)."""
    if not isinstance(table, dict):
        raise ConfigError('delegations: expected a table')
    unknown = set(table) - {f.name for f in dataclasses.fields(DelegationPolicy)}
    if unknown:
        raise ConfigError(f'delegations: unknown keys {sorted(unknown)}')
    values = {}
    for key in ('retry_own_prs', 'rounds'):
        if key in table:
            if not isinstance(table[key], bool):
                raise ConfigError(f'delegations.{key}: expected true or false')
            values[key] = table[key]
    for key, cap in (('max_rounds_per_pr', MAX_GRANTED_ROUNDS),
                     ('max_grants_per_day', MAX_DAILY_GRANTS)):
        if key in table:
            value = table[key]
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= cap:
                raise ConfigError(f'delegations.{key}: expected a whole number from 0 to {cap}, '
                                  'the cap of the code, which the configuration may only lower')
            values[key] = value
    return DelegationPolicy(**values)


def parse_gc(table):
    """``[gc]`` (#380): ``pins``, ``worktrees`` and ``logs``, booleans (default true, true and
    false); ``keep_pins`` (default 5) and ``interval_minutes`` (default 60), whole numbers of at
    least 0; ``log_days`` (default 14), a whole number of days of at least 1."""
    if not isinstance(table, dict):
        raise ConfigError('gc: expected a table')
    unknown = set(table) - {f.name for f in dataclasses.fields(GCPolicy)}
    if unknown:
        raise ConfigError(f'gc: unknown keys {sorted(unknown)}')
    values = {}
    for key in ('pins', 'worktrees', 'logs'):
        if key in table:
            if not isinstance(table[key], bool):
                raise ConfigError(f'gc.{key}: expected true or false')
            values[key] = table[key]
    for key, low, what in (('keep_pins', 0, 'copies'), ('interval_minutes', 0, 'minutes'),
                           ('log_days', 1, 'days')):
        if key in table:
            value = table[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < low:
                raise ConfigError(f'gc.{key}: expected a whole number of {what}, at least {low}')
            values[key] = value
    return GCPolicy(**values)


def _no_tokens(table, where):
    """Refuse a token cap (#365): its semantics are not decided yet (ADR 0004, P2-5a)."""
    for key in table:
        if 'token' in str(key).lower():
            raise ConfigError(f'{where}.{key}: token caps are not supported yet: the budget '
                              'envelope holds turns, time and attested dollars only')


def _budget_count(table, key, where, low):
    value = table[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < low:
        raise ConfigError(f'{where}.{key}: expected a whole number, at least {low}')
    return value


def _budget_usd(table, key, where):
    """A dollar cap or reserve: finite and above 0. A cap is exhausted at equality
    (``core.budget.check_dollars``), so a cap of 0 would refuse every turn, of an agent without
    dollars too; a reserve of 0 is refused by ``core.budget.validate_live_usage``."""
    value = table[key]
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value <= 0):
        raise ConfigError(f'{where}.{key}: expected a finite number of dollars, above 0')
    return float(value)


def _budget_caps(table, key, where, names, required):
    """``((name, cap), ...)`` of the sub-table ``key``: whole numbers of at least 0, keys from
    ``names``, each of ``required`` present."""
    sub = table.get(key, {})
    at = f'{where}.{key}'
    if not isinstance(sub, dict):
        raise ConfigError(f'{at}: expected a table')
    _no_tokens(sub, at)
    unknown = set(sub) - set(names)
    if unknown:
        raise ConfigError(f'{at}: unknown keys {sorted(unknown)}, expected some of {list(names)}')
    for name in required:
        if name not in sub:
            raise ConfigError(f'{at}: missing {name!r} (no default: the operator sets it)')
    return tuple((name, _budget_count(sub, name, at, 0)) for name in names if name in sub)


def _budget_rule(rule, where):
    """A declared rule for unknown usage, as ``core.budget.validate_live_usage`` reads it."""
    if not isinstance(rule, dict):
        raise ConfigError(f'{where}: expected a table with model and reason')
    _no_tokens(rule, where)
    unknown = set(rule) - {'model', 'reason', *BUDGET_USD}
    if unknown:
        raise ConfigError(f'{where}: unknown keys {sorted(unknown)}')
    model = rule.get('model')
    if model not in BUDGET_USAGE_MODELS:
        raise ConfigError(f'{where}.model: must be one of {BUDGET_USAGE_MODELS}')
    reason = rule.get('reason')
    if not isinstance(reason, str) or not reason.strip():
        raise ConfigError(f'{where}.reason: expected a non-empty string')
    values = [('model', model), ('reason', reason)]
    if model == 'reserve_per_invocation':
        for kind in BUDGET_USD:
            if kind not in rule:
                raise ConfigError(f'{where}: missing {kind!r}: a reserve is booked in both '
                                  'dollar kinds')
            values.append((kind, _budget_usd(rule, kind, where)))
    elif set(rule) & set(BUDGET_USD):
        raise ConfigError(f'{where}: unmetered_quota books no dollar reserve')
    return tuple(values)


def parse_budget(table, agents, roles=BUDGET_BASE_ROLES, story_identity=False):
    """``[budget]`` (#365, ADR 0004, P2-5a). ``enabled`` (default false) turns the envelope on;
    then every value is required, with no default (D2): ``turns`` and ``wall_seconds`` (at least
    1), ``metered_usd`` and ``cli_estimate_usd`` (dollars, above 0), ``role_turns`` with each
    of ``roles``, ``phase_turns`` with each kind of phase, and in ``usage`` a rule for each
    agent, claude included (#396): only ``unmetered_quota`` for those whose kind reports no
    dollar figure (codex, agy, D2), while a ``reserve_per_invocation`` is accepted only for
    claude. A story is the unit of the envelope: it needs ``story_identity``. Turning it on is
    refused while a story at its limit has no extension (``BUDGET_EXTENSION``, #396). A token cap
    is refused, on or off: not supported yet."""
    where = 'budget'
    if not isinstance(table, dict):
        raise ConfigError(f'{where}: expected a table')
    _no_tokens(table, where)
    keys = {'enabled', 'turns', 'wall_seconds', *BUDGET_USD, 'role_turns', 'phase_turns',
            'usage'}
    unknown = set(table) - keys
    if unknown:
        raise ConfigError(f'{where}: unknown keys {sorted(unknown)}')
    enabled = table.get('enabled', False)
    if not isinstance(enabled, bool):
        raise ConfigError(f'{where}.enabled: expected true or false')
    if enabled:
        if not BUDGET_EXTENSION:
            raise ConfigError(
                f'{where}.enabled: the budget cannot be turned on yet: a story that reaches '
                'its limit has no extension (grant-budget, ADR 0005, not available), and the '
                'only way out would be enabled = false for every story. Keep enabled = false '
                'until the extension exists (#396)')
        if not story_identity:
            raise ConfigError(f'{where}.enabled: needs story_identity = true: the envelope is '
                              'per story')
        for key in ('turns', 'wall_seconds', *BUDGET_USD):
            if key not in table:
                raise ConfigError(f'{where}: missing {key!r} (no default: the operator sets it)')
    values = {'enabled': enabled}
    for key in ('turns', 'wall_seconds'):
        if key in table:
            values[key] = _budget_count(table, key, where, 1)
    for key in BUDGET_USD:
        if key in table:
            values[key] = _budget_usd(table, key, where)
    values['role_turns'] = _budget_caps(table, 'role_turns', where, BUDGET_ROLES,
                                        roles if enabled else ())
    values['phase_turns'] = _budget_caps(table, 'phase_turns', where, BUDGET_PHASES,
                                         BUDGET_PHASES if enabled else ())
    usage = table.get('usage', {})
    if not isinstance(usage, dict):
        raise ConfigError(f'{where}.usage: expected a table of agents')
    rules = []
    for agent, rule in usage.items():
        if agent not in agents:
            raise ConfigError(f'{where}.usage.{agent}: unknown agent {agent!r}')
        parsed = _budget_rule(rule, f'{where}.usage.{agent}')
        if (agents[agent].kind not in BUDGET_REPORTED_COST
                and dict(parsed)['model'] != 'unmetered_quota'):
            # D2: an agent whose CLI reports no cost (agy, codex) is bounded by the turn caps
            # alone, never by a reserve in USD that nothing attests.
            raise ConfigError(f'{where}.usage.{agent}.model: {agents[agent].kind} reports no '
                              'dollar cost: only unmetered_quota, with the turn caps, and no '
                              'reserve in USD (ADR 0004, D2)')
        rules.append((agent, parsed))
    if enabled:
        declared = dict(rules)
        for key, agent in agents.items():
            if key in declared:
                continue
            if agent.kind not in BUDGET_REPORTED_COST:
                raise ConfigError(f'{where}.usage.{key}: missing: {agent.kind} reports no dollar '
                                  'cost, and its turns need a declared rule: unmetered_quota, '
                                  'with the turn caps (ADR 0004, D2)')
            # #396: a turn that ends without its cost (a model not available, a timeout, a
            # crash, no output) would leave the story uncovered, refusing every later turn.
            raise ConfigError(f'{where}.usage.{key}: missing: {agent.kind} reports its cost '
                              'only when a turn completes, and a turn that ends without it '
                              'would block its story for good: declare unmetered_quota, or '
                              'reserve_per_invocation with metered_usd and cli_estimate_usd '
                              '(ADR 0004, D2)')
    values['usage'] = tuple(rules)
    return BudgetPolicy(**values)


def _docs_path(value, where, document):
    """``value`` as a path of ``[docs] map``: a non-empty path relative to the root, without
    surrounding spaces, ``.``, ``..`` or empty segments, backslashes or pattern characters. A key
    may end in ``/`` (a directory prefix); a ``document`` is a file and may not."""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f'{where}: empty entry')
    parts = (value[:-1] if value.endswith('/') else value).split('/')
    if (value != value.strip() or value.startswith('/')
            or any(p in ('', '.', '..') for p in parts) or any(c in value for c in '*?[]\\')):
        raise ConfigError(f'{where}: {value!r} is not a relative path inside the root')
    if document and value.endswith('/'):
        raise ConfigError(f'{where}: {value!r} is a directory, not a document')
    return value


def docs_entries(data):
    """The ``(key, documents)`` pairs of ``[docs] map`` in the raw configuration ``data``,
    checked for syntax only (#203): every key a directory prefix (``devloop/``) or a file, every
    value a non-empty list of document files, all relative to the root."""
    table = data.get('docs', {})
    if not isinstance(table, dict):
        raise ConfigError('docs: expected a table')
    unknown = set(table) - {'map'}
    if unknown:
        raise ConfigError(f'docs: unknown keys {sorted(unknown)}')
    mapping = table.get('map', {})
    if not isinstance(mapping, dict):
        raise ConfigError('docs.map: expected a table of "<path>" = ["<document>", ...]')
    entries = []
    for key, value in mapping.items():
        where = f'docs.map.{key!r}' if key else 'docs.map'
        _docs_path(key, where, document=False)
        if not isinstance(value, list) or not value:
            raise ConfigError(f'{where}: expected a non-empty list of documents')
        documents = []
        for document in value:
            _docs_path(document, where, document=True)
            if document not in documents:
                documents.append(document)
        entries.append((key, tuple(documents)))
    return tuple(entries)


def parse_docs(data):
    """``[docs] map`` (#203): its entries (``docs_entries``), checked for shape only. The
    documents belong to the repository devloop works on, not to the installation, and a pinned
    copy does not hold them (#210): the dispatcher looks for each one in the worktree of the turn
    when it lists the documents of a diff (``Dispatcher._mapped_docs``)."""
    return DocsMap(entries=docs_entries(data))


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


CONCLAVE_SIZE = 3


def parse_conclave(data, agents):
    """``conclave`` (#141): three distinct configured agents, whose ``(kind, model, effort)``
    differ two by two; ``()`` without the key. That their identities differ too is checked when
    a conclave would open, where the apps are known."""
    if 'conclave' not in data:
        return ()
    keys = _str_list(data, 'conclave', 'config')
    if len(keys) != CONCLAVE_SIZE or len(set(keys)) != CONCLAVE_SIZE:
        raise ConfigError(f'conclave: exactly {CONCLAVE_SIZE} different agents are required')
    unknown = [key for key in keys if key not in agents]
    if unknown:
        raise ConfigError(f'conclave: unknown agent {unknown[0]!r}')
    tuples = {(agents[k].kind, agents[k].model, agents[k].effort) for k in keys}
    if len(tuples) != CONCLAVE_SIZE:
        raise ConfigError('conclave: the agents must differ in (kind, model, effort)')
    return keys


def parse_config(data, root=INSTALL_ROOT):
    """``root`` is where relative ``review_policy.sensitive_path_lists`` are read."""
    test_cmd = _require(data, 'test_cmd', str, 'config')
    agents = {}
    for key, table in (data.get('agents') or {}).items():
        where = f'agents.{key}'
        if key in APP_ONLY_ACTORS:
            raise ConfigError(f'{where}: {key!r} is reserved to devloop\'s own app')
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
            fallback_memory_seconds=_memory_seconds(table, where),
            denied_cooldown_hours=_denied_cooldown(table, where),
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
    conclave = parse_conclave(data, agents)
    trusted =_str_list(data, 'trusted_users', 'config')
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
        if actor not in APP_ONLY_ACTORS and actor not in agents:
            raise ConfigError(f'{where}: not the dispatcher, the merger nor a configured agent')
        if not isinstance(table, dict):
            raise ConfigError(f'{where}: expected a table')
        apps[actor] = AppConfig(app_id=_require(table, 'app_id', int, where),
                                slug=_require(table, 'slug', str, where))
    if len({app.app_id for app in apps.values()}) != len(apps):
        raise ConfigError('apps: each actor needs its own app')
    ints = {}
    for key in ('max_open_prs', 'max_parked_prs', 'max_review_rounds', 'max_actions_per_tick',
                'max_parallel_actions', 'implement_test_retries', 'agent_timeout_seconds', 'test_timeout_seconds',
                'agent_command_timeout_seconds', 'agent_stall_seconds', 'journal_max_mib'):
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
    ci_rerun = (_require(data, 'ci_rerun_unrelated', bool, 'config')
                if 'ci_rerun_unrelated' in data else Config.ci_rerun_unrelated)
    block_uncertain = (_require(data, 'block_uncertain_turns', bool, 'config')
                       if 'block_uncertain_turns' in data else Config.block_uncertain_turns)
    story_identity = (_require(data, 'story_identity', bool, 'config')
                      if 'story_identity' in data else Config.story_identity)
    outbox = (_require(data, 'publication_outbox', bool, 'config')
              if 'publication_outbox' in data else Config.publication_outbox)
    attempt_identity = (_require(data, 'attempt_identity', bool, 'config')
                        if 'attempt_identity' in data else Config.attempt_identity)
    if attempt_identity and not story_identity:
        # An attempt belongs to a phase of a story (#384): without stories there is no phase.
        raise ConfigError('attempt_identity: needs story_identity = true')
    review_policy = parse_review_policy(
        data.get('review_policy', {}), root,
        [(k, apps[k].slug if k in apps else None, agents[k].kind, agents[k].model,
          agents[k].effort, agents[k].review_args,
          tuple((f.model, f.effort) for f in agents[k].fallback_models),
          agents[k].review_commands) for k in pool], agents)
    # The roles whose turns can run (#365): conclave and advisory only when configured.
    budget_roles = (BUDGET_BASE_ROLES + (('conclave',) if conclave else ())
                    + (('advisory',) if review_policy.advisory_reviewer else ()))
    budget = parse_budget(data.get('budget', {}), agents, budget_roles, story_identity)
    return Config(
        repo=_require(data, 'repo', str, 'config'),
        base_branch=data.get('base_branch', 'main'),
        branch_prefix=data.get('branch_prefix', 'agent/issue-'),
        state_dir=data.get('state_dir', '.ssdlc/devloop'),
        test_cmd=test_cmd,
        language=data.get('language', 'English'),
        implementer=implementer, reviewer=pool[0], reviewers=pool, agents=agents,
        preflight=preflight, conclave=conclave,
        trusted_users=trusted,
        blocked_labels=_str_list(data, 'blocked_labels', 'config', ('bloccata',)),
        priority_labels=_str_list(data, 'priority_labels', 'config', ('prerequisito',)),
        protected_paths=_str_list(data, 'protected_paths', 'config', ('.github/',)),
        apps=apps, app_key_dir=key_dir, agent_stall_action=stall_action,
        local_tests=local_tests, ci_rerun_unrelated=ci_rerun,
        block_uncertain_turns=block_uncertain, story_identity=story_identity,
        publication_outbox=outbox, attempt_identity=attempt_identity, budget=budget,
        merge=parse_merge(data.get('merge', {}), supervisor), supervisor=supervisor,
        pin=parse_pin(data.get('pin', {}), sensitive_lists(data)),
        run=parse_run(data.get('run', {})),
        report=parse_report(data.get('report', {})),
        ci=parse_ci(data.get('ci', {})),
        gc=parse_gc(data.get('gc', {})),
        delegations=parse_delegations(data.get('delegations', {})),
        docs=parse_docs(data),
        review_policy=review_policy,
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
                             'fallback_memory_seconds', 'denied_cooldown_hours',
                             'write_commands', 'review_commands'))
OVERLAY_KEYS = {
    **dict.fromkeys(('repo', 'base_branch', 'branch_prefix', 'state_dir', 'test_cmd',
                     'local_tests', 'ci_rerun_unrelated', 'block_uncertain_turns', 'story_identity',
                     'publication_outbox', 'attempt_identity', 'journal_max_mib',
                     'language', 'trusted_users', 'preflight',
                     'conclave', 'max_open_prs',
                     'max_parked_prs', 'max_review_rounds', 'max_actions_per_tick',
                     'max_parallel_actions', 'implement_test_retries', 'agent_timeout_seconds',
                     'test_timeout_seconds',
                     'agent_command_timeout_seconds', 'agent_stall_seconds',
                     'agent_stall_action', 'blocked_labels', 'priority_labels',
                     'protected_paths')),
    'review_policy': {**dict.fromkeys(('default_level', 'label', 'sensitive_paths',
                                       'sensitive_path_lists', 'advisory_reviewer')),
                      'zero': dict.fromkeys(('label', 'allowlist', 'checks'))},
    'merge': {'mode': None, 'operator_only_paths': None, 'levels': {'*': None}},
    'roles': dict.fromkeys(('implementer', 'reviewer', 'reviewers', 'supervisor')),
    'agents': {'*': _AGENT_KEYS},
    'apps': {'key_dir': None, '*': dict.fromkeys(('app_id', 'slug'))},
    'pin': {'auto': None},
    'run': {'alert_issue': None},
    'report': dict.fromkeys(('issue', 'interval')),
    'ci': dict.fromkeys(('grace_minutes', 'required_timeout_minutes')),
    'gc': dict.fromkeys(('pins', 'keep_pins', 'worktrees', 'logs', 'log_days',
                         'interval_minutes')),
    'docs': {'map': {'*': None}},
    'delegations': dict.fromkeys(('retry_own_prs', 'rounds', 'max_rounds_per_pr',
                                  'max_grants_per_day')),
    # Every key of [budget] (#365) is checked by ``parse_budget``, a token cap with its message.
    'budget': {'*': None},
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


# The keys ``devloop run`` reads again before each tick (#278): the operating limits. Any other
# key, merge modes, pin, roles, trust, identities and ``state_dir`` included, needs a restart.
# ``delegations`` (#405) too, so that the operator revokes a delegation from the next tick.
LIVE_KEYS = ('max_open_prs', 'max_parked_prs', 'max_review_rounds', 'delegations')


class Reloader:
    """``devloop run`` (#278): before each tick, ``refresh()`` reads ``path`` (default:
    config.toml of this installation) and the overlay ``local`` again and, when the content of
    one of them changed (its hash: an mtime may not change for a quick rewrite), returns the
    configuration of the tick: ``cfg`` with the ``LIVE_KEYS`` of the files. A change of any other
    key is logged and waits for a restart; a configuration that does not load is logged and the
    previous one stays. Either way the files are loaded again only after their next change.
    The first ``refresh()`` loads them in any case: they may have changed between the load of
    ``cfg`` and the creation of the reloader, and a fingerprint taken here would hide that."""

    def __init__(self, cfg, path=None, local=None, *, log):
        self.cfg = cfg
        self.log = log
        self.paths = (Path(path) if path else DEFAULT_CONFIG,) + ((Path(local),) if local else ())
        self._seen = None  # no content compared with ``cfg`` yet

    @staticmethod
    def _read(path):
        """``(sha256, content)`` of ``path``, or None when it cannot be read."""
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        return hashlib.sha256(raw).hexdigest(), raw

    def _load(self, now):
        """The configuration of the contents just read (the ones hashed), as ``load_config``."""
        data = None
        for path in self.paths:
            what = 'config' if data is None else 'local config'
            if now[path] is None:
                raise ConfigError(f'{what} {path}: cannot be read')
            try:
                table = tomllib.loads(now[path][1].decode())
            except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
                raise ConfigError(f'{what} {path}: {exc}') from exc
            data = table if data is None else apply_overlay(data, table)
        return parse_config(data)

    def refresh(self):
        """The configuration of the next tick."""
        now = {p: self._read(p) for p in self.paths}
        changed = now != self._seen
        self._seen = now
        if not changed:
            return self.cfg
        try:
            new = self._load(now)
        except Exception as exc:  # the loop goes on with the configuration it has
            self.log(f'config: changed but not valid, the previous configuration stays in use: '
                     f'{type(exc).__name__}: {exc}')
            return self.cfg
        restart = [f.name for f in dataclasses.fields(Config) if f.name not in LIVE_KEYS
                   and getattr(new, f.name) != getattr(self.cfg, f.name)]
        if restart:
            self.log(f'config: {", ".join(restart)} changed: the change applies only after a '
                     'restart, the running values stay in use')
        live = {key: getattr(new, key) for key in LIVE_KEYS
                if getattr(new, key) != getattr(self.cfg, key)}
        for key, value in live.items():
            self.log(f'config: {key} {getattr(self.cfg, key)} -> {value} from this tick')
        if live:
            self.cfg = dataclasses.replace(self.cfg, **live)
        return self.cfg
