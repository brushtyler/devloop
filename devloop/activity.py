"""The action a tick is running, recorded in ``state_dir`` so that ``devloop status`` can show it.

The tick writes the record when an action starts and removes it when the action ends, also on
error. The agent and the prompt are filled in when an agent turn starts, and ``stalled`` while
the turn shows no activity (see :mod:`devloop.stall`). A record whose dispatcher
no longer holds the lock, or whose process is gone, belongs to an interrupted tick.
"""

import fcntl
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

FILE = 'activity.json'
LOCK = 'dispatcher.lock'


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Activity:
    """The record of the running action. With parallel actions (#176) each thread has a record
    of its own: the file holds the only record as before, or ``{"actions": [...]}`` while
    several actions run. An agent turn updates the record of the thread that started it; a
    turn in a thread without an action (a reviewer running alongside another one) records
    nothing."""

    def __init__(self, state_dir, clock=time.time, pid=None):
        self.path = Path(state_dir) / FILE
        self._clock = clock
        self._pid = os.getpid() if pid is None else pid
        self._records = {}  # thread ident -> record, in start order
        self._lock = threading.RLock()

    @property
    def _record(self):
        return self._records.get(threading.get_ident())

    def _write(self):
        with self._lock:
            records = list(self._records.values())
            if not records:
                self.path.unlink(missing_ok=True)
                return
            data = records[0] if len(records) == 1 else {'actions': records}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f'.{self.path.name}.{self._pid}.tmp')
            tmp.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
            os.replace(tmp, self.path)

    def start(self, kind, number):
        with self._lock:
            self._records[threading.get_ident()] = {
                'kind': kind, 'number': number, 'agent': None, 'prompt': None,
                'started_at': _iso(self._clock()), 'pid': self._pid}
            self._write()

    def turn(self, agent, prompt, fallback=None):
        """An agent turn of the current action started, with the prompt saved at ``prompt``;
        ``fallback`` is the model it runs on when that is a fallback model (#121)."""
        with self._lock:
            record = self._record
            if record is None:
                return
            record.update(agent=agent, prompt=str(prompt))
            if fallback:
                record['fallback'] = fallback
            else:
                record.pop('fallback', None)
            record.pop('stalled', None)
            self._write()

    def stalled(self, since, command=None):
        """The agent turn shows no activity since ``since`` (epoch); ``command`` is the tool call
        in progress, if known."""
        with self._lock:
            record = self._record
            if record is None:
                return
            record['stalled'] = {'since': _iso(since), 'command': command}
            self._write()

    def resumed(self):
        """The stalled agent turn is active again."""
        with self._lock:
            record = self._record
            if record is not None and record.pop('stalled', None) is not None:
                self._write()

    def clear(self):
        with self._lock:
            self._records.pop(threading.get_ident(), None)
            self._write()

    @contextmanager
    def action(self, kind, number):
        self.start(kind, number)
        try:
            yield
        finally:
            self.clear()


def read(state_dir):
    """The recorded action, or None. An unreadable record is reported as such."""
    path = Path(state_dir) / FILE
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        return {'error': f'{path.name} unreadable: {exc}'}
    return record if isinstance(record, dict) else {'error': f'{path.name} is not an object'}


def lock_held(lock_path):
    """Whether another process holds the dispatcher lock. The probe takes a shared lock for an
    instant and releases it."""
    try:
        fh = open(lock_path, 'rb')
    except FileNotFoundError:
        return False
    with fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False


def pid_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def duration(seconds):
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f'{hours}h{minutes:02d}m{secs:02d}s'
    if minutes:
        return f'{minutes}m{secs:02d}s'
    return f'{secs}s'


def _since(stamp, now):
    """How long ago the ``_iso`` timestamp ``stamp`` was."""
    try:
        begun = datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return 'unknown time'
    return duration(now - begun.timestamp())


def describe(state_dir, now=None, alive=pid_alive, held=lock_held):
    """One line about the recorded action, one per action while several run in parallel
    (#176), or None when no action is recorded."""
    record = read(state_dir)
    if record is None:
        return None
    if 'error' in record:
        return f'current action: {record["error"]}'
    now = time.time() if now is None else now
    if isinstance(record.get('actions'), list):
        return '\n'.join(_describe(r, state_dir, now, alive, held) if isinstance(r, dict)
                         else 'current action: not an object' for r in record['actions'])
    return _describe(record, state_dir, now, alive, held)


def _describe(record, state_dir, now, alive, held):
    what = f'{record.get("kind")} #{record.get("number")}'
    if record.get('agent'):
        what += f' with {record["agent"]}'
        if record.get('fallback'):
            what += f' on fallback model {record["fallback"]}'
    started = record.get('started_at')
    elapsed = _since(started, now)
    prompt = f', prompt {record["prompt"]}' if record.get('prompt') else ''
    if held(Path(state_dir) / LOCK) and alive(record.get('pid')):
        stalled = record.get('stalled')
        if isinstance(stalled, dict):
            since = stalled.get('since')
            command = f', command: {stalled["command"]}' if stalled.get('command') else ''
            return (f'stalled: {what}, no agent activity for {_since(since, now)} (since {since}'
                    f'{command}); running for {elapsed} (since {started}{prompt})')
        return f'running: {what} for {elapsed} (since {started}{prompt})'
    return (f'interrupted: {what}, started {started} ({elapsed} ago{prompt}): no live dispatcher '
            'holds the lock')
