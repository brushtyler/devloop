"""devloop: ssdlc's configuration in its project file (ADR 0006, R3).

The facts of ssdlc and the restrictions it adds left ``devloop/config.toml`` for the project file
on the host, whose example is ``ops/host/ssdlc/project.toml``; the agy commands derived from
ssdlc's tests for the overlay (``ops/host/ssdlc/overlay.toml``). Nothing changes for devloop on
ssdlc: the same values of the effective configuration, the same rendered prompts and the same
selected test modules. The SHA-256 of the project file is left out of the comparison on purpose:
R2 puts it in the policy version by construction, so the version is compared without it.

"Before" is a reference frozen at the last commit of ``main`` before R3,
28eaf9455bad6ff52dcbdbc29ecc07d7dde83e2a, independent of the project file and of the helper that
rebuilds the configuration from it: ``tests/fixtures/ssdlc-pre-r3/config.toml`` is the
``devloop/config.toml`` of that commit, as it was, and ``effective.json`` the effective
configuration and the policy version that the code of that commit computed from it, without
overlay nor project file (its ``source`` says how). A value migrated wrongly, in the project file
or in the overlay, is a difference from it. A later change to the installation's defaults is not
R3's: it updates the frozen ``config.toml`` and ``effective.json`` with it, saying so (``source``
``updated`` lists those made so far: the implementer app of #407 and the capacity delegation of
#412, merged beside R3; the keys by role of #499, which change the policy version too; the
``second_reviewer`` of #521, ``parallel`` by default; the coordinator actor of #528, off by
default; ``max_requested_fix_rounds`` of #536, the cap of the code by default). A change of ssdlc's own values updates it the same way, saying so: the restrictions, the
test prefix and the documentation map of the SSDLC engine, removed with it (ADR 0007, R1, #538),
which change the policy version too.

``repo``, ``trusted_users`` and ``[apps]`` left ``devloop/config.toml`` with R6 (#425) for the
overlay, beside the agy commands (``ops/host/ssdlc/overlay.toml``): ``devloop token``, which
``scripts/agent-token.py`` calls, and ``scripts/supervisor-token.py`` read them from the
configuration of the project. The check that the installation holds no value of ssdlc is
complete: it leaves out no key.
"""

import dataclasses
import hashlib
import importlib.util
import inspect
import io
import json
import os
import re
import shutil
import string
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from devloop import config, context, localtests, policy
from devloop.config import PinPolicy
from helpers import ssdlc_project

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / 'ops' / 'host' / 'ssdlc' / 'check-equivalence.py'
# The reference of before R3, frozen at its last commit (see the module).
FROZEN = REPO / 'tests' / 'fixtures' / 'ssdlc-pre-r3'
FROZEN_COMMIT = '28eaf9455bad6ff52dcbdbc29ecc07d7dde83e2a'
# The prompt templates of before R4 (ADR 0006), the same as at FROZEN_COMMIT: with R4 the text of
# ssdlc in them is data of the project file. They are ``ssdlc_project.PRE_R4_PROMPTS``, read with
# the changes made since R4 (``ssdlc_project.pre_r4_prompts``).
# devloop/localtests.py of 96b24b83e0e8b63217d956c3766cdc282865c693, the last commit of main
# before R5 (#424), as it was: the selection of the test modules and the reading of the CI logs
# with ssdlc's rules in the code.
PRE_R5 = REPO / 'tests' / 'fixtures' / 'ssdlc-pre-r5' / 'localtests.py'


def pre_r5():
    """The module ``PRE_R5``, loaded without byte code beside it."""
    spec = importlib.util.spec_from_file_location('localtests_pre_r5', PRE_R5)
    module = importlib.util.module_from_spec(spec)
    dont_write, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = dont_write
    return module


def tracked():
    """The files the repository's checkout tracks."""
    out = subprocess.run(['git', 'ls-files', '-z'], cwd=REPO, check=True, capture_output=True,
                         text=True).stdout
    return [name for name in out.split('\0') if name]


def frozen():
    """``effective.json``: ``source``, ``policy_version`` and ``config`` of before R3."""
    return json.loads((FROZEN / 'effective.json').read_text(encoding='utf-8'))


def frozen_config():
    """The frozen ``config.toml`` of before R3, parsed by this code."""
    return config.parse_config(config.read_toml(FROZEN / 'config.toml'))


