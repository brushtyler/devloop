#!/usr/bin/env python3
"""Run the unittest suite in parallel processes, with exactly the tests of a full discover.

    python3 scripts/run-tests.py [-j N]

The suite is ``unittest.defaultTestLoader.discover('tests')``, the same as
``python3 -m unittest discover -s tests``: packages, ``load_tests`` hooks and modules with the same
name in different packages are handled by unittest itself. Its top-level children (one per module)
are split across N worker processes, slowest first, using the durations of the previous local run
(``.devloop/test-durations.json``, ignored by git, #563) and, for the modules it does not list,
those of the versioned ``tests/durations.json``, written from a full run: a fresh checkout, as
in CI, has no local history. A module whose duration is above SPLIT_SECONDS is split by test class when ``units``
allows it, so its classes run in different workers. A module with no duration in either file stays
whole, since each class would repeat the imports and fixtures of the module, and is estimated at
0.5 seconds per test. Each worker repeats the same discover and runs only its parts. The summary at the end gives the total, the slowest modules and the output of every
failed module, aggregated per module even when it was split; the exit code is non-zero if any test
failed or a worker did not finish.

Before the workers start, a root that is a git repository gets one ``git update-index -q
--refresh``: in a fresh checkout, as in CI, the index has stale stat data and every ``git status``
of the tests would otherwise rehash all files. It is only an optimisation: a failure is reported and
the run goes on.

A worker imports ``unittest`` and runs the discover before loading this file, as
``python3 -m unittest`` does: the discover puts ``tests/`` first on ``sys.path``, so a helper there
named like a standard module (``tests/json.py``) must reach the tests exactly as in the serial run.
For the same reason the module level below imports only what ``unittest`` has already loaded, and
the worker writes its results with ``repr`` instead of ``json``; the main process imports the rest.
"""

import io
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
# A module whose recorded duration is longer than this is split by class across the workers.
SPLIT_SECONDS = 60
# The estimated cost of a test whose module has no recorded duration; such a module is not split.
SECONDS_PER_TEST = 0.5
# After the discover the boot loads this file with the builtins open, compile and exec only:
# runpy, importlib.util and the like would import modules (pkgutil, ...) that tests/ can shadow.
WORKER_BOOT = '\n'.join([
    'import sys, unittest',
    'children = list(unittest.defaultTestLoader.discover("tests"))',
    'sys.argv = sys.argv[1:]',
    'with open(sys.argv[0], encoding="utf-8") as fh:',
    '    source = fh.read()',
    'runner = {"__name__": "run_tests_worker", "__file__": sys.argv[0]}',
    'exec(compile(source, sys.argv[0], "exec"), runner)',
    'sys.exit(runner["worker_main"](sys.argv[1:], children))',
])


def discover():
    """The top-level children of the full discover, as ``python3 -m unittest discover -s tests``."""
    os.chdir(ROOT)
    # Same import path as `python3 -m unittest`: the working directory first, and not the
    # directory of this script, which `python3 scripts/run-tests.py` would otherwise add.
    here = os.path.dirname(os.path.realpath(__file__))
    sys.path[:] = [p for p in sys.path if p and os.path.realpath(p) != here]
    sys.path.insert(0, ROOT)
    return list(unittest.defaultTestLoader.discover('tests'))


def label(child):
    """A stable name for a child suite: the module of its first test."""
    stack = [child]
    while stack:
        item = stack.pop(0)
        if isinstance(item, unittest.TestSuite):
            stack = list(item) + stack
        else:
            return item.id().rsplit('.', 2)[0] if item.id().count('.') >= 2 else item.id()
    return None


def test_ids(suite):
    """Every test id in ``suite``, in order."""
    if isinstance(suite, unittest.TestSuite):
        return [i for item in suite for i in test_ids(item)]
    return [suite.id()]


def inventory(children):
    """The discovered tests, child by child, as one line of text: workers must see the same suite."""
    return repr([test_ids(c) for c in children])


def units(child):
    """The test classes of a module's suite, in order, each as the list of its tests; None when the
    suite cannot be split by class without changing how it runs.

    Only the instances the discover already found are used, so a ``load_tests`` hook keeps its
    selection, also of classes defined in other modules. The suite stays whole when: it or a nested
    suite is not a plain ``unittest.TestSuite`` (a custom suite may run its tests its own way); an
    item is not a ``TestCase``; the tests of a class are not contiguous; a module of its classes has
    ``setUpModule`` or ``tearDownModule`` (each worker would run them again); it has fewer than two
    classes. A class is never split, so ``setUpClass`` runs once, as in the serial discover."""
    tests = []

    def flatten(suite):
        if type(suite) is not unittest.TestSuite:
            return False
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                if not flatten(item):
                    return False
            elif isinstance(item, unittest.TestCase):
                tests.append(item)
            else:
                return False
        return True

    if not flatten(child):
        return None
    groups, seen = [], set()
    for test in tests:
        cls = type(test)
        if groups and type(groups[-1][0]) is cls:
            groups[-1].append(test)
            continue
        module = sys.modules.get(cls.__module__)
        if (cls in seen or module is None or hasattr(module, 'setUpModule')
                or hasattr(module, 'tearDownModule')):
            return None
        seen.add(cls)
        groups.append([test])
    return groups if len(groups) > 1 else None


