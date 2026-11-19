"""devloop: the texts for prompts and comments moved to ``devloop/context.py`` (#292, step S2 of
#261).

The move changes no behaviour. Since step S7 (#297) the tests of the prompts, the comments and
the label transition import these names from ``devloop.context``, and ``devloop.dispatcher`` no
longer re-exports them. Here only what the move adds is checked: where the names live and the
documentation map of the new module.
"""

import unittest
from pathlib import Path

from devloop import context, dispatcher

from helpers import ssdlc_project

# Every name the move took out of devloop/dispatcher.py.
MOVED = ('PROMPT_DIR', 'REREVIEW_COMMAND', 'LABEL_STYLE', 'requested_fixes', 'operator_comments',
         'requests_rereview', 'format_operator_comments', '_template', 'DOCS_PATHS_SHOWN',
         'docs_map_section', '_docs_list', 'docs_diff_section', 'docs_review_section',
         '_format_findings', 'format_fix_requests', '_format_requested', 'format_previous_rounds',
         '_open_findings_reason', '_review_failure', '_trusted_comments',
         '_render_issue_comments', '_render_acceptance', '_failure_text', '_start_failure',
         '_describe_bases', '_format_conflicts', 'label_transition', 'both_labels_warning',
         'transition_labels')


class ContextModuleTests(unittest.TestCase):
    def test_every_moved_name_lives_in_the_module(self):
        for name in MOVED:
            with self.subTest(name=name):
                self.assertTrue(hasattr(context, name))
                if callable(getattr(context, name)):
                    self.assertEqual(getattr(context, name).__module__, 'devloop.context')

    def test_the_prompts_stay_where_they_were(self):
        self.assertEqual(context.PROMPT_DIR, Path(dispatcher.__file__).resolve().parent / 'prompts')
        self.assertTrue((context.PROMPT_DIR / 'implement.md').is_file())

    def test_the_documentation_map_covers_the_module(self):
        docs = ssdlc_project.load().docs
        self.assertEqual(dict(docs.entries)['devloop/context.py'],
                         dict(docs.entries)['devloop/dispatcher.py'])
        self.assertEqual(sorted(docs.documents(['devloop/context.py'])),
                         sorted(docs.documents(['devloop/dispatcher.py'])))


if __name__ == '__main__':
    unittest.main()
