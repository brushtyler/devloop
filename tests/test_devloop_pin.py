"""devloop: pinned installation, launcher, local config as overlay and pin after a merge (#174)."""

import dataclasses
import fcntl
import importlib.machinery
import importlib.util
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import devloop.__main__ as cli
from devloop import config, markdown, pin, runloop
from devloop.config import ConfigError, MergePolicy, PinPolicy
from devloop.dispatcher import Dispatcher, Snapshot
from devloop.workspace import Workspace, WorkspaceError
from test_devloop import (CFG, REV_NAME, SHA1, FakeRunner, FakeWS, norm, raw_review, receipt)
from test_devloop_merge import Identities, MergeGH, header, records
from test_devloop_zero import changes

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / 'scripts' / 'devloop-pinned'
LIST = 'schemas/recovery-grader-baseline.json'
DATA = tomllib.loads(config.DEFAULT_CONFIG.read_text())
# The documents of [docs] map (#203): the commits of these tests hold the map, not the documents.
DOCUMENTS = list(dict.fromkeys(d for _, docs in config.docs_entries(DATA) for d in docs))


def load_launcher():
    loader = importlib.machinery.SourceFileLoader('devloop_pinned', str(LAUNCHER))
    spec = importlib.util.spec_from_loader('devloop_pinned', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def copy_package(dest):
    """The devloop package and the list file of this checkout into ``dest``: its configuration
    has a ``[docs] map``, but none of the documents it maps (#210)."""
    shutil.copytree(REPO / 'devloop', dest / 'devloop',
                    ignore=shutil.ignore_patterns('__pycache__'))
    (dest / LIST).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / LIST, dest / LIST)


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


