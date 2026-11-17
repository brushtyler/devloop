"""devloop: the documentation map in the implement, fix and review prompts (issue #203)."""

import dataclasses
import tempfile
import tomllib
import unittest
from pathlib import Path

from devloop import config
from devloop.dispatcher import Dispatcher
from devloop.workspace import Workspace

from helpers.devloop_fakes import FakeGH
from test_devloop import (CFG, FakeRunner, FakeWS, finding, fixed, implemented, norm, pr,
                          raw_review)

REPO = Path(__file__).resolve().parent.parent
BLOCKING = ('A behaviour described in one of these documents that the change alters without '
            'updating that document is a **blocking** finding.')


def config_data():
    with open(config.DEFAULT_CONFIG, 'rb') as fh:
        return tomllib.load(fh)


class MapConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # The list file the policy reads, and two documents.
        (self.root / 'schemas').mkdir()
        (self.root / 'schemas/recovery-grader-baseline.json').write_text('{"files": {}}')
        (self.root / 'docs').mkdir()
        (self.root / 'docs/a.md').write_text('a')
        (self.root / 'README.md').write_text('r')

    def parse(self, mapping):
        data = dict(config_data(), docs={'map': mapping})
        return config.parse_config(data, self.root).docs

    def test_this_repository_maps_devloop_to_its_document(self):
        entries = dict(CFG.docs.entries)
        self.assertEqual(entries['devloop/'], ('docs/devloop/guide.md',))
        # #241: the three pages of docs/devloop/ (ADR 0003, I4); the generic entry adds the guide.
        self.assertEqual(CFG.docs.documents(['devloop/dispatcher.py']),
                         {document: ['devloop/dispatcher.py'] for document in
                          ('docs/devloop/guide.md', 'docs/devloop/protocol.md',
                           'docs/devloop/reference.md')})
        self.assertEqual(sorted(CFG.docs.documents(['devloop/config.toml'])),
                         ['docs/devloop/guide.md', 'docs/devloop/reference.md'])
        self.assertEqual(sorted(CFG.docs.documents(['devloop/schemas/review.json'])),
                         ['docs/devloop/guide.md', 'docs/devloop/protocol.md'])
        self.assertEqual(sorted(CFG.docs.documents(['devloop/policy.py'])),
                         ['docs/devloop/guide.md', 'docs/devloop/protocol.md',
                          'docs/devloop/reference.md'])
        self.assertEqual(sorted(CFG.docs.documents(['devloop/ports.py', 'devloop/github.py'])),
                         ['docs/architecture.md', 'docs/devloop/guide.md'])
        self.assertEqual(CFG.docs.documents(['scripts/devloop-pinned']),
                         {'docs/devloop/guide.md': ['scripts/devloop-pinned']})
        # #242: the README is an index (ADR 0003, I6); the engine maps to the getting-started guide.
        for path in ('orchestrator.py', 'ssdlc_tasks.py', 'ssdlc_controller.py'):
            self.assertEqual(CFG.docs.documents([path]),
                             {'docs/getting-started.md': [path], 'docs/architecture.md': [path]})
        self.assertEqual(CFG.docs.documents(['setup-ssdlc.sh']),
                         {'docs/getting-started.md': ['setup-ssdlc.sh'],
                          'docs/configuration.md': ['setup-ssdlc.sh']})
        self.assertNotIn('README.md', {d for _, docs in CFG.docs.entries for d in docs})
        # #245: the recovery transitions also map to the recovery index (ADR 0003, I8).
        self.assertEqual(CFG.docs.documents(['ssdlc_scope_amendments.py']),
                         {'docs/scope-amendments.md': ['ssdlc_scope_amendments.py'],
                          'docs/recovery.md': ['ssdlc_scope_amendments.py']})
        for path in ('ssdlc_continuations.py', 'ssdlc_historical.py'):
            self.assertEqual(CFG.docs.documents([path]),
                             {'docs/continuations.md': [path],
                              'docs/historical-reconciliation.md': [path],
                              'docs/recovery.md': [path]})
        self.assertNotIn('docs/devloop.md', {d for _, docs in CFG.docs.entries for d in docs})
        for _, documents in CFG.docs.entries:
            for document in documents:
                self.assertTrue((REPO / document).is_file(), document)
        # No glob in the keys: the feedback modules are listed one by one.
        self.assertEqual(sorted(k for k, _ in CFG.docs.entries if k.startswith('ssdlc_feedback')),
                         sorted(p.name for p in REPO.glob('ssdlc_feedback*.py')))

    def test_a_valid_map_loads_in_order(self):
        docs = self.parse({'devloop/': ['docs/a.md'], 'x.py': ['README.md', 'docs/a.md']})
        self.assertEqual(docs.entries, (('devloop/', ('docs/a.md',)),
                                        ('x.py', ('README.md', 'docs/a.md'))))
        data = config_data()
        del data['docs']
        self.assertEqual(config.parse_config(data, self.root).docs, config.DocsMap())

    def test_absolute_empty_or_invalid_entries_are_refused(self):
        bad = [
            {'/abs/': ['docs/a.md']},           # absolute key
            {'devloop/': ['/etc/passwd']},      # absolute document
            {'': ['docs/a.md']},                # empty key
            {' ': ['docs/a.md']},               # blank key
            {'devloop/': []},                   # empty entry
            {'devloop/': ['']},                 # empty document
            {'devloop/': 'docs/a.md'},          # not a list
            {'devloop/': [1]},                  # not a string
            {'ssdlc_feedback*.py': ['docs/a.md']},   # glob
            {'devloop/[ab].py': ['docs/a.md']},      # pattern
            {'../x.py': ['docs/a.md']},              # escaping
            {'a//b.py': ['docs/a.md']},              # empty segment
            {'./x.py': ['docs/a.md']},
            {' x.py': ['docs/a.md']},                # padded
            {'x.py': ['docs/']},                     # a directory as document
            {'x.py': ['docs/../README.md']},
        ]
        for mapping in bad:
            with self.subTest(mapping=mapping), self.assertRaises(config.ConfigError):
                self.parse(mapping)
        for table in ({'map': {}, 'other': 1}, {'map': ['x']}, 'x'):
            with self.subTest(table=table), self.assertRaises(config.ConfigError):
                config.parse_config(dict(config_data(), docs=table), self.root)

    def test_documents_missing_from_the_installation_load(self):
        # #210: the configuration checks the shape of the map only. A pinned copy holds no
        # documents; the dispatcher looks for them in the worktree of each turn.
        (self.root / 'docs/link.md').symlink_to('a.md')
        mapping = {'x.py': ['docs/a.md', 'docs/none.md'], 'y.py': ['docs'],
                   'z.py': ['docs/link.md']}
        self.assertEqual(self.parse(mapping).entries,
                         (('x.py', ('docs/a.md', 'docs/none.md')), ('y.py', ('docs',)),
                          ('z.py', ('docs/link.md',))))
        # The repository's own map loads in an installation with none of its documents.
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        root = Path(empty.name)
        (root / 'schemas').mkdir()
        (root / 'schemas/recovery-grader-baseline.json').write_text('{"files": {}}')
        self.assertEqual(config.parse_config(config_data(), root).docs, CFG.docs)

    def test_the_overlay_may_set_the_map(self):
        data = config.apply_overlay(config_data(), {'docs': {'map': {'x.py': ['README.md']}}})
        docs = config.parse_config(data, REPO).docs
        self.assertEqual(dict(docs.entries)['x.py'], ('README.md',))
        self.assertIn('devloop/', dict(docs.entries))
        with self.assertRaises(config.ConfigError):
            config.apply_overlay(config_data(), {'docs': {'other': 1}})

    def test_paths_select_their_documents(self):
        docs = config.DocsMap(entries=(('devloop/', ('docs/devloop.md',)),
                                       ('orchestrator.py', ('README.md', 'docs/architecture.md')),
                                       ('schemas/', ('docs/configuration.md',))))
        self.assertEqual(docs.documents(['devloop/dispatcher.py', 'devloop/prompts/fix.md',
                                         'orchestrator.py', 'other.py']),
                         {'README.md': ['orchestrator.py'],
                          'docs/architecture.md': ['orchestrator.py'],
                          'docs/devloop.md': ['devloop/dispatcher.py', 'devloop/prompts/fix.md']})
        # A file is matched exactly, a directory only by its prefix ending in "/".
        self.assertEqual(docs.documents(['orchestrator.pyc', 'devloopx/a.py', 'schemas',
                                         'tests/test_x.py', 'docs/devloop.md']), {})


