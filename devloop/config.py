"""Dispatcher configuration (TOML)."""

import dataclasses
import hashlib
import json
import math
import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .agentuser import allowed_entries
from .localtests import CI_LOG_FORMATS, SELECTIONS, Tests
from .localtests import MODES as LOCAL_TESTS_MODES
from .policy import MAX_LEVEL, MIN_LEVEL, SECOND_REVIEWER_MODES, ZERO_LEVEL, ReviewPolicy
from .supervision import (MAX_CAPACITY, MAX_CAPACITY_HOURS, MAX_COORDINATOR_MERGES,
                          MAX_DAILY_GRANTS, MAX_DELEGATED_MERGES, MAX_GRANTED_ROUNDS)
from .unavailability import delimited

DEFAULT_CONFIG = Path(__file__).resolve().parent / 'config.toml'
# The root of the running dispatcher's installation (a pinned checkout, see docs/devloop/guide.md):
# relative ``sensitive_path_lists`` are read from here, never from a PR branch.
INSTALL_ROOT = Path(__file__).resolve().parent.parent
# The installation has no list of its own: the lists are facts of the managed repository, added
# by the project file (ADR 0006, R3; ssdlc's in ops/host/ssdlc/project.toml).
DEFAULT_SENSITIVE_LISTS = ()
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
# An argument that would replace the settings devloop passes to every claude turn
# (``agents.CLAUDE_TURN_SETTINGS``, #438): the last ``--settings`` counts.
CLAUDE_FORBIDDEN_ARGS = ('--settings',)


class ConfigError(ValueError):
    pass


# Merge modes (#172), from the most restrictive: ``off`` (a person merges), ``strict`` (devloop
# merges a ready PR that meets the merge directives), ``auto`` (as strict, and the supervisor may
# hold a merge).
MERGE_MODES = ('off', 'strict', 'auto')
DEFAULT_OPERATOR_ONLY = ('docs/adr/', 'AGENTS.md', 'CLAUDE.md')
DEFAULT_PROTECTED = ('.github/',)
DEFAULT_BLOCKED = ('bloccata',)


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
    # The directories of the installation, which the pin extracts; ``parse_pin`` adds the lists.
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
class AgentUserPolicy:
    """``[agent_user]`` (#415): ``name``, the system user the agent CLIs and the test command
    run as, through sudo (``agentuser``), or '' to run them as the dispatcher's user, as before;
    ``check``, the startup check that the user cannot read ``key_dir`` nor ``state_dir`` (on by
    default; off only in development); ``cli_denied``, entries of the list of the code of what
    the directories of the agent user's CLIs may hold (``agentuser.CLI_ALLOWED``, #517) that
    they may not hold here: the configuration only shortens that list, never extends it."""
    name: str = ''
    check: bool = True
    cli_denied: tuple = ()


@dataclass(frozen=True)
class DelegationPolicy:
    """``[delegations]`` (ADR 0005): the delegations of the operator to the supervisor, each
    with its revocation key, all off by default (D10) and revoked by turning them off.

    - ``retry_own_prs`` (P5-2, #406): without effect since #527, still accepted so that an
      overlay that sets it keeps loading. The supervisor decides the escalations of process on
      the PRs of its own agent too (principle 4 of ADR 0002 as clarified on #524), whatever its
      value.
    - ``rounds`` (P5-1, #405): the supervisor may grant one more review round to a PR at the
      round limit (``/devloop grant-round``). ``max_rounds_per_pr`` and ``max_grants_per_day``
      lower the caps of the code (``supervision.MAX_GRANTED_ROUNDS`` and ``MAX_DAILY_GRANTS``),
      never raise them.
    - ``budget`` (P5-7, #411): the supervisor may extend once the budget envelope of a story
      whose turn it refused (``/devloop grant-budget``), in turns and time only, by the
      tolerance of the code (``supervision.BUDGET_TOLERANCE_PERCENT``), within
      ``max_grants_per_day``.
    - ``capacity`` (P5-8, #412): the supervisor may set a temporary capacity limit with
      ``/devloop capacity`` on ``[report] issue``. ``capacity_max_open_prs``,
      ``capacity_max_parked_prs`` and ``capacity_max_hours`` lower the caps of the code
      (``supervision.MAX_CAPACITY`` and ``MAX_CAPACITY_HOURS``), never raise them; every grant
      counts against ``max_grants_per_day``.
    - ``merge`` (#486): the supervisor may merge a ready PR whose merge is the operator's with
      ``/devloop merge <sha> <reason>``, when every condition of the delegation holds.
      ``max_merges_per_day`` lowers the cap of the code (``supervision.MAX_DELEGATED_MERGES``),
      a cap of its own, outside ``max_grants_per_day``; ``merge_trust_paths`` adds paths to the
      trust rule of the code (``supervision.TRUST_RULE_PATHS``), which it may only lengthen."""
    retry_own_prs: bool = False
    rounds: bool = False
    budget: bool = False
    max_rounds_per_pr: int = MAX_GRANTED_ROUNDS
    max_grants_per_day: int = MAX_DAILY_GRANTS
    capacity: bool = False
    capacity_max_open_prs: int = MAX_CAPACITY['max_open_prs']
    capacity_max_parked_prs: int = MAX_CAPACITY['max_parked_prs']
    capacity_max_hours: int = MAX_CAPACITY_HOURS
    merge: bool = False
    max_merges_per_day: int = MAX_DELEGATED_MERGES
    merge_trust_paths: tuple = ()

    def capacity_cap(self, key):
        """The cap of the temporary value of ``key`` (#412), None for a key the delegation does
        not cover."""
        return {'max_open_prs': self.capacity_max_open_prs,
                'max_parked_prs': self.capacity_max_parked_prs}.get(key)


@dataclass(frozen=True)
class SupervisorActor:
    """``[roles.supervisor]`` as a table (ADR 0005, P5-4, #408): the supervisor actor, such as
    ``claude-supervisor``. devloop does not run it, so it is not in ``[agents]``; it signs with
    the role app ``[apps.supervisor]``, which no agent declares. ``kind``, ``provider`` and
    ``model`` say which agent runs it, for the check on the PRs implemented by the same agent
    (``SupervisionMixin._supervisor_implemented``)."""
    name: str
    kind: str
    provider: str
    model: str


# The keys of the ``[roles.supervisor]`` table.
SUPERVISOR_KEYS = ('name', 'kind', 'provider', 'model')


@dataclass(frozen=True)
class CoordinatorActor:
    """``[roles.coordinator]`` (ADR 0002, precision of 2026-11-22, #528): the coordinator actor,
    such as ``claude-coordinator``. devloop does not run it, so it is not in ``[agents]``; it
    signs with the role app ``[apps.coordinator]``, which no agent declares, and its commands
    count only within the closed perimeter of ``supervision.COORDINATOR_DELEGATED``. ``kind``,
    ``provider`` and ``model`` say which agent runs it. ``max_merges_per_day``: the daily cap of
    its reserved merges (#529), optional, from 0 to the cap of the code
    (``supervision.MAX_COORDINATOR_MERGES``), which it may only lower."""
    name: str
    kind: str
    provider: str
    model: str
    max_merges_per_day: int = MAX_COORDINATOR_MERGES


# The keys of the ``[roles.coordinator]`` table: those of the supervisor actor, all required,
# and the optional cap of its reserved merges (#529).
COORDINATOR_KEYS = SUPERVISOR_KEYS
COORDINATOR_OPTIONAL = ('max_merges_per_day',)


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
# The gate of the activation of the budget (#396): while it is off, ``[budget] enabled = true`` is
# refused at startup, since a story at its limit could leave it only by turning the budget off
# for every story. The extension exists since #411 (``grant-budget``, ADR 0005, P5-7), as a
# delegation to the supervisor that is off by default (``[delegations] budget``) and applies once
# per story; lifting the gate is the activation of the budget, a decision of the operator (D8 of
# ADR 0004, D10 of ADR 0005), with the points still open before it. The tests of the envelope
# set it; nothing else does.
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
class PromptFacts:
    """``[prompts]`` (ADR 0006, R4): the project's data the prompt templates of the
    installation are filled with. Each is empty without its key, and the template then drops
    the sentence that names it (``context._template``). The text around them stays the
    installation's: the project gives data, never rules."""
    # What the agents read before starting a turn on an issue or a PR, each a Markdown fragment
    # (``\\`AGENTS.md\\```, ``the README section "Developing SSDLC"``), joined as an English list.
    instructions: tuple = ()
    # What the implementer reads before a fix or a sync of its pull request.
    fix_instructions: tuple = ()
    # The command that runs one test module, ``test_name.py`` standing for the module, and the
    # directory of the helpers the modules import, which that form still lets them import.
    module_test_cmd: str = ''
    module_test_helpers: str = ''
    # ``[prompts.adr]``: the directory of the ADRs, reserved to the operator, the heading of the
    # operator's decisions in an ADR, and the words that mark a value and a choice it leaves
    # open. All four or none.
    adr_dir: str = ''
    adr_decided: str = ''
    adr_open_value: str = ''
    adr_open_choice: str = ''


PROMPT_ADR_KEYS = ('dir', 'decided', 'open_value', 'open_choice')


