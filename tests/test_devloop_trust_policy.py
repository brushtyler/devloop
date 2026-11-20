"""devloop: the rule on the trust policy in the review prompts (#399, #447).

The review prompt and the advisory review prompt carry the same closed list of what the trust
policy is, with its examples, and the same narrowed rule on when a change to it needs the
operator (#447); each then asks for its own signal to the operator."""

import re
import unittest
from types import SimpleNamespace

from devloop.config import PromptFacts
from devloop.context import PROMPT_DIR, _template, prompt_values, trust_policy_rule
from helpers import ssdlc_project

PROMPTS = ('review', 'advisory')

# The five points of the closed list, in order, and their key words.
POINTS = (
    ('1. It changes the value, the default or the way devloop reads one of these configuration '
     'keys:', ('`trusted_users`', '`operator_only_paths`', '`protected_paths`', '`[merge] mode`',
               '`[merge.levels]`', '`[review_policy]`', '`sensitive_paths`',
               '`sensitive_path_lists`', '`zero`', '`advisory_reviewer`', '`[roles]`',
               '`implementer`', '`reviewers`', '`supervisor`', '`conclave`',
               '`max_review_rounds`', '`[pin] auto`', '`[apps]`', '`write_args`',
               '`review_args`', 'the other permissions of the agents')),
    ('2. It changes the constants that cap the delegations and the retries',
     ('`MAX_RETRIES`',)),
    ('3. It changes who is believed:',
     ('the filter of the trusted authors of the comments',
      'the identity that wrote a marker or a record',
      'the identity required for `/devloop` commands',
      # #417: the names by which devloop recognises commands and records.
      'the names by which devloop recognises trusted commands and records: renaming, removing '
      'or changing the meaning of a `/devloop` command',
      '`/devloop hold`', '`/devloop release`', '`/devloop veto`',
      'a kind of the `<!-- devloop:<kind>:v1 ... -->` markers')),
    ('4. It changes when a PR is merged or promoted, or who decides on it:',
     ('the merge conditions', 'the quorum of the reviewers',
      'the checks of `operator_only_paths` and `protected_paths`',
      'the choice of the review level',
      'the classes of escalation the supervisor decides in `auto` mode '
      '(`NON_CRITICAL` in `devloop/supervision.py`) and those that stay with the operator',
      # #417: the names of the labels of merge, hold and escalation.
      'the names of the labels that govern merge, hold and escalation, defined in the `Labels` '
      'dataclass of `devloop/config.py`',
      '`agent:ready-to-merge`', '`agent:merge-hold`', '`agent:needs-operator`',
      '`agent:needs-supervisor`',
      'renaming, removing or changing the meaning of one of them')),
    ('5. It changes one of these prompts in `devloop/prompts/`:',
     ('the review prompt', 'the advisory review prompt',
      'the conclave prompts (`conclave_proposal.md`, `conclave_vote.md`, which define the '
      'perimeter reserved to the operator)',
      'the parts of `implement.md`, `fix.md` and `preflight.md` that instruct the agents on '
      'untrusted content and on trust rules', 'or this rule itself.')),
)

ROOT = PROMPT_DIR.parents[1]


def flat(text):
    return ' '.join(text.split())


def section(name):
    """The "Trust policy" section of a prompt, up to the next heading."""
    text = (PROMPT_DIR / f'{name}.md').read_text(encoding='utf-8')
    match = re.search(r'^## Trust policy\n(.*?)(?=^## |\Z)', text, re.M | re.S)
    assert match, f'{name}.md has no "## Trust policy" section'
    return match.group(1)


def shared(name):
    """The rule both prompts share: from the definition to the examples included."""
    body = section(name)
    return body[:body.index('\nWhen the change needs the operator under this rule')]


