"""devloop: the default state_dir is ``.devloop`` (#563).

Before #563 the default was ``.ssdlc/devloop``, inside ``.ssdlc/``, the state directory of the
old SSDLC engine, which the operator still uses in other repositories. The default is ``.devloop``
everywhere: the code, the installation's configuration, the scripts, ``ops/`` and ``.gitignore``.
No file uses the value of before as its default: only the history of the decision (the ADRs, the
sections of ``ops/host/migrate.md``, the tests of those sections) and the lines that name it as
the value of before #563, which the launcher refuses to fall back on. The tag
``ssdlc-engine-final`` and the ``.ssdlc/`` directories of the old engine in other repositories
are not touched, and the documentation says so.
"""

import importlib.machinery
import importlib.util
import re
import subprocess
import unittest
from pathlib import Path

from devloop import config

ROOT = Path(__file__).resolve().parents[1]
MIGRATE = ROOT / 'ops' / 'host' / 'migrate.md'
SECTION = '\n## Percorsi devloop e istanza devloop@devloop (#563)\n'
# The forms of the state_dir of before #563: as a path, or built from its two names.
OLD = re.compile(r"\.ssdlc/devloop|'\.ssdlc'\)?\s*/\s*'devloop'")
# The history of the decision, where the value of before stays as it was: the ADRs, the guide of
# the steps on the host (its sections of before #563, and the one of #563 that moves it), and the
# tests of those sections of the guide.
HISTORY = ('docs/adr/', 'ops/host/migrate.md', 'tests/test_ops.py')
# Elsewhere a line may name the value of before only as such: with #563 within this many lines.
NEAR = 4


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def files():
    """The files of the checkout, tracked or new, without those git ignores."""
    done = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                          cwd=ROOT, capture_output=True, text=True)
    if done.returncode != 0:
        raise unittest.SkipTest('not a git checkout')
    return sorted({name for name in done.stdout.split('\0') if name and (ROOT / name).is_file()})


class DefaultTests(unittest.TestCase):
    def test_the_code_and_the_installation_default_to_devloop(self):
        installation = config.read_toml(ROOT / 'devloop' / 'config.toml')
        self.assertEqual(installation['state_dir'], '.devloop')
        # Without the key, the code's default.
        source = (ROOT / 'devloop' / 'config.py').read_text(encoding='utf-8')
        self.assertIn("state_dir=data.get('state_dir', '.devloop'),", source)

    def test_the_scripts_default_to_devloop(self):
        launcher = load(ROOT / 'scripts' / 'devloop-pinned', 'devloop_pinned_563')
        self.assertEqual(launcher.STATE, Path('.devloop'))
        self.assertEqual(launcher.OLD_STATE, Path('.ssdlc') / 'devloop')
        wrapper = load(ROOT / 'scripts' / 'agent-token.py', 'agent_token_563')
        self.assertEqual(wrapper.STATE, Path('.devloop'))
        for name in ('supervisor-token.py', 'coordinator-token.py'):
            with self.subTest(script=name):
                text = (ROOT / 'scripts' / name).read_text(encoding='utf-8')
                self.assertIn("STATE = REPO_ROOT / '.devloop'\n", text)
        tests = (ROOT / 'scripts' / 'run-tests.py').read_text(encoding='utf-8')
        self.assertIn("Path(ROOT) / '.devloop' / 'test-durations.json'", tests)

    def test_ops_and_gitignore_default_to_devloop(self):
        lib = (ROOT / 'ops' / 'lib.sh').read_text(encoding='utf-8')
        self.assertIn('\nOPS_STATE_DIR=.devloop\n', lib)
        example = (ROOT / 'ops' / 'params.env.example').read_text(encoding='utf-8')
        self.assertIn('\n# STATE_DIR=.devloop\n', example)
        ignored = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
        self.assertIn('.devloop/', ignored)
        # The old engine's directory stays out of git in an old checkout; devloop never uses it.
        self.assertIn('.ssdlc/', ignored)
        done = subprocess.run(['git', 'check-ignore', '-q', '.devloop/test-durations.json'],
                              cwd=ROOT)
        if done.returncode == 128:
            self.skipTest('not a git checkout')
        self.assertEqual(done.returncode, 0)

    def test_no_file_uses_the_state_dir_of_before_as_its_default(self):
        found = []
        for name in files():
            if name.startswith(HISTORY) or name == 'tests/test_devloop_state_dir.py':
                continue
            try:
                lines = (ROOT / name).read_text(encoding='utf-8').splitlines()
            except UnicodeDecodeError:
                continue
            for i, line in enumerate(lines):
                if not OLD.search(line):
                    continue
                around = '\n'.join(lines[max(0, i - NEAR):i + NEAR + 1])
                if '#563' not in around:
                    found.append(f'{name}:{i + 1}: {line.strip()}')
        self.assertEqual(found, [])

    def test_the_check_sees_a_default_of_before(self):
        # The pattern of the check above catches both forms, and not the new default.
        self.assertTrue(OLD.search("state_dir = data.get('state_dir', '.ssdlc/devloop')"))
        self.assertTrue(OLD.search("STATE = Path('.ssdlc') / 'devloop'"))
        self.assertFalse(OLD.search("STATE = Path('.devloop')"))
        self.assertFalse(OLD.search('the tag ssdlc-engine-final and .ssdlc/ of the engine'))


