"""devloop: ``[roles.coordinator]``, its app and the closed perimeter of its commands (#528).

The operator's decision recorded in ADR 0002 (precision of 2026-11-22, #524 and #527): the
coordinator is an agent with a role app of its own, ``[apps.coordinator]``, turned on with
``[roles.coordinator]`` in the local overlay, off by default (D10). From its identity:

- the delegated commands (``retry``, ``rereview``, ``recheck``, ``fix``, ``hold``, ``release``,
  ``grant-round`` within the caps, ``accept-defaults``, ``decide`` outside the trust policy, the
  credentials and the budget, the resume with the labels) count as a trusted user's;
  ``capacity``, ``grant-budget`` and the priority labels too since #595
  (``test_devloop_coordinator_perimeter``);
- the commands reserved to the operator (``approve``, ``merge-budget``, ``reconcile``,
  ``trusted_users`` and the keys of trust) never count;
- any other command is refused; ``merge`` is the coordinator's reserved merge (#529,
  ``test_devloop_coordinator_merge``).
"""

import dataclasses
import unittest
from pathlib import Path
from unittest import mock

import devloop.__main__ as cli
import test_devloop_fix_command as fix_tests
import test_devloop_merge as merge_tests
import test_devloop_preflight_recheck as recheck_tests
from devloop import apps, config, markdown, supervision, token
from devloop.config import AppConfig, ConfigError, CoordinatorActor, DelegationPolicy
from devloop.dispatcher import Dispatcher
from devloop.planning import Action, plan
from devloop.review import finding_state
from devloop.watch import Trust
from helpers import ssdlc_project
from test_devloop import CFG, REV_NAME, FakeRunner, SnapshotGH, norm, pr, raw_review
from test_devloop_grant_round import ON, REASON, GrantCase, ci_round, granted_record
from test_devloop_preflight import checked, gap
from test_devloop_supervision import (AUTO, HUMAN, PREFLIGHT_AUTO, READY, REVIEW, STRICT, SUPER,
                                      Case, crash)

DATA = ssdlc_project.data()
SLUG = 'brushtyler-devloop-coordinator'
COORDINATOR = f'{SLUG}[bot]'
TABLE = {'name': 'codex-coordinator', 'kind': 'codex', 'provider': 'openai',
         'model': 'gpt-6.1-sol'}
ACTOR = CoordinatorActor(**TABLE)
APP = {'app_id': 7777777, 'slug': SLUG}


def coordinated(cfg):
    """``cfg`` with the coordinator turned on: the actor and its app."""
    return dataclasses.replace(cfg, coordinator=ACTOR,
                               apps=dict(cfg.apps, coordinator=AppConfig(**APP)))


def declared(cfg):
    """``cfg`` with the coordinator's app declared but ``[roles.coordinator]`` off."""
    return dataclasses.replace(cfg, apps=dict(cfg.apps, coordinator=AppConfig(**APP)))


def with_coordinator(table=None, app=True):
    data = dict(DATA, roles=dict(DATA['roles'], coordinator=dict(TABLE if table is None
                                                                  else table)))
    if app:
        data['apps'] = dict(DATA['apps'], coordinator=dict(APP))
    return data


def header():
    """The attribution header the coordinator, an agent, writes above its commands."""
    return markdown.header('Codex', '9.9', 'm', 'high')


class ConfigTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertIsNone(ssdlc_project.load().coordinator)
        self.assertIsNone(ssdlc_project.load_by_role().coordinator)
        self.assertIsNone(CFG.coordinator)
        # Declared without the actor, the app is only declared.
        data = dict(DATA, apps=dict(DATA['apps'], coordinator=dict(APP)))
        self.assertIsNone(config.parse_config(data).coordinator)

    def test_the_table_describes_the_coordinator_actor(self):
        cfg = config.parse_config(with_coordinator())
        self.assertEqual(cfg.coordinator, ACTOR)
        self.assertEqual(cfg.apps['coordinator'], AppConfig(**APP))
        self.assertNotIn('codex-coordinator', cfg.agents)
        # The other roles are untouched, and so is trust.
        self.assertEqual((cfg.supervisor, cfg.trusted_users),
                         (CFG.supervisor, CFG.trusted_users))

    def test_invalid_tables_stop_devloop(self):
        for change, error in (
                ({'name': 'claude-coordinator'}, "name: must be 'codex-coordinator'"),
                ({'name': 'codex-supervisor'}, "name: must be 'codex-coordinator'"),
                ({'kind': 'gemini'}, 'kind: must be one of'),
                ({'provider': ' '}, 'provider: must not be empty'),
                ({'model': ''}, 'model: must not be empty'),
                ({'model': 5}, 'model: expected str'),
                ({'effort': 'high'}, "unknown key 'effort'")):
            with self.subTest(change=change), self.assertRaisesRegex(ConfigError, error):
                config.parse_config(with_coordinator(dict(TABLE, **change)))
        for key in TABLE:
            table = {k: v for k, v in TABLE.items() if k != key}
            with self.subTest(missing=key), self.assertRaisesRegex(ConfigError, f"'{key}'"):
                config.parse_config(with_coordinator(table))
        # Only a table: an agent's key is never the coordinator.
        data = dict(with_coordinator(), roles=dict(DATA['roles'], coordinator='codex'))
        with self.assertRaisesRegex(ConfigError, 'roles.coordinator: expected a table'):
            config.parse_config(data)

    def test_the_actor_needs_its_role_app(self):
        with self.assertRaisesRegex(ConfigError, r'needs \[apps.coordinator\]'):
            config.parse_config(with_coordinator(app=False))

    def test_no_agent_is_the_coordinator(self):
        agents = dict(DATA['agents'], **{'codex-coordinator': dict(DATA['agents']['codex'])})
        with self.assertRaisesRegex(ConfigError, 'not run by devloop'):
            config.parse_config(dict(with_coordinator(), agents=agents))
        agents = dict(DATA['agents'], coordinator=dict(DATA['agents']['codex']))
        with self.assertRaisesRegex(ConfigError, 'agents.coordinator: .*reserved'):
            config.parse_config(dict(DATA, agents=agents))

    def test_the_role_app_is_its_own(self):
        apps_table = dict(DATA['apps'], coordinator={'app_id': DATA['apps']['supervisor']['app_id'],
                                                     'slug': SLUG})
        with self.assertRaisesRegex(ConfigError, 'each actor needs its own app'):
            config.parse_config(dict(with_coordinator(), apps=apps_table))

    def test_the_perimeter_is_closed(self):
        self.assertEqual(set(supervision.COORDINATOR_DELEGATED),
                         {'retry', 'rereview', 'recheck', 'fix', 'hold', 'release',
                          'grant-round', 'grant-budget', 'capacity', 'accept-defaults',
                          'decide', 'merge', 'finding'})
        self.assertEqual(set(supervision.COORDINATOR_RESERVED),
                         {'approve', 'merge-budget', 'reconcile'})
        self.assertFalse(set(supervision.COORDINATOR_DELEGATED)
                         & set(supervision.COORDINATOR_RESERVED))
        for command in supervision.COORDINATOR_DELEGATED:
            self.assertIsNone(supervision.coordinator_refusal(command))
        for command in (*supervision.COORDINATOR_RESERVED, 'escalate', 'veto', 'nuovo'):
            with self.subTest(command=command):
                self.assertIsNotNone(supervision.coordinator_refusal(command))


class IdentityTests(unittest.TestCase):
    def make(self, cfg):
        return Dispatcher(cfg, SnapshotGH([]), fix_tests.FakeWS(), FakeRunner(),
                          log=lambda m: None)

    def test_devloop_never_acts_as_the_coordinator(self):
        cfg = coordinated(CFG)
        self.assertFalse(apps.used(cfg, apps.COORDINATOR))
        self.assertNotIn(COORDINATOR, Trust.from_config(cfg).apps)
        # devloop token (scripts/agent-token.py) does not expose it.
        with mock.patch.object(cli, 'load') as load, \
                mock.patch.object(token.subprocess, 'run') as run:
            with self.assertRaisesRegex(SystemExit, "unknown actor 'coordinator'"):
                cli.main(['token', 'coordinator', '--', 'gh', 'issue', 'comment', '76'])
            load.assert_not_called()
            run.assert_not_called()

    def test_the_identity_is_the_role_app(self):
        d = self.make(coordinated(CFG))
        self.assertEqual(d._coordinator_login(), COORDINATOR)
        self.assertTrue(d._coordinator_may(COORDINATOR, 'retry'))
        self.assertFalse(d._coordinator_may(COORDINATOR, 'approve'))
        self.assertFalse(d._coordinator_may('stranger', 'retry'))
        # Never a trusted user nor the supervisor; one of devloop's apps since #595.
        self.assertFalse(d._may_hold(COORDINATOR))
        self.assertNotIn(COORDINATOR, d.cfg.trusted_users)
        self.assertIn(COORDINATOR, d._devloop_logins())
        self.assertNotIn(COORDINATOR, self.make(declared(CFG))._devloop_logins())


