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
     'keys:', ('`trusted_users`', '`operator_only_paths`', '`[merge] mode`', '`[review_policy]`',
               '`sensitive_paths`', '`sensitive_path_lists`', '`zero`', '`advisory_reviewer`',
               '`[roles]`', '`implementer`', '`reviewers`', '`supervisor`',
               '`max_review_rounds`', '`[apps]`', '`write_args`', '`review_args`',
               'the other permissions of the agents')),
    ('2. It changes the constants that cap the delegations and the retries',
     ('`MAX_RETRIES`',)),
    ('3. It changes who is believed:',
     ('the filter of the trusted authors of the comments',
      'the identity that wrote a marker or a record',
      'the identity required for `/devloop` commands')),
    ('4. It changes when a PR is merged or promoted:',
     ('the merge conditions', 'the quorum of the reviewers', 'the check of `operator_only_paths`',
      'the choice of the review level')),
    ('5. It changes the review prompt, the advisory review prompt or this rule itself.', ()),
)


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
                self.assertIn('- Not trust policy: a new or reworded comment or docstring; a new '
                              'timeout key', text)

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


if __name__ == '__main__':
    unittest.main()
