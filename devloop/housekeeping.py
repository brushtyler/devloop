"""The cleanup of ``state_dir`` (#380): pinned copies, worktrees of closed work, old logs.

``Housekeeping`` runs at a tick with nothing running, at most every ``[gc] interval_minutes``
(``tick``), and at once with ``devloop gc`` (``run``), which holds the dispatcher lock. What it
removes, each removal logged:

- **pinned copies** (``[gc] pins``): those beyond ``current`` and the ``keep_pins`` pinned most
  recently, never one a process uses (``pin.prune``, under the lock of the pins);
- **worktrees** (``[gc] worktrees``): ``issue-<n>…`` of a closed issue, ``pr-<n>…`` of a closed
  or merged PR, read fresh from GitHub; never one of an issue or PR with a turn still open in the
  ledger (``ledger-open-turns.json``). A worktree with another name is ``devloop cleanup``'s;
- **log files** (``[gc] logs``, off by default): the files directly in ``logs/`` not modified
  for ``log_days`` days, never one of an open issue or PR in an escalation
  (``agent:needs-operator``, ``agent:needs-supervisor``): its evidence (``timeout-…``,
  ``conclave-…``) and its turns' logs, which the escalation cites. Nor the files of an issue or
  PR with a turn still open in the ledger, nor an evidence without its issue or PR. The state
  and labels of each issue or PR are read fresh, one by one, not from a list that a limit could
  cut short. When the ledger or an issue or PR cannot be read, the files they would protect
  stay.

A failure is logged and never fails the tick.
"""

import json
import os
import re
import stat
import time
from pathlib import Path

from . import pin
from .github import GitHubError
from .ledger import LedgerError
from .workspace import WorkspaceError

STATE = 'gc-state.json'
LOGS = 'logs'
# A worktree of devloop: the implementer's, the readiness check's, the reviewer's, a conclave's.
WORKTREE = re.compile(r'(issue|pr)-(\d+)(?:-.+)?')
# The issue or PR of a file of the logs: a turn's (``<time>-issue-7-implement-1-claude.log``) or
# an evidence (``timeout-pr-9-fix-r2.diff``, ``conclave-issue-7-<comment>.diff``).
LOG_ITEM = re.compile(r'(?:^|-)(issue|pr)-(\d+)-')
EVIDENCE = ('timeout-', 'conclave-')
DAY = 86400


