"""devloop: the texts for prompts and comments moved to ``devloop/context.py`` (#292, step S2 of
#261).

The move changes no behaviour: the tests of the prompts, the comments and the label transition
keep importing from ``devloop.dispatcher``, which re-exports the same objects. Here only what the
move adds is checked: the re-exports and the documentation map of the new module.
"""

import unittest
from pathlib import Path

from devloop import config, context, dispatcher

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
    def test_the_dispatcher_re_exports_every_moved_name(self):
        for name in MOVED:
            with self.subTest(name=name):
                self.assertIs(getattr(dispatcher, name), getattr(context, name))
                if callable(getattr(context, name)):
                    self.assertEqual(getattr(context, name).__module__, 'devloop.context')

    def test_the_prompts_stay_where_they_were(self):
        self.assertEqual(context.PROMPT_DIR, Path(dispatcher.__file__).resolve().parent / 'prompts')
        self.assertTrue((dispatcher.PROMPT_DIR / 'implement.md').is_file())

    def test_the_documentation_map_covers_the_module(self):
        docs = config.load_config().docs
        self.assertEqual(dict(docs.entries)['devloop/context.py'],
                         dict(docs.entries)['devloop/dispatcher.py'])
        self.assertEqual(sorted(docs.documents(['devloop/context.py'])),
                         sorted(docs.documents(['devloop/dispatcher.py'])))


if __name__ == '__main__':
    unittest.main()
