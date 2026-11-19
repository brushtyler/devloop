"""The repository lock (ADR 0006, R8): two dispatchers with different state_dirs on the same
``repo`` never run together; on different repositories they do."""

import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from devloop import __main__ as cli
from devloop import config, pin
from devloop.config import ConfigError, load_config, repo_lock_name

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def load(self, text):
        overlay = self.tmp / 'config.local.toml'
        overlay.write_text(text)
        return load_config(ROOT / 'devloop/config.toml', overlay)

    def test_unset_by_default(self):
        self.assertEqual(load_config().repo_lock_dir, '')

    def test_an_absolute_directory_in_the_overlay(self):
        cfg = self.load('repo_lock_dir = "/run/devloop/repos"\n')
        self.assertEqual(cfg.repo_lock_dir, '/run/devloop/repos')
        self.assertEqual(self.load('repo_lock_dir = "~/locks"\n').repo_lock_dir, '~/locks')

    def test_a_relative_or_empty_directory_stops_devloop(self):
        for value in ('', 'locks', './locks', '~nosuchuserxyz/locks'):
            with self.subTest(value=value), \
                    self.assertRaisesRegex(ConfigError, 'repo_lock_dir: expected the absolute'):
                self.load(f'repo_lock_dir = "{value}"\n')
        with self.assertRaisesRegex(ConfigError, 'repo_lock_dir: expected str'):
            self.load('repo_lock_dir = 1\n')

    def test_a_repo_that_is_not_owner_and_name_stops_devloop_with_the_lock(self):
        for repo in ('ssdlc', 'a/b/c', 'own--er/x', '-owner/x', 'owner/..', 'owner/', 'o w/x'):
            with self.subTest(repo=repo), \
                    self.assertRaisesRegex(ConfigError, 'repo: expected owner/name'):
                self.load(f'repo = "{repo}"\nrepo_lock_dir = "/run/devloop/repos"\n')

    def test_the_name_is_owner_and_name_in_lower_case(self):
        self.assertEqual(repo_lock_name('brushtyler/ssdlc'), 'brushtyler--ssdlc.lock')
        self.assertEqual(repo_lock_name('BrushTyler/SSDLC'), 'brushtyler--ssdlc.lock')
        self.assertEqual(repo_lock_name('acme-corp/my.widget_2'), 'acme-corp--my.widget_2.lock')
        # Two hyphens never occur in an owner: the separator stays unambiguous.
        self.assertNotEqual(repo_lock_name('a-b/c'), repo_lock_name('a/b-c'))

    def test_it_needs_a_restart(self):
        self.assertNotIn('repo_lock_dir', config.LIVE_KEYS)