def composed(overlay=ssdlc_project.OVERLAY, project=ssdlc_project.PROJECT):
    """ssdlc's configuration as devloop composes it on the host since R3."""
    return config.load_config(None, overlay, project)


def as_json(cfg):
    """``cfg`` as a dict in the form of ``effective.json``: tuples are lists."""
    return json.loads(json.dumps(dataclasses.asdict(cfg)))


def without_hash(values):
    """``values`` (``as_json``) without the hash of the project file (declared, see the
    module), nor the project's data in the prompts (ADR 0006, R4): before R4 that text was in
    the templates, and the equivalence is that of the rendered prompts (``prompts``)."""
    values = json.loads(json.dumps(values))
    values['review_policy'].pop('project')
    values.pop('prompts', None)
    return values


def differing(values, reference):
    """The top-level keys whose values differ between ``values`` and ``reference``."""
    return {key for key in set(values) | set(reference) if values.get(key) != reference.get(key)}


def strings(value):
    """Every string among the values of ``value``, its tables and lists walked down."""
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set().union(*map(strings, value.values())) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*map(strings, value)) if value else set()
    return set()


def prompts(cfg, templates=None):
    """The prompts rendered with the values of ``cfg``: by this code, or, with ``templates``,
    the templates of that directory as the code before R4 rendered them (the reference of
    before R3, whose prompts are those of before R4: ``tests/test_devloop_prompt_data.py``)."""
    values = dict(repo=cfg.repo, base_branch=cfg.base_branch, test_cmd=cfg.test_cmd,
                  language=cfg.language, docs=context.docs_map_section(cfg.docs.entries))
    if templates is not None:
        return {p.stem: string.Template(p.read_text(encoding='utf-8')).safe_substitute(values)
                for p in templates.glob('*.md')}
    values.update(context.prompt_values(cfg))
    return {p.stem: context._template(p.stem, **values) for p in context.PROMPT_DIR.glob('*.md')}


