"""devloop: a blocking finding states its evidence, otherwise it does not send the PR to the fix
(#481).

A reviewer's blocking finding carries ``evidence``: the scenario that shows it, or the code and
the rule it violates with the quotation. One without it is recorded as non-blocking, and the
record says so, unless it is one of security, which stays blocking. The review and fix prompts
describe the evidence and the dispute of a finding whose evidence does not hold, and the
advisory review lists the findings of the quorum without evidence or fixed without need."""

import dataclasses
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from devloop import markdown, review
from devloop.config import PromptFacts
from devloop.context import (_format_findings, _template, format_previous_rounds,
                             owner_assessments, prompt_values)
from devloop.dispatch_advisory import (UNFOUNDED_TITLE, normalize_advisory,
                                       render_advisory_review)
from devloop.dispatcher import Dispatcher
from devloop.schema import load_schema
from helpers import ssdlc_project
from test_devloop import (CFG, IMPL, REV, SHA1, FakeGH, FakeRunner, FakeWS, finding, fixed, pr,
                          raw_review)
from test_devloop_advisory import Base as AdvisoryBase
from test_devloop_advisory import answer
from test_devloop_findings import OTHER, Flow, reviewed_by
from test_devloop_trust_loop import PARTS, resolving, trust

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
                # Neutral, never an approval (#481): it does not send the PR to the fix.
                self.assertEqual(record['verdict'], review.UNPROVEN)
                self.assertIn('non è un\'approvazione', record['note'])
                self.assertEqual(review.open_findings([record]), [])
                self.assertEqual(review.flow_verdict(record), review.UNPROVEN)
                self.assertEqual(markdown.native_event(record), 'COMMENT')
                body = markdown.render_review(record, 'h')
                self.assertIn('**Verdetto:** Nessun finding bloccante con evidenza: non '
                              'approvata', body)
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

    def test_another_owners_open_finding_does_not_keep_the_request_for_changes(self):
        # Another reviewer's finding the reviewer cannot close, and a CI one, stay open: they
        # are not the reviewer's to fix, and its own findings without evidence leave nothing.
        first = reviewed_by(OTHER, raw_review('request_changes', [finding('theirs')]))
        ci = review.ci_failure_review(failed_checks=['Unit Tests'], log_tail='boom',
                                      previous_open=review.open_findings([first]), round_no=1,
                                      max_rounds=3, head_sha=SHA1)
        previous = review.open_findings([first, ci])
        self.assertEqual({f['owner'] for f in previous}, {OTHER, 'ci'})
        second = review.normalize_review(
            raw_review('request_changes', [finding('new', evidence='')],
                       previous=[{'id': f['key'], 'status': 'unresolved', 'note': 'n'}
                                 for f in previous]),
            previous_open=previous, round_no=2, max_rounds=3, head_sha=SHA1, ci='pass',
            reviewer=REV)
        self.assertEqual(second['verdict'], review.UNPROVEN)
        self.assertEqual(second['blocking_findings'], [])
        self.assertTrue(second['non_blocking_findings'][0]['without_evidence'])
        # The other owners' findings stay open: the dispatcher checks them before promoting.
        self.assertEqual({f['owner'] for f in review.open_findings([first, ci, second])},
                         {OTHER, 'ci'})


class NeutralVerdictTests(unittest.TestCase):
    """The supervisor's request on #482: a reviewer whose blocking findings were all demoted
    for lack of evidence gives no approval, neither for the quorum nor for clearing its
    ``trust_policy`` (#461)."""

    def demoted(self, earlier=(), round_no=1, reviewer=REV):
        left = review.open_findings(list(earlier))
        raw = raw_review('request_changes', [finding('unproven', evidence='')], previous=[
            {'id': f['key'], 'status': 'resolved', 'note': 'ok'} for f in left])
        return reviewed_by(reviewer, raw, left, round_no=round_no)

    def test_the_round_is_not_approved_without_the_explicit_approvals(self):
        approval = reviewed_by(OTHER, raw_review('approve'))
        neutral = self.demoted()
        self.assertEqual(neutral['verdict'], review.UNPROVEN)
        # The quorum counts the reviewers whose flow verdict is an approval: not this one.
        self.assertNotEqual(review.flow_verdict(neutral), 'approve')
        self.assertEqual(review.round_verdict([approval, neutral]), review.UNPROVEN)
        self.assertEqual(review.round_verdict([neutral]), review.UNPROVEN)
        # A request for changes or the operator's decision still prevails.
        fixing = reviewed_by(OTHER, raw_review('request_changes', [finding()]))
        self.assertEqual(review.round_verdict([fixing, neutral]), 'request_changes')
        deciding = reviewed_by(OTHER, raw_review('needs_operator', human='scelta'))
        self.assertEqual(review.round_verdict([deciding, neutral]), 'needs_operator')
        # The record of the neutral verdict is valid, and shown in the receipt.
        self.assertEqual(markdown.read_marker(markdown.render_review(neutral, 'h'), 'review'),
                         neutral)

    def test_the_trust_policy_does_not_fall_without_an_explicit_approval(self):
        first = reviewed_by(REV, trust([finding()]))
        # Its own finding closed, and a new one without evidence: no approval, the trust
        # policy still holds the merge.
        second = self.demoted([first], round_no=2)
        self.assertEqual(second['verdict'], review.UNPROVEN)
        self.assertEqual([p['status'] for p in second['previous_findings']], ['resolved'])
        self.assertEqual(review.trust_policy_parts([first, second]), [(REV, 1, PARTS)])
        # An explicit approval of the same round clears it, as #461 decided.
        self.assertEqual(review.trust_policy_parts([first, resolving([first])]), [])