class TrustPolicyPromptTest(unittest.TestCase):

    def test_both_prompts_carry_the_closed_list_of_five_points(self):
        for name in PROMPTS:
            with self.subTest(prompt=name):
                text = flat(section(name))
                self.assertIn('at every review level', text)
                self.assertIn('when, and only when, it does at least one of these five things '
                              '(the list is closed)', text)
                positions = []
                for head, words in POINTS:
                    self.assertIn(head, text)
                    positions.append(text.index(head))
                    for word in words:
                        self.assertIn(word, text)
                self.assertEqual(positions, sorted(positions))
                self.assertNotIn('6. ', text)

    def test_both_prompts_say_what_is_not_trust_policy_with_examples(self):
        for name in PROMPTS:
            with self.subTest(prompt=name):
                text = flat(section(name))
                self.assertIn('Nothing else is trust policy, unless it also does one of the five '
                              'things above: refactoring without a change of behaviour, new '
                              'functions, texts and comments, operational keys such as timeouts, '
                              'capacities and intervals.', text)
                self.assertIn('- Trust policy: adding a login to `trusted_users`', text)
                self.assertIn('raising `max_review_rounds` or `MAX_RETRIES`', text)
                self.assertIn('`.github/` from `protected_paths`', text)
                self.assertIn('enabling `conclave` or `[pin] auto`', text)
                self.assertIn('moving `reviewer_decision` among the non-critical classes of '
                              'escalation', text)
                # #417: renaming a label, a command or a marker is policy.
                self.assertIn('renaming the label of hold or of escalation (`agent:merge-hold`, '
                              '`agent:needs-operator`, `agent:needs-supervisor`); renaming a '
                              '`/devloop` command or the kind of a marker;', text)
                self.assertIn('- Not trust policy: a new or reworded comment or docstring; a new '
                              'timeout key', text)

    def test_both_prompts_carry_the_narrowed_rule_with_its_examples(self):
        # #447: the operator decides the policy, not every change that carries it out.
        for name in PROMPTS:
            with self.subTest(prompt=name):
                text = flat(section(name))
                self.assertIn('The operator decides the policy, not every change that carries '
                              'it out (ADR 0002).', text)
                self.assertIn('A change that touches the trust policy needs the operator when, '
                              'and only when, at least one of these three things holds:', text)
                points = ('1. It goes beyond or against a decision of the operator, or no '
                          'decision covers it.',
                          '2. It weakens a check',
                          # #472: point 3 is the rule alone.
                          '3. It changes the rule itself: this "Trust policy" section of the '
                          'review prompt (`devloop/prompts/review.md`) or of the advisory review '
                          'prompt (`devloop/prompts/advisory.md`), or the conditions that decide '
                          'when a change needs the operator. This holds even when an ADR asks '
                          'for the change.')
                positions = [text.index(point) for point in points]
                self.assertEqual(positions, sorted(positions))
                self.assertIn('the section "Decise dall\'operatore" of an ADR approved with the '
                              'cycle of `docs/adr/README.md` and present on `main`, one '
                              'written by the operator in the body of the issue, or a `/devloop '
                              'approve` of the operator, among those listed under "Decisions of '
                              'the operator on the issue" below (#472, #483). A comment that '
                              'section does not list, an approval written without the command '
                              'included, is not a decision. A `/devloop approve` covers only '
                              'what its text approves, within the limits and on the conditions '
                              'it sets: an approval of one part, or one subject to a condition, '
                              'does not cover the rest of the change. The points an ADR leaves '
                              'open are not decisions.', text)
                self.assertIn('no decision of the operator authorizes that weakening', text)
                # Faithful implementation: conformity, the decision read on main and cited.
                self.assertIn('**Faithful implementation.** When the issue cites the section of '
                              'an ADR on `main` and the change carries out its decision with no '
                              'deviation, check the conformity of the whole change to that '
                              'decision.', text)
                self.assertIn('never in the diff of the PR', text)
                self.assertIn('Cite it in your review: the file of the ADR, the section and the '
                              'point.', text)
                self.assertIn('Without that citation the change needs the operator', text)
                self.assertIn('Every deviation you find needs the operator, with its reason and '
                              'the decision it departs from.', text)
                self.assertIn('`operator_only_paths` (such as `docs/adr/` and `AGENTS.md`) '
                              'is still merged by the operator only', text)
                # One example of each case.
                for example in ('- Faithful implementation, the operator is not needed: the '
                                'issue cites `docs/adr/0005-deleghe-e-ruoli-degli-agent.md`, '
                                'section 4, D7',
                                '- Deviation, the operator is needed (point 1):',
                                '- Weakening without a decision, the operator is needed '
                                '(point 2):',
                                '- Change of the rule, the operator is needed (point 3):'):
                    self.assertIn(example, text)
                self.assertNotIn('needs_operator', shared(name))

    def test_a_class_a_decision_names_is_a_faithful_implementation(self):
        # #472: a change to NON_CRITICAL, a command or a merge condition that a decision names
        # follows point 1; what the decision does not name stays with the operator.
        for name in PROMPTS:
            with self.subTest(prompt=name):
                text = flat(section(name))
                self.assertIn('A change to the classes of escalation (`NON_CRITICAL` in '
                              '`devloop/supervision.py`), to a `/devloop` command or to a '
                              'condition of the merge is not a change of the rule when a '
                              'decision of the operator prescribes it explicitly, naming the '
                              'class, the command or the condition: it is a faithful '
                              'implementation, and follows point 1. What the decision does not '
                              'name, such as a class added beside the one it names, needs the '
                              'operator under point 1.', text)
                self.assertIn('- Faithful implementation, the operator is not needed: the issue '
                              'cites `docs/adr/0005-deleghe-e-ruoli-degli-agent.md`, section 4, '
                              'D2, which approves the delegations of its section 3.1, and the row '
                              'P5-1 of its plan, which names the non-critical class '
                              '`round_grantable`; the change adds `round_grantable` to '
                              '`NON_CRITICAL` with no deviation.', text)
                self.assertIn('or the change also adds to `NON_CRITICAL` a class the decision '
                              'does not name, such as `reviewer_decision`.', text)
                # The examples come in the order of the cases: faithful ones first.
                faithful = text.index('`round_grantable` to `NON_CRITICAL` with no deviation')
                self.assertLess(faithful, text.index('- Deviation, the operator is needed'))
                # Moving a class into NON_CRITICAL is no longer point 3 by itself.
                self.assertNotIn('or moves a class of escalation into or out of `NON_CRITICAL`',
                                 text)

    def test_a_change_of_the_rule_stays_with_the_operator_even_with_a_decision(self):
        # #472: the "Trust policy" section of a prompt is the rule, whatever asks for it.
        for name in PROMPTS:
            with self.subTest(prompt=name):
                text = flat(section(name))
                self.assertIn('- Change of the rule, the operator is needed (point 3): the change '
                              'edits this section of `devloop/prompts/review.md` or of '
                              '`devloop/prompts/advisory.md`, even when an ADR or a decision of '
                              'the operator asks for it.', text)
                self.assertIn('or the conditions that decide when a change needs the operator. '
                              'This holds even when an ADR asks for the change.', text)

    def test_a_comment_of_the_operator_is_cited_as_a_decision(self):
        # #472: the comments devloop lists as decisions of the operator, cited like an ADR.
        for name in PROMPTS:
            with self.subTest(prompt=name):
                text = flat(section(name))
                self.assertIn('The same holds for a decision in the body of the issue or in a '
                              '`/devloop approve` listed under "Decisions of the operator on the '
                              'issue": cite the command with its author and its time.', text)
                template = (PROMPT_DIR / f'{name}.md').read_text(encoding='utf-8')
                # The section follows the issue, after the rule that names it.
                self.assertIn('$issue_body\n\n$operator_decisions\n\n', template)
                self.assertLess(template.index('## Trust policy'),
                                template.index('$operator_decisions'))

    def test_the_readiness_check_applies_the_same_rule(self):
        # #472: the preflight gets the rule of the review prompt, whole, and declares its
        # outcome on the issue; the question goes to the operator before the implementation.
        rule = trust_policy_rule()
        self.assertEqual(rule, shared('review').strip())
        template = (PROMPT_DIR / 'preflight.md').read_text(encoding='utf-8')
        self.assertIn('## Trust policy\n', template)
        self.assertIn('\n$trust_policy\n', template)
        self.assertIn('$issue_comments\n\n$operator_decisions\n', template)
        text = flat(template)
        for words in ('The operator decides a change to the trust policy on the issue, before '
                      'anyone implements it, not on the pull request (#472).',
                      '- `none`: it does not touch the trust policy',
                      '- `covered`: it touches it, and a decision of the operator covers it with '
                      'no deviation and authorizes every weakening of a check it makes',
                      'or a `/devloop approve` of the operator, listed under "Decisions of the '
                      'operator on the issue" below (#483). Any other comment, also of a trusted '
                      'user and also one that approves the change without the command, is not '
                      'such a decision, and a command covers only what its text approves',
                      'A change of the rule, what point 3 lists included, still goes to the '
                      'operator for the merge even when a decision covers it',
                      '- `uncovered`: it touches it, and no decision of the operator covers it, '
                      'it goes beyond or against one, or it weakens a check that no decision '
                      'authorizes. A change of the rule itself is `uncovered` too, unless a '
                      'decision of the operator approves it.',
                      'With `uncovered` the issue is not ready: devloop adds a gap of kind '
                      '`operator_decision` with the question to the operator',
                      '- `trust_policy`: `none`, `covered` or `uncovered`, as above.',
                      'Write `description`, `proposed_default`, `split` and '
                      '`trust_policy_reason` in $language.'):
            self.assertIn(words, text)
        rendered = _template('preflight', **prompt_values(ssdlc_project.load()),
                             trust_policy=rule, language='italiano')
        self.assertIn(flat(rule), flat(rendered))
        self.assertIn('1. It changes the value, the default', rendered)

    def test_the_examples_cite_real_decisions(self):
        # The ADR and the points the examples cite exist on main as decisions of the operator.
        adr = (ROOT / 'docs/adr/0005-deleghe-e-ruoli-degli-agent.md').read_text(encoding='utf-8')
        decided = adr[adr.index('## 4. Decisioni riservate all\'operatore'):]
        self.assertIn('Decise dall\'operatore', decided)
        self.assertIn('- **D7.**', decided)
        self.assertIn('`impl:<agent>` prevale', decided)
        self.assertIn('- **D4.**', decided)
        self.assertIn('costanti del codice', decided)
        # #472: D2 approves the delegations of section 3.1, the row P5-1 names the class, and
        # the code has it among the non-critical ones.
        self.assertIn('- **D2.**', decided)
        self.assertIn('Le deleghe sono tutte approvate', decided)
        self.assertIn('### 3.1 Deleghe al supervisore', adr)
        self.assertIn('| P5-1 |', adr)
        self.assertIn('classe non critica `round_grantable`', adr)
        from devloop import supervision
        self.assertIn('round_grantable', supervision.NON_CRITICAL)
        self.assertTrue((ROOT / 'docs/adr/0002-ruoli-e-responsabilita.md').is_file())

    def test_a_faithful_implementation_gets_the_technical_verdict(self):
        review = flat(section('review'))
        self.assertIn('which of the three points holds, with its reason and, for a deviation, '
                      'the decision it departs from.', review)
        self.assertIn('A faithful implementation gets your technical verdict, `approve` or '
                      '`request_changes`, with the decision cited in `summary`.', review)
        advisory = flat(section('advisory'))
        self.assertIn('When the change needs the operator under this rule, your `opinion` is '
                      '`dubbi`', advisory)
        self.assertIn('A faithful implementation gets your opinion on its merits, with the '
                      'decision cited in `summary`.', advisory)

    def test_the_names_in_the_list_exist(self):
        # The prompts and the code the list names are real, so the rule does not drift from them.
        for name in ('review', 'advisory', 'conclave_proposal', 'conclave_vote', 'implement',
                     'fix', 'preflight'):
            self.assertTrue((PROMPT_DIR / f'{name}.md').is_file(), name)
        from devloop import supervision
        self.assertIn('reviewer_decision', supervision.CRITICAL)
        self.assertNotIn('reviewer_decision', supervision.NON_CRITICAL)
        config = (ROOT / 'devloop' / 'config.py').read_text(encoding='utf-8')
        self.assertIn("'protected_paths'", config)

    def test_the_labels_commands_and_markers_in_the_list_exist(self):
        # #417: the labels, commands and markers the rule names are those of the code.
        from devloop.config import Labels
        from devloop.conclave import VETO_COMMAND
        from devloop.dispatch_supervision import HOLD_COMMAND, RELEASE_COMMAND
        from devloop.markdown import marker, read_marker
        labels = Labels()
        named = (labels.ready_to_merge, labels.merge_hold, labels.needs_operator,
                 labels.needs_supervisor)
        self.assertEqual(named, ('agent:ready-to-merge', 'agent:merge-hold',
                                 'agent:needs-operator', 'agent:needs-supervisor'))
        commands = (HOLD_COMMAND, RELEASE_COMMAND, VETO_COMMAND)
        self.assertEqual(commands, ('/devloop hold', '/devloop release', '/devloop veto'))
        self.assertTrue(marker('hold', {}).startswith('<!-- devloop:hold:v1 '))
        # The form the prompts quote is not itself a record.
        self.assertIsNone(read_marker('<!-- devloop:hold:v1 ... -->', 'hold'))
        for name in PROMPTS:
            with self.subTest(prompt=name):
                text = flat(section(name))
                for word in (*named, *commands):
                    self.assertIn(f'`{word}`', text)

    def test_the_review_reassesses_the_rule_at_every_round_on_the_whole_diff(self):
        text = flat(section('review'))
        self.assertIn('Assess this rule again at every round, on the whole diff of the PR '
                      'against its base (`git diff origin/$base_branch...HEAD`), not only on '
                      'what the latest fix changed: a PR that needs the operator under this '
                      'rule stays `needs_operator` after any fix', text)
        self.assertIn('the conformity of a faithful implementation is checked again on the '
                      'whole diff', text)
        self.assertIn('The limit on new findings in later rounds (see "How to classify") does '
                      'not apply to this rule.', text)

    def test_the_two_prompts_share_the_same_rule(self):
        self.assertEqual(shared('review'), shared('advisory'))

    def test_the_review_asks_for_needs_operator_with_the_parts_touched(self):
        text = flat(section('review'))
        self.assertIn('When the change needs the operator under this rule, your verdict is '
                      '`needs_operator`, even when the change is correct and the issue asks '
                      'for it.', text)
        self.assertIn('Write in `human_reason` that the change touches the trust policy, and '
                      'list the parts it touches', text)
        self.assertIn('A change that does not touch the trust policy is not reported for it.',
                      text)
        verdicts = flat((PROMPT_DIR / 'review.md').read_text(encoding='utf-8'))
        self.assertIn('`approve`: no blocking finding, every previous finding resolved or '
                      'withdrawn, and the change does not need the operator for the trust '
                      'policy.', verdicts)
        self.assertIn('the change needs the operator for the trust policy (see "Trust policy" '
                      'above)', verdicts)

    def test_the_review_tells_trust_policy_from_decision(self):
        # #434: the reason of a needs_operator, and the findings listed in both cases.
        text = flat(section('review'))
        self.assertIn('Your `operator_reason` is `trust_policy`.', text)
        self.assertIn('Report your findings as usual, blocking ones included: this verdict does '
                      'not stop the loop.', text)
        verdicts = flat((PROMPT_DIR / 'review.md').read_text(encoding='utf-8'))
        self.assertIn('`trust_policy` when the only reason is the trust policy, `decision` for '
                      'every other case, which stops the loop until the operator decides. List '
                      'your blocking findings in both cases.', verdicts)
        self.assertIn('Set `operator_reason` to `none` with `approve` and `request_changes`.',
                      verdicts)

    def test_the_advisory_review_asks_for_doubts_and_the_first_residual_risk(self):
        text = flat(section('advisory'))
        self.assertIn('your `opinion` is `dubbi`, or `contrario` when the change weakens a check',
                      text)
        self.assertIn('The first item of `residual_risks` begins with "Policy di fiducia '
                      'toccata:" and lists the parts the change touches', text)
        self.assertIn('A change that does not touch the trust policy is not reported for it.',
                      text)
        # The advisory review has no verdict: its prompt never asks for one.
        self.assertNotIn('needs_operator', text)

    def test_the_rendered_prompts_keep_the_rule_whole(self):
        # With ssdlc's data in the prompts and without any (ADR 0006, R4): the rule is the
        # installation's, whatever the project gives.
        configs = (ssdlc_project.load(), SimpleNamespace(prompts=PromptFacts()))
        for name in PROMPTS:
            for cfg in configs:
                with self.subTest(prompt=name, data=bool(cfg.prompts.instructions)):
                    rendered = _template(name, **prompt_values(cfg), pr_number=7, repo='o/r',
                                         language='italiano')
                    self.assertIn(flat(shared(name)), flat(rendered))


