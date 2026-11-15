"""Transitional expectations in preparatory tests (A6a-3, issue #93).

A preparatory owner task may write a test whose expectation is transitional. The
plan records the test and expectation, the owner, the successor that updates it,
their dependency and the successor's final check. The T12-shaped regression at
the end of this module is synthetic: it is not a replay of a recorded run.
"""
import copy
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest

import ssdlc_bounce as bounce
from ssdlc_owner_revalidation import coverage_ok, transitional_contract, validate_review
from ssdlc_tasks import PlanError, TaskError, atomic_json, digest, file_hash, git
from replay.repository_fixtures import RepositoryTests
from test_gate_reports import GateFixture

TEST_PATH = 'tests/test_value.py'
EXPECTATION = 'self.assertEqual(app.value, 1)'
PREPARATORY = f'''import unittest

import app


class ValueTests(unittest.TestCase):
    def test_value(self):
        {EXPECTATION}
'''
REPLACED = PREPARATORY.replace(EXPECTATION, 'self.assertEqual(app.value, 2)')


def transitional_plan(fixture):
    """T1 owns the preparatory test, T2 changes the producer, T3 updates the test."""
    owner = fixture.task('T1', [], [], [TEST_PATH])
    producer = fixture.task('T2', ['AC-1'], ['SEC-1'], ['app.py'], ['T1'])
    successor = fixture.task('T3', ['AC-1'], [], [TEST_PATH], ['T2'])
    plan = copy.deepcopy(fixture.plan)
    plan['tasks'] = [owner, producer, successor]
    plan['transitional_expectations'] = [dict(
        expectation_id='TE-1', test_path=TEST_PATH, expectation=EXPECTATION,
        owner_task_id='T1', successor_task_id='T3', final_check_id='CHECK-T3')]
    return plan


