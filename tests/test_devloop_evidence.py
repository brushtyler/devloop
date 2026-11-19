"""devloop: a blocking finding states its evidence, otherwise it does not send the PR to the fix
(#481).

A reviewer's blocking finding carries ``evidence``: the scenario that shows it, or the code and
the rule it violates with the quotation. One without it is recorded as non-blocking, and the
record says so, unless it is one of security, which stays blocking. The review and fix prompts
describe the evidence and the dispute of a finding whose evidence does not hold, and the
advisory review lists the findings of the quorum without evidence or fixed without need."""

import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from devloop import markdown, review
from devloop.config import PromptFacts
from devloop.context import _format_findings, _template, format_previous_rounds, prompt_values
from devloop.dispatch_advisory import (UNFOUNDED_TITLE, normalize_advisory,
                                       render_advisory_review)
from devloop.schema import load_schema
from helpers import ssdlc_project
from test_devloop import REV, SHA1, FakeRunner, finding, raw_review
from test_devloop_findings import Flow, reviewed_by

ROOT = Path(__file__).resolve().parent.parent
CONFIGS = (ssdlc_project.load(), SimpleNamespace(prompts=PromptFacts()))


def flat(text):
    return ' '.join(text.split())


def normalized(raw):
    return review.normalize_review(raw, previous_open=[], round_no=1, max_rounds=3,
                                   head_sha=SHA1, ci='pass', reviewer=REV)


class SchemaTests(unittest.TestCase):
    def test_a_blocking_finding_requires_its_evidence(self):
        item = load_schema('review')['properties']['blocking_findings']['items']
        self.assertIn('evidence', item['required'])
        self.assertEqual(item['properties']['evidence'], {'type': 'string'})
        # A blocking finding without the field is not a valid answer.
        bare = {k: v for k, v in finding().items() if k != 'evidence'}
        with self.assertRaises(ValueError):
            normalized(raw_review('request_changes', [bare]))


class NormalizeTests(unittest.TestCase):
    def test_a_blocking_finding_without_evidence_is_recorded_as_non_blocking(self):
        for evidence in ('', '  \n '):
            with self.subTest(evidence=evidence):
                record = normalized(raw_review('request_changes',
                                               [finding('unproven', evidence=evidence)]))
                self.assertEqual(record['blocking_findings'], [])
                (demoted,) = record['non_blocking_findings']
                self.assertEqual((demoted['id'], demoted['title'], demoted['without_evidence']),
                                 ('R1-1', 'unproven', True))
                # The record says so, and the request for changes has nothing left to fix.
                self.assertIn('senza evidenza', record['note'])
                self.assertIn('R1-1', record['note'])
                self.assertEqual(record['verdict'], 'approve')
                self.assertEqual(review.open_findings([record]), [])
                self.assertEqual(review.flow_verdict(record), 'approve')
                body = markdown.render_review(record, 'h')
                self.assertIn('### Non bloccanti', body)
                self.assertIn('registrato come non bloccante', body)
                self.assertNotIn('### Bloccanti', body)

    def test_a_security_finding_without_evidence_stays_blocking(self):
        record = normalized(raw_review('request_changes',
                                       [finding('leak', security=True, evidence='')]))
        (kept,) = record['blocking_findings']
        self.assertEqual((kept['title'], kept['key']), ('leak', f'1/{REV}/R1-1'))
        self.assertNotIn('without_evidence', kept)
        self.assertEqual(record['non_blocking_findings'], [])
        self.assertEqual(record['verdict'], 'request_changes')
        self.assertEqual([f['key'] for f in review.open_findings([record])], [f'1/{REV}/R1-1'])
        # An approval with it is corrected, as for any blocking finding.
        approval = normalized(raw_review('approve', [finding(security=True, evidence='')]))
        self.assertEqual(approval['verdict'], 'request_changes')

    def test_only_the_findings_without_evidence_are_demoted(self):
        raw = raw_review('request_changes', [finding('a', evidence=''),
                                             finding('b', evidence='`python3 x.py` fails')])
        raw['non_blocking_findings'] = [{k: v for k, v in finding('c').items()
                                         if k != 'evidence'}]
        record = normalized(raw)
        self.assertEqual([(f['id'], f['title']) for f in record['blocking_findings']],
                         [('R1-1', 'b')])
        self.assertEqual([(f['id'], f['title'], f.get('without_evidence', False))
                          for f in record['non_blocking_findings']],
                         [('R1-2', 'a', True), ('R1-3', 'c', False)])
        self.assertEqual(record['verdict'], 'request_changes')
        body = markdown.render_review(record, 'h')
        self.assertIn('*Evidenza:* `python3 x.py` fails', body)

    def test_an_unresolved_earlier_finding_still_asks_for_changes(self):
        first = normalized(raw_review('request_changes', [finding()]))
        previous = review.open_findings([first])
        second = review.normalize_review(
            raw_review('request_changes', [finding('new', evidence='')],
                       previous=[{'id': f'1/{REV}/R1-1', 'status': 'unresolved',
                                  'note': 'Ancora: `python3 x.py` fallisce.'}]),
            previous_open=previous, round_no=2, max_rounds=3, head_sha=SHA1, ci='pass',
            reviewer=REV)
        self.assertEqual(second['verdict'], 'request_changes')
        self.assertEqual(second['blocking_findings'], [])
        self.assertEqual([f['key'] for f in review.open_findings([first, second])],
                         [f'1/{REV}/R1-1'])