class OffByDefaultTests(Case):
    """Criterion 1: with ``[roles.coordinator]`` off nothing changes, even with its app
    declared."""

    def test_the_app_alone_has_no_power(self):
        for cfg in (AUTO, declared(AUTO)):
            with self.subTest(declared='coordinator' in cfg.apps):
                d = self.make(cfg)
                self.assertIsNone(d._coordinator_login())
                self.assertFalse(d._coordinator_may(COORDINATOR, 'retry'))
                for author in ('brushtyler', 'stranger'):
                    self.assertEqual(d._admitters(author, coordinator=True),
                                     d._admitters(author))
                    self.assertNotIn(COORDINATOR, d._admitters(author, coordinator=True))

    def test_its_commands_are_ignored_as_anyone_elses(self):
        d = self.make(declared(AUTO), crash(), raw_review())
        self.assertEqual(self.tick(d), [('review', 100)])
        self.assertEqual(self.gh.labels[100], {SUPER})
        for text in ('/devloop retry', '/devloop grant-budget motivo', '/devloop merge x y'):
            self.gh.command(100, text, login=COORDINATOR)
        self.assertEqual(self.tick(d), [])
        self.assertEqual(self.decisions(100), [])
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertTrue(any(f'ignored `/devloop retry` by {COORDINATOR}' in line
                            for line in self.logs))


class EscalationCase(Case):
    def fail_review(self, d, label):
        self.assertEqual(self.tick(d), [('review', 100)])
        self.assertEqual(self.gh.labels[100], {label})

    def answer(self, command=None):
        decision = self.decisions(100)[-1]
        self.assertEqual((decision['by'], decision['outcome']), (COORDINATOR, 'refused'))
        if command is not None:
            self.assertEqual(decision['command'], command)
        return self.gh.threads[100][-1]['body']


class DelegatedEscalationTests(EscalationCase):
    """``retry`` on the escalations to the supervisor and to the operator, as a trusted
    user's."""

    def test_retry_on_an_escalation_to_the_supervisor(self):
        d = self.make(coordinated(AUTO), crash(), raw_review())
        self.fail_review(d, SUPER)
        self.gh.command(100, '/devloop retry', login=COORDINATOR)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        (decision,) = self.decisions(100)
        self.assertEqual((decision['command'], decision['by'], decision['outcome']),
                         ('retry', COORDINATOR, 'applied'))

    def test_retry_on_an_escalation_to_the_operator(self):
        d = self.make(coordinated(STRICT), crash(), raw_review())
        self.fail_review(d, HUMAN)
        self.gh.command(100, '/devloop retry', login=COORDINATOR)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        (decision,) = self.decisions(100)
        self.assertEqual((decision['command'], decision['by'], decision['outcome']),
                         ('retry', COORDINATOR, 'applied'))
        self.assertEqual(self.tick(d), [('review', 100)])


