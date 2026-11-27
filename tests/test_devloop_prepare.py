"""The step that prepares a worktree (``[prepare]``, #696).

With ``[prepare] cmd`` the dispatcher installs a worktree's dependencies before its first turn or
test, and again when one of ``[prepare] inputs`` changed; it runs as the tests run (the same user
and environment, ``test_timeout_seconds``), with a cache of the project outside the worktrees and
the ``state_dir``, and a ``verification`` record of kind ``prepare``. A failure is a critical
escalation to the operator, ``prepare_failed``. The check of the PR worktree after a review admits
only the ignored ``[prepare] outputs``. Without ``[prepare] cmd`` nothing changes.
"""

import dataclasses
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

from devloop import config, markdown
from devloop.dispatcher import Dispatcher
from devloop.ledger import Ledger
from devloop.localtests import Prepare, prepared_output
from devloop.planning import Action
from devloop.supervision import PREPARE_FAILED, critical
from devloop.workspace import PREPARE_CACHE_ENV, Workspace

from helpers import ssdlc_project
from test_devloop import CFG, FakeRunner, finding, fixed, implemented, norm, pr, raw_review
from test_devloop_ledger import LedgerGH, TreeWS
from test_devloop_quickstart import example

CFG_DATA = ssdlc_project.data()

NPM = Prepare(command='npm ci --ignore-scripts --cache "$DEVLOOP_PREPARE_CACHE"',
              inputs=('package-lock.json',), outputs=('node_modules/',))


def git(*args, cwd):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True)


class PrepareWS(TreeWS):
    """A FakeWS with the step that prepares a worktree: ``inputs`` is the content of the inputs
    of every worktree, ``results`` the outcome of each run of the step (then the last one), and
    ``events`` what ran, in order, with the worktree: the step, the tests and the turns."""

    def __init__(self, results=(True,), **kw):
        super().__init__(**kw)
        self.inputs = 'lock v1'
        self.results = list(results)
        self.events = []
        self.stamps = {}
        self.runs = []
        self.outputs_checked = []

    def prepare_stamp(self, path, command, inputs):
        return f'{command}|{inputs}|{self.inputs}'

    def prepared(self, path):
        return self.stamps.get(str(path))

    def mark_prepared(self, path, stamp):
        self.stamps[str(path)] = stamp

    def prepare_cache(self, project):
        return Path('/agent-home/.cache/devloop-prepare') / project.replace('/', '_')

    def run_prepare(self, path, command, timeout, cache):
        self.events.append(('prepare', Path(path).name))
        self.runs.append((command, timeout, cache))
        passed = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        return passed, 'npm ERR! code E404'

    def run_tests(self, path, command, timeout):
        self.events.append(('test', Path(path).name))
        return super().run_tests(path, command, timeout)

    def pristine(self, path, head, outputs=()):
        self.outputs_checked.append(tuple(outputs))
        return super().pristine(path, head, outputs)


class EventRunner(FakeRunner):
    """A FakeRunner that records its turns in the events of ``ws``; ``during``: a function of
    the turn's number (1 first) run inside the turn, as what the agent does."""

    def __init__(self, ws, *outputs, during=None):
        super().__init__(*outputs)
        self.ws, self.during, self.turns = ws, during, 0

    def run(self, agent, mode, prompt, schema_name, workdir, label):
        self.turns += 1
        self.ws.events.append(('turn', Path(workdir).name))
        if self.during:
            self.during(self.turns)
        return super().run(agent, mode, prompt, schema_name, workdir, label)


