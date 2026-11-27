"""devloop: the project file on the host and the facts of the repository (ADR 0006, R2).

The project file is ``<state_dir>/project.toml``, beside the overlay, written by the operator and
passed by the launcher; never a file of the managed repository, a PR branch or the pinned copy.
Its schema is closed: facts (installation < project file < overlay) and restrictions that only
add. Its hash is in the version of the review policy, and every start and reload records the
configuration in the journal (trace T1 of K1)."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import devloop.__main__ as cli
from devloop import config, pin
from devloop.config import DEFAULT_CONFIG, ConfigError, Reloader
from devloop.journal import CONFIG, JournalError, TurnJournal
from devloop.ledger import Ledger
from helpers import ssdlc_project
from test_devloop_pin import LIST, GitCase, git, load_launcher

REPO = Path(__file__).resolve().parent.parent


class FilesCase(unittest.TestCase):
    """An installation's config.toml, an overlay and a project file in a temporary state_dir.
    The installation's file is ssdlc's configuration as one file (``ssdlc_project.text``), as
    before ADR 0006, R3: these tests compose a project file over a complete configuration."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.base = self.tmp / 'config.toml'
        self.base.write_text(ssdlc_project.text(), encoding='utf-8')
        self.local = self.tmp / 'config.local.toml'
        self.local.write_text('max_open_prs = 4\n', encoding='utf-8')
        self.project = self.tmp / 'project.toml'

    def write(self, text, path=None):
        (path or self.project).write_text(text, encoding='utf-8')

    def load(self, text=None, overlay=None):
        if text is not None:
            self.write(text)
        if overlay is not None:
            self.write(overlay, self.local)
        return config.load_config(self.base, self.local,
                                  self.project if text is not None else None)


class EquivalenceTests(FilesCase):
    def test_without_a_project_file_the_configuration_is_that_of_today(self):
        before = config.parse_config(config.apply_overlay(config.read_toml(self.base),
                                                          config.read_toml(self.local)))
        self.assertEqual(config.load_config(self.base, self.local), before)
        self.assertEqual(config.load_config(self.base, self.local, None), before)
        self.assertEqual(config.load_config(self.base, self.local).review_policy.version(),
                         before.review_policy.version())
        self.assertEqual(config.load_data(self.base, self.local),
                         config.apply_overlay(config.read_toml(self.base),
                                              config.read_toml(self.local)))
        self.assertEqual(config.load_data(), config.read_toml(DEFAULT_CONFIG))

    def test_an_empty_project_file_changes_only_the_policy_version(self):
        plain = self.load()
        empty = self.load('')
        self.assertEqual(config.dataclasses.replace(empty, review_policy=plain.review_policy),
                         plain)
        self.assertNotEqual(empty.review_policy.version(), plain.review_policy.version())