class RefusedEscalationTests(EscalationCase):
    """The reserved commands and those not listed, on an escalation: answered as refused,
    spent, and the item stays where it is."""

    COMMANDS = {
        # Delegated since #595, but not on this escalation: a capacity only on the summary
        # issue, a grant-budget only on a budget escalation.
        'capacity': ('/devloop capacity max_open_prs 3 2h motivo',
                     'vale solo sulla issue del riepilogo'),
        # Delegated since #606, only on an open_findings escalation to the operator
        # (test_devloop_coordinator_findings).
        'finding': ('/devloop finding 1/codex/F1 resolved corretto',
                    'escalation `open_findings`'),
        # Reserved to the operator in person.
        'merge-budget': ('/devloop merge-budget 2 motivo', 'riservato all\'operatore'),
        # Reserved too, and no supervisor command (#528): read for the coordinator alone.
        'reconcile': ('/devloop reconcile t1 failed\nNota.', 'riservato all\'operatore'),
        'approve': ('/devloop approve Approvo.', 'riservato all\'operatore'),
        # Not listed: refused. ``merge`` is the coordinator's reserved merge (#529,
        # test_devloop_coordinator_merge).
        'escalate': ('/devloop escalate motivo', 'non è tra i comandi delegati'),
        'veto': ('/devloop veto motivo', 'non è tra i comandi delegati'),
        'nuovo': ('/devloop nuovo motivo', 'non è tra i comandi delegati'),
    }

    def test_on_an_escalation_to_the_supervisor(self):
        for name, (text, why) in self.COMMANDS.items():
            with self.subTest(command=name):
                d = self.make(coordinated(AUTO), crash(), raw_review())
                self.fail_review(d, SUPER)
                self.gh.command(100, text, login=COORDINATOR)
                self.assertEqual(self.tick(d), [('supervise', 100)])
                body = self.answer(name)
                self.assertIn('non applicabile', body)
                self.assertIn(why, body)
                self.assertEqual(self.gh.labels[100], {SUPER})
                self.assertEqual(self.tick(d), [])  # spent

    def test_on_an_escalation_to_the_operator(self):
        for name, (text, why) in self.COMMANDS.items():
            with self.subTest(command=name):
                d = self.make(coordinated(STRICT), crash(), raw_review())
                self.fail_review(d, HUMAN)
                self.gh.command(100, text, login=COORDINATOR)
                self.assertEqual(self.tick(d), [('supervise', 100)])
                body = self.answer(name)
                self.assertIn(why, body)
                self.assertEqual(self.gh.labels[100], {HUMAN})
                self.assertEqual(self.tick(d), [])

    def test_grant_budget_only_on_a_budget_escalation(self):
        # Delegated since #595 (test_devloop_coordinator_perimeter): on a tool_failure it is
        # refused as a trusted user's would be, not as reserved.
        for cfg, label, why in ((AUTO, SUPER, 'vale solo per un\'escalation `budget_grantable`'),
                                (STRICT, HUMAN, 'solo su un\'escalation `budget`')):
            with self.subTest(label=label):
                d = self.make(coordinated(cfg), crash(), raw_review())
                self.fail_review(d, label)
                self.gh.command(100, '/devloop grant-budget motivo', login=COORDINATOR)
                self.assertEqual(self.tick(d), [('supervise', 100)])
                body = self.answer('grant-budget')
                self.assertIn(why, body)
                self.assertNotIn('riservato', body)
                self.assertEqual(self.gh.labels[100], {label})

    def test_the_delegated_commands_of_other_readers_are_not_refused(self):
        # recheck, fix, hold and release are read elsewhere: on an escalation nothing answers
        # them as refused, as for a trusted user's.
        for text in ('/devloop recheck', '/devloop fix motivo', '/devloop hold motivo',
                     '/devloop release motivo'):
            with self.subTest(command=text):
                d = self.make(coordinated(AUTO), crash(), raw_review())
                self.fail_review(d, SUPER)
                self.gh.command(100, text, login=COORDINATOR)
                self.assertIsNone(d._supervisor_view(100, self.gh.pr_comments(100)))
                self.assertEqual(self.decisions(100), [])

    def test_off_the_reserved_and_unlisted_ones_are_ignored(self):
        # With the table off the coordinator's app is anyone else's: no answer, no record.
        for text in ('/devloop reconcile t1 failed\nNota.', '/devloop veto motivo',
                     '/devloop nuovo motivo'):
            with self.subTest(command=text):
                d = self.make(declared(AUTO), crash(), raw_review())
                self.fail_review(d, SUPER)
                self.gh.command(100, text, login=COORDINATOR)
                self.assertEqual(self.tick(d), [])
                self.assertEqual(self.decisions(100), [])

    def test_only_a_plain_name_is_a_command(self):
        self.assertEqual(supervision.coordinator_command(header() + '/devloop veto motivo'),
                         ('veto', 'motivo'))
        for body in ('/devloop', '/devloop `x`', '/devloop Veto', 'veto', '/devloop ' + 'a' * 33):
            with self.subTest(body=body):
                self.assertIsNone(supervision.coordinator_command(body))
        self.assertTrue(supervision.recorded_command('veto', 'refused'))
        self.assertFalse(supervision.recorded_command('veto', 'applied'))
        self.assertFalse(supervision.recorded_command('`x`', 'refused'))

    def test_a_trusted_users_command_waiting_wins(self):
        d = self.make(coordinated(STRICT), crash(), raw_review())
        self.fail_review(d, HUMAN)
        self.gh.command(100, '/devloop retry', login='brushtyler')
        self.gh.command(100, '/devloop merge-budget 2 motivo', login=COORDINATOR)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.decisions(100)[-1]['by'], 'brushtyler')
        self.assertEqual(self.gh.labels[100], {REVIEW})


