"""Stalled agent turns: no transcript event and no child process using CPU for a while (#100).

While an agent turn runs, the dispatcher checks two signals at regular intervals:

- the newest session transcript of the turn's worktree, the files ``devloop follow`` reads: a
  write counts as activity;
- the CPU time of the processes the agent CLI started (its descendants and the members of its
  session, the CLI itself excluded, plus what reaped children used): at least ``CPU_SHARE`` of
  the time elapsed since the previous check counts as activity, so a test suite is active and a
  ``sleep`` or a wait loop is not.

After ``seconds`` without either, the turn is reported once as stalled in the tick log and in the
activity record that ``devloop status`` shows. With ``action = "abort"`` the turn is also
stopped, and the tick escalates it to a human like any agent failure. With ``"warn"`` it goes on,
and is reported again as active if it recovers.

The CPU signal reads ``/proc`` (Linux). Where it is not available the check is off: without it a
long test suite that writes nothing to the transcript would look stalled.
"""

import os
import time
from pathlib import Path

from .activity import duration
from .agents import AgentError
from .config import STALL_ACTIONS
from .follow import find_transcript, pending_call

CPU_SHARE = 0.01
PROC = Path('/proc')


class AgentStalled(AgentError):
    """An agent turn stopped by ``agent_stall_action = "abort"``."""


def _ticks_per_second():
    try:
        return os.sysconf('SC_CLK_TCK')
    except (AttributeError, ValueError, OSError):
        return 100


def cpu_ticks(leader, proc=PROC):
    """``{pid: CPU ticks}`` of the processes a turn started: the descendants of ``leader`` and
    the members of its session, with what their reaped children used, and for ``leader`` only
    what its reaped children used. None when ``proc`` cannot be read."""
    try:
        entries = [e for e in proc.iterdir() if e.name.isdigit()]
    except OSError:
        return None
    stats = {}
    for entry in entries:
        try:
            text = (entry / 'stat').read_text(encoding='ascii', errors='replace')
            # After the command name, in parentheses: state ppid pgrp session ... utime(12th)
            # stime cutime cstime.
            fields = text.rpartition(')')[2].split()
            stats[int(entry.name)] = (int(fields[1]), int(fields[3]),
                                      [int(f) for f in fields[11:15]])
        except (OSError, ValueError, IndexError):
            continue  # gone meanwhile, or not readable
    children = {}
    for pid, (ppid, _, _) in stats.items():
        children.setdefault(ppid, []).append(pid)
    members = {pid for pid, (_, session, _) in stats.items() if session == leader}
    seen, todo = set(), list(children.get(leader, ()))
    while todo:
        pid = todo.pop()
        if pid not in seen:
            seen.add(pid)
            todo.extend(children.get(pid, ()))
    members |= seen
    members.discard(leader)
    ticks = {pid: sum(stats[pid][2]) for pid in members}
    if leader in stats:
        ticks[leader] = sum(stats[leader][2][2:])
    return ticks


class StallWatch:
    """Checks agent turns for stalls; ``turn(agent, workdir)`` gives the check for one turn,
    for :func:`devloop.workspace.run_in_group`'s ``watch``."""

    def __init__(self, seconds, action='warn', *, log=print, activity=None, home=None,
                 clock=time.time, cpu=cpu_ticks, interval=None):
        if action not in STALL_ACTIONS:
            raise ValueError(action)
        self.seconds = seconds
        self.action = action
        self.interval = interval if interval is not None else min(30.0, max(seconds / 10, 1.0))
        self.log = log
        self.activity = activity
        self.home = Path.home() if home is None else Path(home)
        self.clock = clock
        self.cpu = cpu

    def turn(self, agent, workdir):
        return _Turn(self, agent, Path(workdir))


class _Turn:
    def __init__(self, watch, agent, workdir):
        self.w, self.agent, self.workdir = watch, agent, workdir
        self.last = watch.clock()   # last activity seen; the turn starting counts
        self.sample = self.sampled_at = None
        self.reported = False
        self.unavailable = False

    def _transcript(self):
        try:
            return find_transcript(self.workdir, self.w.home)
        except OSError:
            return None

    def __call__(self, pid):
        w, now = self.w, self.w.clock()
        sample = w.cpu(pid)
        if sample is None:
            if not self.unavailable:
                self.unavailable = True
                w.log(f'  stall check off for {self.agent}: cannot read the CPU time of its '
                      'processes')
            return
        transcript = self._transcript()
        if transcript is not None:
            try:
                self.last = max(self.last, transcript.stat().st_mtime)
            except OSError:
                pass
        if self.sample is not None:
            used = sum(max(0, t - self.sample.get(p, 0)) for p, t in sample.items())
            if used / _ticks_per_second() >= CPU_SHARE * (now - self.sampled_at):
                self.last = now
        self.sample, self.sampled_at = sample, now
        idle = now - self.last
        if idle < w.seconds:
            if self.reported:
                self.reported = False
                w.log(f'  {self.agent} in {self.workdir.name} is active again')
                if w.activity:
                    w.activity.resumed()
            return
        if not self.reported:
            self.reported = True
            command = pending_call(transcript) if transcript is not None else None
            self.what = (f'{self.agent} in {self.workdir.name} stalled: no transcript event nor '
                         f'child process using CPU for {duration(idle)}')
            w.log(f'  {self.what}' + (f', command: {command}' if command else '')
                  + ('; stopping it (agent_stall_action = "abort")' if w.action == 'abort'
                     else '; not stopped (agent_stall_action = "warn")'))
            if w.activity:
                w.activity.stalled(self.last, command)
        if w.action == 'abort':
            # The message reaches the escalation comment: no transcript text in it.
            raise AgentStalled(f'{self.what}; stopped by agent_stall_action = "abort"')
