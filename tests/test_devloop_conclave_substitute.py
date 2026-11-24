"""devloop: the coordinator's substitute vote in the conclave (#605).

ADR 0002, second precision of 2026-11-23, point 3: when a member of the conclave cannot vote, the
third vote is the coordinator's. With ``[roles.coordinator]`` a voter that fails does not close
the conclave: it waits, until ``conclave_substitute_timeout_seconds`` from the first
unavailability, for ``/devloop vote <conclave> <sha256> APPROVE|BLOCK|RESERVED <reason>`` from
the coordinator's identity, bound to the conclave's comment and to the hash of its proposal. One
substitute vote per conclave; the proposer is never substituted; the operator's veto still
applies."""

import dataclasses
import sys
import unittest
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from devloop import agents, conclave, config, markdown, report  # noqa: E402
from devloop.activity import iso  # noqa: E402
from devloop.config import AppConfig, ConfigError, CoordinatorActor  # noqa: E402
from helpers import ssdlc_project  # noqa: E402
from test_devloop_conclave import CCFG, LOGINS, RESOLUTION, Base, proposal, vote  # noqa: E402
from test_devloop_report import DISPATCHER as REPORT_DISPATCHER  # noqa: E402
from test_devloop_report import TRUST, ReportGH  # noqa: E402
from test_devloop_report import CFG as REPORT_CFG  # noqa: E402

SLUG = 'brushtyler-devloop-coordinator'
COORDINATOR = f'{SLUG}[bot]'
ACTOR = CoordinatorActor(name='claude-coordinator', kind='claude', provider='anthropic',
                         model='claude-opus-5-5')
COORDINATED = dataclasses.replace(
    CCFG, coordinator=ACTOR, apps=dict(CCFG.apps, coordinator=AppConfig(app_id=7777777,
                                                                        slug=SLUG)))
NOW = 1_800_000_000
TIMEOUT = COORDINATED.conclave_substitute_timeout_seconds
HEADER = '> [!NOTE]\n> **Agent:** Claude Code (CLI v9.9)\n> **Model:** m\n\n'
UNAVAILABLE = agents.AgentError('codex: usage limit reached')


class SubstituteBase(Base):
    cfg = COORDINATED

    def start(self, *outputs):
        """PR #100 blocked by claude's fix: the proposal, codex unavailable, then agy's vote
        when ``outputs`` has it. Returns the record after three ticks."""
        self.make(*outputs)
        self.now = NOW
        self.d.clock = lambda: self.now
        self.block_pr()
        self.run_conclave()
        return self.record()

    def conclave_id(self):
        (found,) = [c['id'] for c in self.gh.thread[100]
                    if markdown.read_marker(c['body'], 'conclave')]
        return found

    def vote_cmd(self, verdict='APPROVE', reason='La risoluzione resta nel perimetro.',
                 digest=None, conclave_id=None, login=COORDINATOR, written=None):
        """A ``/devloop vote`` comment by ``login``, written at ``written`` (default: now)."""
        body = (HEADER + f'/devloop vote {conclave_id or self.conclave_id()} '
                f'{digest or conclave.text_hash(RESOLUTION)} {verdict} {reason}\n')
        ident = self.gh.say(100, body, login)
        at = iso(self.now if written is None else written)
        self.gh.thread[100][-1].update(created_at=at, updated_at=at)
        return ident

    def due(self):
        return self.d._conclave_view(100, self.gh.pr_comments(100), self.target()) is not None


