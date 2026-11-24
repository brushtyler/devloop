"""devloop command line: python3 -m devloop
{setup,status,apps,tick,run,cleanup,gc,follow,watch,pin,report,fallback,token}."""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

from . import activity, follow, pin, redact, report, runloop, token, unavailability, watch
from .agents import AgentRunner, isolated_env
from .agentuser import GH_CONFIG, SHARED_DIR, WORKTREES_DIR, AgentUser, AgentUserError, SharedDir
from .agentuser import check as check_isolation
from .apps import DISPATCHER, MERGER, Identities
from .config import (INSTALL_ROOT, PROJECT_CONFIG, ConfigError, Reloader, compose_sources,
                     fingerprint, load_config, missing_project_tables, read_sources,
                     repo_lock_name)
from .context import both_labels_warning, label_transition
from .dispatch_advisory import AdvisoryFailures, AdvisoryResults
from .dispatch_base import MainAlerts
from .dispatcher import (Dispatcher, TickLog, Workers, ensure_labels, pr_counts_line,
                         pr_status_line)
from .github import GH_TMP_DIR, GitHub, GitHubError
from .housekeeping import Housekeeping
from .journal import MIB, TurnJournal
from .ledger import Ledger
from .markdown import dispatcher_header, publishable
from .outbox import for_identities
from .planning import plan, queue_lines
from .stall import StallWatch
from .workspace import Workspace

# The descriptor of the dispatcher lock, passed on by a restart from a new pinned copy (#174).
LOCK_FD = 'DEVLOOP_LOCK_FD'
# The descriptor of the repository lock (ADR 0006, R8), passed on like ``LOCK_FD``.
REPO_LOCK_FD = 'DEVLOOP_REPO_LOCK_FD'
# The state_dir given to the launcher (``scripts/devloop-pinned --state-dir``, ADR 0006, R1).
STATE_ENV = 'DEVLOOP_STATE_DIR'
# The tables of the project file whose absence was last reported on alert_issue (#462).
TABLES_ALERTED = 'project-tables-alerted.json'
# The record that the agents running as the dispatcher's user were reported on alert_issue (#490).
AGENT_USER_ALERTED = 'agent-user-alerted'


def main_checkout():
    """The main checkout of the repository containing the current directory: the managed
    repository (ADR 0006, R1)."""
    common = subprocess.run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                            capture_output=True, text=True, check=True).stdout.strip()
    return Path(common).parent


def state_path(cfg, root):
    """The ``state_dir`` of ``cfg``: absolute, or relative to the managed repository's main
    checkout ``root``. When the launcher was given a state_dir (``STATE_ENV``, ADR 0006, R1),
    this must be the same directory, else ``ConfigError``: the launcher reads the pinned copy and
    the overlay there, and devloop would write its pins and its state elsewhere."""
    state = root / cfg.state_dir
    given = os.environ.get(STATE_ENV)
    if given and Path(given).resolve() != state.resolve():
        raise ConfigError(f'state_dir: {state} is not the directory given to the launcher, '
                          f'{given}: set state_dir = "{given}" in its {pin.LOCAL_CONFIG}')
    return state


def project_of(args):
    """The project file given with ``--project-config`` (ADR 0006, R2), or None."""
    return getattr(args, 'project_config', None) or None


def tracked(root, path):
    """Whether git tracks ``path`` in the checkout ``root``."""
    try:
        return subprocess.run(['git', '-C', str(root), 'ls-files', '--error-unmatch', '--',
                               str(path)], capture_output=True).returncode == 0
    except OSError:
        return False


def check_project(project, state, root):
    """``ConfigError`` unless the project file ``project`` (ADR 0006, R2), if any, is
    ``<state_dir>/project.toml`` on the host, not a symbolic link, not a file the managed
    repository (its main checkout ``root``) tracks, and, at the path it resolves to (a
    state_dir reached through a link), not a file the git checkout holding it tracks: the
    project file never comes from the repository, a PR branch or the worktree of a turn. Its
    reads refuse a link too (``config.read_project_bytes``)."""
    if project is None:
        return
    expected = Path(state) / PROJECT_CONFIG
    if Path(project).resolve() != expected.resolve():
        raise ConfigError(f'project file: {project} is not {expected}: the project file is '
                          f'{PROJECT_CONFIG} in the state_dir, on the host')
    if Path(project).is_symlink() or expected.is_symlink():
        raise ConfigError(f'project file: {expected} is a symbolic link: the project file is a '
                          'regular file of the host, in the state_dir')
    if tracked(root, expected):
        raise ConfigError(f'project file: {expected} is a file of the managed repository: the '
                          'project file belongs to the host, never to the repository')
    real = expected.resolve()
    if tracked(real.parent, real):
        raise ConfigError(f'project file: {expected} is {real}, a file of a git checkout (the '
                          'repository, a worktree or another clone): the project file belongs '
                          'to the host, never to the repository')


def load_sources(args, root=None):
    """``(configuration, sources)`` of ``args``: the installation's, the project file (ADR
    0006, R2) and the overlay, with the files as they were read (``config.read_sources``). A
    project file given is checked (``check_project``) on the main checkout ``root`` (default:
    ``main_checkout()``)."""
    sources = read_sources(args.config, args.local_config, project_of(args))
    cfg = load_config(sources=sources)
    if project_of(args) is not None:
        root = root or main_checkout()
        check_project(project_of(args), state_path(cfg, root), root)
    return cfg, sources


def load(args, root=None):
    """The configuration of ``args``, as ``load_sources``."""
    return load_sources(args, root)[0]


def config_recorder(journal, when, log):
    """The trace T1 (ADR 0006, K1): ``record(fingerprint, only_if_changed=False)`` writes the
    configuration in the journal (``TurnJournal.configuration``) and says in the log what it
    recorded: the hashes and each trust key changed, before and after."""
    def short(sha):
        return sha[:12] if sha else 'none'

    def record(print_, only_if_changed=False):
        first = not journal.configurations()
        changes = journal.configuration(when, print_, only_if_changed=only_if_changed)
        if changes is None:
            return
        log(f'config ({when}): overlay {short(print_.get("overlay"))}, project file '
            f'{short(print_.get("project"))}, effective {short(print_.get("effective"))}'
            + (f': not valid, {print_["error"]}' if print_.get('error') else ''))
        if first:
            log(f'config: first record of the trust keys ({len(changes)})')
            return
        for change in changes:
            log(f'config: trust key {change["key"]} {change["old"]!r} -> {change["new"]!r}')
    return record


