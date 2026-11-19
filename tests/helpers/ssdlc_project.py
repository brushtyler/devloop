"""ssdlc's configuration for the tests (ADR 0006, R3).

Since R3 ``devloop/config.toml`` holds the installation's defaults only: ssdlc's facts and
restrictions are in its project file, on the host, of which ``ops/host/ssdlc/project.toml`` is
the example, and the agy commands derived from its tests in the overlay
(``ops/host/ssdlc/overlay.toml``). devloop never reads either from the repository.

Since R6 ssdlc's identities and trust (``repo``, ``trusted_users``, ``[apps]``) are in the
overlay too, beside the agy commands: ``IDENTITY_KEYS`` of ``ops/host/ssdlc/overlay.toml``.

The tests describe devloop on ssdlc as before R3: ``data()`` is the single file the
configuration was, the installation's ``config.toml`` with the project file's keys and the
overlay's identity keys set in it; ``load()`` parses it, with an overlay when given.
``tests/test_devloop_ssdlc_project.py`` proves that it is the configuration devloop composes
from the installation, the project file and the overlay, apart from the project file's hash in
the policy version.
"""

import json
import re
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from devloop import config

ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT / 'ops' / 'host' / 'ssdlc' / 'project.toml'
OVERLAY = ROOT / 'ops' / 'host' / 'ssdlc' / 'overlay.toml'
# The keys of the overlay that left config.toml with ADR 0006, R6.
IDENTITY_KEYS = ('repo', 'trusted_users', 'apps')


def project():
    """The table of ssdlc's project file."""
    return config.read_toml(PROJECT, 'project file')


def identity():
    """ssdlc's identities and trust: the ``IDENTITY_KEYS`` of its overlay."""
    overlay = config.read_toml(OVERLAY, 'local config')
    return {key: overlay[key] for key in IDENTITY_KEYS}


def data():
    """ssdlc's raw configuration as one file: ``config.toml`` with the project file's keys and
    the overlay's identity keys."""
    return config._merge(config.read_toml(config.DEFAULT_CONFIG), {**identity(), **project()})


def load(local=None, root=config.INSTALL_ROOT):
    """ssdlc's configuration (``data``), with the overlay ``local`` when given."""
    raw = data()
    if local:
        raw = config.apply_overlay(raw, config.read_toml(local, 'local config'))
    return config.parse_config(raw, root)


def _key(key):
    return key if re.fullmatch(r'[A-Za-z0-9_-]+', key) else json.dumps(key)


def _value(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, list):
        return '[' + ', '.join(_value(v) for v in value) + ']'
    return json.dumps(value)  # a string or a number: the same form in TOML


def text():
    """``data`` as the text of one ``config.toml``: the installation's file with the project
    file's keys and the overlay's identity keys written in it, for the copies of the
    installation the tests pin. Only the shapes those keys have: values, lists and tables of
    them."""
    lines = config.DEFAULT_CONFIG.read_text(encoding='utf-8').split('\n')

    def put(path, table):
        values = [f'{_key(k)} = {_value(v)}' for k, v in table.items() if not isinstance(v, dict)]
        if values:
            if not path:
                lines[0:0] = values  # before the first table
            else:
                header = '[' + '.'.join(_key(k) for k in path) + ']'
                if header in lines:
                    at = lines.index(header) + 1
                    lines[at:at] = values
                else:
                    lines.extend(['', header, *values])
        for key, value in table.items():
            if isinstance(value, dict):
                put(path + (key,), value)

    put((), {**identity(), **project()})
    return '\n'.join(lines)


def toml_text(table, path=()):
    """``table`` as TOML text: its values, then each of its tables under its header. Only the
    shapes of the configuration: values, lists and tables of them."""
    lines = [f'{_key(k)} = {_value(v)}' for k, v in table.items() if not isinstance(v, dict)]
    for key, value in table.items():
        if isinstance(value, dict):
            header = '[' + '.'.join(_key(k) for k in path + (key,)) + ']'
            lines += ['', header, toml_text(value, path + (key,))]
    return '\n'.join(lines)


def identity_overlay(case):
    """For the test ``case``, an overlay with ssdlc's identity keys only (``identity``), without
    the agy commands. Returns the file."""
    tmp = Path(tempfile.mkdtemp())
    case.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    overlay = tmp / 'config.local.toml'
    overlay.write_text(toml_text(identity()) + '\n', encoding='utf-8')
    return overlay


def as_default(case):
    """For the test ``case``, ``config.DEFAULT_CONFIG`` is ssdlc's configuration as one file
    (``text``): the command line that reads the installation's file without a project file
    then loads as before R3. Returns the file."""
    tmp = Path(tempfile.mkdtemp())
    case.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    flat = tmp / 'config.toml'
    flat.write_text(text(), encoding='utf-8')
    patcher = mock.patch.object(config, 'DEFAULT_CONFIG', flat)
    patcher.start()
    case.addCleanup(patcher.stop)
    return flat
