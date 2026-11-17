"""The README is a compact index of the product, and the getting-started guide holds the
introduction moved out of it (issue #242, ADR 0003 I6)."""
import unittest

import doc_contracts as docs

README = docs.ROOT / 'README.md'
GUIDE = 'docs/getting-started.md'


class ReadmeStructureTests(unittest.TestCase):
    def test_the_readme_is_compact(self):
        lines = README.read_text().splitlines()
        self.assertLessEqual(len(lines), 120)

    def test_the_readme_presents_one_product_with_an_explicit_status(self):
        components = docs.plan_section('README.md', 'Components', root=docs.ROOT)
        docs.assert_documented(self, components, [
            'Local engine', '`orchestrator.py`, `ssdlc_*.py`', 'devloop', '`devloop/`'])
        status = docs.plan_section('README.md', 'Status', root=docs.ROOT)
        docs.assert_documented(self, status, [
            'devloop is how this repository is developed, and it is configured for it',
            'not packaged for use on other repositories',
            'guarantees stay unchanged until parity',
            'SSDLC phases on the tracker do not exist yet',
            'The local engine never merges on its own',
            'Integrity checks are not authentication'])
        direction = docs.plan_section('README.md', 'Direction', root=docs.ROOT)
        docs.assert_documented(self, direction, ['Tracker ports', 'done', '`devloop/ports.py`',
                                                 'Convergence of the local engine'])

    def test_the_getting_started_guide_holds_the_introduction(self):
        corpus = docs.user_facing_docs()
        self.assertIn(GUIDE, corpus.names)
        for heading in ('Requirements', 'Installation', 'Your first project',
                        'Basic configuration', 'Everyday commands', 'Where things are stored'):
            docs.plan_section(GUIDE, heading, root=docs.ROOT)
        docs.assert_documented(self, docs.plan_section(GUIDE, 'Installation', root=docs.ROOT),
                               ['./setup-ssdlc.sh --bootstrap'])
        docs.assert_documented(self, docs.plan_section(GUIDE, 'Everyday commands',
                                                       root=docs.ROOT),
                               ['./ssdlc doctor', 'continuation-*', 'usage-profile-*'])
        # Moved, not copied: the README keeps no quick start of its own.
        readme = README.read_text()
        for moved in ('./setup-ssdlc.sh --bootstrap', 'SSDLC_EXECUTION_MODE', '## Requirements'):
            self.assertNotIn(moved, readme)

    def test_every_documentation_page_stays_reachable_from_the_readme(self):
        names = set(docs.user_facing_docs().names)
        pages = sorted(path.relative_to(docs.ROOT).as_posix()
                       for pattern in ('docs/*.md', 'docs/devloop/*.md')
                       for path in docs.ROOT.glob(pattern))
        self.assertEqual([page for page in pages if page not in names], [])
        # The anchor that links published before the recovery index (#245) still use.
        docs.plan_section('README.md', 'Recovery and continuation', root=docs.ROOT)


if __name__ == '__main__':
    unittest.main()
