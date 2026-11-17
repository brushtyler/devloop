"""The split of docs/devloop.md moved its content without rewriting it (#241, ADR 0003, I4).

``tests/fixtures/devloop_split.json`` is the ledger of the split. It lists every block of
the original document (``source``: docs/devloop.md at the ``main`` revision the split
started from) in order: a paragraph, a list, a table, a heading or a fenced code block.
Each block is identified by the digest of its normalized text and has one destination:

- a page (``index``, ``guide``, ``reference``, ``protocol``, or ``architecture``, the
  "Ports and adapters" section of docs/architecture.md), where it must appear unchanged;
- ``adjusted``: the block was split between pages, or a link in it was rewritten. Its
  entry lists the parts, in order, and the edits; undoing the edits on the joined parts
  must give back the original digest;
- ``adr-0002``: the block duplicated ADR 0002 and became a pointer to it.

Normalization ignores whitespace, link targets and heading levels, so a moved block may
change its links and its heading level. Any other change is an explicit edit, and an edit
can only add or rewrite a link (``link``) or rename a path to docs/devloop.md (``path``).
Every block of the pages must come from the ledger: the original blocks, the parts of the
adjusted ones and the blocks the split added (``added``: headings, sentences that link
to another page, and the text of the index).

The ledger records the split, not a rule for later changes. A later change that edits,
adds or removes a block of these pages on purpose (the rewrite of the guide is I5, #243)
updates its entries or drops this test.
"""
import hashlib
import json
import re
import subprocess
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / 'tests' / 'fixtures' / 'devloop_split.json'
ARCHITECTURE_SECTION = '\n## Ports and adapters\n'
ADR_0002 = '0002-ruoli-e-responsabilita.md'

_FENCE = re.compile(r'^\s*(`{3,}|~{3,})')
_LINK_TARGET = re.compile(r'\]\([^)]*\)')
_HEADING = re.compile(r'^#{1,6}\s+')


def blocks(text):
    """The blocks of a Markdown text: runs of non-blank lines, a heading line on its own,
    a fenced code block whole (blank lines included)."""
    found, current, fence = [], [], None
    for line in text.splitlines():
        stripped = line.strip()
        if fence:
            current.append(line)
            if stripped.startswith(fence):
                found.append('\n'.join(current))
                current, fence = [], None
            continue
        opening = _FENCE.match(line)
        if opening:
            if current:
                found.append('\n'.join(current))
            current, fence = [line], opening.group(1)
        elif not stripped:
            if current:
                found.append('\n'.join(current))
            current = []
        elif stripped.startswith('#') and not current:
            found.append(line)
        else:
            current.append(line)
    if current:
        found.append('\n'.join(current))
    return found


def is_heading(block):
    return '\n' not in block.strip() and _HEADING.match(block.strip()) is not None


def normalize(block):
    """Whitespace collapsed, link targets removed (``[text]()``), heading marks removed."""
    text = _LINK_TARGET.sub(']()', block.strip())
    if is_heading(text):
        text = _HEADING.sub('', text)
    return ' '.join(text.split())


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


def load_ledger(path=LEDGER):
    return json.loads(path.read_text(encoding='utf-8'))


def page_texts(ledger, root=ROOT):
    """The text of each destination page; for ``architecture``, its "Ports and adapters"
    section only, up to the next second-level heading."""
    texts = {}
    for name, path in ledger['pages'].items():
        text = (root / path).read_text(encoding='utf-8')
        if name == 'architecture':
            text = text[text.index(ARCHITECTURE_SECTION) + 1:]
            end = text.find('\n## ', 1)
            text = text if end < 0 else text[:end + 1]
        texts[name] = text
    return texts


def page_blocks(texts):
    """For each page, the normalized text of its blocks by digest, and their multiset."""
    found = {}
    for name, text in texts.items():
        normalized = [normalize(block) for block in blocks(text)]
        found[name] = ({digest(item): item for item in normalized},
                       Counter(digest(item) for item in normalized))
    return found


def undo(text, edits):
    """The text before ``edits``; each edit's new text must occur exactly once."""
    problems = []
    for kind, old, new in edits:
        count = text.count(new)
        if count != 1:
            problems.append(f'{kind} edit {new!r} occurs {count} times')
            continue
        text = text.replace(new, old)
    return ' '.join(text.split()), problems


