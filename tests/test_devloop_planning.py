"""devloop: planning and queue moved to ``devloop/planning.py`` (#291, step S1 of #261).

The move changes no behaviour: the tests of ``plan``, the actions and the commands keep importing
from ``devloop.dispatcher``, which re-exports the same objects. Here only what the move adds is
checked: the re-exports and the documentation map of the new module.
"""

import unittest

from devloop import config, dispatcher, planning

# Every name the move took out of devloop/dispatcher.py.
MOVED = ('RECHECK_COMMAND', 'FIX_COMMAND', 'MERGING', 'RESYNC_AFTER_APPROVAL', 'BASE_CHANGED',
         'EXPENSIVE', 'GLOBAL_ACTIONS', 'MERGE_LANE', 'PRIORITY_LABELS', 'PRIORITY_RANK',
         'PullRequest', 'operator_blocked', 'operator_held', 'operator_merge_due', 'Snapshot',
         'Action', 'schedule', '_lanes', 'numbers_of', 'admit', 'label_names', 'issue_priority',
         'start_order', 'queue_lines', 'zero_labels_hold', 'merge_ready', 'parked', 'pr_counts',
         'plan', '_plan_preflight', 'preflight_state', 'pending_reviewers', '_round_records',
         'waived_rounds', '_resynced', 'counted_rounds', '_parents', 'touched_by_base')


class PlanningModuleTests(unittest.TestCase):
    def test_the_dispatcher_re_exports_every_moved_name(self):
        for name in MOVED:
            with self.subTest(name=name):
                self.assertIs(getattr(dispatcher, name), getattr(planning, name))
                if callable(getattr(planning, name)):
                    self.assertEqual(getattr(planning, name).__module__, 'devloop.planning')

    def test_the_documentation_map_covers_the_module(self):
        docs = config.load_config().docs
        self.assertEqual(dict(docs.entries)['devloop/planning.py'],
                         dict(docs.entries)['devloop/dispatcher.py'])
        self.assertEqual(sorted(docs.documents(['devloop/planning.py'])),
                         sorted(docs.documents(['devloop/dispatcher.py'])))


if __name__ == '__main__':
    unittest.main()
