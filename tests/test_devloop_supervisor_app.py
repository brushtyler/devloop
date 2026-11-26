"""devloop: the supervisor's role app (#408, ADR 0005, P5-4).

``[roles.supervisor]`` as a table is the supervisor actor (``claude-supervisor``), outside
``[agents]``, which signs with the role app ``[apps.supervisor]``: the commands, labels and
requests of that app count as the supervisor's instead of those of an agent's app. Off by
default (D10): with ``roles.supervisor`` naming an agent nothing changes. devloop never reads the
app's key; ``scripts/agent-token.py`` does not expose it, and the supervisor mints its tokens with
``scripts/supervisor-token.py``.
"""

import dataclasses
import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import devloop.__main__ as cli
from devloop import apps, config, token
from devloop.config import AppConfig, ConfigError, SupervisorActor
from devloop.dispatcher import Dispatcher
from devloop.watch import Trust
from helpers import ssdlc_project
from test_devloop import CFG, FakeGH, FakeIdentities, FakeRunner, FakeWS, pr, raw_review
from test_devloop_supervision import AUTO, HUMAN, REVIEW, SUPER, Case, crash

ROOT = Path(__file__).resolve().parents[1]
DATA = ssdlc_project.data()
SUPERVISOR_APP = 'brushtyler-devloop-supervisor[bot]'
AGENT_APP = 'agy-agent[bot]'  # the app of the agent the supervisor of AUTO names
TABLE = {'name': 'agy-supervisor', 'kind': 'agy', 'provider': 'google',
         'model': 'gemini-4-argon-high'}
# The supervisor actor run by agy: neither the implementer (claude) nor its model.
ACTOR = SupervisorActor(**TABLE)
ROLE = dataclasses.replace(AUTO, supervisor=ACTOR.name, supervisor_actor=ACTOR)
# The same actor run by claude, the agent that implements the PRs of the tests.
OWN_ACTOR = SupervisorActor('claude-supervisor', 'claude', 'anthropic', 'claude-sonnet-5-5')
OWN = dataclasses.replace(AUTO, supervisor=OWN_ACTOR.name, supervisor_actor=OWN_ACTOR)


def script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'),
                                                  ROOT / 'scripts' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def with_roles(**roles):
    return dict(DATA, roles=dict(DATA['roles'], **roles))


