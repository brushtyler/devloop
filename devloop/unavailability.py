"""Memory of the models found unavailable (issue #151), kept in ``state_dir``.

When a turn of an agent with fallback models fails because its model is unavailable
(``agents.ModelUnavailable``, #121), the dispatcher records the pair ``(agent key, model)`` with
the time, the reason and the time it expects the model back: the one the error announces with
``Resets in <duration>`` (``reset_seconds``), else ``fallback_memory_seconds`` later. Until then
the later turns of that agent start from the first model of its chain that is not remembered.
The record survives a restart of the dispatcher; ``devloop fallback reset <agent>`` removes the
records of that agent.

Every change reads and writes the file under an exclusive ``flock`` of its own lock file, so
that the threads of parallel actions and the reset command never lose each other's changes. A
file that cannot be read, or whose content is not a memory, counts as empty: the turn tries the
configured model, as before #151.
"""

import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

FILE = 'model-unavailability.json'
LOCK = 'model-unavailability.lock'

# The only reset format recognised: ``Resets in`` and a duration of whole hours, minutes and
# seconds, in this order, each optional, at least one present (``152h4m35s``, ``4m``, ``30s``).
RESET = re.compile(r'\bResets in (?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?(?!\w)')


def reset_seconds(text):
    """The seconds after which the error in ``text`` says the model is back, or None."""
    for match in RESET.finditer(text or ''):
        if any(match.groups()):
            hours, minutes, seconds = (int(g or 0) for g in match.groups())
            return hours * 3600 + minutes * 60 + seconds
    return None


def delimited(pattern):
    """The regular expression of the unavailability ``pattern`` matched as a delimited word or
    code (#443): not preceded nor followed by a letter or a digit, so that ``429`` never matches
    inside an identifier such as ``1c42429607c6``."""
    return rf'(?<![^\W_])(?:{pattern})(?![^\W_])'


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _epoch(value):
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _valid(entry):
    return (isinstance(entry, dict) and isinstance(entry.get('reason'), str)
            and _epoch(entry.get('since')) is not None and _epoch(entry.get('until')) is not None)


class Memory:
    """The models each agent skips until their recorded time, persisted in
    ``<state_dir>/model-unavailability.json`` as ``{agent: {model: {since, until, reason}}}``,
    times in UTC ISO 8601."""

    def __init__(self, state_dir, clock=time.time):
        self.path = Path(state_dir) / FILE
        self._lock_path = Path(state_dir) / LOCK
        self._clock = clock

    def now(self):
        return self._clock()

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {key: {model: entry for model, entry in models.items() if _valid(entry)}
                for key, models in data.items() if isinstance(models, dict)}

    def _write(self, data):
        data = {key: models for key, models in data.items() if models}
        if not data:
            self.path.unlink(missing_ok=True)
            return
        tmp = self.path.with_name(f'.{self.path.name}.{os.getpid()}.tmp')
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        os.replace(tmp, self.path)

    @contextmanager
    def _locked(self):
        """The memory read under the lock, written back when the block ends, without the
        records already expired."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path, 'w') as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            data = self._read()
            before = json.dumps(data, sort_keys=True)
            yield data
            now = self.now()
            data = {key: {model: entry for model, entry in models.items()
                          if _epoch(entry['until']) > now}
                    for key, models in data.items()}
            if json.dumps(data, sort_keys=True) != before:
                self._write(data)

    def record(self, key, model, reason, until):
        """Remember that ``key`` finds ``model`` unavailable, for ``reason``, until the epoch
        ``until``. A new record of the same pair replaces the old one. Returns the record."""
        entry = {'since': iso(self.now()), 'until': iso(until), 'reason': reason}
        with self._locked() as data:
            data.setdefault(key, {})[model] = entry
        return entry

    def active(self, key, model):
        """The record of ``(key, model)`` while it has not expired, else None."""
        entry = self._read().get(key, {}).get(model)
        return entry if entry and _epoch(entry['until']) > self.now() else None

    def forget(self, key, model):
        """Remove the record of ``(key, model)``, if any."""
        if self._read().get(key, {}).get(model) is None:
            return
        with self._locked() as data:
            data.get(key, {}).pop(model, None)

    def reset(self, key):
        """Remove every record of the agent ``key``, and only those. Returns the models whose
        record was still active, sorted."""
        with self._locked() as data:
            now = self.now()
            removed = data.pop(key, {})
        return sorted(model for model, entry in removed.items() if _epoch(entry['until']) > now)

    def entries(self):
        """``(key, model, record)`` for each record not yet expired, sorted."""
        now = self.now()
        return [(key, model, entry) for key, models in sorted(self._read().items())
                for model, entry in sorted(models.items()) if _epoch(entry['until']) > now]


def describe(state_dir, now=None):
    """The lines ``devloop status`` shows for the remembered models, one per pair."""
    memory = Memory(state_dir, clock=time.time if now is None else lambda: now)
    return [f'{key} skips model {model} until {entry["until"]} (unavailable since '
            f'{entry["since"]}: {entry["reason"]})' for key, model, entry in memory.entries()]
