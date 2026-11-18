"""devloop: direct imports and the end of the re-exports of ``devloop/dispatcher.py`` (#297, step
S7 of #261).

Steps S1-S6 moved names out of ``devloop/dispatcher.py`` and left it re-exporting them, so that
the existing imports kept working. Step S7 points every import at the module that defines the
name and removes the re-exports: ``devloop/dispatcher.py`` imports only the names it uses, and
nothing imports from it a name it does not define itself.
"""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DISPATCHER = ROOT / 'devloop' / 'dispatcher.py'


def parse(path):
    return ast.parse(path.read_text(encoding='utf-8'), filename=str(path))


def imported_names(tree):
    """The names the module-level imports of ``tree`` bind."""
    return {alias.asname or alias.name.split('.')[0]
            for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names}


def used_names(tree):
    """The names read anywhere in ``tree`` outside its module-level imports."""
    return {node.id
            for top in tree.body if not isinstance(top, (ast.Import, ast.ImportFrom))
            for node in ast.walk(top) if isinstance(node, ast.Name)}


def defined_names(tree):
    """The names the module-level definitions of ``tree`` bind (not its imports)."""
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names |= {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
    return names


def dispatcher_imports(path):
    """``(line, name)`` of each name ``path`` imports from ``devloop.dispatcher``."""
    package = path.parent.name == 'devloop'
    found = []
    for node in ast.walk(parse(path)):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module == 'devloop.dispatcher' or (package and node.level == 1
                                                   and node.module == 'dispatcher'):
            found += [(node.lineno, alias.name) for alias in node.names]
    return found


class DispatcherImportTests(unittest.TestCase):
    def test_the_dispatcher_imports_only_what_it_uses(self):
        tree = parse(DISPATCHER)
        self.assertEqual(sorted(imported_names(tree) - used_names(tree)), [])

    def test_nothing_imports_a_moved_name_from_the_dispatcher(self):
        defined = defined_names(parse(DISPATCHER))
        paths = sorted((ROOT / 'devloop').rglob('*.py')) + sorted((ROOT / 'tests').rglob('*.py'))
        for path in paths:
            for line, name in dispatcher_imports(path):
                with self.subTest(path=str(path.relative_to(ROOT)), line=line, name=name):
                    self.assertIn(name, defined)


if __name__ == '__main__':
    unittest.main()