def worker(children, parts, results_file, expected_file):
    """Run each part: ``(i, None)`` is ``children[i]``, ``(i, u)`` its class ``units(...)[u]``.
    ``children`` comes from the discover in WORKER_BOOT."""
    with open(expected_file, encoding='utf-8') as fh:
        expected = fh.read()
    if inventory(children) != expected:
        print('The discovered tests differ from the ones the main process found: a module changes '
              'the test tree while it is imported, or discovery is not deterministic.', flush=True)
        return 3
    # Every suite is built before any runs: running a suite empties it.
    split = {i: units(children[i]) for i, u in parts if u is not None}
    suites = [children[i] if u is None else unittest.TestSuite(split[i][u]) for i, u in parts]
    with open(results_file, 'a', encoding='utf-8') as out:
        for (index, unit), suite in zip(parts, suites):
            stream = io.StringIO()
            start = time.monotonic()
            result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)
            # One Python literal per line, read back with ast.literal_eval: json is not loaded yet.
            out.write(repr(dict(index=index, unit=unit, run=result.testsRun,
                                ok=result.wasSuccessful(), skipped=len(result.skipped),
                                seconds=round(time.monotonic() - start, 1),
                                output=stream.getvalue())) + '\n')
            out.flush()
    return 0


def parse_part(text):
    """``I`` is the whole child I, ``I.U`` its class U: the inverse of ``format_part``."""
    index, _, unit = text.partition('.')
    return int(index), int(unit) if unit else None


def format_part(part):
    return str(part[0]) if part[1] is None else f'{part[0]}.{part[1]}'


def worker_main(argv, children):
    """Entry point of WORKER_BOOT: ``--worker P,Q,... --results FILE --inventory FILE``."""
    opts = dict(zip(argv[::2], argv[1::2]))
    return worker(children, [parse_part(p) for p in opts['--worker'].split(',') if p],
                  opts['--results'], opts['--inventory'])


def plan(children, labels, previous):
    """The parts to run with their estimated cost: ``[((index, unit), seconds), ...]``.
    ``previous`` maps a module label to its recorded duration; a module without one is not split."""
    parts = []
    for i, child in enumerate(children):
        seconds = previous.get(labels[i])
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
            seconds = None
        groups = units(child) if seconds is not None and seconds > SPLIT_SECONDS else None
        if groups:
            total = sum(len(g) for g in groups)
            for u, group in enumerate(groups):
                parts.append(((i, u), seconds * len(group) / total))
        else:
            parts.append(((i, None), seconds if seconds is not None
                          else child.countTestCases() * SECONDS_PER_TEST))
    return parts


def refresh_index():
    """One ``git update-index -q --refresh`` on a root that is a git repository; a failure is
    reported and ignored."""
    import subprocess  # here, not at module level: see the module docstring
    if not os.path.exists(os.path.join(ROOT, '.git')):
        return
    try:
        done = subprocess.run(['git', 'update-index', '-q', '--refresh'], cwd=ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f'git update-index --refresh failed, continuing: {type(exc).__name__}: {exc}',
              flush=True)
        return
    if done.returncode != 0:
        print(f'git update-index --refresh exited with {done.returncode}, continuing: '
              f'{done.stdout.strip()}', flush=True)


