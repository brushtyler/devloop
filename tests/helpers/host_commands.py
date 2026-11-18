"""Direct host commands for engine tests that do not verify writer fencing.

In an admitted run every trusted host command of the controller goes through
``ssdlc_writer_gate.run_host()`` (#345, docs/writer-fencing.md, "Host writers"): one
supervisor process per command, about 100 ms against the few milliseconds of a direct
``git``. Tests of other engine behaviour run thousands of them.

``direct_host_commands(test)`` installs, for one test, a direct runner through the hook
the controller already has, ``ssdlc_role_fencing.host_commands``: each scope that an
entrypoint opens (``orchestrator._writer_fencing_scope``) installs ``direct_runner``
instead of the run's fenced runner, so every host command is a plain ``subprocess.run``,
as before #345. Admission, the run state and every other engine path are unchanged.

Production code is unchanged and reads no flag or environment variable: only a test that
calls this helper changes runner. The tests of fencing (``test_host_command_fencing``,
``test_role_fencing``, ``test_writer_gate`` and the fencing cases of the other modules)
never call it, so they keep the real supervisor.
"""
import subprocess
from unittest.mock import patch

import ssdlc_role_fencing

_live_host_commands = ssdlc_role_fencing.host_commands


def direct_runner(argv, **kwargs):
    """``subprocess.run``, looked up at each call so that a test's own patch still applies."""
    return subprocess.run(argv, **kwargs)


def _direct_scope(runner):
    """The real hook, installing ``direct_runner`` in place of the scope's ``runner``."""
    return _live_host_commands(direct_runner)


def direct_host_commands(test):
    """Run the host commands of every engine scope directly for ``test``; removed by its cleanups."""
    patcher = patch.object(ssdlc_role_fencing, 'host_commands', _direct_scope)
    patcher.start()
    test.addCleanup(patcher.stop)