def record_start(dispatcher, log):
    """Record the configuration the dispatcher starts with in its journal (ADR 0006, T1): the
    ``fingerprint`` of the very files ``build`` read (``dispatcher.config_print``), never a new
    read; nothing for a dispatcher without a journal of its own. Returns the recorder of the
    reloads, or None."""
    journal = getattr(getattr(dispatcher, 'ledger', None), 'journal', None)
    if not isinstance(journal, TurnJournal):
        return None
    print_ = getattr(dispatcher, 'config_print', None)
    if print_ is None:
        raise ConfigError('the dispatcher has no record of the configuration it was built with')
    record = config_recorder(journal, 'start', log)
    record(print_)
    reload = config_recorder(journal, 'reload', log)
    # The first reload reads the files in any case: recorded only when they changed since.
    return lambda print_: reload(print_, only_if_changed=True)


def project_tables_warning(alert_issue, state, gh, out=None, copy=None):
    """The warning of the tables of the project file that this copy knows and the configuration
    lacks (#462, ``config.missing_project_tables``): ``warn(missing)`` writes it in the log
    each time, at the start and at each reload, and on ``alert_issue`` once for each set of
    missing tables (``TABLES_ALERTED`` in ``state``; forgotten when none is missing, so that a
    later loss is reported again). One attempt; a failure is logged and tried at the next
    call. ``copy`` (default: the installation this runs from) names the copy in the alert;
    ``out`` (default: ``log``) writes the lines."""
    path = Path(state) / TABLES_ALERTED

    def warn(missing):
        _warn(missing, out or log)  # ``log`` is read at the call: defined below

    def _warn(missing, log):
        if not missing:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                log(f'config: {TABLES_ALERTED} not removed: {exc}')
            return
        tables = ', '.join(f'[{name}]' for name in missing)
        log(f'config: this copy reads {tables} from the project file, and the configuration '
            'has none: devloop runs without them, degraded; add them to the project file')
        try:
            alerted = json.loads(path.read_text(encoding='utf-8')).get('missing')
        except (OSError, ValueError, AttributeError):
            alerted = None
        if alerted == list(missing):
            return
        if not alert_issue:
            log('config: no [run].alert_issue is configured: no alert for the missing tables')
            return
        sha = pin.copy_commit(INSTALL_ROOT if copy is None else copy)
        listed = ', '.join(f'`[{name}]`' for name in missing)
        body = publishable(
            dispatcher_header() + '\n## Tabelle del file di progetto assenti\n\n'
            + (f'La copia fissata al commit `{sha[:12]}`' if sha else 'La copia di devloop in '
               'esecuzione') + f' legge dal file di progetto {listed}, ma né il file di '
            'progetto né l\'overlay le definiscono. devloop parte lo stesso, ma con i prompt o '
            'la selezione dei test degradati.\n\nVanno aggiunte al file di progetto '
            '(`project.toml` nella directory di stato), a loop fermo. Questo avviso non si '
            'ripete finché le tabelle assenti restano le stesse.')
        try:
            gh.comment(alert_issue, body)
        except Exception as exc:  # one attempt only: its class, never its output
            log(f'config: the alert of the missing tables on #{alert_issue} could not be '
                f'published ({type(exc).__name__})')
            return
        log(f'config: alert of the missing tables published on #{alert_issue}')
        try:
            tmp = path.with_name(f'.{TABLES_ALERTED}-{os.getpid()}')
            tmp.write_text(json.dumps({'missing': list(missing)}) + '\n', encoding='utf-8')
            os.replace(tmp, path)
        except OSError as exc:
            log(f'config: the alert of the missing tables not recorded, it may repeat: {exc}')
    return warn


def agent_user_warning(off, alert_issue, state, gh, out=None):
    """The warning of the agents running as the dispatcher's user (#490): with
    ``[agent_user] name`` empty (``off``), at the start of ``tick`` and ``run``, a line in the
    log each time and a comment on ``alert_issue`` once (``AGENT_USER_ALERTED`` in ``state``;
    forgotten when an agent user is set, so that a later return to none is reported again).
    Only a warning: devloop goes on. One attempt; a failure is logged and tried at the next
    start. ``out`` (default: ``log``) writes the lines."""
    out = out or log
    path = Path(state) / AGENT_USER_ALERTED
    if not off:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            out(f'agent_user: {AGENT_USER_ALERTED} not removed: {exc}')
        return
    out('agent_user: [agent_user] name is empty: the agents and the tests run as the '
        "dispatcher's user, and can read the keys of the apps, state_dir and the overlay")
    if path.exists():
        return
    if not alert_issue:
        out('agent_user: no [run].alert_issue is configured: no alert for the agent user')
        return
    body = publishable(
        dispatcher_header() + '\n## Agent senza utente dedicato\n\n'
        '`[agent_user] name` è vuoto: gli agent e il comando dei test girano come l\'utente del '
        'dispatcher, e possono leggere le chiavi delle GitHub App, lo `state_dir` e '
        'l\'overlay. devloop continua.\n\nPer attivare l\'utente degli agent (#415): '
        '`ops/host/migrate.md`, sezione "Attivazione dell\'utente degli agent (#415)". Questo '
        'avviso non si ripete finché la chiave resta vuota.')
    try:
        gh.comment(alert_issue, body)
    except Exception as exc:  # one attempt only: its class, never its output
        out(f'agent_user: the alert on #{alert_issue} could not be published '
            f'({type(exc).__name__})')
        return
    out(f'agent_user: alert published on #{alert_issue}')
    try:
        path.write_text('alerted\n', encoding='utf-8')
    except OSError as exc:
        out(f'agent_user: the alert not recorded, it may repeat: {exc}')


def installation_source(cfg, root):
    """The main checkout of the installation's source (``pin.source_root``, ADR 0006, R1):
    ``root`` without ``source``. ``ConfigError`` when ``source`` is not a git checkout."""
    try:
        return pin.source_root(root, cfg.source)
    except pin.PinError as exc:
        raise ConfigError(str(exc)) from exc


def auto_pinner(cfg, root, state, local, log, **kw):
    """The pin after a merge (``pin.AutoPin``) on ``root``, or None when the installation's
    source is not the managed repository (ADR 0006, R1): a merge in another project never
    touches the installation, and ``[pin] auto`` is then off, with a line in the log."""
    source = installation_source(cfg, root)
    if source.resolve() == Path(root).resolve():
        return pin.AutoPin(root, state, local, log=log, **kw)
    if cfg.pin.auto:
        log(f'auto-pin off: the installation comes from {source}, not from the managed '
            'repository; pin a tag of the source by hand')
    return None


