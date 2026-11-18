"""devloop: one dispatcher register per fix (#355). The turns of a fix, the merge of the base made
before it (``sync``) and its local tests (``verification``) are sections of one comment, written
with the first turn ``open`` before the CLI starts; the records written one per comment before
stay readable."""

import unittest

from devloop import ledger, markdown, report
from devloop.ledger import Ledger
from devloop.dispatcher import Dispatcher
from devloop.planning import Snapshot
from test_devloop import CFG, IMPL, SHA1, SHA2, SHA3, finding, fixed, norm, pr, raw_review
from test_devloop_ledger import LedgerGH, ReportingRunner, TreeWS
from test_devloop_report import DISPATCHER, TRUST, ReportGH


class Crash(Exception):
    """The dispatcher killed while a CLI runs."""


def changes_requested():
    return pr(labels=['agent:changes'],
              reviews=[norm(raw_review('request_changes', [finding()]))])


def sections(comment):
    return markdown.read_marker(comment['body'], 'ledger')['records']


class FixRegisterDispatcherTests(unittest.TestCase):
    def make(self, *outputs, ws):
        self.gh, self.ws, self.logs = LedgerGH(), ws, []
        self.runner = ReportingRunner(*outputs, gh=self.gh, number=100)
        return Dispatcher(CFG, self.gh, self.ws, self.runner, log=self.logs.append,
                          ledger=Ledger(self.gh))

    def test_a_fix_with_sync_and_local_tests_writes_one_register_and_the_fix_comment(self):
        # Behind the base without conflicts: the dispatcher merges it before the fix; the
        # first attempt's tests fail, the second's pass.
        ws = TreeWS(tests=(False, True))
        ws.behind, ws.conflicts = True, ()
        d = self.make(fixed(), fixed(), ws=ws)
        d._do_fix(changes_requested())
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        # Two elements in the timeline: the dispatcher's register and the implementer's fix.
        register, fix = self.gh.thread[100]
        self.assertIsNotNone(markdown.read_marker(fix['body'], 'fix'))
        self.assertIsNone(markdown.read_marker(fix['body'], 'ledger'))
        record = markdown.read_marker(register['body'], 'ledger')
        self.assertEqual((record['type'], record['round']), ('fix', 1))
        self.assertEqual([(s['section'], s['type']) for s in record['records']],
                         [(1, 'turn'), (2, 'verification'), (3, 'turn'), (4, 'verification'),
                          (5, 'event')])
        body = register['body']
        self.assertTrue(body.startswith('> [!NOTE]\n> **Bot:** devloop dispatcher'))
        self.assertIn('## Registro devloop: correzione del round 1', body)
        self.assertIn(f'### 1. Turno `fix` di `{IMPL}`', body)
        self.assertIn('### 2. Verifica locale', body)
        self.assertIn('### 5. Evento `sync`', body)
        # Every record is valid and read as a record of its own was.
        found = ledger.collect(self.gh.thread[100], lambda login: True)
        self.assertEqual([(t['kind'], t['agent'], t['state'], t['outcome'])
                          for t in found['turns']],
                         [('fix', IMPL, 'closed', 'success')] * 2)
        self.assertEqual([(v['kind'], v['attempt'], v['outcome'], v['commit'])
                          for v in found['verifications']],
                         [('fix', 1, 'fail', None), ('fix', 2, 'pass', SHA2)])
        self.assertEqual([(e['event'], e['base'], e['head'], e['previous_head'])
                          for e in found['events']], [('sync', SHA3, SHA2, SHA1)])
        self.assertTrue(all(r['register'] == ledger.FIX
                            for _, r in ledger.entries(self.gh.thread[100], lambda login: True)))
        # Each turn was in the register, open, before its CLI started.
        self.assertEqual([[t['state'] for t in seen] for seen in self.runner.open_at_start],
                         [['open'], ['open']])
        self.assertEqual(d.ledger.pending(), [])

    def test_a_fix_without_sync_has_its_turn_and_test_in_one_register(self):
        d = self.make(fixed(), ws=TreeWS())
        d._do_fix(changes_requested())
        register, _ = self.gh.thread[100]
        self.assertEqual([s['type'] for s in sections(register)], ['turn', 'verification'])

    def test_an_interrupted_fix_leaves_its_turn_open_in_the_register(self):
        d = self.make(fixed(), Crash('dispatcher killed'), ws=TreeWS(tests=(False,)))
        p = changes_requested()
        with self.assertRaises(Crash):
            d._do_fix(p)
        (register,) = self.gh.thread[100]
        self.assertEqual([(s['type'], s.get('state') or s['outcome']) for s in sections(register)],
                         [('turn', 'closed'), ('verification', 'fail'), ('turn', 'open')])
        self.assertIn('Turno in corso', register['body'])
        self.assertEqual(d.ledger.pending(), [100])
        (left,) = ledger.open_turns(self.gh.thread[100], d._signed)
        self.assertEqual((left[0], left[1]['kind'], left[1]['section'], left[1]['register']),
                         (register['id'], 'fix', 3, ledger.FIX))
        # The next tick marks only that section incomplete, in the same comment.
        d._flag_incomplete(Snapshot(prs=[p], ready_issues=[], working_issues=[]), dry_run=False)
        self.assertTrue(any('incomplete turn on #100: fix' in m for m in self.logs))
        (register,) = self.gh.thread[100]
        self.assertEqual([(s['type'], s.get('state') or s['outcome']) for s in sections(register)],
                         [('turn', 'closed'), ('verification', 'fail'), ('turn', 'incomplete')])
        self.assertIn('Turno incompleto', register['body'])
        self.assertEqual(d.ledger.pending(), [])


