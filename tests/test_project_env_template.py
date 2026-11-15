"""The generated project.env template must only contain settings the code reads, with the code defaults."""
import contextlib
import io
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import orchestrator as o
import ssdlc_supervisor as s

ROOT = Path(o.__file__).resolve().parent
SOURCES = {p.name: p.read_text(encoding='utf-8') for p in ROOT.glob('*.py')}

# Template values that intentionally differ from the code default, with the reason.
DEFAULT_EXCEPTIONS = {
    'SSDLC_PROJECT_ID': 'set from --id or the project directory name',
    'SSDLC_EXECUTION_MODE': 'set from --mode',
    'SSDLC_SYNC_REMOTE': 'set from --sync-remote',
    'SSDLC_REVIEW_ENGINE': '"auto" keeps 04-review dynamic and resolve_review_entry maps it to openrouter',
    **{name: '"auto" is equivalent to unset in SquadDispatcher.dispatch_role'
       for name in o.ROLE_OVERRIDE_ENV_VARS if name.endswith('_ENGINE')},
}


def read_patterns(name):
    quoted = r"""['"]%s['"]""" % re.escape(name)
    return (r'(?:environ|env)\.get\(\s*' + quoted, r'environ\[\s*' + quoted, quoted + r'\s+in\s+os\.environ',
            r'\(\s*' + quoted + r"""\s*,\s*['"]""")


def code_reads(name):
    if name in o.ROLE_OVERRIDE_ENV_VARS:
        return True
    return any(re.search(p, src) for src in SOURCES.values() for p in read_patterns(name))


def code_defaults(name):
    if name in o.ROLE_OVERRIDE_ENV_VARS:
        return {''}
    quoted = r"""['"]%s['"]""" % re.escape(name)
    pattern = r'(?:\.get|)\(\s*' + quoted + r"""\s*,\s*['"]([^'"]*)['"]\s*\)"""
    return {m for src in SOURCES.values() for m in re.findall(pattern, src)}