def _inherited_lock(lock_path, var=LOCK_FD, mode='w'):
    """The lock a process that restarted itself from a new pinned copy passed on open in ``var``
    (#174), or None. Only a descriptor of this very lock file counts."""
    value = os.environ.pop(var, None)
    if not value or not value.isdigit():
        return None
    fd = int(value)
    try:
        if not os.path.samestat(os.fstat(fd), os.stat(lock_path)):
            return None
    except OSError:
        return None
    os.set_inheritable(fd, False)
    return open(fd, mode)


@contextmanager
def single_instance(lock_path):
    """Hold ``lock_path`` while the block runs; the open file is yielded, so that a restart from
    a new pinned copy can pass it on."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _inherited_lock(lock_path) or open(lock_path, 'w') as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit('devloop: another dispatcher is running (lock held)')
        yield fh


def repo_lock_path(cfg):
    """``<repo_lock_dir>/<owner>--<name>.lock`` of ``cfg.repo`` (ADR 0006, R8), or None when
    ``repo_lock_dir`` is not set."""
    directory = getattr(cfg, 'repo_lock_dir', '')
    if not directory:
        return None
    return Path(directory).expanduser() / repo_lock_name(cfg.repo)


def _open_repo_lock(path):
    """The descriptor of the repository lock ``path``, read-only and never through a symlink
    (ADR 0006, R8). A lock that exists is opened as it is, without ``O_CREAT``: devloop neither
    creates nor replaces it, and leaves its owner and its mode alone. Only a missing one is
    created, with ``O_EXCL``, and made readable by the other devloop users of the host whatever
    the umask: they open it too. ``OSError`` when it cannot be opened."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        return os.open(path, flags)
    except FileNotFoundError:
        pass
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        # Another dispatcher created it in between: it is theirs, opened as it is.
        return os.open(path, flags)
    try:
        os.fchmod(fd, 0o644)
    except OSError:
        os.close(fd)
        raise
    return fd


@contextmanager
def repo_lock(cfg):
    """Hold the repository lock of ``cfg`` (ADR 0006, R8) while the block runs, taken without
    waiting: while another dispatcher on the same ``repo`` holds it, whatever its state_dir and
    its user, devloop stops with a message that names the repository. The directory is the
    host's, prepared by the operator for every devloop user (ops): devloop does not create it.
    The file is opened read-only, which is enough for ``flock``, and never through a symlink;
    one that exists is opened without ``O_CREAT`` (``_open_repo_lock``). Yields the open file,
    or None without ``repo_lock_dir``."""
    path = repo_lock_path(cfg)
    if path is None:
        yield None
        return
    held = _inherited_lock(path, REPO_LOCK_FD, 'r')
    if held is None:
        if not path.parent.is_dir():
            sys.exit(f'devloop: repo_lock_dir {path.parent} is not a directory: the host has to '
                     'prepare it for every devloop user (ops/host/setup-devloop.sh)')
        try:
            fd = _open_repo_lock(path)
        except OSError as exc:
            sys.exit(f'devloop: the repository lock {path} cannot be opened: {exc.strerror}')
        held = open(fd, 'r')
    with held as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(f'devloop: another dispatcher is running on {cfg.repo} (repository lock '
                     f'{path} held)')
        yield fh


def log(message):
    print(f'{time.strftime("%H:%M:%S")} {message}', flush=True)


def agent_environment(cfg, gh_config, agent_user=None):
    """Environment for agent CLIs and the test command: :func:`isolated_env`, plus Bash time
    limits for Claude Code. A command may ask for up to the test timeout, which the full suite
    needs; one that asks for nothing stops after ``agent_command_timeout_seconds``, so a wait loop
    that never ends does not hold the turn for an hour (issue #96). An operator's own values are
    kept. With ``agent_user`` (#415): that user's HOME, USER and LOGNAME. No ``safe.directory``
    here: each turn and test run trusts only its own worktree (#500, ``AgentUser.git_env``)."""
    if agent_user is None:
        env = isolated_env(gh_config)
    else:
        env = agent_user.environment(isolated_env(gh_config))
    # The pinned copy on PYTHONPATH (#174) is devloop's own: the tests of a worktree import the
    # worktree's code.
    env.pop('PYTHONPATH', None)
    # The launcher's state_dir (ADR 0006, R1) is the dispatcher's: not that of the code under test.
    env.pop(STATE_ENV, None)
    env.setdefault('BASH_DEFAULT_TIMEOUT_MS', str(cfg.agent_command_timeout_seconds * 1000))
    env.setdefault('BASH_MAX_TIMEOUT_MS', str(cfg.test_timeout_seconds * 1000))
    return env