class FixRegisterLedgerTests(unittest.TestCase):
    def open(self, lg, kind='fix', **where):
        return lg.open_turn(100, kind=kind, agent='claude', identity=None, mode='write',
                            model='m', effort='high', fallback_for=None, label='l', **where)

    def test_a_requested_fix_has_a_register_of_round_zero(self):
        gh = LedgerGH()
        lg = Ledger(gh)
        fix = lg.fix_register(100, 0)
        lg.close_turn(self.open(lg, register=fix), 'success')
        (only,) = gh.thread[100]
        self.assertEqual(fix.comment_id, only['id'])
        self.assertEqual(markdown.read_marker(only['body'], 'ledger')['round'], 0)
        self.assertIn('## Registro devloop: correzione prima di ogni review', only['body'])

    def test_each_fix_has_its_own_register_apart_from_the_rounds(self):
        gh = LedgerGH()
        lg = Ledger(gh)
        first = lg.fix_register(100, 1)
        lg.close_turn(self.open(lg, register=first), 'success')
        # A review of round 1 does not join the fix register, which answers the same round.
        lg.close_turn(self.open(lg, kind='review', round_no=1), 'success')
        tested = lg.verification(100, kind='fix', attempt=1, command='t', passed=True,
                                 duration_ms=1, tree_before='t1', tree_after='t1', register=first)
        self.assertTrue(lg.bind_commit(tested, SHA2, 't1'))
        second = lg.fix_register(100, 2)
        lg.close_turn(self.open(lg, register=second), 'success')
        kinds = [markdown.read_marker(c['body'], 'ledger')['type'] for c in gh.thread[100]]
        self.assertEqual(kinds, ['fix', 'round', 'fix'])
        self.assertEqual([(s['type'], s.get('commit')) for s in sections(gh.thread[100][0])],
                         [('turn', None), ('verification', SHA2)])

    def test_a_fix_register_is_validated_like_a_round_register(self):
        turn = {'type': 'turn', 'state': 'open', 'kind': 'fix', 'agent': 'a', 'started_at': 'x',
                'ended_at': None, 'outcome': None, 'telemetry': None}
        check = {'type': 'verification', 'command': 't', 'outcome': 'pass'}
        valid = markdown._valid_ledger
        self.assertTrue(valid({'type': 'fix', 'round': 0, 'records': [
            dict(turn, section=1), dict(check, section=2)]}))
        for name, record in {
                'repeated section': {'type': 'fix', 'round': 1, 'records': [
                    dict(turn, section=1), dict(check, section=1)]},
                'a request in it': {'type': 'fix', 'round': 1, 'records': [
                    {'type': 'request', 'hash': 'h', 'comments': [], 'section': 1}]},
                'negative round': {'type': 'fix', 'round': -1, 'records': []},
                'a verification in a round register': {'type': 'round', 'round': 1, 'records': [
                    dict(check, section=1)]}}.items():
            with self.subTest(name):
                self.assertFalse(valid(record))