class WaitTests(SubstituteBase):
    def test_an_unavailable_voter_waits_for_the_substitute_vote_until_the_deadline(self):
        record = self.start(proposal(), UNAVAILABLE, vote())
        self.assertEqual(record['outcome'], '')
        self.assertEqual(record['votes'][0]['state'], conclave.WAITING)
        self.assertIn('usage limit', record['votes'][0]['missing'])
        self.assertEqual(record['votes'][1]['vote'], 'APPROVA')
        substitute = record['substitute']
        self.assertEqual((substitute['state'], substitute['member'], substitute['index']),
                         ('waiting', 'codex', 0))
        # The deadline is recorded from the first unavailability, so it survives a restart.
        self.assertEqual(substitute['deadline'], iso(NOW + 7200))
        self.assertEqual(substitute['conclave'], self.conclave_id())
        self.assertEqual(conclave.consistent(record), (True, ''))
        body = [c['body'] for c in self.gh.thread[100]
                if markdown.read_marker(c['body'], 'conclave')][0]
        self.assertIn(f'/devloop vote {self.conclave_id()} {record["hash"]}', body)
        # Nothing to do until the coordinator votes or the deadline passes.
        self.assertFalse(self.due())
        self.now = NOW + TIMEOUT - 1
        self.assertFalse(self.due())

    def test_without_a_vote_the_deadline_closes_it_as_today(self):
        self.start(proposal(), UNAVAILABLE, vote())
        self.now = NOW + TIMEOUT
        self.assertTrue(self.due())
        self.run_conclave(times=1)
        record = self.record()
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertEqual(record['substitute']['state'], 'expired')
        self.assertEqual(record['votes'][0]['state'], conclave.MISSING)
        self.assertFalse(self.due())

    def test_without_a_coordinator_a_failed_voter_is_missing_as_today(self):
        self.cfg = CCFG
        record = self.start(proposal(), UNAVAILABLE, vote())
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertNotIn('substitute', record)

    def test_the_proposer_is_never_substituted(self):
        record = self.start(agents.AgentError('claude crashed'))
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertNotIn('substitute', record)
        self.assertEqual(len(self.runner.calls), 1)