class GitCase(unittest.TestCase):
    """A clone of a local ``origin`` whose ``main`` holds this checkout's devloop."""

    def setUp(self):
        env = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com',
               'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.com'}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        origin = self.tmp / 'origin.git'
        git(self.tmp, 'init', '--quiet', '--bare', '-b', 'main', str(origin))
        self.root = self.tmp / 'clone'
        git(self.tmp, 'clone', '--quiet', str(origin), str(self.root))
        git(self.root, 'checkout', '--quiet', '-b', 'main')
        copy_package(self.root)
        (self.root / 'NOTES.md').write_text('not pinned\n')
        self.first = self.commit('devloop')
        git(self.root, 'push', '--quiet', 'origin', 'main')
        self.state = self.root / '.ssdlc' / 'devloop'
        self.logs = []
        self.smoked = []

    def passing_smoke(self, copy, local):
        """The smoke test of #260 needs GitHub: these tests record it and let it pass."""
        self.smoked.append(Path(copy))

    def _cleanup(self):
        for path in self.tmp.rglob('*'):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o755)
        shutil.rmtree(self.tmp)

    def commit(self, message):
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '--quiet', '-m', message)
        return git(self.root, 'rev-parse', 'HEAD')

    def push(self, message, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        sha = self.commit(message)
        git(self.root, 'push', '--quiet', 'origin', 'main')
        return sha

    def pin(self, ref, local=None, **kw):
        kw.setdefault('smoke', self.passing_smoke)
        return pin.pin(self.root, self.state, ref, local, log=self.logs.append, **kw)

    def pointed(self):
        return pin.current(self.state)


class PinTests(GitCase):
    def test_pin_makes_a_verified_read_only_copy_and_points_at_it(self):
        # A commit only local on main: a bare branch name is the branch of origin.
        (self.root / 'local.txt').write_text('local')
        self.commit('local only')
        copy = self.pin('main')
        self.assertEqual(copy, self.state / f'pinned-{self.first[:12]}')
        self.assertEqual(self.pointed(), copy.resolve())
        self.assertTrue((self.state / 'current').is_symlink())
        self.assertEqual(os.readlink(self.state / 'current'), copy.name)
        self.assertEqual(sorted(p.name for p in copy.iterdir()), ['devloop', 'pin.json', 'schemas'])
        self.assertEqual(json.loads((copy / 'pin.json').read_text()),
                         {'commit': self.first, 'files': [LIST]})
        self.assertEqual((copy / LIST).read_bytes(), (REPO / LIST).read_bytes())
        self.assertTrue((copy / 'devloop' / 'dispatcher.py').is_file())
        for path in [copy, *copy.rglob('*')]:
            self.assertFalse(path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH),
                             path)
        self.assertFalse(list(self.state.glob('.pinned-*')))
        self.assertEqual(self.logs, [f'pinned main at {self.first[:12]}: {copy}'])

    def test_a_failed_check_leaves_the_pointer_unchanged(self):
        old = self.pin('origin/main')
        broken = self.push('broken', 'devloop/__init__.py',
                           (self.root / 'devloop/__init__.py').read_text()
                           + "\nraise ImportError('broken on purpose')\n")
        with self.assertRaisesRegex(pin.PinError, 'broken on purpose'):
            self.pin('main')
        self.assertEqual(self.pointed(), old.resolve())
        self.assertFalse((self.state / f'pinned-{broken[:12]}').exists())
        self.assertFalse(list(self.state.glob('.pinned-*')))

    def test_a_missing_or_escaping_list_file_fails(self):
        old = self.pin('main')
        text = (self.root / 'devloop/config.toml').read_text().replace(
            f'sensitive_path_lists = ["{LIST}"]', 'sensitive_path_lists = ["schemas/none.json"]')
        self.push('missing list', 'devloop/config.toml', text)
        with self.assertRaisesRegex(pin.PinError, 'schemas/none.json is not a file'):
            self.pin('main')
        local = self.tmp / 'config.local.toml'
        for value in ('/etc/passwd', '../x.json', 'schemas/../x', './x', 'schemas//x'):
            with self.subTest(value=value):
                local.write_text(f'[review_policy]\nsensitive_path_lists = ["{value}"]\n')
                with self.assertRaisesRegex(pin.PinError, 'not a relative path'):
                    self.pin('main', local)
        self.assertEqual(self.pointed(), old.resolve())

    def test_a_commit_with_a_docs_map_pins_without_its_documents(self):
        # #210: the map is checked for shape only; its documents belong to the repository, and
        # the pinned copy holds neither those the commit lacks nor those it has.
        self.assertTrue(DOCUMENTS)
        self.assertIn('docs/devloop/guide.md', DOCUMENTS)
        tree = git(self.root, 'ls-tree', '-r', '--name-only', self.first).splitlines()
        self.assertFalse(set(DOCUMENTS) & set(tree))
        copy = self.pin('main')
        self.assertEqual(self.pointed(), copy.resolve())
        self.assertFalse((copy / 'docs').exists())
        second = self.push('a mapped document', 'docs/devloop/guide.md', 'devloop\n')
        copy = self.pin('main')
        self.assertEqual(copy, self.state / f'pinned-{second[:12]}')
        self.assertEqual(sorted(p.name for p in copy.iterdir()), ['devloop', 'pin.json', 'schemas'])
        self.assertEqual(json.loads((copy / 'pin.json').read_text())['files'], [LIST])

    def test_a_malformed_docs_map_still_fails_the_pin(self):
        # #210: the copy's configuration still refuses a map of the wrong shape.
        old = self.pin('main')
        local = self.tmp / 'config.local.toml'
        for value in ('"x.py" = ["/etc/passwd"]', '"x*.py" = ["docs/a.md"]', '"x.py" = []'):
            with self.subTest(value=value):
                local.write_text(f'[docs.map]\n{value}\n')
                with self.assertRaisesRegex(pin.PinError, 'the check failed:.*docs.map'):
                    self.pin(self.first[:12], local)
        self.assertEqual(self.pointed(), old.resolve())

    def test_a_commit_only_local_is_refused(self):
        self.pin('main')
        (self.root / 'x.txt').write_text('x')
        sha = self.commit('only here')
        for ref in (sha, 'HEAD', '-x', 'a b', ''):
            with self.subTest(ref=ref):
                with self.assertRaises(pin.PinError):
                    self.pin(ref)
        self.assertEqual(self.pointed(), (self.state / f'pinned-{self.first[:12]}').resolve())

    def side_commit(self, branch, path):
        """A commit on ``branch``, off main, not on main."""
        git(self.root, 'checkout', '--quiet', '-b', branch)
        (self.root / path).write_text(branch)
        git(self.root, 'add', '--', path)  # not the state directory
        git(self.root, 'commit', '--quiet', '-m', branch)
        git(self.root, 'checkout', '--quiet', 'main')
        return git(self.root, 'rev-parse', branch)

    def test_a_branch_deleted_on_origin_no_longer_counts(self):
        old = self.pin('main')
        sha = self.side_commit('feature', 'feature.txt')
        git(self.root, 'push', '--quiet', 'origin', 'feature')
        git(self.root, 'fetch', '--quiet', 'origin')  # a stale origin/feature stays locally
        git(self.tmp / 'origin.git', 'branch', '--quiet', '-D', 'feature')
        for ref in (sha, 'feature', 'origin/feature'):
            with self.subTest(ref=ref):
                with self.assertRaises(pin.PinError):
                    self.pin(ref)
        self.assertEqual(git(self.root, 'for-each-ref', 'refs/remotes/origin/feature'), '')
        self.assertFalse((self.state / f'pinned-{sha[:12]}').exists())
        self.assertEqual(self.pointed(), old.resolve())

    def test_a_tag_of_origin_counts_a_local_tag_does_not(self):
        published = self.side_commit('release', 'release.txt')
        git(self.root, 'tag', '-a', '-m', 'v9', 'v9', published)
        git(self.root, 'push', '--quiet', 'origin', 'refs/tags/v9')
        git(self.root, 'branch', '--quiet', '-D', 'release')
        local = self.side_commit('mine', 'mine.txt')
        git(self.root, 'tag', '-f', 'v9', local)  # the local v9 is not the one of origin
        git(self.root, 'tag', 'only-here', local)
        copy = self.pin('v9')
        self.assertEqual(copy.name, f'pinned-{published[:12]}')
        self.assertEqual(self.pointed(), copy.resolve())
        for ref in ('only-here', 'refs/tags/only-here', 'refs/tags/v9', local):
            with self.subTest(ref=ref):
                with self.assertRaisesRegex(pin.PinError, 'not reachable from the refs of origin'):
                    self.pin(ref)
        self.assertEqual(self.pointed(), copy.resolve())
        # A tag deleted on origin no longer counts either.
        gone = self.side_commit('gone', 'gone.txt')
        git(self.root, 'tag', 'v10', gone)
        git(self.root, 'push', '--quiet', 'origin', 'refs/tags/v10')
        git(self.root, 'fetch', '--quiet', 'origin', '+refs/tags/*:refs/devloop/origin-tags/*')
        git(self.tmp / 'origin.git', 'tag', '-d', 'v10')
        for ref in ('v10', gone):
            with self.subTest(ref=ref):
                with self.assertRaisesRegex(pin.PinError, 'not reachable from the refs of origin'):
                    self.pin(ref)
        self.assertEqual(self.pointed(), copy.resolve())

    def test_rollback_reuses_a_verified_copy_without_fetch(self):
        old = self.pin('main')
        new_sha = self.push('new', 'devloop/extra.txt', 'new\n')
        new = self.pin('main')
        self.assertEqual(new.name, f'pinned-{new_sha[:12]}')
        self.assertTrue(old.is_dir())  # earlier copies stay
        git(self.root, 'remote', 'set-url', 'origin', str(self.tmp / 'gone.git'))
        self.assertEqual(self.pin(self.first[:12]), old)
        self.assertEqual(self.pointed(), old.resolve())
        self.assertIn('(copy reused)', self.logs[-1])
        # A reused copy is checked again against the current overlay.
        local = self.tmp / 'config.local.toml'
        local.write_text('[review_policy]\nsensitive_path_lists = ["NOTES.md"]\n')
        with self.assertRaisesRegex(pin.PinError, 'NOTES.md is missing'):
            self.pin(new_sha, local)
        self.assertEqual(self.pointed(), old.resolve())

    def test_the_overlay_applies_to_the_check(self):
        self.pin('main')
        local = self.tmp / 'config.local.toml'
        local.write_text('bogus = 1\n')
        with self.assertRaisesRegex(pin.PinError, "unknown key 'bogus'"):
            self.pin('main', local)
        local.write_text('max_open_prs = 0\n')  # a known key, out of range for the copy
        with self.assertRaisesRegex(pin.PinError, 'max_open_prs: out of range'):
            self.pin('main', local)

    def test_a_pin_clears_the_request_of_a_merge(self):
        pin.request(self.state, 100, SHA1)
        self.assertEqual(pin.pending(self.state)['pr'], 100)
        self.pin('main')
        self.assertIsNone(pin.pending(self.state))

    def test_the_fetch_of_an_auto_pin_holds_the_lock_of_the_workspace(self):
        # #301: the pin may run while actions run; its fetch waits for theirs, and theirs for it.
        held = []

        class Lock:
            def __enter__(_):
                held.append('enter')

            def __exit__(_, *exc):
                held.append('exit')
        real = pin._git

        def git_(root, *args, **kw):
            if args[0] == 'fetch':
                held.append('fetch')
            return real(root, *args, **kw)
        auto = pin.AutoPin(self.root, self.state, log=self.logs.append, smoke=self.passing_smoke,
                           fetch_lock=Lock())
        with mock.patch.object(pin, '_git', git_):
            copy = auto.pin('main')
        self.assertEqual(held, ['enter', 'fetch', 'exit'])
        self.assertEqual(auto.current(), copy.resolve())

    def test_the_command_line(self):
        with mock.patch.object(cli, 'main_checkout', return_value=self.root), \
                mock.patch.object(cli, 'log', self.logs.append), \
                mock.patch.object(pin, 'Smoke') as smoke:
            cli.main(['pin', 'main'])
            self.assertEqual(self.pointed().name, f'pinned-{self.first[:12]}')
            with self.assertRaises(SystemExit) as raised:
                cli.main(['pin', 'nothing-here'])
        self.assertIn('pin failed, the pinned copy is unchanged', str(raised.exception.code))
        # The smoke test of #260 ran from the main checkout on the new copy, before the pointer.
        self.assertEqual(smoke.call_args.args, (self.root,))
        copy, local = smoke.return_value.call_args.args
        self.assertEqual(copy.parent, self.state)
        self.assertTrue(copy.name.startswith(f'.pinned-{self.first[:12]}-'))
        self.assertEqual(local, self.state / 'config.local.toml')