class ConfigTests(unittest.TestCase):
    def test_off_by_default_with_the_app_declared(self):
        cfg = ssdlc_project.load()
        self.assertEqual((cfg.supervisor, cfg.supervisor_actor), ('', None))
        self.assertEqual(cfg.apps['supervisor'],
                         AppConfig(5824243, 'brushtyler-devloop-supervisor'))

    def test_the_table_describes_the_supervisor_actor(self):
        cfg = config.parse_config(with_roles(supervisor=dict(TABLE)))
        self.assertEqual((cfg.supervisor, cfg.supervisor_actor), ('agy-supervisor', ACTOR))
        self.assertNotIn('agy-supervisor', cfg.agents)
        # It is a supervisor for the merge modes, as an agent key was.
        auto = dict(with_roles(supervisor=dict(TABLE)), merge={'mode': 'auto'})
        self.assertEqual(config.parse_config(auto).merge.mode, 'auto')

    def test_the_overlay_turns_it_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / 'config.local.toml'
            local.write_text('[roles.supervisor]\nname = "claude-supervisor"\nkind = "claude"\n'
                             'provider = "anthropic"\nmodel = "claude-opus-5-5"\n')
            cfg = ssdlc_project.load(local)
        self.assertEqual(cfg.supervisor_actor,
                         SupervisorActor('claude-supervisor', 'claude', 'anthropic',
                                         'claude-opus-5-5'))
        self.assertEqual(cfg.implementer, 'claude')  # the other roles are untouched

    def test_the_agent_key_of_before_still_works(self):
        cfg = config.parse_config(with_roles(supervisor='claude'))
        self.assertEqual((cfg.supervisor, cfg.supervisor_actor), ('claude', None))
        with self.assertRaisesRegex(ConfigError, "roles.supervisor: unknown agent 'nobody'"):
            config.parse_config(with_roles(supervisor='nobody'))

    def test_invalid_tables_stop_devloop(self):
        for change, error in (
                ({'name': 'claude-supervisor'}, "name: must be 'agy-supervisor'"),
                ({'name': 'agy'}, "name: must be 'agy-supervisor'"),
                ({'kind': 'gemini'}, 'kind: must be one of'),
                ({'provider': ' '}, 'provider: must not be empty'),
                ({'model': ''}, 'model: must not be empty'),
                ({'model': 5}, 'model: expected str'),
                ({'effort': 'high'}, "unknown key 'effort'")):
            with self.subTest(change=change), self.assertRaisesRegex(ConfigError, error):
                config.parse_config(with_roles(supervisor=dict(TABLE, **change)))
        for key in ('name', 'kind', 'provider', 'model'):
            table = {k: v for k, v in TABLE.items() if k != key}
            with self.subTest(missing=key), self.assertRaisesRegex(ConfigError, f"'{key}'"):
                config.parse_config(with_roles(supervisor=table))
        with self.assertRaisesRegex(ConfigError, 'an agent or a table'):
            config.parse_config(with_roles(supervisor=['claude']))

    def test_the_actor_needs_its_role_app(self):
        without = {k: v for k, v in DATA['apps'].items() if k != 'supervisor'}
        with self.assertRaisesRegex(ConfigError, r'needs \[apps.supervisor\]'):
            config.parse_config(dict(with_roles(supervisor=dict(TABLE)), apps=without))
        # Declared without the actor, the app is only declared.
        self.assertIsNone(config.parse_config(dict(DATA, apps=DATA['apps'])).supervisor_actor)

    def test_no_agent_is_the_supervisor(self):
        agents = dict(DATA['agents'], **{'agy-supervisor': dict(DATA['agents']['agy'])})
        with self.assertRaisesRegex(ConfigError, 'not run by devloop'):
            config.parse_config(dict(with_roles(supervisor=dict(TABLE)), agents=agents))
        agents = dict(DATA['agents'], supervisor=dict(DATA['agents']['agy']))
        with self.assertRaisesRegex(ConfigError, 'agents.supervisor: .*reserved'):
            config.parse_config(dict(DATA, agents=agents))

    def test_the_role_app_is_its_own(self):
        apps_table = dict(DATA['apps'], supervisor={'app_id': DATA['apps']['claude']['app_id'],
                                                    'slug': 'x'})
        with self.assertRaisesRegex(ConfigError, 'each actor needs its own app'):
            config.parse_config(dict(DATA, apps=apps_table))


class IdentityTests(unittest.TestCase):
    def test_devloop_never_reads_the_supervisors_key(self):
        cfg = ssdlc_project.load()
        with tempfile.TemporaryDirectory() as tmp:
            for actor, app in cfg.apps.items():
                if actor != apps.SUPERVISOR:
                    key = Path(tmp) / f'{app.slug}.2026-11-19.private-key.pem'
                    key.write_text('k')
                    key.chmod(0o600)
            with mock.patch.dict(os.environ, {'DEVLOOP_APP_KEY_DIR': tmp}):
                identities = apps.Identities.from_config(cfg)
        self.assertNotIn(apps.SUPERVISOR, identities.apps)
        self.assertIsNone(identities.login(apps.SUPERVISOR))
        self.assertNotIn(SUPERVISOR_APP, identities.agent_logins())

    def test_the_watcher_trusts_the_role_app_only_when_it_is_the_supervisor(self):
        self.assertNotIn(SUPERVISOR_APP, Trust.from_config(CFG).apps)
        self.assertIn(SUPERVISOR_APP, Trust.from_config(ROLE).apps)
        # #682: only the role app makes a comment a "supervisor comment"; the agent's app the
        # supervisor of AUTO signs with is shared with the agents' turns.
        self.assertEqual(Trust.from_config(ROLE).roles, {SUPERVISOR_APP: 'supervisor'})
        self.assertEqual(Trust.from_config(AUTO).roles, {})