class WorkspaceFilesTests(unittest.TestCase):
    def test_missing_files_are_those_not_regular_files_of_the_worktree(self):
        # #210: a mapped document counts only as a regular file of the worktree.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'docs').mkdir()
            (root / 'docs/a.md').write_text('a')
            (root / 'docs/link.md').symlink_to('a.md')
            ws = Workspace.__new__(Workspace)  # missing_files reads only the worktree
            self.assertEqual(ws.missing_files(root, ['docs/a.md', 'docs/none.md', 'docs',
                                                     'docs/link.md']),
                             ['docs/none.md', 'docs', 'docs/link.md'])


class PromptTests(unittest.TestCase):
    def make(self, runner, files, tests=(True,), absent=()):
        self.gh, self.ws = FakeGH(), FakeWS(files=files, tests=tests)
        self.ws.absent = absent
        self.logs = []
        # The full suite runs after each write turn, so a failure makes a second attempt.
        cfg = dataclasses.replace(CFG, local_tests='full')
        return Dispatcher(cfg, self.gh, self.ws, runner, log=self.logs.append)

    def implement(self, files, tests=(True,), absent=()):
        runner = FakeRunner(implemented(), implemented())
        self.make(runner, files, tests, absent)._do_implement(7)
        return [call[3] for call in runner.calls]

    def fix(self, files, absent=()):
        runner = FakeRunner(fixed())
        first = norm(raw_review('request_changes', [finding()]))
        self.make(runner, files, absent=absent)._do_fix(pr(labels=['agent:changes'], reviews=[first]))
        return runner.calls[0][3]

    def review(self, files, absent=()):
        runner = FakeRunner(raw_review())
        d = self.make(runner, files, absent=absent)
        p = pr()
        self.gh.labels[100] = set(p.labels)
        d._do_review(p)
        return runner.calls[0][3]

    def test_the_first_implement_prompt_gets_the_whole_map(self):
        prompt = self.implement(['x.py'])[0]
        self.assertIn('## Documentation map', prompt)
        self.assertIn('Your diff is still empty, so this is the whole map.', prompt)
        self.assertIn('- `devloop/` → `docs/devloop/guide.md`', prompt)
        self.assertIn('- `devloop/dispatcher.py` → `docs/devloop/reference.md`, '
                      '`docs/devloop/protocol.md`', prompt)
        self.assertIn('- `orchestrator.py` → `docs/getting-started.md`, `docs/architecture.md`',
                      prompt)
        self.assertLess(prompt.index('## Documentation map'), prompt.index('## Issue #7'))

    def test_a_later_implement_attempt_gets_the_documents_of_its_diff(self):
        first, second = self.implement(['devloop/dispatcher.py'], tests=(False, True))
        self.assertIn('## Documentation map', first)
        self.assertNotIn('## Documentation map', second)
        self.assertIn('## Documents mapped to the diff', second)
        for document in ('guide', 'protocol', 'reference'):
            self.assertIn(f'- `docs/devloop/{document}.md`, mapped from `devloop/dispatcher.py`',
                          second)
        self.assertIn('FAILED test_x', second)

    def test_a_later_implement_attempt_on_an_unmapped_file_adds_nothing(self):
        prompts = self.implement(['x.py'], tests=(False, True))
        self.assertEqual(len(prompts), 2)
        self.assertNotIn('## Documentation map', prompts[1])
        self.assertNotIn('## Documents mapped to the diff', prompts[1])
        self.assertNotIn('docs/devloop/', prompts[1])

    def test_the_fix_prompt_lists_the_documents_of_the_diff(self):
        prompt = self.fix(['devloop/dispatcher.py'])
        self.assertIn('## Documents mapped to the diff', prompt)
        self.assertIn('- `docs/devloop/guide.md`, mapped from `devloop/dispatcher.py`', prompt)
        self.assertLess(prompt.index('## Documents mapped'), prompt.index('## Issue #7'))
        self.assertNotIn('docs/devloop/', self.fix(['x.py']))
        self.assertNotIn('## Documents mapped to the diff', self.fix(['x.py']))

    def test_the_review_prompt_lists_the_documents_and_the_blocking_rule(self):
        prompt = self.review(['devloop/dispatcher.py'])
        self.assertIn('## Documents mapped to the diff', prompt)
        self.assertIn('- `docs/devloop/guide.md`, mapped from `devloop/dispatcher.py`', prompt)
        self.assertIn(BLOCKING, prompt)
        self.assertIn('When a mapped document stays correct without changes, say so in '
                      '`checks_performed`.', prompt)
        self.assertLess(prompt.index('## Documents mapped'), prompt.index('## Diff summary'))
        unmapped = self.review(['x.py'])
        self.assertNotIn('## Documents mapped to the diff', unmapped)
        self.assertNotIn(BLOCKING, unmapped)
        self.assertNotIn('docs/devloop/', unmapped)

    def test_many_files_are_counted_after_the_first_ones(self):
        files = [f'devloop/m{i}.py' for i in range(7)]
        prompt = self.review(files)
        self.assertIn('`devloop/m4.py` and 2 more', prompt)
        self.assertNotIn('devloop/m5.py', prompt)

    def test_a_document_missing_from_the_worktree_is_omitted_with_a_log_line(self):
        # #210: the review lists only the mapped documents the worktree holds.
        # devloop/watch.py is covered only by the generic devloop/ entry, the guide.
        files = ['devloop/watch.py', 'orchestrator.py']
        guide = 'docs/devloop/guide.md'
        line = f'  docs.map: {guide} is not a file of the worktree; omitted from the prompt'
        prompt = self.review(files, absent=(guide,))
        self.assertNotIn(guide, prompt)
        self.assertIn('- `docs/getting-started.md`, mapped from `orchestrator.py`', prompt)
        self.assertIn(BLOCKING, prompt)
        self.assertIn(line, self.logs)
        # Every mapped document missing: no section at all.
        prompt = self.review(['devloop/watch.py'], absent=(guide,))
        self.assertNotIn('## Documents mapped to the diff', prompt)
        self.assertIn(line, self.logs)
        prompt = self.fix(['devloop/watch.py'], absent=(guide,))
        self.assertNotIn(guide, prompt)
        self.assertIn(line, self.logs)
        # Nothing missing, nothing logged.
        self.review(files)
        self.assertFalse([m for m in self.logs if 'docs.map' in m])

    def test_the_whole_map_omits_the_documents_missing_from_the_worktree(self):
        guide = 'docs/devloop/guide.md'
        first, second = self.implement(['devloop/watch.py'], tests=(False, True),
                                       absent=(guide, 'docs/architecture.md'))
        self.assertIn('## Documentation map', first)
        self.assertNotIn(guide, first)
        self.assertNotIn('`devloop/` →', first)  # a key left with no document
        self.assertNotIn('`devloop/ports.py` →', first)
        self.assertIn('- `orchestrator.py` → `docs/getting-started.md`\n', first)
        self.assertNotIn(guide, second)
        self.assertNotIn('## Documents mapped to the diff', second)
        for document in ('docs/architecture.md', guide):
            self.assertIn(f'  docs.map: {document} is not a file of the worktree; omitted from '
                          'the prompt', self.logs)

    def test_without_a_map_no_section_is_added(self):
        runner = FakeRunner(implemented(), raw_review())
        gh, ws = FakeGH(), FakeWS(files=['devloop/dispatcher.py'])
        cfg = dataclasses.replace(CFG, local_tests='full', docs=config.DocsMap())
        d = Dispatcher(cfg, gh, ws, runner, log=lambda m: None)
        d._do_implement(7)
        self.assertNotIn('## Documentation map', runner.calls[0][3])
        self.assertNotIn('docs/devloop/', runner.calls[0][3])


class DocumentationTests(unittest.TestCase):
    def test_the_devloop_document_describes_the_map(self):
        text = (REPO / 'docs/devloop/reference.md').read_text(encoding='utf-8')
        section = text[text.index('### Documentation map'):]
        section = section[:section.index('\n### ', 1)]
        for phrase in ('[docs.map]', '"devloop/" = ["docs/devloop/guide.md"]', 'First implement attempt',
                       'blocking', '`checks_performed`', 'asks for nothing',
                       'checks its shape only', 'omitted from the list'):
            self.assertIn(phrase, section)
        self.assertIn('documentation map](#documentation-map)', text)


if __name__ == '__main__':
    unittest.main()
