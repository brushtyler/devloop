"""ops/ (issue #402): guide, scripts and declarative files that recreate devloop's installation.

The scripts run here only with ``--dry-run`` and on temporary directories: a ``PATH`` of shims
records any command that would change the host (``sudo``, ``apt-get``, ``useradd``, …), and the
tests check that none ran.
"""
import getpass
import grp
import html
import importlib.util
import itertools
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

from devloop import agentuser, apps
from devloop.config import load_config
from helpers import ssdlc_project

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / 'ops'
SCRIPTS = ('host/setup-devloop.sh', 'host/setup-supervisor.sh', 'host/setup-agent-user.sh',
           'host/setup-coordinator.sh', 'host/check-host.sh', 'host/start-role.sh',
           'github/create-apps.sh')
BASH = shutil.which('bash')
ME = getpass.getuser()
PREFIX = 'brushtyler-devloop-'
DEVLOOP_KEYS = ('dispatcher', 'merger', 'claude-agent', 'codex-agent', 'agy-agent')
SUPERVISOR_KEYS = ('supervisor', 'claude-agent')
# Commands that change the host: in --dry-run none of them may run.
MUTATING = ('sudo', 'apt-get', 'useradd', 'setfacl', 'visudo', 'chown', 'chmod', 'install',
            'git', 'npm', 'runuser', 'touch', 'mkdir', 'systemctl', 'rsync', 'groupadd',
            'usermod', 'systemd-tmpfiles')
# The primary group of the user running the tests: the group of the repository locks of the
# checks (ADR 0006, R8), one the user is a member of.
GROUP = grp.getgrgid(os.getgid()).gr_name
LEVEL = {'read': 1, 'write': 2}
# The users of the host the fakes simulate (``OpsTestCase.fake_host``, #497), never the user that
# runs the tests: a default of params.env (devloop-agent, supervisor) may be that user.
FAKE_AGENT = 'opsdry-agent'
FAKE_SUPERVISOR = 'opsdry-supervisor'
FAKE_COORDINATOR = 'opsdry-coordinator'
# What the fakes of the CLIs print, as a model would: check-host.sh never shows it (#530).
MODEL_TEXT = 'MODEL-TEXT-4c1d9e'
# A COORDINATOR_OPS_DIR for setup-coordinator.sh --dry-run, which refuses one under directories
# not root's (#530, R1-1): the temporary directories of the tests are the user's. It need not
# exist: the directories above it that exist (/usr, /usr/local) are root's on any host.
ROOT_COPY = '/usr/local/lib/devloop-coordinator-opstest'
# What ``sudo --version`` prints for each sudo the fakes simulate: the host moved from sudo-rs to
# the classic sudo on 2026-11-20, and the scripts behave the same with either.
SUDO_FLAVORS = {'sudo-rs': 'sudo-rs 0.2.8', 'classic sudo': 'Sudo version 1.9.17p2'}


def supervisor_token():
    spec = importlib.util.spec_from_file_location('supervisor_token',
                                                  ROOT / 'scripts/supervisor-token.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifest(role):
    return json.loads((OPS / 'github/apps' / f'{role}.json').read_text())


# What devloop/apps.py asks for each role, action by action.
AGENT = (apps.IMPLEMENT, apps.REVIEW, apps.PR_COMMENT, apps.ISSUE_COMMENT)
REQUIRED = {
    'dispatcher': (apps.RERUN, apps.PR_COMMENT, apps.ISSUE_COMMENT),
    'merger': (apps.MERGE,),
    'claude-agent': AGENT,
    'codex-agent': AGENT,
    'agy-agent': AGENT,
    'implementer': (apps.IMPLEMENT, apps.PR_COMMENT, apps.ISSUE_COMMENT),
    # The supervisor writes through scripts/supervisor-token.py (#408): its default permissions
    # and the highest level of each permission it may ask for.
    'supervisor': (supervisor_token().DEFAULT,
                   {name: max(levels, key=LEVEL.get)
                    for name, levels in supervisor_token().ALLOWED.items()}),
}


def render(template, values):
    """``render`` of ops/lib.sh: ``@NAME@`` replaced, one trailing newline."""
    text = (OPS / template).read_text().rstrip('\n')
    for name, value in values.items():
        text = text.replace(f'@{name}@', value)
    return text + '\n'


@unittest.skipIf(BASH is None, 'bash is not installed')
class OpsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='ops-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        self.calls = self.tmp / 'calls'
        for name in MUTATING:
            self.shim(name, 'echo "$(basename "$0") $*" >> "$OPS_TEST_CALLS"')
        self.release = self.tmp / 'os-release'
        self.release.write_text('ID=ubuntu\nVERSION_ID="26.04"\n')
        self.home = self.tmp / 'home'

    def shim(self, name, body):
        path = self.bin / name
        path.write_text(f'#!/bin/sh\n{body}\n')
        path.chmod(0o755)

    def fake_host(self, sudo, users=()):
        """Fakes of the host (#497) in the PATH of the scripts, which then never reach the real
        ``id``, ``getent``, ``getfacl`` or ``sudo``: what a test sees does not depend on the user
        that runs it nor on the host. They simulate:

        - ``id``: the current user ``ME``, the owner of the temporary files, unprivileged (uid
          1000) and in ``GROUP``; the users ``users``, each with a group of its own; no other;
        - ``getent group``: ``GROUP`` and the groups of ``users``;
        - ``getfacl``, unless the test has its own: no ACL entry for any user;
        - ``sudo``: ``sudo`` of ``SUDO_FLAVORS``, whose ``--version`` says which; any other call
          is recorded as a change of the host, as the shims of ``MUTATING`` (``setfacl``
          among them) are."""
        known = '|'.join(shlex.quote(u) for u in (ME, *users))
        groups = '|'.join(shlex.quote(g) for g in (GROUP, *users))
        self.shim('id', f'''opt=
case "$1" in -u | -un | -gn | -nG | -G) opt=$1; shift ;; esac
user=${{1:-{shlex.quote(ME)}}}
case "$user" in {known}) ;; *) echo "id: '$user': no such user" >&2; exit 1 ;; esac
if [ "$user" = {shlex.quote(ME)} ]; then uid=1000 group={shlex.quote(GROUP)}
else uid=1001 group=$user; fi
case "$opt" in
    -u) echo "$uid" ;;
    -un) echo "$user" ;;
    -gn | -nG) echo "$group" ;;
    -G) echo "$uid" ;;
    *) echo "uid=$uid($user) gid=$uid($group) groups=$uid($group)" ;;
esac''')
        self.shim('getent', f'''[ "$1" = group ] && [ $# -eq 2 ] || exit 2
case "$2" in {groups}) echo "$2:x:1000:" ;; *) exit 2 ;; esac''')
        if not (self.bin / 'getfacl').exists():
            self.shim('getfacl', 'echo "user::rwx"\necho "group::r-x"\necho "other::---"')
        self.shim('sudo', f'''if [ "$1" = --version ] || [ "$1" = -V ]; then
    echo {shlex.quote(SUDO_FLAVORS[sudo])}; exit 0
fi
echo "sudo $*" >> "$OPS_TEST_CALLS"''')

    def params(self, **overrides):
        values = {
            'DEVLOOP_HOME': str(self.home / 'devloop'),
            'SUPERVISOR_HOME': str(self.home / 'supervisor'),
            'SYSTEMD_DIR': str(self.tmp / 'systemd'),
            'SUDOERS_FILE': str(self.tmp / 'sudoers.d' / 'devloop-supervisor'),
            'AGENT_HOME': str(self.home / 'agent'),
            'AGENT_SUDOERS_FILE': str(self.tmp / 'sudoers.d' / 'devloop-agent'),
            'REPO_LOCK_DIR': str(self.tmp / 'locks' / 'repos'),
            'REPO_LOCK_GROUP': GROUP,
            'TMPFILES_FILE': str(self.tmp / 'tmpfiles.d' / 'devloop-repos.conf'),
            # The coordinator (#530): a user the host does not have, unless a test fakes it.
            'COORDINATOR_USER': FAKE_COORDINATOR,
            'COORDINATOR_HOME': str(self.home / 'coordinator'),
            'COORDINATOR_SUDOERS_FILE': str(self.tmp / 'sudoers.d' / 'devloop-coordinator'),
            'COORDINATOR_OPS_DIR': str(self.tmp / 'lib' / 'devloop-coordinator'),
        }
        values.update(overrides)
        path = self.tmp / 'params.env'
        lines = [f'. {OPS / "params.env.example"}'] + [f'{k}={v}' for k, v in values.items()]
        path.write_text('\n'.join(lines) + '\n')
        return path

    def run_script(self, script, *args, arch='aarch64'):
        env = dict(os.environ, PATH=f'{self.bin}:{os.environ.get("PATH", "/usr/bin:/bin")}',
                   OPS_TEST_CALLS=str(self.calls), OPS_OS_RELEASE=str(self.release),
                   OPS_ARCH=arch, LC_ALL='C.UTF-8')
        return subprocess.run([BASH, str(OPS / script), *args], capture_output=True, text=True,
                              env=env, stdin=subprocess.DEVNULL, timeout=120)

    def lock_dir(self, path):
        """The directory of the repository locks as setup-devloop.sh leaves it (ADR 0006, R8),
        owned by the user of the tests instead of root, with its tmpfiles.d file."""
        path.mkdir(parents=True)
        path.chmod(0o3770)
        tmpfiles = self.tmp / 'tmpfiles.d/devloop-repos.conf'
        tmpfiles.parent.mkdir()
        tmpfiles.write_text(render('host/tmpfiles-devloop-repos.conf',
                                   {'REPO_LOCK_DIR': str(path), 'REPO_LOCK_GROUP': GROUP}))

    def mutating_calls(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def tree(self):
        return sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob('*')
                      if not p.is_relative_to(self.bin) and p != self.calls)


class LayoutTests(unittest.TestCase):
    def test_ops_has_the_guide_the_scripts_and_the_declarative_files(self):
        for name in ('README.md', 'params.env.example', 'lib.sh',
                     'host/setup-devloop.sh', 'host/setup-supervisor.sh', 'host/devloop.service',
                     'host/sudoers-supervisor', 'host/config.local.toml.example',
                     'host/setup-agent-user.sh', 'host/sudoers-agent',
                     'host/setup-coordinator.sh', 'host/sudoers-coordinator',
                     'host/check-host.sh', 'host/migrate.md', 'host/start-role.sh',
                     'github/create-apps.sh', 'github/ruleset-main.json', 'github/tokens.md',
                     *(f'github/apps/{role}.json' for role in REQUIRED)):
            self.assertTrue((OPS / name).is_file(), name)

    def test_the_scripts_are_bash(self):
        # The guide runs them with `bash <script>`; the shebang says which shell they need.
        for script in SCRIPTS:
            first = (OPS / script).read_text().splitlines()[0]
            self.assertEqual(first, '#!/usr/bin/env bash', script)

    def test_no_secret_in_ops(self):
        secret = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{20,}'
                            r'|github_pat_[A-Za-z0-9_]{20,}|client_secret|webhook_secret')
        for path in OPS.rglob('*'):
            if path.is_file():
                self.assertNotIn(path.suffix, {'.pem', '.key'}, path)
                self.assertIsNone(secret.search(path.read_text()), path)

    def test_the_local_params_stay_out_of_git(self):
        if not (ROOT / '.git').exists():
            self.skipTest('not a git checkout')
        done = subprocess.run(['git', 'check-ignore', '-q', 'ops/params.env'], cwd=ROOT)
        self.assertEqual(done.returncode, 0)

    @unittest.skipIf(BASH is None, 'bash is not installed')
    def test_the_scripts_pass_bash_n(self):
        for script in ('lib.sh', *SCRIPTS):
            done = subprocess.run([BASH, '-n', str(OPS / script)], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, f'{script}: {done.stderr}')

    def test_the_guide_explains_another_project_and_the_migration(self):
        guide = ' '.join((OPS / 'README.md').read_text().split())
        for phrase in ('## Un altro progetto', '**devloop nel repository gestito**',
                       "**Sorgente dell'installazione a parte** (ADR 0006, R1)", '`SOURCE_REF`',
                       '`scripts/devloop-pinned`', '`core/`', '`review_policy.sensitive_path_lists`',
                       'devloop/prompts/',
                       '## Migrazione su un\'altra macchina', 'host/migrate.md',
                       'Ubuntu 26.04 LTS su aarch64', 'shellcheck', 'tokens.md'):
            self.assertIn(phrase, guide)
        migrate = (OPS / 'host/migrate.md').read_text()
        for phrase in ('ledger-open-turns.json', 'model-unavailability.json', 'activity.json',
                       'config.local.toml', 'turn-journal.jsonl', '~/supervisor',
                       'memory', '--project-config .ssdlc/devloop/project.toml',
                       '--local-config .ssdlc/devloop/config.local.toml pin origin/main',
                       'mai due dispatcher attivi', 'systemctl stop'):
            self.assertIn(phrase, migrate)

    def test_the_guide_completes_the_manifest_flow_before_the_installation(self):
        # The manifest flow ends with the exchange of the code (POST /app-manifests/<code>/
        # conversions, within an hour); its answer holds the private key, which goes to key_dir.
        guide = ' '.join((OPS / 'README.md').read_text().split())
        exchange = guide.index('https://api.github.com/app-manifests/$CODE/conversions')
        self.assertIn('entro un\'ora', guide)
        self.assertIn('umask 077', guide)
        self.assertIn('rm -rf "$work"', guide)
        self.assertLess(exchange, guide.index('*Install App*'))
        self.assertNotIn('non serve, l\'app è già creata', guide)

    def test_the_devloop_guide_links_the_ops_guide(self):
        self.assertIn('(../../ops/README.md)', (ROOT / 'docs/devloop/guide.md').read_text())


class R3MigrationGuideTests(unittest.TestCase):
    """ops/host/migrate.md, "Migrazione di R3 su un host attivo" (#452): the order, the user, the
    complete commands of check-equivalence.py and its exit codes. Every path it cites exists: a
    path of the repository in the repository, a path of the host among those the code uses."""

    HEADING = '## Migrazione di R3 su un host attivo'
    ANCHOR = 'host/migrate.md#migrazione-di-r3-su-un-host-attivo'
    STATE = '/home/devloop/ssdlc/.ssdlc/devloop/'   # devloop's state_dir
    CLONE = '/home/devloop/ssdlc/'                  # devloop's clone: a checkout of this repo
    UPDATED = '/tmp/ssdlc-r3/'                      # the updated checkout
    OPERATOR = '/root/repos/ssdlc'                  # the operator's checkout
    SAVED = 'config.local.toml.prima-r3'            # the overlay of before, made by step 2
    SCRIPT = ROOT / 'ops/host/ssdlc/check-equivalence.py'
    HOST_PATH = re.compile(r'(?<![\w.-])/(?:home|root|tmp|etc|usr|var|run)(?:/[\w.-]+)*')
    REPO_PATH = re.compile(r'`((?:ops|scripts|docs|devloop|tests)/[\w./-]*[\w])`')

    def section(self):
        text = (OPS / 'host/migrate.md').read_text(encoding='utf-8')
        start = text.index(self.HEADING)
        end = text.find('\n## ', start + 1)
        return text[start:] if end < 0 else text[start:end]

    def blocks(self):
        """The shell blocks of the section, each with its continuation lines joined."""
        found = re.findall(r'```bash\n(.*?)```', self.section(), re.S)
        return ['\n'.join(line.strip() for line in re.sub(r'\\\n\s*', '', block).splitlines())
                for block in found]

    def check_command(self):
        commands = [line for block in self.blocks() for line in block.splitlines()
                    if 'check-equivalence.py' in line]
        self.assertEqual(len(commands), 1, commands)
        return shlex.split(commands[0])

    def test_the_ops_guide_links_the_section(self):
        self.assertIn(self.HEADING, (OPS / 'host/migrate.md').read_text(encoding='utf-8'))
        self.assertIn(f'({self.ANCHOR})', (OPS / 'README.md').read_text(encoding='utf-8'))

    def test_the_repository_paths_it_cites_exist(self):
        section = self.section()
        cited = set(self.REPO_PATH.findall(section))
        for link in re.findall(r'\]\(([^)#\s]+)(?:#[^)]*)?\)', section):
            if '://' not in link:
                cited.add(os.path.relpath((OPS / 'host' / link).resolve(), ROOT))
        for path in self.HOST_PATH.findall(section):
            if (path + '/').startswith(self.STATE):
                continue  # the state_dir: the test below
            for checkout in (self.UPDATED, self.CLONE):
                if path.startswith(checkout):
                    cited.add(path[len(checkout):])
        self.assertIn('ops/host/ssdlc/check-equivalence.py', cited)
        self.assertIn('ops/host/ssdlc/project.toml', cited)
        self.assertIn('ops/host/ssdlc/overlay.toml', cited)
        self.assertIn('scripts/devloop-pinned', cited)
        for path in sorted(cited):
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).exists(), path)

    def test_the_host_paths_it_cites_are_those_the_code_uses(self):
        # The state_dir of the installation, as at the top of migrate.md; in it, only the names
        # the launcher and pin read, and the copy of the overlay that step 2 makes.
        self.assertIn('`.ssdlc/devloop`', (OPS / 'host/migrate.md').read_text(encoding='utf-8'))
        launcher = (ROOT / 'scripts/devloop-pinned').read_text(encoding='utf-8')
        names = {'current', 'config.local.toml', 'project.toml'}
        for name in names:
            self.assertIn(f"state / '{name}'", launcher)
        from devloop import pin
        self.assertEqual(pin.POINTER, 'current')
        self.assertEqual(pin.LOCAL_CONFIG, 'config.local.toml')
        names.add(self.SAVED)
        for path in set(self.HOST_PATH.findall(self.section())):
            with self.subTest(path=path):
                if path + '/' == self.STATE:
                    continue
                if path.startswith(self.STATE):
                    self.assertIn(path[len(self.STATE):], names)
                elif path.startswith(self.UPDATED) or path.startswith(self.CLONE):
                    continue  # checkouts of this repository: the test above
                else:  # the checkouts themselves (devloop's clone too) and the operator's home
                    self.assertIn(path, {self.UPDATED.rstrip('/'), self.CLONE.rstrip('/'),
                                         self.OPERATOR, '/root'})
        saved = self.STATE + self.SAVED
        install = [line for block in self.blocks() for line in block.splitlines()
                   if line.startswith('test -e') or line.startswith('install ')]
        self.assertTrue(any(line.rstrip().endswith(saved) for line in install), install)

    def test_the_check_command_is_complete(self):
        argv = self.check_command()
        self.assertEqual(argv[:2], ['python3', self.UPDATED + 'ops/host/ssdlc/check-equivalence.py'])
        flags = dict(zip(argv[2::2], argv[3::2]))
        self.assertEqual(len(argv[2:]), 2 * len(flags))
        self.assertEqual(flags, {
            '--before': self.STATE + 'current',
            '--before-local': self.STATE + self.SAVED,
            '--after': self.UPDATED.rstrip('/'),
            '--after-local': self.STATE + 'config.local.toml',
            '--after-project': self.STATE + 'project.toml',
        })
        source = self.SCRIPT.read_text(encoding='utf-8')
        declared = set(re.findall(r"add_argument\('(--[\w-]+)'", source))
        required = set(re.findall(r"add_argument\('(--[\w-]+)', required=True", source))
        self.assertLessEqual(set(flags), declared)
        self.assertLessEqual(required, set(flags))

    def commands(self):
        return [line for block in self.blocks() for line in block.splitlines()]

    def test_the_clone_is_updated_as_devloop_before_the_pin_and_the_restart(self):
        # #455: the launcher comes from devloop's clone, not from the pinned copy; an old clone
        # does not pass --project-config. Updated as devloop, before pin and restart; the clone
        # is in detached HEAD, so a checkout of origin/main, not a pull (#490).
        commands = self.commands()
        clone = self.CLONE.rstrip('/')
        fetch = f'sudo -u devloop -H git -C {clone} fetch origin'
        merge = f'sudo -u devloop -H git -C {clone} checkout --detach origin/main'
        self.assertIn(fetch, commands)
        self.assertIn(merge, commands)
        pins = [i for i, line in enumerate(commands) if line.endswith(' pin origin/main')]
        self.assertEqual(len(pins), 2, commands)
        self.assertLess(commands.index(fetch), commands.index(merge))
        self.assertLess(commands.index(merge), min(pins))
        section = ' '.join(self.section().split())
        step = section[section.index('6. **Clone di devloop, pin e riavvio.**'):]
        self.assertLess(step.index(merge), step.index('`sudo systemctl restart devloop`'))
        self.assertLess(step.index(merge), step.index('sudo systemctl start devloop'))
        self.assertIn("l'aggiornamento del clone di devloop del passo 6, poi `sudo systemctl "
                      'restart devloop`', section)
        # The launcher is the clone's: it passes --project-config, from the state_dir.
        launcher = (ROOT / 'scripts/devloop-pinned').read_text(encoding='utf-8')
        self.assertIn("ROOT = Path(__file__).resolve().parent.parent", launcher)
        self.assertIn("extra += ['--project-config', str(project)]", launcher)

    def test_the_pin_with_the_new_code_is_complete_and_says_when(self):
        # #455: when the copy in use precedes R3, the pin runs the updated clone's code with the
        # project file and the overlay; devloop-pinned would run the copy in use.
        lines = [line for line in self.commands() if ' -m devloop ' in line]
        self.assertEqual(len(lines), 1, lines)
        cd, command = lines[0].split(' && ')
        self.assertEqual(cd, 'cd ' + self.CLONE.rstrip('/'))
        argv = shlex.split(command)
        self.assertEqual(argv[:6], ['sudo', '-u', 'devloop', '-H', 'python3', '-m'])
        self.assertEqual(argv[6], 'devloop')
        self.assertEqual(argv[-2:], ['pin', 'origin/main'])
        options = argv[7:-2]
        flags = dict(zip(options[::2], options[1::2]))
        self.assertEqual(len(options), 2 * len(flags))
        self.assertEqual(flags, {'--project-config': self.STATE + 'project.toml',
                                 '--local-config': self.STATE + 'config.local.toml'})
        # The options and the subcommand are devloop's.
        source = (ROOT / 'devloop/__main__.py').read_text(encoding='utf-8')
        declared = set(re.findall(r"parser\.add_argument\('(--[\w-]+)'", source))
        self.assertLessEqual(set(flags), declared)
        self.assertIn("sub.add_parser('pin'", source)
        self.assertIn("pin_cmd.add_argument('ref'", source)
        section = ' '.join(self.section().split())
        for phrase in ('**Quando la copia in uso è precedente a R3**',
                       '`devloop-pinned pin` non basta perché il pin lo fa la copia in uso',
                       'Una copia precedente a R2 non passa il file di progetto',
                       "`config: missing 'test_cmd'`", 'I pin successivi tornano al launcher.',
                       'cambia il launcher (`scripts/devloop-pinned`) o ciò che il pin deve '
                       'passare alla copia nuova',
                       'di fare il pin con il codice nuovo, dal clone aggiornato'):
            self.assertIn(phrase, section)
        # R2 taught the pin to pass the project file to the check of the new copy.
        from devloop import pin
        import inspect
        self.assertIn('project', inspect.signature(pin.pin).parameters)
        self.assertIn('project', inspect.signature(pin.verify).parameters)

    def test_it_says_the_order_the_user_and_the_exit_codes(self):
        section = ' '.join(self.section().split())
        for phrase in ('### Ordine', '**prima** che devloop prenda la copia di R3',
                       '**Prima del merge della PR di R3**', '**Se il loop si è già fermato**',
                       'RestartPreventExitStatus=4', 'Tutti **come root**', 'Permission denied',
                       'legge e non scrive', 'sudo systemctl start devloop',
                       '**che cosa fare prima del merge**'):
            self.assertIn(phrase, section)
        for code in ('0', '1', '2'):
            self.assertIn(f'| {code} |', section)
        # The messages it quotes are those of the script and of devloop.
        script = self.SCRIPT.read_text(encoding='utf-8')
        self.assertIn('0 when there is none, 1 when there is\none, 2 when a side does not load',
                      script)
        for message in ('equivalent: ', 'difference(s): the migration is not equivalent',
                        'not an installation of devloop', 'the configuration does not load',
                        'not a file'):
            self.assertIn(message, section)
            self.assertIn(message, script)
        devloop = ''.join(path.read_text(encoding='utf-8')
                          for path in sorted((ROOT / 'devloop').glob('*.py')))
        for quoted, source in (('auto-pin of origin/main failed, the pinned copy is unchanged',
                                'failed, the pinned copy is unchanged: '),
                               ("config: missing 'test_cmd'", "config: missing 'test_cmd'"),
                               ('ALERT: 3 consecutive ticks failed', 'consecutive ticks failed')):
            self.assertIn(quoted, section)
            self.assertIn(source, devloop)