@dataclass(frozen=True)
class FallbackModel:
    """A model an agent falls back to when the ones before it are unavailable (issue #121)."""
    model: str
    model_label: str
    effort: str
    # Who makes the model (ADR 0005, D8, P5-5, #409), lowercase, '' when not declared: then
    # ``provider_of`` reads it from the prefix of the model's name.
    provider: str = ''


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
    # The actor of ``[apps.*]`` the agent signs with (ADR 0005, P5-3, #407), as the agent
    # declares it with ``app``; '' for its key, as before (``Config.app_of``). The implementers
    # declare the role app ``implementer``. Without a table of that name the agent uses the
    # ambient ``gh`` login.
    app: str = ''
    # Who makes ``model`` (ADR 0005, D8, P5-5, #409), lowercase, '' when not declared: then
    # ``provider_of`` reads it from the prefix of the model's name. Required, for the agent and
    # each fallback model, with ``review_policy.provider_quorum``.
    provider: str = ''

    def chain(self):
        """The agent with its configured model, then with each fallback model in order: same
        key, CLI and arguments, with the fallback's model, label, effort and provider (its own,
        never the agent's: agy may fall back on a model of another provider)."""
        return [self] + [dataclasses.replace(self, model=f.model, model_label=f.model_label,
                                             effort=f.effort, provider=f.provider,
                                             fallback_models=(), fallback_for=self.model)
                         for f in self.fallback_models]


# Actors that are an app of devloop's and never an agent: the dispatcher, the merger, the app
# that only merges (#201), the supervisor's role app (ADR 0005, P5-4, #408), which counts only
# with ``[roles.supervisor]`` as a table and whose key devloop never reads, the
# implementer's role app (ADR 0005, P5-3, #407), which the implementers declare, and the
# coordinator's role app (#528), which counts only with ``[roles.coordinator]`` and whose key
# devloop never reads either.
APP_ONLY_ACTORS = ('dispatcher', 'merger', 'supervisor', 'implementer', 'coordinator')
# The role app of the implementers (#407): commits, pushes, PRs and the records of fixes and
# syncs, whichever agent does the work. Only a ``<kind>-implementer`` key may declare it.
IMPLEMENTER_APP = 'implementer'
# The apps that sign for a role and not for an agent: a reviewer's record signed with one of
# them would be the implementer's or the supervisor's (#407).
ROLE_APPS = (IMPLEMENTER_APP, 'supervisor')
# The roles an agent key may name in full (ADR 0005, P5-3, #407): ``<kind>-<role>``, such as
# ``claude-implementer`` or ``codex-reviewer``. A key that names a role serves only that role;
# a key that names none (``claude``, ``codex``, ``agy``, the keys before the migration) serves
# the roles it is given, as before.
KEY_ROLES = ('implementer', 'reviewer')
# The label that chooses the implementer of an issue (#407, D7): ``impl:<kind>`` chooses
# ``<kind>-implementer``.
IMPLEMENTER_LABEL = 'impl:'


def key_role(key):
    """The role ``key`` names in full (``KEY_ROLES``), or None for a key without one."""
    for role in KEY_ROLES:
        if isinstance(key, str) and key.endswith(f'-{role}'):
            return role
    return None


def legacy_key(key, role, agents):
    """The configured key that the key ``key`` of a historical record (a marker, a turn, a
    review) stands for in ``role`` (#407): ``key`` itself when it is configured; for one of the
    keys before the migration (``claude``, ``codex``, ``agy``) no longer configured, its
    ``<kind>-<role>`` when that one is; else ``key``, unknown. The record is never rewritten."""
    if key in agents or key not in AGENT_KINDS:
        return key
    renamed = f'{key}-{role}'
    return renamed if renamed in agents else key


# The providers of the models devloop knows, by the prefix of the model's name (ADR 0005, P5-3,
# #407): ``kind`` is the CLI, not who makes the model, and agy also offers Claude models. Where
# an agent or a fallback declares no provider (``provider``), ``provider_of`` reads it here; where
# one declares it, the table is a check (D8, option (c), P5-5, #409): a model with one of these
# prefixes declared with another provider stops devloop at startup (``_provider``).
MODEL_PROVIDERS = (('claude-', 'anthropic'), ('gpt-', 'openai'), ('gemini-', 'google'))


def prefix_provider(model):
    """The provider the prefix of ``model`` names (``MODEL_PROVIDERS``), or ''."""
    if isinstance(model, str):
        for prefix, provider in MODEL_PROVIDERS:
            if model.lower().startswith(prefix):
                return provider
    return ''


def _field(x, name):
    return x.get(name) if isinstance(x, dict) else getattr(x, name, None)


def provider_of(x):
    """The provider of the model of ``x`` (an ``AgentConfig``, a marker profile, a supervisor
    actor), lowercase: the one it declares (``[roles.supervisor]``), else the one the prefix of
    its model names (``MODEL_PROVIDERS``), else '' when unknown."""
    declared = _field(x, 'provider')
    if isinstance(declared, str) and declared.strip():
        return declared.strip().lower()
    return prefix_provider(_field(x, 'model'))


def same_agent(a, b):
    """Whether ``a`` and ``b`` (an ``AgentConfig``, a marker profile, a supervisor actor:
    anything with ``kind`` and ``model``) are the same agent in effect (ADR 0005, D6, D8, P5-3,
    #407): the same kind of CLI, the same model, or the same provider (``provider_of``) when
    both are known. The conflict checks compare this, never the keys, which differ by role for
    one same agent (``claude-implementer``, ``claude-reviewer``): agy run with a Claude model
    is the same provider as claude. The reviewers are chosen with it until the operator turns on
    ``review_policy.provider_quorum`` (P5-5, #409): then with ``foreign`` and the tuple."""
    provider = provider_of(a)
    return (_field(a, 'kind') == _field(b, 'kind') or _field(a, 'model') == _field(b, 'model')
            or bool(provider and provider == provider_of(b)))


def foreign(reviewer, authors):
    """Whether ``reviewer`` (a profile, an ``AgentConfig``) is of a provider other than every
    one of ``authors`` (ADR 0005, D8, P5-5, #409). Fails closed: a provider that is not known,
    the reviewer's or an author's, is never another one."""
    provider = provider_of(reviewer)
    return bool(provider) and all(provider_of(a) and provider_of(a) != provider
                                  for a in authors)


def provider_quorum(reviewers, authors):
    """The quorum rule (ii) of D8 (ADR 0005, P5-5, #409): at least one of ``reviewers`` is of a
    provider other than every one of ``authors`` (``foreign``). At level 1 the only reviewer
    must be; at level 2 one of the two may share the authors' provider."""
    return any(foreign(r, authors) for r in reviewers)


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
    # The clone of the installation's source (ADR 0006, R1), where ``devloop pin`` takes the
    # code from: absolute, with ``~``, or relative to the managed repository's main checkout.
    # '' when the source is the managed repository itself, as for ssdlc.
    source: str = ''
    # The host directory of the repository locks (ADR 0006, R8), common to every devloop user of
    # the host: absolute, or with ``~``. A dispatcher holds ``<dir>/<owner>--<name>.lock`` of
    # its ``repo`` while it ticks, so that two of them on the same repository never run together
    # whatever their state_dir. '' when it is not set: only the lock of state_dir, as before.
    repo_lock_dir: str = ''
    # The names the repository had before a rename (#540, ADR 0007), a trust key of the
    # operator in the overlay: a link to a comment under one of them counts as one under
    # ``repo`` once the comment is read by id in ``repo`` with the API, and a record of the
    # journal or a marker written under one of them stays the repository's. Empty by default.
    repo_aliases: tuple = ()
    # The reviewer pool, in order (``roles.reviewers``; ``roles.reviewer`` is a pool of one).
    reviewers: tuple = ()
    # The implementers of the area labels (``[roles.implementers]``, ADR 0005, D7, #407):
    # ``(label, key)`` pairs in configuration order. Empty by default: every issue goes to
    # ``implementer`` unless an ``impl:<kind>`` label chooses another.
    implementers: tuple = ()
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
    # Which tests run locally after a writing turn (#169): see ``localtests``. ``changed`` without
    # a selection in ``tests`` is ``full`` (ADR 0006, R5).
    local_tests: str = 'full'
    # Re-run once, without a round, a CI failure whose test modules are not pertinent to the PR
    # (#200, ``Dispatcher._rerun_unrelated``). Off without a CI log format and a selection in
    # ``tests`` (ADR 0006, R5): nothing would tell the failed modules or their pertinence.
    ci_rerun_unrelated: bool = True
    # The selection of the test modules and the format of the CI logs (``[tests]``, ADR 0006,
    # R5), facts of the project: none by default.
    tests: Tests = Tests()
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
    # The supervisor whose identity, besides trusted_users, may hold and release a merge in
    # ``auto`` mode (#172), or '' when none is configured: the agent key ``roles.supervisor``
    # names, whose app it signs with, or the name of ``supervisor_actor``.
    supervisor: str = ''
    # ``[roles.supervisor]`` as a table (#408): the supervisor actor, which signs with the app of
    # ``[apps.supervisor]``; None with the agent key of before.
    supervisor_actor: SupervisorActor = None
    # ``[roles.coordinator]`` (#528): the coordinator actor, which signs with the app of
    # ``[apps.coordinator]``; None, the default (D10), without it: no command of the coordinator
    # counts, and devloop behaves as before.
    coordinator: CoordinatorActor = None
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
    # The system user of the agents and of the tests (#415).
    agent_user: AgentUserPolicy = field(default_factory=AgentUserPolicy)
    # Code paths and their documents, listed in the implement, fix and review prompts (#203).
    docs: DocsMap = field(default_factory=DocsMap)
    # The project's data in the prompts (ADR 0006, R4).
    prompts: PromptFacts = field(default_factory=PromptFacts)

    def agent(self, key):
        return self.agents[key]

    def app_of(self, actor):
        """The actor of ``[apps.*]`` that ``actor`` signs with (#407): a configured agent's
        declared app; any other actor (``dispatcher``, ``merger``, the key of a historical
        record no longer configured) itself."""
        agent = self.agents.get(actor)
        return (agent.app or actor) if agent is not None else actor

    def signs_for_role(self, key):
        """Whether the configured agent ``key`` signs with a role app (``ROLE_APPS``, #407):
        its records count only as the implementer's, never as a review, a readiness check or a
        conclave's."""
        return key in self.agents and self.app_of(key) in ROLE_APPS

    def review_keys(self):
        """The keys whose review records count where history is read (the daily summary,
        #407): the configured agents that do not sign with a role app, and each key before the
        migration (``claude``, ``codex``, ``agy``) no longer configured whose ``<kind>-reviewer``
        is one of them (``legacy_key``): its records are read as they are, never rewritten."""
        keys = {key for key in self.agents if not self.signs_for_role(key)}
        return frozenset(keys | {kind for kind in AGENT_KINDS
                                 if legacy_key(kind, 'reviewer', self.agents) in keys - {kind}})

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
        unknown = set(item) - {'model', 'model_label', 'effort', 'provider'}
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
        models.append(FallbackModel(model=name, model_label=label, effort=level,
                                    provider=_provider(item, at, name)))
    return tuple(models)


