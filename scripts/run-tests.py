#!/usr/bin/env python3
"""Run the unittest suite in parallel processes, with exactly the tests of a full discover.

    python3 scripts/run-tests.py [-j N]

The suite is ``unittest.defaultTestLoader.discover('tests')``, the same as
``python3 -m unittest discover -s tests``: packages, ``load_tests`` hooks and modules with the same
name in different packages are handled by unittest itself. Its top-level children (one per module)
are split across N worker processes, slowest first, using the durations of the previous run
(``.ssdlc/test-durations.json``, ignored by git). Each worker repeats the same discover and runs only
its children. The summary at the end gives the total, the slowest modules and the output of every
failed module; the exit code is non-zero if any test failed or a worker did not finish.
"""

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DURATIONS = ROOT / '.ssdlc' / 'test-durations.json'


def discover():
    """The top-level children of the full discover, as ``python3 -m unittest discover -s tests``."""
    os.chdir(ROOT)
    # Same import path as `python3 -m unittest`: the working directory first, and not the
    # directory of this script, which `python3 scripts/run-tests.py` would otherwise add.
    here = Path(__file__).resolve().parent
    sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != here]
    sys.path.insert(0, str(ROOT))
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
    """A fingerprint of the discovered tests, child by child: workers must see the same suite."""
    return hashlib.sha256(json.dumps([test_ids(c) for c in children]).encode()).hexdigest()


def worker(indices, results_file, expected):
    children = discover()
    if inventory(children) != expected:
        print('The discovered tests differ from the ones the main process found: a module changes '
              'the test tree while it is imported, or discovery is not deterministic.', flush=True)
        return 3
    with open(results_file, 'a', encoding='utf-8') as out:
        for index in indices:
            child = children[index]
            stream = io.StringIO()
            start = time.monotonic()
            result = unittest.TextTestRunner(stream=stream, verbosity=1).run(child)
            out.write(json.dumps(dict(index=index, run=result.testsRun, ok=result.wasSuccessful(),
                                      skipped=len(result.skipped),
                                      seconds=round(time.monotonic() - start, 1),
                                      output=stream.getvalue())) + '\n')
            out.flush()
    return 0


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('-j', '--jobs', type=int, default=os.cpu_count() or 2)
    parser.add_argument('--worker', help=argparse.SUPPRESS)
    parser.add_argument('--results', help=argparse.SUPPRESS)
    parser.add_argument('--inventory', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker is not None:
        return worker([int(i) for i in args.worker.split(',') if i], args.results, args.inventory)

    children = discover()
    expected = inventory(children)
    labels = [label(c) or f'#{i}' for i, c in enumerate(children)]
    try:
        previous = json.loads(DURATIONS.read_text())
    except (OSError, ValueError):
        previous = {}
    cost = [previous.get(labels[i], c.countTestCases() * 0.5) for i, c in enumerate(children)]
    jobs = max(1, min(args.jobs, len(children)))
    bins = [[] for _ in range(jobs)]
    load = [0.0] * jobs
    for i in sorted(range(len(children)), key=lambda i: cost[i], reverse=True):
        k = load.index(min(load))
        bins[k].append(i)
        load[k] += cost[i]

    start = time.monotonic()
    rows, problems = {}, []
    lock = threading.Lock()
    with tempfile.TemporaryDirectory() as tmp:
        def run(k):
            try:
                collect(k)
            except Exception as exc:  # any failure here must fail the run, never vanish
                with lock:
                    problems.append(f'worker {k}: {type(exc).__name__}: {exc}')

        def collect(k):
            results, log = Path(tmp) / f'w{k}.jsonl', Path(tmp) / f'w{k}.log'
            with open(log, 'w') as fh:
                code = subprocess.run([sys.executable, __file__, '--worker', ','.join(map(str, bins[k])),
                                       '--results', str(results), '--inventory', expected],
                                      cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT).returncode
            done = [json.loads(line) for line in results.read_text().splitlines()] if results.exists() else []
            with lock:
                for row in done:
                    rows[row['index']] = row
                    print(f'{"ok  " if row["ok"] else "FAIL"} {labels[row["index"]]} '
                          f'({row["run"]} tests, {row["seconds"]:.0f}s)', flush=True)
                missing = [labels[i] for i in bins[k] if i not in rows]
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
    missing = [labels[i] for i in range(len(children)) if i not in rows]
    if missing and not problems:
        problems.append('no result for ' + ', '.join(missing))

    try:
        DURATIONS.parent.mkdir(exist_ok=True)
        DURATIONS.write_text(json.dumps({**previous, **{labels[i]: r['seconds'] for i, r in rows.items()}},
                                        indent=1, sort_keys=True))
    except OSError:
        pass

    failed = [i for i, r in sorted(rows.items()) if not r['ok']]
    for i in failed:
        print(f'\n{"=" * 70}\nFAILED MODULE {labels[i]}\n{"=" * 70}\n{rows[i]["output"].rstrip()}')
    for problem in problems:
        print(f'\n{"=" * 70}\nWORKER PROBLEM\n{"=" * 70}\n{problem}')
    slowest = sorted(rows.items(), key=lambda item: item[1]['seconds'], reverse=True)[:5]
    print('\nSlowest: ' + ', '.join(f'{labels[i]} {r["seconds"]:.0f}s' for i, r in slowest))
    print('-' * 70)
    print(f'Ran {sum(r["run"] for r in rows.values())} tests in {elapsed:.3f}s '
          f'({len(children)} modules, {jobs} jobs)')
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
