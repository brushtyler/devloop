"""scripts/run-tests.py must run exactly the tests of `python3 -m unittest discover -s tests`."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'run-tests.py'


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


def ran(output):
    found = re.findall(r'^Ran (\d+) tests?', output, re.M)
    return int(found[-1]) if found else None


class ProjectMixin:
    def project(self, files):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / 'scripts').mkdir()
        shutil.copy(SCRIPT, tmp / 'scripts' / 'run-tests.py')
        for name, text in files.items():
            write(tmp, name, text)
        return tmp

    def both(self, root):
        serial = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'], cwd=root,
                                capture_output=True, text=True, timeout=120)
        parallel = subprocess.run([sys.executable, 'scripts/run-tests.py', '-j', '3'], cwd=root,
                                  capture_output=True, text=True, timeout=120)
        return serial, parallel


class RunTestsEquivalenceTests(ProjectMixin, unittest.TestCase):
    def test_same_tests_and_outcome_as_discover(self):
        # Codex review of #75: a load_tests hook that forwards the pattern, and modules with the
        # same name in two packages, were run differently from the full discover.
        passing = '''
            import unittest
            class T(unittest.TestCase):
                def test_ok(self):
                    pass
        '''
        root = self.project({
            'tests/test_plain.py': passing,
            'tests/a/__init__.py': '',
            'tests/a/test_same.py': passing,
            'tests/b/__init__.py': '',
            'tests/b/test_same.py': passing,
            'tests/test_loader.py': '''
                import os
                def load_tests(loader, tests, pattern):
                    here = os.path.dirname(__file__)
                    return loader.discover(os.path.join(here, 'cases'), pattern=pattern or 'test*.py',
                                           top_level_dir=os.path.join(here, 'cases'))
            ''',
            'tests/cases/test_delegated.py': passing,
        })
        serial, parallel = self.both(root)
        self.assertEqual(serial.returncode, 0, serial.stderr)
        self.assertEqual(parallel.returncode, 0, parallel.stdout)
        self.assertEqual(ran(parallel.stdout), ran(serial.stderr))
        self.assertEqual(ran(serial.stderr), 4)

        write(root, 'tests/cases/test_regression.py', '''
            import unittest
            class T(unittest.TestCase):
                def test_fails(self):
                    self.fail('regression')
        ''')
        serial, parallel = self.both(root)
        self.assertNotEqual(serial.returncode, 0)
        self.assertNotEqual(parallel.returncode, 0)
        self.assertEqual(ran(parallel.stdout), ran(serial.stderr))
        self.assertIn('regression', parallel.stdout)

    def test_a_lost_result_fails_the_run(self):
        # Codex review of #75, round 2: an error while reading a worker's results ended its
        # thread silently, and the run reported OK with fewer tests.
        root = self.project({
            'tests/test_corrupt.py': '''
                import sys, unittest
                class T(unittest.TestCase):
                    def test_writes_into_the_results_file(self):
                        if '--results' in sys.argv:
                            with open(sys.argv[sys.argv.index('--results') + 1], 'a') as fh:
                                fh.write('{truncated\\n')
            ''',
        })
        serial, parallel = self.both(root)
        self.assertEqual(serial.returncode, 0, serial.stderr)
        self.assertNotEqual(parallel.returncode, 0, parallel.stdout)
        self.assertIn('WORKER PROBLEM', parallel.stdout)

    def test_a_test_tree_that_changes_during_discovery_fails_the_run(self):
        # Codex review of #75, round 3: workers picked children by the main process's indices, so
        # a module creating a test module on import shifted them and a failing test went unrun.
        root = self.project({
            'tests/test_a_generator.py': '''
                import os, unittest
                generated = os.path.join(os.path.dirname(__file__), 'test_0_generated.py')
                if not os.path.exists(generated):
                    with open(generated, 'w') as fh:
                        fh.write('import unittest\\nclass G(unittest.TestCase):\\n    def test_g(self):\\n        pass\\n')
                class T(unittest.TestCase):
                    def test_ok(self):
                        pass
            ''',
            'tests/test_z_failure.py': '''
                import unittest
                class T(unittest.TestCase):
                    def test_fails(self):
                        self.fail('must not be skipped')
            ''',
        })
        parallel = subprocess.run([sys.executable, 'scripts/run-tests.py', '-j', '2'], cwd=root,
                                  capture_output=True, text=True, timeout=120)
        self.assertNotEqual(parallel.returncode, 0, parallel.stdout)

    def test_the_script_directory_is_not_importable(self):
        # Codex review of #75, round 4: `python3 scripts/run-tests.py` put scripts/ on sys.path,
        # so code importing a helper from scripts/ passed here and failed with unittest.
        root = self.project({
            'scripts/runtime_helpers.py': 'VALUE = 42\n',
            'app.py': 'from runtime_helpers import VALUE\n',
            'tests/test_app.py': '''
                import unittest
                class T(unittest.TestCase):
                    def test_value(self):
                        import app
                        self.assertEqual(app.VALUE, 42)
            ''',
        })
        serial, parallel = self.both(root)
        self.assertNotEqual(serial.returncode, 0)
        self.assertNotEqual(parallel.returncode, 0, parallel.stdout)

    def test_a_local_module_shadowing_the_runner_imports_is_seen_by_the_tests(self):
        # Codex review of #75, round 5: the worker imported json before the project root was on
        # sys.path, so a root json.py broke the serial discover but not the worker.
        root = self.project({
            'json.py': 'BROKEN = True\n',
            'tests/test_uses_json.py': '''
                import unittest
                class T(unittest.TestCase):
                    def test_loads(self):
                        import json
                        self.assertEqual(json.loads('42'), 42)
            ''',
        })
        serial, parallel = self.both(root)
        self.assertNotEqual(serial.returncode, 0)
        self.assertNotEqual(parallel.returncode, 0, parallel.stdout)

    def test_a_tests_helper_shadowing_the_runner_imports_is_seen_by_the_tests(self):
        # Codex review of #75, round 6 (#78): the worker imported json before its discover put
        # tests/ first on sys.path, so tests/json.py broke the serial discover but not the worker.
        root = self.project({
            'tests/json.py': 'BROKEN = True\n',
            'tests/test_uses_json.py': '''
                import unittest
                class T(unittest.TestCase):
                    def test_loads(self):
                        import json
                        self.assertEqual(json.loads('42'), 42)
            ''',
        })
        serial, parallel = self.both(root)
        self.assertNotEqual(serial.returncode, 0)
        self.assertNotEqual(parallel.returncode, 0, parallel.stdout)
        self.assertIn('FAILED MODULE test_uses_json', parallel.stdout)
        self.assertNotIn('WORKER PROBLEM', parallel.stdout)

        # The runner itself must not pick up the helper: with no test using json, both pass.
        write(root, 'tests/test_uses_json.py', '''
            import unittest
            class T(unittest.TestCase):
                def test_ok(self):
                    pass
        ''')
        serial, parallel = self.both(root)
        self.assertEqual(serial.returncode, 0, serial.stderr)
        self.assertEqual(parallel.returncode, 0, parallel.stdout)
        self.assertEqual(ran(parallel.stdout), ran(serial.stderr))

    def test_the_worker_imports_nothing_after_its_discover(self):
        # Review of #89: the worker loaded the runner with runpy, which imports pkgutil after the
        # discover, so an unused tests/pkgutil.py broke the worker but not the serial discover.
        # The other names are the modules the main process imports, also unused by the tests.
        shadowed = ['pkgutil', 'runpy', 'json', 'argparse', 'ast', 'subprocess', 'tempfile',
                    'threading', 'pathlib']
        root = self.project({
            **{f'tests/{name}.py': 'BROKEN = True\n' for name in shadowed},
            'tests/test_plain.py': '''
                import unittest
                class T(unittest.TestCase):
                    def test_ok(self):
                        pass
            ''',
        })
        serial, parallel = self.both(root)
        self.assertEqual(serial.returncode, 0, serial.stderr)
        self.assertEqual(parallel.returncode, 0, parallel.stdout)
        self.assertEqual(ran(parallel.stdout), ran(serial.stderr))
        self.assertEqual(ran(serial.stderr), 1)

    def test_no_tests_is_not_a_success(self):
        # Codex review of #75, round 3: like `python3 -m unittest`, running nothing exits with 5.
        root = self.project({'tests/test_empty.py': 'import unittest\n'})
        serial, parallel = self.both(root)
        self.assertEqual(parallel.returncode, serial.returncode, parallel.stdout)
        self.assertNotEqual(parallel.returncode, 0)

    def test_an_unimportable_module_fails_the_run(self):
        root = self.project({'tests/test_broken.py': 'import does_not_exist_anywhere\n',
                             'tests/test_fine.py': 'import unittest\n'})
        serial, parallel = self.both(root)
        self.assertNotEqual(serial.returncode, 0)
        self.assertNotEqual(parallel.returncode, 0)
        self.assertIn('does_not_exist_anywhere', parallel.stdout)


# A helper the test modules below import (tests/ is on sys.path in the discover): each call
# appends `<what> <pid>` to log.txt at the project root.
RECORDER = '''
    import os
    LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'log.txt')
    def record(what):
        with open(LOG, 'a') as fh:
            fh.write(f'{what} {os.getpid()}\\n')
'''


def two_classes(name, fail=False):
    """A module with classes A (with setUpClass, two tests) and B (one test, failing if ``fail``)."""
    outcome = "self.fail('split failure')" if fail else 'pass'
    return textwrap.dedent(f'''
        import unittest
        from recorder import record
        class A(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                record('{name}.A.setUpClass')
            def test_one(self):
                record('{name}.A')
            def test_two(self):
                record('{name}.A')
        class B(unittest.TestCase):
            def test_one(self):
                record('{name}.B')
                {outcome}
    ''')


class RunTestsDistributionTests(ProjectMixin, unittest.TestCase):
    """Issue #272: slow modules are split by class across the workers, and the git index is
    refreshed once before they start."""

    def run_tests(self, root, jobs=2, env=None):
        return subprocess.run([sys.executable, 'scripts/run-tests.py', '-j', str(jobs)], cwd=root,
                              capture_output=True, text=True, timeout=120, env=env)

    def both(self, root, jobs=2):
        """The serial discover, then the runner; log.txt keeps only what the runner recorded."""
        serial = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'],
                                cwd=root, capture_output=True, text=True, timeout=120)
        (root / 'log.txt').unlink(missing_ok=True)
        return serial, self.run_tests(root, jobs)

    def history(self, root, durations):
        (root / '.ssdlc').mkdir(exist_ok=True)
        (root / '.ssdlc' / 'test-durations.json').write_text(json.dumps(durations))

    def log(self, root):
        """``{what: [pid, ...]}`` from log.txt."""
        entries = {}
        path = root / 'log.txt'
        for line in path.read_text().splitlines() if path.exists() else []:
            what, pid = line.rsplit(' ', 1)
            entries.setdefault(what, []).append(pid)
        return entries

    def test_a_slow_module_runs_its_classes_in_different_workers(self):
        root = self.project({'tests/recorder.py': RECORDER, 'tests/test_slow.py': two_classes('slow')})
        self.history(root, {'test_slow': 100})
        serial, parallel = self.both(root)
        self.assertEqual(parallel.returncode, 0, parallel.stdout)
        self.assertEqual(ran(parallel.stdout), ran(serial.stderr))
        self.assertEqual(ran(parallel.stdout), 3)
        self.assertIn('(1 modules, 1 split by class, 2 jobs)', parallel.stdout)
        # The output stays per module: one progress line for the whole module.
        self.assertEqual(len(re.findall(r'^ok   test_slow \(3 tests', parallel.stdout, re.M)), 1)
        log = self.log(root)
        self.assertEqual(len(set(log['slow.A'])), 1, log)   # a class is never split
        self.assertEqual(len(log['slow.A.setUpClass']), 1, log)
        self.assertNotEqual(log['slow.A'][0], log['slow.B'][0], log)
        durations = json.loads((root / '.ssdlc' / 'test-durations.json').read_text())
        self.assertIsInstance(durations['test_slow'], (int, float))
        self.assertLess(durations['test_slow'], 100)

    def test_a_module_with_a_short_history_stays_whole(self):
        root = self.project({'tests/recorder.py': RECORDER, 'tests/test_fast.py': two_classes('fast')})
        self.history(root, {'test_fast': 10})
        parallel = self.run_tests(root)
        self.assertEqual(parallel.returncode, 0, parallel.stdout)
        self.assertIn('(1 modules, 0 split by class, 1 jobs)', parallel.stdout)
        log = self.log(root)
        self.assertEqual(log['fast.A'][0], log['fast.B'][0], log)

    def test_without_history_divisible_modules_are_split(self):
        # load_tests may select classes defined elsewhere: the discovered instances are split.
        root = self.project({
            'tests/recorder.py': RECORDER,
            'tests/cases_elsewhere.py': two_classes('elsewhere'),
            'tests/test_loader.py': '''
                import cases_elsewhere
                def load_tests(loader, tests, pattern):
                    return loader.loadTestsFromModule(cases_elsewhere)
            ''',
        })
        serial, parallel = self.both(root)
        self.assertEqual(parallel.returncode, 0, parallel.stdout)
        self.assertEqual(ran(parallel.stdout), ran(serial.stderr))
        self.assertIn('(1 modules, 1 split by class, 2 jobs)', parallel.stdout)
        log = self.log(root)
        self.assertEqual(len(log['elsewhere.A.setUpClass']), 1, log)
        self.assertNotEqual(log['elsewhere.A'][0], log['elsewhere.B'][0], log)

    def test_module_fixtures_and_custom_suites_are_not_split(self):
        root = self.project({
            'tests/recorder.py': RECORDER,
            'tests/test_fixture.py': two_classes('fixture') + textwrap.dedent('''
                def setUpModule():
                    record('fixture.setUpModule')
            '''),
            'tests/cases_custom.py': two_classes('custom'),
            'tests/test_custom.py': '''
                import unittest, cases_custom
                class Custom(unittest.TestSuite):
                    pass
                def load_tests(loader, tests, pattern):
                    return Custom(loader.loadTestsFromModule(cases_custom))
            ''',
        })
        # The label of a module is the module of its first test: cases_custom for test_custom.
        self.history(root, {'test_fixture': 100, 'cases_custom': 100})
        serial, parallel = self.both(root, jobs=4)
        self.assertEqual(parallel.returncode, 0, parallel.stdout)
        self.assertEqual(ran(parallel.stdout), ran(serial.stderr))
        self.assertIn('(2 modules, 0 split by class, 2 jobs)', parallel.stdout)
        log = self.log(root)
        self.assertEqual(len(log['fixture.setUpModule']), 1, log)
        for name in ('fixture', 'custom'):
            self.assertEqual(log[f'{name}.A'][0], log[f'{name}.B'][0], log)

    def test_a_failure_in_a_split_module_is_reported_once_for_the_module(self):
        root = self.project({'tests/recorder.py': RECORDER,
                             'tests/test_slow.py': two_classes('slow', fail=True)})
        self.history(root, {'test_slow': 100})
        serial, parallel = self.both(root)
        self.assertNotEqual(serial.returncode, 0)
        self.assertEqual(parallel.returncode, 1, parallel.stdout)
        self.assertEqual(ran(parallel.stdout), ran(serial.stderr))
        self.assertIn('1 split by class', parallel.stdout)
        self.assertEqual(parallel.stdout.count('FAILED MODULE test_slow'), 1, parallel.stdout)
        self.assertIn('split failure', parallel.stdout)
        self.assertIn('FAILED (modules=1, worker problems=0)', parallel.stdout)
        self.assertEqual(len(re.findall(r'^FAIL test_slow \(3 tests', parallel.stdout, re.M)), 1)
        # A failed module still records its total time.
        durations = json.loads((root / '.ssdlc' / 'test-durations.json').read_text())
        self.assertLess(durations['test_slow'], 100)

    def fake_git(self, root, code):
        """A `git` on PATH that logs its arguments to log.txt and exits with ``code``."""
        bin_dir = root / 'fakebin'
        bin_dir.mkdir()
        git = bin_dir / 'git'
        git.write_text(f'#!/bin/sh\necho "git $*" >> "{root / "log.txt"}"\n'
                       f'echo "fatal: simulated" >&2\nexit {code}\n')
        git.chmod(0o755)
        return dict(os.environ, PATH=f'{bin_dir}{os.pathsep}{os.environ.get("PATH", "")}')

    def test_the_index_is_refreshed_once_before_the_workers_and_a_failure_is_not_fatal(self):
        root = self.project({'tests/recorder.py': RECORDER, 'tests/test_slow.py': two_classes('slow')})
        (root / '.git').mkdir()  # the root is a repository: the fake git does the rest
        parallel = self.run_tests(root, jobs=2, env=self.fake_git(root, 1))
        self.assertEqual(parallel.returncode, 0, parallel.stdout)
        self.assertIn('git update-index --refresh exited with 1, continuing: fatal: simulated',
                      parallel.stdout)
        lines = (root / 'log.txt').read_text().splitlines()
        calls = [line for line in lines if line.startswith('git ')]
        self.assertEqual(calls, ['git update-index -q --refresh'])
        self.assertEqual(lines[0], calls[0])  # before any test ran

    def test_a_root_that_is_not_a_repository_is_not_refreshed(self):
        root = self.project({'tests/recorder.py': RECORDER, 'tests/test_slow.py': two_classes('slow')})
        parallel = self.run_tests(root, jobs=2, env=self.fake_git(root, 0))
        self.assertEqual(parallel.returncode, 0, parallel.stdout)
        self.assertNotIn('git', ' '.join(self.log(root)))
        self.assertNotIn('update-index', parallel.stdout)

    @unittest.skipUnless(shutil.which('git'), 'git is not installed')
    def test_a_stale_index_is_refreshed(self):
        root = self.project({'tests/test_plain.py': '''
            import unittest
            class T(unittest.TestCase):
                def test_ok(self):
                    pass
        '''})
        git = ['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
               '-c', 'commit.gpgsign=false']
        for cmd in (['init', '-q'], ['add', '-A'], ['commit', '-q', '-m', 'init']):
            subprocess.run(git + cmd, cwd=root, check=True, capture_output=True)
        # New stat data, same content: `git diff-files` does not refresh, so it lists the file
        # until the index is refreshed.
        path = root / 'tests' / 'test_plain.py'
        os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 100))
        stale = subprocess.run(['git', 'diff-files', '--name-only'], cwd=root, capture_output=True,
                               text=True, check=True).stdout
        self.assertIn('tests/test_plain.py', stale)
        parallel = self.run_tests(root)
        self.assertEqual(parallel.returncode, 0, parallel.stdout)
        fresh = subprocess.run(['git', 'diff-files', '--name-only'], cwd=root, capture_output=True,
                               text=True, check=True).stdout
        self.assertEqual(fresh, '')


if __name__ == '__main__':
    unittest.main()