def _provider(table, where, model):
    """``provider`` of an agent or of a fallback model (ADR 0005, D8, P5-5, #409): who makes
    ``model``, lowercase, or '' when not declared. A model whose prefix names a provider
    (``MODEL_PROVIDERS``) declared with another one stops devloop: the table is the check of
    option (c), for agy, which also offers Claude models."""
    if 'provider' not in table:
        return ''
    provider = _require(table, 'provider', str, where).strip().lower()
    if not provider:
        raise ConfigError(f'{where}.provider: must not be empty')
    known = prefix_provider(model)
    if known and known != provider:
        raise ConfigError(f'{where}.provider: {model!r} is a model of {known!r}, not of '
                          f'{provider!r}')
    return provider


def _providers(agent):
    """``(providers,)``: the declared providers of ``agent``'s model and of each fallback, ''
    each when not declared, as ``ReviewPolicy.reviewers`` records them; ``()`` when none is
    declared."""
    providers = (agent.provider, *(f.provider for f in agent.fallback_models))
    return (providers,) if any(providers) else ()


def _check_providers(agents, rule='review_policy.provider_quorum'):
    """With ``review_policy.provider_quorum`` (P5-5, #409) or ``fallback_provider_ban`` (P5-6,
    #410), ``rule``, every agent and every fallback model declares its provider (D8, option
    (c)): the rule never reads it from a prefix, and the fallbacks the implementer and the fixers
    run are recorded with theirs."""
    for key, agent in agents.items():
        if not agent.provider:
            raise ConfigError(f'agents.{key}.provider: required with {rule}')
        for position, fallback in enumerate(agent.fallback_models):
            if not fallback.provider:
                raise ConfigError(f'agents.{key}.fallback_models[{position}].provider: required '
                                  f'with {rule}')


def _check_reviewer_fallbacks(agents, pool, implementers):
    """With ``review_policy.fallback_provider_ban`` (ADR 0005, P5-6, #410), the check at
    startup: no reviewer of ``pool`` has a fallback model of a provider other than its own
    model's and the same as an agent's of ``implementers``, the keys that may implement (agy
    falling back on Claude while claude implements). The ban at runtime still holds the rule:
    the authors of a PR are known only per PR."""
    providers = {provider_of(agents[key]): key for key in sorted(implementers)}
    providers.pop('', None)
    for key in pool:
        agent = agents[key]
        for position, fallback in enumerate(agent.fallback_models):
            provider = provider_of(fallback)
            if provider != provider_of(agent) and provider in providers:
                raise ConfigError(
                    f'agents.{key}.fallback_models[{position}].provider: {provider!r} is the '
                    f'provider of {providers[provider]!r}, which may implement, and not of the '
                    'reviewer\'s model: refused with review_policy.fallback_provider_ban')


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
            re.compile(delimited(pattern))  # as unavailable_reason uses it (#443)
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
    default the tokens of ``test_cmd`` (none when it is empty) followed by
    ``AGY_DEFAULT_COMMANDS``, for both modes. ``write_args`` and ``review_args`` may not bypass
    or replace them, and those of a claude agent may not replace its ``--settings`` (#438)."""
    keys = ('write_commands', 'review_commands')
    if kind == 'claude':
        for key in ('write_args', 'review_args'):
            for arg in _str_list(table, key, where):
                if arg.split('=', 1)[0] in CLAUDE_FORBIDDEN_ARGS:
                    raise ConfigError(f'{where}.{key}: {arg.split("=", 1)[0]} is not allowed: '
                                      'devloop passes the settings of each claude turn itself, '
                                      'with the auto memory off (#438)')
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
        raw = (_str_list(table, key, where) if configured
               else (*((test_cmd,) if test_cmd else ()), *AGY_DEFAULT_COMMANDS))
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
        raise _unknown_agent(f'{where}.advisory_reviewer', advisory, agents, ('reviewer',))
    if advisory:
        _check_role(advisory, 'reviewer', f'{where}.advisory_reviewer')
    findings = _findings_reviewer(table, default, [r[0] for r in reviewers], agents)
    quorum = default.provider_quorum
    if 'provider_quorum' in table:
        quorum = _require(table, 'provider_quorum', bool, where)
    ban = default.fallback_provider_ban
    if 'fallback_provider_ban' in table:
        ban = _require(table, 'fallback_provider_ban', bool, where)
    # When the second reviewer of a level 2 round starts (#521): an unknown mode stops devloop.
    second = table.get('second_reviewer', default.second_reviewer)
    if not isinstance(second, str) or second not in SECOND_REVIEWER_MODES:
        raise ConfigError(f'{where}.second_reviewer: unknown mode {second!r}; expected one of '
                          + ', '.join(f'"{mode}"' for mode in SECOND_REVIEWER_MODES))
    return ReviewPolicy(default_level=level, label=label, sensitive_paths=paths,
                        sensitive_files=tuple(files), reviewers=tuple(reviewers),
                        zero_label=zero_label, zero_allowlist=allowlist, zero_checks=checks,
                        advisory_reviewer=advisory, findings_reviewer=findings,
                        provider_quorum=quorum, fallback_provider_ban=ban,
                        second_reviewer=second)


def _findings_reviewer(table, default, pool, agents):
    """``findings_reviewer`` (ADR 0005, D8, integration of 2026-11-19, #416): '' (off, D10) or a
    configured key that may review (not an implementer's), outside the pool ``pool``, so that
    it never enters the quorum, not even in the place of a missing reviewer. A key that signs
    with a role app never reviews (``Dispatcher._findings_reviewers``)."""
    where = 'review_policy.findings_reviewer'
    key = table.get('findings_reviewer', default.findings_reviewer)
    if not isinstance(key, str):
        raise ConfigError(f'{where}: expected a string')
    if not key:
        return key
    _check_role(key, 'reviewer', where)
    if agents is not None and key not in agents:
        raise ConfigError(f'{where}: unknown agent {key!r}')
    if key in pool:
        raise ConfigError(f'{where}: {key!r} is in the reviewer pool (roles.reviewers): the '
                          'findings-only reviewer never counts for the quorum, nor takes the '
                          'place of a missing reviewer')
    return key


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


TESTS_KEYS = ('selection', 'source_prefixes', 'dir_modules', 'ci_log_format')
_NAME_PART = re.compile(r'\w+', re.ASCII)
_MODULE_PREFIX = re.compile(r'test_\w*', re.ASCII)


def parse_tests(table):
    """``[tests]`` (ADR 0006, R5), facts of the project, each optional: ``selection``, one of
    ``localtests.SELECTIONS``, with its parameters ``source_prefixes`` (name prefixes such as
    ``"ssdlc_"``) and ``dir_modules`` (``"<directory>/" = "test_<prefix>"``), which need it;
    ``ci_log_format``, one of ``localtests.CI_LOG_FORMATS``. Anything else stops devloop."""
    if not isinstance(table, dict):
        raise ConfigError('tests: expected a table')
    unknown = set(table) - set(TESTS_KEYS)
    if unknown:
        raise ConfigError(f'tests: unknown keys {sorted(unknown)}')
    values = {}
    for key, names in (('selection', SELECTIONS), ('ci_log_format', CI_LOG_FORMATS)):
        if key in table:
            value = table[key]
            if not isinstance(value, str) or value not in names:
                raise ConfigError(f'tests.{key}: must be one of {names}')
            values[key] = value
    prefixes = _str_list(table, 'source_prefixes', 'tests')
    for prefix in prefixes:
        if not _NAME_PART.fullmatch(prefix):
            raise ConfigError(f'tests.source_prefixes: {prefix!r} is not a name prefix '
                              '(letters, digits and "_")')
    dirs = table.get('dir_modules', {})
    if not isinstance(dirs, dict):
        raise ConfigError('tests.dir_modules: expected a table of "<directory>/" = '
                          '"test_<prefix>"')
    pairs = []
    for directory, prefix in dirs.items():
        at = f'tests.dir_modules.{directory!r}'
        parts = directory.split('/')
        if (not directory.endswith('/') or directory.startswith('/') or '\\' in directory
                or any(part in ('', '.', '..') for part in parts[:-1])):
            raise ConfigError(f'{at}: expected a relative directory ending in "/"')
        if not isinstance(prefix, str) or not _MODULE_PREFIX.fullmatch(prefix):
            raise ConfigError(f'{at}: expected the prefix of test modules, "test_<name>"')
        pairs.append((directory, prefix))
    if (prefixes or pairs) and 'selection' not in values:
        raise ConfigError('tests: source_prefixes and dir_modules are parameters of a '
                          'selection: they need tests.selection')
    return Tests(source_prefixes=prefixes, dir_modules=tuple(pairs), **values)


def parse_delegations(table):
    """``[delegations]`` (ADR 0005): ``retry_own_prs`` (#406), ``rounds`` (#405), ``budget``
    (#411), ``capacity`` (#412) and ``merge`` (#486), true or false (default false, D10);
    ``max_rounds_per_pr``, ``max_grants_per_day`` and ``max_merges_per_day``, whole numbers from
    0 to the caps of the code; ``capacity_max_open_prs``, ``capacity_max_parked_prs`` and
    ``capacity_max_hours``, whole numbers from 1 to the caps of the code; ``merge_trust_paths``,
    a list of paths (files, or directories ending in ``/``) the trust rule of the code gains. A
    value above a cap is refused: the configuration and the overlay may only lower the caps
    (D4), and only lengthen the trust rule."""
    if not isinstance(table, dict):
        raise ConfigError('delegations: expected a table')
    unknown = set(table) - {f.name for f in dataclasses.fields(DelegationPolicy)}
    if unknown:
        raise ConfigError(f'delegations: unknown keys {sorted(unknown)}')
    values = {}
    for key in ('retry_own_prs', 'rounds', 'budget', 'capacity', 'merge'):
        if key in table:
            if not isinstance(table[key], bool):
                raise ConfigError(f'delegations.{key}: expected true or false')
            values[key] = table[key]
    for key, low, cap in (('max_rounds_per_pr', 0, MAX_GRANTED_ROUNDS),
                          ('max_grants_per_day', 0, MAX_DAILY_GRANTS),
                          ('capacity_max_open_prs', 1, MAX_CAPACITY['max_open_prs']),
                          ('capacity_max_parked_prs', 1, MAX_CAPACITY['max_parked_prs']),
                          ('capacity_max_hours', 1, MAX_CAPACITY_HOURS),
                          ('max_merges_per_day', 0, MAX_DELEGATED_MERGES)):
        if key in table:
            value = table[key]
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= cap:
                raise ConfigError(f'delegations.{key}: expected a whole number from {low} to '
                                  f'{cap}, the cap of the code, which the configuration may only '
                                  'lower')
            values[key] = value
    if 'merge_trust_paths' in table:
        paths = table['merge_trust_paths']
        if (not isinstance(paths, list)
                or not all(isinstance(p, str) and p.strip().strip('/') for p in paths)):
            raise ConfigError('delegations.merge_trust_paths: expected a list of paths, each a '
                              'file or a directory ending in "/"; it may only add to the trust '
                              'rule of the code')
        values['merge_trust_paths'] = tuple(paths)
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


_USER_NAME = re.compile(r'[a-z_][a-z0-9_-]{0,31}')


def parse_agent_user(table):
    """``[agent_user]`` (#415): ``name``, a system user name or '' (default: off); ``check``, a
    boolean (default true); ``cli_denied`` (#517), a list of entries of
    ``agentuser.CLI_ALLOWED`` (default none): any other is refused."""
    if not isinstance(table, dict):
        raise ConfigError('agent_user: expected a table')
    unknown = set(table) - {f.name for f in dataclasses.fields(AgentUserPolicy)}
    if unknown:
        raise ConfigError(f'agent_user: unknown keys {sorted(unknown)}')
    values = {}
    if 'name' in table:
        name = table['name']
        if not isinstance(name, str) or name and not _USER_NAME.fullmatch(name):
            raise ConfigError('agent_user.name: expected a system user name, or "" for none')
        values['name'] = name
    if 'check' in table:
        if not isinstance(table['check'], bool):
            raise ConfigError('agent_user.check: expected true or false')
        values['check'] = table['check']
    if 'cli_denied' in table:
        denied = table['cli_denied']
        if not isinstance(denied, list) or not all(isinstance(e, str) for e in denied):
            raise ConfigError('agent_user.cli_denied: expected a list of entries of the CLIs, '
                              'such as ".codex/memories_1.sqlite*"')
        try:
            allowed_entries(denied)
        except ValueError as exc:
            raise ConfigError(f'agent_user.cli_denied: {exc}') from None
        values['cli_denied'] = tuple(dict.fromkeys(denied))
    return AgentUserPolicy(**values)


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
    refused while the gate of the activation is closed (``BUDGET_EXTENSION``, #396). A token cap
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
                f'{where}.enabled: the budget cannot be turned on yet: the gate of its '
                'activation (BUDGET_EXTENSION, #396) stays closed until the operator lifts it. '
                'A story that reaches its limit has only the one extension the supervisor may '
                'grant (grant-budget, ADR 0005, with [delegations] budget), and beyond it the '
                'only way out would be enabled = false for every story. Keep enabled = false')
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


def _prompt_text(value, where):
    """A value of ``[prompts]``: a string on one line, not empty, put in a prompt as it is."""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f'{where}: expected a non-empty string')
    if value != value.strip() or any(c in value for c in '\r\n'):
        raise ConfigError(f'{where}: expected one line without leading or trailing spaces')
    return value


