"""GitHub App identities: one app per agent, one for the dispatcher and an optional one for merges.

Each actor (``dispatcher``, ``merger`` or an agent key) may have a GitHub App. Its installation tokens are
minted per action with only the permissions that action needs, so a reviewer never holds a
token that can push. An actor without an app falls back to the ambient ``gh`` login, as before.

Tokens stay in this process: they reach ``gh`` and ``git`` through the environment of a single
command and are never given to the agent CLIs. Only the standard library and the ``openssl``
binary are used (RS256 is not in the standard library).
"""

import base64
import calendar
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .github import GitHub, GitHubError
from .ports import Backend

API = 'https://api.github.com'
DISPATCHER = 'dispatcher'
# The app that only merges (#201): never used for comments, labels or pushes, and not an agent.
MERGER = 'merger'

# Permissions of the token minted for each kind of action (metadata: read is always implied).
IMPLEMENT = {'contents': 'write', 'pull_requests': 'write'}
REVIEW = {'contents': 'read', 'pull_requests': 'write'}
PR_COMMENT = {'pull_requests': 'write'}
ISSUE_COMMENT = {'issues': 'write'}
MERGE = {'contents': 'write', 'pull_requests': 'write'}  # only the merger's token (#201)
RERUN = {'actions': 'write'}  # only the dispatcher's, to re-run failed CI jobs (#200)

_REFRESH_MARGIN = 300  # seconds of validity a cached token must still have
STEP_TIMEOUT = 30  # seconds, for each step of a token's mint (signature, HTTP request)


