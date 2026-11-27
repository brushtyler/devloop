"""devloop: clone and fetch of the managed repository with the dispatcher's token (#716).

Every fetch of the workspace goes, when the dispatcher has an app, to the repository's HTTPS URL
with a token of that app limited to ``contents=read``, with the defences of the push: the token
only in the environment of the command, credential helpers, askpass and SSH off, only HTTPS, URL
rewrites refused. ``devloop clone DEST`` makes the first clone the same way, and ``devloop fetch
[--check]`` makes the fetch of the base branch, or only reaches it, for check-host.sh (#718).
"""

import base64
import functools
import http.server
import io
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import devloop.__main__ as cli
from devloop import apps, config
from devloop.github import GitHubError
from devloop.workspace import Workspace, WorkspaceError, clone
from helpers import ssdlc_project

URL = 'https://github.com/o/r.git'
SECRET = 'ghs_SECRET-TOKEN'
BASE = '+refs/heads/main:refs/remotes/origin/main'


def git(cwd, *args):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@x', *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


def basic(user_pass):
    return 'basic ' + base64.b64encode(user_pass.encode()).decode()


def config_pairs(env):
    return [(env[f'GIT_CONFIG_KEY_{i}'], env[f'GIT_CONFIG_VALUE_{i}'])
            for i in range(int(env['GIT_CONFIG_COUNT']))]


