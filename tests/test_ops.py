"""ops/ (issue #402): guide, scripts and declarative files that recreate devloop's installation.

The scripts run here only with ``--dry-run`` and on temporary directories: a ``PATH`` of shims
records any command that would change the host (``sudo``, ``apt-get``, ``useradd``, …), and the
tests check that none ran.
"""
import getpass
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

from devloop import apps
from devloop.config import load_config

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / 'ops'
SCRIPTS = ('host/setup-devloop.sh', 'host/setup-supervisor.sh', 'host/check-host.sh',
           'github/create-apps.sh')
BASH = shutil.which('bash')
ME = getpass.getuser()
PREFIX = 'brushtyler-devloop-'
DEVLOOP_KEYS = ('dispatcher', 'merger', 'claude-agent', 'codex-agent', 'agy-agent')
SUPERVISOR_KEYS = ('supervisor', 'claude-agent')
# Commands that change the host: in --dry-run none of them may run.
MUTATING = ('sudo', 'apt-get', 'useradd', 'setfacl', 'visudo', 'chown', 'chmod', 'install',
            'git', 'npm', 'runuser', 'touch', 'mkdir', 'systemctl', 'rsync')
LEVEL = {'read': 1, 'write': 2}


def agent_token():
    spec = importlib.util.spec_from_file_location('agent_token', ROOT / 'scripts/agent-token.py')
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
    # The supervisor writes through scripts/agent-token.py, with its default permissions.
    'supervisor': (agent_token().DEFAULT,),
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
        for phrase in ('## Un altro progetto', 'il repository gestito contiene devloop',
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
        cfg = load_config(ROOT / 'devloop/config.toml')
        for actor, app in cfg.apps.items():
            self.assertTrue(app.slug.startswith(PREFIX), actor)
            role = app.slug[len(PREFIX):]
            self.assertIn(role, REQUIRED, actor)
            needs = {apps.DISPATCHER: REQUIRED['dispatcher'],
                     apps.MERGER: REQUIRED['merger']}.get(actor, AGENT)
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
                     'ExecStart=/usr/bin/python3 @DEVLOOP_CLONE@/scripts/devloop-pinned run',
                     'StandardOutput=append:@DEVLOOP_CLONE@/@STATE_DIR@/run.log',
                     'StandardError=append:@DEVLOOP_CLONE@/@STATE_DIR@/run.log',
                     'KillMode=mixed', 'TimeoutStopSec=4200', 'Restart=on-failure',
                     'RestartSec=60'):
            self.assertIn(line + '\n', unit)

    def test_the_sudoers_allows_only_the_listed_commands(self):
        text = (OPS / 'host/sudoers-supervisor').read_text()
        body = [line for line in text.replace('\\\n', ' ').splitlines()
                if line.strip() and not line.startswith('#')]
        self.assertEqual(body[0], 'Defaults>@DEVLOOP_USER@ always_set_home, '
                                  'secure_path="@DEVLOOP_PATH@"')
        as_devloop = [c.strip() for c in body[1].split('NOPASSWD:', 1)[1].split(',')]
        launcher = '/usr/bin/python3 @DEVLOOP_CLONE@/scripts/devloop-pinned '
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
            cfg = load_config(ROOT / 'devloop/config.toml', overlay)
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
        self.assertIn('    |   "autoUpdates": false,', out)
        self.assertIn('da fare a mano: installare claude', out)
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
        (state / 'config.local.toml').write_text('[merge]\nmode = "off"\n')
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
            'STATE_DIR': '.ssdlc/devloop',
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

    def test_another_state_dir_is_refused(self):
        # scripts/devloop-pinned looks for the pinned copy and the overlay in .ssdlc/devloop only:
        # setup and check refuse another state_dir instead of preparing one the launcher ignores.
        for script in ('host/setup-devloop.sh', 'host/setup-supervisor.sh', 'host/check-host.sh'):
            done = self.run_script(script, '--dry-run', '--params',
                                   str(self.params(STATE_DIR='custom-state')))
            self.assertNotEqual(done.returncode, 0, script)
            self.assertIn('STATE_DIR non è configurabile', done.stderr, script)
            self.assertIn('.ssdlc/devloop', done.stderr, script)
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
        for cli in ('claude', 'codex', 'agy'):
            self.exe(self.devloop / '.local/bin' / cli)
        self.exe(self.supervisor / '.local/bin/claude')
        state = self.devloop / 'ssdlc/.ssdlc/devloop'
        (state / 'current').mkdir(parents=True)
        (state / 'run.log').touch()
        (self.tmp / 'systemd').mkdir()
        (self.tmp / 'systemd/devloop.service').write_text(render('host/devloop.service', {
            'REPO': 'brushtyler/ssdlc', 'DEVLOOP_USER': ME,
            'DEVLOOP_CLONE': str(self.devloop / 'ssdlc'), 'STATE_DIR': '.ssdlc/devloop',
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
        params = self.params(DEVLOOP_USER=ME, SUPERVISOR_USER=ME)
        done = self.run_script('host/check-host.sh', '--dry-run', '--params', str(params), *args)
        errors = [line for line in done.stdout.splitlines()
                  if 'ERRORE' in line and 'sudoers' not in line]
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
        self.assertIn(f'{self.devloop}/.local/bin/claude --version', out)
        self.assertRegex(out, r'\[dry-run\] sudo .*/scripts/devloop-pinned apps')
        self.assertEqual(self.mutating_calls(), [])

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


if __name__ == '__main__':
    unittest.main()