class RootStepsGuideTests(unittest.TestCase):
    """ops/host/migrate.md (#490): the scripts of ops/host run as root from the operator's
    checkout, never from devloop's clone, which devloop can write; devloop's clone, in detached
    HEAD, is updated with a checkout of origin/main, never with a pull. And the section that
    turns on the agent user (#415)."""

    CLONE = '/home/devloop/ssdlc'
    OPERATOR = '/root/repos/ssdlc'
    SECTIONS = ('## Migrazione di R3 su un host attivo',
                '## File delle CLI di root sull\'utente degli agent (#438)',
                '## Attivazione dell\'utente degli agent (#415)')
    ACTIVATION = '## Attivazione dell\'utente degli agent (#415)'

    def text(self):
        return (OPS / 'host/migrate.md').read_text(encoding='utf-8')

    def section(self, heading):
        text = self.text()
        start = text.index(f'\n{heading}\n')
        end = text.find('\n## ', start + 1)
        return text[start:] if end < 0 else text[start:end]

    @staticmethod
    def commands(text, inline=True):
        """The commands of ``text``: the lines of its shell blocks, with their continuation
        lines joined, and its inline code (with ``inline``)."""
        lines = []
        for block in re.findall(r'```bash\n(.*?)```', text, re.S):
            block = re.sub(r'\\\n\s*', '', block)
            lines += [line.strip() for line in block.splitlines() if line.strip()]
        if inline:
            prose = re.sub(r'```.*?```', '', text, flags=re.S)
            lines += [' '.join(code.split())
                      for code in re.findall(r'`([^`\n]+(?:\n[^`\n]+)*)`', prose)]
        return lines

    def test_no_root_command_runs_a_script_of_devloops_clone(self):
        for path in ('host/migrate.md', 'README.md'):
            text = (OPS / path).read_text(encoding='utf-8')
            for command in self.commands(text):
                with self.subTest(path=path, command=command):
                    if command.startswith('sudo -u devloop ') or \
                            command.startswith('sudo -u devloop-agent '):
                        continue  # not as root
                    self.assertNotIn(f'{self.CLONE}/ops/', command)

    def test_no_pull_on_devloops_clone(self):
        text = self.text()
        self.assertNotIn('pull --ff-only', text)
        for command in self.commands(text):
            if self.CLONE in command and ' git ' in f' {command} ':
                with self.subTest(command=command):
                    self.assertNotIn(' pull', command)
                    self.assertNotIn('merge --ff-only', command)

    def test_the_sections_use_the_operators_checkout_and_a_detached_checkout(self):
        detach = f'sudo -u devloop -H git -C {self.CLONE} checkout --detach origin/main'
        fetch = f'sudo -u devloop -H git -C {self.CLONE} fetch origin'
        for heading in self.SECTIONS:
            with self.subTest(section=heading):
                commands = self.commands(self.section(heading))
                self.assertIn(fetch, commands)
                self.assertIn(detach, commands)
                self.assertLess(commands.index(fetch), commands.index(detach))
                # Every script of ops/host runs from the operator's checkout, by its full path.
                scripts = [c for c in commands if re.search(r'\bops/host/[\w-]+\.sh\b', c)]
                for command in scripts:
                    self.assertTrue(command.startswith(f'sudo bash {self.OPERATOR}/ops/host/'),
                                    command)
                if heading != self.SECTIONS[0]:
                    self.assertTrue(scripts, heading)
                    for line in (f'git -C {self.OPERATOR} fetch origin',
                                 f'git -C {self.OPERATOR} checkout --detach origin/main',
                                 f'test -f {self.OPERATOR}/ops/params.env'):
                        self.assertIn(line, commands)
                        # The operator's checkout is updated before its scripts run.
                        self.assertLess(commands.index(line), commands.index(scripts[0]))
        section = ' '.join(self.section(self.SECTIONS[1]).split())
        self.assertIn('**come root dal checkout dell\'operatore**', section)
        self.assertIn('manca `ops/params.env`', section)

    def test_the_cli_probe_uses_the_shell_tool_and_the_guide_says_what_to_do(self):
        # #494: a question without commands does not start the shell tool of agy, which
        # stopped with permission denied on a directory it creates at its first command.
        section = self.section(self.SECTIONS[1])
        commands = self.commands(section, inline=False)
        script = (OPS / 'host/check-host.sh').read_text(encoding='utf-8')
        prompt = re.search(r"^SHELL_PROMPT='(.*)'$", script, re.M).group(1)
        self.assertIn(f"P='{prompt}'", commands)
        self.assertIn('git --version', prompt)
        agy = [c for c in commands if c.startswith('sudo -u devloop-agent -H '
                                                   '/home/devloop-agent/.local/bin/agy -p "$P"')]
        self.assertEqual(len(agy), 1, commands)
        self.assertIn('"command(git --version)"', agy[0])
        for cli in ('claude', 'codex'):
            self.assertTrue([c for c in commands if c.startswith(
                f'sudo -u devloop-agent -H /home/devloop-agent/.local/bin/{cli} ') and '"$P"' in c],
                cli)
        self.assertNotIn('claude -p ciao', section)
        # #517: what to do when a directory of the CLIs holds an entry devloop does not admit.
        start = section.index('\n### Una voce sconosciuta in una directory delle CLI\n')
        case = section[start:]
        flat = ' '.join(case.split())
        steps = ['`sudo systemctl stop devloop`', 'come `devloop-agent`',
                 'rimuoverla come `devloop-agent`', '`CLI_ALLOWED` di `devloop/agentuser.py`',
                 '`AGENT_CLI_ALLOWED` di `ops/lib.sh`', '`CLI_CLEARED`',
                 f'`sudo bash {self.OPERATOR}/ops/host/check-host.sh --no-apps`']
        at = -1
        for step in steps:
            with self.subTest(step=step):
                self.assertIn(step, flat[at + 1:])
                at = flat.index(step, at + 1)
        self.assertIn('con una PR', flat)
        self.assertIn('(#una-voce-sconosciuta-in-una-directory-delle-cli)', section)
        self.assertIn('(host/migrate.md#una-voce-sconosciuta-in-una-directory-delle-cli)',
                      (OPS / 'README.md').read_text(encoding='utf-8'))
        self.assertNotIn('permission-denied', (OPS / 'host/migrate.md').read_text(
            encoding='utf-8'))

    def test_the_guide_says_how_to_put_agy_back_among_the_reviewers(self):
        # #525: the operator took agy out of the reviewers while cli.log, a link, was refused;
        # after the pin of the fix it goes back, with the loop stopped, after check-host.sh.
        section = self.section(self.SECTIONS[1])
        start = section.index('\n### Il link `cli.log` di agy (#525)\n')
        flat = ' '.join(section[start:].split())
        for phrase in ('`cli.log -> log/cli-<YYYYMMDD_HHMMSS>.log`', '`CLI_LINKS` di',
                       '`AGENT_CLI_LINKS` di `ops/lib.sh`', 'con `..`',
                       '`[roles] reviewers = ["codex-reviewer", "claude-reviewer"]`'):
            self.assertIn(phrase, flat)
        steps = ['**dopo il pin**', '`sudo systemctl stop devloop`',
                 'devloop-pinned status`',
                 f'`sudo bash {self.OPERATOR}/ops/host/check-host.sh --no-apps`',
                 'rimettere `agy-reviewer` in `[roles] reviewers`',
                 '/home/devloop/ssdlc/.ssdlc/devloop/config.local.toml',
                 '`sudo systemctl start devloop`']
        at = -1
        for step in steps:
            with self.subTest(step=step):
                self.assertIn(step, flat[at + 1:])
                at = flat.index(step, at + 1)

    def test_the_activation_has_every_step_and_the_rollback(self):
        text = self.section(self.ACTIVATION)
        commands = self.commands(text)
        setup = f'sudo bash {self.OPERATOR}/ops/host/setup-agent-user.sh'
        overlay = ('sudo -u devloop -H "${EDITOR:-vi}" '
                   '/home/devloop/ssdlc/.ssdlc/devloop/config.local.toml')
        steps = [
            'sudo systemctl stop devloop',
            f'{setup} --unseal',
            'sudo -iu devloop-agent /home/devloop-agent/.local/bin/claude',
            'sudo -iu devloop-agent /home/devloop-agent/.local/bin/codex',
            'sudo -iu devloop-agent /home/devloop-agent/.local/bin/agy',
            setup,
            overlay,
            f'sudo bash {self.OPERATOR}/ops/host/check-host.sh --no-apps',
            'sudo systemctl start devloop',
        ]
        at = -1
        for step in steps:
            with self.subTest(step=step):
                self.assertIn(step, commands[at + 1:])
                at = commands.index(step, at + 1)
        # The versions: devloop's before the installation, devloop-agent's after, the same.
        versions = [c for c in commands if c.endswith('--version')]
        self.assertEqual(len(versions), 2, versions)
        self.assertIn('sudo -u devloop -H env PATH=/home/devloop/.local/bin:', versions[0])
        self.assertIn('sudo -u devloop-agent -H /home/devloop-agent/.local/bin/$cli', versions[1])
        self.assertLess(commands.index(versions[0]), commands.index(f'{setup} --unseal'))
        self.assertLess(commands.index(f'{setup} --unseal'), commands.index(versions[1]))
        self.assertLess(commands.index(versions[1]), commands.index(steps[2]))
        # The default PATH of the unit is the one the guide uses for devloop's CLIs.
        lib = (OPS / 'lib.sh').read_text(encoding='utf-8')
        self.assertIn(': "${DEVLOOP_PATH:=$DEVLOOP_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin}"',
                      lib)
        self.assertIn('PATH=/home/devloop/.local/bin:/usr/local/bin:/usr/bin:/bin', versions[0])
        # The installation is the ops guide's, with the same method and versions.
        flat = ' '.join(text.split())
        self.assertIn('(../README.md#5-utente-degli-agent-e-cli)', flat)
        self.assertIn('**con lo stesso metodo e le stesse versioni**', flat)
        self.assertIn('### 5. Utente degli agent e CLI', (OPS / 'README.md').read_text())
        # The key in the overlay, the check and the rollback.
        self.assertIn('```toml [agent_user] name = "devloop-agent" ```', flat)
        self.assertIn('**Rollback.**', text)
        rollback = self.commands(text[text.index('**Rollback.**'):], inline=False)
        self.assertEqual(rollback, ['sudo systemctl stop devloop', overlay,
                                    'sudo systemctl start devloop'])
        script = (OPS / 'host/check-host.sh').read_text(encoding='utf-8')
        self.assertIn('--no-apps', script)
        self.assertIn('non imposta [agent_user] name', script)
        # The ops guide links the section.
        self.assertIn('(host/migrate.md#attivazione-dellutente-degli-agent-415)',
                      (OPS / 'README.md').read_text(encoding='utf-8'))


class CoordinatorGuideTests(unittest.TestCase):
    """ops/README.md and ops/host/migrate.md describe the installation of the coordinator (#530):
    the script run as root from the operator's checkout, the reading commands of its sudoers with
    the values of the installation, check-host.sh --read-only and the rollback."""

    OPERATOR = '/root/repos/ssdlc'
    VALUES = {'DEVLOOP_USER': 'devloop', 'COORDINATOR_USER': 'coordinator',
              'LAUNCHER_CMD': '/usr/bin/python3 /home/devloop/ssdlc/scripts/devloop-pinned',
              'STATE_PATH': '/home/devloop/ssdlc/.ssdlc/devloop', 'SERVICE_NAME': 'devloop',
              'COORDINATOR_OPS_DIR': '/usr/local/lib/devloop-coordinator'}

    def sudoers(self):
        """The commands of sudoers-coordinator with the values of the installation."""
        text = render('host/sudoers-coordinator', self.VALUES).replace('\\\n', ' ')
        return [c.strip() for line in text.splitlines() if 'NOPASSWD:' in line
                for c in line.split('NOPASSWD:', 1)[1].split(',')]

    def test_the_ops_guide_describes_the_installation(self):
        text = (OPS / 'README.md').read_text(encoding='utf-8')
        start = text.index('\n### 10. Coordinatore (facoltativo)\n')
        section = text[start:text.index('\n## ', start)]
        commands = RootStepsGuideTests.commands(section, inline=False)
        self.assertEqual(commands[:2], ['sudo bash ops/host/setup-coordinator.sh --dry-run',
                                        'sudo bash ops/host/setup-coordinator.sh'])
        # Each example of the coordinator is a command of its sudoers.
        allowed = self.sudoers()
        for command in commands[2:]:
            with self.subTest(command=command):
                argv = command.split()
                self.assertEqual(argv[0], 'sudo')
                if argv[1] == '-u':
                    self.assertEqual(argv[2], 'devloop')
                    argv = argv[3:]
                else:
                    argv = argv[1:]
                self.assertIn(' '.join(argv), allowed)
        self.assertIn('sudo /usr/bin/bash /usr/local/lib/devloop-coordinator/host/check-host.sh '
                      '--read-only', commands)
        flat = ' '.join(section.split())
        for phrase in ('**solo lettura**', '`ops/host/sudoers-coordinator`', '`visudo -c`',
                       '**copia di root**', '**Dopo ogni aggiornamento di `ops/`**',
                       'senza caratteri jolly', 'nessun modello, nessuna quota',
                       'senza `devloop apps` e senza scrivere nulla',
                       '(host/migrate.md#utente-del-coordinatore-530)',
                       '(../docs/adr/0002-ruoli-e-responsabilita.md#precisazione-del-2026-11-22-'
                       'modello-a-quattro-ruoli-524-527)'):
            self.assertIn(phrase, flat)
        self.assertIn('### Precisazione del 2026-11-22: modello a quattro ruoli (#524, #527)',
                      (ROOT / 'docs/adr/0002-ruoli-e-responsabilita.md').read_text())
        flat = ' '.join(text.split())
        self.assertIn('**solo l\'esito, con il codice di uscita**', flat)
        self.assertNotIn("con la riga del `permission denied` se c'è", flat)

    def test_the_migration_guide_describes_the_installation_and_the_rollback(self):
        text = RootStepsGuideTests().section('## Utente del coordinatore (#530)')
        commands = RootStepsGuideTests.commands(text, inline=False)
        setup = f'sudo bash {self.OPERATOR}/ops/host/setup-coordinator.sh'
        steps = [f'git -C {self.OPERATOR} fetch origin',
                 f'git -C {self.OPERATOR} checkout --detach origin/main',
                 f'test -f {self.OPERATOR}/ops/params.env', f'{setup} --dry-run', setup,
                 'sudo -u coordinator -H sudo -n -l',
                 'sudo -u coordinator -H sudo -n /usr/bin/bash '
                 '/usr/local/lib/devloop-coordinator/host/check-host.sh --read-only',
                 'rm /etc/sudoers.d/devloop-coordinator',
                 'rm -r /usr/local/lib/devloop-coordinator', 'userdel -r coordinator']
        self.assertEqual(commands, steps)
        # The check as the coordinator is the one its sudoers allows.
        self.assertIn(steps[6].split(' -n ', 1)[1], self.sudoers())
        flat = ' '.join(text.split())
        for phrase in ('**dal checkout dell\'operatore**', 'mai dal clone di devloop',
                       f'`sudo bash {self.OPERATOR}/ops/host/check-host.sh --no-apps`',
                       '**come `coordinator`**', '**Dopo ogni aggiornamento di `ops/`**',
                       'nessun pin lo aspetta', '**Rollback.**',
                       '(../README.md#10-coordinatore-facoltativo)'):
            self.assertIn(phrase, flat)
        migrate = ' '.join((OPS / 'host/migrate.md').read_text(encoding='utf-8').split())
        self.assertIn('`setup-supervisor.sh` e, se c\'è il coordinatore, `setup-coordinator.sh`',
                      migrate)
        self.assertIn('[Utente del coordinatore](#utente-del-coordinatore-530)', migrate)


class ManifestTests(unittest.TestCase):
    """The manifests of ops/github/apps cover what devloop/apps.py asks for each role."""

    def test_each_manifest_covers_the_permissions_devloop_asks_for_its_role(self):
        for role, needs in REQUIRED.items():
            granted = manifest(role)['default_permissions']
            for need in needs:
                for name, level in need.items():
                    self.assertIn(name, granted, f'{role}: {name}')
                    self.assertGreaterEqual(LEVEL[granted[name]], LEVEL[level], f'{role}: {name}')

    def test_no_workflows_to_the_implementer_and_no_contents_to_the_supervisor(self):
        self.assertNotIn('workflows', manifest('implementer')['default_permissions'])
        self.assertNotIn('contents', manifest('supervisor')['default_permissions'])
        for role in REQUIRED:  # no app pushes workflows or administers the repository
            granted = manifest(role)['default_permissions']
            self.assertNotIn('workflows', granted, role)
            self.assertNotIn('administration', granted, role)

    def test_the_merger_only_merges(self):
        self.assertEqual(manifest('merger')['default_permissions'],
                         {'metadata': 'read', **apps.MERGE})

    def test_every_app_has_metadata_no_webhook_no_events_and_no_oauth(self):
        for role in REQUIRED:
            data = manifest(role)
            self.assertEqual(data['name'], role)
            self.assertEqual(data['default_permissions'].get('metadata'), 'read', role)
            self.assertFalse(data['hook_attributes']['active'], role)
            self.assertEqual(data['default_events'], [], role)
            self.assertFalse(data['public'], role)
            self.assertFalse(data['request_oauth_on_install'], role)
            self.assertNotIn('callback_urls', data, role)
            self.assertTrue(set(data['default_permissions'].values()) <= set(LEVEL), role)

    def test_the_configured_apps_have_a_manifest_and_the_prefix_of_params(self):
        params = (OPS / 'params.env.example').read_text()
        self.assertIn(f'APP_PREFIX={PREFIX}\n', params)
        # ssdlc's apps are in its overlay since ADR 0006, R6.
        cfg = load_config(ROOT / 'devloop/config.toml', ssdlc_project.OVERLAY,
                          ssdlc_project.PROJECT)
        self.assertTrue(cfg.apps)
        for actor, app in cfg.apps.items():
            self.assertTrue(app.slug.startswith(PREFIX), actor)
            role = app.slug[len(PREFIX):]
            self.assertIn(role, REQUIRED, actor)
            needs = {apps.DISPATCHER: REQUIRED['dispatcher'],
                     apps.MERGER: REQUIRED['merger'],
                     apps.SUPERVISOR: REQUIRED['supervisor']}.get(actor, AGENT)
            granted = manifest(role)['default_permissions']
            for need in needs:
                for name, level in need.items():
                    self.assertGreaterEqual(LEVEL.get(granted.get(name), 0), LEVEL[level],
                                            f'{actor}: {name}')

    def test_the_ruleset_keeps_the_required_checks_of_the_ci(self):
        ruleset = json.loads((OPS / 'github/ruleset-main.json').read_text())
        rules = {rule['type']: rule.get('parameters', {}) for rule in ruleset['rules']}
        self.assertIn('deletion', rules)
        self.assertIn('non_fast_forward', rules)
        self.assertEqual(sorted(rules['pull_request']['allowed_merge_methods']),
                         ['merge', 'rebase', 'squash'])
        checks = rules['required_status_checks']['required_status_checks']
        workflow = (ROOT / '.github/workflows/test.yml').read_text()
        for check in checks:
            self.assertEqual(check['integration_id'], 15368)
            self.assertIn(f'name: {check["context"]}\n', workflow)
        # The isolated recovery gate of the SSDLC engine left with it (ADR 0007, R1, #538).
        self.assertEqual({c['context'] for c in checks}, {'Unit Tests (Python 3.12)'})
        self.assertEqual(ruleset['bypass_actors'],
                         [{'actor_id': 5, 'actor_type': 'RepositoryRole', 'bypass_mode': 'always'}])
        guide = (OPS / 'README.md').read_text()
        self.assertIn('"dismiss_stale_reviews_on_push": true', guide)
        self.assertIn('"require_last_push_approval": true', guide)


