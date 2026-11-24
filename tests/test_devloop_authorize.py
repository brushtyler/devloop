"""devloop: the coordinator authorizes the work, the supervisor queues it (#607, ADR 0002, second
precision of 2026-11-23, point 1).

With ``[roles.coordinator]``:

- the coordinator's ``/devloop authorize [priority:<p>]`` is recorded by the dispatcher, bound to
  the SHA-256 of the issue's title and body, and removes ``agent:needs-coordinator``;
- the supervisor's ``agent:ready`` admits an issue only while an authorization holds for its
  current text: without one, or after a change of the text, it is ignored and logged;
- a readiness check that declares the trust policy touched sends an issue not opened by a
  trusted user to ``agent:needs-operator`` even when authorized (#595);
- ``/devloop authorize`` from any other identity is refused, and recorded;
- a trusted user's and the coordinator's ``agent:ready`` admit the issue as before.

Without ``[roles.coordinator]`` nothing changes: the supervisor's labels count as with #178.
"""

import dataclasses
import unittest
from pathlib import Path

import test_devloop_preflight_recheck as recheck_tests
from devloop import authorization, markdown, supervision
from devloop.dispatch_base import BaseBroken
from devloop.dispatch_preflight import GAP_TEXTS
from devloop.dispatcher import Dispatcher
from devloop.planning import Snapshot, plan
from test_devloop import implemented
from test_devloop_coordinator import COORDINATOR, coordinated, declared, header
from test_devloop_preflight import checked
from test_devloop_supervision import HUMAN, READY, SUPERVISOR_LOGIN, Identities

NEEDS = 'agent:needs-coordinator'
WORKING = 'agent:working'
CFG = coordinated(recheck_tests.CFG)
ROOT = Path(__file__).resolve().parents[1]
# The gap devloop writes, in the language of the tests' project (Italian, #632).
AUTHORIZED_TRUST_GAP = GAP_TEXTS.texts(CFG.language)('authorized_gap')


class TextGH(recheck_tests.ClockGH):
    """Issue #7, opened by the supervisor and waiting for the coordinator, whose title and body
    may change (``edit``)."""

    def __init__(self):
        super().__init__()
        self.author = SUPERVISOR_LOGIN
        self.title, self.body = 'Issue 7', 'Do the thing.'
        self.labels[7] = {NEEDS}
        self.ready_events = []

    def issue_history(self, number):
        return dict(super().issue_history(number), title=self.title, body=self.body)

    def edit(self, body, login='brushtyler'):
        """``login`` changes the body: even a trusted user's edit voids an authorization."""
        self.body = body
        self.edits = (self.now(), login)

    def rename(self, title, login='brushtyler'):
        """``login`` changes the title."""
        self.title = title
        self.renames = [*self.renames, (login, self.now())]

    def ready(self, login=SUPERVISOR_LOGIN):
        """``login`` adds agent:ready."""
        self.labels[7].add(READY)
        self.ready_events.append((login, self.now()))


class Case(unittest.TestCase):
    def make(self, cfg=CFG):
        self.cfg = cfg
        self.gh, self.ws = TextGH(), recheck_tests.CountingWS()
        self.runner, self.logs = recheck_tests.StepRunner(), []
        return Dispatcher(cfg, self.gh, self.ws, self.runner, log=self.logs.append,
                          identities=Identities(self.gh))

    def planned(self, d):
        return [(a.kind, a.number) for a in plan(d.snapshot(), self.cfg)]

    def ticked(self, d):
        return [(a.kind, a.number) for a in d.tick()]

    def records(self):
        return self.gh.records(7, 'authorize')

    def authorize(self, d, arguments='', login=COORDINATOR):
        """``login`` writes ``/devloop authorize`` and a tick records it."""
        command = ' '.join(filter(None, (authorization.AUTHORIZE_COMMAND, arguments)))
        self.gh.say(7, login, header() + '\n' + command + '\n\nRientra nel tema attivo.')
        self.assertEqual(self.ticked(d), [('authorize', 7)])
        return self.records()[-1]

    def check(self, d, trust='none'):
        self.runner.outputs.append(checked(True, trust=trust,
                                           reason='' if trust == 'none' else 'punto 1'))
        self.assertEqual(self.ticked(d), [('preflight', 7)])
        return self.gh.records(7, 'preflight')[-1]

    def ignored(self):
        return [m for m in self.logs if 'ignored agent:ready by' in m]