def main(argv):
    # Imported here, not at module level: a worker loads this file after its discover (see the
    # module docstring), when these names would resolve to tests/ first.
    import argparse
    import ast
    import json
    import subprocess
    import tempfile
    import threading
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('-j', '--jobs', type=int, default=os.cpu_count() or 2)
    args = parser.parse_args(argv)

    def read(path):
        try:
            found = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        return found if isinstance(found, dict) else {}

    durations = Path(ROOT) / '.devloop' / 'test-durations.json'
    refresh_index()
    children = discover()
    labels = [label(c) or f'#{i}' for i, c in enumerate(children)]
    previous = read(durations)
    # The local history wins; the versioned file covers what it lacks, as in a fresh checkout.
    parts = plan(children, labels, {**read(Path(ROOT) / 'tests' / 'durations.json'), **previous})
    expected_parts = {}
    for (i, u), _ in parts:
        expected_parts.setdefault(i, []).append(u)
    jobs = max(1, min(args.jobs, len(parts)))
    bins = [[] for _ in range(jobs)]
    load = [0.0] * jobs
    for part, cost in sorted(parts, key=lambda p: p[1], reverse=True):
        k = load.index(min(load))
        bins[k].append(part)
        load[k] += cost

    def module(i, partial=False):
        """The results of child i, its parts merged; None until every part has one, unless
        ``partial`` (then None only without any)."""
        found = [rows[(i, u)] for u in expected_parts[i] if (i, u) in rows]
        if not found or (len(found) < len(expected_parts[i]) and not partial):
            return None
        return dict(run=sum(r['run'] for r in found), ok=all(r['ok'] for r in found),
                    skipped=sum(r.get('skipped', 0) for r in found),
                    seconds=round(sum(r['seconds'] for r in found), 1),
                    output='\n'.join(r['output'].rstrip() for r in found))

    start = time.monotonic()
    rows, problems, printed = {}, [], set()
    lock = threading.Lock()
    with tempfile.TemporaryDirectory() as tmp:
        expected = Path(tmp) / 'inventory.txt'
        expected.write_text(inventory(children), encoding='utf-8')

        def run(k):
            try:
                collect(k)
            except Exception as exc:  # any failure here must fail the run, never vanish
                with lock:
                    problems.append(f'worker {k}: {type(exc).__name__}: {exc}')

        def collect(k):
            results, log = Path(tmp) / f'w{k}.results', Path(tmp) / f'w{k}.log'
            with open(log, 'w') as fh:
                # `python3 -c` puts the working directory first on sys.path before any import, as
                # `python3 -m unittest` does; WORKER_BOOT then runs the discover before loading
                # this file, so the tests see the same modules as in the serial discover.
                code = subprocess.run([sys.executable, '-c', WORKER_BOOT, __file__, '--worker',
                                       ','.join(map(format_part, bins[k])), '--results', str(results),
                                       '--inventory', str(expected)],
                                      cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT).returncode
            done = ([ast.literal_eval(line) for line in results.read_text(encoding='utf-8').splitlines()]
                    if results.exists() else [])
            with lock:
                for row in done:
                    rows[(row['index'], row['unit'])] = row
                # A split module is reported once, when the last of its parts is in.
                for i in sorted({row['index'] for row in done} - printed):
                    if (merged := module(i)) is not None:
                        printed.add(i)
                        print(f'{"ok  " if merged["ok"] else "FAIL"} {labels[i]} '
                              f'({merged["run"]} tests, {merged["seconds"]:.0f}s)', flush=True)
                missing = list(dict.fromkeys(labels[i] for i, u in bins[k] if (i, u) not in rows))
                if code != 0 or missing:
                    tail = '\n'.join(log.read_text(errors='replace').splitlines()[-40:])
                    problems.append(f'worker {k} exited with {code} before finishing '
                                    f'{", ".join(missing) or "-"}\n{tail}')
        threads = [threading.Thread(target=run, args=(k,)) for k in range(jobs) if bins[k]]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    elapsed = time.monotonic() - start
    modules = {i: m for i in range(len(children)) if (m := module(i)) is not None}
    missing = [labels[i] for i in range(len(children)) if i not in modules]
    if missing and not problems:
        problems.append('no result for ' + ', '.join(missing))

    try:
        durations.parent.mkdir(exist_ok=True)
        durations.write_text(json.dumps({**previous, **{labels[i]: m['seconds'] for i, m in modules.items()}},
                                        indent=1, sort_keys=True))
    except OSError:
        pass

    # A module split by class still counts once, with the output of all its parts, so a CI log
    # keeps one FAILED MODULE line per failed module (devloop/localtests.py reads them).
    # A module with only some parts done (its worker died) is reported if one of them failed.
    reported = {i: m for i in range(len(children)) if (m := module(i, partial=True)) is not None}
    failed = [i for i, m in sorted(reported.items()) if not m['ok']]
    for i in failed:
        print(f'\n{"=" * 70}\nFAILED MODULE {labels[i]}\n{"=" * 70}\n{reported[i]["output"].rstrip()}')
    for problem in problems:
        print(f'\n{"=" * 70}\nWORKER PROBLEM\n{"=" * 70}\n{problem}')
    slowest = sorted(modules.items(), key=lambda item: item[1]['seconds'], reverse=True)[:5]
    print('\nSlowest: ' + ', '.join(f'{labels[i]} {m["seconds"]:.0f}s' for i, m in slowest))
    print('-' * 70)
    split = sum(1 for us in expected_parts.values() if us != [None])
    print(f'Ran {sum(r["run"] for r in rows.values())} tests in {elapsed:.3f}s '
          f'({len(children)} modules, {split} split by class, {jobs} jobs)')
    if failed or problems:
        print(f'\nFAILED (modules={len(failed)}, worker problems={len(problems)})')
        return 1
    if not sum(r['run'] for r in rows.values()) and not sum(r.get('skipped', 0) for r in rows.values()):
        print('\nNO TESTS RAN')
        return 5  # as `python3 -m unittest` does since Python 3.12
    print('\nOK')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