class IdentityOfTheSupervisorTests(unittest.TestCase):
    def make(self, cfg):
        gh = FakeGH()
        return Dispatcher(cfg, gh, FakeWS(), FakeRunner(), log=lambda m: None,
                          identities=FakeIdentities(gh))

    def test_the_role_app_replaces_the_agents_app(self):
        role = self.make(ROLE)
        self.assertEqual(role._supervisor_login(), SUPERVISOR_APP)
        self.assertTrue(role._may_hold(SUPERVISOR_APP))
        self.assertFalse(role._may_hold(AGENT_APP))
        self.assertTrue(role._may_hold('brushtyler'))
        self.assertFalse(role._may_hold('stranger'))
        # With the agent key of before, the agent's app, as before.
        legacy = self.make(AUTO)
        self.assertEqual(legacy._supervisor_login(), AGENT_APP)
        self.assertTrue(legacy._may_hold(AGENT_APP))
        self.assertFalse(legacy._may_hold(SUPERVISOR_APP))
        self.assertIsNone(self.make(CFG)._supervisor_login())

    def test_the_role_app_admits_issues_of_trusted_users_and_of_devloop(self):
        d = self.make(ROLE)
        self.assertIn(SUPERVISOR_APP, d._admitters('brushtyler'))
        self.assertIn(SUPERVISOR_APP, d._admitters('codex-agent[bot]'))
        # An issue the supervisor opened with its app is one of devloop's.
        self.assertIn(SUPERVISOR_APP, d._admitters(SUPERVISOR_APP))
        self.assertTrue(d._devloop_author(SUPERVISOR_APP))
        self.assertEqual(d._admitters('stranger'), set(CFG.trusted_users))
        self.assertNotIn(AGENT_APP, d._admitters('brushtyler'))
        # Not the supervisor: the role app admits nothing and authors no devloop issue.
        legacy = self.make(AUTO)
        self.assertNotIn(SUPERVISOR_APP, legacy._admitters('brushtyler'))
        self.assertFalse(legacy._devloop_author(SUPERVISOR_APP))


class OwnPRTests(unittest.TestCase):
    def implemented(self, cfg, meta=None):
        gh = FakeGH()
        d = Dispatcher(cfg, gh, FakeWS(), FakeRunner(), log=lambda m: None)
        change = pr()
        if meta is not None:
            change.meta = meta
        return d._supervisor_implemented(change)

    def test_the_agent_key_of_before_compares_keys(self):
        self.assertTrue(self.implemented(dataclasses.replace(AUTO, supervisor=CFG.implementer)))
        self.assertFalse(self.implemented(AUTO))

    def test_the_actor_compares_the_agent_that_ran_never_the_key(self):
        # claude-supervisor is never a key of [agents], yet claude implemented the PR.
        self.assertTrue(self.implemented(OWN))
        self.assertFalse(self.implemented(ROLE))
        # The same model is the same agent, whatever the CLI.
        same_model = dataclasses.replace(ACTOR, model=CFG.agent(CFG.implementer).model)
        self.assertTrue(self.implemented(dataclasses.replace(ROLE, supervisor_actor=same_model)))

    def test_the_profile_in_the_marker_is_what_ran(self):
        profile = {'identity': None, 'kind': 'agy', 'model': 'gemini-4-argon-high',
                   'effort': 'high', 'args': []}
        meta = dict(pr().meta, profile=profile)
        self.assertTrue(self.implemented(ROLE, meta))
        self.assertFalse(self.implemented(OWN, meta))

    def test_an_agent_that_cannot_be_read_counts_as_the_supervisors(self):
        self.assertTrue(self.implemented(ROLE, dict(pr().meta, implementer='gone')))
        self.assertTrue(self.implemented(ROLE, dict(pr().meta, implementer='gone',
                                                    profile={'kind': 'codex'})))


