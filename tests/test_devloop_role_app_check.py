"""``--check`` of ``scripts/supervisor-token.py`` and ``scripts/coordinator-token.py`` (#726).

Run as the role's user with its key, the check reads the permissions of the installation of the
role app on the project's repository (``GET /repos/{repo}/installation`` with the app's JWT) and
compares them with the role's manifest, ``ops/github/apps/<role>.json``: a permission missing or
not accepted is an error, one more a warning; ``contents: read`` (#717) missing is a warning for
a public repository, an error for a private one. It mints no installation token, prints none and
runs no command.
"""

import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import doc_contracts as docs
from devloop import apps
from devloop.github import GitHubError
from helpers import ssdlc_project

ROOT = Path(__file__).resolve().parents[1]
APP_IDS = {'supervisor': 5824243, 'coordinator': 7777777}


def script(role):
    spec = importlib.util.spec_from_file_location(f'{role}_token_check',
                                                  ROOT / 'scripts' / f'{role}-token.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifest(role):
    path = ROOT / 'ops/github/apps' / f'{role}.json'
    return json.loads(path.read_text(encoding='utf-8'))['default_permissions']


class ComparePermissionsTests(unittest.TestCase):
    MANIFEST = {'contents': 'read', 'issues': 'write', 'metadata': 'read',
                'pull_requests': 'write'}

    def test_the_permissions_of_the_manifest_are_enough(self):
        self.assertEqual(apps.compare_permissions(self.MANIFEST, dict(self.MANIFEST), False),
                         ([], []))

    def test_a_permission_missing_or_lower_is_an_error(self):
        for installed in ({'contents': 'read', 'metadata': 'read', 'pull_requests': 'write'},
                          dict(self.MANIFEST, issues='read')):
            with self.subTest(installed=installed):
                errors, warnings = apps.compare_permissions(self.MANIFEST, installed, True)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn('issues=write of the manifest is', errors[0])
                self.assertIn('not accepted by the owner', errors[0])
                self.assertEqual(warnings, [])

    def test_contents_is_needed_only_for_a_private_repository(self):
        # #717: the role app reads contents only to clone a private managed repository.
        installed = {k: v for k, v in self.MANIFEST.items() if k != 'contents'}
        errors, warnings = apps.compare_permissions(self.MANIFEST, installed, True)
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn('contents=read of the manifest is missing', warnings[0])
        self.assertIn('the repository is public', warnings[0])
        errors, warnings = apps.compare_permissions(self.MANIFEST, installed, False)
        self.assertEqual(len(errors), 1)
        self.assertIn('contents=read of the manifest is missing', errors[0])
        self.assertEqual(warnings, [])

    def test_a_permission_more_is_a_warning(self):
        installed = dict(self.MANIFEST, contents='write', administration='read')
        errors, warnings = apps.compare_permissions(self.MANIFEST, installed, False)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [
            'administration=read on the installation is not in the manifest',
            'contents=write on the installation is more than the manifest (contents=read)'])


