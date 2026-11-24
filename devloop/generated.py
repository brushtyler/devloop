"""Generated files in the commits of devloop (#627).

devloop commits everything a writing turn leaves in its worktree (``commit_all``), the
products of the tests included: a project that tracks its bytecode, or has no ``.gitignore``
for it, would see the ``__pycache__/`` of the tests in the PR. devloop does not leave them out,
since a tracked file may be wanted: it points them out, in the PR or in the fix record, so that
whoever reviews the change sees them."""

# A path under one of these directories, or with one of these suffixes, looks generated:
# cache and bytecode of Python.
GENERATED_DIRS = frozenset({'__pycache__'})
GENERATED_SUFFIXES = ('.pyc', '.pyo')
# At most this many files listed in the comment; the marker keeps them all.
MAX_LISTED = 30


def looks_generated(name):
    """Whether the path ``name`` (relative, with ``/``) looks like a cache or bytecode file."""
    *dirs, base = name.split('/')
    return any(d in GENERATED_DIRS for d in dirs) or base.endswith(GENERATED_SUFFIXES)


def changed_by_tests(tested):
    """The files the passing test ``tested`` (a ledger entry, or None) changed while it ran."""
    if tested is None:
        return []
    return list(tested.record.get('changed_by_tests') or [])


def generated_files(committed, by_tests=()):
    """The files of a commit to point out: among ``committed``, those that look generated and
    those the local tests changed (``by_tests``), sorted, each ``{path, reasons}``."""
    by_tests = set(by_tests)
    found = []
    for name in sorted(set(committed)):
        reasons = ([] if not looks_generated(name) else ['generated']) + (
            ['tests'] if name in by_tests else [])
        if reasons:
            found.append({'path': name, 'reasons': reasons})
    return found


_REASON_TEXT = {'generated': 'cache o bytecode', 'tests': 'modificato dai test locali'}


def render_generated(files):
    """The files of ``generated_files`` as Markdown, under their own heading; '' without
    any."""
    if not files:
        return ''
    parts = ['### File generati nel commit', '',
             'Il commit contiene file che sembrano generati (cache o bytecode, come '
             '`__pycache__/` e `*.pyc`) o che i test locali eseguiti da devloop hanno modificato. '
             'devloop non li esclude, perché un file tracciato può essere voluto: chi rivede la '
             'modifica verifichi che l\'issue li richieda.', '']
    for entry in files[:MAX_LISTED]:
        path = entry['path'].replace('`', "'").replace('\n', ' ')
        reasons = ', '.join(_REASON_TEXT[r] for r in entry['reasons'])
        parts.append(f'- `{path}` ({reasons})')
    if len(files) > MAX_LISTED:
        parts.append(f'- … e altri {len(files) - MAX_LISTED}')
    return '\n'.join(parts)
