"""The action a tick is running, recorded in ``state_dir`` so that ``devloop status`` can show it.

The tick writes the record when an action starts and removes it when the action ends, also on
error. The agent and the prompt are filled in when an agent turn starts. A record whose dispatcher
no longer holds the lock, or whose process is gone, belongs to an interrupted tick.
"""

import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

FILE = 'activity.json'
LOCK = 'dispatcher.lock'


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Activity:
    def __init__(self, state_dir, clock=time.time, pid=None):
        self.path = Path(state_dir) / FILE
        self._clock = clock
        self._pid = os.getpid() if pid is None else pid
        self._record = None

    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f'.{self.path.name}.{self._pid}.tmp')
        tmp.write_text(json.dumps(self._record, indent=2) + '\n', encoding='utf-8')
        os.replace(tmp, self.path)

    def start(self, kind, number):
        self._record = {'kind': kind, 'number': number, 'agent': None, 'prompt': None,
                        'started_at': _iso(self._clock()), 'pid': self._pid}
        self._write()

    def turn(self, agent, prompt):
        """An agent turn of the current action started, with the prompt saved at ``prompt``."""
        if self._record is None:
            return
        self._record.update(agent=agent, prompt=str(prompt))
        self._write()

    def clear(self):
        self._record = None
        self.path.unlink(missing_ok=True)

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


def describe(state_dir, now=None, alive=pid_alive, held=lock_held):
    """One line about the recorded action, or None when no action is recorded."""
    record = read(state_dir)
    if record is None:
        return None
    if 'error' in record:
        return f'current action: {record["error"]}'
    what = f'{record.get("kind")} #{record.get("number")}'
    if record.get('agent'):
        what += f' with {record["agent"]}'
    started = record.get('started_at')
    try:
        begun = datetime.strptime(started, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        elapsed = duration((time.time() if now is None else now) - begun.timestamp())
    except (TypeError, ValueError):
        elapsed = 'unknown time'
    prompt = f', prompt {record["prompt"]}' if record.get('prompt') else ''
    if held(Path(state_dir) / LOCK) and alive(record.get('pid')):
        return f'running: {what} for {elapsed} (since {started}{prompt})'
    return (f'interrupted: {what}, started {started} ({elapsed} ago{prompt}): no live dispatcher '
            'holds the lock')
