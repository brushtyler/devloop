"""ssdlc's configuration for the tests (ADR 0006, R3).

Since R3 ``devloop/config.toml`` holds the installation's defaults only: ssdlc's facts and
restrictions are in its project file, on the host, of which ``ops/host/ssdlc/project.toml`` is
the example, and the agy commands derived from its tests in the overlay
(``ops/host/ssdlc/overlay.toml``). devloop never reads either from the repository.

The tests describe devloop on ssdlc as before R3: ``data()`` is the single file the
configuration was, the installation's ``config.toml`` with the project file's keys set in it;
``load()`` parses it, with an overlay when given. ``tests/test_devloop_ssdlc_project.py`` proves
that it is the configuration devloop composes from the installation, the project file and the
overlay, apart from the project file's hash in the policy version.
"""

import json
import re
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from devloop import config

ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT / 'ops' / 'host' / 'ssdlc' / 'project.toml'
OVERLAY = ROOT / 'ops' / 'host' / 'ssdlc' / 'overlay.toml'


def project():
    """The table of ssdlc's project file."""
    return config.read_toml(PROJECT, 'project file')


def data():
    """ssdlc's raw configuration as one file: ``config.toml`` with the project file's keys."""
    return config._merge(config.read_toml(config.DEFAULT_CONFIG), project())


def load(local=None, root=config.INSTALL_ROOT):
    """ssdlc's configuration (``data``), with the overlay ``local`` when given."""
    raw = data()
    if local:
        raw = config.apply_overlay(raw, config.read_toml(local, 'local config'))
    return config.parse_config(raw, root)


def _key(key):
    return key if re.fullmatch(r'[A-Za-z0-9_-]+', key) else json.dumps(key)


def _value(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, list):
        return '[' + ', '.join(_value(v) for v in value) + ']'
    return json.dumps(value)  # a string or a number: the same form in TOML


def text():
    """``data`` as the text of one ``config.toml``: the installation's file with the project
    file's keys written in it, for the copies of the installation the tests pin. Only the
    shapes the project file has: values, lists and tables of them."""
    lines = config.DEFAULT_CONFIG.read_text(encoding='utf-8').split('\n')

    def put(path, table):
        values = [f'{_key(k)} = {_value(v)}' for k, v in table.items() if not isinstance(v, dict)]
        if values:
            if not path:
                lines[0:0] = values  # before the first table
            else:
                header = '[' + '.'.join(_key(k) for k in path) + ']'
                if header in lines:
                    at = lines.index(header) + 1
                    lines[at:at] = values
                else:
                    lines.extend(['', header, *values])
        for key, value in table.items():
            if isinstance(value, dict):
                put(path + (key,), value)

    put((), project())
    return '\n'.join(lines)


def as_default(case):
    """For the test ``case``, ``config.DEFAULT_CONFIG`` is ssdlc's configuration as one file
    (``text``): the command line that reads the installation's file without a project file
    then loads as before R3. Returns the file."""
    tmp = Path(tempfile.mkdtemp())
    case.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    flat = tmp / 'config.toml'
    flat.write_text(text(), encoding='utf-8')
    patcher = mock.patch.object(config, 'DEFAULT_CONFIG', flat)
    patcher.start()
    case.addCleanup(patcher.stop)
    return flat


# The prompt templates of before R4 (ADR 0006), frozen as they were: the reference of the
# rendered prompts (``tests/test_devloop_prompt_data.py``).
PRE_R4_PROMPTS = ROOT / 'tests' / 'fixtures' / 'ssdlc-pre-r4' / 'prompts'

_HOST_STEP_IMPLEMENT = """\
- `host_step`: empty, unless the code of your change cannot run as intended, once devloop pins
  it, without a step the operator makes on the host first, with the loop stopped: for example
  a new table or key of the project file or of the local overlay that the code reads, a change
  to the schema of either, or to the procedures of the host (such as `ops/host/`). Then a short
  text, in $language, of what the operator must do, with a reference to the document that
  describes it (for example the section of the migration guide of the host). devloop merges a
  pull request that declares it as any other, but holds the automatic pin of that commit and
  of the later ones until the operator pins by hand, after the step. The reviewer checks that
  the declaration is there when it is needed and empty otherwise.
"""
_HOST_STEP_FIX = """\
- `host_step`: the declaration of a step on the host after your changes, which replaces the
  current one below: empty, unless the code of the pull request cannot run as intended, once
  devloop pins it, without a step the operator makes on the host first, with the loop stopped
  (for example a new table or key of the project file or of the local overlay that the code
  reads, a change to the schema of either, or to the procedures of the host such as
  `ops/host/`). Then a short text, in $language, of what the operator must do, with a reference
  to the document that describes it. Return the current declaration again when it still
  holds. devloop holds the automatic pin after the merge of a pull request that declares one,
  until the operator pins by hand after the step.
"""
_HOST_STEP_REVIEW = """\
8. The declaration of a step on the host (`host_step`, under "Step on the host" below). A PR
   needs one when its code cannot run as intended, once devloop pins it, without a step the
   operator makes on the host first, with the loop stopped: it changes the schema of the
   project file or of the local overlay (a new table or key the code reads), or the procedures
   of the host (such as `ops/host/`). Then the declaration must be there, with a short text of
   the step and a reference to the document that describes it; in every other case it must be
   empty. A declaration missing where it is needed, or present where it is not, is a blocking
   finding: devloop merges a PR that declares one, but holds the automatic pin until the
   operator pins by hand after the step, and pins a PR that declares none without waiting.
"""
# The changes of the templates since R4, each as ``(anchor, text)``: ``text`` follows the only
# occurrence of ``anchor`` in the reference. The frozen files stay as they were; the prompts of
# now are the reference with these changes, and nothing else.
SINCE_R4 = {
    # #462: the step on the host, declared by implementer and fixer and checked by the reviewer.
    'implement': (
        ('  list (`[]`) when the issue has no recognisable acceptance criteria.\n',
         _HOST_STEP_IMPLEMENT),
    ),
    'fix': (
        ('  the issue has no recognisable acceptance criteria.\n', _HOST_STEP_FIX),
        ('Current acceptance map of the pull request:\n\n$acceptance_map\n\n',
         '## Step on the host\n\nCurrent declaration of the pull request (`host_step`):\n\n'
         '$host_step\n\n'),
    ),
    'review': (
        ('   the next section.\n', _HOST_STEP_REVIEW),
        ('Latest coverage map of the pull request, from the implementer or its latest fix:\n\n'
         '$acceptance_map\n\n',
         '## Step on the host\n\nLatest declaration of the pull request (`host_step`), from the '
         'implementer or its latest fix:\n\n$host_step\n\n'),
    ),
}


def pre_r4_prompt(name):
    """The template ``name`` of before R4 with the changes made since (``SINCE_R4``)."""
    text = (PRE_R4_PROMPTS / f'{name}.md').read_text(encoding='utf-8')
    for anchor, added in SINCE_R4.get(name, ()):
        if text.count(anchor) != 1:
            raise AssertionError(f'{name}: the anchor {anchor!r} is not once in the reference')
        text = text.replace(anchor, anchor + added)
    return text


def pre_r4_prompts(dest):
    """The templates of ``pre_r4_prompt`` written in the directory ``dest``, created."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for path in sorted(PRE_R4_PROMPTS.glob('*.md')):
        (dest / path.name).write_text(pre_r4_prompt(path.stem), encoding='utf-8')
    return dest
