"""ssdlc's configuration for the tests (ADR 0006, R3).

Since R3 ``devloop/config.toml`` holds the installation's defaults only: ssdlc's facts and
restrictions are in its project file, on the host, of which ``ops/host/ssdlc/project.toml`` is
the example, and the agy commands derived from its tests in the overlay
(``ops/host/ssdlc/overlay.toml``). devloop never reads either from the repository.

Since R6 ssdlc's identities and trust (``repo``, ``trusted_users``, ``[apps]``) are in the
overlay too, beside the agy commands: ``IDENTITY_KEYS`` of ``ops/host/ssdlc/overlay.toml``.

The tests describe devloop on ssdlc as before R3: ``by_role()`` is the single file the
configuration was, the installation's ``config.toml`` with the project file's keys and the
overlay's identity keys set in it; ``load_by_role()`` parses it, with an overlay when given.
Since #499 ``config.toml`` names the agents by role: ``data()`` and ``load()`` give the same
configuration with the keys of before (``before_499``), which the tests written before #499
describe.
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


def by_role():
    """ssdlc's raw configuration as one file: ``config.toml`` with the project file's keys and
    the overlay's identity keys. Since #499 its agents have keys by role (ADR 0005, P5-3)."""
    return config._merge(config.read_toml(config.DEFAULT_CONFIG), {**identity(), **project()})


# The keys by role of config.toml since #499, each with the key it had before; the third
# reviewer had none. The code still reads keys without a role as before (a key that names no
# role serves the roles it is given), and the tests written before #499 describe devloop with
# them: ``data`` gives them back. The tests of the keys by role use ``by_role``.
KEYS_BEFORE_499 = {'claude-implementer': 'claude', 'codex-reviewer': 'codex',
                   'agy-reviewer': 'agy'}
THIRD_REVIEWER = 'claude-reviewer'


def before_499(raw):
    """The raw configuration ``raw`` (``by_role``) with the agents as config.toml had them
    before #499: the keys without a role, no declared app nor provider, no third reviewer, the
    pool of codex and agy."""
    agents = {}
    for key, table in raw['agents'].items():
        if key == THIRD_REVIEWER:
            continue
        table = {k: v for k, v in table.items() if k not in ('app', 'provider')}
        if 'fallback_models' in table:
            table['fallback_models'] = [{k: v for k, v in f.items() if k != 'provider'}
                                        for f in table['fallback_models']]
        agents[KEYS_BEFORE_499.get(key, key)] = table
    roles = dict(raw['roles'], implementer=KEYS_BEFORE_499[raw['roles']['implementer']],
                 reviewers=[KEYS_BEFORE_499[k] for k in raw['roles']['reviewers']
                            if k != THIRD_REVIEWER])
    return dict(raw, agents=agents, roles=roles)


def data():
    """ssdlc's raw configuration as one file, with the agents as before #499
    (``before_499``)."""
    return before_499(by_role())


def load(local=None, root=config.INSTALL_ROOT):
    """ssdlc's configuration (``data``), with the overlay ``local`` when given."""
    raw = data()
    if local:
        raw = config.apply_overlay(raw, config.read_toml(local, 'local config'))
    return config.parse_config(raw, root)


