"""devloop: readiness check moved to ``devloop/dispatch_preflight.py`` (#294, step S4 of #261).

The move changes no behaviour: the tests of the readiness check keep importing from
``devloop.dispatcher`` and calling the methods on ``Dispatcher``, which inherits them from the
mixin. Here only what the move adds is checked: the re-exports, the composition of
``Dispatcher`` and the documentation map of the new module.
"""

import unittest

from devloop import config, dispatch_preflight, dispatcher

# Every module-level name the move took out of devloop/dispatcher.py.
MOVED = ('devloop_text', 'normalize_preflight')
# Every method of Dispatcher the move took into the mixin.
METHODS = ('_preflight_records', '_published', '_ledger_record', '_automatic',
           '_recheck_requested', '_owns_block', '_owned_block', '_preflight_view',
           '_publish_preflight', '_do_preflight', '_do_preflight_block')


class DispatchPreflightModuleTests(unittest.TestCase):
    def test_the_dispatcher_re_exports_every_moved_name(self):
        for name in MOVED:
            with self.subTest(name=name):
                self.assertIs(getattr(dispatcher, name), getattr(dispatch_preflight, name))
                self.assertEqual(getattr(dispatch_preflight, name).__module__,
                                 'devloop.dispatch_preflight')

    def test_the_dispatcher_inherits_the_methods_from_the_mixin(self):
        self.assertTrue(issubclass(dispatcher.Dispatcher, dispatch_preflight.PreflightMixin))
        for name in METHODS:
            with self.subTest(name=name):
                self.assertIn(name, vars(dispatch_preflight.PreflightMixin))
                self.assertNotIn(name, vars(dispatcher.Dispatcher))
                self.assertIs(getattr(dispatcher.Dispatcher, name),
                              getattr(dispatch_preflight.PreflightMixin, name))

    def test_the_documentation_map_covers_the_module(self):
        docs = config.load_config().docs
        self.assertEqual(dict(docs.entries)['devloop/dispatch_preflight.py'],
                         dict(docs.entries)['devloop/dispatcher.py'])
        self.assertEqual(sorted(docs.documents(['devloop/dispatch_preflight.py'])),
                         sorted(docs.documents(['devloop/dispatcher.py'])))


if __name__ == '__main__':
    unittest.main()