def build(args):
    root = main_checkout()
    cfg, sources = load_sources(args, root)
    state = state_path(cfg, root)
    try:
        # Every `gh` of the dispatcher with a temporary directory of its own (#545).
        identities = Identities.from_config(cfg, tmp_dir=state / GH_TMP_DIR)
    except OSError as exc:
        raise ConfigError(f'GitHub App key: {exc}. Set $DEVLOOP_APP_KEY_DIR or remove the app '
                          'from [apps] to use the ambient gh login.') from exc
    # The published texts name the parameters, not the host's paths (#583): the map of ops in
    # state_dir, with the dispatcher's own state_dir and key_dir prevailing.
    identities.redactor = redact.runtime(cfg, state, log=log)
    # Agents and the test command never see the operator's GitHub credentials. With an agent
    # user (#415) they run as that user, and what they need outside their worktree is in the
    # shared directory, the only entry of state_dir besides the worktrees that user reaches.
    agent_user = agent_user_of(cfg)
    shared = state / SHARED_DIR
    if agent_user:
        state.mkdir(parents=True, exist_ok=True)
        gh_config = SharedDir(shared).ensure(GH_CONFIG)  # no link followed
    else:
        gh_config = state / 'agent-gh-config'
        gh_config.mkdir(parents=True, exist_ok=True)
    agent_env = agent_environment(cfg, gh_config, agent_user)
    # With parallel actions (#176) each test run gets a temporary directory of its own.
    ws = Workspace(root, cfg.state_dir, cfg.base_branch, test_env=agent_env,
                   private_tmp=cfg.max_parallel_actions > 1, agent_user=agent_user)
    current = activity.Activity(state)
    # One log for the whole tick: each line whole, with the number of its action (#176).
    tick_log = TickLog(log)
    # Session transcripts are found where the agent CLIs write them, in their HOME.
    stall = StallWatch(cfg.agent_stall_seconds, cfg.agent_stall_action, log=tick_log,
                       activity=current, home=agent_env.get('HOME'))
    runner = AgentRunner(state / 'logs', cfg.agent_timeout_seconds, env=agent_env,
                         on_turn=lambda agent, prompt: current.turn(
                             agent.key, prompt, agent.model if agent.fallback_for else None),
                         stall=stall, agent_user=agent_user,
                         shared_dir=shared if agent_user else None)
    # Merges (#172) run with the merger's app (#201), the only one that gets contents=write,
    # or without it with the local gh login devloop runs with.
    # Without the dispatcher lock, status and a dry run read the journal of the turns and never
    # write it (#362): the running dispatcher keeps its chain in memory.
    command = getattr(args, 'command', None)
    read_only = command == 'status' or (command == 'tick' and getattr(args, 'dry_run', False))
    # The records written before a rename of the repository stay its own (#540).
    ledger = Ledger(None, state_dir=state, read_only=read_only,
                    journal_max_bytes=cfg.journal_max_mib * MIB,
                    repo_aliases=dict.fromkeys(cfg.repo_aliases, cfg.repo))
    # With publication_outbox (#385), every client's comments, native reviews and pull requests
    # are in that journal before they are sent; never from status or a dry run, which do not
    # write it.
    if cfg.publication_outbox and not read_only:
        identities.outbox = for_identities(identities, ledger.journal, cfg.trusted_users,
                                           log=tick_log)
    gh = identities.github(DISPATCHER)
    ledger.gh = gh
    dispatcher = Dispatcher(cfg, gh, ws, runner, log=tick_log, identities=identities,
                            activity=current, ledger=ledger,
                            merger=identities.merger(),
                            pinner=auto_pinner(cfg, root, state, args.local_config, tick_log,
                                               project=project_of(args),
                                               fetch_lock=ws.shared_lock,
                                               keep=keep_pins(cfg)),
                            memory=unavailability.Memory(state),
                            advisory_results=AdvisoryResults(state),
                            advisory_failures=AdvisoryFailures(state),
                            main_alerts=MainAlerts(state),
                            housekeeping=Housekeeping(state, ws, gh, ledger=ledger,
                                                      log=tick_log))
    # What the trace T1 (ADR 0006, K1) records of the start: the files this configuration came
    # from, as they were read.
    data = compose_sources(sources)
    dispatcher.config_print = fingerprint(sources, data)
    # The tables of the project file this copy knows and the configuration lacks (#462).
    dispatcher.config_missing = missing_project_tables(data)
    # The agents run as the dispatcher's user (#490): a warning at the start.
    dispatcher.agent_user_off = agent_user is None
    return cfg, state, dispatcher


def agent_user_of(cfg, factory=AgentUser):
    """The ``AgentUser`` of ``[agent_user] name`` (#415), or None when it is empty. An unknown
    user is a ConfigError."""
    name = cfg.agent_user.name
    if not name:
        return None
    try:
        return factory(name, cli_denied=cfg.agent_user.cli_denied)
    except AgentUserError as exc:
        raise ConfigError(str(exc)) from exc


def check_agent_user(cfg, state, local_config, agent_user, log=log, me=None, git_dir=None,
                     env=None):
    """The startup check of the agent user (#415), before ``tick`` and ``run``
    (``agentuser.check``): an ``AgentUserError`` when it can read or enter ``key_dir`` (or
    ``$DEVLOOP_APP_KEY_DIR``) or the entries of ``state_dir``, read the local overlay, write
    ``git_dir`` (the repository's ``.git``), the worktrees directory or the fixed directories
    of the shared one, cannot write the directories of its turns, or cannot run git, with
    ``env`` (the environment of the turns), in a worktree of the dispatcher's user (#500).
    Nothing to check without an agent user; with ``[agent_user] check = false`` a warning in
    the log. Before it, also with ``check = false``: an ``AgentUserError`` when the environment
    devloop passes with ``sudo -E`` does not reach the agent user (sudo-rs, ``SETENV`` missing;
    #496)."""
    if agent_user is None:
        return
    agent_user.check_environment()
    if not cfg.agent_user.check:
        log(f'agent_user: the startup check of {agent_user.name} is off ([agent_user] check = '
            'false): for development only')
        return
    for sub in (WORKTREES_DIR, SHARED_DIR):
        (state / sub).mkdir(parents=True, exist_ok=True)
    key_dir = os.environ.get('DEVLOOP_APP_KEY_DIR') or cfg.app_key_dir
    check_isolation(agent_user, key_dir, state, local_config, me=me, git_dir=git_dir, env=env)


def check_keys(dispatcher, log=log):
    """The startup step of the agent keys (ADR 0005, P5-3, #407), before ``tick`` and ``run``:
    the memory of the models found unavailable moves from the keys before the migration
    (``claude``, ``codex``, ``agy``) to the keys that name their role
    (``unavailability.Memory.migrate``); then devloop stops (``SystemExit``) when an open PR of
    devloop has a marker that names a key not configured, or one of another role
    (``Dispatcher.key_problems``), and when the open PRs cannot be read: the keys change with
    an empty queue (D10). A dispatcher without the check (a stand-in) is left as it is."""
    check = getattr(dispatcher, 'key_problems', None)
    if not callable(check):
        return
    migrate = getattr(getattr(dispatcher, 'memory', None), 'migrate', None)
    if callable(migrate):
        for old, keys in migrate(dispatcher.cfg.agents):
            log(f'fallback memory: the models remembered unavailable for {old} now hold for '
                + ', '.join(keys))
    try:
        problems = check()
    except GitHubError as exc:
        sys.exit(f'devloop: the markers of the open PRs cannot be checked against the agent '
                 f'keys: {exc}')
    if problems:
        sys.exit('devloop: open PRs name agent keys that this configuration does not have in '
                 'that role; the keys change with an empty queue (ADR 0005, D10):\n'
                 + '\n'.join(f'  {line}' for line in problems))


def hold_lock(dispatcher, lock_fd, repo_lock_fd=None):
    """With the outbox of the publications (#385), the `gh` of each write holds the dispatcher
    lock ``lock_fd`` too, under a timeout of its own (``GitHub.KILL_AFTER``): no next dispatcher
    starts while one of them may still publish, even after this process stopped. Without it
    (#397, ADR 0004, D8) no `gh` gets the lock: a dispatcher killed while a `gh` of its runs
    leaves the lock free at once, as before #385. The repository lock (ADR 0006, R8), when held
    on ``repo_lock_fd``, passes on with it: no dispatcher with another state_dir takes over the
    repository in the middle of a publication either. Returns whether the locks were passed on."""
    identities = getattr(dispatcher, 'identities', None)
    if getattr(identities, 'outbox', None) is None:
        return False
    if repo_lock_fd is None:
        identities.hold(lock_fd)
    else:
        identities.hold(lock_fd, repo_lock_fd)
    return True