class GrantRoundTests(GrantCase):
    """``grant-round`` within the caps of the delegation, as a trusted user's."""

    def test_a_grant_within_the_caps(self):
        d = self.make(coordinated(ON))
        self.assertEqual(self.at_limit(d, ci_round())['class'], 'round_grantable')
        self.gh.command(100, '/devloop grant-round ' + REASON, login=COORDINATOR)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {REVIEW})
        (decision,) = self.decisions(100)
        self.assertEqual({k: decision[k] for k in ('by', 'outcome', 'granted', 'cap', 'daily',
                                                   'daily_cap')},
                         {'by': COORDINATOR, 'outcome': 'granted', 'granted': 1, 'cap': 2,
                          'daily': 1, 'daily_cap': 3})

    def test_beyond_the_cap_of_the_pr_one_round_then_the_operators(self):
        # On a round_limit to the operator the coordinator grants at most one round per PR,
        # beyond the caps of its delegations (the operator's decision on #553); a second one on
        # the same PR stays the operator's.
        d = self.make(coordinated(ON))
        self.gh.say(100, 'brushtyler', granted_record(1))
        self.gh.say(100, 'brushtyler', granted_record(2))
        record = self.at_limit(d, ci_round(1), ci_round(2), ci_round(3))
        self.assertEqual((record['class'], record['to']), ('round_limit', 'operator'))
        self.gh.command(100, '/devloop grant-round ' + REASON, login=COORDINATOR)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        decision = self.decisions(100)[-1]
        self.assertEqual({k: decision.get(k) for k in ('by', 'outcome', 'class', 'operator',
                                                       'role', 'granted', 'cap', 'left')},
                         {'by': COORDINATOR, 'outcome': 'granted', 'class': 'round_limit',
                          'operator': True, 'role': 'coordinator', 'granted': 1, 'cap': 1,
                          'left': 0})
        self.assertEqual(self.gh.labels[100], {REVIEW})
        self.assertEqual(self.snapshot_pr(d).granted_rounds, 3)
        # The next limit: the delegation's caps are spent, the coordinator's round too.
        record = self.at_limit(d, ci_round(4))
        self.assertEqual((record['class'], record['to']), ('round_limit', 'operator'))
        self.gh.command(100, '/devloop grant-round ' + REASON, login=COORDINATOR)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        decision = self.decisions(100)[-1]
        self.assertEqual((decision['by'], decision['outcome']), (COORDINATOR, 'refused'))
        self.assertIn('il coordinatore ne concede al massimo 1 per PR', self.gh.threads[100][-1]
                      ['body'])
        self.assertEqual(self.gh.labels[100], {HUMAN})
        # The operator's own grant still applies.
        self.gh.command(100, '/devloop grant-round ' + REASON, login='brushtyler')
        self.assertEqual(self.tick(d), [('supervise', 100)])
        decision = self.decisions(100)[-1]
        self.assertEqual((decision['by'], decision['outcome'], decision['granted']),
                         ('brushtyler', 'granted', 2))
        self.assertNotIn('role', decision)
        self.assertEqual(self.gh.labels[100], {REVIEW})

    def test_off_the_delegation_nothing_is_granted(self):
        off = dataclasses.replace(ON, delegations=DelegationPolicy())
        d = self.make(coordinated(off))
        self.assertEqual(self.at_limit(d, ci_round())['class'], 'round_limit')


DECISION = '/devloop decide Formato: YAML invece di JSON.'