class InstallationTests(unittest.TestCase):
    def app(self, answers, calls):
        def http(method, path, auth, body=None, timeout=None):
            calls.append((method, path, auth))
            answer = answers[path]
            if isinstance(answer, Exception):
                raise answer
            return answer
        return apps.AppAuth(11, 'acme-supervisor', 'key.pem', 'acme/widget', http=http,
                            sign=lambda key, data, timeout=None: b'sig', clock=lambda: 1000)

    def test_the_installation_is_read_with_the_jwt_alone(self):
        calls = []
        app = self.app({'/repos/acme/widget/installation': {
            'id': 5, 'app_id': 11, 'permissions': {'issues': 'write'}}}, calls)
        self.assertEqual(app.installation_permissions(), (5, {'issues': 'write'}))
        self.assertEqual([(m, p) for m, p, _ in calls],
                         [('GET', '/repos/acme/widget/installation')])
        self.assertTrue(calls[0][2].startswith('Bearer '))
        # Another app's installation is refused.
        app = self.app({'/repos/acme/widget/installation': {'id': 5, 'app_id': 12}}, [])
        with self.assertRaisesRegex(GitHubError, 'belongs to app 12, not 11'):
            app.installation_permissions()

    def test_whether_the_repository_is_public_is_asked_without_credentials(self):
        for answer, public in (({'private': False}, True), ({'private': True}, False),
                               (GitHubError('HTTP 404'), False), (None, False)):
            calls = []

            def http(method, path, auth, body=None, timeout=None):
                calls.append((method, path, auth))
                if isinstance(answer, Exception):
                    raise answer
                return answer
            with self.subTest(answer=answer):
                self.assertIs(apps.repo_public('acme/widget', http), public)
                self.assertEqual(calls, [('GET', '/repos/acme/widget', None)])

    def test_an_anonymous_request_carries_no_authorization(self):
        seen = {}

        class Answer(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(req, timeout):
            seen['headers'] = dict(req.header_items())
            return Answer(b'{"private": false}')
        with mock.patch.object(apps.urllib.request, 'urlopen', urlopen):
            self.assertEqual(apps.http_json('GET', '/repos/acme/widget', None),
                             {'private': False})
            self.assertNotIn('Authorization', seen['headers'])
            apps.http_json('GET', '/app', 'Bearer x')
            self.assertEqual(seen['headers']['Authorization'], 'Bearer x')


class RoleScriptCheckTests(unittest.TestCase):
    """``--check`` of the two scripts, the same for each role."""

    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state)
        self.keys = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.keys)
        overlay = ssdlc_project.OVERLAY.read_text(encoding='utf-8')
        if '[apps.coordinator]' not in overlay:
            overlay += (f'\n[apps.coordinator]\napp_id = {APP_IDS["coordinator"]}\n'
                        'slug = "brushtyler-devloop-coordinator"\n')
        (self.state / 'config.local.toml').write_text(overlay, encoding='utf-8')
        for role in APP_IDS:
            key = self.keys / f'brushtyler-devloop-{role}.2026-11-27.private-key.pem'
            key.write_text('k')
            key.chmod(0o600)
        environ = mock.patch.dict(os.environ, {'DEVLOOP_APP_KEY_DIR': str(self.keys)})
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop('GH_REPO', None)

    def run_check(self, role, installed, public=True, argv=None):
        """main of the role's script with --check: its exit code and its output, with the
        installation having ``installed``; no token minted, no command run."""
        module = script(role)
        out = io.StringIO()
        seen = {}

        def installation(app):
            seen['app'] = (app.app_id, app.slug, app.repo)
            return 42, dict(installed)
        with mock.patch.object(module.AppAuth, 'installation_permissions', installation), \
                mock.patch.object(module.AppAuth, 'token') as mint, \
                mock.patch.object(apps, 'repo_public', return_value=public) as visibility, \
                mock.patch.object(module.subprocess, 'run') as run:
            code = module.main(argv or ['--state-dir', str(self.state), '--check'], out=out)
            mint.assert_not_called()
            run.assert_not_called()
        self.assertEqual(seen['app'], (APP_IDS[role], f'brushtyler-devloop-{role}',
                                       'brushtyler/ssdlc'))
        seen['visibility'] = visibility.call_count
        return code, out.getvalue(), seen

    def test_the_installation_with_the_manifest_passes(self):
        for role in APP_IDS:
            with self.subTest(role=role):
                code, out, seen = self.run_check(role, manifest(role))
                self.assertEqual(code, 0, out)
                self.assertIn(f'{role}: brushtyler-devloop-{role}[bot], installation 42 on '
                              'brushtyler/ssdlc: ', out)
                self.assertIn(f'{role}: manifest ops/github/apps/{role}.json: ', out)
                self.assertTrue(out.endswith(f'{role}: the installation has the permissions '
                                             'the role needs\n'), out)
                self.assertNotIn('error', out)
                self.assertNotIn('warning', out)
                # contents is there: whether the repository is public is not asked.
                self.assertEqual(seen['visibility'], 0)

    def test_a_permission_missing_or_not_accepted_fails(self):
        for role in APP_IDS:
            with self.subTest(role=role):
                installed = dict(manifest(role), pull_requests='read')
                code, out, _ = self.run_check(role, installed)
                self.assertEqual(code, 1)
                self.assertIn(f'{role}: error: pull_requests=write of the manifest is read on the '
                              'installation: not granted, or not accepted by the owner\n', out)

    def test_contents_missing_is_a_warning_for_a_public_repository_only(self):
        for role in APP_IDS:
            installed = {k: v for k, v in manifest(role).items() if k != 'contents'}
            with self.subTest(role=role, repository='public'):
                code, out, seen = self.run_check(role, installed, public=True)
                self.assertEqual(code, 0, out)
                self.assertIn(f'{role}: warning: contents=read of the manifest is missing', out)
                self.assertEqual(seen['visibility'], 1)
            with self.subTest(role=role, repository='private'):
                code, out, _ = self.run_check(role, installed, public=False)
                self.assertEqual(code, 1)
                self.assertIn(f'{role}: error: contents=read of the manifest is missing', out)

    def test_a_permission_more_is_a_warning(self):
        for role in APP_IDS:
            with self.subTest(role=role):
                code, out, _ = self.run_check(role, dict(manifest(role), contents='write'))
                self.assertEqual(code, 0, out)
                self.assertIn(f'{role}: warning: contents=write on the installation is more '
                              'than the manifest (contents=read)\n', out)

    def test_an_installation_that_does_not_answer_fails(self):
        for role in APP_IDS:
            module = script(role)
            out = io.StringIO()
            with self.subTest(role=role), \
                    mock.patch.object(module.AppAuth, 'installation_permissions',
                                      side_effect=GitHubError('HTTP 404: Not Found')), \
                    mock.patch.object(module.AppAuth, 'token') as mint:
                self.assertEqual(module.main(['--state-dir', str(self.state), '--check'],
                                             out=out), 1)
                self.assertIn(f'{role}: error: the installation of brushtyler-devloop-{role}[bot] '
                              'on brushtyler/ssdlc: HTTP 404: Not Found\n', out.getvalue())
                mint.assert_not_called()

    def test_without_the_key_it_stops(self):
        for role in APP_IDS:
            module = script(role)
            with self.subTest(role=role), tempfile.TemporaryDirectory() as empty, \
                    mock.patch.dict(os.environ, {'DEVLOOP_APP_KEY_DIR': empty}):
                with self.assertRaisesRegex(SystemExit, f'{role}-token: no private key'):
                    module.main(['--state-dir', str(self.state), '--check'])

    def test_check_takes_no_command_and_no_permission(self):
        for role in APP_IDS:
            module = script(role)
            with self.subTest(role=role):
                self.assertIsNone(module.parse_check(['--', 'gh', '--check']))
                self.assertIsNone(module.parse_check(['--perm', 'issues=read', '--', 'gh']))
                self.assertEqual(module.parse_check(['--check']), module.STATE)
                self.assertEqual(module.parse_check(['--state-dir', '/srv/p', '--check']),
                                 Path('/srv/p'))
                self.assertEqual(module.parse_check(['--check', '--state-dir', '/srv/p']),
                                 Path('/srv/p'))
                for argv in (['--check', '--', 'gh', 'x'], ['--perm', 'issues=read', '--check'],
                             ['--check', '--allow-host-details'], ['--check', '--state-dir']):
                    with self.subTest(argv=argv), \
                            self.assertRaisesRegex(SystemExit, 'mints no token'):
                        module.parse_check(argv)

    def test_the_manifest_is_the_roles(self):
        for role in APP_IDS:
            with self.subTest(role=role):
                module = script(role)
                self.assertEqual(module.MANIFEST, ROOT / 'ops/github/apps' / f'{role}.json')
                self.assertTrue(module.MANIFEST.is_file())


