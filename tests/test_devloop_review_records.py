"""devloop: fewer items per review round (#325).

An agent with an app of its own publishes its review as one native review, whose body is the
record devloop reads; the review comments published before stay records. The review turns of a
round and its CI event share one register of the dispatcher, written under a lock, each update
on the register read again right before.
"""

import threading
import time
import unittest

from devloop import ledger, markdown
from devloop.dispatcher import with_native_reviews
from devloop.github import GitHubError
from devloop.ledger import Ledger
from devloop.planning import Action, counted_rounds
from devloop.review import finding_state, open_findings
from helpers.devloop_fakes import FakeIdentities, at
from test_devloop import CFG, IMPL, REV, SHA1, finding, norm, raw_review
from test_devloop_ledger import GREEN, LedgerGH, ledger_records
from test_devloop_parallel import PCFG, CopyWS, SleepingRunner
from test_devloop_quorum import CORE, FIRST, SECOND, Identities, Quorum, QuorumGH

LEVEL1 = [{'path': 'x.py', 'links': []}]  # a diff of level 1: one reviewer, REV
LOGINS = FakeIdentities.LOGINS


def native(body, login, state='CHANGES_REQUESTED', submitted=1, review_id=1):
    """A native review as ``pr_reviews`` returns it."""
    return {'id': review_id, 'login': login, 'state': state, 'body': body,
            'submitted_at': at(submitted)}


def comment(body, login, created):
    return {'author': {'login': login}, 'body': body, 'created_at': at(created)}


def fix_body(round_no, finding_id):
    return markdown.render_fix({'summary': 's', 'head_sha': SHA1, 'responses': [
        {'finding_id': finding_id, 'action': 'fixed', 'note': 'done'}]}, 'h', round_no)


