"""devloop: the coordinator closes the findings of an ``open_findings`` escalation (#606, ADR 0002,
second precision of 2026-11-23, point 3).

An approval that leaves open findings no agent can close any more sends the PR to
``agent:needs-operator`` with the class ``open_findings``. With ``[roles.coordinator]`` the
coordinator's identity closes them with the form of the operator's closure, ``/devloop finding
<round/reviewer/ID> resolved|withdrawn <reason>``, as the first line of a comment; when none is
left open the PR goes back to ``agent:review`` and the recorded approval is applied again. A
finding a ``trust_policy`` review marks stays the operator's, and so does every finding while a
reviewer of the latest round asks for changes on the head; a closure without a reason, or from
any other identity, closes nothing.
"""

import unittest

from devloop import markdown, report, review, supervision
from devloop.context import _open_findings_reason
from devloop.ledger import Ledger
from test_devloop import CFG, IMPL, REV, SHA1, SHA2, finding, raw_review, receipt
from test_devloop_coordinator import COORDINATOR, coordinated, declared
from test_devloop_report import TRUST, ReportGH
from test_devloop_supervision import (AUTO, HUMAN, REVIEW, SUPER, SUPERVISOR, SUPERVISOR_LOGIN,
                                     Case)

# A reviewer other than the one of the later round: its finding is no agent's to close then.
OTHER = next(k for k in sorted(CFG.agents) if k not in (IMPL, REV))
CO = coordinated(CFG)
KEY = f'1/{OTHER}/R1-1'
REASON = 'il controllo è già nel chiamante, vedi x.py:3'
READY = 'agent:ready-to-merge'


def reviewed(reviewer, raw, previous_open=(), round_no=1, head=SHA1):
    record = review.normalize_review(raw, previous_open=list(previous_open), round_no=round_no,
                                     max_rounds=3, head_sha=head, ci='pass', reviewer=reviewer)
    record['receipt'] = receipt(head=head)
    return record


def first_round(**raw):
    """Round 1: ``OTHER`` asks for changes with one finding, ``KEY``."""
    return reviewed(OTHER, raw_review(raw.pop('verdict', 'request_changes'),
                                      raw.pop('blocking', [finding()]), **raw))


class FindingCase(Case):
    def make(self, cfg=CO, *outputs):
        d = super().make(cfg, *outputs)
        d.ledger = Ledger(self.gh)
        self.ws.tree = lambda path: 'e' * 40
        return d

    def record(self, record):
        # The supervisor's agent signs with its app in these tests; the others with the ambient
        # (trusted) login.
        login = SUPERVISOR_LOGIN if record['reviewer'] == SUPERVISOR else 'brushtyler'
        self.gh.say(100, login, markdown.publishable('review', 'review', record))

    def escalated(self, *records):
        """The thread of PR #100 with ``records``, escalated ``open_findings`` as the
        dispatcher does when an approval leaves findings of other owners open."""
        for record in records:
            self.record(record)
        self.gh.labels[100] = {HUMAN}
        left = review.open_findings(list(records))
        self.gh.say(100, 'brushtyler', markdown.render_escalation(
            _open_findings_reason(left), cls='open_findings'))

    def close(self, d, text, login=COORDINATOR):
        self.gh.command(100, text, login=login)
        return self.tick(d)

    def closures(self):
        return [c for r in self.gh.records(100, 'finding') for c in r['closed']]

    def trace(self):
        return ' '.join([c['body'] for c in self.gh.threads[100]
                         if markdown.read_marker(c['body'], 'supervision')][-1].split())

    def refused(self, d, text, why, login=COORDINATOR):
        self.assertEqual(self.close(d, text, login), [('supervise', 100)])
        decision = self.decisions(100)[-1]
        self.assertEqual((decision['command'], decision['by'], decision['outcome']),
                         ('finding', login, 'refused'))
        self.assertIn(why, self.trace())
        self.assertEqual(self.closures(), [])
        self.assertEqual(self.gh.labels[100], {HUMAN})
        return decision


class PerimeterTests(unittest.TestCase):
    def test_the_closure_is_delegated_to_the_coordinator(self):
        self.assertIn('finding', supervision.COORDINATOR_DELEGATED)
        self.assertIsNone(supervision.coordinator_refusal('finding'))
        # Not a supervisor's command: a trusted user's line stays the operator's closure.
        self.assertNotIn('finding', supervision.COMMANDS)
        self.assertTrue(supervision.recorded_command('finding', 'closed'))

    def test_the_coordinators_closure_is_a_dispatcher_record(self):
        entry = {'key': KEY, 'status': 'resolved', 'by': 'coordinator', 'author': COORDINATOR,
                 'reason': REASON, 'head_sha': SHA1}
        body = markdown.render_supervision(
            {'command': 'finding', 'by': COORDINATOR, 'reason': 'x', 'comment': 1,
             'outcome': 'closed'}, 't', records=[('finding', {'closed': [entry]})])
        self.assertEqual(markdown.read_marker(body, 'finding'), {'closed': [entry]})
        self.assertEqual(markdown.read_marker(body, 'supervision')['outcome'], 'closed')
        # Like the operator's, it closes a finding of any owner.
        self.assertEqual(review.open_findings([first_round()], [entry]), [])

    def test_the_trust_policy_marks_the_findings_it_opens_or_keeps_open(self):
        held = first_round(verdict='needs_operator', reason='trust_policy')
        self.assertEqual(review.trust_policy_findings([held]), {KEY})
        later = reviewed(OTHER, raw_review('needs_operator', reason='trust_policy', previous=[
            {'id': KEY, 'status': 'unresolved', 'note': 'n'}]),
            review.open_findings([first_round()]), round_no=2)
        self.assertEqual(review.trust_policy_findings([first_round(), later]), {KEY})
        self.assertEqual(review.trust_policy_findings([first_round()]), set())


