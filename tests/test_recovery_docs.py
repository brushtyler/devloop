"""The recovery index and the split between reference and history in the recovery
documents (issue #245, ADR 0003 I8)."""
import re
import unittest

import doc_contracts as docs
from ssdlc_recovery import STOPS

INDEX = 'docs/recovery.md'
RECOVERY_DOCUMENTS = ('candidate-capture.md', 'bounded-recovery.md', 'continuations.md',
                      'historical-reconciliation.md', 'scope-amendments.md',
                      'developer-feedback.md', 'native-gate-convergence.md',
                      'bounce-preview.md', 'writer-fencing.md')

# Increment parts moved into the History section of each document, not deleted: one phrase
# for each moved block.
HISTORY = {
    'candidate-capture.md': [
        'increment I0 of PLAN_AUTONOMY_WORKSTREAMS.md',
        'The issue #22 preserved T01 has not been imported, verified or resumed by implementing I0',
        'python3 -m unittest discover -s tests -p test_candidate_capture.py -v',
        'I2 integrity rejection hardening (#29)'],
    'bounded-recovery.md': [
        'I2 implements A4.4–A4.7, A6b, the new A3 stop inventory',
        'PR #26 is its independently reviewable first increment for new runs',
        'Other new-run I2 hardening remains in issue #29',
        'The v2 baseline adds oscillating findings',
        'tests/test_i2_hardening.py additionally covers',
        'The issue #22 v4 fixture-boundary revision'],
    'continuations.md': [
        'This implements the first compatibility rule in PLAN_AUTONOMY_WORKSTREAMS.md',
        "Issue #22's real run has only been inventoried read-only while implementing I1"],
    'historical-reconciliation.md': [
        'Verification and next rollout',
        'Hardening of other new-run I2 paths is tracked separately in #29'],
    'scope-amendments.md': [
        'This implements the post-checkpoint scope-recovery case needed by #22 under A6a',
        'Offline validation and its limits are recorded in',
        'separately tracks native phase-boundary stops'],
    'developer-feedback.md': [
        'Issue #39 extends A6a. Its implementation is split into three increments',
        'Implementation status', 'Decisions from the two issue reviews',
        'resolve the Claude/Gemini review comments as follows',
        'Offline acceptance covers funded activation and repair',
        'Historical #22 is untouched',
        'Live rollout is opt-in after review'],
}
INCREMENT_HEADINGS = re.compile(
    r'^#{2,3} (Implementation status|Decisions from the two issue reviews|Offline acceptance'
    r'|Verification and next rollout|.*\(#29\)|.*issue #29 validation)\s*$', re.M)
# Baseline of the moved blocks, one file per document: the content of its History when
# the increment parts were moved there. Nothing in it may disappear from the History.
BASELINE = docs.ROOT / 'tests' / 'fixtures' / 'recovery-history'


def reference_and_history(name):
    text = (docs.ROOT / 'docs' / name).read_text()
    reference, marker, history = text.partition('\n## History\n')
    return reference, marker, history


def units(text):
    """Headings, paragraphs, list items and table rows, each normalized on its own."""
    found = []
    for block in re.split(r'\n\s*\n', text):
        current = []
        for line in block.splitlines():
            if re.match(r'\s*(?:[-*] |\d+\. |\|)', line) and current:
                found.append(current)
                current = []
            current.append(line)
        if current:
            found.append(current)
    return [docs.normalize('\n'.join(lines)) for lines in found]


class RecoveryIndexTests(unittest.TestCase):
    def test_the_index_is_reachable_from_the_readme(self):
        self.assertIn(INDEX, docs.user_facing_docs().names)

    def test_the_index_links_every_recovery_document(self):
        text = (docs.ROOT / INDEX).read_text()
        linked = set(docs.local_links(text))
        self.assertEqual([name for name in RECOVERY_DOCUMENTS if name not in linked], [])

    def test_the_index_goes_from_stop_to_transition_to_document(self):
        table = docs.plan_section(INDEX, 'From stop to transition', root=docs.ROOT)
        docs.assert_documented(self, table, ['| Stop or situation | Transition | Document |'])
        # Every registered recovery stop, and the scope stop, has a row.
        for stop in sorted(STOPS) + ['SCOPE_REQUIRED']:
            with self.subTest(stop=stop):
                docs.assert_documented(self, table, [stop])
        for command in ('continuation-scope-', 'continuation-limits-', 'usage-profile-',
                        'feedback-repair-', 'feedback-profile-', 'bounce-preview'):
            with self.subTest(command=command):
                docs.assert_documented(self, table, [command])


class ReferenceAndHistoryTests(unittest.TestCase):
    def test_each_recovery_document_ends_with_its_history(self):
        for name, phrases in HISTORY.items():
            with self.subTest(document=name):
                reference, marker, history = reference_and_history(name)
                self.assertTrue(marker, f'{name} has no History section')
                self.assertNotIn('\n## ', history, f'{name}: History is not the last section')
                docs.assert_documented(
                    self, docs.Corpus(f'{name} § History', {name: history}), phrases)
                # Moved, not copied: the reference keeps none of the increment headings.
                self.assertEqual(INCREMENT_HEADINGS.findall(reference), [])
                self.assertIn('(#history)', reference)

    def test_the_history_keeps_every_moved_block(self):
        self.assertEqual(sorted(path.name for path in BASELINE.glob('*.md')), sorted(HISTORY))
        for name in HISTORY:
            with self.subTest(document=name):
                expected = units((BASELINE / name).read_text())
                self.assertGreater(len(expected), 0)
                history = docs.normalize(reference_and_history(name)[2])
                missing = [unit[:80] for unit in expected if unit not in history]
                self.assertEqual(missing, [], f'{name}: moved content is missing from History')

    def test_the_baseline_keeps_every_decision_of_issue_39(self):
        decisions = [unit for unit in units((BASELINE / 'developer-feedback.md').read_text())
                     if unit.startswith('- ')]
        self.assertEqual(len(decisions), 9)

    def test_removing_a_moved_block_is_detected(self):
        name = 'developer-feedback.md'
        history = reference_and_history(name)[2]
        decisions = history.index('resolve the Claude/Gemini review comments as follows:')
        end = history.index('### F2 offline acceptance')
        mutilated = docs.normalize(history[:decisions] + history[end:])
        expected = units((BASELINE / name).read_text())
        missing = [unit for unit in expected if unit not in mutilated]
        self.assertEqual(len([unit for unit in missing if unit.startswith('- ')]), 9)


if __name__ == '__main__':
    unittest.main()
