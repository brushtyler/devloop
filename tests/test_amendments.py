import copy
import json
import unittest

from ssdlc_amendments import validate_amendment
from ssdlc_tasks import PlanError


class AmendmentTests(unittest.TestCase):
    def setUp(self):
        self.old = {'plan_revision': 1, 'tasks': [
            {'task_id': 'A', 'depends_on': []}, {'task_id': 'B', 'depends_on': ['A']},
            {'task_id': 'C', 'depends_on': ['B']}]}
        self.new = copy.deepcopy(self.old)
        self.new['plan_revision'] = 2
        self.report = json.dumps({'verdict': 'APPROVED', 'findings': []})

    def check(self, lineage=None):
        return validate_amendment(self.old, self.new, self.report, lambda p: p['tasks'], [], {}, lineage=lineage)

    def test_preserves_transitive_order(self):
        self.assertEqual(self.check()['ordered_task_ids'], ['A', 'B', 'C'])
        self.new['tasks'][2]['depends_on'] = ['A']
        with self.assertRaisesRegex(PlanError, 'transitive ordering'):
            self.check()

    def test_merge_preserves_remaining_dependencies(self):
        self.new['tasks'] = [{'task_id': 'AB', 'depends_on': []}, {'task_id': 'C', 'depends_on': ['AB']}]
        self.check({'A': ['AB'], 'B': ['AB'], 'C': ['C']})
        self.new['tasks'][1]['depends_on'] = []
        with self.assertRaises(PlanError):
            self.check({'A': ['AB'], 'B': ['AB'], 'C': ['C']})

    def test_split_requires_each_predecessor(self):
        self.new['tasks'] = [{'task_id': 'A1', 'depends_on': []}, {'task_id': 'A2', 'depends_on': []},
                             {'task_id': 'B', 'depends_on': ['A1', 'A2']}, {'task_id': 'C', 'depends_on': ['B']}]
        self.check({'A': ['A1', 'A2'], 'B': ['B'], 'C': ['C']})
        self.new['tasks'][2]['depends_on'].remove('A2')
        with self.assertRaises(PlanError):
            self.check({'A': ['A1', 'A2'], 'B': ['B'], 'C': ['C']})

    def test_missing_lineage_and_skipped_revision_refused(self):
        self.new['tasks'].pop()
        with self.assertRaises(PlanError):
            self.check()
        self.new = copy.deepcopy(self.old)
        with self.assertRaises(PlanError):
            self.check()

    def test_review_cannot_be_omitted(self):
        self.report = 'The model said it approved'
        with self.assertRaises(PlanError):
            self.check()