class PrecedenceTests(FilesCase):
    def test_facts_replace_the_installation_and_the_overlay_replaces_them(self):
        cfg = self.load('test_cmd = "make check"\nlanguage = "Italiano"\nlocal_tests = "full"\n'
                        'priority_labels = ["urgente"]\n'
                        '[docs.map]\n"src/" = ["docs/api.md"]\n')
        self.assertEqual((cfg.test_cmd, cfg.language, cfg.local_tests, cfg.priority_labels),
                         ('make check', 'Italiano', 'full', ('urgente',)))
        self.assertIn(('src/', ('docs/api.md',)), cfg.docs.entries)
        # The installation's map entries stay: the tables merge key by key.
        self.assertTrue(set(config.load_config(self.base).docs.entries) <= set(cfg.docs.entries))
        cfg = self.load('test_cmd = "make check"\n',
                        overlay='max_open_prs = 4\ntest_cmd = "make test"\n')
        self.assertEqual(cfg.test_cmd, 'make test')

    def test_the_test_cmd_of_the_project_file_never_reaches_the_agents_permissions(self):
        # agy without commands of its own (the installation's): its defaults derive from the
        # test command of the installation or of the overlay, never of the project file (D2).
        floor = self.load()
        self.assertNotIn('write_commands', config.read_toml(self.base)['agents']['agy-reviewer'])
        cfg = self.load('test_cmd = "python3 project_runner.py"\n')
        self.assertEqual(cfg.test_cmd, 'python3 project_runner.py')
        agy = cfg.agents['agy-reviewer']
        for commands in (agy.write_commands, agy.review_commands):
            self.assertNotIn('python3 project_runner.py', commands)
        self.assertEqual((agy.write_commands, agy.review_commands),
                         (floor.agents['agy-reviewer'].write_commands, floor.agents['agy-reviewer'].review_commands))
        # The overlay admits it.
        cfg = self.load('test_cmd = "python3 project_runner.py"\n',
                        overlay='max_open_prs = 4\ntest_cmd = "python3 project_runner.py"\n')
        self.assertIn('python3 project_runner.py', cfg.agents['agy-reviewer'].write_commands)
        self.assertIn('python3 project_runner.py', cfg.agents['agy-reviewer'].review_commands)
        # The reloader composes them the same way.
        self.write('max_open_prs = 4\n', self.local)
        self.write('test_cmd = "python3 project_runner.py"\n')
        reloaded, _ = Reloader(floor, self.base, self.local, self.project,
                               log=lambda _: None)._load(config.read_sources(
                                   self.base, self.local, self.project))
        self.assertNotIn('python3 project_runner.py', reloaded.agents['agy-reviewer'].review_commands)

    def test_restrictions_only_add_to_the_installation_and_the_overlay(self):
        floor = self.load()
        cfg = self.load('protected_paths = ["infra/"]\nblocked_labels = ["wontfix"]\n'
                        '[review_policy]\nsensitive_paths = ["src/auth/", "devloop/"]\n'
                        '[merge]\noperator_only_paths = ["LICENSE"]\n')
        self.assertEqual(cfg.protected_paths, floor.protected_paths + ('infra/',))
        self.assertEqual(cfg.blocked_labels, floor.blocked_labels + ('wontfix',))
        self.assertEqual(cfg.review_policy.sensitive_paths,
                         floor.review_policy.sensitive_paths + ('src/auth/',))
        self.assertEqual(cfg.merge.operator_only_paths,
                         floor.merge.operator_only_paths + ('LICENSE',))
        # An empty list removes nothing.
        cfg = self.load('protected_paths = []\n[review_policy]\nsensitive_paths = []\n')
        self.assertEqual(cfg.protected_paths, floor.protected_paths)
        self.assertEqual(cfg.review_policy.sensitive_paths, floor.review_policy.sensitive_paths)

    def test_restrictions_add_to_the_overlay_too(self):
        cfg = self.load('[review_policy]\nsensitive_paths = ["src/auth/"]\n',
                        overlay='[review_policy]\nsensitive_paths = ["devloop/"]\n')
        self.assertEqual(cfg.review_policy.sensitive_paths, ('devloop/', 'src/auth/'))
        # Without the key below, the project adds to the default of the code.
        data = config.compose({}, {'protected_paths': ['infra/']})
        self.assertEqual(data['protected_paths'], ['.github/', 'infra/'])

    def test_checks_of_level_zero_are_added_never_turned_on(self):
        on = ('[review_policy.zero]\nallowlist = ["docs/"]\n'
              'checks = [{ name = "tests", app_id = 15368 }]\n')
        cfg = self.load('[review_policy.zero]\nchecks = [{ name = "lint", app_id = 15368 }]\n',
                        overlay=on)
        self.assertEqual(cfg.review_policy.zero_checks, (('tests', 15368), ('lint', 15368)))
        with self.assertRaisesRegex(ConfigError, 'never turns level 0 on'):
            self.load('[review_policy.zero]\nchecks = [{ name = "lint", app_id = 15368 }]\n',
                      overlay='[review_policy.zero]\nallowlist = ["docs/"]\n')

    def test_sensitive_path_lists_add_too(self):
        # ssdlc lists none since the engine left (ADR 0007, R1): the base here lists one.
        base = config.read_toml(self.base)
        base['review_policy']['sensitive_path_lists'] = [LIST]
        data = config.compose(base, {'review_policy': {'sensitive_path_lists': ['extra.json']}})
        self.assertEqual(data['review_policy']['sensitive_path_lists'], [LIST, 'extra.json'])


