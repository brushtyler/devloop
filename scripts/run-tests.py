#!/usr/bin/env python3
"""Run the unittest suite one module per process, in parallel.

    python3 scripts/run-tests.py [-j N] [pattern ...]

Each module runs as ``python3 -m unittest discover -s tests -p <file>``: the same imports as
``python3 -m unittest discover -s tests``, so the two are interchangeable. Modules start
longest-first, using the durations of the previous run (``.ssdlc/test-durations.json``, ignored by
git) or the file size the first time. The summary at the end gives the total, the slowest modules
and the full output of every module that failed; the exit code is non-zero if any module failed.
"""

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / 'tests'
DURATIONS = ROOT / '.ssdlc' / 'test-durations.json'


def run_module(path):
    start = time.monotonic()
    done = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', path.name],
                          cwd=ROOT, capture_output=True, text=True)
    output = done.stdout + done.stderr
    ran = re.findall(r'^Ran (\d+) tests? in', output, re.M)
    return path.name, done.returncode, int(ran[-1]) if ran else 0, time.monotonic() - start, output


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('-j', '--jobs', type=int, default=os.cpu_count() or 2)
    parser.add_argument('--list', action='store_true', help='list the modules and exit')
    parser.add_argument('patterns', nargs='*', default=['test*.py'])
    args = parser.parse_args(argv)

    # Like discover: test files in tests/ and in its packages (directories with __init__.py).
    modules = sorted({p for pattern in args.patterns for p in TESTS.rglob(pattern)
                      if p.is_file() and all((d / '__init__.py').is_file()
                                             for d in p.parents if d != TESTS and TESTS in d.parents)})
    if not modules:
        print('No test modules match.', file=sys.stderr)
        return 1
    if args.list:
        print('\n'.join(str(p.relative_to(TESTS)) for p in modules))
        return 0
    try:
        previous = json.loads(DURATIONS.read_text())
    except (OSError, ValueError):
        previous = {}
    modules.sort(key=lambda p: previous.get(p.name, p.stat().st_size / 1000), reverse=True)

    start = time.monotonic()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for name, code, ran, seconds, output in pool.map(run_module, modules):
            results.append((name, code, ran, seconds, output))
            print(f'{"ok  " if code == 0 else "FAIL"} {name} ({ran} tests, {seconds:.0f}s)', flush=True)
    elapsed = time.monotonic() - start

    try:
        DURATIONS.parent.mkdir(exist_ok=True)
        DURATIONS.write_text(json.dumps({**previous, **{r[0]: round(r[3], 1) for r in results}},
                                        indent=1, sort_keys=True))
    except OSError:
        pass

    failed = [r for r in results if r[1] != 0]
    for name, _, _, _, output in failed:
        print(f'\n{"=" * 70}\nFAILED MODULE {name}\n{"=" * 70}\n{output.rstrip()}')
    slowest = sorted(results, key=lambda r: r[3], reverse=True)[:5]
    print('\nSlowest: ' + ', '.join(f'{r[0]} {r[3]:.0f}s' for r in slowest))
    print('-' * 70)
    print(f'Ran {sum(r[2] for r in results)} tests in {elapsed:.3f}s '
          f'({len(results)} modules, {args.jobs} jobs)')
    if failed:
        print(f'\nFAILED (modules={len(failed)}: {", ".join(r[0] for r in failed)})')
        return 1
    print('\nOK')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
