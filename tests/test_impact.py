"""Deterministic advisory impact report and additive feedback-summary metrics."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ssdlc_impact
from ssdlc_feedback_analysis import impact_attribution
from ssdlc_impact import build_report, pending_touched, profile_delta, related_paths, summary_metrics
from ssdlc_tasks import TaskError, atomic_json, digest, file_hash


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


FILES = {
    'pkg/__init__.py': '',
    'pkg/core.py': 'value = 1\nfrom pkg.schema_ref import X  # schemas/core.schema.json\n',
    'pkg/schema_ref.py': 'X = 1\n',
    'pkg/user.py': '"""Uses core."""\nfrom pkg import core\n',
    'pkg/rel.py': '\n\nfrom . import core\n',
    'app.py': 'import os\nimport pkg.core as c\n',
    'loader.py': 'import importlib\n\ndef load(name):\n    return importlib.import_module(name)\n',
    'broken.py': 'def broken(:\n',
    'tests/test_core.py': 'import unittest\nfrom pkg.core import value\n',
    'tests/test_patch.py': 'from unittest.mock import patch\n\n\n@patch("pkg.core.value", 2)\ndef test(): pass\n',
    'tests/fixtures/data.json': '{\n  "source": "pkg/core.py"\n}\n',
    'tests/fixtures/other.json': '{"source": "lib/pkg/core.py", "copy": "pkg/core.py.bak"}\n',
    'schemas/core.schema.json': '{\n  "description": "validates pkg/core.py output"\n}\n',
    'schemas/unrelated.schema.json': '{}\n',
    'docs/core.md': 'See pkg/core.py.\n',
    'CODEOWNERS': '* @everyone\n/pkg/ @core-owners @alpha\ndocs/* @docs\n',
}


class ImpactFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        git(self.repo, 'config', 'user.name', 'test')
        for name, text in FILES.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        os.symlink('pkg/user.py', self.repo / 'link.py')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'fixture')
        self.base = git(self.repo, 'rev-parse', 'HEAD')
        self.plan = dict(base_commit=self.base, tasks=[
            dict(task_id='T1', target_files=['pkg/core.py', 'tests/test_core.py']),
            dict(task_id='T2', target_files=['docs/new.md'])])


class ImpactReportTests(ImpactFixture):
    def target(self, report, path):
        return next(row for row in report['targets'] if row['path'] == path)

    def test_consumers_tests_validators_and_owners_have_evidence(self):
        report = build_report(self.repo, self.plan)
        core = self.target(report, 'pkg/core.py')
        self.assertTrue(core['exists_at_base'])
        self.assertEqual(core['consumers_status'], 'STATIC_PYTHON')
        self.assertEqual(core['consumers'], [
            dict(path='app.py', evidence_path='app.py', line=2, via='IMPORT'),
            dict(path='pkg/rel.py', evidence_path='pkg/rel.py', line=3, via='IMPORT'),
            dict(path='pkg/user.py', evidence_path='pkg/user.py', line=2, via='IMPORT')])
        self.assertEqual(core['tests'], [
            dict(path='tests/fixtures/data.json', evidence_path='tests/fixtures/data.json', line=2,
                 via='PATH_REFERENCE'),
            dict(path='tests/test_core.py', evidence_path='tests/test_core.py', line=2, via='IMPORT'),
            dict(path='tests/test_patch.py', evidence_path='tests/test_patch.py', line=4, via='PATCH_TARGET')])
        # Explicit references only, in either direction.
        self.assertEqual(core['validators'], [
            dict(path='schemas/core.schema.json', evidence_path='pkg/core.py', line=2, via='PATH_REFERENCE'),
            dict(path='schemas/core.schema.json', evidence_path='schemas/core.schema.json', line=2,
                 via='PATH_REFERENCE')])
        self.assertEqual(core['owners'], dict(status='DECLARED', owners=['@alpha', '@core-owners'],
                                              evidence_path='CODEOWNERS', line=2))
        self.assertEqual(report['owners_source'], 'CODEOWNERS')
        new = self.target(report, 'docs/new.md')
        self.assertFalse(new['exists_at_base'])
        self.assertEqual(new['consumers_status'], 'UNKNOWN')
        self.assertEqual(new['owners']['owners'], ['@docs'])

    def test_dynamic_import_and_parse_error_are_unknown_without_a_destination(self):
        report = build_report(self.repo, self.plan)
        self.assertEqual(report['unknown'], [
            dict(path='broken.py', line=1, reason='PARSE_ERROR'),
            dict(path='loader.py', line=4, reason='DYNAMIC_IMPORT')])
        related = related_paths(report)
        self.assertNotIn('loader.py', related)
        # Symlinks, the working tree and untracked files are not the plan base.
        self.assertNotIn('link.py', related)
        (self.repo / 'untracked.py').write_text('import pkg.core\n')
        self.assertNotIn('untracked.py', related_paths(build_report(self.repo, self.plan)))

    def test_possible_omissions_list_uncovered_consumers_and_tests_only(self):
        report = build_report(self.repo, self.plan)
        self.assertEqual(report['possible_omissions'], [
            dict(path='app.py', relations=['CONSUMER'], targets=['pkg/core.py']),
            dict(path='pkg/rel.py', relations=['CONSUMER'], targets=['pkg/core.py']),
            dict(path='pkg/user.py', relations=['CONSUMER'], targets=['pkg/core.py']),
            dict(path='tests/fixtures/data.json', relations=['TEST'], targets=['pkg/core.py']),
            dict(path='tests/test_patch.py', relations=['TEST'], targets=['pkg/core.py'])])
        self.assertTrue(report['advisory'])
        self.assertFalse(report['blocking'])
        self.assertFalse(report['complete_dependency_proof'])
        self.plan['tasks'][1]['target_files'].append('app.py')
        covered = build_report(self.repo, self.plan)
        self.assertNotIn('app.py', [row['path'] for row in covered['possible_omissions']])

    def test_same_input_gives_identical_output_without_timestamps(self):
        first = build_report(self.repo, self.plan)
        second = build_report(self.repo, json.loads(json.dumps(self.plan)))
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(first['plan_sha256'], digest(self.plan))
        self.assertFalse({'created_at', 'generated_at', 'timestamp'} & set(first))
        # Later commits do not change a report bound to the plan base.
        (self.repo / 'late.py').write_text('import pkg.core\n')
        git(self.repo, 'add', 'late.py'); git(self.repo, 'commit', '-q', '-m', 'late')
        self.assertEqual(build_report(self.repo, self.plan), first)

    def test_absolute_imports_resolve_from_the_repository_root_only(self):
        files = {'core.py': 'root = 1\n',
                 'pkg/absolute.py': 'import core\n',
                 'pkg/sibling_only.py': '\nimport schema_ref\n',
                 'pkg/explicit.py': 'from . import core\n'}
        for name, text in files.items():
            (self.repo / name).write_text(text)
        git(self.repo, 'add', *files); git(self.repo, 'commit', '-q', '-m', 'same-named modules')
        self.plan['base_commit'] = git(self.repo, 'rev-parse', 'HEAD')
        self.plan['tasks'].append(dict(task_id='T3', target_files=['core.py', 'pkg/schema_ref.py']))
        report = build_report(self.repo, self.plan)
        core = self.target(report, 'pkg/core.py')
        # ``import core`` is absolute: pkg/core.py is not its static destination.
        self.assertNotIn('pkg/absolute.py', [row['path'] for row in core['consumers']])
        self.assertIn(dict(path='pkg/explicit.py', evidence_path='pkg/explicit.py', line=1, via='IMPORT'),
                      core['consumers'])
        self.assertEqual(self.target(report, 'core.py')['consumers'], [
            dict(path='pkg/absolute.py', evidence_path='pkg/absolute.py', line=1, via='IMPORT')])
        # Only the importer's own directory would resolve it: no destination.
        self.assertEqual(self.target(report, 'pkg/schema_ref.py')['consumers'], [
            dict(path='pkg/core.py', evidence_path='pkg/core.py', line=2, via='IMPORT')])
        omissions = {row['path'] for row in report['possible_omissions']}
        self.assertIn('pkg/explicit.py', omissions)
        self.assertNotIn('pkg/sibling_only.py', omissions)
        environment = [row for row in report['unknown'] if row['reason'] == 'ENVIRONMENT_DEPENDENT_IMPORT']
        self.assertEqual(environment, [
            dict(path='pkg/absolute.py', line=1, reason='ENVIRONMENT_DEPENDENT_IMPORT'),
            dict(path='pkg/sibling_only.py', line=2, reason='ENVIRONMENT_DEPENDENT_IMPORT')])

    def test_unknown_base_commit_is_refused(self):
        self.plan['base_commit'] = '0' * 40
        with self.assertRaises(TaskError):
            build_report(self.repo, self.plan)

    def test_unsupported_codeowners_pattern_is_unknown(self):
        (self.repo / 'CODEOWNERS').write_text('[abc].py @x\n')
        git(self.repo, 'commit', '-q', '-am', 'owners')
        self.plan['base_commit'] = git(self.repo, 'rev-parse', 'HEAD')
        report = build_report(self.repo, self.plan)
        self.assertEqual({row['owners']['status'] for row in report['targets']}, {'UNKNOWN'})

    def test_attribution_reads_new_and_legacy_reports(self):
        report = build_report(self.repo, self.plan)
        result = impact_attribution(report, digest(report), ['app.py', 'elsewhere.py'])
        self.assertEqual(result['status'], 'DETECTABLE')
        self.assertEqual(result['detectable_files'], ['app.py'])
        legacy = dict(schema_version=1, references=[dict(path='app.py', references=['core'])])
        self.assertEqual(related_paths(legacy), {'app.py'})
        with self.assertRaises(TaskError):
            related_paths(dict(kind=ssdlc_impact.KIND))


class SummaryMetricTests(ImpactFixture):
    def test_profile_delta_matches_operator_recovery_format(self):
        old = dict(development_attempts=3, metered_usd=1.0, wall_seconds=10)
        new = dict(development_attempts=4, metered_usd=1.0, wall_seconds=10)
        self.assertEqual(profile_delta(old, new), {'development_attempts': dict(old=3, new=4)})
        self.assertEqual(profile_delta(old, old), {})

    def test_pending_tasks_touched_use_pre_extension_targets(self):
        plan = dict(tasks=[dict(task_id='T1', target_files=['a.py']),
                           dict(task_id='T2', target_files=['b.py', 'shared.py']),
                           dict(task_id='T3', target_files=['shared.py']),
                           dict(task_id='T4', target_files=['shared.py'])])
        journal = dict(tasks=dict(T1=dict(status='COMPLETED'), T2=dict(status='FAILED'),
                                  T3=dict(status='PENDING'), T4=dict(status='COMPLETED')))
        scope = {'T2': dict(added=['shared.py'], removed=[])}
        self.assertEqual(pending_touched(journal, plan, scope), ['T3'])
        journal['tasks']['T2']['status'] = 'PENDING'
        # T2's own added path did not belong to it before the extension.
        self.assertEqual(pending_touched(journal, plan, scope), ['T3'])

    def register(self, root, plan, reports):
        """Mirror what planning, continuations and generations register in ``state.json``."""
        root.mkdir(parents=True, exist_ok=True)
        artifacts = {}
        for ref, value in [('02-design/plan.json', plan), *reports]:
            (root / ref).parent.mkdir(parents=True, exist_ok=True)
            atomic_json(root / ref, value)
            artifacts[ref] = dict(path=ref, sha256=file_hash(root / ref))
        atomic_json(root / 'state.json', dict(artifacts=artifacts))
        return root

    def ticket(self, report=True):
        ticket = self.repo / '.ssdlc' / 'TKT'
        reports = [('02-design/task-planning/impact-report-' + digest(self.plan) + '.json',
                    build_report(self.repo, self.plan))] if report else []
        return self.register(ticket, self.plan, reports)

    def extended(self, ticket, identity, task, path):
        """An execution generation that copied the ticket's files and extended its plan."""
        plan = json.loads(json.dumps(self.plan))
        next(t for t in plan['tasks'] if t['task_id'] == task)['target_files'].append(path)
        copied = [(ref, json.loads((ticket / ref).read_text()))
                  for ref in json.loads((ticket / 'state.json').read_text())['artifacts'] if 'impact-report' in ref]
        self.register(ticket / 'execution-runtimes' / identity, plan, copied)
        chain = [dict(transition_id=identity, proposal={}, recipe=dict(subject=dict(
            old_plan_sha256=digest(self.plan), new_plan_sha256=digest(plan))))]
        return plan, chain

    def test_avoidable_omissions_count_distinct_flagged_paths_once(self):
        ticket = self.ticket()
        _, chain = self.extended(ticket, 'g1', 'T2', 'app.py')
        extensions = [dict(transition_id='g1', added_paths=['app.py', 'unflagged.py']),
                      dict(transition_id='g1', added_paths=['app.py'])]
        [result] = ssdlc_impact._avoidable(ticket, chain, extensions)
        self.assertEqual(result['status'], 'MEASURED')
        self.assertEqual(result['count'], 1)
        # The extended plan's omissions belong to the plan it was extended from.
        self.assertEqual(result['plan_sha256'], digest(self.plan))
        self.assertEqual(result['paths'], [dict(path='app.py', observed_in=['MANUAL_EXTENSION'])])

    def test_avoidable_omissions_aggregate_feedback_and_extensions_per_originating_plan(self):
        ticket = self.ticket()
        extended, chain = self.extended(ticket, 'g1', 'T2', 'app.py')
        (ticket / 'feedback').mkdir()
        (ticket / 'feedback/authority.json').write_text('{}')
        def request(plan, files, cause='MISSING_TARGET'):
            return dict(request=dict(cause=cause, requested_files=files), binding=dict(plan_sha256=plan))
        requests = {'r1': request(digest(extended), ['app.py', 'pkg/user.py']),
                    'r2': request(digest(self.plan), ['tests/test_patch.py'], cause='IMPROVEMENT'),
                    'r3': request('f' * 64, ['pkg/rel.py'])}
        with patch('ssdlc_feedback.FeedbackStore') as store:
            store.return_value.inspect.return_value = dict(requests=requests)
            result = ssdlc_impact._avoidable(ticket, chain, [dict(transition_id='g1', added_paths=['app.py'])])
        measured, unknown = result
        self.assertEqual(measured['plan_sha256'], digest(self.plan))
        self.assertEqual(measured['count'], 2)
        self.assertEqual(measured['paths'], [
            dict(path='app.py', observed_in=['FEEDBACK_MISSING_TARGET', 'MANUAL_EXTENSION']),
            dict(path='pkg/user.py', observed_in=['FEEDBACK_MISSING_TARGET'])])
        # A plan without a report is UNKNOWN, never 0, and is not merged into another plan.
        self.assertEqual(unknown, dict(plan_sha256='f' * 64, report_sha256=None, status='UNKNOWN',
                                       count=None, paths=[], reason='NO_IMPACT_REPORT'))

    def test_feedback_attribution_reads_the_planning_report_of_the_plan_lineage(self):
        from ssdlc_feedback_analysis import historical_impact
        from ssdlc_reconciliation import _files
        ticket = self.ticket()
        def source(root, plan):
            return dict(root=root, plan=plan, state=json.loads((root / 'state.json').read_text()),
                        files=_files(root))
        result = historical_impact(source(ticket, self.plan), ['app.py', 'elsewhere.py'])
        self.assertEqual((result['status'], result['detectable_files']), ('DETECTABLE', ['app.py']))
        # An extended runtime still reaches the report of the plan it extended.
        extended, chain = self.extended(ticket, 'g1', 'T2', 'app.py')
        runtime = ticket / 'execution-runtimes/g1'
        with patch('ssdlc_impact._transitions', return_value=chain):
            self.assertEqual(historical_impact(source(runtime, extended), ['app.py'])['status'], 'DETECTABLE')
        # Without that lineage the copied report is bound to another plan.
        self.assertEqual(historical_impact(source(runtime, extended), ['app.py'])['status'], 'UNKNOWN')
        # An unregistered report or one bound to another plan is not evidence.
        other = json.loads(json.dumps(self.plan)); other['tasks'][1]['target_files'] = ['docs/other.md']
        self.assertEqual(historical_impact(source(ticket, other), ['app.py'])['status'], 'UNKNOWN')
        state = json.loads((ticket / 'state.json').read_text())
        state['artifacts'] = {k: v for k, v in state['artifacts'].items() if 'impact-report' not in k}
        atomic_json(ticket / 'state.json', state)
        self.assertEqual(historical_impact(source(ticket, self.plan), ['app.py'])['status'], 'UNKNOWN')

    def test_missing_report_is_unknown_not_zero(self):
        ticket = self.ticket(report=False)
        metrics = summary_metrics(ticket)
        self.assertEqual(metrics['avoidable_omissions'][0]['status'], 'UNKNOWN')
        self.assertIsNone(metrics['avoidable_omissions'][0]['count'])
        self.assertEqual(metrics['avoidable_omissions'][0]['reason'], 'NO_IMPACT_REPORT')
        self.assertEqual(metrics['profile_delta']['status'], 'UNKNOWN')
        self.assertEqual(metrics['manual_extension_pending_tasks'], dict(status='MEASURED', transitions=[]))

    def test_changed_report_evidence_is_unknown(self):
        ticket = self.ticket()
        path = ticket / '02-design/task-planning' / ('impact-report-' + digest(self.plan) + '.json')
        path.write_text(path.read_text().replace('app.py', 'xyz.py'))
        result = summary_metrics(ticket)['avoidable_omissions'][0]
        self.assertEqual((result['status'], result['count']), ('UNKNOWN', None))