def parse_prompts(table, merge):
    """``[prompts]`` (ADR 0006, R4): the project's data in the prompts (``PromptFacts``). The
    ADR directory must be reserved to the operator (``merge``'s ``operator_only_paths``): the
    preflight takes its decisions as settled because only the operator merges them there."""
    if not isinstance(table, dict):
        raise ConfigError('prompts: expected a table')
    keys = {'instructions', 'fix_instructions', 'module_test_cmd', 'module_test_helpers', 'adr'}
    unknown = set(table) - keys
    if unknown:
        raise ConfigError(f'prompts: unknown keys {sorted(unknown)}')
    values = {}
    for key in ('instructions', 'fix_instructions'):
        items = table.get(key, [])
        if not isinstance(items, list):
            raise ConfigError(f'prompts.{key}: expected a list of strings')
        values[key] = tuple(_prompt_text(item, f'prompts.{key}') for item in items)
    for key in ('module_test_cmd', 'module_test_helpers'):
        if key in table:
            values[key] = _prompt_text(table[key], f'prompts.{key}')
    if values.get('module_test_helpers') and not values.get('module_test_cmd'):
        raise ConfigError('prompts.module_test_helpers: needs prompts.module_test_cmd, the '
                          'command the helpers are imported with')
    if 'adr' in table:
        adr = table['adr']
        if not isinstance(adr, dict):
            raise ConfigError('prompts.adr: expected a table')
        unknown = set(adr) - set(PROMPT_ADR_KEYS)
        if unknown:
            raise ConfigError(f'prompts.adr: unknown keys {sorted(unknown)}')
        missing = [key for key in PROMPT_ADR_KEYS if key not in adr]
        if missing:
            raise ConfigError(f'prompts.adr: missing {missing}: the four keys go together')
        for key in PROMPT_ADR_KEYS:
            values[f'adr_{key}'] = _prompt_text(adr[key], f'prompts.adr.{key}')
        directory = values['adr_dir']
        _docs_path(directory, 'prompts.adr.dir', document=False)
        if not directory.endswith('/'):
            raise ConfigError(f'prompts.adr.dir: {directory!r} is not a directory (ending in "/")')
        if not merge.operator_only(directory):
            raise ConfigError(f'prompts.adr.dir: {directory!r} is not in [merge] '
                              'operator_only_paths: the preflight takes the decisions of its '
                              'ADRs as the operator\'s only when the operator alone merges them')
    return PromptFacts(**values)


# The roles without keys of their own that run read-only, as a review, and so take a reviewer
# key too (ADR 0005, precisazione del 2026-11-21 di D6, #499): never an implementer key.
READ_ONLY_ROLES = ('preflight', 'conclave')


def _check_role(key, role, where):
    """Refuse the key ``key`` given the role ``role`` when it names another one (ADR 0005,
    P5-3, #407): ``claude-reviewer`` as ``roles.implementer`` stops devloop at startup. A key
    that names no role (``key_role``) serves the roles it is given, as before the migration.
    The read-only roles without keys of their own (``READ_ONLY_ROLES``: preflight, conclave)
    take a reviewer key too (#499); the supervisor as an agent key takes only a key without a
    role."""
    named = key_role(key)
    if named == 'reviewer' and role in READ_ONLY_ROLES:
        return
    if named is not None and named != role:
        raise ConfigError(f'{where}: {key!r} is a {named} key and serves only that role, not '
                          f'the {role}')


_MIGRATION_STEP = '(ops/host/migrate.md, "Chiavi per ruolo su un host attivo")'


def _unknown_agent(where, key, agents, roles=KEY_ROLES, instead=''):
    """The error of ``where`` naming the agent ``key`` that is not configured. For a key before
    the migration (``claude``, ``codex``, ``agy``) once the agents have keys by role (#499), it
    says which of its keys of ``roles``, the roles ``where`` takes, to use instead (or
    ``instead``), and where the migration is described."""
    message = f'{where}: unknown agent {key!r}'
    if key in AGENT_KINDS and any(key_role(k) for k in agents):
        renamed = [f'{key}-{role}' for role in roles if f'{key}-{role}' in agents]
        if renamed:
            instead = f'use {" or ".join(map(repr, renamed))}'
        elif not instead:
            instead = f'and no key of {key} serves here'
        message += f': the agents have keys by role, {instead} {_MIGRATION_STEP}'
    return ConfigError(message)


def parse_implementers(roles, agents):
    """``[roles.implementers]`` (ADR 0005, D7, #407): the implementer of each area label, as
    ``(label, key)`` pairs; ``()`` without the table. Every key is a configured agent that may
    implement; an ``impl:`` label chooses by itself and is not an area."""
    table = roles.get('implementers', {})
    where = 'roles.implementers'
    if not isinstance(table, dict):
        raise ConfigError(f'{where}: expected a table of "<label>" = "<implementer key>"')
    pairs = []
    for label, key in table.items():
        at = f'{where}.{label!r}'
        if not label.strip() or label.startswith(IMPLEMENTER_LABEL):
            raise ConfigError(f'{at}: expected an area label, not empty nor an '
                              f'{IMPLEMENTER_LABEL}<kind> label')
        if not isinstance(key, str):
            raise ConfigError(f'{at}: expected the key of an implementer')
        if key not in agents:
            raise ConfigError(f'{at}: unknown agent {key!r}')
        _check_role(key, 'implementer', at)
        pairs.append((label, key))
    return tuple(pairs)


