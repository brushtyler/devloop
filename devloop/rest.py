"""The roles at rest (#684): ``devloop run`` starts no new work while a role is at rest.

A role (the supervisor or the coordinator) that has nothing to do at the end of its turn declares
its rest: ``ops/host/role-wait.sh --rest`` writes ``<rest_dir>/<role>`` (``[run] rest_dir`` of the
overlay, ``$COORDINATION_DIR/rest`` of the host), and the role then waits for the sentinel of the
roles (``ops/host/role-sentinel.sh``) instead of beating. While a role is at rest the loop goes
on, but holds the actions that start new work, ``HELD``: a readiness check and an
implementation. The other actions run as before.

``RestHold.apply`` filters the plan of a tick. The first time it holds something it writes
``HOLD_FILE`` in the directory of the stream of the events (``events.DIRECTORY``), ``<since>
<roles> <actions>``: the sentinel wakes every role at rest on it, and the watchdog of the roles
reports it after ``REST_WAKE_TIMEOUT`` seconds. While it holds, the loop's waits call
``RestHold.poll`` every second: once no role is at rest it wakes the loop for a new tick, and
after ``[run] rest_wake_timeout`` seconds from the first hold it tells the operator once,
with ``devloop notify``, and goes on holding. A tick that holds nothing removes the file.

A directory devloop cannot read (the ACL of the host is missing) holds nothing: the loop works as
before, and a line of the log says so once.
"""

import os
import secrets
import time
from pathlib import Path

from .events import DIR_MODE, DIRECTORY, FILE_MODE
from .i18n import ENGLISH
from .notify import TEXTS as NOTIFY_TEXTS

HOLD_FILE = 'hold'
# The actions that start new work: held while a role is at rest.
HELD = ('preflight', 'implement')
ROLES = ('supervisor', 'coordinator')


class RestHold:
    """The hold of new work for the roles at rest of ``rest_dir``, with ``HOLD_FILE`` in
    ``state_dir/events``. ``timeout`` is ``[run] rest_wake_timeout``; ``notifier``
    (``notify.Notifier``), when given, gets the notice of the roles that do not wake up;
    ``clock`` is the wall clock."""

    def __init__(self, rest_dir, state_dir, timeout, *, log=print, notifier=None,
                 clock=time.time, language=ENGLISH):
        self.rest_dir = Path(rest_dir)
        self.hold_path = Path(state_dir) / DIRECTORY / HOLD_FILE
        self.timeout = timeout
        self.log = log
        self.notifier = notifier
        self.clock = clock
        self.language = language
        # Since when work is held (wall clock), what is held and whether the operator was told.
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
        """The roles at rest now, in ``ROLES`` order: those whose state is in ``rest_dir``."""
        found = []
        for role in ROLES:
            try:
                os.stat(self.rest_dir / role)
            except FileNotFoundError:
                continue
            except OSError as exc:
                if not self._unreadable:
                    self.log(f'rest: {self.rest_dir} cannot be read ({type(exc).__name__}): new '
                             'work starts as if no role were at rest (ops/host/migrate.md, "Il '
                             'riposo dei ruoli (#684)")')
                    self._unreadable = True
                return ()
            found.append(role)
        self._unreadable = False
        return tuple(found)

    def apply(self, actions):
        """``actions`` without those of ``HELD`` while a role is at rest."""
        held = [a for a in actions if a.kind in HELD]
        roles = self.resting() if held else ()
        if not roles:
            self.release()
            return actions
        listed = ', '.join(f'{a.kind} #{a.number}' for a in held)
        now = self.clock()
        if self.since is None:
            self.since, self.notified = now, False
        self.held, self.roles = listed, roles
        self._write(f'{int(self.since)} {",".join(roles)} {listed}\n')
        self.log(f'rest: {", ".join(roles)} at rest: {listed} held until the roles leave their '
                 f'rest (since {int(now - self.since)} seconds)')
        self._check_timeout(now)
        return [a for a in actions if a.kind not in HELD]

    def poll(self):
        """During a wait of the loop: True once the held work may start (no role at rest any
        more); the notice to the operator when the roles do not wake up in time."""
        if self.since is None:
            return False
        if not self.resting():
            self.log(f'rest: no role at rest any more: {self.held} may start')
            return True
        self._check_timeout(self.clock())
        return False

    def holding(self):
        return self.since is not None

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
        if self.notified or now - self.since < self.timeout:
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
                     'sentinel does not wake the roles for the held work')
            try:
                os.unlink(tmp)
            except OSError:
                pass
