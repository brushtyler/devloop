"""devloop: supervision and commands moved to ``devloop/dispatch_supervision.py`` (#293, step S3
of #261).

The move changes no behaviour: the tests of supervision, hold, release and fix commands call the
methods on ``Dispatcher``, which inherits them from the mixin. Since step S7 (#297) they import
the module-level names from ``devloop.dispatch_supervision``, and ``devloop.dispatcher`` no
longer re-exports them. Here only what the move adds is checked: where the names live, the
composition of ``Dispatcher`` and the documentation map of the new module.
"""

import unittest

from devloop import config, dispatch_supervision, dispatcher

# Every module-level name the move took out of devloop/dispatcher.py.
MOVED = ('HOLD_COMMAND', 'RELEASE_COMMAND', 'RESUME_STEP', 'merge_command', 'fix_command',
         'UntrustedChange')
# Every method of Dispatcher the move took into the mixin.
METHODS = ('_supervised', '_issue_key', '_current_key', '_supervisor_view', '_to_operator',
           '_do_supervise', '_accept_defaults', '_decide', '_decidable_check', '_hold_command',
           '_read_thread', '_fix_requests')


class DispatchSupervisionModuleTests(unittest.TestCase):
    def test_every_moved_name_lives_in_the_module(self):
        for name in MOVED:
            with self.subTest(name=name):
                self.assertTrue(hasattr(dispatch_supervision, name))
                if callable(getattr(dispatch_supervision, name)):
                    self.assertEqual(getattr(dispatch_supervision, name).__module__,
                                     'devloop.dispatch_supervision')

    def test_the_dispatcher_inherits_the_methods_from_the_mixin(self):
        self.assertTrue(issubclass(dispatcher.Dispatcher, dispatch_supervision.SupervisionMixin))
        for name in METHODS:
            with self.subTest(name=name):
                self.assertIn(name, vars(dispatch_supervision.SupervisionMixin))
                self.assertNotIn(name, vars(dispatcher.Dispatcher))
                self.assertIs(getattr(dispatcher.Dispatcher, name),
                              getattr(dispatch_supervision.SupervisionMixin, name))

    def test_the_documentation_map_covers_the_module(self):
        docs = config.load_config().docs
        self.assertEqual(dict(docs.entries)['devloop/dispatch_supervision.py'],
                         dict(docs.entries)['devloop/dispatcher.py'])
        self.assertEqual(sorted(docs.documents(['devloop/dispatch_supervision.py'])),
                         sorted(docs.documents(['devloop/dispatcher.py'])))


if __name__ == '__main__':
    unittest.main()