def step_timeout(time_left):
    """Seconds one step of a token's mint may take: ``STEP_TIMEOUT``, or less when ``time_left``
    (a callable returning the seconds left, None: no limit) says so. No step starts once the
    time is spent (#197: ``devloop watch --timeout`` bounds the mint of its token too)."""
    left = time_left() if time_left is not None else None
    if left is None:
        return STEP_TIMEOUT
    if left <= 0:
        raise GitHubError('app token: no time left')
    return min(STEP_TIMEOUT, left)


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def sign_rs256(key_path, data, run=subprocess.run, timeout=STEP_TIMEOUT):
    try:
        done = run(['openssl', 'dgst', '-sha256', '-sign', str(key_path)], input=data,
                   capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitHubError(f'openssl could not sign the app JWT: {exc}') from exc
    if done.returncode != 0:
        raise GitHubError(f'openssl could not sign the app JWT: {done.stderr.decode()[:300]}')
    return done.stdout


def http_json(method, path, auth, body=None, timeout=STEP_TIMEOUT, api=API):
    """``method`` on ``api + path``, its JSON answer. ``timeout`` bounds the whole request: name
    resolution, connection, the response's headers and body, of an error too. ``urlopen``'s own
    timeout applies to each socket operation only, so slow headers and a slow body could add up
    beyond it (#197): the request runs in a worker thread, awaited at most ``timeout`` seconds."""
    req = urllib.request.Request(
        api + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization': auth, 'Accept': 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'devloop'})
    outcome = {}

    def fetch():
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                outcome['raw'] = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                outcome['detail'] = exc.read().decode(errors='replace')[:300]
            except Exception:  # an error body that fails to arrive: the status is enough
                outcome['detail'] = ''
            outcome['error'] = exc
        except Exception as exc:  # re-raised by the caller, in its thread
            outcome['error'] = exc

    # A daemon thread: one still blocked after the timeout does not keep the process alive, and
    # its socket timeout ends it soon after.
    worker = threading.Thread(target=fetch, name='devloop-http', daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise GitHubError(f'{method} {path}: timed out after {timeout}s')
    exc = outcome.get('error')
    if isinstance(exc, urllib.error.HTTPError):
        raise GitHubError(f'{method} {path}: HTTP {exc.code}: {outcome["detail"]}') from exc
    if isinstance(exc, (urllib.error.URLError, OSError)):
        raise GitHubError(f'{method} {path}: {exc}') from exc
    if exc is not None:
        raise exc
    raw = outcome['raw']
    return json.loads(raw) if raw else None


def find_key(key_dir, slug):
    """The newest private key GitHub generated for ``slug`` (``<slug>.<date>.private-key.pem``).

    The key must not be readable by group or others."""
    keys = sorted(Path(key_dir).expanduser().glob(f'{slug}.*.private-key.pem'))
    if not keys:
        raise FileNotFoundError(f'no private key for {slug} in {key_dir}')
    key = keys[-1]
    if key.stat().st_mode & 0o077:
        raise PermissionError(f'{key} is readable by group or others (chmod 600)')
    return key


class AppAuth:
    """One GitHub App installed on the repository."""

    def __init__(self, app_id, slug, key_path, repo, *, http=http_json, sign=sign_rs256,
                 clock=time.time):
        self.app_id = app_id
        self.slug = slug
        self.key_path = key_path
        self.repo = repo
        self._http = http
        self._sign = sign
        self._clock = clock
        self._installation = None
        self._user_id = None
        self._tokens = {}
        # Parallel actions (#176) ask for tokens from several threads: one mint per need.
        self._lock = threading.RLock()

    @property
    def login(self):
        return f'{self.slug}[bot]'

    def jwt(self, time_left=None):
        now = int(self._clock())
        head = _b64(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode())
        claims = _b64(json.dumps({'iat': now - 60, 'exp': now + 540, 'iss': str(self.app_id)}).encode())
        signing_input = f'{head}.{claims}'
        signature = self._sign(self.key_path, signing_input.encode(),
                               timeout=step_timeout(time_left))
        return f'{signing_input}.{_b64(signature)}'

    def installation_id(self, time_left=None):
        with self._lock:
            if self._installation is None:
                auth = f'Bearer {self.jwt(time_left)}'
                found = self._http('GET', f'/repos/{self.repo}/installation', auth,
                                   timeout=step_timeout(time_left))
                if found['app_id'] != self.app_id:
                    raise GitHubError(f'installation {found["id"]} belongs to app '
                                      f'{found["app_id"]}, not {self.app_id}')
                self._installation = found['id']
            return self._installation

    def token(self, permissions=None, time_left=None):
        """An installation token for this repository only, limited to ``permissions`` (all the
        installation's permissions when None). Reused while it has a few minutes left.
        ``time_left`` (see ``step_timeout``) bounds each step of a new mint."""
        with self._lock:
            return self._token(permissions, time_left)

    def _token(self, permissions, time_left):
        key = tuple(sorted((permissions or {}).items()))
        cached = self._tokens.get(key)
        if cached and cached[1] - self._clock() > _REFRESH_MARGIN:
            return cached[0]
        body = {'repositories': [self.repo.split('/', 1)[1]]}
        if permissions:
            body['permissions'] = dict(permissions)
        installation = self.installation_id(time_left)
        auth = f'Bearer {self.jwt(time_left)}'
        minted = self._http('POST', f'/app/installations/{installation}/access_tokens', auth,
                            body, timeout=step_timeout(time_left))
        expires = calendar.timegm(time.strptime(minted['expires_at'], '%Y-%m-%dT%H:%M:%SZ'))
        self._tokens[key] = (minted['token'], expires)
        return minted['token']

    def user_id(self, token):
        """The id of the app's bot user, for its noreply commit address (read with ``token``)."""
        with self._lock:
            if self._user_id is None:
                user = self._http('GET', f'/users/{self.login}', f'token {token}')
                self._user_id = user['id']
            return self._user_id

    def describe(self):
        """What GitHub says about the app and its installation, for ``devloop apps``."""
        app = self._http('GET', '/app', f'Bearer {self.jwt()}')
        if app['slug'] != self.slug:
            raise GitHubError(f'app {self.app_id} is {app["slug"]!r}, configured as {self.slug!r}')
        inst = self._http('GET', f'/app/installations/{self.installation_id()}', f'Bearer {self.jwt()}')
        return {'login': self.login, 'installation': inst['id'],
                'repository_selection': inst['repository_selection'],
                'permissions': inst['permissions']}


class Identities:
    """Who acts on GitHub for each actor, with an app or with the ambient ``gh`` login."""

    def __init__(self, repo, apps, run=subprocess.run):
        self.repo = repo
        self.apps = dict(apps)
        self._run = run
        self._ambient = GitHub(repo, run=run)

    @classmethod
    def from_config(cls, cfg, run=subprocess.run):
        key_dir = os.environ.get('DEVLOOP_APP_KEY_DIR') or cfg.app_key_dir
        apps = {actor: AppAuth(app.app_id, app.slug, find_key(key_dir, app.slug), cfg.repo)
                for actor, app in cfg.apps.items()}
        return cls(cfg.repo, apps, run=run)

    # The outbox of the publications (#385, ``outbox.Outbox``), set by devloop when
    # ``publication_outbox`` is on: the clients write their comments, native reviews and pull
    # requests through it.
    outbox = None

    def github(self, actor, permissions=None) -> Backend:
        """The client ``actor`` acts with: the ports, on GitHub, holding only ``permissions``;
        with an outbox, its ``comment``, ``edit_comment``, ``submit_review`` and ``create_pr``
        pass through it (#385)."""
        client = self.direct(actor, permissions)
        if self.outbox is None:
            return client
        return self.outbox.client(client, actor, permissions)

    def direct(self, actor, permissions=None) -> Backend:
        """The client of ``actor`` as ``github`` returns it, without the outbox."""
        app = self.apps.get(actor)
        if app is None:
            return self._ambient
        # The client's ``time_left`` (set by ``devloop watch``) bounds the mint of its token too.
        client = GitHub(self.repo, run=self._run,
                        token=lambda: app.token(permissions, time_left=client.time_left))
        return client

    def login(self, actor):
        """The app's bot login, or None when the actor uses the ambient login."""
        app = self.apps.get(actor)
        return app.login if app else None

    def agent_logins(self):
        return {app.login for actor, app in self.apps.items() if actor not in (DISPATCHER, MERGER)}

    def merger(self) -> Backend:
        """The client merges run with (#201): the ``merger`` app, with a token limited to
        ``MERGE`` and minted when the merge runs, or the ambient ``gh`` login without it."""
        return self.github(MERGER, MERGE)

    def git_auth(self, actor, permissions):
        """Commit identity and push credentials for ``actor``, or None for the ambient ones."""
        app = self.apps.get(actor)
        if app is None:
            return None
        token = app.token(permissions)
        return {'name': app.login, 'email': f'{app.user_id(token)}+{app.login}@users.noreply.github.com',
                'url': f'https://github.com/{self.repo}.git', 'token': token}