class OldEngineTests(unittest.TestCase):
    """Criterion 5: the tag ssdlc-engine-final and the .ssdlc/ of the old engine in other
    repositories are not touched, and the documentation says so."""

    def section(self):
        text = MIGRATE.read_text(encoding='utf-8')
        start = text.index(SECTION)
        end = text.find('\n## ', start + 1)
        return text[start:] if end < 0 else text[start:end]

    def test_the_documentation_says_so(self):
        for path, phrases in (
                ('README.md', ('`ssdlc-engine-final`', 'devloop neither reads nor writes `.ssdlc/`',
                               'are not touched')),
                ('ops/README.md', ('`ssdlc-engine-final`', 'devloop non legge né scrive `.ssdlc/`',
                                   'nessuno script e nessun passo li tocca')),
                ('docs/adr/0007-da-ssdlc-a-devloop.md', (
                    '## Precisazione del 2026-11-23: i percorsi sull\'host (#563)',
                    'Il tag `ssdlc-engine-final` e le directory `.ssdlc/` del vecchio motore negli '
                    'altri repository: devloop non le legge né le scrive.')),
                ('ops/host/migrate.md', ('**Il vecchio motore non si tocca:** il tag '
                                         '`ssdlc-engine-final` resta com\'è',
                                         'directory `.ssdlc/` del vecchio motore negli altri '
                                         'repository')),
                ('docs/devloop/reference.md', ('which devloop neither reads nor writes',))):
            text = ' '.join((ROOT / path).read_text(encoding='utf-8').split())
            for phrase in phrases:
                with self.subTest(path=path, phrase=phrase):
                    self.assertIn(phrase, text)

    def test_the_migration_touches_only_devloops_own_state_dir(self):
        section = self.section()
        # No command touches the tag.
        self.assertNotRegex(section, r'git (tag|push)\b[^\n]*ssdlc-engine-final')
        # Every .ssdlc path it names is devloop's state_dir of before, or the directory of devloop's
        # clone that held it: never the .ssdlc/ of another repository.
        clones = ('/home/devloop/ssdlc/.ssdlc', '/home/devloop/devloop/.ssdlc')
        for match in re.finditer(r'[^\s`\'"(]*\.ssdlc\b[^\s`\'")]*', section.replace('\\', '')):
            path = match.group()
            with self.subTest(path=path):
                if path.startswith('/'):
                    self.assertTrue(path.startswith(clones), path)
                else:
                    self.assertTrue(path.startswith(('.ssdlc/devloop', '.ssdlc/')), path)
        # The code of devloop never builds a path under .ssdlc/: only the launchers name it, to
        # stop on it.
        for path in sorted((ROOT / 'devloop').rglob('*.py')):
            with self.subTest(module=path.name):
                self.assertNotIn('.ssdlc', path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
