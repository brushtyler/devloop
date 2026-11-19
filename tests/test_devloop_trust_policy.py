"""devloop: the rule on the trust policy in the review prompts (#399).

The review prompt and the advisory review prompt carry the same closed list of what the trust
policy is, with its examples; each then asks for its own signal to the operator."""

import re
import unittest

from devloop.context import PROMPT_DIR, _template

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
      'the identity required for `/devloop` commands')),
    ('4. It changes when a PR is merged or promoted, or who decides on it:',
     ('the merge conditions', 'the quorum of the reviewers',
      'the checks of `operator_only_paths` and `protected_paths`',
      'the choice of the review level',
      'the classes of escalation the supervisor decides in `auto` mode '
      '(`NON_CRITICAL` in `devloop/supervision.py`) and those that stay with the operator')),
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
    return body[:body.index('\nWhen the change touches the trust policy')]


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
                self.assertIn('- Not trust policy: a new or reworded comment or docstring; a new '
                              'timeout key', text)

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

    def test_the_review_reassesses_the_rule_at_every_round_on_the_whole_diff(self):
        text = flat(section('review'))
        self.assertIn('Assess this rule again at every round, on the whole diff of the PR '
                      'against its base (`git diff origin/$base_branch...HEAD`), not only on '
                      'what the latest fix changed: a PR that touches the trust policy stays '
                      '`needs_operator` after any fix', text)
        self.assertIn('The limit on new findings in later rounds (see "How to classify") does '
                      'not apply to this rule.', text)

    def test_the_two_prompts_share_the_same_rule(self):
        self.assertEqual(shared('review'), shared('advisory'))

    def test_the_review_asks_for_needs_operator_with_the_parts_touched(self):
        text = flat(section('review'))
        self.assertIn('When the change touches the trust policy, your verdict is '
                      '`needs_operator`, even when the change is correct and the issue asks '
                      'for it.', text)
        self.assertIn('Write in `human_reason` that the change touches the trust policy, and '
                      'list the parts it touches', text)
        self.assertIn('A change that does not touch the trust policy is not reported for it.',
                      text)
        verdicts = flat((PROMPT_DIR / 'review.md').read_text(encoding='utf-8'))
        self.assertIn('`approve`: no blocking finding, every previous finding resolved or '
                      'withdrawn, and the change does not touch the trust policy.', verdicts)
        self.assertIn('the change touches the trust policy (see "Trust policy" above)', verdicts)

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
        self.assertIn('Leave `operator_reason` empty (`""`) with `approve` and '
                      '`request_changes`.', verdicts)

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
        for name in PROMPTS:
            with self.subTest(prompt=name):
                rendered = _template(name, pr_number=7, repo='o/r', language='italiano')
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
                     '`implement.md`, `fix.md` and `preflight.md`'):
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
                      'devloop never merges the PR itself',
                      'A `decision`, or a `needs_operator` without `operator_reason`'):
            self.assertIn(words, text)

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
        for words in ('asks for `needs_operator` when the change touches the trust policy, at '
                      'every review level',
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