class PreflightDecisionTests(Case):
    """``accept-defaults`` and ``decide`` on the gaps of a readiness check."""

    def blocked(self, trust='none', cfg=PREFLIGHT_AUTO):
        d = self.make(coordinated(cfg),
                      checked(False, [gap('default_available', 'JSON')], trust=trust,
                              reason='punto 1' if trust != 'none' else ''),
                      has_pr=False)
        self.assertEqual(self.tick(d), [('preflight', 7)])
        return d

    def test_accept_defaults(self):
        d = self.blocked()
        self.assertEqual(self.gh.labels[7], {READY, SUPER})
        self.gh.command(7, '/devloop accept-defaults', login=COORDINATOR)
        self.assertEqual(self.tick(d), [('supervise', 7)])
        decision = self.decisions(7)[-1]
        self.assertEqual((decision['by'], decision['outcome']), (COORDINATOR, 'accepted'))
        self.assertEqual(self.gh.labels[7], {READY})

    def test_decide_outside_the_trust_policy(self):
        d = self.blocked()
        self.gh.command(7, DECISION, login=COORDINATOR)
        self.assertEqual(self.tick(d), [('supervise', 7)])
        decision = self.decisions(7)[-1]
        self.assertEqual((decision['by'], decision['outcome']), (COORDINATOR, 'decided'))
        self.assertEqual(self.gh.labels[7], {READY})

    def test_decide_on_the_operators_escalation(self):
        d = self.blocked(cfg=dataclasses.replace(STRICT, preflight=PREFLIGHT_AUTO.preflight))
        self.assertEqual(self.gh.labels[7], {READY, HUMAN})
        self.gh.command(7, DECISION, login=COORDINATOR)
        self.assertEqual(self.tick(d), [('supervise', 7)])
        decision = self.decisions(7)[-1]
        self.assertEqual((decision['by'], decision['outcome']), (COORDINATOR, 'decided'))
        self.assertNotIn(HUMAN, self.gh.labels[7])

    def test_decide_is_refused_on_the_trust_policy(self):
        # A check that declares the trust policy touched, covered by a decision of the operator.
        for login, outcome in ((COORDINATOR, 'refused'), ('brushtyler', 'decided')):
            with self.subTest(login=login):
                d = self.blocked('covered')
                self.gh.command(7, DECISION, login=login)
                self.assertEqual(self.tick(d), [('supervise', 7)])
                decision = self.decisions(7)[-1]
                self.assertEqual((decision['by'], decision['outcome']), (login, outcome))
                if login == COORDINATOR:
                    self.assertTrue(any('policy di fiducia' in c['body']
                                        for c in self.gh.threads[7][-2:]))
                    self.assertIn(HUMAN, self.gh.labels[7])
                else:
                    # A trusted user decides it: the trust policy binds only the coordinator.
                    self.assertNotIn(HUMAN, self.gh.labels[7])
                self.assertEqual(len(self.runner.calls), 1)  # nothing ran after the check

    def test_decide_without_a_declaration_of_the_check_is_refused(self):
        d = self.make(coordinated(PREFLIGHT_AUTO), has_pr=False)
        record = {'ready': False, 'gaps': [gap('default_available', 'JSON')], 'split': ''}
        problem = []
        d._untrusted_change = lambda history: None
        with mock.patch.object(d, '_preflight_records', return_value=[(0, dict(record))]), \
                mock.patch.object(d, '_request_hash', return_value='h'):
            result = d._decidable_check(7, {READY, SUPER}, [], lambda *a, **k: problem.append(a),
                                        'x', 'y', operator=True, coordinator=True)
        self.assertIsNone(result)
        self.assertIn('policy di fiducia', problem[0][1])
        # The supervisor's decide is not bound by it: the check's other conditions hold.
        with mock.patch.object(d, '_preflight_records',
                               return_value=[(0, dict(record, hash='h'))]), \
                mock.patch.object(d, '_request_hash', return_value='h'):
            self.assertIsNotNone(d._decidable_check(7, {READY, SUPER}, [], lambda *a, **k: None,
                                                    'x', 'y', operator=True))


class RereviewTests(unittest.TestCase):
    """``rereview`` after a verdict, as a trusted user's: a new round, without its text in the
    prompt."""

    def thread(self, login, body):
        reviewer = markdown.header(REV_NAME, '9.9', 'm', 'high')
        return [{'author': {'login': 'brushtyler'},
                 'body': markdown.render_review(norm(raw_review('needs_operator',
                                                                human='manca una issue')),
                                                reviewer)},
                {'author': {'login': login}, 'body': body}]

    def make(self, cfg, comments):
        self.gh = SnapshotGH(comments)
        self.gh.labels[100] = {'agent:review'}
        self.runner = FakeRunner(raw_review())
        return Dispatcher(cfg, self.gh, fix_tests.FakeWS(), self.runner, log=lambda m: None)

    def test_the_coordinators_rereview_runs_a_new_round(self):
        cfg = coordinated(CFG)
        for body in ('/devloop rereview', header() + '/devloop rereview Aperta la #999.'):
            with self.subTest(body=body):
                d = self.make(cfg, self.thread(COORDINATOR, body))
                snap = d.snapshot()
                self.assertTrue(snap.prs[0].rereview)
                self.assertEqual(snap.prs[0].operator_comments, [])
                self.assertEqual([a.kind for a in plan(snap, cfg)], ['review'])
                d.tick()
                self.assertNotIn('Aperta la #999', self.runner.calls[0][3])

    def test_off_it_is_ignored(self):
        for cfg in (CFG, declared(CFG)):
            d = self.make(cfg, self.thread(COORDINATOR, header() + '/devloop rereview x'))
            snap = d.snapshot()
            self.assertFalse(snap.prs[0].rereview)
            self.assertEqual([a.kind for a in plan(snap, cfg)], ['apply_review'])