class NativeReviewTests(Quorum):
    """Criterion 1 and 3: one native review with the whole review and its marker, read back
    with the identity checks; its submission retried, then escalated."""

    def test_the_review_is_one_native_review_that_devloop_reads(self):
        self.make(raw_review('request_changes', [finding('a')]), diff=LEVEL1)
        self.assertEqual(self.d.tick(), [Action('review', 100)])
        ((number, sent),) = self.gh.native
        self.assertEqual((number, sent['login'], sent['event'], sent['commit']),
                         (100, LOGINS[REV], 'REQUEST_CHANGES', SHA1))
        # The whole review, readable and with its marker, in the body of the native review.
        self.assertIn('## Review automatica, round 1/', sent['body'])
        self.assertIn('### Bloccanti', sent['body'])
        record = markdown.read_marker(sent['body'], 'review')
        self.assertEqual([f['title'] for f in record['blocking_findings']], ['a'])
        # No review comment at all: the register and the dispatcher's records are the rest.
        self.assertEqual([b for _, b in self.gh.comments
                          if markdown.read_marker(b, 'review') is not None], [])
        # The next tick reads verdict and findings from the native review.
        pr = self.d.snapshot().prs[0]
        self.assertEqual(pr.reviews, [record])
        self.assertEqual([f['key'] for f in open_findings(pr.reviews, pr.closures)],
                         [f'1/{REV}/R1-1'])
        self.assertEqual(self.gh.labels[100], {'agent:changes'})

    def test_a_native_record_counts_only_from_the_named_reviewers_app(self):
        self.make(raw_review('request_changes', [finding('a')]), diff=LEVEL1)
        self.d.tick()
        ((_, sent),) = self.gh.native
        forged = [
            native(sent['body'], LOGINS[IMPL], review_id=2),       # the implementer's app
            native(sent['body'], LOGINS[FIRST if FIRST != REV else SECOND],
                   review_id=3),                                  # another agent's app
            native(sent['body'], 'brushtyler', review_id=4),      # a trusted user
            native(sent['body'], LOGINS[REV], state='PENDING', review_id=5),  # not published
        ]
        self.gh.native_reviews = forged
        self.assertEqual(len(self.d.snapshot().prs[0].reviews), 1)
        # Without the reviewer's own one, none of them is a record.
        self.gh.native = []
        self.assertEqual(self.d.snapshot().prs[0].reviews, [])

    def test_an_agent_without_an_app_keeps_the_review_comment(self):
        class NoApp(Identities):
            def login(self, actor):
                return None if actor == REV else super().login(actor)

        d = self.make(raw_review(), diff=LEVEL1)
        d.identities = NoApp(self.gh)
        p = d.snapshot().prs[0]
        d._do_review(p)
        self.assertEqual(self.gh.native, [])
        (body,) = [b for _, b in self.gh.comments if markdown.read_marker(b, 'review')]
        # The same record as a native review is not read for an agent without an app; as a
        # comment of a trusted user it is, as before.
        record = markdown.read_marker(body, 'review')
        thread = with_native_reviews([], [native(body, 'brushtyler', 'APPROVED')])
        self.assertEqual(d._markers(p, thread, 'review'), [])
        self.assertEqual(d._markers(p, [comment(body, 'brushtyler', 1)], 'review'), [record])

    def test_a_failed_native_review_is_retried_and_may_succeed(self):
        self.make(raw_review(), diff=LEVEL1)
        self.d.review_retry_pause = 0
        failures = [GitHubError('HTTP 502')]
        submit = self.gh.submit_review

        def flaky(number, **kw):
            if failures:
                raise failures.pop()
            return submit(number, **kw)
        self.gh.submit_review = flaky
        self.d.tick()
        self.assertEqual(len(self.gh.native), 1)
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        self.assertTrue(any('attempt 1 of 3' in line for line in self.logs))

    def test_a_review_published_despite_the_error_is_not_submitted_again(self):
        self.make(raw_review(), diff=LEVEL1)
        self.d.review_retry_pause = 0
        submit = self.gh.submit_review

        def lost_answer(number, **kw):
            submit(number, **kw)  # GitHub published it, the answer was lost
            raise GitHubError('timed out')
        self.gh.submit_review = lost_answer
        self.d.tick()
        self.assertEqual(len(self.gh.native), 1)
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})

    def test_a_native_review_that_keeps_failing_escalates(self):
        self.make(raw_review(), diff=LEVEL1)
        self.d.review_retry_pause = 0
        calls = []

        def refuse(number, **kw):
            calls.append(number)
            raise GitHubError('HTTP 502')
        self.gh.submit_review = refuse
        self.d.tick()
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        notice = markdown.read_marker(self.gh.comments[-1][1], 'notice')
        self.assertEqual(notice['event'], 'escalation')
        self.assertIn('failed 3 times', self.gh.comments[-1][1])
        # The verdict that was never published is no record: the round has none.
        self.assertEqual(self.d.snapshot().prs[0].reviews, [])


class TruncationTests(Quorum):
    """The limit of 65 536 characters: only the readable part is cut, with a notice; a record
    that does not fit even so is not published and the step escalates."""

    def test_only_the_readable_part_is_cut(self):
        record = norm(raw_review('request_changes', [finding('a')]))
        # The readable text and the marker each fit, not both together.
        record['summary'] = 'x' * 30000
        self.assertGreater(len(markdown.publishable('h\n' + record['summary'], 'review', record)),
                           markdown.MAX_BODY)
        body = markdown.render_review(record, '> [!NOTE]\n> **Agent:** A\n')
        self.assertLessEqual(len(body), markdown.MAX_BODY)
        self.assertTrue(body.startswith('> [!NOTE]\n> **Agent:** A\n'))
        self.assertIn(markdown.TRUNCATED, body)
        self.assertEqual(markdown.read_marker(body, 'review'), record)  # findings intact
        # A body within the limit is not touched.
        short = norm(raw_review())
        self.assertNotIn(markdown.TRUNCATED, markdown.render_review(short, 'h'))

    def test_a_record_too_long_for_github_is_not_published(self):
        detail = finding('a')
        detail['detail'] = 'y' * 70000
        self.make(raw_review('request_changes', [detail]), diff=LEVEL1)
        self.d.tick()
        self.assertEqual(self.gh.native, [])
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        # Only the escalation: no review split over more comments, no finding dropped.
        self.assertEqual([b for _, b in self.gh.comments
                          if markdown.read_marker(b, 'notice') is None
                          and markdown.read_marker(b, 'ledger') is None], [])
        self.assertIn('does not fit', self.gh.comments[-1][1])