class EquivalenceTests(unittest.TestCase):
    """Before: the reference frozen at the last commit before R3 (see the module). After: the
    installation's file, ssdlc's project file and the keys the overlay gains."""

    def setUp(self):
        self.frozen = frozen()
        self.before = frozen_config()
        self.after = composed()

    def test_the_reference_is_frozen_at_the_commit_before_r3(self):
        self.assertEqual(self.frozen['source']['commit'], FROZEN_COMMIT)
        # The file of before as it was: the facts of ssdlc in the installation's file.
        raw = config.read_toml(FROZEN / 'config.toml')
        self.assertEqual(raw['test_cmd'], 'python3 scripts/run-tests.py')
        self.assertEqual(raw['priority_labels'], ['prerequisito'])
        # This code reads it as the code of that commit did: the reference of the prompts. The
        # project's data in the prompts (R4) is not in that file: its text is in the templates.
        self.assertEqual(self.before.prompts, config.PromptFacts())
        values = as_json(self.before)
        values.pop('prompts')
        self.assertEqual(values, self.frozen['config'])
        self.assertEqual(self.before.review_policy.version(), self.frozen['policy_version'])

    def test_the_values_are_the_same_key_by_key(self):
        self.assertEqual(self.frozen['config']['review_policy']['project'], '')
        before, after = without_hash(self.frozen['config']), without_hash(as_json(self.after))
        for key in sorted(set(before) | set(after)):
            with self.subTest(key=key):
                self.assertEqual(after.get(key), before.get(key))
        self.assertEqual(after, before)

    def test_a_value_migrated_wrongly_is_a_difference(self):
        # One value changed in a copy of the project file, the rest as it is: the comparison
        # with the reference names that key, and only it.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        old = ssdlc_project.PROJECT.read_text(encoding='utf-8')
        for key, before, wrong in (
                ('priority_labels', 'priority_labels = ["prerequisito"]',
                 'priority_labels = ["altra"]'),
                ('language', 'language = "Italian"', 'language = "English"'),
                ('local_tests', 'local_tests = "changed"', 'local_tests = "full"')):
            with self.subTest(key=key):
                self.assertIn(before, old)
                project = tmp / f'{key}.toml'
                project.write_text(old.replace(before, wrong), encoding='utf-8')
                values = without_hash(as_json(composed(project=project)))
                self.assertEqual(differing(values, without_hash(self.frozen['config'])), {key})

    def test_tests_left_behind_are_a_difference(self):
        # ADR 0006, R5: without [tests] "changed" is "full" and the re-run of unrelated
        # failures is off; without one rule the selection differs.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        old = ssdlc_project.PROJECT.read_text(encoding='utf-8')
        start, end = old.index('[tests]\n'), old.index('[review_policy]\n')
        for name, text, keys in (
                ('none', old[:start] + old[end:], {'tests', 'local_tests', 'ci_rerun_unrelated'}),
                ('dir_modules', old.replace('"devloop/" = "test_devloop"\n', ''), {'tests'})):
            with self.subTest(name=name):
                self.assertNotEqual(text, old)
                project = tmp / f'{name}.toml'
                project.write_text(text, encoding='utf-8')
                values = without_hash(as_json(composed(project=project)))
                self.assertEqual(differing(values, without_hash(self.frozen['config'])), keys)

    def test_the_helper_of_the_other_tests_is_the_reference(self):
        # tests/helpers/ssdlc_project.py rebuilds the configuration of before for the other
        # modules: it is the frozen one, with the overlay keys or without.
        values = as_json(ssdlc_project.load_by_role())
        values.pop('prompts')  # R4: compared through the rendered prompts
        self.assertEqual(values, self.frozen['config'])
        self.assertEqual(
            without_hash(as_json(ssdlc_project.load_by_role(ssdlc_project.OVERLAY))),
            without_hash(self.frozen['config']))
        # The tests written before #499 read it with the keys of before: only the agents and
        # the roles differ, and only by the keys by role.
        legacy = as_json(ssdlc_project.load())
        legacy.pop('prompts')
        self.assertEqual(legacy['implementer'], 'claude')
        self.assertEqual(legacy['reviewers'], ['codex', 'agy'])
        self.assertEqual(sorted(legacy['agents']), ['agy', 'claude', 'codex'])
        for key, before in ssdlc_project.KEYS_BEFORE_499.items():
            self.assertEqual(dict(legacy['agents'][before], key=key),
                             dict(values['agents'][key], app='', provider='',
                                  fallback_models=[dict(f, provider='') for f in
                                                   values['agents'][key]['fallback_models']]))
        self.assertEqual(differing(legacy, values),
                         {'implementer', 'reviewer', 'reviewers', 'agents', 'review_policy'})

    def test_the_one_file_form_is_the_installation_with_the_project_keys(self):
        self.assertEqual(tomllib.loads(ssdlc_project.text()), ssdlc_project.by_role())
        installation = config.read_toml(config.DEFAULT_CONFIG)
        for key, value in ssdlc_project.project().items():
            if isinstance(value, dict):
                for inner in value:
                    self.assertNotIn(inner, installation.get(key, {}), f'{key}.{inner}')
            else:
                self.assertNotIn(key, installation, key)

    def test_the_hash_of_the_project_file_is_left_out_by_design(self):
        # R2: the hash enters the policy version, so the version differs; without it, the
        # policy of after is the one of before.
        digest = hashlib.sha256(ssdlc_project.PROJECT.read_bytes()).hexdigest()
        self.assertEqual(self.after.review_policy.project, digest)
        self.assertNotEqual(self.after.review_policy.version(), self.frozen['policy_version'])
        self.assertEqual(dataclasses.replace(self.after.review_policy, project='').version(),
                         self.frozen['policy_version'])

    def test_the_rendered_prompts_are_the_same(self):
        # The templates of before R4 with the changes made since (``ssdlc_project.SINCE_R4``).
        with tempfile.TemporaryDirectory() as tmp:
            before = prompts(self.before, ssdlc_project.pre_r4_prompts(tmp))
        after = prompts(self.after)
        self.assertTrue(before)
        self.assertEqual(after, before)
        self.assertIn('Italian', after['implement'])
        self.assertIn('python3 scripts/run-tests.py', after['implement'])

    def test_the_selected_test_modules_are_the_same(self):
        # ADR 0006, R5: the rules of the selection are ssdlc's [tests], no longer the code's.
        # The code of before, frozen (PRE_R5), and the code of now with ssdlc's project file
        # pick the same modules for every file the repository tracks, on its own and together.
        self.assertEqual((self.frozen['config']['local_tests'], self.after.local_tests),
                         ('changed', 'changed'))
        old = pre_r5()
        self.assertEqual(list(inspect.signature(old.select_modules).parameters),
                         ['changed', 'modules'])
        modules = sorted(p.name for p in (REPO / 'tests').glob('test_*.py'))
        files = tracked() + ['ssdlc_new.py', 'devloop/new/x.md', 'tests/test_gone.py',
                             'pkg/ssdlc_impact.py', 'ssdlc_.py', 'scripts/devloop-pinned']
        self.assertGreater(len(files), 100)
        for path in files:
            self.assertEqual(localtests.select_modules([path], modules, self.after.tests),
                             old.select_modules([path], modules), path)
        self.assertEqual(localtests.select_modules(files, modules, self.after.tests),
                         old.select_modules(files, modules))
        self.assertEqual(localtests.module_command(modules), old.module_command(modules))

    def test_the_ci_logs_are_read_as_before(self):
        # The format of ssdlc's CI logs is the one the code of before read, so the re-run of
        # unrelated failures (#200) and the summary of the suite (#300) stay on.
        self.assertEqual((self.after.tests.ci_log_format, self.after.ci_rerun_unrelated),
                         ('run-tests', True))
        old = pre_r5()
        prefix = 'Unit Tests\tRun tests\t2026-11-16T10:00:00Z '
        log = '\n'.join(prefix + line for line in (
            'FAILED MODULE tests.test_impact', 'FAILED MODULE test_devloop_parallel',
            'Slowest: test_a 12s', 'Ran 9 tests in 2.500s (4 modules, 1 split by class, 2 jobs)',
            'FAILED (modules=2, worker problems=0)'))
        for text in (log, '', 'Ran 3 tests in 1.0s', log.replace('problems=0', 'problems=1')):
            self.assertEqual(localtests.failed_modules(text), old.failed_modules(text))
            self.assertEqual(localtests.suite_summary(text), old.suite_summary(text))

    def test_the_restrictions_ssdlc_wants_guaranteed_are_on_the_host(self):
        # ADR 0006, section 3.2, requirement 5: devloop/ sensitive, from the project file.
        # core/ is not among them today, and R3 does not add it. schemas/, the engine's, and the
        # list of the replay gate's baseline left with the engine (ADR 0007, R1).
        paths = self.after.review_policy.sensitive_paths
        self.assertIn('devloop/', paths)
        self.assertNotIn('schemas/', paths)
        self.assertNotIn('core/', paths)
        self.assertEqual(ssdlc_project.project()['review_policy']['sensitive_paths'], list(paths))
        self.assertEqual(self.after.review_policy.sensitive_files, ())
        self.assertEqual(self.after.pin.paths, ('devloop/', 'core/'))

    def test_without_the_overlay_keys_agy_loses_the_test_command(self):
        # The agents' commands derived from the project's tests are the overlay's alone (D2):
        # without its keys the configuration is not equivalent, and the check says so.
        bare = composed(overlay=ssdlc_project.identity_overlay(self))
        self.assertNotIn('python3 scripts/run-tests.py',
                         bare.agents['agy-reviewer'].review_commands)
        self.assertNotEqual(dataclasses.replace(bare.review_policy, project='').version(),
                            self.frozen['policy_version'])

    def test_without_a_project_file_devloop_does_not_start(self):
        with self.assertRaisesRegex(config.ConfigError, "missing 'test_cmd'.*project file"):
            config.load_config()

    def test_the_identities_come_from_the_overlay(self):
        # R6: repo, trusted_users and the apps, as before, from the overlay alone.
        reference = self.frozen['config']
        self.assertEqual(self.after.repo, reference['repo'])
        self.assertEqual(list(self.after.trusted_users), reference['trusted_users'])
        self.assertEqual(as_json(self.after)['apps'], reference['apps'])
        self.assertEqual(self.after.app_key_dir, reference['app_key_dir'])

    def test_without_the_identities_in_the_overlay_devloop_does_not_start(self):
        # The overlay with the agy commands only, as after R3: the copy of R6 does not start.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        agy = tomllib.loads(ssdlc_project.OVERLAY.read_text(encoding='utf-8'))['agents']
        overlay = tmp / 'config.local.toml'
        overlay.write_text(ssdlc_project.toml_text({'agents': agy}) + '\n', encoding='utf-8')
        with self.assertRaisesRegex(config.ConfigError, 'trusted_users.*local overlay'):
            composed(overlay=overlay)
        overlay.write_text(ssdlc_project.toml_text(
            {'trusted_users': ['brushtyler'], 'agents': agy}) + '\n', encoding='utf-8')
        with self.assertRaisesRegex(config.ConfigError, "missing 'repo'.*local overlay"):
            composed(overlay=overlay)


