"""The long-lived OAuth token of the claude turns (#507).

Since the agent user (#415) the agents' ``~/.claude`` is root's and sealed (#438): Claude Code
cannot save the access token it renews in ``.credentials.json``, and the turns fail once it
expires. With ``[claude_token] enabled = true`` devloop gives every claude turn, and only those,
a long-lived token the operator generates with ``claude setup-token``, as
``CLAUDE_CODE_OAUTH_TOKEN`` in its isolated environment; ``~/.claude`` stays sealed.

- **The file.** ``claude.<YYYY-MM-DD>.oauth-token`` in the ``key_dir`` of the apps
  (``DEVLOOP_APP_KEY_DIR`` or ``[apps] key_dir``), which the agent user can neither read nor
  enter: the date in the name is the day the token was issued, as the date in the name of the
  keys of the apps. Exactly one such file, a regular file that is not a link, not readable by
  group or others, holding the token on one line. Anything else refuses the turn.
- **The age.** ``setup-token`` does not choose how long the token lasts (about a year), so
  devloop limits it: from ``rotate_days`` after the issue (default 30) one alert on
  ``[run].alert_issue`` with the procedure of the rotation (``RotationAlert``, once per token
  file); from ``max_days`` (default 90, at most ``MAX_DAYS``, a constant of the code that the
  configuration may only lower) the claude turns are refused before they start. A date that
  cannot be read, or a date in the future, counts as expired: devloop fails closed.
- **The secret.** The token is read at each claude turn and goes only to the environment of its
  CLI: never to the log, a record, a prompt or the output of a turn (``agents.AgentRunner``
  replaces it with ``[REDACTED]`` wherever the turn could echo it). The messages of this module
  name the file and its age, never its content.
"""

import json
import os
import re
import stat
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

# The cap of ``[claude_token] max_days``: a constant of the code, which the configuration and the
# overlay may only lower (the operator's decision on #507).
MAX_DAYS = 90
ROTATE_DAYS = 30
ENV = 'CLAUDE_CODE_OAUTH_TOKEN'
PREFIX = 'claude.'
SUFFIX = '.oauth-token'
GLOB = f'{PREFIX}*{SUFFIX}'
_DATE = re.compile(r'\d{4}-\d{2}-\d{2}')
# Where the procedure of creation, rotation and revocation is described.
PROCEDURE = 'ops/host/migrate.md, "Token di claude per l\'utente degli agent (#507)"'


class TokenRefused(Exception):
    """The token cannot be used: missing, ambiguous, unreadable, without a valid date, or older
    than ``max_days``. Its message names the file and why, never the token."""


@dataclass(frozen=True)
class TokenStatus:
    """What ``ClaudeToken.status`` found: the token file, the day it was issued and its age in
    days on ``today``; ``problem`` is '' for a token in use, else why it is refused;
    ``rotate``: its age reached ``rotate_days``."""
    path: Path
    issued: date
    age: int
    problem: str = ''
    rotate: bool = False


def issued_on(path):
    """The day in the name of the token file ``path`` (``claude.<YYYY-MM-DD>.oauth-token``), or
    None when there is none or it is not a valid day."""
    name = Path(path).name
    if not (name.startswith(PREFIX) and name.endswith(SUFFIX)):
        return None
    middle = name[len(PREFIX):-len(SUFFIX)]
    if not _DATE.fullmatch(middle):
        return None
    try:
        return date.fromisoformat(middle)
    except ValueError:
        return None


def today_utc(clock=time.time):
    return datetime.fromtimestamp(clock(), timezone.utc).date()