def generate_template(tmp):
    project, home = Path(tmp) / 'project', Path(tmp) / 'home'
    project.mkdir()
    home.mkdir()
    result = subprocess.run(['bash', str(ROOT / 'setup-ssdlc.sh'), '--init-project', str(project), '--id', 'tpl',
                             '--mode', 'local', '--sync-remote', 'false'],
                            env={'PATH': os.defpath, 'HOME': str(home)}, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return project / '.ssdlc/project.env'


def template_assignments(env_file):
    out = []
    for line in env_file.read_text(encoding='utf-8').splitlines():
        match = re.match(r'export\s+([A-Z0-9_]+)=(.*)$', line.strip())
        if match:
            value = shlex.split(match.group(2), comments=True)
            out.append((match.group(1), value[0] if value else ''))
    return out


class TemplateMatchesCodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env_file = generate_template(cls.tmp.name)
        cls.assignments = template_assignments(cls.env_file)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_template_has_no_duplicates(self):
        names = [name for name, _ in self.assignments]
        self.assertEqual(sorted(n for n in set(names) if names.count(n) > 1), [])

    def test_template_only_generates_variables_read_by_code(self):
        self.assertTrue(self.assignments)
        for name, _ in self.assignments:
            with self.subTest(name=name):
                self.assertNotIn(name, o.OBSOLETE_PROJECT_ENV_VARS)
                self.assertIn(name, o.KNOWN_PROJECT_ENV_VARS)
                self.assertTrue(code_reads(name), f'{name} is generated but never read')

    def test_template_values_match_code_defaults(self):
        for name, value in self.assignments:
            if name in DEFAULT_EXCEPTIONS:
                continue
            with self.subTest(name=name):
                defaults = code_defaults(name)
                self.assertEqual(defaults, {value}, f'{name}: template {value!r}, code defaults {defaults}')

    def test_design_override_names_match_dispatcher(self):
        names = {name for name, _ in self.assignments}
        self.assertTrue({'SSDLC_ARCH_ENGINE', 'SSDLC_ARCH_MODEL', 'SSDLC_SEC_ENGINE', 'SSDLC_SEC_MODEL'} <= names)
        with patch.dict(os.environ, {'SSDLC_ARCH_ENGINE': 'codex', 'SSDLC_ARCH_MODEL': 'm1',
                                     'SSDLC_SEC_ENGINE': 'claude', 'SSDLC_SEC_MODEL': 'm2'}):
            self.assertEqual(o.SquadDispatcher.dispatch_role('02-design-arch')[:2], ('codex', 'm1'))
            self.assertEqual(o.SquadDispatcher.dispatch_role('02-design-sec')[:2], ('claude', 'm2'))

    def test_generated_template_passes_audit(self):
        self.assertEqual(o.audit_env_file(self.env_file), [])


class RegistryTests(unittest.TestCase):
    def test_known_settings_are_read_by_code(self):
        setup = (ROOT / 'setup-ssdlc.sh').read_text(encoding='utf-8')
        for name in o.KNOWN_PROJECT_ENV_VARS:
            with self.subTest(name=name):
                self.assertTrue(code_reads(name) or '${%s' % name in setup, name)

    def test_settings_read_by_code_are_known(self):
        read = set()
        for src in SOURCES.values():
            read.update(re.findall(r"""(?:environ|env)\.get\(\s*['"](SSDLC_[A-Z0-9_]+)['"]""", src))
            read.update(re.findall(r"""environ\[\s*['"](SSDLC_[A-Z0-9_]+)['"]\s*\]""", src))
        internal = {n for n in read if n.startswith('SSDLC_SUPERVISED_')}
        self.assertEqual(read - internal - o.KNOWN_PROJECT_ENV_VARS - set(o.OBSOLETE_PROJECT_ENV_VARS), set())

    def test_obsolete_replacements_are_known(self):
        for name, replacement in o.OBSOLETE_PROJECT_ENV_VARS.items():
            self.assertNotIn(name, o.KNOWN_PROJECT_ENV_VARS)
            self.assertFalse(code_reads(name), name)
            if replacement:
                self.assertIn(replacement, o.KNOWN_PROJECT_ENV_VARS)


class AuditTests(unittest.TestCase):
    LEGACY = ('export SSDLC_PROJECT_ID="p"\n'
              'export SSDLC_DESIGN_ARCH_ENGINE="codex"\n'
              'export SSDLC_DEV_ESCALATION_MODEL=""\n'
              'export SSDLC_CONTAINER_PIDS_LIMIT="1024"\n'
              '# export SSDLC_TYPO="commented"\n'
              'export SSDLC_TYPO="x"\n'
              'export OPENROUTER_API_KEY="sk-or-secret"\n'
              'export SSDLC_CONTAINER_PIDS_LIMIT="512"\n')

    def test_audit_reports_obsolete_unknown_and_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / 'project.env'
            env_file.write_text(self.LEGACY)
            findings = o.audit_env_file(env_file)
            self.assertEqual(env_file.read_text(), self.LEGACY)
        self.assertEqual(findings, [
            'line 2: SSDLC_DESIGN_ARCH_ENGINE is obsolete and has no effect; rename it to SSDLC_ARCH_ENGINE',
            'line 3: SSDLC_DEV_ESCALATION_MODEL is obsolete and has no effect; remove it',
            'lines 4, 8: SSDLC_CONTAINER_PIDS_LIMIT is set 2 times and the last assignment (line 8) wins; '
            'keep a single assignment',
            'line 6: SSDLC_TYPO is not read by SSDLC; check the name or remove it',
        ])
        self.assertFalse(any('sk-or-secret' in f for f in findings))

    def test_duplicated_api_keys_are_reported_without_values(self):
        content = ('export OPENAI_API_KEY="sk-first-secret"\n'
                   'export ANTHROPIC_API_KEY=sk-ant-only\n'
                   "export OPENAI_API_KEY='sk-second-secret'\n")
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / 'project.env'
            env_file.write_text(content)
            for override in (True, False):
                with self.subTest(override=override):
                    findings = o.audit_env_file(env_file, override=override)
                    self.assertEqual(len(findings), 1)
                    self.assertTrue(findings[0].startswith('lines 1, 3: OPENAI_API_KEY is set 2 times'))
                    for secret in ('sk-first-secret', 'sk-second-secret', 'sk-ant-only'):
                        self.assertNotIn(secret, findings[0])

    def assert_effective_pids_limit(self, env_file, preset, expected):
        env = {'SSDLC_CONTAINER_PIDS_LIMIT': preset} if preset else {}
        with patch.dict(os.environ, env, clear=False):
            if not preset:
                os.environ.pop('SSDLC_CONTAINER_PIDS_LIMIT', None)
            o.load_env_file(env_file, override=False)
            self.assertEqual(os.environ['SSDLC_CONTAINER_PIDS_LIMIT'], expected)

    def test_global_env_duplicates_report_first_assignment_like_the_loaders(self):
        content = ('export SSDLC_CONTAINER_PIDS_LIMIT="1024"\n'
                   'export SSDLC_CONTAINER_PIDS_LIMIT="512"\n')
        with tempfile.TemporaryDirectory() as tmp:
            home, repo = Path(tmp) / 'home', Path(tmp) / 'repo'
            (home / '.ssdlc').mkdir(parents=True)
            (repo / '.ssdlc').mkdir(parents=True)
            global_env = home / '.ssdlc/global.env'
            global_env.write_text(content)
            findings = o.audit_env_file(global_env, override=False)
            self.assertEqual(findings, [
                'lines 1, 2: SSDLC_CONTAINER_PIDS_LIMIT is set 2 times and the first assignment (line 1) '
                'wins unless SSDLC_CONTAINER_PIDS_LIMIT is already set in the environment; '
                'keep a single assignment'])
            # The reported precedence matches both loaders, with and without a pre-existing value.
            self.assert_effective_pids_limit(global_env, None, '1024')
            self.assert_effective_pids_limit(global_env, '256', '256')
            self.assertEqual(s.build_project_env(repo, {'HOME': str(home)})['SSDLC_CONTAINER_PIDS_LIMIT'], '1024')
            self.assertEqual(s.build_project_env(repo, {'HOME': str(home), 'SSDLC_CONTAINER_PIDS_LIMIT': '256'})
                             ['SSDLC_CONTAINER_PIDS_LIMIT'], '256')
            # project.env keeps the last assignment.
            project_env = repo / '.ssdlc/project.env'
            project_env.write_text(content)
            self.assertIn('the last assignment (line 2) wins', o.audit_env_file(project_env)[0])
            self.assertEqual(s.build_project_env(repo, {'HOME': str(home)})['SSDLC_CONTAINER_PIDS_LIMIT'], '512')

    def test_audit_ignores_missing_file(self):
        self.assertEqual(o.audit_env_file(Path('/nonexistent/project.env')), [])

    def test_doctor_prints_findings_without_rewriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, home = Path(tmp) / 'repo', Path(tmp) / 'home'
            (repo / '.ssdlc').mkdir(parents=True)
            home.mkdir()
            env_file = repo / '.ssdlc/project.env'
            env_file.write_text(self.LEGACY)
            global_env = home / '.ssdlc/global.env'
            global_env.parent.mkdir()
            global_dup = ('export SSDLC_CONTAINER_PIDS_LIMIT="1024"\nexport OPENAI_API_KEY="sk-global-one"\n'
                          'export SSDLC_CONTAINER_PIDS_LIMIT="512"\nexport OPENAI_API_KEY="sk-global-two"\n')
            global_env.write_text(global_dup)
            out = io.StringIO()
            remote = type('Remote', (), dict(token='', remote_url='', provider='none', owner='', repo='',
                                              enabled=False, mode='false'))()
            with patch.dict(os.environ, {'HOME': str(home), 'SSDLC_INHERIT_HOST_CODEX_AUTH': 'false',
                                         'SSDLC_INHERIT_HOST_CLAUDE_AUTH': 'false'}), \
                    patch.object(o, 'get_cli_version', return_value='MISSING'), \
                    patch.object(o, 'RemoteSyncManager', return_value=remote), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(o.cmd_doctor(repo), 0)
            self.assertEqual(env_file.read_text(), self.LEGACY)
            self.assertEqual(global_env.read_text(), global_dup)
        text = out.getvalue()
        self.assertIn('rename it to SSDLC_ARCH_ENGINE', text)
        self.assertIn(f'{env_file}: lines 4, 8: SSDLC_CONTAINER_PIDS_LIMIT is set 2 times and the last '
                      'assignment (line 8) wins', text)
        self.assertIn(f'{global_env}: lines 1, 3: SSDLC_CONTAINER_PIDS_LIMIT is set 2 times and the first '
                      'assignment (line 1) wins unless', text)
        self.assertIn(f'{global_env}: lines 2, 4: OPENAI_API_KEY is set 2 times', text)
        for secret in ('sk-or-secret', 'sk-global-one', 'sk-global-two'):
            self.assertNotIn(secret, text)


class ReviewEngineAutoTests(unittest.TestCase):
    def test_auto_and_empty_default_engine_resolve_to_openrouter(self):
        for engine in ('auto', 'AUTO', '', ' auto '):
            with self.subTest(engine=engine):
                self.assertEqual(o.resolve_review_entry('deepseek/deepseek-r1', engine),
                                 ('openrouter', 'deepseek/deepseek-r1'))
        self.assertEqual(o.resolve_review_entry('m', 'codex'), ('codex', 'm'))
        self.assertEqual(o.resolve_review_entry('claude:sonnet', 'auto'), ('claude', 'sonnet'))

    def test_supervisor_review_panel_with_unprefixed_entries_and_auto(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {'SSDLC_REVIEW_MODELS': 'deepseek/deepseek-r1,codex:gpt-6.1-sol',
                                        'SSDLC_REVIEW_ENGINE': 'auto'}):
            panel = s.route_child(Path(tmp), 'run', 'M')['review_panel']
        self.assertEqual(panel, [dict(engine='openrouter', model='deepseek/deepseek-r1'),
                                     dict(engine='codex', model='gpt-6.1-sol')])


if __name__ == '__main__':
    unittest.main()
