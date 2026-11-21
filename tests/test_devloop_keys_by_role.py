"""devloop: the keys by role turned on in config.toml, and claude-reviewer as the reserve of agy
(#499, ADR 0005, P5-3, P5-5, D8).

``devloop/config.toml`` names the agents by role: ``claude-implementer`` with the implementer app,
``codex-reviewer``, ``agy-reviewer`` and, last in the pool, ``claude-reviewer`` with Sonnet and
the app of claude. With ``review_policy.provider_quorum``, which ssdlc's overlay turns on, a new
round of a level 2 PR of ``claude-implementer`` skips agy when every model of its chain is
remembered unavailable, and ``claude-reviewer`` takes its place beside codex; with agy available
the pair is codex and agy, and ``claude-reviewer`` never reviews without a reviewer of another
provider in the round. ``preflight`` and ``conclave``, read-only as a review, take a reviewer key
(the operator's approval on #499), never an implementer key. A local overlay written before, with
the tables of the keys of before, stops devloop at startup saying what to rename.
"""

import dataclasses
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path

from devloop import config, markdown, unavailability
from devloop.config import ConfigError
from devloop.dispatcher import Dispatcher
from helpers import ssdlc_project
from helpers.devloop_fakes import FakeGH
from test_devloop import FakeRunner, FakeWS, implemented, pr
from test_devloop_implementer_app import RoleIdentities, role_meta
from test_devloop_preflight import READY, IssueGH, checked

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000
POOL =('codex-reviewer', 'agy-reviewer', 'claude-reviewer')


def overlay(case, text):
    """A local overlay with ssdlc's identities and the TOML ``text``, for the test ``case``."""
    tmp = Path(tempfile.mkdtemp())
    case.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    path = tmp / 'config.local.toml'
    table = config._merge(ssdlc_project.identity(), tomllib.loads(text))
    path.write_text(ssdlc_project.toml_text(table) + '\n', encoding='utf-8')
    return path


def ssdlc(case, extra=''):
    """config.toml as it is, with ssdlc's project file and an overlay that turns on the reviewer
    rules of P5-5 (ssdlc's), plus the TOML ``extra``."""
    local = overlay(case, config_text(extra, '[review_policy]\nprovider_quorum = true\n'))
    return dataclasses.replace(ssdlc_project.load_by_role(local), max_parallel_actions=1)


def config_text(text, more):
    """The TOML ``text`` and ``more`` as one table."""
    return ssdlc_project.toml_text(config._merge(tomllib.loads(more), tomllib.loads(text)))


def unavailable(tmp, cfg, key):
    """A memory of the unavailable models where every model of ``key`` is remembered."""
    memory = unavailability.Memory(tmp, clock=lambda: NOW)
    for agent in cfg.agent(key).chain():
        memory.record(key, agent.model, 'quota', NOW + 3600)
    return memory


def change(cfg, reviewer='codex-reviewer'):
    """A level 2 PR implemented by claude-implementer, as its marker records it."""
    made = pr()
    made.meta = dict(role_meta(cfg, 'claude-implementer'), reviewer=reviewer)
    return made


class DefaultsTests(unittest.TestCase):
    def test_config_toml_names_the_agents_by_role(self):
        raw = config.read_toml(config.DEFAULT_CONFIG)
        self.assertEqual(sorted(raw['agents']), sorted(('claude-implementer',) + POOL))
        self.assertEqual(raw['roles'], {'implementer': 'claude-implementer',
                                        'reviewers': list(POOL)})
        cfg = ssdlc_project.load_by_role()
        self.assertEqual(cfg.pool(), POOL)
        self.assertEqual({key: cfg.app_of(key) for key in cfg.agents},
                         {'claude-implementer': 'implementer', 'codex-reviewer': 'codex',
                          'agy-reviewer': 'agy', 'claude-reviewer': 'claude'})
        third, implementer = cfg.agent('claude-reviewer'), cfg.agent('claude-implementer')
        self.assertEqual((third.kind, third.model, third.provider),
                         ('claude', 'claude-sonnet-5-5', 'anthropic'))
        self.assertNotEqual(third.model, implementer.model)
        # The table of claude without what only writing turns use.
        self.assertEqual(third.review_args, implementer.review_args)
        self.assertEqual((third.write_args, third.commit_trailer), ((), ''))
        # Every agent and fallback declares its provider: the overlay may turn the rules on.
        for agent in cfg.agents.values():
            for model in agent.chain():
                self.assertTrue(model.provider, model.model)
        # The reserve is off where it belongs: advisory_reviewer is the overlay's.
        self.assertEqual(cfg.review_policy.advisory_reviewer, '')
        self.assertFalse(cfg.review_policy.provider_quorum)
        self.assertEqual(cfg.preflight, '')

    def test_ssdlc_overlay_and_rules_on(self):
        cfg = ssdlc(self, '[review_policy]\nadvisory_reviewer = "claude-reviewer"\n')
        self.assertTrue(cfg.review_policy.provider_quorum)
        self.assertEqual(cfg.review_policy.advisory_reviewer, 'claude-reviewer')
        self.assertIn('python3 scripts/run-tests.py',
                      ssdlc_project.load_by_role(ssdlc_project.OVERLAY)
                      .agent('agy-reviewer').review_commands)