class SchemaTests(FilesCase):
    def test_a_trust_key_is_refused_naming_it(self):
        for text, key in (('trusted_users = ["someone"]\n', "'trusted_users'"),
                          ('repo = "x/y"\n', "'repo'"),
                          ('state_dir = "/tmp"\n', "'state_dir'"),
                          ('max_open_prs = 9\n', "'max_open_prs'"),
                          ('[merge]\nmode = "strict"\n', "'merge.mode'"),
                          ('[roles]\nimplementer = "codex"\n', "'roles'"),
                          ('[apps]\nkey_dir = "/k"\n', "'apps'"),
                          ('[agents.claude]\nreview_commands = ["rm"]\n', "'agents'"),
                          ('[review_policy]\ndefault_level = 1\n', "'review_policy.default_level'"),
                          ('[review_policy.zero]\nallowlist = ["src/"]\n',
                           "'review_policy.zero.allowlist'"),
                          ('[budget]\nenabled = false\n', "'budget'"),
                          ('[delegations]\nrounds = true\n', "'delegations'")):
            with self.subTest(text=text), self.assertRaises(ConfigError) as raised:
                self.load(text)
            self.assertIn(f'project file: {key} is not a key of the project file',
                          str(raised.exception))
            self.assertIn('local overlay', str(raised.exception))

    def test_an_unknown_key_or_a_wrong_shape_is_refused(self):
        with self.assertRaisesRegex(ConfigError, "project file: unknown key 'bogus'"):
            self.load('bogus = 1\n')
        with self.assertRaisesRegex(ConfigError, "project file: unknown key 'docs.other'"):
            self.load('[docs]\nother = 1\n')
        with self.assertRaisesRegex(ConfigError, 'project file: docs must be a table'):
            self.load('docs = 1\n')
        with self.assertRaisesRegex(ConfigError, 'project file: protected_paths: expected a list'):
            self.load('protected_paths = "infra/"\n')
        with self.assertRaisesRegex(ConfigError, 'project file '):
            self.load('test_cmd = \n')

    def test_the_schema_holds_only_the_project_keys(self):
        self.assertEqual(set(config.PROJECT_KEYS), {
            'test_cmd', 'local_tests', 'language', 'priority_labels', 'docs', 'protected_paths',
            'blocked_labels', 'review_policy', 'merge', 'tests', 'prompts', 'prepare'})
        # #696: the step that prepares a worktree, a fact the overlay may replace key by key.
        self.assertEqual(config.PROJECT_KEYS['prepare'],
                         {'cmd': None, 'inputs': None, 'outputs': None})
        self.assertEqual(config.OVERLAY_KEYS['prepare'], config.PROJECT_KEYS['prepare'])
        # ADR 0006, R5: the selection of the test modules and the format of the CI logs.
        self.assertEqual(config.PROJECT_KEYS['tests'],
                         {'selection': None, 'source_prefixes': None, 'dir_modules': {'*': None},
                          'ci_log_format': None})
        # The project's data in the prompts (ADR 0006, R4): a fact, which the overlay may set.
        self.assertEqual(config.PROJECT_KEYS['prompts'], {
            'instructions': None, 'fix_instructions': None, 'module_test_cmd': None,
            'module_test_helpers': None,
            'adr': {'dir': None, 'decided': None, 'open_value': None, 'open_choice': None}})
        self.assertEqual(config.OVERLAY_KEYS['prompts'], config.PROJECT_KEYS['prompts'])
        self.assertEqual(config.PROJECT_KEYS['review_policy'],
                         {'sensitive_paths': None, 'sensitive_path_lists': None,
                          'zero': {'checks': None}})
        self.assertEqual(config.PROJECT_KEYS['merge'], {'operator_only_paths': None})


class PolicyTests(FilesCase):
    def test_the_hash_of_the_project_file_is_in_the_policy_version(self):
        text = 'language = "Italiano"\n'
        cfg = self.load(text)
        self.assertEqual(cfg.review_policy.project,
                         hashlib.sha256(text.encode()).hexdigest())
        first = cfg.review_policy.version()
        self.assertNotEqual(first, self.load().review_policy.version())
        self.assertNotEqual(first, self.load(text + '# a comment\n').review_policy.version())
        self.assertEqual(first, self.load(text).review_policy.version())
        self.assertEqual(config.load_config(self.base, self.local).review_policy.project, '')