class AutoPinTests(GitCase):
    """The pin after a merge, through the real repository and a dispatcher whose tick stops at
    the pin."""

    def dispatcher(self, auto):
        cfg = dataclasses.replace(CFG, pin=PinPolicy(auto=auto))
        snapshots = []
        d = Dispatcher(cfg, None, FakeWS(), FakeRunner(), log=self.logs.append,
                       pinner=pin.AutoPin(self.root, self.state, log=self.logs.append,
                                          smoke=self.passing_smoke))
        d.labels_checked = True  # no GitHub here: the label rename of #248 is not under test
        d.snapshot = lambda: snapshots.append(1) or (_ for _ in ()).throw(AssertionError('plan'))
        return d, snapshots

    def test_a_requested_pin_runs_before_planning_and_restarts(self):
        self.pin('main')
        new_sha = self.push('merged', 'devloop/extra.txt', 'new\n')
        pin.request(self.state, 100, new_sha)
        d, snapshots = self.dispatcher(auto=True)
        with self.assertRaises(pin.Repinned) as raised:
            d.tick()
        self.assertEqual(raised.exception.copy, self.state / f'pinned-{new_sha[:12]}')
        self.assertEqual(self.pointed().name, f'pinned-{new_sha[:12]}')
        self.assertIsNone(pin.pending(self.state))
        self.assertEqual(snapshots, [])
        self.assertIn('auto-pin of origin/main after the merge of #100', self.logs)

    def test_off_never_pins(self):
        old = self.pin('main')
        self.push('merged', 'devloop/extra.txt', 'new\n')
        pin.request(self.state, 100, SHA1)
        d, snapshots = self.dispatcher(auto=False)
        with self.assertRaisesRegex(AssertionError, 'plan'):
            d.tick()
        self.assertEqual(snapshots, [1])
        self.assertEqual(self.pointed(), old.resolve())

    def test_a_failed_pin_stops_the_tick_and_keeps_the_pointer(self):
        old = self.pin('main')
        self.push('broken', 'devloop/__init__.py', "raise ImportError('broken on purpose')\n")
        pin.request(self.state, 100, SHA1)
        d, snapshots = self.dispatcher(auto=True)
        with self.assertRaisesRegex(pin.PinError, 'auto-pin of origin/main failed, the pinned '
                                                  'copy is unchanged: .*broken on purpose'):
            d.tick()
        self.assertEqual((snapshots, self.pointed()), ([], old.resolve()))
        self.assertEqual(pin.pending(self.state)['pr'], 100)  # tried again at the next tick
        # `run` counts it as a failed tick and goes on with the next one, after the backoff.
        d.tick = mock.Mock(side_effect=[pin.PinError('auto-pin failed'), KeyboardInterrupt])
        loop = runloop.Loop(d.tick, None, self.tmp / 'STOP', log=self.logs.append,
                            clock=lambda: 0, sleep=lambda s: None)
        with mock.patch.object(runloop, 'BACKOFF_SECONDS', (0, 0, 0)), \
                self.assertRaises(KeyboardInterrupt):
            loop.run()
        self.assertIn('tick failed (1 in a row): PinError: auto-pin failed', self.logs)


# A fake copy (#260): its `python3 -m devloop` does, for each step and attempt, what plan.json
# next to devloop/ says (`<command><attempt>` first, then `<command>`), and records each call.
FAKE_MAIN = r'''
import json, os, sys
from pathlib import Path
import devloop
here = Path(__file__).resolve().parent.parent
plan = json.loads((here / 'plan.json').read_text())
log = Path(os.environ['SMOKE_CALLS'])
calls = json.loads(log.read_text()) if log.exists() else []
command = 'tick' if 'tick' in sys.argv else 'watch'
calls.append({'argv': sys.argv[1:], 'cwd': os.getcwd(), 'path': os.environ.get('PYTHONPATH'),
              'package': devloop.__file__})
log.write_text(json.dumps(calls))
attempt = sum(1 for c in calls if command in c['argv'])
action = plan.get(f'{command}{attempt}', plan.get(command, 'ok'))


class GitHubError(RuntimeError):
    pass


if action == 'import':
    import devloop.nothing_here
if action == 'config':
    raise SystemExit('devloop: invalid config: max_open_prs: out of range')
if action == '502':
    raise GitHubError('gh api graphql: HTTP 502: Bad Gateway')
if action == 'auth':
    raise GitHubError('gh api graphql: HTTP 401: Bad credentials')
if action in ('round-network', 'round-auth'):
    print('devloop watch: round failed (%s)' % action.split('-')[1].replace('auth', 'authentication'),
          file=sys.stderr)
    sys.exit(3)
if action == 'silent':
    sys.exit(3)
if command == 'watch':
    print('12:00 watch baseline: 0 open PRs, 0 open issues')
    sys.exit(3)
print('[dry-run] open PRs: 0')
'''