def _check_apps(agents, declared, apps):
    """The apps the agents sign with (ADR 0005, P5-3, #407). ``declared``: the keys of the
    agents with an ``app`` of their own. Each ``[apps.*]`` table is one actor's, devloop's own
    or one agent's, except ``implementer``, which every ``<kind>-implementer`` may declare and
    nobody else; no agent declares devloop's own apps; an implementer key signs with the
    implementer app or with the ambient login, never with an agent's app."""
    owners = {}
    for key, agent in agents.items():
        app, where = agent.app or key, f'agents.{key}.app'
        if app in APP_ONLY_ACTORS and app != IMPLEMENTER_APP:
            raise ConfigError(f'{where}: {app!r} is reserved to devloop\'s own app')
        if app == IMPLEMENTER_APP and key_role(key) != 'implementer':
            raise ConfigError(f'{where}: only a <kind>-implementer key may declare the '
                              'implementer app: a review or a check signed with it would be the '
                              'implementer\'s')
        if key_role(key) == 'implementer' and app != IMPLEMENTER_APP and app in apps:
            raise ConfigError(f'{where}: an implementer signs with the implementer app '
                              f'(app = "{IMPLEMENTER_APP}"), not with {app!r}')
        if key in declared and app not in apps:
            raise ConfigError(f'{where}: no [apps.{app}] table')
        if app != IMPLEMENTER_APP:
            owners.setdefault(app, []).append(key)
    for app, keys in owners.items():
        if app in apps and len(keys) > 1:
            raise ConfigError(f'apps.{app}: each app belongs to one actor, declared by '
                              + ', '.join(sorted(keys)))
    for actor in apps:
        # The app of an agent's key before the migration (``claude``, ``codex``, ``agy``) stays
        # even when no key declares it: the records it signed are read with it.
        if actor not in APP_ONLY_ACTORS and actor not in owners and actor not in AGENT_KINDS:
            raise ConfigError(f'apps.{actor}: not the dispatcher, the merger, a role app nor the '
                              'app of a configured agent')


def _reviewer_pool(roles, agents, implementers):
    """``roles.reviewers``, or ``roles.reviewer`` as a pool of one: known agents with a
    reviewer's key, no repeats, none of the keys that may implement (``implementers``): a key
    serves one role (#407). Whether a reviewer is the same agent as the implementer of a PR is
    checked for each PR, never here (ADR 0005, D6)."""
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
            raise _unknown_agent('roles.reviewers', key, agents, ('reviewer',))
        _check_role(key, 'reviewer', 'roles.reviewers')
        if key in implementers:
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
        raise _unknown_agent('conclave', unknown[0], agents, ('reviewer',))
    for key in keys:
        _check_role(key, 'conclave', 'conclave')
    tuples = {(agents[k].kind, agents[k].model, agents[k].effort) for k in keys}
    if len(tuples) != CONCLAVE_SIZE:
        raise ConfigError('conclave: the agents must differ in (kind, model, effort)')
    return keys


def parse_supervisor(roles, agents):
    """``(supervisor, actor)`` of ``roles.supervisor``: ``('', None)`` without it. As a string
    it is the key of a configured agent, which signs with that agent's app (#172), and
    ``actor`` is None. As a table (ADR 0005, P5-4, #408) it is the supervisor actor, with the
    keys of ``SUPERVISOR_KEYS``: ``name`` names agent and role in full (``<kind>-supervisor``)
    and is not a key of ``[agents]``, since devloop does not run it."""
    if 'supervisor' not in roles:
        return '', None
    value = roles['supervisor']
    if isinstance(value, str):
        if value not in agents:
            # A key with a role is never the supervisor: with the keys by role it is the table.
            raise _unknown_agent('roles.supervisor', value, agents, (),
                                 'and a key with a role is never the supervisor: use the table '
                                 f'[roles.supervisor], name = "{value}-supervisor"')
        _check_role(value, 'supervisor', 'roles.supervisor')
        return value, None
    if not isinstance(value, dict):
        raise ConfigError('roles.supervisor: expected the key of an agent or a table')
    where = 'roles.supervisor'
    unknown = sorted(set(value) - set(SUPERVISOR_KEYS))
    if unknown:
        raise ConfigError(f'{where}: unknown key {unknown[0]!r}')
    name, kind, provider, model = (_require(value, key, str, where) for key in SUPERVISOR_KEYS)
    if kind not in AGENT_KINDS:
        raise ConfigError(f'{where}.kind: must be one of {AGENT_KINDS}')
    if name != f'{kind}-supervisor':
        raise ConfigError(f'{where}.name: must be {kind + "-supervisor"!r}, the agent and the '
                          'role in full')
    if name in agents:
        raise ConfigError(f'{where}.name: {name!r} is an agent of [agents]; the supervisor '
                          'actor is not run by devloop')
    for key, text in (('provider', provider), ('model', model)):
        if not text.strip():
            raise ConfigError(f'{where}.{key}: must not be empty')
    return name, SupervisorActor(name=name, kind=kind, provider=provider, model=model)


def parse_coordinator(roles, agents):
    """The coordinator actor of ``[roles.coordinator]`` (#528), or None without it (off by
    default, D10). Only a table, with the keys of ``COORDINATOR_KEYS``: ``name`` names agent and
    role in full (``<kind>-coordinator``) and is not a key of ``[agents]``, since devloop does
    not run it. ``max_merges_per_day`` (#529), optional, may only lower
    ``MAX_COORDINATOR_MERGES``."""
    if 'coordinator' not in roles:
        return None
    value = roles['coordinator']
    where = 'roles.coordinator'
    if not isinstance(value, dict):
        raise ConfigError(f'{where}: expected a table')
    unknown = sorted(set(value) - set(COORDINATOR_KEYS) - set(COORDINATOR_OPTIONAL))
    if unknown:
        raise ConfigError(f'{where}: unknown key {unknown[0]!r}')
    name, kind, provider, model = (_require(value, key, str, where) for key in COORDINATOR_KEYS)
    cap = value.get('max_merges_per_day', MAX_COORDINATOR_MERGES)
    if not isinstance(cap, int) or isinstance(cap, bool) or not 0 <= cap <= MAX_COORDINATOR_MERGES:
        raise ConfigError(f'{where}.max_merges_per_day: expected a whole number from 0 to '
                          f'{MAX_COORDINATOR_MERGES}, the cap of the code, which the '
                          'configuration may only lower')
    if kind not in AGENT_KINDS:
        raise ConfigError(f'{where}.kind: must be one of {AGENT_KINDS}')
    if name != f'{kind}-coordinator':
        raise ConfigError(f'{where}.name: must be {kind + "-coordinator"!r}, the agent and the '
                          'role in full')
    if name in agents:
        raise ConfigError(f'{where}.name: {name!r} is an agent of [agents]; the coordinator '
                          'actor is not run by devloop')
    for key, text in (('provider', provider), ('model', model)):
        if not text.strip():
            raise ConfigError(f'{where}.{key}: must not be empty')
    return CoordinatorActor(name=name, kind=kind, provider=provider, model=model,
                            max_merges_per_day=cap)


