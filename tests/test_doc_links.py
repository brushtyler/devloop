"""Links, anchors and cited paths in the documentation resolve (issue #204).

The check reads README.md, AGENTS.md and every Markdown page under docs/ and reports:

- relative links whose file does not exist, or that leave the repository;
- anchors (``#section``) that match no heading of the target page, using GitHub's
  slugs: lower case, punctuation removed, spaces turned into ``-``, and ``-1``, ``-2``
  suffixes for duplicate headings, after removing formatting such as emphasis;
  ``id``/``name`` attributes of HTML tags outside inline code also count;
- paths cited in inline code that start with a known repository directory
  (``CODE_DIRS``) or name a ``*.py`` file at the root, and do not exist.

External links (any URL scheme, such as ``https:`` or ``mailto:``), fenced code blocks
and the line after a ``<!-- doc-links: ignore -->`` comment are not checked.
"""
import re
import tempfile
import unicodedata
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
CODE_DIRS = ('devloop/', 'scripts/', 'schemas/', 'docs/', 'tests/')
IGNORE = '<!-- doc-links: ignore -->'

# Allow-list of known broken references, each (page, target exactly as written). The
# documentation audit (#205, issue #238) emptied it: reword an illustrative name, or put
# `<!-- doc-links: ignore -->` on the line before it. Adding an entry needs a review, and
# an entry that no longer fails makes the test fail until it is removed.
ALLOWED = frozenset()

_FENCE = re.compile(r'^\s*(`{3,}|~{3,})')
_HEADING = re.compile(r'^ {0,3}#{1,6}(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$')
_CODE_SPAN = re.compile(r'(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)')
_REFERENCE = re.compile(r'^ {0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)')
_LINK_TITLE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\((?:\\.|[^()\\])*\)')
_EMPHASIS = re.compile(r'(?<![\w\\])([*_]{1,3})(?=\S)(.+?)(?<=[^\s\\])\1(?!\w)')
_SCHEME = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*:')
_ROOT_PY = re.compile(r'[\w.-]+\.py')


def documentation(root):
    """README.md, AGENTS.md and docs/**/*.md that exist under ``root``, sorted."""
    pages = [root / name for name in ('README.md', 'AGENTS.md') if (root / name).is_file()]
    return pages + sorted((root / 'docs').rglob('*.md'))


def _lines(text):
    """Yield (line number, line, ignored) for lines outside fenced code blocks."""
    fence, ignore_next = None, False
    for number, line in enumerate(text.splitlines(), 1):
        opening = _FENCE.match(line)
        if fence:
            if opening and opening.group(1)[0] == fence[0] and len(opening.group(1)) >= len(fence) \
                    and not line.strip().strip(fence[0]):
                fence = None
            continue
        if opening:
            fence, ignore_next = opening.group(1), False
            continue
        yield number, line, ignore_next
        ignore_next = line.strip() == IGNORE


def _link_destination(text, start):
    """The destination of an inline link whose ``(`` is at ``start``, or None if it is not one.

    Follows GFM: the destination is ``<...>`` or a run without spaces whose parentheses
    are balanced; an optional title in ``"..."``, ``'...'`` or ``(...)`` may follow.
    """
    position = start + 1
    while position < len(text) and text[position].isspace():
        position += 1
    if text.startswith('<', position):
        end = text.find('>', position)
        if end < 0:
            return None
        destination, position = text[position:end + 1], end + 1
    else:
        depth, begin = 0, position
        while position < len(text) and not text[position].isspace():
            char = text[position]
            if char == '\\' and position + 1 < len(text):
                position += 2
                continue
            if char == '(':
                depth += 1
            elif char == ')':
                if depth == 0:
                    break
                depth -= 1
            position += 1
        if depth:
            return None
        destination = re.sub(r'\\([!-/:-@\[-`{-~])', r'\1', text[begin:position])
    rest = text[position:]
    stripped = rest.lstrip()
    if stripped != rest and (title := _LINK_TITLE.match(stripped)):
        stripped = stripped[title.end():].lstrip()
    return destination if stripped.startswith(')') else None