class ReloaderTests(FilesCase):
    def setUp(self):
        super().setUp()
        self.write('language = "Italiano"\n')
        self.cfg = config.load_config(self.base, self.local, self.project)
        self.logs, self.records = [], []
        self.reloader = Reloader(self.cfg, self.base, self.local, self.project,
                                 log=self.logs.append, record=self.records.append)

    def test_the_project_file_is_read_again_and_recorded(self):
        self.assertIs(self.reloader.refresh(), self.cfg)
        self.assertEqual(self.reloader.paths, {'config': self.base, 'local': self.local,
                                               'project': self.project})
        self.assertEqual(len(self.records), 1)  # the first refresh loads in any case
        print_ = self.records[0]
        self.assertEqual(print_['project'], hashlib.sha256(self.project.read_bytes()).hexdigest())
        self.assertEqual(print_['overlay'], hashlib.sha256(self.local.read_bytes()).hexdigest())
        self.assertEqual(self.reloader.refresh(), self.cfg)
        self.assertEqual(len(self.records), 1)
        self.write('language = "Italiano"\n[review_policy]\nsensitive_paths = ["src/"]\n')
        self.assertIs(self.reloader.refresh(), self.cfg)  # the policy waits for a restart
        self.assertIn('review_policy', self.logs[-1])
        self.assertEqual(len(self.records), 2)
        self.assertIn('src/', self.records[1]['trust']['review_policy.sensitive_paths'])

    def test_an_invalid_project_file_is_recorded_and_the_configuration_stays(self):
        self.reloader.refresh()
        self.write('trusted_users = ["someone"]\n')
        self.assertIs(self.reloader.refresh(), self.cfg)
        self.assertIn('previous configuration stays in use', self.logs[-1])
        print_ = self.records[-1]
        self.assertIsNone(print_['effective'])
        self.assertIsNone(print_['trust'])
        self.assertIn("'trusted_users'", print_['error'])
        self.assertEqual(print_['project'], hashlib.sha256(self.project.read_bytes()).hexdigest())

    def test_a_project_file_that_becomes_a_link_or_fails_its_check_is_not_read(self):
        self.reloader.refresh()
        tracked = self.tmp / 'tracked-project.toml'
        tracked.write_text('language = "Esperanto"\n')
        self.project.unlink()
        self.project.symlink_to(tracked)
        self.assertIs(self.reloader.refresh(), self.cfg)
        self.assertIn('symbolic link', self.logs[-1])
        self.assertIn('symbolic link', self.records[-1]['error'])
        self.assertIsNone(self.records[-1]['project'])
        # A check that fails (the project file became a file of the repository) stops the read.
        self.project.unlink()
        self.write('language = "Esperanto"\n')
        reloader = Reloader(self.cfg, self.base, self.local, self.project, log=self.logs.append,
                            check=mock.Mock(side_effect=ConfigError('a file of the repository')))
        self.assertIs(reloader.refresh(), self.cfg)
        self.assertIn('a file of the repository', self.logs[-1])
        reloader.check.assert_called_once_with()

    def test_a_failed_record_is_logged(self):
        reloader = Reloader(self.cfg, self.base, self.local, self.project, log=self.logs.append,
                            record=mock.Mock(side_effect=JournalError('full')))
        self.assertIs(reloader.refresh(), self.cfg)
        self.assertIn('record of the configuration was not written', self.logs[-1])


