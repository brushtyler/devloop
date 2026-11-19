"""devloop: ssdlc's configuration in its project file (ADR 0006, R3).

The facts of ssdlc and the restrictions it adds left ``devloop/config.toml`` for the project file
on the host, whose example is ``ops/host/ssdlc/project.toml``; the agy commands derived from
ssdlc's tests for the overlay (``ops/host/ssdlc/overlay.toml``). Nothing changes for devloop on
ssdlc: the same values of the effective configuration, the same rendered prompts and the same
selected test modules. The SHA-256 of the project file is left out of the comparison on purpose:
R2 puts it in the policy version by construction, so the version is compared without it.

``repo``, ``trusted_users`` and ``[apps]`` stay in ``devloop/config.toml`` until R6 (#425), which
brings ``devloop token`` with the configuration of the project: ``scripts/agent-token.py`` and
``scripts/supervisor-token.py`` read them there. The check that the installation holds no value
of ssdlc leaves them out, and says so.
"""

import dataclasses
import hashlib
import importlib.util
import inspect
import io
import shutil
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from devloop import config, context, localtests, policy
from devloop.config import PinPolicy
from helpers import ssdlc_project

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / 'ops' / 'host' / 'ssdlc' / 'check-equivalence.py'
LIST = 'schemas/recovery-grader-baseline.json'
# Left in the installation until R6 (#425): the token scripts read them from config.toml.
UNTIL_R6 = ('repo', 'trusted_users', 'apps')


def composed(overlay=ssdlc_project.OVERLAY):
    """ssdlc's configuration as devloop composes it on the host since R3."""
    return config.load_config(None, overlay, ssdlc_project.PROJECT)


def without_hash(cfg):
    """``cfg`` as a dict, without the hash of the project file (declared, see the module)."""
    values = dataclasses.asdict(cfg)
    values['review_policy'].pop('project')
    return values


def strings(value):
    """Every string among the values of ``value``, its tables and lists walked down."""
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set().union(*map(strings, value.values())) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*map(strings, value)) if value else set()
    return set()


def prompts(cfg):
    values = dict(repo=cfg.repo, base_branch=cfg.base_branch, test_cmd=cfg.test_cmd,
                  language=cfg.language, docs=context.docs_map_section(cfg.docs.entries))
    return {p.stem: context._template(p.stem, **values) for p in context.PROMPT_DIR.glob('*.md')}


