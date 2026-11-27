"""The roles at rest (#684): ``devloop run`` starts no new work while a role is at rest.

A role (the supervisor or the coordinator) that has nothing to do at the end of its turn declares
its rest: ``ops/host/role-wait.sh --rest`` writes ``<rest_dir>/<role>`` (``[run] rest_dir`` of the
overlay, ``$COORDINATION_DIR/rest`` of the host), and the role then waits for the sentinel of the
roles (``ops/host/role-sentinel.sh``) instead of beating. While a role is at rest the loop goes
on, but holds the actions that start new work, ``HELD``: a readiness check and an
implementation. The other actions run as before.

``RestHold.apply`` filters the plan of a tick. While a role is at rest and there is work, held
actions or a queue that is not empty (``queue``: the ready issues, those being worked on and the
open PRs of the snapshot, the blocked ones included, which no action may start), it writes
``HOLD_FILE`` in the directory of the stream of the events (``events.DIRECTORY``), ``<since>
<roles> <work>``: the sentinel wakes every role at rest on it, and the watchdog of the roles
reports a rest with work after ``REST_WAKE_TIMEOUT`` seconds. While it holds actions, the loop's
waits call ``RestHold.poll`` every second: once no role is at rest it wakes the loop for a new
tick, and after ``[run] rest_wake_timeout`` seconds from the first time work was found it tells
the operator once, with ``devloop notify``, and goes on holding. A tick without work, or without
a role at rest, removes the file.

A rest that cannot be verified (``rest_dir`` missing, or a directory devloop cannot read: the ACL
of the host is missing) is not taken for no rest: the actions stay held, ``UNKNOWN`` takes the
place of the roles in the file, and the operator is told once, with ``devloop notify``, until the
directory can be read again.

The pause of the operator (#685, choice R4 (a)) is the second reason of the same mechanism: the
host command ``devloop --project <p> pause``, run by the operator as root, writes
``<rest_dir>/pause``, ``<start> pausa``, and stops the service; ``resume`` removes it and starts
the service again. Only root's file counts (``operator_pause``): a role, which may write in
``rest_dir``, cannot open the pause, nor remove root's file there (the sticky bit). While it is
on, ``devloop run`` starts no tick (``runloop.Loop`` stops at once, or after the tick in
progress): a service started by anyone but the operator's ``resume`` does not end the pause. So
that the watchdog of the roles can tell the work that waits in a pause, each tick also writes
``QUEUE_FILE`` in the directory of the stream of the events, ``<since> <queue>``: the queue of
its snapshot, and since when it is the same.

The maintenance of the host (#705) is the third reason: the host command ``devloop --project <p>
maintenance start "<sections>"``, run as root by whoever carries out a step on the host (the
sections of ``ops/host/migrate.md``), creates ``<rest_dir>/maintenance``, ``<start> manutenzione
<expiry> <sections>`` (``-`` in place of the expiry until the service is stopped), and stops the
service; ``maintenance end`` removes it and starts the service again, unless the operator's pause
is on. Only root's file counts (``host_maintenance``). While it is on, ``devloop run`` starts no
tick, as in the pause, and ``devloop pin`` refuses (``maintenance_refusal``), also after the
expiry (choice M5 (a) of the operator): only the pin of the host command itself, as root, during
that maintenance passes (``MAINTENANCE_PIN``).
"""

import os
import re
import secrets
import stat
import time
from pathlib import Path

from .events import DIR_MODE, DIRECTORY, FILE_MODE
from .i18n import ENGLISH
from .notify import TEXTS as NOTIFY_TEXTS