class MixedRoundsTests(Quorum):
    """Criterion 2: the review comments published before #325 stay records, and a PR with
    rounds of both forms counts rounds and findings as if all were comments."""

    def records(self):
        r1 = norm(raw_review('request_changes', [finding('a')]))
        r2 = norm(raw_review('request_changes', [finding('b')],
                             previous=[{'id': 'R1-1', 'status': 'resolved', 'note': 'ok'}]),
                  previous_open=open_findings([r1]), round_no=2)
        r3 = norm(raw_review(previous=[{'id': 'R2-1', 'status': 'resolved', 'note': 'ok'}]),
                  previous_open=open_findings([r1, r2]), round_no=3)
        return [markdown.render_review(r, 'h') for r in (r1, r2, r3)]

    def snapshot(self, comments, natives):
        self.make(diff=LEVEL1)
        self.gh.earlier = comments
        self.gh.native_reviews = natives
        return self.d.snapshot().prs[0]

    def test_mixed_rounds_count_as_before(self):
        b1, b2, b3 = self.records()
        rev, impl = LOGINS[REV], LOGINS[IMPL]
        rereview = comment('/devloop rereview\n\nAncora una volta.', 'brushtyler', 12)
        fixes = [comment(fix_body(1, 'R1-1'), impl, 4), comment(fix_body(2, 'R2-1'), impl, 8)]
        before = self.snapshot([comment(b1, rev, 2), fixes[0], comment(b2, rev, 6), fixes[1],
                                comment(b3, rev, 10), rereview], [])
        # Round 1 as a comment, as published before #325; rounds 2 and 3 as native reviews.
        mixed = self.snapshot([comment(b1, rev, 2), *fixes, rereview],
                              [native(b2, rev, submitted=6, review_id=1),
                               native(b3, rev, 'APPROVED', submitted=10, review_id=2)])
        self.assertEqual([r['round'] for r in mixed.reviews], [1, 2, 3])
        self.assertEqual(mixed.reviews, before.reviews)
        self.assertEqual((counted_rounds(mixed.reviews), counted_rounds(before.reviews)), (3, 3))
        for pr in (before, mixed):
            state = finding_state(pr.reviews, pr.closures)
            self.assertEqual((state.open, sorted(state.known), state.problems),
                             ([], [f'1/{REV}/R1-1', f'2/{REV}/R2-1'], []))
            self.assertEqual(len(pr.fixes), 2)
            # The rereview written after the latest native review comes after it.
            self.assertTrue(pr.rereview)

    def test_a_native_review_takes_its_place_among_the_comments(self):
        body = markdown.render_review(norm(raw_review()), 'h')
        comments = [comment('a', 'x', 1), comment('b', 'x', 3), {'author': {}, 'body': 'old'}]
        thread = with_native_reviews(comments, [
            native(body, 'r', submitted=3, review_id=7),          # same time: before 'b'
            native('no marker', 'r', submitted=2, review_id=8),   # not a record: left out
            native(body, 'r', state='PENDING', submitted=0, review_id=9)])
        self.assertEqual([c['body'] if not c.get('native') else c['review_id'] for c in thread],
                         ['a', 7, 'b', 'old'])
        self.assertEqual(thread[1]['author'], {'login': 'r'})
        self.assertIsNone(thread[1]['id'])