class ConfigTests(unittest.TestCase):
    def data(self, **keys):
        return dict({k: v for k, v in CFG_DATA.items() if k != 'prepare'}, **keys)

    def test_without_the_table_nothing_is_prepared(self):
        self.assertEqual(config.Config.prepare, Prepare())
        self.assertEqual(CFG.prepare, Prepare())
        self.assertNotIn('prepare', config.read_toml(config.DEFAULT_CONFIG))
        self.assertEqual(config.parse_config(self.data()).prepare, Prepare())

    def test_the_table_is_read(self):
        cfg = config.parse_config(self.data(prepare={
            'cmd': ' npm ci --ignore-scripts ', 'inputs': ['package-lock.json', 'a/b.lock'],
            'outputs': ['node_modules/', 'packages/*/node_modules/', '.tool-versions']}))
        self.assertEqual(cfg.prepare, Prepare(
            command='npm ci --ignore-scripts', inputs=('package-lock.json', 'a/b.lock'),
            outputs=('node_modules/', 'packages/*/node_modules/', '.tool-versions')))

    def test_a_wrong_table_stops_devloop(self):
        for bad, message in (
                ({'command': 'x'}, 'unknown keys'),
                ({'cmd': 1}, 'prepare.cmd: expected a string'),
                ({'inputs': ['package-lock.json']}, 'need prepare.cmd'),
                ({'cmd': ' ', 'outputs': ['node_modules/']}, 'need prepare.cmd'),
                ({'cmd': 'x', 'inputs': 'package-lock.json'}, 'expected a list of strings'),
                ({'cmd': 'x', 'inputs': ['/etc/passwd']}, 'not the relative path'),
                ({'cmd': 'x', 'inputs': ['../x.lock']}, 'not the relative path'),
                ({'cmd': 'x', 'inputs': ['locks/']}, 'not the relative path'),
                ({'cmd': 'x', 'inputs': ['*.lock']}, 'not the relative path'),
                ({'cmd': 'x', 'outputs': ['/node_modules/']}, 'not a relative path'),
                ({'cmd': 'x', 'outputs': ['a/../node_modules/']}, 'not a relative path'),
                ({'cmd': 'x', 'outputs': ['./']}, 'not a relative path'),
                ({'cmd': 'x', 'outputs': ['/']}, 'not a relative path'),
                ({'cmd': 'x', 'outputs': ['node_[m]odules/']}, 'not a relative path'),
                ({'cmd': 'x', 'outputs': ['node modules/']}, 'not a relative path')):
            with self.subTest(bad=bad), self.assertRaisesRegex(config.ConfigError, message):
                config.parse_config(self.data(prepare=bad))

    def test_a_fact_of_the_project_file_that_the_overlay_replaces_key_by_key(self):
        project = {'test_cmd': 'npm test',
                   'prepare': {'cmd': 'npm ci', 'inputs': ['package-lock.json'],
                               'outputs': ['node_modules/']}}
        data = config.compose(CFG_DATA, project, {'prepare': {'cmd': 'npm ci --ignore-scripts'}})
        self.assertEqual(config.parse_config(data).prepare,
                         Prepare(command='npm ci --ignore-scripts',
                                 inputs=('package-lock.json',), outputs=('node_modules/',)))
        # A trust key (ADR 0006, K1): its changes are recorded in the journal.
        self.assertIn('prepare', config.TRUST_KEYS)
        self.assertEqual(config.trust_values(data)['prepare.cmd'], 'npm ci --ignore-scripts')

    def test_the_quick_start_prepares_the_npm_project(self):
        cfg = config.parse_config(config.compose(
            CFG_DATA, tomllib.loads(example('npm project.toml'))))
        self.assertEqual(cfg.prepare.inputs, ('package-lock.json',))
        self.assertEqual(cfg.prepare.outputs, ('node_modules/',))
        self.assertIn('--ignore-scripts', cfg.prepare.command)
        self.assertIn('$DEVLOOP_PREPARE_CACHE', cfg.prepare.command)


