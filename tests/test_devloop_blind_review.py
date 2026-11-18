"""devloop: a review blind to the author of the change (#389).

The review and advisory prompts name neither the implementer's key nor its model, and say that the
reviewer is not the author; the attribution headers and the markers are taken out of the texts
they pass on (issue, PR description, comments, implementer responses). The originals on GitHub,
the PR's marker and the selection of the reviewers do not change."""

import sys
import unittest
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from devloop import markdown  # noqa: E402
from devloop.context import format_operator_comments, format_previous_rounds  # noqa: E402
from devloop.dispatcher import Dispatcher  # noqa: E402
from helpers.devloop_fakes import FakeGH  # noqa: E402
from test_devloop import CFG, IMPL, META, FakeRunner, FakeWS, norm, pr, raw_review  # noqa: E402
from test_devloop_advisory import ADVISORY_TITLE, Base, answer  # noqa: E402

AGENT = CFG.agent(IMPL)
# The header devloop publishes the implementer's texts with: bot, version, agent, model, effort.
HEADER = markdown.header(AGENT.display_name, '9.9', AGENT.model_label, AGENT.effort,
                         bot=markdown.agent_bot(IMPL), version='0123456789ab')
# A header an agent writes outside devloop, as on the issues the operator opens with one.
ISSUE_HEADER = markdown.header(AGENT.display_name, '2.1.322', AGENT.model_label, 'high')
ISSUE_BODY = ISSUE_HEADER + '\nRichiesta dell\'operatore.\n\n## Problema\n\nIl reviewer sa chi.'
PR_BODY = markdown.publishable(HEADER + '\nMotivazione della PR.\n\nCloses #7', 'pr',
                               dict(META, profile={'model': AGENT.model}))
BLIND = 'You are not the author of this change'


def assert_blind(test, prompt):
    """No trace of who wrote the change in ``prompt``, and the sentence on not being its author."""
    flat = ' '.join(prompt.split())
    test.assertIn(BLIND, flat)
    test.assertIn('that does not make you the author', flat)
    for trace in ('> [!NOTE]', '**Agent:**', '**Model:**', '**Bot:**', '**Reasoning Level:**',
                  'devloop:pr:v1', AGENT.display_name, AGENT.model, AGENT.model_label,
                  markdown.agent_bot(IMPL)):
        test.assertNotIn(trace, prompt)
    # Not even the implementer's key, in any case.
    test.assertNotIn(IMPL.lower(), prompt.lower())
    # The texts themselves arrive.
    test.assertIn('Richiesta dell\'operatore.', prompt)
    test.assertIn('Motivazione della PR.', prompt)


class AttributedGH(FakeGH):
    """FakeGH whose issue opens with an attribution header."""

    def issue(self, number):
        return dict(super().issue(number), body=ISSUE_BODY)

    def issue_history(self, number):
        return dict(super().issue_history(number), body=ISSUE_BODY)


class WithoutAttributionTests(unittest.TestCase):
    def test_the_headers_and_the_markers_go_the_text_stays(self):
        self.assertEqual(markdown.without_attribution(PR_BODY),
                         'Motivazione della PR.\n\nCloses #7')
        self.assertEqual(markdown.without_attribution(ISSUE_BODY),
                         'Richiesta dell\'operatore.\n\n## Problema\n\nIl reviewer sa chi.')
        # A dispatcher header, two headers in a row, a header further down: all go.
        text = (markdown.dispatcher_header() + '\n' + ISSUE_HEADER + '\nPrima.\n\n' + HEADER
                + '\nDopo.')
        self.assertEqual(markdown.without_attribution(text), 'Prima.\n\nDopo.')
        for empty in (None, '', HEADER):
            self.assertEqual(markdown.without_attribution(empty), '')

    def test_technical_text_and_other_notes_stay(self):
        # Text that names an agent or a model, a note with other content, a header quoted in a
        # code block: none of them is an attribution header.
        text = ('Il trailer `Co-Authored-By: Claude Opus 5.5` resta.\n\n'
                '> [!NOTE]\n> **Model:** Claude Opus 5.5\n> Il modello dell\'esempio.\n\n'
                '```markdown\n> [!NOTE]\n> **Agent:** <AGENT_NAME> (CLI v<CLI_VERSION>)\n```\n\n'
                '~~~\n> [!NOTE]\n> **Agent:** x\n~~~\nFine.')
        self.assertEqual(markdown.without_attribution(text), text)