def load_by_role(local=None, root=config.INSTALL_ROOT):
    """ssdlc's configuration as config.toml sets it since #499 (``by_role``), with the overlay
    ``local`` when given."""
    raw = by_role()
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
# #483: a decision on the issue is a `/devloop approve`, and what stays with the operator whatever
# a decision prescribes is listed, with an example. Applied after ``_TRUST_POLICY_472``.
_TRUST_POLICY_483 = (
    Replaced(
        '   of the issue, or a comment of the operator that approves the change, among those '
        'listed under\n'
        '   "Decisions of the operator on the issue" below (#472). A comment that section does '
        'not list\n'
        '   is not a decision. The points an ADR leaves open are not decisions.\n',
        '   of the issue, or a `/devloop approve` of the operator, among those listed under '
        '"Decisions of\n'
        '   the operator on the issue" below (#472, #483). A comment that section does not list, '
        'an\n'
        '   approval written without the command included, is not a decision. A `/devloop '
        'approve` covers\n'
        '   only what its text approves, within the limits and on the conditions it sets: an '
        'approval of\n'
        '   one part, or one subject to a condition, does not cover the rest of the change. The '
        'points an\n'
        '   ADR leaves open are not decisions.\n'),
    Replaced(
        '   or the conditions that decide when a change needs the operator. This holds even when '
        'an ADR\n'
        '   asks for the change.\n',
        '   or the conditions that decide when a change needs the operator. This holds even when '
        'an ADR\n'
        '   asks for the change. These conditions always include, and stay with the operator '
        'even when a\n'
        '   decision of the operator, a `/devloop approve` on the issue included, prescribes the '
        'change\n'
        '   (#483): the quorum and the choice of the reviewers; the checks of '
        '`operator_only_paths` and\n'
        '   of `protected_paths`; the names of the labels of hold, of escalation and of merge;\n'
        '   `trusted_users`; this "Trust policy" section itself.\n'),
    Replaced(
        'added beside the one it names, needs the operator under point 1.\n',
        'added beside the one it names, needs the operator under point 1. What point 3 lists is '
        'never a\n'
        'faithful implementation: a change of the quorum, for one, is a condition of the merge '
        'that still\n'
        'needs the operator.\n'),
    Replaced(
        'for a decision in the body of the issue or in a comment listed under "Decisions of the '
        'operator\n'
        'on the issue": cite the comment with its author and its time. Without that citation '
        'the change\n'
        'needs the operator, as under point 1. Every deviation you find needs the operator, '
        'with its\n'
        'reason and the decision it departs from. A change that touches `operator_only_paths` '
        '(such as\n'
        '`docs/adr/` and `AGENTS.md`) is still merged by the operator only, whatever this rule '
        'says.\n',
        'for a decision in the body of the issue or in a `/devloop approve` listed under '
        '"Decisions of the\n'
        'operator on the issue": cite the command with its author and its time. Without that '
        'citation the\n'
        'change needs the operator, as under point 1. Every deviation you find needs the '
        'operator, with\n'
        'its reason and the decision it departs from. A change that touches '
        '`operator_only_paths` (such\n'
        'as `docs/adr/` and `AGENTS.md`) is still merged by the operator only, whatever this '
        'rule says.\n'),
    ('  of the operator asks for it.\n',
     '- Change of the rule, the operator is needed (point 3): the issue carries a `/devloop '
     'approve` of\n'
     '  the operator that names the change, and the change lowers the quorum of a level, from '
     'two\n'
     '  approvals to one, or narrows the check of `operator_only_paths` so that it no longer '
     'covers\n'
     '  `docs/adr/`. The approval does not make it a faithful implementation.\n'),
)
# #483: the same in the readiness check, applied after ``_TRUST_POLICY_PREFLIGHT``.
_TRUST_POLICY_PREFLIGHT_483 = (
    Replaced(
        '  the body of the issue, or a comment of the operator that approves the change, listed '
        'under\n'
        '  "Decisions of the operator on the issue" below. Any other comment, also of a trusted '
        'user, is\n'
        '  not such a decision;\n',
        '  the body of the issue, or a `/devloop approve` of the operator, listed under '
        '"Decisions of the\n'
        '  operator on the issue" below (#483). Any other comment, also of a trusted user and '
        'also one\n'
        '  that approves the change without the command, is not such a decision, and a command '
        'covers\n'
        '  only what its text approves, on the conditions it sets;\n'),
    ('  `uncovered` too, unless a decision of the operator approves it.\n\n',
     'A change of the rule, what point 3 lists included, still goes to the operator for the '
     'merge\n'
     'even when a decision covers it: with `covered`, say so in `trust_policy_reason`.\n\n'),
    Replaced(
        'of the ADR, or the author and the time of the comment) or why none does;',
        'of the ADR, or the author and the time of the command) or why none does;'),
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
_FINDINGS_ONLY_REVIEW = """\
  The exception is a finding of a findings-only reviewer (its round is marked `findings-only`
  below): the reviewers of the quorum close it together, when each of them marks it `resolved` or
  `withdrawn` in the same round. Judge it as if it were yours: while you keep it `unresolved` you
  cannot approve.
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
# #506: the permalink of the comments, cited by the implementer and the fixer. The prompts of
# review and advisory review do not change.
_PERMALINK_IMPLEMENT = """\
- When an ADR or another document must cite a comment below, for example a decision of the
  operator, cite it with the link of its `Permalink:` line: the link GitHub gives that comment.
"""
_PERMALINK_FIX = """\
- When an ADR or another document must cite a request below, a decision of the operator for
  example, cite it with the link of its `Permalink:` line: the link GitHub gives its comment.
"""
_PERMALINK_PREFLIGHT = """\

A comment below with a `Permalink:` line carries the link GitHub gives it. When the issue asks to
cite that comment, a decision of the operator in an ADR for example, the implementer gets the
same line and cites that link: the missing link is not a gap.
"""
# ADR 0007, R1 (#538): the replay gate left with the SSDLC engine, and its baseline is no longer
# among the decisions reserved to the operator.
_NO_REPLAY_GATE_CONCLAVE = Replaced('- trust rules;\n- revisions of the replay gate baseline;\n',
                                    '- trust rules;\n')
# The changes of the templates since R4, each as ``(anchor, text)``, where ``text`` follows the
# only occurrence of ``anchor`` in the reference, or as ``Replaced(old, new)``. The frozen files
# stay as they were; the prompts of now are the reference with these changes, and nothing else.
SINCE_R4 = {
    # #462: the step on the host, declared by implementer and fixer and checked by the reviewer.
    'implement': (
        ('  list (`[]`) when the issue has no recognisable acceptance criteria.\n',
         _HOST_STEP_IMPLEMENT),
        ('  the issue explicitly asks for it.\n', _PERMALINK_IMPLEMENT),
    ),
    'fix': (
        # #481
        ('  technical reason (for example, the finding misreads the code or the issue). Do not '
         'dispute to avoid work.\n', _EVIDENCE_FIX),
        ('  the issue has no recognisable acceptance criteria.\n', _HOST_STEP_FIX),
        ('Current acceptance map of the pull request:\n\n$acceptance_map\n\n',
         '## Step on the host\n\nCurrent declaration of the pull request (`host_step`):\n\n'
         '$host_step\n\n'),
        ('- Do not commit, push or comment on GitHub. The dispatcher publishes your changes after '
         'you finish.\n', _PERMALINK_FIX),
    ),
    'review': (
        ('   the next section.\n', _HOST_STEP_REVIEW),
        ('Latest coverage map of the pull request, from the implementer or its latest fix:\n\n'
         '$acceptance_map\n\n',
         '## Step on the host\n\nLatest declaration of the pull request (`host_step`), from the '
         'implementer or its latest fix:\n\n$host_step\n\n'),
        *_TRUST_POLICY_472,
        *_TRUST_POLICY_483,
        # #481
        ('  before merge. Style preferences and optional improvements are never blocking.\n',
         _EVIDENCE_REVIEW),
        ('  implementer\'s responses below. Accept a reasoned dispute when it is right '
         '(`withdrawn`).\n', _DISPUTE_REVIEW),
        # #416: the findings of the findings-only reviewer, closed by the quorum.
        ('  A finding opened by CI or by another reviewer stays open whatever you write: assess '
         'it anyway.\n', _FINDINGS_ONLY_REVIEW),
    ),
    'advisory': (
        *_TRUST_POLICY_472,
        *_TRUST_POLICY_483,
        # #481: the findings of the quorum without evidence, or fixed without need.
        ('- `overlooked`: points the reviews below may have missed; `[]` when you see none.\n',
         _UNFOUNDED_ADVISORY),
        # #486: the opinion decides whether the supervisor may merge the head.
        Replaced(
            'the round limit, it opens no finding, it changes no label, and devloop never reads '
            'it to decide.\n',
            'the round limit, it opens no finding and it changes no label. Its opinion decides '
            'one thing only:\n'
            'when the operator has delegated the merge to the supervisor (`[delegations] '
            'merge`), devloop\n'
            'refuses a merge the supervisor asks for on this head unless your opinion is `ok` or '
            '`dubbi`; with\n'
            '`contrario`, or without a review, the merge stays the operator\'s.\n'),
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
        *_TRUST_POLICY_PREFLIGHT_483,
        ('that show the gap, or none. Use `proposed_default: ""` when there is no default to '
         'propose.\n', _PERMALINK_PREFLIGHT),
        # ADR 0007, R1 (#538)
        Replaced('permissions, trust rules, the replay gate baseline, the merge mode, ',
                 'permissions, trust rules, the merge mode, '),
    ),
    'conclave_proposal': (_NO_REPLAY_GATE_CONCLAVE,),
    'conclave_vote': (_NO_REPLAY_GATE_CONCLAVE,),
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
