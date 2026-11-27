"""``scripts/coordinator-token.py`` (#549): the coordinator mints the tokens of its role app.

As ``scripts/supervisor-token.py`` does for the supervisor: ``repo`` and ``[apps.coordinator]``
from the overlay of the state_dir given with ``--state-dir``, only the coordinator's key from
its own key_dir, only ``issues`` and ``pull_requests`` and the reads of its manifest
(``actions``, ``checks``, ``statuses``, ``metadata``), and the token only in ``GH_TOKEN``,
never printed. ``devloop token`` (``scripts/agent-token.py``) does not expose the app.
"""

import contextlib
import dataclasses
import importlib.util
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import devloop.__main__ as cli
from devloop import apps, token
from helpers import ssdlc_project
from test_devloop import CFG

ROOT = Path(__file__).resolve().parents[1]
SLUG = 'brushtyler-devloop-coordinator'
APP_ID = 7777777
SECRET = 'ghs_coordinator-token-never-printed'


def script():
    spec = importlib.util.spec_from_file_location('coordinator_token',
                                                  ROOT / 'scripts/coordinator-token.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CoordinatorTokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = script()

    def setUp(self):
        # The project's state_dir, with ssdlc's overlay (ADR 0006, R6) and the coordinator's app.
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state)
        overlay = ssdlc_project.OVERLAY.read_text(encoding='utf-8')
        (self.state / 'config.local.toml').write_text(
            overlay + f'\n[apps.coordinator]\napp_id = {APP_ID}\nslug = "{SLUG}"\n',
            encoding='utf-8')
        self.keys = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.keys)
        self.key = self.keys / f'{SLUG}.2026-11-22.private-key.pem'
        self.key.write_text('k')
        self.key.chmod(0o600)
        # A GH_REPO of the environment running the tests would change what the script refuses.
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop('GH_REPO', None)

    def test_permissions(self):
        state = self.script.STATE
        default = {'issues': 'write', 'pull_requests': 'write'}
        self.assertEqual(self.script.parse(['--', 'gh', 'x']), (state, default, ['gh', 'x']))
        self.assertEqual(self.script.parse(['--state-dir', '/srv/p', '--', 'gh', 'x']),
                         (Path('/srv/p'), default, ['gh', 'x']))
        # The reads of the manifest, for the diagnosis of the CI.
        for perm in ('issues=read', 'pull_requests=read', 'actions=read', 'checks=read',
                     'statuses=read', 'metadata=read'):
            name, level = perm.split('=')
            with self.subTest(perm=perm):
                self.assertEqual(self.script.parse(['--perm', perm, '--', 'gh', 'x']),
                                 (state, {name: level}, ['gh', 'x']))
        # #717: contents read, for the clone of a private repository, only when asked.
        self.assertEqual(self.script.parse(['--perm', 'contents=read', '--', 'gh', 'x']),
                         (state, {'contents': 'read'}, ['gh', 'x']))
        self.assertNotIn('contents', default)
        # The coordinator neither implements nor merges: nothing above its manifest.
        for perm in ('contents=write', 'actions=write', 'checks=write',
                     'statuses=write', 'administration=write', 'workflows=write',
                     'metadata=write', 'issues=admin'):
            with self.subTest(perm=perm), self.assertRaisesRegex(SystemExit, 'not allowed'):
                self.script.parse(['--perm', perm, '--', 'gh', 'x'])
        for argv in ([], ['--'], ['--help'], ['gh', 'x'], ['--perm', '--', 'gh'],
                     ['--other', '--', 'gh']):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.script.parse(argv)

    def test_a_refused_permission_mints_nothing(self):
        with mock.patch.object(self.script.AppAuth, 'token') as mint, \
                mock.patch.object(self.script.subprocess, 'run') as run, \
                mock.patch.dict(os.environ, {'DEVLOOP_APP_KEY_DIR': str(self.keys)}):
            with self.assertRaisesRegex(SystemExit, 'contents=write is not allowed'):
                self.script.main(['--state-dir', str(self.state), '--perm', 'contents=write',
                                  '--', 'gh', 'x'])
            mint.assert_not_called()
            run.assert_not_called()

    def test_the_allowed_permissions_are_those_of_the_manifest(self):
        manifest = json.loads((ROOT / 'ops/github/apps/coordinator.json').read_text())
        granted = manifest['default_permissions']
        for name, levels in self.script.ALLOWED.items():
            self.assertIn(granted[name], levels, name)
            self.assertLessEqual(levels, {'read', 'write'} if granted[name] == 'write'
                                 else {'read'}, name)
        self.assertEqual(set(self.script.ALLOWED), set(granted))
        # #717: contents read only, never write, never by default.
        self.assertEqual(self.script.ALLOWED['contents'], {'read'})
        self.assertNotIn('contents', self.script.DEFAULT)

    def test_only_the_coordinators_key_is_read_and_the_token_is_never_printed(self):
        # Another app's key beside it is never read.
        other = self.keys / 'brushtyler-devloop-supervisor.2026-11-22.private-key.pem'
        other.write_text('s')
        other.chmod(0o600)
        seen = {}

        def fake_run(command, env):
            seen.update(command=command, token=env['GH_TOKEN'],
                        leaked={k for k in ('GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN') if k in env})
            return SimpleNamespace(returncode=3)

        def mint(app, permissions):
            seen.update(app=(app.app_id, app.slug, app.key_path, app.repo),
                        permissions=permissions)
            return SECRET
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {'DEVLOOP_APP_KEY_DIR': str(self.keys), 'GH_TOKEN': 'op',
                                          'GITHUB_TOKEN': 'op2'}), \
                mock.patch.object(self.script.AppAuth, 'token', mint), \
                mock.patch.object(self.script.subprocess, 'run', fake_run), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.script.main(['--state-dir', str(self.state), '--perm', 'checks=read',
                                     '--', 'gh', 'run', 'view', '1'])
        self.assertEqual(code, 3)
        self.assertEqual(seen, {'command': ['gh', 'run', 'view', '1'], 'token': SECRET,
                                'leaked': set(),
                                'app': (APP_ID, SLUG, self.key, 'brushtyler/ssdlc'),
                                'permissions': {'checks': 'read'}})
        # The token reaches the command through GH_TOKEN only: nothing printed.
        self.assertEqual(out.getvalue() + err.getvalue(), '')
        self.assertNotRegex((ROOT / 'scripts/coordinator-token.py').read_text(),
                            r'\bprint\(|sys\.std(out|err)')

    def test_the_repository_is_the_projects(self):
        # ADR 0006, R6: repo and the app come from the overlay of the state_dir given.
        overlay = self.state / 'config.local.toml'
        overlay.write_text(overlay.read_text(encoding='utf-8').replace(
            'repo = "brushtyler/ssdlc"', 'repo = "brushtyler/widget"'), encoding='utf-8')
        cfg = self.script.load_config(self.state)
        self.assertEqual(cfg.repo, 'brushtyler/widget')
        self.assertEqual(cfg.apps[apps.COORDINATOR].slug, SLUG)

    def test_gh_acts_on_the_projects_repository_not_on_the_clones(self):
        # #571: with the source apart, the coordinator runs the script from its clone of the
        # source, whose origin is not the project's repository; gh would pick the origin.
        overlay = self.state / 'config.local.toml'
        overlay.write_text(overlay.read_text(encoding='utf-8').replace(
            'repo = "brushtyler/ssdlc"', 'repo = "acme/widget"'), encoding='utf-8')
        for given in (None, 'acme/widget', 'ACME/Widget', 'github.com/acme/widget'):
            seen = {}

            def fake_run(command, env):
                seen['repo'] = env.get('GH_REPO')
                return SimpleNamespace(returncode=0)
            environ = {'DEVLOOP_APP_KEY_DIR': str(self.keys)}
            if given:
                environ['GH_REPO'] = given
            with self.subTest(given=given), mock.patch.dict(os.environ, environ), \
                    mock.patch.object(self.script.AppAuth, 'token', return_value=SECRET), \
                    mock.patch.object(self.script.subprocess, 'run', fake_run):
                self.assertEqual(self.script.main(['--state-dir', str(self.state), '--',
                                                   'gh', 'issue', 'view', '1']), 0)
                self.assertEqual(seen, {'repo': 'acme/widget'})
        # A GH_REPO naming another repository, the source's for example, is refused before any
        # key is read: the token would not reach it.
        with mock.patch.dict(os.environ, {'DEVLOOP_APP_KEY_DIR': str(self.keys),
                                          'GH_REPO': 'brushtyler/devloop'}), \
                mock.patch.object(self.script, 'find_key') as find, \
                mock.patch.object(self.script.AppAuth, 'token') as mint, \
                mock.patch.object(self.script.subprocess, 'run') as run:
            with self.assertRaisesRegex(SystemExit, 'GH_REPO=brushtyler/devloop is not the '
                                        "project's repository acme/widget"):
                self.script.main(['--state-dir', str(self.state), '--', 'gh', 'x'])
            find.assert_not_called()
            mint.assert_not_called()
            run.assert_not_called()

    def test_without_an_overlay_it_refuses(self):
        with tempfile.TemporaryDirectory() as empty, \
                mock.patch.object(self.script.subprocess, 'run') as run:
            with self.assertRaisesRegex(SystemExit, 'no local overlay'):
                self.script.main(['--state-dir', empty, '--', 'gh', 'x'])
            run.assert_not_called()

    def test_no_key_in_an_agents_environment(self):
        # devloop's agents run with a key directory without keys: they cannot mint it.
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {'DEVLOOP_APP_KEY_DIR': tmp}), \
                mock.patch.object(self.script.subprocess, 'run') as run:
            with self.assertRaises(FileNotFoundError):
                self.script.main(['--state-dir', str(self.state), '--', 'gh', 'x'])
            run.assert_not_called()

    def test_the_key_comes_from_the_coordinators_key_dir_not_from_the_overlays(self):
        # The overlay's [apps] key_dir is devloop's (DEVLOOP_KEY_DIR; with PROJECT an absolute
        # path such as /home/devloop-widget/.ghapps): the coordinator's key is not there. With
        # DEVLOOP_APP_KEY_DIR on its own key_dir (COORDINATOR_KEY_DIR, as setup-coordinator.sh
        # prints it), the key is found; without it, the script looks in devloop's and fails.
        devloop_keys = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, devloop_keys)
        overlay = self.state / 'config.local.toml'
        text = overlay.read_text(encoding='utf-8')
        self.assertRegex(text, r'(?m)^key_dir = ')
        overlay.write_text(re.sub(r'(?m)^key_dir = .*$', f'key_dir = "{devloop_keys}"', text),
                           encoding='utf-8')
        self.assertEqual(self.script.load_config(self.state).app_key_dir, str(devloop_keys))
        env = {k: v for k, v in os.environ.items() if k != 'DEVLOOP_APP_KEY_DIR'}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(self.script.AppAuth, 'token') as mint, \
                mock.patch.object(self.script.subprocess, 'run') as run:
            with self.assertRaisesRegex(FileNotFoundError, re.escape(str(devloop_keys))):
                self.script.main(['--state-dir', str(self.state), '--', 'gh', 'x'])
            mint.assert_not_called()
            run.assert_not_called()
        seen = {}

        def fake_mint(app, permissions):
            seen['key'] = app.key_path
            return SECRET
        with mock.patch.dict(os.environ, {'DEVLOOP_APP_KEY_DIR': str(self.keys)}), \
                mock.patch.object(self.script.AppAuth, 'token', fake_mint), \
                mock.patch.object(self.script.subprocess, 'run',
                                  return_value=SimpleNamespace(returncode=0)):
            self.assertEqual(self.script.main(['--state-dir', str(self.state), '--', 'gh', 'x']),
                             0)
        self.assertEqual(seen, {'key': self.key})

    def test_without_the_app_it_refuses(self):
        cfg = dataclasses.replace(CFG, apps={k: v for k, v in CFG.apps.items()
                                             if k != apps.COORDINATOR})
        with mock.patch.object(self.script, 'load_config', return_value=cfg), \
                mock.patch.object(self.script.subprocess, 'run') as run:
            with self.assertRaisesRegex(SystemExit, r'no \[apps.coordinator\]'):
                self.script.main(['--', 'gh', 'x'])
            run.assert_not_called()

    def test_devloop_token_does_not_expose_it(self):
        # devloop token, which scripts/agent-token.py calls.
        with mock.patch.object(cli, 'load') as load, \
                mock.patch.object(token.subprocess, 'run') as run:
            with self.assertRaisesRegex(SystemExit, "unknown actor 'coordinator'"):
                cli.main(['token', 'coordinator', '--', 'gh', 'issue', 'comment', '76'])
            load.assert_not_called()
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