class OutputsTests(unittest.TestCase):
    def test_a_path_under_a_listed_directory(self):
        outputs = ('node_modules/', 'packages/*/node_modules/', '.cache/x.json')
        for name in ('node_modules/', 'node_modules/a/index.js', 'packages/ui/node_modules/x',
                     '.cache/x.json'):
            with self.subTest(name=name):
                self.assertTrue(prepared_output(name, outputs))
        for name in ('src/node_modules/x', 'node_modulesx/a', 'packages/node_modules/x',
                     'packages/a/b/node_modules/x', 'packages/ui/x', '.cache/y.json',
                     '.cache/x.json/', 'x.js'):
            with self.subTest(name=name):
                self.assertFalse(prepared_output(name, outputs))
        self.assertFalse(prepared_output('node_modules/a', ()))


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / 'repo'
        self.root.mkdir()
        self.home = Path(tmp.name) / 'home'
        self.home.mkdir()
        (self.root / '.gitignore').write_text('node_modules/\nbuild/\n.state/\n')
        (self.root / 'package-lock.json').write_text('{"v": 1}\n')
        (self.root / 'node_modules').mkdir()
        git('init', '-q', cwd=self.root)
        git('add', '-A', cwd=self.root)
        (self.root / 'node_modules' / 'tracked.js').write_text('tracked\n')
        git('add', '-f', 'node_modules/tracked.js', cwd=self.root)
        git('-c', 'user.name=t', '-c', 'user.email=t@x', 'commit', '-q', '-m', 'c',
            cwd=self.root)
        env = dict(os.environ, HOME=str(self.home))
        self.ws = Workspace(self.root, '.state', 'main', test_env=env)
        self.head = self.ws.head(self.root)

    def test_the_check_after_a_review_admits_only_the_ignored_outputs(self):
        ws, root, head = self.ws, self.root, self.head
        outputs = ('node_modules/',)
        self.assertTrue(ws.pristine(root, head, outputs))
        (root / 'node_modules' / 'left-pad').mkdir()
        (root / 'node_modules' / 'left-pad' / 'index.js').write_text('x\n')
        self.assertTrue(ws.pristine(root, head, outputs))
        # Without outputs, as before #696: an ignored file is a change.
        self.assertFalse(ws.pristine(root, head))
        # An ignored file elsewhere, not listed.
        (root / 'build').mkdir()
        (root / 'build' / 'out.js').write_text('x\n')
        self.assertFalse(ws.pristine(root, head, outputs))
        (root / 'build' / 'out.js').unlink()
        self.assertTrue(ws.pristine(root, head, outputs))
        # A tracked file under the outputs, changed: the diff stays checked.
        (root / 'node_modules' / 'tracked.js').write_text('changed\n')
        self.assertFalse(ws.pristine(root, head, outputs))
        git('checkout', '--', 'node_modules/tracked.js', cwd=root)
        self.assertTrue(ws.pristine(root, head, outputs))
        # A new file git does not ignore, even under a listed output.
        (root / '.gitignore').write_text('build/\n.state/\n')
        self.assertFalse(ws.pristine(root, head, outputs))
        git('checkout', '--', '.gitignore', cwd=root)
        # A new file anywhere else, and another commit.
        (root / 'new.txt').write_text('x\n')
        self.assertFalse(ws.pristine(root, head, outputs))
        (root / 'new.txt').unlink()
        self.assertFalse(ws.pristine(root, '0' * 40, outputs))

    def test_the_stamp_follows_the_command_and_the_inputs(self):
        ws, root = self.ws, self.root
        inputs = ('package-lock.json', 'missing.lock')
        first = ws.prepare_stamp(root, 'npm ci', inputs)
        self.assertEqual(ws.prepare_stamp(root, 'npm ci', inputs), first)
        self.assertNotEqual(ws.prepare_stamp(root, 'npm ci --ignore-scripts', inputs), first)
        (root / 'package-lock.json').write_text('{"v": 2}\n')
        second = ws.prepare_stamp(root, 'npm ci', inputs)
        self.assertNotEqual(second, first)
        (root / 'missing.lock').write_text('now here\n')
        self.assertNotEqual(ws.prepare_stamp(root, 'npm ci', inputs), second)
        # A link is not followed, a pipe is not waited on.
        (root / 'missing.lock').unlink()
        (root / 'missing.lock').symlink_to(root / 'package-lock.json')
        linked = ws.prepare_stamp(root, 'npm ci', inputs)
        self.assertNotEqual(linked, second)
        (root / 'missing.lock').unlink()
        os.mkfifo(root / 'missing.lock')
        self.assertNotIn(ws.prepare_stamp(root, 'npm ci', inputs), (linked, second))

    def test_a_worktree_created_again_or_removed_is_prepared_again(self):
        ws = self.ws
        path = ws.path('issue-7')
        ws.mark_prepared(path, 's1')
        self.assertEqual(ws.prepared(path), 's1')
        ws.remove('issue-7')
        self.assertIsNone(ws.prepared(path))
        ws.mark_prepared(path, 's1')
        ws._fresh(path)
        self.assertIsNone(ws.prepared(path))

    def test_the_step_runs_with_its_cache_and_a_time_limit(self):
        ws, root = self.ws, self.root
        cache = ws.prepare_cache('acme/widget')
        self.assertEqual(cache, self.home / '.cache' / 'devloop-prepare' / 'acme_widget')
        self.assertFalse(str(cache).startswith(str(root)))
        passed, tail = ws.run_prepare(
            root, f'echo "cache=${PREPARE_CACHE_ENV}"; mkdir -p node_modules/dep; echo done',
            60, cache)
        self.assertTrue(passed, tail)
        self.assertIn(f'cache={cache}', tail)
        self.assertTrue(cache.is_dir())  # created before the command, by the user running it
        self.assertTrue((root / 'node_modules' / 'dep').is_dir())
        passed, tail = ws.run_prepare(root, 'echo boom >&2; exit 3', 60, cache)
        self.assertEqual((passed, tail), (False, 'boom'))
        passed, tail = ws.run_prepare(root, 'sleep 30', 1, cache)
        self.assertEqual((passed, tail), (False, 'Prepare command timed out after 1s'))


