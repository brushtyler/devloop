"""The stream of the events (#683): what ``devloop run`` reads on GitHub, for the roles' wake-ups.

One producer, the loop: a thread of ``devloop run`` (``Producer``) reads GitHub every
``WATCH_INTERVAL`` seconds, during a long action too, with the loop's own watcher
(``watch.Watcher`` on ``run-watch-state.json``), and appends each event to the stream, the same
lines ``devloop watch`` prints (labels, ``operator reply``, alarms, the routine of #682). The loop
waits for news on it (``Producer.wait``) instead of reading GitHub a second time.

The stream is the directory ``events/`` of the state_dir (``Stream``):

- ``events.log``, the current segment, in append only: one line per event,
  ``<seq> <YYYY-MM-DDTHH:MM:SSZ> <line>``, with a number that grows by one at each event and
  goes on across the segments, and the UTC time it was written. Each line is one ``write`` of
  an ``O_APPEND`` descriptor: a reader takes only the lines that end with a newline, and a line
  left half-written by a crash, which no reader has taken, is removed at the next start.
- ``events.log.1``, the previous segment: before a line would take the current one past
  ``MAX_SEGMENT`` bytes (10 MiB) the current one becomes the previous, which replaces the one
  before it. At most two segments, about 20 MiB, are kept.
- ``alive``, the sign of life: the time (seconds since the epoch) of the latest complete read
  of GitHub that succeeded, with or without events, replaced atomically.
- ``id``, the identity of the stream, written when the stream is created: a reader whose cursor
  names another one knows the stream was replaced.

The files are created ``FILE_MODE`` (640) and the directory ``DIR_MODE`` (750), so the
read-only ACL that setup-supervisor.sh and setup-coordinator.sh give the roles on the directory,
with its default for the new files, holds for each new segment (the mode of a new file bounds
the mask of its ACL). The producer knows no reader, and the readers cannot write the stream.
A stream that cannot be written is logged, and the loop goes on: the read is not confirmed (the
state of the watcher is not saved, the sign of life not renewed), so its events are written at
the first read after the stream recovers, and meanwhile the wake-up of the coordinator sees the
sign of life stop. At its start the producer goes on from the state the watcher saved, so what
changed while the loop was stopped is an event too.
"""

import os
import re
import secrets
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from .watch import ERROR_LINE, ERROR_ROUNDS, NOTHING_NEW, failure_class, load_state

DIRECTORY = 'events'
CURRENT = 'events.log'
PREVIOUS = 'events.log.1'
ALIVE = 'alive'
IDENTITY = 'id'
MAX_SEGMENT = 10 * 1024 * 1024
FILE_MODE = 0o640
DIR_MODE = 0o750
# Seconds between two reads of GitHub of the producer.
WATCH_INTERVAL = 60
# How long the end of the loop waits for a read in progress.
JOIN_SECONDS = 15
# How often a wait for news looks at the stop request and at the news.
POLL_SECONDS = 1
LINE = re.compile(r'(\d+) (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) (.*)')
# How much of the end of a segment is read to find its last number.
TAIL_BYTES = 64 * 1024


def one_line(text):
    """``text`` on one line: no control character (a newline of a label would split an event)."""
    return ''.join(' ' if unicodedata.category(ch).startswith('C') else ch for ch in text)


def last_number(data):
    """The number of the last complete event of ``data`` (bytes of a segment), or None."""
    for raw in reversed(data.split(b'\n')[:-1]):
        match = LINE.fullmatch(raw.decode('utf-8', 'replace'))
        if match:
            return int(match.group(1))
    return None


class Stream:
    """The stream of the events in ``state_dir/events``. ``now`` gives the UTC time of the
    lines and of the sign of life; ``max_bytes`` the size of a segment."""

    def __init__(self, state_dir, *, max_bytes=MAX_SEGMENT,
                 now=lambda: datetime.now(timezone.utc)):
        self.dir = Path(state_dir) / DIRECTORY
        self.max_bytes = max_bytes
        self._now = now
        self._seq = None

    @property
    def current(self):
        return self.dir / CURRENT

    @property
    def previous(self):
        return self.dir / PREVIOUS

    def _create(self, path):
        """A new file of the stream, ``FILE_MODE``, opened for writing."""
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)

    def open(self):
        """The directory, the identity and the number of the last event; a half-written line
        at the end of the current segment is removed, so it never joins the next one."""
        self.dir.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        identity = self.dir / IDENTITY
        if not identity.exists():
            self._replace(identity, secrets.token_hex(8) + '\n')
        seq = None
        for path in (self.current, self.previous):
            try:
                with path.open('rb') as fh:
                    size = fh.seek(0, os.SEEK_END)
                    fh.seek(max(0, size - TAIL_BYTES))
                    data = fh.read()
            except FileNotFoundError:
                continue
            if path == self.current and data and not data.endswith(b'\n'):
                # A line without its newline, left by a crash: never an event. Readers take only
                # complete lines, so none has read it.
                keep = size - len(data) + data.rfind(b'\n') + 1
                os.truncate(path, keep)
                data = data[:data.rfind(b'\n') + 1]
            seq = last_number(data)
            if seq is not None:
                break
        self._seq = seq or 0

    def _write(self, data):
        fd = os.open(self.current, os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_MODE)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)

    def append(self, lines):
        """Each of ``lines`` as an event, with its number and the time; ``open`` first, once."""
        if self._seq is None:
            self.open()
        stamp = self._now().strftime('%Y-%m-%dT%H:%M:%SZ')
        for line in lines:
            data = f'{self._seq + 1} {stamp} {one_line(line)}\n'.encode('utf-8')
            try:
                size = self.current.stat().st_size
            except FileNotFoundError:
                size = 0
            if size and size + len(data) > self.max_bytes:
                os.replace(self.current, self.previous)  # the segment before it goes
            self._write(data)
            self._seq += 1

    def alive(self):
        """The sign of life: the time of the read that just succeeded."""
        if self._seq is None:
            self.open()
        self._replace(self.dir / ALIVE, f'{int(self._now().timestamp())}\n')

    def _replace(self, path, text):
        """``path`` with ``text``, atomically: a temporary file that begins with ".", then a
        rename."""
        tmp = path.with_name(f'.{path.name}.{secrets.token_hex(4)}')
        fd = self._create(tmp)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                fh.write(text)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class Closed(Exception):
    """The producer is closing, or the loop must stop: no other read of GitHub starts."""


