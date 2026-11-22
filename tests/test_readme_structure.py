"""The README is a compact index of the product (issue #242, ADR 0003 I6). Since R1 of ADR 0007
(#538) the product is devloop: the local engine, its getting-started guide and its documents left
``main``."""
import unittest

import doc_contracts as docs

README = docs.ROOT / 'README.md'
DEVELOPMENT = 'docs/development.md'


class ReadmeStructureTests(unittest.TestCase):
    def test_the_readme_is_compact(self):
        lines = README.read_text().splitlines()
        self.assertLessEqual(len(lines), 120)

    def test_the_readme_presents_devloop_with_an_explicit_status(self):
        components = docs.plan_section('README.md', 'Components', root=docs.ROOT)
        docs.assert_documented(self, components, ['devloop', '`devloop/`', '`core/`'])
        self.assertEqual(components.missing(['Local engine', '`orchestrator.py`']),
                         ['Local engine', '`orchestrator.py`'])
        intro = README.read_text().split('\n## ', 1)[0]
        for phrase in ('ssdlc-engine-final', '(docs/adr/0007-da-ssdlc-a-devloop.md)'):
            self.assertIn(phrase, intro)
        status = docs.plan_section('README.md', 'Status', root=docs.ROOT)
        docs.assert_documented(self, status, [
            'devloop is how this repository is developed, and it is configured for it',
            'Merges are explicit',
            'Cost accounting is partial'])
        direction = docs.plan_section('README.md', 'Direction', root=docs.ROOT)
        docs.assert_documented(self, direction, ['ADR 0007', 'R1', 'ssdlc-engine-final',
                                                 'brushtyler/devloop'])

    def test_the_development_guide_is_linked_and_covers_its_scope(self):
        # Issue #246 (ADR 0003, I9): tests, documentation tests, devloop CI, map, pinned controller.
        self.assertIn(DEVELOPMENT, docs.user_facing_docs().names)
        developing = docs.plan_section('README.md', 'Developing SSDLC', root=docs.ROOT)
        docs.assert_documented(self, developing, ['(docs/development.md)'])
        sections = {
            'Running the tests': ['scripts/run-tests.py', '-j N',
                                  "python3 -m unittest discover -s tests -p 'test_name.py'",
                                  'tests/test_engine_removed.py'],
            'Documentation tests': ['tests/test_doc_links.py', 'tests/doc_contracts.py',
                                    '<!-- doc-links: ignore -->', 'user_facing_docs()',
                                    'records of their time'],
            'CI': ['.github/workflows/devloop.yml', '.github/workflows/test.yml',
                   'without installing the requirements', 'Isolated A5 Recovery Benchmark'],
            'Documents follow the code': ['[docs.map]', '#203',
                                          'Who changes a mapped file updates its documents',
                                          'blocking finding'],
            'The pinned controller': ['python3 -m devloop pin <ref>', 'scripts/devloop-pinned'],
        }
        for heading, phrases in sections.items():
            docs.assert_documented(self, docs.plan_section(DEVELOPMENT, heading, root=docs.ROOT),
                                   phrases)

    def test_every_documentation_page_stays_reachable_from_the_readme(self):
        names = set(docs.user_facing_docs().names)
        pages = sorted(path.relative_to(docs.ROOT).as_posix()
                       for pattern in ('docs/*.md', 'docs/devloop/*.md')
                       for path in docs.ROOT.glob(pattern))
        self.assertEqual([page for page in pages if page not in names], [])


if __name__ == '__main__':
    unittest.main()