def doc(path):
    return flat((ROOT / path).read_text(encoding='utf-8'))


class TrustPolicyDocsTest(unittest.TestCase):
    """The guide and the protocol describe the rule and its accepted residual risks (#399)."""

    def guide_section(self):
        text = (ROOT / 'docs/devloop/guide.md').read_text(encoding='utf-8')
        match = re.search(r'^### Changes to the trust policy\n(.*?)(?=^#{2,3} |\Z)', text,
                          re.M | re.S)
        self.assertTrue(match, 'guide.md has no "Changes to the trust policy" section')
        return flat(match.group(1))

    def test_the_guide_describes_the_closed_list(self):
        text = self.guide_section()
        self.assertIn('at every review level', text)
        self.assertIn('when, and only when, it does one of these five things (a closed list)',
                      text)
        for word in ('`trusted_users`', '`operator_only_paths`', '`protected_paths`',
                     '`[merge] mode`', '`[merge.levels]`', '`[review_policy]`', '`[roles]`',
                     '`conclave`', '`max_review_rounds`', '`[pin] auto`', '`[apps]`',
                     '`write_args`', '`review_args`', '`MAX_RETRIES`',
                     'the identity required for `/devloop` commands',
                     'the checks of `operator_only_paths` and `protected_paths`',
                     '`NON_CRITICAL` in `devloop/supervision.py`',
                     '`conclave_proposal.md`, `conclave_vote.md`',
                     '`implement.md`, `fix.md` and `preflight.md`',
                     # #417
                     'the names by which devloop recognises trusted commands and records (#417)',
                     'a kind of the `<!-- devloop:<kind>:v1 ... -->` markers',
                     'the names of the labels that govern merge, hold and escalation, defined in '
                     'the `Labels` dataclass of `devloop/config.py` and not configurable',
                     '`agent:ready-to-merge`', '`agent:merge-hold`', '`agent:needs-operator`',
                     '`agent:needs-supervisor`',
                     'would bypass the hold or the escalations'):
            self.assertIn(word, text)
        for point in range(1, 6):
            self.assertIn(f'{point}. it changes', text)
        self.assertNotIn('6. it changes', text)
        self.assertIn('Nothing else is policy, unless it also does one of the five things', text)

    def test_the_guide_describes_the_signals_and_the_rounds(self):
        text = self.guide_section()
        self.assertIn('gives the verdict `needs_operator`', text)
        self.assertIn('assess the rule again at every round, on the whole diff of the PR against '
                      'its base', text)
        self.assertIn('a PR already flagged stays `needs_operator` after a fix', text)
        self.assertIn('its opinion is `dubbi`, or `contrario` when the change weakens a check',
                      text)
        self.assertIn('"Policy di fiducia toccata:"', text)
        # #434: one hand-off to the operator.
        for words in ('`trust_policy` for this rule, `decision` for anything else',
                      'A `trust_policy` does not stop the loop',
                      'within `max_review_rounds` as usual',
                      'gets `agent:needs-operator` with the',
                      'devloop does not merge the PR itself',
                      'A `decision`, or a `needs_operator` without `operator_reason`'):
            self.assertIn(words, text)
        # #461: the operator's decision of 2026-11-19, a precisation of D10 in ADR 0005.
        for words in ('with one exception decided by the operator on 2026-11-19 (#461',
                      'that same reviewer approves the head of the latest round',
                      'it closed its own blocking findings itself',
                      'no reviewer of that round gives `trust_policy`'):
            self.assertIn(words, text)
        adr = (ROOT / 'docs' / 'adr' / '0005-deleghe-e-ruoli-degli-agent.md').read_text(
            encoding='utf-8')
        d10 = adr[adr.index('- **D10.** Tutto spento'):adr.index('Restano **aperti**')]
        self.assertIn('**Precisazione del 2026-11-19**', d10)
        self.assertIn('issues/461#issuecomment-7012437974', d10)
        self.assertIn('cade solo se lo stesso reviewer che l\'ha dato approva l\'head finale', d10)

    def test_the_guide_describes_when_the_operator_decides(self):
        # #447: the narrowed rule, with ADR 0002 cited.
        text = self.guide_section()
        start = text.index('**When the operator decides** (#447).')
        body = text[start:text.index('- **Review of the quorum.**', start)]
        for words in ('The operator decides the policy, not every change that carries it out '
                      '([ADR 0002](../adr/0002-ruoli-e-responsabilita.md)',
                      'needs the operator when, and only when, at least one of these three '
                      'things holds',
                      '1. it goes beyond or against a decision of the operator, or no decision '
                      'covers it', '"Decise dall\'operatore"',
                      '[cycle of the ADRs](../adr/README.md#ciclo-di-un-adr)',
                      '2. it weakens a check and no decision of the operator authorizes that '
                      'weakening',
                      '3. it changes the rule itself', '`NON_CRITICAL` in `devloop/supervision.py`',
                      'A **faithful implementation** does not need the operator',
                      'cites it in the review (file of the ADR, section and point, or the author '
                      'and time of the operator\'s command)',
                      '`approve` or `request_changes`, and devloop merges the PR like any other '
                      'of its level', 'Without the citation the PR needs the operator as before',
                      'every deviation found needs the operator',
                      'a conforming implementation of an ADR, a class of `NON_CRITICAL` an ADR '
                      'names, a deviation, a weakening without a decision, a change of the rule',
                      '`operator_only_paths` (`docs/adr/`, `AGENTS.md`, `CLAUDE.md`)',
                      '(D10) are not PRs', 'a `needs_operator` for a `decision` works as below'):
            self.assertIn(words, body)

    def test_the_guide_and_the_reference_describe_the_two_rules_of_472(self):
        # #472: point 3 narrowed to the rule itself, and the decision taken on the issue.
        text = self.guide_section()
        for words in ('3. it changes the rule itself: the "Trust policy" section of the review or '
                      'the advisory review prompt, or the conditions that decide when a change '
                      'needs the operator, even when an ADR asks for it.',
                      'Since #472 point 3 is the rule alone.',
                      'that a decision of the operator **prescribes explicitly**, naming the '
                      'class, the command or the condition, is a faithful implementation and '
                      'follows point 1',
                      'What the decision does not name, such as another class added beside it, '
                      'still does, under point 1.',
                      '**The decision on the issue, not on the PR** (#472, decided by the '
                      'operator on 2026-11-19',
                      # #483: a command, not any comment.
                      '**Approval with `/devloop approve`** (#483). A decision of the operator '
                      'on the issue is a command',
                      'not from the supervisor\'s identity nor from any app, after the latest '
                      'change of the title or the body of the issue and before devloop took it '
                      'on',
                      'an approval written without the command too, is context for the '
                      'implementer and authorizes nothing',
                      'A change of the text after the command drops it',
                      'are refused, and devloop answers each once, with the reason: in the '
                      'comment of the next readiness check, or, when none is due',
                      '**Complete history** (#483)',
                      'Since #483 the prompts list explicitly what point 3 always keeps with the '
                      'operator',
                      'declares in `trust_policy`',
                      '"Approvi la modifica della policy di fiducia descritta qui?"',
                      'with class `operator_decision`, to the operator also in `auto` mode',
                      'a faithful PR with no open finding and green CI is merged by devloop like '
                      'any other of its level, except for the paths of `operator_only_paths`'):
            self.assertIn(words, text)
        readiness = doc('docs/devloop/guide.md')
        readiness = readiness[readiness.index('### Readiness check before implementation'):]
        self.assertIn('`trust_policy` and `trust_policy_reason` (#472)', readiness)
        reference = doc('docs/devloop/reference.md')
        for words in ('Since #472 the rule itself is only the "Trust policy" section of the two '
                      'prompts and the conditions that decide when a change needs the operator',
                      'a change to `NON_CRITICAL`, to a `/devloop` command or to a condition of '
                      'the merge that a decision names explicitly is a faithful implementation',
                      'Since #483 a decision of the operator on the issue is only a `/devloop '
                      'approve` command, followed by the text of the decision, that a trusted '
                      'user wrote in person, not the supervisor\'s identity nor an app, after the '
                      'latest change of the issue\'s text and before the takeover',
                      'the history of the issue is read in full, or no command counts',
                      'The readiness check declares `trust_policy` and blocks, with the question '
                      'to the operator'):
            self.assertIn(words, reference)
        protocol = doc('docs/devloop/protocol.md')
        self.assertIn('`preflight.json` gains `trust_policy` and `trust_policy_reason` (#472',
                      protocol)
        self.assertIn('Since #472 the marker also records `trust_policy`', protocol)

    def test_the_adr_records_the_decision_on_472_as_a_precisation_of_d10(self):
        adr = (ROOT / 'docs' / 'adr' / '0005-deleghe-e-ruoli-degli-agent.md').read_text(
            encoding='utf-8')
        d10 = adr[adr.index('- **D10.** Tutto spento'):adr.index('Restano **aperti**')]
        self.assertIn('**Precisazione del 2026-11-19, su #472**', d10)
        # #483: the link points to the operator's comment, not to the issue.
        self.assertIn('[commento su #472](https://github.com/brushtyler/ssdlc/issues/472'
                      '#issuecomment-7018240088)', d10)
        self.assertNotIn('(https://github.com/brushtyler/ssdlc/issues/472)', d10)
        for words in ('Il punto 3 della regola dei reviewer (#447) resta solo per la regola stessa',
                      'prescrive esplicitamente',
                      'La decisione si prende sulla issue, non sulla PR',
                      'non dal supervisore né da un agent',
                      'una modifica del testo successiva lo rende non valido',
                      'salvo i percorsi di `operator_only_paths`'):
            self.assertIn(words, d10)
        status = adr[:adr.index('\n## ')]
        self.assertIn('la decisione dell\'operatore del 2026-11-19 su #472', status)

    def test_the_guide_lists_the_residual_risks(self):
        text = self.guide_section()
        self.assertIn('**Residual risks**, accepted by the operator', text)
        risks = text[text.index('**Residual risks**'):]
        for risk in ('the rule depends on the attention of the reviewers, at every round',
                     'a reviewer of a later round who looks only at the latest fix may approve '
                     'a PR an earlier round flagged',
                     'a change deliberately disguised as something else is not covered',
                     'the operator\'s local overlay (`config.local.toml`) can weaken the policy',
                     'with a conclave configured',
                     'is judged with the prompts of the running installation'):
            self.assertIn(risk, risks)

    def test_the_protocol_describes_the_rule_and_points_to_the_risks(self):
        text = doc('docs/devloop/protocol.md')
        start = text.index('**Trust policy** (#399).')
        body = text[start:text.index('Findings get stable ids', start)]
        for words in ('asks for `needs_operator` when the change touches the trust policy and '
                      'needs the operator for it, at every review level',
                      'Since #447 a change to the policy needs the operator only when it goes '
                      'beyond or against a decision of the operator',
                      'weakens a check that no decision authorizes',
                      'or changes the rule itself',
                      'the operator decides the policy, not every change that carries it out '
                      '([ADR 0002](../adr/0002-ruoli-e-responsabilita.md))',
                      'which the issue cites and the reviewer cites in its review (file, '
                      'section and point), gets the technical verdict',
                      'without the citation, or with a deviation, it is `needs_operator` as '
                      'before',
                      'again at every round on the whole diff of the PR against its base',
                      'the opinion `dubbi`', '"Policy di fiducia toccata:"',
                      'the same closed list of five points',
                      '[Changes to the trust policy](guide.md#changes-to-the-trust-policy)',
                      '`advisory.json` does not change, `review.json` only gains '
                      '`operator_reason` (#434)', 'residual risks, accepted by the operator',
                      'the attention of the reviewers at every round', 'a disguised change',
                      'the local overlay', 'the reserved perimeter'):
            self.assertIn(words, body)


if __name__ == '__main__':
    unittest.main()
