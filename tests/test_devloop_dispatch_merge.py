"""devloop: receipt, level and merge moved to ``devloop/dispatch_merge.py`` (#295, step S5 of
#261).

The move changes no behaviour: the tests of receipts, promotions and merges keep importing from
``devloop.dispatcher`` and calling the methods on ``Dispatcher``, which inherits them from the
mixin. Here only what the move adds is checked: the composition of ``Dispatcher`` and the
documentation map of the new module. No module-level name moved.
"""

import unittest

from devloop import config, dispatch_merge, dispatcher

# Every method of Dispatcher the move took into the mixin.
METHODS = ('_approval_current', '_receipt', '_receipt_and_paths', '_base_check', '_base_checks',
           '_merge_check', '_select', '_policy_comment', '_escalate_level', '_promote',
           '_promote_zero', '_zero_problem', '_acceptance_problems', '_refuse_zero', '_do_revoke',
           '_do_hold', '_do_merge', '_close_merged_issue', '_do_merged_issue')


class DispatchMergeModuleTests(unittest.TestCase):
    def test_the_dispatcher_inherits_the_methods_from_the_mixin(self):
        self.assertTrue(issubclass(dispatcher.Dispatcher, dispatch_merge.MergeMixin))
        for name in METHODS:
            with self.subTest(name=name):
                self.assertIn(name, vars(dispatch_merge.MergeMixin))
                self.assertNotIn(name, vars(dispatcher.Dispatcher))
                self.assertIs(getattr(dispatcher.Dispatcher, name),
                              getattr(dispatch_merge.MergeMixin, name))

    def test_the_documentation_map_covers_the_module(self):
        docs = config.load_config().docs
        self.assertEqual(dict(docs.entries)['devloop/dispatch_merge.py'],
                         dict(docs.entries)['devloop/dispatcher.py'])
        self.assertEqual(sorted(docs.documents(['devloop/dispatch_merge.py'])),
                         sorted(docs.documents(['devloop/dispatcher.py'])))


if __name__ == '__main__':
    unittest.main()