class RegisterGH(QuorumGH):
    """QuorumGH whose comments have ids and can be edited, as the REST API allows. Each read
    and each edit lets other threads run (``pause``): two writers that did not take turns on
    the register would lose an update."""

    pause = 0.02

    def __init__(self):
        super().__init__()
        self.details = [dict(c) for c in GREEN]

    def comment(self, number, body):
        super().comment(number, body)
        return len(self.comments)

    def edit_comment(self, comment_id, body):
        time.sleep(self.pause)
        number, _ = self.comments[comment_id - 1]
        self.comments[comment_id - 1] = (number, body)

    def pr_comments(self, number):
        found = [{'id': i + 1, 'author': {'login': who}, 'body': body, 'created_at': at(2 * i)}
                 for i, ((n, body), who) in enumerate(zip(self.comments, self.written))
                 if n == number]
        time.sleep(self.pause)
        return found

    issue_comments = pr_comments


class OpenAtStart(SleepingRunner):
    """SleepingRunner that remembers, as each CLI starts, the turns open in the register."""

    def __init__(self, gh, outputs=None):
        super().__init__(outputs, delay=0.2)
        self.gh = gh
        self.open_at_start = {}

    def run(self, agent, mode, prompt, schema_name, workdir, label):
        found = [r for r in ledger_records(self.gh.pr_comments(100), 'turn')
                 if r['state'] == 'open']
        with self._lock:
            self.open_at_start[agent.key] = [r['agent'] for r in found]
        return super().run(agent, mode, prompt, schema_name, workdir, label)