class CommandTests(unittest.TestCase):
    def test_authorize_is_delegated_to_the_coordinator(self):
        self.assertIn('authorize', supervision.COORDINATOR_DELEGATED)
        self.assertIsNone(supervision.coordinator_refusal('authorize'))

    def test_arguments(self):
        body = header() + '\n/devloop authorize priority:low\n\nperché'
        self.assertEqual(authorization.authorize_arguments(body), ('low', ''))
        self.assertEqual(authorization.authorize_arguments('/devloop authorize'), ('', ''))
        for line in ('/devloop authorize priority:urgent', '/devloop authorize high',
                     '/devloop authorize priority:high priority:low'):
            with self.subTest(line=line):
                self.assertEqual(authorization.authorize_arguments(line),
                                 ('', authorization.MALFORMED))
        for line in ('/devloop authorized', 'authorize', '/devloop recheck', ''):
            with self.subTest(line=line):
                self.assertIsNone(authorization.authorize_arguments(line))

    def test_the_hash_binds_title_and_body(self):
        digest = authorization.text_hash('T', 'B')
        self.assertEqual(len(digest), 64)
        self.assertNotEqual(digest, authorization.text_hash('T', 'B2'))
        self.assertNotEqual(digest, authorization.text_hash('T2', 'B'))
        self.assertNotEqual(authorization.text_hash('a', 'bc'), authorization.text_hash('ab', 'c'))

    def test_a_change_in_the_same_second_counts_as_after(self):
        at = '2026-11-24T10:00:00Z'
        history = {'last_edited_at': at, 'renamed': []}
        self.assertTrue(authorization.changed_since(history, at))
        self.assertFalse(authorization.changed_since(history, '2026-11-24T10:00:01Z'))
        renamed = {'last_edited_at': None, 'renamed': [('x', '2026-11-24T10:00:05Z')]}
        self.assertTrue(authorization.changed_since(renamed, at))
        self.assertFalse(authorization.changed_since({'last_edited_at': None, 'renamed': []}, ''))

    def test_the_record_is_valid_only_well_formed(self):
        good = {'comment': 3, 'by': COORDINATOR, 'at': 't', 'priority': 'high',
                'outcome': 'authorized', 'hash': authorization.text_hash('T', 'B')}
        body = markdown.render_authorize(good)
        self.assertEqual(markdown.read_marker(body, 'authorize'), good)
        self.assertIn('Lavoro autorizzato dal coordinatore', body)
        for bad in (dict(good, hash='x'), dict(good, priority='urgent'),
                    dict(good, outcome='maybe'), {k: v for k, v in good.items() if k != 'hash'},
                    dict(good, outcome='refused', reason='r')):
            with self.subTest(bad=bad):
                self.assertIsNone(markdown.read_marker(
                    markdown.publishable('x', 'authorize', bad), 'authorize'))