class SelectionTests(unittest.TestCase):
    def make(self, cfg, memory=None):
        self.logs = []
        gh = FakeGH()
        return Dispatcher(cfg, gh, FakeWS(), FakeRunner(), log=self.logs.append,
                          identities=RoleIdentities(gh), memory=memory)

    def test_agy_unavailable_claude_reviewer_takes_its_place(self):
        cfg = ssdlc(self)
        with tempfile.TemporaryDirectory() as tmp:
            d = self.make(cfg, unavailable(tmp, cfg, 'agy-reviewer'))
            self.assertEqual(d._select(change(cfg), 2, fresh=True),
                             (['codex-reviewer', 'claude-reviewer'], ''))
        self.assertIn('reviewer agy-reviewer skipped: every model of its chain is remembered '
                      'unavailable', '\n'.join(self.logs))

    def test_agy_available_the_pair_stays_codex_and_agy(self):
        cfg = ssdlc(self)
        with tempfile.TemporaryDirectory() as tmp:
            d = self.make(cfg, unavailability.Memory(tmp, clock=lambda: NOW))
            self.assertEqual(d._select(change(cfg), 2, fresh=True),
                             (['codex-reviewer', 'agy-reviewer'], ''))
        self.assertEqual(self.make(cfg)._select(change(cfg), 2),
                         (['codex-reviewer', 'agy-reviewer'], ''))
        # Level 1: the PR's reviewer, codex, as before.
        self.assertEqual(self.make(cfg)._select(change(cfg), 1), (['codex-reviewer'], ''))

    def test_claude_reviewer_needs_a_reviewer_of_another_provider(self):
        # Only claude-reviewer and agy, run on a Claude model, are left: no reviewer of the round
        # is of a provider other than claude-implementer's, so no review at all.
        cfg = ssdlc(self, '[roles]\nreviewers = ["agy-reviewer", "claude-reviewer"]\n'
                          '[agents.agy-reviewer]\nmodel = "claude-haiku-5-5"\n'
                          'provider = "anthropic"\nfallback_models = []\n')
        reviewers, problem = self.make(cfg)._select(change(cfg, 'agy-reviewer'), 2)
        self.assertEqual(reviewers, [])
        self.assertIn('non ne ha abbastanza', problem)
        # Level 1: claude-reviewer alone is never the only reviewer of a PR of claude.
        cfg = ssdlc(self, '[roles]\nreviewers = ["claude-reviewer"]\n')
        reviewers, problem = self.make(cfg)._select(change(cfg, 'claude-reviewer'), 1)
        self.assertEqual(reviewers, [])
        self.assertIn('almeno un reviewer deve essere di un fornitore diverso', problem)
        # Without codex and with agy unavailable, the only valid pair has agy, the reviewer of
        # another provider: it is kept, and its turn fails as before; claude-reviewer never
        # reviews in its place without one.
        cfg = ssdlc(self, '[roles]\nreviewers = ["agy-reviewer", "claude-reviewer"]\n')
        with tempfile.TemporaryDirectory() as tmp:
            d = self.make(cfg, unavailable(tmp, cfg, 'agy-reviewer'))
            self.assertEqual(d._select(change(cfg, 'agy-reviewer'), 2, fresh=True),
                             (['agy-reviewer', 'claude-reviewer'], ''))

    def test_without_the_rules_claude_reviewer_never_reviews_claude(self):
        # provider_quorum off (the default): claude-reviewer is the same agent as the
        # implementer, and with agy unavailable the pair is codex and agy all the same.
        cfg = dataclasses.replace(ssdlc_project.load_by_role(), max_parallel_actions=1)
        with tempfile.TemporaryDirectory() as tmp:
            d = self.make(cfg, unavailable(tmp, cfg, 'agy-reviewer'))
            self.assertEqual(d._select(change(cfg), 2, fresh=True),
                             (['codex-reviewer', 'agy-reviewer'], ''))


