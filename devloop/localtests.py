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

The project declares both (ADR 0006, R5, ``Tests``): the selection is a strategy of the
installation, with a name, and the project's name rules as its parameters (``SELECTIONS``); the
format of the CI logs has a name too (``CI_LOG_FORMATS``). The code holds no rule of a project.
Without a selection ``changed`` is ``full``; without a format no CI log is read for the summary,
and ``ci_rerun_unrelated`` is off (``config.parse_config``).
"""

import re
import shlex
from dataclasses import dataclass

MODES = ('full', 'changed', 'none')
# The selection strategies of the installation (ADR 0006, R5): ``python-unittest-modules`` is the
# algorithm of #169, with its name rules as parameters (``Tests.source_prefixes`` and
# ``Tests.dir_modules``).
UNITTEST_MODULES = 'python-unittest-modules'
SELECTIONS = (UNITTEST_MODULES,)
# The formats of the CI logs the dispatcher reads (ADR 0006, R5): ``run-tests`` is what
# ``scripts/run-tests.py`` prints (``failed_modules``, ``suite_summary``).
RUN_TESTS_LOG = 'run-tests'
CI_LOG_FORMATS = (RUN_TESTS_LOG,)


@dataclass(frozen=True)
class Tests:
    """``[tests]`` of the configuration (ADR 0006, R5), facts of the project. ``selection``:
    the strategy of ``local_tests = "changed"``, '' without one. ``source_prefixes``: with each
    ``<prefix>``, a changed ``<prefix><name>.py`` also selects ``test_<name>*.py``.
    ``dir_modules``: ``(directory, module prefix)`` pairs, any file under the directory
    selecting the modules whose name starts with the prefix. ``ci_log_format``: the format of
    the CI logs, '' without one."""
    selection: str = ''
    source_prefixes: tuple = ()
    dir_modules: tuple = ()
    ci_log_format: str = ''
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


def _prefixes(stem, source_prefixes):
    """The name prefixes of the test modules of a source module named ``stem``: ``test_<stem>``,
    and for ``<prefix><name>``, with ``<prefix>`` one of ``source_prefixes``, also
    ``test_<name>``."""
    prefixes = [f'test_{stem}']
    for prefix in source_prefixes:
        if stem.startswith(prefix) and len(stem) > len(prefix):
            prefixes.append(f'test_{stem[len(prefix):]}')
    return prefixes


def select_modules(changed, modules, tests):
    """The names among ``modules`` (the ``tests/test_*.py`` files of the worktree) that the
    changed paths ``changed`` make pertinent with the strategy of ``tests`` (``Tests``), sorted.
    With ``python-unittest-modules``:

    - a changed or added ``tests/test_*.py`` that still exists;
    - for every other changed ``X.py`` (or ``pkg/X.py``), the modules ``test_X*.py``, and for
      ``<prefix><name>.py``, with ``<prefix>`` one of ``source_prefixes``, also
      ``test_<name>*.py``;
    - for any file under a directory of ``dir_modules``, the modules whose name starts with
      its prefix.

    Without a strategy nothing can tell which modules are pertinent: ValueError. The
    configuration makes ``changed`` ``full`` then, and turns ``ci_rerun_unrelated`` off."""
    if tests.selection != UNITTEST_MODULES:
        raise ValueError(f'no test selection strategy {tests.selection!r}')
    available = set(modules)
    chosen = set()
    for path in changed:
        name = path.rsplit('/', 1)[-1]
        for directory, prefix in tests.dir_modules:
            if path.startswith(directory):
                chosen.update(m for m in available if m.startswith(prefix))
        if path == f'tests/{name}' and name.startswith('test_') and name.endswith('.py'):
            if name in available:
                chosen.add(name)
            continue
        stem = name[:-len('.py')] if name.endswith('.py') else ''
        if stem:
            prefixes = _prefixes(stem, tests.source_prefixes)
            chosen.update(m for m in available if m.startswith(tuple(prefixes)))
    return sorted(chosen)


def module_command(modules):
    """One shell command running ``modules`` one at a time, stopping at the first that fails, so
    that its output ends the tail the implementer gets back."""
    return ' && '.join(f'python3 -m unittest discover -s tests -p {shlex.quote(m)}'
                       for m in modules)