def parse_config(data, root=INSTALL_ROOT, project='', agent_test_cmd=None):
    """``root`` is where relative ``review_policy.sensitive_path_lists`` are read. ``project``
    is the SHA-256 of the project file (ADR 0006, R2) composed into ``data``, '' without one:
    it enters the version of the review policy, and so every receipt. ``agent_test_cmd``, when
    given, is the test command the default commands of agy derive from instead of
    ``test_cmd``: the agents' permissions are the installation's and the overlay's (D2)."""
    refuse_removed(data, 'config')
    if 'test_cmd' not in data:
        # The installation has none since ADR 0006, R3: it is a fact of the project.
        raise ConfigError("config: missing 'test_cmd': set it in the project file on the host, "
                          '<state_dir>/project.toml (--project-config; the launcher passes it)')
    test_cmd = _require(data, 'test_cmd', str, 'config')
    agent_cmd = test_cmd if agent_test_cmd is None else agent_test_cmd
    agents, declared = {}, set()
    tables = data.get('agents') or {}
    for key, table in tables.items():
        where = f'agents.{key}'
        if key in APP_ONLY_ACTORS:
            raise ConfigError(f'{where}: {key!r} is reserved to devloop\'s own app')
        renamed = [f'{key}-{role}' for role in KEY_ROLES if f'{key}-{role}' in tables]
        if key in AGENT_KINDS and renamed:
            # #499: config.toml names the agents by role, and a local overlay written before
            # still has the table of the key of before, which the overlay cannot remove.
            raise ConfigError(f'{where}: the agents have keys by role: rename [{where}] of the '
                              'local overlay to '
                              + ' or '.join(f'[agents.{name}]' for name in renamed)
                              + ', and the keys that name it (preflight, conclave, roles, '
                              f'review_policy) to the new keys {_MIGRATION_STEP}')
        kind = _require(table, 'kind', str, where)
        if kind not in AGENT_KINDS:
            raise ConfigError(f'{where}.kind: must be one of {AGENT_KINDS}')
        role = key_role(key)
        if role is not None and key != f'{kind}-{role}':
            raise ConfigError(f'{where}: a key with a role names the agent and the role in full: '
                              f'{kind + "-" + role!r}')
        app = ''  # the key's own app, as before (``Config.app_of``)
        if 'app' in table:
            app = _require(table, 'app', str, where)
            if not app.strip():
                raise ConfigError(f'{where}.app: must not be empty')
            declared.add(key)
        model = _require(table, 'model', str, where)
        effort = _require(table, 'effort', str, where)
        patterns, codes = _unavailable(table, where)
        write_commands, review_commands = _agy_commands(table, where, kind, agent_cmd)
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
            write_commands=write_commands, review_commands=review_commands, app=app,
            provider=_provider(table, where, model))
    roles = data.get('roles') or {}
    implementer = _require(roles, 'implementer', str, 'roles')
    if implementer not in agents:
        raise _unknown_agent('roles.implementer', implementer, agents, ('implementer',))
    _check_role(implementer, 'implementer', 'roles.implementer')
    implementers = parse_implementers(roles, agents)
    # The keys that may implement an issue: the default, those of the area map and every
    # <kind>-implementer, which an impl:<kind> label chooses (#407).
    selectable = ({implementer} | {key for _, key in implementers}
                  | {key for key in agents if key_role(key) == 'implementer'})
    pool = _reviewer_pool(roles, agents, selectable)
    supervisor, supervisor_actor = parse_supervisor(roles, agents)
    coordinator = parse_coordinator(roles, agents)
    preflight = ''
    if 'preflight' in data:
        preflight = _require(data, 'preflight', str, 'config')
        if preflight not in agents:
            raise _unknown_agent('preflight', preflight, agents, ('reviewer',))
        # A reviewer key, read-only as a review, or a key without a role (#499).
        _check_role(preflight, 'preflight', 'preflight')
        # The implementer chosen for an issue is checked against it at the takeover (#407).
        if preflight in selectable:
            raise ConfigError('preflight: must be a different agent from the implementer')
    conclave = parse_conclave(data, agents)
    trusted =_str_list(data, 'trusted_users', 'config')
    if not trusted:
        # The installation has none since ADR 0006, R6: trust is the project's, in the overlay.
        raise ConfigError('trusted_users: at least one GitHub login is required: set it in the '
                          'local overlay on the host, <state_dir>/config.local.toml')
    if any(login.endswith('[bot]') for login in trusted):
        raise ConfigError('trusted_users: bots and apps cannot approve work')
    if 'repo' not in data:
        # The installation has none since ADR 0006, R6: the managed repository is the project's.
        # Checked before the apps, which the agents of the installation declare (#499).
        raise ConfigError("config: missing 'repo': set it in the local overlay on the host, "
                          '<state_dir>/config.local.toml (--local-config; the launcher passes it)')
    apps_table = dict(data.get('apps') or {})
    key_dir = apps_table.pop('key_dir', '~/.ghapps')
    if not isinstance(key_dir, str):
        raise ConfigError('apps.key_dir: expected str')
    apps = {}
    for actor, table in apps_table.items():
        where = f'apps.{actor}'
        if not isinstance(table, dict):
            raise ConfigError(f'{where}: expected a table')
        apps[actor] = AppConfig(app_id=_require(table, 'app_id', int, where),
                                slug=_require(table, 'slug', str, where))
    _check_apps(agents, declared, apps)
    if len({app.app_id for app in apps.values()}) != len(apps):
        raise ConfigError('apps: each actor needs its own app')
    if supervisor_actor is not None and 'supervisor' not in apps:
        # The actor exists to sign with its role app: without it nobody would be the supervisor.
        raise ConfigError('roles.supervisor: the supervisor actor needs [apps.supervisor]')
    if coordinator is not None and 'coordinator' not in apps:
        # The coordinator exists to sign with its role app (#528): without it nobody would be.
        raise ConfigError('roles.coordinator: the coordinator actor needs [apps.coordinator]')
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
    tests = parse_tests(data.get('tests', {}))
    # ADR 0006, R5: without a selection ``changed`` is ``full``, a project without a strategy
    # loses no test; without a format of the CI logs and a selection nothing tells the failed
    # modules or their pertinence, so a CI failure is never re-run without a round.
    if local_tests == 'changed' and not tests.selection:
        local_tests = 'full'
    ci_rerun = ci_rerun and bool(tests.ci_log_format) and bool(tests.selection)
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
        [(k, apps[agents[k].app or k].slug if (agents[k].app or k) in apps else None,
          agents[k].kind, agents[k].model,
          agents[k].effort, agents[k].review_args,
          tuple((f.model, f.effort) for f in agents[k].fallback_models),
          agents[k].review_commands)
         # The declared providers (#409) only when there are some: without them the pool is
         # recorded as before.
         + _providers(agents[k]) for k in pool], agents)
    if review_policy.provider_quorum:
        _check_providers(agents)
    if review_policy.fallback_provider_ban:
        _check_providers(agents, 'review_policy.fallback_provider_ban')
        _check_reviewer_fallbacks(agents, pool, selectable)
    if project:
        review_policy = dataclasses.replace(review_policy, project=project)
    # The roles whose turns can run (#365): conclave and advisory only when configured.
    budget_roles = (BUDGET_BASE_ROLES + (('conclave',) if conclave else ())
                    + (('advisory',) if review_policy.advisory_reviewer else ()))
    budget = parse_budget(data.get('budget', {}), agents, budget_roles, story_identity)
    report = parse_report(data.get('report', {}))
    delegations = parse_delegations(data.get('delegations', {}))
    if delegations.capacity and not report.issue:
        # The command of a temporary capacity limit lives on the issue of the summary (#412).
        raise ConfigError('delegations.capacity: needs [report] issue, where the supervisor '
                          'writes `/devloop capacity`')
    if delegations.merge and not review_policy.advisory_reviewer:
        # A delegated merge needs the advisory review of the head it merges (#486).
        raise ConfigError('delegations.merge: needs review_policy.advisory_reviewer: a delegated '
                          'merge needs an advisory review of the head, `ok` or `dubbi`')
    merge = parse_merge(data.get('merge', {}), supervisor)
    return Config(
        repo=_require(data, 'repo', str, 'config'),
        base_branch=data.get('base_branch', 'main'),
        branch_prefix=data.get('branch_prefix', 'agent/issue-'),
        state_dir=data.get('state_dir', '.ssdlc/devloop'),
        source=_source(data),
        repo_lock_dir=_repo_lock_dir(data),
        repo_aliases=_repo_aliases(data),
        test_cmd=test_cmd,
        language=data.get('language', 'English'),
        implementer=implementer, implementers=implementers, reviewer=pool[0], reviewers=pool,
        agents=agents,
        preflight=preflight, conclave=conclave,
        trusted_users=trusted,
        blocked_labels=_str_list(data, 'blocked_labels', 'config', DEFAULT_BLOCKED),
        priority_labels=_str_list(data, 'priority_labels', 'config', ('prerequisito',)),
        protected_paths=_str_list(data, 'protected_paths', 'config', DEFAULT_PROTECTED),
        apps=apps, app_key_dir=key_dir, agent_stall_action=stall_action,
        local_tests=local_tests, ci_rerun_unrelated=ci_rerun, tests=tests,
        block_uncertain_turns=block_uncertain, story_identity=story_identity,
        publication_outbox=outbox, attempt_identity=attempt_identity, budget=budget,
        merge=merge, supervisor=supervisor,
        supervisor_actor=supervisor_actor, coordinator=coordinator,
        pin=parse_pin(data.get('pin', {}), sensitive_lists(data)),
        run=parse_run(data.get('run', {})),
        report=report,
        ci=parse_ci(data.get('ci', {})),
        gc=parse_gc(data.get('gc', {})),
        agent_user=parse_agent_user(data.get('agent_user', {})),
        delegations=delegations,
        docs=parse_docs(data),
        prompts=parse_prompts(data.get('prompts', {}), merge),
        review_policy=review_policy,
        **ints)


def _source(data):
    """``source`` (ADR 0006, R1): a path that is not empty, or '' when the key is not set."""
    if 'source' not in data:
        return ''
    value = _require(data, 'source', str, 'config')
    if not value.strip():
        raise ConfigError("source: expected the path of a clone of the installation's source; "
                          'leave the key out when the source is the managed repository')
    return value


# ``repo`` as GitHub names a repository: an owner of letters and digits with single hyphens
# inside, and a name of letters, digits, ``.``, ``_`` and ``-``.
_REPO = re.compile(r'([A-Za-z0-9](?:-?[A-Za-z0-9])*)/([A-Za-z0-9._-]+)')


def repo_lock_name(repo):
    """The name of the lock file of ``repo`` (ADR 0006, R8): owner and name in lower case,
    ``<owner>--<name>.lock``. An owner never has two hyphens in a row, so two repositories never
    share a name. ``ConfigError`` when ``repo`` is not ``owner/name``."""
    match = _REPO.fullmatch(repo) if isinstance(repo, str) else None
    if not match or match.group(2) in ('.', '..'):
        raise ConfigError(f'repo: expected owner/name for the repository lock, not {repo!r}')
    return f'{match.group(1).lower()}--{match.group(2).lower()}.lock'


def _repo_aliases(data):
    """``repo_aliases`` (#540, ADR 0007): the names ``owner/name`` the repository had before a
    rename, each once and none equal to ``repo``; () when the key is not set."""
    aliases = _str_list(data, 'repo_aliases', 'config')
    repo = data.get('repo')
    for alias in aliases:
        match = _REPO.fullmatch(alias)
        if not match or match.group(2) in ('.', '..'):
            raise ConfigError(f'repo_aliases: expected owner/name, not {alias!r}')
        if isinstance(repo, str) and alias.lower() == repo.lower():
            raise ConfigError(f'repo_aliases: {alias!r} is repo itself: list only the names '
                              'the repository had before a rename')
    if len({a.lower() for a in aliases}) != len(aliases):
        raise ConfigError('repo_aliases: a name is listed twice')
    return aliases


def _repo_lock_dir(data):
    """``repo_lock_dir`` (ADR 0006, R8): an absolute path, or one starting with ``~``, with a
    ``repo`` of the form ``owner/name``; '' when the key is not set."""
    if 'repo_lock_dir' not in data:
        return ''
    value = _require(data, 'repo_lock_dir', str, 'config')
    try:
        absolute = Path(value).expanduser().is_absolute()
    except RuntimeError:  # ~user of a user that does not exist
        absolute = False
    if not absolute:
        raise ConfigError('repo_lock_dir: expected the absolute path of the directory of the '
                          'repository locks, common to the devloop users of the host; leave the '
                          'key out to keep only the lock of state_dir')
    repo_lock_name(data.get('repo'))
    return value


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
                             'write_commands', 'review_commands', 'app', 'provider'))