class ClosureTests(FindingCase):
    def test_a_valid_closure_resumes_the_pr(self):
        d = self.make(CO, raw_review('approve'))
        self.record(first_round())
        self.gh.say(100, 'brushtyler', markdown.render_fix(
            {'summary': 's', 'responses': [], 'head_sha': SHA1},
            markdown.header('X', '9.9', 'm', 'high'), 1))
        self.assertEqual(self.tick(d), [('review', 100)])
        # The approval of round 2 leaves the finding of round 1 open: no agent closes it.
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.assertEqual(self.escalations(100)[-1]['class'], 'open_findings')

        self.assertEqual(self.close(d, f'/devloop finding {KEY} withdrawn {REASON}'),
                         [('supervise', 100)])
        decision = self.decisions(100)[-1]
        self.assertEqual({k: decision.get(k) for k in ('command', 'by', 'outcome', 'class',
                                                       'head')},
                         {'command': 'finding', 'by': COORDINATOR, 'outcome': 'closed',
                          'class': 'open_findings', 'head': SHA1})
        self.assertEqual(self.closures(), [{'key': KEY, 'status': 'withdrawn',
                                            'by': 'coordinator', 'author': COORDINATOR,
                                            'reason': REASON, 'head_sha': SHA1}])
        self.assertIn(f'`{KEY}`: ritirato: {REASON}', self.trace())
        self.assertEqual(self.gh.labels[100], {REVIEW})
        # The recorded approval is applied again, without a new review.
        self.tick(d)
        self.assertEqual(self.gh.labels[100], {READY})
        self.assertEqual(len(self.runner.calls), 1)
        # The command is spent: it is never applied again.
        self.assertEqual(len([r for r in self.decisions(100) if r['command'] == 'finding']), 1)

    def test_a_comment_closes_several_findings_and_a_partial_closure_waits(self):
        d = self.make()
        two = first_round(blocking=[finding(), finding('other')])
        second = f'1/{OTHER}/R1-2'
        self.escalated(two, reviewed(REV, raw_review('approve'), review.open_findings([two]),
                                     round_no=2))
        # One of two: the PR stays with the operator, and the escalation still takes commands.
        self.close(d, f'/devloop finding {KEY} resolved {REASON}')
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'closed')
        self.assertIn(f'Restano aperti `{second}`', self.trace())
        self.assertEqual(self.gh.labels[100], {HUMAN})
        self.close(d, f'/devloop finding {second} resolved corretto in x.py\n'
                      f'/devloop finding {KEY} resolved di nuovo')
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn(f'`{KEY}` non è aperto su questa PR', self.trace())
        # A command is the first line of a comment: below other text it is none.
        decided = len(self.decisions(100))
        self.assertEqual(self.close(d, f'Nota.\n/devloop finding {second} resolved x'), [])
        self.assertEqual(len(self.decisions(100)), decided)
        self.close(d, f'/devloop finding {second} resolved corretto in x.py')
        self.assertEqual([c['key'] for c in self.closures()], [KEY, second])
        self.assertEqual(self.gh.labels[100], {REVIEW})

    def test_a_finding_of_the_trust_policy_is_refused(self):
        d = self.make()
        held = first_round(verdict='needs_operator', reason='trust_policy')
        self.escalated(held, reviewed(REV, raw_review('approve'), review.open_findings([held]),
                                      round_no=2))
        self.refused(d, f'/devloop finding {KEY} resolved {REASON}',
                     f'un reviewer ha marcato `trust_policy` `{KEY}`')

    def test_a_request_for_changes_still_standing_is_refused(self):
        d = self.make()
        first = first_round()
        self.escalated(first, reviewed(REV, raw_review('request_changes', [finding('mine')]),
                                       review.open_findings([first]), round_no=2))
        self.refused(d, f'/devloop finding {KEY} resolved {REASON}',
                     f'`{REV}` non approva il head e chiede ancora modifiche')
        # Nor on an approval of an older head.
        d = self.make()
        self.escalated(first, reviewed(REV, raw_review('approve'), review.open_findings([first]),
                                       round_no=2))
        self.gh.head = SHA2
        self.refused(d, f'/devloop finding {KEY} resolved {REASON}',
                     'l\'ultimo round di review non è del head della PR')

    def test_a_command_from_another_identity_is_refused(self):
        first = first_round()
        approval = reviewed(REV, raw_review('approve'), review.open_findings([first]), round_no=2)
        for login in (SUPERVISOR_LOGIN, 'brushtyler', 'someone-else'):
            with self.subTest(login=login):
                d = self.make()
                self.escalated(first, approval)
                # Not a command of the escalation: nothing applies, nothing is closed.
                self.assertEqual(self.close(d, f'/devloop finding {KEY} resolved {REASON}',
                                            login=login), [])
                self.assertEqual(self.decisions(100), [])
                self.assertEqual(self.closures(), [])
                self.assertEqual(self.gh.labels[100], {HUMAN})
        # The coordinator's app with [roles.coordinator] off is any other identity.
        d = self.make(declared(CFG))
        self.escalated(first, approval)
        self.assertEqual(self.close(d, f'/devloop finding {KEY} resolved {REASON}'), [])
        self.assertEqual(self.closures(), [])
        # Back in review, its line is none of the operator's closures either.
        self.gh.labels[100] = {REVIEW}
        (found,) = [p for p in self.make(CO).snapshot().prs if p.number == 100]
        self.assertEqual(found.commands, [])

    def test_a_closure_without_a_reason_is_refused(self):
        first = first_round()
        approval = reviewed(REV, raw_review('approve'), review.open_findings([first]), round_no=2)
        for text in (f'/devloop finding {KEY} resolved', f'/devloop finding {KEY}',
                     '/devloop finding', f'/devloop finding {KEY} fixed {REASON}',
                     f'/devloop finding {KEY} resolved {REASON}\n'
                     f'/devloop finding {KEY} resolved'):
            with self.subTest(text=text):
                d = self.make()
                self.escalated(first, approval)
                self.refused(d, text, 'il motivo è obbligatorio')

    def test_on_another_escalation_it_is_refused(self):
        d = self.make()
        first = first_round()
        for record in (first, reviewed(REV, raw_review('approve'), review.open_findings([first]),
                                       round_no=2)):
            self.record(record)
        self.gh.labels[100] = {HUMAN}
        self.gh.say(100, 'brushtyler', markdown.render_escalation('x', cls='round_limit'))
        self.refused(d, f'/devloop finding {KEY} resolved {REASON}',
                     'vale solo su un\'escalation `open_findings` di una PR')

    def test_on_an_escalation_to_the_supervisor_it_is_refused(self):
        d = self.make(coordinated(AUTO))
        self.gh.labels[100] = {SUPER}
        self.gh.say(100, 'brushtyler', markdown.publishable(
            markdown.dispatcher_header() + '\nx', 'notice',
            {'event': 'escalation', 'class': 'tool_failure', 'to': 'supervisor',
             'step': 'review', 'key': {'step': 'review', 'head': SHA1}, 'resume': REVIEW}))
        self.assertEqual(self.close(d, f'/devloop finding {KEY} resolved {REASON}'),
                         [('supervise', 100)])
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('vale solo su un\'escalation `open_findings` all\'operatore', self.trace())
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual(self.runner.calls, [])  # never a retry


