#!/usr/bin/env python3
"""Check that ssdlc's configuration is the same before and after its migration to the project
file (ADR 0006, R3).

    python3 ops/host/ssdlc/check-equivalence.py --before DIR [--before-local FILE]
        --after DIR [--after-local FILE] --after-project FILE [--root CHECKOUT]

``DIR`` is an installation of devloop: a pinned copy (``<state_dir>/current``) or the top of a
checkout, holding ``devloop/`` with its ``config.toml``. "Before" is the installation of before
R3 with the overlay of before; "after" the installation of R3 with the project file and the
overlay that gains the keys of ``ops/host/ssdlc/overlay.toml``. Each side runs in a Python of
its own, with only its installation on the module path, as ``devloop pin`` checks a copy. The
script compares, side by side:

- the values of the effective configuration, key by key. The SHA-256 of the project file
  (``review_policy.project``) is left out on purpose: R2 puts it in the policy version by
  construction, so the version differs. In its place the version of the policy after, computed
  without the hash, must be the version of before;
- the prompts of ``devloop/prompts/``, each rendered with the values of the configuration
  (``repo``, ``base_branch``, ``test_cmd``, ``language`` and the documentation map); the other
  placeholders stay as they are, the same on both sides;
- the test modules ``local_tests`` selects: the mode, and with ``changed`` the modules picked for
  each file the checkout ``--root`` tracks (default: the checkout of this script).

Relative paths are taken from the current directory, as usual, even though each side runs in
its installation. It prints every difference and ends with 0 when there is none, 1 when there is
one, 2 when a side does not load. It reads the files it is given and writes nothing.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
# Left out of the comparison of the values (ADR 0006, R3): the hash of the project file, which
# the policy version includes by construction since R2.
DECLARED = ('review_policy.project',)

# Run by each side with only its installation on the module path.
DUMP = r'''
import dataclasses, inspect, json, subprocess, sys
from pathlib import Path
from devloop import config, context, localtests

local, project, root = sys.argv[1] or None, sys.argv[2] or None, Path(sys.argv[3])
if project:
    if 'project' not in inspect.signature(config.load_config).parameters:
        sys.exit('this installation cannot read a project file')
    cfg = config.load_config(None, local, project)
else:
    cfg = config.load_config(None, local)
policy = cfg.review_policy
bare = (dataclasses.replace(policy, project='') if hasattr(policy, 'project') else policy)
values = dict(repo=cfg.repo, base_branch=cfg.base_branch, test_cmd=cfg.test_cmd,
              language=cfg.language, docs=context.docs_map_section(cfg.docs.entries))
prompts = {path.stem: context._template(path.stem, **values)
           for path in sorted(context.PROMPT_DIR.glob('*.md'))}
selected = {}
if cfg.local_tests == 'changed':
    modules = sorted(p.name for p in (root / 'tests').glob('test_*.py'))
    files = subprocess.run(['git', 'ls-files', '-z'], cwd=root, check=True,
                           capture_output=True, text=True).stdout.split('\0')
    selected = {f: localtests.select_modules([f], modules) for f in files if f}
print(json.dumps({'config': dataclasses.asdict(cfg), 'version': bare.version(),
                  'prompts': prompts,
                  'tests': {'local_tests': cfg.local_tests, 'selected': selected}},
                 default=str))
'''


def dump(install, local, project, root):
    """The configuration, prompts and test selection of the installation ``install``, or
    SystemExit(2) when it does not load."""
    install = Path(install).resolve()
    if not (install / 'devloop' / 'config.toml').is_file():
        print(f'{install}: not an installation of devloop (no devloop/config.toml)',
              file=sys.stderr)
        raise SystemExit(2)
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}
    env['PYTHONPATH'] = str(install)
    done = subprocess.run([sys.executable, '-P', '-c', DUMP, str(local or ''),
                           str(project or ''), str(root)],
                          cwd=install, env=env, capture_output=True, text=True)
    if done.returncode:
        print(f'{install}: the configuration does not load: '
              f'{(done.stderr or done.stdout).strip()}', file=sys.stderr)
        raise SystemExit(2)
    return json.loads(done.stdout)


def flatten(value, prefix=''):
    """``{dotted key: value}`` of the tables of ``value``; a list is one value."""
    if isinstance(value, dict) and value:
        out = {}
        for key, item in value.items():
            out.update(flatten(item, f'{prefix}.{key}' if prefix else str(key)))
        return out
    return {prefix: value}


def differences(before, after):
    """The lines that tell ``before`` from ``after``, the dumps of the two sides."""
    lines = []
    old, new = flatten(before['config']), flatten(after['config'])
    for key in sorted(set(old) | set(new)):
        if key in DECLARED or old.get(key, '<missing>') == new.get(key, '<missing>'):
            continue
        lines.append(f'config {key}: before {json.dumps(old.get(key, "<missing>"))}, '
                     f'after {json.dumps(new.get(key, "<missing>"))}')
    if before['version'] != after['version']:
        lines.append('policy version without the project file: before '
                     f'{before["version"]}, after {after["version"]}')
    for name in sorted(set(before['prompts']) | set(after['prompts'])):
        if before['prompts'].get(name) != after['prompts'].get(name):
            lines.append(f'prompt {name}: rendered differently')
    if before['tests']['local_tests'] != after['tests']['local_tests']:
        lines.append(f'local_tests: before {before["tests"]["local_tests"]}, '
                     f'after {after["tests"]["local_tests"]}')
    old, new = before['tests']['selected'], after['tests']['selected']
    for path in sorted(set(old) | set(new)):
        if old.get(path) != new.get(path):
            lines.append(f'tests for {path}: before {old.get(path)}, after {new.get(path)}')
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--before', required=True, metavar='DIR',
                        help='the installation of before R3')
    parser.add_argument('--before-local', metavar='FILE', help='the overlay of before R3')
    parser.add_argument('--after', required=True, metavar='DIR',
                        help='the installation of R3')
    parser.add_argument('--after-local', metavar='FILE', help='the overlay of after R3')
    parser.add_argument('--after-project', required=True, metavar='FILE',
                        help="ssdlc's project file")
    parser.add_argument('--root', default=str(ROOT), metavar='CHECKOUT',
                        help='the checkout whose tracked files select the tests')
    args = parser.parse_args(argv)
    # Each side runs in its installation: a relative path is the caller's, so it is made
    # absolute here, before any process changes directory. Not resolved: a link stays a link,
    # and the project file's read still refuses it.
    for name in ('before', 'after', 'before_local', 'after_local', 'after_project', 'root'):
        if getattr(args, name):
            setattr(args, name, os.path.abspath(getattr(args, name)))
    for path in (args.before_local, args.after_local, args.after_project):
        if path and not Path(path).is_file():
            parser.error(f'{path}: not a file')
    before = dump(args.before, args.before_local, None, args.root)
    after = dump(args.after, args.after_local, args.after_project, args.root)
    lines = differences(before, after)
    for line in lines:
        print(line)
    if lines:
        print(f'{len(lines)} difference(s): the migration is not equivalent')
        return 1
    print(f'equivalent: {len(flatten(after["config"])) - len(DECLARED)} values of the '
          f'configuration, {len(after["prompts"])} prompts, the test selection of '
          f'{len(after["tests"]["selected"])} files; left out by design: '
          f'{", ".join(DECLARED)} (the project file\'s hash, in the policy version since R2)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