class DocumentationTests(unittest.TestCase):
    """The reference of the role apps and ops/README.md describe ``--check`` (#726)."""

    def test_the_reference_describes_the_check(self):
        section = docs.plan_section('docs/devloop/reference.md',
                                    '### The permissions of the role apps')
        docs.assert_documented(self, section, phrases=(
            'scripts/supervisor-token.py --state-dir <STATE_DIR> --check',
            'scripts/coordinator-token.py --state-dir <STATE_DIR> --check',
            "run as the role's user with its key",
            "`GET /repos/{repo}/installation` with the app's JWT",
            'It mints no installation token, prints none and runs no command',
            'missing on the installation, or there at a lower level (not granted, or not yet '
            'accepted by the owner of the installation), is an error',
            'that the manifest does not have, a warning',
            'its absence is a warning for a public repository, an error for a private one',
            'It exits with 1 on an error, with 0 otherwise',
            '`--no-apps` and `--read-only` skip it',
        ), issues=(726, 717))

    def test_the_ops_readme_describes_the_check_of_check_host(self):
        verify = docs.plan_section('ops/README.md', '### 8. Verifica')
        docs.assert_documented(self, verify, phrases=(
            'i permessi delle app dei ruoli contro i loro manifest',
            "come l'utente del ruolo, con la sua `key_dir`, `scripts/<ruolo>-token.py --check`",
            'senza coniare token né eseguire comandi',
            'un permesso di `ops/github/apps/<ruolo>.json` che manca o che il proprietario non '
            'ha accettato è un errore, uno in più un avviso',
            '`contents: read` mancante è un avviso per un repository pubblico, un errore per '
            'uno privato',
            'non con `--no-apps` né con `--read-only`',
        ), issues=(726,))
        apps_step = docs.plan_section('ops/README.md', '### 2. GitHub App')
        docs.assert_documented(self, apps_step, phrases=(
            '`--check` dei loro script dei token',
            "legge i permessi dell'installazione e li confronta con il manifest",
        ), issues=(726,))


if __name__ == '__main__':
    unittest.main()