class OldFormTests(unittest.TestCase):
    """The records of a fix written before #355, one comment each."""

    def old_thread(self):
        gh = LedgerGH()
        lg = Ledger(gh)
        lg.close_turn(self.open(lg), 'success')
        lg.verification(100, kind='fix', attempt=1, command='t', passed=True, duration_ms=5,
                        tree_before='t1', tree_after='t1')
        lg.event(100, 'sync', base=SHA3, head=SHA2, previous_head=SHA1)
        self.open(lg)  # left open by a crash
        return gh, lg

    open = FixRegisterLedgerTests.open

    def test_records_of_their_own_are_still_read(self):
        gh, lg = self.old_thread()
        self.assertEqual([markdown.read_marker(c['body'], 'ledger')['type']
                          for c in gh.thread[100]], ['turn', 'verification', 'event', 'turn'])
        # A later fix writes a register; both forms are read together.
        lg.close_turn(self.open(lg, register=lg.fix_register(100, 2)), 'success')
        found = ledger.collect(gh.thread[100], lambda login: True)
        self.assertEqual([t['state'] for t in found['turns']], ['closed', 'open', 'closed'])
        self.assertEqual([v['outcome'] for v in found['verifications']], ['pass'])
        self.assertEqual([e['event'] for e in found['events']], ['sync'])
        # The old open turn is flagged in its own comment.
        (left,) = ledger.open_turns(gh.thread[100], lambda login: True)
        self.assertIsNone(left[1].get('register'))
        lg.flag_incomplete(100, *left)
        self.assertEqual(markdown.read_marker(gh.thread[100][3]['body'], 'ledger')['state'],
                         'incomplete')
        self.assertEqual(len(gh.thread[100]), 5)

    def test_the_daily_summary_reads_both_forms(self):
        day = '2026-11-16'
        gh = ReportGH()
        turn = {'type': 'turn', 'state': 'closed', 'kind': 'fix', 'agent': 'claude',
                'started_at': f'{day}T11:00:00Z', 'ended_at': f'{day}T11:01:00Z',
                'duration_ms': 60000, 'outcome': 'success', 'telemetry': {}}
        sync = {'type': 'event', 'event': 'sync', 'at': f'{day}T11:02:00Z', 'base': 'b',
                'head': 'h'}
        gh.add(12, DISPATCHER, ledger.render(turn), f'{day}T11:00:01Z')
        gh.add(12, DISPATCHER, ledger.render(sync), f'{day}T11:02:00Z')
        gh.add(14, DISPATCHER, ledger.render({'type': 'fix', 'round': 1, 'records': [
            dict(turn, section=1), dict(sync, section=2)]}), f'{day}T11:00:01Z')
        when = report.parse_day(day)
        s = report.Records(gh, TRUST, when, when, CFG.agents).summary(when)
        self.assertEqual(s.actions, [('11:00', 12, 'fix', 'claude', 'success', 60000),
                                     ('11:00', 14, 'fix', 'claude', 'success', 60000),
                                     ('11:02', 12, 'evento sync', None, None, None),
                                     ('11:02', 14, 'evento sync', None, None, None)])
        self.assertEqual(s.rounds, 0)  # a fix register opens no review round


if __name__ == '__main__':
    unittest.main()
