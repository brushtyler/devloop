"""ADR 0007, R1 (#538): the SSDLC engine left ``main``, with its constraints.

No file of the engine is left in the repository, no test imports it, and what devloop runs and
installs (``devloop/``, ``core/``, ``ops/``, ``scripts/``) names no path of it. The engine stays
in the tag ``ssdlc-engine-final`` (``tests/helpers/engine.py``).
"""

import os
import unittest
from pathlib import Path

from devloop import config
from helpers import engine, ssdlc_project

ROOT = Path(__file__).resolve().parents[1]
# What devloop runs, installs and documents for the host.
SEARCHED = ('devloop', 'core', 'ops', 'scripts')
# The one text that names the engine on purpose: the step on the host of R1, which says what to
# take out of the project file and where the tag goes. Only that section, up to the next one.
REMOVAL_STEP = ('ops/host/migrate.md',
                '## Il motore SSDLC esce da `main` (ADR 0007, R1, #538)\n')


def without_removal_step(path, text):
    """``text`` of ``path`` without the section ``REMOVAL_STEP``, when it is that file."""
    name, heading = REMOVAL_STEP
    if path != name:
        return text
    start = text.index(heading)
    end = text.find('\n## ', start + len(heading))
    return text[:start] + (text[end:] if end >= 0 else '')


def files(root, top):
    """Every file under ``root/top``, without byte code, relative to ``root``."""
    for directory, subdirs, names in os.walk(root / top):
        subdirs[:] = sorted(d for d in subdirs if d != '__pycache__')
        for name in sorted(names):
            yield (Path(directory) / name).relative_to(root).as_posix()


class EnginePathsTests(unittest.TestCase):
    def test_engine_paths_are_recognised(self):
        for path in ('orchestrator.py', 'ssdlc_writer_gate.py', './setup-ssdlc.sh',
                     'PLAN_AUTONOMY_WORKSTREAMS.md', 'schemas/', 'schemas/plan.schema.json',
                     'standards/00-PROJECT_CONVENTIONS.md', 'docs/task-mode.md#run',
                     'docs/evidence/*.json', 'tests/replay/benchmark.py',
                     'tests/test_review_closure.py', 'Dockerfile'):
            with self.subTest(path=path):
                self.assertTrue(engine.is_engine_path(path))
        for path in ('devloop/schemas/review.json', 'devloop/policy.py', 'core/budget.py',
                     'docs/devloop/guide.md', 'docs/architecture.md', 'tests/test_core.py',
                     'tests/helpers/ssdlc_project.py', 'pkg/ssdlc_impact.py', 'ssdlc_', ''):
            with self.subTest(path=path):
                self.assertFalse(engine.is_engine_path(path))

    def test_references_in_a_text(self):
        text = ('from `orchestrator.py` and "ssdlc_grader.py", see docs/recovery.md#x; '
                "ZERO_EXCLUDED = ('standards/', 'schemas/'); devloop/schemas/fix.json; "
                'sensitive_path_lists = ["schemas/recovery-grader-baseline.json"].')
        self.assertEqual(engine.references(text),
                         ['orchestrator.py', 'ssdlc_grader.py', 'docs/recovery.md',
                          'schemas/recovery-grader-baseline.json'])


class EngineRemovedTests(unittest.TestCase):
    def test_no_file_of_the_engine_is_left(self):
        # Every file of the checkout, without .git, byte code and the local state and tools.
        left = []
        for directory, subdirs, names in os.walk(ROOT):
            subdirs[:] = sorted(d for d in subdirs
                                if d not in ('.git', '__pycache__', '.devloop', '.ssdlc',
                                             '.tools'))
            left += [path for name in sorted(names)
                     if engine.is_engine_path(
                         path := (Path(directory) / name).relative_to(ROOT).as_posix())]
        self.assertEqual(left, [])

    def test_no_test_imports_the_engine(self):
        for path in sorted((ROOT / 'tests').rglob('*.py')):
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                self.assertIsNone(engine.ENGINE_IMPORT.search(path.read_text(encoding='utf-8')))

    def test_devloop_ops_and_scripts_name_no_path_of_the_engine(self):
        found = {}
        for top in SEARCHED:
            for path in files(ROOT, top):
                text = (ROOT / path).read_bytes().decode('utf-8', errors='replace')
                text = without_removal_step(path, text)
                if engine.references(text):
                    found[path] = engine.references(text)
        self.assertEqual(found, {})

    def test_the_removal_step_is_on_the_host_guide(self):
        # The step is there, it names the tag, and leaving it out of the search hides nothing
        # else: the rest of the guide is searched as any other file.
        name, heading = REMOVAL_STEP
        text = (ROOT / name).read_text(encoding='utf-8')
        start = text.index(heading)
        end = text.find('\n## ', start + len(heading))
        # The section alone: a later section (#563) may name the tag too.
        section = text[start:] if end < 0 else text[start:end]
        for phrase in ('`ssdlc-engine-final`', 'git tag -a ssdlc-engine-final',
                       '`sensitive_path_lists`', '`source_prefixes`', '`[docs.map]`',
                       'Isolated A5 Recovery Benchmark', '**Rollback.**'):
            self.assertIn(phrase, section)
        self.assertEqual(text.count(heading), 1)
        self.assertEqual(without_removal_step(name, text).count('ssdlc-engine-final'),
                         text.count('ssdlc-engine-final') - section.count('ssdlc-engine-final'))

    def test_ssdlc_project_file_has_no_constraint_of_the_engine(self):
        project = ssdlc_project.project()
        policy = project['review_policy']
        self.assertNotIn('sensitive_path_lists', policy)
        self.assertEqual([p for p in policy['sensitive_paths'] if p.rstrip('/') == 'schemas'
                          or engine.is_engine_path(p)], [])
        self.assertNotIn('source_prefixes', project['tests'])
        for key, documents in config.docs_entries(project):
            with self.subTest(key=key):
                self.assertFalse(engine.is_engine_path(key))
                self.assertEqual([d for d in documents if engine.is_engine_path(d)], [])
                self.assertTrue((ROOT / key).exists(), key)


if __name__ == '__main__':
    unittest.main()
