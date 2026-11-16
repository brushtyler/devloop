"""devloop: acceptance criteria coverage map in agent output and review (#134)."""

import unittest

from devloop import acceptance, markdown, review, schema
from devloop.dispatcher import PROMPT_DIR, Dispatcher
from test_devloop import (CFG, META, REV, SHA1, FakeGH, FakeRunner, FakeWS, finding, fixed,
                          implemented, pr, raw_review)

ISSUE = """Motivation.

## Requisiti

- [ ] Not a criterion: another section.
1. Neither is this.

## Criteri di accettazione

- [ ] The schemas have `acceptance_map`.
  There are tests.
- [x] The prompt includes the map.
- a plain bullet, not a criterion

### Notes inside the section

1. A numbered criterion.
2) Another numbered one.

## Dopo

- [ ] Out of the section.
"""

CRITERIA = ['The schemas have `acceptance_map`. There are tests.', 'The prompt includes the map.',
            'A numbered criterion.', 'Another numbered one.']


def entry(criterion, status='covered', reason='', tests=(('tests/test_x.py', 'test_x'),)):
    return {'criterion': criterion, 'status': status, 'reason': reason,
            'tests': [{'file': f, 'name': n} for f, n in tests]}


def covered_map(criteria=CRITERIA):
    return [entry(c) for c in criteria]


def normalized(raw, criteria=(), acceptance_map=None):
    return review.normalize_review(raw, previous_open=[], round_no=1, max_rounds=3,
                                   head_sha=SHA1, ci='pass', reviewer=REV, criteria=criteria,
                                   acceptance_map=acceptance_map)


class IssueGH(FakeGH):
    """FakeGH whose issue has the given body."""

    def __init__(self, body, **kw):
        super().__init__(**kw)
        self.body = body

    def issue(self, number):
        return dict(super().issue(number), body=self.body)

    def issue_history(self, number):
        return dict(super().issue_history(number), body=self.body)


class SchemaTests(unittest.TestCase):
    def test_implement_and_fix_require_the_map(self):
        for name, output in (('implement', implemented()), ('fix', fixed())):
            with self.subTest(schema=name):
                s = schema.load_schema(name)
                self.assertIn('acceptance_map', s['required'])
                schema.validate(dict(output, acceptance_map=covered_map()), s)
                without = {k: v for k, v in output.items() if k != 'acceptance_map'}
                for bad in (without, dict(output, acceptance_map=[entry('c', status='maybe')]),
                            dict(output, acceptance_map=[dict(entry('c'), extra=1)]),
                            dict(output, acceptance_map=[dict(entry('c'), tests=[{'file': 'x'}])])):
                    with self.assertRaises(schema.SchemaError):
                        schema.validate(bad, s)


class CriteriaTests(unittest.TestCase):
    def test_checkbox_and_numbered_items_of_the_section_in_order(self):
        self.assertEqual(acceptance.acceptance_criteria(ISSUE), CRITERIA)

    def test_english_heading_and_section_end(self):
        body = ('# Title\n\n### Acceptance criteria:\n\n1. One.\n2. Two.\n\n### Other\n\n'
                '3. Three.\n')
        self.assertEqual(acceptance.acceptance_criteria(body), ['One.', 'Two.'])

    def test_no_recognisable_criteria(self):
        for body in (None, '', 'Do the thing.\n\n- [ ] a task, outside any criteria section',
                     '## Criteri di accettazione\n\n- only plain bullets\n- here\n',
                     '## Criteri di accettazione\n\n```\n- [ ] in a code block\n```\n'):
            with self.subTest(body=body):
                self.assertEqual(acceptance.acceptance_criteria(body), [])

    def test_latest_map_is_the_latest_fix_that_carries_one(self):
        meta = dict(META, acceptance_map=[entry('a')])
        self.assertEqual(acceptance.latest_map(meta, []), [entry('a')])
        fixes = [{'round': 1, 'acceptance_map': [entry('b')]}, {'round': 2}]  # e.g. pre-#134
        self.assertEqual(acceptance.latest_map(meta, fixes), [entry('b')])
        self.assertIsNone(acceptance.latest_map(META, [{'round': 1}]))
        self.assertIsNone(acceptance.latest_map(meta, [{'acceptance_map': [{'criterion': 1}]}]))


