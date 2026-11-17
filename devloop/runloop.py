"""``devloop run`` (#177): the continuous loop of ticks.

1. A tick runs.
2. If it ran at least one action, the next one starts at once.
3. Otherwise the loop waits for news on GitHub, as ``devloop watch --until-change`` does (#173),
   for at most ``idle_timeout`` seconds; then a tick runs anyway.

It stops cleanly on ``SIGTERM`` or ``SIGINT``, or when the ``STOP`` file of the state directory
exists: the tick in progress ends, no other starts, and a wait in progress is cut short. The
``STOP`` file is never removed by devloop: while it exists ``devloop run`` exits at once, without
a tick, so not even a restart by a service manager starts the loop again.

A tick fails when it raises an error (an action that fails is escalated by the tick itself, and
the tick succeeds). After a failed tick the loop waits ``BACKOFF_SECONDS`` (1 minute after the
first failure in a row, 5 after the second); the ``MAX_FAILURES``-th failure in a row stops the
loop at once, with an alert: a comment of the dispatcher on ``[run].alert_issue``, one attempt,
or the local log when none is configured or the comment fails. A tick that succeeds resets the
count. A new pin (``Repinned``, #174) is not a failure: it propagates, and the process starts
again from the new copy, unless a stop was requested during that tick: then the loop ends.
"""

import signal
import time
from pathlib import Path

from .markdown import dispatcher_header, publishable
from .pin import Repinned
from .watch import NOTHING_NEW, failure_class

STOP_FILE = 'STOP'
# The state of the loop's own watcher, apart from the one of `devloop watch` (one per file).
WATCH_STATE_FILE = 'run-watch-state.json'
IDLE_TIMEOUT = 900
# Seconds between two reads of GitHub while the loop waits for news.
WATCH_INTERVAL = 60
# The wait after the 1st, 2nd and 3rd failed tick in a row; the 3rd is never waited, since the
# MAX_FAILURES-th failure stops the loop at once.
BACKOFF_SECONDS = (60, 300, 1800)
MAX_FAILURES = 3
# Exit code of a loop stopped by MAX_FAILURES failed ticks in a row, alert published or not.
FAILED = 4
# How often a wait looks at the stop request and the STOP file.
POLL_SECONDS = 1
SIGNALS = (signal.SIGTERM, signal.SIGINT)


class Stopped(Exception):
    """A stop was requested during a wait."""


def alert_body(classes):
    """The alert of a loop stopped after ``classes``, the failure classes of its last ticks
    (``watch.failure_class``): no message, path or token of the errors."""
    listed = ', '.join(f'`{name}`' for name in classes)
    return publishable(
        dispatcher_header()
        + '\n## `devloop run` si è fermato\n\n'
        + f'{len(classes)} tick consecutivi sono falliti con un errore: il ciclo si è fermato e '
          'non avvia altri tick.\n\n'
        + f'Classi degli errori, dal primo all\'ultimo: {listed}. Il dettaglio è nel log locale '
          'del ciclo.\n\n'
        + 'Rimossa la causa, l\'operatore riavvia il ciclo (`devloop run` o il suo servizio).')