class TransitionalPlanTests(RepositoryTests):
    def setUp(self):
        super().setUp()
        self.plan = transitional_plan(self)
        self.record = self.plan['transitional_expectations'][0]

    def diagnostics(self, plan=None):
        with self.assertRaises(PlanError) as caught:
            self.validator().validate(plan or self.plan)
        return caught.exception.diagnostics

    def codes(self, plan=None):
        return [d['code'] for d in self.diagnostics(plan)]

    def test_owner_successor_dependency_and_final_check_are_accepted(self):
        ordered = self.validator().validate(self.plan)
        self.assertEqual([t['task_id'] for t in ordered], ['T1', 'T2', 'T3'])
        # A direct dependency is accepted as well as the transitive one above.
        self.plan['tasks'][2]['depends_on'] = ['T1', 'T2']
        self.validator().validate(self.plan)

    def test_missing_owner_or_successor_is_rejected(self):
        for field in ('owner_task_id', 'successor_task_id', 'final_check_id', 'expectation', 'test_path'):
            with self.subTest(field=field):
                plan = copy.deepcopy(self.plan)
                del plan['transitional_expectations'][0][field]
                issues = self.diagnostics(plan)
                self.assertEqual(issues[0]['code'], 'INVALID_SCHEMA')
                self.assertIn(field, issues[0]['message'])

    def test_unknown_or_identical_owner_and_successor_are_rejected(self):
        cases = ((dict(owner_task_id='T9'), 'TRANSITIONAL_OWNER_REQUIRED'),
                 (dict(successor_task_id='T9'), 'TRANSITIONAL_SUCCESSOR_REQUIRED'),
                 (dict(successor_task_id='T1'), 'TRANSITIONAL_SUCCESSOR_REQUIRED'))
        for change, code in cases:
            with self.subTest(change=change):
                self.record.update(change)
                self.assertEqual(self.codes(), [code])
                self.plan = transitional_plan(self)
                self.record = self.plan['transitional_expectations'][0]

    def test_successor_must_depend_on_owner(self):
        self.plan['tasks'][2]['depends_on'] = []
        self.assertEqual(self.codes(), ['TRANSITIONAL_DEPENDENCY_REQUIRED'])
        # Reverse order: the owner runs after its successor.
        self.plan['tasks'][0]['depends_on'] = ['T3']
        self.assertEqual(self.codes(), ['TRANSITIONAL_DEPENDENCY_REQUIRED'])

    def test_test_must_be_in_owner_and_successor_scope(self):
        self.plan['tasks'][2]['target_files'] = ['tests/other.py']
        issues = self.diagnostics()
        self.assertEqual([(d['code'], d['role']) for d in issues],
                         [('TRANSITIONAL_TEST_OUTSIDE_SCOPE', 'successor')])
        self.plan = transitional_plan(self)
        self.plan['tasks'][0]['target_files'] = ['tests/other.py']
        self.assertEqual([(d['code'], d['role']) for d in self.diagnostics()],
                         [('TRANSITIONAL_TEST_OUTSIDE_SCOPE', 'owner')])

    def test_final_check_must_belong_to_successor(self):
        self.record['final_check_id'] = 'CHECK-T2'
        issues = self.diagnostics()
        self.assertEqual([d['code'] for d in issues], ['TRANSITIONAL_FINAL_CHECK_REQUIRED'])
        self.assertEqual(issues[0]['task_id'], 'T3')

    def test_duplicate_and_blank_expectations_are_rejected(self):
        duplicate = dict(self.record, expectation='self.assertTrue(app.value)')
        self.plan['transitional_expectations'].append(duplicate)
        self.assertEqual(self.codes(), ['DUPLICATE_TRANSITIONAL_EXPECTATION'])
        self.plan['transitional_expectations'] = [dict(self.record, expectation='   ')]
        self.assertEqual(self.codes(), ['INVALID_TRANSITIONAL_EXPECTATION'])

    def test_subgoal_and_review_only_owner_or_successor_stay_excluded(self):
        for index in (0, 2):
            with self.subTest(index=index, kind='subgoal'):
                plan = transitional_plan(self)
                plan['tasks'][index].update(kind='subgoal', subgoal_reason='later')
                self.assertIn('UNSUPPORTED_EXECUTION_MODE', self.codes(plan))
            with self.subTest(index=index, mode='review_only'):
                plan = transitional_plan(self)
                plan['tasks'][index]['verification'].update(
                    mode='review_only', reason='manual', review_criteria=['inspect'])
                self.assertIn('UNSUPPORTED_EXECUTION_MODE', self.codes(plan))

    def test_records_do_not_change_ac_and_sec_obligations(self):
        # The producer loses its SEC check and the successor its AC check. The
        # transitional record does not cover either obligation.
        self.plan['tasks'][1]['verification']['checks'][0]['sec_refs'] = []
        self.plan['tasks'][2]['verification']['checks'][0]['ac_refs'] = []
        with_record = self.diagnostics()
        without = copy.deepcopy(self.plan)
        del without['transitional_expectations']
        self.assertEqual(with_record, self.diagnostics(without))
        self.assertEqual([(d['code'], d['task_id'], d['missing_refs']) for d in with_record],
                         [('MISSING_VERIFICATION_REF', 'T2', ['SEC-1']),
                          ('MISSING_VERIFICATION_REF', 'T3', ['AC-1'])])
        # The preparatory owner still cannot cover unowned requirements.
        plan = transitional_plan(self)
        plan['tasks'][1]['ac_refs'] = plan['tasks'][1]['sec_refs'] = []
        plan['tasks'][1]['verification']['checks'][0].update(ac_refs=[], sec_refs=[])
        self.assertEqual(self.diagnostics(plan)[-1]['missing_refs'], ['SEC-1'])