class EquivalenceTests(unittest.TestCase):
    """Before: ssdlc's configuration as one file, as ``devloop/config.toml`` was. After: the
    installation's file, ssdlc's project file and the keys the overlay gains."""

    def setUp(self):
        self.before = ssdlc_project.load()
        self.after = composed()

    def test_the_one_file_form_is_the_installation_with_the_project_keys(self):
        self.assertEqual(tomllib.loads(ssdlc_project.text()), ssdlc_project.data())
        installation = config.read_toml(config.DEFAULT_CONFIG)
        for key, value in ssdlc_project.project().items():
            if isinstance(value, dict):
                for inner in value:
                    self.assertNotIn(inner, installation.get(key, {}), f'{key}.{inner}')
            else:
                self.assertNotIn(key, installation, key)

    def test_the_values_are_the_same_key_by_key(self):
        before, after = without_hash(self.before), without_hash(self.after)
        for key in sorted(set(before) | set(after)):
            with self.subTest(key=key):
                self.assertEqual(after.get(key), before.get(key))
        self.assertEqual(after, before)

    def test_the_hash_of_the_project_file_is_left_out_by_design(self):
        # R2: the hash enters the policy version, so the version differs; without it, the
        # policy of after is the one of before.
        digest = hashlib.sha256(ssdlc_project.PROJECT.read_bytes()).hexdigest()
        self.assertEqual(self.after.review_policy.project, digest)
        self.assertEqual(self.before.review_policy.project, '')
        self.assertNotEqual(self.after.review_policy.version(),
                            self.before.review_policy.version())
        self.assertEqual(dataclasses.replace(self.after.review_policy, project='').version(),
                         self.before.review_policy.version())

    def test_the_rendered_prompts_are_the_same(self):
        before, after = prompts(self.before), prompts(self.after)
        self.assertTrue(before)
        self.assertEqual(after, before)
        self.assertIn('Italian', after['implement'])
        self.assertIn('python3 scripts/run-tests.py', after['implement'])

    def test_the_selected_test_modules_are_the_same(self):
        # The selection reads no configuration but local_tests (its rules stay in the code
        # until R5, #424): the same mode selects the same modules. The script compares them
        # file by file on two installations (CheckScriptTests).
        self.assertEqual((self.before.local_tests, self.after.local_tests), ('changed', 'changed'))
        self.assertEqual(list(inspect.signature(localtests.select_modules).parameters),
                         ['changed', 'modules'])

    def test_the_restrictions_ssdlc_wants_guaranteed_are_on_the_host(self):
        # ADR 0006, section 3.2, requirement 5: devloop/ and schemas/ sensitive, from the
        # project file. core/ is not among them today, and R3 does not add it.
        paths = self.after.review_policy.sensitive_paths
        self.assertIn('devloop/', paths)
        self.assertIn('schemas/', paths)
        self.assertNotIn('core/', paths)
        self.assertEqual(ssdlc_project.project()['review_policy']['sensitive_paths'], list(paths))
        self.assertEqual(self.after.pin.paths, ('devloop/', 'core/', LIST))

    def test_without_the_overlay_keys_agy_loses_the_test_command(self):
        # The agents' commands derived from the project's tests are the overlay's alone (D2):
        # without its keys the configuration is not equivalent, and the check says so.
        bare = composed(overlay=None)
        self.assertNotIn('python3 scripts/run-tests.py', bare.agents['agy'].review_commands)
        self.assertNotEqual(dataclasses.replace(bare.review_policy, project='').version(),
                            self.before.review_policy.version())

    def test_without_a_project_file_devloop_does_not_start(self):
        with self.assertRaisesRegex(config.ConfigError, "missing 'test_cmd'.*project file"):
            config.load_config()


class InstallationTests(unittest.TestCase):
    """The installation holds no value of ssdlc (ADR 0006, section 3.5), but for the keys left
    to R6 and the code values left to other issues, each declared here."""

    def test_config_toml_sets_no_fact_nor_restriction_of_the_project(self):
        installation = config.read_toml(config.DEFAULT_CONFIG)
        for key in ('test_cmd', 'local_tests', 'language', 'priority_labels', 'docs'):
            self.assertNotIn(key, installation, key)
        for path in config.PROJECT_RESTRICTIONS:
            node = installation
            for key in path:
                node = node.get(key, {}) if isinstance(node, dict) else {}
            self.assertIn(node, ({}, []), '.'.join(path))

    def test_config_toml_holds_no_value_of_ssdlc_but_those_left_to_r6(self):
        installation = config.read_toml(config.DEFAULT_CONFIG)
        ssdlc = strings(ssdlc_project.project())
        rest = {k: v for k, v in installation.items() if k not in UNTIL_R6}
        self.assertEqual(strings(rest) & ssdlc, set())
        # Declared: still here until R6 (#425), for scripts/agent-token.py and
        # scripts/supervisor-token.py.
        self.assertEqual(installation['repo'], 'brushtyler/ssdlc')
        self.assertEqual(installation['trusted_users'], ['brushtyler'])
        self.assertIn('dispatcher', installation['apps'])

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
        # the level-0 exclusions for good, the selection of the tests until R5 (#424).
        self.assertEqual(policy.ZERO_EXCLUDED,
                         ('devloop/prompts/', 'standards/', '.github/', 'schemas/'))
        self.assertEqual((localtests.DEVLOOP_DIR, localtests.DEVLOOP_TESTS),
                         ('devloop/', 'test_devloop'))


