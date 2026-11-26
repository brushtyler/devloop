"""The notices to the operator outside GitHub (#593): ``devloop notify``.

The operator learns that devloop needs them from three channels, each with what goes in it
(docs/devloop/guide.md, "Notices to the operator"):

- the decisions on an issue or a PR: a mention of ``operator_mentions`` in the comment that asks
  for it (``markdown.with_mentions``), which reaches the phone through GitHub Mobile;
- what does not or cannot stay on GitHub (a service down, a refused start, a stopped loop, the
  host's problems with their details): this module, outbound only;
- a vulnerability of devloop not yet fixed: a private advisory of GitHub (``SECURITY.md``),
  never here.

A notice is a short text with a **key**: the same key is not sent again within ``interval``
seconds of ``[notify]`` (``NOTIFY_INTERVAL`` by default). The state is ``STATE_FILE`` in the
state_dir, beside the overlay, shared by every caller of the project (the dispatcher, the
roles through their sudoers, the unit of a service down), under ``LOCK_FILE``. It is updated
at **every attempt**, before the send, a failed one too: one attempt per key and interval, and
never a retry. A failure is a line in the log, and never stops the caller: ``Notifier.notify``
does not raise.

The channel is a small port (``Channel``: ``send(text)``), one class per backend of
``CHANNELS``; Telegram (``TelegramChannel``) is the only one. It only sends: devloop never
reads the messages the bot receives, so the phone is never a channel of commands. The bot's
token is a file of the dispatcher's user, mode 600, in its key_dir (``read_token``): never in
the overlay, in a log line, in an error or in a message.

No secret goes in a notice (tokens, keys, the content of the overlay), nor the details of a
vulnerability: Telegram is private with respect to the repository, not confidential.
"""

import fcntl
import hashlib
import json
import os
import re
import stat
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import ConfigError, NotifyPolicy, parse_notify
from .i18n import ENGLISH, Catalog

STATE_FILE = 'notify-state.json'
LOCK_FILE = 'notify.lock'
# The longest text sent: Telegram takes 4096 characters.
MAX_TEXT = 3500
# A key: letters, digits and a few separators, so that it is safe in a log line and in JSON.
KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,99}')
# The name of a project of the host, as ``ops/host/devloop-command`` takes it: the PROJECT of
# ``ops/lib.sh``, or ``_default``, the instance of an installation without PROJECT.
PROJECT = re.compile(r'_default|[a-z0-9]([a-z0-9-]{0,14}[a-z0-9])?')
# The form of a bot token of Telegram: its number, ':' and its secret.
TOKEN = re.compile(r'[0-9]{1,20}:[A-Za-z0-9_-]{20,100}')

# The outcomes of ``Notifier.notify``.
SENT = 'sent'
SKIPPED = 'skipped'
FAILED = 'failed'
OFF = 'off'

# The notices of devloop, in the project's ``language`` (#635); English for a language without
# translation.
TEXTS = Catalog(
    en={
        'stopped': ('`devloop run` stopped: {count} ticks in a row failed ({classes}); it starts '
                    'no other tick until the operator restarts it.'),
        'stopped_issue': ' The alert is on issue #{issue}.',
        'stopped_log': ' The detail is in the local log of the loop.',
        'refused': '`devloop {command}` did not start, or stopped: {reason}',
        'alert': 'New alert of devloop on issue #{issue}.',
        'service_failed': ('The devloop service of project {project} is in the failed state '
                           '(systemd): see its journal and the log of the loop.'),
        'sentinel_failed': ('The sentinel of the roles at rest of project {project} is in the '
                            'failed state (systemd): the roles at rest are not woken up. See '
                            'journalctl -u devloop-role-sentinel@{project} and check-host.sh.'),
        'rest_hold': ('`devloop run` holds {actions}: the roles at rest ({roles}) have not woken '
                      'up within {seconds} seconds. No readiness check nor implementation starts '
                      'until they leave their rest: see the sessions of the roles, the sentinel '
                      'and the watchdog of the roles.'),
        'rest_unreadable': ('`devloop run` cannot read the rest of the roles ([run] rest_dir, '
                            '{error}): no readiness check nor implementation starts until it '
                            'can. See the ACL of devloop on the mailbox between the roles '
                            '(check-host.sh), or remove rest_dir from the overlay.'),
        'test': 'Test of the notices of devloop: the channel works.',
    },
    it={
        'stopped': ('`devloop run` si è fermato: {count} tick di fila sono falliti ({classes}); '
                    'non avvia altri tick finché l\'operatore non lo riavvia.'),
        'stopped_issue': ' L\'allarme è sull\'issue #{issue}.',
        'stopped_log': ' Il dettaglio è nel log locale del ciclo.',
        'refused': '`devloop {command}` non è partito, o si è fermato: {reason}',
        'alert': 'Nuovo allarme di devloop sull\'issue #{issue}.',
        'service_failed': ('Il servizio di devloop del progetto {project} è nello stato failed '
                           '(systemd): vedere il suo journal e il log del ciclo.'),
        'sentinel_failed': ('La sentinella dei ruoli a riposo del progetto {project} è nello '
                            'stato failed (systemd): i ruoli a riposo non si svegliano. Vedere '
                            'journalctl -u devloop-role-sentinel@{project} e check-host.sh.'),
        'rest_hold': ('`devloop run` trattiene {actions}: i ruoli a riposo ({roles}) non si sono '
                      'svegliati entro {seconds} secondi. Nessun controllo di prontezza né '
                      'implementazione parte finché non escono dal riposo: vedere le sessioni dei '
                      'ruoli, la sentinella e il watchdog dei ruoli.'),
        'rest_unreadable': ('`devloop run` non legge il riposo dei ruoli ([run] rest_dir, '
                            '{error}): nessun controllo di prontezza né implementazione parte '
                            'finché non lo legge. Vedere le ACL di devloop sulla casella fra i '
                            'ruoli (check-host.sh), o togliere rest_dir dall\'overlay.'),
        'test': 'Prova degli avvisi di devloop: il canale funziona.',
    })