class RepoLockTests(unittest.TestCase):
    """Two dispatchers, each with its own state_dir, in one process: flock excludes two open
    files of the same lock as it excludes two processes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.locks = self.tmp / 'repos'
        self.locks.mkdir()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in (cli.LOCK_FD, cli.REPO_LOCK_FD):
            os.environ.pop(var, None)
        log = mock.patch.object(cli, 'log')
        log.start()
        self.addCleanup(log.stop)

    def cfg(self, repo='brushtyler/ssdlc', locks=None):
        return SimpleNamespace(repo=repo, repo_lock_dir=str(self.locks if locks is None
                                                            else locks))

    def dispatcher(self, tick=None):
        return SimpleNamespace(tick=mock.Mock(side_effect=tick, return_value=[]),
                               ws=mock.Mock())

    def main(self, argv, cfg, state, dispatcher):
        with mock.patch.object(cli, 'build', return_value=(cfg, self.tmp / state, dispatcher)):
            cli.main(argv)

    def test_two_state_dirs_on_the_same_repository_do_not_start_together(self):
        second = self.dispatcher()

        def tick(dry_run=False):
            for argv in (['tick'], ['run']):
                with self.assertRaises(SystemExit) as raised:
                    self.main(argv, self.cfg(), 'state-b', second)
                self.assertEqual(raised.exception.code,
                                 'devloop: another dispatcher is running on brushtyler/ssdlc '
                                 f'(repository lock {self.locks}/brushtyler--ssdlc.lock held)')
            # The same repository in other letters is the same lock.
            with self.assertRaisesRegex(SystemExit, 'another dispatcher is running on '
                                                    'BrushTyler/SSDLC'):
                self.main(['tick'], self.cfg('BrushTyler/SSDLC'), 'state-c', second)
        first = self.dispatcher(tick)
        with mock.patch.object(cli, 'run_loop') as loop:
            self.main(['tick'], self.cfg(), 'state-a', first)
        first.tick.assert_called_once_with(dry_run=False)
        second.tick.assert_not_called()
        loop.assert_not_called()
        self.assertEqual(sorted(p.name for p in self.locks.iterdir()),
                         ['brushtyler--ssdlc.lock'])
        # Released at the end: the second one starts now.
        self.main(['tick'], self.cfg(), 'state-b', second)
        second.tick.assert_called_once_with(dry_run=False)

    def test_two_state_dirs_on_different_repositories_start_together(self):
        other = self.dispatcher()

        def tick(dry_run=False):
            self.main(['tick'], self.cfg('acme/widget'), 'state-b', other)
        self.main(['tick'], self.cfg(), 'state-a', self.dispatcher(tick))
        other.tick.assert_called_once_with(dry_run=False)
        self.assertEqual(sorted(p.name for p in self.locks.iterdir()),
                         ['acme--widget.lock', 'brushtyler--ssdlc.lock'])

    def test_another_process_holds_the_lock(self):
        # A dispatcher of another process (or user) on the same repository: refused while it
        # holds the lock, which the kernel releases when it ends.
        path = self.locks / 'brushtyler--ssdlc.lock'
        holder = subprocess.Popen(
            [sys.executable, '-c',
             'import fcntl, sys\n'
             f'fh = open({str(path)!r}, "w")\n'
             'fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)\n'
             'print("held", flush=True)\n'
             'sys.stdin.read()\n'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait, 30)
        self.addCleanup(holder.stdout.close)
        try:
            self.assertEqual(holder.stdout.readline(), 'held\n')
            dispatcher = self.dispatcher()
            with self.assertRaisesRegex(SystemExit, 'another dispatcher is running on '
                                                    'brushtyler/ssdlc'):
                self.main(['run'], self.cfg(), 'state-b', dispatcher)
        finally:
            holder.stdin.close()
        holder.wait(30)
        with mock.patch.object(cli, 'run_loop', return_value=0) as loop:
            self.main(['run'], self.cfg(), 'state-b', dispatcher)
        loop.assert_called_once()

    def test_the_lock_of_state_dir_is_checked_first(self):
        # Two dispatchers on the same state_dir: the message of before (#174).
        def tick(dry_run=False):
            with self.assertRaisesRegex(SystemExit, r'another dispatcher is running \(lock '
                                                    r'held\)'):
                self.main(['tick'], self.cfg(), 'state-a', self.dispatcher())
        self.main(['tick'], self.cfg(), 'state-a', self.dispatcher(tick))

    def test_without_the_directory_nothing_changes(self):
        # Without repo_lock_dir only the lock of state_dir, as before ADR 0006, R8.
        other = self.dispatcher()

        def tick(dry_run=False):
            self.main(['tick'], SimpleNamespace(repo='brushtyler/ssdlc', repo_lock_dir=''),
                      'state-b', other)
        self.main(['tick'], SimpleNamespace(repo='brushtyler/ssdlc', repo_lock_dir=''),
                  'state-a', self.dispatcher(tick))
        other.tick.assert_called_once()
        self.assertEqual(list(self.locks.iterdir()), [])

    def test_a_missing_directory_stops_devloop_and_is_not_created(self):
        missing = self.tmp / 'run' / 'repos'
        dispatcher = self.dispatcher()
        with self.assertRaises(SystemExit) as raised:
            self.main(['tick'], self.cfg(locks=missing), 'state-a', dispatcher)
        self.assertEqual(raised.exception.code,
                         f'devloop: repo_lock_dir {missing} is not a directory: the host has to '
                         'prepare it for every devloop user (ops/host/setup-devloop.sh)')
        self.assertFalse(missing.parent.exists())
        dispatcher.tick.assert_not_called()

    def test_a_symlink_in_place_of_the_lock_is_refused(self):
        target = self.tmp / 'elsewhere'
        target.write_text('not a lock\n')
        (self.locks / 'brushtyler--ssdlc.lock').symlink_to(target)
        dispatcher = self.dispatcher()
        with self.assertRaisesRegex(SystemExit, 'the repository lock .* cannot be opened'):
            self.main(['tick'], self.cfg(), 'state-a', dispatcher)
        dispatcher.tick.assert_not_called()
        self.assertEqual(target.read_text(), 'not a lock\n')

    def test_a_dry_run_cleanup_and_gc_do_not_take_it(self):
        path = self.locks / 'brushtyler--ssdlc.lock'
        with open(path, 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            dispatcher = self.dispatcher()
            self.main(['tick', '--dry-run'], self.cfg(), 'state-a', dispatcher)
            dispatcher.tick.assert_called_once_with(dry_run=True)
            dispatcher.ws.names.return_value = []
            self.main(['cleanup'], self.cfg(), 'state-a', dispatcher)
            gc = SimpleNamespace(repo='brushtyler/ssdlc', repo_lock_dir=str(self.locks),
                                 gc=SimpleNamespace(enabled=lambda: False))
            self.main(['gc'], gc, 'state-a', dispatcher)

    def test_a_restart_from_a_new_copy_passes_the_lock_on(self):
        dispatcher = self.dispatcher(pin.Repinned(self.tmp / 'new'))
        seen = {}

        def reexec(copy, argv, fd, var):
            value = os.environ[cli.REPO_LOCK_FD]
            seen['inheritable'] = os.get_inheritable(int(value))
            seen['same'] = os.path.samestat(os.fstat(int(value)),
                                            os.stat(self.locks / 'brushtyler--ssdlc.lock'))
        with mock.patch.object(cli.pin, 'reexec', reexec):
            self.main(['tick'], self.cfg(), 'state-a', dispatcher)
        self.assertEqual(seen, {'inheritable': True, 'same': True})

    def test_the_new_process_keeps_the_inherited_lock(self):
        path = self.locks / 'brushtyler--ssdlc.lock'
        with open(path, 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd = os.dup(held.fileno())
            os.environ[cli.REPO_LOCK_FD] = str(fd)
            with cli.repo_lock(self.cfg()) as fh:
                self.assertEqual(fh.fileno(), fd)
                self.assertNotIn(cli.REPO_LOCK_FD, os.environ)
                self.assertFalse(os.get_inheritable(fd))
            # Without the inherited descriptor the lock is someone else's.
            with self.assertRaisesRegex(SystemExit, 'another dispatcher is running on'):
                with cli.repo_lock(self.cfg()):
                    pass

    def test_a_descriptor_of_another_file_is_not_the_lock(self):
        with open(self.tmp / 'other', 'w') as other:
            os.environ[cli.REPO_LOCK_FD] = str(other.fileno())
            with cli.repo_lock(self.cfg()) as fh:
                self.assertNotEqual(fh.fileno(), other.fileno())
            self.assertFalse(other.closed)

    def test_agents_never_inherit_it(self):
        with cli.repo_lock(self.cfg()) as fh:
            self.assertFalse(os.get_inheritable(fh.fileno()))

    def test_the_other_devloop_users_can_open_it_whatever_the_umask(self):
        before = os.umask(0o077)
        try:
            with cli.repo_lock(self.cfg()):
                pass
        finally:
            os.umask(before)
        mode = (self.locks / 'brushtyler--ssdlc.lock').stat().st_mode & 0o777
        self.assertEqual(mode, 0o644)


if __name__ == '__main__':
    unittest.main()
