"""Declared contract changes, demonstrated omissions as PLAN_DEFECT and prerequisites (A6a-2, issue #93)."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import SSDLCOrchestrator
from replay.repository_fixtures import RepositoryTests
from ssdlc_impact import build_report, contract_defects
from ssdlc_planning import PlanRepair, validate_plan_review
from ssdlc_tasks import PlanError, digest, git, validate_document
import test_task_bootstrap as bootstrap


FILES = {
    'pkg/__init__.py': '',
    'pkg/api.py': 'def load(path):\n    return path\n\n\ndef helper():\n    return 1\n',
    'pkg/caller.py': 'from pkg.api import load\n\nload("x")\n',
    'pkg/attr_user.py': 'import pkg.api as api\n\n\nvalue = api.load("y")\n',
    # Imports the producer but never uses the changed contract: not a file to modify.
    'pkg/other_user.py': 'from pkg import api\n\napi.helper()\n',
    'pkg/dynamic.py': 'from pkg import api\n\n\ndef pick(name):\n    return getattr(api, name)\n',
    'pkg/star.py': 'from pkg.api import *\n',
    'tests/test_api.py': 'from unittest.mock import patch\n\n\n@patch("pkg.api.load")\ndef test(mocked): pass\n',
    'schemas/api.schema.json': '{}\n',
}
CONSUMERS = ['pkg/attr_user.py', 'pkg/caller.py', 'tests/test_api.py']


def change(contract='load', producer='pkg/api.py', expected='REMOVED'):
    return dict(contract=contract, producer=producer, expected_change=expected,
                description='load is replaced by read with a different signature')


def finding(identifier='C1', **kwargs):
    return {'finding_id': identifier, 'status': 'OPEN', 'severity': 'BLOCKING', 'category': 'PLAN_DEFECT',
            'requirement_refs': ['AC-1'], 'task_ids': ['T1'],
            'message': 'Removing pkg.api.load breaks consumers without an owner task',
            'evidence': 'Consumers ' + ', '.join(CONSUMERS) + ' use load and no task covers them',
            'requested_change': 'Add the consumers to a task ordered with T1 through depends_on',
            'new_issue_reason': 'INITIAL_REVIEW', **kwargs}


def review(findings):
    blockers = [f for f in findings if f['status'] == 'OPEN' and f['severity'] == 'BLOCKING']
    return json.dumps({'verdict': 'REVISION_REQUIRED' if blockers else 'APPROVED', 'findings': findings})


class ContractFixture(RepositoryTests):
    def setUp(self):
        super().setUp()
        for name, text in FILES.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        git(self.repo, 'add', *FILES)
        git(self.repo, 'commit', '-m', 'contract fixture')
        self.base = git(self.repo, 'rev-parse', 'HEAD')
        self.quality['base_commit'] = self.plan['base_commit'] = self.base
        self.plan['tasks'] = [self.task('T1', ['AC-1'], ['SEC-1'], ['pkg/api.py'])]
        self.plan['tasks'][0]['contract_changes'] = [change()]

    def contracts(self, plan=None):
        return build_report(self.repo, plan or self.plan)['contracts']

    def validate_review(self, raw, plan=None):
        plan = plan or self.plan
        return validate_plan_review(raw, plan, ['AC-1', 'SEC-1'], [], self.quality,
                                    contract_defects=contract_defects(build_report(self.repo, plan)))


class SchemaTests(ContractFixture):
    def test_plans_without_declarations_validate_as_before(self):
        plan = copy.deepcopy(self.plan)
        del plan['tasks'][0]['contract_changes']
        self.validator().validate(plan)
        report = build_report(self.repo, plan)
        # The report of an undeclared plan keeps its earlier shape and bytes.
        self.assertNotIn('contracts', report)
        self.assertEqual(contract_defects(report), [])
        self.assertFalse(report['blocking'])

    def test_declarations_are_structured_and_scoped_to_the_producer_task(self):
        self.validator().validate(self.plan)
        for modify in (lambda c: c.update(expected_change='MAYBE'), lambda c: c.update(owner='T9'),
                       lambda c: c.pop('producer')):
            plan = copy.deepcopy(self.plan)
            modify(plan['tasks'][0]['contract_changes'][0])
            with self.subTest(change=plan['tasks'][0]['contract_changes'][0]), self.assertRaises(PlanError):
                validate_document('plan', plan)
        self.plan['tasks'][0]['contract_changes'] = [change(producer='pkg/caller.py')]
        with self.assertRaises(PlanError) as raised:
            self.validator().validate(self.plan)
        self.assertIn('CONTRACT_PRODUCER_OUTSIDE_SCOPE', [d['code'] for d in raised.exception.diagnostics])
        # contract_refs is unchanged: it still names design artifacts only.
        self.assertEqual(self.plan['tasks'][0]['contract_refs'], [])


class DemonstratedDefectTests(ContractFixture):
    def test_uncovered_consumer_of_a_removed_contract_is_a_plan_defect_bound_to_obligations(self):
        contracts = self.contracts()
        [declaration] = contracts['declarations']
        self.assertEqual(declaration['status'], 'ANALYZED')
        self.assertEqual([(c['path'], c['line'], c['via'], c['coverage']) for c in declaration['consumers']], [
            ('pkg/attr_user.py', 4, 'ATTRIBUTE', 'UNCOVERED'), ('pkg/caller.py', 1, 'IMPORT', 'UNCOVERED'),
            ('tests/test_api.py', 4, 'PATCH_TARGET', 'UNCOVERED')])
        # An importer that does not use the contract is not a consumer to modify.
        self.assertNotIn('pkg/other_user.py', [c['path'] for c in declaration['consumers']])
        self.assertEqual([d['consumer'] for d in contracts['defects']], CONSUMERS)
        for defect in contracts['defects']:
            self.assertEqual((defect['category'], defect['gap'], defect['task_id']), ('PLAN_DEFECT', 'OWNER_OR_SCOPE', 'T1'))
            self.assertEqual(defect['requirement_refs'], ['AC-1', 'SEC-1'])
            self.assertEqual(defect['quality_check_refs'], ['CHECK-GLOBAL'])

    def test_the_review_must_report_the_defect_as_a_blocking_plan_defect(self):
        with self.assertRaisesRegex(PlanError, 'omitted demonstrated contract') as raised:
            self.validate_review(review([]))
        self.assertEqual({d['code'] for d in raised.exception.diagnostics}, {'CONTRACT_DEFECT_NOT_REPORTED'})
        self.assertEqual(sorted(d['consumer'] for d in raised.exception.diagnostics), CONSUMERS)
        # Advisory, unrelated or unbound findings do not report it.
        for wrong in (finding(severity='ADVISORY'), finding(task_ids=[]),
                      finding(evidence='Something else entirely', message='Unrelated'),
                      finding(category='ENVIRONMENT_PREREQUISITE')):
            with self.subTest(finding=wrong), self.assertRaises(PlanError):
                self.validate_review(review([wrong]))
        accepted = self.validate_review(review([finding()]))
        self.assertEqual(accepted['verdict'], 'REVISION_REQUIRED')
        # A quality obligation of the defect is an approved link too.
        bound = finding(requirement_refs=[], quality_check_refs=['CHECK-GLOBAL'])
        self.assertEqual(self.validate_review(review([bound]))['verdict'], 'REVISION_REQUIRED')

    def test_planning_cannot_approve_while_the_defect_remains(self):
        root = Path(tempfile.mkdtemp(dir=self.tmp.name))
        responses = iter([review([])] * 2)
        repair = PlanRepair(root, {'context': {'requirement_catalogue': {'required_ids': ['AC-1', 'SEC-1']},
                                               'quality_contract': self.quality}},
                            lambda plan: None, lambda *args: json.dumps(self.plan), lambda *args: next(responses),
                            lambda: None, structured_reviews=True,
                            contract_defects=lambda plan: contract_defects(build_report(self.repo, plan)))
        with self.assertRaisesRegex(PlanError, 'Invalid review protocol'):
            repair.run()
        session = json.loads((root / 'task-planning/session.json').read_text())
        self.assertEqual(session['attempts'][-1]['failure_kind'], 'REVIEW_PROTOCOL')
        errors = (root / 'task-planning' / session['attempts'][-1]['diagnostics']).read_text()
        self.assertIn('CONTRACT_DEFECT_NOT_REPORTED', errors)

    def test_compatible_change_and_unbound_task_do_not_block(self):
        self.plan['tasks'][0]['contract_changes'] = [change(expected='COMPATIBLE')]
        contracts = self.contracts()
        self.assertEqual(contracts['defects'], [])
        self.assertEqual(len(contracts['declarations'][0]['consumers']), 3)
        self.assertEqual(self.validate_review(review([]))['verdict'], 'APPROVED')
        # Without an approved obligation the omission is a diagnosis, never a blocker.
        plan = dict(base_commit=self.base, tasks=[dict(task_id='T1', target_files=['pkg/api.py'],
                                                       contract_changes=[change()])])
        contracts = self.contracts(plan)
        self.assertEqual(contracts['defects'], [])
        self.assertEqual(sorted(d['path'] for d in contracts['diagnoses'] if d['reason'] == 'NO_APPROVED_OBLIGATION'),
                         CONSUMERS)


class UnknownDependencyTests(ContractFixture):
    def test_unknown_dependencies_and_generic_references_are_diagnoses_without_block(self):
        task = self.plan['tasks'][0]
        task['target_files'] = ['pkg/api.py', *CONSUMERS, 'schemas/api.schema.json', 'pkg/new.py']
        task['contract_changes'] = [change(), change(contract='schema root', producer='schemas/api.schema.json'),
                                    change(contract='Api.method'), change(contract='missing'),
                                    change(contract='read', producer='pkg/new.py')]
        self.validator().validate(self.plan)
        contracts = self.contracts()
        self.assertEqual(contracts['defects'], [])
        status = {(d['contract'], d['producer']): (d['status'], d.get('reason')) for d in contracts['declarations']}
        self.assertEqual(status, {
            ('load', 'pkg/api.py'): ('ANALYZED', None),
            ('schema root', 'schemas/api.schema.json'): ('UNKNOWN', 'GENERIC_CONTRACT'),
            ('Api.method', 'pkg/api.py'): ('UNKNOWN', 'ATTRIBUTE_CONTRACT'),
            ('missing', 'pkg/api.py'): ('UNKNOWN', 'CONTRACT_NOT_AT_BASE'),
            ('read', 'pkg/new.py'): ('UNKNOWN', 'PRODUCER_NOT_AT_BASE')})
        load = [(d['reason'], d['path'], d['line']) for d in contracts['diagnoses'] if d['contract'] == 'load']
        self.assertEqual(load, [('DYNAMIC_ATTRIBUTE', 'pkg/dynamic.py', 5), ('STAR_IMPORT', 'pkg/star.py', 1)])
        self.assertEqual(self.validate_review(review([]))['verdict'], 'APPROVED')
        # A non-Python producer is UNKNOWN, not evidence of a dependency.
        task['contract_changes'] = [change(contract='root', producer='schemas/api.schema.json')]
        [declaration] = self.contracts()['declarations']
        self.assertEqual((declaration['status'], declaration['reason']), ('UNKNOWN', 'NON_PYTHON_PRODUCER'))


class PrerequisiteTests(ContractFixture):
    def test_prerequisite_task_is_validated_ordered_and_closes_the_defect(self):
        prerequisite = self.task('T0', [], [], list(CONSUMERS))
        self.plan['tasks'][0]['depends_on'] = ['T0']
        self.plan['tasks'].append(prerequisite)
        ordered = self.validator().validate(self.plan)
        self.assertEqual([t['task_id'] for t in ordered], ['T0', 'T1'])
        contracts = self.contracts()
        self.assertEqual(contracts['defects'], [])
        self.assertEqual({(c['coverage'], tuple(c['covering_task_ids'])) for c in contracts['declarations'][0]['consumers']},
                         {('COVERED', ('T0',))})
        self.assertEqual(self.validate_review(review([]))['verdict'], 'APPROVED')
        # A consumer task unordered with the producer task lacks ordering.
        self.plan['tasks'][0]['depends_on'] = []
        self.validator().validate(self.plan)
        self.assertEqual({(d['gap'], tuple(d['covering_task_ids'])) for d in self.contracts()['defects']},
                         {('ORDERING', ('T0',))})
        # A dependent successor also orders the change; an unknown prerequisite is refused.
        self.plan['tasks'][1]['depends_on'] = ['T1']
        self.assertEqual([t['task_id'] for t in self.validator().validate(self.plan)], ['T1', 'T0'])
        self.assertEqual(self.contracts()['defects'], [])
        self.plan['tasks'][1]['depends_on'] = ['T-missing']
        with self.assertRaisesRegex(PlanError, 'Unknown or self dependency'):
            self.validator().validate(self.plan)


class OrchestratorContractTests(RepositoryTests):
    make_orchestrator = bootstrap.OrchestratorBootstrapTests.make_orchestrator

    def setUp(self):
        super().setUp()
        environment = patch.object(SSDLCOrchestrator, '_verification_environment', return_value={
            'mode': 'role_container', 'platform': 'fixture',
            'image_id': 'sha256:' + '1' * 64, 'reuse_eligible': True})
        environment.start()
        self.addCleanup(environment.stop)

    def test_planning_requires_a_revision_for_a_demonstrated_contract_defect(self):
        (self.repo / 'tests').mkdir()
        (self.repo / 'tests/test_app.py').write_text('import unittest\n')
        (self.repo / 'consumer.py').write_text('from app import value\n')
        git(self.repo, 'add', 'consumer.py', 'tests/test_app.py')
        git(self.repo, 'commit', '-m', 'consumer')
        with patch.dict(os.environ, {'SSDLC_ENABLE_TASK_BREAKDOWN': 'true'}):
            orch = self.make_orchestrator()
            self.plan['base_commit'] = orch.state['base_commit']
            requirements = orch.dir_po / 'requirements.md'
            requirements.write_text('AC-1 SEC-1')
            orch.record_artifact('01-po/requirements.md', requirements)
            planned = copy.deepcopy(self.plan)
            planned['tasks'][0]['verification']['quality_check_refs'] = ['CHECK-TESTS']
            planned['tasks'][0]['contract_changes'] = [change('value', 'app.py')]
            reviews = []
            def invoke(engine, model, system, user, *args, **kwargs):
                payload = json.loads(user)
                if args[-1] == '02-design-task-plan':
                    self.assertIn('contract_changes', system)
                    return json.dumps(planned), 0, 0, 0, 'trace'
                self.assertIn('contracts.defects', system)
                reviews.append(payload['impact_report'])
                return json.dumps({'verdict': 'APPROVED', 'findings': []}), 0, 0, 0, 'trace'
            def dispatch(role, *args, **kwargs):
                return ('openrouter', 'reviewer' if role.endswith('sec') else 'planner', 'low', 'test')
            with patch('orchestrator.EngineRunner.run_sandboxed_cmd', return_value=('Ran 1 test\nOK', '', 0)), \
                    patch('orchestrator.SquadDispatcher.dispatch_role', side_effect=dispatch), \
                    patch('orchestrator.EngineRunner.invoke_role', side_effect=invoke):
                with self.assertRaisesRegex(PlanError, 'Invalid review protocol'):
                    orch._ensure_task_plan()
            self.assertEqual(len(reviews), 2)
            [defect] = reviews[0]['contracts']['defects']
            self.assertEqual((defect['consumer'], defect['gap'], defect['task_id']), ('consumer.py', 'OWNER_OR_SCOPE', 'T1'))
            ref = '02-design/task-planning/impact-report-' + digest(planned) + '.json'
            self.assertIn(ref, orch.state['artifacts'])
            self.assertFalse((orch.dir_design / 'plan.json').exists())
            self.assertFalse(any(g['gate'] == 'TASK_PLAN_GATE' and g['passed'] for g in orch.state['gate_history']))


if __name__ == '__main__':
    unittest.main()
