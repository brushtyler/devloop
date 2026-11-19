"""ops/ (issue #402): guide, scripts and declarative files that recreate devloop's installation.

The scripts run here only with ``--dry-run`` and on temporary directories: a ``PATH`` of shims
records any command that would change the host (``sudo``, ``apt-get``, ``useradd``, …), and the
tests check that none ran.
"""
import getpass
import grp
import html
import importlib.util
import json
import os
import re
import shutil
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
           'host/check-host.sh', 'github/create-apps.sh')
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
                     'host/check-host.sh', 'host/migrate.md',
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
                       '`scripts/devloop-pinned`', '`core/`', '`schemas/`', 'devloop/prompts/',
                       '## Migrazione su un\'altra macchina', 'host/migrate.md',
                       'Ubuntu 26.04 LTS su aarch64', 'shellcheck', 'tokens.md'):
            self.assertIn(phrase, guide)
        migrate = (OPS / 'host/migrate.md').read_text()
        for phrase in ('ledger-open-turns.json', 'model-unavailability.json', 'activity.json',
                       'config.local.toml', 'turn-journal.jsonl', '~/supervisor',
                       'memory', 'devloop pin', 'mai due dispatcher attivi', 'systemctl stop'):
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
        cfg = load_config(ROOT / 'devloop/config.toml', None, ssdlc_project.PROJECT)
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
        self.assertEqual({c['context'] for c in checks},
                         {'Unit Tests (Python 3.12)', 'Isolated A5 Recovery Benchmark'})
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
        (state / 'config.local.toml').write_text(f'repo_lock_dir = "{locks}"\n'
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
        params = self.params(DEVLOOP_USER=ME)
        for _ in range(2):
            done = self.run_script('host/setup-devloop.sh', '--dry-run', '--params', str(params))
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            actions = [line for line in done.stdout.splitlines()
                       if '[dry-run]' in line and 'apt-get' not in line]
            self.assertEqual(actions, [])
            self.assertNotIn('da fare a mano', done.stdout)
            self.assertIn('devloop.service è già aggiornato', done.stdout)
        self.assertEqual(self.mutating_calls(), [])

    def test_setup_supervisor_dry_run_prints_the_acl_and_the_sudoers(self):
        (self.home / 'devloop').mkdir(parents=True)
        params = self.params(DEVLOOP_USER=ME, SUPERVISOR_USER='opsdry-nosup')
        before = self.tree()
        done = self.run_script('host/setup-supervisor.sh', '--dry-run', '--params', str(params))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        out = done.stdout
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
        self.assertIn(f'[dry-run] install -d -m 700 -o opsdry-noagent -g opsdry-noagent '
                      f'{agent}/.claude', out)
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
        self.assertIn('da fare a mano: installare claude come opsdry-noagent', out)
        self.assertIn('da fare a mano: impostare [agent_user] name = "opsdry-noagent"', out)
        self.assertNotRegex(out, r'@[A-Z_]+@')
        self.assertEqual(self.mutating_calls(), [])
        self.assertEqual(self.tree(), before)

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
        # (test_the_directory_of_the_repository_locks_is_roots).
        errors = [line for line in done.stdout.splitlines()
                  if 'ERRORE' in line and 'sudoers' not in line
                  and line != f'  ERRORE: {self.locks} è di {ME}, deve essere di root']
        return done, errors

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
        params = self.params(DEVLOOP_USER=ME, SUPERVISOR_USER=ME, REPO='acme/widget',
                             DEVLOOP_CLONE=str(self.devloop / 'ssdlc'),
                             SOURCE_REPO='brushtyler/ssdlc', SOURCE_REF='v1.2.0',
                             STATE_DIR=str(state), LAUNCHER_DIR=str(lib))
        done = self.run_script('host/check-host.sh', '--dry-run', '--params', str(params))
        out = done.stdout
        self.assertEqual(done.returncode, 1, out + done.stderr)
        self.assertIn(f'ERRORE: {lib}/devloop-pinned non esiste (setup-devloop.sh, dal tag '
                      'v1.2.0)', out)
        self.assertIn(f'ok: {self.devloop}/devloop-source è un clone della sorgente', out)
        self.assertIn(f'[dry-run] sudo -u {ME} -H -- test -r {state}/run.log', out)
        self.assertIn(f'[dry-run] sudo -u {ME} -H -- env -C {self.devloop}/ssdlc /usr/bin/python3 '
                      f'{lib}/devloop-pinned --state-dir {state} apps', out)
        self.assertEqual(self.mutating_calls(), [])


if __name__ == '__main__':
    unittest.main()
