"""``devloop run`` (#177): the continuous loop of ticks.

1. A tick runs.
2. If it ran at least one action, the next one starts at once.
3. Otherwise the loop waits for news on GitHub for at most ``idle_timeout`` seconds; then a
   tick runs anyway.

The news come from the loop's one reader of GitHub (#683, ``events.Producer``): a thread that
reads every minute, during a long action too, as ``devloop watch`` does (#173), and writes each
event to the stream of the events of the state_dir, which the wake-ups of the roles read.

It stops cleanly on ``SIGTERM`` or ``SIGINT``, or when the ``STOP`` file of the state directory
exists: the tick in progress ends, no other starts, and a wait in progress is cut short. The
``STOP`` file is never removed by devloop: while it exists ``devloop run`` exits at once, without
a tick, so not even a restart by a service manager starts the loop again.

A tick fails when it raises an error (an action that fails is escalated by the tick itself, and
the tick succeeds). After a failed tick the loop waits ``BACKOFF_SECONDS`` (1 minute after the
first failure in a row, 5 after the second); the ``MAX_FAILURES``-th failure in a row stops the
loop at once, with an alert: a comment of the dispatcher on ``[run].alert_issue``, one attempt,
or the local log when none is configured or the comment fails; then, with ``[notify]`` (#593),
a notice to the operator's phone that names the issue of the alert. A tick that succeeds resets
the count. A new pin (``Repinned``, #174) is not a failure: it propagates, and the process starts
again from the new copy, unless a stop was requested during that tick: then the loop ends.

Continuous mode (#227, with ``workers``, ``dispatcher.Workers``): a tick starts its actions in
the free slots and returns without waiting for them. While some still run, the loop waits for
one to end; with a slot free, also for news on GitHub, at most ``idle_timeout`` seconds. Either
way the next tick plans at once, so a long action no longer holds the others. A pin after a merge
does not stop the planning either (#301): the restart from the new copy (``Repinned``) comes from
the first tick at which nothing runs. A stop, or the
``MAX_FAILURES``-th failed tick, waits for the running actions to end before the loop returns.
"""

import signal
import time
from pathlib import Path

from .i18n import ENGLISH, Catalog
from .markdown import dispatcher_header, publishable
from .notify import TEXTS as NOTIFY_TEXTS
from .pin import Repinned
from .watch import NOTHING_NEW, failure_class

STOP_FILE = 'STOP'
# The state of the loop's own watcher, the producer of the stream of the events (#683), apart
# from the one of `devloop watch` (one per file).
WATCH_STATE_FILE = 'run-watch-state.json'
IDLE_TIMEOUT = 900
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


class Woken(Stopped):
    """Continuous mode (#227): an action ended during a wait, and the next tick plans its
    slot."""


# The alert of a stopped loop in the project's ``language`` (#635); English for a language
# without translation.
TEXTS = Catalog(
    en={
        'title': '`devloop run` has stopped',
        'text': ('{count} consecutive ticks failed with an error: the loop has stopped and starts '
                 'no other tick.\n\n'
                 'Classes of the errors, from the first to the last: {listed}. The detail is in '
                 'the local log of the loop.\n\n'
                 'Once the cause is removed, the operator restarts the loop (`devloop run` or its '
                 'service).'),
    },
    it={
        'title': '`devloop run` si è fermato',
        'text': ('{count} tick consecutivi sono falliti con un errore: il ciclo si è fermato e '
                 'non avvia altri tick.\n\n'
                 'Classi degli errori, dal primo all\'ultimo: {listed}. Il dettaglio è nel log '
                 'locale del ciclo.\n\n'
                 'Rimossa la causa, l\'operatore riavvia il ciclo (`devloop run` o il suo '
                 'servizio).'),
    })


def alert_body(classes, language=ENGLISH):
    """The alert of a loop stopped after ``classes``, the failure classes of its last ticks
    (``watch.failure_class``), in ``language`` (#635): no message, path or token of the
    errors."""
    t = TEXTS.texts(language)
    listed = ', '.join(f'`{name}`' for name in classes)
    return publishable(dispatcher_header() + f'\n## {t("title")}\n\n'
                       + t('text', count=len(classes), listed=listed))