class DeclarativeFileTests(unittest.TestCase):
    def test_the_unit_is_that_of_the_current_installation(self):
        unit = (OPS / 'host/devloop.service').read_text()
        for line in ('User=@DEVLOOP_USER@', 'WorkingDirectory=@DEVLOOP_CLONE@',
                     'Environment=PYTHONUNBUFFERED=1', 'Environment=PATH=@DEVLOOP_PATH@',
                     'ExecStart=@LAUNCHER_CMD@ run',
                     'StandardOutput=append:@STATE_PATH@/run.log',
                     'StandardError=append:@STATE_PATH@/run.log',
                     'KillMode=mixed', 'TimeoutStopSec=4200', 'Restart=on-failure',
                     'RestartSec=60'):
            self.assertIn(line + '\n', unit)

    @staticmethod
    def directives(*names):
        """``{(section, key): value}`` of the unit files ``names`` in order, as systemd merges a
        drop-in over its unit: a later value of a key replaces the earlier one."""
        merged = {}
        for name in names:
            section = None
            for line in (OPS / 'host' / name).read_text().splitlines():
                if not line.strip() or line.startswith('#'):
                    continue
                if line.startswith('['):
                    section = line
                    continue
                key, value = line.split('=', 1)
                if key == 'Environment':
                    key, value = f'Environment {value.split("=", 1)[0]}', value
                merged[section, key] = value
        return merged

    def test_the_template_with_the_project_drop_in_is_the_unit(self):
        # ADR 0006, R8: devloop@<project> runs as devloop.service does, with the parameters of
        # its project in the drop-in.
        self.assertEqual(self.directives('devloop@.service', 'devloop-project.conf'),
                         self.directives('devloop.service'))
        template = self.directives('devloop@.service')
        self.assertEqual(template['[Unit]', 'Description'], 'devloop continuous loop (%i)')
        # Without the drop-in an instance has no command, user or directory: it cannot start.
        for key in ('ExecStart', 'User', 'WorkingDirectory'):
            self.assertNotIn(('[Service]', key), template)
        self.assertNotRegex((OPS / 'host/devloop@.service').read_text(), r'@[A-Z_]+@')

    def test_the_directory_of_the_repository_locks(self):
        text = (OPS / 'host/tmpfiles-devloop-repos.conf').read_text()
        body = [line for line in text.splitlines() if line.strip() and not line.startswith('#')]
        # Root's, of the group of the devloop users only, setgid and sticky.
        self.assertEqual(body, ['d @REPO_LOCK_DIR@ 3770 root @REPO_LOCK_GROUP@ -'])

    def test_the_sudoers_allows_only_the_listed_commands(self):
        text = (OPS / 'host/sudoers-supervisor').read_text()
        body = [line for line in text.replace('\\\n', ' ').splitlines()
                if line.strip() and not line.startswith('#')]
        self.assertEqual(body[0], 'Defaults>@DEVLOOP_USER@ always_set_home, '
                                  'secure_path="@DEVLOOP_PATH@"')
        as_devloop = [c.strip() for c in body[1].split('NOPASSWD:', 1)[1].split(',')]
        launcher = '@LAUNCHER_CMD@ '
        self.assertTrue(body[1].startswith('@SUPERVISOR_USER@ ALL=(@DEVLOOP_USER@) NOPASSWD:'))
        self.assertEqual(as_devloop, [launcher + args for args in (
            'status', 'status *', 'watch *', 'report *', 'tick --dry-run', 'pin *')])
        as_root = [c.strip() for c in body[2].split('NOPASSWD:', 1)[1].split(',')]
        self.assertTrue(body[2].startswith('@SUPERVISOR_USER@ ALL=(root) NOPASSWD:'))
        self.assertEqual(as_root, ['/usr/bin/systemctl start @SERVICE_NAME@',
                                   '/usr/bin/systemctl stop @SERVICE_NAME@',
                                   '/usr/bin/systemctl restart @SERVICE_NAME@',
                                   '/usr/bin/systemctl status @SERVICE_NAME@ *'])
        self.assertEqual(len(body), 3)
        self.assertNotIn('config.local.toml', ' '.join(body))

    def test_the_coordinator_sudoers_allows_only_reading_commands(self):
        # #530 (ADR 0002, precisazione del 2026-11-22): journalctl and systemctl status of the
        # unit, run.log, devloop-pinned status and check-host.sh --read-only; nothing else.
        text = (OPS / 'host/sudoers-coordinator').read_text()
        body = [line for line in text.replace('\\\n', ' ').splitlines()
                if line.strip() and not line.startswith('#')]
        self.assertEqual(len(body), 3, body)
        self.assertEqual(body[0], 'Defaults>@DEVLOOP_USER@ always_set_home, '
                                  'secure_path="@DEVLOOP_PATH@"')
        self.assertTrue(body[1].startswith('@COORDINATOR_USER@ ALL=(@DEVLOOP_USER@) NOPASSWD: '))
        as_devloop = [c.strip() for c in body[1].split('NOPASSWD:', 1)[1].split(',')]
        self.assertEqual(as_devloop, ['@LAUNCHER_CMD@ status',
                                      '/usr/bin/tail -n 200 @STATE_PATH@/run.log',
                                      '/usr/bin/tail -n 1000 @STATE_PATH@/run.log'])
        self.assertTrue(body[2].startswith('@COORDINATOR_USER@ ALL=(root) NOPASSWD: '))
        as_root = [c.strip() for c in body[2].split('NOPASSWD:', 1)[1].split(',')]
        self.assertEqual(as_root, [
            '/usr/bin/journalctl --no-pager -n 200 -u @SERVICE_NAME@',
            '/usr/bin/journalctl --no-pager -n 1000 -u @SERVICE_NAME@',
            '/usr/bin/systemctl status --no-pager @SERVICE_NAME@',
            '/usr/bin/bash @COORDINATOR_OPS_DIR@/host/check-host.sh --read-only'])
        # Each command with its exact arguments: no wildcard, no environment, no other target.
        rules = ' '.join(body[1:])
        for forbidden in ('*', '?', '[', 'SETENV', 'ALL=(ALL', 'sudoedit', 'config.local.toml'):
            self.assertNotIn(forbidden, rules)
        # Only reading programs and verbs: what the coordinator runs never changes the host,
        # starts the loop or a CLI, opens a pager or reaches devloop's clone as root.
        reading = {'@LAUNCHER_CMD@': ['status'], '/usr/bin/tail': ['-n'],
                   '/usr/bin/journalctl': ['--no-pager'], '/usr/bin/systemctl': ['status'],
                   '/usr/bin/bash': ['@COORDINATOR_OPS_DIR@/host/check-host.sh']}
        for command in as_devloop + as_root:
            with self.subTest(command=command):
                program, verb = command.split()[:2]
                self.assertEqual(reading[program], [verb])
                for word in ('start', 'stop', 'restart', 'pin', 'tick', 'run', 'watch', 'apps',
                             'token', '--vacuum-size', '--rotate', '--flush', '-f', '-F'):
                    self.assertNotIn(word, command.split())
                if program in ('/usr/bin/journalctl', '/usr/bin/systemctl'):
                    self.assertIn('--no-pager', command.split())
        self.assertTrue(as_root[-1].endswith(' --read-only'))
        self.assertNotIn('@DEVLOOP_CLONE@', rules)

    def test_the_agent_sudoers_allows_only_the_clis_bash_and_kill(self):
        # #415: devloop starts as the agent user only the CLIs, the test command and the kill of
        # a stopped group, with the environment it builds (SETENV) and in its own process group.
        text = (OPS / 'host/sudoers-agent').read_text()
        body = [line for line in text.replace('\\\n', ' ').splitlines()
                if line.strip() and not line.startswith('#')]
        self.assertEqual(body[0], 'Defaults>@AGENT_USER@ !use_pty, secure_path="@AGENT_PATH@"')
        self.assertTrue(body[1].startswith('@DEVLOOP_USER@ ALL=(@AGENT_USER@) NOPASSWD:SETENV:'))
        allowed = [c.strip() for c in body[1].split('SETENV:', 1)[1].split(',')]
        self.assertEqual(allowed, ['@AGENT_HOME@/.local/bin/claude',
                                   '@AGENT_HOME@/.local/bin/codex',
                                   '@AGENT_HOME@/.local/bin/agy', agentuser.BASH,
                                   agentuser.KILL])
        self.assertEqual(len(body), 2)

    def test_the_example_overlay_is_valid_for_devloop(self):
        example = (OPS / 'host/config.local.toml.example').read_text()
        # Every commented key and table, uncommented, is accepted by devloop's config.
        active = '\n'.join(line[2:] for line in example.splitlines()
                           if re.match(r'# (\[[a-z_.]+\]|[a-z_]+ = )', line))
        self.assertIn('[merge]', active)
        self.assertEqual(tomllib.loads(example), {})
        with tempfile.TemporaryDirectory() as tmp:
            overlay = Path(tmp) / 'config.local.toml'
            overlay.write_text(active + '\n')
            # With ssdlc's project file, as on its host (ADR 0006, R3).
            cfg = load_config(ROOT / 'devloop/config.toml', overlay, ssdlc_project.PROJECT)
        self.assertEqual(cfg.repo, 'brushtyler/ssdlc')