class VoteTests(SubstituteBase):
    def test_a_substitute_approve_with_the_other_approve_is_unanimous(self):
        self.start(proposal(), UNAVAILABLE, vote())
        ident = self.vote_cmd()
        self.assertTrue(self.due())
        self.now += 60
        self.run_conclave(times=1)
        record = self.record()
        self.assertEqual(record['outcome'], conclave.UNANIMOUS)
        substituted = record['votes'][0]
        self.assertEqual((substituted['state'], substituted['vote'], substituted['agent']),
                         ('done', 'APPROVA', 'codex'))
        self.assertEqual(substituted['proposal_hash'], record['hash'])
        # The record says it is a substitute vote, whom it replaces and the coordinator's model.
        substitute = record['substitute']
        self.assertEqual((substitute['state'], substitute['by'], substitute['comment'],
                          substitute['verdict'], substitute['at']),
                         ('voted', COORDINATOR, ident, 'APPROVE', iso(NOW + 60)))
        self.assertEqual((substituted['profile']['identity'], substituted['profile']['kind'],
                          substituted['profile']['model']),
                         (COORDINATOR, 'claude', 'claude-opus-5-5'))
        # Two Claude models voted: the record holds, the diversity is reduced and visible.
        self.assertEqual(conclave.consistent(record), (True, ''))
        body = [c['body'] for c in self.gh.thread[100]
                if markdown.read_marker(c['body'], 'conclave')][0]
        self.assertIn('voto sostitutivo `APPROVE` del coordinatore', body)
        self.assertIn('claude-opus-5-5', body)
        # The unanimous decision restarts the work, as any other (#142).
        view = self.d._conclave_view(100, self.gh.pr_comments(100), self.target())
        self.assertTrue(view['resume'])
        # No more calls of the agents: the coordinator's vote is not a turn.
        self.assertEqual(len(self.runner.calls), 3)

    def test_the_operator_veto_annuls_a_decision_taken_with_a_substitute_vote(self):
        self.start(proposal(), UNAVAILABLE, vote())
        self.vote_cmd()
        self.run_conclave(times=1)
        comment = self.conclave_id()
        decision = self.d._conclave_decision(7, [], self.gh.pr_comments(100))
        self.assertEqual((decision['comment'], decision['veto']), (comment, None))
        veto = self.gh.say(100, '/devloop veto due voti Claude non bastano', 'brushtyler')
        decision = self.d._conclave_decision(7, [], self.gh.pr_comments(100))
        self.assertEqual(decision['veto'], (veto, 'brushtyler', 'due voti Claude non bastano'))
        self.assertIsNone(self.d._conclave_resolution(7, self.record()['issue_hash'], [],
                                                      self.gh.pr_comments(100), self.target()
                                                      .head_sha))

    def test_a_substitute_block_leaves_it_to_the_operator(self):
        self.start(proposal(), UNAVAILABLE, vote())
        self.vote_cmd('BLOCK', 'Allarga il perimetro dell\'issue.')
        self.run_conclave(times=1)
        record = self.record()
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertEqual(record['votes'][0]['vote'], 'BLOCCA')
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})

    def test_a_reserved_perimeter_flag_ends_it_reserved(self):
        self.start(proposal(), UNAVAILABLE, vote())
        self.vote_cmd('RESERVED', 'Tocca le regole di fiducia.')
        self.run_conclave(times=1)
        record = self.record()
        self.assertEqual(record['outcome'], conclave.RESERVED)
        self.assertTrue(record['votes'][0]['reserved'])
        self.assertEqual(record['votes'][0]['reserved_reason'], 'Tocca le regole di fiducia.')

    def test_a_vote_after_the_deadline_is_refused(self):
        self.start(proposal(), UNAVAILABLE, vote())
        self.now = NOW + TIMEOUT + 10
        ident = self.vote_cmd()
        self.run_conclave(times=1)
        record = self.record()
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertEqual(record['substitute']['state'], 'expired')
        (refused,) = record['substitute']['refused']
        self.assertEqual(refused['comment'], ident)
        self.assertIn('dopo la scadenza', refused['why'])

    def test_a_comment_turned_into_a_vote_after_the_deadline_is_refused(self):
        self.start(proposal(), UNAVAILABLE, vote())
        ident = self.gh.say(100, HEADER + 'Leggo la risoluzione.\n', COORDINATOR)
        self.gh.thread[100][-1].update(created_at=iso(NOW + 10), updated_at=iso(NOW + 10))
        self.now = NOW + TIMEOUT + 60
        for c in self.gh.thread[100]:
            if c['id'] == ident:
                c['body'] = (HEADER + f'/devloop vote {self.conclave_id()} '
                             f'{conclave.text_hash(RESOLUTION)} APPROVE In perimetro.\n')
                c['updated_at'] = iso(self.now)
        self.run_conclave(times=1)
        record = self.record()
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertEqual(record['substitute']['state'], 'expired')
        (refused,) = record['substitute']['refused']
        self.assertEqual(refused['comment'], ident)
        self.assertIn('modificato dopo la scadenza', refused['why'])

    def test_a_command_without_the_time_of_its_latest_change_is_refused(self):
        self.start(proposal(), UNAVAILABLE, vote())
        ident = self.vote_cmd()
        del self.gh.thread[100][-1]['updated_at']
        self.run_conclave(times=1)
        record = self.record()
        self.assertEqual(record['outcome'], '')
        self.assertEqual(record['substitute']['refused'][0]['comment'], ident)
        self.assertIn('non sono leggibili', record['substitute']['refused'][0]['why'])

    def test_a_changed_hash_of_the_resolution_is_refused(self):
        self.start(proposal(), UNAVAILABLE, vote())
        ident = self.vote_cmd(digest=conclave.text_hash('un\'altra risoluzione'))
        self.run_conclave(times=1)
        record = self.record()
        self.assertEqual(record['outcome'], '')
        self.assertEqual(record['substitute']['state'], 'waiting')
        self.assertEqual(record['substitute']['refused'][0]['comment'], ident)
        self.assertIn('SHA-256', record['substitute']['refused'][0]['why'])
        # The refused command is spent: nothing more to do until another one.
        self.assertFalse(self.due())
        # A correct command afterwards still counts.
        self.vote_cmd()
        self.run_conclave(times=1)
        self.assertEqual(self.record()['outcome'], conclave.UNANIMOUS)

    def test_a_command_for_another_conclave_or_malformed_is_refused(self):
        self.start(proposal(), UNAVAILABLE, vote())
        self.vote_cmd(conclave_id=self.conclave_id() + 1)
        self.vote_cmd(verdict='FORSE')
        self.run_conclave(times=1)
        record = self.record()
        whys = [r['why'] for r in record['substitute']['refused']]
        self.assertIn('nomina il conclave', whys[0])
        self.assertIn('non ben formato', whys[1])
        self.assertEqual(record['outcome'], '')

    def test_a_command_from_another_identity_is_refused(self):
        self.start(proposal(), UNAVAILABLE, vote())
        for login in ('brushtyler', LOGINS['codex'], 'someone-else'):
            self.vote_cmd(login=login)
        # Not the coordinator's: nothing due, and nothing recorded for them.
        self.assertFalse(self.due())
        self.run_conclave(times=1)
        record = self.record()
        self.assertEqual(record['outcome'], '')
        self.assertEqual(record['substitute']['state'], 'waiting')
        self.assertEqual(record['substitute']['refused'], [])
        # When the conclave runs, past the deadline, they are ignored and logged: no vote.
        self.now = NOW + TIMEOUT
        self.run_conclave(times=1)
        record = self.record()
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertEqual(record['substitute']['state'], 'expired')
        self.assertEqual(record['substitute']['refused'], [])
        self.assertEqual(sum('only the coordinator' in line for line in self.logs), 3)

    def test_a_command_without_roles_coordinator_is_not_a_vote(self):
        self.cfg = CCFG
        record = self.start(proposal(), vote(), vote())
        self.assertEqual(record['outcome'], conclave.UNANIMOUS)
        self.assertIsNone(self.d._coordinator_login())