class SmokeTests(unittest.TestCase):
    """``pin.Smoke`` (#260) on fake copies, through the real subprocess and wrapper."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.copy = self.tmp / 'copy'
        (self.copy / 'devloop').mkdir(parents=True)
        (self.copy / 'devloop' / '__init__.py').write_text('')
        (self.copy / 'devloop' / '__main__.py').write_text(FAKE_MAIN)
        self.root = self.tmp / 'root'
        self.root.mkdir()
        self.calls = self.tmp / 'calls.json'
        patcher = mock.patch.dict(os.environ, {'SMOKE_CALLS': str(self.calls),
                                               'PYTHONPATH': '/elsewhere'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logs = []

    def smoke(self, local=None, **plan):
        (self.copy / 'plan.json').write_text(json.dumps(plan))
        pin.Smoke(self.root, log=self.logs.append, watch_seconds=5)(self.copy, local)

    def recorded(self):
        return json.loads(self.calls.read_text())

    def commands(self):
        return ['tick' if 'tick' in c['argv'] else 'watch' for c in self.recorded()]

    def test_a_healthy_copy_passes_from_the_copy_with_the_overlay(self):
        local = self.tmp / 'config.local.toml'
        local.write_text('max_open_prs = 4\n')
        self.smoke(local)
        tick, watch = self.recorded()
        self.assertEqual(tick['argv'], ['--local-config', str(local.resolve()), 'tick',
                                        '--dry-run'])
        self.assertEqual(watch['argv'][:8], ['--local-config', str(local.resolve()), 'watch',
                                             '--until-change', '--interval', '5', '--timeout',
                                             '5'])
        # A state file of its own, gone afterwards.
        self.assertEqual(watch['argv'][8], '--state')
        self.assertFalse(Path(watch['argv'][9]).exists())
        for call in (tick, watch):
            self.assertEqual(call['cwd'], str(self.root.resolve()))
            self.assertEqual(call['path'], str(self.copy.resolve()))
            self.assertEqual(call['package'], str(self.copy.resolve() / 'devloop' / '__init__.py'))
        self.assertEqual(self.logs, ['smoke test passed: tick --dry-run, watch'])
        # Without an overlay, no --local-config.
        self.calls.unlink()
        self.smoke(self.tmp / 'missing.toml')
        self.assertEqual(self.recorded()[0]['argv'], ['tick', '--dry-run'])

    def test_an_import_error_fails_at_once(self):
        with self.assertRaises(pin.StepFailed) as raised:
            self.smoke(tick='import')
        self.assertEqual(raised.exception.step, 'tick --dry-run')
        self.assertFalse(raised.exception.transient)
        self.assertIn("ModuleNotFoundError: No module named 'devloop.nothing_here'",
                      str(raised.exception))
        self.assertEqual(self.commands(), ['tick'])  # an error of the code is not retried

    def test_an_invalid_config_fails(self):
        with self.assertRaisesRegex(pin.StepFailed, 'invalid config: max_open_prs') as raised:
            self.smoke(tick='config')
        self.assertFalse(raised.exception.transient)
        self.assertEqual(self.commands(), ['tick'])

    def test_a_transient_error_is_retried_once(self):
        self.smoke(tick1='502')
        self.assertEqual(self.commands(), ['tick', 'tick', 'watch'])
        self.assertEqual(self.logs[0], 'smoke test: tick --dry-run failed with a transient error '
                                       '(GitHubError: gh api graphql: HTTP 502: Bad Gateway), '
                                       'trying once more')
        self.assertEqual(self.logs[-1], 'smoke test passed: tick --dry-run, watch')
        self.calls.unlink()
        self.smoke(watch1='round-network')
        self.assertEqual(self.commands(), ['tick', 'watch', 'watch'])

    def test_a_second_transient_error_fails(self):
        with self.assertRaisesRegex(pin.StepFailed, 'HTTP 502') as raised:
            self.smoke(tick='502')
        self.assertTrue(raised.exception.transient)
        self.assertEqual(self.commands(), ['tick', 'tick'])

    def test_other_github_errors_are_not_retried(self):
        with self.assertRaisesRegex(pin.StepFailed, 'HTTP 401'):
            self.smoke(tick='auth')
        self.assertEqual(self.commands(), ['tick'])

    def test_watch_needs_a_round_that_ends(self):
        with self.assertRaisesRegex(pin.StepFailed, r'a round failed \(authentication\)') as raised:
            self.smoke(watch='round-auth')
        self.assertEqual(raised.exception.step, 'watch')
        self.assertEqual(self.commands(), ['tick', 'watch'])
        self.calls.unlink()
        with self.assertRaisesRegex(pin.StepFailed, 'no round ended in 5 s'):
            self.smoke(watch='silent')
        self.assertEqual(self.commands(), ['tick', 'watch', 'watch'])
        self.calls.unlink()
        with self.assertRaisesRegex(pin.StepFailed, 'ModuleNotFoundError'):
            self.smoke(watch='import')
        self.assertEqual(self.commands(), ['tick', 'watch'])

    def test_a_step_that_does_not_end_is_transient(self):
        def run(*args, **kw):
            raise subprocess.TimeoutExpired(args[0], kw['timeout'])
        smoke = pin.Smoke(self.root, log=self.logs.append, run=run, tick_seconds=7)
        with self.assertRaisesRegex(pin.StepFailed, 'did not end in 7 s') as raised:
            smoke(self.copy, None)
        self.assertTrue(raised.exception.transient)
        self.assertIn('trying once more', self.logs[0])


def completed(code=0, stdout='', stderr=''):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class PinSmokeTests(GitCase):
    """The smoke test of #260 inside ``pin``, on the real repository."""

    def break_main(self):
        """A commit whose devloop imports and loads its configuration, but whose
        ``python3 -m devloop`` fails with an import error: only the smoke test sees it."""
        text = (self.root / 'devloop/__main__.py').read_text()
        self.assertIn("if __name__ == '__main__':\n    main()\n", text)
        return self.push('broken', 'devloop/__main__.py', text.replace(
            "if __name__ == '__main__':\n    main()\n",
            "if __name__ == '__main__':\n    raise ImportError('broken on purpose')\n"))

    def test_a_copy_that_fails_the_smoke_test_never_becomes_current(self):
        old = self.pin('main')
        broken = self.break_main()
        for attempt in (1, 2):  # a second pin of the same commit extracts and tests it again
            with self.subTest(attempt=attempt):
                with self.assertRaises(pin.SmokeError) as raised:
                    self.pin('main', smoke=pin.Smoke(self.root, log=self.logs.append))
                error = raised.exception
                self.assertEqual((error.sha, error.step), (broken, 'tick --dry-run'))
                self.assertIn(f'smoke test of {broken[:12]} failed at tick --dry-run: '
                              'ImportError: broken on purpose; the copy is kept in', str(error))
                self.assertEqual(self.pointed(), old.resolve())
                self.assertEqual(error.copy, self.state / f'rejected-{broken[:12]}')
                self.assertTrue((error.copy / 'devloop' / '__main__.py').is_file())
                self.assertFalse((self.state / f'pinned-{broken[:12]}').exists())
                self.assertFalse(list(self.state.glob('.pinned-*')))
        # --no-smoke pins it all the same, and says so.
        copy = self.pin('main', smoke=False)
        self.assertEqual(copy.name, f'pinned-{broken[:12]}')
        self.assertIn(f'smoke test of {broken[:12]} skipped (--no-smoke)', self.logs)

    def test_a_healthy_copy_becomes_current_after_its_smoke_test(self):
        def smoke(copy, local):
            self.assertIsNone(self.pointed())  # the pointer moves only afterwards
            self.assertTrue((Path(copy) / 'devloop' / 'pin.py').is_file())
            self.smoked.append(Path(copy))
        copy = self.pin('main', smoke=smoke)
        self.assertEqual(self.pointed(), copy.resolve())
        self.assertEqual(len(self.smoked), 1)
        # A copy already pinned (the rollback) is not tested again.
        self.assertEqual(self.pin(self.first[:12], smoke=smoke), copy)
        self.assertEqual(len(self.smoked), 1)

    def test_a_transient_error_then_a_success_pins(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv[argv.index('-c') + 2:])
            if len(calls) == 1:
                return completed(1, stderr='Traceback ...\n' + pin.SMOKE_MARK + json.dumps(
                    {'types': ['GitHubError', 'RuntimeError'],
                     'message': 'gh pr list: error connecting to api.github.com'}))
            if 'watch' in argv:
                return completed(3, stdout='12:00 watch baseline: 1 open PRs, 2 open issues\n')
            return completed(0, stdout='[dry-run] open PRs: 1\n')
        copy = self.pin('main', smoke=pin.Smoke(self.root, log=self.logs.append, run=run))
        self.assertEqual(self.pointed(), copy.resolve())
        self.assertEqual([c[0] for c in calls], ['tick', 'tick', 'watch'])
        self.assertIn('smoke test: tick --dry-run failed with a transient error (GitHubError: gh '
                      'pr list: error connecting to api.github.com), trying once more', self.logs)
        self.assertEqual(self.logs[-1], f'pinned main at {self.first[:12]}: {copy}')


class AlertGH:
    def __init__(self, fail=False):
        self.comments, self.fail = [], fail

    def comment(self, number, body):
        if self.fail:
            raise RuntimeError('gh: HTTP 502 with a token ghs_secret')
        self.comments.append((number, body))