class JournalTests(FilesCase):
    def setUp(self):
        super().setUp()
        self.journal = TurnJournal(self.tmp / 'state')

    def fingerprint(self, text=None, overlay=None):
        if text is not None:
            self.write(text)
        if overlay is not None:
            self.write(overlay, self.local)
        return config.fingerprint(config.read_sources(
            self.base, self.local, self.project if text is not None else None))

    def test_each_record_lists_the_trust_keys_changed_before_and_after(self):
        first = self.journal.configuration('start', self.fingerprint())
        self.assertIn({'key': 'trusted_users', 'old': None, 'new': ['brushtyler']}, first)
        self.assertTrue(all(c['old'] is None for c in first))
        self.assertEqual(self.journal.trust(), {c['key']: c['new'] for c in first})
        changes = self.journal.configuration(
            'reload', self.fingerprint(overlay='max_open_prs = 4\ntrusted_users = ["a", "b"]\n'))
        self.assertEqual(changes, [{'key': 'trusted_users', 'old': ['brushtyler'],
                                    'new': ['a', 'b']}])
        changes = self.journal.configuration(
            'start', self.fingerprint('[review_policy]\nsensitive_paths = ["src/"]\n'))
        self.assertEqual([c['key'] for c in changes], ['review_policy.sensitive_paths'])
        self.assertEqual(changes[0]['new'][-1], 'src/')
        # Only the limits changed: a record with its hashes and no trust key.
        print_ = self.fingerprint(overlay='max_open_prs = 5\ntrusted_users = ["a", "b"]\n',
                                  text='[review_policy]\nsensitive_paths = ["src/"]\n')
        self.assertEqual(self.journal.configuration('reload', print_), [])
        records = TurnJournal(self.tmp / 'state').configurations()  # read back from the file
        self.assertEqual([r['when'] for r in records], ['start', 'reload', 'start', 'reload'])
        self.assertEqual(records[-1]['overlay'], print_['overlay'])
        self.assertEqual(records[-1]['project'], print_['project'])
        self.assertEqual(records[-1]['effective'], print_['effective'])
        self.assertIsNone(records[0]['project'])

    def test_a_change_of_max_review_rounds_is_recorded_before_and_after(self):
        rounds = config.read_toml(self.base)['max_review_rounds']
        self.journal.configuration('start', self.fingerprint())
        self.assertEqual(self.journal.trust()['max_review_rounds'], rounds)
        changes = self.journal.configuration(
            'reload', self.fingerprint(overlay=f'max_open_prs = 4\nmax_review_rounds = {rounds + 3}\n'),
            only_if_changed=True)
        self.assertEqual(changes, [{'key': 'max_review_rounds', 'old': rounds,
                                    'new': rounds + 3}])

    def test_only_if_changed_skips_the_same_hashes(self):
        print_ = self.fingerprint()
        self.journal.configuration('start', print_)
        self.assertIsNone(self.journal.configuration('reload', print_, only_if_changed=True))
        self.assertEqual(len(self.journal.configurations()), 1)
        self.assertEqual(self.journal.configuration('reload', self.fingerprint('language = "x"\n'),
                                                    only_if_changed=True), [])

    def test_an_invalid_configuration_is_recorded_without_changes(self):
        self.journal.configuration('start', self.fingerprint())
        self.journal.configuration('reload', {'overlay': 'a', 'project': 'b', 'effective': None,
                                              'trust': None, 'error': 'ConfigError: x'})
        last = self.journal.configurations()[-1]
        self.assertEqual((last['effective'], last['changes'], last['error']),
                         (None, [], 'ConfigError: x'))
        self.assertEqual(self.journal.trust()['repo'], 'brushtyler/ssdlc')

    def test_a_read_only_journal_records_nothing(self):
        journal = TurnJournal(self.tmp / 'state', read_only=True)
        with self.assertRaises(JournalError):
            journal.configuration('start', self.fingerprint())
        self.assertFalse((self.tmp / 'state').exists())


