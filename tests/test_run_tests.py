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

    def test_an_unimportable_module_fails_the_run(self):
        root = self.project({'tests/test_broken.py': 'import does_not_exist_anywhere\n',
                             'tests/test_fine.py': 'import unittest\n'})
        serial, parallel = self.both(root)
        self.assertNotEqual(serial.returncode, 0)
        self.assertNotEqual(parallel.returncode, 0)
        self.assertIn('does_not_exist_anywhere', parallel.stdout)


if __name__ == '__main__':
    unittest.main()