class ReplacementReviewTests(RepositoryTests):
    """The successor's replacement reuses the hash-bound owner coverage review."""

    def setUp(self):
        super().setUp()
        self.plan = transitional_plan(self)
        (self.repo / 'tests').mkdir()
        self.test = self.repo / TEST_PATH
        self.test.write_text(PREPARATORY)
        git(self.repo, 'add', TEST_PATH)
        git(self.repo, 'commit', '-m', 'T1 preparatory test with a transitional expectation')
        self.owner_head = git(self.repo, 'rev-parse', 'HEAD')
        (self.repo / 'app.py').write_text('value = 2\n')
        git(self.repo, 'commit', '-am', 'T2 producer changes behavior')
        self.head = git(self.repo, 'rev-parse', 'HEAD')
        self.checkpoint = dict(commit=self.owner_head, capture_sha256='a' * 64)
        self.value = self.contract()
        self.scheduler = SimpleNamespace(wt=self.repo)

    def contract(self, plan=None):
        return transitional_contract(plan or self.plan, 'TE-1', self.repo, self.head, self.checkpoint)

    def review(self, value, changes=()):
        return dict(contract_sha256=digest(value), coverage_preserved=True,
                    approved_test_changes=list(changes))

    def change(self, new_sha256):
        return dict(path=TEST_PATH, old_sha256=self.value['files'][TEST_PATH]['sha256'],
                    new_sha256=new_sha256, reason='T3 replaces the transitional expectation of TE-1')

    def test_contract_binds_record_owner_successor_and_obligations(self):
        value = self.value
        self.assertEqual(value['expectation'], self.plan['transitional_expectations'][0])
        self.assertEqual(value['files'], {TEST_PATH: dict(kind='COMPLETED_TEST', owners=['T1'],
                                                          sha256=file_hash(self.test))})
        self.assertEqual(value['owners']['T1']['task'], self.plan['tasks'][0])
        self.assertEqual(value['owners']['T1']['commit'], self.owner_head)
        self.assertEqual(value['successor'], self.plan['tasks'][2])
        self.assertEqual(value['successor']['ac_refs'], ['AC-1'])
        with self.assertRaises(TaskError):
            transitional_contract(self.plan, 'TE-404', self.repo, self.head, self.checkpoint)
        with self.assertRaises(TaskError):
            transitional_contract(self.plan, 'TE-1', self.repo, self.head, dict(commit=self.owner_head))

    def test_unreviewed_replacement_is_refused_and_reviewed_one_is_accepted(self):
        self.test.write_text(REPLACED)
        changed = [TEST_PATH]
        self.assertFalse(coverage_ok(self.scheduler, self.value, self.review(self.value), changed))
        reviewed = self.review(self.value, [self.change(file_hash(self.test))])
        self.assertTrue(coverage_ok(self.scheduler, self.value, reviewed, changed))
        # The review binds the exact new hash, not any later rewrite.
        self.test.write_text(REPLACED.replace('2)', '3)'))
        self.assertFalse(coverage_ok(self.scheduler, self.value, reviewed, changed))
        # Deleting the test is not a replacement.
        self.test.unlink()
        self.assertFalse(coverage_ok(self.scheduler, self.value, reviewed, changed))

    def test_review_does_not_survive_changed_obligations_or_record(self):
        self.test.write_text(REPLACED)
        reviewed = self.review(self.value, [self.change(file_hash(self.test))])
        validate_review(self.value, reviewed)
        mutations = (
            lambda plan: plan['tasks'][2].update(ac_refs=[]),
            lambda plan: plan['tasks'][0].update(sec_refs=['SEC-1']),
            lambda plan: plan['tasks'][2]['verification']['checks'][0].update(argv=['true']),
            lambda plan: plan['transitional_expectations'][0].update(final_check_id='CHECK-OTHER'),
            lambda plan: plan['transitional_expectations'][0].update(expectation='app.value == 1'),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                plan = transitional_plan(self)
                mutate(plan)
                # Build from the owner's test bytes, as before the successor attempt.
                self.test.write_text(PREPARATORY)
                value = self.contract(plan)
                self.test.write_text(REPLACED)
                with self.assertRaises(TaskError):
                    validate_review(value, reviewed)


class SyntheticT12RegressionTests(GateFixture):
    """SYNTHETIC regression with the shape of T12; not a replay of a real run.

    A preparatory test keeps the previous expectation, the producer changes
    behavior and the successor omits the update. The gate still runs and records
    its evidence; the bounce reader must refuse it before any corrective scope
    (repair roots and revalidation tasks) is fixed.
    """

    def stage(self, update, expectation=None, tail='# T3 successor\n'):
        wt = self.orch.worktree_dir
        (wt / 'tests').mkdir()
        (wt / 'tests/__init__.py').write_text('')
        (wt / TEST_PATH).write_text(PREPARATORY)
        git(wt, 'add', 'tests')
        git(wt, 'commit', '-m', 'T1 preparatory test with a transitional expectation')
        (wt / 'app.py').write_text('value = 2\n')
        git(wt, 'commit', '-am', 'T2 producer changes behavior')
        # The successor touches its test; only the updated variant replaces it.
        (wt / TEST_PATH).write_text((REPLACED if update else PREPARATORY) + tail)
        git(wt, 'commit', '--allow-empty', '-am', 'T3 successor')
        head = git(wt, 'rev-parse', 'HEAD')
        self.orch.state['task_verified_head'] = head
        path = self.orch.dir_dev / 'implementation.diff'
        path.write_text(git(wt, 'diff', '--binary', self.base, 'HEAD') + '\n')
        self.orch.record_artifact('03-dev/implementation.diff', path)
        self.plan = transitional_plan(self)
        if expectation is not None:
            self.plan['transitional_expectations'][0]['expectation'] = expectation
        atomic_json(self.orch.dir_design / 'plan.json', self.plan)
        self.orch.record_artifact('02-design/plan.json', self.orch.dir_design / 'plan.json')
        self.orch._save_state()
        test =subprocess.run(['python3', '-B', '-m', 'unittest', 'tests.test_value'], cwd=wt,
                              capture_output=True, text=True, timeout=60,
                              env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(git(wt, 'status', '--porcelain'), '')
        return test

    def read(self):
        state = self.orch._load_or_init_state()
        return bounce.read_evidence(root=self.orch.ssdlc_root, state=state, repo=self.orch.repo_root,
                                    worktree=self.orch.worktree_dir, phase='04-review', attempt=1)

    def test_omitted_update_bounces_before_scope_is_fixed(self):
        test = self.stage(update=False)
        self.assertNotEqual(test.returncode, 0, 'the synthetic bounce must be a real test failure')
        self.assertIn('2 != 1', test.stderr)
        # The plan itself is valid: the defect is only in the successor's work.
        self.validator('AC-1: Correct behavior\nSEC-1: Secure behavior\n').validate(self.plan)
        result, _ = self.run_review()
        self.assertTrue(result[0], 'the gate passes, so detection comes from recorded evidence')
        value = self.read()
        self.assertEqual(value['status'], 'blocked')
        self.assertEqual(value['blockers'], [{'code': 'contradictory_evidence'}])
        self.assertIn(value['blockers'][0]['code'], ('integrity_mismatch', 'contradictory_evidence'))
        self.assertFalse(value['execution_authorized'])
        for field in ('plan', 'evaluations'):
            self.assertFalse(value.get(field))
        preview = self.orch.preview_bounce('04-review', 1)
        self.assertEqual(preview, dict(status='blocked', execution_authorized=False,
                                       blockers=[{'code': 'contradictory_evidence'}]))
        self.assertFalse((self.orch.ssdlc_root / 'bounce-preview').exists())

    def test_expectation_with_trailing_newline_at_end_of_file_bounces(self):
        # The expectation includes its newline and is the test's last line: the
        # blob must be compared without stripping that newline.
        self.assertTrue(PREPARATORY.endswith(EXPECTATION + '\n'))
        self.stage(update=False, expectation=EXPECTATION + '\n', tail='')
        self.validator('AC-1: Correct behavior\nSEC-1: Secure behavior\n').validate(self.plan)
        self.assertTrue(self.run_review()[0][0])
        value = self.read()
        self.assertEqual(value['status'], 'blocked')
        self.assertEqual(value['blockers'], [{'code': 'contradictory_evidence'}])

    def test_removed_test_is_contradictory_evidence(self):
        self.stage(update=True)
        wt = self.orch.worktree_dir
        git(wt, 'rm', '-q', TEST_PATH)
        git(wt, 'commit', '-m', 'T3 removes the test')
        self.orch.state['task_verified_head'] = git(wt, 'rev-parse', 'HEAD')
        path = self.orch.dir_dev / 'implementation.diff'
        path.write_text(git(wt, 'diff', '--binary', self.base, 'HEAD') + '\n')
        self.orch.record_artifact('03-dev/implementation.diff', path)
        self.orch._save_state()
        self.run_review()
        self.assertEqual(self.read()['blockers'], [{'code': 'contradictory_evidence'}])

    def test_updated_successor_reads_verified_evidence(self):
        test = self.stage(update=True)
        self.assertEqual(test.returncode, 0, test.stderr)
        self.assertTrue(self.run_review()[0][0])
        value = self.read()
        self.assertEqual(value['status'], 'verified', value)
        proposal = bounce.plan_bounce(repo=self.orch.repo_root, artifacts=self.orch.ssdlc_root,
            plan=value['plan'], requirements_text=value['requirements_text'], quality=value['quality'],
            reviewed_commit=value['reviewed_commit'], evaluations=value['evaluations'],
            phase=value['phase'], attempt=value['attempt'])
        self.assertEqual(proposal['status'], 'empty')
        self.assertFalse(proposal['execution_authorized'])


if __name__ == '__main__':
    unittest.main()
