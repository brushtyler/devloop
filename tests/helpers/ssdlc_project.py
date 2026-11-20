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
# #481: the evidence of a blocking finding, and the dispute of one whose evidence does not hold.
_EVIDENCE_REVIEW = """\
- **Evidence**: every blocking finding states in `evidence` what shows the defect, so that the
  implementer can verify it: the scenario you reproduced (a test that fails, a command with its
  output, a concrete input and the behaviour you observed) or, for a defect that cannot be run,
  the precise reference to the code and to the rule it violates (the issue, an ADR, a document)
  with the quotation. Check it yourself before you write it. devloop records a blocking finding
  with an empty `evidence` as non-blocking, and it does not send the PR to the fix; a finding
  with `security` set to `true` stays blocking even without evidence, but give it one all the
  same.
"""
_DISPUTE_REVIEW = """\
  When the implementer disputes one of your findings because its evidence does not hold, and
  reports how it verified it, check that verification: either withdraw the finding
  (`withdrawn`), or keep it `unresolved` and give in `note` new evidence that answers the
  dispute. Keeping it open without new evidence is not enough.
"""
_EVIDENCE_FIX = """\
- Before fixing a finding, verify its evidence (`Evidence` below, when the reviewer gave one,
  and the `Latest assessment by its owner`, which answers an earlier dispute with new evidence):
  run its scenario again, or read again the code and the rule it cites. When the evidence does
  not hold (the scenario does not fail, the code does not do what it says, the rule says
  something else), dispute the finding (`disputed`) instead of changing the code, and report in
  the `note` of its response how you verified it and what you observed. Do not fix a finding
  only because fixing is the shorter way to converge: an unneeded change costs a round and can
  add a defect. In the next round the reviewer who opened it either withdraws it or answers with
  new evidence.
"""
_UNFOUNDED_ADVISORY = """\
- `unfounded`: the findings of the reviews below whose evidence does not hold, and the fixes the
  implementer made that were not needed, shown in the section "Finding senza evidenza o
  corretti senza necessità". Verify each one: run its scenario again, or read again the code
  and the rule it cites. The reviews below also list, under "Blocking findings without
  evidence", those devloop recorded as non-blocking because they had none: include them. Name
  the finding (`round/reviewer/ID`) and say what you observed; `[]` when you see none.
"""
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
        # #481
        ('  technical reason (for example, the finding misreads the code or the issue). Do not '
         'dispute to avoid work.\n', _EVIDENCE_FIX),
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
        # #481
        ('  before merge. Style preferences and optional improvements are never blocking.\n',
         _EVIDENCE_REVIEW),
        ('  implementer\'s responses below. Accept a reasoned dispute when it is right '
         '(`withdrawn`).\n', _DISPUTE_REVIEW),
    ),
    'advisory': (
        *_TRUST_POLICY_472,
        # #481: the findings of the quorum without evidence, or fixed without need.
        ('- `overlooked`: points the reviews below may have missed; `[]` when you see none.\n',
         _UNFOUNDED_ADVISORY),
    ),
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
