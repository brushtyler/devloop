"""Local tests limited to the modules pertinent to the diff (issue #169)."""

import dataclasses
import subprocess
import tempfile
import unittest
from pathlib import Path

from devloop import config, markdown
from devloop.dispatcher import Dispatcher
from devloop.localtests import module_command, select_modules
from devloop.workspace import Workspace

from helpers import ssdlc_project
from helpers.devloop_fakes import FakeGH
from test_devloop import (CFG, FakeRunner, FakeWS, finding, fixed, implemented, norm, pr,
                          raw_review)

MODULES = ['test_devloop.py', 'test_devloop_ledger.py', 'test_impact.py', 'test_impact_more.py',
           'test_orchestrator_cli.py', 'test_other.py', 'test_ssdlc_impact_x.py']


def config_data():
    return ssdlc_project.data()


def cmd(*modules):
    return module_command(list(modules))


class SelectionTests(unittest.TestCase):
    def test_a_source_module_selects_its_tests(self):
        # ssdlc_<name>.py: test_ssdlc_<name>*.py and test_<name>*.py.
        self.assertEqual(select_modules(['ssdlc_impact.py'], MODULES),
                         ['test_impact.py', 'test_impact_more.py', 'test_ssdlc_impact_x.py'])
        self.assertEqual(select_modules(['orchestrator.py'], MODULES),
                         ['test_orchestrator_cli.py'])
        self.assertEqual(select_modules(['pkg/other.py'], MODULES), ['test_other.py'])

    def test_any_file_under_devloop_selects_the_devloop_tests(self):
        for path in ('devloop/dispatcher.py', 'devloop/config.toml', 'devloop/prompts/fix.md'):
            with self.subTest(path=path):
                self.assertEqual(select_modules([path], MODULES),
                                 ['test_devloop.py', 'test_devloop_ledger.py'])

    def test_a_changed_test_module_selects_itself_if_it_exists(self):
        self.assertEqual(select_modules(['tests/test_other.py'], MODULES), ['test_other.py'])
        self.assertEqual(select_modules(['tests/test_gone.py'], MODULES), [])  # deleted

    def test_documentation_only_selects_nothing(self):
        self.assertEqual(select_modules(['docs/devloop.md', 'README.md', '.py'], MODULES), [])

    def test_the_command_runs_each_module_on_its_own(self):
        self.assertEqual(cmd('test_a.py', "test_b'.py"),
                         "python3 -m unittest discover -s tests -p test_a.py && "
                         "python3 -m unittest discover -s tests -p 'test_b'\"'\"'.py'")


class ConfigTests(unittest.TestCase):
    def test_this_repository_runs_the_changed_modules(self):
        self.assertEqual(CFG.local_tests, 'changed')

    def test_default_is_full_and_unknown_values_are_refused(self):
        data = config_data()
        del data['local_tests']
        self.assertEqual(config.parse_config(data).local_tests, 'full')
        for mode in ('full', 'changed', 'none'):
            self.assertEqual(config.parse_config(dict(data, local_tests=mode)).local_tests, mode)
        for bad in ('all', 'Changed', '', 1, True, ['full']):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config.parse_config(dict(data, local_tests=bad))