class TwoMissingTests(SubstituteBase):
    def test_two_missing_members_go_to_the_operator(self):
        record = self.start(proposal(), UNAVAILABLE, agents.AgentError('agy: quota exhausted'))
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertEqual([v['state'] for v in record['votes']], ['missing', 'missing'])
        self.assertEqual(record['substitute']['state'], 'void')
        self.assertIn('un solo voto sostitutivo', record['votes'][0]['missing'])

    def test_a_second_vote_over_the_limits_goes_to_the_operator(self):
        record = self.start(proposal(), UNAVAILABLE, vote(reason='x' * 1001))
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertEqual([v['state'] for v in record['votes']], ['missing', 'missing'])
        self.assertEqual(record['substitute']['state'], 'void')
        self.assertIn('un solo voto sostitutivo', record['votes'][0]['missing'])
        self.assertEqual(conclave.consistent(record), (True, ''))
        self.assertFalse(self.due())

    def test_a_second_call_started_without_a_result_goes_to_the_operator(self):
        self.make(proposal(), UNAVAILABLE, RuntimeError('dispatcher crash'))
        self.now = NOW
        self.d.clock = lambda: self.now
        self.block_pr()
        self.run_conclave(times=2)
        with self.assertRaises(RuntimeError):
            self.run_conclave(times=1)
        record = self.record()
        self.assertEqual([v['state'] for v in record['votes']], ['waiting', 'started'])
        # The restart settles the started call, before the deadline and without a command.
        self.assertTrue(self.due())
        self.run_conclave(times=1)
        record = self.record()
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertEqual([v['state'] for v in record['votes']], ['missing', 'missing'])
        self.assertEqual(record['votes'][1]['missing'], conclave.STARTED_WITHOUT_RESULT)
        self.assertEqual(record['substitute']['state'], 'void')
        self.assertEqual(len(self.runner.calls), 3)
        self.assertFalse(self.due())

    def test_a_second_missing_member_after_the_substitute_vote_goes_to_the_operator(self):
        self.make(proposal(), UNAVAILABLE, agents.AgentError('agy: quota exhausted'))
        self.now = NOW
        self.d.clock = lambda: self.now
        self.block_pr()
        self.run_conclave(times=2)
        self.vote_cmd()
        self.run_conclave(times=1)
        record = self.record()
        self.assertEqual(record['substitute']['state'], 'voted')
        self.assertEqual(record['votes'][1]['state'], 'missing')
        self.assertEqual(record['outcome'], conclave.BLOCKED)


