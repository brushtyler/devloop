"""The host's details out of the texts published on GitHub (#583).

The repository is public, and a text published on GitHub stays readable in its revisions. The
output of the tests and of the commands that devloop quotes (a traceback of a ``host_tests``
escalation, the tail of a failed run, an agent's text) holds the paths of the host: the homes,
and with them the names of the roles' system users, the ``state_dir`` with its worktrees, the
key_dirs and the names of the key files. Before a text is published, ``Redactor.redact`` puts
the names of the parameters of ``ops/params.env`` in their place:

- every path of the map, longest first: the ``state_dir`` lies inside a home, and becomes
  ``$STATE_DIR``, never ``$DEVLOOP_HOME/...``. A path ends where a name does: ``/home/devloop``
  does not match ``/home/devloop-agent``. ``$HOME`` alone is never used: a traceback holds the
  homes of different users;
- the name of a role's user standing alone, as ``<DEVLOOP_USER>``: only a name that is not one
  of the documented defaults (``PUBLIC_USERS``, already in the public documentation, and
  common words of devloop's own texts), at least ``MIN_USER`` characters long, and not one of
  the GitHub logins of ``keep`` (the trusted users, whose names the records carry);
- the name of a key file (``<slug>.<date>.private-key.pem``) as the role of its app, never the
  slug nor the date: ``<chiave dell'app implementer>``.

The map comes from ops (``ops/host/setup-devloop.sh`` writes ``MAP_FILE`` in the ``state_dir``
from the resolved parameters, never reading a key) and devloop reads it at the start; the
runtime values of the dispatcher prevail for its ``state_dir`` and its key_dir, and its own
home and user and the agent user's fill what the map lacks. Without the map, those runtime
values alone are replaced.

``host_details`` is the last check of the role scripts that publish on their own
(``scripts/supervisor-token.py``, ``scripts/coordinator-token.py``): a body that still holds an
absolute path of the host or the name of a key file is refused, unless the caller passes
``ALLOW_OPTION``, whose every use is logged (``publication_check``).
"""

import json
import os
import pwd
import re
import sys
from pathlib import Path

# The map of the substitutions, in the state_dir, written by ops/host/setup-devloop.sh.
MAP_FILE = 'redactions.json'
# The names of the parameters (ops/params.env) a path or a user of the map may have.
NAME = re.compile(r'^[A-Z][A-Z0-9_]*$')
# The default names of the roles' users: public in the documentation, and words of devloop's
# own texts. Replacing them would rewrite every "devloop" of a comment.
PUBLIC_USERS = frozenset({'devloop', 'devloop-agent', 'supervisor', 'coordinator', 'root'})
MIN_USER = 3
# A key file GitHub generated for an app (``apps.find_key``).
KEY_FILE = re.compile(r'(?<![\w.-])([A-Za-z0-9][A-Za-z0-9-]*)\.\d{4}-\d{2}-\d{2}\.private-key'
                      r'\.pem\b')
# Where a path or a name stops: it does not go on with another character of a name.
_AFTER_PATH = r'(?![\w.-])'
_BEFORE = r'(?<![\w.-])'
# A user stands alone: not inside a name nor a path.
_BEFORE_USER = r'(?<![\w./-])'
_AFTER_USER = r'(?![\w/-])'

# The check of the role scripts (#583): an absolute path under one of these prefixes, with at
# least one name after it, or the name of a key file.
HOST_PREFIXES = ('/home/', '/etc/', '/var/lib/', '/usr/local/lib/', '/run/', '/root/')
_HOST_PATH = re.compile(r'(?<![\w.~/-])(?:' + '|'.join(re.escape(p) for p in HOST_PREFIXES)
                        + r')[\w.-]+')
_ANY_KEY_FILE = re.compile(r'[\w.-]*\.private-key\.pem\b')
ALLOW_OPTION = '--allow-host-details'


