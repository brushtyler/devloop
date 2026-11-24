"""Generated files in the commits of devloop (#627).

devloop commits everything a writing turn leaves in its worktree (``commit_all``), the
products of the tests included: a project that tracks its bytecode, or has no ``.gitignore``
for it, would see the ``__pycache__/`` of the tests in the PR. devloop does not leave them out,
since a tracked file may be wanted: it points them out, in the PR or in the fix record, so that
whoever reviews the change sees them."""

import json

from .i18n import ENGLISH, Catalog

# The section of the generated files in the PR and in a fix record, in the project's
# ``language`` (#635); English for a language without translation. ``reason_*``: why a file is
# listed.
TEXTS = Catalog(
    en={
        'title': '### Generated files in the commit',
        'intro': ('The commit holds files that look generated (cache or bytecode, such as '
                  '`__pycache__/` and `*.pyc`), binary files or files the local tests run by '
                  'devloop changed. devloop does not leave them out, since a tracked file may be '
                  'wanted: whoever reviews the change checks that the issue asks for them.'),
        'reason_generated': 'cache or bytecode',
        'reason_binary': 'binary',
        'reason_tests': 'changed by the local tests',
        'more': '- … and {more} more',
    },
    it={
        'title': '### File generati nel commit',
        'intro': ('Il commit contiene file che sembrano generati (cache o bytecode, come '
                  '`__pycache__/` e `*.pyc`), file binari o file che i test locali eseguiti da '
                  'devloop hanno modificato. devloop non li esclude, perché un file tracciato può '
                  'essere voluto: chi rivede la modifica verifichi che l\'issue li richieda.'),
        'reason_generated': 'cache o bytecode',
        'reason_binary': 'binario',
        'reason_tests': 'modificato dai test locali',
        'more': '- … e altri {more}',
    })

# A path under one of these directories, or with one of these suffixes, looks generated:
# cache and bytecode of Python.
GENERATED_DIRS = frozenset({'__pycache__'})
GENERATED_SUFFIXES = ('.pyc', '.pyo')
# At most this many files listed in a text and kept in its marker or record alike, and at most
# this many characters of the body spent on them (:func:`cost`): the others are only counted, so
# that a commit full of caches does not take the body beyond the limit of a GitHub text
# (``markdown.MAX_BODY``).
MAX_LISTED = 30
MAX_CHARS = 12000
# The characters of an entry beyond its path, readable and in the marker, at most: the bullet,
# the reasons and the JSON keys (R2-1).
ENTRY_OVERHEAD = 150


def looks_generated(name):
    """Whether the path ``name`` (relative, with ``/``) looks like a cache or bytecode file."""
    *dirs, base = name.split('/')
    return any(d in GENERATED_DIRS for d in dirs) or base.endswith(GENERATED_SUFFIXES)


def cost(name):
    """The characters the path ``name`` takes in a published body (R2-1): its readable line,
    and its form in the marker, where ``json.dumps`` escapes every character outside ASCII (up to
    12 characters for one outside the basic plane) and Base64 turns 3 bytes into 4; plus
    ``ENTRY_OVERHEAD``."""
    return len(name) + 4 * -(-len(json.dumps(name)) // 3) + ENTRY_OVERHEAD


def capped(paths):
    """The first of ``paths`` within ``MAX_LISTED`` and, by :func:`cost`, ``MAX_CHARS``: those a
    record keeps."""
    kept, size = [], 0
    for name in paths:
        size += cost(name)
        if len(kept) == MAX_LISTED or size > MAX_CHARS:
            break
        kept.append(name)
    return kept


def changed_by_tests(tested):
    """The files the passing test ``tested`` (a ledger entry, or None) changed while it ran, as
    its record keeps them (:func:`capped`)."""
    if tested is None:
        return []
    return list(tested.record.get('changed_by_tests') or [])


def generated_files(committed, by_tests=(), binary=()):
    """The files of a commit to point out: among ``committed``, those that look generated,
    those git sees as binary (``binary``: also a binary the agent's own tests regenerated during
    its turn, which the test of the dispatcher no longer changes) and those the local tests
    changed (``by_tests``), sorted, each ``{path, reasons}``."""
    by_tests, binary = set(by_tests), set(binary)
    found = []
    for name in sorted(set(committed)):
        reasons = (['generated'] if looks_generated(name) else []) + (
            ['binary'] if name in binary else []) + (['tests'] if name in by_tests else [])
        if reasons:
            found.append({'path': name, 'reasons': reasons})
    return found


def _kept(files):
    """The entries of ``files`` (of :func:`generated_files`) that :func:`capped` keeps."""
    return files[:len(capped([entry['path'] for entry in files]))]


def generated_record(files):
    """The fields of a ``pr`` or ``fix`` marker for ``files`` (of :func:`generated_files`):
    those :func:`capped` keeps in ``generated_files``, and how many it left out in
    ``generated_omitted`` when it left out any; ``{}`` without files."""
    if not files:
        return {}
    kept = _kept(files)
    record = {'generated_files': kept}
    if len(kept) < len(files):
        record['generated_omitted'] = len(files) - len(kept)
    return record


def render_generated(files, language=ENGLISH):
    """The files of :func:`generated_files` as Markdown, under their own heading, in
    ``language`` (#635): those of :func:`generated_record` listed, the others counted; ''
    without any."""
    if not files:
        return ''
    t = TEXTS.texts(language)
    parts = [t('title'), '', t('intro'), '']
    kept = _kept(files)
    for entry in kept:
        path = entry['path'].replace('`', "'").replace('\n', ' ')
        reasons = ', '.join(t(f'reason_{r}') for r in entry['reasons'])
        parts.append(f'- `{path}` ({reasons})')
    if len(files) > len(kept):
        parts.append(t('more', more=len(files) - len(kept)))
    return '\n'.join(parts)