class AutoPinSmokeTests(GitCase):
    """An auto-pin whose copy fails the smoke test (#260): the request is dropped, ``current``
    stays, the tick plans from the running copy and does not fail, and one alert per commit."""

    def refusing_smoke(self, copy, local):
        self.smoked.append(Path(copy))
        raise pin.StepFailed('ImportError: broken on purpose', step='tick --dry-run')

    def dispatcher(self, gh, alert_issue=7):
        cfg = dataclasses.replace(CFG, pin=PinPolicy(auto=True),
                                  run=config.RunPolicy(alert_issue=alert_issue))
        d = Dispatcher(cfg, gh, FakeWS(), FakeRunner(), log=self.logs.append,
                       pinner=pin.AutoPin(self.root, self.state, log=self.logs.append,
                                          smoke=self.refusing_smoke))
        d.labels_checked = True  # the label rename of #248 is not under test
        self.snapshots = []
        d.snapshot = lambda: self.snapshots.append(1) or Snapshot([], [], [])
        return d

    def test_a_refused_copy_leaves_the_loop_on_the_previous_one(self):
        old = self.pin('main')
        new_sha = self.push('merged', 'devloop/extra.txt', 'new\n')
        pin.request(self.state, 100, new_sha)
        gh = AlertGH()
        d = self.dispatcher(gh)
        self.assertEqual(d.tick(), [])  # no Repinned, no PinError: planning went on
        self.assertEqual(self.snapshots, [1])
        self.assertEqual(self.pointed(), old.resolve())
        self.assertIsNone(pin.pending(self.state))  # the request is dropped
        self.assertTrue((self.state / f'rejected-{new_sha[:12]}').is_dir())
        self.assertTrue(any(line.startswith('auto-pin of origin/main refused, the pinned copy is '
                                            'unchanged and the request is dropped: smoke test of '
                                            f'{new_sha[:12]} failed at tick --dry-run: '
                                            'ImportError: broken on purpose')
                            for line in self.logs), self.logs)
        (number, body), = gh.comments
        self.assertEqual(number, 7)
        self.assertIn(f'`{new_sha[:12]}`', body)
        self.assertIn('`tick --dry-run`', body)
        self.assertNotIn('broken on purpose', body)  # the error's text stays in the local log
        self.assertNotIn(str(self.tmp), body)
        # The same commit refused again: no second alert.
        pin.request(self.state, 101, new_sha)
        self.assertEqual(d.tick(), [])
        self.assertEqual(len(gh.comments), 1)
        self.assertEqual(self.snapshots, [1, 1])
        self.assertIn(f'  the refusal of {new_sha[:12]} was already reported on #7', self.logs)
        # Another refused commit gets its own.
        other = self.push('merged again', 'devloop/extra.txt', 'newer\n')
        pin.request(self.state, 102, other)
        d.tick()
        self.assertEqual(len(gh.comments), 2)
        self.assertIn(f'`{other[:12]}`', gh.comments[1][1])

    def test_run_goes_on_without_counting_a_failed_tick(self):
        old = self.pin('main')
        new_sha = self.push('merged', 'devloop/extra.txt', 'new\n')
        pin.request(self.state, 100, new_sha)
        d = self.dispatcher(AlertGH())
        self.smoked.clear()
        ticks = []

        def tick():
            ticks.append(1)
            if len(ticks) > 2:
                raise KeyboardInterrupt
            return d.tick()

        def watch(seconds, sleep, check):
            return runloop.NOTHING_NEW
        loop = runloop.Loop(tick, watch, self.tmp / 'STOP', log=self.logs.append,
                            clock=lambda: 0, sleep=lambda s: None)
        with self.assertRaises(KeyboardInterrupt):
            loop.run()
        self.assertEqual(len(self.smoked), 1)  # tried once: the request is gone
        self.assertEqual(self.snapshots, [1, 1])
        self.assertFalse([line for line in self.logs if 'tick failed' in line], self.logs)
        self.assertEqual(self.pointed(), old.resolve())

    def test_without_an_alert_issue_or_with_a_failing_comment_it_only_logs(self):
        self.pin('main')
        new_sha = self.push('merged', 'devloop/extra.txt', 'new\n')
        pin.request(self.state, 100, new_sha)
        self.dispatcher(AlertGH(), alert_issue=0).tick()
        self.assertIn('  no [run].alert_issue is configured: no alert for the refused pin',
                      self.logs)
        pin.request(self.state, 100, new_sha)
        gh = AlertGH(fail=True)
        self.dispatcher(gh).tick()
        self.assertIn('  the alert of the refused pin on #7 could not be published '
                      '(RuntimeError)', self.logs)
        self.assertFalse(any('ghs_secret' in line for line in self.logs))
        # Not published: a later refusal of the same commit tries again.
        pin.request(self.state, 100, new_sha)
        gh = AlertGH()
        self.dispatcher(gh).tick()
        self.assertEqual(len(gh.comments), 1)

    def test_other_pin_errors_still_fail_the_tick(self):
        old = self.pin('main')
        self.push('broken', 'devloop/__init__.py', "raise ImportError('broken on purpose')\n")
        pin.request(self.state, 100, SHA1)
        with self.assertRaisesRegex(pin.PinError, 'auto-pin of origin/main failed'):
            self.dispatcher(AlertGH()).tick()
        self.assertEqual(pin.pending(self.state)['pr'], 100)
        self.assertEqual(self.pointed(), old.resolve())


class FakePinner:
    def __init__(self):
        self.requests, self.pins = [], []
        self.request_ = None

    def request(self, pr, head):
        self.requests.append((pr, head))

    def pending(self):
        return {'pr': self.requests[-1][0]} if self.requests else None

    def pin(self, ref):
        self.pins.append(ref)
        raise pin.Repinned('/new')


class MergeRequestTests(unittest.TestCase):
    """A merge by devloop requests the pin only with auto = true and a diff touching
    devloop/ or a list file."""

    def merge(self, auto, path):
        # Not a sensitive path here, so that one level-1 approval suffices for the merge.
        rp = dataclasses.replace(CFG.review_policy, sensitive_paths=())
        cfg = dataclasses.replace(CFG, merge=MergePolicy(mode='strict'), review_policy=rp,
                                  pin=PinPolicy(auto=auto))
        record = norm(raw_review(), bound=False)
        record['receipt'] = receipt(files=(path,), cfg=cfg)
        gh = MergeGH()
        gh.thread = []
        gh.say('brushtyler', markdown.render_review(record, header(REV_NAME)))
        ws = FakeWS()
        ws.diff = changes(path)
        pinner, logs = FakePinner(), []
        d = Dispatcher(cfg, gh, ws, FakeRunner(), log=logs.append, identities=Identities(gh),
                       pinner=pinner)
        d.tick()
        self.assertEqual(gh.merged, [(100, SHA1)])
        self.assertEqual(len(records(gh, 'merged')), 1)
        return d, pinner, logs

    def test_auto_requests_a_pin_after_a_merge_touching_devloop(self):
        for path in ('devloop/dispatcher.py', LIST):
            with self.subTest(path=path):
                d, pinner, logs = self.merge(True, path)
                self.assertEqual(pinner.requests, [(100, SHA1)])
                self.assertIn('  auto-pin of origin/main requested for the next tick', logs)
                self.assertEqual(pinner.pins, [])
                # The next tick pins before planning.
                with self.assertRaises(pin.Repinned):
                    d.tick()
                self.assertEqual(pinner.pins, ['origin/main'])

    def test_no_request_without_auto_or_outside_the_watched_paths(self):
        for auto, path in ((False, 'devloop/dispatcher.py'), (True, 'x.py'),
                           (True, 'docs/devloop.md')):
            with self.subTest(auto=auto, path=path):
                d, pinner, _ = self.merge(auto, path)
                self.assertEqual(pinner.requests, [])


class PinConfigTests(unittest.TestCase):
    def test_the_distributed_config_is_off(self):
        cfg = config.load_config()
        self.assertEqual(cfg.pin, PinPolicy(auto=False, paths=('devloop/', LIST)))
        self.assertEqual(DATA['pin'], {'auto': False, 'restart_max_wait_minutes': 30})
        self.assertEqual(cfg.pin.restart_max_wait_minutes, 30)
        self.assertEqual(config.parse_config({k: v for k, v in DATA.items() if k != 'pin'}).pin,
                         PinPolicy())

    def test_the_restart_wait_limit(self):
        # #301: whole minutes, 0 included (the drain at once).
        for value in (0, 5, 120):
            with self.subTest(value=value):
                cfg = config.parse_config(dict(DATA, pin={'restart_max_wait_minutes': value}))
                self.assertEqual(cfg.pin.restart_max_wait_minutes, value)

    def test_invalid_pin_tables_stop_devloop(self):
        for table in ({'auto': 'yes'}, {'auto': 1}, {'every': True}, 'on',
                      {'restart_max_wait_minutes': -1}, {'restart_max_wait_minutes': 1.5},
                      {'restart_max_wait_minutes': True}, {'restart_max_wait_minutes': '30'}):
            with self.subTest(table=table):
                with self.assertRaises(ConfigError):
                    config.parse_config(dict(DATA, pin=table))

    def test_touches(self):
        policy = PinPolicy(auto=True, paths=('devloop/', LIST))
        self.assertTrue(policy.touches(['docs/x.md', 'devloop/pin.py']))
        self.assertTrue(policy.touches([LIST]))
        self.assertFalse(policy.touches(['devloopx/a.py', 'schemas/other.json', 'README.md']))


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.local = self.tmp / 'config.local.toml'

    def load(self, text):
        self.local.write_text(text)
        return config.load_config(None, self.local)

    def test_only_the_keys_set_change(self):
        cfg = self.load('preflight = "codex"\nmax_open_prs = 5\n'
                        '[merge]\nmode = "strict"\n[merge.levels]\n"2" = "off"\n'
                        '[agents.codex]\neffort = "xhigh"\n[pin]\nauto = true\n')
        base = config.load_config()
        self.assertEqual((cfg.preflight, cfg.max_open_prs), ('codex', 5))
        self.assertEqual(cfg.merge, MergePolicy(mode='strict', levels=((2, 'off'),),
                                                operator_only_paths=base.merge.operator_only_paths))
        self.assertEqual(cfg.agent('codex').effort, 'xhigh')
        self.assertEqual(cfg.agent('codex').model, base.agent('codex').model)
        self.assertTrue(cfg.pin.auto)
        self.assertEqual((cfg.max_review_rounds, cfg.repo, cfg.reviewers),
                         (base.max_review_rounds, base.repo, base.reviewers))
        # A list replaces the one below.
        self.assertEqual(self.load('blocked_labels = ["x"]\n').blocked_labels, ('x',))

    def test_unknown_keys_are_refused(self):
        for text, key in (('bogus = 1\n', 'bogus'), ('[merge]\nmethod = "squash"\n', 'merge.method'),
                          ('[agents.codex]\ntemperature = 1\n', 'agents.codex.temperature'),
                          ('[review_policy.zero]\nlabels = []\n', 'review_policy.zero.labels'),
                          ('[pin]\nevery = 1\n', 'pin.every'), ('[extra]\na = 1\n', 'extra')):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ConfigError, f'unknown key {key!r}'):
                    self.load(text)
        with self.assertRaisesRegex(ConfigError, 'merge must be a table'):
            self.load('merge = "strict"\n')
        with self.assertRaisesRegex(ConfigError, 'local config'):
            self.load('not toml = = 1\n')

    def test_the_overlay_is_validated_like_the_config(self):
        with self.assertRaisesRegex(ConfigError, 'merge.mode'):
            self.load('[merge]\nmode = "yes"\n')


