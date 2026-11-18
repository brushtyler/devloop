"""devloop is an independent tool: it may import only the standard library, itself and `core`.

`core` (ADR 0001, decision 1; ADR 0004, P2-1) is the only allowed widening: pure functions on the
standard library, which import neither the engine (`ssdlc_*`, `orchestrator`) nor devloop.
"""

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'devloop'
CORE = ROOT / 'core'
# What `core` may import: no I/O, no clock, no randomness, no process or network access.
CORE_STDLIB = {'math', 're'}
# Calls that would give `core` I/O or dynamic code.
CORE_FORBIDDEN_CALLS = {'open', 'input', 'print', 'exec', 'eval', 'compile', '__import__', 'breakpoint'}


def imported_roots(path):
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split('.')[0], node.lineno
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split('.')[0], node.lineno


def impurities(path):
    """Calls of `CORE_FORBIDDEN_CALLS` and `global`/`nonlocal` statements: I/O or state."""
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in CORE_FORBIDDEN_CALLS:
            yield node.func.id, node.lineno
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            yield type(node).__name__.lower(), node.lineno


class DevloopBoundaryTests(unittest.TestCase):
    def test_only_stdlib_devloop_and_core_imports(self):
        sources = sorted(PACKAGE.rglob('*.py'))
        self.assertTrue(sources)
        allowed = set(sys.stdlib_module_names) | {'devloop', 'core'}
        for path in sources:
            for root, line in imported_roots(path):
                with self.subTest(file=path.name, line=line, module=root):
                    self.assertIn(root, allowed,
                                  f'{path.name}:{line} imports {root!r}; devloop must not depend '
                                  'on ssdlc modules or third-party packages')

    def test_detects_forbidden_imports(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / 'probe.py'
            probe.write_text('import json\nfrom orchestrator import x\nimport ssdlc_tasks.sub\n'
                             'from . import sibling\nfrom core.budget import check\n')
            self.assertEqual([r for r, _ in imported_roots(probe)],
                             ['json', 'orchestrator', 'ssdlc_tasks', 'core'])


class CoreBoundaryTests(unittest.TestCase):
    def test_core_exists(self):
        self.assertTrue((CORE / '__init__.py').is_file())
        self.assertTrue({'budget.py', 'schema.py', 'telemetry.py'} <= {p.name for p in CORE.glob('*.py')})

    def test_core_imports_only_pure_stdlib_and_itself(self):
        sources = sorted(CORE.rglob('*.py'))
        self.assertTrue(sources)
        for path in sources:
            for root, line in imported_roots(path):
                with self.subTest(file=path.name, line=line, module=root):
                    self.assertFalse(root.startswith('ssdlc_'), f'{path.name}:{line} imports the engine')
                    self.assertNotIn(root, {'devloop', 'orchestrator'}, f'{path.name}:{line} imports {root!r}')
                    self.assertIn(root, CORE_STDLIB | {'core'},
                                  f'{path.name}:{line} imports {root!r}; core is pure: no I/O, '
                                  'clock, randomness or third-party package')

    def test_core_makes_no_io_calls_and_keeps_no_global_state(self):
        for path in sorted(CORE.rglob('*.py')):
            with self.subTest(file=path.name):
                self.assertEqual(list(impurities(path)), [])

    def test_detects_impure_core_code(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / 'probe.py'
            probe.write_text('import os\nimport ssdlc_recovery\nfrom devloop import ledger\n'
                             'COUNT = 0\ndef f():\n    global COUNT\n    return open("x")\n')
            self.assertEqual([r for r, _ in imported_roots(probe)], ['os', 'ssdlc_recovery', 'devloop'])
            self.assertEqual([name for name, _ in impurities(probe)], ['global', 'open'])

    def test_core_imports_without_the_engine_or_devloop(self):
        # A fresh interpreter with only the repository root on the path: importing `core`
        # loads no engine or devloop module.
        import subprocess
        code = ('import sys; import core.budget, core.schema, core.telemetry; '
                'bad = sorted(m for m in sys.modules if m.startswith(("ssdlc_", "devloop", "orchestrator"))); '
                'print(",".join(bad))')
        result = subprocess.run([sys.executable, '-I', '-c', f'import sys; sys.path.insert(0, {str(ROOT)!r}); {code}'],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '')


if __name__ == '__main__':
    unittest.main()