class NormalizeReviewTests(unittest.TestCase):
    def test_not_covered_without_reason_blocks_an_approval(self):
        amap = covered_map()
        amap[1] = entry(CRITERIA[1], status='not_covered', reason='  ', tests=())
        record = normalized(raw_review(), CRITERIA, amap)
        self.assertEqual(record['verdict'], 'request_changes')
        (derived,) = record['blocking_findings']
        self.assertEqual(derived['key'], f'1/{REV}/R1-1')
        self.assertIn('Criterio di accettazione 2', derived['title'])
        self.assertIn('mappa di copertura', record['note'])

    def test_not_covered_with_reason_is_left_to_the_reviewer(self):
        amap = covered_map()
        amap[1] = entry(CRITERIA[1], status='not_covered', reason='Needs #133.', tests=())
        self.assertEqual(normalized(raw_review(), CRITERIA, amap)['verdict'], 'approve')
        record = normalized(raw_review('request_changes', [finding('weak test')]), CRITERIA, amap)
        self.assertEqual([f['title'] for f in record['blocking_findings']], ['weak test'])

    def test_derived_findings_follow_the_reviewer_ones(self):
        amap = [entry(c, status='not_covered', tests=()) for c in CRITERIA[:2]]
        record = normalized(raw_review('request_changes', [finding()]), CRITERIA[:2], amap)
        self.assertEqual([f['id'] for f in record['blocking_findings']], ['R1-1', 'R1-2', 'R1-3'])
        self.assertEqual(record['blocking_findings'][0]['title'], 'bug')

    def test_missing_or_incomplete_map_blocks_when_the_issue_has_criteria(self):
        for amap in (None, [], covered_map()[:3]):
            with self.subTest(amap=amap):
                record = normalized(raw_review(), CRITERIA, amap)
                self.assertEqual(record['verdict'], 'request_changes')
                self.assertEqual(len(record['blocking_findings']), 1)

    def test_issue_without_criteria_needs_no_map(self):
        for amap in (None, []):
            with self.subTest(amap=amap):
                record = normalized(raw_review(), [], amap)
                self.assertEqual(record['verdict'], 'approve')
                self.assertEqual(record['blocking_findings'], [])
                self.assertEqual(record['note'], '')

    def test_defaults_keep_the_previous_behaviour(self):
        record = review.normalize_review(raw_review(), previous_open=[], round_no=1, max_rounds=3,
                                         head_sha=SHA1, ci='pass', reviewer=REV)
        self.assertEqual(record['verdict'], 'approve')