_PROMPTS_KEYS = {**dict.fromkeys(('instructions', 'fix_instructions', 'module_test_cmd',
                                  'module_test_helpers')),
                 'adr': dict.fromkeys(PROMPT_ADR_KEYS)}
OVERLAY_KEYS = {
    **dict.fromkeys(('repo', 'repo_aliases', 'base_branch', 'branch_prefix', 'state_dir',
                     'source', 'repo_lock_dir',
                     'test_cmd', 'local_tests', 'ci_rerun_unrelated', 'block_uncertain_turns', 'story_identity',
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
                                       'sensitive_path_lists', 'advisory_reviewer',
                                       'findings_reviewer', 'provider_quorum', 'fallback_provider_ban',
                                       'second_reviewer')),
                      'zero': dict.fromkeys(('label', 'allowlist', 'checks'))},
    'merge': {'mode': None, 'operator_only_paths': None, 'levels': {'*': None}},
    'roles': {**dict.fromkeys(('implementer', 'reviewer', 'reviewers', 'supervisor',
                               'coordinator')),
              'implementers': {'*': None}},
    'agents': {'*': _AGENT_KEYS},
    'apps': {'key_dir': None, '*': dict.fromkeys(('app_id', 'slug'))},
    'pin': {'auto': None},
    'run': {'alert_issue': None},
    'report': dict.fromkeys(('issue', 'interval')),
    'ci': dict.fromkeys(('grace_minutes', 'required_timeout_minutes')),
    'gc': dict.fromkeys(('pins', 'keep_pins', 'worktrees', 'logs', 'log_days',
                         'interval_minutes')),
    'agent_user': dict.fromkeys(('name', 'check', 'cli_denied')),
    'docs': {'map': {'*': None}},
    'tests': {'selection': None, 'source_prefixes': None, 'dir_modules': {'*': None},
              'ci_log_format': None},
    'prompts': _PROMPTS_KEYS,
    'delegations': dict.fromkeys(('retry_own_prs', 'rounds', 'budget', 'max_rounds_per_pr',
                                  'max_grants_per_day', 'capacity', 'capacity_max_open_prs',
                                  'capacity_max_parked_prs', 'capacity_max_hours', 'merge',
                                  'max_merges_per_day', 'merge_trust_paths')),
    # Every key of [budget] (#365) is checked by ``parse_budget``, a token cap with its message.
    'budget': {'*': None},
}


# The tables devloop no longer has, with why and what to do: a configuration that names one is
# refused with that message, not with the generic one of an unknown key.
REMOVED_TABLES = {
    'claude_token': (
        '[claude_token] was removed (#517): the long-lived token of the claude turns (#507) is '
        'no longer used, since the agent user\'s CLIs save their own logins in directories with '
        'the sticky bit (ops/host/setup-agent-user.sh). Remove the table from the '
        'configuration, the token file claude.<date>.oauth-token from key_dir, and revoke the '
        'token on claude.ai (ops/host/migrate.md, "Directory delle CLI con lo sticky bit '
        '(#517)")'),
}


def refuse_removed(table, where):
    """Raise ConfigError when ``table``, a configuration read from ``where``, names a table of
    ``REMOVED_TABLES``."""
    if isinstance(table, dict):
        for key in table:
            if key in REMOVED_TABLES:
                raise ConfigError(f'{where}: {REMOVED_TABLES[key]}')


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


def _merge(base, top):
    """``base`` with the keys of ``top`` set over it: tables merge key by key, any other value
    (a list too) replaces the one below. Neither is changed."""
    out = dict(base)
    for key, value in top.items():
        below = out.get(key)
        out[key] = (_merge(below, value) if isinstance(value, dict) and isinstance(below, dict)
                    else value)
    return out


def apply_overlay(data, overlay):
    """``data`` with the keys of ``overlay`` (the local config, #174) set over it: tables merge
    key by key, any other value (a list too) replaces the one below. A key that config.toml
    cannot have is refused."""
    refuse_removed(overlay, 'local config')
    _check_overlay(overlay, OVERLAY_KEYS, '')
    return _merge(data, overlay)


# The project file (ADR 0006, R2): on the host, in ``state_dir`` beside the overlay, written by
# the operator; never in the managed repository, a PR branch or the pinned copy. Its schema is
# closed: the facts of the project, which replace the installation's and which the overlay
# replaces in turn, and the restrictions, which only add to those of the installation and the
# overlay. Every other key (trust, identities, limits, activations) is the overlay's alone.
PROJECT_CONFIG = 'project.toml'
PROJECT_FACTS = {
    'test_cmd': None, 'local_tests': None, 'language': None, 'priority_labels': None,
    'docs': {'map': {'*': None}}, 'prompts': _PROMPTS_KEYS,
    # The selection of the test modules and the format of the CI logs (ADR 0006, R5). Never a
    # permission of the agents: those are the installation's and the overlay's (D2).
    'tests': {'selection': None, 'source_prefixes': None, 'dir_modules': {'*': None},
              'ci_log_format': None},
}
# Each restriction with its value when neither the installation nor the overlay sets it.
PROJECT_RESTRICTIONS = {
    ('protected_paths',): DEFAULT_PROTECTED,
    ('blocked_labels',): DEFAULT_BLOCKED,
    ('review_policy', 'sensitive_paths'): ReviewPolicy.sensitive_paths,
    ('review_policy', 'sensitive_path_lists'): DEFAULT_SENSITIVE_LISTS,
    ('review_policy', 'zero', 'checks'): (),
    ('merge', 'operator_only_paths'): DEFAULT_OPERATOR_ONLY,
}


def _project_schema():
    schema = dict(PROJECT_FACTS)
    for path in PROJECT_RESTRICTIONS:
        node = schema
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = None
    return schema


PROJECT_KEYS = _project_schema()
_MISSING = object()
# The tables of the project file that change what devloop does (#462): without them a copy
# that knows them still starts, but its prompts or its test selection are degraded.
PROJECT_BEHAVIOUR_TABLES = ('prompts', 'tests')


def missing_project_tables(data):
    """The tables of ``PROJECT_BEHAVIOUR_TABLES`` that this copy reads from the project file
    (``PROJECT_FACTS``) and that the raw configuration ``data`` lacks: neither the project file
    nor the overlay nor the installation sets them (#462)."""
    return [name for name in PROJECT_BEHAVIOUR_TABLES
            if name in PROJECT_FACTS and name not in data]


def _check_project(table, schema, overlay, where=''):
    """Refuse a key the project file cannot have, naming it: a key of the overlay (``overlay``
    is the overlay's schema at this level, or None) is told apart from an unknown one."""
    for key, value in table.items():
        at = f'{where}.{key}' if where else key
        if key in schema or '*' in schema:
            expected = schema[key] if key in schema else schema['*']
        elif isinstance(overlay, dict) and (key in overlay or '*' in overlay):
            raise ConfigError(f'project file: {at!r} is not a key of the project file: set it '
                              'in the local overlay (config.local.toml)')
        else:
            raise ConfigError(f'project file: unknown key {at!r}')
        if isinstance(expected, dict):
            if not isinstance(value, dict):
                raise ConfigError(f'project file: {at} must be a table')
            below = overlay.get(key, overlay.get('*')) if isinstance(overlay, dict) else None
            _check_project(value, expected, below, at)


def _get(table, path):
    for key in path:
        if not isinstance(table, dict) or key not in table:
            return _MISSING
        table = table[key]
    return table


def _nested(path, value):
    for key in reversed(path):
        value = {key: value}
    return value


def _facts(project):
    """``project`` without its restrictions, and without the tables they leave empty."""
    def strip(table, prefix):
        out = {}
        for key, value in table.items():
            path = prefix + (key,)
            if path in PROJECT_RESTRICTIONS:
                continue
            if isinstance(value, dict) and any(r[:len(path)] == path
                                               for r in PROJECT_RESTRICTIONS):
                value = strip(value, path)
                if not value:
                    continue
            out[key] = value
        return out
    return strip(project, ())


def _restrict(data, path, extra, default):
    """``data`` with the restriction ``extra`` of the project file added to the one at
    ``path``: the entries already there stay, in their order, and those of ``extra`` that are
    not there follow. What is below and not a list, or not in a table, is left to
    ``parse_config``, which refuses it."""
    where = '.'.join(path)
    if not isinstance(extra, list):
        raise ConfigError(f'project file: {where}: expected a list')
    for n in range(1, len(path)):
        above = _get(data, path[:n])
        if above is not _MISSING and not isinstance(above, dict):
            return data
    floor = _get(data, path)
    if floor is _MISSING:
        floor = list(default)
    if not isinstance(floor, list):
        return data
    if path == ('review_policy', 'zero', 'checks') and extra and not floor:
        raise ConfigError('project file: review_policy.zero.checks: level 0 has no checks in '
                          'the installation and the local overlay; the project file only adds '
                          'checks to them, it never turns level 0 on')
    union = list(floor)
    for item in extra:
        if item not in union:
            union.append(item)
    return _merge(data, _nested(path, union))


def compose(data, project=None, overlay=None):
    """The raw configuration (ADR 0006, R2): the installation's ``data``, the facts of the
    ``project`` file over it, the ``overlay`` over both, and the restrictions of the project
    file added on top. ``project`` and ``overlay`` are parsed tables, or None without the file.
    A key the project file cannot have is refused, naming it."""
    if project is not None:
        refuse_removed(project, 'project file')
        _check_project(project, PROJECT_KEYS, OVERLAY_KEYS)
        data = _merge(data, _facts(project))
    if overlay is not None:
        data = apply_overlay(data, overlay)
    if project is not None:
        for path, default in PROJECT_RESTRICTIONS.items():
            extra = _get(project, path)
            if extra is not _MISSING:
                data = _restrict(data, path, extra, default)
    return data


