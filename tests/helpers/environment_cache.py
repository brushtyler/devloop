"""Reuse repeated, identical observations of the test host within one process.

The engine re-observes two things that the tests never change: the executing
controller checkout (three git subprocesses and a hash of every controller
file) and the identity of the role-container image (one ``docker image
inspect``). The guards that make these observations still run and still compare
or record the result; only the repeated observation of the unchanged host is
computed once per test process. Any other root and any other command is
observed live, and a test that patches these entry points itself replaces the
cache for its duration. Production code is unchanged.
"""
import copy
import subprocess
from pathlib import Path
from unittest.mock import patch

import orchestrator
import ssdlc_controller

CONTROLLER_ROOT = Path(orchestrator.__file__).resolve().parent
_live_controller_snapshot = ssdlc_controller.controller_snapshot
_live_run = subprocess.run
_cache = {}


def cached_controller_snapshot(root):
    """Snapshot the executing controller checkout once per test process."""
    if Path(root).resolve() != CONTROLLER_ROOT:
        return _live_controller_snapshot(root)
    if 'snapshot' not in _cache:
        _cache['snapshot'] = _live_controller_snapshot(root)
    return copy.deepcopy(_cache['snapshot'])


def _image_probe_argv():
    return ['docker', 'image', 'inspect', '--format', '{{.Id}}', orchestrator.EngineRunner.IMAGE_NAME]


def cached_run(*args, **kwargs):
    """subprocess.run that probes the role-container image once per process.

    Only the exact identity probe of the verification environment is reused,
    with its original arguments; every other command runs live.
    """
    argv = args[0] if args else kwargs.get('args')
    if (isinstance(argv, list) and argv == _image_probe_argv() and len(args) == 1
            and kwargs == dict(capture_output=True, text=True, timeout=30)):
        key = ('image_probe', tuple(argv))
        if key not in _cache:
            try:
                _cache[key] = ('result', _live_run(*args, **kwargs))
            except (OSError, subprocess.SubprocessError) as error:
                _cache[key] = ('error', error)
        kind, value = _cache[key]
        if kind == 'error':
            raise value
        return subprocess.CompletedProcess(value.args, value.returncode, value.stdout, value.stderr)
    return _live_run(*args, **kwargs)


def reuse_host_observations(test):
    """Install both caches for one test; they are removed by its cleanups."""
    for target, replacement in (
            ('ssdlc_controller.controller_snapshot', cached_controller_snapshot),
            ('orchestrator.controller_snapshot', cached_controller_snapshot),
            ('subprocess.run', cached_run)):
        patcher = patch(target, new=replacement)
        patcher.start()
        test.addCleanup(patcher.stop)