class CommandTests(FilesCase):
    def setUp(self):
        super().setUp()
        env = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com',
               'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.com'}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(cli.STATE_ENV, None)
        self.root = self.tmp / 'clone'
        git(self.tmp, 'init', '--quiet', '-b', 'main', str(self.root))
        self.state = self.root / '.devloop'
        self.state.mkdir(parents=True)
        self.project = self.state / 'project.toml'

    def args(self, project, command='status'):
        return SimpleNamespace(config=str(self.base), local_config=str(self.local),
                               project_config=str(project) if project else None,
                               command=command, dry_run=False)

    def test_the_project_file_is_that_of_the_state_dir(self):
        self.write('language = "Esperanto"\n')
        cfg = cli.load(self.args(self.project), self.root)
        self.assertEqual(cfg.language, 'Esperanto')
        self.assertEqual(cli.load(self.args(None), self.root).language,
                         config.load_config(self.base, self.local).language)
        elsewhere = self.root / 'project.toml'
        elsewhere.write_text('language = "Italiano"\n')
        with self.assertRaisesRegex(ConfigError, 'is not .*project.toml: the project file is '
                                                 'project.toml in the state_dir'):
            cli.load(self.args(elsewhere), self.root)

    def test_a_project_file_of_the_repository_is_refused(self):
        # A state_dir inside the clone whose project.toml the repository tracks.
        self.write('language = "Italiano"\n')
        git(self.root, 'add', '-f', str(self.project))
        git(self.root, 'commit', '--quiet', '-m', 'a project file in the repository')
        with self.assertRaisesRegex(ConfigError, 'is a file of the managed repository'):
            cli.load(self.args(self.project), self.root)
        self.assertFalse(cli.tracked(self.root, self.state / 'config.local.toml'))

    def test_a_link_to_a_file_of_the_repository_is_refused(self):
        # An untracked project.toml in the state_dir, a link to a file the repository tracks.
        target = self.root / 'tracked-project.toml'
        target.write_text('test_cmd = "python3 project_runner.py"\n')
        git(self.root, 'add', str(target))
        git(self.root, 'commit', '--quiet', '-m', 'a file of the repository')
        self.project.symlink_to(target)
        self.assertFalse(cli.tracked(self.root, self.project))
        with self.assertRaisesRegex(ConfigError, 'symbolic link'):
            cli.load(self.args(self.project), self.root)
        with self.assertRaisesRegex(ConfigError, 'symbolic link'):
            config.read_sources(self.base, self.local, self.project)
        with self.assertRaisesRegex(ConfigError, 'symbolic link'):
            cli.check_project(self.project, self.state, self.root)
        # A hard link to it is refused too.
        self.project.unlink()
        os.link(target, self.project)
        with self.assertRaisesRegex(ConfigError, 'hard links'):
            cli.load(self.args(self.project), self.root)

    def test_a_state_dir_linked_into_a_checkout_is_refused(self):
        # The state_dir is a link into a git checkout (a worktree, another clone) whose
        # project.toml git tracks: not the host's file.
        other = self.tmp / 'worktree'
        git(self.tmp, 'init', '--quiet', '-b', 'main', str(other))
        (other / 'project.toml').write_text('test_cmd = "python3 project_runner.py"\n')
        git(other, 'add', 'project.toml')
        git(other, 'commit', '--quiet', '-m', 'a project file of the branch')
        linked = self.tmp / 'linked-state'
        linked.symlink_to(other)
        with self.assertRaisesRegex(ConfigError, 'a file of a git checkout'):
            cli.check_project(linked / 'project.toml', linked, self.root)
        # An untracked one beside it is the host's.
        git(other, 'rm', '--quiet', '--cached', 'project.toml')
        cli.check_project(linked / 'project.toml', linked, self.root)

    def test_build_reads_the_project_file_and_the_pinner_gets_it(self):
        self.write('language = "Italiano"\n')
        args = self.args(self.project)
        with mock.patch.object(cli, 'main_checkout', return_value=self.root), \
                mock.patch.object(cli.Identities, 'from_config',
                                  side_effect=lambda cfg, **kw: cli.Identities(cfg.repo, {},
                                                                               **kw)):
            cfg, state, d = cli.build(args)
        self.assertEqual(cfg.language, 'Italiano')
        self.assertEqual(state, self.state)
        self.assertEqual(d.pinner.project, str(self.project))
        # Every `gh` of the dispatcher has its temporary directory in state_dir (#545).
        self.assertEqual(d.identities.tmp_dir, self.state / 'gh-tmp')

    def test_the_start_records_the_files_build_read_not_a_new_read(self):
        self.write('language = "Italiano"\n')
        args = self.args(self.project, 'run')
        with mock.patch.object(cli, 'main_checkout', return_value=self.root), \
                mock.patch.object(cli.Identities, 'from_config',
                                  side_effect=lambda cfg, **kw: cli.Identities(cfg.repo, {},
                                                                               **kw)):
            cfg, _, d = cli.build(args)
        used = cfg.review_policy.project
        # The operator changes the file between the load and the record.
        self.write('language = "Esperanto"\n')
        logs = []
        cli.record_start(d, logs.append)
        record = d.ledger.journal.configurations()[-1]
        self.assertEqual(record['when'], 'start')
        self.assertEqual(record['project'], used)
        self.assertNotEqual(record['project'],
                            hashlib.sha256(self.project.read_bytes()).hexdigest())
        # A dispatcher without the record of its build is refused, never read again.
        with self.assertRaisesRegex(ConfigError, 'no record of the configuration'):
            cli.record_start(SimpleNamespace(ledger=d.ledger), logs.append)

    def test_the_start_is_recorded_and_the_reloads_only_when_changed(self):
        self.write('language = "Italiano"\n')
        ledger = Ledger(None, state_dir=self.state)
        logs = []
        print_ = config.fingerprint(config.read_sources(self.base, self.local, self.project))
        reload = cli.record_start(SimpleNamespace(ledger=ledger, config_print=print_),
                                  logs.append)
        records = ledger.journal.configurations()
        self.assertEqual([r['when'] for r in records], ['start'])
        self.assertEqual(records[0]['project'],
                         hashlib.sha256(self.project.read_bytes()).hexdigest())
        self.assertIn('config (start): overlay ', logs[0])
        self.assertIn('first record of the trust keys', logs[1])
        sources = config.read_sources(self.base, self.local, self.project)
        reload(config.fingerprint(sources))
        self.assertEqual(len(ledger.journal.configurations()), 1)
        self.write(self.local.read_text() + 'trusted_users = ["a"]\n', self.local)
        reload(config.fingerprint(config.read_sources(self.base, self.local, self.project)))
        self.assertEqual([r['when'] for r in ledger.journal.configurations()],
                         ['start', 'reload'])
        self.assertIn("config: trust key trusted_users ['brushtyler'] -> ['a']", logs)
        # A dispatcher without a journal of its own records nothing.
        self.assertIsNone(cli.record_start(SimpleNamespace(), logs.append))

    def test_run_records_the_start_before_the_loop_and_gives_the_reloader_the_files(self):
        self.write('language = "Italiano"\n')
        ledger = Ledger(None, state_dir=self.state)
        seen = []

        def loop(cfg, state, d, timeout, reloader):
            seen.append((reloader, ledger.journal.configurations()))
            return 0
        argv = ['--config', str(self.base), '--local-config', str(self.local),
                '--project-config', str(self.project), 'run']
        print_ = config.fingerprint(config.read_sources(self.base, self.local, self.project))
        dispatcher = SimpleNamespace(ledger=ledger, config_print=print_,
                                     ws=SimpleNamespace(repo_root=self.root))
        with mock.patch.object(cli, 'build', return_value=(
                ssdlc_project.load(), self.state, dispatcher)), \
                mock.patch.object(cli, 'log'), mock.patch.object(cli, 'run_loop', loop):
            cli.main(argv)
        reloader, records = seen[0]
        self.assertEqual(reloader.paths['project'], self.project)
        self.assertEqual([r['when'] for r in records], ['start'])
        self.assertIsNotNone(reloader.record)
        # The reloader checks the project file before each read of it.
        reloader.check()
        self.project.unlink()
        self.project.symlink_to(self.base)
        with self.assertRaisesRegex(ConfigError, 'symbolic link'):
            reloader.check()

    def test_a_start_that_cannot_be_recorded_stops(self):
        ledger = Ledger(None, state_dir=self.state)
        with mock.patch.object(ledger.journal, 'configuration',
                               side_effect=JournalError('full')), \
                mock.patch.object(cli, 'build', return_value=(
                    ssdlc_project.load(), self.state,
                    SimpleNamespace(ledger=ledger, tick=mock.Mock(),
                                    config_print=config.fingerprint(
                                        config.read_sources(self.base))))) as build, \
                mock.patch.object(cli, 'log'), self.assertRaises(SystemExit) as raised:
            cli.main(['--config', str(self.base), 'tick'])
        self.assertIn('the configuration was not recorded in the journal', str(raised.exception))
        build.return_value[2].tick.assert_not_called()