def keep_pins(cfg):
    """How many pinned copies besides ``current`` a successful pin keeps (#380), or None when
    ``[gc] pins`` is off."""
    return cfg.gc.keep_pins if cfg.gc.pins else None


def run_fallback(args):
    """``devloop fallback reset <agent>`` (#151): forget the models ``agent`` remembers
    unavailable, so that its next turn starts from the configured model. Local files only."""
    try:
        root = main_checkout()
        cfg = load(args, root)
        state = state_path(cfg, root)
    except ConfigError as exc:
        sys.exit(f'devloop: invalid config: {exc}')
    memory = unavailability.Memory(state)
    known = {key for key, _, _ in memory.entries()}
    if args.agent not in cfg.agents and args.agent not in known:
        sys.exit(f'devloop: fallback reset: unknown agent {args.agent!r}')
    removed = memory.reset(args.agent)
    if removed:
        log(f'{args.agent}: forgot the unavailability of {", ".join(removed)}')
    else:
        log(f'{args.agent}: no model remembered unavailable')


def run_pin(args):
    """``devloop pin [--no-smoke] <ref>`` (#174). The overlay is ``--local-config``, or
    ``<state_dir>/config.local.toml`` when it exists. ``--no-smoke`` skips the smoke test of a
    new copy (#260), and the log says so. The commit comes from the installation's source
    (``source``, ADR 0006, R1), the smoke test runs in the managed repository."""
    try:
        root = main_checkout()
        cfg = load(args, root)
        state = state_path(cfg, root)
        local = args.local_config or state / pin.LOCAL_CONFIG
        # The project file of the state_dir (ADR 0006, R2), as the launcher would pass it.
        project = project_of(args) or state / PROJECT_CONFIG
        check_project(project if Path(project).exists() or Path(project).is_symlink() else None,
                      state, root)
    except ConfigError as exc:
        sys.exit(f'devloop: invalid config: {exc}')
    # The pin by hand is the one after the step on the host (#462): it releases, under the lock
    # of the pins, the holds whose merge the pinned commit contains (#473).
    released = {}

    def release(source, sha):
        try:
            released['steps'] = pin.release(state, source, sha)
        except OSError as exc:
            released['error'] = exc
    try:
        source = installation_source(cfg, root)
        pin.pin(source, state, args.ref, local, log=log, smoke=not args.no_smoke,
                keep=keep_pins(cfg), managed=root, project=project,
                then=lambda sha: release(source, sha))
    except (pin.PinError, ConfigError, OSError) as exc:
        sys.exit(f'devloop: pin failed, the pinned copy is unchanged: {exc}')
    if 'error' in released:
        sys.exit(f'devloop: pinned, but the hold of the auto-pin was not released: '
                 f'{released["error"]}')
    steps, left = released.get('steps'), pin.held(state)
    if steps:
        log(f'auto-pin {"still held" if left else "resumed"}: the hold after the step on the '
            f'host of {pin.held_prs(steps)} is released')
    known = [s for s in left or [] if pin.merge_known(s)]
    unknown = [s for s in left or [] if not pin.merge_known(s)]
    if known:
        log(f'auto-pin held: the pinned commit does not contain the merge of '
            f'{pin.held_prs(known)}; pin by hand a commit that contains it, after its step')
    if unknown:
        log(f'auto-pin held: the merge commit of {pin.held_prs(unknown)} is not known yet '
            '(devloop reads it on GitHub at its next tick, or the merge outcome is uncertain); '
            'pin by hand again once devloop status shows it')


def run_token(args, command):
    """``devloop token <actor> [--perm NAME=LEVEL ...] -- <command>`` (ADR 0006, R6): run
    ``command`` with a token of the actor's app, on the ``repo`` and with the ``[apps]`` of the
    configuration of the project (``token.run``): the installation's, the project file and the
    overlay of its state_dir, as the launcher passes them. The actor and the permissions are
    checked before any file is read. Exits with the command's exit code."""
    try:
        actor = token.actor_of(args.actor)
        permissions = token.permissions_of(args.perm or ())
    except token.TokenError as exc:
        sys.exit(f'devloop: token: {exc}')
    if not command:
        sys.exit('devloop: token: the command goes after --: devloop token <actor> '
                 '[--perm NAME=LEVEL ...] -- <command> [args...]')
    try:
        root = main_checkout()
    except (OSError, subprocess.CalledProcessError):
        sys.exit('devloop: token: run it from the checkout of the managed repository, or one of '
                 'its worktrees')
    try:
        cfg = load(args, root)
        state_path(cfg, root)
    except ConfigError as exc:
        sys.exit(f'devloop: invalid config: {exc}')
    try:
        code = token.run(cfg, actor, permissions, command)
    except (token.TokenError, OSError, GitHubError) as exc:
        sys.exit(f'devloop: token: {exc}')
    sys.exit(code)


def run_watch(parser, args):
    """``devloop watch`` (#173). It reads GitHub with the identity of the ticks' reads (#197):
    the dispatcher's app when ``[apps]`` has one, else the ambient ``gh`` login."""
    if args.interval < 1:
        parser.error('watch: --interval must be at least 1 second')
    if args.timeout is not None and args.timeout < 1:
        parser.error('watch: --timeout must be a positive number of seconds')
    if args.timeout is not None and args.exec_command is not None:
        parser.error('watch: --timeout applies only to --until-change')
    exec_argv = None
    if args.exec_command is not None:
        try:
            exec_argv = watch.parse_exec(args.exec_command)
        except ValueError as exc:
            parser.error(f'watch: {exc}')
    try:
        cfg = load(args)
        state = (Path(args.state) if args.state
                 else state_path(cfg, main_checkout()) / watch.STATE_FILE)
    except ConfigError as exc:
        sys.exit(f'devloop: invalid config: {exc}')
    try:
        gh = Identities.from_config(cfg).github(DISPATCHER)
    except OSError as exc:
        sys.exit(f'devloop: invalid config: GitHub App key: {exc}. Set $DEVLOOP_APP_KEY_DIR or '
                 'remove the app from [apps] to use the ambient gh login.')
    watcher = watch.Watcher(gh, watch.Trust.from_config(cfg), state,
                            interval=args.interval, timeout=args.timeout, exec_argv=exec_argv)
    gh.time_left = watcher.time_left  # no gh call outlasts --timeout
    try:
        return watcher.run()
    except KeyboardInterrupt:
        return 130