class ManualExtensionMetricTests(unittest.TestCase):
    """A real activated manual scope amendment, without model calls."""
    def setUp(self):
        import test_scope_amendments as fixtures
        self.case = fixtures.ScopeAmendmentTests()
        self.case.initialize()
        self.addCleanup(self.case.doCleanups)

    def test_manual_extension_pending_tasks_and_profile_delta(self):
        from ssdlc_scope_amendments import activate
        case = self.case
        before = summary_metrics(case.ticket)
        staged = case.stage()
        selected = activate(case.ticket, staged['preparation'], case.auth(staged))
        metrics = summary_metrics(case.ticket)
        transitions = metrics['manual_extension_pending_tasks']['transitions']
        self.assertEqual(metrics['manual_extension_pending_tasks']['status'], 'MEASURED')
        self.assertEqual(len(transitions), 1)
        row = transitions[0]
        self.assertEqual(row['transition_id'], selected['transition_id'])
        self.assertEqual(row['authorization_contract'], 'scope-amendment-v1')
        self.assertEqual(row['added_paths'], ['app.py'])
        self.assertEqual(row['status'], 'MEASURED')
        recipe = json.loads((case.ticket / 'execution' / row['transition_id'] / 'recipe.json').read_text())
        plan = json.loads((case.ticket / 'execution-runtimes' / row['transition_id'] / '02-design/plan.json').read_text())
        self.assertEqual(row['pending_task_ids'], pending_touched(recipe['journal_state'], plan,
                                                                  staged['proposal']['scope_diff']))
        # A scope-only extension keeps the authorized limits profile unchanged.
        self.assertEqual(metrics['profile_delta']['status'], 'MEASURED')
        self.assertEqual(metrics['profile_delta']['changes'], {})
        self.assertEqual(metrics['profile_delta']['initial_profile_sha256'],
                         before['profile_delta']['initial_profile_sha256'])
        self.assertEqual(summary_metrics(case.ticket), metrics)
        # The extended plan came from the continuation plan, whose runtime registered
        # ``02-design/impact-report.json``; the historical ticket plan has none.
        report = json.loads((ssdlc_impact._continuation_root(case.ticket) / '02-design/impact-report.json').read_text())
        self.assertEqual(report['plan_sha256'], digest(case.source['plan']))
        avoidable = {row['plan_sha256']: row for row in metrics['avoidable_omissions']}
        self.assertEqual(avoidable[report['plan_sha256']], dict(
            plan_sha256=report['plan_sha256'], report_sha256=digest(report), status='MEASURED', count=0, paths=[]))
        historical = [row for sha, row in avoidable.items() if sha != report['plan_sha256']]
        self.assertEqual([(row['status'], row['reason']) for row in historical], [('UNKNOWN', 'NO_IMPACT_REPORT')])
        # Feedback attribution in the generation reads the same report.
        from ssdlc_feedback_analysis import historical_impact
        attribution = historical_impact(case.source, ['app.py'])
        self.assertEqual((attribution['status'], attribution['report_sha256']), ('NOT_IN_REPORT', digest(report)))


class ContinuationMetricTests(unittest.TestCase):
    """A real activated continuation registers its report in the selected runtime."""
    def setUp(self):
        import test_continuations as fixtures
        self.case = fixtures.ContinuationTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_activated_continuation_report_is_measured(self):
        from ssdlc_transitions import runtime_root
        case = self.case
        case.activate(case.prepare())
        report = json.loads((runtime_root(case.target) / '02-design/impact-report.json').read_text())
        self.assertEqual(report['plan_sha256'], digest(case.new_plan))
        metrics = summary_metrics(case.target)
        self.assertEqual(metrics['avoidable_omissions'], [dict(
            plan_sha256=digest(case.new_plan), report_sha256=digest(report), status='MEASURED', count=0, paths=[])])
        self.assertEqual(summary_metrics(case.target), metrics)


if __name__ == '__main__':
    unittest.main()