class CommandTests(Case):
    def fail_review(self, d, label):
        self.assertEqual(self.tick(d), [('review', 100)])
        self.assertEqual(self.gh.labels[100], {label})

    def test_only_the_role_app_commands_as_the_supervisor(self):
        d = self.make(ROLE, crash(), raw_review())
        self.fail_review(d, SUPER)
        # The app of the agent that used to be the supervisor no longer counts.
        self.gh.command(100, '/devloop retry', login=AGENT_APP)
        self.assertEqual(self.tick(d), [])
        self.assertEqual(self.decisions(100), [])
        self.assertTrue(any(f'ignored `/devloop retry` by {AGENT_APP}' in line
                            for line in self.logs))
        self.gh.command(100, '/devloop retry', login=SUPERVISOR_APP)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        (decision,) = self.decisions(100)
        self.assertEqual((decision['by'], decision['outcome']), (SUPERVISOR_APP, 'applied'))

    def test_a_pr_of_the_supervisors_agent_goes_to_the_supervisor(self):
        # A retry is an escalation of process (#527): the role app decides it on its agent's
        # PRs too.
        d = self.make(OWN, crash(), raw_review())
        self.fail_review(d, SUPER)
        self.gh.command(100, '/devloop retry', login=SUPERVISOR_APP)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'applied')


class AgentTokenTests(unittest.TestCase):
    def test_the_supervisors_app_is_not_exposed(self):
        # devloop token (ADR 0006, R6), which scripts/agent-token.py calls.
        for actor in ('supervisor', 'SUPERVISOR'):
            with self.subTest(actor=actor), \
                    mock.patch.object(cli, 'load') as load, \
                    mock.patch.object(token.subprocess, 'run') as run:
                with self.assertRaisesRegex(SystemExit, 'scripts/supervisor-token.py'):
                    cli.main(['token', actor, '--', 'gh', 'issue', 'comment', '76'])
                load.assert_not_called()
                run.assert_not_called()


class SupervisorTokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = script('supervisor-token')

    def setUp(self):
        # The project's state_dir, with ssdlc's overlay (ADR 0006, R6): repo and [apps].
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state)
        shutil.copy(ssdlc_project.OVERLAY, self.state / 'config.local.toml')
        # A GH_REPO of the environment running the tests would change what the script refuses.
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop('GH_REPO', None)

    def test_permissions(self):
        state = self.script.STATE
        self.assertEqual(self.script.parse(['--', 'gh', 'x']),
                         (state, {'issues': 'write', 'pull_requests': 'write'}, ['gh', 'x']))
        self.assertEqual(self.script.parse(['--perm', 'issues=read', '--', 'gh', 'x']),
                         (state, {'issues': 'read'}, ['gh', 'x']))
        self.assertEqual(self.script.parse(['--state-dir', '/srv/p', '--', 'gh', 'x']),
                         (Path('/srv/p'), {'issues': 'write', 'pull_requests': 'write'},
                          ['gh', 'x']))
        # #643: the supervisor reads the CI with its app, read only and only when asked.
        self.assertEqual(self.script.parse(['--perm', 'actions=read', '--perm', 'checks=read',
                                            '--perm', 'statuses=read', '--', 'gh', 'x']),
                         (state, {'actions': 'read', 'checks': 'read', 'statuses': 'read'},
                          ['gh', 'x']))
        # The supervisor neither implements nor merges: no contents, no other permission.
        for perm in ('contents=read', 'contents=write', 'actions=write', 'checks=write',
                     'statuses=write', 'administration=write', 'metadata=write'):
            with self.subTest(perm=perm), self.assertRaisesRegex(SystemExit, 'not allowed'):
                self.script.parse(['--perm', perm, '--', 'gh', 'x'])
        for argv in ([], ['--'], ['--help'], ['gh', 'x'], ['--perm', '--', 'gh']):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.script.parse(argv)

    def test_only_the_supervisors_key_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / 'brushtyler-devloop-supervisor.2026-11-18.private-key.pem'
            key.write_text('k')
            key.chmod(0o600)
            seen = {}

            def fake_run(command, env):
                seen.update(command=command, token=env['GH_TOKEN'])
                return SimpleNamespace(returncode=0)

            def token(app, permissions):
                seen.update(app=(app.app_id, app.slug, app.key_path), permissions=permissions)
                return 'sup-tok'
            with mock.patch.dict(os.environ, {'DEVLOOP_APP_KEY_DIR': tmp, 'GH_TOKEN': 'op'}), \
                    mock.patch.object(self.script.AppAuth, 'token', token), \
                    mock.patch.object(self.script.subprocess, 'run', fake_run):
                self.assertEqual(self.script.main(['--state-dir', str(self.state), '--',
                                                   'gh', 'x']), 0)
        self.assertEqual(seen, {'command': ['gh', 'x'], 'token': 'sup-tok',
                                'app': (5824243, 'brushtyler-devloop-supervisor', key),
                                'permissions': {'issues': 'write', 'pull_requests': 'write'}})

    def test_the_repository_is_the_projects(self):
        # ADR 0006, R6: repo and the app come from the overlay of the state_dir given.
        (self.state / 'config.local.toml').write_text(
            (self.state / 'config.local.toml').read_text(encoding='utf-8').replace(
                'repo = "brushtyler/ssdlc"', 'repo = "brushtyler/widget"'), encoding='utf-8')
        cfg = self.script.load_config(self.state)
        self.assertEqual(cfg.repo, 'brushtyler/widget')
        self.assertEqual(cfg.apps[apps.SUPERVISOR].slug, 'brushtyler-devloop-supervisor')

    def test_gh_acts_on_the_projects_repository_not_on_the_clones(self):
        # #571: with the source apart, the supervisor runs the script from its clone of the
        # source, whose origin is not the project's repository; gh would pick the origin.
        overlay = self.state / 'config.local.toml'
        overlay.write_text(overlay.read_text(encoding='utf-8').replace(
            'repo = "brushtyler/ssdlc"', 'repo = "acme/widget"'), encoding='utf-8')
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / 'brushtyler-devloop-supervisor.2026-11-18.private-key.pem'
            key.write_text('k')
            key.chmod(0o600)
            for given in (None, 'acme/widget', 'ACME/Widget', 'github.com/acme/widget'):
                seen = {}

                def fake_run(command, env):
                    seen['repo'] = env.get('GH_REPO')
                    return SimpleNamespace(returncode=0)
                environ = {'DEVLOOP_APP_KEY_DIR': tmp}
                if given:
                    environ['GH_REPO'] = given
                with self.subTest(given=given), mock.patch.dict(os.environ, environ), \
                        mock.patch.object(self.script.AppAuth, 'token', return_value='sup-tok'), \
                        mock.patch.object(self.script.subprocess, 'run', fake_run):
                    self.assertEqual(self.script.main(['--state-dir', str(self.state), '--',
                                                       'gh', 'issue', 'view', '1']), 0)
                    self.assertEqual(seen, {'repo': 'acme/widget'})
            # A GH_REPO naming another repository, the source's for example, is refused before
            # any key is read: the token would not reach it.
            with mock.patch.dict(os.environ, {'DEVLOOP_APP_KEY_DIR': tmp,
                                              'GH_REPO': 'brushtyler/devloop'}), \
                    mock.patch.object(self.script, 'find_key') as find, \
                    mock.patch.object(self.script.AppAuth, 'token') as mint, \
                    mock.patch.object(self.script.subprocess, 'run') as run:
                with self.assertRaisesRegex(SystemExit, 'GH_REPO=brushtyler/devloop is not the '
                                            "project's repository acme/widget"):
                    self.script.main(['--state-dir', str(self.state), '--', 'gh', 'x'])
                find.assert_not_called()
                mint.assert_not_called()
                run.assert_not_called()

    def test_no_key_in_an_agents_environment(self):
        # devloop's agents run with a key directory without keys: they cannot mint it.
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {'DEVLOOP_APP_KEY_DIR': tmp}), \
                mock.patch.object(self.script.subprocess, 'run') as run:
            with self.assertRaises(FileNotFoundError):
                self.script.main(['--state-dir', str(self.state), '--', 'gh', 'x'])
            run.assert_not_called()

    def test_without_the_app_it_refuses(self):
        cfg = dataclasses.replace(CFG, apps={k: v for k, v in CFG.apps.items()
                                             if k != apps.SUPERVISOR})
        with mock.patch.object(self.script, 'load_config', return_value=cfg), \
                mock.patch.object(self.script.subprocess, 'run') as run:
            with self.assertRaisesRegex(SystemExit, r'no \[apps.supervisor\]'):
                self.script.main(['--', 'gh', 'x'])
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