class ReadOnlyRolesTests(unittest.TestCase):
    def test_preflight_and_conclave_take_a_reviewer_key(self):
        cfg = ssdlc(self, 'preflight = "codex-reviewer"\n'
                          'conclave = ["claude-reviewer", "codex-reviewer", "agy-reviewer"]\n')
        self.assertEqual(cfg.preflight, 'codex-reviewer')
        self.assertEqual(cfg.conclave, ('claude-reviewer', 'codex-reviewer', 'agy-reviewer'))
        # The same app and the same model as preflight = "codex" before the keys by role.
        before = ssdlc_project.load()
        self.assertEqual((cfg.app_of('codex-reviewer'), cfg.agent('codex-reviewer').model,
                          cfg.agent('codex-reviewer').effort),
                         (before.app_of('codex'), before.agent('codex').model,
                          before.agent('codex').effort))

    def test_never_an_implementer_key(self):
        data = ssdlc_project.by_role()
        for change_, error in (
                ({'preflight': 'claude-implementer'},
                 "preflight: 'claude-implementer' is a implementer key"),
                ({'conclave': ['claude-implementer', 'codex-reviewer', 'agy-reviewer']},
                 "conclave: 'claude-implementer' is a implementer key")):
            with self.subTest(error=error), self.assertRaisesRegex(ConfigError, error):
                config.parse_config(dict(data, **change_))

    def test_the_readiness_check_runs_with_codex_reviewer(self):
        cfg = ssdlc(self, 'preflight = "codex-reviewer"\n')
        gh, ws = IssueGH((READY,)), FakeWS()
        runner = FakeRunner(checked(True), implemented())
        d = Dispatcher(cfg, gh, ws, runner, log=lambda *_: None)
        d.tick()
        self.assertEqual(runner.calls[0][:3], ('codex-reviewer', 'review', 'preflight'))
        records = [r for c in gh.thread if (r := markdown.read_marker(c['body'], 'preflight'))]
        self.assertEqual([(r['agent'], r['ready']) for r in records], [('codex-reviewer', True)])
        # The check is read as codex-reviewer's, and the issue goes on to claude-implementer.
        self.assertEqual(d._preflight_records(gh.thread)[0][1]['agent'], 'codex-reviewer')
        d.tick()
        self.assertEqual([c[:2] for c in runner.calls],
                         [('codex-reviewer', 'review'), ('claude-implementer', 'write')])


