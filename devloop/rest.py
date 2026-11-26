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
"""

import os
import secrets
import stat
import time
from pathlib import Path

from .events import DIR_MODE, DIRECTORY, FILE_MODE
from .i18n import ENGLISH
from .notify import TEXTS as NOTIFY_TEXTS

HOLD_FILE = 'hold'
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


class RestHold:
    """The hold of new work for the roles at rest of ``rest_dir``, with ``HOLD_FILE`` in
    ``state_dir/events``. ``timeout`` is ``[run] rest_wake_timeout``; ``notifier``
    (``notify.Notifier``), when given, gets the notice of the roles that do not wake up and of
    a rest that cannot be verified; ``clock`` is the wall clock."""

    def __init__(self, rest_dir, state_dir, timeout, *, log=print, notifier=None,
                 clock=time.time, language=ENGLISH):
        self.rest_dir = Path(rest_dir)
        self.hold_path = Path(state_dir) / DIRECTORY / HOLD_FILE
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

    def apply(self, actions, snapshot=None):
        """``actions`` without those of ``HELD`` while a role is at rest, or while the rest
        cannot be verified; ``snapshot``, the one of the plan, gives the queue (``queue``)."""
        held = [a for a in actions if a.kind in HELD]
        waiting = queue(snapshot)
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
            more = len(waiting) - QUEUE_SHOWN
            work.append('queue: ' + ', '.join(waiting[:QUEUE_SHOWN])
                        + (f' and {more} more' if more > 0 else ''))
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

    def _write(self, text):
        """``HOLD_FILE`` with ``text``, atomically, ``FILE_MODE``: the default ACL of the
        directory of the stream gives its readers the reading of it."""
        directory = self.hold_path.parent
        try:
            if self.hold_path.read_text(encoding='utf-8') == text:
                return
        except OSError:
            pass
        tmp = directory / f'.{HOLD_FILE}.{secrets.token_hex(4)}'
        try:
            directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                fh.write(text)
            os.replace(tmp, self.hold_path)
        except OSError as exc:
            self.log(f'rest: {self.hold_path} could not be written ({type(exc).__name__}): the '
                     'sentinel does not wake the roles for the work')
            try:
                os.unlink(tmp)
            except OSError:
                pass
