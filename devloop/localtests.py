"""Which tests the dispatcher runs locally after a writing turn (issue #169).

``local_tests`` is one of ``MODES``:

- ``full``: the whole ``test_cmd``, as before #169;
- ``changed``: only the test modules pertinent to the branch's diff (``select_modules``), each
  with ``python3 -m unittest discover -s tests -p '<module>'``; none when the set is empty;
- ``none``: no local test.

The CI stays the gate before review: with ``changed`` or ``none`` a review needs the CI passed,
and a head without CI gets the full ``test_cmd`` locally first (``Dispatcher._do_review``).
"""

import shlex

MODES = ('full', 'changed', 'none')
DEVLOOP_DIR = 'devloop/'
DEVLOOP_TESTS = 'test_devloop'


def _prefixes(stem):
    """The name prefixes of the test modules of a source module named ``stem``: ``test_<stem>``,
    and for ``ssdlc_<name>`` also ``test_<name>``."""
    prefixes = [f'test_{stem}']
    if stem.startswith('ssdlc_') and len(stem) > len('ssdlc_'):
        prefixes.append(f'test_{stem[len("ssdlc_"):]}')
    return prefixes


def select_modules(changed, modules):
    """The names among ``modules`` (the ``tests/test_*.py`` files of the worktree) that the
    changed paths ``changed`` make pertinent, sorted:

    - a changed or added ``tests/test_*.py`` that still exists;
    - for every other changed ``X.py`` (or ``pkg/X.py``), the modules ``test_X*.py``, and for
      ``ssdlc_<name>.py`` also ``test_<name>*.py``;
    - for any file under ``devloop/``, the modules ``test_devloop*.py``."""
    available = set(modules)
    chosen = set()
    for path in changed:
        name = path.rsplit('/', 1)[-1]
        if path.startswith(DEVLOOP_DIR):
            chosen.update(m for m in available if m.startswith(DEVLOOP_TESTS))
        if path == f'tests/{name}' and name.startswith('test_') and name.endswith('.py'):
            if name in available:
                chosen.add(name)
            continue
        stem = name[:-len('.py')] if name.endswith('.py') else ''
        if stem:
            prefixes = _prefixes(stem)
            chosen.update(m for m in available if m.startswith(tuple(prefixes)))
    return sorted(chosen)


def module_command(modules):
    """One shell command running ``modules`` one at a time, stopping at the first that fails, so
    that its output ends the tail the implementer gets back."""
    return ' && '.join(f'python3 -m unittest discover -s tests -p {shlex.quote(m)}'
                       for m in modules)