class DispatcherTests(unittest.TestCase):
    def make(self, runner, body=ISSUE, ws=None):
        self.gh, self.ws = IssueGH(body), ws or FakeWS()
        return Dispatcher(CFG, self.gh, self.ws, runner, log=lambda m: None)

    def test_implementer_receives_the_criteria_and_the_pr_records_its_map(self):
        runner = FakeRunner(implemented(acceptance_map=covered_map()))
        d = self.make(runner)
        d._do_implement(7)
        prompt = runner.calls[0][3]
        self.assertIn('`acceptance_map`', prompt)
        self.assertIn('\n'.join(f'{i}. {c}' for i, c in enumerate(CRITERIA, 1)), prompt)
        body = self.gh.created[0]['body']
        self.assertEqual(markdown.read_marker(body, 'pr')['acceptance_map'], covered_map())
        self.assertIn('### Copertura dei criteri di accettazione', body)
        self.assertIn('`tests/test_x.py`: test_x', body)

    def test_issue_without_criteria_declares_an_empty_map(self):
        runner = FakeRunner(implemented())
        d = self.make(runner, body='Do the thing.')
        d._do_implement(7)
        self.assertIn('no recognisable acceptance criteria', runner.calls[0][3])
        body = self.gh.created[0]['body']
        self.assertEqual(markdown.read_marker(body, 'pr')['acceptance_map'], [])
        self.assertIn('Mappa vuota: nessun criterio di accettazione riconoscibile', body)
        # The review of that PR blocks nothing on the map.
        runner = FakeRunner(raw_review())
        d = self.make(runner, body='Do the thing.')
        p = pr()
        p.meta = dict(META, acceptance_map=[])
        self.gh.labels[100] = set(p.labels)
        d._do_review(p)
        self.assertEqual(markdown.read_marker(self.gh.comments[0][1], 'review')['verdict'],
                         'approve')

    def test_review_prompt_includes_the_map_and_asks_to_verify_it(self):
        text = ' '.join((PROMPT_DIR / 'review.md').read_text().split())
        self.assertIn('Verify every entry against the code and the tests', text)
        self.assertIn('whose tests do not actually verify it, is a blocking finding', text)
        amap = covered_map()
        amap[3] = entry(CRITERIA[3], status='not_covered', reason='', tests=())
        runner = FakeRunner(raw_review())
        d = self.make(runner)
        p = pr()
        p.meta = dict(META, acceptance_map=amap)
        self.gh.labels[100] = set(p.labels)
        d._do_review(p)
        prompt = runner.calls[0][3]
        self.assertIn('## Acceptance criteria coverage', prompt)
        self.assertIn(acceptance.format_map(amap), prompt)
        self.assertIn(f'4. {CRITERIA[3]}', prompt)
        record = markdown.read_marker(self.gh.comments[0][1], 'review')
        self.assertEqual(record['verdict'], 'request_changes')
        self.assertEqual(self.gh.labels[100], {'agent:changes'})

    def test_pr_without_map_gets_a_finding_and_the_fix_provides_it(self):
        runner = FakeRunner(raw_review())
        d = self.make(runner)
        p = pr()  # opened before #134: its marker carries no map
        self.gh.labels[100] = set(p.labels)
        d._do_review(p)
        self.assertIn('No acceptance map is recorded', runner.calls[0][3])
        first = markdown.read_marker(self.gh.comments[0][1], 'review')
        self.assertEqual(first['verdict'], 'request_changes')
        self.assertIn('assente', first['blocking_findings'][0]['title'])
        # The fix records the full map, which holds from then on.
        runner = FakeRunner(fixed(acceptance_map=covered_map()))
        d = self.make(runner)
        d._do_fix(pr(labels=['agent:changes'], reviews=[first]))
        self.assertIn('acceptance_map', runner.calls[0][3])
        self.assertIn('No acceptance map is recorded', runner.calls[0][3])
        fix = markdown.read_marker(self.gh.comments[0][1], 'fix')
        self.assertEqual(fix['acceptance_map'], covered_map())
        self.assertIn('### Copertura dei criteri di accettazione', self.gh.comments[0][1])
        # The next review gets the fix's map, and the derived finding is gone.
        runner = FakeRunner(raw_review(previous=[{'id': f'1/{REV}/R1-1', 'status': 'resolved',
                                                  'note': 'ok'}]))
        d = self.make(runner)
        p = pr(reviews=[first], fixes=[fix])
        self.gh.labels[100] = set(p.labels)
        d._do_review(p)
        self.assertIn(acceptance.format_map(fix['acceptance_map']), runner.calls[0][3])
        second = markdown.read_marker(self.gh.comments[0][1], 'review')
        self.assertEqual((second['round'], second['verdict']), (2, 'approve'))


if __name__ == '__main__':
    unittest.main()
