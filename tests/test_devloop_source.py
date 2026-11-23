"""devloop: the installation's source apart from the managed repository (ADR 0006, R1).

Two local git repositories, each with its own ``origin``: the source, which holds devloop and a
tag, and the managed repository, which does not. The pin takes the code from the source, the
smoke test, the worktrees and GitHub stay on the managed repository, the pin after a merge is off,
and the launcher gets the host's ``state_dir`` as a parameter. Without ``source`` everything is
as before (test_devloop_pin.py).
"""

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import devloop.__main__ as cli
from devloop import apps, config, pin
from devloop.config import ConfigError
from helpers import ssdlc_project
from test_devloop import CFG
from test_devloop_pin import LAUNCHER, copy_package, git, load_launcher


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class TwoRepositoriesCase(unittest.TestCase):
    """``source``: a clone of ``source.git`` holding devloop, tagged ``v1``. ``managed``: a clone
    of ``managed.git`` without devloop. ``state``: the host's state_dir, outside both."""

    def setUp(self):
        env = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com',
               'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.com'}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(cli.STATE_ENV, None)
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(self._cleanup)
        self.source = self.clone('source')
        copy_package(self.source)
        self.tagged = self.commit(self.source, 'devloop')
        git(self.source, 'tag', 'v1')
        git(self.source, 'push', '--quiet', 'origin', 'main', 'v1')
        self.managed = self.clone('managed')
        write(self.managed / 'README.md', 'the managed project\n')
        self.commit(self.managed, 'project')
        git(self.managed, 'push', '--quiet', 'origin', 'main')
        self.state = self.tmp / 'host' / 'state'
        self.state.mkdir(parents=True)
        self.overlay = self.state / 'config.local.toml'
        self.overlay.write_text(f'source = "{self.source}"\nstate_dir = "{self.state}"\n')
        self.logs = []

    def _cleanup(self):
        for path in self.tmp.rglob('*'):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o755)
        shutil.rmtree(self.tmp)

    def clone(self, name):
        origin = self.tmp / f'{name}.git'
        git(self.tmp, 'init', '--quiet', '--bare', '-b', 'main', str(origin))
        root = self.tmp / name
        git(self.tmp, 'clone', '--quiet', str(origin), str(root))
        git(root, 'checkout', '--quiet', '-b', 'main')
        return root

    @staticmethod
    def commit(root, message):
        git(root, 'add', '-A')
        git(root, 'commit', '--quiet', '-m', message)
        return git(root, 'rev-parse', 'HEAD')

    def cfg(self, **changes):
        return dataclasses.replace(CFG, source=str(self.source), state_dir=str(self.state),
                                   **changes)


class SourceRootTests(TwoRepositoriesCase):
    def test_without_source_it_is_the_managed_repository(self):
        self.assertEqual(pin.source_root(self.managed, ''), self.managed)

    def test_an_absolute_or_relative_path_names_the_source_clone(self):
        self.assertEqual(pin.source_root(self.managed, str(self.source)).resolve(), self.source)
        self.assertEqual(pin.source_root(self.managed, '../source').resolve(), self.source)
        with mock.patch.dict(os.environ, {'HOME': str(self.tmp)}):
            self.assertEqual(pin.source_root(self.managed, '~/source').resolve(), self.source)

    def test_a_worktree_of_the_source_names_its_main_checkout(self):
        worktree = self.tmp / 'source-worktree'
        git(self.source, 'worktree', 'add', '--quiet', '--detach', str(worktree))
        self.assertEqual(pin.source_root(self.managed, str(worktree)).resolve(), self.source)

    def test_a_path_that_is_not_a_checkout_is_refused(self):
        (self.tmp / 'plain').mkdir()
        cases = (('missing', 'not a directory'), (str(self.tmp / 'plain'), 'git rev-parse'),
                 (str(self.source / 'devloop'), 'not the top of a git checkout'),
                 (str(self.tmp / 'source.git'), 'git rev-parse'))
        for source, message in cases:
            with self.subTest(source=source), self.assertRaisesRegex(pin.PinError, message):
                pin.source_root(self.managed, source)
        with self.assertRaisesRegex(ConfigError, 'not a directory'):
            cli.installation_source(dataclasses.replace(CFG, source='missing'), self.managed)