class InstallationTests(unittest.TestCase):
    """The installation holds no value of ssdlc (ADR 0006, section 3.5), but for the code values
    left to other issues, each declared here. Since R6 no key is left out."""

    def test_config_toml_sets_no_fact_nor_restriction_of_the_project(self):
        installation = config.read_toml(config.DEFAULT_CONFIG)
        for key in ('test_cmd', 'local_tests', 'language', 'priority_labels', 'docs', 'prompts'):
            self.assertNotIn(key, installation, key)
        for path in config.PROJECT_RESTRICTIONS:
            node = installation
            for key in path:
                node = node.get(key, {}) if isinstance(node, dict) else {}
            self.assertIn(node, ({}, []), '.'.join(path))

    def test_config_toml_holds_no_value_of_ssdlc(self):
        installation = config.read_toml(config.DEFAULT_CONFIG)
        ssdlc = strings(ssdlc_project.project()) | strings(ssdlc_project.identity())
        self.assertEqual(strings(installation) & ssdlc, set())
        # R6: the identities and trust of the project are the overlay's (ADR 0006, D3 and D6).
        for key in ssdlc_project.IDENTITY_KEYS:
            self.assertNotIn(key, installation, key)

    def test_the_installation_names_no_identity_of_ssdlc(self):
        # Not in a value, a default of the code or a comment: no owner, repository or app slug
        # of ssdlc in what the pin extracts (devloop/ and core/).
        identity = ssdlc_project.identity()
        owner = identity['repo'].split('/')[0]
        app_ids = {str(app['app_id']) for app in identity['apps'].values()
                   if isinstance(app, dict)}
        for package in ('devloop', 'core'):
            for path in sorted((REPO / package).rglob('*')):
                if not path.is_file() or '__pycache__' in path.parts:
                    continue
                text = path.read_text(encoding='utf-8')
                with self.subTest(path=str(path.relative_to(REPO))):
                    self.assertNotIn(owner, text)
                    self.assertEqual({i for i in app_ids if i in text}, set())

    def test_the_code_has_no_default_of_ssdlc_for_the_project_keys(self):
        self.assertEqual(policy.ReviewPolicy.sensitive_paths, ())
        self.assertEqual(config.DEFAULT_SENSITIVE_LISTS, ())
        # The pin watches the installation's own directories, which it extracts, and the lists.
        self.assertEqual(PinPolicy().paths, ('devloop/', 'core/'))
        ssdlc = strings(ssdlc_project.project())
        self.assertEqual(ssdlc & {*policy.ReviewPolicy.sensitive_paths,
                                  *config.DEFAULT_SENSITIVE_LISTS}, set())

    def test_the_code_values_left_to_other_issues_are_declared(self):
        # Without a key of the project file, they stay in the code (R3 defines no key):
        # the level-0 exclusions for good.
        self.assertEqual(policy.ZERO_EXCLUDED,
                         ('devloop/prompts/', 'standards/', '.github/', 'schemas/'))

    def test_the_code_has_no_rule_of_ssdlc_for_its_tests(self):
        # ADR 0006, R5: the name rules of the selection are parameters of the project file.
        self.assertEqual(config.Config.tests, localtests.Tests())
        self.assertFalse(hasattr(localtests, 'DEVLOOP_DIR'))
        self.assertFalse(hasattr(localtests, 'DEVLOOP_TESTS'))
        source = inspect.getsource(localtests)
        rules = ssdlc_project.project()['tests']
        for rule in (*rules.get('source_prefixes', ()), *rules['dir_modules'].items()):
            for text in ([rule] if isinstance(rule, str) else rule):
                self.assertNotIn(repr(text), source)
                self.assertNotIn(f'"{text}"', source)