class DispatcherTests(unittest.TestCase):
    def make(self, ws, *outputs, prepare=NPM, during=None, ledger=False):
        self.gh, self.ws = LedgerGH(), ws
        self.logs = []
        self.runner = EventRunner(ws, *outputs, during=during)
        cfg = dataclasses.replace(CFG, prepare=prepare)
        return Dispatcher(cfg, self.gh, ws, self.runner, log=self.logs.append,
                          ledger=Ledger(self.gh) if ledger else None)

    def test_a_new_worktree_has_its_dependencies_before_the_first_turn(self):
        ws = PrepareWS()
        self.make(ws, implemented())._do_implement(7)
        self.assertEqual(ws.events, [('prepare', 'issue-7'), ('turn', 'issue-7'),
                                     ('test', 'issue-7')])
        self.assertEqual(ws.runs, [(NPM.command, CFG.test_timeout_seconds,
                                    ws.prepare_cache(CFG.repo))])
        self.assertEqual(self.gh.labels[100], {'agent:review'})

    def test_a_change_of_an_input_prepares_again_before_the_next_turn_and_test(self):
        ws = PrepareWS(tests=(False, True))

        def agent(turn):
            if turn == 1:
                ws.inputs = 'lock v2'  # the first attempt changed the lock file
        self.make(ws, implemented(), implemented(), during=agent)._do_implement(7)
        self.assertEqual(ws.events, [('prepare', 'issue-7'), ('turn', 'issue-7'),
                                     ('prepare', 'issue-7'), ('test', 'issue-7'),
                                     ('turn', 'issue-7'), ('test', 'issue-7')])
        self.assertIn('  prepare issue-7: [prepare] inputs changed, running [prepare] cmd again',
                      self.logs)

    def test_a_change_during_the_tests_prepares_again_before_the_next_turn(self):
        ws = PrepareWS(tests=(False, True))
        original = ws.run_tests

        def run_tests(path, command, timeout):
            ws.inputs = 'lock v3'
            return original(path, command, timeout)
        ws.run_tests = run_tests
        self.make(ws, implemented(), implemented())._do_implement(7)
        self.assertEqual(ws.events, [('prepare', 'issue-7'), ('turn', 'issue-7'),
                                     ('test', 'issue-7'), ('prepare', 'issue-7'),
                                     ('turn', 'issue-7'), ('test', 'issue-7')])

    def test_a_failure_is_a_critical_escalation_to_the_operator_with_the_output(self):
        ws = PrepareWS(results=(False,))
        d = self.make(ws, implemented())
        failed = d._run_action(Action('implement', 7), {})
        self.assertIsNotNone(failed)
        self.assertEqual(ws.events, [('prepare', 'issue-7')])  # no turn ran
        self.assertEqual(self.runner.calls, [])
        notice = markdown.read_marker(self.gh.comments[-1][1], 'notice')
        self.assertEqual((notice['class'], notice['critical'], notice['to']),
                         (PREPARE_FAILED, True, 'operator'))
        self.assertTrue(critical(PREPARE_FAILED))
        self.assertIn('npm ERR! code E404', self.gh.comments[-1][1])
        self.assertIn('agent:needs-operator', self.gh.labels[7])
        self.assertEqual(self.gh.created, [])

    def test_the_step_is_recorded_as_a_local_verification(self):
        ws = PrepareWS()
        self.make(ws, implemented(), ledger=True)._do_implement(7)
        prepared, tested = self.gh.records(7, 'verification')
        self.assertEqual((prepared['kind'], prepared['command'], prepared['outcome'],
                          prepared['attempt'], prepared['commit']),
                         ('prepare', NPM.command, 'pass', 1, None))
        self.assertEqual((tested['kind'], tested['outcome']), ('implement', 'pass'))
        ws = PrepareWS(results=(False,))
        with self.assertRaisesRegex(Exception, r'\[prepare\] cmd'):
            self.make(ws, implemented(), ledger=True)._do_implement(7)
        (failed,) = self.gh.records(7, 'verification')
        self.assertEqual((failed['kind'], failed['outcome']), ('prepare', 'fail'))

    def test_the_worktree_of_a_fix_is_prepared_and_recorded_in_its_register(self):
        ws = PrepareWS()
        first = norm(raw_review('request_changes', [finding()]))
        self.make(ws, fixed(), ledger=True)._do_fix(pr(labels=['agent:changes'], reviews=[first]))
        self.assertEqual(ws.events[:2], [('prepare', 'pr-100'), ('turn', 'pr-100')])
        prepared = [v for v in self.gh.records(100, 'verification') if v['kind'] == 'prepare']
        self.assertEqual([(v['outcome'], v['section']) for v in prepared], [('pass', 1)])

    def test_the_copy_of_a_reviewer_is_prepared_and_the_check_admits_the_outputs(self):
        ws = PrepareWS()
        d = self.make(ws, raw_review(), ledger=True)
        p = pr()
        self.gh.labels[100] = set(p.labels)
        d._do_review(p)
        self.assertEqual(ws.events, [('prepare', 'pr-100-review'), ('turn', 'pr-100-review')])
        self.assertEqual(ws.outputs_checked, [NPM.outputs])
        # A record of its own: the register of a round holds only turns and events.
        (prepared,) = self.gh.records(100, 'verification')
        self.assertEqual((prepared['kind'], prepared['outcome']), ('prepare', 'pass'))
        self.assertNotIn('section', prepared)

    def test_the_base_commit_is_prepared_before_its_tests(self):
        ws = PrepareWS()
        ws.cases = {'issue-7': ['test_x.T.test_a'], 'issue-7-base': ['test_x.T.test_a']}
        d = self.make(ws)
        d._base_broken(Path('/wt/issue-7'))
        self.assertEqual(ws.events[:2], [('prepare', 'issue-7-base'), ('test', 'issue-7-base')])
        # A failed step there leaves the failure to the branch: nothing tells it is the base's.
        ws = PrepareWS(results=(False,))
        ws.cases = {'issue-7': ['test_x.T.test_a'], 'issue-7-base': ['test_x.T.test_a']}
        self.assertIsNone(self.make(ws)._base_broken(Path('/wt/issue-7')))
        self.assertEqual(ws.events, [('prepare', 'issue-7-base')])

    def test_without_the_command_nothing_runs_and_the_check_is_as_before(self):
        ws = PrepareWS()
        self.make(ws, implemented(), prepare=Prepare())._do_implement(7)
        self.assertEqual(ws.events, [('turn', 'issue-7'), ('test', 'issue-7')])
        self.assertEqual(ws.runs, [])
        ws = PrepareWS()
        d = self.make(ws, raw_review(), prepare=Prepare())
        p = pr()
        self.gh.labels[100] = set(p.labels)
        d._do_review(p)
        self.assertEqual(ws.events, [('turn', 'pr-100-review')])
        self.assertEqual(ws.outputs_checked, [()])


if __name__ == '__main__':
    unittest.main()
