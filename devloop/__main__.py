"""devloop command line: python3 -m devloop
{setup,status,apps,tick,run,cleanup,gc,follow,watch,pin,report,fallback}."""

import argparse
import fcntl
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from . import activity, follow, pin, report, runloop, unavailability, watch
from .agents import AgentRunner, isolated_env
from .apps import DISPATCHER, MERGER, Identities
from .config import ConfigError, Reloader, load_config
from .context import both_labels_warning, label_transition
from .dispatch_advisory import AdvisoryFailures, AdvisoryResults
from .dispatcher import (Dispatcher, TickLog, Workers, ensure_labels, pr_counts_line,
                         pr_status_line)
from .github import GitHub, GitHubError
from .housekeeping import Housekeeping
from .journal import MIB
from .ledger import Ledger
from .outbox import for_identities
from .planning import plan, queue_lines
from .stall import StallWatch
from .workspace import Workspace

# The descriptor of the dispatcher lock, passed on by a restart from a new pinned copy (#174).
LOCK_FD = 'DEVLOOP_LOCK_FD'


def main_checkout():
    """The main checkout of the repository containing the current directory."""
    common = subprocess.run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                            capture_output=True, text=True, check=True).stdout.strip()
    return Path(common).parent


def _inherited_lock(lock_path):
    """The lock a process that restarted itself from a new pinned copy passed on open (#174),
    or None. Only a descriptor of this very lock file counts."""
    value = os.environ.pop(LOCK_FD, None)
    if not value or not value.isdigit():
        return None
    fd = int(value)
    try:
        if not os.path.samestat(os.fstat(fd), os.stat(lock_path)):
            return None
    except OSError:
        return None
    os.set_inheritable(fd, False)
    return open(fd, 'w')


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


def log(message):
    print(f'{time.strftime("%H:%M:%S")} {message}', flush=True)


def agent_environment(cfg, gh_config):
    """Environment for agent CLIs and the test command: :func:`isolated_env`, plus Bash time
    limits for Claude Code. A command may ask for up to the test timeout, which the full suite
    needs; one that asks for nothing stops after ``agent_command_timeout_seconds``, so a wait loop
    that never ends does not hold the turn for an hour (issue #96). An operator's own values are
    kept."""
    env = isolated_env(gh_config)
    # The pinned copy on PYTHONPATH (#174) is devloop's own: the tests of a worktree import the
    # worktree's code.
    env.pop('PYTHONPATH', None)
    env.setdefault('BASH_DEFAULT_TIMEOUT_MS', str(cfg.agent_command_timeout_seconds * 1000))
    env.setdefault('BASH_MAX_TIMEOUT_MS', str(cfg.test_timeout_seconds * 1000))
    return env


def build(args):
    cfg = load_config(args.config, args.local_config)
    root = main_checkout()
    state = root / cfg.state_dir
    try:
        identities = Identities.from_config(cfg)
    except OSError as exc:
        raise ConfigError(f'GitHub App key: {exc}. Set $DEVLOOP_APP_KEY_DIR or remove the app '
                          'from [apps] to use the ambient gh login.') from exc
    # Agents and the test command never see the operator's GitHub credentials.
    gh_config = state / 'agent-gh-config'
    gh_config.mkdir(parents=True, exist_ok=True)
    agent_env = agent_environment(cfg, gh_config)
    # With parallel actions (#176) each test run gets a temporary directory of its own.
    ws = Workspace(root, cfg.state_dir, cfg.base_branch, test_env=agent_env,
                   private_tmp=cfg.max_parallel_actions > 1)
    current = activity.Activity(state)
    # One log for the whole tick: each line whole, with the number of its action (#176).
    tick_log = TickLog(log)
    # Session transcripts are found where the agent CLIs write them, in their HOME.
    stall = StallWatch(cfg.agent_stall_seconds, cfg.agent_stall_action, log=tick_log,
                       activity=current, home=agent_env.get('HOME'))
    runner = AgentRunner(state / 'logs', cfg.agent_timeout_seconds, env=agent_env,
                         on_turn=lambda agent, prompt: current.turn(
                             agent.key, prompt, agent.model if agent.fallback_for else None),
                         stall=stall)
    # Merges (#172) run with the merger's app (#201), the only one that gets contents=write,
    # or without it with the local gh login devloop runs with.
    # Without the dispatcher lock, status and a dry run read the journal of the turns and never
    # write it (#362): the running dispatcher keeps its chain in memory.
    command = getattr(args, 'command', None)
    read_only = command == 'status' or (command == 'tick' and getattr(args, 'dry_run', False))
    ledger = Ledger(None, state_dir=state, read_only=read_only,
                    journal_max_bytes=cfg.journal_max_mib * MIB)
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
                            pinner=pin.AutoPin(root, state, args.local_config, log=tick_log,
                                               fetch_lock=ws.shared_lock,
                                               keep=keep_pins(cfg)),
                            memory=unavailability.Memory(state),
                            advisory_results=AdvisoryResults(state),
                            advisory_failures=AdvisoryFailures(state),
                            housekeeping=Housekeeping(state, ws, gh, ledger=ledger,
                                                      log=tick_log))
    return cfg, state, dispatcher