class ExampleFilesTests(unittest.TestCase):
    def test_the_project_file_has_only_keys_of_the_project_file(self):
        table = config.read_project_toml(ssdlc_project.PROJECT)
        config.compose({}, table)  # the closed schema refuses any other key
        self.assertEqual(set(table) - set(config.PROJECT_KEYS), set())

    def test_the_overlay_keys_are_the_agy_commands_only(self):
        table = config.read_toml(ssdlc_project.OVERLAY, 'local config')
        self.assertEqual(table, {'agents': {'agy': {
            'write_commands': table['agents']['agy']['write_commands'],
            'review_commands': table['agents']['agy']['review_commands']}}})
        # The commands agy had by default from the test_cmd of config.toml.
        expected = ('python3 scripts/run-tests.py',) + config.AGY_DEFAULT_COMMANDS
        after = composed()
        self.assertEqual(after.agents['agy'].write_commands, expected)
        self.assertEqual(after.agents['agy'].review_commands, expected)

    def test_the_token_scripts_read_repo_and_apps_without_a_project_file(self):
        for name in ('agent-token', 'supervisor-token'):
            spec = importlib.util.spec_from_file_location(
                name.replace('-', '_'), REPO / 'scripts' / f'{name}.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            cfg = module.load_config(config.DEFAULT_CONFIG)
            with self.subTest(script=name):
                self.assertEqual(cfg.repo, 'brushtyler/ssdlc')
                self.assertEqual(cfg.apps, ssdlc_project.load().apps)


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
    """``ops/host/ssdlc/check-equivalence.py`` on two installations: before, ssdlc's
    configuration as one file; after, the installation of R3 with the project file."""

    @classmethod
    def setUpClass(cls):
        cls.script = load_script()
        cls.tmp = Path(tempfile.mkdtemp())
        for side in ('before', 'after'):
            for package in ('devloop', 'core'):
                shutil.copytree(REPO / package, cls.tmp / side / package,
                                ignore=shutil.ignore_patterns('__pycache__'))
            (cls.tmp / side / LIST).parent.mkdir(parents=True)
            shutil.copy(REPO / LIST, cls.tmp / side / LIST)
        (cls.tmp / 'before' / 'devloop' / 'config.toml').write_text(ssdlc_project.text(),
                                                                    encoding='utf-8')
        cls.project = cls.tmp / 'project.toml'
        shutil.copy(ssdlc_project.PROJECT, cls.project)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def check(self, project=None, overlay=ssdlc_project.OVERLAY):
        argv = ['--before', str(self.tmp / 'before'), '--after', str(self.tmp / 'after'),
                '--after-project', str(project or self.project), '--root', str(REPO)]
        if overlay:
            argv += ['--after-local', str(overlay)]
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

    def test_a_restriction_left_behind_is_a_difference(self):
        old = ssdlc_project.PROJECT.read_text(encoding='utf-8')
        text = old.replace('"ssdlc_grader.py",', '')
        self.assertNotEqual(text, old)
        project = self.tmp / 'short.toml'
        project.write_text(text, encoding='utf-8')
        code, out = self.check(project)
        self.assertEqual(code, 1, out)
        self.assertIn('config review_policy.sensitive_paths:', out)
        self.assertIn('policy version without the project file', out)

    def test_without_the_overlay_keys_agy_differs(self):
        code, out = self.check(overlay=None)
        self.assertEqual(code, 1, out)
        self.assertIn('config agents.agy.review_commands:', out)

    def test_a_fact_changed_is_a_difference_in_the_prompts(self):
        text = ssdlc_project.PROJECT.read_text(encoding='utf-8').replace(
            'language = "Italian"', 'language = "English"')
        project = self.tmp / 'english.toml'
        project.write_text(text, encoding='utf-8')
        code, out = self.check(project)
        self.assertEqual(code, 1, out)
        self.assertIn('config language:', out)
        self.assertIn('prompt implement: rendered differently', out)

    def test_a_side_that_does_not_load_stops_the_check(self):
        project = self.tmp / 'bad.toml'
        project.write_text('trusted_users = ["someone"]\n', encoding='utf-8')
        code, out = self.check(project)
        self.assertEqual(code, 2)
        self.assertIn('does not load', out)


if __name__ == '__main__':
    unittest.main()