class RecheckTests(recheck_tests.Case):
    """``recheck`` and the resume with ``agent:ready`` added again."""

    def test_the_coordinators_recheck_repeats_the_check(self):
        cfg = coordinated(recheck_tests.CFG)
        d = self.make(cfg=cfg)
        self.blocked(d, gap())
        self.gh.say(7, COORDINATOR, header() + '/devloop recheck')
        self.assertEqual(self.planned(d, cfg), [('preflight', 7)])
        self.runner.outputs.append(checked(True))
        self.assertEqual([a.kind for a in d.tick()], ['preflight'])
        self.assertEqual(self.gh.labels[7], {READY})

    def test_the_coordinators_agent_ready_resumes_the_issue(self):
        cfg = coordinated(recheck_tests.CFG)
        d = self.make(cfg=cfg)
        self.blocked(d, gap())
        self.gh.relabel(COORDINATOR)
        self.assertEqual(self.planned(d, cfg), [('preflight', 7)])
        self.runner.outputs.append(checked(True))
        self.assertEqual([a.kind for a in d.tick()], ['preflight'])
        self.assertEqual(self.planned(d, cfg), [('implement', 7)])

    def test_off_both_are_ignored(self):
        cfg = declared(recheck_tests.CFG)
        d = self.make(cfg=cfg)
        self.blocked(d, gap())
        self.gh.say(7, COORDINATOR, header() + '/devloop recheck')
        self.gh.relabel(COORDINATOR)
        self.assertEqual(self.planned(d, cfg), [])

    def test_only_agent_ready_and_priority_and_only_on_issues_of_trusted_authors(self):
        d = self.make(cfg=coordinated(recheck_tests.CFG))
        # agent:ready and, since #595, the priority labels.
        self.assertIn(COORDINATOR, d._admitters('brushtyler', coordinator=True))
        # Its dependency and implementer labels never count.
        self.assertNotIn(COORDINATOR, d._admitters('brushtyler'))
        # Never on an issue of an untrusted author.
        self.assertNotIn(COORDINATOR, d._admitters('stranger', coordinator=True))


class FixTests(fix_tests.Case):
    def test_the_coordinators_fix_runs(self):
        cfg = coordinated(CFG)
        d = self.make(cfg=cfg, runner=FakeRunner(dict(fix_tests.fixed(), responses=[])))
        self.approve()
        command = self.gh.say(COORDINATOR, header() + f'\n/devloop fix\n{fix_tests.REQUEST}')
        self.assertEqual(self.planned(d, cfg),
                         [Action('fix', 100, f'/devloop fix by {COORDINATOR}')])
        d.tick()
        (fix,) = fix_tests.records(self.gh, 'fix')
        self.assertEqual((fix['requests'][0]['comment'], fix['requests'][0]['by']),
                         (command, COORDINATOR))

    def test_off_it_is_ignored(self):
        d = self.make(cfg=declared(CFG))
        self.gh.say(COORDINATOR, header() + f'\n/devloop fix {fix_tests.REQUEST}')
        self.assertEqual([a.kind for a in self.planned(d)], ['review'])
        self.assertTrue(self.ignored('neither the supervisor nor a trusted user'))


class HoldTests(merge_tests.Case):
    def test_the_coordinator_holds_and_releases(self):
        cfg = coordinated(merge_tests.AUTO)
        d = self.make(cfg)
        self.gh.say(COORDINATOR, header() + '/devloop hold aspetto la decisione')
        self.assertEqual(d.tick(), [Action('hold', 100, f'hold by {COORDINATOR}')])
        self.assertEqual(self.gh.labels[100], {merge_tests.READY, merge_tests.HOLD})
        self.assertEqual(d.tick(), [])
        self.assert_not_merged()
        self.gh.say(COORDINATOR, header() + '/devloop release')
        self.assertEqual(d.tick(), [Action('hold', 100, f'release by {COORDINATOR}')])
        self.assertEqual(d.tick(), [Action('merge', 100, 'merge mode auto')])

    def test_off_it_is_ignored(self):
        d = self.make(declared(merge_tests.AUTO))
        self.gh.say(COORDINATOR, header() + '/devloop hold fermo tutto')
        self.assertEqual(d.tick(), [Action('merge', 100, 'merge mode auto')])


