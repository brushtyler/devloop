"""devloop command line: python3 -m devloop
{setup,status,apps,tick,run,cleanup,follow,watch,pin}."""

import argparse
import fcntl
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from . import activity, follow, pin, watch
from .agents import AgentRunner, isolated_env
from .apps import DISPATCHER, Identities
from .config import ConfigError, load_config
from .dispatcher import (Dispatcher, TickLog, ensure_labels, plan, pr_counts_line, queue_lines,
                         run_forever)
from .github import GitHub, GitHubError
from .ledger import Ledger
from .review import last_round
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
    gh = identities.github(DISPATCHER)
    # Merges (#172) use the local gh login devloop runs with: no app gets contents=write.
    dispatcher = Dispatcher(cfg, gh, ws, runner, log=tick_log, identities=identities,
                            activity=current, ledger=Ledger(gh, state_dir=state),
                            merger=GitHub(cfg.repo),
                            pinner=pin.AutoPin(root, state, args.local_config, log=tick_log))
    return cfg, state, dispatcher


def run_pin(args):
    """``devloop pin <ref>`` (#174). The overlay is ``--local-config``, or
    ``<state_dir>/config.local.toml`` when it exists."""
    try:
        cfg = load_config(args.config, args.local_config)
    except ConfigError as exc:
        sys.exit(f'devloop: invalid config: {exc}')
    root = main_checkout()
    state = root / cfg.state_dir
    local = args.local_config or state / pin.LOCAL_CONFIG
    try:
        pin.pin(root, state, args.ref, local, log=log)
    except (pin.PinError, ConfigError, OSError) as exc:
        sys.exit(f'devloop: pin failed, the pinned copy is unchanged: {exc}')


def run_watch(parser, args, github=GitHub):
    """``devloop watch`` (#173). It reads GitHub with the ambient ``gh`` login: no app key is
    needed."""
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
    gh = github(cfg.repo)
    watcher = watch.Watcher(gh, watch.Trust.from_config(cfg), state,
                            interval=args.interval, timeout=args.timeout, exec_argv=exec_argv)
    gh.time_left = watcher.time_left  # no gh call outlasts --timeout
    try:
        return watcher.run()
    except KeyboardInterrupt:
        return 130


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
    run = sub.add_parser('run', help='tick forever')
    run.add_argument('--interval', type=int, default=600, help='seconds between ticks')
    sub.add_parser('cleanup', help='remove devloop worktrees left by failed steps')
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
    argv = sys.argv[1:] if argv is None else list(argv)
    args = parser.parse_args(argv)

    if args.command == 'pin':
        return run_pin(args)
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
        ensure_labels(dispatcher.gh, cfg.labels, cfg.review_policy)
        rp = cfg.review_policy
        log('labels ready: ' + ', '.join((*cfg.labels.all(), rp.label,
                                          *((rp.zero_label,) if rp.zero_enabled() else ()))))
        return
    if args.command == 'apps':
        failed = False
        for actor in [DISPATCHER, *cfg.agents]:
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
        snap = dispatcher.snapshot()
        for pr in snap.prs:
            labels = ', '.join(sorted(l for l in pr.labels if l.startswith('agent:')))
            conflict = ' CONFLICT with ' + cfg.base_branch if pr.merge_state == 'conflict' else ''
            log(f'PR #{pr.number} (issue #{pr.issue}) [{labels}] rounds={last_round(pr.reviews)} '
                f'ci={pr.checks} merge={pr.merge_state}{conflict}')
        log(pr_counts_line(snap, cfg))
        for line in queue_lines(snap):
            log(line)
        for action in plan(snap, cfg):
            log(f'next: {action.kind} #{action.number}')
        return
    with single_instance(state / 'dispatcher.lock') as lock:
        if args.command == 'cleanup':
            for name in dispatcher.ws.names():
                dispatcher.ws.remove(name)
                log(f'removed worktree {name}')
            return
        try:
            if args.command == 'tick':
                dispatcher.tick(dry_run=args.dry_run)
            elif args.command == 'run':
                run_forever(dispatcher, args.interval)
        except pin.PinError as exc:
            sys.exit(f'devloop: {exc}')
        except pin.Repinned as exc:
            # Configuration, policy and dispatcher load again from the new copy, with the same
            # arguments and the lock still held (#174).
            log(f'restarting from {exc.copy}')
            pin.reexec(exc.copy, argv, lock.fileno(), LOCK_FD)


if __name__ == '__main__':
    main()