def idle_timeout(parser, args):
    """The ``--idle-timeout`` of ``devloop run`` (#177); ``--interval`` is its deprecated alias,
    and the two together are refused."""
    if args.idle_timeout is not None and args.interval is not None:
        parser.error('run: --interval is a deprecated alias of --idle-timeout: use only one')
    value = args.idle_timeout if args.interval is None else args.interval
    if value is None:
        return runloop.IDLE_TIMEOUT
    if value < 1:
        parser.error('run: --idle-timeout must be at least 1 second')
    return value


def run_report(parser, args):
    """``devloop report [--day YYYY-MM-DD] [--publish]`` (#256): print the summary of a day
    (default: today, UTC) from devloop's records, or publish it on ``[report] issue`` as the
    dispatcher. Reads GitHub with the identity of the ticks' reads, as ``devloop watch``."""
    day = None
    if args.day is not None:
        try:
            day = report.parse_day(args.day)
        except ValueError:
            parser.error(f'report: --day must be a day in the form YYYY-MM-DD, not {args.day!r}')
    try:
        cfg = load(args)
    except ConfigError as exc:
        sys.exit(f'devloop: invalid config: {exc}')
    if args.publish and not cfg.report.issue:
        sys.exit('devloop: report --publish: no [report] issue is configured. Set the number of '
                 'the dedicated issue as "issue" in [report] of the local overlay.')
    try:
        state = state_path(cfg, main_checkout()) if args.publish else None
    except ConfigError as exc:
        sys.exit(f'devloop: invalid config: {exc}')
    try:
        identities = Identities.from_config(cfg)
    except OSError as exc:
        sys.exit(f'devloop: invalid config: GitHub App key: {exc}. Set $DEVLOOP_APP_KEY_DIR or '
                 'remove the app from [apps] to use the ambient gh login.')
    if state is not None:
        identities.redactor = redact.runtime(cfg, state, log=log)  # #583
    gh = identities.github(DISPATCHER)
    trust = watch.Trust.from_config(cfg)
    try:
        if args.publish:
            report.build(cfg, gh, trust, state, log=log).publish(day)
            return
        day = day or report.utc_today()
        summary = report.Records(gh, trust, day, day, cfg.review_keys()).summary(day)
        print(report.render_text(summary), end='', flush=True)
    except (GitHubError, report.ReportError) as exc:
        sys.exit(f'devloop: report: {exc}')


def run_loop(cfg, state, dispatcher, timeout, reloader=None):
    """``devloop run`` (#177): ``runloop.Loop`` with the dispatcher's ticks, a watcher with the
    identity of the ticks' reads (as ``devloop watch``) on a state file of its own, and the
    alert on ``[run].alert_issue`` as the dispatcher. With ``max_parallel_actions`` above 1 the
    loop runs in continuous mode (#227): the dispatcher's ticks start their actions in
    ``Workers`` and do not wait for them. Before each tick ``reloader`` (``config.Reloader``,
    #278), when given, gives the dispatcher the limits of the configuration files as they are
    now; the actions already running go on. With ``[report] issue`` (#256) the daily summary is
    published at the end of a tick, at most every ``[report] interval`` minutes; a failure is
    logged and does not fail the tick. Returns the exit code."""
    trust = watch.Trust.from_config(cfg)
    workers = None
    if cfg.max_parallel_actions > 1:
        workers = dispatcher.workers = Workers(cfg.max_parallel_actions)
    gh = dispatcher.identities.github(DISPATCHER) if dispatcher.identities else dispatcher.gh
    publisher = report.build(cfg, gh, trust, state, log=log)
    daily = (report.AutoReport(publisher, state, cfg.report.interval, log=log)
             if publisher is not None else None)

    def wait_for_news(seconds, sleep, check):
        watcher = watch.Watcher(gh, trust, state / runloop.WATCH_STATE_FILE,
                                interval=min(runloop.WATCH_INTERVAL, seconds), timeout=seconds,
                                sleep=sleep, out=lambda line: log(f'news: {line}'), err=log)

        def time_left():
            check()  # after a stop no other gh call (nor token mint) starts: Stopped
            return watcher.time_left()
        # No gh call outlasts the wait; the client may be the ticks' own, so it is restored.
        # The limit belongs to this thread: actions running meanwhile are not bounded by it.
        before, gh.time_left = gh.time_left, time_left
        try:
            return watcher.run()
        finally:
            gh.time_left = before

    issue = cfg.run.alert_issue
    alert = (lambda body: dispatcher.gh.comment(issue, body)) if issue else None

    def tick():
        if reloader is not None:
            dispatcher.cfg = reloader.refresh()
        actions = dispatcher.tick()
        if daily is not None:
            daily.after_tick()
        return actions

    return runloop.Loop(tick, wait_for_news, state / runloop.STOP_FILE, log=log,
                        alert=alert, alert_issue=issue, idle_timeout=timeout,
                        workers=workers).run()