class ReservedTests(unittest.TestCase):
    """The commands reserved to the operator outside an escalation, and the commands not
    listed that only trusted users write: never the coordinator's."""

    def make(self, cfg=None):
        self.logs = []
        self.gh = SnapshotGH([])
        return Dispatcher(cfg or coordinated(CFG), self.gh, fix_tests.FakeWS(), FakeRunner(),
                          log=self.logs.append)

    def comment(self, body, ident=5001, login=COORDINATOR, **extra):
        return dict({'id': ident, 'author': {'login': login}, 'body': body}, **extra)

    def logged(self, text):
        return any(text in line and COORDINATOR in line for line in self.logs)

    def test_approve(self):
        d = self.make()
        approve = self.comment('/devloop approve Approvo la modifica della regola.',
                               created_at='2026-11-22T10:00:00Z',
                               updated_at='2026-11-22T10:00:00Z')
        history = {'complete': True, 'labeled': [], 'last_edited_at': None, 'renamed': []}
        self.assertEqual(d._issue_decisions([approve], history), [])
        ((refused, why),) = d._refused_approvals([approve])
        self.assertEqual(refused['id'], 5001)
        self.assertIn('un\'app', why)

    def test_merge_budget_on_the_summary_issue(self):
        # ``capacity`` is delegated since #595 (test_devloop_coordinator_perimeter).
        d = self.make()
        comments = [self.comment(header() + '/devloop merge-budget 3 motivo', ident=5002)]
        self.assertEqual(d._capacity_commands(316, comments), 0)
        self.assertEqual(self.gh.comments, [])
        self.assertTrue(self.logged('ignored `/devloop merge-budget`'))

    def test_reconcile(self):
        d = self.make()
        journal = mock.Mock()
        journal.handled.return_value = False
        d.ledger = mock.Mock(journal=journal)
        d._reconcile_comment(7, self.comment('/devloop reconcile t1 failed\nNota.'), False)
        journal.reconcile.assert_not_called()
        journal.refuse.assert_not_called()
        self.assertTrue(self.logged('ignored `/devloop reconcile`'))

    def test_trusted_users_and_the_keys_of_trust(self):
        # The coordinator is never a trusted user: a bot in trusted_users stops devloop, and
        # its table changes no key of trust.
        data = dict(with_coordinator(), trusted_users=['brushtyler', COORDINATOR])
        with self.assertRaisesRegex(ConfigError, 'bots and apps cannot approve'):
            config.parse_config(data)
        cfg = config.parse_config(with_coordinator())
        self.assertEqual(cfg.trusted_users, CFG.trusted_users)
        self.assertEqual(cfg.review_policy, config.parse_config(DATA).review_policy)
        d = self.make(cfg)
        # One of devloop's apps since #595, never a trusted user: its approve stays refused
        # (test_approve).
        self.assertNotIn(COORDINATOR, d.cfg.trusted_users)

    def test_finding_no_operator_closure_and_veto_not_listed(self):
        d = self.make()
        # Its finding is a command of an open_findings escalation (#606), never an operator's
        # closure read in review.
        body = '/devloop finding 1/codex/F1 resolved corretto'
        self.assertEqual(d._finding_commands(pr(), [self.comment(body)],
                                             finding_state([], [])), [])
        self.assertEqual(d._veto_commands([self.comment('/devloop veto motivo')], 7), [])
        self.assertTrue(self.logged('ignored `/devloop veto`'))


class DocsTests(unittest.TestCase):
    def test_the_guide_and_the_reference_describe_the_role(self):
        base = Path(__file__).resolve().parents[1] / 'docs' / 'devloop'
        guide = (base / 'guide.md').read_text(encoding='utf-8')
        reference = (base / 'reference.md').read_text(encoding='utf-8')
        self.assertIn('### The coordinator', guide)
        self.assertIn('### The coordinator app', reference)
        for command in (*supervision.COORDINATOR_DELEGATED, *supervision.COORDINATOR_RESERVED):
            with self.subTest(command=command):
                self.assertIn(f'`{command}`', guide)
        self.assertIn('[roles.coordinator]', reference)
        self.assertIn('[apps.coordinator]', reference)


if __name__ == '__main__':
    unittest.main()