class DryRunTests(OpsTestCase):
    def test_setup_devloop_dry_run_prints_the_actions_and_changes_nothing(self):
        params = self.params(DEVLOOP_USER='opsdry-nouser')
        before = self.tree()
        done = self.run_script('host/setup-devloop.sh', '--dry-run', '--params', str(params))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        out = done.stdout
        self.assertIn('piattaforma verificata', out)
        self.assertIn(f'[dry-run] useradd --create-home --home-dir {self.home}/devloop', out)
        self.assertIn('[dry-run] sudo -u opsdry-nouser -H -- git clone '
                      f'https://github.com/brushtyler/ssdlc.git {self.home}/devloop/ssdlc', out)
        self.assertIn(f'[dry-run] install -d -m 700 -o opsdry-nouser -g opsdry-nouser '
                      f'{self.home}/devloop/.ghapps', out)
        self.assertIn(f'scriverebbe {self.tmp}/systemd/devloop.service', out)
        self.assertIn('    | User=opsdry-nouser\n', out)
        self.assertIn(f'    | StandardOutput=append:{self.home}/devloop/ssdlc/.ssdlc/devloop/run.log',
                      out)
        # Without SOURCE_REPO and with the default state_dir, the launcher of the clone without
        # parameters, as before ADR 0006, R1.
        self.assertIn(f'    | ExecStart=/usr/bin/python3 {self.home}/devloop/ssdlc/scripts/'
                      'devloop-pinned run\n', out)
        self.assertNotIn('env -C', out)
        self.assertNotIn('scrivere state_dir', out)
        self.assertNotIn("Sorgente dell'installazione", out)
        self.assertIn('    |   "autoUpdates": false,', out)
        # The CLIs are the agent user's (#415).
        self.assertIn("le CLI e i loro login sono dell'utente devloop-agent (#415): "
                      'setup-agent-user.sh', out)
        self.assertNotIn('installare claude', out)
        self.assertNotRegex(out, r'@[A-Z_]+@')
        self.assertEqual(self.mutating_calls(), [])
        self.assertEqual(self.tree(), before)

    def test_setup_devloop_dry_run_does_nothing_already_done(self):
        # The state of an installation already in place: a rerun has nothing left to do.
        devloop = self.home / 'devloop'
        clone = devloop / 'ssdlc'
        state = clone / '.ssdlc/devloop'
        (clone / '.git').mkdir(parents=True)
        state.mkdir(parents=True)
        (state / 'run.log').touch()
        locks = self.tmp / 'locks/repos'
        # The repository is the overlay's since ADR 0006, R6.
        (state / 'config.local.toml').write_text('repo = "brushtyler/ssdlc"\n'
                                                 f'repo_lock_dir = "{locks}"\n'
                                                 '[merge]\nmode = "off"\n')
        self.lock_dir(locks)
        for name, mode in (('.ghapps', 0o700), ('.claude', 0o700), ('.codex', 0o700),
                           ('.local', 0o755), ('.local/bin', 0o755)):
            (devloop / name).mkdir(exist_ok=True)
            (devloop / name).chmod(mode)
        (devloop / '.claude/settings.json').write_text(
            '{"autoUpdates": false, "env": {"DISABLE_AUTOUPDATER": "1"}}')
        for cli in ('claude', 'codex', 'agy'):
            (devloop / '.local/bin' / cli).write_text('#!/bin/sh\n')
            (devloop / '.local/bin' / cli).chmod(0o755)
        for role in DEVLOOP_KEYS:
            key = devloop / '.ghapps' / f'{PREFIX}{role}.2026-11-18.private-key.pem'
            key.write_text('not a key\n')
            key.chmod(0o600)
        (self.tmp / 'systemd').mkdir()
        (self.tmp / 'systemd/devloop.service').write_text(render('host/devloop.service', {
            'REPO': 'brushtyler/ssdlc', 'DEVLOOP_USER': ME, 'DEVLOOP_CLONE': str(clone),
            'LAUNCHER_CMD': f'/usr/bin/python3 {clone}/scripts/devloop-pinned',
            'STATE_PATH': f'{clone}/.ssdlc/devloop',
            'DEVLOOP_PATH': f'{devloop}/.local/bin:/usr/local/bin:/usr/bin:/bin'}))
        # #497: the host is simulated (fake_host): the user of the tests is devloop's, the agent
        # user and the supervisor are other users, and sudo is sudo-rs or the classic one.
        params = self.params(DEVLOOP_USER=ME, AGENT_USER=FAKE_AGENT,
                             SUPERVISOR_USER=FAKE_SUPERVISOR)
        for sudo in SUDO_FLAVORS:
            self.fake_host(sudo, users=(FAKE_AGENT, FAKE_SUPERVISOR))
            for _ in range(2):
                with self.subTest(sudo):
                    done = self.run_script('host/setup-devloop.sh', '--dry-run', '--params',
                                           str(params))
                    self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                    actions = [line for line in done.stdout.splitlines()
                               if '[dry-run]' in line and 'apt-get' not in line]
                    self.assertEqual(actions, [])
                    self.assertNotIn('da fare a mano', done.stdout)
                    self.assertIn(f"ok: l'utente {ME} esiste", done.stdout)
                    self.assertIn(f'ok: {ME} è nel gruppo {GROUP}', done.stdout)
                    self.assertIn('devloop.service è già aggiornato', done.stdout)
                    self.assertIn(f"dell'utente {FAKE_AGENT} (#415)", done.stdout)
        self.assertEqual(self.mutating_calls(), [])

    def test_setup_supervisor_dry_run_prints_the_acl_and_the_sudoers(self):
        (self.home / 'devloop').mkdir(parents=True)
        # #497: the host is simulated (fake_host): the user of the tests is devloop's, the agent
        # user exists, the supervisor does not yet, no ACL on the home of devloop, and sudo is
        # sudo-rs or the classic one.
        params = self.params(DEVLOOP_USER=ME, SUPERVISOR_USER='opsdry-nosup',
                             AGENT_USER=FAKE_AGENT)
        before = self.tree()
        for sudo in SUDO_FLAVORS:
            with self.subTest(sudo):
                self.fake_host(sudo, users=(FAKE_AGENT,))
                self.check_setup_supervisor(params, before)

    def check_setup_supervisor(self, params, before):
        done = self.run_script('host/setup-supervisor.sh', '--dry-run', '--params', str(params))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        out = done.stdout
        self.assertIn(f"ok: l'utente {ME} e {self.home}/devloop esistono", out)
        self.assertIn(f'[dry-run] useradd --create-home --home-dir {self.home}/supervisor '
                      '--shell /bin/bash opsdry-nosup', out)
        self.assertIn(f'[dry-run] setfacl -m u:opsdry-nosup:--x {self.home}/devloop', out)
        self.assertIn(f'scriverebbe {self.tmp}/sudoers.d/devloop-supervisor (mode 440', out)
        self.assertIn(f'    | Defaults>{ME} always_set_home, secure_path="{self.home}/devloop/'
                      '.local/bin:/usr/local/bin:/usr/bin:/bin"', out)
        self.assertIn(f'    | opsdry-nosup ALL=({ME}) NOPASSWD:', out)
        self.assertIn(f'/usr/bin/python3 {self.home}/devloop/ssdlc/scripts/devloop-pinned '
                      'tick --dry-run', out)
        self.assertIn('/usr/bin/systemctl restart devloop', out)
        self.assertIn(f'install -d -m 700 -o opsdry-nosup -g opsdry-nosup '
                      f'{self.home}/supervisor/.ghapps', out)
        self.assertIn('token fine-grained di sola lettura', out)
        self.assertNotRegex(out, r'@[A-Z_]+@')
        self.assertEqual(self.mutating_calls(), [])
        self.assertEqual(self.tree(), before)

    def test_setup_coordinator_dry_run_prints_the_user_the_copy_and_the_sudoers(self):
        # #530: the coordinator's user, the root copy of check-host.sh with the files it reads
        # and the parameters, and the sudoers with reading commands only.
        (self.home / 'devloop').mkdir(parents=True)
        params = self.params(DEVLOOP_USER=ME, SUPERVISOR_USER=FAKE_SUPERVISOR,
                             AGENT_USER=FAKE_AGENT, COORDINATOR_OPS_DIR=ROOT_COPY)
        copy = ROOT_COPY
        before = self.tree()
        for sudo in SUDO_FLAVORS:
            with self.subTest(sudo):
                self.fake_host(sudo, users=(FAKE_AGENT, FAKE_SUPERVISOR))
                done = self.run_script('host/setup-coordinator.sh', '--dry-run', '--params',
                                       str(params))
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                out = done.stdout
                self.assertIn(f'[dry-run] useradd --create-home --home-dir {self.home}/coordinator '
                              f'--shell /bin/bash {FAKE_COORDINATOR}\n', out)
                self.assertIn(f'[dry-run] install -d -m 755 -o root -g root {copy}\n', out)
                self.assertIn(f'[dry-run] install -d -m 755 -o root -g root {copy}/host\n', out)
                lib = (OPS / 'lib.sh').read_text()
                files = re.search(r"^OPS_COORDINATOR_FILES='(.*)'$", lib, re.M).group(1).split()
                for name in files:
                    self.assertIn(f'[dry-run] install -m 644 -o root -g root {OPS}/{name} '
                                  f'{copy}/{name}\n', out)
                self.assertIn(f'[dry-run] install -m 644 -o root -g root {params} '
                              f'{copy}/params.env\n', out)
                self.assertIn(f'scriverebbe {self.tmp}/sudoers.d/devloop-coordinator (mode 440, '
                              'proprietario root)', out)
                launcher = f'/usr/bin/python3 {self.home}/devloop/ssdlc/scripts/devloop-pinned'
                state = f'{self.home}/devloop/ssdlc/.ssdlc/devloop'
                for line in (f'{FAKE_COORDINATOR} ALL=({ME}) NOPASSWD: \\',
                             f'    {launcher} status, \\',
                             f'    /usr/bin/tail -n 200 {state}/run.log, \\',
                             f'{FAKE_COORDINATOR} ALL=(root) NOPASSWD: \\',
                             '    /usr/bin/systemctl status --no-pager devloop, \\',
                             f'    /usr/bin/bash {copy}/host/check-host.sh --read-only'):
                    self.assertIn(f'    | {line}\n', out)
                self.assertIn('token fine-grained di sola lettura', out)
                self.assertNotRegex(out, r'@[A-Z_]+@')
        self.assertEqual(self.mutating_calls(), [])
        self.assertEqual(self.tree(), before)

    def test_the_coordinator_of_a_project_has_its_sudoers_and_its_copy(self):
        params = self.project_params(COORDINATOR_SUDOERS_FILE='', COORDINATOR_OPS_DIR='')
        done = self.run_script('host/setup-coordinator.sh', '--dry-run', '--params', str(params))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        out = done.stdout
        self.assertIn('scriverebbe /etc/sudoers.d/devloop-coordinator-widget (mode 440', out)
        self.assertIn('install -d -m 755 -o root -g root /usr/local/lib/devloop-coordinator-widget\n',
                      out)
        self.assertIn('    /usr/bin/journalctl --no-pager -n 200 -u devloop@widget, \\\n', out)
        self.assertIn('    /usr/bin/bash /usr/local/lib/devloop-coordinator-widget/host/check-host.sh'
                      ' --read-only\n', out)
        self.assertEqual(self.mutating_calls(), [])

    def test_the_coordinator_has_a_user_of_its_own_and_a_copy_out_of_reach(self):
        # #530: its user has no other role's privileges, and what it runs as root lies out of
        # the homes and of state_dir.
        for overrides, message in (
                ({'COORDINATOR_USER': 'supervisor'},
                 'COORDINATOR_USER deve essere diverso da SUPERVISOR_USER'),
                ({'COORDINATOR_USER': 'devloop'},
                 'COORDINATOR_USER deve essere diverso da DEVLOOP_USER'),
                ({'COORDINATOR_USER': 'devloop-agent'},
                 'COORDINATOR_USER deve essere diverso da AGENT_USER'),
                ({'COORDINATOR_OPS_DIR': str(self.home / 'devloop/ops')},
                 'COORDINATOR_OPS_DIR non può stare dentro né contenere'),
                ({'COORDINATOR_OPS_DIR': str(self.home / 'coordinator/ops')},
                 'COORDINATOR_OPS_DIR non può stare dentro né contenere'),
                ({'COORDINATOR_OPS_DIR': '/opt/../tmp/x'}, 'COORDINATOR_OPS_DIR deve essere'),
                ({'COORDINATOR_SUDOERS_FILE': str(self.tmp / 'sudoers.d/coordinator.conf')},
                 "il nome di COORDINATOR_SUDOERS_FILE non può contenere '.'")):
            with self.subTest(overrides=overrides):
                for script in ('host/setup-coordinator.sh', 'host/check-host.sh'):
                    done = self.run_script(script, '--dry-run', '--params',
                                           str(self.params(**overrides)))
                    self.assertNotEqual(done.returncode, 0, script)
                    self.assertIn(message, done.stderr)
        self.assertEqual(self.mutating_calls(), [])

    def test_setup_coordinator_refuses_a_copy_another_user_can_replace(self):
        # #530, R1-1: under a directory others can write (or not root's, or a link), its user
        # would rename the copy and put its own script there, which its sudoers runs as root.
        # setup-coordinator.sh stops before the user, the copy and the sudoers.
        shared = self.tmp / 'shared'
        shared.mkdir()
        shared.chmod(0o777)
        (shared / 'ops').mkdir()
        (shared / 'ops').chmod(0o555)
        self.addCleanup((shared / 'ops').chmod, 0o755)
        (self.tmp / 'link').symlink_to('/usr/local/lib')
        (self.tmp / 'copy-link').symlink_to(ROOT_COPY)
        for path, message in (
                (shared / 'ops', f'{shared} è 777 {ME}: COORDINATOR_OPS_DIR deve stare sotto '
                                 'directory di root non scrivibili dal gruppo né dagli altri; '
                                 'niente installato'),
                # Not root's, even if no other can write it.
                (self.tmp / 'lib/devloop-coordinator', f'{self.tmp} è 700 {ME}: '),
                (self.tmp / 'link/devloop-coordinator', f'{self.tmp}/link è un link'),
                (self.tmp / 'copy-link', f'{self.tmp}/copy-link è un link')):
            with self.subTest(path=path):
                params = self.params(COORDINATOR_OPS_DIR=str(path))
                done = self.run_script('host/setup-coordinator.sh', '--dry-run', '--params',
                                       str(params))
                self.assertNotEqual(done.returncode, 0)
                self.assertIn(message, done.stderr)
                self.assertNotIn('useradd', done.stdout)
                self.assertNotIn('install', done.stdout)
                self.assertNotIn('sudoers del coordinatore', done.stdout)
                self.assertNotIn('check-host.sh --read-only', done.stdout)
        self.assertEqual(self.mutating_calls(), [])

    def test_the_sudoers_of_the_coordinator_gives_no_role_on_the_host(self):
        # #530: one coordinator per host, like the supervisor: its sudoers, of this project or of
        # another, does not make its devloop user another installation's.
        sudoers = self.tmp / 'sudoers.d'
        sudoers.mkdir()
        for name, user in (('devloop-coordinator', 'opsdry-nouser'),
                           ('devloop-coordinator-gadget', 'devloop-gadget')):
            (sudoers / name).write_text(f'Defaults>{user} always_set_home\n\n'
                                        f'coordinator ALL=({user}) NOPASSWD: \\\n'
                                        '    /usr/bin/true\n')
        params = self.params(DEVLOOP_USER='opsdry-nouser', COORDINATOR_USER='coordinator',
                             COORDINATOR_OPS_DIR=ROOT_COPY)
        for script in ('host/setup-devloop.sh', 'host/setup-coordinator.sh'):
            with self.subTest(script=script):
                done = self.run_script(script, '--dry-run', '--params', str(params))
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        # A sudoers of a supervisor with the same rule still gives the role (R1-1).
        (sudoers / 'devloop-supervisor-gadget').write_text(
            'coordinator ALL=(devloop-gadget) NOPASSWD: \\\n    /usr/bin/true\n')
        done = self.run_script('host/setup-coordinator.sh', '--dry-run', '--params', str(params))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("COORDINATOR_USER 'coordinator' è già l'utente supervisor di un'altra "
                      'installazione', done.stderr)
        self.assertEqual(self.mutating_calls(), [])

    def test_setup_agent_user_dry_run_prints_the_user_the_acls_and_the_sudoers(self):
        # #415: the agent user traverses the home of devloop and state_dir, writes only in the
        # worktrees and in the directories of its turns and tests, never in .git, and has no
        # access to any other entry of state_dir.
        devloop = self.home / 'devloop'
        state = devloop / 'ssdlc/.ssdlc/devloop'
        (state / 'logs').mkdir(parents=True)
        (state / 'turn-journal.jsonl').write_text('')
        (state / 'config.local.toml').write_text('[merge]\nmode = "off"\n')
        git_worktrees = devloop / 'ssdlc/.git/worktrees'
        git_worktrees.mkdir(parents=True)
        # The ACL an earlier version gave the agent user on .git/worktrees, nothing elsewhere.
        self.shim('getfacl', f'for a in "$@"; do last=$a; done\n'
                             f'if [ "$last" = "{git_worktrees}" ]; then '
                             f'echo "default:user:opsdry-noagent:rwx"; fi')
        params = self.params(DEVLOOP_USER=ME, AGENT_USER='opsdry-noagent')
        before = self.tree()
        done = self.run_script('host/setup-agent-user.sh', '--dry-run', '--params', str(params))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        out = done.stdout
        agent = self.home / 'agent'
        self.assertIn(f'[dry-run] useradd --create-home --home-dir {agent} --shell /bin/bash '
                      'opsdry-noagent', out)
        # #438: the home root's and sticky, ~/.local root's; #517: the directories of the CLIs
        # root's, of the agent user's group and sticky, as the home; the sessions the agent
        # user's.
        self.assertIn(f'[dry-run] install -d -m 1770 -o root -g opsdry-noagent {agent}\n', out)
        for name in agentuser.CLI_DIRS:
            self.assertIn(f'[dry-run] install -d -m 1770 -o root -g opsdry-noagent '
                          f'{agent}/{name}\n', out)
        for name in ('.local', '.local/bin'):
            self.assertIn(f'[dry-run] install -d -m 755 -o root -g root {agent}/{name}\n', out)
        self.assertNotIn(f'-m 755 -o root -g root {agent}/.claude\n', out)
        # #517: each sealed entry that does not exist is a placeholder of root: a file when
        # its name has a suffix ({} for a JSON file), a directory otherwise.
        for name in agentuser.CLI_SEALED[:-1]:  # .local: created above
            if name.endswith('.json'):
                line = f'install -m 644 -o root -g opsdry-noagent {OPS}/host/placeholder.json'
            elif agentuser.placeholder_is_file(name):
                line = 'install -m 644 -o root -g opsdry-noagent /dev/null'
            else:
                line = 'install -d -m 755 -o root -g opsdry-noagent'
            self.assertIn(f'[dry-run] {line} {agent}/{name}\n', out)
        self.assertEqual(json.loads((OPS / 'host/placeholder.json').read_text()), {})
        self.assertIn(f'[dry-run] install -d -m 750 -o opsdry-noagent -g opsdry-noagent '
                      f'{agent}/.claude/projects', out)
        self.assertIn(f'scriverebbe {agent}/.claude/settings.json (mode 644, proprietario root)',
                      out)
        self.assertNotIn('Directory delle CLI aperte', out)
        self.assertIn(f'[dry-run] setfacl -m u:opsdry-noagent:--x {devloop}\n', out)
        # `run` prints each argument as printf %q does: a comma is escaped.
        self.assertIn(f'[dry-run] setfacl -m u:{ME}:r-x\\,d:u:{ME}:r-x\\,d:u:opsdry-noagent:rwx '
                      f'{agent}/.claude/projects', out)
        self.assertIn(f'[dry-run] setfacl -m u:opsdry-noagent:--x\\,d:u:opsdry-noagent:--- '
                      f'{state}\n', out)
        # The agent user enters the directories that hold the worktrees and the directories of
        # the turns and of the tests without writing them (1/codex/R1-2), and writes in what
        # is created there; the shared directory and the schemas are read-only.
        container = f'u:opsdry-noagent:r-x\\,d:u:opsdry-noagent:rwx\\,d:u:{ME}:rwx'
        shared = state / 'agent-shared'
        for path in (state / 'worktrees', shared / 'turns', shared / 'tmp'):
            self.assertIn(f'[dry-run] setfacl -m {container} {path}\n', out)
        for path in (shared, shared / 'schemas'):
            self.assertIn(f'[dry-run] setfacl -m u:opsdry-noagent:r-x\\,d:u:opsdry-noagent:r-x '
                          f'{path}\n', out)
        self.assertIn(f'[dry-run] setfacl -m u:opsdry-noagent:rwx\\,d:u:opsdry-noagent:rwx'
                      f'\\,d:u:{ME}:rwx {shared}/gh-config\n', out)
        # Write access to an existing directory of devloop: the empty gh config only.
        self.assertEqual(re.findall(r'setfacl -m u:opsdry-noagent:rwx\S* (\S+)', out),
                         [f'{shared}/gh-config'])
        # The git metadata: the ACL of an earlier version is removed (1/codex/R1-1).
        self.assertIn(f'[dry-run] setfacl -R -b {git_worktrees}\n', out)
        for name in ('logs', 'turn-journal.jsonl', 'config.local.toml'):
            self.assertIn(f'[dry-run] setfacl -m u:opsdry-noagent:--- {state / name}\n', out)
        self.assertNotIn(f'u:opsdry-noagent:--- {state / "worktrees"}', out)
        self.assertIn(f'scriverebbe {self.tmp}/sudoers.d/devloop-agent (mode 440', out)
        self.assertIn(f'    | {ME} ALL=(opsdry-noagent) NOPASSWD:SETENV: \\', out)
        self.assertIn(f'    |     {agent}/.local/bin/claude, \\', out)
        self.assertIn('da fare a mano: con --unseal, installare claude come opsdry-noagent', out)
        self.assertIn('da fare a mano: impostare [agent_user] name = "opsdry-noagent"', out)
        self.assertNotRegex(out, r'@[A-Z_]+@')
        self.assertEqual(self.mutating_calls(), [])
        self.assertEqual(self.tree(), before)

    def test_setup_agent_user_makes_the_files_of_the_clis_roots(self):
        # #438: what carries instructions, configuration or binaries becomes root's and not
        # writable; the rest (credentials, sessions, caches, logs) stays the agent user's.
        agent = self.home / 'agent'
        for name in ('.claude/CLAUDE.md', '.claude/.credentials.json', '.codex/config.toml',
                     '.local/bin/claude'):
            (agent / name).parent.mkdir(parents=True, exist_ok=True)
            (agent / name).write_text('x\n')
        (agent / '.claude/skills').mkdir()
        params = self.params(DEVLOOP_USER=ME, AGENT_USER='opsdry-noagent')
        done = self.run_script('host/setup-agent-user.sh', '--dry-run', '--params', str(params))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        out = done.stdout
        # Root's, of the agent user's group, which reads them but cannot write them.
        for name in ('.claude/CLAUDE.md', '.claude/skills', '.codex/config.toml', '.local'):
            self.assertIn(f'[dry-run] chown -hR root:opsdry-noagent {agent}/{name}\n', out)
            self.assertIn(f'[dry-run] chmod -R g+rX\\,go-w {agent}/{name}\n', out)
        # #517: what does not exist becomes a placeholder of root, which the agent user cannot
        # create, rename nor remove in the sticky directories of the CLIs.
        self.assertIn(f'[dry-run] install -m 644 -o root -g opsdry-noagent /dev/null '
                      f'{agent}/.codex/AGENTS.md\n', out)
        self.assertIn(f'[dry-run] install -d -m 755 -o root -g opsdry-noagent '
                      f'{agent}/.claude/rules\n', out)
        self.assertIn(f'[dry-run] install -d -m 755 -o root -g opsdry-noagent '
                      f'{agent}/.codex/plugins\n', out)
        self.assertNotIn(f'install -m 644 -o root -g opsdry-noagent /dev/null '
                         f'{agent}/.claude/CLAUDE.md', out)  # it exists: sealed as it is
        self.assertNotIn('.credentials.json', out)
        self.assertNotIn(' porta a ', out)
        # --unseal gives the directories of the CLIs back, and says devloop will not start.
        done = self.run_script('host/setup-agent-user.sh', '--dry-run', '--unseal', '--params',
                               str(params))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        out = done.stdout
        for name in ('.claude', '.codex', '.local'):
            self.assertIn(f'[dry-run] chown -hR opsdry-noagent:opsdry-noagent {agent}/{name}\n',
                          out)
        self.assertNotIn('chown -hR root:root', out)
        self.assertIn('devloop non parte finché non si riesegue lo script senza --unseal', out)
        self.assertEqual(self.mutating_calls(), [])

    def test_setup_agent_user_seals_the_configuration_of_agy(self):
        # #517: ~/.gemini/config, 700 of the agent user on the host, holds the plugins agy
        # enables and the MCP servers it loads: it becomes a directory of the CLIs, root's and
        # sticky, with config.json, mcp_config.json and projects root's; .migrated stays.
        agent = self.home / 'agent'
        config_dir = agent / '.gemini/config'
        (config_dir / 'projects').mkdir(parents=True)
        config_dir.chmod(0o700)
        (config_dir / 'config.json').write_text('{"plugins": {"swarm": {"enabled": true}}}\n')
        (config_dir / 'mcp_config.json').write_text('{}\n')
        (config_dir / 'projects/default-cli-project.json').write_text('{}\n')
        (config_dir / '.migrated').write_text('')
        params = self.params(DEVLOOP_USER=ME, AGENT_USER='opsdry-noagent')
        done = self.run_script('host/setup-agent-user.sh', '--dry-run', '--params', str(params))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        out = done.stdout
        self.assertIn(f'[dry-run] chown root:opsdry-noagent {config_dir}\n', out)
        self.assertIn(f'[dry-run] chmod 1770 {config_dir}\n', out)
        for name in ('config.json', 'mcp_config.json', 'projects'):
            self.assertIn(f'[dry-run] chown -hR root:opsdry-noagent {config_dir}/{name}\n', out)
            self.assertIn(f'[dry-run] chmod -R g+rX\\,go-w {config_dir}/{name}\n', out)
        self.assertNotIn(f'{config_dir}/.migrated', out)
        self.assertNotIn(f'{config_dir}/config.json non è nella lista', out)
        # Missing, each one is a placeholder of root.
        for path in (config_dir / 'config.json', config_dir / 'mcp_config.json',
                     config_dir / 'projects/default-cli-project.json'):
            path.unlink()
        (config_dir / 'projects').rmdir()
        done = self.run_script('host/setup-agent-user.sh', '--dry-run', '--params', str(params))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        for name in ('config.json', 'mcp_config.json'):
            self.assertIn(f'[dry-run] install -m 644 -o root -g opsdry-noagent '
                          f'{OPS}/host/placeholder.json {config_dir}/{name}\n', done.stdout)
        self.assertIn(f'[dry-run] install -d -m 755 -o root -g opsdry-noagent '
                      f'{config_dir}/projects\n', done.stdout)
        self.assertEqual(self.mutating_calls(), [])

    def test_setup_agent_user_turns_the_memories_of_codex_off(self):
        # Request of the operator on PR #523: the sealed ~/.codex/config.toml sets
        # [features] memories = false in so many words; the script adds the line that is
        # missing, and changes none that is there.
        agent = self.home / 'agent'
        config = agent / '.codex/config.toml'
        params = self.params(DEVLOOP_USER=ME, AGENT_USER='opsdry-noagent')

        def written(before):
            if before is None:
                config.unlink(missing_ok=True)
            else:
                config.parent.mkdir(parents=True, exist_ok=True)
                config.write_text(before)
            done = self.run_script('host/setup-agent-user.sh', '--dry-run', '--params',
                                   str(params))
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            out = done.stdout
            head = f'  [dry-run] scriverebbe {config} (mode '
            if head not in out:
                return out, None
            lines = out.split(head, 1)[1].splitlines()[1:]
            content = ''.join(line[6:] + '\n' for line in
                              itertools.takewhile(lambda l: l.startswith('    | '), lines))
            self.assertEqual(config.read_text() if before is not None else None, before)
            return out, content
        # Missing: the placeholder of root gets the line.
        out, content = written(None)
        self.assertIn(f'[dry-run] install -m 644 -o root -g opsdry-noagent /dev/null {config}\n',
                      out)
        self.assertIn(f'scriverebbe {config} (mode 644, proprietario root):', out)
        self.assertEqual(content, '[features]\nmemories = false\n')
        # Without [features]: a new table at the end.
        out, content = written('model = "gpt-5"\n')
        self.assertEqual(content, 'model = "gpt-5"\n\n[features]\nmemories = false\n')
        # With [features]: the line in it.
        out, content = written('model = "gpt-5"\n[features] # on\nshell_tool = true\n'
                               '[tui]\nx = 1\n')
        self.assertEqual(content, 'model = "gpt-5"\n[features] # on\nmemories = false\n'
                                  'shell_tool = true\n[tui]\nx = 1\n')
        # Already there: nothing to write.
        out, content = written('[features]\nmemories = false\n')
        self.assertIsNone(content)
        self.assertIn(f'ok: {config} imposta [features] memories = false', out)
        # Another value, or a table the line cannot be added to: reported, never changed.
        for before, why in (('[features]\nmemories = true\n', 'imposta [features] memories = true'),
                            ('features = { shell_tool = true }\n',
                             'non imposta [features] memories = false, e la riga non si '
                             'aggiunge da sola')):
            with self.subTest(before=before):
                out, content = written(before)
                self.assertIsNone(content)
                self.assertIn(f'da fare a mano: {config} {why}', out)
        # A link: what it leads to is set by hand.
        config.unlink()
        config.symlink_to(self.tmp / 'elsewhere.toml')
        done = self.run_script('host/setup-agent-user.sh', '--dry-run', '--params', str(params))
        self.assertIn(f'da fare a mano: {config} è un link: impostare a mano [features] '
                      'memories = false', done.stdout)
        self.assertEqual(self.mutating_calls(), [])

    def test_setup_agent_user_keeps_the_files_of_the_clis_readable(self):
        # #438: a version before wrote settings.json 600, and a CLI may keep a directory 700:
        # once root's, the agent user still reads them, through its group, without writing.
        agent = self.home / 'agent'
        rules = agent / '.claude/rules'
        rules.mkdir(parents=True)
        rules.chmod(0o700)
        (rules / 'style.md').write_text('x\n')
        (rules / 'style.md').chmod(0o600)
        (agent / '.claude/settings.json').write_text('{}\n')
        (agent / '.claude/settings.json').chmod(0o600)
        params = self.params(DEVLOOP_USER=ME, AGENT_USER='opsdry-noagent')
        done = self.run_script('host/setup-agent-user.sh', '--dry-run', '--params', str(params))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        for path in (rules, agent / '.claude/settings.json'):
            self.assertIn(f'[dry-run] chown -hR root:opsdry-noagent {path}\n', done.stdout)
            self.assertIn(f'[dry-run] chmod -R g+rX\\,go-w {path}\n', done.stdout)
        self.assertEqual(self.mutating_calls(), [])
        # The modes the same chmod gives: readable and enterable by the group, never writable.
        subprocess.run(['chmod', '-R', 'g+rX,go-w', str(rules), str(agent / '.claude/settings.json')],
                       check=True)
        self.assertEqual(stat.S_IMODE(rules.stat().st_mode), 0o750)
        self.assertEqual(stat.S_IMODE((rules / 'style.md').stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE((agent / '.claude/settings.json').stat().st_mode), 0o640)

    def test_setup_agent_user_reports_the_links_that_lead_out_of_the_sealed_files(self):
        # #438: chown -h changes a link, not what it leads to; the startup check follows it and
        # refuses a target the agent user can change: setup says to make it root's.
        agent = self.home / 'agent'
        elsewhere = self.tmp / 'elsewhere'
        elsewhere.mkdir()
        (elsewhere / 'config.toml').write_text('x\n')
        (agent / '.codex').mkdir(parents=True)
        (agent / '.codex/config.toml').symlink_to(elsewhere / 'config.toml')
        versions = agent / '.local/share/claude/versions'
        versions.mkdir(parents=True)
        (versions / '2.0.0').write_text('#!/bin/sh\n')
        (versions / '2.0.0').chmod(0o755)
        (agent / '.local/bin').mkdir()
        (agent / '.local/bin/claude').symlink_to(versions / '2.0.0')  # inside ~/.local: fine
        (agent / '.local/bin/agy').symlink_to(elsewhere / 'agy')
        params = self.params(DEVLOOP_USER=ME, AGENT_USER='opsdry-noagent')
        done = self.run_script('host/setup-agent-user.sh', '--dry-run', '--params', str(params))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        out = done.stdout
        self.assertIn(f'da fare a mano: {agent}/.codex/config.toml porta a '
                      f'{elsewhere}/config.toml: renderlo di root', out)
        self.assertIn(f'da fare a mano: {agent}/.local/bin/agy porta a {elsewhere}/agy', out)
        self.assertNotIn(f'{agent}/.local/bin/claude porta a', out)
        # chmod would follow the link given as an argument: not run on it.
        self.assertIn(f'[dry-run] chown -hR root:opsdry-noagent {agent}/.codex/config.toml\n', out)
        self.assertNotIn(f'chmod -R g+rX\\,go-w {agent}/.codex/config.toml', out)
        self.assertEqual(self.mutating_calls(), [])

    def test_setup_agent_user_creates_what_the_clis_create_on_first_use(self):
        # #494: agy creates ~/.gemini/antigravity-cli/bin at its first shell command, in a
        # directory of root's: the script creates it as the agent user's, 700.
        agent = self.home / 'agent'
        params = self.params(DEVLOOP_USER=ME, AGENT_USER='opsdry-noagent')
        agy_bin = agent / '.gemini/antigravity-cli/bin'
        for unseal in ((), ('--unseal',)):
            with self.subTest(unseal=unseal):
                done = self.run_script('host/setup-agent-user.sh', '--dry-run', *unseal,
                                       '--params', str(params))
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn(f'[dry-run] install -d -m 700 -o opsdry-noagent -g opsdry-noagent '
                              f'{agy_bin}\n', done.stdout)
        # One that exists, of another owner or mode, becomes the agent user's, 700.
        agy_bin.mkdir(parents=True)
        done = self.run_script('host/setup-agent-user.sh', '--dry-run', '--params', str(params))
        self.assertIn(f'[dry-run] chown opsdry-noagent:opsdry-noagent {agy_bin}\n', done.stdout)
        self.assertIn(f'[dry-run] chmod 700 {agy_bin}\n', done.stdout)
        self.assertNotIn(f'root:opsdry-noagent {agy_bin}', done.stdout)  # never sealed
        # A link is never followed by chown: it is reported.
        agy_bin.rmdir()
        agy_bin.symlink_to(self.tmp)
        done = self.run_script('host/setup-agent-user.sh', '--dry-run', '--params', str(params))
        self.assertIn(f'da fare a mano: {agy_bin} è un link: sostituirlo con una directory di '
                      'opsdry-noagent', done.stdout)
        self.assertNotIn(f'chown opsdry-noagent:opsdry-noagent {agy_bin}', done.stdout)
        self.assertEqual(self.mutating_calls(), [])
        self.assertEqual(agentuser.CLI_WRITABLE, ('.gemini/antigravity-cli/bin',))
        self.assertEqual(agentuser.AGY_BIN, agentuser.CLI_WRITABLE[0])

    def test_setup_agent_user_gives_the_group_write_in_the_acl_of_an_earlier_version(self):
        # 1/codex-reviewer/R1-1: an earlier version gave ~/.claude and ~/.codex, then 755, an
        # ACL entry for devloop: chmod 1770 then changes the mask, not group::r-x, and the agent
        # user could not write there. The script sets group::rwx where it is missing.
        agent = self.home / 'agent'
        for name in agentuser.CLI_DIRS:
            (agent / name).mkdir(parents=True, exist_ok=True)
        earlier = (f'{agent}/.claude|{agent}/.codex')
        self.shim('getfacl', f'''for a in "$@"; do last=$a; done
case "$last" in
    {earlier}) printf '%s\\n' user::rwx "user:{ME}:--x" group::r-x mask::r-x other::r-x ;;
    *) printf '%s\\n' user::rwx group::rwx other::--- ;;
esac''')
        params = self.params(DEVLOOP_USER=ME, AGENT_USER='opsdry-noagent')
        done = self.run_script('host/setup-agent-user.sh', '--dry-run', '--params', str(params))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        out = done.stdout
        for name in ('.claude', '.codex'):
            self.assertIn(f'[dry-run] chmod 1770 {agent}/{name}\n', out)
            self.assertIn(f'[dry-run] setfacl -m g::rwx {agent}/{name}\n', out)
        for name in ('.gemini', '.gemini/config', '.gemini/antigravity-cli'):  # group::rwx
            self.assertNotIn(f'setfacl -m g::rwx {agent}/{name}\n', out)
            self.assertIn(f'ACL di {agent}/{name}: group::rwx', out)
        self.assertEqual(self.mutating_calls(), [])

    def test_the_lists_of_the_cli_files_are_those_of_the_startup_check(self):
        lists = subprocess.run(
            [BASH, '-c', f'. {OPS / "lib.sh"}; set +u; '
                         'printf "%s\\n" "${AGENT_CLI_DIRS[*]}" "${AGENT_CLI_SEALED[*]}" '
                         '"${AGENT_CLIS[*]}" "${AGENT_CLI_WRITABLE[*]}" '
                         '"${AGENT_CLI_ALLOWED[*]}" "${AGENT_CLI_LINKS[*]}"'],
            capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertEqual([line.split() for line in lists],
                         [list(agentuser.CLI_DIRS), list(agentuser.CLI_SEALED),
                          list(agentuser.CLIS), list(agentuser.CLI_WRITABLE),
                          [f'{entry}:{types}' for entry, types in agentuser.CLI_ALLOWED],
                          [f'{entry}:{inside}' for entry, inside in agentuser.CLI_LINKS]])

    def test_the_shell_knows_the_entries_of_the_list_as_devloop_does(self):
        # #517: agent_cli_entry_known of ops/lib.sh, for setup-agent-user.sh and check-host.sh,
        # matches the names as cli_state_problems of devloop/agentuser.py.
        cases = [('.claude', '.credentials.json', 'f', 0),
                 ('.claude', '.credentials.json.tmp.4242', 'f', 0),
                 ('.claude', 'CLAUDE.md', 'f', 0), ('.claude', 'skills', 'd', 0),
                 ('.codex', '.auth-Xa12', 'f', 0), ('.codex', 'auth.lock', 'f', 0),
                 ('.codex', 'memories_1.sqlite-shm', 'f', 0), ('.codex', 'plugins', 'd', 0),
                 ('.gemini', 'antigravity-cli', 'd', 0), ('.gemini', 'config', 'd', 0),
                 ('.gemini/antigravity-cli', 'jetbox_summaries_proto.pb.a1.tmp', 'f', 0),
                 ('.gemini/config', 'config.json', 'f', 0),
                 ('.gemini/config', 'mcp_config.json', 'f', 0),
                 ('.gemini/config', 'projects', 'd', 0), ('.gemini/config', '.migrated', 'f', 0),
                 ('.codex', 'hooks.json', 'f', 1), ('.claude', 'agents.bak', 'd', 1),
                 ('.gemini', 'mcp_config.json', 'f', 1),
                 ('.gemini/config', 'settings.json', 'f', 1),
                 ('.gemini/config', '.migrated', 'd', 2), ('.codex', 'auth.json', 'l', 2),
                 ('.claude', 'projects', 'f', 2)]
        for directory, name, kind, expected in cases:
            with self.subTest(name=name):
                done = subprocess.run(
                    [BASH, '-c', f'. {OPS / "lib.sh"}; set +e; agent_cli_entry_known "$@"; '
                                 'echo $?', '_', directory, name, kind],
                    capture_output=True, text=True, check=True)
                self.assertEqual(done.stdout.strip(), str(expected))
                listing = f'D:{directory}\0e:{kind}:0:{name}\0'.encode()
                baseline = agentuser.cli_state_problems('/h', f'D:{directory}\0'.encode())
                problems = agentuser.cli_state_problems('/h', listing)
                self.assertEqual(bool(set(problems) - set(baseline)), expected != 0)

    def test_the_shell_knows_the_cleanup_mark_of_claude_as_devloop_does(self):
        # #525: Claude Code writes ~/.claude/.last-cleanup at each start; a file only.
        for kind, expected in (('f', 0), ('d', 2), ('l', 2)):
            with self.subTest(kind=kind):
                done = subprocess.run(
                    [BASH, '-c', f'. {OPS / "lib.sh"}; set +e; agent_cli_entry_known "$@"; '
                                 'echo $?', '_', '.claude', '.last-cleanup', kind],
                    capture_output=True, text=True, check=True)
                self.assertEqual(done.stdout.strip(), str(expected))
                listing = f'D:.claude\0e:{kind}:0:.last-cleanup\0'.encode()
                baseline = agentuser.cli_state_problems('/h', b'D:.claude\0')
                problems = agentuser.cli_state_problems('/h', listing)
                self.assertEqual(bool(set(problems) - set(baseline)), expected != 0)

    def test_the_shell_knows_the_link_of_agy_as_devloop_does(self):
        # #525: cli.log of agy may be a link, only to a file of log/ next to it.
        agy = '.gemini/antigravity-cli'
        cases = [(agy, 'cli.log', 'l', 'log/cli-20261121_132524.log', 0),
                 (agy, 'cli.log', 'f', '', 0),
                 (agy, 'cli.log', 'l', '/var/log/cli.log', 3),
                 (agy, 'cli.log', 'l', '../antigravity-cli/log/cli-1.log', 3),
                 (agy, 'cli.log', 'l', 'log/../cli-1.log', 3), (agy, 'cli.log', 'l', 'log/..', 3),
                 (agy, 'cli.log', 'l', 'log/', 3), (agy, 'cli.log', 'l', 'log', 3),
                 (agy, 'cli.log', 'l', 'log/sub/cli-1.log', 3),
                 (agy, 'cli.log', 'l', 'brain/cli-1.log', 3),
                 (agy, 'cli.log', 'l', './log/cli-1.log', 3), (agy, 'cli.log', 'l', '', 3),
                 (agy, 'cli.log', 'd', '', 2), (agy, 'log', 'l', 'brain', 2),
                 (agy, 'installation_id', 'l', 'log/cli-1.log', 2),
                 # 1/codex-reviewer/R1-1: into log/, but not to a regular file there: a link
                 # again (out of log/ or not), a directory, nothing.
                 (agy, 'cli.log', 'l', 'log/chained.log', 4),
                 (agy, 'cli.log', 'l', 'log/inside.log', 4),
                 (agy, 'cli.log', 'l', 'log/sub', 4), (agy, 'cli.log', 'l', 'log/missing.log', 4)]
        log = self.home / agy / 'log'
        log.mkdir(parents=True)
        (log / 'cli-20261121_132524.log').write_text('started\n')
        outside = self.tmp / 'outside.log'
        outside.write_text('x\n')
        (log / 'chained.log').symlink_to(outside)
        (log / 'inside.log').symlink_to('cli-20261121_132524.log')
        (log / 'sub').mkdir()
        for directory, name, kind, target, expected in cases:
            with self.subTest(name=name, kind=kind, target=target):
                done = subprocess.run(
                    [BASH, '-c', f'. {OPS / "lib.sh"}; set +e; agent_cli_entry_known "$@"; '
                                 'echo $?', '_', directory, name, kind, target, str(self.home)],
                    capture_output=True, text=True, check=True)
                self.assertEqual(done.stdout.strip(), str(expected))
                entry = f'e:{kind}:0:{name}\0' + (f't:{target}\0' if kind == 'l' else '')
                regular = (f'F:{directory}/{name}\0'
                           if (self.home / directory / target).is_file()
                           and not (self.home / directory / target).is_symlink() else '')
                problems = agentuser.cli_state_problems(
                    '/h', f'{regular}D:{directory}\0{entry}'.encode())
                baseline = agentuser.cli_state_problems('/h', f'D:{directory}\0'.encode())
                self.assertEqual(bool(set(problems) - set(baseline)), expected != 0)
        # Without the home the target cannot be checked: the link is refused.
        done = subprocess.run(
            [BASH, '-c', f'. {OPS / "lib.sh"}; set +e; agent_cli_entry_known "$@"; echo $?',
             '_', agy, 'cli.log', 'l', 'log/cli-20261121_132524.log'],
            capture_output=True, text=True, check=True)
        self.assertEqual(done.stdout.strip(), '4')

    def test_setup_agent_user_reports_the_entries_not_in_the_list(self):
        # #517: devloop refuses every turn while one is there; setup says so, removes nothing.
        agent = self.home / 'agent'
        (agent / '.codex').mkdir(parents=True)
        (agent / '.codex/auth.json').write_text('{}\n')
        (agent / '.codex/hooks.json').write_text('{}\n')
        (agent / '.claude').mkdir()
        (agent / '.claude/projects').write_text('not a directory\n')
        # #525: the log link of agy into log/ is accepted, one out of it reported.
        (agent / '.gemini/antigravity-cli/log').mkdir(parents=True)
        (agent / '.gemini/antigravity-cli/log/cli-20261121_132524.log').write_text('started\n')
        (agent / '.gemini/antigravity-cli/cli.log').symlink_to('log/cli-20261121_132524.log')
        (agent / '.gemini/antigravity-cli/installation_id').symlink_to('/etc/hostname')
        (agent / '.gemini/config').mkdir()
        (agent / '.gemini/config/.migrated').symlink_to('../antigravity-cli/log/x')
        params = self.params(DEVLOOP_USER=ME, AGENT_USER='opsdry-noagent')
        done = self.run_script('host/setup-agent-user.sh', '--dry-run', '--params', str(params))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        out = done.stdout
        self.assertIn(f'da fare a mano: {agent}/.codex/hooks.json non è nella lista '
                      'AGENT_CLI_ALLOWED', out)
        self.assertIn(f'da fare a mano: {agent}/.claude/projects è di tipo f', out)
        self.assertIn(f'da fare a mano: {agent}/.gemini/antigravity-cli/installation_id è di '
                      'tipo l', out)
        self.assertIn(f'da fare a mano: {agent}/.gemini/config/.migrated è di tipo l', out)
        self.assertNotIn('cli.log', out)
        self.assertNotIn('auth.json', out)
        self.assertNotIn('contengono solo voci della lista', out)
        self.assertTrue((agent / '.codex/hooks.json').exists())
        self.assertEqual(self.mutating_calls(), [])
        (agent / '.gemini/antigravity-cli/cli.log').unlink()
        (agent / '.gemini/antigravity-cli/cli.log').symlink_to('/var/log/cli.log')
        done = self.run_script('host/setup-agent-user.sh', '--dry-run', '--params', str(params))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f'da fare a mano: {agent}/.gemini/antigravity-cli/cli.log è un link a '
                      '/var/log/cli.log, fuori dalla directory che AGENT_CLI_LINKS ammette per '
                      'quel nome', done.stdout)
        self.assertTrue((agent / '.gemini/antigravity-cli/cli.log').is_symlink())
        # 1/codex-reviewer/R1-1: into log/, to a link again that leads out of it.
        (agent / '.gemini/antigravity-cli/cli.log').unlink()
        (agent / '.gemini/antigravity-cli/log/cli-1.log').symlink_to('/var/log/cli.log')
        (agent / '.gemini/antigravity-cli/cli.log').symlink_to('log/cli-1.log')
        done = self.run_script('host/setup-agent-user.sh', '--dry-run', '--params', str(params))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f'da fare a mano: {agent}/.gemini/antigravity-cli/cli.log è un link a '
                      'log/cli-1.log, che non è un file regolare (un altro link, che potrebbe '
                      'uscire dalla directory, o nulla)', done.stdout)

    def test_the_agent_user_must_differ_from_devloop(self):
        done = self.run_script('host/setup-agent-user.sh', '--dry-run', '--params',
                               str(self.params(DEVLOOP_USER=ME, AGENT_USER=ME)))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('AGENT_USER deve essere diverso da DEVLOOP_USER', done.stderr)
        self.assertEqual(self.mutating_calls(), [])

    def test_create_apps_dry_run_lists_the_apps_and_writes_nothing(self):
        page = self.tmp / 'page.html'
        done = self.run_script('github/create-apps.sh', '--dry-run', '--params', str(self.params()),
                               '--out', str(page))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f'[dry-run] scriverebbe {page}', done.stdout)
        self.assertIn('brushtyler-devloop-supervisor: issues: write, metadata: read, '
                      'pull_requests: write', done.stderr)
        self.assertFalse(page.exists())
        self.assertEqual(self.mutating_calls(), [])

    def test_create_apps_writes_a_page_with_one_form_per_manifest(self):
        page = self.tmp / 'page.html'
        done = self.run_script('github/create-apps.sh', '--params', str(self.params()),
                               '--out', str(page))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        text = page.read_text()
        forms = re.findall(r'name="manifest" value="([^"]*)"', text)
        self.assertEqual(len(forms), len(REQUIRED))
        names = []
        for value in forms:
            data = json.loads(html.unescape(value))
            names.append(data['name'])
            self.assertEqual(data['url'], 'https://github.com/brushtyler/ssdlc')
            self.assertEqual(data['default_permissions'],
                             manifest(data['name'][len(PREFIX):])['default_permissions'])
        self.assertEqual(sorted(names), sorted(PREFIX + role for role in REQUIRED))
        self.assertIn('action="https://github.com/settings/apps/new"', text)
        self.assertIn('POST /app-manifests/&lt;code&gt;/conversions', text)
        self.assertEqual(self.mutating_calls(), [])

    def test_another_platform_warns_and_goes_on_in_dry_run(self):
        self.release.write_text('ID=debian\nVERSION_ID="13"\n')
        done = self.run_script('host/setup-devloop.sh', '--dry-run', '--params',
                               str(self.params(DEVLOOP_USER='opsdry-nouser')), arch='x86_64')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('piattaforma non verificata: debian 13 su x86_64', done.stdout)
        self.assertEqual(self.mutating_calls(), [])

    def test_without_dry_run_another_platform_asks_for_a_confirmation(self):
        # No terminal and no --yes: the script stops before any action.
        self.release.write_text('ID=debian\nVERSION_ID="13"\n')
        done = self.run_script('host/setup-devloop.sh', '--params',
                               str(self.params(DEVLOOP_USER='opsdry-nouser')), arch='x86_64')
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('rieseguire con --yes', done.stderr)
        self.assertEqual(self.mutating_calls(), [])

    def test_a_value_unfit_for_sudoers_stops_the_script(self):
        done = self.run_script('host/setup-supervisor.sh', '--dry-run', '--params',
                               str(self.params(SUPERVISOR_USER='"evil ALL=(ALL) ALL"')))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('SUPERVISOR_USER non è un nome utente valido', done.stderr)
        self.assertEqual(self.mutating_calls(), [])

    def test_a_state_dir_of_unknown_form_is_refused(self):
        # It ends up in the unit and in the sudoers (ADR 0006, R1): only known forms.
        for value in ('../elsewhere', '/var/lib/../x', 'with space', './state', '/var//x'):
            for script in ('host/setup-devloop.sh', 'host/setup-supervisor.sh',
                           'host/setup-agent-user.sh', 'host/check-host.sh'):
                with self.subTest(value=value, script=script):
                    done = self.run_script(script, '--dry-run', '--params',
                                           str(self.params(STATE_DIR=f"'{value}'")))
                    self.assertNotEqual(done.returncode, 0)
                    self.assertIn('STATE_DIR deve essere un percorso assoluto o relativo',
                                  done.stderr)
        self.assertEqual(self.mutating_calls(), [])

    def test_another_state_dir_reaches_the_launcher_as_a_parameter(self):
        # ADR 0006, R1: the state_dir of the host, absolute or relative to the clone, is passed
        # to the launcher, which then runs in the managed clone.
        clone = f'{self.home}/devloop/ssdlc'
        for value, path in (('/var/lib/devloop/ssdlc', '/var/lib/devloop/ssdlc'),
                            ('custom-state', f'{clone}/custom-state')):
            with self.subTest(value=value):
                params = self.params(DEVLOOP_USER='opsdry-nouser', STATE_DIR=value)
                done = self.run_script('host/setup-devloop.sh', '--dry-run', '--params',
                                       str(params))
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                command = (f'/usr/bin/env -C {clone} /usr/bin/python3 {clone}/scripts/'
                           f'devloop-pinned --state-dir {path}')
                self.assertIn(f'    | ExecStart={command} run\n', done.stdout)
                self.assertIn(f'    | StandardOutput=append:{path}/run.log\n', done.stdout)
                self.assertIn(f'da fare a mano: scrivere state_dir = "{path}" in '
                              f'{path}/config.local.toml', done.stdout)
                self.assertNotIn('scrivere source', done.stdout)
                done = self.run_script('host/setup-supervisor.sh', '--dry-run', '--params',
                                       str(params))
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn(f'    {command} tick --dry-run, \\\n', done.stdout)
        self.assertEqual(self.mutating_calls(), [])

    def source_params(self, **overrides):
        values = dict(REPO='acme/widget', DEVLOOP_USER='opsdry-nouser',
                      SOURCE_REPO='brushtyler/ssdlc', SOURCE_REF='v1.2.0',
                      STATE_DIR=str(self.tmp / 'state' / 'widget'),
                      LAUNCHER_DIR=str(self.tmp / 'lib' / 'devloop'))
        values.update(overrides)
        return self.params(**values)

    def test_a_separate_source_installs_the_launcher_from_its_tag(self):
        # ADR 0006, R1 and section 3.5: the launcher on the host, from the source at the pinned
        # tag, outside the pinned copy and the managed repository.
        params = self.source_params()
        before = self.tree()
        done = self.run_script('host/setup-devloop.sh', '--dry-run', '--params', str(params))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        out = done.stdout
        home, lib, state = self.home / 'devloop', self.tmp / 'lib/devloop', self.tmp / 'state/widget'
        self.assertIn('[dry-run] sudo -u opsdry-nouser -H -- git clone '
                      f'https://github.com/acme/widget.git {home}/widget', out)
        self.assertIn('[dry-run] sudo -u opsdry-nouser -H -- git clone '
                      f'https://github.com/brushtyler/ssdlc.git {home}/devloop-source', out)
        self.assertIn(f'[dry-run] install -d -m 755 -o root -g root {lib}', out)
        self.assertIn(f'[dry-run] installerebbe {lib}/devloop-pinned (mode 755, proprietario '
                      'root): scripts/devloop-pinned del tag v1.2.0 di origin', out)
        self.assertIn(f'[dry-run] install -d -m 755 -o opsdry-nouser -g opsdry-nouser {state}',
                      out)
        command = (f'/usr/bin/env -C {home}/widget /usr/bin/python3 {lib}/devloop-pinned '
                   f'--state-dir {state}')
        self.assertIn(f'    | ExecStart={command} run\n', out)
        self.assertIn(f'    | WorkingDirectory={home}/widget\n', out)
        self.assertIn(f'    | StandardError=append:{state}/run.log\n', out)
        self.assertIn(f'scrivere source = "{home}/devloop-source" in {state}/config.local.toml',
                      out)
        self.assertIn('primo pin del tag v1.2.0', out)
        self.assertNotRegex(out, r'@[A-Z_]+@')
        self.assertEqual(self.mutating_calls(), [])
        self.assertEqual(self.tree(), before)

    def test_a_separate_source_needs_a_tag_and_another_repository(self):
        for overrides, message in (({'SOURCE_REF': ''}, 'SOURCE_REF deve essere il tag'),
                                   ({'SOURCE_REF': 'v1..2'}, 'SOURCE_REF deve essere il tag'),
                                   ({'SOURCE_REPO': 'Acme/Widget'}, 'SOURCE_REPO è il repository '
                                                                    'gestito'),
                                   ({'SOURCE_REPO': 'not a repo'}, 'SOURCE_REPO deve essere')):
            with self.subTest(overrides=overrides):
                done = self.run_script('host/check-host.sh', '--dry-run', '--params',
                                       str(self.source_params(**{
                                           k: f"'{v}'" for k, v in overrides.items()})))
                self.assertNotEqual(done.returncode, 0)
                self.assertIn(message, done.stderr)
        self.assertEqual(self.mutating_calls(), [])

    def test_setup_devloop_prepares_the_directory_of_the_repository_locks(self):
        # ADR 0006, R8: a group of the devloop users, the directory from tmpfiles.d, the overlay.
        params = self.params(DEVLOOP_USER='opsdry-nouser', REPO_LOCK_GROUP='opsdry-nogroup')
        before = self.tree()
        done = self.run_script('host/setup-devloop.sh', '--dry-run', '--params', str(params))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        out = done.stdout
        locks, tmpfiles = self.tmp / 'locks/repos', self.tmp / 'tmpfiles.d/devloop-repos.conf'
        self.assertIn('[dry-run] groupadd --system opsdry-nogroup\n', out)
        self.assertIn('[dry-run] usermod -aG opsdry-nogroup opsdry-nouser\n', out)
        self.assertIn(f'scriverebbe {tmpfiles} (mode 644, proprietario root)', out)
        self.assertIn(f'    | d {locks} 3770 root opsdry-nogroup -\n', out)
        self.assertIn(f'[dry-run] systemd-tmpfiles --create {tmpfiles}\n', out)
        self.assertIn(f'da fare a mano: scrivere repo_lock_dir = "{locks}" in {self.home}/devloop/'
                      'ssdlc/.ssdlc/devloop/config.local.toml', out)
        # ADR 0006, R6: the repository is the overlay's, devloop/config.toml has none.
        self.assertIn(f'da fare a mano: scrivere repo = "brushtyler/ssdlc" in {self.home}/'
                      'devloop/ssdlc/.ssdlc/devloop/config.local.toml', out)
        self.assertEqual(self.mutating_calls(), [])
        self.assertEqual(self.tree(), before)

    def test_a_foreign_directory_is_never_taken_for_the_locks(self):
        locks = self.tmp / 'locks/repos'
        locks.mkdir(parents=True)
        (locks / 'something').write_text('not ours\n')
        done = self.run_script('host/setup-devloop.sh', '--dry-run', '--params',
                               str(self.params(DEVLOOP_USER='opsdry-nouser')))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(f'{locks} esiste già, non è vuota e non è di root:{GROUP}', done.stderr)
        self.assertEqual(self.mutating_calls(), [])

    def test_a_directory_of_the_locks_of_unknown_form_is_refused(self):
        home = f'{self.home}/devloop'
        for value, message in (('/locks', 'REPO_LOCK_DIR deve essere un percorso assoluto'),
                               ('locks/repos', 'REPO_LOCK_DIR deve essere un percorso assoluto'),
                               ('/run/../x', 'REPO_LOCK_DIR deve essere un percorso assoluto'),
                               (f'{home}/locks', f'REPO_LOCK_DIR non può stare dentro né '
                                                 f'contenere {home}'),
                               (str(self.home), 'REPO_LOCK_DIR non può stare dentro né')):
            for script in ('host/setup-devloop.sh', 'host/check-host.sh'):
                with self.subTest(value=value, script=script):
                    done = self.run_script(script, '--dry-run', '--params',
                                           str(self.params(REPO_LOCK_DIR=f"'{value}'")))
                    self.assertNotEqual(done.returncode, 0)
                    self.assertIn(message, done.stderr)
        self.assertEqual(self.mutating_calls(), [])

    def project_params(self, **overrides):
        """A project of a host with several (ADR 0006, R8): the defaults of PROJECT for the
        users, the service and the sudoers."""
        values = dict(PROJECT='widget', REPO='acme/widget', APP_PREFIX='acme-devloop-',
                      DEVLOOP_HOME=str(self.home / 'devloop-widget'),
                      AGENT_HOME=str(self.home / 'devloop-widget-agent'),
                      SUDOERS_FILE='', AGENT_SUDOERS_FILE='')
        values.update(overrides)
        return self.params(**values)

    def test_a_project_installs_the_template_and_the_drop_in_of_its_instance(self):
        params = self.project_params()
        before = self.tree()
        done = self.run_script('host/setup-devloop.sh', '--dry-run', '--params', str(params))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        out = done.stdout
        systemd, home = self.tmp / 'systemd', self.home / 'devloop-widget'
        # A system user per project (D6), with its clone, state_dir and key_dir.
        self.assertIn(f'[dry-run] useradd --create-home --home-dir {home} --shell /bin/bash '
                      'devloop-widget\n', out)
        self.assertIn(f'install -d -m 700 -o devloop-widget -g devloop-widget {home}/.ghapps', out)
        self.assertIn(f'[dry-run] usermod -aG {GROUP} devloop-widget\n', out)
        self.assertIn(f'scriverebbe {systemd}/devloop@.service (mode 644', out)
        self.assertIn('    | Description=devloop continuous loop (%i)\n', out)
        self.assertIn(f'[dry-run] install -d -m 755 -o root -g root '
                      f'{systemd}/devloop@widget.service.d\n', out)
        self.assertIn(f'scriverebbe {systemd}/devloop@widget.service.d/project.conf (mode 644', out)
        self.assertIn('    | Description=devloop continuous loop (acme/widget)\n', out)
        self.assertIn('    | User=devloop-widget\n', out)
        self.assertIn(f'    | WorkingDirectory={home}/widget\n', out)
        self.assertIn(f'    | ExecStart=/usr/bin/python3 {home}/widget/scripts/devloop-pinned run\n',
                      out)
        self.assertIn(f'    | StandardOutput=append:{home}/widget/.ssdlc/devloop/run.log\n', out)
        self.assertNotIn(f'{systemd}/devloop.service', out)
        self.assertNotRegex(out, r'@[A-Z_]+@')
        # The supervisor: one sudoers per unit, the key_dir of the project.
        done = self.run_script('host/setup-supervisor.sh', '--dry-run', '--params', str(params))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        out = done.stdout
        self.assertIn('scriverebbe /etc/sudoers.d/devloop-supervisor-widget (mode 440', out)
        self.assertIn('    | supervisor ALL=(devloop-widget) NOPASSWD:', out)
        self.assertIn('    /usr/bin/systemctl restart devloop@widget, \\\n', out)
        self.assertIn('    /usr/bin/systemctl status devloop@widget *\n', out)
        self.assertIn(f'{self.home}/supervisor/.ghapps-widget', out)
        # The agent user of the project.
        done = self.run_script('host/setup-agent-user.sh', '--dry-run', '--params', str(params))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('scriverebbe /etc/sudoers.d/devloop-agent-widget (mode 440', done.stdout)
        self.assertIn('    | devloop-widget ALL=(devloop-widget-agent) NOPASSWD:SETENV:',
                      done.stdout)
        self.assertEqual(self.mutating_calls(), [])
        self.assertEqual(self.tree(), before)

    def test_a_project_has_a_valid_name_and_the_instance_of_the_template(self):
        for overrides, message in (({'PROJECT': 'Widget'}, 'PROJECT ammette minuscole'),
                                   ({'PROJECT': 'widget-'}, 'PROJECT ammette minuscole'),
                                   ({'PROJECT': 'a' * 17}, 'PROJECT ammette minuscole'),
                                   ({'SERVICE_NAME': 'devloop'}, "l'istanza devloop@widget")):
            with self.subTest(overrides=overrides):
                done = self.run_script('host/check-host.sh', '--dry-run', '--params',
                                       str(self.project_params(**overrides)))
                self.assertNotEqual(done.returncode, 0)
                self.assertIn(message, done.stderr)
        done = self.run_script('host/check-host.sh', '--dry-run', '--params',
                               str(self.params(SERVICE_NAME='devloop@widget')))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("SERVICE_NAME non valido: 'devloop@widget'", done.stderr)
        self.assertEqual(self.mutating_calls(), [])

    def test_the_names_of_a_project_never_take_those_of_another_role(self):
        # R1-1: devloop-<p>-agent is the agent user of project p, devloop-agent that of the
        # installation without PROJECT: no project takes those names for its devloop user.
        for project in ('agent', 'widget-agent'):
            with self.subTest(project=project):
                done = self.run_script('host/check-host.sh', '--dry-run', '--params',
                                       str(self.project_params(PROJECT=project)))
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("PROJECT non può essere 'agent' né finire con '-agent'",
                              done.stderr)
        done = self.run_script('host/setup-agent-user.sh', '--dry-run', '--params',
                               str(self.params(AGENT_USER='supervisor')))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('AGENT_USER deve essere diverso da SUPERVISOR_USER', done.stderr)
        self.assertEqual(self.mutating_calls(), [])

    def other_project(self, unit_user='devloop-gadget', agent_line=None, supervisor_line=None):
        """Another project of the host, gadget, as its unit and its sudoers describe it."""
        dropin = self.tmp / 'systemd/devloop@gadget.service.d/project.conf'
        dropin.parent.mkdir(parents=True, exist_ok=True)
        dropin.write_text('[Unit]\nDescription=devloop continuous loop (acme/gadget)\n\n'
                          f'[Service]\nUser={unit_user}\nGroup={unit_user}\n')
        sudoers = self.tmp / 'sudoers.d'
        sudoers.mkdir(exist_ok=True)
        (sudoers / 'devloop-agent-gadget').write_text(
            'Defaults>devloop-gadget-agent !use_pty\n\n'
            + (agent_line or 'devloop-gadget ALL=(devloop-gadget-agent) NOPASSWD:SETENV: \\')
            + '\n    /usr/bin/bash\n')
        (sudoers / 'devloop-supervisor-gadget').write_text(
            'Defaults>devloop-gadget always_set_home\n\n'
            + (supervisor_line or 'supervisor ALL=(devloop-gadget) NOPASSWD: \\')
            + '\n    /usr/bin/true\n\nsupervisor ALL=(root) NOPASSWD: \\\n'
              '    /usr/bin/systemctl start devloop@gadget\n')

    def widget_params(self):
        return self.project_params(
            SUDOERS_FILE=str(self.tmp / 'sudoers.d/devloop-supervisor-widget'),
            AGENT_SUDOERS_FILE=str(self.tmp / 'sudoers.d/devloop-agent-widget'))

    def test_the_users_of_a_project_are_of_no_other_installation_of_the_host(self):
        # R1-1: the scripts refuse a user that another installation of the host already has,
        # in the same role or in another; the supervisor alone is the same in every project.
        self.other_project()
        for script in ('host/setup-devloop.sh', 'host/setup-supervisor.sh',
                       'host/setup-agent-user.sh'):
            with self.subTest(script=script):
                done = self.run_script(script, '--dry-run', '--params',
                                       str(self.widget_params()))
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        sudoers = self.tmp / 'sudoers.d'
        for kwargs, message in (
                ({'unit_user': 'devloop-widget'},
                 "DEVLOOP_USER 'devloop-widget' è già l'utente devloop di un'altra installazione "
                 f"di devloop dell'host ({self.tmp}/systemd/devloop@gadget.service.d/"
                 "project.conf)"),
                ({'agent_line': 'devloop-gadget ALL=(devloop-widget) NOPASSWD:SETENV: \\'},
                 "DEVLOOP_USER 'devloop-widget' è già l'utente agent di un'altra installazione "
                 f"di devloop dell'host ({sudoers}/devloop-agent-gadget)"),
                ({'agent_line': 'devloop-widget-agent ALL=(devloop-gadget-agent) '
                                'NOPASSWD:SETENV: \\'},
                 "AGENT_USER 'devloop-widget-agent' è già l'utente devloop"),
                ({'supervisor_line': 'devloop-widget-agent ALL=(devloop-gadget) NOPASSWD: \\'},
                 "AGENT_USER 'devloop-widget-agent' è già l'utente supervisor"),
                ({'supervisor_line': 'supervisor ALL=(supervisor) NOPASSWD: \\'},
                 "SUPERVISOR_USER 'supervisor' è già l'utente devloop")):
            with self.subTest(**kwargs):
                self.other_project(**kwargs)
                for script in ('host/setup-devloop.sh', 'host/check-host.sh'):
                    done = self.run_script(script, '--dry-run', '--params',
                                           str(self.widget_params()))
                    self.assertNotEqual(done.returncode, 0)
                    self.assertIn(message, done.stderr)
        self.assertEqual(self.mutating_calls(), [])

    def test_the_supervisor_has_a_clone_per_project(self):
        # R1-4: like its key_dir, the supervisor's clone is the project's.
        clone = self.home / 'supervisor/devloop-widget'
        done = self.run_script('host/setup-supervisor.sh', '--dry-run', '--params',
                               str(self.project_params()))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f'git clone https://github.com/acme/widget.git {clone}\n', done.stdout)
        self.assertIn(f'{self.home}/supervisor/.ghapps-widget', done.stdout)
        # Without PROJECT, the clone of before.
        done = self.run_script('host/setup-supervisor.sh', '--dry-run', '--params',
                               str(self.params()))
        self.assertIn(f'git clone https://github.com/brushtyler/ssdlc.git '
                      f'{self.home}/supervisor/ssdlc\n', done.stdout)
        # A clone already there of another repository is another project's: never shared.
        (clone / '.git').mkdir(parents=True)
        config = clone / '.git/config'
        for url, refused in (('https://github.com/acme/gadget.git', True),
                             ('git@github.com:Acme/Widget.git', False),
                             ('https://github.com/acme/widget', False)):
            with self.subTest(url=url):
                config.write_text('[core]\n\tbare = false\n[remote "origin"]\n'
                                  f'\turl = {url}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n')
                done = self.run_script('host/setup-supervisor.sh', '--dry-run', '--params',
                                       str(self.project_params()))
                if refused:
                    self.assertNotEqual(done.returncode, 0)
                    self.assertIn(f"{clone} è un clone di '{url}', non di acme/widget",
                                  done.stderr)
                else:
                    self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                    self.assertIn(f'ok: {clone} è già un clone di acme/widget', done.stdout)
        self.assertEqual(self.mutating_calls(), [])

    def test_a_missing_params_file_is_explained(self):
        done = self.run_script('host/check-host.sh', '--dry-run', '--params',
                               str(self.tmp / 'missing.env'))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('params.env.example', done.stderr)