def hold_lock(dispatcher, lock_fd):
    """With the outbox of the publications (#385), the `gh` of each write holds the dispatcher
    lock ``lock_fd`` too, under a timeout of its own (``GitHub.KILL_AFTER``): no next dispatcher
    starts while one of them may still publish, even after this process stopped. Without it
    (#397, ADR 0004, D8) no `gh` gets the lock: a dispatcher killed while a `gh` of its runs
    leaves the lock free at once, as before #385. Returns whether the lock was passed on."""
    identities = getattr(dispatcher, 'identities', None)
    if getattr(identities, 'outbox', None) is None:
        return False
    identities.hold(lock_fd)
    return True


def keep_pins(cfg):
    """How many pinned copies besides ``current`` a successful pin keeps (#380), or None when
    ``[gc] pins`` is off."""
    return cfg.gc.keep_pins if cfg.gc.pins else None


def run_fallback(args):
    """``devloop fallback reset <agent>`` (#151): forget the models ``agent`` remembers
    unavailable, so that its next turn starts from the configured model. Local files only."""
    try:
        cfg = load_config(args.config, args.local_config)
    except ConfigError as exc:
        sys.exit(f'devloop: invalid config: {exc}')
    memory = unavailability.Memory(main_checkout() / cfg.state_dir)
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
    new copy (#260), and the log says so."""
    try:
        cfg = load_config(args.config, args.local_config)
    except ConfigError as exc:
        sys.exit(f'devloop: invalid config: {exc}')
    root = main_checkout()
    state = root / cfg.state_dir
    local = args.local_config or state / pin.LOCAL_CONFIG
    try:
        pin.pin(root, state, args.ref, local, log=log, smoke=not args.no_smoke,
                keep=keep_pins(cfg))
    except (pin.PinError, ConfigError, OSError) as exc:
        sys.exit(f'devloop: pin failed, the pinned copy is unchanged: {exc}')


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
        cfg = load_config(args.config, args.local_config)
    except ConfigError as exc:
        sys.exit(f'devloop: invalid config: {exc}')
    state = Path(args.state) if args.state else main_checkout() / cfg.state_dir / watch.STATE_FILE
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
        cfg = load_config(args.config, args.local_config)
    except ConfigError as exc:
        sys.exit(f'devloop: invalid config: {exc}')
    if args.publish and not cfg.report.issue:
        sys.exit('devloop: report --publish: no [report] issue is configured. Set the number of '
                 'the dedicated issue as "issue" in [report] of the local overlay.')
    try:
        gh = Identities.from_config(cfg).github(DISPATCHER)
    except OSError as exc:
        sys.exit(f'devloop: invalid config: GitHub App key: {exc}. Set $DEVLOOP_APP_KEY_DIR or '
                 'remove the app from [apps] to use the ambient gh login.')
    trust = watch.Trust.from_config(cfg)
    try:
        if args.publish:
            state = main_checkout() / cfg.state_dir
            report.build(cfg, gh, trust, state, log=log).publish(day)
            return
        day = day or report.utc_today()
        summary = report.Records(gh, trust, day, day, cfg.agents).summary(day)
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
    argv = sys.argv[1:] if argv is None else list(argv)
    args = parser.parse_args(argv)

    if args.command == 'run':
        timeout = idle_timeout(parser, args)
        if args.interval is not None:
            log('run: --interval is deprecated, use --idle-timeout')
    if args.command == 'pin':
        return run_pin(args)
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
            cfg = load_config(args.config, args.local_config)
        except ConfigError as exc:
            sys.exit(f'devloop: invalid config: {exc}')
        root = main_checkout() / cfg.state_dir / 'worktrees'
        try:
            sys.exit(follow.follow(follow.worktrees(root, args.target)))
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
        for actor in [DISPATCHER, MERGER, *cfg.agents]:
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
        if failed:
            sys.exit(1)
        return
    if args.command == 'status':
        for line in (activity.describe(state) or 'no action running').split('\n'):
            log(line)
        restart = activity.describe_restart(state)
        if restart:
            log(restart)
        for line in unavailability.describe(state):
            log(line)
        if label_transition(dispatcher.gh, cfg.labels) == 'both':
            log(f'labels: {both_labels_warning(cfg.labels)}')
        snap = dispatcher.snapshot()
        for pr in snap.prs:
            log(pr_status_line(pr, cfg, dispatcher.status_reviews(pr)))
        log(pr_counts_line(snap, cfg))
        for line in dispatcher.status_budget(snap):
            log(line)
        for line in queue_lines(snap):
            log(line)
        for action in plan(snap, cfg):
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
    with single_instance(state / 'dispatcher.lock') as lock:
        hold_lock(dispatcher, lock.fileno())
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
        try:
            if args.command == 'tick':
                dispatcher.tick(dry_run=args.dry_run)
            elif args.command == 'run':
                code = run_loop(cfg, state, dispatcher, timeout,
                                Reloader(cfg, args.config, args.local_config, log=log))
                if code:
                    sys.exit(code)
        except pin.PinError as exc:
            sys.exit(f'devloop: {exc}')
        except pin.Repinned as exc:
            # Configuration, policy and dispatcher load again from the new copy, with the same
            # arguments and the lock still held (#174).
            log(f'restarting from {exc.copy}')
            pin.reexec(exc.copy, argv, lock.fileno(), LOCK_FD)


if __name__ == '__main__':
    main()