class PinTests(GitCase):
    """The pin reads the project file of the state_dir, never one of the commit."""

    def setUp(self):
        super().setUp()
        self.smoke_calls = []

    def smoke(self, copy, local, *project):
        self.smoke_calls.append(project)

    def test_the_lists_of_the_project_file_reach_the_copy(self):
        self.push('a list', 'schemas/extra.json', json.dumps({'files': {'src/a.py': {}}}))
        project = self.tmp / 'project.toml'
        project.write_text('[review_policy]\nsensitive_path_lists = ["schemas/extra.json"]\n')
        copy = self.pin('main', smoke=self.smoke, project=project)
        self.assertTrue((copy / 'schemas' / 'extra.json').is_file())
        self.assertEqual(json.loads((copy / 'pin.json').read_text())['files'],
                         [LIST, 'schemas/extra.json'])
        self.assertEqual(self.smoke_calls, [(project,)])

    def test_a_project_file_of_the_commit_is_never_read(self):
        sha = self.push('a project file in the repository', '.devloop/project.toml',
                        '[review_policy]\nsensitive_path_lists = ["schemas/none.json"]\n')
        self.assertEqual(pin.required_files(self.root, sha, None), [LIST])
        missing = self.tmp / 'project.toml'
        self.assertEqual(pin.required_files(self.root, sha, None, missing), [LIST])
        self.pin('main', smoke=self.smoke, project=missing)
        self.assertEqual(self.smoke_calls, [()])  # no file, no third argument

    def test_an_invalid_project_file_fails_the_pin(self):
        old = self.pin('main', smoke=self.smoke)
        project = self.tmp / 'project.toml'
        project.write_text('trusted_users = ["someone"]\n')
        self.push('another commit', 'NOTES.md', 'more\n')
        with self.assertRaisesRegex(pin.PinError, "'trusted_users' is not a key of the project"):
            self.pin('main', smoke=self.smoke, project=project)
        self.assertEqual(self.pointed(), old.resolve())

    def test_a_copy_that_cannot_read_the_project_file_is_refused(self):
        project = self.tmp / 'project.toml'
        project.write_text('language = "Italiano"\n')
        old = (self.root / 'devloop' / 'config.py').read_text()
        text = old.replace('def load_config(path=None, local=None, project=None, *, sources=None):',
                           'def load_config(path=None, local=None):').replace(
            '    return parse_sources(read_sources(path, local, project) if sources is None '
            'else sources)\n\n\n# The trust',
            '    return parse_sources(read_sources(path, local))\n\n\n# The trust')
        self.assertNotEqual(text, old)
        self.push('a copy before R2', 'devloop/config.py', text)
        with self.assertRaisesRegex(pin.PinError, 'cannot read the project file'):
            self.pin('main', smoke=self.smoke, project=project)
        self.pin('main', smoke=self.smoke)  # without a project file it pins

    def test_the_smoke_test_passes_the_project_file(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            out = 'devloop watch: nothing new' if 'watch' in argv else ''
            return subprocess.CompletedProcess(argv, 0, out, '')
        project = self.tmp / 'project.toml'
        project.write_text('')
        smoke = pin.Smoke(self.root, log=self.logs.append, run=run)
        with mock.patch.object(smoke, '_watch_ok'), mock.patch.object(smoke, '_tick_ok'):
            smoke(self.tmp, None, project)
            smoke(self.tmp, None)
        self.assertIn('--project-config', calls[0])
        self.assertEqual(calls[0][calls[0].index('--project-config') + 1], str(project.resolve()))
        self.assertNotIn('--project-config', calls[-1])

    def test_run_pin_takes_the_project_file_of_the_state_dir(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / 'project.toml').write_text('language = "Italiano"\n')
        flat = self.tmp / 'config.toml'  # an installation with a test_cmd (ADR 0006, R3)
        flat.write_text(ssdlc_project.text(), encoding='utf-8')
        args = SimpleNamespace(config=str(flat), local_config=None, project_config=None,
                               ref='main', no_smoke=True)
        with mock.patch.object(cli, 'main_checkout', return_value=self.root), \
                mock.patch.object(cli.pin, 'pin') as pinned:
            cli.run_pin(args)
        self.assertEqual(pinned.call_args.kwargs['project'], self.state / 'project.toml')


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / 'clone'
        self.state = self.root / '.devloop'
        copy = self.state / 'pinned-0123456789ab'
        (copy / 'devloop').mkdir(parents=True)
        (copy / 'devloop' / '__main__.py').write_text('')
        os.symlink(copy.name, self.state / 'current')

    def test_it_passes_the_project_file_of_the_state_dir_when_it_exists(self):
        launcher = load_launcher()
        argv, _ = launcher.command(self.root, ['tick'])
        self.assertNotIn('--project-config', argv)
        (self.root / 'project.toml').write_text('')  # never one of the clone
        (self.state / 'config.local.toml').write_text('')
        (self.state / 'project.toml').write_text('')
        argv, _ = launcher.command(self.root, ['tick'])
        self.assertEqual(argv[4:], ['--local-config', str(self.state / 'config.local.toml'),
                                    '--project-config', str(self.state / 'project.toml'),
                                    'tick'])
        # A link, even a dangling one, is passed: devloop refuses it rather than run without.
        (self.state / 'project.toml').unlink()
        (self.state / 'project.toml').symlink_to(self.tmp / 'missing.toml')
        argv, _ = launcher.command(self.root, ['tick'])
        self.assertIn('--project-config', argv)


class DocsTests(unittest.TestCase):
    def test_the_documents_describe_the_project_file(self):
        reference = (REPO / 'docs' / 'devloop' / 'reference.md').read_text(encoding='utf-8')
        guide = (REPO / 'docs' / 'devloop' / 'guide.md').read_text(encoding='utf-8')
        protocol = (REPO / 'docs' / 'devloop' / 'protocol.md').read_text(encoding='utf-8')
        for needle in ('project.toml', '--project-config', 'only add'):
            self.assertIn(needle, reference)
        self.assertIn('project.toml', guide)
        self.assertIn(f'`{CONFIG}`', protocol)


if __name__ == '__main__':
    unittest.main()