class Producer:
    """The loop's reader of GitHub (#683): ``watcher`` (``watch.Watcher``, its client in
    ``watcher.gh``) reads every ``interval`` seconds in a thread of its own; each event goes to
    ``stream`` before the state of the watcher is saved, so an interruption repeats an event and
    never loses it, and each read that succeeds renews the sign of life. Three failed reads in a
    row give ``watch.ERROR_LINE``, as ``devloop watch`` does. ``wait`` is the loop's wait for
    news (``runloop.Loop``)."""

    def __init__(self, watcher, stream, *, log, interval=WATCH_INTERVAL, clock=time.monotonic):
        self.watcher = watcher
        self.stream = stream
        self.log = log
        self.interval = interval
        self._clock = clock
        self._news = threading.Event()
        self._closing = threading.Event()
        self._stopped = None
        self._thread = None
        self._failures = 0
        self._alarmed = False
        self._loaded = False

    # The thread ------------------------------------------------------------
    def start(self, stopped=None):
        """Start reading. ``stopped`` (the loop's ``stop_reason``) is asked before each call to
        GitHub: after a stop no other read starts."""
        self._stopped = stopped
        self._thread = threading.Thread(target=self._run, name='devloop-events', daemon=True)
        self._thread.start()

    def close(self):
        """Stop reading: a read in progress starts no other call, and is waited for at most
        ``JOIN_SECONDS``."""
        self._closing.set()
        if self._thread is not None:
            self._thread.join(JOIN_SECONDS)

    def _time_left(self):
        if self._closing.is_set() or (self._stopped is not None and self._stopped()):
            raise Closed()
        return None

    def _run(self):
        # The limit belongs to this thread (``GitHub.time_left``): the ticks are not bound by it.
        self.watcher.gh.time_left = self._time_left
        while not self._closing.is_set():
            try:
                self.step()
            except Closed:
                return
            except Exception as exc:  # a read never ends the thread: the next one tries again
                self.log(f'events: the read failed ({failure_class(exc)})')
            self._closing.wait(self.interval)

    def _load(self):
        """The state the watcher saved before this start, once, before its first read: what
        changed since then is an event of the stream, not a new baseline."""
        self._loaded = True
        watcher = self.watcher
        watcher.state = load_state(watcher.state_path)
        if watcher.state is None and watcher.state_path.exists():
            self.log('events: the state of the watcher is unreadable, a new baseline replaces it')

    def step(self):
        """One read of GitHub; its events, also written to the stream. None when it failed, or
        when its events could not be written: then the state of the watcher is not saved and the
        sign of life not renewed, so the next read gives them again and the coordinator sees the
        stream stop."""
        if not self._loaded:
            self._load()
        undelivered = []

        def deliver(lines):
            if not self._write(lines):
                undelivered.append(lines)
                raise OSError(f'the stream {self.stream.dir} could not be written')
        lines = self.watcher.round(deliver=deliver)
        if undelivered:
            return None  # GitHub was read: no read failure, the same events at the next read
        if lines is None:
            self._failures += 1
            if self._failures == ERROR_ROUNDS:
                self._news.set()
            # The alarm is an event too: tried again at each failed read until it is written.
            if self._failures >= ERROR_ROUNDS and not self._alarmed:
                self._alarmed = self._write([ERROR_LINE])
            return None
        self._failures = 0
        self._alarmed = False
        for line in lines:
            self.log(f'news: {line}')
        if lines:
            self._news.set()
        try:
            self.stream.alive()
        except OSError as exc:
            self.log(f'events: the sign of life of {self.stream.dir} could not be written '
                     f'({type(exc).__name__})')
        return lines

    def _write(self, lines):
        """``lines`` appended to the stream: False, logged, when it cannot be written (the loop
        goes on; the coordinator sees the stream stop)."""
        try:
            self.stream.append(lines)
        except OSError as exc:
            self.log(f'events: {len(lines)} events could not be written to {self.stream.dir} '
                     f'({type(exc).__name__})')
            return False
        return True

    # The wait of the loop ----------------------------------------------------
    def wait(self, seconds, sleep, check):
        """News since the previous wait, or within ``seconds``: 0 at once when there are, else
        ``watch.NOTHING_NEW`` at the end. ``check`` (``runloop.Stopped`` on a stop) is called
        before each pause, ``sleep`` pauses."""
        start = self._clock()
        while True:
            check()
            if self._news.is_set():
                self._news.clear()
                return 0
            left = seconds - (self._clock() - start)
            if left <= 0:
                return NOTHING_NEW
            sleep(min(POLL_SECONDS, left))