HOLD_FILE = 'hold'
# The queue of the latest tick (#685), for the watchdog of the roles during a pause.
QUEUE_FILE = 'queue'
# The pause of the operator (#685) in ``rest_dir``: a file of root only.
PAUSE_FILE = 'pause'
OPERATOR_UID = 0
PAUSE_LINE = re.compile(r'([0-9]{1,12}) pausa( [^\n]*)?\n?')
# The maintenance of the host (#705) in ``rest_dir``: a file of root only, as the pause.
MAINTENANCE_FILE = 'maintenance'
MAINTENANCE_LINE = re.compile(r'([0-9]{1,12}) manutenzione ([0-9]{1,12}|-) ([^\n]+)\n?')
# The environment variable with which the host command, as root, passes its own pin during the
# maintenance (#705): the start of that maintenance. A role cannot set it (its rules of sudo
# reset the environment).
MAINTENANCE_PIN = 'DEVLOOP_MAINTENANCE_PIN'
# The actions that start new work: held while a role is at rest.
HELD = ('preflight', 'implement')
ROLES = ('supervisor', 'coordinator')
# The roles of ``HOLD_FILE`` when the rest cannot be verified.
UNKNOWN = 'unknown'
# How many items of the queue ``HOLD_FILE`` names.
QUEUE_SHOWN = 10


def queue(snapshot):
    """The work in the queue of ``snapshot`` (``planning.Snapshot``, or None): ``issue #N`` for
    each ready issue (those blocked by a prerequisite included) and each issue being worked on,
    ``PR #N`` for each open PR. A role does not rest with any of them (ADR 0002, precision of
    2026-11-26, point 1)."""
    if snapshot is None:
        return []
    issues = {issue['number'] for issue in snapshot.ready_issues} | set(snapshot.working_issues)
    return ([f'issue #{number}' for number in sorted(issues)]
            + [f'PR #{number}' for number in sorted(pr.number for pr in snapshot.prs)])


def shown(waiting):
    """The items of the queue ``waiting``, at most ``QUEUE_SHOWN`` of them named."""
    more = len(waiting) - QUEUE_SHOWN
    return ', '.join(waiting[:QUEUE_SHOWN]) + (f' and {more} more' if more > 0 else '')


def operator_pause(rest_dir):
    """The start (seconds since the epoch) of the pause of the operator (#685), or None: the file
    ``PAUSE_FILE`` of ``rest_dir``, a regular file of root (``OPERATOR_UID``, not a link) that
    says ``<start> pausa``. Any other file there, one of a role included, is no pause; nor is a
    file that cannot be read."""
    match = PAUSE_LINE.fullmatch(_root_file(Path(rest_dir) / PAUSE_FILE, 200) or '')
    return int(match.group(1)) if match else None


