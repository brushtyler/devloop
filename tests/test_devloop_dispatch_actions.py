"""devloop: work actions moved to ``devloop/dispatch_actions.py`` (#296, step S6 of #261).

The move changes no behaviour: the tests of implement, CI failures, reviews, fixes and syncs call
the methods on ``Dispatcher``, which inherits them from the mixin. Since step S7 (#297) they
import the module-level names from ``devloop.dispatch_actions``, and ``devloop.dispatcher`` no
longer re-exports them. Here only what the move adds is checked: where the names live, the
composition of ``Dispatcher`` and the documentation map of the new module.
"""

import unittest

from devloop import config, dispatch_actions, dispatcher

# Every module-level name the move took out of devloop/dispatcher.py.
MOVED = ('rerun_of', 'synced_from', 'skipped_models', 'Models', 'ready_approval')
# Every method of Dispatcher the move took into the mixin.
METHODS = ('_do_implement', '_current_receipt', '_do_ci_failed', '_rerun_unrelated',
           '_last_round_allowed', '_open_round', '_do_review', '_together',
           '_full_suite_without_ci', '_do_fix', '_align_fix', '_publish_fix',
           '_do_requested_fix', '_do_sync')


class DispatchActionsModuleTests(unittest.TestCase):
    def test_every_moved_name_lives_in_the_module(self):
        for name in MOVED:
            with self.subTest(name=name):
                self.assertTrue(hasattr(dispatch_actions, name))
                self.assertEqual(getattr(dispatch_actions, name).__module__,
                                 'devloop.dispatch_actions')

    def test_the_dispatcher_inherits_the_methods_from_the_mixin(self):
        self.assertTrue(issubclass(dispatcher.Dispatcher, dispatch_actions.ActionsMixin))
        for name in METHODS:
            with self.subTest(name=name):
                self.assertIn(name, vars(dispatch_actions.ActionsMixin))
                self.assertNotIn(name, vars(dispatcher.Dispatcher))
                self.assertIs(getattr(dispatcher.Dispatcher, name),
                              getattr(dispatch_actions.ActionsMixin, name))

    def test_the_documentation_map_covers_the_module(self):
        docs = config.load_config().docs
        self.assertEqual(dict(docs.entries)['devloop/dispatch_actions.py'],
                         dict(docs.entries)['devloop/dispatcher.py'])
        self.assertEqual(sorted(docs.documents(['devloop/dispatch_actions.py'])),
                         sorted(docs.documents(['devloop/dispatcher.py'])))


if __name__ == '__main__':
    unittest.main()