def main(argv=None):
    parser = argparse.ArgumentParser(prog='devloop', description=__doc__)
    parser.add_argument('--config', help='TOML config (default: devloop/config.toml)')
    parser.add_argument('--local-config', metavar='PATH',
                        help='local overlay: only the keys it sets replace those of the config '
                             '(scripts/devloop-pinned passes <state_dir>/config.local.toml)')
    parser.add_argument('--project-config', metavar='PATH',
                        help='project file on the host, <state_dir>/project.toml: the facts and '
                             'the added restrictions of the managed repository (scripts/'
                             'devloop-pinned passes it when it exists)')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('setup', help='create or update the agent:* labels')
    sub.add_parser('status', help='show the running action, devloop PRs, ready issues and the '
                                  'planned actions')
    sub.add_parser('apps', help='check the GitHub App of each actor and its installation')
    tick = sub.add_parser('tick', help='plan and run one round of actions')
    tick.add_argument('--dry-run', action='store_true', help='only print the planned actions')
    run = sub.add_parser('run', help='tick continuously: a tick with actions is followed by '
                                     'another at once, one without waits for news on GitHub')
    run.add_argument('--idle-timeout', type=int, metavar='SECONDS',
                     help='after a tick without actions, run the next one after this many '
                          'seconds without news (default 900)')
    run.add_argument('--interval', type=int, metavar='SECONDS',
                     help='deprecated alias of --idle-timeout')
    sub.add_parser('cleanup', help='remove devloop worktrees left by failed steps')
    sub.add_parser('gc', help='clean up state_dir now as [gc] sets: old pinned copies, '
                              'worktrees of closed issues and PRs, old log files')
    follow_cmd = sub.add_parser('follow', help='follow live the agent working in a devloop worktree')
    follow_cmd.add_argument('target', help='the worktree: issue-<N> (implementer) or pr-<N> '
                                           '(reviewer or fixer)')
    watch_cmd = sub.add_parser('watch', help='poll GitHub and report what changed, to wake the '
                                             'agent that supervises devloop')
    watch_cmd.add_argument('--interval', type=int, default=60,
                           help='seconds between two reads of GitHub (default 60)')
    mode = watch_cmd.add_mutually_exclusive_group()
    mode.add_argument('--until-change', action='store_true',
                      help='(default) exit 0 at the first round with news, printing its lines')
    mode.add_argument('--exec', dest='exec_command', metavar='COMMAND',
                      help='keep running and run COMMAND (no shell) with the lines of each round '
                           'with news on its stdin, one at a time')
    watch_cmd.add_argument('--timeout', type=int,
                           help='with --until-change: exit 3 after this many seconds without '
                                'news (default: wait forever)')
    watch_cmd.add_argument('--state', help='state file (default: <state_dir>/watch-state.json in '
                                           'the main checkout)')
    pin_cmd = sub.add_parser('pin', help='pin a commit of origin: verified read-only copy in '
                                         '<state_dir>/pinned-<sha12>, pointed at by '
                                         '<state_dir>/current')
    pin_cmd.add_argument('ref', help='a branch or tag of origin, or a commit reachable from '
                                     'them or of a copy already pinned (rollback)')
    pin_cmd.add_argument('--no-smoke', action='store_true',
                         help='skip the smoke test of a new copy (tick --dry-run and a round of '
                              'watch from the copy); for exceptional cases, logged')
    report_cmd = sub.add_parser('report', help='print the summary of a day from devloop\'s '
                                               'records, or publish it on [report] issue')
    report_cmd.add_argument('--day', metavar='YYYY-MM-DD',
                            help='the UTC day to summarize (default: today)')
    report_cmd.add_argument('--publish', action='store_true',
                            help='publish as the dispatcher on [report] issue: today in the body, '
                                 'a past day in its archive comment')
    fallback_cmd = sub.add_parser('fallback',help='the memory of the models found unavailable')
    fallback_sub = fallback_cmd.add_subparsers(dest='fallback_command', required=True)
    reset_cmd = fallback_sub.add_parser('reset', help='forget the models an agent remembers '
                                                      'unavailable: its next turn starts from '
                                                      'its configured model')
    reset_cmd.add_argument('agent', help='the agent key, as in [agents.<key>]')
    token_cmd = sub.add_parser('token', help='run a command after -- as an agent\'s GitHub App '
                                             'of the project, with a token limited to --perm '
                                             '(default issues=write, pull_requests=write)')
    token_cmd.add_argument('actor', help=f'one of {", ".join(token.ACTORS)}')
    token_cmd.add_argument('--perm', action='append', metavar='NAME=LEVEL',
                           help='a permission of the token; repeat it for more')
    argv = sys.argv[1:] if argv is None else list(argv)
    # The command of `token` (ADR 0006, R6) is everything after its --, options included.
    command = None
    if '--' in argv and 'token' in argv[:argv.index('--')]:
        split = argv.index('--')
        argv, command = argv[:split], argv[split + 1:]
    args = parser.parse_args(argv)

    if args.command == 'run':
        timeout = idle_timeout(parser, args)
        if args.interval is not None:
            log('run: --interval is deprecated, use --idle-timeout')
    if args.command == 'pin':
        return run_pin(args)
    if args.command == 'token':
        return run_token(args, command)
    if args.command == 'fallback':
        return run_fallback(args)
    if args.command == 'report':
        return run_report(parser, args)
    if args.command == 'watch':
        sys.exit(run_watch(parser, args))
    if args.command == 'follow':
        # Local files only: no GitHub identities needed.
        if not follow.NAME.fullmatch(args.target):
            sys.exit(f'devloop: follow needs issue-<N> or pr-<N>, not {args.target!r}')
        try:
            cfg = load(args)
            root = state_path(cfg, main_checkout()) / 'worktrees'
        except ConfigError as exc:
            sys.exit(f'devloop: invalid config: {exc}')
        try:
            # The transcripts are in the HOME of the user the agents run as (#415).
            agent_user = agent_user_of(cfg)
        except ConfigError as exc:
            sys.exit(f'devloop: invalid config: {exc}')
        extra = {'home': agent_user.home} if agent_user else {}
        try:
            sys.exit(follow.follow(follow.worktrees(root, args.target), **extra))
        except KeyboardInterrupt:
            sys.exit(0)

    try:
        cfg, state, dispatcher = build(args)
    except ConfigError as exc:
        sys.exit(f'devloop: invalid config: {exc}')

    if args.command == 'setup':
        note = ensure_labels(dispatcher.gh, cfg.labels, cfg.review_policy)
        if note:
            log(f'labels: {note}')
        rp = cfg.review_policy
        log('labels ready: ' + ', '.join((*cfg.labels.all(), rp.label,
                                          *((rp.zero_label,) if rp.zero_enabled() else ()))))
        return
    if args.command == 'apps':
        failed = False
        # The project's set of apps (ADR 0006, D3): those of its repo, with its key_dir.
        log(f'apps of {cfg.repo}, keys in '
            f'{os.environ.get("DEVLOOP_APP_KEY_DIR") or cfg.app_key_dir}')
        # The coordinator's reserved merge (#529) needs the merger app in mode off too.
        if MERGER in cfg.apps and not cfg.merge.enabled() and not cfg.coordinator:
            log(f'{MERGER}: [merge] mode is off: the merger app is only for a repository where '
                'devloop merges (ADR 0006, D3)')
        # Each app once, by the actor of its table: the agents' declared apps, the implementer's
        # role app among them (#407).
        for actor in dict.fromkeys([DISPATCHER, MERGER, *map(cfg.app_of, cfg.agents)]):
            app = dispatcher.identities.apps.get(actor)
            if app is None:
                log(f'{actor}: no app, uses the ambient gh login')
                continue
            try:
                info = app.describe()
            except GitHubError as exc:
                failed = True
                log(f'{actor}: {exc}')
                continue
            perms = ', '.join(f'{k}={v}' for k, v in sorted(info['permissions'].items()))
            log(f'{actor}: {info["login"]} installation {info["installation"]} '
                f'({info["repository_selection"]}): {perms}')
            # #570: an installation on all the owner's repositories is a warning, not a
            # failure. The count of a 'selected' one would need a token not limited to the
            # project's repository, which devloop never mints.
            if info['repository_selection'] == 'all':
                log(f'{actor}: warning: installation {info["installation"]} covers all the '
                    f'repositories of {cfg.repo.split("/", 1)[0]} (repository_selection all): '
                    'the app has its permissions on each of them; choose Only select '
                    'repositories in its settings on GitHub')
        if failed:
            sys.exit(1)
        return
    if args.command == 'status':
        for line in (activity.describe(state) or 'no action running').split('\n'):
            log(line)
        restart = activity.describe_restart(state)
        if restart:
            log(restart)
        # #430: the copy this runs from, behind the base branch in the installation's files.
        try:
            behind = dispatcher.pinner and dispatcher.pinner.behind(cfg.base_branch,
                                                                    cfg.pin.touches)
        except (pin.PinError, OSError) as exc:
            behind = f'pinned copy: not compared with origin/{cfg.base_branch}: {exc}'
        if behind:
            log(behind)
        failed = pin.failed_line(state)  # #453: why the last auto-pin did not move current
        if failed:
            log(failed)
        steps = pin.held(state)  # #462: a step on the host holds the auto-pin
        if steps:
            log(f'auto-pin held: the step on the host of {pin.held_prs(steps)} waits for a pin '
                'by hand (devloop pin)')
            for line in pin.describe_held(steps):  # #473: each hold with its step
                log(line)
        for line in unavailability.describe(state):
            log(line)
        if label_transition(dispatcher.gh, cfg.labels) == 'both':
            log(f'labels: {both_labels_warning(cfg.labels)}')
        snap = dispatcher.snapshot()
        for pr in snap.prs:
            log(pr_status_line(pr, cfg, dispatcher.status_reviews(pr)))
        # With the budget on (#393), the budget of the stories in progress; nothing when off.
        for line in dispatcher.budget_status(list(snap.working_issues)
                                             + [pr.issue for pr in snap.prs]):
            log(line)
        # The supervisor's temporary capacity limits in force (#412), as a tick plans with them;
        # read only, like a dry run: no command is answered here.
        planned = dispatcher._capacity(dry_run=True)
        log(pr_counts_line(snap, planned))
        for line in queue_lines(snap):
            log(line)
        for action in plan(snap, planned):
            log(f'next: {action.kind} #{action.number}'
                + (f': {action.reason}' if action.reason else ''))
        return
    if args.command == 'tick' and args.dry_run:
        # It plans and writes nothing: no dispatcher lock, so that the smoke test of a pin (#260)
        # runs it while a dispatcher runs. Its git commands do not fetch and write nothing to the
        # repository: the objects of merge-tree go to a temporary directory.
        with tempfile.TemporaryDirectory(prefix='devloop-dry-run-') as objects:
            dispatcher.ws.read_only(objects)
            log('[dry-run] read-only repository: the remote branches are those last fetched')
            dispatcher.tick(dry_run=True)
        return
    if args.command in ('tick', 'run'):
        # Before any agent turn (#415): an agent user that can read the keys stops devloop.
        ws = getattr(dispatcher, 'ws', None)
        agent_user = getattr(ws, 'agent_user', None)
        try:
            if isinstance(agent_user, AgentUser):
                common = getattr(ws, 'common_dir', None)
                check_agent_user(cfg, state, args.local_config, agent_user,
                                 git_dir=common() if callable(common) else None,
                                 env=getattr(ws, 'test_env', None))
        except AgentUserError as exc:
            sys.exit(f'devloop: {exc}')
    # tick and run, which act on the repository, hold its lock too (ADR 0006, R8): two
    # dispatchers on the same repository with different state_dirs never run together.
    acting = args.command in ('tick', 'run')
    with single_instance(state / 'dispatcher.lock') as lock, \
            (repo_lock(cfg) if acting else nullcontext()) as repo:
        hold_lock(dispatcher, lock.fileno(), repo.fileno() if repo is not None else None)
        recorder = None
        if args.command in ('tick', 'run'):
            # The trace T1 of the configuration (ADR 0006, K1), under the lock: no record, no
            # tick.
            try:
                recorder = record_start(dispatcher, log)
            except (ConfigError, GitHubError) as exc:
                sys.exit(f'devloop: the configuration was not recorded in the journal: {exc}')
        warn = None
        missing = getattr(dispatcher, 'config_missing', None)
        if args.command in ('tick', 'run') and missing is not None:
            # The tables of the project file this copy knows and lacks (#462): at the start
            # and at each reload of `run`.
            warn = project_tables_warning(cfg.run.alert_issue, state, dispatcher.gh)
            warn(missing)
        off = getattr(dispatcher, 'agent_user_off', None)
        if args.command in ('tick', 'run') and isinstance(off, bool):
            # Without an agent user (#490): a warning, once on alert_issue; devloop goes on.
            agent_user_warning(off, cfg.run.alert_issue, state, dispatcher.gh)
        if args.command == 'cleanup':
            for name in dispatcher.ws.names():
                dispatcher.ws.remove(name)
                log(f'removed worktree {name}')
            return
        if args.command == 'gc':
            # Under the dispatcher lock: no action runs (#380).
            if not cfg.gc.enabled():
                log('gc: every cleanup is off in [gc]')
                return
            dispatcher.housekeeping.run(cfg, verbose=True)
            return
        # Before any action (ADR 0005, P5-3, #407): the keys of the agents, renamed by role, and
        # the open PRs that name the keys of before.
        check_keys(dispatcher)
        try:
            if args.command == 'tick':
                dispatcher.tick(dry_run=args.dry_run)
            elif args.command == 'run':
                project = project_of(args)
                # Each reload checks the project file again before reading it (ADR 0006, D2).
                check = (lambda: check_project(project, state, dispatcher.ws.repo_root)
                         ) if project else None
                code = run_loop(cfg, state, dispatcher, timeout,
                                Reloader(cfg, args.config, args.local_config, project,
                                         log=log, record=recorder, check=check,
                                         notice=(lambda data: warn(missing_project_tables(data))
                                                 ) if warn else None))
                if code:
                    sys.exit(code)
        except pin.PinError as exc:
            sys.exit(f'devloop: {exc}')
        except pin.Repinned as exc:
            # Configuration, policy and dispatcher load again from the new copy, with the same
            # arguments and the lock still held (#174).
            log(f'restarting from {exc.copy}')
            if repo is not None:
                # The repository lock passes on open as well: no other dispatcher in between.
                os.set_inheritable(repo.fileno(), True)
                os.environ[REPO_LOCK_FD] = str(repo.fileno())
            pin.reexec(exc.copy, argv, lock.fileno(), LOCK_FD)


if __name__ == '__main__':
    main()