def edit_problems(edits):
    problems = []
    for kind, old, new in edits:
        if kind == 'link':
            if ']()' not in new or new.count(']()') < old.count(']()'):
                problems.append(f'link edit {new!r} adds or rewrites no link')
        elif kind == 'path':
            if 'docs/devloop.md' not in old or 'docs/devloop/' not in new:
                problems.append(f'path edit {old!r} -> {new!r} does not move a docs/devloop.md path')
        else:
            problems.append(f'unknown edit kind {kind!r}')
    return problems


def split_problems(ledger, texts):
    """Every reason why the pages are not the ledger's split of the original document."""
    pages = page_blocks(texts)
    expected = {name: Counter() for name in pages}
    problems = []
    for destination, block, section in ledger['original']:
        if destination == 'adr-0002':
            continue
        if destination == 'adjusted':
            entry = ledger['adjusted'].get(block)
            if entry is None:
                problems.append(f'{section}: adjusted block {block} has no entry')
                continue
            parts = []
            for page, part in entry['parts']:
                expected[page][part] += 1
                if part not in pages[page][0]:
                    problems.append(f'{section}: part {part} of block {block} is not in {page}')
                    continue
                parts.append(pages[page][0][part])
            problems += [f'{section}: {problem}' for problem in edit_problems(entry['edits'])]
            restored, undone = undo(' '.join(parts), entry['edits'])
            problems += [f'{section}: {problem}' for problem in undone]
            if len(parts) == len(entry['parts']) and not undone and digest(restored) != block:
                problems.append(f'{section}: the parts of block {block} are not the original text')
            continue
        if destination not in pages:
            problems.append(f'{section}: unknown destination {destination!r}')
            continue
        expected[destination][block] += 1
        if block not in pages[destination][1]:
            problems.append(f'{section}: block {block} is not in {destination}')
    for page, block, kind in ledger['added']:
        expected[page][block] += 1
        text = pages[page][0].get(block)
        if text is None:
            problems.append(f'added {kind} block {block} is not in {page}')
        elif kind == 'link' and ']()' not in text:
            problems.append(f'added block {block} of {page} has no link')
        elif kind == 'index' and page != 'index':
            problems.append(f'added block {block} of {page} is index text outside the index')
        elif kind not in ('link', 'heading', 'index'):
            problems.append(f'added block {block} of {page} has unknown kind {kind!r}')
    headings = {name: {digest(normalize(block)) for block in blocks(text) if is_heading(block)}
                for name, text in texts.items()}
    for page, block, kind in ledger['added']:
        if kind == 'heading' and block in pages[page][0] and block not in headings[page]:
            problems.append(f'added block {block} of {page} is not a heading')
    for name, (by_digest, found) in pages.items():
        for block, count in sorted((found - expected[name]).items()):
            problems.append(f'{name}: block not in the ledger ({count}x): {by_digest[block][:120]}')
    if any(destination == 'adr-0002' for destination, _, _ in ledger['original']):
        if f'](../adr/{ADR_0002})' not in texts['guide']:
            problems.append('guide: the sections that duplicated ADR 0002 have no pointer to it')
    return problems


