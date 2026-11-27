"""The clone of a role of a private managed repository with the token of the role's app (#717).

``scripts/role-git.py``, run by ``scripts/supervisor-token.py`` or
``scripts/coordinator-token.py`` with ``--perm contents=read``, clones, fetches or checks the
role's clone with the token of ``GH_TOKEN`` only (``workspace.clone``, ``fetch_clone``,
``reach``), with the defences of the fetches of devloop (#716).
"""

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devloop.workspace import WorkspaceError, clone, fetch_clone, reach
from test_devloop_fetch_auth import SECRET, Server, basic, git

ROOT = Path(__file__).resolve().parents[1]


def load_script():
    spec = importlib.util.spec_from_file_location('role_git_717', ROOT / 'scripts/role-git.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def http_allowed(argv, **kw):
    """The test server has no TLS: its git commands use plain HTTP, nothing else changes."""
    if argv[1] in ('fetch', 'clone', 'ls-remote'):
        kw['env'] = dict(kw['env'], GIT_ALLOW_PROTOCOL='http')
    return subprocess.run(argv, **kw)


class RealGitTests(unittest.TestCase):
    """Real git against a local server: only the app token is sent, the clone's own
    credentials take no part, and the token stays out of the clone and of the errors."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.work = self.tmp / 'work'
        self.work.mkdir()
        git(self.work, 'init', '-q', '-b', 'main')
        git(self.work, 'commit', '-q', '--allow-empty', '-m', 'base')
        self.bare = self.tmp / 'r.git'
        git(self.tmp, 'clone', '-q', '--bare', str(self.work), str(self.bare))
        git(self.bare, 'update-server-info')
        self.app = basic(f'x-access-token:{SECRET}')
        self.server = Server(self, self.tmp, self.app)
        self.url = self.server.url + 'r.git'
        self.dest = self.tmp / 'clone'
        clone(self.url, SECRET, self.dest, run=http_allowed)

    def advance(self):
        git(self.work, 'commit', '-q', '--allow-empty', '-m', 'next')
        git(self.work, 'push', '-q', str(self.bare), 'main')
        git(self.bare, 'update-server-info')
        return git(self.work, 'rev-parse', 'HEAD')

    def test_fetch_updates_the_remote_branches_with_the_app_token_only(self):
        head = self.advance()
        # Personal credentials in the clone's config and in an inherited git -c take no part.
        personal = f'AUTHORIZATION: {basic("me:PERSONAL")}'
        git(self.dest, 'config', f'http.{self.url}.extraHeader', personal)
        git(self.dest, 'config', f'credential.{self.url}.helper',
            '!f() { echo username=me; echo password=HELPER; }; f')
        self.server.seen.clear()
        inherited = {'GIT_CONFIG_PARAMETERS': f"'http.{self.url}.extraheader'='{personal}'"}
        with mock.patch.dict(os.environ, inherited):
            fetch_clone(self.url, SECRET, self.dest, run=http_allowed)
        self.assertEqual(git(self.dest, 'rev-parse', 'origin/main'), head)
        self.assertTrue(self.server.seen)
        self.assertEqual(set(self.server.seen), {self.app})
        git(self.dest, 'config', '--unset', f'http.{self.url}.extraHeader')
        for path in (self.dest / '.git').rglob('*'):
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes(), path)

    def test_check_reads_without_writing_in_the_clone(self):
        self.advance()
        before = git(self.dest, 'rev-parse', 'origin/main')
        self.server.seen.clear()
        reach(self.url, SECRET, self.dest, run=http_allowed)
        self.assertEqual(set(self.server.seen), {self.app})
        self.assertEqual(git(self.dest, 'rev-parse', 'origin/main'), before)
        self.assertFalse((self.dest / '.git/FETCH_HEAD').exists())

    def test_a_refused_token_fails_without_showing_it(self):
        for action in (fetch_clone, reach):
            with self.subTest(action=action.__name__):
                self.server.seen.clear()
                with self.assertRaises(WorkspaceError) as caught:
                    action(self.url, 'WRONG-TOKEN', self.dest, run=http_allowed)
                self.assertNotIn('WRONG-TOKEN', str(caught.exception))
                # One request: no retry with credentials from a helper or a prompt.
                self.assertEqual(self.server.seen, [basic('x-access-token:WRONG-TOKEN')])

    def test_a_rewrite_in_the_clone_is_refused(self):
        git(self.dest, 'config', f'url.{self.url}-elsewhere.insteadOf', self.url)
        self.server.seen.clear()
        for action, what in ((fetch_clone, 'fetch'), (reach, 'ls-remote')):
            with self.subTest(what=what), \
                    self.assertRaisesRegex(WorkspaceError, f'rewrites .*refusing to {what}'):
                action(self.url, SECRET, self.dest, run=http_allowed)
        self.assertEqual(self.server.seen, [])


class ScriptTests(unittest.TestCase):
    """``scripts/role-git.py``: the repository of GH_REPO and the token of GH_TOKEN, as the
    token scripts set them."""

    def setUp(self):
        self.script = load_script()
        self.calls = []
        self.actions = {name: (lambda name: lambda *a: self.calls.append((name, *a)))(name)
                        for name in self.script.ACTIONS}
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)

    def test_each_action_gets_the_url_of_the_project_and_the_token(self):
        os.environ.update(GH_TOKEN=SECRET, GH_REPO='acme/widget')
        for action in ('clone', 'fetch', 'check'):
            with self.subTest(action=action):
                self.calls.clear()
                with mock.patch('builtins.print') as shown:
                    self.assertEqual(self.script.main([action, '/srv/c'], self.actions), 0)
                self.assertEqual(self.calls, [(action, 'https://github.com/acme/widget.git',
                                               SECRET, '/srv/c')])
                self.assertNotIn(SECRET, str(shown.call_args_list))
        os.environ['GH_REPO'] = 'github.com/acme/widget'
        self.calls.clear()
        with mock.patch('builtins.print'):
            self.script.main(['fetch', '/srv/c'], self.actions)
        self.assertEqual(self.calls[0][1], 'https://github.com/acme/widget.git')

    def test_without_the_token_script_it_stops(self):
        for env in ({}, {'GH_TOKEN': SECRET}, {'GH_REPO': 'acme/widget'},
                    {'GH_TOKEN': SECRET, 'GH_REPO': 'acme/widget/../x'}):
            with self.subTest(env=env):
                for name in ('GH_TOKEN', 'GH_REPO'):
                    os.environ.pop(name, None)
                os.environ.update(env)
                with self.assertRaisesRegex(SystemExit, '--perm contents=read'):
                    self.script.main(['fetch', '/srv/c'], self.actions)
        self.assertEqual(self.calls, [])

    def test_usage(self):
        os.environ.update(GH_TOKEN=SECRET, GH_REPO='acme/widget')
        for argv in ([], ['fetch'], ['push', '/srv/c'], ['fetch', '/srv/c', 'x']):
            with self.subTest(argv=argv), self.assertRaisesRegex(SystemExit, 'Usage'):
                self.script.main(argv, self.actions)
        self.assertEqual(self.calls, [])

    def test_a_failure_of_git_is_a_message_without_the_token(self):
        os.environ.update(GH_TOKEN=SECRET, GH_REPO='acme/widget')

        def failing(*args):
            raise WorkspaceError('git fetch https://github.com/acme/widget.git: denied')
        with self.assertRaises(SystemExit) as caught:
            self.script.main(['fetch', '/srv/c'], {'fetch': failing})
        self.assertEqual(str(caught.exception),
                         'role-git: git fetch https://github.com/acme/widget.git: denied')

    def test_the_actions_are_those_of_the_workspace(self):
        self.assertEqual(self.script.ACTIONS,
                         {'clone': clone, 'fetch': fetch_clone, 'check': reach})


if __name__ == '__main__':
    unittest.main()