@unittest.skipIf(BASH is None, 'bash is not installed')
class PutFileTests(unittest.TestCase):
    """put_file of ops/lib.sh, called the way the scripts call it: in an `if` or with `|| true`,
    where bash does not apply errexit to the function's body."""

    SCRIPT = '''
. "$OPS_LIB"
DRY_RUN=0
if put_file "$1" 644 "$2" contenuto; then echo scritto; else echo aggiornato; fi
put_file "$1" 644 "$2" contenuto || true
echo dopo
'''

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='ops-put-file-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def put_file(self, dest):
        env = dict(os.environ, OPS_LIB=str(OPS / 'lib.sh'), LC_ALL='C.UTF-8')
        return subprocess.run([BASH, '-c', self.SCRIPT, 'put-file-test', str(dest), ME],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_a_new_file_is_written_and_a_rerun_leaves_it(self):
        dest = self.tmp / 'unit.service'
        done = self.put_file(dest)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(dest.read_text(), 'contenuto\n')
        self.assertEqual(oct(dest.stat().st_mode & 0o777), oct(0o644))
        self.assertIn('scritto\n', done.stdout)
        self.assertIn('è già aggiornato', done.stdout)
        self.assertIn('dopo', done.stdout)
        done = self.put_file(dest)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('aggiornato\n', done.stdout)
        self.assertNotIn('scritto\n', done.stdout)

    def test_a_failed_install_stops_the_script(self):
        # The parent directory is missing: install fails, and the script stops right there
        # instead of reporting the file as written.
        dest = self.tmp / 'missing' / 'sudoers.d' / 'devloop-supervisor'
        done = self.put_file(dest)
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn(f'install non riesce a scrivere {dest}', done.stderr)
        self.assertNotIn('scritto', done.stdout)
        self.assertNotIn('dopo', done.stdout)
        self.assertFalse(dest.exists())

    def test_an_unknown_owner_stops_the_script(self):
        dest = self.tmp / 'unit.service'
        env = dict(os.environ, OPS_LIB=str(OPS / 'lib.sh'), LC_ALL='C.UTF-8')
        done = subprocess.run([BASH, '-c', self.SCRIPT, 'put-file-test', str(dest),
                               'opsdry-nouser'], capture_output=True, text=True, env=env,
                              timeout=60)
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn("l'utente opsdry-nouser non esiste", done.stderr)
        self.assertNotIn('dopo', done.stdout)
        self.assertFalse(dest.exists())


class CheckHostTests(OpsTestCase):
    """check-host.sh on a temporary installation: key permissions, ACLs and the service."""

    def setUp(self):
        super().setUp()
        self.devloop = self.home / 'devloop'
        self.supervisor = self.home / 'supervisor'
        for home, dirs in ((self.devloop, ('.ghapps', '.claude', '.codex')),
                           (self.supervisor, ('.ghapps', '.claude'))):
            for name in dirs:
                (home / name).mkdir(parents=True)
                (home / name).chmod(0o700)
            (home / '.local/bin').mkdir(parents=True)
        for home, roles in ((self.devloop, DEVLOOP_KEYS), (self.supervisor, SUPERVISOR_KEYS)):
            for role in roles:
                self.key(home, role)
        self.agent = self.home / 'agent'
        (self.agent / '.local/bin').mkdir(parents=True)
        for cli in ('claude', 'codex', 'agy'):
            self.exe(self.agent / '.local/bin' / cli)
        for name in agentuser.CLI_DIRS:  # root's and sticky on the host (#517)
            (self.agent / name).mkdir()
            (self.agent / name).chmod(0o1770)
        self.agent.chmod(0o1770)  # root's on the host (#438)
        # The memories of codex off (request of the operator on PR #523).
        (self.agent / '.codex/config.toml').write_text('[features]\nmemories = false\n')
        self.exe(self.supervisor / '.local/bin/claude')
        self.state = state = self.devloop / 'ssdlc/.ssdlc/devloop'
        (state / 'current').mkdir(parents=True)
        (state / 'run.log').touch()
        # The agent user of the checks (#415): an existing user other than devloop's. The
        # directory of the repository locks (ADR 0006, R8).
        self.locks = self.tmp / 'locks/repos'
        self.lock_dir(self.locks)
        (state / 'config.local.toml').write_text(f'repo_lock_dir = "{self.locks}"\n'
                                                 '[agent_user]\nname = "root"\n')
        (self.tmp / 'systemd').mkdir()
        (self.tmp / 'systemd/devloop.service').write_text(render('host/devloop.service', {
            'REPO': 'brushtyler/ssdlc', 'DEVLOOP_USER': ME,
            'DEVLOOP_CLONE': str(self.devloop / 'ssdlc'),
            'LAUNCHER_CMD': f'/usr/bin/python3 {self.devloop}/ssdlc/scripts/devloop-pinned',
            'STATE_PATH': str(state),
            'DEVLOOP_PATH': f'{self.devloop}/.local/bin:/usr/local/bin:/usr/bin:/bin'}))
        self.acl(f'user:{ME}:--x')
        self.service('active')

    def key(self, home, role, mode=0o600):
        path = home / '.ghapps' / f'{PREFIX}{role}.2026-11-18.private-key.pem'
        path.write_text('not a key\n')
        path.chmod(mode)
        return path

    def exe(self, path):
        path.write_text('#!/bin/sh\n')
        path.chmod(0o755)

    def acl(self, entry):
        """getfacl: ``entry`` on the home of devloop only, nothing on any other path."""
        self.shim('getfacl', f'for a in "$@"; do last=$a; done\n'
                             f'echo "user::rwx"\n'
                             f'if [ "$last" = "{self.devloop}" ] && [ -n "{entry}" ]; then '
                             f'echo "{entry}"; fi\n'
                             f'echo "group::r-x"\necho "other::---"')

    def service(self, state):
        self.shim('systemctl', f'case "$1" in is-active) echo {state} ;; is-enabled) echo enabled ;;'
                               f' show) echo mixed ;; esac')

    def check(self, *args):
        params = self.params(DEVLOOP_USER=ME, SUPERVISOR_USER=ME, AGENT_USER='root')
        done = self.run_script('host/check-host.sh', '--dry-run', '--params', str(params), *args)
        # Outside root the directory of the locks cannot be root's
        # (test_the_directory_of_the_repository_locks_is_roots), nor the agent user's home.
        home = (f'  ERRORE: {self.agent} è 1770 {ME}, deve essere 1770 root '
                '(setup-agent-user.sh)')
        cli_dirs = {f'  ERRORE: {self.agent}/{d} è 1770 {ME}, deve essere 1770 root'
                    for d in agentuser.CLI_DIRS}  # nor the directories of its CLIs (#517)
        errors = [line for line in done.stdout.splitlines()
                  if 'ERRORE' in line and 'sudoers' not in line
                  and line != f'  ERRORE: {self.locks} è di {ME}, deve essere di root'
                  and line != home and line not in cli_dirs]
        return done, errors

    def test_the_files_of_the_agent_clis_are_checked_as_the_agent_user(self):
        # #438: the home root's and sticky; as the agent user, the probe of the startup check.
        self.agent.chmod(0o755)
        done, errors = self.check()
        self.assertEqual(errors, [f'  ERRORE: {self.agent} è 755 {ME}, deve essere 1770 root '
                                  '(setup-agent-user.sh)'])
        out = done.stdout
        lines = [line for line in out.splitlines()
                 if '/usr/bin/bash -c' in line and 'bad=0' in line]
        targets = [line.rsplit(' ', 2)[1:] for line in lines]
        self.assertEqual(targets, (
            [['cli', f'{self.agent}/{d}'] for d in agentuser.CLI_DIRS]
            + [['sealed', f'{self.agent}/{p}'] for p in agentuser.CLI_SEALED]
            + [['bin', f'{self.agent}/.local/bin/{cli}'] for cli in agentuser.CLIS]))
        for line in lines:
            self.assertTrue(line.startswith('  [dry-run] sudo -u root -H -- /usr/bin/bash -c '),
                            line)

    def test_a_configuration_of_codex_that_does_not_turn_its_memories_off_is_an_error(self):
        # Request of the operator on PR #523: as the startup check of devloop, the
        # configuration of codex must set [features] memories = false, in so many words.
        config = self.agent / '.codex/config.toml'
        done, errors = self.check()
        self.assertEqual(errors, [])
        self.assertIn(f'ok: {config} imposta [features] memories = false', done.stdout)
        for data, why in (('model = "gpt-5"\n', 'non imposta [features] memories = false'),
                          ('[features]\nmemories = true\n', 'imposta [features] memories = true'),
                          ('[features]\nmemories = false\n[profiles.p.features]\nmemories = true\n',
                           'imposta [profiles.p.features] memories = true'),
                          (None, 'non esiste')):
            with self.subTest(data=data):
                if data is None:
                    config.unlink()
                else:
                    config.write_text(data)
                done, errors = self.check()
                self.assertEqual(errors, [f'  ERRORE: {config} {why}: devloop non parte '
                                          '(setup-agent-user.sh aggiunge [features] memories = '
                                          'false)'])

    def test_an_entry_of_the_clis_not_in_the_list_is_an_error(self):
        # #517: as devloop before each turn.
        (self.agent / '.codex/auth.json').write_text('{}\n')
        (self.agent / '.codex/auth.lock').write_text('')
        (self.agent / '.gemini/antigravity-cli/mcp_config.json').write_text('{}\n')
        done, errors = self.check()
        self.assertEqual(errors, [f'  ERRORE: {self.agent}/.gemini/antigravity-cli/'
                                  'mcp_config.json non è nella lista AGENT_CLI_ALLOWED: devloop '
                                  f'rifiuta i turni finché c\'è (rimuoverla come root a loop '
                                  'fermo, o aggiungerla con una PR)'])
        self.assertIn(f'ok: {self.agent}/.codex contiene solo voci della lista', done.stdout)

    def test_the_log_link_of_agy_is_checked_as_devloop_does(self):
        # #525: cli.log -> log/cli-<data>.log, as agy makes it at each start, is accepted; a
        # link that leads out of log/ is an error.
        agy = self.agent / '.gemini/antigravity-cli'
        (agy / 'log').mkdir(exist_ok=True)
        (agy / 'log/cli-20261121_132524.log').write_text('started\n')
        link = agy / 'cli.log'
        link.symlink_to('log/cli-20261121_132524.log')
        done, errors = self.check()
        self.assertEqual(errors, [])
        self.assertIn(f'ok: {agy} contiene solo voci della lista', done.stdout)
        for target in ('/etc/passwd', '../antigravity-cli/log/cli-20261121_132524.log',
                       'brain/cli.log'):
            with self.subTest(target=target):
                link.unlink()
                link.symlink_to(target)
                done, errors = self.check()
                self.assertEqual(errors, [f'  ERRORE: {agy}/cli.log è un link a {target}, fuori '
                                          'dalla directory che AGENT_CLI_LINKS ammette per quel '
                                          'nome: devloop rifiuta i turni finché c\'è (rimuoverlo '
                                          'come root a loop fermo)'])
        # 1/codex-reviewer/R1-1: cli.log -> log/cli-1.log -> a file out of the home.
        outside = self.tmp / 'outside.log'
        outside.write_text('x\n')
        (agy / 'log/cli-1.log').symlink_to(outside)
        link.unlink()
        link.symlink_to('log/cli-1.log')
        done, errors = self.check()
        self.assertEqual(errors, [f'  ERRORE: {agy}/cli.log è un link a log/cli-1.log, che non '
                                  'è un file regolare (un altro link, che potrebbe uscire dalla '
                                  'directory, o nulla): devloop rifiuta i turni finché c\'è '
                                  '(rimuoverlo come root a loop fermo)'])

    def test_the_probe_of_the_cli_files_refuses_what_the_agent_user_can_change(self):
        # The probe of check-host.sh, run as the current user who owns the temporary files.
        if os.geteuid() == 0:
            self.skipTest('root reads and writes everything')
        text = (OPS / 'host/check-host.sh').read_text()
        probe = re.search(r"^CLI_PROBE='(.*?)'$", text, re.S | re.M).group(1)

        def run(kind, path, env=None):
            return subprocess.run([BASH, '-c', probe, '_', kind, str(path)],
                                  capture_output=True, text=True, timeout=60, env=env)
        (self.agent / '.claude/CLAUDE.md').write_text('x\n')
        # #517: a directory of the CLIs is writable and sticky, but not the agent user's.
        done = run('cli', self.agent / '.claude')
        self.assertEqual(done.returncode, 1)
        self.assertIn(f'suo: {self.agent}/.claude', done.stderr)
        self.assertNotIn('sticky', done.stderr)
        self.assertNotIn('non scrivibile', done.stderr)
        (self.agent / '.codex').chmod(0o755)
        done = run('cli', self.agent / '.codex')
        self.assertIn(f'senza sticky bit: {self.agent}/.codex', done.stderr)
        (self.agent / '.codex').chmod(0o1770)
        tmp = Path(tempfile.gettempdir())  # sticky, writable, root's: accepted
        if tmp.stat().st_mode & 0o1000 and tmp.stat().st_uid == 0:
            self.assertEqual(run('cli', tmp).returncode, 0, run('cli', tmp).stderr)
        done = run('sealed', self.agent / '.claude/CLAUDE.md')
        self.assertEqual(done.returncode, 1)
        self.assertIn(f'scrivibile: {self.agent}/.claude/CLAUDE.md', done.stderr)
        self.assertIn(f'suo: {self.agent}/.claude', done.stderr)
        # Without its placeholder, a sealed entry can be created in the sticky directory.
        done = run('sealed', self.agent / '.codex/AGENTS.md')
        self.assertIn(f'creabile: {self.agent}/.codex/AGENTS.md', done.stderr)
        # agy's configuration written by the agent user: an MCP server, a plugin (#517).
        for name in ('mcp_config.json', 'config.json'):
            path = self.agent / '.gemini/config' / name
            path.write_text('{"mcpServers": {"x": {"command": "/tmp/x"}}}\n')
            done = run('sealed', path)
            self.assertEqual(done.returncode, 1, name)
            self.assertIn(f'scrivibile: {path}', done.stderr)
        done = run('sealed', self.agent / '.gemini/config/projects')
        self.assertIn(f'creabile: {self.agent}/.gemini/config/projects', done.stderr)
        self.assertEqual(run('sealed', BASH).returncode, 0)
        self.assertEqual(run('bin', BASH).returncode, 0)
        # The tools of the agent user's PATH (~/.local/bin first) are not those the probe runs.
        hostile = self.tmp / 'hostile'
        hostile.mkdir()
        for name, body in {'stat': 'echo 0', 'find': 'exit 0', 'id': 'echo 65534'}.items():
            (hostile / name).write_text(f'#!/bin/sh\n{body}\n')
            (hostile / name).chmod(0o755)
        done = run('sealed', self.agent / '.claude/CLAUDE.md',
                   env=dict(os.environ, PATH=f'{hostile}:/usr/bin:/bin'))
        self.assertIn(f'suo: {self.agent}/.claude', done.stderr)
        # A link is followed: the file it leads to, and a loop, are refused.
        elsewhere = self.tmp / 'elsewhere'
        elsewhere.mkdir()
        (elsewhere / 'config.toml').write_text('x\n')
        (elsewhere / 'config.toml').chmod(0o444)
        (self.agent / '.codex').mkdir(exist_ok=True)
        (self.agent / '.codex/config.toml').unlink(missing_ok=True)
        (self.agent / '.codex/config.toml').symlink_to(elsewhere / 'config.toml')
        done = run('sealed', self.agent / '.codex/config.toml')
        self.assertIn(f'suo: {elsewhere}/config.toml', done.stderr)
        (self.agent / '.claude/rules').symlink_to('rules')
        done = run('sealed', self.agent / '.claude/rules')
        self.assertIn(f'link non seguibile: {self.agent}/.claude/rules', done.stderr)
        # What the agent user cannot read is refused too.
        (self.agent / '.claude/CLAUDE.md').chmod(0)
        done = run('sealed', self.agent / '.claude/CLAUDE.md')
        self.assertIn(f'illeggibile: {self.agent}/.claude/CLAUDE.md', done.stderr)

    def test_a_correct_installation_has_no_errors(self):
        done, errors = self.check()
        self.assertEqual(errors, [], done.stdout + done.stderr)
        out = done.stdout
        for role in DEVLOOP_KEYS:
            self.assertIn(f'{self.devloop}/.ghapps/{PREFIX}{role}.2026-11-18.private-key.pem: '
                          f'600, {ME}', out)
        self.assertIn(f'ok: ACL user:{ME}:--x su {self.devloop}', out)
        self.assertIn('ok: devloop è attivo', out)
        # The checks through sudo or the network are only printed.
        self.assertIn(f'[dry-run] sudo -u {ME} -H -- test -r {self.devloop}/ssdlc/.ssdlc/devloop/'
                      'run.log', out)
        self.assertIn(f'[dry-run] sudo -u root -H -- {self.agent}/.local/bin/claude --version',
                      out)
        self.assertRegex(out, r'\[dry-run\] sudo .*/scripts/devloop-pinned apps')
        # The agent user reads neither the keys nor state_dir (#415).
        for path in (self.devloop / '.ghapps', self.state, self.state / 'config.local.toml'):
            self.assertIn(f'[dry-run] sudo -u root -H -- /usr/bin/bash -c ' + r"\!\ \[\ -r\ "
                          + r'\"\$1\"\ \]' + f' _ {path}\n', out)
        self.assertIn(r'\!\ \[\ -x\ \"\$1\"\ \]' + f' _ {self.devloop}/.ghapps\n', out)
        # Every other entry of state_dir is neither read nor entered (1/codex/R1-3).
        for name in ('run.log', 'current', 'config.local.toml'):
            self.assertRegex(out, r'\[dry-run\] sudo -u root -H -- /usr/bin/bash -c .*-r.*-x.* _ '
                                  + re.escape(f'{self.state}/{name}') + '\n')
        # It enters the directories of the worktrees and of the turns without writing them,
        # and writes nothing in .git (1/codex/R1-1, 1/codex/R1-2).
        for name in ('worktrees', 'agent-shared', 'agent-shared/turns', 'agent-shared/tmp',
                     'agent-shared/schemas'):
            self.assertRegex(out, r'\[dry-run\] sudo -u root -H -- /usr/bin/bash -c .*\\!\\ '
                                  r'\\\[\\ -w.* _ ' + re.escape(f'{self.state}/{name}') + '\n')
        self.assertRegex(out, r'-writable.* _ ' + re.escape(f'{self.devloop}/ssdlc/.git') + '\n')
        self.assertIn(f'ok: {self.state}/config.local.toml imposta [agent_user] name = "root"',
                      out)
        self.assertEqual(self.mutating_calls(), [])

    def test_git_runs_as_the_agent_user_in_a_worktree_with_only_its_safe_directory(self):
        # #500: as devloop does, through `sudo -n -E` from devloop's user, safe.directory for
        # that worktree only; without a worktree, a warning.
        done, errors = self.check()
        self.assertEqual(errors, [], done.stdout)
        self.assertIn(f'ATTENZIONE: nessun worktree in {self.state}/worktrees: git come root '
                      'non verificato', done.stdout)
        worktree = self.state / 'worktrees/issue-7'
        worktree.mkdir(parents=True)
        (worktree / '.git').write_text('gitdir: /elsewhere\n')
        done, errors = self.check()
        self.assertEqual(errors, [], done.stdout)
        line, = [line for line in done.stdout.splitlines() if 'safe.directory' in line]
        self.assertTrue(line.startswith(
            f'  [dry-run] sudo -u {ME} -H -- /usr/bin/env GIT_CONFIG_COUNT=1 '
            f'GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0={worktree} '
            'sudo -n -E -u root -- /usr/bin/bash -c '), line)
        self.assertTrue(line.endswith(f' _ {worktree}'), line)
        self.assertIn('git\\ -C\\ \\"\\$1\\"\\ status', line)
        self.assertNotIn('*', line)
        self.assertNotIn('nessun worktree', done.stdout)

    def test_the_clis_run_a_command_with_their_shell_tool_as_the_agent_user(self):
        # #494: --version does not start the shell tool, where agy stopped with permission
        # denied; check-host has each CLI run git --version with it, as the agent user. And
        # the agent user writes what the CLIs create on first use.
        done, errors = self.check()
        self.assertEqual(errors, [], done.stdout)
        out = done.stdout
        def q(text):  # as `run` prints an argument
            return subprocess.run([BASH, '-c', 'printf %q "$1"', '_', text], check=True,
                                  capture_output=True, text=True).stdout
        prompt = q('Run the shell command `git --version` with your shell tool, then reply with '
                   'its output only.')
        lines = {cli: [line for line in out.splitlines()
                       if f'timeout 300 {self.agent}/.local/bin/{cli} ' in line]
                 for cli in agentuser.CLIS}
        for cli, found in lines.items():
            self.assertEqual(len(found), 1, cli)
            self.assertTrue(found[0].startswith('  [dry-run] sudo -u root -H -- timeout 300 '),
                            found[0])
        settings = q('{"permissions": {"allow": ["command(git --version)"]}}')
        self.assertIn(f'agy -p {prompt} --output-format json --settings {settings}\n',
                      lines['agy'][0] + '\n')
        self.assertIn(f'claude -p {prompt} --allowedTools {q("Bash(git --version)")}\n',
                      lines['claude'][0] + '\n')
        self.assertIn(f'codex exec --skip-git-repo-check -s read-only {prompt}', lines['codex'][0])
        self.assertRegex(out, r'\[dry-run\] sudo -u root -H -- /usr/bin/bash -c .*-w.* _ '
                              + re.escape(f'{self.agent}/.gemini/antigravity-cli/bin') + '\n')

    def test_a_cli_that_cannot_run_a_command_is_an_error(self):
        # #494, with fakes of the CLIs, run as the current user, the agent user of this check.
        args = self.tmp / 'agy-args'
        for cli in ('claude', 'codex'):
            path = self.agent / '.local/bin' / cli
            path.write_text('#!/bin/sh\necho "git version 2.47.0"\n')
        agy = self.agent / '.local/bin/agy'
        denied = (f'mkdir {self.agent}/.gemini/antigravity-cli/bin: permission denied')
        agy.write_text(f'#!/bin/sh\n[ "$1" = --version ] && exit 0\n'
                       f'printf "%s\\n" "$@" > {shlex.quote(str(args))}\n'
                       f'echo "starting the shell tool"\necho {shlex.quote(denied)} >&2\nexit 1\n')
        self.fake_host('classic sudo', users=('opsdry-devloop', FAKE_SUPERVISOR))
        params = self.params(DEVLOOP_USER='opsdry-devloop', SUPERVISOR_USER=FAKE_SUPERVISOR,
                             AGENT_USER=ME)

        def run():
            done = self.run_script('host/check-host.sh', '--no-apps', '--params', str(params))
            return done, [line for line in done.stdout.splitlines()
                          if 'strumento di shell' in line]
        done, lines = run()
        self.assertEqual(done.returncode, 1)
        self.assertIn(f'  ok: claude come {ME} esegue git --version con il suo strumento di shell '
                      '(uscita 0)', lines)
        self.assertIn(f'  ok: codex come {ME} esegue git --version con il suo strumento di shell '
                      '(uscita 0)', lines)
        # #530: the outcome and the exit code, never what the CLI printed.
        failed = (f'  ERRORE: agy come {ME} non esegue git --version con il suo strumento di '
                  'shell (uscita {}; l\'output non si stampa: ripetere la prova a mano, '
                  'ops/host/migrate.md, una CLI che si ferma con permission denied)')
        self.assertIn(failed.format(1), lines)
        self.assertNotIn(denied, done.stdout + done.stderr)
        self.assertEqual(args.read_text().splitlines()[:2],
                         ['-p', 'Run the shell command `git --version` with your shell tool, '
                                'then reply with its output only.'])
        self.assertIn('{"permissions": {"allow": ["command(git --version)"]}}',
                      args.read_text().splitlines())
        # An answer without the output of the command is an error too, whatever the exit.
        agy.write_text('#!/bin/sh\necho \'{"status": "SUCCESS", "result": "I cannot."}\'\n')
        done, lines = run()
        self.assertIn(failed.format(0), lines)
        self.assertNotIn('I cannot', done.stdout + done.stderr)
        # The words without the version, or an explicit denial, are errors with exit 0 too
        # (1/codex/R1-1).
        for result in ('I cannot report the git version: the shell command failed with '
                       'permission denied.',
                       'git version 2.47.0, but mkdir bin: Permission denied'):
            answer = '{"status": "SUCCESS", "result": "' + result + '"}'
            agy.write_text('#!/bin/sh\necho ' + shlex.quote(answer) + '\n')
            done, lines = run()
            self.assertEqual(done.returncode, 1)
            self.assertIn(failed.format(0), lines)
            self.assertNotIn(result, done.stdout + done.stderr)
        agy.write_text('#!/bin/sh\necho \'{"status": "SUCCESS", "result": "git version 2.47.0"}\'\n')
        done, lines = run()
        self.assertIn(f'  ok: agy come {ME} esegue git --version con il suo strumento di shell '
                      '(uscita 0)', lines)
        self.assertEqual([line for line in lines if 'ERRORE' in line], [])

    def fake_clis(self, body):
        """The CLIs of the agent user and of the supervisor, with ``body`` after a line that
        records each start in ``bin/started`` (out of ``tree``)."""
        started = self.bin / 'started'
        for path in [self.agent / '.local/bin' / cli for cli in agentuser.CLIS] \
                + [self.supervisor / '.local/bin/claude']:
            path.write_text(f'#!/bin/sh\necho "$0 $*" >> {shlex.quote(str(started))}\n{body}\n')
        return started

    def executing_sudo(self):
        """A classic ``sudo`` that runs the command as the current user and logs, in
        ``bin/sudo-log`` (out of ``tree``), its options and the program it runs, not as a change
        of the host."""
        log = self.bin / 'sudo-log'
        self.shim('sudo', f'''if [ "$1" = --version ]; then
    echo {shlex.quote(SUDO_FLAVORS['classic sudo'])}; exit 0
fi
opts=
while [ $# -gt 0 ] && [ "$1" != -- ]; do opts="$opts $1"; shift; done
shift
echo "sudo$opts -- $1" >> {shlex.quote(str(log))}
exec "$@"''')
        return log

    def test_the_probes_of_the_clis_print_only_the_outcome_and_the_exit_code(self):
        # #530: the full check runs --version and the shell tool of each CLI, and of their output,
        # the model's text, prints nothing: only passed or failed, with the exit code.
        self.fake_host('classic sudo', users=(FAKE_AGENT, FAKE_SUPERVISOR))
        self.executing_sudo()
        params = self.params(DEVLOOP_USER=ME, SUPERVISOR_USER=FAKE_SUPERVISOR,
                             AGENT_USER=FAKE_AGENT)
        for status, version, denial in ((0, 'git version 2.47.0', ''),
                                        (3, '', ': permission denied'),
                                        (0, 'git version 2.47.0', ': permission denied'),
                                        (0, '', '')):
            with self.subTest(status=status, version=version, denial=denial):
                started = self.fake_clis(f'echo "{version} {MODEL_TEXT} on stdout"\n'
                                         f'echo "{MODEL_TEXT} on stderr{denial}" >&2\n'
                                         f'[ "$1" = --version ] && exit 0\nexit {status}')
                started.unlink(missing_ok=True)
                done = self.run_script('host/check-host.sh', '--no-apps', '--params', str(params))
                self.assertNotIn(MODEL_TEXT, done.stdout + done.stderr)
                # The probes ran: --version of the four CLIs and the shell tool of three.
                self.assertEqual(len(started.read_text().splitlines()), 7)
                out = done.stdout
                for cli in agentuser.CLIS:
                    self.assertIn(f'  ok: {cli} --version come {FAKE_AGENT} (uscita 0)\n', out)
                    if version and not denial:
                        self.assertIn(f'  ok: {cli} come {FAKE_AGENT} esegue git --version con il '
                                      'suo strumento di shell (uscita 0)\n', out)
                    else:  # a denial, or no version, is a failure even with exit 0
                        self.assertIn(f'  ERRORE: {cli} come {FAKE_AGENT} non esegue git --version '
                                      f'con il suo strumento di shell (uscita {status}; l\'output '
                                      'non si stampa', out)
                self.assertIn(f'  ok: claude --version come {FAKE_SUPERVISOR} (uscita 0)\n', out)
        # A --version that fails: its exit code, not its output.
        self.fake_clis(f'echo "{MODEL_TEXT}"\nexit 5')
        done = self.run_script('host/check-host.sh', '--no-apps', '--params', str(params))
        self.assertNotIn(MODEL_TEXT, done.stdout + done.stderr)
        self.assertIn(f'  ERRORE: codex --version come {FAKE_AGENT} non riuscito (uscita 5)\n',
                      done.stdout)
        self.assertEqual(self.mutating_calls(), [])

    def coordinator_copy(self, params):
        """The root copy of check-host.sh that setup-coordinator.sh installs (#530), here owned
        by the user of the tests, with the parameters as its params.env."""
        lib = (OPS / 'lib.sh').read_text()
        files = re.search(r"^OPS_COORDINATOR_FILES='(.*)'$", lib, re.M).group(1).split()
        copy = self.tmp / 'lib/devloop-coordinator'
        for name in files:
            (copy / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(OPS / name, copy / name)
            (copy / name).chmod(0o644)
        shutil.copyfile(params, copy / 'params.env')
        (copy / 'params.env').chmod(0o644)
        for directory in (copy, copy / 'host'):
            directory.chmod(0o755)
        return copy

    def test_the_read_only_check_starts_no_cli_and_writes_nothing(self):
        # #530: the coordinator's check, as root through its sudoers, from the root copy: no
        # CLI started, no devloop apps, nothing written, only status lines.
        self.fake_host('classic sudo', users=(FAKE_AGENT, FAKE_SUPERVISOR, FAKE_COORDINATOR))
        sudo_log = self.executing_sudo()
        self.shim('visudo', 'exit 0')  # visudo -c only reads
        started = self.fake_clis(f'echo "{MODEL_TEXT}"')
        params = self.params(DEVLOOP_USER=ME, SUPERVISOR_USER=FAKE_SUPERVISOR,
                             AGENT_USER=FAKE_AGENT)
        copy = self.coordinator_copy(params)
        sudoers = self.tmp / 'sudoers.d/devloop-coordinator'
        sudoers.parent.mkdir()
        sudoers.write_text(render('host/sudoers-coordinator', {
            'DEVLOOP_USER': ME, 'COORDINATOR_USER': FAKE_COORDINATOR,
            'DEVLOOP_PATH': f'{self.devloop}/.local/bin:/usr/local/bin:/usr/bin:/bin',
            'LAUNCHER_CMD': f'/usr/bin/python3 {self.devloop}/ssdlc/scripts/devloop-pinned',
            'STATE_PATH': str(self.state), 'SERVICE_NAME': 'devloop',
            'COORDINATOR_OPS_DIR': str(copy)}))
        sudoers.chmod(0o440)
        # From the coordinator's own directory, with a module python would import from there.
        cwd = self.tmp / 'cwd'
        cwd.mkdir()
        (cwd / 'tomllib.py').write_text(f'open({str(self.tmp / "imported")!r}, "w")\n')
        before = self.tree()
        env = dict(os.environ, PATH=f'{self.bin}:{os.environ.get("PATH", "/usr/bin:/bin")}',
                   OPS_TEST_CALLS=str(self.calls), OPS_OS_RELEASE=str(self.release),
                   OPS_ARCH='aarch64', LC_ALL='C.UTF-8')
        done = subprocess.run([BASH, str(copy / 'host/check-host.sh'), '--read-only'],
                              capture_output=True, text=True, env=env, cwd=cwd,
                              stdin=subprocess.DEVNULL, timeout=120)
        out = done.stdout
        self.assertNotIn('No such file', done.stderr)
        self.assertFalse(started.exists(), started.read_text() if started.exists() else '')
        self.assertNotIn(MODEL_TEXT, out + done.stderr)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.mutating_calls(), [])
        # The checks as the other users ran, through sudo, and none was a CLI or devloop: only
        # test and the probes of bash.
        calls = sudo_log.read_text().splitlines()
        self.assertTrue(any(f'-u {FAKE_AGENT} ' in call for call in calls), calls)
        self.assertEqual({call.rsplit(' -- ', 1)[1] for call in calls}, {'test', '/usr/bin/bash'})
        self.assertIn('Sola lettura (--read-only): nessuna CLI avviata, niente devloop apps.', out)
        self.assertIn('  prove delle CLI saltate (--read-only): nessuna CLI avviata\n', out)
        self.assertIn(f'  ok: {self.agent}/.local/bin/agy presente\n', out)
        self.assertIn(f'  ok: {self.supervisor}/.local/bin/claude presente\n', out)
        self.assertIn('==> GitHub App (devloop apps)\n  saltato (--read-only)\n', out)
        # Its parameters and templates are the copy's.
        self.assertIn(f'  ok: {sudoers} coincide con ops/host/sudoers-coordinator\n', out)
        self.assertIn(f"  ok: l'utente {FAKE_COORDINATOR} esiste\n", out)
        self.assertNotIn('è diverso da', out)
        # Only status lines: steps, results and the count.
        for line in out.splitlines():
            self.assertRegex(line, r'^(|==> .*|  \S.*|Sola lettura .*|\d+ errori, \d+ avvisi)$')
        # The same host, without --read-only, starts the CLIs.
        done = self.run_script('host/check-host.sh', '--no-apps', '--params', str(params))
        self.assertTrue(started.exists())

    def test_the_coordinator_is_checked_when_it_is_installed(self):
        # #530: absent, a line; present, its sudoers and the root copy, equal to this checkout.
        done, errors = self.check()
        self.assertEqual(errors, [], done.stdout)
        self.assertIn(f"  l'utente {FAKE_COORDINATOR} non esiste: coordinatore non installato "
                      '(setup-coordinator.sh)\n', done.stdout)
        self.fake_host('classic sudo', users=(FAKE_AGENT, FAKE_SUPERVISOR, FAKE_COORDINATOR))
        params = self.params(DEVLOOP_USER=ME, SUPERVISOR_USER=FAKE_SUPERVISOR,
                             AGENT_USER=FAKE_AGENT)
        copy = self.coordinator_copy(params)
        (copy / 'host/check-host.sh').write_text('# an older copy\n')
        (copy / 'lib.sh').chmod(0o664)
        done = self.run_script('host/check-host.sh', '--dry-run', '--params', str(params))
        out = done.stdout
        self.assertIn(f"  ok: l'utente {FAKE_COORDINATOR} esiste\n", out)
        self.assertIn(f'  ERRORE: {self.tmp}/sudoers.d/devloop-coordinator non esiste '
                      '(setup-coordinator.sh)', out)
        # The copy is not root's here: an error for it and for each directory above it.
        self.assertIn(f'  ERRORE: {copy}/host è 755 {ME}: deve essere di root e non scrivibile '
                      'dal gruppo né dagli altri\n', out)
        self.assertIn(f'  ERRORE: {copy}/lib.sh è 664 {ME}, deve essere 644 root\n', out)
        self.assertIn(f'  ok: {copy}/params.env coincide con {params}\n', out)
        self.assertIn(f'  ok: {copy}/host/sudoers-agent coincide con {OPS}/host/sudoers-agent\n',
                      out)
        self.assertIn(f'  ATTENZIONE: {copy}/host/check-host.sh è diverso da '
                      f'{OPS}/host/check-host.sh: rieseguire setup-coordinator.sh\n', out)
        (copy / 'host/sudoers-agent').unlink()
        done = self.run_script('host/check-host.sh', '--dry-run', '--params', str(params))
        self.assertIn(f'  ERRORE: {copy}/host/sudoers-agent non esiste (setup-coordinator.sh)\n',
                      done.stdout)
        self.assertEqual(self.mutating_calls(), [])

    def test_the_copy_holds_every_file_of_ops_check_host_reads(self):
        # #530: setup-coordinator.sh copies the files of ops/host that check-host.sh renders.
        lib = (OPS / 'lib.sh').read_text()
        files = set(re.search(r"^OPS_COORDINATOR_FILES='(.*)'$", lib, re.M).group(1).split())
        script = (OPS / 'host/check-host.sh').read_text()
        named = {f'host/{p.name}' for p in (OPS / 'host').iterdir()
                 if p.is_file() and p.suffix not in ('.sh', '.md', '.example', '.json')
                 and (p.name in script or f'UNIT_TEMPLATE={p.name}' in lib)}
        self.assertEqual(named | {'lib.sh', 'host/check-host.sh'}, files)
        self.assertIn('host/devloop@.service', named)

    def real_sudo(self, sudo, env_stderr='', env_status=0):
        """A ``sudo`` whose ``--version`` is that of ``SUDO_FLAVORS``, and whose ``sudo -n -E``
        runs the command as the current user: sudo-rs with a minimal environment, the classic
        sudo with the caller's (#496). With ``env_status`` it fails, printing ``env_stderr``.
        Any other call is recorded, as the shims of ``MUTATING``."""
        start = ('exec env -i PATH=/usr/bin:/bin "$@"' if sudo == 'sudo-rs' else 'exec "$@"')
        if env_status:
            start = f'exit {env_status}'
        warning = f'printf "%s\\n" {shlex.quote(env_stderr)} >&2' if env_stderr else ':'
        self.shim('sudo', f'''if [ "$1" = --version ]; then
    echo {shlex.quote(SUDO_FLAVORS[sudo])}; exit 0
fi
if [ "$1" = -n ] && [ "$2" = -E ] && [ "$3" = -u ] && [ "$5" = -- ]; then
    shift 5
    {warning}
    {start}
fi
echo "sudo $*" >> "$OPS_TEST_CALLS"''')

    def check_for_real(self):
        """check-host.sh without --dry-run: the checks through sudo reach the shim of sudo."""
        params = self.params(DEVLOOP_USER=ME, SUPERVISOR_USER=ME, AGENT_USER='root')
        done = self.run_script('host/check-host.sh', '--no-apps', '--params', str(params))
        return done, [line for line in done.stdout.splitlines() if 'ERRORE' in line]

    def test_sudo_rs_is_an_error_with_the_fix(self):
        # #496: sudo-rs ignores -E, and the agents ran without the environment devloop prepares.
        self.real_sudo('sudo-rs', env_stderr="sudo: preserving the entire environment is not "
                                             "supported, '-E' is ignored")
        done, errors = self.check_for_real()
        self.assertEqual(done.returncode, 1)
        fix = 'update-alternatives --set sudo /usr/bin/sudo.ws'
        self.assertIn(f"  ERRORE: sudo è {SUDO_FLAVORS['sudo-rs']}: ignora -E e gli agent "
                      f"girerebbero senza l'ambiente che devloop prepara; serve sudo 1.9: {fix}",
                      errors)
        env_errors = [line for line in errors if "l'ambiente non arriva" in line]
        self.assertEqual(len(env_errors), 1, errors)
        self.assertIn(f'root con sudo -E (uscita 0): sudo-rs ignora -E: {fix}', env_errors[0])
        self.assertIn("'-E' is ignored", env_errors[0])

    def test_the_classic_sudo_passes_the_environment(self):
        self.real_sudo('classic sudo')
        done, errors = self.check_for_real()
        self.assertIn(f"  ok: sudo: {SUDO_FLAVORS['classic sudo']}", done.stdout)
        self.assertIn("  ok: l'ambiente arriva a root con sudo -E", done.stdout)
        self.assertEqual([line for line in errors if 'sudo' in line and 'sudoers' not in line],
                         [], done.stdout)

    def test_a_rule_without_setenv_is_an_error(self):
        self.real_sudo('classic sudo', env_status=1,
                       env_stderr='sudo: sorry, you are not allowed to preserve the environment')
        done, errors = self.check_for_real()
        self.assertEqual(done.returncode, 1)
        env_errors = [line for line in errors if "l'ambiente non arriva" in line]
        self.assertEqual(len(env_errors), 1, errors)
        self.assertIn('(uscita 1): manca il tag SETENV nel sudoers', env_errors[0])

    def test_the_dry_run_prints_the_check_of_the_environment(self):
        done, errors = self.check()
        self.assertIn('  [dry-run] sudo --version\n', done.stdout)
        self.assertRegex(done.stdout, r'\[dry-run\] env DEVLOOP_ENV_PROBE=devloop-env-probe-\S+ '
                                      r'sudo -n -E -u root -- /usr/bin/bash -c ')
        self.assertEqual(self.mutating_calls(), [])

    def test_an_overlay_without_the_agent_user_is_an_error(self):
        (self.state / 'config.local.toml').write_text(f'repo_lock_dir = "{self.locks}"\n'
                                                      '[merge]\nmode = "off"\n')
        done, errors = self.check()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(errors, [f'  ERRORE: {self.state}/config.local.toml non imposta '
                                  f'[agent_user] name = "root": gli agent girerebbero come {ME}'])

    def test_a_missing_agent_cli_is_an_error(self):
        (self.agent / '.local/bin/codex').unlink()
        done, errors = self.check()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(errors, [f'  ERRORE: {self.agent}/.local/bin/codex non trovato '
                                  '(setup-agent-user.sh)'])

    def test_a_key_readable_by_others_is_an_error(self):
        key = self.key(self.devloop, 'merger', 0o644)
        done, errors = self.check()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(errors, [f'  ERRORE: {key} è 644 {ME}, deve essere 600 {ME}'])

    def test_a_missing_key_is_an_error(self):
        next((self.devloop / '.ghapps').glob(f'{PREFIX}dispatcher.*')).unlink()
        done, errors = self.check()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(errors, [f'  ERRORE: manca la chiave di {PREFIX}dispatcher in '
                                  f'{self.devloop}/.ghapps'])

    def test_a_key_dir_not_700_is_an_error(self):
        (self.devloop / '.ghapps').chmod(0o750)
        done, errors = self.check()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(errors, [f'  ERRORE: {self.devloop}/.ghapps è 750 {ME}, deve essere 700 {ME}'])

    def test_the_supervisor_holds_only_its_keys(self):
        extra = self.key(self.supervisor, 'dispatcher')
        done, errors = self.check()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn(f'{extra}: chiave non attesa', errors[0])

    def test_a_listing_acl_is_an_error(self):
        self.acl(f'user:{ME}:r-x')
        done, errors = self.check()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(errors, [f'  ERRORE: ACL user:{ME}:r-x su {self.devloop}: deve essere '
                                  '--x, senza elenco né scrittura'])

    def test_a_missing_acl_is_an_error(self):
        self.acl('')
        done, errors = self.check()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn(f"manca l'ACL u:{ME}:--x", errors[0])

    def test_the_state_of_the_service_is_reported(self):
        self.service('inactive')
        done, errors = self.check('--no-apps')
        self.assertEqual(errors, [])
        self.assertIn('ATTENZIONE: devloop non è attivo (inactive)', done.stdout)
        self.assertIn('saltato (--no-apps)', done.stdout)
        self.service('failed')
        done, errors = self.check()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('devloop è fallito', errors[0])

    def test_a_missing_unit_is_an_error(self):
        (self.tmp / 'systemd/devloop.service').unlink()
        done, errors = self.check()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(errors, [f'  ERRORE: {self.tmp}/systemd/devloop.service non esiste '
                                  '(setup-devloop.sh)'])

    def test_the_repository_lock_is_checked(self):
        # ADR 0006, R8: the directory, the group, the agent user outside it, the overlay.
        done, errors = self.check()
        out = done.stdout
        self.assertEqual(errors, [], out)
        self.assertIn(f'ok: {self.locks}: 3770, gruppo {GROUP}', out)
        self.assertIn(f'ok: {ME} è nel gruppo {GROUP}', out)
        self.assertIn(f'ok: {self.tmp}/tmpfiles.d/devloop-repos.conf coincide con '
                      'ops/host/tmpfiles-devloop-repos.conf', out)
        self.assertIn(f'ok: {self.state}/config.local.toml imposta repo_lock_dir = '
                      f'"{self.locks}"', out)
        self.assertIn(r'[dry-run] sudo -u root -H -- /usr/bin/bash -c \!\ \[\ -x\ \"\$1\"\ \]'
                      f' _ {self.locks}\n', out)

    @unittest.skipIf(os.geteuid() == 0, 'as root the directory of the tests is root\'s')
    def test_the_directory_of_the_repository_locks_is_roots(self):
        done, _ = self.check()
        self.assertIn(f'  ERRORE: {self.locks} è di {ME}, deve essere di root\n', done.stdout)

    def test_a_missing_repository_lock_is_an_error(self):
        (self.state / 'config.local.toml').write_text('[agent_user]\nname = "root"\n')
        self.locks.chmod(0o770)
        (self.tmp / 'tmpfiles.d/devloop-repos.conf').unlink()
        done, errors = self.check()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(errors, [
            f'  ERRORE: {self.locks} è 770 {GROUP}, deve essere 3770 del gruppo {GROUP}',
            f'  ERRORE: {self.tmp}/tmpfiles.d/devloop-repos.conf non esiste (setup-devloop.sh): '
            f'{self.locks} non tornerebbe dopo un riavvio',
            f'  ERRORE: {self.state}/config.local.toml non imposta repo_lock_dir = '
            f'"{self.locks}": un altro dispatcher sullo stesso repository non sarebbe escluso'])
        self.locks.rmdir()
        done, errors = self.check()
        self.assertIn(f'  ERRORE: {self.locks} non esiste (setup-devloop.sh, systemd-tmpfiles)',
                      errors)

    def test_a_project_checks_its_instance_and_one_service_per_repository(self):
        # ADR 0006, R8: the template and the drop-in of devloop@widget; another unit of the host
        # on the same repository is an error.
        systemd = self.tmp / 'systemd'
        clone = self.devloop / 'widget'
        state = clone / '.ssdlc/devloop'
        (state / 'current').mkdir(parents=True)
        (state / 'run.log').touch()
        (state / 'config.local.toml').write_text(f'repo_lock_dir = "{self.locks}"\n'
                                                 '[agent_user]\nname = "root"\n')
        (systemd / 'devloop@.service').write_text(render('host/devloop@.service', {}))
        # ssdlc, another installation of the host, has a user of its own (R1-1).
        unit = systemd / 'devloop.service'
        unit.write_text(unit.read_text().replace(f'User={ME}\n', 'User=opsdry-ssdlc\n'))
        dropin = systemd / 'devloop@widget.service.d/project.conf'
        dropin.parent.mkdir()
        dropin.write_text(render('host/devloop-project.conf', {
            'REPO': 'acme/widget', 'DEVLOOP_USER': ME, 'DEVLOOP_CLONE': str(clone),
            'LAUNCHER_CMD': f'/usr/bin/python3 {clone}/scripts/devloop-pinned',
            'STATE_PATH': str(state),
            'DEVLOOP_PATH': f'{self.devloop}/.local/bin:/usr/local/bin:/usr/bin:/bin'}))
        params = self.params(PROJECT='widget', REPO='acme/widget', DEVLOOP_USER=ME,
                             SUPERVISOR_USER=ME, AGENT_USER='root',
                             SUPERVISOR_KEY_DIR=str(self.supervisor / '.ghapps'),
                             SUDOERS_FILE=str(self.tmp / 'sudoers.d/devloop-supervisor-widget'))
        done = self.run_script('host/check-host.sh', '--dry-run', '--params', str(params))
        out = done.stdout
        self.assertIn(f'ok: {systemd}/devloop@.service coincide con ops/host/devloop@.service',
                      out)
        self.assertIn(f'ok: {dropin} coincide con ops/host/devloop-project.conf', out)
        self.assertIn('==> Servizio devloop@widget', out)
        self.assertNotIn('gestisce anche', out)
        # devloop.service of ssdlc is another repository; a second instance on acme/widget is not.
        other = systemd / 'devloop@widget2.service.d/project.conf'
        other.parent.mkdir()
        other.write_text(dropin.read_text().replace('Description=devloop continuous loop '
                                                    '(acme/widget)',
                                                    'Description=devloop continuous loop '
                                                    '(Acme/Widget)')
                         .replace(f'User={ME}\n', 'User=opsdry-widget2\n'))
        done = self.run_script('host/check-host.sh', '--dry-run', '--params', str(params))
        self.assertEqual(done.returncode, 1)
        self.assertIn(f'ERRORE: {other} gestisce anche acme/widget: un repository, un servizio',
                      done.stdout)
        self.assertEqual(self.mutating_calls(), [])

    def test_a_separate_source_checks_the_launcher_and_the_state_dir_of_the_host(self):
        # ADR 0006, R1: the launcher installed from the source's tag, the host's state_dir.
        state, lib = self.tmp / 'state', self.tmp / 'lib'
        (state / 'current').mkdir(parents=True)
        (state / 'run.log').touch()
        (self.devloop / 'devloop-source/.git').mkdir(parents=True)
        # #497: the host is simulated (fake_host, with the ACL of setUp): the user of the tests,
        # unprivileged, is devloop's and the supervisor, the agent user another one, and sudo is
        # sudo-rs or the classic one.
        params = self.params(DEVLOOP_USER=ME, SUPERVISOR_USER=ME, AGENT_USER=FAKE_AGENT,
                             REPO='acme/widget', DEVLOOP_CLONE=str(self.devloop / 'ssdlc'),
                             SOURCE_REPO='brushtyler/ssdlc', SOURCE_REF='v1.2.0',
                             STATE_DIR=str(state), LAUNCHER_DIR=str(lib))
        for sudo in SUDO_FLAVORS:
            with self.subTest(sudo):
                self.fake_host(sudo, users=(FAKE_AGENT,))
                self.check_separate_source(params, state, lib)

    def check_separate_source(self, params, state, lib):
        done = self.run_script('host/check-host.sh', '--dry-run', '--params', str(params))
        out = done.stdout
        self.assertEqual(done.returncode, 1, out + done.stderr)
        self.assertIn('ATTENZIONE: non eseguito come root', out)
        self.assertIn(f"ok: l'utente {FAKE_AGENT} esiste", out)
        self.assertIn(f'ERRORE: {lib}/devloop-pinned non esiste (setup-devloop.sh, dal tag '
                      'v1.2.0)', out)
        self.assertIn(f'ok: {self.devloop}/devloop-source è un clone della sorgente', out)
        self.assertIn(f'[dry-run] sudo -u {ME} -H -- test -r {state}/run.log', out)
        self.assertIn(f'[dry-run] sudo -u {ME} -H -- env -C {self.devloop}/ssdlc /usr/bin/python3 '
                      f'{lib}/devloop-pinned --state-dir {state} apps', out)
        self.assertEqual(self.mutating_calls(), [])


@unittest.skipIf(BASH is None, 'bash is not installed')
class FakeHostTests(OpsTestCase):
    """The fakes of ``fake_host`` (#497): the scripts see the simulated host, whoever runs the
    tests and whatever sudo the host has."""

    def sh(self, command):
        env = dict(os.environ, PATH=f'{self.bin}:{os.environ.get("PATH", "/usr/bin:/bin")}',
                   OPS_TEST_CALLS=str(self.calls))
        return subprocess.run([BASH, '-c', command], capture_output=True, text=True, env=env,
                              timeout=60)

    def test_the_fakes_simulate_an_unprivileged_user_and_either_sudo(self):
        for sudo, version in SUDO_FLAVORS.items():
            with self.subTest(sudo):
                self.fake_host(sudo, users=(FAKE_AGENT,))
                for command, out in [('id -u', '1000'), ('id -un', ME), (f'id -gn {ME}', GROUP),
                                     (f'id -nG {ME}', GROUP), (f'id -u {FAKE_AGENT}', '1001'),
                                     (f'id -gn {FAKE_AGENT}', FAKE_AGENT),
                                     (f'getent group {GROUP}', f'{GROUP}:x:1000:'),
                                     ('sudo --version', version), ('getfacl -p /x', 'user::rwx\n'
                                                                   'group::r-x\nother::---')]:
                    done = self.sh(command)
                    self.assertEqual((done.returncode, done.stdout.strip()), (0, out), command)
                # A user or a group the simulated host does not have, whatever the real one has.
                others = [u for u in ('root', 'nobody', 'devloop', 'devloop-agent', 'supervisor')
                          if u != ME]
                for command in [f'id -u {u}' for u in others] + ['getent group nogroup']:
                    self.assertNotEqual(self.sh(command).returncode, 0, command)
                self.assertEqual(self.sh('sudo -u x -H -- true').returncode, 0)
        self.assertEqual(self.mutating_calls(), ['sudo -u x -H -- true'] * len(SUDO_FLAVORS))


# The runbooks of the roles (#531), which start-role.sh gives as the first message.
ROLE_RUNBOOKS = {role: ROOT / 'docs' / 'devloop' / 'roles' / f'{role}.md'
                 for role in ('supervisor', 'coordinator', 'host')}
FAKE_OPERATOR = 'opsdry-operator'
# The fake of a CLI: its name, the user it runs as (the fake sudo says it), its PATH and its
# arguments, separated by NUL, in cli-args of the directory it runs in.
FAKE_CLI = '''#!/bin/sh
printf '%s\\0' "$(basename "$0")" "${OPS_TEST_AS:-}" "$PATH" "$@" > "$PWD/cli-args"
'''


@unittest.skipIf(BASH is None, 'bash is not installed')
class StartRoleTests(OpsTestCase):
    """ops/host/start-role.sh (#531): the session of a role in tmux, as the role's user, with the
    CLI and the model of its table of ``[roles]`` (for host, the operator's choice) and its
    runbook as the first message. It runs from a copy of the checkout, so that the session of the
    agent of the host, which starts in the checkout, writes there. The host is simulated:
    ``sudo`` and ``tmux`` run the command they get, and each CLI is a fake in the role user's
    ``~/.local/bin`` that records what it received."""

    OVERLAY = ('[roles.supervisor]\nname = "claude-supervisor"\nkind = "claude"\n'
               'provider = "anthropic"\nmodel = "claude-opus-5-5"\n\n'
               '[roles.coordinator]\nname = "codex-coordinator"\nkind = "codex"\n'
               'provider = "openai"\nmodel = "gpt-6.1-sol"\n')

    def setUp(self):
        super().setUp()
        self.fake_host('classic sudo', users=(FAKE_SUPERVISOR, FAKE_COORDINATOR, FAKE_OPERATOR))
        # sudo -u USER -H -- CMD runs CMD, saying the user; tmux has no session, unless the test
        # says so, and runs the command of new-session in the directory of -c.
        self.shim('sudo', '''echo "sudo $1 $2 $3 $4 $5 $6 $7 $8 $9 ${10}" >> "$OPS_TEST_CALLS"
[ "$1" = -u ] || exit 97
user=$2; shift 2
[ "$1" = -H ] && shift
[ "$1" = -- ] && shift
OPS_TEST_AS=$user exec "$@"''')
        self.shim('tmux', '''echo "tmux $1 $2 $3 $4 $5 $6 $7" >> "$OPS_TEST_CALLS"
case $1 in
    has-session) [ -n "${OPS_TEST_SESSION:-}" ] && exit 0; exit 1 ;;
    new-session)
        shift
        while [ $# -gt 0 ]; do
            case $1 in
                -d) shift ;;
                -s) shift 2 ;;
                -c) dir=$2; shift 2 ;;
                *) break ;;
            esac
        done
        cd "$dir" && exec "$@" ;;
esac
exit 2''')
        self.operator = self.home / 'operator'
        self.shim('getent', f'''if [ "$1" = passwd ] && [ "$2" = {FAKE_OPERATOR} ]; then
    echo "{FAKE_OPERATOR}:x:1002:1002::{self.operator}:/bin/bash"; exit 0
fi
[ "$1" = group ] && [ $# -eq 2 ] || exit 2
echo "$2:x:1000:"''')
        self.checkout = self.tmp / 'checkout'
        for name in ('ops/lib.sh', 'ops/host/start-role.sh', 'devloop/config.toml',
                     *(f'docs/devloop/roles/{role}.md' for role in ROLE_RUNBOOKS)):
            (self.checkout / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, self.checkout / name)
        self.state = self.tmp / 'state'
        self.state.mkdir()
        self.overlay = self.state / 'config.local.toml'
        self.overlay.write_text(self.OVERLAY)
        self.clone = self.home / 'supervisor' / 'ssdlc'
        self.clone.mkdir(parents=True)
        (self.home / 'coordinator').mkdir()
        for home in (self.home / 'supervisor', self.home / 'coordinator', self.operator):
            for cli in ('claude', 'codex', 'agy'):
                path = home / '.local' / 'bin' / cli
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(FAKE_CLI)
                path.chmod(0o755)
        self.params_file = self.params(SUPERVISOR_USER=FAKE_SUPERVISOR,
                                       SUPERVISOR_CLONE=str(self.clone),
                                       STATE_DIR=str(self.state))

    def start(self, *args, sudo_user=None, session=False):
        env = dict(os.environ, PATH=f'{self.bin}:{os.environ.get("PATH", "/usr/bin:/bin")}',
                   OPS_TEST_CALLS=str(self.calls), OPS_OS_RELEASE=str(self.release),
                   OPS_ARCH='aarch64', LC_ALL='C.UTF-8')
        for name in ('SUDO_USER', 'OPS_TEST_SESSION', 'OPS_TEST_AS'):
            env.pop(name, None)
        if sudo_user is not None:
            env['SUDO_USER'] = sudo_user
        if session:
            env['OPS_TEST_SESSION'] = '1'
        return subprocess.run([BASH, str(self.checkout / 'ops/host/start-role.sh'), '--params',
                               str(self.params_file), *args], capture_output=True, text=True,
                              env=env, stdin=subprocess.DEVNULL, timeout=120)

    def cli_args(self, directory):
        """What the fake CLI received: its name, its user, its PATH and its arguments."""
        name, user, path, *args = (directory / 'cli-args').read_text().split('\0')[:-1]
        return name, user, path, args

    def assert_prompt(self, prompt, role):
        self.assertTrue(prompt.startswith(f'You hold the role "{role}" of devloop for '
                                          'brushtyler/ssdlc. Your instructions are the runbook '
                                          f'below (docs/devloop/roles/{role}.md'), prompt[:200])
        self.assertTrue(prompt.endswith(ROLE_RUNBOOKS[role].read_text().rstrip('\n')))

    def launches(self):
        """The sessions tmux started."""
        return [c for c in self.mutating_calls() if c.startswith('tmux new-session')]

    def test_the_supervisor_starts_with_the_cli_and_the_model_of_its_table(self):
        done = self.start('supervisor')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        name, user, path, args = self.cli_args(self.clone)
        self.assertEqual((name, user), ('claude', FAKE_SUPERVISOR))
        self.assertTrue(path.startswith(f'{self.home}/supervisor/.local/bin:'), path)
        self.assertEqual(args[:2], ['--model', 'claude-opus-5-5'])
        self.assertEqual(len(args), 3)
        self.assert_prompt(args[2], 'supervisor')
        calls = [c.rstrip() for c in self.mutating_calls()]
        self.assertIn(f'sudo -u {FAKE_SUPERVISOR} -H -- tmux has-session -t =devloop-supervisor',
                      calls)
        self.assertIn(f'tmux new-session -d -s devloop-supervisor -c {self.clone} env', calls)
        self.assertIn(f'sudo -u {FAKE_SUPERVISOR} -H tmux attach -t devloop-supervisor',
                      done.stdout)
        # The runbook is not printed: its name stands for the first message.
        self.assertIn('claude --model claude-opus-5-5 <istruzioni: '
                      'docs/devloop/roles/supervisor.md>', done.stdout)
        self.assertNotIn('## Prohibitions', done.stdout)

    def test_the_coordinator_starts_with_the_cli_and_the_model_of_its_table(self):
        done = self.start('coordinator')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        name, user, path, args = self.cli_args(self.home / 'coordinator')
        self.assertEqual((name, user), ('codex', FAKE_COORDINATOR))
        self.assertTrue(path.startswith(f'{self.home}/coordinator/.local/bin:'), path)
        self.assertEqual(args[:2], ['--model', 'gpt-6.1-sol'])
        self.assert_prompt(args[2], 'coordinator')

    def test_the_overlay_sets_the_table_over_the_installation(self):
        # The installation's table, with the overlay's model over it.
        config = self.checkout / 'devloop/config.toml'
        config.write_text(config.read_text().replace(
            '\n[roles]\n', '\n[roles.supervisor]\nname = "claude-supervisor"\nkind = "claude"\n'
            'provider = "anthropic"\nmodel = "claude-opus-5-5"\n\n[roles]\n', 1))
        self.overlay.write_text('[roles.supervisor]\nmodel = "claude-sonnet-5-5"\n')
        done = self.start('supervisor')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        name, _, _, args = self.cli_args(self.clone)
        self.assertEqual((name, args[:2]), ('claude', ['--model', 'claude-sonnet-5-5']))

    def test_the_agent_of_the_host_runs_the_cli_and_the_model_of_the_operator(self):
        done = self.start('host', '--cli', 'agy', '--model', 'gemini-4-argon-high',
                          sudo_user=FAKE_OPERATOR)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        # In the checkout the script comes from, as the operator's user, from its home.
        name, user, path, args = self.cli_args(self.checkout)
        self.assertEqual((name, user), ('agy', FAKE_OPERATOR))
        self.assertTrue(path.startswith(f'{self.operator}/.local/bin:'), path)
        self.assertEqual(args[:3], ['--model', 'gemini-4-argon-high', '-i'])
        self.assert_prompt(args[3], 'host')
        self.assertEqual(len(self.launches()), 1)
        self.assertIn(f'-c {self.checkout} env', self.launches()[0])

    def test_dry_run_starts_nothing(self):
        done = self.start('--dry-run', 'supervisor')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('[dry-run]', done.stdout)
        self.assertIn('claude --model claude-opus-5-5 <istruzioni: '
                      'docs/devloop/roles/supervisor.md>', done.stdout)
        self.assertEqual(self.mutating_calls(), [])
        self.assertFalse((self.clone / 'cli-args').exists())

    def refused(self, *args, says, **kwargs):
        done = self.start(*args, **kwargs)
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn(says, done.stdout + done.stderr)
        self.assertEqual(self.launches(), [])
        for directory in (self.clone, self.home / 'coordinator', self.checkout):
            self.assertFalse((directory / 'cli-args').exists(), directory)

    def test_without_the_table_nothing_starts(self):
        self.overlay.write_text('')
        self.refused('supervisor', says='manca la tabella [roles.supervisor]')
        self.refused('coordinator', says='manca la tabella [roles.coordinator]')

    def test_a_supervisor_that_is_an_agent_key_is_refused(self):
        self.overlay.write_text('[roles]\nsupervisor = "claude"\n')
        self.refused('supervisor', says='roles.supervisor non è una tabella')

    def test_the_table_decides_cli_and_model(self):
        self.refused('supervisor', '--cli', 'codex', says='--cli e --model valgono solo per host')
        self.overlay.write_text(self.OVERLAY.replace('kind = "claude"', 'kind = "bash"'))
        self.refused('supervisor', says="CLI sconosciuta: 'bash'")
        self.overlay.write_text(self.OVERLAY.replace('"claude-opus-5-5"', '"x; rm -rf /"'))
        self.refused('supervisor', says='modello non valido')

    def test_the_agent_of_the_host_needs_cli_model_and_the_operators_user(self):
        self.refused('host', '--model', 'm', sudo_user=FAKE_OPERATOR,
                     says="per host CLI e modello li sceglie l'operatore")
        self.refused('host', '--cli', 'claude', '--model', 'm', sudo_user='root',
                     says='non come root')
        self.refused('host', '--cli', 'claude', '--model', 'm', sudo_user=FAKE_SUPERVISOR,
                     says='SUPERVISOR_USER')

    def test_an_existing_session_is_not_started_again(self):
        self.refused('supervisor', session=True, says='la sessione devloop-supervisor')


class RoleRunbookTests(unittest.TestCase):
    """The runbooks of the roles (#531) and the documents that cite them."""

    def test_the_runbooks_exist_with_inputs_privileges_tasks_and_prohibitions(self):
        for role, path in ROLE_RUNBOOKS.items():
            text = path.read_text()
            with self.subTest(role=role):
                for heading in ('## Inputs', '## Privileges', '## Tasks', '## Prohibitions'):
                    self.assertIn(f'\n{heading}\n', text)
                self.assertIn('valid for any CLI', ' '.join(text.split()))
                self.assertIn(f'ops/host/start-role.sh {role}', text)

    def test_the_agent_of_the_host_runs_only_main_and_reads_no_agent_text(self):
        host = ' '.join(ROLE_RUNBOOKS['host'].read_text().split())
        self.assertIn('**Only** these: - the procedures and the scripts already on `main`', host)
        self.assertIn('**Never** the text of issues, pull requests, comments, reviews, or the '
                      'output of the agents', host)
        self.assertIn('Check that the checkout is on `main` and equal to `origin/main`', host)
        self.assertIn('**No** writing on GitHub', host)

    def test_the_supervisor_may_grant_budget_and_capacity_only_through_their_delegations(self):
        # ADR 0005, D2 and D4: grant-budget and capacity are delegations of the supervisor,
        # reserved to the operator only for the coordinator (ADR 0002, 2026-11-22).
        supervisor = ' '.join(ROLE_RUNBOOKS['supervisor'].read_text().split())
        tasks, prohibitions = supervisor.split('## Prohibitions')
        tasks = tasks.split('## Tasks')[1]
        self.assertIn('Only when the operator turned the delegation on, and only when every one '
                      'of its conditions holds: `grant-budget`', tasks)
        self.assertIn('`[delegations] budget`', tasks)
        self.assertIn('`[delegations] capacity`', tasks)
        self.assertIn('never on a pull request implemented by your own agent', tasks)
        reserved = prohibitions.split('Never use a command reserved to the operator:')[1]
        reserved = reserved.split('.')[0]
        self.assertNotIn('grant-budget', reserved)
        self.assertNotIn('capacity', reserved)
        self.assertIn('`grant-budget` stays the operator\'s on the pull requests of your own '
                      'agent', prohibitions)
        coordinator = ' '.join(ROLE_RUNBOOKS['coordinator'].read_text().split())
        coordinator_prohibitions = coordinator.split('## Prohibitions')[1]
        self.assertIn('`grant-budget`, `capacity`, `reconcile` and the keys of trust are the '
                      'operator\'s in person', coordinator_prohibitions)

    def test_the_documents_cite_the_runbooks(self):
        readme = (OPS / 'README.md').read_text()
        guide = (ROOT / 'docs' / 'devloop' / 'guide.md').read_text()
        for role in ROLE_RUNBOOKS:
            with self.subTest(role=role):
                self.assertIn(f'(../docs/devloop/roles/{role}.md)', readme)
                self.assertIn(f'(roles/{role}.md)', guide)
        # The index of devloop's documents leads to them through the guide.
        self.assertIn('their runbooks in [the guide](devloop/guide.md#who-does-what)',
                      (ROOT / 'docs' / 'devloop.md').read_text())
        # The agent of the host is cited where the steps on the host are described.
        self.assertIn('(../../docs/devloop/roles/host.md)', (OPS / 'host/migrate.md').read_text())
        self.assertIn('\n## I quattro ruoli\n', readme)
        self.assertIn('sudo bash ops/host/start-role.sh supervisor', readme)
        self.assertIn('start-role.sh            avvia in tmux', readme)


if __name__ == '__main__':
    unittest.main()