class DispatcherTests(unittest.TestCase):
    def make(self, runner, ws, mode='changed', ci='pass'):
        self.gh, self.ws = FakeGH(ci=ci), ws
        self.logs = []
        cfg = dataclasses.replace(CFG, local_tests=mode)
        return Dispatcher(cfg, self.gh, ws, runner, log=self.logs.append)

    def implement(self, files, mode='changed', tests=(True,)):
        runner = FakeRunner(implemented(), implemented())
        ws = FakeWS(files=files, modules=MODULES, tests=tests)
        self.make(runner, ws, mode)._do_implement(7)
        return runner

    def marker(self):
        return markdown.read_marker(self.gh.created[0]['body'], 'pr')['local_tests']

    def test_changed_runs_only_the_pertinent_modules(self):
        self.implement(['ssdlc_impact.py'])
        expected = ['test_impact.py', 'test_impact_more.py', 'test_ssdlc_impact_x.py']
        self.assertEqual(self.ws.commands, [(Path('/wt/issue-7'), cmd(*expected))])
        self.assertNotIn(CFG.test_cmd, self.ws.commands[0][1])
        self.assertEqual(self.marker(), {'mode': 'changed', 'modules': expected})
        self.assertIn('  local tests (changed): ' + ', '.join(expected), self.logs)

    def test_a_change_under_devloop_runs_the_devloop_modules(self):
        self.implement(['devloop/dispatcher.py'])
        self.assertEqual([c for _, c in self.ws.commands],
                         [cmd('test_devloop.py', 'test_devloop_ledger.py')])

    def test_an_empty_set_runs_nothing_and_the_pr_goes_to_ci(self):
        runner = self.implement(['docs/devloop.md'])
        self.assertEqual(self.ws.commands, [])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.assertEqual(self.ws.pushes, [('agent/issue-7', True)])
        self.assertEqual(self.marker(), {'mode': 'changed', 'modules': []})

    def test_full_runs_the_test_command_and_none_runs_nothing(self):
        self.implement(['ssdlc_impact.py'], mode='full')
        self.assertEqual([c for _, c in self.ws.commands], [CFG.test_cmd])
        self.assertEqual(self.marker(), {'mode': 'full'})
        self.implement(['ssdlc_impact.py'], mode='none')
        self.assertEqual(self.ws.commands, [])
        self.assertEqual(self.marker(), {'mode': 'none', 'modules': []})
        self.assertEqual(self.gh.labels[100], {'agent:review'})

    def test_retries_apply_to_the_local_tests_recomputed_on_each_attempt(self):
        runner = FakeRunner(implemented(), implemented())
        ws = FakeWS(files=['ssdlc_impact.py'], modules=MODULES, tests=(False, True))
        d = self.make(runner, ws)
        original = ws.run_tests

        def run_tests(path, command, timeout):
            ws.files = ['ssdlc_impact.py', 'tests/test_other.py']  # the second attempt adds it
            return original(path, command, timeout)
        ws.run_tests = run_tests
        d._do_implement(7)
        self.assertEqual(len(runner.calls), 2)
        self.assertIn('FAILED test_x', runner.calls[1][3])
        self.assertEqual([c for _, c in ws.commands],
                         [cmd('test_impact.py', 'test_impact_more.py', 'test_ssdlc_impact_x.py'),
                          cmd('test_impact.py', 'test_impact_more.py', 'test_other.py',
                              'test_ssdlc_impact_x.py')])
        self.assertIn('test_other.py', self.marker()['modules'])

    def test_a_local_failure_after_the_retries_escalates_as_before(self):
        runner = self.implement(['ssdlc_impact.py'], tests=(False, False))
        self.assertEqual(len(runner.calls), 1 + CFG.implement_test_retries)
        self.assertEqual(self.gh.created, [])
        self.assertEqual(self.gh.labels[7], {'agent:needs-operator'})
        self.assertIn('I test locali falliscono', self.gh.comments[-1][1])
        self.assertIn('FAILED test_x', self.gh.comments[-1][1])

    def test_the_fix_marker_records_mode_and_modules(self):
        first = norm(raw_review('request_changes', [finding()]))
        ws = FakeWS(files=['devloop/review.py'], modules=MODULES)
        self.make(FakeRunner(fixed()), ws)._do_fix(pr(labels=['agent:changes'], reviews=[first]))
        record = markdown.read_marker(self.gh.comments[0][1], 'fix')
        self.assertEqual(record['local_tests'],
                         {'mode': 'changed', 'modules': ['test_devloop.py',
                                                         'test_devloop_ledger.py']})


class CiGateTests(unittest.TestCase):
    """Without the full suite locally, a review needs the CI passed; without CI, the full suite
    runs locally before the review."""

    def review(self, mode, checks, tests=(True,)):
        runner = FakeRunner(raw_review())
        self.gh, self.ws = FakeGH(ci=checks), FakeWS(tests=tests)
        d = Dispatcher(dataclasses.replace(CFG, local_tests=mode), self.gh, self.ws, runner,
                       log=lambda m: None)
        p = pr(checks=checks)
        self.gh.labels[100] = set(p.labels)
        d._do_review(p)
        return runner

    def test_a_head_without_ci_gets_the_full_suite_before_the_review(self):
        for mode in ('changed', 'none'):
            with self.subTest(mode=mode):
                runner = self.review(mode, 'none')
                self.assertEqual(self.ws.commands, [(Path('/wt/pr-100-review'), CFG.test_cmd)])
                self.assertEqual(len(runner.calls), 1)
                self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})

    def test_a_failing_full_suite_escalates_without_review(self):
        runner = self.review('changed', 'none', tests=(False,))
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        self.assertIn('FAILED test_x', self.gh.comments[-1][1])
        self.assertIn('pr-100-review', self.ws.removed)

    def test_passed_ci_or_full_mode_runs_no_local_suite(self):
        for mode, checks in (('changed', 'pass'), ('none', 'pass'), ('full', 'none')):
            with self.subTest(mode=mode, checks=checks):
                runner = self.review(mode, checks)
                self.assertEqual(self.ws.commands, [])
                self.assertEqual(len(runner.calls), 1)


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


class WorktreeChangesTests(unittest.TestCase):
    def test_the_diff_covers_commits_index_changes_and_new_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git(root, 'init', '-q', '-b', 'main')
            git(root, 'config', 'user.email', 't@example.com')
            git(root, 'config', 'user.name', 't')
            for name in ('a.py', 'b.py', 'c.py', 'd.py', '.gitignore'):
                (root / name).write_text('*.log\n' if name == '.gitignore' else 'x\n')
            (root / 'tests').mkdir()
            (root / 'tests' / 'test_a.py').write_text('')
            (root / 'tests' / 'helper.py').write_text('')
            git(root, 'add', '-A')
            git(root, 'commit', '-q', '-m', 'base')
            git(root, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
            git(root, 'checkout', '-q', '-b', 'work')
            (root / 'a.py').write_text('committed\n')
            git(root, 'commit', '-q', '-am', 'branch')
            (root / 'b.py').write_text('staged\n')
            git(root, 'add', 'b.py')
            (root / 'c.py').write_text('unstaged\n')
            git(root, 'rm', '-q', 'd.py')
            (root / 'new.py').write_text('')
            (root / 'run.log').write_text('')  # ignored
            ws = Workspace(root, '.state', 'main')
            self.assertEqual(ws.worktree_changes(root), ['a.py', 'b.py', 'c.py', 'd.py', 'new.py'])
            self.assertEqual(ws.test_modules(root), ['test_a.py'])


if __name__ == '__main__':
    unittest.main()