class ReviewPromptTests(unittest.TestCase):
    def review(self, p, comments=()):
        self.gh = AttributedGH()
        runner = FakeRunner(raw_review())
        d = Dispatcher(CFG, self.gh, FakeWS(), runner, log=lambda m: None)
        p.body = PR_BODY
        p.operator_comments = list(comments)
        d._do_review(p)
        ((key, mode, schema, prompt),) = runner.calls
        self.assertEqual((mode, schema), ('review', 'review'))
        return key, prompt

    def test_the_review_prompt_is_blind_to_the_author(self):
        key, prompt = self.review(pr())
        assert_blind(self, prompt)
        # The reviewer is still chosen away from the implementer, whose key the marker keeps.
        self.assertNotEqual(key, IMPL)
        self.assertEqual(META['implementer'], IMPL)

    def test_comments_and_responses_lose_their_headers(self):
        comment = {'author': {'login': 'brushtyler'},
                   'body': '/devloop rereview\n\n' + HEADER + '\nDa rivedere il punto 2.'}
        rounds = [norm(raw_review('request_changes', [{'file': 'x.py', 'line': 1, 'title': 'b',
                                                       'detail': 'd', 'suggested_fix': ''}]))]
        fixes = [{'round': 1, 'head_sha': 'a' * 40, 'responses': [
            {'finding_id': 'R1-1', 'action': 'fixed', 'note': HEADER + '\nCorretto in x.py.'}]}]
        _, prompt = self.review(pr(reviews=rounds, fixes=fixes), [comment])
        assert_blind(self, prompt)
        self.assertIn('Da rivedere il punto 2.', prompt)
        self.assertIn('- R1-1: fixed: Corretto in x.py.', prompt)

    def test_the_sections_strip_the_headers_by_themselves(self):
        comments = format_operator_comments([{'author': {'login': 'brushtyler'},
                                              'body': HEADER + '\nTesto.'}])
        self.assertIn('### Comment by brushtyler\n\nTesto.', comments)
        self.assertNotIn('[!NOTE]', comments)
        rounds = format_previous_rounds([norm(raw_review('request_changes'))], [
            {'round': 1, 'head_sha': 'a' * 40,
             'responses': [{'finding_id': 'R1-1', 'action': 'disputed', 'note': HEADER + 'No.'}]}])
        self.assertIn('- R1-1: disputed: No.', rounds)
        self.assertNotIn('[!NOTE]', rounds)


class AdvisoryPromptTests(Base):
    def test_the_advisory_prompt_is_blind_to_the_author(self):
        self.make(answer())
        self.gh.issue = lambda number: dict(FakeGH.issue(self.gh, number), body=ISSUE_BODY)
        history = self.gh.issue_history
        self.gh.issue_history = lambda number: dict(history(number), body=ISSUE_BODY)
        raw_prs = self.gh.open_prs
        self.gh.open_prs = lambda: [dict(raw, body=PR_BODY) for raw in raw_prs()]
        self.tick()
        ((key, mode, schema, prompt),) = self.runner.calls
        # Even run by the implementer's own agent, the advisory review is told it is not the
        # author, and sees no trace of who is.
        self.assertEqual((key, schema), (IMPL, 'advisory'))
        self.assertIn(ADVISORY_TITLE, ' '.join(prompt.split()))
        assert_blind(self, prompt)


if __name__ == '__main__':
    unittest.main()