class FakeGit:
    """Records each git command with its environment; empty output, exit status 0."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw.get('env')))
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def fetches(self, command='fetch'):
        return [(argv, env) for argv, env in self.calls if argv[1] == command]


class FetchTests(unittest.TestCase):
    def workspace(self, run, fetch_auth):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Workspace(tmp.name, '.state', 'main', run=run, fetch_auth=fetch_auth)

    def test_every_fetch_uses_the_authenticated_environment(self):
        run = FakeGit()
        ws = self.workspace(run, lambda: {'url': URL, 'token': SECRET})
        steps = {'new_branch': lambda: ws.new_branch('issue-7', 'agent/issue-7'),
                 'existing_branch': lambda: ws.existing_branch('pr-8', 'agent/issue-8'),
                 'base_copy': lambda: ws.base_copy('preflight-9'),
                 'base_head': ws.base_head,
                 'commit_copy': lambda: ws.commit_copy('conclave-8', 'abc', branch='agent/b'),
                 'review_diff': lambda: ws.review_diff('agent/b'),
                 'resync_merges': lambda: ws.resync_merges('agent/b', 'h', 'a')}
        basic_app = base64.b64encode(f'x-access-token:{SECRET}'.encode()).decode()
        key = f'http.{URL}.extraHeader'
        for name, step in steps.items():
            with self.subTest(name), mock.patch.dict(
                    os.environ, {'GIT_CONFIG_PARAMETERS': "'http.extraheader'='x'"}):
                run.calls.clear()
                step()
                fetches = run.fetches()
                self.assertEqual(len(fetches), 1)
                argv, env = fetches[0]
                self.assertEqual(argv[:4], ['git', 'fetch', URL, BASE])
                self.assertTrue(all(ref.startswith('+refs/heads/') for ref in argv[3:]))
                # The token is never on the command line, nor in a command of git.
                for other, _ in run.calls:
                    self.assertNotIn(SECRET, ' '.join(other))
                    self.assertNotIn(basic_app, ' '.join(other))
                self.assertNotIn('GIT_CONFIG_PARAMETERS', env)
                self.assertEqual(config_pairs(env), [
                    ('credential.helper', ''), (key, ''),
                    (key, f'AUTHORIZATION: basic {basic_app}')])
                self.assertEqual((env['GIT_ALLOW_PROTOCOL'], env['GIT_ASKPASS'],
                                  env['SSH_ASKPASS'], env['GIT_SSH_COMMAND'],
                                  env['GIT_TERMINAL_PROMPT']),
                                 ('https', 'false', 'false', 'false', '0'))
                # The URL rewrites are read before the fetch.
                index = run.calls.index(fetches[0])
                self.assertEqual(run.calls[index - 1][0][1:4], ['config', '--null', '--get-regexp'])

    def test_without_the_dispatcher_app_the_fetch_is_as_before(self):
        for fetch_auth in (None, lambda: None):
            with self.subTest(fetch_auth=fetch_auth):
                run = FakeGit()
                self.workspace(run, fetch_auth).base_head()
                self.assertEqual(run.fetches(), [(['git', 'fetch', 'origin', 'main'], None)])

    def test_a_token_that_cannot_be_had_fails_the_fetch(self):
        run = FakeGit()

        def broken():
            raise GitHubError('POST /app/installations/1/access_tokens: HTTP 401: Bad creds')
        ws = self.workspace(run, broken)
        with self.assertRaisesRegex(WorkspaceError, 'git fetch: no token of the dispatcher app '
                                                    'to fetch main: .*HTTP 401'):
            ws.base_head()
        self.assertEqual(run.fetches(), [])

    def test_a_read_only_workspace_mints_no_token(self):
        run = FakeGit()
        minted = []
        ws = self.workspace(run, lambda: minted.append(1) or {'url': URL, 'token': SECRET})
        ws._read_only = {'GIT_OPTIONAL_LOCKS': '0'}
        ws.base_head()
        self.assertEqual((minted, run.fetches()), ([], []))

    def test_the_fetch_refuses_a_url_that_git_rewrites(self):
        for key in ('insteadOf', 'pushInsteadOf'):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                git(tmp, 'init', '-q')
                git(tmp, 'config', f'url.git@github.com:.{key}', 'https://github.com/')
                ws = Workspace(tmp, '.state', 'main',
                               fetch_auth=lambda: {'url': URL, 'token': SECRET})
                with self.assertRaisesRegex(WorkspaceError, 'rewrites .*refusing to fetch'):
                    ws.base_head()
                with self.assertRaisesRegex(WorkspaceError, 'rewrites .*refusing to fetch'):
                    ws.fetch_base(check=True)

    def test_fetch_base_fetches_or_with_check_only_reaches_the_base_branch(self):
        # #718: devloop fetch, for check-host.sh. --check writes nothing: ls-remote, no fetch,
        # with the same token and environment.
        run = FakeGit()
        ws = self.workspace(run, lambda: {'url': URL, 'token': SECRET})
        ws.fetch_base()
        self.assertEqual([argv for argv, _ in run.fetches()], [['git', 'fetch', URL, BASE]])
        self.assertEqual(run.fetches('ls-remote'), [])
        run.calls.clear()
        ws.fetch_base(check=True)
        self.assertEqual(run.fetches(), [])
        (argv, env), = run.fetches('ls-remote')
        self.assertEqual(argv, ['git', 'ls-remote', '--exit-code', URL, 'refs/heads/main'])
        self.assertNotIn(SECRET, ' '.join(argv))
        self.assertEqual((env['GIT_ALLOW_PROTOCOL'], env['GIT_TERMINAL_PROMPT']), ('https', '0'))
        self.assertEqual(config_pairs(env)[0], ('credential.helper', ''))
        # Without the dispatcher's app, origin with the ambient credentials, as the fetches.
        run = FakeGit()
        self.workspace(run, None).fetch_base(check=True)
        self.assertEqual(run.fetches('ls-remote'),
                         [(['git', 'ls-remote', '--exit-code', 'origin', 'refs/heads/main'], None)])

    def test_a_token_that_cannot_be_had_fails_the_check(self):
        run = FakeGit()

        def broken():
            raise GitHubError('HTTP 401: Bad creds')
        with self.assertRaisesRegex(WorkspaceError, 'git ls-remote: no token of the dispatcher '
                                                    'app to fetch main: HTTP 401'):
            self.workspace(run, broken).fetch_base(check=True)
        self.assertEqual(run.fetches('ls-remote'), [])


class Server:
    """A dumb HTTP git server of the bare repository ``root``: it answers 401 to a request
    without the Authorization header ``expected``, and records every header it got."""

    def __init__(self, test, root, expected):
        self.seen = []
        seen = self.seen

        class Handler(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                got = self.headers.get_all('Authorization') or []
                seen.extend(got)
                if got != [expected]:
                    self.send_response(401)
                    self.send_header('WWW-Authenticate', 'Basic realm="r"')
                    self.end_headers()
                    return
                super().do_GET()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(('127.0.0.1', 0),
                                        functools.partial(Handler, directory=str(root)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        test.addCleanup(server.server_close)
        test.addCleanup(server.shutdown)
        self.url = f'http://127.0.0.1:{server.server_port}/'


def http_allowed(argv, **kw):
    """The test server has no TLS: its clone and fetches use plain HTTP, nothing else changes."""
    if argv[1] in ('fetch', 'clone', 'ls-remote'):
        kw['env'] = dict(kw['env'], GIT_ALLOW_PROTOCOL='http')
    return subprocess.run(argv, **kw)


class RealGitTests(unittest.TestCase):
    """Real git against a local server: only the app token is sent, and it stays out of the
    clone's config and of the output."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        work = self.tmp / 'work'
        work.mkdir()
        git(work, 'init', '-q', '-b', 'main')
        git(work, 'commit', '-q', '--allow-empty', '-m', 'base')
        self.head = git(work, 'rev-parse', 'HEAD')
        self.bare = self.tmp / 'r.git'
        git(self.tmp, 'clone', '-q', '--bare', str(work), str(self.bare))
        git(self.bare, 'update-server-info')
        self.app = basic(f'x-access-token:{SECRET}')
        self.server = Server(self, self.tmp, self.app)
        self.url = self.server.url + 'r.git'

    def assert_no_secret(self, root):
        for path in (root / '.git').rglob('*'):
            if path.is_file():
                data = path.read_bytes()
                self.assertNotIn(SECRET.encode(), data, path)
                self.assertNotIn(self.app.split()[1].encode(), data, path)

    def test_clone_and_fetch_send_only_the_app_token(self):
        dest = self.tmp / 'clone'
        clone(self.url, SECRET, dest, run=http_allowed)
        self.assertEqual(git(dest, 'rev-parse', 'origin/main'), self.head)
        self.assertEqual(git(dest, 'config', 'remote.origin.url'), self.url)
        # Personal credentials in the clone's config and in an inherited git -c take no part.
        personal = f'AUTHORIZATION: {basic("me:PERSONAL")}'
        git(dest, 'config', f'http.{self.url}.extraHeader', personal)
        git(dest, 'config', f'credential.{self.url}.helper',
            '!f() { echo username=me; echo password=HELPER; }; f')
        work = self.tmp / 'work'
        git(work, 'commit', '-q', '--allow-empty', '-m', 'next')
        git(work, 'push', '-q', str(self.bare), 'main')
        git(self.bare, 'update-server-info')
        self.server.seen.clear()
        ws = Workspace(dest, '.state', 'main', run=http_allowed,
                       fetch_auth=lambda: {'url': self.url, 'token': SECRET})
        inherited = {'GIT_CONFIG_PARAMETERS': f"'http.{self.url}.extraheader'='{personal}'"}
        with mock.patch.dict(os.environ, inherited):
            self.assertEqual(ws.base_head(), git(work, 'rev-parse', 'HEAD'))
        self.assertTrue(self.server.seen)
        self.assertEqual(set(self.server.seen), {self.app})
        git(dest, 'config', '--unset', f'http.{self.url}.extraHeader')
        self.assert_no_secret(dest)

    def test_the_check_reaches_the_base_branch_with_the_app_token_only(self):
        # #718: devloop fetch --check, what check-host.sh --read-only runs: nothing written.
        dest = self.tmp / 'clone'
        clone(self.url, SECRET, dest, run=http_allowed)
        before = sorted(str(p) for p in (dest / '.git').rglob('*'))
        self.server.seen.clear()
        ws = Workspace(dest, '.state', 'main', run=http_allowed,
                       fetch_auth=lambda: {'url': self.url, 'token': SECRET})
        ws.fetch_base(check=True)
        self.assertEqual(set(self.server.seen), {self.app})
        self.assertEqual(sorted(str(p) for p in (dest / '.git').rglob('*')), before)
        ws = Workspace(dest, '.state', 'main', run=http_allowed,
                       fetch_auth=lambda: {'url': self.url, 'token': 'WRONG-TOKEN'})
        with self.assertRaises(WorkspaceError) as caught:
            ws.fetch_base(check=True)
        self.assertNotIn('WRONG-TOKEN', str(caught.exception))
        # A base branch that is not there fails the check.
        ws = Workspace(dest, '.state', 'other', run=http_allowed,
                       fetch_auth=lambda: {'url': self.url, 'token': SECRET})
        with self.assertRaises(WorkspaceError):
            ws.fetch_base(check=True)

    def test_a_relative_destination_is_resolved_from_the_current_directory(self):
        (self.tmp / 'parent').mkdir()
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        clone(self.url, SECRET, 'parent/clone', run=http_allowed)
        self.assertEqual(git(self.tmp / 'parent' / 'clone', 'rev-parse', 'origin/main'),
                         self.head)
        self.assertFalse((self.tmp / 'parent' / 'parent').exists())

    def test_a_refused_token_fails_without_showing_it(self):
        self.server.seen.clear()
        with self.assertRaises(WorkspaceError) as caught:
            clone(self.url, 'WRONG-TOKEN', self.tmp / 'clone', run=http_allowed)
        self.assertNotIn('WRONG-TOKEN', str(caught.exception))
        # One request: git did not retry with credentials from a helper or a prompt.
        self.assertEqual(self.server.seen, [basic('x-access-token:WRONG-TOKEN')])
        dest = self.tmp / 'clone'
        clone(self.url, SECRET, dest, run=http_allowed)
        ws = Workspace(dest, '.state', 'main', run=http_allowed,
                       fetch_auth=lambda: {'url': self.url, 'token': 'WRONG-TOKEN'})
        with self.assertRaises(WorkspaceError) as caught:
            ws.base_head()
        self.assertNotIn('WRONG-TOKEN', str(caught.exception))

    def test_clone_refuses_a_url_that_git_rewrites(self):
        home = self.tmp / 'home'
        home.mkdir()
        (home / '.gitconfig').write_text(f'[url "{self.url}-elsewhere"]\n'
                                         f'\tinsteadOf = {self.url}\n')
        with mock.patch.dict(os.environ, {'HOME': str(home), 'XDG_CONFIG_HOME': str(home)}), \
                self.assertRaisesRegex(WorkspaceError, 'rewrites .*refusing to clone'):
            clone(self.url, SECRET, self.tmp / 'clone', run=http_allowed)
        self.assertFalse((self.tmp / 'clone').exists())
        self.assertEqual(self.server.seen, [])