class Loop:
    """``tick()`` runs one tick and returns its actions. ``watch(timeout, sleep, check)`` waits
    for news on GitHub for at most ``timeout`` seconds, sleeping with ``sleep`` and calling
    ``check`` (``Stopped`` on a stop) before each read, and returns 0 on news or
    ``watch.NOTHING_NEW`` at the timeout. ``alert(body)`` publishes the alert, None
    without ``[run].alert_issue``, in ``language`` (#635). ``clock`` is monotonic. ``workers``,
    in continuous mode (#227), holds the actions the ticks started and that may still run.
    ``notify`` (``notify.Notifier``, #593), when given, sends the notice of a stopped loop to the
    operator, after the alert, with the number of its issue. ``producer`` (``events.Producer``,
    #683), when given, is started once the signals are handled and closed when the loop ends: its
    ``wait`` is then the ``watch``."""

    def __init__(self, tick, watch, stop_file, *, log, alert=None, alert_issue=0,
                 idle_timeout=IDLE_TIMEOUT, clock=time.monotonic, sleep=time.sleep,
                 workers=None, language=ENGLISH, notify=None, producer=None):
        self.tick = tick
        self.producer = producer
        self.notify = notify
        self.language = language
        self.watch = watch
        self.workers = workers
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

    def check_wake(self):
        """``check_stop``, and ``Woken`` once an action of the continuous mode ended (#227)."""
        self.check_stop()
        if self.workers is not None and self.workers.ended():
            raise Woken()

    def pause(self, seconds, check=None):
        """Sleep ``seconds``, a little at a time; ``Stopped`` as soon as a stop is requested
        (``check``, by default ``check_stop``, raises it)."""
        check = check or self.check_stop
        deadline = self._clock() + seconds
        while True:
            check()
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
            if self.producer is not None:
                self.producer.start(self.stop_reason)
            return self._loop()
        finally:
            if self.producer is not None:
                self.producer.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            if self.workers is not None:
                self.workers.close()

    def _loop(self):
        failures = []
        while True:
            reason = self.stop_reason()
            if reason:
                self.log(f'stop requested ({reason}): no further tick')
                self._drain()
                return 0
            try:
                actions = self.tick()
            except Repinned:
                # A restart happens only when no action runs (continuous mode included, #301).
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
                    self._drain()
                    return self._give_up(failures)
                wait = BACKOFF_SECONDS[len(failures) - 1]
                self.log(f'next tick in {wait} seconds')
                self._wait(self.pause, wait)
                continue
            failures = []
            if self.workers is not None and self.workers.running():
                self._wait(self._busy)
                continue
            if actions:
                continue
            if self.workers is not None and self.workers.ended():
                # The last action ended during the tick, after it read what runs: its result is
                # not in that plan, and no wait would notice it.
                self.log('an action ended during the tick: next tick')
                continue
            self._wait(self._idle)

    def _drain(self):
        """Continuous mode (#227): wait for the running actions to end; none starts."""
        if self.workers is None or not self.workers.running():
            return
        self.log(f'waiting for {self._running()} to end')
        while self.workers.running():
            self._sleep(POLL_SECONDS)

    def _running(self):
        return ', '.join(f'{action.kind} #{action.number}'
                         for action, _ in self.workers.running()) or 'no action'

    def _busy(self):
        """Continuous mode (#227): wait while actions run. With every slot taken, until one
        ends; with a slot free, also until news on GitHub arrive, at most ``idle_timeout``
        seconds. A watcher that fails waits the rest of the time, or until an action ends."""
        try:
            if self.workers.full():
                self.log(f'every slot is taken ({self._running()}): waiting for an action '
                         'to end')
                while True:
                    self.check_wake()
                    self._sleep(POLL_SECONDS)
            self.log(f'running: {self._running()}; waiting for an action to end or for news '
                     f'on GitHub, at most {self.idle_timeout} seconds')
            start = self._clock()

            def wait(seconds):
                self.pause(seconds, self.check_wake)
            try:
                code = self.watch(self.idle_timeout, wait, self.check_wake)
            except Stopped:
                raise
            except Exception as exc:  # the next tick runs when an action ends or at the timeout
                self.log(f'waiting for news failed ({failure_class(exc)}): next tick when an '
                         'action ends or at the timeout')
                wait(self.idle_timeout - (self._clock() - start))
                return
        except Woken:
            self.log('an action ended: next tick')
            return
        self.log('no news: next tick' if code == NOTHING_NEW else 'news on GitHub: next tick')

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
        published = False
        if self.alert is None:
            self.log(f'{summary}; no [run].alert_issue is configured')
        else:
            try:
                self.alert(alert_body(failures, self.language))
            except Exception as exc:  # one attempt only: its class, never its output
                self.log(f'{summary}; the alert on issue #{self.alert_issue} could not be '
                         f'published ({failure_class(exc)})')
            else:
                published = True
                self.log(f'{summary}; alert published on issue #{self.alert_issue}')
        self._notify_stopped(failures, published)
        return FAILED

    def _notify_stopped(self, failures, published):
        """The notice of the stopped loop to the operator's phone (#593), with the issue of the
        alert when it was published there; ``notify.Notifier.notify`` never raises."""
        if self.notify is None:
            return
        t = NOTIFY_TEXTS.texts(self.language)
        text = t('stopped', count=len(failures), classes=', '.join(failures))
        text += (t('stopped_issue', issue=self.alert_issue) if published
                 else t('stopped_log'))
        self.notify.notify(text, key='run-stopped')