class AuthorizeTests(Case):
    def test_a_valid_authorization_lets_the_supervisors_ready_start_the_issue(self):
        d = self.make()
        record = self.authorize(d, 'priority:high')
        self.assertEqual(record['outcome'], 'authorized')
        self.assertEqual((record['by'], record['priority']), (COORDINATOR, 'high'))
        self.assertEqual(record['hash'], authorization.text_hash('Issue 7', 'Do the thing.'))
        self.assertTrue(record['at'])
        # devloop removes agent:needs-coordinator, and records the command once.
        self.assertEqual(self.gh.labels[7], set())
        self.assertEqual(self.planned(d), [])
        self.gh.ready()
        self.assertEqual(self.planned(d), [('preflight', 7)])
        self.assertTrue(self.check(d)['ready'])
        self.assertEqual(self.planned(d), [('implement', 7)])
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.ignored(), [])

    def test_a_text_changed_after_the_authorization_does_not_start(self):
        d = self.make()
        self.authorize(d)
        self.gh.edit('Do another thing.')
        self.gh.ready()
        self.assertEqual(self.planned(d), [])
        self.assertTrue(self.ignored())
        # A new authorization, of the text read now, lets it start.
        self.authorize(d)
        self.assertEqual(self.planned(d), [('preflight', 7)])

    def test_a_text_changed_and_restored_after_the_authorization_does_not_start(self):
        # The text the coordinator read comes back, but a change followed the command: the
        # authorization does not come back with it.
        for change in ('body', 'title'):
            with self.subTest(change=change):
                d = self.make()
                self.authorize(d)
                if change == 'body':
                    self.gh.edit('Testo temporaneo')
                    self.gh.edit('Do the thing.')
                else:
                    self.gh.rename('Titolo temporaneo')
                    self.gh.rename('Issue 7')
                self.gh.ready()
                self.assertEqual(self.planned(d), [])
                self.assertTrue(self.ignored())
                self.authorize(d)
                self.assertEqual(self.planned(d), [('preflight', 7)])

    def test_a_text_changed_after_the_command_and_before_its_record_is_refused(self):
        d = self.make()
        self.gh.say(7, COORDINATOR, header() + '\n/devloop authorize')
        self.gh.edit('Do another thing.')
        self.assertEqual(self.ticked(d), [('authorize', 7)])
        record = self.records()[-1]
        self.assertEqual(record['outcome'], 'refused')
        self.assertIn('cambiati dopo il comando', record['reason'])
        self.assertEqual(self.gh.labels[7], {NEEDS})

    def test_a_text_changed_before_the_command_is_authorized(self):
        d = self.make()
        self.gh.edit('Do another thing.')
        record = self.authorize(d)
        self.assertEqual(record['hash'], authorization.text_hash('Issue 7', 'Do another thing.'))

    def test_a_change_after_the_snapshot_stops_the_implementation(self):
        cfg = coordinated(dataclasses.replace(recheck_tests.CFG, preflight=''))
        d = self.make(cfg)
        self.authorize(d)
        self.gh.ready()
        self.assertEqual(self.planned(d), [('implement', 7)])
        self.gh.edit('Do another thing.')
        d._do_implement(7)
        self.assertNotIn('agent:working', self.gh.labels[7])
        self.assertTrue(any('without an authorization of the coordinator' in m
                            for m in self.logs))

    def waiting(self, d):
        """The authorized issue 7, queued by the supervisor and checked, whose implementation
        waits for a base branch that has already moved on (#459): it keeps agent:working."""
        self.authorize(d)
        self.gh.ready()
        if self.cfg.preflight:
            self.check(d)
        self.gh.labels[7] = {WORKING}
        d._wait_base(7, 'implement', BaseBroken('f' * 40, ['test_x.Case.test_a']), 'f' * 40)

    def test_a_restart_after_the_base_wait_needs_an_authorization_of_the_current_text(self):
        for preflight in (True, False):
            with self.subTest(preflight=preflight):
                cfg = CFG if preflight else coordinated(
                    dataclasses.replace(recheck_tests.CFG, preflight=''))
                d = self.make(cfg)
                self.waiting(d)
                self.gh.edit('Do another thing.')
                turns = len(self.runner.calls)
                # No check and no implementation: the issue goes back to the coordinator.
                self.assertEqual(self.ticked(d), [('unauthorized', 7)])
                self.assertEqual(len(self.runner.calls), turns)
                self.assertEqual(self.gh.labels[7], {NEEDS})
                self.assertEqual(self.gh.created, [])
                self.assertEqual(self.planned(d), [])
                # A new authorization of the text read now, then the supervisor's agent:ready.
                self.authorize(d)
                self.assertEqual(self.gh.labels[7], set())
                self.gh.ready()
                self.assertEqual(self.planned(d),
                                 [('preflight' if preflight else 'implement', 7)])

    def test_a_restart_with_a_valid_authorization_starts_again(self):
        d = self.make()
        self.waiting(d)
        self.assertEqual(self.planned(d), [('implement', 7)])
        self.runner.outputs.append(implemented())
        self.assertEqual(self.ticked(d), [('implement', 7)])
        self.assertEqual(len(self.gh.created), 1)

    def test_a_change_after_the_snapshot_stops_the_restart(self):
        d = self.make()
        self.waiting(d)
        self.assertEqual(self.planned(d), [('implement', 7)])
        self.gh.edit('Do another thing.')
        d._do_implement(7)
        self.assertEqual(self.gh.labels[7], {NEEDS})
        self.assertEqual(self.gh.created, [])

    def test_the_supervisors_ready_without_authorization_does_not_start_and_is_logged(self):
        d = self.make()
        self.gh.labels[7] = set()
        self.gh.ready()
        self.assertEqual(self.planned(d), [])
        self.assertEqual(len(self.ignored()), 1)
        self.assertIn(authorization.AUTHORIZE_COMMAND, self.ignored()[0])
        snap = d.snapshot()
        self.assertFalse(snap.ready_issues[0]['admitted'])

    def test_the_supervisors_ready_waits_for_the_record_of_the_command(self):
        d = self.make()
        self.gh.labels[7] = set()
        self.gh.ready()
        self.gh.say(7, COORDINATOR, header() + '\n/devloop authorize')
        self.assertEqual(self.ticked(d), [('authorize', 7)])
        self.assertEqual(self.planned(d), [('preflight', 7)])

    def test_trust_policy_touched_goes_to_the_operator_even_if_authorized(self):
        for trust in ('covered', 'uncovered'):
            with self.subTest(trust=trust):
                d = self.make()
                self.authorize(d)
                self.gh.ready()
                self.runner.outputs.append(checked(True, trust=trust, reason='punto 1'))
                self.assertEqual(self.ticked(d), [('preflight', 7)])
                record = self.gh.records(7, 'preflight')[-1]
                self.assertFalse(record['ready'])
                self.assertEqual(record['gaps'][0]['kind'], 'operator_decision')
                self.assertEqual(record['gaps'][0]['description'], AUTHORIZED_TRUST_GAP)
                self.assertEqual(self.gh.labels[7], {READY, HUMAN})
                self.assertEqual(self.planned(d), [])
                # The operator's agent:ready admits it.
                self.gh.labels[7].discard(HUMAN)
                self.gh.ready('brushtyler')
                self.assertTrue(self.check(d, 'covered')['ready'])
                self.assertEqual(self.planned(d), [('implement', 7)])

    def test_another_identity_is_refused_and_recorded(self):
        for login in ('brushtyler', SUPERVISOR_LOGIN, 'stranger'):
            with self.subTest(login=login):
                d = self.make()
                record = self.authorize(d, login=login)
                self.assertEqual(record['outcome'], 'refused')
                self.assertEqual(record['by'], login)
                self.assertNotIn('hash', record)
                self.assertIn('coordinatore', record['reason'])
                self.assertIn('rifiutato', self.gh.threads[7][-1]['body'])
                self.assertEqual(self.gh.labels[7], {NEEDS})
                self.assertTrue(any(f'refused /devloop authorize by {login}' in m
                                    for m in self.logs))
                # Recorded once; the supervisor's agent:ready still does not start it.
                self.gh.ready()
                self.assertEqual(self.planned(d), [])

    def test_malformed_arguments_are_refused(self):
        d = self.make()
        record = self.authorize(d, 'priority:urgent')
        self.assertEqual((record['outcome'], record['reason']),
                         ('refused', authorization.MALFORMED))

    def test_a_strangers_issue_is_not_authorized(self):
        d = self.make()
        self.gh.author = 'stranger'
        record = self.authorize(d)
        self.assertEqual(record['outcome'], 'refused')
        self.assertIn('utente fidato', record['reason'])

    def test_the_coordinator_authorizes_its_own_issues_and_those_of_trusted_users(self):
        for author in (COORDINATOR, 'brushtyler'):
            with self.subTest(author=author):
                d = self.make()
                self.gh.author = author
                self.assertEqual(self.authorize(d)['outcome'], 'authorized')
                self.gh.ready()
                self.assertEqual(self.planned(d), [('preflight', 7)])

    def test_a_trusted_users_ready_starts_the_issue_as_today(self):
        for author in (SUPERVISOR_LOGIN, 'brushtyler'):
            with self.subTest(author=author):
                d = self.make()
                self.gh.author = author
                self.gh.ready('brushtyler')
                self.assertEqual(self.planned(d), [('preflight', 7)])
                self.assertTrue(self.check(d)['ready'])
                self.assertEqual(self.planned(d), [('implement', 7)])

    def test_the_coordinators_ready_starts_it_without_authorization(self):
        # The fallback of point 2 of the ADR, when the supervisor does not answer.
        d = self.make()
        self.gh.ready(COORDINATOR)
        self.assertEqual(self.planned(d), [('preflight', 7)])

    def test_off_nothing_changes(self):
        for cfg in (recheck_tests.CFG, declared(recheck_tests.CFG)):
            with self.subTest(coordinator=cfg.coordinator):
                d = self.make(cfg)
                self.gh.say(7, COORDINATOR, header() + '\n/devloop authorize')
                self.assertEqual(self.planned(d), [])
                # The supervisor's agent:ready on its own issue counts as with #178.
                self.gh.ready()
                self.assertEqual(self.planned(d), [('preflight', 7)])
                self.assertEqual(self.records(), [])

    def test_plan_records_the_commands_without_a_slot(self):
        cfg = dataclasses.replace(CFG, max_open_prs=0)
        actions = plan(Snapshot([], [], [], authorizations=[7, 8]), cfg)
        self.assertEqual([(a.kind, a.number) for a in actions],
                         [('authorize', 7), ('authorize', 8)])
        self.assertEqual(plan(Snapshot([], [], [], authorizations=[7]), cfg, busy={7}), [])


class LabelTests(unittest.TestCase):
    def test_the_label_is_configurable_and_created(self):
        from devloop import config, context
        labels = config.Labels()
        self.assertEqual(labels.needs_coordinator, NEEDS)
        self.assertIn(NEEDS, labels.all())
        self.assertIn('needs_coordinator', context.LABEL_STYLE)


class DocsTests(unittest.TestCase):
    def test_the_guide_and_the_runbooks_describe_the_authorization(self):
        for path in ('docs/devloop/guide.md', 'docs/devloop/roles/coordinator.md',
                     'docs/devloop/roles/supervisor.md'):
            with self.subTest(path=path):
                text = ' '.join((ROOT / path).read_text(encoding='utf-8').split())
                for phrase in ('/devloop authorize', NEEDS, '#607'):
                    self.assertIn(phrase, text)
        script = (ROOT / 'ops/host/role-wait.sh').read_text(encoding='utf-8')
        self.assertIn(NEEDS, script)


if __name__ == '__main__':
    unittest.main()