class LauncherTests(unittest.TestCase):
    """``scripts/devloop-pinned`` in a clone of its own, whose checkout holds a devloop that must
    never run."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / 'clone'
        (self.root / 'scripts').mkdir(parents=True)
        shutil.copy(LAUNCHER, self.root / 'scripts' / 'devloop-pinned')
        self.write(self.root / 'devloop' / '__main__.py', 'raise SystemExit("the checkout ran")\n')
        self.write(self.root / 'devloop' / '__init__.py', '')
        self.state = self.root / '.ssdlc' / 'devloop'
        self.copy = self.state / 'pinned-0123456789ab'

    @staticmethod
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def fake_copy(self):
        self.write(self.copy / 'devloop' / '__init__.py', '')
        self.write(self.copy / 'devloop' / '__main__.py', (
            'import json, os, sys\nimport devloop\n'
            'print(json.dumps({"argv": sys.argv[1:], "package": devloop.__file__, '
            '"cwd": os.getcwd(), "path": os.environ.get("PYTHONPATH")}))\n'))
        os.symlink(self.copy.name, self.state / 'current')

    def launch(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith('PYTHON')}
        return subprocess.run([sys.executable, str(self.root / 'scripts' / 'devloop-pinned'),
                               *args], cwd=self.tmp, env=env, capture_output=True, text=True,
                              timeout=120)

    def test_it_runs_the_pointed_copy_with_the_local_config(self):
        self.fake_copy()
        proc = self.launch('status')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        seen = json.loads(proc.stdout)
        self.assertEqual(seen, {'argv': ['status'],
                                'package': str(self.copy.resolve() / 'devloop' / '__init__.py'),
                                'cwd': str(self.root.resolve()), 'path': str(self.copy.resolve())})
        local = self.state / 'config.local.toml'
        local.write_text('max_open_prs = 4\n')
        seen = json.loads(self.launch('tick', '--dry-run').stdout)
        self.assertEqual(seen['argv'], ['--local-config', str(local.resolve()), 'tick',
                                        '--dry-run'])

    def test_without_a_pin_it_stops(self):
        proc = self.launch('status')
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('no pinned copy', proc.stderr)
        self.assertNotIn('the checkout ran', proc.stderr)

    def test_the_real_package_refuses_an_unknown_key_of_the_overlay(self):
        copy_package(self.copy)
        os.symlink(self.copy.name, self.state / 'current')
        local = self.state / 'config.local.toml'
        local.write_text('max_open_prs = 4\n[merge]\nmethod = "squash"\n')
        proc = self.launch('follow', 'issue-1')
        self.assertEqual(proc.returncode, 1)
        self.assertIn("devloop: invalid config: local config: unknown key 'merge.method'",
                      proc.stderr)
        local.write_text('max_open_prs = 0\n')  # the overlay reaches the copy's config
        proc = self.launch('follow', 'issue-1')
        self.assertIn('devloop: invalid config: max_open_prs: out of range', proc.stderr)

    def test_root_needs_allow_root(self):
        self.fake_copy()
        launcher = load_launcher()
        calls = []
        execve = lambda *a: calls.append(a)  # noqa: E731
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        with self.assertRaises(SystemExit) as raised:
            launcher.main(['run'], euid=0, execve=execve, root=self.root)
        self.assertIn('refusing to run as root', str(raised.exception.code))
        self.assertIn('--allow-root', str(raised.exception.code))
        self.assertEqual(calls, [])
        launcher.main(['--allow-root', 'run'], euid=0, execve=execve, root=self.root)
        launcher.main(['run'], euid=1000, execve=execve, root=self.root)
        self.assertEqual([c[1][1:] for c in calls], [['-P', '-m', 'devloop', 'run']] * 2)
        self.assertEqual(calls[0][2]['PYTHONPATH'], str(self.copy.resolve()))

    def test_it_disables_the_cli_autoupdater_unless_the_operator_set_it(self):
        # A CLI that updates itself mid-tick leaves no binary for a turn to start (#218).
        self.fake_copy()
        launcher = load_launcher()
        with mock.patch.dict(os.environ):
            os.environ.pop('DISABLE_AUTOUPDATER', None)
            _, env = launcher.command(self.root, ['run'])
            self.assertEqual(env['DISABLE_AUTOUPDATER'], '1')
            os.environ['DISABLE_AUTOUPDATER'] = '0'
            _, env = launcher.command(self.root, ['run'])
            self.assertEqual(env['DISABLE_AUTOUPDATER'], '0')

    def test_the_installation_recommends_cli_without_automatic_updates(self):
        text = (REPO / 'docs' / 'devloop' / 'guide.md').read_text(encoding='utf-8')
        start = text.index('#### Installation as a dedicated user')
        section = ' '.join(text[start:text.index('#### Pin\n', start)].split())
        self.assertIn('disable their automatic updates: for Claude Code, '
                      '`DISABLE_AUTOUPDATER=1` in the launcher\'s environment', section)
        # #243: every agent CLI belongs to the devloop user, Claude Code included.
        self.assertIn('install them in the home of the `devloop` user, Claude Code included',
                      section)

    def installation(self):
        """The guide from "Installation as a dedicated user" up to "Pin", whitespace joined."""
        text = (REPO / 'docs' / 'devloop' / 'guide.md').read_text(encoding='utf-8')
        start = text.index('#### Installation as a dedicated user')
        return text[start:text.index('#### Pin\n', start)]

    def subsection(self, heading):
        section = self.installation()
        start = section.index(f'#### {heading}\n')
        end = section.find('\n#### ', start + 1)
        return ' '.join(section[start:end if end >= 0 else len(section)].split())

    def test_the_installation_path_goes_from_the_user_to_the_rollback(self):
        # #243 (ADR 0003, I5): one path, in order: user, keys, CLIs, overlay, pin, start, rollback.
        section = self.subsection('Installation as a dedicated user')
        steps = ('**User and clone.**', '**Keys.**', '**Agent CLIs.**', '**Overlay.**',
                 '**Pin.**', '**Checks.**', '**Start.**', '**Rollback.**')
        positions = [section.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))
        for needle in ('sudo useradd --create-home devloop', 'install -d -m 700 /home/devloop/.ghapps',
                       '<slug>.<date>.private-key.pem', 'mode `600`', 'config.local.toml',
                       '`[pin] auto`', '`[run] alert_issue`', '(#rollback)',
                       'devloop-pinned apps', 'devloop-pinned tick --dry-run'):
            with self.subTest(needle=needle):
                self.assertIn(needle, section)

    def test_the_first_pin_is_the_one_the_launcher_asks_for(self):
        # The launcher without a pin names the command the installation runs first (#174).
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        with self.assertRaises(SystemExit) as raised:
            load_launcher().command(tmp, ['run'])
        message = str(raised.exception.code)
        command = message[message.index('`') + 1:message.rindex('`')]
        self.assertEqual(command, 'python3 -m devloop pin origin/main')
        section = self.subsection('Installation as a dedicated user')
        self.assertIn(f"sudo -u devloop -H sh -c 'cd ~/ssdlc && {command}'", section)
        # Every later command runs the pinned copy as the devloop user, never as root.
        self.assertIn('sudo -u devloop -H python3 /home/devloop/ssdlc/scripts/devloop-pinned',
                      section)
        self.assertNotIn('--allow-root', self.installation())

    def test_the_agent_clis_belong_to_the_devloop_user(self):
        section = self.subsection('Agent CLIs')
        for needle in ('`~/.local/bin`',
                       'npm install -g --prefix /home/devloop/.local @anthropic-ai/claude-code',
                       '"autoUpdates": false', '"DISABLE_AUTOUPDATER": "1"',
                       '`~/.claude/settings.json`', '**codex** and **agy**',
                       '**Updating them by hand, with the loop stopped**', '`STOP` file',
                       'remove `STOP` and start `devloop run` again'):
            with self.subTest(needle=needle):
                self.assertIn(needle, section)

    def test_starting_and_stopping_the_loop_is_documented(self):
        section = self.subsection('Starting and stopping the loop')
        for needle in ('PYTHONUNBUFFERED=1 nohup python3 scripts/devloop-pinned run',
                       '**Unbuffered log.**', 'touch /home/devloop/ssdlc/.ssdlc/devloop/STOP',
                       'rm /home/devloop/ssdlc/.ssdlc/devloop/STOP', '**`[pin] auto`.**',
                       '**`[run] alert_issue`.**', 'exit code 4'):
            with self.subTest(needle=needle):
                self.assertIn(needle, section)
        # The process the launcher starts keeps the operator's environment, PYTHONUNBUFFERED too.
        self.fake_copy()
        with mock.patch.dict(os.environ, {'PYTHONUNBUFFERED': '1'}):
            _, env = load_launcher().command(self.root, ['run'])
        self.assertEqual(env['PYTHONUNBUFFERED'], '1')

    def test_the_start_command_finds_the_clis_in_the_home_of_the_devloop_user(self):
        # `sh -c` under sudo is not a login shell: the command itself must put ~/.local/bin on
        # the PATH, the same as the unit, or the agents' CLIs are not found (#243).
        text = (REPO / 'docs' / 'devloop' / 'guide.md').read_text(encoding='utf-8')
        unit = next(line.removeprefix('Environment=PATH=') for line in text.splitlines()
                    if line.startswith('Environment=PATH='))
        line = next(line for line in self.installation().splitlines()
                    if 'nohup python3 scripts/devloop-pinned run' in line)
        argv = shlex.split(line)
        self.assertEqual(argv[:6], ['sudo', '-u', 'devloop', '-H', 'sh', '-c'])
        script = argv[6]
        self.assertIn(f'PATH={unit} ', script)
        self.assertTrue(script.endswith(' &'))
        # The documented command, in the foreground, for a home of its own and with the PATH
        # of a sudo that leaves out ~/.local/bin.
        home = self.tmp / 'home'
        root = home / 'ssdlc'
        (root / 'scripts').mkdir(parents=True)
        shutil.copy(LAUNCHER, root / 'scripts' / 'devloop-pinned')
        state = root / '.ssdlc' / 'devloop'
        copy = state / 'pinned-0123456789ab'
        self.write(copy / 'devloop' / '__init__.py', '')
        self.write(copy / 'devloop' / '__main__.py', (
            'import json, os, shutil\n'
            'print(json.dumps({"claude": shutil.which("claude"), '
            '"unbuffered": os.environ.get("PYTHONUNBUFFERED")}))\n'))
        os.symlink(copy.name, state / 'current')
        bin_dir = home / '.local' / 'bin'
        self.write(bin_dir / 'claude', '#!/bin/sh\necho fake\n')
        (bin_dir / 'claude').chmod(0o755)
        os.symlink(sys.executable, bin_dir / 'python3')
        minimal = {'PATH': '/usr/bin:/bin', 'HOME': str(home)}
        self.assertIsNone(shutil.which('claude', path=minimal['PATH']))
        foreground = script[:-len(' &')].replace('/home/devloop', str(home))
        proc = subprocess.run(['/bin/sh', '-c', foreground], env=minimal, cwd=self.tmp,
                              capture_output=True, text=True, timeout=120)
        log = (state / 'run.log').read_text()
        self.assertEqual(proc.returncode, 0, log)
        seen = json.loads(log.splitlines()[-1])
        self.assertEqual(seen, {'claude': str(bin_dir / 'claude'), 'unbuffered': '1'})


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.lock = self.tmp / 'dispatcher.lock'

    def test_a_repin_restarts_with_the_same_arguments_and_the_lock(self):
        dispatcher = SimpleNamespace(tick=mock.Mock(side_effect=pin.Repinned(self.tmp / 'new')))
        cfg = SimpleNamespace()
        with mock.patch.object(cli, 'build', return_value=(cfg, self.tmp, dispatcher)), \
                mock.patch.object(cli, 'log'), \
                mock.patch.object(cli.pin, 'reexec') as reexec:
            cli.main(['--local-config', 'x.toml', 'tick'])
        copy, argv, fd, var = reexec.call_args.args
        self.assertEqual((copy, argv, var), (self.tmp / 'new', ['--local-config', 'x.toml',
                                                                'tick'], 'DEVLOOP_LOCK_FD'))
        self.assertIsInstance(fd, int)

    def test_a_failed_pin_stops_the_tick_command(self):
        dispatcher = SimpleNamespace(tick=mock.Mock(side_effect=pin.PinError('auto-pin failed')))
        with mock.patch.object(cli, 'build', return_value=(None, self.tmp, dispatcher)), \
                self.assertRaises(SystemExit) as raised:
            cli.main(['tick'])
        self.assertEqual(raised.exception.code, 'devloop: auto-pin failed')

    def test_a_dry_run_tick_runs_while_a_dispatcher_holds_the_lock(self):
        # The smoke test of a pin (#260) runs `tick --dry-run` while `run` holds the lock.
        dispatcher = SimpleNamespace(tick=mock.Mock(return_value=[]), ws=mock.Mock())
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(cli.LOCK_FD, None)  # no lock passed on by a restart
        with open(self.lock, 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(cli, 'build', return_value=(None, self.tmp, dispatcher)), \
                    mock.patch.object(cli, 'log'):
                cli.main(['tick', '--dry-run'])
                dispatcher.tick.assert_called_once_with(dry_run=True)
                dispatcher.ws.read_only.assert_called_once()
                with self.assertRaisesRegex(SystemExit, 'another dispatcher'):
                    cli.main(['tick'])

    def test_no_smoke_reaches_the_pin(self):
        cfg = SimpleNamespace(state_dir='.ssdlc/devloop')
        with mock.patch.object(cli, 'load_config', return_value=cfg), \
                mock.patch.object(cli, 'main_checkout', return_value=self.tmp), \
                mock.patch.object(cli.pin, 'pin') as pinned:
            cli.main(['pin', '--no-smoke', 'main'])
            self.assertIs(pinned.call_args.kwargs['smoke'], False)
            cli.main(['pin', 'main'])
            self.assertIs(pinned.call_args.kwargs['smoke'], True)

    def test_reexec_passes_the_lock_open(self):
        calls = []
        with open(self.lock, 'w') as fh:
            fd = fh.fileno()
            pin.reexec(self.tmp / 'new', ['run'], fd, 'LOCK_VAR',
                       execve=lambda *a: calls.append(a))
            self.assertTrue(os.get_inheritable(fd))
        (exe, args, env), = calls
        self.assertEqual((exe, args), (sys.executable, [sys.executable, '-P', '-m', 'devloop',
                                                         'run']))
        self.assertEqual((env['PYTHONPATH'], env['LOCK_VAR']), (str(self.tmp / 'new'), str(fd)))

    def test_the_new_process_keeps_the_inherited_lock(self):
        with open(self.lock, 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd = os.dup(held.fileno())
            with mock.patch.dict(os.environ, {cli.LOCK_FD: str(fd)}):
                with cli.single_instance(self.lock):
                    self.assertNotIn(cli.LOCK_FD, os.environ)
                    self.assertFalse(os.get_inheritable(fd))
            # Without the inherited descriptor the lock is someone else's.
            with self.assertRaisesRegex(SystemExit, 'another dispatcher'):
                with cli.single_instance(self.lock):
                    pass

    def test_a_descriptor_of_another_file_is_not_the_lock(self):
        other = self.tmp / 'other'
        with open(other, 'w') as fh:
            with mock.patch.dict(os.environ, {cli.LOCK_FD: str(fh.fileno())}):
                self.assertIsNone(cli._inherited_lock(self.lock.with_name('missing')))
                self.lock.touch()
                self.assertIsNone(cli._inherited_lock(self.lock))

    def test_agents_do_not_inherit_the_pinned_copy(self):
        with mock.patch.dict(os.environ, {'PYTHONPATH': '/pinned'}):
            env = cli.agent_environment(config.load_config(), self.tmp)
        self.assertNotIn('PYTHONPATH', env)


class DryRunTests(GitCase):
    """``tick --dry-run`` runs without the lock (#260): with a real Workspace and a PR ready to
    merge, it writes nothing to the repository of the dispatcher that holds the lock."""

    BRANCH = 'agent/issue-7'
    TRACKING = f'refs/remotes/origin/{BRANCH}'

    def setUp(self):
        super().setUp()
        git(self.root, 'checkout', '--quiet', '-b', self.BRANCH)
        (self.root / 'x.py').write_text('x\n')
        self.head = self.commit('pr')
        git(self.root, 'push', '--quiet', 'origin', self.BRANCH)
        git(self.root, 'checkout', '--quiet', 'main')
        self.state.mkdir(parents=True)

    def dispatcher(self):
        """PR #100, ready to merge in mode strict at ``self.head``, approved at level 1."""
        rp = dataclasses.replace(CFG.review_policy, sensitive_paths=())
        cfg = dataclasses.replace(CFG, merge=MergePolicy(mode='strict'), review_policy=rp)
        record = norm(raw_review(), head=self.head, bound=False)
        record['receipt'] = receipt(head=self.head, base=self.first, files=('x.py',), cfg=cfg)
        gh = MergeGH()
        gh.head = self.head
        gh.thread = []
        gh.say('brushtyler', markdown.render_review(record, header(REV_NAME)))
        ws = Workspace(self.root, cfg.state_dir, cfg.base_branch)
        return Dispatcher(cfg, gh, ws, FakeRunner(), log=self.logs.append,
                          identities=Identities(gh))

    def files(self):
        """Every file of the repository's git directory, with its content."""
        git_dir = self.root / '.git'
        return {str(p.relative_to(git_dir)): p.read_bytes()
                for p in sorted(git_dir.rglob('*')) if p.is_file()}

    def dry_run(self):
        dispatcher = self.dispatcher()
        before = self.files()
        with mock.patch.dict(os.environ), \
                open(self.state / 'dispatcher.lock', 'w') as held, \
                mock.patch.object(cli, 'build', return_value=(None, self.state, dispatcher)), \
                mock.patch.object(cli, 'log'):
            os.environ.pop(cli.LOCK_FD, None)
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            cli.main(['tick', '--dry-run'])
        self.assertEqual(self.files(), before)
        return dispatcher

    def test_a_ready_pr_is_planned_from_the_refs_last_fetched_without_writes(self):
        self.dry_run()
        self.assertIn('[dry-run] merge #100: merge mode strict', self.logs)

    def test_a_branch_never_fetched_is_not_fetched(self):
        git(self.root, 'update-ref', '-d', self.TRACKING)
        self.dry_run()
        self.assertTrue(any('merge mode of #100 not computable' in line for line in self.logs))
        self.assertNotIn('[dry-run] merge #100: merge mode strict', self.logs)
        self.assertEqual(subprocess.run(['git', 'rev-parse', '--verify', '-q', self.TRACKING],
                                        cwd=self.root, capture_output=True).returncode, 1)

    def test_merge_tree_writes_out_of_the_repository_and_writing_commands_are_refused(self):
        ws = Workspace(self.root, '.ssdlc/devloop', 'main')
        (self.root / 'NOTES.md').write_text('main moved on\n')
        base = self.commit('main')  # the merged tree is new: git has to write it
        before = self.files()
        with tempfile.TemporaryDirectory() as objects:
            ws.read_only(objects)
            tree, clean, conflicts = ws._merge_tree(base, self.head)
            self.assertEqual((clean, conflicts), (True, []))
            self.assertTrue(any(Path(objects).rglob('*')))
            self.assertEqual(ws._git('cat-file', '-t', tree).strip(), 'tree')
            # The fetch is skipped: the base is the one last fetched.
            self.assertEqual(ws.base_head(), self.first)
            for write in (lambda: ws.base_copy('copy'), lambda: ws.new_branch('w', 'b'),
                          lambda: ws.remove('copy')):
                with self.assertRaisesRegex(WorkspaceError, 'read-only'):
                    write()
        self.assertEqual(self.files(), before)
        self.assertFalse(ws.root.exists())


class DocsTests(unittest.TestCase):
    def test_the_installation_is_documented(self):
        text = (REPO / 'docs' / 'devloop' / 'guide.md').read_text()
        for needle in ('python3 -m devloop pin', 'scripts/devloop-pinned', '--allow-root',
                       'config.local.toml', 'sudo -u devloop -H', 'Rollback', '[pin]'):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_the_smoke_test_is_documented(self):
        # #260: the smoke test of the pinned installation and --no-smoke.
        text = (REPO / 'docs' / 'devloop' / 'guide.md').read_text()
        start = text.index('#### Pin\n')
        section = ' '.join(text[start:text.index('#### Launcher', start)].split())
        for needle in ('runs the **smoke test** of a new copy', '`python3 -m devloop tick '
                       '--dry-run`', 'one round of `python3 -m devloop watch`',
                       'at most 30 seconds', '`.ssdlc/devloop/rejected-<sha12>`',
                       'runs once more before the copy is refused', '`pin --no-smoke <ref>`',
                       'smoke test of <sha12> skipped (--no-smoke)'):
            with self.subTest(needle=needle):
                self.assertIn(needle, section)
        start = text.index('#### Pin after a merge')
        section = ' '.join(text[start:text.index('## Limits', start)].split())
        for needle in ('A copy refused by the [smoke test](#pin) is not a failed tick',
                       'the request is dropped', 'once per refused commit'):
            with self.subTest(needle=needle):
                self.assertIn(needle, section)


if __name__ == '__main__':
    unittest.main()