def _absolute(value):
    """``value`` as a path worth replacing: absolute, with at least two names (never ``/`` nor
    ``/home`` alone, which would rewrite every path of a text), without a trailing slash."""
    if not isinstance(value, str) or not value.startswith('/'):
        return None
    value = value.rstrip('/')
    if value.count('/') < 2 or not all(value.split('/')[1:]):
        return None
    return value


class Redactor:
    """The substitutions of one installation: ``paths``, pairs ``(name, path)``; ``users``,
    pairs ``(name, user)``; ``roles``, the slugs of the apps mapped to their roles, and
    ``prefix``, the ``APP_PREFIX`` of ops that tells the role of the other slugs; ``keep``, the
    logins never replaced as users."""

    def __init__(self, paths=(), users=(), roles=None, keep=(), prefix=''):
        self.prefix = prefix if isinstance(prefix, str) else ''
        seen = {}
        for name, value in paths:
            value = _absolute(value)
            if value and NAME.match(name or '') and value not in seen:
                seen[value] = name
        self.paths = sorted(seen.items(), key=lambda item: (-len(item[0]), item[0]))
        kept = {k.lower() for k in keep if k}
        named = {}
        for name, user in users:
            if (isinstance(user, str) and NAME.match(name or '') and len(user) >= MIN_USER
                    and user not in PUBLIC_USERS and user.lower() not in kept
                    and re.fullmatch(r'[a-z_][a-z0-9_-]*', user) and user not in named):
                named[user] = name
        self.users = sorted(named.items(), key=lambda item: (-len(item[0]), item[0]))
        self.roles = dict(roles or {})
        self._path_re = (re.compile(_BEFORE + '(' + '|'.join(re.escape(p) for p, _ in self.paths)
                                    + ')' + _AFTER_PATH) if self.paths else None)
        self._user_re = (re.compile(_BEFORE_USER + '('
                                    + '|'.join(re.escape(u) for u, _ in self.users)
                                    + ')' + _AFTER_USER) if self.users else None)

    def redact(self, text):
        """``text`` with the names of the parameters in place of the host's values: the paths
        first, longest first, then the key files, then the users standing alone. Anything else,
        None included, is returned as it is."""
        if not isinstance(text, str) or not text:
            return text
        if self._path_re is not None:
            names = dict(self.paths)
            text = self._path_re.sub(lambda m: '$' + names[m.group(1)], text)
        text = KEY_FILE.sub(self._key, text)
        if self._user_re is not None:
            users = dict(self.users)
            text = self._user_re.sub(lambda m: f'<{users[m.group(1)]}>', text)
        return text

    def _key(self, match):
        slug = match.group(1)
        role = self.roles.get(slug)
        if role is None and self.prefix and slug.startswith(self.prefix):
            role = slug[len(self.prefix):] or None
        return f"<chiave dell'app {role}>" if role else "<chiave di un'app>"

    @classmethod
    def from_map(cls, data, roles=None, keep=()):
        """The redactor of a map as ``MAP_FILE`` holds it: ``{"paths": {NAME: path},
        "users": {NAME: user}, "app_prefix": APP_PREFIX}``. Anything else in it is ignored."""
        data = data if isinstance(data, dict) else {}
        paths = data.get('paths') if isinstance(data.get('paths'), dict) else {}
        users = data.get('users') if isinstance(data.get('users'), dict) else {}
        return cls(paths.items(), users.items(), roles, keep, data.get('app_prefix', ''))


