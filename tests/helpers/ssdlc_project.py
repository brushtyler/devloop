"""ssdlc's configuration for the tests (ADR 0006, R3).

Since R3 ``devloop/config.toml`` holds the installation's defaults only: ssdlc's facts and
restrictions are in its project file, on the host, of which ``ops/host/ssdlc/project.toml`` is
the example, and the agy commands derived from its tests in the overlay
(``ops/host/ssdlc/overlay.toml``). devloop never reads either from the repository.

Since R6 ssdlc's identities and trust (``repo``, ``trusted_users``, ``[apps]``) are in the
overlay too, beside the agy commands: ``IDENTITY_KEYS`` of ``ops/host/ssdlc/overlay.toml``.

The tests describe devloop on ssdlc as before R3: ``data()`` is the single file the
configuration was, the installation's ``config.toml`` with the project file's keys and the
overlay's identity keys set in it; ``load()`` parses it, with an overlay when given.
``tests/test_devloop_ssdlc_project.py`` proves that it is the configuration devloop composes
from the installation, the project file and the overlay, apart from the project file's hash in
the policy version.
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
# The keys of the overlay that left config.toml with ADR 0006, R6.
IDENTITY_KEYS = ('repo', 'trusted_users', 'apps')


def project():
    """The table of ssdlc's project file."""
    return config.read_toml(PROJECT, 'project file')


def identity():
    """ssdlc's identities and trust: the ``IDENTITY_KEYS`` of its overlay."""
    overlay = config.read_toml(OVERLAY, 'local config')
    return {key: overlay[key] for key in IDENTITY_KEYS}


def data():
    """ssdlc's raw configuration as one file: ``config.toml`` with the project file's keys and
    the overlay's identity keys."""
    return config._merge(config.read_toml(config.DEFAULT_CONFIG), {**identity(), **project()})


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
    file's keys and the overlay's identity keys written in it, for the copies of the
    installation the tests pin. Only the shapes those keys have: values, lists and tables of
    them."""
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

    put((), {**identity(), **project()})
    return '\n'.join(lines)


def toml_text(table, path=()):
    """``table`` as TOML text: its values, then each of its tables under its header. Only the
    shapes of the configuration: values, lists and tables of them."""
    lines = [f'{_key(k)} = {_value(v)}' for k, v in table.items() if not isinstance(v, dict)]
    for key, value in table.items():
        if isinstance(value, dict):
            header = '[' + '.'.join(_key(k) for k in path + (key,)) + ']'
            lines += ['', header, toml_text(value, path + (key,))]
    return '\n'.join(lines)


def identity_overlay(case):
    """For the test ``case``, an overlay with ssdlc's identity keys only (``identity``), without
    the agy commands. Returns the file."""
    tmp = Path(tempfile.mkdtemp())
    case.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    overlay = tmp / 'config.local.toml'
    overlay.write_text(toml_text(identity()) + '\n', encoding='utf-8')
    return overlay


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
   needs one when, and only when, its code cannot run as intended, once devloop pins it,
   without something the operator does on the host first, with the loop stopped: a file the
   code needs in the `state_dir`; a key or table of the project file or of the local overlay
   that is required, so that without it the code fails, refuses to start or runs degraded; or
   a command run as root or on the service. Then the declaration must be there, with a short
   text of the step and a reference to the document that describes it. None is needed for a
   change only to the documents of `ops/host/`, nor for a key or table that is optional and has
   a default the code uses when it is absent. A declaration missing where one is needed is a
   blocking finding: devloop pins a PR that declares none without waiting. A declaration where
   none is needed is only a non-blocking finding: devloop merges the PR all the same, and the
   declaration only holds the automatic pin until the operator pins by hand.
"""


class Replaced(tuple):
    """A change of ``SINCE_R4`` that replaces the only occurrence of ``old`` in the reference
    with ``new``, where adding a text after an anchor is not enough."""

    def __new__(cls, old, new):
        return super().__new__(cls, (old, new))