class NotifyError(Exception):
    """A notice not sent: the message never holds the token, nor the answer of the backend."""


def read_token(path, uid=None):
    """The bot's token in ``path``: a regular file, not a link, of the user ``uid`` (default:
    this process's), readable by that user alone (no permission for the group or the others).
    ``NotifyError`` otherwise, or when it does not hold a token: never with its content."""
    uid = os.geteuid() if uid is None else uid
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise NotifyError(f'the token file cannot be opened ({exc.strerror})') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise NotifyError('the token file is not a regular file')
        if info.st_uid != uid:
            raise NotifyError("the token file is not of devloop's user")
        if info.st_mode & 0o077:
            raise NotifyError(f'the token file is {stat.S_IMODE(info.st_mode):o}: it must be '
                              '600, readable by its owner alone')
        with os.fdopen(fd, 'rb', closefd=False) as fh:
            raw = fh.read(512)
    finally:
        os.close(fd)
    token = raw.decode('ascii', errors='replace').strip()
    if not TOKEN.fullmatch(token):
        raise NotifyError('the token file does not hold the token of a bot')
    return token


class Channel:
    """The port of the notices: ``send(text)`` delivers ``text`` or raises ``NotifyError``. A
    backend never reads what the operator answers."""

    def send(self, text):
        raise NotImplementedError


class TelegramChannel(Channel):
    """The Bot API of Telegram: one ``sendMessage`` to ``chat_id``, outbound only."""

    API = 'https://api.telegram.org'
    TIMEOUT = 15

    def __init__(self, policy, opener=urllib.request.urlopen, token=read_token):
        self.chat_id = policy.chat_id
        self.token_file = policy.token_file
        self._open = opener
        self._token = token

    def send(self, text):
        token = self._token(self.token_file)
        data = urllib.parse.urlencode({'chat_id': self.chat_id, 'text': text,
                                       'disable_web_page_preview': 'true'}).encode()
        request = urllib.request.Request(f'{self.API}/bot{token}/sendMessage', data=data,
                                         method='POST')
        # Only the class or the status of an error: its text may hold the URL, and the URL the
        # token.
        try:
            with self._open(request, timeout=self.TIMEOUT) as response:
                body = response.read(65536)
        except urllib.error.HTTPError as exc:
            raise NotifyError(f'HTTP {exc.code}') from None
        except (OSError, ValueError) as exc:
            raise NotifyError(type(exc).__name__) from None
        try:
            ok = json.loads(body).get('ok') is True
        except (ValueError, AttributeError):
            ok = False
        if not ok:
            raise NotifyError('the API did not confirm the message')


CHANNELS = {'telegram': TelegramChannel}


def key_of(text):
    """The key of a notice without one: the hash of its text."""
    return 'message:' + hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