class Loop:
    """``tick()`` runs one tick and returns its actions. ``watch(timeout, sleep, check)`` waits
    for news on GitHub for at most ``timeout`` seconds, sleeping with ``sleep`` and calling
    ``check`` (``Stopped`` on a stop) before each read, and returns 0 on news or
    ``watch.NOTHING_NEW`` at the timeout. ``alert(body)`` publishes the alert, None
    without ``[run].alert_issue``. ``clock`` is monotonic."""

    def __init__(self, tick, watch, stop_file, *, log, alert=None, alert_issue=0,
                 idle_timeout=IDLE_TIMEOUT, clock=time.monotonic, sleep=time.sleep):
        self.tick = tick
        self.watch = watch
        self.stop_file = Path(stop_file)
        self.log = log
        self.alert = alert
        self.alert_issue = alert_issue
        self.idle_timeout = idle_timeout
        self._clock = clock
        self._sleep = sleep
        self.signalled = None

    # Stop ------------------------------------------------------------------
    def _on_signal(self, signum, frame):
        # Only a flag: the loop logs it once the tick in progress ends.
        self.signalled = signal.Signals(signum).name

    def stop_reason(self):
        """Why the loop must stop, or None."""
        if self.signalled:
            return self.signalled
        if self.stop_file.exists():
            return f'the {self.stop_file.name} file'
        return None

    def check_stop(self):
        """``Stopped`` when a stop is requested: the watcher calls it before each read of
        GitHub, so a stop during a read lets no other read start."""
        if self.stop_reason():
            raise Stopped()

    def pause(self, seconds):
        """Sleep ``seconds``, a little at a time; ``Stopped`` as soon as a stop is requested."""
        deadline = self._clock() + seconds
        while True:
            self.check_stop()
            left = deadline - self._clock()
            if left <= 0:
                return
            self._sleep(min(POLL_SECONDS, left))

    # Loop ------------------------------------------------------------------
    def run(self):
        """Run until a stop (0) or ``MAX_FAILURES`` failed ticks in a row (``FAILED``)."""
        if self.stop_file.exists():
            self.log(f'{self.stop_file} exists: no tick runs until the operator removes it')
            return 0
        previous = {sig: signal.signal(sig, self._on_signal) for sig in SIGNALS}
        try:
            return self._loop()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def _loop(self):
        failures = []
        while True:
            reason = self.stop_reason()
            if reason:
                self.log(f'stop requested ({reason}): no further tick')
                return 0
            try:
                actions = self.tick()
            except Repinned:
                reason = self.stop_reason()
                if reason:  # the restart would forget a signal: no new copy, no further tick
                    self.log(f'stop requested ({reason}): no restart from the new pin, '
                             'no further tick')
                    return 0
                raise
            except Exception as exc:  # a failed tick: counted, never lost
                failures.append(failure_class(exc))
                self.log(f'tick failed ({len(failures)} in a row): {type(exc).__name__}: {exc}')
                if len(failures) >= MAX_FAILURES:
                    return self._give_up(failures)
                wait = BACKOFF_SECONDS[len(failures) - 1]
                self.log(f'next tick in {wait} seconds')
                self._wait(self.pause, wait)
                continue
            failures = []
            if actions:
                continue
            self._wait(self._idle)

    def _wait(self, how, *args):
        if self.stop_reason():
            return  # no wait, not even a read of GitHub, after a stop: the loop ends
        try:
            how(*args)
        except Stopped:
            pass  # the top of the loop logs the stop

    def _idle(self):
        """Wait for news on GitHub, at most ``idle_timeout`` seconds. A watcher that fails
        waits the rest of the time."""
        self.log(f'no action: waiting for news on GitHub, at most {self.idle_timeout} seconds')
        start = self._clock()
        try:
            code = self.watch(self.idle_timeout, self.pause, self.check_stop)
        except Stopped:
            raise
        except Exception as exc:  # the next tick runs at the timeout all the same
            self.log(f'waiting for news failed ({failure_class(exc)}): next tick at the timeout')
            self.pause(self.idle_timeout - (self._clock() - start))
            return
        self.log('no news: next tick' if code == NOTHING_NEW else 'news on GitHub: next tick')

    def _give_up(self, failures):
        summary = (f'ALERT: {len(failures)} consecutive ticks failed '
                   f'({", ".join(failures)}): the loop stops')
        if self.alert is None:
            self.log(f'{summary}; no [run].alert_issue is configured')
            return FAILED
        try:
            self.alert(alert_body(failures))
        except Exception as exc:  # one attempt only: its class, never its output
            self.log(f'{summary}; the alert on issue #{self.alert_issue} could not be published '
                     f'({failure_class(exc)})')
            return FAILED
        self.log(f'{summary}; alert published on issue #{self.alert_issue}')
        return FAILED