class SourcePinTests(TwoRepositoriesCase):
    def setUp(self):
        super().setUp()
        ssdlc_project.as_default(self)  # the installation's file has no test_cmd (R3)

    def run_pin(self, ref):
        with mock.patch.object(cli, 'main_checkout', return_value=self.managed), \
                mock.patch.object(cli, 'log', self.logs.append), \
                mock.patch.object(pin, 'Smoke') as smoke:
            cli.main(['--local-config', str(self.overlay), 'pin', ref])
        return smoke

    def test_the_pin_takes_the_tag_of_the_source_and_smokes_in_the_managed_repository(self):
        smoke = self.run_pin('v1')
        copy = pin.current(self.state)
        self.assertEqual(copy, (self.state / f'pinned-{self.tagged[:12]}').resolve())
        self.assertEqual(pin.copy_commit(copy), self.tagged)
        self.assertTrue((copy / 'devloop' / '__main__.py').is_file())
        # The smoke test runs devloop of the new copy in the managed repository.
        self.assertEqual(smoke.call_args.args, (self.managed,))
        self.assertEqual(smoke.return_value.call_args.args[1], str(self.overlay))
        # Only the source was fetched; the managed repository is untouched.
        self.assertIn('refs/devloop/origin-tags/v1',
                      git(self.source, 'for-each-ref', '--format=%(refname)', 'refs/devloop/'))
        self.assertEqual(git(self.managed, 'for-each-ref', 'refs/devloop/'), '')
        self.assertEqual(git(self.managed, 'status', '--porcelain'), '')

    def test_a_ref_of_the_managed_repository_is_not_one_of_the_source(self):
        git(self.managed, 'checkout', '--quiet', '-b', 'only-managed')
        git(self.managed, 'push', '--quiet', 'origin', 'only-managed')
        with self.assertRaises(SystemExit) as raised:
            self.run_pin('only-managed')
        self.assertIn('pin failed, the pinned copy is unchanged', str(raised.exception.code))
        self.assertIsNone(pin.current(self.state))

    def test_a_source_that_is_not_a_checkout_fails_the_pin(self):
        self.overlay.write_text(f'source = "{self.tmp / "nowhere"}"\nstate_dir = "{self.state}"\n')
        with self.assertRaises(SystemExit) as raised:
            self.run_pin('v1')
        self.assertIn('pin failed, the pinned copy is unchanged: source', str(raised.exception.code))
        self.assertIsNone(pin.current(self.state))


class AutoPinTests(TwoRepositoriesCase):
    def test_the_pin_after_a_merge_only_when_the_source_is_the_managed_repository(self):
        auto = dataclasses.replace(CFG.pin, auto=True)
        pinner = cli.auto_pinner(self.cfg(pin=auto), self.managed, self.state, None,
                                 self.logs.append)
        self.assertIsNone(pinner)
        self.assertEqual(len(self.logs), 1)
        self.assertIn('auto-pin off: the installation comes from', self.logs[0])
        # With [pin] auto off there is nothing to say.
        self.assertIsNone(cli.auto_pinner(self.cfg(pin=dataclasses.replace(auto, auto=False)),
                                          self.managed, self.state, None, self.logs.append))
        self.assertEqual(len(self.logs), 1)
        # Without source, or with the managed repository as source, as before.
        for source in ('', str(self.managed), '.'):
            with self.subTest(source=source):
                cfg = dataclasses.replace(CFG, source=source, pin=auto)
                pinner = cli.auto_pinner(cfg, self.managed, self.state, None, self.logs.append)
                self.assertIsInstance(pinner, pin.AutoPin)
                self.assertEqual(pinner.root, self.managed)
        self.assertEqual(len(self.logs), 1)

    def test_build_gives_the_dispatcher_no_pinner_and_the_managed_worktrees(self):
        args = SimpleNamespace(config=None, local_config=None)
        cfg = self.cfg(pin=dataclasses.replace(CFG.pin, auto=True))
        with mock.patch.object(cli, 'load_config', return_value=cfg), \
                mock.patch.object(cli, 'main_checkout', return_value=self.managed), \
                mock.patch.object(cli, 'log', self.logs.append), \
                mock.patch.object(cli.Identities, 'from_config',
                                  return_value=apps.Identities(CFG.repo, {})):
            _, state, dispatcher = cli.build(args)
        self.assertEqual(state, self.state)
        self.assertIsNone(dispatcher.pinner)
        self.assertEqual(dispatcher.ws.repo_root, self.managed)
        self.assertEqual(dispatcher.ws.root, self.state / 'worktrees')
        self.assertTrue(any('auto-pin off' in line for line in self.logs))