class ExampleFilesTests(unittest.TestCase):
    def test_the_project_file_has_only_keys_of_the_project_file(self):
        table = config.read_project_toml(ssdlc_project.PROJECT)
        config.compose({}, table)  # the closed schema refuses any other key
        self.assertEqual(set(table) - set(config.PROJECT_KEYS), set())

    def test_the_overlay_keys_are_the_agy_commands_and_the_identities(self):
        table = config.read_toml(ssdlc_project.OVERLAY, 'local config')
        # The table of agy's key by role since #499.
        self.assertEqual(table, {**ssdlc_project.identity(), 'agents': {'agy-reviewer': {
            'write_commands': table['agents']['agy-reviewer']['write_commands'],
            'review_commands': table['agents']['agy-reviewer']['review_commands']}}})
        # R6: the values config.toml had before, frozen with the reference of before R3.
        frozen_raw = config.read_toml(FROZEN / 'config.toml')
        self.assertEqual(ssdlc_project.identity(),
                         {key: frozen_raw[key] for key in ssdlc_project.IDENTITY_KEYS})
        # The commands agy had by default from the test_cmd of config.toml.
        expected = ('python3 scripts/run-tests.py',) + config.AGY_DEFAULT_COMMANDS
        after = composed()
        self.assertEqual(after.agents['agy-reviewer'].write_commands, expected)
        self.assertEqual(after.agents['agy-reviewer'].review_commands, expected)

    def test_the_supervisor_token_reads_repo_and_apps_from_the_overlay(self):
        # R6: from the overlay of the project's state_dir, without a project file.
        spec = importlib.util.spec_from_file_location(
            'supervisor_token', REPO / 'scripts' / 'supervisor-token.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, state)
        shutil.copy(ssdlc_project.OVERLAY, state / 'config.local.toml')
        cfg = module.load_config(state)
        self.assertEqual(cfg.repo, 'brushtyler/ssdlc')
        self.assertEqual(cfg.apps, ssdlc_project.load().apps)
        # The installation alone has neither: without the overlay it stops, naming it.
        (state / 'config.local.toml').unlink()
        with self.assertRaisesRegex(SystemExit, 'no local overlay.*--state-dir'):
            module.load_config(state)


class FirstPinTests(unittest.TestCase):
    """The first pin the documents give runs the clone's devloop, not the launcher: since R3
    it passes the project file and the overlay itself, before the command."""

    STATE = Path('.ssdlc') / 'devloop'
    ARGV = ['--project-config', '.ssdlc/devloop/project.toml',
            '--local-config', '.ssdlc/devloop/config.local.toml', 'pin', 'origin/main']
    DOCS = ('ops/README.md', 'ops/host/migrate.md', 'docs/devloop/guide.md')

    def setUp(self):
        from devloop import __main__ as cli
        self.cli = cli
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        state = self.root / self.STATE
        state.mkdir(parents=True)
        # The host as ops/host/migrate.md leaves it: the example project file copied, the
        # overlay with the keys of ops/host/ssdlc/overlay.toml. The installation is this one.
        shutil.copy(ssdlc_project.PROJECT, state / 'project.toml')
        shutil.copy(ssdlc_project.OVERLAY, state / 'config.local.toml')
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)

    def pin(self, argv):
        with mock.patch.dict(os.environ), \
                mock.patch.object(self.cli, 'main_checkout', return_value=self.root), \
                mock.patch.object(self.cli.pin, 'pin') as pinned:
            os.environ.pop(self.cli.STATE_ENV, None)
            self.cli.main(argv)
        return pinned

    def test_the_documents_give_the_command(self):
        for doc in self.DOCS:
            text = re.sub(r'\\\n\s*', '', (REPO / doc).read_text(encoding='utf-8'))
            with self.subTest(doc=doc):
                self.assertIn('python3 -m devloop ' + ' '.join(self.ARGV), text)

    def test_it_pins_with_the_distributed_configuration(self):
        pinned = self.pin(self.ARGV)
        self.assertEqual(Path(pinned.call_args.kwargs['project']).resolve(),
                         (self.root / self.STATE / 'project.toml').resolve())
        self.assertEqual(pinned.call_args.args[2], 'origin/main')

    def test_without_the_project_file_it_does_not_start(self):
        with self.assertRaisesRegex(SystemExit, "missing 'test_cmd'"):
            self.pin(['pin', 'origin/main'])