def read_map(path):
    """The map of ``path`` (``MAP_FILE``), or ``(None, reason)`` when it is missing or cannot be
    read: ``(data, '')`` otherwise. A link is not followed."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None, 'missing'
    except OSError as exc:
        return None, f'not readable: {exc.strerror or exc}'
    try:
        with os.fdopen(fd, 'rb') as fh:
            data = json.loads(fh.read().decode('utf-8'))
    except (OSError, ValueError) as exc:
        return None, f'not valid: {exc}'
    if not isinstance(data, dict):
        return None, 'not valid: not an object'
    return data, ''


def _home(user):
    try:
        return pwd.getpwnam(user).pw_dir
    except (KeyError, TypeError):
        return None


def runtime(cfg, state, log=None, uid=None):
    """The redactor of the dispatcher (#583): the map of ``state`` (``MAP_FILE``), with the
    runtime values prevailing for its ``state_dir`` (``state``, as given and resolved) and its
    key_dir (``$DEVLOOP_APP_KEY_DIR``, else ``apps.key_dir`` of ``cfg``), and its own user and
    home, the agent user's (``[agent_user] name``) and that user's home filling what the map
    lacks. ``log`` takes a line about a map missing or not valid."""
    data, why = read_map(Path(state) / MAP_FILE)
    if data is None:
        data = {}
        if log is not None:
            log(f'redaction: {MAP_FILE} in state_dir {why}: only the runtime values of the '
                'dispatcher are replaced in the published texts (ops/host/setup-devloop.sh '
                'writes it)')
    paths = dict(data['paths']) if isinstance(data.get('paths'), dict) else {}
    users = dict(data['users']) if isinstance(data.get('users'), dict) else {}
    # The runtime values prevail over the map's, as given and resolved (a link on the way).
    key_dir = os.environ.get('DEVLOOP_APP_KEY_DIR') or getattr(cfg, 'app_key_dir', '') or ''
    pairs = []
    for name, value in (('STATE_DIR', state), ('DEVLOOP_KEY_DIR', key_dir)):
        if value:
            path = Path(os.path.abspath(Path(value).expanduser()))
            pairs += [(name, str(path)), (name, str(path.resolve()))]
            paths.pop(name, None)
    try:
        me = pwd.getpwuid(os.getuid() if uid is None else uid)
    except KeyError:
        me = None
    if me is not None:
        users.setdefault('DEVLOOP_USER', me.pw_name)
        paths.setdefault('DEVLOOP_HOME', me.pw_dir)
    agent = getattr(getattr(cfg, 'agent_user', None), 'name', '') or ''
    if agent:
        users.setdefault('AGENT_USER', agent)
        if _home(agent):
            paths.setdefault('AGENT_HOME', _home(agent))
    roles = {app.slug: actor for actor, app in (getattr(cfg, 'apps', None) or {}).items()}
    return Redactor(pairs + list(paths.items()), users.items(), roles,
                    keep=getattr(cfg, 'trusted_users', ()), prefix=data.get('app_prefix', ''))


# The client that redacts ------------------------------------------------------
# The writes of the ports, with the arguments that carry text: what reaches GitHub.
WRITES = {'comment': ('body',), 'alert': ('body',), 'edit_comment': ('body',),
          'submit_review': ('body',), 'create_pr': ('title', 'body'),
          'edit_issue_body': ('body',)}


class RedactingClient:
    """``client`` with the text of its writes (``WRITES``) passed through ``redactor`` before
    they reach it: before the outbox, which records and reconciles the very body published.
    Everything else reaches ``client`` as it is, attributes set included."""

    def __init__(self, client, redactor):
        object.__setattr__(self, '_client', client)
        object.__setattr__(self, '_redactor', redactor)

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if name not in WRITES or not callable(attr):
            return attr
        fields = WRITES[name]

        def write(*args, **kwargs):
            kwargs = {k: (self._redactor.redact(v) if k in fields else v)
                      for k, v in kwargs.items()}
            # ``comment(number, body)``, ``edit_comment(comment_id, body)``, ...: the body is
            # the second positional argument when it is given so.
            if 'body' in fields and len(args) >= 2:
                args = (args[0], self._redactor.redact(args[1]), *args[2:])
            return attr(*args, **kwargs)
        return write

    def __setattr__(self, name, value):
        setattr(self._client, name, value)  # ``time_left``, set by ``devloop watch``


# The check of the role scripts ------------------------------------------------
def host_details(text):
    """The host's details still in ``text``: each absolute path under ``HOST_PREFIXES`` and
    each name of a key file, once, in order. Empty when there is none."""
    found = []
    for match in [*_HOST_PATH.finditer(text or ''), *_ANY_KEY_FILE.finditer(text or '')]:
        if match.group(0) not in found:
            found.append(match.group(0))
    return found


def _bodies(command, stdin):
    """The texts ``command`` (a ``gh`` command line) publishes: ``--body``/``-b`` and the
    file of ``--body-file``/``-F`` (``-`` reads ``stdin``, a callable returning it), and the
    file of ``--input`` of ``gh api``. ``-F`` of ``gh api`` is a field, checked as a text."""
    api = len(command) > 1 and Path(command[0]).name == 'gh' and command[1] == 'api'
    texts, files = [], []
    i = 0
    while i < len(command):
        arg = command[i]
        value = command[i + 1] if i + 1 < len(command) else None
        for opt, kind in (('--body', 'text'), ('--body-file', 'file'), ('--input', 'file'),
                          ('--field', 'text'), ('--raw-field', 'text')):
            if arg.startswith(opt + '='):
                (texts if kind == 'text' else files).append(arg[len(opt) + 1:])
                break
            if arg == opt and value is not None:
                (texts if kind == 'text' else files).append(value)
                i += 1
                break
        else:
            if (arg == '-b' or (arg == '-f' and api)) and value is not None:
                texts.append(value)
                i += 1
            elif arg == '-F' and value is not None:
                (texts if api else files).append(value)
                i += 1
        i += 1
    out = list(texts)
    for name in files:
        if name == '-':
            out.append(stdin())
            continue
        try:
            out.append(Path(name).read_text(encoding='utf-8', errors='replace'))
        except OSError:
            continue  # gh says it: nothing published
    return out


def allow_option(argv):
    """``(allow, argv)``: whether the options of ``argv``, before ``--``, hold
    ``ALLOW_OPTION``, and ``argv`` without it."""
    split = argv.index('--') if '--' in argv else len(argv)
    options = argv[:split]
    return ALLOW_OPTION in options, [a for a in options if a != ALLOW_OPTION] + argv[split:]


def publication_check(script, command, allow, stdin=None, err=None, log=None):
    """The last check of a role script (#583) before ``command`` runs: ``SystemExit`` when a
    text it publishes still holds a detail of the host (``host_details``), unless ``allow``
    (``ALLOW_OPTION``), whose use is written on ``err`` (default: standard error) and in the
    system log (``log``, default ``syslog``), with the command and what was found. Returns the
    text read from standard input, when the command reads its body there, to pass on to it;
    otherwise None."""
    read = {}

    def from_stdin():
        if 'text' not in read:
            read['text'] = (stdin or sys.stdin.read)()
        return read['text']
    found = []
    for text in _bodies(command, from_stdin):
        found += [d for d in host_details(text) if d not in found]
    if found and not allow:
        raise SystemExit(f'{script}: the text to publish still holds details of the host '
                         f'({len(found)}: absolute paths or key files): put the names of the '
                         'parameters in their place ($STATE_DIR, $DEVLOOP_KEY_DIR, '
                         '$AGENT_HOME, ...; AGENTS.md, "Dettagli dell\'host nei testi '
                         f'pubblici"), or pass {ALLOW_OPTION} to publish it anyway (logged)')
    if found:
        line = (f'{script}: {ALLOW_OPTION}: published with {len(found)} details of the host, '
                f'command {Path(command[0]).name} {" ".join(command[1:3])}')
        (err or sys.stderr).write(line + '\n')
        (log or _syslog)(line)
    return read.get('text')


def _syslog(line):
    try:
        import syslog
        syslog.openlog('devloop-publication', syslog.LOG_PID, syslog.LOG_AUTH)
        syslog.syslog(syslog.LOG_WARNING, line)
    except (ImportError, OSError):
        pass