class DisputeTests(unittest.TestCase):
    """A finding disputed for its evidence, kept open by its owner with new evidence, and fixed
    again: the implementer sees the new evidence, not only the one it disputed."""

    OLD, NEW = 'OLD: input 1 fails', 'NEW: input 2 fails; output=error'

    def rounds(self):
        key = f'1/{REV}/R1-1'
        first = reviewed_by(REV, raw_review('request_changes', [finding(evidence=self.OLD)]))
        assess = [{'id': key, 'status': 'unresolved', 'note': self.NEW}]
        second = reviewed_by(REV, raw_review('request_changes', previous=assess),
                             review.open_findings([first]), round_no=2)
        # Another reviewer's assessment of the same finding is not its owner's.
        foreign = reviewed_by(OTHER, raw_review('approve', previous=[
            dict(assess[0], note='FOREIGN: looks fine')]), review.open_findings([first]),
            round_no=2)
        return key, first, second, foreign

    def test_the_fix_after_a_dispute_shows_the_new_evidence(self):
        key, first, second, foreign = self.rounds()
        self.assertEqual(owner_assessments([first, second, foreign]),
                         {key: (2, 'unresolved', self.NEW)})
        runner = FakeRunner(fixed())
        Dispatcher(CFG, FakeGH(), FakeWS(), runner, log=lambda m: None)._do_fix(
            pr(labels=['agent:changes'], reviews=[first, second, foreign]))
        ((_, mode, schema, prompt),) = runner.calls
        self.assertEqual(schema, 'fix')
        self.assertIn(f'Evidence: {self.OLD}', prompt)
        self.assertIn(f'Latest assessment by its owner (round 2, unresolved): {self.NEW}', prompt)
        self.assertNotIn('FOREIGN', prompt)
        self.assertIn('the `Latest assessment by its owner`, which answers an earlier dispute '
                      'with new evidence', flat(prompt))

    def test_the_previous_rounds_show_the_dispute_and_the_new_evidence(self):
        key, first, second, _ = self.rounds()
        fix = {'round': 1, 'head_sha': SHA1, 'summary': 's', 'responses': [
            {'finding_id': key, 'action': 'disputed', 'note': 'input 1 passes: exit 0'}]}
        text = format_previous_rounds([first, second], [fix])
        self.assertIn(f'Evidence: {self.OLD}', text)
        self.assertIn(f'- {key}: disputed: input 1 passes: exit 0', text)
        self.assertIn(f'Assessments of earlier findings:\n- {key}: unresolved: {self.NEW}', text)


class FlowTests(Flow, unittest.TestCase):
    def test_a_finding_without_evidence_does_not_send_the_pr_to_the_fix(self):
        self.make([], runner=FakeRunner(raw_review('request_changes',
                                                   [finding(evidence='')])))
        self.d.tick()
        (recorded,) = self.published('review')
        self.assertEqual(recorded['verdict'], review.UNPROVEN)
        self.assertEqual(recorded['blocking_findings'], [])
        self.assertNotIn('agent:changes', self.gh.labels[100])
        self.assertNotIn('fix', [a.kind for a in self.planned()])

    def test_a_reviewer_with_only_demoted_findings_does_not_approve_the_pr(self):
        # The supervisor's request on #482: the neutral verdict is no approval for the quorum.
        # The PR is neither promoted nor fixed: it stays in review, and the next tick plans a
        # new review of the same head.
        self.make([], runner=FakeRunner(raw_review('request_changes',
                                                   [finding(evidence='')])))
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.assertEqual(self.published('policy'), [])  # no promotion
        (action,) = self.planned()
        self.assertEqual((action.kind, action.reason),
                         ('review', 'a reviewer asked for changes without evidence and did not '
                                    'approve'))
        # The recorded round is never replayed as an approval.
        self.assertNotIn('apply_review', [a.kind for a in self.planned()])

    def test_at_the_round_limit_the_neutral_round_goes_to_the_operator(self):
        self.make([], runner=FakeRunner(raw_review('request_changes', [finding(evidence='')])),
                  cfg=dataclasses.replace(CFG, max_review_rounds=1))
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.assertEqual([a.kind for a in self.planned()], ['round_limit'])

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


class AdvisoryTurnTests(AdvisoryBase):
    def test_the_advisory_turn_gets_the_findings_without_evidence(self):
        record = reviewed_by(REV, raw_review('request_changes', [
            dict(finding('UNIQUE_DEMOTED_FINDING', evidence=''), detail='DEMOTED_DETAIL')]))
        self.make(answer())
        p = dataclasses.replace(self.d.snapshot().prs[0], reviews=[record])
        self.d._advisory_turn(p, IMPL)
        ((_, _, schema, prompt),) = self.runner.calls
        self.assertEqual(schema, 'advisory')
        self.assertIn('Blocking findings without evidence, recorded by devloop as non-blocking:\n'
                      f'- **1/{REV}/R1-1** (x.py:3): UNIQUE_DEMOTED_FINDING\n  DEMOTED_DETAIL',
                      prompt)
        self.assertIn('under "Blocking findings without evidence", those devloop recorded as '
                      'non-blocking because they had none: include them', flat(prompt))


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