# #472: the trust policy of the review and the advisory review prompts, narrowed in point 3 and
# with the comments of the operator on the issue among the decisions of point 1.
_TRUST_POLICY_472 = (
    Replaced(
        '   cycle of `docs/adr/README.md` and present on `main`, or one written by the operator in '
        'the body\n'
        '   of the issue. The points an ADR leaves open are not decisions.\n',
        '   cycle of `docs/adr/README.md` and present on `main`, one written by the operator in '
        'the body\n'
        '   of the issue, or a comment of the operator that approves the change, among those '
        'listed under\n'
        '   "Decisions of the operator on the issue" below (#472). A comment that section does '
        'not list\n'
        '   is not a decision. The points an ADR leaves open are not decisions.\n'),
    Replaced(
        '   or the classes of escalation (`NON_CRITICAL` in `devloop/supervision.py`). This holds '
        'even\n'
        '   when an ADR asks for the change.\n',
        '   or the conditions that decide when a change needs the operator. This holds even when '
        'an ADR\n'
        '   asks for the change.\n'
        '\n'
        'A change to the classes of escalation (`NON_CRITICAL` in `devloop/supervision.py`), to '
        'a\n'
        '`/devloop` command or to a condition of the merge is not a change of the rule when a '
        'decision of\n'
        'the operator prescribes it explicitly, naming the class, the command or the condition: '
        'it is a\n'
        'faithful implementation, and follows point 1. What the decision does not name, such as '
        'a class\n'
        'added beside the one it names, needs the operator under point 1.\n'),
    Replaced(
        'of the PR. Cite it in your review: the file of the ADR, the section and the point. '
        'Without that\n'
        'citation the change needs the operator, as under point 1. Every deviation you find '
        'needs the\n'
        'operator, with its reason and the decision it departs from. A change that touches\n'
        '`operator_only_paths` (such as `docs/adr/` and `AGENTS.md`) is still merged by the '
        'operator\n'
        'only, whatever this rule says. Examples:\n',
        'of the PR. Cite it in your review: the file of the ADR, the section and the point. The '
        'same holds\n'
        'for a decision in the body of the issue or in a comment listed under "Decisions of the '
        'operator\n'
        'on the issue": cite the comment with its author and its time. Without that citation '
        'the change\n'
        'needs the operator, as under point 1. Every deviation you find needs the operator, '
        'with its\n'
        'reason and the decision it departs from. A change that touches `operator_only_paths` '
        '(such as\n'
        '`docs/adr/` and `AGENTS.md`) is still merged by the operator only, whatever this rule '
        'says.\n'
        'Examples:\n'),
    ('  and the supervisor, as D7 says.\n',
     '- Faithful implementation, the operator is not needed: the issue cites\n'
     '  `docs/adr/0005-deleghe-e-ruoli-degli-agent.md`, section 4, D2, which approves the '
     'delegations\n'
     '  of its section 3.1, and the row P5-1 of its plan, which names the non-critical class\n'
     '  `round_grantable`; the change adds `round_grantable` to `NON_CRITICAL` with no '
     'deviation.\n'),
    Replaced(
        '  constant of the code becomes a key of `devloop/config.toml` that can raise it.\n',
        '  constant of the code becomes a key of `devloop/config.toml` that can raise it; or the '
        'change\n'
        '  also adds to `NON_CRITICAL` a class the decision does not name, such as '
        '`reviewer_decision`.\n'),
    Replaced(
        '  `devloop/prompts/review.md` or of `devloop/prompts/advisory.md`, or moves a class of '
        'escalation\n'
        '  into or out of `NON_CRITICAL`.\n',
        '  `devloop/prompts/review.md` or of `devloop/prompts/advisory.md`, even when an ADR or a '
        'decision\n'
        '  of the operator asks for it.\n'),
    ('$issue_body\n\n', '$operator_decisions\n\n'),
)
# #472: the readiness check applies the trust policy to the issue and declares it.
_TRUST_POLICY_PREFLIGHT = """\
## Trust policy

The operator decides a change to the trust policy on the issue, before anyone implements it, not
on the pull request (#472). The reviewers of the pull request will apply this rule, from the
review prompt of devloop; apply it now to the change the issue asks for:

$trust_policy

Declare in `trust_policy`, for the change the issue asks for:

- `none`: it does not touch the trust policy (the closed list of five points above);
- `covered`: it touches it, and a decision of the operator covers it with no deviation and
  authorizes every weakening of a check it makes: a decision of an ADR as the rule says, one in
  the body of the issue, or a comment of the operator that approves the change, listed under
  "Decisions of the operator on the issue" below. Any other comment, also of a trusted user, is
  not such a decision;
- `uncovered`: it touches it, and no decision of the operator covers it, it goes beyond or
  against one, or it weakens a check that no decision authorizes. A change of the rule itself is
  `uncovered` too, unless a decision of the operator approves it.

In `trust_policy_reason` list the parts of the trust policy the change touches, each with its
point of the closed list, and the decision that covers them (the file, the section and the point
of the ADR, or the author and the time of the comment) or why none does; `""` with `none`. With
`uncovered` the issue is not ready: devloop adds a gap of kind `operator_decision` with the
question to the operator and your `trust_policy_reason`, so do not list that gap yourself.

"""
# (#473 made the criterion of point 8 precise: the cases that need a declaration, those that do
# not, and a superfluous declaration that is not blocking.)
# The changes of the templates since R4, each as ``(anchor, text)``, where ``text`` follows the
# only occurrence of ``anchor`` in the reference, or as ``Replaced(old, new)``. The frozen files
# stay as they were; the prompts of now are the reference with these changes, and nothing else.
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
        *_TRUST_POLICY_472,
    ),
    'advisory': _TRUST_POLICY_472,
    'preflight': (
        ('external tool, access or resource can close.\n\n', _TRUST_POLICY_PREFLIGHT),
        ('  implementable in one run; otherwise `""`.\n',
         '- `trust_policy`: `none`, `covered` or `uncovered`, as above.\n'
         '- `trust_policy_reason`: the parts touched and the decision that covers them, or '
         '`""`.\n'),
        Replaced('Write `description`, `proposed_default` and `split` in $language.\n',
                 'Write `description`, `proposed_default`, `split` and `trust_policy_reason` in '
                 '$language.\n'),
        ('$issue_comments\n', '\n$operator_decisions\n'),
    ),
}


def pre_r4_prompt(name):
    """The template ``name`` of before R4 with the changes made since (``SINCE_R4``)."""
    text = (PRE_R4_PROMPTS / f'{name}.md').read_text(encoding='utf-8')
    for change in SINCE_R4.get(name, ()):
        old, new = change if isinstance(change, Replaced) else (change[0], change[0] + change[1])
        if text.count(old) != 1:
            raise AssertionError(f'{name}: the anchor {old!r} is not once in the reference')
        text = text.replace(old, new)
    return text


def pre_r4_prompts(dest):
    """The templates of ``pre_r4_prompt`` written in the directory ``dest``, created."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for path in sorted(PRE_R4_PROMPTS.glob('*.md')):
        (dest / path.name).write_text(pre_r4_prompt(path.stem), encoding='utf-8')
    return dest