class RecordTests(unittest.TestCase):
    def waiting(self):
        record = conclave.new_record({'kind': 'blocked', 'comment': 5, 'agent': 'claude',
                                      'step': 'fix'}, 'fix', ['claude', 'codex', 'agy'], 7,
                                     'i' * 64, 'c' * 40, 'c' * 40, 'd' * 64)
        record['proposal'].update(state='done', resolution=RESOLUTION,
                                  hash=conclave.text_hash(RESOLUTION),
                                  profile={'identity': 'a', 'kind': 'claude', 'model': 'm',
                                           'effort': 'high', 'args': []})
        record['hash'] = record['proposal']['hash']
        return conclave.await_substitute(record, 1, 'agy fallito', '2026-11-24T12:00:00Z', 900,
                                         901)

    def test_the_marker_round_trips_with_the_substitute(self):
        record = self.waiting()
        body = conclave.render(record)
        self.assertEqual(markdown.read_marker(body, 'conclave'), record)
        broken = dict(record, substitute=dict(record['substitute'], index=0))
        self.assertFalse(conclave.valid_record(broken))
        self.assertFalse(conclave.valid_record(dict(record, substitute=None)))

    def test_the_command_is_parsed_with_english_verdicts(self):
        digest = 'a' * 64
        command = conclave.vote_command(f'{HEADER}/devloop vote 900 {digest} approve Ok.\nAltro.')
        self.assertEqual(command, {'conclave': 900, 'hash': digest, 'verdict': 'APPROVE',
                                   'reason': 'Ok.\nAltro.', 'error': ''})
        self.assertIsNone(conclave.vote_command('/devloop veto'))
        for text in (f'/devloop vote 900 {digest} APPROVE', f'/devloop vote x {digest} BLOCK m',
                     '/devloop vote 900 abc BLOCK m', f'/devloop vote 900 {digest} APPROVA m'):
            with self.subTest(text=text):
                self.assertTrue(conclave.vote_command(text)['error'])
        long = conclave.vote_command(f'/devloop vote 900 {digest} BLOCK ' + 'x' * 1001)
        self.assertIn('supera', long['error'])


class ConfigTests(unittest.TestCase):
    def test_the_timeout_defaults_to_two_hours_and_must_be_positive(self):
        self.assertEqual(ssdlc_project.load().conclave_substitute_timeout_seconds, 7200)
        data = ssdlc_project.data()
        self.assertEqual(config.parse_config(dict(data, conclave_substitute_timeout_seconds=600))
                         .conclave_substitute_timeout_seconds, 600)
        for value in (0, -1, '7200', True):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    config.parse_config(dict(data, conclave_substitute_timeout_seconds=value))

    def test_the_overlay_may_set_it(self):
        self.assertIn('conclave_substitute_timeout_seconds', config.OVERLAY_KEYS)


class ReportTests(unittest.TestCase):
    def test_the_daily_summary_lists_the_substitute_vote(self):
        record = RecordTests().waiting()
        command = {'verdict': 'APPROVE', 'reason': 'ok'}
        conclave.apply_substitute(record, command, 950, COORDINATOR,
                                  {'identity': COORDINATOR, 'kind': 'claude',
                                   'model': 'claude-opus-5-5', 'effort': '', 'args': []},
                                  '2026-11-16T10:30:00Z')
        gh = ReportGH()
        ident = gh.add(12, REPORT_DISPATCHER, conclave.render(record), '2026-11-15T09:00:00Z')
        # Written the day before, edited with the vote on the day.
        for c in gh.comments:
            if c['id'] == ident:
                c['updated_at'] = '2026-11-16T10:30:00Z'
        # A forged record does not count.
        gh.add(12, 'someone-else', conclave.render(record), '2026-11-16T11:00:00Z')
        day = report.parse_day('2026-11-16')
        summary = report.Records(gh, TRUST, day, day, REPORT_CFG.agents).summary(day)
        self.assertEqual(summary.substitutes,
                         [('10:30', 12, 'agy', COORDINATOR, 'claude claude-opus-5-5', 'APPROVE')])
        text = report.render(summary)
        self.assertIn('Voti sostitutivi del coordinatore nel conclave', text)
        self.assertIn('| 10:30 | #12 | `agy` |', text)
        before = report.Records(gh, TRUST, day, day, REPORT_CFG.agents).summary(
            report.parse_day('2026-11-15'))
        self.assertEqual(before.substitutes, [])


if __name__ == '__main__':
    unittest.main()
