"""The quick start for a new project (ADR 0006, R9): docs/devloop/quickstart.md.

The page is checked locally, without GitHub, apps, logins or a host (the real run is the
operator's, #515): its example project file and overlay load with the installation's
configuration, its parameters are accepted by ops/lib.sh, the overlay and the commands name the
users and paths those parameters give, and the commands it cites exist.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from devloop import config

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / 'docs' / 'devloop' / 'quickstart.md'
OPS = ROOT / 'ops'
BASH = shutil.which('bash')
# The actor of each [apps.*] table and the role of its manifest in ops/github/apps/.
ROLE = {'dispatcher': 'dispatcher', 'implementer': 'implementer', 'merger': 'merger',
        'supervisor': 'supervisor', 'claude': 'claude-agent', 'codex': 'codex-agent',
        'agy': 'agy-agent'}
# The parameters ops/lib.sh derives, printed by ``derived()``.
DERIVED = ('REPO', 'APP_PREFIX', 'PROJECT', 'DEFAULT_BRANCH', 'DEVLOOP_USER', 'AGENT_USER',
           'DEVLOOP_HOME', 'DEVLOOP_CLONE', 'STATE_PATH', 'SOURCE_REF', 'SOURCE_CLONE',
           'DEVLOOP_KEY_DIR', 'SUPERVISOR_KEY_DIR', 'LAUNCHER', 'LAUNCHER_CMD', 'SERVICE_NAME',
           'REPO_LOCK_DIR', 'DEVLOOP_KEYS', 'SUPERVISOR_KEYS', 'APPS', 'SUPERVISOR_SOURCE_CLONE',
           'COORDINATOR_KEY_DIR')


def text():
    return PAGE.read_text(encoding='utf-8')


def example(name):
    """The fenced block right after ``<!-- quickstart: <name> -->``."""
    match = re.search(r'<!-- quickstart: ' + re.escape(name) + r' -->\n```[a-z]*\n(.*?)\n```',
                      text(), re.S)
    if match is None:
        raise AssertionError(f'{PAGE}: no example {name!r}')
    return match.group(1) + '\n'


def blocks(language):
    return re.findall(r'```' + language + r'\n(.*?)\n```', text(), re.S)


def commands():
    """The commands of the bash blocks, one per line, with their continuations joined."""
    out = []
    for block in blocks('bash'):
        for line in block.replace('\\\n', ' ').splitlines():
            line = line.split('  #', 1)[0].strip()
            if line and not line.startswith('#'):
                out.append(' '.join(line.split()))
    return out


class QuickstartTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='quickstart-')
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def write(self, name, content):
        path = self.tmp / name
        path.write_text(content, encoding='utf-8')
        return path

    def load(self):
        overlay = self.write('config.local.toml', example('config.local.toml'))
        project = self.write('project.toml', example('project.toml'))
        return config.load_config(ROOT / 'devloop' / 'config.toml', overlay, project)

    def derived(self):
        """The parameters of the example, completed and checked by ``ops_load_params``. The
        units and sudoers of the host are left out: what the test sees does not depend on it."""
        params = self.write('params-widget.env', example('params'))
        script = ('. "$1/lib.sh"\nPARAMS_FILE=$2\nops_load_params\n'
                  'for v in ' + ' '.join(DERIVED) + '; do printf "%s=%s\\n" "$v" "${!v}"; done\n')
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LC_ALL': 'C.UTF-8',
               'SYSTEMD_DIR': str(self.tmp / 'systemd'),
               'SUDOERS_FILE': str(self.tmp / 'sudoers.d' / 'devloop-supervisor-widget'),
               'AGENT_SUDOERS_FILE': str(self.tmp / 'sudoers.d' / 'devloop-agent-widget')}
        done = subprocess.run([BASH, '-c', script, 'quickstart', str(OPS), str(params)],
                              capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return dict(line.split('=', 1) for line in done.stdout.splitlines())


class PageTests(unittest.TestCase):
    def test_the_page_is_linked_from_the_devloop_index_the_guide_and_the_ops_guide(self):
        self.assertIn('(devloop/quickstart.md)', (ROOT / 'docs/devloop.md').read_text())
        self.assertIn('(quickstart.md)', (ROOT / 'docs/devloop/guide.md').read_text())
        self.assertIn('(../docs/devloop/quickstart.md)', (OPS / 'README.md').read_text())

    def test_no_installation_example_takes_devloop_from_the_old_name(self):
        # ADR 0007, R4 (#541): the repository is brushtyler/devloop, and the examples of an
        # installation take devloop from it, never from brushtyler/ssdlc.
        self.assertIn('SOURCE_REPO=brushtyler/devloop\n', example('params'))
        pages = {'docs/devloop/quickstart.md', 'docs/devloop/guide.md', 'docs/devloop/reference.md',
                 'ops/README.md', 'ops/params.env.example', 'README.md'}
        for name in sorted(pages):
            page = (ROOT / name).read_text(encoding='utf-8')
            with self.subTest(page=name):
                self.assertNotIn('SOURCE_REPO=brushtyler/ssdlc', page)
                self.assertNotIn('github.com/brushtyler/ssdlc.git', page)
        self.assertIn('# SOURCE_REPO=brushtyler/devloop\n',
                      (OPS / 'params.env.example').read_text(encoding='utf-8'))
        self.assertIn('`SOURCE_REPO=brushtyler/devloop`', (OPS / 'README.md').read_text())

    def test_it_states_the_rules_of_adr_0006(self):
        page = ' '.join(text().split())
        for phrase in ('One repository, one host.', 'never configure the same repository on '
                       'two hosts', 'installed on that repository only and never shared',
                       'The merger app is created only for a repository where devloop merges',
                       'There is no container for now', 'is the operator\'s (#515)'):
            self.assertIn(phrase, page)


class ExampleFileTests(QuickstartTestCase):
    def test_the_project_file_and_the_overlay_load_with_the_installation(self):
        cfg = self.load()
        self.assertEqual(cfg.repo, 'acme/widget')
        self.assertEqual(cfg.trusted_users, ('acme-operator',))
        self.assertEqual(cfg.test_cmd, 'python3 -m pytest -q')
        self.assertEqual(cfg.local_tests, 'full')
        self.assertEqual(cfg.merge.mode, 'off')
        # The set of the repository, without the merger, since devloop does not merge here.
        self.assertEqual(set(cfg.apps), {'dispatcher', 'implementer', 'claude', 'codex', 'agy',
                                         'supervisor'})
        self.assertEqual(cfg.app_of(cfg.implementer), 'implementer')
        # The overlay alone admits the project's test command to agy.
        self.assertIn('python3 -m pytest', cfg.agents['agy-reviewer'].write_commands)
        self.assertEqual(cfg.prompts.module_test_cmd, 'python3 -m pytest -q tests/test_name.py')

    def test_the_project_file_holds_only_keys_of_the_project_file(self):
        project = config.read_toml(self.write('project.toml', example('project.toml')))
        config.compose({}, project)
        overlay = config.read_toml(self.write('config.local.toml',
                                              example('config.local.toml')))
        # Each trust key of the overlay would stop devloop in the project file.
        for key in ('repo', 'trusted_users', 'state_dir', 'source', 'apps', 'agent_user'):
            with self.subTest(key=key):
                with self.assertRaisesRegex(config.ConfigError, 'set it in the local overlay'):
                    config.compose({}, {key: overlay[key]})

    def test_a_merge_mode_without_the_merger_is_still_the_operators_choice(self):
        # The page tells to add [apps.merger] with a merge mode: the example has neither.
        self.assertNotIn('[merge]', example('config.local.toml'))
        lists = [line for line in example('params').splitlines()
                 if line.startswith(('APPS=', 'DEVLOOP_KEYS='))]
        self.assertEqual(len(lists), 2)
        for line in lists:
            self.assertNotIn('merger', line)


@unittest.skipIf(BASH is None, 'bash is not installed')
class ParameterTests(QuickstartTestCase):
    def test_the_parameters_are_accepted_and_give_the_names_of_the_page(self):
        p = self.derived()
        self.assertEqual(p['PROJECT'], 'widget')
        self.assertEqual(p['DEVLOOP_USER'], 'devloop-widget')
        self.assertEqual(p['AGENT_USER'], 'devloop-widget-agent')
        self.assertEqual(p['SERVICE_NAME'], 'devloop@widget')
        page = text()
        for value in ('DEVLOOP_USER', 'AGENT_USER', 'DEVLOOP_CLONE', 'SOURCE_CLONE',
                      'STATE_PATH', 'DEVLOOP_KEY_DIR', 'SUPERVISOR_KEY_DIR', 'LAUNCHER',
                      'SERVICE_NAME'):
            with self.subTest(value=value):
                self.assertIn(f'`{p[value]}`', page)

    def test_the_overlay_names_the_users_and_paths_of_the_parameters(self):
        p, cfg = self.derived(), self.load()
        self.assertEqual(cfg.repo, p['REPO'])
        self.assertEqual(cfg.base_branch, p['DEFAULT_BRANCH'])
        self.assertEqual(cfg.state_dir, p['STATE_PATH'])
        self.assertEqual(cfg.source, p['SOURCE_CLONE'])
        self.assertEqual(cfg.repo_lock_dir, p['REPO_LOCK_DIR'])
        self.assertEqual(cfg.agent_user.name, p['AGENT_USER'])
        self.assertEqual(cfg.app_key_dir, p['DEVLOOP_KEY_DIR'])
        for actor, app in cfg.apps.items():
            with self.subTest(actor=actor):
                self.assertEqual(app.slug, p['APP_PREFIX'] + ROLE[actor])
                self.assertIn(ROLE[actor], p['APPS'].split())
        # devloop reads the key of every app but the supervisor's, which is in the supervisor's
        # key_dir.
        self.assertEqual(sorted(p['DEVLOOP_KEYS'].split()),
                         sorted(ROLE[a] for a in cfg.apps if a != 'supervisor'))
        self.assertIn('supervisor', p['SUPERVISOR_KEYS'].split())
        for role in p['APPS'].split():
            with self.subTest(manifest=role):
                self.assertTrue((OPS / 'github' / 'apps' / f'{role}.json').is_file())

    def test_the_commands_name_the_users_and_paths_of_the_parameters(self):
        p = self.derived()
        lines = commands()
        launcher = f'sudo -u {p["DEVLOOP_USER"]} -H {p["LAUNCHER_CMD"]}'
        for sub in ('apps', 'tick --dry-run', 'setup'):
            with self.subTest(sub=sub):
                self.assertIn(f'{launcher} {sub}', lines)
        state = p['STATE_PATH']
        pin = [line for line in lines if line.endswith(f' pin {p["SOURCE_REF"]}\'')]
        self.assertEqual(len(pin), 1, lines)
        for part in (f'sudo -u {p["DEVLOOP_USER"]} -H', f'PYTHONPATH={p["SOURCE_CLONE"]}'.replace(
                p['DEVLOOP_HOME'], '~'), f'cd {p["DEVLOOP_CLONE"]}'.replace(p['DEVLOOP_HOME'], '~'),
                'python3 -P -m devloop', f'--project-config {state}/project.toml',
                f'--local-config {state}/config.local.toml'):
            with self.subTest(part=part):
                self.assertIn(part, pin[0])
        self.assertIn(f'sudo systemctl start {p["SERVICE_NAME"]}', lines)
        self.assertIn(f'sudo -u {p["DEVLOOP_USER"]} -H tail -f {state}/run.log', lines)
        owner = f'-o {p["DEVLOOP_USER"]} -g {p["DEVLOOP_USER"]} -m 600'
        self.assertIn(f'sudo install {owner} project.toml {state}/project.toml', lines)
        self.assertIn(f'sudo install {owner} <slug>.<date>.private-key.pem '
                      f'{p["DEVLOOP_KEY_DIR"]}/', lines)
        self.assertIn(f'{state}/config.local.toml', text())

    def test_the_supervisor_mints_its_tokens_from_the_clone_of_the_source(self):
        # #571: acme/widget does not hold scripts/supervisor-token.py: the supervisor runs it
        # from the clone of the source that setup-supervisor.sh makes, with its key_dir and the
        # project's state_dir; its read-only token of gh must reach the new repository.
        p = self.derived()
        lines = commands()
        self.assertIn(f'cd {p["SUPERVISOR_SOURCE_CLONE"]}', lines)
        self.assertIn(f'DEVLOOP_APP_KEY_DIR={p["SUPERVISOR_KEY_DIR"]} python3 '
                      f'scripts/supervisor-token.py --state-dir {p["STATE_PATH"]} -- '
                      'gh issue comment <ISSUE> --body-file <FILE>', lines)
        self.assertIn(f'`{p["SUPERVISOR_SOURCE_CLONE"]}` (`SUPERVISOR_SOURCE_CLONE`)', text())
        self.assertTrue((ROOT / 'scripts' / 'supervisor-token.py').is_file())
        page = ' '.join(text().split())
        self.assertIn("The supervisor's read-only `gh` token must reach `acme/widget` too", page)

    def test_the_coordinator_is_optional_for_the_project(self):
        # #571: check-host.sh checks the coordinator only if the overlay declares it; the page
        # says how to add it, with the app, its key_dir and setup-coordinator.sh.
        p = self.derived()
        page = ' '.join(text().split())
        self.assertIn('A project has a coordinator only if its overlay declares it, with '
                      '`[apps.coordinator]` or `[roles.coordinator]`', page)
        self.assertIn('the app `coordinator` in `APPS`', page)
        self.assertIn(f'its key in `{p["COORDINATOR_KEY_DIR"]}`', page)
        self.assertIn('`sudo bash ops/host/setup-coordinator.sh --params ops/params-widget.env`',
                      page)
        # The example has none: the parameters leave out its app, the overlay its tables.
        self.assertNotIn('coordinator', p['APPS'].split())
        self.assertNotIn('coordinator', example('config.local.toml'))


class CommandTests(unittest.TestCase):
    def test_the_ops_paths_it_cites_exist(self):
        cited = set(re.findall(r'(?<![\w/.~-])ops/[\w./@-]*[\w/]', text()))
        self.assertIn('ops/host/setup-devloop.sh', cited)
        # The parameters file is the one the page has the operator write.
        cited.discard('ops/params-widget.env')
        for path in sorted(cited):
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).exists())

    def test_the_ops_scripts_take_the_options_it_gives_them(self):
        common = (OPS / 'lib.sh').read_text()
        scripts = [line for line in commands() if re.match(r'(sudo )?bash ops/', line)]
        self.assertTrue(scripts)
        for line in scripts:
            script = re.search(r'bash (ops/\S+)', line).group(1)
            source = (ROOT / script).read_text()
            for option in re.findall(r'(?<!\S)(--[a-z-]+)', line):
                with self.subTest(script=script, option=option):
                    self.assertTrue(f'{option})' in source or f'{option})' in common
                                    or f'{option} |' in common, option)

    def test_the_devloop_commands_it_cites_exist(self):
        cited = set()
        for line in commands():
            match = re.search(r'(?:devloop-pinned --state-dir \S+|-m devloop(?: --\S+ \S+)*) '
                              r'([a-z]+)((?: --[a-z-]+)*)', line)
            if match:
                cited.add((match.group(1), match.group(2).strip()))
        self.assertEqual({sub for sub, _ in cited}, {'pin', 'apps', 'tick', 'setup'})
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        for sub, options in sorted(cited):
            with self.subTest(command=sub):
                done = subprocess.run([sys.executable, '-P', '-m', 'devloop', sub, '--help'],
                                      cwd=ROOT, env=env, capture_output=True, text=True,
                                      timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr)
                for option in options.split():
                    self.assertIn(option, done.stdout)
        # The launcher of the page takes --state-dir.
        self.assertIn('--state-dir', (ROOT / 'scripts' / 'devloop-pinned').read_text())


if __name__ == '__main__':
    unittest.main()
