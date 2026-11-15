"""scripts/run-tests.py must run exactly the tests of `python3 -m unittest discover -s tests`."""

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


class RunTestsEquivalenceTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