class StateDirTests(TwoRepositoriesCase):
    def test_the_state_dir_given_to_the_launcher_must_be_that_of_the_configuration(self):
        cfg = self.cfg()
        self.assertEqual(cli.state_path(cfg, self.managed), self.state)
        with mock.patch.dict(os.environ, {cli.STATE_ENV: str(self.state)}):
            self.assertEqual(cli.state_path(cfg, self.managed), self.state)
            # Relative to the managed repository, the same directory.
            inside = dataclasses.replace(cfg, state_dir='.devloop')
            with mock.patch.dict(os.environ, {cli.STATE_ENV: str(self.managed / '.devloop')}):
                self.assertEqual(cli.state_path(inside, self.managed),
                                 self.managed / '.devloop')
            with self.assertRaisesRegex(ConfigError, 'is not the directory given to the launcher'):
                cli.state_path(inside, self.managed)

    def test_the_agents_do_not_get_the_state_dir_of_the_launcher(self):
        with mock.patch.dict(os.environ, {cli.STATE_ENV: str(self.state)}):
            env = cli.agent_environment(CFG, self.tmp / 'gh')
        self.assertNotIn(cli.STATE_ENV, env)


class ConfigTests(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            overlay = Path(tmp) / 'config.local.toml'
            overlay.write_text(text)
            return ssdlc_project.load(overlay)

    def test_source_is_a_key_of_the_overlay(self):
        self.assertEqual(ssdlc_project.load().source, '')
        self.assertEqual(self.load('source = "/srv/ssdlc"\n').source, '/srv/ssdlc')

    def test_an_empty_or_wrong_source_is_refused(self):
        with self.assertRaisesRegex(ConfigError, 'source: expected the path'):
            self.load('source = " "\n')
        with self.assertRaisesRegex(ConfigError, 'source'):
            self.load('source = 1\n')


class LauncherTests(TwoRepositoriesCase):
    """The launcher installed on the host, outside the pinned copy and the managed repository,
    with the host's state_dir as a parameter."""

    def setUp(self):
        super().setUp()
        self.launcher = self.tmp / 'lib' / 'devloop-pinned'
        self.launcher.parent.mkdir()
        shutil.copy(LAUNCHER, self.launcher)
        self.copy = self.state / 'pinned-0123456789ab'

    def fake_copy(self):
        write(self.copy / 'devloop' / '__init__.py', '')
        write(self.copy / 'devloop' / '__main__.py', (
            'import json, os, sys\nimport devloop\n'
            'print(json.dumps({"argv": sys.argv[1:], "package": devloop.__file__, '
            '"cwd": os.getcwd(), "state": os.environ.get("DEVLOOP_STATE_DIR")}))\n'))
        os.symlink(self.copy.name, self.state / 'current')

    def launch(self, *args, cwd=None):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('PYTHON') and k != cli.STATE_ENV}
        return subprocess.run([sys.executable, str(self.launcher), *args],
                              cwd=cwd or self.managed, env=env, capture_output=True, text=True,
                              timeout=120)

    def test_it_runs_the_copy_of_the_state_dir_in_the_managed_repository(self):
        self.fake_copy()
        for option in (['--state-dir', str(self.state)], [f'--state-dir={self.state}']):
            with self.subTest(option=option):
                proc = self.launch(*option, 'status', cwd=self.managed / '.git' / '..')
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(json.loads(proc.stdout), {
                    'argv': ['--local-config', str(self.overlay), 'status'],
                    'package': str(self.copy / 'devloop' / '__init__.py'),
                    'cwd': str(self.managed), 'state': str(self.state)})

    def test_it_needs_an_absolute_state_dir_and_the_managed_checkout(self):
        self.fake_copy()
        proc = self.launch('--state-dir', 'host/state', 'status', cwd=self.tmp)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('--state-dir needs an absolute path', proc.stderr)
        proc = self.launch('--state-dir', str(self.state), 'status', cwd=self.tmp)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('run it from the checkout of the managed repository', proc.stderr)
        proc = self.launch('--state-dir', str(self.tmp / 'empty'), 'status')
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("pin a tag of the installation's source first", proc.stderr)

    def test_root_still_needs_allow_root(self):
        self.fake_copy()
        launcher = load_launcher()
        calls = []
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.managed)
        with self.assertRaises(SystemExit) as raised:
            launcher.main(['--state-dir', str(self.state), 'run'], euid=0,
                          execve=lambda *a: calls.append(a))
        self.assertIn('refusing to run as root', str(raised.exception.code))
        launcher.main(['--allow-root', '--state-dir', str(self.state), 'run'], euid=0,
                      execve=lambda *a: calls.append(a))
        (_, argv, env), = calls
        self.assertEqual(argv[1:], ['-P', '-m', 'devloop', '--local-config', str(self.overlay),
                                    'run'])
        self.assertEqual(env[cli.STATE_ENV], str(self.state))
        self.assertEqual(os.getcwd(), str(self.managed))

    def test_without_the_parameter_no_state_dir_reaches_devloop(self):
        # The launcher of the clone, as for ssdlc: today's behaviour, and a stale variable of the
        # caller does not reach devloop.
        root = self.tmp / 'clone'
        (root / 'scripts').mkdir(parents=True)
        state = root / '.devloop'
        write(state / 'pinned-0123456789ab' / 'devloop' / '__main__.py', '')
        os.symlink('pinned-0123456789ab', state / 'current')
        with mock.patch.dict(os.environ, {cli.STATE_ENV: '/elsewhere'}):
            argv, env = load_launcher().command(root, ['run'])
        self.assertNotIn(cli.STATE_ENV, env)
        self.assertEqual(argv[1:], ['-P', '-m', 'devloop', 'run'])

    def test_the_state_dir_of_before_563_stops_the_launcher(self):
        # #563: the default state_dir is .devloop. A clone that still has .ssdlc/devloop, and no
        # .devloop, stops the launcher with the section of the migration: no silent fallback.
        root = self.tmp / 'clone'
        old = root / '.ssdlc' / 'devloop'
        write(old / 'pinned-0123456789ab' / 'devloop' / '__main__.py', '')
        os.symlink('pinned-0123456789ab', old / 'current')
        write(old / 'config.local.toml', '')
        launcher = load_launcher()
        with self.assertRaises(SystemExit) as raised:
            launcher.command(root, ['run'])
        message = str(raised.exception.code)
        self.assertIn(f'devloop-pinned: {old} is the state_dir of before #563 and '
                      f'{root / ".devloop"} does not exist', message)
        self.assertIn('stop the loop and move it', message)
        self.assertIn('ops/host/migrate.md, section "Percorsi devloop e istanza devloop@devloop"',
                      message)
        # Through main too, before anything runs.
        calls = []
        with self.assertRaises(SystemExit):
            launcher.main(['status'], euid=1000, execve=lambda *a: calls.append(a), root=root)
        self.assertEqual(calls, [])
        # The section it names exists.
        migrate = (Path(__file__).resolve().parents[1] / 'ops/host/migrate.md').read_text(
            encoding='utf-8')
        self.assertIn('\n## Percorsi devloop e istanza devloop@devloop (#563)\n', migrate)
        # Without either directory, the message of before: no pinned copy, pin first.
        shutil.rmtree(root / '.ssdlc')
        with self.assertRaises(SystemExit) as raised:
            launcher.command(root, ['run'])
        message = str(raised.exception.code)
        self.assertIn(f'no pinned copy at {root / ".devloop" / "current"}', message)
        self.assertIn('--project-config .devloop/project.toml', message)
        # With .devloop present, .ssdlc/devloop beside it is never read (#563).
        new = root / '.devloop'
        write(new / 'pinned-ba9876543210' / 'devloop' / '__main__.py', '')
        os.symlink('pinned-ba9876543210', new / 'current')
        write(old / 'config.local.toml', '')
        argv, env = launcher.command(root, ['run'])
        self.assertEqual(argv[1:], ['-P', '-m', 'devloop', 'run'])
        self.assertEqual(env['PYTHONPATH'], str((new / 'pinned-ba9876543210').resolve()))

    def test_the_real_package_stops_on_another_state_dir(self):
        copy_package(self.copy)
        os.symlink(self.copy.name, self.state / 'current')
        self.overlay.write_text(f'source = "{self.source}"\n')  # state_dir left to the default
        # The implementer of the real package's config.toml: a key by role since #499.
        copied = config.read_toml(self.copy / 'devloop' / 'config.toml')
        implementer = copied['roles']['implementer']
        proc = self.launch('--state-dir', str(self.state), 'fallback', 'reset', implementer)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('devloop: invalid config: state_dir: '
                      f'{self.managed / ".devloop"} is not the directory given to the '
                      f'launcher, {self.state}', proc.stderr)
        self.overlay.write_text(f'source = "{self.source}"\nstate_dir = "{self.state}"\n')
        proc = self.launch('--state-dir', str(self.state), 'fallback', 'reset', implementer)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('no model remembered unavailable', proc.stdout)
        self.assertFalse((self.managed / '.devloop').exists())
        self.assertFalse((self.managed / '.ssdlc').exists())


if __name__ == '__main__':
    unittest.main()