def load_script():
    # No __pycache__ beside the script: ops/ holds only the files of the repository.
    spec = importlib.util.spec_from_file_location('check_equivalence', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    dont_write, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = dont_write
    return module


class CheckScriptTests(unittest.TestCase):
    """``ops/host/ssdlc/check-equivalence.py`` on two installations: before, with the
    ``config.toml`` frozen at the commit before R3 (see the module); after, the installation of
    R3 with the project file."""

    @classmethod
    def setUpClass(cls):
        cls.script = load_script()
        cls.tmp = Path(tempfile.mkdtemp())
        for side in ('before', 'after'):
            for package in ('devloop', 'core'):
                shutil.copytree(REPO / package, cls.tmp / side / package,
                                ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy(FROZEN / 'config.toml', cls.tmp / 'before' / 'devloop' / 'config.toml')
        # The installation of before R3 had the prompts of before R4, with the text of ssdlc;
        # the changes of the templates made since R4 are in both (``ssdlc_project.SINCE_R4``).
        shutil.rmtree(cls.tmp / 'before' / 'devloop' / 'prompts')
        ssdlc_project.pre_r4_prompts(cls.tmp / 'before' / 'devloop' / 'prompts')
        cls.project = cls.tmp / 'project.toml'
        shutil.copy(ssdlc_project.PROJECT, cls.project)
        # A host already on R3 (R4): the installation of R3, with the configuration of this one
        # and the prompts of before R4, and its project file, without [prompts].
        shutil.copytree(cls.tmp / 'after', cls.tmp / 'r3')
        shutil.rmtree(cls.tmp / 'r3' / 'devloop' / 'prompts')
        ssdlc_project.pre_r4_prompts(cls.tmp / 'r3' / 'devloop' / 'prompts')
        text = ssdlc_project.PROJECT.read_text(encoding='utf-8')
        start = text.index("# The project's data in the prompts")
        cls.r3_project = cls.tmp / 'project-r3.toml'
        cls.r3_project.write_text(text[:start] + text[text.index('\n[merge]\n', start) + 1:],
                                  encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def check(self, project=None, overlay=ssdlc_project.OVERLAY, before=(), before_project=None):
        argv = ['--before', str(self.tmp / 'before'), *before, '--after', str(self.tmp / 'after'),
                '--after-project', str(project or self.project), '--root', str(REPO)]
        if overlay:
            argv += ['--after-local', str(overlay)]
        if before_project:
            # The installation of before is R3 or later: its project file and overlay.
            argv = ['--before', str(self.tmp / 'after'), '--before-project', str(before_project),
                    '--before-local', str(ssdlc_project.OVERLAY)] + argv[2:]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = self.script.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue() + err.getvalue()

    def test_the_migration_is_equivalent(self):
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn('equivalent:', out)
        self.assertIn('review_policy.project', out)  # declared, not hidden

    def test_it_writes_nothing_in_the_installations(self):
        # Run as root on the host (ops/host/migrate.md), a byte-code file would land in the
        # read-only pinned copy of devloop: each side runs without writing any, whatever the
        # outcome of the comparison.
        code, out = self.check()
        self.assertIn(code, (0, 1), out)
        for side in ('before', 'after'):
            self.assertEqual(list((self.tmp / side).rglob('__pycache__')), [], side)

    def test_relative_paths_are_the_callers(self):
        # As in ops/host/migrate.md: run from a directory with relative paths, the
        # installations in directories of their own; each side runs in its installation.
        shutil.copy(ssdlc_project.OVERLAY, self.tmp / 'overlay.toml')
        done = subprocess.run(
            [sys.executable, '-B', str(SCRIPT), '--before', 'before', '--after', 'after',
             '--after-local', 'overlay.toml', '--after-project', 'project.toml',
             '--root', os.path.relpath(REPO, self.tmp)],
            cwd=self.tmp, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('equivalent:', done.stdout)

    def test_a_value_migrated_wrongly_is_a_difference(self):
        old = ssdlc_project.PROJECT.read_text(encoding='utf-8')
        text = old.replace('priority_labels = ["prerequisito"]', 'priority_labels = ["altra"]')
        self.assertNotEqual(text, old)
        project = self.tmp / 'priority.toml'
        project.write_text(text, encoding='utf-8')
        code, out = self.check(project)
        self.assertEqual(code, 1, out)
        self.assertIn('config priority_labels: before ["prerequisito"], after ["altra"]', out)

    def test_a_restriction_left_behind_is_a_difference(self):
        old = ssdlc_project.PROJECT.read_text(encoding='utf-8')
        text = old.replace('"scripts/agent-token.py", ', '')
        self.assertNotEqual(text, old)
        project = self.tmp / 'short.toml'
        project.write_text(text, encoding='utf-8')
        code, out = self.check(project)
        self.assertEqual(code, 1, out)
        self.assertIn('config review_policy.sensitive_paths:', out)
        self.assertIn('policy version without the project file', out)

    def test_without_the_overlay_keys_agy_differs(self):
        code, out = self.check(overlay=ssdlc_project.identity_overlay(self))
        self.assertEqual(code, 1, out)
        self.assertIn('config agents.agy-reviewer.review_commands:', out)

    def test_without_the_identities_in_the_overlay_the_after_side_does_not_load(self):
        # R6: repo and trusted_users are the overlay's; without them the copy does not start.
        code, out = self.check(overlay=None)
        self.assertEqual(code, 2, out)
        self.assertIn('trusted_users', out)

    def test_a_fact_changed_is_a_difference_in_the_prompts(self):
        text = ssdlc_project.PROJECT.read_text(encoding='utf-8').replace(
            'language = "Italian"', 'language = "English"')
        project = self.tmp / 'english.toml'
        project.write_text(text, encoding='utf-8')
        code, out = self.check(project)
        self.assertEqual(code, 1, out)
        self.assertIn('config language:', out)
        self.assertIn('prompt implement: rendered differently', out)

    def test_a_project_file_without_tests_is_a_difference(self):
        # ADR 0006, R5: the host's project file must gain [tests] before the pin of R5, or
        # "changed" becomes "full" and every file selects differently.
        old = ssdlc_project.PROJECT.read_text(encoding='utf-8')
        project = self.tmp / 'no-tests.toml'
        project.write_text(old[:old.index('[tests]\n')] + old[old.index('[review_policy]\n'):],
                           encoding='utf-8')
        code, out = self.check(project)
        self.assertEqual(code, 1, out)
        self.assertIn('local_tests: before changed, after full', out)
        self.assertIn('config tests.selection: before "python-unittest-modules", after ""', out)
        self.assertIn('config ci_rerun_unrelated: before true, after false', out)

    def test_the_installation_of_before_may_read_a_project_file(self):
        # As on a host already migrated to R3: before is an installation with its project
        # file (--before-project). The same file on both sides is equivalent.
        code, out = self.check(before_project=self.project)
        self.assertEqual(code, 0, out)
        self.assertIn('equivalent:', out)
        other = self.tmp / 'english-before.toml'
        other.write_text(ssdlc_project.PROJECT.read_text(encoding='utf-8').replace(
            'language = "Italian"', 'language = "English"'), encoding='utf-8')
        code, out = self.check(before_project=other)
        self.assertEqual(code, 1, out)
        self.assertIn('config language: before "English", after "Italian"', out)

    def test_r4_on_a_host_already_on_r3_is_equivalent(self):
        # The installation of R3 with its project file, against R4 with [prompts] added: the
        # same configuration and the same rendered prompts; the data of [prompts] is declared.
        self.assertNotIn('[prompts]', self.r3_project.read_text(encoding='utf-8'))
        r3 = ['--before', str(self.tmp / 'r3'), '--before-local', str(ssdlc_project.OVERLAY),
              '--before-project', str(self.r3_project)]
        code, out = self.check(before=r3)
        self.assertEqual(code, 0, out)
        self.assertIn('equivalent:', out)
        self.assertIn('prompts.*', out)

    def test_r4_without_prompts_in_the_project_file_differs(self):
        # Without [prompts] the sentences of ssdlc are dropped: the prompts differ.
        r3 = ['--before', str(self.tmp / 'r3'), '--before-local', str(ssdlc_project.OVERLAY),
              '--before-project', str(self.r3_project)]
        code, out = self.check(self.r3_project, before=r3)
        self.assertEqual(code, 1, out)
        for name in ('implement', 'fix', 'sync', 'preflight', 'review', 'advisory',
                     'conclave_proposal', 'conclave_vote'):
            self.assertIn(f'prompt {name}: rendered differently', out)
        self.assertNotIn('config ', out)

    def test_a_side_that_does_not_load_stops_the_check(self):
        project = self.tmp / 'bad.toml'
        project.write_text('trusted_users = ["someone"]\n', encoding='utf-8')
        code, out = self.check(project)
        self.assertEqual(code, 2)
        self.assertIn('does not load', out)


if __name__ == '__main__':
    unittest.main()
