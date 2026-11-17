"""devloop: pinned installation, launcher, local config as overlay and pin after a merge (#174)."""

import dataclasses
import fcntl
import importlib.machinery
import importlib.util
import json
import os
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
from devloop import config, markdown, pin
from devloop.config import ConfigError, MergePolicy, PinPolicy
from devloop.dispatcher import Dispatcher, run_forever
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
        self.assertIn('docs/devloop.md', DOCUMENTS)
        tree = git(self.root, 'ls-tree', '-r', '--name-only', self.first).splitlines()
        self.assertFalse(set(DOCUMENTS) & set(tree))
        copy = self.pin('main')
        self.assertEqual(self.pointed(), copy.resolve())
        self.assertFalse((copy / 'docs').exists())
        second = self.push('a mapped document', 'docs/devloop.md', 'devloop\n')
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

    def test_the_command_line(self):
        with mock.patch.object(cli, 'main_checkout', return_value=self.root), \
                mock.patch.object(cli, 'log', self.logs.append):
            cli.main(['pin', 'main'])
            self.assertEqual(self.pointed().name, f'pinned-{self.first[:12]}')
            with self.assertRaises(SystemExit) as raised:
                cli.main(['pin', 'nothing-here'])
        self.assertIn('pin failed, the pinned copy is unchanged', str(raised.exception.code))


class AutoPinTests(GitCase):
    """The pin after a merge, through the real repository and a dispatcher whose tick stops at
    the pin."""

    def dispatcher(self, auto):
        cfg = dataclasses.replace(CFG, pin=PinPolicy(auto=auto))
        snapshots = []
        d = Dispatcher(cfg, None, FakeWS(), FakeRunner(), log=self.logs.append,
                       pinner=pin.AutoPin(self.root, self.state, log=self.logs.append))
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
        # `run` logs it and goes on with the next tick.
        d.tick = mock.Mock(side_effect=[pin.PinError('auto-pin failed'), KeyboardInterrupt])
        with self.assertRaises(KeyboardInterrupt):
            run_forever(d, 1, sleep=lambda s: None)
        self.assertIn('tick failed: auto-pin failed', self.logs)


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
        self.assertEqual(DATA['pin'], {'auto': False})
        self.assertEqual(config.parse_config({k: v for k, v in DATA.items() if k != 'pin'}).pin,
                         PinPolicy())

    def test_invalid_pin_tables_stop_devloop(self):
        for table in ({'auto': 'yes'}, {'auto': 1}, {'every': True}, 'on'):
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


class DocsTests(unittest.TestCase):
    def test_the_installation_is_documented(self):
        text = (REPO / 'docs' / 'devloop.md').read_text()
        for needle in ('python3 -m devloop pin', 'scripts/devloop-pinned', '--allow-root',
                       'config.local.toml', 'sudo -u devloop -H', 'Rollback', '[pin]'):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)


if __name__ == '__main__':
    unittest.main()