class RegisterTests(Quorum):
    """Criterion 4: one register per round, with every turn and the CI event, no update lost,
    and an interrupted turn left open."""

    def make_register(self, cfg=PCFG, outputs=None):
        self.gh = RegisterGH()
        self.gh.labels[100] = {'agent:review'}
        self.ws = CopyWS()
        self.ws.diff = list(CORE)
        self.runner = OpenAtStart(self.gh, outputs)
        self.logs = []
        from devloop.dispatcher import Dispatcher
        self.d = Dispatcher(cfg, self.gh, self.ws, self.runner, log=self.logs.append,
                            identities=Identities(self.gh))
        self.gh.meta = {'issue': 7, 'implementer': IMPL, 'reviewer': cfg.reviewer,
                        'profile': self.d._profile(IMPL, 'write')}
        self.d.ledger = Ledger(self.gh)
        return self.d

    def registers(self):
        return [(i + 1, r) for i, (_, body) in enumerate(self.gh.comments)
                if (r := markdown.read_marker(body, 'ledger')) is not None]

    def test_two_reviewers_in_parallel_write_one_register(self):
        self.make_register()
        self.assertEqual(self.d.tick(), [Action('review', 100)])
        self.assertEqual(sorted(self.reviewed()), sorted([FIRST, SECOND]))
        ((_, register),) = self.registers()  # the only ledger comment of the PR
        self.assertEqual((register['type'], register['round']), ('round', 1))
        turns = [s for s in register['records'] if s['type'] == 'turn']
        self.assertEqual(sorted(t['agent'] for t in turns), sorted([FIRST, SECOND]))
        self.assertEqual({(t['kind'], t['state'], t['outcome']) for t in turns},
                         {('review', 'closed', 'success')})
        (event,) = [s for s in register['records'] if s['type'] == 'event']
        self.assertEqual((event['event'], event['round'], event['state']), ('ci_round', 1, 'pass'))
        self.assertEqual(sorted(s['section'] for s in register['records']), [1, 2, 3])
        # Each turn was in the register, open, before its CLI started.
        for key in (FIRST, SECOND):
            self.assertIn(key, self.runner.open_at_start[key])
        # The round's items: two native reviews and one register.
        self.assertEqual(len(self.gh.native), 2)
        self.assertEqual([b for _, b in self.gh.comments
                          if markdown.read_marker(b, 'review') is not None], [])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        body = self.gh.comments[0][1]
        self.assertIn('## Registro devloop: round 1', body)
        self.assertIn(f'Turno `review` di `{FIRST}`', body)
        self.assertIn('Evento `ci_round`', body)

    def test_reviewers_of_one_round_in_later_actions_share_its_register(self):
        self.make_register(cfg=CFG)
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.reviewed(), [FIRST, SECOND])
        ((_, register),) = self.registers()
        self.assertEqual([(s['type'], s.get('agent') or s.get('event'))
                          for s in register['records']],
                         [('turn', FIRST), ('event', 'ci_round'), ('turn', SECOND)])

    def pending_ci(self):
        """The CI of the head runs."""
        self.gh.ci = 'pending'
        self.gh.details = [dict(c, bucket='pending', conclusion=None, duration_s=None)
                           for c in GREEN]

    def fail_ci(self):
        """The CI of the head, until now pending, fails."""
        self.gh.ci = 'fail'
        self.gh.details = LedgerGH(ci='fail').check_details(SHA1)

    def round_items(self):
        ((_, register),) = self.registers()
        return [(s['type'], s.get('agent') or s.get('event'), s.get('state'))
                for s in register['records']]

    def test_a_ci_failure_after_a_request_for_changes_joins_its_round_and_register(self):
        # One head, one round (#352): the review asked for changes with the CI pending, the CI
        # then failed. The failure is a ci finding of round 1, its event in round 1's register.
        self.make_register(cfg=CFG, outputs={REV: [raw_review('request_changes',
                                                              [finding('a')])]})
        self.ws.diff = list(LEVEL1)
        self.pending_ci()
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        self.assertEqual(self.d.tick(), [])  # the fix waits for the CI of the head
        self.fail_ci()
        self.assertEqual(self.d.tick(), [Action('ci_failed', 100, 'CI failed after the review: '
                                                'it joins its round')])
        self.assertEqual([(r['source'], r['round']) for r in self.records('review')],
                         [('agent', 1), ('ci', 1)])
        self.assertEqual(self.round_items(), [('turn', REV, 'closed'),
                                              ('event', 'ci_round', 'fail')])
        ((_, register),) = self.registers()
        (event,) = [s for s in register['records'] if s['type'] == 'event']
        self.assertNotIn('voided_approval', event)  # no approval to void (#359)
        snap = self.d.snapshot()
        self.assertEqual(counted_rounds(snap.prs[0].reviews), 1)
        self.assertEqual(self.planned(), [Action('fix', 100)])

    def test_a_ci_failure_after_an_approval_joins_its_round_and_register(self):
        # One head, one round (#359): the review approved with the CI pending, the CI then
        # failed. The failure is a ci finding of round 1, its event in round 1's register
        # says that it voided the approval, and the fix follows without a new round.
        self.make_register(cfg=CFG, outputs={REV: [raw_review()]})
        self.ws.diff = list(LEVEL1)
        self.pending_ci()
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.assertEqual(self.d.tick(), [])  # the approval waits for the CI of the head
        self.fail_ci()
        self.assertEqual(self.d.tick(), [Action('ci_failed', 100, 'CI failed after the '
                                                'approval: it joins its round')])
        self.assertEqual([(r['source'], r['round'], r['verdict'])
                          for r in self.records('review')],
                         [('agent', 1, 'approve'), ('ci', 1, 'request_changes')])
        self.assertEqual(self.round_items(), [('turn', REV, 'closed'),
                                              ('event', 'ci_round', 'fail')])
        ((_, register),) = self.registers()
        (event,) = [s for s in register['records'] if s['type'] == 'event']
        self.assertIs(event['voided_approval'], True)
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        snap = self.d.snapshot()
        self.assertEqual(counted_rounds(snap.prs[0].reviews), 1)
        self.assertEqual(self.planned(), [Action('fix', 100)])

    def test_a_ci_failure_joins_a_round_that_only_approved_so_far(self):
        # Level 2 (#359): the first reviewer approved with the CI pending, the CI failed before
        # the second reviewed. The failure joins the round, which waits for the second; the
        # approval decays and the fix follows, without a new round.
        self.make_register(cfg=CFG, outputs={FIRST: [raw_review()], SECOND: [raw_review()]})
        self.pending_ci()
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.fail_ci()
        self.assertEqual(self.planned(), [Action('ci_failed', 100, 'CI failed during the '
                                                 'review: it joins its round')])
        self.d.tick()
        self.assertIn(f'  CI failure joins round 1, which waits for {SECOND}', self.logs)
        self.d.tick()
        self.assertEqual(self.reviewed(), [FIRST, SECOND])
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        self.assertEqual([(r['reviewer'], r['round']) for r in self.records('review')],
                         [(FIRST, 1), ('ci', 1), (SECOND, 1)])
        snap = self.d.snapshot()
        self.assertEqual(counted_rounds(snap.prs[0].reviews), 1)
        self.assertEqual(self.planned(), [Action('fix', 100)])

    def test_a_ci_failure_joins_a_round_still_waiting_for_a_reviewer(self):
        # Level 2: the first reviewer asked for changes with the CI pending, the CI failed
        # before the second reviewed. The failure joins the round, which waits for the second;
        # then one fix answers the reviewers' findings and the CI's.
        self.make_register(cfg=CFG, outputs={FIRST: [raw_review('request_changes',
                                                                [finding('a')])],
                                             SECOND: [raw_review()]})
        self.pending_ci()
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.fail_ci()
        self.assertEqual(self.planned(), [Action('ci_failed', 100, 'CI failed during the '
                                                 'review: it joins its round')])
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.assertIn(f'  CI failure joins round 1, which waits for {SECOND}', self.logs)
        self.assertEqual(self.planned(), [Action('review', 100,
                                                 f'the round waits for {SECOND}')])
        self.d.tick()
        self.assertEqual(self.reviewed(), [FIRST, SECOND])
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        self.assertEqual([(r['reviewer'], r['round']) for r in self.records('review')],
                         [(FIRST, 1), ('ci', 1), (SECOND, 1)])
        self.assertEqual(self.round_items(), [('turn', FIRST, 'closed'),
                                              ('event', 'ci_round', 'fail'),
                                              ('turn', SECOND, 'closed')])
        snap = self.d.snapshot()
        self.assertEqual(counted_rounds(snap.prs[0].reviews), 1)
        self.assertEqual(sorted(f['key'] for f in open_findings(snap.prs[0].reviews)),
                         sorted([f'1/{FIRST}/R1-1', '1/ci/R1-1']))
        self.assertEqual(self.planned(), [Action('fix', 100)])

    def test_an_interrupted_turn_stays_open_in_the_register(self):
        # The process dies during the turn: an Exception would close it as a failure (#381).
        class Killed(BaseException):
            pass
        self.make_register(outputs={SECOND: [Killed('dispatcher killed')]})
        self.d.tick()
        ((comment_id, register),) = self.registers()
        states = {s['agent']: s['state'] for s in register['records'] if s['type'] == 'turn'}
        self.assertEqual(states, {FIRST: 'closed', SECOND: 'open'})
        # The next tick marks only that turn incomplete, in the same register.
        (left,) = ledger.open_turns(self.gh.pr_comments(100), self.d._signed)
        self.assertEqual((left[0], left[1]['agent'], left[1]['register']),
                         (comment_id, SECOND, 1))
        self.d.ledger.flag_incomplete(100, *left)
        ((_, register),) = self.registers()
        states = {s['agent']: s['state'] for s in register['records'] if s['type'] == 'turn'}
        self.assertEqual(states, {FIRST: 'closed', SECOND: 'incomplete'})