class Notifier:
    """The notices of a project: ``policy`` (``config.NotifyPolicy``), the state in
    ``state_dir`` (None: no deduplication, every notice is sent), ``repo`` named at the start of
    each text. ``log`` writes the lines; ``clock`` is the wall clock, shared by the processes;
    ``channels`` the backends, by name."""

    def __init__(self, policy, state_dir, *, repo='', log=print, clock=time.time,
                 channels=None):
        self.policy = policy or NotifyPolicy()
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.repo = repo
        self.log = log
        self.clock = clock
        self.channels = CHANNELS if channels is None else channels

    def enabled(self):
        return self.policy.enabled()

    def text(self, message):
        """``message`` as it is sent: the project first, at most ``MAX_TEXT`` characters."""
        text = (f'devloop {self.repo}: ' if self.repo else 'devloop: ') + message.strip()
        return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + '…'

    def notify(self, message, key=None, force=False):
        """Send ``message`` under ``key`` (default: ``key_of`` the message), unless the same
        key was attempted less than ``interval`` seconds ago; ``force`` (``--test``) sends it
        all the same. Returns ``SENT``, ``SKIPPED``, ``FAILED`` or ``OFF``; never raises."""
        try:
            return self._notify(message, key, force)
        except Exception as exc:  # a notice never stops its caller
            self.log(f'notify: not sent ({type(exc).__name__})')
            return FAILED

    def _notify(self, message, key, force):
        key = key or key_of(message)
        if not KEY.fullmatch(key):
            self.log('notify: not sent: the key is not valid')
            return FAILED
        if not self.enabled():
            self.log(f'notify: {key}: no channel is configured ([notify] of the overlay): '
                     'only this line')
            return OFF
        channel = self.channels.get(self.policy.backend)
        if channel is None:
            self.log(f'notify: {key}: unknown backend {self.policy.backend!r}: not sent')
            return FAILED
        if not self._attempt(key, force):
            return SKIPPED
        try:
            channel(self.policy).send(self.text(message))
        except NotifyError as exc:
            self.log(f'notify: {key} not sent on {self.policy.backend} ({exc}): no retry')
            return FAILED
        except Exception as exc:  # the class only: its text may hold what the backend read
            self.log(f'notify: {key} not sent on {self.policy.backend} '
                     f'({type(exc).__name__}): no retry')
            return FAILED
        self.log(f'notify: {key} sent on {self.policy.backend}')
        return SENT

    def _attempt(self, key, force):
        """Record the attempt of ``key`` now, under the lock; False when the same key was
        attempted within the interval and ``force`` is not set. A state that cannot be read or
        written lets the notice go: a notice lost is worse than one repeated."""
        if self.state_dir is None:
            return True
        try:
            with self._locked():
                path = self.state_dir / STATE_FILE
                try:
                    seen = json.loads(path.read_text(encoding='utf-8')).get('keys') or {}
                    if not isinstance(seen, dict):
                        seen = {}
                except FileNotFoundError:
                    seen = {}
                except (OSError, ValueError, AttributeError):
                    self.log(f'notify: {STATE_FILE} is not readable: started again')
                    seen = {}
                now = self.clock()
                last = seen.get(key)
                if (not force and isinstance(last, (int, float))
                        and 0 <= now - last < self.policy.interval):
                    self.log(f'notify: {key} already attempted {int(now - last)} seconds ago, '
                             f'within {self.policy.interval}: not sent again')
                    return False
                kept = {k: v for k, v in seen.items()
                        if isinstance(v, (int, float)) and 0 <= now - v < self.policy.interval}
                kept[key] = now
                tmp = path.with_name(f'.{STATE_FILE}-{os.getpid()}')
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                    json.dump({'keys': kept}, fh, sort_keys=True)
                    fh.write('\n')
                os.replace(tmp, path)
        except OSError as exc:
            self.log(f'notify: {STATE_FILE} not updated ({exc.strerror or type(exc).__name__}): '
                     'sent without the deduplication')
        return True

    def _locked(self):
        return _Lock(self.state_dir / LOCK_FILE)


class _Lock:
    def __init__(self, path):
        self.path = path
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                          0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        os.close(self.fd)


def from_config(cfg, state_dir, log=print, **kw):
    """The ``Notifier`` of a configuration already loaded."""
    return Notifier(cfg.notify, state_dir, repo=cfg.repo, log=log, **kw)


def from_overlay(path, log=print, **kw):
    """``(notifier, language)`` of the overlay ``path`` alone, its ``[notify]``, ``repo`` and
    ``language``, with the state beside it, in the state_dir: so that a configuration that does
    not load (the cause of a refused start) still has its channel. Without the overlay, or with
    a ``[notify]`` that is not valid, a notifier without a channel, and a line in the log."""
    if not path:
        return Notifier(None, None, log=log, **kw), ENGLISH
    try:
        with open(path, 'rb') as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return Notifier(None, None, log=log, **kw), ENGLISH
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log(f'notify: the overlay is not readable ({type(exc).__name__}): no channel')
        return Notifier(None, None, log=log, **kw), ENGLISH
    try:
        policy = parse_notify(data.get('notify', {}))
    except ConfigError as exc:
        log(f'notify: [notify] is not valid ({exc}): no channel')
        policy = NotifyPolicy()
    repo = data.get('repo') if isinstance(data.get('repo'), str) else ''
    language = data.get('language')
    if not isinstance(language, str):
        # The language is a fact of the project too, in the project file beside the overlay.
        try:
            with open(Path(path).parent / 'project.toml', 'rb') as fh:
                language = tomllib.load(fh).get('language')
        except (OSError, tomllib.TOMLDecodeError):
            language = None
    language = language if isinstance(language, str) else ENGLISH
    return Notifier(policy, Path(path).parent, repo=repo, log=log, **kw), language
