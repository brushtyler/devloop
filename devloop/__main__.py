"""devloop command line: python3 -m devloop {setup,status,apps,tick,run,cleanup,follow}."""

import argparse
import fcntl
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from . import activity, follow
from .agents import AgentRunner, isolated_env
from .apps import DISPATCHER, Identities
from .config import ConfigError, load_config
from .dispatcher import Dispatcher, ensure_labels, plan, run_forever
from .github import GitHubError
from .ledger import Ledger
from .review import last_round
from .stall import StallWatch
from .workspace import Workspace


def main_checkout():
    """The main checkout of the repository containing the current directory."""
    common = subprocess.run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                            capture_output=True, text=True, check=True).stdout.strip()
    return Path(common).parent


@contextmanager
def single_instance(lock_path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, 'w') as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit('devloop: another dispatcher is running (lock held)')
        yield


def log(message):
    print(f'{time.strftime("%H:%M:%S")} {message}', flush=True)


def agent_environment(cfg, gh_config):
    """Environment for agent CLIs and the test command: :func:`isolated_env`, plus Bash time
    limits for Claude Code. A command may ask for up to the test timeout, which the full suite
    needs; one that asks for nothing stops after ``agent_command_timeout_seconds``, so a wait loop
    that never ends does not hold the turn for an hour (issue #96). An operator's own values are
    kept."""
    env = isolated_env(gh_config)
    env.setdefault('BASH_DEFAULT_TIMEOUT_MS', str(cfg.agent_command_timeout_seconds * 1000))
    env.setdefault('BASH_MAX_TIMEOUT_MS', str(cfg.test_timeout_seconds * 1000))
    return env


def build(args):
    cfg = load_config(args.config)
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
    ws = Workspace(root, cfg.state_dir, cfg.base_branch, test_env=agent_env)
    current = activity.Activity(state)
    # Session transcripts are found where the agent CLIs write them, in their HOME.
    stall = StallWatch(cfg.agent_stall_seconds, cfg.agent_stall_action, log=log,
                       activity=current, home=agent_env.get('HOME'))
    runner = AgentRunner(state / 'logs', cfg.agent_timeout_seconds, env=agent_env,
                         on_turn=lambda agent, prompt: current.turn(
                             agent.key, prompt, agent.model if agent.fallback_for else None),
                         stall=stall)
    gh = identities.github(DISPATCHER)
    dispatcher = Dispatcher(cfg, gh, ws, runner, log=log, identities=identities,
                            activity=current, ledger=Ledger(gh, state_dir=state))
    return cfg, state, dispatcher


def main(argv=None):
    parser = argparse.ArgumentParser(prog='devloop', description=__doc__)
    parser.add_argument('--config', help='TOML config (default: devloop/config.toml)')
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
    args = parser.parse_args(argv)

    if args.command == 'follow':
        # Local files only: no GitHub identities needed.
        if not follow.NAME.fullmatch(args.target):
            sys.exit(f'devloop: follow needs issue-<N> or pr-<N>, not {args.target!r}')
        try:
            cfg = load_config(args.config)
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
        log(activity.describe(state) or 'no action running')
        snap = dispatcher.snapshot()
        for pr in snap.prs:
            labels = ', '.join(sorted(l for l in pr.labels if l.startswith('agent:')))
            conflict = ' CONFLICT with ' + cfg.base_branch if pr.merge_state == 'conflict' else ''
            log(f'PR #{pr.number} (issue #{pr.issue}) [{labels}] rounds={last_round(pr.reviews)} '
                f'ci={pr.checks} merge={pr.merge_state}{conflict}')
        for issue in snap.ready_issues:
            log(f'ready #{issue["number"]} labelled by {issue["labeler"]}: {issue["title"]}')
        for action in plan(snap, cfg):
            log(f'next: {action.kind} #{action.number}')
        return
    with single_instance(state / 'dispatcher.lock'):
        if args.command == 'tick':
            dispatcher.tick(dry_run=args.dry_run)
        elif args.command == 'run':
            run_forever(dispatcher, args.interval)
        elif args.command == 'cleanup':
            for name in dispatcher.ws.names():
                dispatcher.ws.remove(name)
                log(f'removed worktree {name}')


if __name__ == '__main__':
    main()
