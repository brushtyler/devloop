"""devloop: identities per project (ADR 0006, R6, #425).

``devloop token <actor> [--perm NAME=LEVEL ...] -- <command>`` runs a command as an agent's app of
the project: ``repo`` and ``[apps]`` come from the configuration of the project devloop runs with
(the installation's, the project file and the local overlay of its state_dir), never from the
``config.toml`` beside a checkout, which has none since R6. ``scripts/agent-token.py`` is a thin
wrapper that calls it through the launcher, from the pinned copy. ``Identities`` and ``devloop
apps`` act on the project's ``repo`` too, with its own set of apps and key_dir (D3).
"""

import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import devloop.__main__ as cli
from devloop import apps, config, token
from helpers import ssdlc_project

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / 'scripts' / 'devloop-pinned'
WRAPPER = ROOT / 'scripts' / 'agent-token.py'
STATE = Path('.ssdlc') / 'devloop'


def wrapper():
    spec = importlib.util.spec_from_file_location('agent_token', WRAPPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_launcher():
    loader = importlib.machinery.SourceFileLoader('devloop_pinned', str(LAUNCHER))
    spec = importlib.util.spec_from_loader('devloop_pinned', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def widget_overlay(apps_table=None):
    """The overlay of another project: its repo, its trusted users and its own set of apps,
    over ssdlc's agy commands."""
    agy = config.read_toml(ssdlc_project.OVERLAY, 'local config')['agents']
    table = {'repo': 'acme/widget', 'trusted_users': ['acme-operator'], 'agents': agy}
    table['apps'] = apps_table if apps_table is not None else {
        'key_dir': '~/.ghapps-widget',
        'dispatcher': {'app_id': 101, 'slug': 'acme-devloop-dispatcher'},
        'claude': {'app_id': 102, 'slug': 'acme-devloop-claude-agent'},
        'codex': {'app_id': 103, 'slug': 'acme-devloop-codex-agent'}}
    return ssdlc_project.toml_text(table) + '\n'


class ProjectCase(unittest.TestCase):
    """A managed repository's main checkout with the state_dir of its project: ssdlc's project
    file and the overlay of another repository, ``acme/widget``."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.state = self.root / STATE
        self.state.mkdir(parents=True)
        shutil.copy(ssdlc_project.PROJECT, self.state / 'project.toml')
        self.overlay = self.state / 'config.local.toml'
        self.overlay.write_text(widget_overlay(), encoding='utf-8')

    def argv(self, *rest):
        return ['--project-config', str(self.state / 'project.toml'),
                '--local-config', str(self.overlay), 'token', *rest]

    def token(self, *rest, environ=None):
        """``devloop token`` with the project's files: ``(exit code, AppAuth arguments, the
        command and its environment)``; ``AppAuth`` and the command are stand-ins."""
        seen = {}

        class Auth:
            def __init__(self, app_id, slug, key_path, repo):
                seen['app'] = (app_id, slug, key_path, repo)

            def token(self, permissions):
                seen['permissions'] = permissions
                return 'app-token'

        def run(command, env):
            seen.update(command=command, env=env)
            return SimpleNamespace(returncode=7)

        environ = {'GH_TOKEN': 'operator', 'PYTHONPATH': '/pinned',
                   cli.STATE_ENV: str(self.state), 'HOME': '/home/x', **(environ or {})}
        # subprocess.run is the command's: git does not tell whether the project file is tracked.
        with mock.patch.object(cli, 'main_checkout', return_value=self.root), \
                mock.patch.object(cli, 'tracked', return_value=False), \
                mock.patch.object(token, 'AppAuth', Auth), \
                mock.patch.object(token, 'find_key', lambda key_dir, slug: f'{key_dir}/{slug}'), \
                mock.patch.object(token.subprocess, 'run', run), \
                mock.patch.dict(os.environ, environ, clear=True):
            with self.assertRaises(SystemExit) as stop:
                cli.main(self.argv(*rest))
        return stop.exception.code, seen


class TokenCommandTests(ProjectCase):
    def test_the_repository_and_the_app_are_the_projects(self):
        code, seen = self.token('claude', '--', 'gh', 'pr', 'comment', '70')
        self.assertEqual(code, 7)  # the command's exit code
        self.assertEqual(seen['app'], (102, 'acme-devloop-claude-agent',
                                       '~/.ghapps-widget/acme-devloop-claude-agent',
                                       'acme/widget'))
        self.assertEqual(seen['permissions'], token.DEFAULT)
        self.assertEqual(seen['command'], ['gh', 'pr', 'comment', '70'])

    def test_the_command_gets_the_token_only(self):
        _, seen = self.token('codex', '--', 'gh', 'x')
        env = seen['env']
        self.assertEqual(env['GH_TOKEN'], 'app-token')
        self.assertEqual(env['HOME'], '/home/x')
        # Neither the caller's credentials nor what the launcher set for devloop.
        for name in ('PYTHONPATH', cli.STATE_ENV):
            self.assertNotIn(name, env)

    def test_the_key_dir_of_the_environment_wins(self):
        _, seen = self.token('codex', '--', 'gh', 'x', environ={'DEVLOOP_APP_KEY_DIR': '/keys'})
        self.assertEqual(seen['app'][2], '/keys/acme-devloop-codex-agent')

    def test_everything_after_the_separator_is_the_command(self):
        _, seen = self.token('claude', '--perm', 'issues=read', '--perm', 'checks=read', '--',
                             'gh', 'api', '--perm', 'x', '--', 'y')
        self.assertEqual(seen['permissions'], {'issues': 'read', 'checks': 'read'})
        self.assertEqual(seen['command'], ['gh', 'api', '--perm', 'x', '--', 'y'])

    def test_an_actor_without_an_app_in_the_project_is_refused(self):
        code, seen = self.token('agy', '--', 'gh', 'x')
        self.assertIn("no GitHub App configured for 'agy'", code)
        self.assertIn('acme/widget', code)
        self.assertNotIn('command', seen)

    def test_reserved_actors_and_permissions_are_refused_before_any_read(self):
        cases = ((('dispatcher', '--', 'gh', 'x'), 'reserved to devloop'),
                 (('merger', '--', 'gh', 'x'), 'reserved to devloop'),
                 (('implementer', '--', 'gh', 'x'), 'reserved to devloop'),
                 (('supervisor', '--', 'gh', 'x'), 'scripts/supervisor-token.py'),
                 (('someone', '--', 'gh', 'x'), 'unknown actor'),
                 (('claude', '--perm', 'contents=write', '--', 'gh', 'x'), 'not allowed'),
                 (('claude', '--perm', 'administration=read', '--', 'gh', 'x'), 'not allowed'),
                 (('claude', '--perm', 'issues', '--', 'gh', 'x'), 'NAME=LEVEL'),
                 (('claude',), 'the command goes after --'),
                 (('claude', '--'), 'the command goes after --'))
        for argv, message in cases:
            with self.subTest(argv=argv), mock.patch.object(cli, 'load') as load:
                code, seen = self.token(*argv)
                self.assertIn(message, code)
                load.assert_not_called()
                self.assertEqual(seen, {})

    def test_without_the_identities_in_the_overlay_it_does_not_run(self):
        # R6: the installation has no repo and no trusted_users: they are the overlay's.
        self.overlay.write_text('', encoding='utf-8')
        code, seen = self.token('claude', '--', 'gh', 'x')
        self.assertIn('invalid config', code)
        self.assertIn('local overlay', code)
        self.assertEqual(seen, {})

    def test_the_state_dir_given_to_the_launcher_must_be_the_projects(self):
        code, seen = self.token('claude', '--', 'gh', 'x',
                                environ={cli.STATE_ENV: str(self.root / 'other')})
        self.assertIn('is not the directory given to the launcher', code)
        self.assertEqual(seen, {})


class IdentitiesTests(ProjectCase):
    def test_identities_act_on_the_projects_repository(self):
        cfg = cli.load(SimpleNamespace(config=None, local_config=str(self.overlay),
                                       project_config=str(self.state / 'project.toml')),
                       self.root)
        with mock.patch.object(apps, 'find_key', lambda key_dir, slug: Path(key_dir) / slug), \
                mock.patch.dict(os.environ):
            os.environ.pop('DEVLOOP_APP_KEY_DIR', None)
            identities = apps.Identities.from_config(cfg)
        self.assertEqual(identities.repo, 'acme/widget')
        self.assertEqual({actor: app.repo for actor, app in identities.apps.items()},
                         dict.fromkeys(('dispatcher', 'claude', 'codex'), 'acme/widget'))
        self.assertEqual(identities.login('dispatcher'), 'acme-devloop-dispatcher[bot]')
        self.assertEqual(identities.apps['claude'].key_path,
                         Path('~/.ghapps-widget/acme-devloop-claude-agent'))

    def apps_lines(self, cfg):
        app = mock.Mock(**{'describe.return_value': {
            'login': 'x[bot]', 'installation': 1, 'repository_selection': 'selected',
            'permissions': {'issues': 'write'}}})
        dispatcher = SimpleNamespace(identities=SimpleNamespace(apps={'dispatcher': app}))
        out = io.StringIO()
        with mock.patch.object(cli, 'build', return_value=(cfg, self.state, dispatcher)), \
                mock.patch.dict(os.environ, {}, clear=False), redirect_stdout(out):
            os.environ.pop('DEVLOOP_APP_KEY_DIR', None)
            cli.main(['apps'])
        return out.getvalue()

    def test_devloop_apps_names_the_projects_repository_and_key_dir(self):
        cfg = cli.load(SimpleNamespace(config=None, local_config=str(self.overlay),
                                       project_config=str(self.state / 'project.toml')),
                       self.root)
        out = self.apps_lines(cfg)
        self.assertIn('apps of acme/widget, keys in ~/.ghapps-widget', out)
        self.assertNotIn('merger', out.split('\n')[1])

    def test_devloop_apps_notes_a_merger_without_merges(self):
        # D3: the merger only where devloop merges. ssdlc's [merge] mode is off in the
        # installation: a note, nothing else changes.
        cfg = ssdlc_project.load(ssdlc_project.OVERLAY)
        self.assertFalse(cfg.merge.enabled())
        out = self.apps_lines(cfg)
        self.assertIn('merger: [merge] mode is off: the merger app is only for a repository '
                      'where devloop merges', out)
        strict = config.parse_config(config._merge(ssdlc_project.data(), {'merge': {'mode':
                                                                                     'strict'}}))
        self.assertNotIn('[merge] mode is off', self.apps_lines(strict))


class WrapperTests(unittest.TestCase):
    """``scripts/agent-token.py``: the thin wrapper of ``devloop token``."""

    def setUp(self):
        self.script = wrapper()

    def test_it_calls_devloop_token_through_the_launcher(self):
        argv = self.script.command(['claude', '--perm', 'issues=read', '--', 'gh', 'x'],
                                   python='py')
        self.assertEqual(argv, ['py', str(LAUNCHER), '--state-dir',
                                str(ROOT / '.ssdlc' / 'devloop'), 'token', 'claude', '--perm',
                                'issues=read', '--', 'gh', 'x'])

    def test_the_state_dir_of_another_project(self):
        for given in (['--state-dir', '/var/lib/devloop/widget'],
                      ['--state-dir=/var/lib/devloop/widget']):
            with self.subTest(given=given):
                argv = self.script.command([*given, 'codex', '--', 'gh', 'x'], python='py')
                self.assertEqual(argv[2:5], ['--state-dir', '/var/lib/devloop/widget', 'token'])
        # Relative to the caller's directory, made absolute for the launcher.
        argv = self.script.command(['--state-dir', 'state', 'codex', '--', 'gh'], python='py')
        self.assertEqual(argv[3], os.path.abspath('state'))

    def test_usage(self):
        for argv in ([], ['-h'], ['--help'], ['claude'], ['claude', 'gh'], ['--state-dir'],
                     ['--state-dir', '/x']):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.script.command(argv)

    def test_main_replaces_itself_with_the_launcher(self):
        calls = []
        self.script.main(['claude', '--', 'gh', 'x'], execv=lambda path, args: calls.append(args))
        self.assertEqual(calls, [self.script.command(['claude', '--', 'gh', 'x'])])

    def test_it_imports_nothing_from_the_clone(self):
        text = WRAPPER.read_text(encoding='utf-8')
        self.assertNotIn('from devloop', text)
        self.assertNotIn('import devloop', text)

    def test_the_launcher_passes_the_projects_files_before_token(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        copy = tmp / 'pinned-0123456789ab'
        (copy / 'devloop').mkdir(parents=True)
        (copy / 'devloop' / '__main__.py').write_text('')
        os.symlink(copy.name, tmp / 'current')
        (tmp / 'config.local.toml').write_text('')
        (tmp / 'project.toml').write_text('')
        argv, env = load_launcher().command(ROOT, ['token', 'claude', '--', 'gh'], state=tmp)
        self.assertEqual(argv[4:], ['--local-config', str(tmp / 'config.local.toml'),
                                    '--project-config', str(tmp / 'project.toml'),
                                    'token', 'claude', '--', 'gh'])
        self.assertEqual(env['PYTHONPATH'], str(copy))


@unittest.skipIf(os.geteuid() == 0, 'the launcher refuses to run as root')
class EndToEndTests(unittest.TestCase):
    """``scripts/agent-token.py`` -> ``scripts/devloop-pinned`` -> ``devloop token`` of the
    pinned copy, in a managed repository whose overlay names another repository and its apps.
    No token is minted: the chain stops at the key, which names the project's app."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / 'widget'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        self.state = self.root / STATE
        copy = self.state / 'pinned-0123456789ab'
        for package in ('devloop', 'core'):
            shutil.copytree(ROOT / package, copy / package,
                            ignore=shutil.ignore_patterns('__pycache__'))
        # The lists of review_policy.sensitive_path_lists, which a pin extracts with the copy.
        for name in config.read_toml(ssdlc_project.PROJECT)['review_policy'][
                'sensitive_path_lists']:
            (copy / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / name, copy / name)
        os.symlink(copy.name, self.state / 'current')
        shutil.copy(ssdlc_project.PROJECT, self.state / 'project.toml')
        self.keys = self.tmp / 'keys'
        self.keys.mkdir(mode=0o700)
        (self.state / 'config.local.toml').write_text(widget_overlay(
            {'key_dir': str(self.keys),
             'claude': {'app_id': 102, 'slug': 'acme-devloop-claude-agent'}}), encoding='utf-8')

    def run_wrapper(self, *argv):
        env = {k: v for k, v in os.environ.items()
               if k not in ('DEVLOOP_APP_KEY_DIR', cli.STATE_ENV, 'PYTHONPATH')}
        return subprocess.run([sys.executable, '-B', str(WRAPPER), '--state-dir',
                               str(self.state), *argv], cwd=self.root, env=env,
                              capture_output=True, text=True, timeout=120)

    def test_the_pinned_copy_reads_the_projects_app(self):
        done = self.run_wrapper('claude', '--', 'true')
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(f'no private key for acme-devloop-claude-agent in {self.keys}',
                      done.stderr)

    def test_an_app_the_project_does_not_have_is_refused(self):
        done = self.run_wrapper('codex', '--', 'true')
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("no GitHub App configured for 'codex'", done.stderr)
        self.assertIn('acme/widget', done.stderr)


if __name__ == '__main__':
    unittest.main()