def original_text(revision, path='docs/devloop.md'):
    """The original document from git, or None when the revision is not available."""
    try:
        result = subprocess.run(['git', 'show', f'{revision}:{path}'], cwd=ROOT,
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


class SplitCheckTests(unittest.TestCase):
    """The check itself, on small pages."""

    LEDGER = {
        'pages': {'guide': 'g.md', 'protocol': 'p.md'},
        'original': [
            ['guide', digest('Title'), 'Title'],
            ['guide', digest('First paragraph, see [x]().'), 'Title'],
            ['adjusted', digest('Marker text. Effect text.'), 'Title'],
            ['adr-0002', digest('Roles table.'), 'Who does what'],
        ],
        'adjusted': {
            digest('Marker text. Effect text.'): {
                'parts': [['protocol', digest('Marker text.')],
                          ['guide', digest('Effect text. See [Marker]().')]],
                'edits': [['link', '', 'See [Marker]().']],
            },
        },
        'added': [['protocol', digest('Marker'), 'heading'],
                  ['guide', digest('Roles: see [ADR 0002]().'), 'link']],
    }
    POINTER = 'Roles: see [ADR 0002](../adr/0002-ruoli-e-responsabilita.md).\n'
    GUIDE = ('## Title\n\nFirst paragraph,\nsee [x](p.md#x).\n\nEffect text. See [Marker](p.md#marker).\n\n'
             + POINTER)
    PROTOCOL = '# Marker\n\nMarker text.\n'

    def problems(self, guide, protocol):
        return split_problems(self.LEDGER, {'guide': guide, 'protocol': protocol})

    def test_a_faithful_split_passes(self):
        self.assertEqual(self.problems(self.GUIDE, self.PROTOCOL), [])

    def test_a_rewritten_body_under_the_same_heading_fails(self):
        guide = self.GUIDE.replace('First paragraph,\nsee [x](p.md#x).', 'A different paragraph.')
        problems = self.problems(guide, self.PROTOCOL)
        self.assertIn(f'Title: block {digest("First paragraph, see [x]().")} is not in guide', problems)
        self.assertIn('guide: block not in the ledger (1x): A different paragraph.', problems)

    def test_a_block_moved_to_another_page_fails(self):
        guide = self.GUIDE.replace('First paragraph,\nsee [x](p.md#x).\n\n', '')
        problems = self.problems(guide, self.PROTOCOL + '\nFirst paragraph, see [x]().\n')
        self.assertEqual(problems, [
            f'Title: block {digest("First paragraph, see [x]().")} is not in guide',
            'protocol: block not in the ledger (1x): First paragraph, see [x]().'])

    def test_a_rewritten_part_fails(self):
        guide = self.GUIDE.replace('Effect text.', 'Effect text, reworded.')
        problems = self.problems(guide, self.PROTOCOL)
        self.assertIn(f'Title: part {digest("Effect text. See [Marker]().")} of block '
                      f'{digest("Marker text. Effect text.")} is not in guide', problems)

    def test_an_added_block_and_a_missing_pointer_fail(self):
        problems = self.problems(self.GUIDE.replace(self.POINTER, 'New text.\n'), self.PROTOCOL)
        self.assertIn('guide: block not in the ledger (1x): New text.', problems)
        self.assertIn('guide: the sections that duplicated ADR 0002 have no pointer to it', problems)

    def test_an_edit_must_add_a_link(self):
        self.assertEqual(edit_problems([['link', 'below', 'above']]),
                         ["link edit 'above' adds or rewrites no link"])
        self.assertEqual(edit_problems([['style', 'a', 'b']]), ["unknown edit kind 'style'"])
        self.assertEqual(edit_problems([['path', 'docs/x.md', 'docs/devloop/guide.md']]),
                         ["path edit 'docs/x.md' -> 'docs/devloop/guide.md' does not move a docs/devloop.md path"])

    def test_normalization_ignores_whitespace_link_targets_and_heading_levels(self):
        self.assertEqual(normalize('#### Fixes start\n'), normalize('### Fixes start'))
        self.assertEqual(normalize('see [a](#a)\n and  more'), 'see [a]() and more')
        self.assertEqual(blocks('# T\ntext\n\n```\na\n\nb\n```\nafter\n'),
                         ['# T', 'text', '```\na\n\nb\n```', 'after'])


class RepositorySplitTests(unittest.TestCase):
    def test_the_pages_are_the_split_of_the_original_document(self):
        ledger = load_ledger()
        problems = split_problems(ledger, page_texts(ledger))
        self.assertEqual(problems, [], 'docs/devloop/ differs from the ledger of the split:\n'
                         + '\n'.join(problems))

    def test_the_ledger_lists_every_block_of_the_original_document(self):
        ledger = load_ledger()
        original = original_text(ledger['source']['revision'], ledger['source']['path'])
        if original is None:
            self.skipTest('the source revision of the split is not in this clone')
        self.assertEqual([digest(normalize(block)) for block in blocks(original)],
                         [block for _, block, _ in ledger['original']])


if __name__ == '__main__':
    unittest.main()