class FlowTests(Flow, unittest.TestCase):
    def test_a_finding_without_evidence_does_not_send_the_pr_to_the_fix(self):
        self.make([], runner=FakeRunner(raw_review('request_changes',
                                                   [finding(evidence='')])))
        self.d.tick()
        (recorded,) = self.published('review')
        self.assertEqual(recorded['verdict'], 'approve')
        self.assertEqual(recorded['blocking_findings'], [])
        self.assertNotIn('agent:changes', self.gh.labels[100])
        self.assertNotIn('fix', [a.kind for a in self.planned()])

    def test_a_security_finding_without_evidence_sends_the_pr_to_the_fix(self):
        self.make([], runner=FakeRunner(raw_review('request_changes',
                                                   [finding(security=True, evidence='')])))
        self.d.tick()
        (recorded,) = self.published('review')
        self.assertEqual(recorded['verdict'], 'request_changes')
        self.assertEqual(self.gh.labels[100], {'agent:changes'})


class PromptTests(unittest.TestCase):
    def render(self, name, **values):
        return [flat(_template(name, **prompt_values(cfg), **values)) for cfg in CONFIGS]

    def test_the_review_prompt_asks_for_evidence_and_answers_a_dispute(self):
        for text in self.render('review', pr_number=7, repo='o/r', language='italiano'):
            self.assertIn('every blocking finding states in `evidence` what shows the defect',
                          text)
            self.assertIn('a test that fails, a command with its output, a concrete input and '
                          'the behaviour you observed', text)
            self.assertIn('the precise reference to the code and to the rule it violates', text)
            self.assertIn('devloop records a blocking finding with an empty `evidence` as '
                          'non-blocking', text)
            self.assertIn('a finding with `security` set to `true` stays blocking even without '
                          'evidence', text)
            self.assertIn('either withdraw the finding (`withdrawn`), or keep it `unresolved` '
                          'and give in `note` new evidence', text)

    def test_the_fix_prompt_shows_the_evidence_and_asks_to_verify_it(self):
        findings = _format_findings([dict(finding(evidence='`python3 x.py` exits 1'),
                                          key=f'1/{REV}/R1-1')])
        for text in self.render('fix', pr_number=7, issue_number=3, repo='o/r',
                                language='italiano', findings=findings):
            self.assertIn('Evidence: `python3 x.py` exits 1', text)
            self.assertIn('Before fixing a finding, verify its evidence', text)
            self.assertIn('run its scenario again, or read again the code and the rule it cites',
                          text)
            self.assertIn('dispute the finding (`disputed`) instead of changing the code', text)
            self.assertIn('report in the `note` of its response how you verified it and what '
                          'you observed', text)
            self.assertIn('the reviewer who opened it either withdraws it or answers with new '
                          'evidence', text)

    def test_the_previous_rounds_show_the_evidence(self):
        record = reviewed_by(REV, raw_review('request_changes',
                                             [finding(evidence='input 3 gives 4')]))
        self.assertIn('Evidence: input 3 gives 4', format_previous_rounds([record], []))


class AdvisoryTests(unittest.TestCase):
    def answer(self, unfounded):
        return {'summary': 's', 'residual_risks': [], 'operator_checks': [], 'overlooked': [],
                'unfounded': unfounded, 'opinion': 'dubbi', 'opinion_reason': 'r'}

    def test_the_advisory_prompt_asks_for_the_findings_without_evidence(self):
        self.assertIn('unfounded', load_schema('advisory')['required'])
        for cfg in CONFIGS:
            text = flat(_template('advisory', **prompt_values(cfg), pr_number=7, repo='o/r',
                                  language='italiano'))
            self.assertIn('`unfounded`: the findings of the reviews below whose evidence does '
                          'not hold, and the fixes the implementer made that were not needed',
                          text)
            self.assertIn(f'"{UNFOUNDED_TITLE}"', text)

    def test_the_advisory_review_has_the_section(self):
        advisory = normalize_advisory(self.answer(['1/codex/R1-2: lo scenario passa.', ' ']))
        self.assertEqual(advisory['unfounded'], ['1/codex/R1-2: lo scenario passa.'])
        body = render_advisory_review(advisory, 'codex', SHA1, 'h')['body']
        section = re.search(rf'### {UNFOUNDED_TITLE}\n\n(.*?)\n\n###', body, re.S)
        self.assertIsNotNone(section)
        self.assertEqual(section.group(1), '- 1/codex/R1-2: lo scenario passa.')
        empty = render_advisory_review(normalize_advisory(self.answer([])), 'codex', SHA1, 'h')
        self.assertIn(f'### {UNFOUNDED_TITLE}\n\n_Nessuno indicato._', empty['body'])


class DocsTests(unittest.TestCase):
    def test_protocol_and_reference_describe_the_field(self):
        for path in ('docs/devloop/protocol.md', 'docs/devloop/reference.md'):
            with self.subTest(path=path):
                text = flat((ROOT / path).read_text(encoding='utf-8'))
                self.assertIn('`evidence`', text)
                self.assertIn('`without_evidence: true`', text)
                self.assertIn('#481', text)


if __name__ == '__main__':
    unittest.main()