class ClaudeToken:
    """The token of ``key_dir`` and its limits. ``clock`` dates the age (UTC days)."""

    def __init__(self, key_dir, rotate_days=ROTATE_DAYS, max_days=MAX_DAYS, clock=time.time):
        self.key_dir = Path(key_dir).expanduser()
        self.rotate_days = rotate_days
        self.max_days = min(max_days, MAX_DAYS)  # the cap holds whatever the caller passes
        self._clock = clock

    def locate(self):
        """The one token file of ``key_dir``; ``TokenRefused`` when there is none or more."""
        try:
            found = sorted(p for p in self.key_dir.glob(GLOB))
        except OSError as exc:
            raise TokenRefused(f'{self.key_dir}: not readable ({type(exc).__name__})') from exc
        if not found:
            raise TokenRefused(f'no claude token ({GLOB}) in {self.key_dir}')
        if len(found) > 1:
            raise TokenRefused(f'{len(found)} claude tokens in {self.key_dir} ('
                               + ', '.join(p.name for p in found) + '): leave only the current '
                               'one, after revoking the others')
        return found[0]

    def status(self):
        """The ``TokenStatus`` of the token file; ``TokenRefused`` when there is no single
        file. A date that is missing, invalid or in the future is a ``problem``, as an age of
        ``max_days`` or more."""
        path = self.locate()
        issued = issued_on(path)
        today = today_utc(self._clock)
        if issued is None:
            return TokenStatus(path, None, None, f'{path.name}: no valid issue date in the name '
                               '(claude.<YYYY-MM-DD>.oauth-token): treated as expired')
        age = (today - issued).days
        if age < 0:
            return TokenStatus(path, issued, age, f'{path.name}: issue date {issued} in the '
                               'future: treated as expired')
        if age >= self.max_days:
            return TokenStatus(path, issued, age, f'{path.name}: issued on {issued}, {age} days '
                               f'ago, beyond max_days = {self.max_days}: expired', rotate=True)
        return TokenStatus(path, issued, age, rotate=age >= self.rotate_days)

    def value(self):
        """The token, for one claude turn; ``TokenRefused`` when it may not be used."""
        current = self.status()
        if current.problem:
            raise TokenRefused(current.problem)
        return read_token(current.path)


def read_token(path):
    """The token in ``path``: opened without following a link, a regular file with one link,
    not readable by group or others, one line that is not empty. ``TokenRefused`` otherwise."""
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise TokenRefused(f'{Path(path).name}: cannot be opened ({type(exc).__name__}; a link '
                           'is refused)') from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise TokenRefused(f'{Path(path).name}: not a regular file with a single link')
        if info.st_mode & 0o077:
            raise TokenRefused(f'{Path(path).name}: readable by group or others (chmod 600)')
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    try:
        text = b''.join(chunks).decode('utf-8').strip()
    except UnicodeDecodeError:
        raise TokenRefused(f'{Path(path).name}: not text') from None
    if not text or any(c.isspace() or not c.isprintable() for c in text):
        raise TokenRefused(f'{Path(path).name}: expected the token alone, on one line')
    return text


class RotationAlert:
    """The token file whose rotation was last alerted, in ``state_dir`` (``FILE``) so that a
    restart does not alert it again; without ``state_dir`` it lasts as long as the process. A
    new token has a new name, and is alerted in its turn. A file that cannot be read counts as
    empty: the alert is published again rather than lost."""

    FILE = 'claude-token-alerted.json'

    def __init__(self, state_dir=None):
        self._path = Path(state_dir) / self.FILE if state_dir is not None else None
        self._memory = None
        self._lock = threading.Lock()

    def alerted(self, name):
        with self._lock:
            if self._path is None:
                return self._memory == name
            try:
                data = json.loads(self._path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return False
            return isinstance(data, dict) and data.get('alerted') == name

    def mark(self, name):
        """Record the token file ``name`` as alerted; ``OSError`` when it cannot be written."""
        with self._lock:
            self._memory = name
            if self._path is None:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(f'.{self._path.name}.{os.getpid()}.tmp')
            tmp.write_text(json.dumps({'alerted': name}) + '\n', encoding='utf-8')
            os.replace(tmp, self._path)


def redact(text, token):
    """``text`` without ``token``, replaced with ``[REDACTED]``."""
    if not token or not isinstance(text, str):
        return text
    return text.replace(token, '[REDACTED]')


def redact_output(value, token):
    """``value`` (the output of a turn: dicts, lists, strings) with ``token`` replaced in every
    string."""
    if not token:
        return value
    if isinstance(value, str):
        return redact(value, token)
    if isinstance(value, dict):
        return {redact(k, token): redact_output(v, token) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_output(v, token) for v in value]
    return value