def _inline_links(prose):
    """Destinations of the inline links ``[text](destination "title")`` in a line."""
    return [destination for match in re.finditer(r'\]\(', prose)
            if (destination := _link_destination(prose, match.end() - 1)) is not None]


def slug(heading):
    """GitHub's anchor for a heading's text, before the duplicate suffix.

    Formatting is removed first, as GitHub derives the anchor from the rendered text:
    emphasis delimiters go, while intraword, escaped and inline-code underscores stay.
    Inline code is swapped for a placeholder character while formatting is removed, so
    emphasis that wraps code is recognised and the code's own content is left untouched.
    """
    codes = []
    heading = re.sub('[\U000F0000-\U000FFFFD]', '', heading)  # never in real headings

    def placeholder(code):
        content = code.group(2)
        if content.startswith(' ') and content.endswith(' ') and content.strip():
            content = content[1:-1]
        codes.append(content)
        return chr(0xF0000 + len(codes) - 1)  # private-use character: not a word character

    text = _CODE_SPAN.sub(placeholder, heading)
    text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'<[^>]+>', '', text)
    previous = None
    while previous != text:
        previous, text = text, _EMPHASIS.sub(r'\2', text)
    text = re.sub('[\U000F0000-\U000FFFFD]', lambda char: codes[ord(char.group()) - 0xF0000], text).lower()
    kept = ''.join(ch for ch in text
                   if ch in ' -' or unicodedata.category(ch)[0] in 'LNM' or unicodedata.category(ch) == 'Pc')
    return kept.replace(' ', '-')