class FakeApp:
    def __init__(self, permissions):
        self.permissions = permissions

    def token(self, permissions=None, time_left=None):
        self.permissions.append(permissions)
        return SECRET


class IdentityTests(unittest.TestCase):
    def test_the_dispatcher_token_for_the_fetches_reads_contents_only(self):
        minted = []
        identities = apps.Identities('o/r', {'dispatcher': FakeApp(minted)})
        self.assertEqual(identities.fetch_auth(), {'url': URL, 'token': SECRET})
        self.assertEqual(minted, [{'contents': 'read'}])
        self.assertIsNone(apps.Identities('o/r', {'claude': FakeApp([])}).fetch_auth())


class CloneCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        # The overlay alone: no project file, which the whole configuration needs.
        overlay = config.read_toml(ssdlc_project.OVERLAY, 'local config')
        self.repo = overlay['repo']
        self.slug = overlay['apps']['dispatcher']['slug']
        key = self.tmp / f'{self.slug}.2026-01-01.private-key.pem'
        key.write_text('not a key')
        key.chmod(0o600)
        patcher = mock.patch.dict(os.environ, {'DEVLOOP_APP_KEY_DIR': str(self.tmp)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def args(self, local=ssdlc_project.OVERLAY):
        return SimpleNamespace(config=None, local_config=local and str(local), project_config=None,
                               dest=str(self.tmp / 'clone'))

    def test_clone_with_the_dispatcher_token(self):
        made, cloned = [], []

        def auth(app_id, slug, key_path, repo):
            made.append((slug, Path(key_path).parent, repo))
            return FakeApp(minted)
        minted = []
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.run_clone(self.args(), auth=auth,
                                 clone=lambda *a: cloned.append(a))
        self.assertEqual(code, 0)
        self.assertEqual(made, [(self.slug, self.tmp, self.repo)])
        self.assertEqual(minted, [{'contents': 'read'}])
        self.assertEqual(cloned, [(f'https://github.com/{self.repo}.git', SECRET,
                                   str(self.tmp / 'clone'))])
        self.assertNotIn(SECRET, out.getvalue())

    def test_without_the_dispatcher_app_or_its_token_it_stops(self):
        overlay = self.tmp / 'overlay.toml'
        table = config.read_toml(ssdlc_project.OVERLAY, 'local config')
        table['apps'] = {k: v for k, v in table['apps'].items() if k != 'dispatcher'}
        overlay.write_text(ssdlc_project.toml_text(table) + '\n')
        with self.assertRaisesRegex(SystemExit, 'no GitHub App of the dispatcher'):
            cli.run_clone(self.args(overlay), clone=lambda *a: self.fail('cloned'))
        table['repo'] = 'o/r/../x'
        overlay.write_text(ssdlc_project.toml_text(table) + '\n')
        with self.assertRaisesRegex(SystemExit, 'invalid config: repo: expected owner/name'):
            cli.run_clone(self.args(overlay), clone=lambda *a: self.fail('cloned'))
        with self.assertRaisesRegex(SystemExit, 'give the overlay with --local-config'):
            cli.run_clone(self.args(None), clone=lambda *a: self.fail('cloned'))

        def auth(*args):
            raise GitHubError('HTTP 401')
        with self.assertRaisesRegex(SystemExit, 'no token of the dispatcher app: HTTP 401'):
            cli.run_clone(self.args(), auth=auth, clone=lambda *a: self.fail('cloned'))

    def test_the_subcommand_is_wired(self):
        with mock.patch.object(cli, 'run_clone', return_value=0) as run_clone, \
                self.assertRaises(SystemExit) as caught:
            cli.main(['--local-config', str(ssdlc_project.OVERLAY), 'clone', 'dest'])
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(run_clone.call_args[0][0].dest, 'dest')


class FetchCommandTests(unittest.TestCase):
    """``devloop fetch [--check]`` (#718), with the configuration of the project."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.slug = 'o-devloop-dispatcher'
        key = self.tmp / f'{self.slug}.2026-01-01.private-key.pem'
        key.write_text('not a key')
        key.chmod(0o600)
        self.cfg = SimpleNamespace(
            repo='o/r', base_branch='main', state_dir='.state', app_key_dir=str(self.tmp),
            apps={'dispatcher': config.AppConfig(app_id=7, slug=self.slug)})
        for name, value in (('main_checkout', lambda: self.tmp),
                            ('load', lambda args, root: self.cfg),
                            ('state_path', lambda cfg, root: self.tmp / '.state')):
            patcher = mock.patch.object(cli, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('DEVLOOP_APP_KEY_DIR', None)

    def fetch(self, check, auth=None):
        minted, made, run = [], [], FakeGit()

        def app(app_id, slug, key_path, repo):
            made.append((app_id, slug, Path(key_path).parent, repo))
            return FakeApp(minted)
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.run_fetch(SimpleNamespace(check=check), auth=auth or app, run=run)
        return code, made, minted, run, out.getvalue()

    def test_fetch_and_check_with_the_dispatcher_token(self):
        code, made, minted, run, out = self.fetch(check=False)
        self.assertEqual(code, 0)
        self.assertEqual(made, [(7, self.slug, self.tmp, 'o/r')])
        self.assertEqual(minted, [{'contents': 'read'}])
        self.assertEqual([argv for argv, _ in run.fetches()], [['git', 'fetch', URL, BASE]])
        self.assertIn('fetch: main of o/r fetched into origin/main', out)
        self.assertNotIn(SECRET, out)
        code, _, minted, run, out = self.fetch(check=True)
        self.assertEqual(code, 0)
        self.assertEqual(minted, [{'contents': 'read'}])
        self.assertEqual(run.fetches(), [])
        self.assertEqual([argv for argv, _ in run.fetches('ls-remote')],
                         [['git', 'ls-remote', '--exit-code', URL, 'refs/heads/main']])
        self.assertIn('fetch: main of o/r reached', out)

    def test_without_the_dispatcher_app_or_its_token_it_stops(self):
        class Refused:
            def __init__(self, *args):
                pass

            def token(self, permissions=None):
                raise GitHubError('HTTP 401')
        with self.assertRaisesRegex(SystemExit, 'fetch: git ls-remote: no token of the '
                                                'dispatcher app .*HTTP 401'):
            self.fetch(check=True, auth=Refused)
        (self.tmp / f'{self.slug}.2026-01-01.private-key.pem').unlink()
        with self.assertRaisesRegex(SystemExit, 'fetch: no token of the dispatcher app'):
            self.fetch(check=True)
        self.cfg.apps = {}
        with self.assertRaisesRegex(SystemExit, 'no GitHub App of the dispatcher'):
            self.fetch(check=True)

    def test_the_subcommand_is_wired(self):
        for argv, check in ((['fetch'], False), (['fetch', '--check'], True)):
            with self.subTest(argv=argv), \
                    mock.patch.object(cli, 'run_fetch', return_value=0) as run_fetch, \
                    self.assertRaises(SystemExit) as caught:
                cli.main(argv)
            self.assertEqual(caught.exception.code, 0)
            self.assertEqual(run_fetch.call_args[0][0].check, check)


if __name__ == '__main__':
    unittest.main()