class Housekeeping:
    """``state``: the state directory; ``ws``: the ``Workspace`` of the worktrees; ``gh``: the
    dispatcher's ``ports.Backend``; ``ledger``: the ``ledger.Ledger`` whose ``pending`` lists the
    issues and PRs with turns not closed, or None; ``in_use``: for ``pin.prune``."""

    def __init__(self, state, ws, gh, ledger=None, log=print, clock=time.time, in_use=None):
        self.state, self.ws, self.gh, self.ledger = Path(state), ws, gh, ledger
        self.log, self.clock, self.in_use = log, clock, in_use

    # When ------------------------------------------------------------------
    def _last(self):
        try:
            value = json.loads((self.state / STATE).read_text(encoding='utf-8')).get('last')
        except (OSError, ValueError, AttributeError):
            return None
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def _done(self):
        self.state.mkdir(parents=True, exist_ok=True)
        tmp = self.state / f'.{STATE}-{os.getpid()}'
        tmp.write_text(json.dumps({'last': self.clock()}) + '\n', encoding='utf-8')
        os.replace(tmp, self.state / STATE)

    def tick(self, cfg):
        """The cleanup of a tick with nothing running: ``run`` when ``[gc]`` enables something
        and ``interval_minutes`` passed since the last one."""
        policy = cfg.gc
        if not policy.enabled():
            return None
        last = self._last()
        if last is not None and self.clock() - last < policy.interval_minutes * 60:
            return None
        return self.run(cfg)

    def run(self, cfg, verbose=False):
        """Run the cleanups ``[gc]`` enables; returns ``{what: names removed}``. ``verbose``
        logs the totals even when nothing was removed (``devloop gc``)."""
        policy = cfg.gc
        removed = {'pinned copies': [], 'worktrees': [], 'log files': []}
        open_turns = self._open_turns()
        if policy.pins:
            try:
                removed['pinned copies'] = pin.prune_now(self.state, policy.keep_pins,
                                                         log=self.log, in_use=self.in_use)
            except OSError as exc:
                self.log(f'gc: the cleanup of the pinned copies failed: {exc}')
        if policy.worktrees:
            removed['worktrees'] = self.worktrees(open_turns)
        if policy.logs:
            removed['log files'] = self.logs(cfg, open_turns)
        try:
            self._done()
        except OSError as exc:
            self.log(f'gc: {STATE} not written: {exc}')
        if verbose or any(removed.values()):
            self.log('gc: removed ' + ', '.join(f'{len(names)} {what}'
                                                for what, names in removed.items()))
        return removed

    def _open_turns(self):
        """The issues and PRs with a turn still open in the ledger, or None when they cannot be
        read."""
        if self.ledger is None:
            return set()
        try:
            return set(self.ledger.pending())
        except LedgerError as exc:
            self.log(f'gc: the open turns of the ledger cannot be read: {exc}')
            return None

    # Worktrees ---------------------------------------------------------------
    def _closed(self, kind, number):
        """``closed``/``merged`` when the issue or PR is, else None."""
        if kind == 'issue':
            state = (self.gh.issue(number) or {}).get('state')
        else:
            state = (self.gh.pr_state(number) or {}).get('state')
        state = (state or '').upper()
        return state.lower() if state in ('CLOSED', 'MERGED') else None

    def worktrees(self, open_turns):
        """Remove the worktrees of closed issues and of closed or merged PRs; returns their
        names. ``open_turns``: the numbers to keep, or None to keep every worktree."""
        if open_turns is None:
            self.log('gc: worktrees not cleaned up: the open turns of the ledger are unknown')
            return []
        groups = {}
        for name in self.ws.names():
            match = WORKTREE.fullmatch(name)
            if match:
                groups.setdefault((match[1], int(match[2])), []).append(name)
        removed = []
        for (kind, number), names in sorted(groups.items(), key=lambda item: item[0][::-1]):
            if number in open_turns:
                continue
            try:
                state = self._closed(kind, number)
            except GitHubError as exc:
                self.log(f'gc: the state of {kind} #{number} cannot be read, its worktrees stay: '
                         f'{exc}')
                continue
            if state is None:
                continue
            for name in names:
                try:
                    self.ws.remove(name)
                except (WorkspaceError, OSError) as exc:
                    self.log(f'gc: the worktree {name} could not be removed: {exc}')
                    continue
                if self.ws.path(name).exists():
                    self.log(f'gc: the worktree {name} could not be removed')
                    continue
                removed.append(name)
                self.log(f'gc: removed the worktree {name} ({kind} #{number} {state})')
        return removed

    # Logs ----------------------------------------------------------------
    def _escalated(self, kind, number, names):
        """Whether ``kind`` (``issue``/``pr``) ``number`` is open with one of the labels
        ``names``, read fresh: not from a list of the open escalations, which a limit could cut
        short. None when it cannot be read."""
        try:
            item = (self.gh.issue(number) if kind == 'issue' else self.gh.pr_state(number)) or {}
            state = (item.get('state') or '').upper()
            labels = {label.get('name') for label in item.get('labels') or []}
        except (GitHubError, AttributeError, TypeError) as exc:
            self.log(f'gc: the state of {kind} #{number} cannot be read, its log files stay: '
                     f'{exc}')
            return None
        return state not in ('CLOSED', 'MERGED') and bool(labels & names)

    def logs(self, cfg, open_turns):
        """Remove the files of ``logs/`` not modified for ``[gc] log_days`` days, except those
        of an open escalation (its evidence and its turns' logs, which it cites), those of a
        turn still open and an evidence without its issue or PR; returns their names.
        ``open_turns`` None keeps every file of an issue or PR."""
        folder = self.state / LOGS
        cutoff = self.clock() - cfg.gc.log_days * DAY
        old = []
        try:
            entries = sorted(folder.iterdir()) if folder.is_dir() else []
        except OSError as exc:
            self.log(f'gc: {folder} cannot be read: {exc}')
            return []
        for path in entries:
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                old.append(path)
        names = set(cfg.labels.operator()) | {cfg.labels.needs_supervisor}
        escalated = {}  # (kind, number): True, False, or None when it cannot be read
        removed = []
        for path in old:
            match = LOG_ITEM.search(path.name)
            if match is None:
                if path.name.startswith(EVIDENCE):
                    continue  # an evidence whose issue or PR is unknown
            else:
                item = (match[1], int(match[2]))
                if open_turns is None or item[1] in open_turns:
                    continue
                if item not in escalated:
                    escalated[item] = self._escalated(*item, names)
                if escalated[item] is not False:
                    continue
            try:
                path.unlink()
            except OSError as exc:
                self.log(f'gc: the log file {path.name} could not be removed: {exc}')
                continue
            removed.append(path.name)
            self.log(f'gc: removed the log file {path.name} (older than {cfg.gc.log_days} days)')
        return removed