class _AnchorParser(HTMLParser):
    """Collects the values of the real ``id`` and ``name`` attributes of HTML start tags."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found = set()

    def handle_starttag(self, tag, attrs):
        self.found.update(value for name, value in attrs if name in ('id', 'name') and value)


def _html_anchors(line):
    """``id``/``name`` attributes of the HTML tags in a line, outside inline code."""
    parser = _AnchorParser()
    parser.feed(_CODE_SPAN.sub('', line))
    parser.close()
    return parser.found


def anchors(text):
    """Every anchor a page offers: heading slugs with duplicate suffixes, and HTML ids."""
    found, occurrences = set(), {}
    for _, line, _ in _lines(text):
        found.update(_html_anchors(line))
        heading = _HEADING.match(line)
        if not heading:
            continue
        original = result = slug(heading.group(1) or '')
        while result in occurrences:
            occurrences[original] += 1
            result = f'{original}-{occurrences[original]}'
        occurrences[result] = 0
        found.add(result)
    return found


class Problem:
    def __init__(self, page, line, target, message):
        self.page, self.line, self.target, self.message = page, line, target, message

    @property
    def key(self):
        return self.page, self.target

    def __str__(self):
        return f'{self.page}:{self.line}: {self.target!r}: {self.message}'


def _check_link(root, page, target, cache):
    target = target[1:-1] if target.startswith('<') else target
    if not target or _SCHEME.match(target):
        return None
    path, _, anchor = target.partition('#')
    destination = (page.parent / unquote(path)).resolve() if path else page
    if destination != root and root not in destination.parents:
        return 'leaves the repository'
    if not destination.exists():
        return 'file not found'
    if not anchor or destination.suffix != '.md' or not destination.is_file():
        return None
    if destination not in cache:
        cache[destination] = anchors(destination.read_text(encoding='utf-8'))
    return None if unquote(anchor) in cache[destination] else 'no heading with this anchor'


def _check_cited(root, span):
    if any(ch.isspace() for ch in span) or not (span.startswith(CODE_DIRS) or _ROOT_PY.fullmatch(span)):
        return None
    if any(ch in span for ch in '*?['):
        return None if next(root.glob(span), None) else 'no file matches this pattern'
    return None if (root / span).exists() else 'cited path not found'


def broken_references(root):
    """Every broken link, anchor and cited path in the documentation under ``root``."""
    root = Path(root).resolve()
    problems, cache = [], {}
    for page in documentation(root):
        name = page.relative_to(root).as_posix()
        for number, line, ignored in _lines(page.read_text(encoding='utf-8')):
            if ignored:
                continue
            spans = [match.group(2).strip() for match in _CODE_SPAN.finditer(line)]
            prose = _CODE_SPAN.sub('', line)
            targets = _inline_links(prose) + _REFERENCE.findall(prose)
            for target in targets:
                message = _check_link(root, page, target, cache)
                if message:
                    problems.append(Problem(name, number, target, message))
            for span in spans:
                message = _check_cited(root, span)
                if message:
                    problems.append(Problem(name, number, span, message))
    return problems


class Fixture:
    def __init__(self, files):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')


class DocLinkCheckTests(unittest.TestCase):
    def broken(self, files):
        fixture = Fixture(files)
        self.addCleanup(fixture._tmp.cleanup)
        return [(problem.page, problem.target, problem.message) for problem in broken_references(fixture.root)]

    def test_valid_links_anchors_and_paths_pass(self):
        self.assertEqual(self.broken({
            'README.md': '# Intro\n[guide](docs/guide.md) [usage](docs/guide.md#usage-and-limits) '
                         '[self](#intro) [dir](docs/) [img](<docs/a b.md>)\n'
                         'See `devloop/run.py`, `docs/`, `tests/test_*.py` and `orchestrator.py`.\n',
            'docs/guide.md': '## Usage and limits\n[back](../README.md#intro)\n',
            'docs/a b.md': 'text',
            'devloop/run.py': '',
            'tests/test_x.py': '',
            'orchestrator.py': '',
        }), [])

    def test_reports_a_broken_relative_link(self):
        self.assertEqual(self.broken({
            'README.md': '[guide](docs/missing.md)\n',
            'docs/guide.md': '[up](../../outside.md) [ref]\n\n[ref]: gone.md\n',
        }), [
            ('README.md', 'docs/missing.md', 'file not found'),
            ('docs/guide.md', '../../outside.md', 'leaves the repository'),
            ('docs/guide.md', 'gone.md', 'file not found'),
        ])

    def test_link_titles_and_parentheses_in_destinations(self):
        self.assertEqual(self.broken({
            'README.md': (
                "[a](docs/missing.md 'Guide') [b](docs/gone.md (Guide)) [c](docs/lost.md \"Guide\")\n"
                '[d](docs/old(v1).md) [e](docs/guide(v2).md#nowhere \'T\')\n'
                "[ok](docs/guide(v2).md) [ok](docs/guide(v2).md#usage 'Usage') "
                '[ok](docs/guide(v2).md (Usage)) [ok](<docs/guide(v2).md> "Usage") '
                '[ok](docs/guide\\(v2\\).md)\n'),
            'docs/guide(v2).md': '# Usage\n',
        }), [
            ('README.md', 'docs/missing.md', 'file not found'),
            ('README.md', 'docs/gone.md', 'file not found'),
            ('README.md', 'docs/lost.md', 'file not found'),
            ('README.md', 'docs/old(v1).md', 'file not found'),
            ('README.md', 'docs/guide(v2).md#nowhere', 'no heading with this anchor'),
        ])

    def test_reports_a_missing_anchor(self):
        self.assertEqual(self.broken({
            'README.md': '# Title\n[a](docs/g.md#old-name) [b](#nowhere) [c](docs/g.md#new-name)\n',
            'docs/g.md': '## New name\n',
        }), [
            ('README.md', 'docs/g.md#old-name', 'no heading with this anchor'),
            ('README.md', '#nowhere', 'no heading with this anchor'),
        ])

    def test_reports_a_missing_cited_path(self):
        self.assertEqual(self.broken({
            'README.md': 'See `devloop/gone.py`, `schemas/x.json`, `missing.py` and `tests/nope_*.py`.\n',
            'AGENTS.md': 'Run `scripts/run.py`.\n',
        }), [
            ('README.md', 'devloop/gone.py', 'cited path not found'),
            ('README.md', 'schemas/x.json', 'cited path not found'),
            ('README.md', 'missing.py', 'cited path not found'),
            ('README.md', 'tests/nope_*.py', 'no file matches this pattern'),
            ('AGENTS.md', 'scripts/run.py', 'cited path not found'),
        ])

    def test_github_slugs_cover_punctuation_code_and_duplicates(self):
        text = ('# Local config as an overlay\n## `devloop/` and *roles*: what?\n'
                '## Ruoli e responsabilità\n## Notes\n## Notes\n## Notes-1\n## Notes\n'
                '<a id="custom"></a>\n```\n# not a heading\n```\n')
        self.assertEqual(anchors(text), {
            'local-config-as-an-overlay', 'devloop-and-roles-what', 'ruoli-e-responsabilità',
            'notes', 'notes-1', 'notes-1-1', 'notes-2', 'custom'})

    def test_slugs_drop_emphasis_delimiters_but_keep_literal_underscores(self):
        text = ('# _Usage_\n# Usage\n## __Bold__ and ***both*** and _a_ _b_\n'
                '## snake_case, `__init__` and `_x_`\n## \\_escaped\\_\n## _nested *mix*_\n')
        self.assertEqual(anchors(text), {
            'usage', 'usage-1', 'bold-and-both-and-a-b', 'snake_case-__init__-and-_x_',
            '_escaped_', 'nested-mix'})
        self.assertEqual(self.broken({
            'README.md': '# _Usage_\n[ok](#usage) [bad](#_usage_)\n',
        }), [('README.md', '#_usage_', 'no heading with this anchor')])

    def test_emphasis_around_inline_code_is_removed_but_code_underscores_stay(self):
        text = '# _Usage `now`_\n## **Run `__init__` and `_x_`**\n## _a `b` c_ and `d_e`\n'
        self.assertEqual(anchors(text), {'usage-now', 'run-__init__-and-_x_', 'a-b-c-and-d_e'})
        self.assertEqual(self.broken({
            'README.md': '# _Usage `now`_\n[ok](#usage-now) [bad](#_usage-now_)\n',
        }), [('README.md', '#_usage-now_', 'no heading with this anchor')])

    def test_html_anchors_come_only_from_real_tags(self):
        self.assertEqual(self.broken({
            'README.md': ('[bad](#missing) [ok](#custom) [ok](#named) [bad](#data) [bad](#titled)\n'
                          'Write `id="missing"` or `<a id="missing">` to add one.\n'
                          '<a id="custom"></a> <span name=\'named\'></span> <div data-id="data"></div>\n'
                          '<div title="id=\'missing\'">example</div> <p title=\'name="titled"\'>x</p>\n'),
        }), [
            ('README.md', '#missing', 'no heading with this anchor'),
            ('README.md', '#data', 'no heading with this anchor'),
            ('README.md', '#titled', 'no heading with this anchor'),
        ])

    def test_external_links_code_blocks_and_ignored_lines_are_not_checked(self):
        self.assertEqual(self.broken({
            'README.md': (
                '[web](https://example.com/missing.md) [plain](http://x/y#z) '
                '[mail](mailto:someone@example.com)\n'
                '```markdown\n[fenced](missing.md) `devloop/missing.py`\n```\n'
                '~~~~\n[tilde](missing.md)\n```\nstill fenced](missing.md)\n~~~~\n'
                f'{IGNORE}\n'
                '[example](missing.md#anchor) `devloop/missing.py`\n'
                'Inline code is not a link: `[x](missing.md)`. Prose `devloop missing.py` is not a path.\n'
                '[checked](after-ignore.md)\n'),
        }), [('README.md', 'after-ignore.md', 'file not found')])

    def test_pages_outside_the_scope_are_not_checked(self):
        self.assertEqual(self.broken({
            'README.md': 'ok', 'PLAN_X.md': '[x](missing.md)', 'tests/replay/README.md': '[x](missing.md)',
        }), [])


class RepositoryDocLinksTests(unittest.TestCase):
    def test_documentation_links_anchors_and_paths_resolve(self):
        problems = broken_references(ROOT)
        unexpected = [str(problem) for problem in problems if problem.key not in ALLOWED]
        self.assertEqual(unexpected, [], 'broken references in the documentation:\n' + '\n'.join(unexpected))
        fixed = sorted(ALLOWED - {problem.key for problem in problems})
        self.assertEqual(fixed, [], 'allow-listed references that now resolve: remove them from ALLOWED')

    def test_scope_covers_readme_agents_and_docs(self):
        names = [page.relative_to(ROOT).as_posix() for page in documentation(ROOT)]
        self.assertEqual(names[:2], ['README.md', 'AGENTS.md'])
        self.assertIn('docs/devloop.md', names)
        for page in ('guide', 'reference', 'protocol'):
            self.assertIn(f'docs/devloop/{page}.md', names)
        self.assertIn('docs/adr/0002-ruoli-e-responsabilita.md', names)

    def test_the_devloop_document_is_an_index_of_its_three_pages(self):
        # #241 (ADR 0003, I4): docs/devloop.md is an index, the content lives in docs/devloop/.
        index = (ROOT / 'docs' / 'devloop.md').read_text(encoding='utf-8')
        self.assertLess(len(index.splitlines()), 30)
        for page in ('guide', 'reference', 'protocol'):
            self.assertIn(f'](devloop/{page}.md)', index)
        headings = {page: [line for line in (ROOT / 'docs' / 'devloop' / f'{page}.md')
                           .read_text(encoding='utf-8').splitlines() if line.startswith('## ')]
                    for page in ('guide', 'reference', 'protocol')}
        for page, expected in (
                ('guide', ('## The loop', '## Merge modes', '## Who does what', '## Usage',
                           '## Limits')),
                ('reference', ('## Configuration', '## Review level and policy receipt',
                               '## GitHub identities')),
                ('protocol', ('## Conflicts with `main`', '## Rounds', '## Receipts',
                              '## Structured review', '## Attribution',
                              '## Evidence and telemetry'))):
            for heading in expected:
                self.assertIn(heading, headings[page], page)
        architecture = (ROOT / 'docs' / 'architecture.md').read_text(encoding='utf-8')
        self.assertIn('\n## Ports and adapters\n', architecture)

    def test_the_architecture_describes_the_product_before_the_engine(self):
        # #244 (ADR 0003, I7): components, engine and dispatcher, ports and adapters, the
        # devloop boundary and the future `core` package, then the local engine.
        architecture = (ROOT / 'docs' / 'architecture.md').read_text(encoding='utf-8')
        headings = [line for line in architecture.splitlines() if line.startswith('## ')]
        product = ['## Components', '## Engine and dispatcher', '## Ports and adapters',
                   '## The devloop boundary', '## Future `core` package']
        self.assertEqual(headings[:len(product)], product)
        self.assertEqual(headings[len(product)], '## Phases')
        self.assertIn('\n### Task journal: guarantees and threat model\n', architecture)
        sections = {heading: architecture.split(f'\n{heading}\n', 1)[1].split('\n## ', 1)[0]
                    for heading in product}
        for code in ('`orchestrator.py`', '`ssdlc_*.py`', '`devloop/`'):
            self.assertIn(code, sections['## Components'])
        self.assertIn('`tests/test_devloop_boundary.py`', sections['## The devloop boundary'])
        self.assertIn('adr/0001-prodotto-unico-fasi-su-tracker.md', architecture)
        for phrase in ('`tests/test_devloop_boundary.py`', 'Pure validators only',
                       '`ssdlc_*`'):
            self.assertIn(phrase, sections['## Future `core` package'])


if __name__ == '__main__':
    unittest.main()