class SlowLedgerGH(LedgerGH):
    """LedgerGH whose reads and edits let other threads run in between."""

    def issue_comments(self, number):
        found = super().issue_comments(number)
        time.sleep(0.01)
        return found

    pr_comments = issue_comments

    def edit_comment(self, comment_id, body):
        time.sleep(0.01)
        super().edit_comment(comment_id, body)


class RegisterLedgerTests(unittest.TestCase):
    def open(self, lg, agent, round_no=1):
        return lg.open_turn(100, kind='review', agent=agent, identity=None, mode='review',
                            model='m', effort='high', fallback_for=None, label='l',
                            round_no=round_no)

    def test_parallel_writers_lose_no_update(self):
        gh = SlowLedgerGH()
        lg = Ledger(gh)
        start = threading.Barrier(6)

        def work(i):
            start.wait()
            lg.close_turn(self.open(lg, f'a{i}'), 'success')
            lg.event(100, 'ci_round', round_no=1, head=SHA1, state='pass', round=1, checks=[])

        threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        (only,) = gh.thread[100]
        register = markdown.read_marker(only['body'], 'ledger')
        turns = [s for s in register['records'] if s['type'] == 'turn']
        self.assertEqual(sorted(t['agent'] for t in turns), [f'a{i}' for i in range(6)])
        self.assertEqual({t['state'] for t in turns}, {'closed'})
        self.assertEqual(sorted(s['section'] for s in register['records']), list(range(1, 13)))
        self.assertEqual(lg.pending(), [])

    def test_each_round_has_its_own_register_and_other_records_stay_apart(self):
        gh = LedgerGH()
        lg = Ledger(gh)
        lg.close_turn(self.open(lg, 'a'), 'success')
        lg.close_turn(self.open(lg, 'b', round_no=2), 'success')
        lg.event(100, 'sync', base='b', head='h')
        kinds = [markdown.read_marker(c['body'], 'ledger')['type'] for c in gh.thread[100]]
        self.assertEqual(kinds, ['round', 'round', 'event'])
        self.assertEqual([r['agent'] for r in ledger_records(gh.thread[100], 'turn')],
                         ['a', 'b'])

    def test_a_register_of_another_identity_is_never_edited(self):
        gh = LedgerGH()
        forged = ledger.render({'type': 'round', 'round': 1, 'records': []})
        gh.thread[100] = [{'id': 1, 'author': {'login': 'stranger'}, 'body': forged}]
        lg = Ledger(gh, signed=lambda login: login == 'brushtyler')
        self.open(lg, 'a')
        self.assertEqual(gh.edited, [])
        self.assertEqual(gh.thread[100][0]['body'], forged)
        self.assertEqual(len(gh.thread[100]), 2)
        # Nor does it count when read.
        self.assertEqual([r['agent'] for _, r in ledger.entries(
            gh.thread[100], lambda login: login == 'brushtyler')], ['a'])

    def test_sections_must_be_distinct(self):
        turn = {'type': 'turn', 'state': 'open', 'kind': 'review', 'agent': 'a',
                'started_at': 'x', 'ended_at': None, 'outcome': None, 'telemetry': None}
        good = {'type': 'round', 'round': 1, 'records': [dict(turn, section=1),
                                                          dict(turn, section=2)]}
        self.assertIsNotNone(markdown.read_marker(markdown.marker('ledger', good), 'ledger'))
        for bad in (dict(good, records=[dict(turn, section=1)] * 2),
                    dict(good, records=[turn]),
                    dict(good, records=[{'type': 'round', 'round': 1, 'records': [],
                                         'section': 1}])):
            self.assertIsNone(markdown.read_marker(markdown.marker('ledger', bad), 'ledger'))


if __name__ == '__main__':
    unittest.main()