def read_project_bytes(path):
    """The content of the project file ``path`` (ADR 0006, R2), read without following a link:
    a symbolic link, a file with other hard links or anything but a regular file is refused
    (``ConfigError``), so that the project file is never a file of the managed repository, of a
    worktree or of a pinned copy under another name."""
    where = Path(path)
    try:
        fd = os.open(where, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        if where.is_symlink():
            raise ConfigError(f'project file {where}: a symbolic link: the project file is a '
                              'regular file of the host, in the state_dir') from exc
        raise ConfigError(f'project file {where}: {exc}') from exc
    with os.fdopen(fd, 'rb') as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f'project file {where}: not a regular file')
        if info.st_nlink != 1:
            raise ConfigError(f'project file {where}: it has {info.st_nlink} hard links: the '
                              'project file is a file of the host of its own, in the state_dir')
        try:
            return fh.read()
        except OSError as exc:
            raise ConfigError(f'project file {where}: {exc}') from exc


def read_project_toml(path):
    """The table of the project file ``path``, read as ``read_project_bytes``."""
    raw = read_project_bytes(path)
    try:
        return tomllib.loads(raw.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f'project file {path}: {exc}') from exc


def read_toml(path, what='config'):
    try:
        with open(path, 'rb') as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f'{what} {path}: {exc}') from exc


# What each file of the configuration is called in an error.
SOURCES = {'config': 'config', 'local': 'local config', 'project': 'project file'}


def read_sources(path=None, local=None, project=None):
    """The files of the configuration as ``{name: (path, bytes)}``: ``config`` (``path``,
    default: config.toml of this installation), ``local`` (the overlay, #174) and ``project``
    (the project file, ADR 0006, R2) when given. Each is read once: what is hashed is what is
    parsed. The project file is read as ``read_project_bytes``."""
    paths = {'config': Path(path) if path else DEFAULT_CONFIG}
    if local:
        paths['local'] = Path(local)
    if project:
        paths['project'] = Path(project)
    sources = {}
    for name, where in paths.items():
        if name == 'project':
            sources[name] = (where, read_project_bytes(where))
            continue
        try:
            sources[name] = (where, where.read_bytes())
        except OSError as exc:
            raise ConfigError(f'{SOURCES[name]} {where}: {exc}') from exc
    return sources


def _tables(sources):
    tables = {}
    for name, (where, raw) in sources.items():
        try:
            tables[name] = tomllib.loads(raw.decode())
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f'{SOURCES[name]} {where}: {exc}') from exc
    return tables


def agent_test_cmd(tables):
    """The test command the default commands of agy derive from (``_agy_commands``): that of
    the overlay, else the installation's, never the project file's (ADR 0006, D2: the agents'
    commands derived from the project's tests are admitted by the overlay alone); '' when it
    is not a string."""
    local = tables.get('local') or {}
    value = local['test_cmd'] if 'test_cmd' in local else tables['config'].get('test_cmd')
    return value if isinstance(value, str) else ''


def compose_sources(sources):
    """The raw configuration of ``sources`` (``read_sources``), as ``compose``."""
    tables = _tables(sources)
    return compose(tables['config'], tables.get('project'), tables.get('local'))


def parse_composed(sources, root=INSTALL_ROOT):
    """``(configuration, raw configuration)`` of ``sources``, as ``parse_sources``."""
    tables = _tables(sources)
    data = compose(tables['config'], tables.get('project'), tables.get('local'))
    agent_cmd = agent_test_cmd(tables) if 'project' in tables else None
    return parse_config(data, root, project_hash(sources), agent_cmd), data


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def project_hash(sources):
    """The SHA-256 of the project file of ``sources``, '' without one."""
    return _sha(sources['project'][1]) if 'project' in sources else ''


def parse_sources(sources, root=INSTALL_ROOT):
    """The configuration of ``sources`` (``read_sources``), the project file's hash in the
    review policy. The default commands of agy derive from the test command of the overlay or
    of the installation, never from that of the project file (``agent_test_cmd``)."""
    return parse_composed(sources, root)[0]


def load_data(path=None, local=None, project=None):
    """The raw configuration: ``path`` (default: config.toml of this installation) with the
    project file ``project`` (ADR 0006, R2) and the local overlay ``local`` (#174) composed,
    when given (``compose``)."""
    return compose_sources(read_sources(path, local, project))


def load_config(path=None, local=None, project=None, *, sources=None):
    """The configuration of ``path`` (default: config.toml of this installation) with the
    project file ``project`` (ADR 0006, R2) and the local overlay ``local`` (#174), when
    given; of ``sources`` (``read_sources``), files already read, when given."""
    return parse_sources(read_sources(path, local, project) if sources is None else sources)


# The trust keys (ADR 0006, K1 and T1): a change of a value under them is recorded, with the
# value before and after, in the journal of the dispatcher at its start and at each reload.
TRUST_KEYS = ('repo', 'repo_aliases', 'base_branch', 'state_dir', 'source', 'test_cmd', 'trusted_users',
              'preflight', 'conclave', 'roles', 'agents', 'apps', 'merge', 'review_policy',
              'max_review_rounds', 'protected_paths', 'blocked_labels', 'budget', 'delegations',
              'agent_user', 'pin')


def _plain(value):
    """``value`` as JSON has it (a TOML date as its text)."""
    return json.loads(json.dumps(value, default=str))


def trust_values(data):
    """The values under ``TRUST_KEYS`` of the raw configuration ``data``, by dotted key: a
    table is walked down to its values, a list is one value."""
    out = {}

    def walk(at, value):
        if isinstance(value, dict) and value:
            for key, sub in value.items():
                walk(f'{at}.{key}', sub)
        else:
            out[at] = _plain(value)
    for key in TRUST_KEYS:
        if key in data:
            walk(key, data[key])
    return out


def file_hashes(sources):
    """``{'overlay', 'project'}``: the SHA-256 of each of those files of ``sources`` (as
    ``read_sources`` gives them; None as the bytes of a file that cannot be read), None
    without the file."""
    def sha(name):
        raw = sources[name][1] if name in sources else None
        return _sha(raw) if raw is not None else None
    return {'overlay': sha('local'), 'project': sha('project')}


def fingerprint(sources, data=None):
    """What the journal records of a configuration (ADR 0006, T1): the hashes of the overlay
    and of the project file (``file_hashes``), that of the effective raw configuration
    (``effective``, of its canonical JSON) and its trust values (``trust_values``). ``data`` is
    the composition of ``sources``, composed here when not given."""
    data = compose_sources(sources) if data is None else data
    text = json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      default=str)
    return {**file_hashes(sources), 'effective': _sha(text.encode('utf-8')),
            'trust': trust_values(data)}


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
    ``cfg`` and the creation of the reloader, and a fingerprint taken here would hide that.

    The project file ``project`` (ADR 0006, R2), when given, is read again with them. Each
    time the files are loaded, ``record`` (when given) gets the ``fingerprint`` of what was
    read, or, for a configuration that does not load, the hashes of its files with
    ``effective`` and ``trust`` None and the ``error``: the trace of T1 (ADR 0006, K1). An
    exception of ``record`` is logged, and the configuration of the tick is the same. The
    project file is read as ``read_project_bytes``, and ``check`` (when given) is called before
    each read of it: a ``ConfigError`` of either is a configuration that does not load.
    ``notice`` (when given) gets the raw configuration of each load that succeeds: the warning
    of the tables of the project file it lacks (#462)."""

    def __init__(self, cfg, path=None, local=None, project=None, *, log, record=None,
                 check=None, notice=None):
        self.cfg = cfg
        self.log = log
        self.record = record
        self.check = check
        self.notice = notice
        self._errors = {}
        self.paths = {'config': Path(path) if path else DEFAULT_CONFIG}
        if local:
            self.paths['local'] = Path(local)
        if project:
            self.paths['project'] = Path(project)
        self._seen = None  # no content compared with ``cfg`` yet

    def _read(self, name, path):
        """The content of ``path``, or None when it cannot be read (for the project file, the
        reason in ``self._errors``)."""
        try:
            if name == 'project':
                if self.check is not None:
                    self.check()
                return read_project_bytes(path)
            return path.read_bytes()
        except ConfigError as exc:
            self._errors[name] = str(exc)
            return None
        except OSError:
            return None

    def _load(self, sources):
        """The configuration of the contents just read (the ones hashed), as ``load_config``,
        and their ``fingerprint``."""
        for name, (path, raw) in sources.items():
            if raw is None:
                raise ConfigError(self._errors.get(name)
                                  or f'{SOURCES[name]} {path}: cannot be read')
        new, data = parse_composed(sources)
        self._data = data
        return new, fingerprint(sources, data)

    def _record(self, print_):
        if self.record is None:
            return
        try:
            self.record(print_)
        except Exception as exc:  # the trace failed: said, and the tick goes on
            self.log(f'config: the record of the configuration was not written: '
                     f'{type(exc).__name__}: {exc}')

    def refresh(self):
        """The configuration of the next tick."""
        self._errors = {}
        sources = {name: (path, self._read(name, path)) for name, path in self.paths.items()}
        now = {name: _sha(raw) if raw is not None else None
               for name, (_, raw) in sources.items()}
        changed = now != self._seen
        self._seen = now
        if not changed:
            return self.cfg
        try:
            new, print_ = self._load(sources)
        except Exception as exc:  # the loop goes on with the configuration it has
            self.log(f'config: changed but not valid, the previous configuration stays in use: '
                     f'{type(exc).__name__}: {exc}')
            self._record({**file_hashes(sources), 'effective': None, 'trust': None,
                          'error': f'{type(exc).__name__}: {exc}'})
            return self.cfg
        self._record(print_)
        if self.notice is not None:
            try:
                self.notice(self._data)
            except Exception as exc:  # the warning failed: said, and the tick goes on
                self.log(f'config: the check of the project tables failed: '
                         f'{type(exc).__name__}: {exc}')
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