def _root_file(path, size):
    """The text (at most ``size`` bytes) of ``path``, a regular file of root (``OPERATOR_UID``,
    not a link), or None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != OPERATOR_UID:
            return None
        return os.read(fd, size).decode('utf-8', 'replace')
    except OSError:
        return None
    finally:
        os.close(fd)


def host_maintenance(rest_dir):
    """The maintenance of the host (#705), ``(start, expiry, sections)``, or None: the file
    ``MAINTENANCE_FILE`` of ``rest_dir``, a regular file of root (not a link) that says ``<start>
    manutenzione <expiry> <sections>``; ``expiry`` is None until the service is stopped. Any
    other file there, one of a role included, is no maintenance."""
    match = MAINTENANCE_LINE.fullmatch(_root_file(Path(rest_dir) / MAINTENANCE_FILE, 1000) or '')
    if not match:
        return None
    expiry = None if match.group(2) == '-' else int(match.group(2))
    return int(match.group(1)), expiry, match.group(3).strip()


def _utc(seconds):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(seconds))


def describe_maintenance(maintenance, now=None):
    """The maintenance ``(start, expiry, sections)`` in words, for the log and the refusals."""
    start, expiry, sections = maintenance
    now = time.time() if now is None else now
    if expiry is None:
        until = 'the service is being stopped'
    elif now >= expiry:
        until = f'expired at {_utc(expiry)}, not ended yet'
    else:
        until = f'until {_utc(expiry)}'
    return f'since {_utc(start)} ({until}), sections: {sections}'


def maintenance_refusal(rest_dir, environ=os.environ):
    """Why ``devloop pin`` refuses (#705), or None: the maintenance of the host is on (also
    after its expiry, choice M5 (a)), and the pin is not that of the host command during this
    maintenance (``MAINTENANCE_PIN`` with its start)."""
    if not rest_dir:
        return None
    maintenance = host_maintenance(rest_dir)
    if maintenance is None or environ.get(MAINTENANCE_PIN) == str(maintenance[0]):
        return None
    return (f'the host is in maintenance (#705) {describe_maintenance(maintenance)}: no pin '
            'until it ends (maintenance end of the devloop command of the host); the step on '
            'the host pins with the devloop command of the host, as root')


class RestHold:
    """The hold of new work for the roles at rest of ``rest_dir``, with ``HOLD_FILE`` in
    ``state_dir/events``. ``timeout`` is ``[run] rest_wake_timeout``; ``notifier``
    (``notify.Notifier``), when given, gets the notice of the roles that do not wake up and of
    a rest that cannot be verified; ``clock`` is the wall clock."""

    def __init__(self, rest_dir, state_dir, timeout, *, log=print, notifier=None,
                 clock=time.time, language=ENGLISH):
        self.rest_dir = Path(rest_dir)
        self.hold_path = Path(state_dir) / DIRECTORY / HOLD_FILE
        self.queue_path = Path(state_dir) / DIRECTORY / QUEUE_FILE
        self.timeout = timeout
        self.log = log
        self.notifier = notifier
        self.clock = clock
        self.language = language
        # Since when there is work with a role at rest (wall clock), the actions held, the roles
        # at rest (None when they cannot be verified) and whether the operator was told.
        self.since = None
        self.held = ''
        self.roles = ()
        self.notified = False
        self._unreadable = False

    @classmethod
    def from_config(cls, cfg, state_dir, **kw):
        """The hold of ``[run] rest_dir``, or None when it is not set."""
        if not cfg.run.rest_dir:
            return None
        return cls(cfg.run.rest_dir, state_dir, cfg.run.rest_wake_timeout,
                   language=cfg.language, **kw)

    def resting(self):
        """The roles at rest now, in ``ROLES`` order: those whose state is in ``rest_dir``; None
        when it cannot be verified (``rest_dir`` missing, not a directory, or not readable)."""
        found = []
        try:
            if not stat.S_ISDIR(os.stat(self.rest_dir).st_mode):
                raise NotADirectoryError(str(self.rest_dir))
            for role in ROLES:
                try:
                    os.stat(self.rest_dir / role)
                except FileNotFoundError:
                    continue
                found.append(role)
        except OSError as exc:
            self._unverifiable(exc)
            return None
        if self._unreadable:
            self._unreadable = False
            self.log(f'rest: {self.rest_dir} can be read again')
        return tuple(found)

    def _unverifiable(self, exc):
        """Once until ``rest_dir`` can be read again: the log and the notice to the operator."""
        if self._unreadable:
            return
        self._unreadable = True
        error = type(exc).__name__
        self.log(f'rest: {self.rest_dir} cannot be read ({error}): the rest of the roles cannot '
                 'be verified, and no readiness check nor implementation starts until it can; '
                 'the operator is told (ops/host/migrate.md, "Il riposo dei ruoli (#684)")')
        if self.notifier is not None:
            t = NOTIFY_TEXTS.texts(self.language)
            self.notifier.notify(t('rest_unreadable', error=error), key='rest-unreadable')

    def paused(self):
        """The start of the pause of the operator (``operator_pause``), or None."""
        return operator_pause(self.rest_dir)

    def maintenance(self):
        """The maintenance of the host (``host_maintenance``), or None."""
        return host_maintenance(self.rest_dir)

    def apply(self, actions, snapshot=None):
        """``actions`` without those of ``HELD`` while a role is at rest, or while the rest
        cannot be verified; ``snapshot``, the one of the plan, gives the queue (``queue``), also
        written to ``QUEUE_FILE``."""
        held = [a for a in actions if a.kind in HELD]
        waiting = queue(snapshot)
        if snapshot is not None:
            self._write_queue(waiting)
        roles = self.resting() if held or waiting else ()
        if roles == ():
            self.release()
            return actions
        listed = ', '.join(f'{a.kind} #{a.number}' for a in held)
        now = self.clock()
        if self.since is None:
            self.since, self.notified = now, False
        self.held, self.roles = listed, roles
        work = []
        if listed:
            work.append(f'held: {listed}')
        if waiting:
            work.append('queue: ' + shown(waiting))
        who = UNKNOWN if roles is None else ','.join(roles)
        self._write(f'{int(self.since)} {who} {"; ".join(work)}\n')
        if roles is None:
            if listed:
                self.log(f'rest: the rest of the roles cannot be verified: {listed} held')
        elif listed:
            self.log(f'rest: {", ".join(roles)} at rest: {listed} held until the roles leave '
                     f'their rest (since {int(now - self.since)} seconds)')
        else:
            self.log(f'rest: {", ".join(roles)} at rest with work in the queue: the sentinel '
                     'wakes them')
        self._check_timeout(now)
        return [a for a in actions if a.kind not in HELD]

    def poll(self):
        """During a wait of the loop: True once the held work may start (no role at rest any
        more); the notice to the operator when the roles do not wake up in time."""
        if not self.held:
            return False
        roles = self.resting()
        if roles == ():
            self.log(f'rest: no role at rest any more: {self.held} may start')
            return True
        if roles is not None:
            self.roles = roles
            self._check_timeout(self.clock())
        return False

    def holding(self):
        """Whether actions are held: the waits of the loop poll the rest only then."""
        return bool(self.held)

    def release(self):
        """Nothing held any more: the file goes."""
        self.since, self.held, self.roles, self.notified = None, '', (), False
        try:
            self.hold_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            self.log(f'rest: {self.hold_path} could not be removed ({type(exc).__name__})')

    def _check_timeout(self, now):
        """The notice of the roles at rest that do not wake up: once, with held actions and
        known roles (a rest that cannot be verified has its own notice)."""
        if self.notified or not self.held or not self.roles or now - self.since < self.timeout:
            return
        self.notified = True
        roles = ', '.join(self.roles)
        self.log(f'rest: {self.held} held for more than {self.timeout} seconds: the roles at rest '
                 f'({roles}) have not woken up; the operator is told')
        if self.notifier is not None:
            t = NOTIFY_TEXTS.texts(self.language)
            self.notifier.notify(t('rest_hold', actions=self.held, roles=roles,
                                   seconds=self.timeout), key='rest-hold')

    def _write_queue(self, waiting):
        """``QUEUE_FILE``, ``<since> <queue>`` (``<since>`` alone with an empty queue): the time
        changes only with the queue."""
        items = shown(waiting)
        try:
            old = self.queue_path.read_text(encoding='utf-8').rstrip('\n').split(' ', 1)
            if old[0].isdigit() and (old[1] if len(old) > 1 else '') == items:
                return
        except OSError:
            pass
        text = f'{int(self.clock())} {items}'.rstrip() + '\n'
        self._write(text, self.queue_path,
                    'the watchdog of the roles does not see the work that waits in a pause')

    def _write(self, text, path=None,
               consequence='the sentinel does not wake the roles for the work'):
        """``path`` (``HOLD_FILE`` by default) with ``text``, atomically, ``FILE_MODE``: the
        default ACL of the directory of the stream gives its readers the reading of it."""
        path = path or self.hold_path
        directory = path.parent
        try:
            if path.read_text(encoding='utf-8') == text:
                return
        except OSError:
            pass
        tmp = directory / f'.{path.name}.{secrets.token_hex(4)}'
        try:
            directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                fh.write(text)
            os.replace(tmp, path)
        except OSError as exc:
            self.log(f'rest: {path} could not be written ({type(exc).__name__}): {consequence}')
            try:
                os.unlink(tmp)
            except OSError:
                pass