class SummaryTests(unittest.TestCase):
    def test_the_daily_summary_lists_each_closure_with_its_reason(self):
        gh = ReportGH()
        dispatcher = TRUST.writers['dispatcher']
        entry = {'key': KEY, 'status': 'withdrawn', 'by': 'coordinator', 'author': COORDINATOR,
                 'reason': REASON + '\nseconda riga', 'head_sha': SHA1}
        gh.add(12, dispatcher, markdown.render_supervision(
            {'command': 'finding', 'by': COORDINATOR, 'reason': 'x', 'comment': 1,
             'outcome': 'closed', 'class': 'open_findings'}, 't',
            records=[('finding', {'closed': [entry]})]), '2026-11-24T10:00:00Z')
        # A refused command closes nothing, and the summary does not list it here.
        gh.add(12, dispatcher, markdown.render_supervision(
            {'command': 'finding', 'by': COORDINATOR, 'reason': 'x', 'comment': 2,
             'outcome': 'refused'}, 't'), '2026-11-24T11:00:00Z')
        day = report.parse_day('2026-11-24')
        s = report.Records(gh, TRUST, day, day, CFG.agents, 'Italian').summary(day)
        self.assertEqual(s.closures, [('10:00', 12, KEY, 'withdrawn', COORDINATOR, REASON)])
        text = report.render(s)
        self.assertIn('### Finding chiusi dal coordinatore', text)
        self.assertIn(f'| 10:00 | #12 | `{KEY}` | ritirato | `{COORDINATOR}` | {REASON} |', text)


if __name__ == '__main__':
    unittest.main()
