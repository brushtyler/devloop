"""Which tests the dispatcher runs locally after a writing turn (issue #169).

``local_tests`` is one of ``MODES``:

- ``full``: the whole ``test_cmd``, as before #169;
- ``changed``: only the test modules pertinent to the branch's diff (``select_modules``), each
  with ``python3 -m unittest discover -s tests -p '<module>'``; none when the set is empty;
- ``none``: no local test.

The CI stays the gate before review: with ``changed`` or ``none`` a review needs the CI passed,
and a head without CI gets the full ``test_cmd`` locally first (``Dispatcher._do_review``).

The same correspondence tells whether a CI failure concerns the PR (#200): ``failed_modules``
reads the modules ``scripts/run-tests.py`` reports as failed in a CI log, and a failure none of
whose modules ``select_modules`` picks for the PR's diff is re-run once instead of becoming a
round. ``suite_summary`` reads the final lines of ``scripts/run-tests.py`` in a CI job log, for
the summary of the CI in the review and fix prompts (#300).
"""

import re
import shlex

MODES = ('full', 'changed', 'none')
DEVLOOP_DIR = 'devloop/'
DEVLOOP_TESTS = 'test_devloop'
# What scripts/run-tests.py prints for each failed module and at the end of a failed suite.
# In a `gh run view --log-failed` log each line starts with the job, the step and a timestamp.
_FAILED_MODULE = re.compile(r'(?:^|\s)FAILED MODULE (\S+)\s*$')
_FAILED_SUMMARY = re.compile(r'(?:^|\s)FAILED \(modules=(\d+), worker problems=(\d+)\)\s*$')
_MODULE = re.compile(r'test_\w+')
# The last lines of every run of scripts/run-tests.py (#300): "Slowest: …" and "Ran N tests in
# S (… modules, … split by class, … jobs)". unittest's own "Ran N tests in S" has no parentheses.
_SUITE_RAN = re.compile(r'(?:^|\s)(Ran \d+ tests? in [\d.]+s \(\d+ modules?, [^()`\n]*jobs?\))'
                        r'\s*$')
_SUITE_SLOWEST = re.compile(r'(?:^|\s)(Slowest: [^`\n]*?)\s*$')
SUITE_LINE_LIMIT = 300


def failed_modules(log):
    """The test modules (``test_x.py``) that a CI log of ``scripts/run-tests.py`` reports as
    failed, sorted, or None when the log does not say which failed for sure: no final summary,
    a worker problem, as many ``FAILED MODULE`` lines as the summary counts, or a name that is
    not a test module."""
    names, summary = [], None
    for line in (log or '').splitlines():
        if match := _FAILED_MODULE.search(line):
            names.append(match.group(1))
        elif match := _FAILED_SUMMARY.search(line):
            summary = (int(match.group(1)), int(match.group(2)))
    if summary is None or summary[1] or not names or summary[0] != len(names):
        return None
    # The label of a module is its dotted name (``test_x``, or ``tests.test_x``).
    modules = [name.rsplit('.', 1)[-1] for name in names]
    if not all(_MODULE.fullmatch(m) for m in modules):
        return None
    return sorted({f'{m}.py' for m in modules})


def suite_summary(log):
    """``{ran, slowest}`` of the last run of ``scripts/run-tests.py`` in a CI job log (#300):
    its final line "Ran N tests in S (… modules, … jobs)" and its "Slowest" line (None when
    missing), each cut to ``SUITE_LINE_LIMIT`` characters. None when the log has no such final
    line: the job did not run ``scripts/run-tests.py``, or did not reach its end."""
    ran = slowest = None
    for line in (log or '').splitlines():
        if match := _SUITE_RAN.search(line):
            ran = match.group(1)
        elif match := _SUITE_SLOWEST.search(line):
            slowest = match.group(1)
    if ran is None:
        return None
    return {'ran': ran[:SUITE_LINE_LIMIT],
            'slowest': slowest[:SUITE_LINE_LIMIT] if slowest is not None else None}


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