class StartupTests(unittest.TestCase):
    """An overlay written before #499 stops devloop at startup and says what to rename."""

    def test_the_tables_of_the_keys_of_before(self):
        for table in ('[agents.agy]\nreview_commands = ["git diff"]\n',
                      '[agents.claude]\nprovider = "anthropic"\n',
                      '[agents.codex]\nprovider = "openai"\n'):
            with self.subTest(table=table), self.assertRaises(ConfigError) as ctx:
                ssdlc_project.load_by_role(overlay(self, table))
            message = str(ctx.exception)
            self.assertIn('the agents have keys by role: rename', message)
            self.assertIn('ops/host/migrate.md', message)
        self.assertIn('rename [agents.agy] of the local overlay to [agents.agy-reviewer]',
                      str(self._error('[agents.agy]\nreview_commands = ["git diff"]\n')))
        self.assertIn('[agents.claude-implementer] or [agents.claude-reviewer]',
                      str(self._error('[agents.claude]\nprovider = "anthropic"\n')))

    def test_the_keys_of_before_in_the_roles(self):
        # Each error names only the keys the role takes: a reviewer key for preflight and
        # conclave, never claude-implementer; the table for the supervisor.
        for text, where, hint in (
                ('preflight = "codex"\n', 'preflight', "use 'codex-reviewer' ("),
                ('conclave = ["claude", "codex", "agy"]\n', 'conclave',
                 "use 'claude-reviewer' ("),
                ('[roles]\nimplementer = "claude"\n', 'roles.implementer',
                 "use 'claude-implementer' ("),
                ('[roles]\nreviewers = ["codex", "agy"]\n', 'roles.reviewers',
                 "use 'codex-reviewer' ("),
                ('[roles]\nsupervisor = "claude"\n', 'roles.supervisor',
                 'a key with a role is never the supervisor: use the table [roles.supervisor], '
                 'name = "claude-supervisor"'),
                ('[review_policy]\nadvisory_reviewer = "claude"\n',
                 'review_policy.advisory_reviewer', "use 'claude-reviewer' (")):
            with self.subTest(where=where):
                message = str(self._error(text))
                self.assertIn(f'{where}: unknown agent', message)
                self.assertIn('the agents have keys by role, ', message)
                self.assertIn(hint, message)
                self.assertNotIn('implementer' if where != 'roles.implementer' else 'reviewer',
                                 message.split('role, ')[1])
                self.assertIn('ops/host/migrate.md', message)
        # Any other unknown key, as before.
        self.assertEqual(str(self._error('preflight = "nobody"\n')),
                         "preflight: unknown agent 'nobody'")

    def _error(self, text):
        with self.assertRaises(ConfigError) as ctx:
            ssdlc_project.load_by_role(overlay(self, text))
        return ctx.exception


class DocsTests(unittest.TestCase):
    """The step on the host the errors name, and the documents of the keys by role."""

    def test_migrate_describes_the_step_the_errors_name(self):
        text = (ROOT / 'ops' / 'host' / 'migrate.md').read_text(encoding='utf-8')
        heading = '## Chiavi per ruolo su un host attivo\n'
        self.assertIn(heading, text)
        section = text[text.index(heading):]
        for needed in ('`[agents.claude]` diventa `[agents.claude-implementer]`',
                       '`[agents.codex]` diventa\n     `[agents.codex-reviewer]`',
                       '`[agents.agy]` diventa `[agents.agy-reviewer]`',
                       '`preflight = "codex"` diventa `preflight = "codex-reviewer"`',
                       'advisory_reviewer = "claude-reviewer"', 'provider_quorum = true',
                       'sudo systemctl stop devloop', 'config.local.toml.prima-499',
                       'pin origin/main', '**Rollback.**', 'brushtyler-devloop-implementer'):
            self.assertIn(needed, section)
        # The rollback uses the commit saved before the pin, not the one current points at
        # after it, and step 2 saves it before the pin of step 6.
        rollback = section[section.index('**Rollback.**'):]
        self.assertIn('commit.prima-499', rollback)
        self.assertIn('pin "$sha"', rollback)
        self.assertLess(section.index('/current/pin.json'), section.index('pin origin/main'))
        # The anchor the guide links.
        guide = (ROOT / 'docs' / 'devloop' / 'guide.md').read_text(encoding='utf-8')
        self.assertIn('migrate.md#chiavi-per-ruolo-su-un-host-attivo', guide)

    def test_reference_and_guide_name_the_keys_by_role(self):
        reference = (ROOT / 'docs' / 'devloop' / 'reference.md').read_text(encoding='utf-8')
        self.assertIn('**On since #499**', reference)
        self.assertIn('take a reviewer key too, never an\n  implementer key', reference)
        guide = (ROOT / 'docs' / 'devloop' / 'guide.md').read_text(encoding='utf-8')
        self.assertIn('preflight = "codex-reviewer"', guide)
        # No example names a key of before: copied into the overlay it would stop devloop.
        self.assertNotIn('advisory_reviewer = "claude"', guide)
        self.assertIn('Since #499 the installation\'s `config.toml` names its agents by role',
                      guide)


if __name__ == '__main__':
    unittest.main()
